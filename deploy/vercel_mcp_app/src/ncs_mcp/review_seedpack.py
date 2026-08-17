from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from ncs_mcp.db import clamp_limit, now_utc
from ncs_mcp.review_priority import (
    DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES,
    MAX_REVIEW_PRIORITY_ITEMS,
    MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE,
    review_priority_summary,
)
from ncs_mcp.review_safety import neutralize_suggested_action
from ncs_mcp.training_recommendation import evaluate_training_transition_scenarios


SEEDPACK_FORMAT_VERSION = "ncs-review-seedpack-v1"
TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION = "ncs-transition-scenario-review-v1"
ALLOWED_DECISIONS = ["approve", "reject", "defer"]
ALLOWED_TRANSITION_SCENARIO_REVIEW_STATUSES = [
    "candidate",
    "candidate_auto",
    "human_reviewed",
    "reviewed",
    "accepted",
    "rejected",
]
ALLOWED_PROPOSED_TRANSITION_SCENARIO_REVIEW_STATUSES = [
    "",
    "candidate",
    "candidate_auto",
    "human_reviewed",
    "reviewed",
    "accepted",
    "rejected",
]
TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES = {"human_reviewed", "reviewed", "accepted"}
MAX_CONTEXT_EXCERPT_CHARS = 900
REVIEW_SEEDPACK_CSV_FIELDS = [
    "sequence",
    "issue_type",
    "target_type",
    "target_id",
    "current_review_status",
    "priority_score",
    "priority_reason",
    "source_context_excerpt",
    "suggested_action",
    "issue_detail",
    "decision",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "human_decision_required",
    "status_update_allowed",
    "db_writes",
    "approval_claim",
    "proposed_target_review_status",
    "proposed_issue_resolution",
    "target_snapshot_hash",
    "seedpack_id",
]


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _content_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _seedpack_id_from_timestamp(
    exported_at: str,
    discriminator: Any | None = None,
    *,
    prefix: str = "review-seedpack",
) -> str:
    compact = exported_at
    if compact.endswith("+00:00"):
        compact = compact[:-6] + "Z"
    compact = compact.replace(":", "").replace("+", "Z")
    suffix = f"-{_content_hash(discriminator)[:10]}" if discriminator is not None else ""
    return f"{prefix}-{compact}{suffix}"


def _trim_text(value: str, *, max_chars: int = MAX_CONTEXT_EXCERPT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "... [truncated]"


def _context_excerpt(context: dict[str, Any]) -> str:
    preferred_keys = [
        "compe_unit_name",
        "train_goal",
        "criteria_text_raw",
        "concept_name",
        "unit_name_raw",
        "unit_name",
        "source_concept_name",
        "target_concept_name",
    ]
    parts = [str(context[key]) for key in preferred_keys if context.get(key)]
    if not parts:
        parts = [str(value) for value in context.values() if isinstance(value, str) and value.strip()]
    return _trim_text(" | ".join(parts))


def _current_review_status(issue: dict[str, Any], context: dict[str, Any]) -> str | None:
    target_type = str(issue.get("target_type") or "")
    if target_type == "training_goal_concept_link":
        return context.get("review_status")
    if target_type == "task_ksa_concept_relation":
        return context.get("review_status")
    if target_type == "ontology_concept":
        return context.get("review_status")
    if target_type in {"criteria", "ksa", "element", "unit"}:
        return context.get("review_status") or context.get("api_match_status")
    return context.get("review_status")


def _safe_review_context(context: dict[str, Any], issue: dict[str, Any]) -> dict[str, Any]:
    safe_context = dict(context)
    if "suggested_action" in safe_context:
        safe_context["suggested_action"] = neutralize_suggested_action(
            safe_context.get("suggested_action"),
            issue_type=issue.get("issue_type"),
            target_type=issue.get("target_type"),
        )
    return safe_context


def _seedpack_item(seedpack_id: str, sequence: int, item: dict[str, Any]) -> dict[str, Any]:
    issue = item.get("issue") or {}
    context = item.get("context") or {}
    safe_context = _safe_review_context(context, issue)
    safe_suggested_action = neutralize_suggested_action(
        issue.get("suggested_action"),
        issue_type=issue.get("issue_type"),
        target_type=issue.get("target_type"),
    )
    safe_issue = dict(issue)
    safe_issue["suggested_action"] = safe_suggested_action
    snapshot_payload = {
        "issue_id": issue.get("issue_id"),
        "issue_type": issue.get("issue_type"),
        "target_type": issue.get("target_type"),
        "target_id": issue.get("target_id"),
        "current_review_status": _current_review_status(issue, safe_context),
        "context": safe_context,
    }
    return {
        "record_type": "review_item",
        "format_version": SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "sequence": sequence,
        "priority_score": item.get("priority_score"),
        "priority_reason": item.get("priority_reason"),
        "issue_id": issue.get("issue_id"),
        "issue_type": issue.get("issue_type"),
        "target_type": issue.get("target_type"),
        "target_id": str(issue.get("target_id")),
        "target_snapshot_hash": _content_hash(snapshot_payload),
        "current_review_status": snapshot_payload["current_review_status"],
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        "proposed_target_review_status": "",
        "proposed_issue_resolution": "",
        "issue_detail": issue.get("issue_detail"),
        "suggested_action": safe_suggested_action,
        "source_context_excerpt": _context_excerpt(safe_context),
        "issue": safe_issue,
        "context": safe_context,
    }


def _db_fingerprint(conn: sqlite3.Connection, item_records: list[dict[str, Any]]) -> str:
    status_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT issue_type, severity, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY issue_type, severity
            ORDER BY issue_type, severity
            """
        ).fetchall()
    ]
    max_issue_id = conn.execute("SELECT MAX(issue_id) FROM quality_issues").fetchone()[0]
    return _content_hash(
        {
            "max_issue_id": max_issue_id,
            "open_issue_counts": status_rows,
            "selected": [
                {
                    "issue_id": item.get("issue_id"),
                    "target_type": item.get("target_type"),
                    "target_id": item.get("target_id"),
                    "snapshot": item.get("target_snapshot_hash"),
                }
                for item in item_records
            ],
        }
    )


def export_review_seedpack(
    conn: sqlite3.Connection,
    *,
    out_path: Path,
    limit: int = 50,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
    source_report_path: str | None = None,
    selection_command: str | None = None,
) -> dict[str, Any]:
    selected_issue_types = issue_types or DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES
    max_items = clamp_limit(limit, default=50, maximum=MAX_REVIEW_PRIORITY_ITEMS)
    max_per_type = clamp_limit(
        per_issue_type_limit,
        default=5,
        maximum=MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE,
    )
    exported_at = now_utc()
    seedpack_id = _seedpack_id_from_timestamp(
        exported_at,
        {
            "out_path": str(out_path),
            "issue_types": selected_issue_types,
            "limit": max_items,
            "per_issue_type_limit": max_per_type,
            "source_report_path": source_report_path,
            "selection_command": selection_command,
        },
    )
    priority = review_priority_summary(
        conn,
        limit=max_items,
        per_issue_type_limit=max_per_type,
        issue_types=selected_issue_types,
    )
    item_records = [
        _seedpack_item(seedpack_id, index, item)
        for index, item in enumerate(priority.get("top_items", []), start=1)
    ]
    batch_record = {
        "record_type": "batch",
        "format_version": SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "exported_at": exported_at,
        "db_fingerprint": _db_fingerprint(conn, item_records),
        "selection_command": selection_command,
        "source_report_path": source_report_path,
        "encoding": "utf-8",
        "allowed_decisions": ALLOWED_DECISIONS,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "trusted_status_write_allowed": False,
        "raw_source_mutation_allowed": False,
        "issue_types": selected_issue_types,
        "limit": max_items,
        "per_issue_type_limit": max_per_type,
        "item_count": len(item_records),
        "open_issue_counts": priority.get("open_issue_counts", []),
        "notes": [
            "This seedpack is an export-only human review artifact.",
            "Do not infer approve/reject/defer from confidence or ranking.",
            "Raw source fields must remain unchanged.",
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in [batch_record, *item_records]:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "ok": True,
        "format_version": SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "out_path": str(out_path),
        "item_count": len(item_records),
        "issue_types": selected_issue_types,
        "allowed_decisions": ALLOWED_DECISIONS,
        "db_fingerprint": batch_record["db_fingerprint"],
        "source_report_path": source_report_path,
        "selection_command": selection_command,
    }


def export_review_seedpack_from_db(
    db_path: Path,
    *,
    out_path: Path,
    limit: int = 50,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
    source_report_path: str | None = None,
    selection_command: str | None = None,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return export_review_seedpack(
            conn,
            out_path=out_path,
            limit=limit,
            per_issue_type_limit=per_issue_type_limit,
            issue_types=issue_types,
            source_report_path=source_report_path,
            selection_command=selection_command,
        )
    finally:
        conn.close()


def _transition_scenario_rows(
    conn: sqlite3.Connection,
    *,
    review_statuses: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    placeholders = ",".join("?" for _ in review_statuses)
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for status in review_statuses:
        if len(selected) >= limit:
            break
        row = conn.execute(
            """
            SELECT *
            FROM training_transition_gold_scenarios
            WHERE review_status = ?
            ORDER BY scenario_id
            LIMIT 1
            """,
            (status,),
        ).fetchone()
        if row is None:
            continue
        item = dict(row)
        seen_ids.add(int(item["scenario_id"]))
        selected.append(item)
    remaining = limit - len(selected)
    if remaining <= 0:
        return selected
    rows = conn.execute(
        f"""
        SELECT *
        FROM training_transition_gold_scenarios
        WHERE review_status IN ({placeholders})
        ORDER BY scenario_id
        LIMIT ?
        """,
        (*review_statuses, limit + len(selected)),
    ).fetchall()
    for row in rows:
        item = dict(row)
        scenario_id = int(item["scenario_id"])
        if scenario_id in seen_ids:
            continue
        selected.append(item)
        seen_ids.add(scenario_id)
        if len(selected) >= limit:
            break
    return selected


def _transition_seedpack_item(
    seedpack_id: str,
    sequence: int,
    scenario: dict[str, Any],
    case: dict[str, Any],
) -> dict[str, Any]:
    scenario_label = scenario.get("scenario_name") or scenario.get("scenario_id") or "unknown"
    try:
        expected_courses = json.loads(scenario.get("expected_course_names_json") or "[]")
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid expected_course_names_json for transition scenario {scenario_label}: {exc}"
        ) from exc
    if not isinstance(expected_courses, list):
        raise ValueError(
            f"Invalid expected_course_names_json for transition scenario {scenario_label}: expected a list"
        )
    snapshot_payload = {
        "scenario_id": scenario.get("scenario_id"),
        "scenario_name": scenario.get("scenario_name"),
        "review_status": scenario.get("review_status"),
        "current_query": scenario.get("current_query"),
        "target_query": scenario.get("target_query"),
        "expected_course_names_json": scenario.get("expected_course_names_json"),
        "case": case,
    }
    return {
        "record_type": "transition_scenario_review_item",
        "format_version": TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "sequence": sequence,
        "scenario_id": scenario.get("scenario_id"),
        "scenario_name": scenario.get("scenario_name"),
        "current_review_status": scenario.get("review_status"),
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        "proposed_review_status": "",
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "target_snapshot_hash": _content_hash(snapshot_payload),
        "current_query": scenario.get("current_query"),
        "target_query": scenario.get("target_query"),
        "major_code": scenario.get("major_code"),
        "expected_current_match_text": scenario.get("expected_current_match_text"),
        "expected_target_match_text": scenario.get("expected_target_match_text"),
        "expected_courses": expected_courses,
        "current_match": case.get("current_match"),
        "target_match": case.get("target_match"),
        "expected_course_hits": case.get("expected_course_hits", []),
        "recommended_courses": case.get("recommended_courses", []),
        "recommended_course_evidence": case.get("recommended_course_evidence", []),
        "recommended_course_scope_summary": case.get("recommended_course_scope_summary", {}),
        "expected_recall_at_k": case.get("expected_recall_at_k"),
        "precision_at_k": case.get("precision_at_k"),
        "top1_expected_hit": case.get("top1_expected_hit"),
        "evaluation_case_missing": not bool(case),
        "source_context_excerpt": _trim_text(
            " | ".join(
                str(part)
                for part in [
                    scenario.get("current_query"),
                    scenario.get("target_query"),
                    ", ".join(expected_courses),
                ]
                if part
            )
        ),
        "scenario": scenario,
        "evaluation_case": case,
    }


def export_transition_scenario_seedpack(
    conn: sqlite3.Connection,
    *,
    out_path: Path,
    review_statuses: list[str] | None = None,
    scenario_limit: int = 20,
    recommendation_limit: int = 5,
    source_report_path: str | None = None,
    selection_command: str | None = None,
) -> dict[str, Any]:
    selected_statuses = review_statuses or ["candidate", "candidate_auto"]
    invalid = sorted(set(selected_statuses) - set(ALLOWED_TRANSITION_SCENARIO_REVIEW_STATUSES))
    if invalid:
        raise ValueError(
            "Unsupported transition scenario review status: "
            + ", ".join(invalid)
            + ". Allowed values: "
            + ", ".join(ALLOWED_TRANSITION_SCENARIO_REVIEW_STATUSES)
        )
    max_scenarios = clamp_limit(scenario_limit, default=20, maximum=200)
    max_recommendations = clamp_limit(recommendation_limit, default=5, maximum=50)
    exported_at = now_utc()
    seedpack_id = _seedpack_id_from_timestamp(
        exported_at,
        {
            "out_path": str(out_path),
            "review_statuses": selected_statuses,
            "scenario_limit": max_scenarios,
            "recommendation_limit": max_recommendations,
            "source_report_path": source_report_path,
            "selection_command": selection_command,
        },
        prefix="transition-scenario-seedpack",
    )
    scenarios = _transition_scenario_rows(
        conn,
        review_statuses=selected_statuses,
        limit=max_scenarios,
    )
    actual_review_status_counts = dict(
        sorted(Counter(str(row.get("review_status") or "unknown") for row in scenarios).items())
    )
    missing_requested_review_statuses = [
        status for status in selected_statuses if actual_review_status_counts.get(status, 0) == 0
    ]
    trusted_review_status_count = sum(
        count
        for status, count in actual_review_status_counts.items()
        if status in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES
    )
    evaluation = evaluate_training_transition_scenarios(
        conn,
        limit=max_recommendations,
        scenario_limit=max_scenarios,
        review_statuses=selected_statuses,
        scenario_ids=[int(item["scenario_id"]) for item in scenarios],
    )
    cases_by_id = {
        int(case["scenario_id"]): case
        for case in evaluation.get("cases", [])
        if isinstance(case, dict) and case.get("scenario_id") is not None
    }
    missing_evaluation_scenario_ids: list[int] = []
    item_records = [
        _transition_seedpack_item(
            seedpack_id,
            index,
            scenario,
            cases_by_id.get(int(scenario["scenario_id"]), {}),
        )
        for index, scenario in enumerate(scenarios, start=1)
    ]
    missing_evaluation_scenario_ids = [
        int(item["scenario_id"])
        for item in item_records
        if item.get("evaluation_case_missing")
    ]
    evaluation_summary = {
        key: value
        for key, value in evaluation.items()
        if key != "cases"
    }
    batch_record = {
        "record_type": "batch",
        "format_version": TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "exported_at": exported_at,
        "selection_command": selection_command,
        "source_report_path": source_report_path,
        "encoding": "utf-8",
        "allowed_decisions": ALLOWED_DECISIONS,
        "allowed_proposed_review_statuses": ["", "candidate", "candidate_auto", "rejected"],
        "trusted_review_statuses_hidden_until_guarded_apply": sorted(TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES),
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "review_statuses": selected_statuses,
        "actual_review_status_counts": actual_review_status_counts,
        "missing_requested_review_statuses": missing_requested_review_statuses,
        "trusted_review_status_count": trusted_review_status_count,
        "scenario_limit": max_scenarios,
        "recommendation_limit": max_recommendations,
        "item_count": len(item_records),
        "missing_evaluation_scenario_ids": missing_evaluation_scenario_ids,
        "evaluation_summary": evaluation_summary,
        "db_fingerprint": _content_hash(
            {
                "selected": [
                    {
                        "scenario_id": item.get("scenario_id"),
                        "scenario_name": item.get("scenario_name"),
                        "snapshot": item.get("target_snapshot_hash"),
                    }
                    for item in item_records
                ],
                "evaluation_summary": evaluation_summary,
                "selection_snapshot": {
                    "review_statuses": selected_statuses,
                    "actual_review_status_counts": actual_review_status_counts,
                    "missing_requested_review_statuses": missing_requested_review_statuses,
                    "trusted_review_status_count": trusted_review_status_count,
                    "missing_evaluation_scenario_ids": missing_evaluation_scenario_ids,
                },
            }
        ),
        "notes": [
            "This seedpack is an export-only human review artifact for transition scenarios.",
            "Use the decision fields only as reviewer input; no DB status update is allowed from this export.",
            "Do not treat candidate or candidate_auto scenarios as trusted readiness without a separate guarded human-decision apply workflow.",
        ],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in [batch_record, *item_records]:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return {
        "ok": True,
        "format_version": TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
        "seedpack_id": seedpack_id,
        "out_path": str(out_path),
        "item_count": len(item_records),
        "review_statuses": selected_statuses,
        "actual_review_status_counts": actual_review_status_counts,
        "missing_requested_review_statuses": missing_requested_review_statuses,
        "trusted_review_status_count": trusted_review_status_count,
        "allowed_decisions": ALLOWED_DECISIONS,
        "allowed_proposed_review_statuses": batch_record["allowed_proposed_review_statuses"],
        "trusted_review_statuses_hidden_until_guarded_apply": batch_record[
            "trusted_review_statuses_hidden_until_guarded_apply"
        ],
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "evaluation_summary": evaluation_summary,
        "missing_evaluation_scenario_ids": missing_evaluation_scenario_ids,
        "db_fingerprint": batch_record["db_fingerprint"],
        "source_report_path": source_report_path,
        "selection_command": selection_command,
    }


def export_transition_scenario_seedpack_from_db(
    db_path: Path,
    *,
    out_path: Path,
    review_statuses: list[str] | None = None,
    scenario_limit: int = 20,
    recommendation_limit: int = 5,
    source_report_path: str | None = None,
    selection_command: str | None = None,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return export_transition_scenario_seedpack(
            conn,
            out_path=out_path,
            review_statuses=review_statuses,
            scenario_limit=scenario_limit,
            recommendation_limit=recommendation_limit,
            source_report_path=source_report_path,
            selection_command=selection_command,
        )
    finally:
        conn.close()


def write_review_seedpack_markdown(summary: dict[str, Any], seedpack_path: Path, out_path: Path) -> None:
    batch_record: dict[str, Any] = {}
    item_records: list[dict[str, Any]] = []
    if seedpack_path.exists():
        batch_record, item_records = _load_review_seedpack_records(seedpack_path)
    issue_type_counts = Counter(str(item.get("issue_type") or "unknown") for item in item_records)
    review_status_counts = Counter(
        str(item.get("current_review_status") or "unknown") for item in item_records
    )
    lines = [
        "# NCS Review Seedpack",
        "",
        f"- seedpack_id: {summary.get('seedpack_id')}",
        f"- format_version: {summary.get('format_version')}",
        f"- item_count: {summary.get('item_count')}",
        f"- seedpack_path: {seedpack_path}",
        f"- source_report_path: {summary.get('source_report_path') or ''}",
        f"- selection_command: {summary.get('selection_command') or ''}",
        f"- db_fingerprint: {summary.get('db_fingerprint')}",
        f"- allowed_decisions: {', '.join(summary.get('allowed_decisions') or [])}",
        "",
        "## Review Rules",
        "",
        "- Fill `decision` with only `approve`, `reject`, or `defer`.",
        "- Fill `reviewer_id`, `reviewed_at`, and `rationale` for every decision.",
        "- `approve` is reviewer input only; it is not an approval claim and does not update DB status.",
        "- Do not mark any row human-reviewed without explicit human approval.",
        "- Preserve raw source fields; use reviewer-proposed derived fields only for a later guarded apply workflow.",
    ]
    if item_records:
        lines.extend(
            [
                "",
                "## Selection Snapshot",
                "",
                f"- selected_issue_type_counts: {json.dumps(dict(sorted(issue_type_counts.items())), ensure_ascii=False, sort_keys=True)}",
                f"- current_review_status_counts: {json.dumps(dict(sorted(review_status_counts.items())), ensure_ascii=False, sort_keys=True)}",
                f"- open_issue_counts: {json.dumps(batch_record.get('open_issue_counts') or [], ensure_ascii=False, sort_keys=True)}",
                "",
                "## Review Item Preview",
                "",
                "| Seq | Issue Type | Target | Status | Priority | Context | Suggested Action |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in item_records[:20]:
            target = f"{item.get('target_type') or ''}:{item.get('target_id') or ''}"
            lines.append(
                "| "
                + " | ".join(
                    [
                        _markdown_cell(item.get("sequence")),
                        _markdown_cell(item.get("issue_type")),
                        _markdown_cell(target),
                        _markdown_cell(item.get("current_review_status")),
                        _markdown_cell(item.get("priority_score")),
                        _markdown_cell(_trim_text(str(item.get("source_context_excerpt") or ""), max_chars=160)),
                        _markdown_cell(_trim_text(str(item.get("suggested_action") or ""), max_chars=160)),
                    ]
                )
                + " |"
            )
        if len(item_records) > 20:
            lines.append("")
            lines.append(f"Preview truncated to 20 of {len(item_records)} review items.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_review_seedpack_csv(seedpack_path: Path, out_path: Path) -> dict[str, Any]:
    batch_record, item_records = _load_review_seedpack_records(seedpack_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_SEEDPACK_CSV_FIELDS)
        writer.writeheader()
        for item in item_records:
            writer.writerow(
                {
                    field: _csv_cell(item.get(field))
                    for field in REVIEW_SEEDPACK_CSV_FIELDS
                }
            )
    return {
        "csv_path": str(out_path),
        "seedpack_id": batch_record.get("seedpack_id"),
        "item_count": len(item_records),
        "fieldnames": list(REVIEW_SEEDPACK_CSV_FIELDS),
        "encoding": "utf-8-sig",
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def _load_review_seedpack_records(seedpack_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    batch_record: dict[str, Any] = {}
    item_records: list[dict[str, Any]] = []
    for line_number, line in enumerate(seedpack_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("record_type") == "batch":
            batch_record = record
        elif record.get("record_type") == "review_item":
            item_records.append(record)
        else:
            raise ValueError(
                f"Unsupported review seedpack record_type at {seedpack_path}:{line_number}"
            )
    if not batch_record:
        raise ValueError(f"Review seedpack is missing a batch record: {seedpack_path}")
    return batch_record, item_records


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _markdown_cell(value: Any) -> str:
    text = str(value or "")
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def write_transition_scenario_seedpack_markdown(
    summary: dict[str, Any],
    seedpack_path: Path,
    out_path: Path,
) -> None:
    evaluation = summary.get("evaluation_summary") or {}
    lines = [
        "# NCS Transition Scenario Review Seedpack",
        "",
        f"- seedpack_id: {summary.get('seedpack_id')}",
        f"- format_version: {summary.get('format_version')}",
        f"- item_count: {summary.get('item_count')}",
        f"- seedpack_path: {seedpack_path}",
        f"- source_report_path: {summary.get('source_report_path') or ''}",
        f"- selection_command: {summary.get('selection_command') or ''}",
        f"- db_fingerprint: {summary.get('db_fingerprint')}",
        f"- allowed_decisions: {', '.join(summary.get('allowed_decisions') or [])}",
        f"- allowed_proposed_review_statuses: {', '.join(summary.get('allowed_proposed_review_statuses') or [])}",
        f"- trusted_review_statuses_hidden_until_guarded_apply: {', '.join(summary.get('trusted_review_statuses_hidden_until_guarded_apply') or [])}",
        f"- status_update_allowed: {summary.get('status_update_allowed')}",
        f"- db_writes: {summary.get('db_writes')}",
        f"- human_decision_required: {summary.get('human_decision_required')}",
        f"- approval_claim: {summary.get('approval_claim')}",
        "",
        "## Selection Snapshot",
        "",
        f"- requested_review_statuses: {', '.join(summary.get('review_statuses') or [])}",
        f"- actual_review_status_counts: {json.dumps(summary.get('actual_review_status_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- missing_requested_review_statuses: {', '.join(summary.get('missing_requested_review_statuses') or []) or 'none'}",
        f"- trusted_review_status_count: {summary.get('trusted_review_status_count', 0)}",
        f"- missing_evaluation_scenario_ids: {', '.join(str(value) for value in summary.get('missing_evaluation_scenario_ids') or []) or 'none'}",
        "",
        "## Evaluation Snapshot",
        "",
        f"- scenario_count: {evaluation.get('scenario_count')}",
        f"- expected_course_recall_at_k: {evaluation.get('expected_course_recall_at_k')}",
        f"- precision_at_k: {evaluation.get('precision_at_k')}",
        f"- top1_expected_hit_rate: {evaluation.get('top1_expected_hit_rate')}",
        f"- course_scope_relation_counts: {json.dumps(evaluation.get('course_scope_relation_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- course_scope_alignment_counts: {json.dumps(evaluation.get('course_scope_alignment_counts') or {}, ensure_ascii=False, sort_keys=True)}",
        f"- course_scope_review_required_count: {evaluation.get('course_scope_review_required_count', 0)}",
        "",
        "## Review Rules",
        "",
        "- Fill `decision` with only `approve`, `reject`, or `defer`.",
        "- Fill `proposed_review_status` only with one of the listed `allowed_proposed_review_statuses`, or leave it empty.",
        "- Fill `reviewer_id`, `reviewed_at`, and `rationale` for every non-empty `decision`; leave them blank when no human decision is supplied.",
        "- `approve` is reviewer input only; it is not an approval claim and does not update DB status.",
        "- Candidate scenarios are not trusted readiness until a separate guarded human-decision apply workflow confirms them.",
        "- Preserve the original scenario row; do not write review/status fields from this export.",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
