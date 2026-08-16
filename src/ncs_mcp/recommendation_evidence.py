from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.db import clamp_limit, rows_to_dicts


GOAL_LINK_METHOD_PRIORITY = {
    "training_goal_concept_text": 0,
    "training_goal_concept_token": 1,
    "training_goal_element_implied_concept": 2,
}


def _json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _training_course_id_from_payload(value: str | None) -> int | None:
    payload = _json_loads(value)
    course = payload.get("training_course")
    if not isinstance(course, dict):
        return None
    raw_id = course.get("training_course_id")
    try:
        return int(raw_id)
    except (TypeError, ValueError):
        return None


def _goal_link_candidates(
    conn: sqlite3.Connection,
    *,
    training_course_id: int,
    concept_name: str,
) -> list[dict[str, Any]]:
    candidates = rows_to_dicts(
        conn.execute(
            """
            SELECT
                gl.link_id,
                gl.training_course_id,
                gl.unit_code,
                gl.concept_id,
                gl.link_method,
                gl.confidence_score,
                gl.evidence_text,
                oc.concept_name
            FROM training_goal_concept_links gl
            JOIN ontology_concepts oc ON oc.concept_id = gl.concept_id
            WHERE gl.training_course_id = ?
              AND oc.concept_name = ?
            """,
            (training_course_id, concept_name),
        ).fetchall()
    )
    candidates.sort(
        key=lambda item: (
            -float(item.get("confidence_score") or 0),
            GOAL_LINK_METHOD_PRIORITY.get(str(item.get("link_method") or ""), 99),
            int(item.get("link_id") or 0),
        )
    )
    return candidates


def _orphan_training_goal_evidence_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT
                e.evidence_id,
                e.run_id,
                e.item_id,
                e.source_id,
                e.evidence_type,
                e.evidence_text,
                e.evidence_summary,
                e.created_at,
                i.rank,
                i.learn_module_name,
                i.recommendation_payload,
                r.query
            FROM education_recommendation_evidence e
            JOIN education_recommendation_items i ON i.item_id = e.item_id
            JOIN education_recommendation_runs r ON r.run_id = e.run_id
            LEFT JOIN training_goal_concept_links gl
              ON CAST(gl.link_id AS TEXT) = CAST(e.source_id AS TEXT)
            WHERE e.source_table = 'training_goal_concept_links'
              AND TRIM(COALESCE(e.source_id, '')) <> ''
              AND gl.link_id IS NULL
            ORDER BY e.evidence_id
            """
        ).fetchall()
    )


def recommendation_evidence_hygiene_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
) -> dict[str, Any]:
    max_rows = clamp_limit(limit, default=50, maximum=500)
    training_goal_evidence_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM education_recommendation_evidence
            WHERE source_table = 'training_goal_concept_links'
              AND TRIM(COALESCE(source_id, '')) <> ''
            """
        ).fetchone()[0]
    )
    orphan_rows = _orphan_training_goal_evidence_rows(conn)
    assigned_by_item_summary: dict[tuple[int, str], set[int]] = {}
    remap_updates: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    ambiguous_count = 0
    for row in orphan_rows:
        concept_name = str(row.get("evidence_summary") or "").strip()
        training_course_id = _training_course_id_from_payload(row.get("recommendation_payload"))
        candidates: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        if training_course_id is not None and concept_name:
            candidates = _goal_link_candidates(
                conn,
                training_course_id=training_course_id,
                concept_name=concept_name,
            )
            if len(candidates) > 1:
                ambiguous_count += 1
            used = assigned_by_item_summary.setdefault((int(row["item_id"]), concept_name), set())
            selected = next((candidate for candidate in candidates if int(candidate["link_id"]) not in used), None)
            if selected is None and candidates:
                selected = candidates[0]
            if selected is not None:
                used.add(int(selected["link_id"]))
        base = {
            "evidence_id": row.get("evidence_id"),
            "run_id": row.get("run_id"),
            "item_id": row.get("item_id"),
            "rank": row.get("rank"),
            "query": row.get("query"),
            "course_name": row.get("learn_module_name"),
            "training_course_id": training_course_id,
            "old_source_id": row.get("source_id"),
            "concept_name": concept_name,
            "candidate_count": len(candidates),
        }
        if selected:
            update = {
                **base,
                "new_source_id": str(selected.get("link_id")),
                "link_method": selected.get("link_method"),
                "confidence_score": selected.get("confidence_score"),
            }
            remap_updates.append(update)
        else:
            unresolved.append(base)
        if len(samples) < max_rows:
            samples.append(
                {
                    **base,
                    "selected_source_id": str(selected.get("link_id")) if selected else None,
                    "candidates": candidates[:5],
                }
            )
    return {
        "ok": True,
        "mode": "dry_run",
        "training_goal_link_evidence_count": training_goal_evidence_count,
        "orphan_training_goal_link_evidence_count": len(orphan_rows),
        "resolvable_count": len(remap_updates),
        "unresolved_count": len(unresolved),
        "ambiguous_candidate_count": ambiguous_count,
        "remap_updates": remap_updates,
        "unresolved": unresolved,
        "samples": samples,
    }


def apply_recommendation_evidence_hygiene(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    max_updates: int | None = None,
) -> dict[str, Any]:
    if max_updates is not None and max_updates < 0:
        raise ValueError("max_updates must be non-negative when provided.")
    before = recommendation_evidence_hygiene_report(conn, limit=limit)
    updates = list(before.get("remap_updates") or [])
    if max_updates is not None:
        updates = updates[:max_updates]
    updated_count = 0
    for update in updates:
        conn.execute(
            """
            UPDATE education_recommendation_evidence
            SET source_id = ?
            WHERE evidence_id = ?
              AND source_table = 'training_goal_concept_links'
            """,
            (str(update["new_source_id"]), update["evidence_id"]),
        )
        updated_count += 1
    conn.commit()
    after = recommendation_evidence_hygiene_report(conn, limit=limit)
    return {
        "ok": True,
        "mode": "applied",
        "updated_evidence_count": updated_count,
        "max_updates": max_updates,
        "before": before,
        "after": after,
    }


def write_recommendation_evidence_hygiene_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = ["# Recommendation Evidence Hygiene", ""]
    if report.get("mode") == "applied":
        before = report.get("before") or {}
        after = report.get("after") or {}
        lines.extend(
            [
                "- mode: applied",
                f"- updated_evidence_count: {report.get('updated_evidence_count')}",
                f"- max_updates: {report.get('max_updates')}",
                "",
                "## Before",
                "",
                f"- training_goal_link_evidence_count: {before.get('training_goal_link_evidence_count')}",
                f"- orphan_training_goal_link_evidence_count: {before.get('orphan_training_goal_link_evidence_count')}",
                f"- resolvable_count: {before.get('resolvable_count')}",
                f"- unresolved_count: {before.get('unresolved_count')}",
                "",
                "## After",
                "",
                f"- training_goal_link_evidence_count: {after.get('training_goal_link_evidence_count')}",
                f"- orphan_training_goal_link_evidence_count: {after.get('orphan_training_goal_link_evidence_count')}",
                f"- resolvable_count: {after.get('resolvable_count')}",
                f"- unresolved_count: {after.get('unresolved_count')}",
            ]
        )
    else:
        lines.extend(
            [
                "- mode: dry_run",
                f"- training_goal_link_evidence_count: {report.get('training_goal_link_evidence_count')}",
                f"- orphan_training_goal_link_evidence_count: {report.get('orphan_training_goal_link_evidence_count')}",
                f"- resolvable_count: {report.get('resolvable_count')}",
                f"- unresolved_count: {report.get('unresolved_count')}",
                f"- ambiguous_candidate_count: {report.get('ambiguous_candidate_count')}",
            ]
        )
    samples = report.get("samples") or (report.get("before") or {}).get("samples") or []
    lines.extend(["", "## Samples", ""])
    for item in samples[:50]:
        lines.append(
            f"- evidence_id {item.get('evidence_id')}: {item.get('old_source_id')} -> "
            f"{item.get('selected_source_id') or item.get('new_source_id') or 'unresolved'} "
            f"({item.get('concept_name') or ''})"
        )
    lines.extend(
        [
            "",
            "## Policy",
            "",
            "- This operation does not call external APIs.",
            "- It updates only saved recommendation evidence `source_id` values that can be remapped to current training-goal concept links.",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
