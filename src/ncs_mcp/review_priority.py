from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES = [
    "hr_training_goal_link_human_review_required",
    "ontology_training_goal_link_human_review_required",
    "ontology_task_ksa_relation_human_review_required",
    "hr_core_concept_human_review_required",
    "ontology_core_concept_human_review_required",
    "criteria_format_issue",
    "api_element_unmatched",
    "api_value_mismatch",
    "api_element_value_mismatch",
    "suspected_typo",
]

ISSUE_TYPE_WEIGHTS = {
    "hr_training_goal_link_human_review_required": 100,
    "ontology_training_goal_link_human_review_required": 95,
    "ontology_task_ksa_relation_human_review_required": 90,
    "hr_core_concept_human_review_required": 85,
    "ontology_core_concept_human_review_required": 80,
    "criteria_format_issue": 60,
    "api_element_unmatched": 55,
    "api_value_mismatch": 50,
    "api_element_value_mismatch": 50,
    "suspected_typo": 40,
}

SEVERITY_WEIGHTS = {
    "error": 40,
    "high": 30,
    "warning": 20,
    "medium": 15,
    "info": 5,
}
MAX_REVIEW_TEXT_CHARS = 900
MAX_REVIEW_PRIORITY_ITEMS = 200
MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE = 50


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _trim_text(value: str, *, max_chars: int = MAX_REVIEW_TEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "... [truncated]"


def _trim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    truncated_fields: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str):
            trimmed_value = _trim_text(value)
            trimmed[key] = trimmed_value
            if trimmed_value != value:
                truncated_fields.append(key)
        else:
            trimmed[key] = value
    if truncated_fields:
        trimmed["_truncated_fields"] = truncated_fields
    return trimmed


def _issue_priority_score(issue: dict[str, Any]) -> int:
    return ISSUE_TYPE_WEIGHTS.get(issue["issue_type"], 10) + SEVERITY_WEIGHTS.get(
        issue["severity"],
        0,
    )


def _priority_reason(issue: dict[str, Any]) -> str:
    issue_type = issue["issue_type"]
    if "training_goal_link" in issue_type:
        return "Training-goal concept links directly affect recommendation ranking and explanations."
    if "task_ksa_relation" in issue_type:
        return "Task-KSA relations affect transfer and gap reasoning."
    if "core_concept" in issue_type:
        return "Core ontology concepts affect many downstream KSA explanations."
    if issue_type == "criteria_format_issue":
        return "Criteria text quality affects task evidence shown to users."
    if issue_type.startswith("api_"):
        return "API mismatches affect NCS scope and evidence trust."
    if issue_type == "suspected_typo":
        return "Typos affect query matching and user-visible evidence quality."
    return "Open quality issue selected for review."


def _context_for_issue(conn: sqlite3.Connection, issue: dict[str, Any]) -> dict[str, Any]:
    target_type = issue["target_type"]
    if target_type == "unit":
        return _row_dict(
            conn.execute(
                """
                SELECT unit_code, unit_name_raw, api_unit_name, api_match_status
                FROM competency_units
                WHERE unit_code = ?
                """,
                (str(issue["target_id"]),),
            ).fetchone()
        )

    target_id = _as_int(issue["target_id"])
    if target_id is None:
        return {}

    if target_type == "ontology_concept":
        return _row_dict(
            conn.execute(
                """
                SELECT concept_id, concept_name, concept_type,
                       definition_status, relation_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "training_goal_concept_link":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    gl.link_id, gl.link_method, gl.confidence_score, gl.review_status,
                    tc.training_course_id, tc.compe_unit_name, tc.ncs_cl_cd,
                    tc.train_goal, tc.train_time, tc.meth_name,
                    oc.concept_id, oc.concept_name, oc.concept_type,
                    oc.definition_status AS concept_definition_status,
                    oc.review_status AS concept_review_status
                FROM training_goal_concept_links gl
                JOIN ncs_training_courses tc
                  ON tc.training_course_id = gl.training_course_id
                JOIN ontology_concepts oc
                  ON oc.concept_id = gl.concept_id
                WHERE gl.link_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "task_ksa_concept_relation":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    r.relation_id, r.relation_type, r.confidence_score, r.review_status,
                    pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                    ce.element_id, ce.element_name_raw,
                    cu.unit_code, cu.unit_name_raw,
                    source.concept_name AS source_concept_name,
                    source.concept_type AS source_concept_type,
                    target.concept_name AS target_concept_name,
                    target.concept_type AS target_concept_type
                FROM task_ksa_concept_relations r
                JOIN performance_criteria pc ON pc.criteria_id = r.criteria_id
                JOIN competency_elements ce ON ce.element_id = r.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN ontology_concepts source ON source.concept_id = r.source_concept_id
                JOIN ontology_concepts target ON target.concept_id = r.target_concept_id
                WHERE r.relation_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "criteria":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                    pc.criteria_text_refined, pc.review_status,
                    ce.element_id, ce.element_name_raw,
                    cu.unit_code, cu.unit_name_raw
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE pc.criteria_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "ksa":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    ki.ksa_id, ki.ksa_type_name, ki.ksa_no, ki.ksa_text_raw,
                    ki.ksa_text_refined, ki.review_status,
                    ce.element_id, ce.element_name_raw,
                    cu.unit_code, cu.unit_name_raw
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ki.ksa_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "element":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    ce.element_id, ce.element_code_raw, ce.element_name_raw,
                    ce.api_match_status, cu.unit_code, cu.unit_name_raw
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ce.element_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    return {}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _open_issue_counts(conn: sqlite3.Connection, issue_types: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in issue_types)
    return [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT issue_type, severity, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
              AND issue_type IN ({placeholders})
            GROUP BY issue_type, severity
            ORDER BY issue_type, severity
            """,
            issue_types,
        ).fetchall()
    ]


def review_priority_summary(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    selected_issue_types = issue_types or DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES
    max_items = max(1, min(int(limit or 20), MAX_REVIEW_PRIORITY_ITEMS))
    max_per_type = max(1, min(int(per_issue_type_limit or 5), MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE))
    placeholders = ",".join("?" for _ in selected_issue_types)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT issue_id, target_type, target_id, issue_type, severity,
                   issue_detail, suggested_action, detected_at
            FROM quality_issues
            WHERE resolved_at IS NULL
              AND issue_type IN ({placeholders})
            """,
            selected_issue_types,
        ).fetchall()
    ]
    items: list[dict[str, Any]] = []
    for issue in rows:
        priority_score = _issue_priority_score(issue)
        items.append(
            {
                "priority_score": priority_score,
                "priority_reason": _priority_reason(issue),
                "issue": _trim_payload(issue),
                "context": _trim_payload(_context_for_issue(conn, issue)),
            }
        )
    items.sort(
        key=lambda item: (
            -item["priority_score"],
            item["issue"]["issue_type"],
            item["issue"]["issue_id"],
        )
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    capped_items: list[dict[str, Any]] = []
    seen_target_signatures: set[tuple[str, str, str]] = set()
    duplicate_target_items_skipped = 0
    for item in items:
        issue_type = item["issue"]["issue_type"]
        target_signature = (
            str(issue_type),
            str(item["issue"].get("target_type") or ""),
            str(item["issue"].get("target_id") or ""),
        )
        if target_signature in seen_target_signatures:
            duplicate_target_items_skipped += 1
            continue
        bucket = groups.setdefault(issue_type, [])
        if len(bucket) < max_per_type:
            bucket.append(item)
            capped_items.append(item)
            seen_target_signatures.add(target_signature)
    capped_items.sort(
        key=lambda item: (
            -item["priority_score"],
            item["issue"]["issue_type"],
            item["issue"]["issue_id"],
        )
    )

    return {
        "ok": True,
        "issue_types": selected_issue_types,
        "open_issue_counts": _open_issue_counts(conn, selected_issue_types),
        "top_items": capped_items[:max_items],
        "groups": groups,
        "duplicate_target_items_skipped": duplicate_target_items_skipped,
        "next_actions": [
            "Review training-goal links before broad criteria cleanup because they affect ranking evidence directly.",
            "Mark only human-verified concepts or links as human_reviewed; preserve raw source text.",
            "Use prepare-*-review-queue --dry-run before changing queue caps.",
        ],
    }


def review_priority_summary_from_db(
    db_path: Path,
    *,
    limit: int = 20,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return review_priority_summary(
            conn,
            limit=limit,
            per_issue_type_limit=per_issue_type_limit,
            issue_types=issue_types,
        )
    finally:
        conn.close()


def write_review_priority_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# NCS Review Priority",
        "",
        f"- ok: {report.get('ok')}",
        f"- issue_types: {', '.join(report.get('issue_types') or [])}",
        f"- duplicate_target_items_skipped: {report.get('duplicate_target_items_skipped', 0)}",
        "",
        "## Open Issue Counts",
        "",
    ]
    for row in report.get("open_issue_counts", []):
        lines.append(
            f"- {row.get('issue_type')} / {row.get('severity')}: {row.get('count')}"
        )
    lines.extend(["", "## Top Items", ""])
    for item in report.get("top_items", []):
        issue = item.get("issue") or {}
        context = item.get("context") or {}
        lines.extend(
            [
                f"### {issue.get('issue_type')} #{issue.get('issue_id')}",
                "",
                f"- priority_score: {item.get('priority_score')}",
                f"- reason: {item.get('priority_reason')}",
                f"- target: {issue.get('target_type')}:{issue.get('target_id')}",
                f"- detail: {issue.get('issue_detail')}",
                f"- suggested_action: {issue.get('suggested_action')}",
            ]
        )
        if issue.get("_truncated_fields"):
            lines.append(f"- truncated_issue_fields: {', '.join(issue['_truncated_fields'])}")
        label_parts = [
            context.get("compe_unit_name"),
            context.get("unit_name_raw"),
            context.get("unit_name"),
            context.get("concept_name"),
            context.get("criteria_text_raw"),
        ]
        label = next((str(part) for part in label_parts if part), None)
        if label:
            lines.append(f"- context: {label}")
        if context.get("_truncated_fields"):
            lines.append(f"- truncated_context_fields: {', '.join(context['_truncated_fields'])}")
        lines.append("")
    lines.extend(["## Next Actions", ""])
    for action in report.get("next_actions", []):
        lines.append(f"- {action}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
