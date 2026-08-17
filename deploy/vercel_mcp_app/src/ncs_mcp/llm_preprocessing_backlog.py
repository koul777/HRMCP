from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LLM_PREPROCESSING_BACKLOG_SCHEMA = "ncs_llm_preprocessing_backlog_map_v1"
LLM_PREPROCESSING_WORK_PLAN_SCHEMA = "ncs_llm_preprocessing_work_plan_v1"
LLM_PREPROCESSING_RUNBOOK_SCHEMA = "ncs_llm_preprocessing_runbook_v1"
TRUSTED_LABEL_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
DEFAULT_SECONDS_PER_ITEM = (3, 5, 10, 20)
SIDECAR_SAFETY_FALSE_FIELDS = ("status_update_allowed", "db_writes", "approval_claim")
SIDECAR_SAFETY_TRUE_FIELDS = ("ok", "report_only")
SIDECAR_OPTIONAL_FALSE_FIELDS = (
    "human_reviewed_written_by_report",
    "accepted_written_by_report",
    "reviewed_written_by_report",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _default_runbook_agent_queue_path(artifact_suffix: str) -> str:
    for part in str(artifact_suffix or "").split("_"):
        if len(part) == 8 and part.isdigit():
            return f"reports\\aihr_agent_queue_{part}.json"
    return f"reports\\aihr_agent_queue_{artifact_suffix}.json"


def _row_dict(row: sqlite3.Row | tuple[Any, ...]) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {key: row[key] for key in row.keys()}
    return {str(index): value for index, value in enumerate(row)}


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def _scalar(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return 0
    value = row[0] if not isinstance(row, sqlite3.Row) else row[0]
    return int(value or 0)


def _count_table(conn: sqlite3.Connection, table_name: str) -> int | None:
    if not _table_exists(conn, table_name):
        return None
    return _scalar(conn, f"SELECT COUNT(*) FROM {table_name}")


def _group_count(
    conn: sqlite3.Connection,
    table_name: str,
    columns: list[str],
    *,
    where_sql: str = "",
    params: tuple[Any, ...] = (),
    limit: int | None = None,
) -> list[dict[str, Any]]:
    if not _table_exists(conn, table_name):
        return []
    column_sql = ", ".join(columns)
    group_sql = ", ".join(str(index) for index in range(1, len(columns) + 1))
    order_sql = "ORDER BY count DESC"
    limit_sql = f"LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"""
        SELECT {column_sql}, COUNT(*) AS count
        FROM {table_name}
        {where_sql}
        GROUP BY {group_sql}
        {order_sql}
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [_row_dict(row) for row in rows]


def _status_count(conn: sqlite3.Connection, table_name: str) -> dict[str, int]:
    rows = _group_count(conn, table_name, ["COALESCE(review_status, '') AS review_status"])
    return {str(row["review_status"]): int(row["count"]) for row in rows}


def _definition_status_count(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _group_count(
        conn,
        "ontology_concepts",
        [
            "COALESCE(definition_status, '') AS definition_status",
            "COALESCE(definition_source, '') AS definition_source",
            "COALESCE(review_status, '') AS review_status",
        ],
        limit=30,
    )


def _label_major_progress(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not _table_exists(conn, "ontology_concept_label_candidates"):
        return []
    major_names: dict[str, str] = {}
    if _table_exists(conn, "classifications"):
        rows = conn.execute(
            """
            SELECT major_code, MIN(major_name) AS major_name
            FROM classifications
            GROUP BY major_code
            ORDER BY major_code
            """
        ).fetchall()
        major_names = {str(row["major_code"]): str(row["major_name"]) for row in rows}
    rows = conn.execute(
        """
        SELECT
            SUBSTR(source_scope_key, 1, 2) AS major_code,
            COUNT(*) AS total,
            SUM(CASE WHEN review_status = 'human_reviewed' THEN 1 ELSE 0 END) AS human_reviewed,
            SUM(CASE WHEN review_status = 'llm_reviewed' THEN 1 ELSE 0 END) AS llm_reviewed,
            SUM(CASE WHEN review_status = 'needs_review' THEN 1 ELSE 0 END) AS needs_review,
            SUM(CASE WHEN review_status = 'candidate' THEN 1 ELSE 0 END) AS candidate,
            SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM ontology_concept_label_candidates
        GROUP BY SUBSTR(source_scope_key, 1, 2)
        ORDER BY major_code
        """
    ).fetchall()
    progress: list[dict[str, Any]] = []
    for row in rows:
        major_code = str(row["major_code"] or "unknown")
        total = int(row["total"] or 0)
        human_reviewed = int(row["human_reviewed"] or 0)
        llm_reviewed = int(row["llm_reviewed"] or 0)
        needs_review = int(row["needs_review"] or 0)
        progress.append(
            {
                "major_code": major_code,
                "major_name": major_names.get(major_code, ""),
                "total": total,
                "human_reviewed": human_reviewed,
                "llm_reviewed": llm_reviewed,
                "needs_review": needs_review,
                "candidate": int(row["candidate"] or 0),
                "rejected": int(row["rejected"] or 0),
                "human_review_rate": round(human_reviewed / total, 4) if total else 0.0,
                "pending_llm_or_needs_review": llm_reviewed + needs_review,
            }
        )
    return progress


def _estimate_review_time(
    review_set: str,
    count: int,
    meaning: str,
    seconds_per_item: tuple[int, ...],
) -> list[dict[str, Any]]:
    estimates: list[dict[str, Any]] = []
    for seconds in seconds_per_item:
        hours = count * seconds / 3600
        estimates.append(
            {
                "review_set": review_set,
                "count": count,
                "seconds_per_item": seconds,
                "hours": round(hours, 2),
                "eight_hour_days": round(hours / 8, 2),
                "meaning": meaning,
            }
        )
    return estimates


def _sidecar_safety_issues(
    *,
    name: str,
    payload: Any,
    expected_schema: str,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(payload, dict):
        return [
            {
                "severity": "blocker",
                "code": "sidecar_not_object",
                "sidecar": name,
                "message": f"{name} sidecar must be a JSON object.",
            }
        ]
    if payload.get("schema") != expected_schema:
        issues.append(
            {
                "severity": "blocker",
                "code": "sidecar_schema_mismatch",
                "sidecar": name,
                "expected_schema": expected_schema,
                "actual_schema": payload.get("schema"),
                "message": f"{name} sidecar schema does not match the expected report.",
            }
        )
    for field in SIDECAR_SAFETY_TRUE_FIELDS:
        if payload.get(field) is not True:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "sidecar_safety_flag_not_true",
                    "sidecar": name,
                    "field": field,
                    "actual": payload.get(field),
                    "message": f"{name}.{field} must be true.",
                }
            )
    for field in SIDECAR_SAFETY_FALSE_FIELDS:
        if payload.get(field) is not False:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "sidecar_safety_flag_not_false",
                    "sidecar": name,
                    "field": field,
                    "actual": payload.get(field),
                    "message": f"{name}.{field} must be false.",
                }
            )
    for field in SIDECAR_OPTIONAL_FALSE_FIELDS:
        if payload.get(field) is True:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "sidecar_optional_safety_flag_true",
                    "sidecar": name,
                    "field": field,
                    "actual": True,
                    "message": f"{name}.{field} must not be true.",
                }
            )
    if name == "auto_triage_report":
        scope_policy = (
            payload.get("scope_policy")
            if isinstance(payload.get("scope_policy"), dict)
            else {}
        )
        required_scope_values = {
            "target_scope_is_filtered": False,
            "scoped_counts_are_local_view": False,
            "scoped_report_is_canonical_bulk_plan": False,
            "all_scope_required_for_bulk_planning": True,
            "operator_sampling_plan_required_before_bulk_use": True,
        }
        if not scope_policy:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "auto_triage_scope_policy_missing",
                    "sidecar": name,
                    "message": "auto_triage_report.scope_policy is required for LLM preprocessing planning.",
                }
            )
        else:
            for field, expected in required_scope_values.items():
                if scope_policy.get(field) is not expected:
                    issues.append(
                        {
                            "severity": "blocker",
                            "code": "auto_triage_scope_policy_invalid",
                            "sidecar": name,
                            "field": f"scope_policy.{field}",
                            "expected": expected,
                            "actual": scope_policy.get(field),
                            "message": (
                                f"auto_triage_report.scope_policy.{field} must be "
                                f"{str(expected).lower()}."
                            ),
                        }
                    )
        if not isinstance(payload.get("classification_v2_counts"), dict):
            issues.append(
                {
                    "severity": "blocker",
                    "code": "auto_triage_classification_counts_missing",
                    "sidecar": name,
                    "message": "auto_triage_report.classification_v2_counts is required.",
                }
            )
        if not isinstance(payload.get("major_bucket_rollup"), list):
            issues.append(
                {
                    "severity": "blocker",
                    "code": "auto_triage_major_rollup_missing",
                    "sidecar": name,
                    "message": "auto_triage_report.major_bucket_rollup is required.",
                }
            )
    if name == "sampling_plan":
        source_issues = payload.get("source_issues")
        if source_issues not in ([], None):
            issues.append(
                {
                    "severity": "blocker",
                    "code": "sampling_plan_source_issues_present",
                    "sidecar": name,
                    "actual": source_issues,
                    "message": "sampling_plan.source_issues must be empty.",
                }
            )
        if not isinstance(payload.get("summary"), dict):
            issues.append(
                {
                    "severity": "blocker",
                    "code": "sampling_plan_summary_missing",
                    "sidecar": name,
                    "message": "sampling_plan.summary is required.",
                }
            )
    return issues


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _source_backlog_issues(backlog_map: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(backlog_map, dict):
        return [
            {
                "severity": "blocker",
                "code": "backlog_map_not_object",
                "message": "Backlog map source must be a JSON object.",
            }
        ]
    if backlog_map.get("schema") != LLM_PREPROCESSING_BACKLOG_SCHEMA:
        issues.append(
            {
                "severity": "blocker",
                "code": "backlog_map_schema_mismatch",
                "expected_schema": LLM_PREPROCESSING_BACKLOG_SCHEMA,
                "actual_schema": backlog_map.get("schema"),
                "message": "Backlog map source schema is not allowed.",
            }
        )
    if backlog_map.get("ok") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "backlog_map_not_ok",
                "actual": backlog_map.get("ok"),
                "message": "Backlog map must be ok=true before planning.",
            }
        )
    for field in SIDECAR_SAFETY_FALSE_FIELDS:
        if backlog_map.get(field) is not False:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "backlog_map_safety_flag_not_false",
                    "field": field,
                    "actual": backlog_map.get(field),
                    "message": f"Backlog map {field} must be false.",
                }
            )
    if backlog_map.get("report_only") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "backlog_map_not_report_only",
                "actual": backlog_map.get("report_only"),
                "message": "Backlog map must be report_only=true.",
            }
        )
    if backlog_map.get("human_decision_required_for_approval") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "human_decision_gate_missing",
                "actual": backlog_map.get("human_decision_required_for_approval"),
                "message": "Backlog map must require a human decision for approval.",
            }
        )
    review_status_policy = (
        backlog_map.get("review_status_policy")
        if isinstance(backlog_map.get("review_status_policy"), dict)
        else {}
    )
    if review_status_policy.get("human_decision_required_for_status_update") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "status_update_human_gate_missing",
                "actual": review_status_policy.get(
                    "human_decision_required_for_status_update"
                ),
                "message": "Backlog map must require a human decision for status updates.",
            }
        )
    forbidden_statuses = set(review_status_policy.get("forbidden_automatic_statuses") or [])
    missing_forbidden = sorted(set(TRUSTED_LABEL_REVIEW_STATUSES) - forbidden_statuses)
    if missing_forbidden:
        issues.append(
            {
                "severity": "blocker",
                "code": "forbidden_automatic_statuses_missing",
                "missing": missing_forbidden,
                "message": "Backlog map must forbid automatic trusted status writes.",
            }
        )
    return issues


def _source_work_plan_issues(work_plan: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if not isinstance(work_plan, dict):
        return [
            {
                "severity": "blocker",
                "code": "work_plan_not_object",
                "message": "Work-plan source must be a JSON object.",
            }
        ]
    if work_plan.get("schema") != LLM_PREPROCESSING_WORK_PLAN_SCHEMA:
        issues.append(
            {
                "severity": "blocker",
                "code": "work_plan_schema_mismatch",
                "expected_schema": LLM_PREPROCESSING_WORK_PLAN_SCHEMA,
                "actual_schema": work_plan.get("schema"),
                "message": "Work-plan source schema is not allowed.",
            }
        )
    if work_plan.get("ok") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "work_plan_not_ok",
                "actual": work_plan.get("ok"),
                "message": "Work-plan source must be ok=true before runbook generation.",
            }
        )
    for field in SIDECAR_SAFETY_FALSE_FIELDS:
        if work_plan.get(field) is not False:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "work_plan_safety_flag_not_false",
                    "field": field,
                    "actual": work_plan.get(field),
                    "message": f"Work-plan {field} must be false.",
                }
            )
    if work_plan.get("report_only") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "work_plan_not_report_only",
                "actual": work_plan.get("report_only"),
                "message": "Work-plan source must be report_only=true.",
            }
        )
    if work_plan.get("human_decision_required_for_approval") is not True:
        issues.append(
            {
                "severity": "blocker",
                "code": "human_decision_gate_missing",
                "actual": work_plan.get("human_decision_required_for_approval"),
                "message": "Work-plan must require a human decision for approval.",
            }
        )
    safety_contract = (
        work_plan.get("safety_contract")
        if isinstance(work_plan.get("safety_contract"), dict)
        else {}
    )
    if safety_contract.get("trusted_status_write_allowed") is not False:
        issues.append(
            {
                "severity": "blocker",
                "code": "trusted_status_write_not_blocked",
                "actual": safety_contract.get("trusted_status_write_allowed"),
                "message": "Work-plan must block trusted status writes.",
            }
        )
    if safety_contract.get("raw_source_mutation_allowed") is not False:
        issues.append(
            {
                "severity": "blocker",
                "code": "raw_source_mutation_not_blocked",
                "actual": safety_contract.get("raw_source_mutation_allowed"),
                "message": "Work-plan must block raw source mutation.",
            }
        )
    forbidden_statuses = set(safety_contract.get("forbidden_automatic_statuses") or [])
    missing_forbidden = sorted(set(TRUSTED_LABEL_REVIEW_STATUSES) - forbidden_statuses)
    if missing_forbidden:
        issues.append(
            {
                "severity": "blocker",
                "code": "forbidden_automatic_statuses_missing",
                "missing": missing_forbidden,
                "message": "Work-plan must forbid automatic trusted status writes.",
            }
        )
    artifact_policy = (
        work_plan.get("artifact_policy")
        if isinstance(work_plan.get("artifact_policy"), dict)
        else {}
    )
    for field in (
        "db_apply_allowed",
        "guarded_collection_allowed",
        "operator_decision_fields_auto_filled",
    ):
        if artifact_policy.get(field) is not False:
            issues.append(
                {
                    "severity": "blocker",
                    "code": "artifact_policy_flag_not_false",
                    "field": field,
                    "actual": artifact_policy.get(field),
                    "message": f"Work-plan artifact_policy.{field} must be false.",
                }
            )
    return issues


def build_llm_preprocessing_work_plan(
    backlog_map: dict[str, Any],
    *,
    source_path: str | None = None,
    source_artifact_hash: str | None = None,
) -> dict[str, Any]:
    """Build a report-only next-work plan from an LLM preprocessing backlog map."""

    source_issues = _source_backlog_issues(backlog_map)
    ok = not source_issues
    summary = backlog_map.get("summary") if isinstance(backlog_map, dict) else {}
    if not isinstance(summary, dict):
        summary = {}
    policy = backlog_map.get("policy_snapshot") if isinstance(backlog_map, dict) else {}
    if not isinstance(policy, dict):
        policy = {}
    auto_triage = policy.get("auto_triage") if isinstance(policy.get("auto_triage"), dict) else {}
    sampling = policy.get("sampling_plan") if isinstance(policy.get("sampling_plan"), dict) else {}
    classification_counts = (
        auto_triage.get("classification_v2_counts")
        if isinstance(auto_triage.get("classification_v2_counts"), dict)
        else {}
    )
    meaning_backlog = (
        backlog_map.get("meaning_definition_backlog")
        if isinstance(backlog_map, dict)
        and isinstance(backlog_map.get("meaning_definition_backlog"), dict)
        else {}
    )
    relation_backlog = (
        backlog_map.get("relation_link_backlog")
        if isinstance(backlog_map, dict)
        and isinstance(backlog_map.get("relation_link_backlog"), dict)
        else {}
    )
    quality_backlog = (
        backlog_map.get("quality_backlog")
        if isinstance(backlog_map, dict)
        and isinstance(backlog_map.get("quality_backlog"), dict)
        else {}
    )
    query_alias_backlog = (
        backlog_map.get("query_alias_backlog")
        if isinstance(backlog_map, dict)
        and isinstance(backlog_map.get("query_alias_backlog"), dict)
        else {}
    )
    label_candidates = _int_value(summary.get("label_candidate_rows"))
    pending_labels = _int_value(summary.get("pending_label_rows_not_trusted"))
    human_reviewed_labels = _int_value(summary.get("human_reviewed_label_rows"))
    recommended_samples = _int_value(sampling.get("recommended_sample_rows_total"))
    click_reduction = _float_value(sampling.get("estimated_click_reduction_ratio"))
    definition_candidates = _int_value(meaning_backlog.get("meaning_candidate_rows"))
    term_definition_candidates = 0
    for row in meaning_backlog.get("role_method_counts") or []:
        if not isinstance(row, dict):
            continue
        if row.get("meaning_role") == "term_definition_candidate":
            term_definition_candidates += _int_value(row.get("count"))
    task_relations = _int_value(relation_backlog.get("task_ksa_concept_relation_rows"))
    training_goal_links = _int_value(relation_backlog.get("training_goal_concept_link_rows"))
    training_course_links = _int_value(
        relation_backlog.get("training_course_concept_link_rows")
    )
    unresolved_quality = _int_value(quality_backlog.get("unresolved_quality_issue_rows"))
    query_alias_candidates = _int_value(
        (query_alias_backlog.get("status_counts") or {}).get("candidate")
        if isinstance(query_alias_backlog.get("status_counts"), dict)
        else 0
    )

    work_tracks = [
        {
            "priority": "P0",
            "track": "label_policy_triage_and_sampling",
            "input_rows": pending_labels,
            "basis": {
                "label_candidate_rows": label_candidates,
                "human_reviewed_label_rows": human_reviewed_labels,
                "classification_v2_counts": classification_counts,
                "recommended_sample_rows_total": recommended_samples,
                "estimated_click_reduction_ratio": click_reduction,
            },
            "automation_scope": [
                "regenerate all-scope label triage from the current database",
                "refresh the operator sampling plan from the all-scope source only",
                "surface counts in the KSA preprocessing dashboard as non-approval status",
            ],
            "human_gate": (
                "operator sample decisions can inform policy, but cannot set "
                "human_reviewed, accepted, or reviewed automatically"
            ),
            "acceptance_checks": [
                "source report is all-scope and report_only=true",
                "status_update_allowed=false, db_writes=false, approval_claim=false",
                "bucket counts reconcile to the label candidate count",
                "sample_is_approval is absent or false",
            ],
        },
        {
            "priority": "P1",
            "track": "label_modify_pattern_cleanup",
            "input_rows": _int_value(classification_counts.get("modify-recommended")),
            "basis": {
                "modify_recommended_rows": _int_value(
                    classification_counts.get("modify-recommended")
                ),
                "human_sample_needed_rows": _int_value(
                    classification_counts.get("human-sample-needed")
                ),
                "domain_expert_needed_rows": _int_value(
                    classification_counts.get("domain-expert-needed")
                ),
            },
            "automation_scope": [
                "group repeated suffix and boilerplate label edits by pattern family",
                "separate generic labels from domain-preserving terms",
                "prepare examples for operator review instead of row-by-row approval",
            ],
            "human_gate": "policy changes need sample review before any status update workflow",
            "acceptance_checks": [
                "no generated row claims approval",
                "domain-expert-needed rows stay separate from HR general review",
                "pattern examples include source counts and representative labels",
            ],
        },
        {
            "priority": "P1",
            "track": "definition_candidate_cleanup",
            "input_rows": term_definition_candidates or definition_candidates,
            "basis": {
                "meaning_candidate_rows": definition_candidates,
                "term_definition_candidate_rows": term_definition_candidates,
                "ontology_concepts_human_reviewed": _int_value(
                    summary.get("ontology_concepts_human_reviewed")
                ),
            },
            "automation_scope": [
                "detect boilerplate definitions and rank useful definition candidates",
                "cluster duplicate draft definitions by concept and evidence family",
                "prepare small operator packets with blank decision fields",
            ],
            "human_gate": (
                "draft definitions cannot be promoted to ontology_concepts.definition "
                "without separate guarded approval"
            ),
            "acceptance_checks": [
                "definition_status is not changed by the plan",
                "draft_definition is labeled as review assistance only",
                "decision, reviewer_id, reviewed_at, and rationale fields start blank",
            ],
        },
        {
            "priority": "P2",
            "track": "relation_link_evidence_hygiene",
            "input_rows": task_relations + training_goal_links + training_course_links,
            "basis": {
                "task_ksa_concept_relation_rows": task_relations,
                "training_goal_concept_link_rows": training_goal_links,
                "training_course_concept_link_rows": training_course_links,
            },
            "automation_scope": [
                "rank weak or inherited links by method and evidence density",
                "separate direct training-goal evidence from inherited unit links",
                "prepare diagnostics for overbroad KSA and course links",
            ],
            "human_gate": "candidate and auto_linked relation statuses remain non-approval states",
            "acceptance_checks": [
                "link_method and review_status counts are preserved",
                "weak evidence is reported as a diagnostic, not deleted",
                "course-link similarity across majors is marked as review-required",
            ],
        },
        {
            "priority": "P2",
            "track": "quality_issue_deduplication_plan",
            "input_rows": unresolved_quality,
            "basis": {
                "unresolved_quality_issue_rows": unresolved_quality,
                "top_unresolved_issues": quality_backlog.get("top_unresolved_issues") or [],
            },
            "automation_scope": [
                "rank duplicate_text and short_ksa issue families for cleanup proposals",
                "identify recurring source patterns that can be fixed by preprocessing rules",
                "write report-only cleanup proposals with no mutation flag",
            ],
            "human_gate": "quality issue cleanup proposals require a separate apply command and approval",
            "acceptance_checks": [
                "resolved_at is not written by this plan",
                "raw KSA text remains unchanged",
                "cleanup proposal includes dry-run counts before any guarded command",
            ],
        },
        {
            "priority": "P3",
            "track": "query_alias_gap_candidates",
            "input_rows": query_alias_candidates,
            "basis": {
                "query_alias_candidate_rows": query_alias_candidates,
                "query_alias_status_counts": query_alias_backlog.get("status_counts") or {},
            },
            "automation_scope": [
                "prepare alias candidate packets for failed or ambiguous user queries",
                "escape CSV formula-like text and preserve route provenance",
                "audit packet readability before operator review",
            ],
            "human_gate": "routing aliases that affect public tools need explicit review before trusted use",
            "acceptance_checks": [
                "candidate packets are report_only=true",
                "decision fields are blank",
                "route fingerprints are preserved for drift checks",
            ],
        },
    ]
    eight_hour_run = [
        {
            "slot": "00:00-00:30",
            "focus": "source_artifact_safety_check",
            "expected_output": "verify backlog map, auto-triage, and sampling plan safety flags",
        },
        {
            "slot": "00:30-02:30",
            "focus": "label_policy_triage_and_sampling",
            "expected_output": "fresh all-scope triage, sampling plan, and dashboard snapshot",
        },
        {
            "slot": "02:30-04:00",
            "focus": "label_modify_pattern_cleanup",
            "expected_output": "pattern-family cleanup proposal and examples",
        },
        {
            "slot": "04:00-05:30",
            "focus": "definition_candidate_cleanup",
            "expected_output": "definition candidate family report and small operator packet",
        },
        {
            "slot": "05:30-07:00",
            "focus": "relation_link_evidence_hygiene",
            "expected_output": "weak-link diagnostic and course-link evidence hygiene report",
        },
        {
            "slot": "07:00-08:00",
            "focus": "verification_and_handoff",
            "expected_output": "lint, smoke, focused tests, readability audit, and handoff summary",
        },
    ]
    return {
        "schema": LLM_PREPROCESSING_WORK_PLAN_SCHEMA,
        "generated_at": _now_iso(),
        "ok": ok,
        "status": "ready_for_llm_preprocessing" if ok else "blocked_unsafe_source_artifact",
        "source_schema": backlog_map.get("schema") if isinstance(backlog_map, dict) else None,
        "source_generated_at": (
            backlog_map.get("generated_at") if isinstance(backlog_map, dict) else None
        ),
        "source_backlog_map": source_path,
        "source_artifact_hash": source_artifact_hash,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required_for_approval": True,
        "safety_contract": {
            "raw_source_mutation_allowed": False,
            "trusted_status_write_allowed": False,
            "source_payload_exposed": False,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
            "non_approval_statuses": [
                "llm_reviewed",
                "model_preprocessed",
                "auto_linked",
                "candidate",
                "needs_review",
                "auto-pass-candidate",
            ],
        },
        "artifact_policy": {
            "allowed_outputs": ["json", "markdown", "csv review packets"],
            "db_apply_allowed": False,
            "guarded_collection_allowed": False,
            "operator_decision_fields_auto_filled": False,
        },
        "next_action": (
            "run_report_only_track_artifacts" if ok else "fix_source_backlog_map"
        ),
        "input_summary": {
            "raw_ksa_rows": _int_value(summary.get("raw_ksa_rows")),
            "label_candidate_rows": label_candidates,
            "pending_label_rows_not_trusted": pending_labels,
            "human_reviewed_label_rows": human_reviewed_labels,
            "meaning_candidate_rows": definition_candidates,
            "term_definition_candidate_rows": term_definition_candidates,
            "task_ksa_concept_relation_rows": task_relations,
            "training_goal_concept_link_rows": training_goal_links,
            "training_course_concept_link_rows": training_course_links,
            "unresolved_quality_issue_rows": unresolved_quality,
            "query_alias_candidate_rows": query_alias_candidates,
            "recommended_sample_rows_total": recommended_samples,
            "estimated_click_reduction_ratio": click_reduction,
        },
        "work_tracks": work_tracks,
        "eight_hour_run_plan": eight_hour_run,
        "not_recommended_for_llm_run": [
            "row-by-row approval clicking across all label candidates",
            "writing human_reviewed, accepted, or reviewed statuses",
            "changing ksa_items.ksa_text_raw",
            "promoting draft definitions into ontology_concepts.definition",
            "running guarded API collection, DB apply, or status-update commands",
        ],
        "acceptance_criteria": [
            "all generated artifacts remain report_only=true",
            "status_update_allowed=false, db_writes=false, approval_claim=false",
            "human decision fields remain blank unless filled outside automation",
            "LLM outputs are classified as candidates, diagnostics, or review packets only",
            "verification includes focused tests, lint, smoke, and artifact readability checks",
        ],
        "source_issues": source_issues,
        "blocker_count": len(source_issues),
    }


def build_llm_preprocessing_backlog_map(
    conn: sqlite3.Connection,
    *,
    seconds_per_item: tuple[int, ...] = DEFAULT_SECONDS_PER_ITEM,
    top_limit: int = 25,
    auto_triage_report: dict[str, Any] | None = None,
    sampling_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a read-only map of LLM-assisted preprocessing and human-gated review work."""

    conn.row_factory = sqlite3.Row
    label_total = _count_table(conn, "ontology_concept_label_candidates") or 0
    label_status = _status_count(conn, "ontology_concept_label_candidates")
    human_reviewed_labels = int(label_status.get("human_reviewed", 0))
    trusted_placeholders = ",".join("?" for _ in TRUSTED_LABEL_REVIEW_STATUSES)
    pending_label_rows = (
        _scalar(
            conn,
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE review_status IS NULL
               OR review_status NOT IN ({trusted_placeholders})
            """,
            TRUSTED_LABEL_REVIEW_STATUSES,
        )
        if _table_exists(conn, "ontology_concept_label_candidates")
        else 0
    )
    distinct_label_keys = (
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT normalized_label_key) FROM ontology_concept_label_candidates",
        )
        if _table_exists(conn, "ontology_concept_label_candidates")
        else 0
    )
    concepts_with_label_candidates = (
        _scalar(
            conn,
            "SELECT COUNT(DISTINCT concept_id) FROM ontology_concept_label_candidates",
        )
        if _table_exists(conn, "ontology_concept_label_candidates")
        else 0
    )

    concept_status = _status_count(conn, "ontology_concepts")
    concept_total = _count_table(conn, "ontology_concepts") or 0
    meaning_total = _count_table(conn, "ksa_meaning_candidates") or 0
    task_relation_total = _count_table(conn, "task_ksa_concept_relations") or 0
    training_goal_total = _count_table(conn, "training_goal_concept_links") or 0
    course_concept_total = _count_table(conn, "ncs_training_course_concept_links") or 0
    unresolved_quality_issues = (
        _scalar(conn, "SELECT COUNT(*) FROM quality_issues WHERE resolved_at IS NULL")
        if _table_exists(conn, "quality_issues")
        else 0
    )

    source_totals = {
        "raw_ksa_rows": _count_table(conn, "ksa_items"),
        "atomic_ksa_rows": _count_table(conn, "ksa_atomic_items"),
        "ontology_concepts": concept_total,
        "ontology_concept_aliases": _count_table(conn, "ontology_concept_aliases"),
        "criteria_concept_links": _count_table(conn, "criteria_concept_links"),
        "ksa_concept_links": _count_table(conn, "ksa_concept_links"),
        "ksa_atomic_concept_links": _count_table(conn, "ksa_atomic_concept_links"),
    }
    label_backlog = {
        "label_candidate_rows": label_total,
        "status_counts": label_status,
        "human_reviewed_rows": human_reviewed_labels,
        "pending_rows_not_trusted": pending_label_rows,
        "distinct_normalized_label_keys": distinct_label_keys,
        "distinct_concepts_with_label_candidates": concepts_with_label_candidates,
        "source_method_counts": _group_count(
            conn,
            "ontology_concept_label_candidates",
            [
                "COALESCE(source_method, '') AS source_method",
                "COALESCE(review_status, '') AS review_status",
            ],
            limit=top_limit,
        ),
        "major_progress": _label_major_progress(conn),
    }
    meaning_backlog = {
        "meaning_candidate_rows": meaning_total,
        "status_counts": _status_count(conn, "ksa_meaning_candidates"),
        "role_method_counts": _group_count(
            conn,
            "ksa_meaning_candidates",
            [
                "COALESCE(meaning_role, '') AS meaning_role",
                "COALESCE(source_method, '') AS source_method",
                "COALESCE(review_status, '') AS review_status",
            ],
            limit=top_limit,
        ),
    }
    ontology_backlog = {
        "concept_rows": concept_total,
        "concept_review_status_counts": concept_status,
        "definition_status_counts": _definition_status_count(conn),
        "human_reviewed_concepts": int(concept_status.get("human_reviewed", 0)),
    }
    relation_backlog = {
        "task_ksa_concept_relation_rows": task_relation_total,
        "task_relation_counts": _group_count(
            conn,
            "task_ksa_concept_relations",
            [
                "COALESCE(review_status, '') AS review_status",
                "COALESCE(relation_type, '') AS relation_type",
            ],
            limit=top_limit,
        ),
        "training_goal_concept_link_rows": training_goal_total,
        "training_goal_link_counts": _group_count(
            conn,
            "training_goal_concept_links",
            [
                "COALESCE(review_status, '') AS review_status",
                "COALESCE(link_method, '') AS link_method",
            ],
            limit=top_limit,
        ),
        "training_course_concept_link_rows": course_concept_total,
        "training_course_link_counts": _group_count(
            conn,
            "ncs_training_course_concept_links",
            [
                "COALESCE(review_status, '') AS review_status",
                "COALESCE(link_method, '') AS link_method",
            ],
            limit=top_limit,
        ),
    }
    quality_backlog = {
        "unresolved_quality_issue_rows": unresolved_quality_issues,
        "top_unresolved_issues": _group_count(
            conn,
            "quality_issues",
            [
                "COALESCE(target_type, '') AS target_type",
                "COALESCE(issue_type, '') AS issue_type",
                "COALESCE(severity, '') AS severity",
            ],
            where_sql="WHERE resolved_at IS NULL",
            limit=top_limit,
        ),
    }

    review_time_estimates: list[dict[str, Any]] = []
    review_time_estimates.extend(
        _estimate_review_time(
            "all_label_candidate_rows",
            label_total,
            "All KSA short-label candidate rows; closest to UI button-row workload.",
            seconds_per_item,
        )
    )
    review_time_estimates.extend(
        _estimate_review_time(
            "pending_label_rows_not_trusted",
            pending_label_rows,
            "Label rows without trusted human/accepted/reviewed status.",
            seconds_per_item,
        )
    )
    review_time_estimates.extend(
        _estimate_review_time(
            "distinct_normalized_label_keys",
            distinct_label_keys,
            "Unique normalized label strings; useful for menu/pattern planning, not approval.",
            seconds_per_item,
        )
    )
    review_time_estimates.extend(
        _estimate_review_time(
            "concepts_with_label_candidates",
            concepts_with_label_candidates,
            "Distinct ontology concepts with at least one label candidate.",
            seconds_per_item,
        )
    )

    policy_snapshot: dict[str, Any] = {
        "auto_triage_report_provided": auto_triage_report is not None,
        "sampling_plan_provided": sampling_plan is not None,
    }
    source_issues: list[dict[str, Any]] = []
    if auto_triage_report is not None:
        source_issues.extend(
            _sidecar_safety_issues(
                name="auto_triage_report",
                payload=auto_triage_report,
                expected_schema="ksa_label_auto_triage_report_v1",
            )
        )
    if sampling_plan is not None:
        source_issues.extend(
            _sidecar_safety_issues(
                name="sampling_plan",
                payload=sampling_plan,
                expected_schema="ksa_label_policy_v2_operator_sampling_plan_v1",
            )
        )
    if isinstance(auto_triage_report, dict):
        policy_snapshot["auto_triage"] = {
            "schema": auto_triage_report.get("schema"),
            "ok": auto_triage_report.get("ok"),
            "status": auto_triage_report.get("status"),
            "candidate_count": auto_triage_report.get("candidate_count"),
            "classification_v2_counts": auto_triage_report.get(
                "classification_v2_counts"
            ),
            "full_scope_decision_row_count": auto_triage_report.get(
                "full_scope_decision_row_count"
            ),
            "full_scope_manual_review_recommended_count": auto_triage_report.get(
                "full_scope_manual_review_recommended_count"
            ),
            "status_update_allowed": auto_triage_report.get("status_update_allowed"),
            "db_writes": auto_triage_report.get("db_writes"),
            "approval_claim": auto_triage_report.get("approval_claim"),
            "safety_ok": not any(
                issue.get("sidecar") == "auto_triage_report"
                for issue in source_issues
            ),
        }
    if isinstance(sampling_plan, dict):
        summary = sampling_plan.get("summary") or {}
        policy_snapshot["sampling_plan"] = {
            "schema": sampling_plan.get("schema"),
            "ok": sampling_plan.get("ok"),
            "status": sampling_plan.get("status"),
            "candidate_count": summary.get("candidate_count"),
            "recommended_sample_rows_total": summary.get(
                "recommended_sample_rows_total"
            ),
            "decision_rows_total_from_major_rollup": summary.get(
                "decision_rows_total_from_major_rollup"
            ),
            "estimated_click_reduction_ratio": summary.get(
                "estimated_click_reduction_ratio"
            ),
            "status_update_allowed": sampling_plan.get("status_update_allowed"),
            "db_writes": sampling_plan.get("db_writes"),
            "approval_claim": sampling_plan.get("approval_claim"),
            "safety_ok": not any(
                issue.get("sidecar") == "sampling_plan" for issue in source_issues
            ),
        }
    ok = not source_issues
    status = "review_planning_only" if ok else "blocked_unsafe_source_artifact"

    return {
        "schema": LLM_PREPROCESSING_BACKLOG_SCHEMA,
        "generated_at": _now_iso(),
        "ok": ok,
        "status": status,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required_for_approval": True,
        "review_status_policy": {
            "human_decision_required_for_status_update": True,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
            "non_approval_statuses": [
                "llm_reviewed",
                "model_preprocessed",
                "auto_linked",
                "candidate",
                "needs_review",
                "auto-pass-candidate",
            ],
        },
        "summary": {
            "raw_ksa_rows": source_totals["raw_ksa_rows"],
            "label_candidate_rows": label_total,
            "human_reviewed_label_rows": human_reviewed_labels,
            "pending_label_rows_not_trusted": pending_label_rows,
            "distinct_normalized_label_keys": distinct_label_keys,
            "distinct_concepts_with_label_candidates": concepts_with_label_candidates,
            "ontology_concepts": concept_total,
            "ontology_concepts_human_reviewed": int(concept_status.get("human_reviewed", 0)),
            "meaning_candidate_rows": meaning_total,
            "task_ksa_concept_relation_rows": task_relation_total,
            "training_goal_concept_link_rows": training_goal_total,
            "training_course_concept_link_rows": course_concept_total,
            "unresolved_quality_issue_rows": unresolved_quality_issues,
        },
        "source_totals": source_totals,
        "label_backlog": label_backlog,
        "meaning_definition_backlog": meaning_backlog,
        "ontology_backlog": ontology_backlog,
        "relation_link_backlog": relation_backlog,
        "quality_backlog": quality_backlog,
        "query_alias_backlog": {
            "query_alias_rows": _count_table(conn, "ncs_query_aliases"),
            "status_counts": _status_count(conn, "ncs_query_aliases"),
            "source_method_counts": _group_count(
                conn,
                "ncs_query_aliases",
                [
                    "COALESCE(review_status, '') AS review_status",
                    "COALESCE(source_method, '') AS source_method",
                ],
                limit=top_limit,
            ),
        },
        "review_audit_snapshot": {
            "review_audit_log_rows": _count_table(conn, "review_audit_log"),
            "top_actions": _group_count(
                conn,
                "review_audit_log",
                [
                    "COALESCE(entity_type, '') AS entity_type",
                    "COALESCE(action, '') AS action",
                    "COALESCE(new_status, '') AS new_status",
                ],
                limit=top_limit,
            ),
        },
        "review_time_estimates": review_time_estimates,
        "policy_snapshot": policy_snapshot,
        "source_issues": source_issues,
        "blocker_count": len(source_issues),
        "recommended_next_llm_work": [
            {
                "track": "label_policy_triage",
                "automation_role": "classify and group rows into auto-pass candidate, modify-recommended, human-sample-needed, and domain-expert-needed buckets",
                "human_gate": "sampling decisions and any trusted status changes require an operator decision packet",
            },
            {
                "track": "definition_candidate_cleanup",
                "automation_role": "detect boilerplate, rank high-value concept definition candidates, and prepare small review packets",
                "human_gate": "draft definitions cannot be promoted to ontology_concepts.definition without guarded human approval",
            },
            {
                "track": "relation_link_evidence_hygiene",
                "automation_role": "rank weak task-KSA and training-course links by confidence, method, and evidence text for review",
                "human_gate": "relation/link review_status must remain candidate/auto_linked until a human review flow writes otherwise",
            },
            {
                "track": "query_alias_gap_candidates",
                "automation_role": "mine failed or ambiguous user queries and generate alias candidate decision sheets",
                "human_gate": "aliases that affect routing should be sampled and approved before trusted use",
            },
        ],
        "forbidden_without_explicit_operator_approval": [
            "Do not set human_reviewed, accepted, or reviewed automatically.",
            "Do not modify ksa_items.ksa_text_raw.",
            "Do not promote boilerplate or draft definitions into trusted ontology definitions.",
            "Do not treat llm_reviewed, auto_linked, or auto-pass-candidate as human approval.",
            "Do not run guarded collection or DB apply commands from this planning report.",
        ],
    }


def _runbook_command(
    *,
    stage: str,
    track: str,
    command: list[str],
    outputs: list[str],
    purpose: str,
    acceptance_checks: list[str],
) -> dict[str, Any]:
    return {
        "stage": stage,
        "track": track,
        "purpose": purpose,
        "command": command,
        "expected_outputs": outputs,
        "mutation_policy": "regenerate_reports_only",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_fields_auto_filled": False,
        "acceptance_checks": acceptance_checks,
    }


def _runbook_artifact_path(artifact_dir: str, filename: str) -> str:
    base = str(artifact_dir or "reports").strip() or "reports"
    return base.rstrip("\\/") + "\\" + filename


def build_llm_preprocessing_runbook(
    work_plan: dict[str, Any],
    *,
    artifact_suffix: str,
    artifact_dir: str = "reports",
    source_path: str | None = None,
    source_artifact_hash: str | None = None,
    agent_queue_path: str | None = None,
    agent_queue_path_source: str | None = None,
) -> dict[str, Any]:
    """Turn a safe LLM preprocessing work plan into report-only CLI steps."""

    source_issues = _source_work_plan_issues(work_plan)
    ok = not source_issues
    tracks = (
        [track for track in work_plan.get("work_tracks", []) if isinstance(track, dict)]
        if isinstance(work_plan, dict)
        else []
    )
    track_by_name = {str(track.get("track")): track for track in tracks}
    commands: list[dict[str, Any]] = []
    queue_path = agent_queue_path or _default_runbook_agent_queue_path(artifact_suffix)
    artifact = lambda filename: _runbook_artifact_path(artifact_dir, filename)
    queue_path_source = (
        agent_queue_path_source
        if agent_queue_path
        else "artifact_suffix_date_fallback"
    )

    commands.append(
        _runbook_command(
            stage="preflight",
            track="source_artifact_safety_check",
            purpose="Refresh queue readiness and prove automated steps are report-only.",
            command=[
                "python",
                "scripts\\ncs_harness.py",
                "agent-queue-status",
                "--queue",
                queue_path,
                "--out",
                artifact(f"aihr_agent_queue_status_llm_preprocessing_{artifact_suffix}.json"),
                "--markdown-out",
                artifact(f"aihr_agent_queue_status_llm_preprocessing_{artifact_suffix}.md"),
            ],
            outputs=[
                artifact(f"aihr_agent_queue_status_llm_preprocessing_{artifact_suffix}.json"),
                artifact(f"aihr_agent_queue_status_llm_preprocessing_{artifact_suffix}.md"),
            ],
            acceptance_checks=[
                "manual or human-decision items remain can_start_automated=false",
                "only regenerate_reports_only items are eligible for automated execution",
            ],
        )
    )
    commands.append(
        _runbook_command(
            stage="preflight",
            track="source_artifact_safety_check",
            purpose="Dry-run automated queue execution without guarded items.",
            command=[
                "python",
                "scripts\\ncs_harness.py",
                "agent-queue-run-ready",
                "--queue",
                queue_path,
                "--dry-run",
                "--out",
                artifact(f"aihr_agent_queue_run_dryrun_llm_preprocessing_{artifact_suffix}.json"),
                "--markdown-out",
                artifact(f"aihr_agent_queue_run_dryrun_llm_preprocessing_{artifact_suffix}.md"),
            ],
            outputs=[
                artifact(f"aihr_agent_queue_run_dryrun_llm_preprocessing_{artifact_suffix}.json"),
                artifact(f"aihr_agent_queue_run_dryrun_llm_preprocessing_{artifact_suffix}.md"),
            ],
            acceptance_checks=[
                "dry_run=true",
                "no human-decision, DB apply, or guarded API collection item is executed",
            ],
        )
    )
    if "label_policy_triage_and_sampling" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="label_policy_triage_and_sampling",
                purpose=(
                    "Regenerate all-scope label bucket counts from previously "
                    "reviewed label-policy evidence; this is not an approval signal."
                ),
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-label-auto-triage-report",
                    "--out",
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.md"),
                    "--csv-out",
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                outputs=[
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.md"),
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                acceptance_checks=track_by_name[
                    "label_policy_triage_and_sampling"
                ].get("acceptance_checks", []),
            )
        )
        commands.append(
            _runbook_command(
                stage="track",
                track="label_policy_triage_and_sampling",
                purpose="Convert triage buckets into a small operator sampling plan.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-label-policy-v2-sampling-plan",
                    "--source-report",
                    artifact(f"ksa_label_auto_triage_all_llm_preprocessing_{artifact_suffix}.json"),
                    "--out",
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.md"),
                    "--csv-out",
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                outputs=[
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.md"),
                    artifact(f"ksa_label_policy_v2_sampling_plan_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                acceptance_checks=[
                    "status_update_allowed=false",
                    "db_writes=false",
                    "approval_claim=false",
                    "operator sampling rows do not claim human approval",
                ],
            )
        )
    if "label_modify_pattern_cleanup" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="label_modify_pattern_cleanup",
                purpose="Group repeated short-label edits into reviewable label families.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-short-label-family-report",
                    "--limit",
                    "200",
                    "--sample-limit",
                    "3",
                    "--out",
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.md"),
                    "--csv-out",
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                outputs=[
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.md"),
                    artifact(f"ksa_short_label_family_report_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                acceptance_checks=track_by_name[
                    "label_modify_pattern_cleanup"
                ].get("acceptance_checks", []),
            )
        )
        commands.append(
            _runbook_command(
                stage="track",
                track="label_modify_pattern_cleanup",
                purpose="Group needs-review label transformations by repeated pattern.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-short-label-pattern-report",
                    "--limit",
                    "100",
                    "--sample-limit",
                    "5",
                    "--out",
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.md"),
                    "--csv-out",
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                outputs=[
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.md"),
                    artifact(f"ksa_short_label_pattern_report_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                acceptance_checks=track_by_name[
                    "label_modify_pattern_cleanup"
                ].get("acceptance_checks", []),
            )
        )
    if "definition_candidate_cleanup" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="definition_candidate_cleanup",
                purpose="Refresh the operator packet for definition review without promotion.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-definition-review-operator-packet",
                    "--limit",
                    "25",
                    "--evidence-limit",
                    "2",
                    "--out",
                    artifact(f"ksa_definition_review_operator_packet_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"ksa_definition_review_operator_packet_llm_preprocessing_{artifact_suffix}.md"),
                ],
                outputs=[
                    artifact(f"ksa_definition_review_operator_packet_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"ksa_definition_review_operator_packet_llm_preprocessing_{artifact_suffix}.md"),
                    "sidecar promotion/status/review/action-plan artifacts",
                ],
                acceptance_checks=track_by_name[
                    "definition_candidate_cleanup"
                ].get("acceptance_checks", []),
            )
        )
    if "relation_link_evidence_hygiene" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="relation_link_evidence_hygiene",
                purpose="Refresh recommendation evidence hygiene diagnostics only.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "recommendation-evidence-hygiene",
                    "--limit",
                    "100",
                    "--out",
                    artifact(f"recommendation_evidence_hygiene_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"recommendation_evidence_hygiene_llm_preprocessing_{artifact_suffix}.md"),
                ],
                outputs=[
                    artifact(f"recommendation_evidence_hygiene_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"recommendation_evidence_hygiene_llm_preprocessing_{artifact_suffix}.md"),
                ],
                acceptance_checks=track_by_name[
                    "relation_link_evidence_hygiene"
                ].get("acceptance_checks", []),
            )
        )
    if "quality_issue_deduplication_plan" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="quality_issue_deduplication_plan",
                purpose="Refresh read-only quality gates before proposing cleanup families.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "quality-gates",
                    "--out",
                    artifact(f"quality_gates_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"quality_gates_llm_preprocessing_{artifact_suffix}.md"),
                ],
                outputs=[
                    artifact(f"quality_gates_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"quality_gates_llm_preprocessing_{artifact_suffix}.md"),
                ],
                acceptance_checks=[
                    "quality issue counts are diagnostic only",
                    "resolved_at is not written",
                    "raw KSA text remains unchanged",
                ],
            )
        )
        commands.append(
            _runbook_command(
                stage="track",
                track="quality_issue_deduplication_plan",
                purpose="Rank high-frequency KSA definition debt while preserving quality issue state.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "ksa-definition-priority-report",
                    "--limit",
                    "2000",
                    "--out",
                    artifact(f"ksa_definition_priority_report_llm_preprocessing_{artifact_suffix}.json"),
                ],
                outputs=[
                    artifact(f"ksa_definition_priority_report_llm_preprocessing_{artifact_suffix}.json"),
                ],
                acceptance_checks=track_by_name[
                    "quality_issue_deduplication_plan"
                ].get("acceptance_checks", []),
            )
        )
    if "query_alias_gap_candidates" in track_by_name:
        commands.append(
            _runbook_command(
                stage="track",
                track="query_alias_gap_candidates",
                purpose="Regenerate alias candidate packets with blank decision fields.",
                command=[
                    "python",
                    "scripts\\ncs_harness.py",
                    "query-alias-candidate-packet",
                    "--gap-report",
                    artifact(f"query_resolution_gap_report_clean_{artifact_suffix}.json"),
                    "--out",
                    artifact(f"query_alias_candidate_packet_llm_preprocessing_{artifact_suffix}.json"),
                    "--markdown-out",
                    artifact(f"query_alias_candidate_packet_llm_preprocessing_{artifact_suffix}.md"),
                    "--decision-sheet-out",
                    artifact(f"query_alias_candidate_decision_sheet_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                outputs=[
                    artifact(f"query_alias_candidate_packet_llm_preprocessing_{artifact_suffix}.json"),
                    artifact(f"query_alias_candidate_packet_llm_preprocessing_{artifact_suffix}.md"),
                    artifact(f"query_alias_candidate_decision_sheet_llm_preprocessing_{artifact_suffix}.csv"),
                ],
                acceptance_checks=track_by_name[
                    "query_alias_gap_candidates"
                ].get("acceptance_checks", []),
            )
        )
    commands.append(
        _runbook_command(
            stage="verification",
            track="verification_and_handoff",
            purpose="Audit generated review artifacts for readability and safety before handoff.",
            command=[
                "python",
                "scripts\\ncs_harness.py",
                "audit-review-artifact-readability",
                "--artifact",
                artifact(f"llm_preprocessing_backlog_map_{artifact_suffix}.json"),
                "--artifact",
                artifact(f"llm_preprocessing_next_8h_work_plan_{artifact_suffix}.json"),
                "--artifact",
                artifact(f"llm_preprocessing_runbook_{artifact_suffix}.json"),
                "--out",
                artifact(f"review_artifact_readability_llm_preprocessing_runbook_{artifact_suffix}.json"),
                "--markdown-out",
                artifact(f"review_artifact_readability_llm_preprocessing_runbook_{artifact_suffix}.md"),
            ],
            outputs=[
                artifact(f"review_artifact_readability_llm_preprocessing_runbook_{artifact_suffix}.json"),
                artifact(f"review_artifact_readability_llm_preprocessing_runbook_{artifact_suffix}.md"),
            ],
            acceptance_checks=[
                "finding_count=0 or findings are reviewed before handoff",
                "audit remains read-only",
            ],
        )
    )
    planned_track_names = [
        str(track.get("track"))
        for track in tracks
        if str(track.get("track") or "").strip()
    ]
    track_coverage = {
        track_name: {
            "covered": any(command.get("track") == track_name for command in commands),
            "command_count": sum(
                1 for command in commands if command.get("track") == track_name
            ),
        }
        for track_name in planned_track_names
    }
    uncovered_work_tracks = [
        track_name
        for track_name, coverage in track_coverage.items()
        if not coverage.get("covered")
    ]
    coverage_issues = [
        {
            "severity": "blocker",
            "code": "work_track_without_runbook_command",
            "track": track_name,
            "message": (
                "The work plan contains a track that has no report-only runbook "
                "command. Add a safe command or remove the track from the work plan."
            ),
        }
        for track_name in uncovered_work_tracks
    ]
    source_issues = [*source_issues, *coverage_issues]
    ok = ok and not coverage_issues
    return {
        "schema": LLM_PREPROCESSING_RUNBOOK_SCHEMA,
        "generated_at": _now_iso(),
        "ok": ok,
        "status": "ready_to_run_report_only_commands" if ok else "blocked_unsafe_work_plan",
        "source_schema": work_plan.get("schema") if isinstance(work_plan, dict) else None,
        "source_generated_at": (
            work_plan.get("generated_at") if isinstance(work_plan, dict) else None
        ),
        "source_work_plan": source_path,
        "source_artifact_hash": source_artifact_hash,
        "artifact_suffix": artifact_suffix,
        "artifact_dir": artifact_dir,
        "agent_queue_path": queue_path,
        "agent_queue_path_source": queue_path_source,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required_for_approval": True,
        "safety_contract": {
            "raw_source_mutation_allowed": False,
            "trusted_status_write_allowed": False,
            "db_apply_allowed": False,
            "guarded_collection_allowed": False,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
        },
        "command_count": len(commands),
        "commands": commands,
        "planned_work_track_count": len(planned_track_names),
        "covered_work_track_count": len(planned_track_names) - len(uncovered_work_tracks),
        "track_coverage": track_coverage,
        "uncovered_work_tracks": uncovered_work_tracks,
        "manual_or_guarded_exclusions": [
            "record-aihr-plan-review-decision",
            "review-training-transition-scenarios --apply",
            "preprocess-ncs-ontology with reset/apply-style mutations",
            "collect-* API commands that persist newly fetched rows",
            "any command that writes human_reviewed, accepted, or reviewed",
        ],
        "source_issues": source_issues,
        "blocker_count": len(source_issues),
    }


def write_llm_preprocessing_backlog_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") or {}
    policy = report.get("policy_snapshot") or {}
    auto_triage = policy.get("auto_triage") or {}
    sampling = policy.get("sampling_plan") or {}
    review_status_policy = (
        report.get("review_status_policy")
        if isinstance(report.get("review_status_policy"), dict)
        else {}
    )
    lines = [
        "# LLM Preprocessing Backlog Map",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status: `{report.get('status')}`",
        f"- report_only: `{str(report.get('report_only')).lower()}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        f"- db_writes: `{str(report.get('db_writes')).lower()}`",
        f"- approval_claim: `{str(report.get('approval_claim')).lower()}`",
        "",
        "## Review Status Policy",
        "",
        f"- human_decision_required_for_status_update: `{str(review_status_policy.get('human_decision_required_for_status_update')).lower()}`",
        f"- forbidden_automatic_statuses: `{json.dumps(review_status_policy.get('forbidden_automatic_statuses') or [], ensure_ascii=False)}`",
        f"- non_approval_statuses: `{json.dumps(review_status_policy.get('non_approval_statuses') or [], ensure_ascii=False)}`",
        "",
        "## Headline Counts",
        "",
        f"- Raw KSA rows: {summary.get('raw_ksa_rows')}",
        f"- Label candidate rows: {summary.get('label_candidate_rows')}",
        f"- Human-reviewed label rows: {summary.get('human_reviewed_label_rows')}",
        f"- Pending label rows not trusted: {summary.get('pending_label_rows_not_trusted')}",
        f"- Distinct normalized label keys: {summary.get('distinct_normalized_label_keys')}",
        f"- Concepts with label candidates: {summary.get('distinct_concepts_with_label_candidates')}",
        f"- Ontology concepts: {summary.get('ontology_concepts')}",
        f"- Human-reviewed ontology concepts: {summary.get('ontology_concepts_human_reviewed')}",
        f"- Meaning candidate rows: {summary.get('meaning_candidate_rows')}",
        f"- Task-KSA concept relation rows: {summary.get('task_ksa_concept_relation_rows')}",
        f"- Training goal concept link rows: {summary.get('training_goal_concept_link_rows')}",
        f"- Training course concept link rows: {summary.get('training_course_concept_link_rows')}",
        f"- Unresolved quality issue rows: {summary.get('unresolved_quality_issue_rows')}",
        "",
    ]
    if auto_triage:
        lines.extend(
            [
                "## Policy Snapshot",
                "",
                f"- Auto-triage status: `{auto_triage.get('status')}`",
                f"- Auto-triage candidate count: {auto_triage.get('candidate_count')}",
                f"- Auto-triage decision row count: {auto_triage.get('full_scope_decision_row_count')}",
                f"- Manual-review-recommended rows: {auto_triage.get('full_scope_manual_review_recommended_count')}",
                f"- Classification counts: `{json.dumps(auto_triage.get('classification_v2_counts'), ensure_ascii=False)}`",
                "",
            ]
        )
    if sampling:
        lines.extend(
            [
                "## Sampling Plan",
                "",
                f"- Recommended sample rows total: {sampling.get('recommended_sample_rows_total')}",
                f"- Decision rows total from major rollup: {sampling.get('decision_rows_total_from_major_rollup')}",
                f"- Estimated click reduction ratio: {sampling.get('estimated_click_reduction_ratio')}",
                "",
            ]
        )
    source_issues = [
        issue for issue in report.get("source_issues") or [] if isinstance(issue, dict)
    ]
    if source_issues:
        lines.extend(["## Source Issues", ""])
        for issue in source_issues:
            lines.append(
                "- "
                f"{issue.get('severity')}:{issue.get('code')} "
                f"{issue.get('sidecar') or ''}.{issue.get('field') or ''} "
                f"{issue.get('message')}"
            )
        lines.append("")
    estimates = report.get("review_time_estimates") or []
    lines.extend(["## Review Time", ""])
    for item in estimates[:12]:
        lines.append(
            "- "
            f"{item.get('review_set')} at {item.get('seconds_per_item')}s/item: "
            f"{item.get('hours')}h ({item.get('eight_hour_days')} eight-hour days)"
        )
    lines.extend(["", "## Safe Next LLM Work", ""])
    for item in report.get("recommended_next_llm_work") or []:
        lines.append(
            f"- {item.get('track')}: {item.get('automation_role')} Human gate: {item.get('human_gate')}"
        )
    lines.extend(["", "## Forbidden Without Operator Approval", ""])
    for item in report.get("forbidden_without_explicit_operator_approval") or []:
        lines.append(f"- {item}")
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_llm_preprocessing_work_plan_markdown(
    report: dict[str, Any], out_path: Path
) -> None:
    summary = report.get("input_summary") if isinstance(report.get("input_summary"), dict) else {}
    lines = [
        "# LLM Preprocessing Work Plan",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status: `{report.get('status')}`",
        f"- report_only: `{str(report.get('report_only')).lower()}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        f"- db_writes: `{str(report.get('db_writes')).lower()}`",
        f"- approval_claim: `{str(report.get('approval_claim')).lower()}`",
        f"- source_schema: `{report.get('source_schema')}`",
        f"- source_backlog_map: `{report.get('source_backlog_map')}`",
        f"- source_artifact_hash: `{report.get('source_artifact_hash')}`",
        f"- next_action: `{report.get('next_action')}`",
        "",
        "## Safety Contract",
        "",
    ]
    safety_contract = (
        report.get("safety_contract") if isinstance(report.get("safety_contract"), dict) else {}
    )
    artifact_policy = (
        report.get("artifact_policy") if isinstance(report.get("artifact_policy"), dict) else {}
    )
    lines.extend(
        [
            f"- raw_source_mutation_allowed: `{str(safety_contract.get('raw_source_mutation_allowed')).lower()}`",
            f"- trusted_status_write_allowed: `{str(safety_contract.get('trusted_status_write_allowed')).lower()}`",
            f"- source_payload_exposed: `{str(safety_contract.get('source_payload_exposed')).lower()}`",
            f"- forbidden_automatic_statuses: `{json.dumps(safety_contract.get('forbidden_automatic_statuses') or [], ensure_ascii=False)}`",
            f"- non_approval_statuses: `{json.dumps(safety_contract.get('non_approval_statuses') or [], ensure_ascii=False)}`",
            f"- db_apply_allowed: `{str(artifact_policy.get('db_apply_allowed')).lower()}`",
            f"- guarded_collection_allowed: `{str(artifact_policy.get('guarded_collection_allowed')).lower()}`",
            f"- operator_decision_fields_auto_filled: `{str(artifact_policy.get('operator_decision_fields_auto_filled')).lower()}`",
            "",
        ]
    )
    lines.extend(
        [
            "## Input Summary",
            "",
            f"- Raw KSA rows: {summary.get('raw_ksa_rows')}",
            f"- Label candidate rows: {summary.get('label_candidate_rows')}",
            f"- Pending label rows not trusted: {summary.get('pending_label_rows_not_trusted')}",
            f"- Human-reviewed label rows: {summary.get('human_reviewed_label_rows')}",
            f"- Meaning candidate rows: {summary.get('meaning_candidate_rows')}",
            f"- Term-definition candidate rows: {summary.get('term_definition_candidate_rows')}",
            f"- Task-KSA concept relation rows: {summary.get('task_ksa_concept_relation_rows')}",
            f"- Training goal concept link rows: {summary.get('training_goal_concept_link_rows')}",
            f"- Training course concept link rows: {summary.get('training_course_concept_link_rows')}",
            f"- Unresolved quality issue rows: {summary.get('unresolved_quality_issue_rows')}",
            f"- Query alias candidate rows: {summary.get('query_alias_candidate_rows')}",
            f"- Recommended sample rows total: {summary.get('recommended_sample_rows_total')}",
            f"- Estimated click reduction ratio: {summary.get('estimated_click_reduction_ratio')}",
            "",
            "## Priority Tracks",
            "",
        ]
    )
    for track in report.get("work_tracks") or []:
        if not isinstance(track, dict):
            continue
        lines.extend(
            [
                f"### {track.get('priority')} {track.get('track')}",
                "",
                f"- input_rows: {track.get('input_rows')}",
                f"- human_gate: {track.get('human_gate')}",
                "- automation_scope:",
            ]
        )
        for item in track.get("automation_scope") or []:
            lines.append(f"  - {item}")
        lines.append("- acceptance_checks:")
        for item in track.get("acceptance_checks") or []:
            lines.append(f"  - {item}")
        lines.append("")
    lines.extend(["## Eight-Hour Run Plan", ""])
    for slot in report.get("eight_hour_run_plan") or []:
        if not isinstance(slot, dict):
            continue
        lines.append(
            f"- {slot.get('slot')}: {slot.get('focus')} -> {slot.get('expected_output')}"
        )
    lines.extend(["", "## Not Recommended For LLM Run", ""])
    for item in report.get("not_recommended_for_llm_run") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Acceptance Criteria", ""])
    for item in report.get("acceptance_criteria") or []:
        lines.append(f"- {item}")
    source_issues = [
        issue for issue in report.get("source_issues") or [] if isinstance(issue, dict)
    ]
    if source_issues:
        lines.extend(["", "## Source Issues", ""])
        for issue in source_issues:
            lines.append(
                "- "
                f"{issue.get('severity')}:{issue.get('code')} "
                f"{issue.get('field') or ''} {issue.get('message')}"
            )
    lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")


def write_llm_preprocessing_runbook_markdown(
    report: dict[str, Any], out_path: Path
) -> None:
    lines = [
        "# LLM Preprocessing Runbook",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- status: `{report.get('status')}`",
        f"- report_only: `{str(report.get('report_only')).lower()}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        f"- db_writes: `{str(report.get('db_writes')).lower()}`",
        f"- approval_claim: `{str(report.get('approval_claim')).lower()}`",
        f"- source_work_plan: `{report.get('source_work_plan')}`",
        f"- source_artifact_hash: `{report.get('source_artifact_hash')}`",
        f"- artifact_suffix: `{report.get('artifact_suffix')}`",
        f"- command_count: `{report.get('command_count')}`",
        "",
        "## Safety Contract",
        "",
    ]
    safety_contract = (
        report.get("safety_contract")
        if isinstance(report.get("safety_contract"), dict)
        else {}
    )
    lines.extend(
        [
            f"- raw_source_mutation_allowed: `{str(safety_contract.get('raw_source_mutation_allowed')).lower()}`",
            f"- trusted_status_write_allowed: `{str(safety_contract.get('trusted_status_write_allowed')).lower()}`",
            f"- db_apply_allowed: `{str(safety_contract.get('db_apply_allowed')).lower()}`",
            f"- guarded_collection_allowed: `{str(safety_contract.get('guarded_collection_allowed')).lower()}`",
            f"- forbidden_automatic_statuses: `{json.dumps(safety_contract.get('forbidden_automatic_statuses') or [], ensure_ascii=False)}`",
            "",
            "## Track Coverage",
            "",
            f"- planned_work_track_count: `{report.get('planned_work_track_count')}`",
            f"- covered_work_track_count: `{report.get('covered_work_track_count')}`",
            f"- uncovered_work_tracks: `{json.dumps(report.get('uncovered_work_tracks') or [], ensure_ascii=False)}`",
            "",
            "## Commands",
            "",
        ]
    )
    for index, command in enumerate(report.get("commands") or [], start=1):
        if not isinstance(command, dict):
            continue
        command_line = " ".join(str(part) for part in command.get("command") or [])
        lines.extend(
            [
                f"### {index}. {command.get('stage')} / {command.get('track')}",
                "",
                f"- purpose: {command.get('purpose')}",
                f"- mutation_policy: `{command.get('mutation_policy')}`",
                f"- status_update_allowed: `{str(command.get('status_update_allowed')).lower()}`",
                f"- db_writes: `{str(command.get('db_writes')).lower()}`",
                f"- approval_claim: `{str(command.get('approval_claim')).lower()}`",
                f"- command: `{command_line}`",
                "- expected_outputs:",
            ]
        )
        for output in command.get("expected_outputs") or []:
            lines.append(f"  - `{output}`")
        lines.append("- acceptance_checks:")
        for check in command.get("acceptance_checks") or []:
            lines.append(f"  - {check}")
        lines.append("")
    exclusions = report.get("manual_or_guarded_exclusions") or []
    if exclusions:
        lines.extend(["## Manual Or Guarded Exclusions", ""])
        for item in exclusions:
            lines.append(f"- {item}")
        lines.append("")
    source_issues = [
        issue for issue in report.get("source_issues") or [] if isinstance(issue, dict)
    ]
    if source_issues:
        lines.extend(["## Source Issues", ""])
        for issue in source_issues:
            lines.append(
                "- "
                f"{issue.get('severity')}:{issue.get('code')} "
                f"{issue.get('field') or ''} {issue.get('message')}"
            )
        lines.append("")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines), encoding="utf-8")
