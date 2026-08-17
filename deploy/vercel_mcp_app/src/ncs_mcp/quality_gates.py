from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from ncs_mcp.api_quality import (
    API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE,
    API_ELEMENT_UNMATCHED_ISSUE_TYPE,
    normalize_api_element_issue_type,
)
from ncs_mcp.job_base_api import job_base_summary
from ncs_mcp.qualification_api import qualification_retry_hygiene_report, qualification_summary
from ncs_mcp.refinement import refinement_stats
from ncs_mcp.review_safety import (
    REVIEW_PACKET_EXTENSIONS,
    evidence_refs_json_is_nonempty_string_list,
    resolve_repo_reports_artifact,
    review_packet_sha256,
)
from ncs_mcp.training_recommendation import (
    TRUSTED_TRANSITION_REVIEW_STATUSES,
    TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
    evaluate_training_transition_scenarios,
)


PASS = "pass"
WARN = "warn"
FAIL = "fail"
HUMAN_TRUSTED_LABEL_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
TRUSTED_LABEL_REVIEW_STATUSES = HUMAN_TRUSTED_LABEL_REVIEW_STATUSES
AUTOMATED_REVIEWER_IDS = ("dashboard", "mcp", "automation", "automated_eval_gate", "system")
TRUSTED_REVIEW_PACKET_EXTENSIONS = REVIEW_PACKET_EXTENSIONS
TRUSTED_TRANSITION_REVIEW_AUDIT_ACTION = "packet_backed_human_review"
ALWAYS_SHOW_DETAIL_GATE_NAMES = {
    "transition_eval:job_base_signal_surface",
}
NON_HR_SURFACE_SMOKE_CONTRACTS = {
    "non_hr_query_smoke": {
        "schema": "ncs_non_hr_query_smoke_v1",
        "requires_education_plan_contract": False,
    },
    "non_hr_transition_smoke": {
        "schema": "ncs_non_hr_transition_smoke_v1",
        "requires_education_plan_contract": False,
    },
    "non_hr_education_plan_smoke": {
        "schema": "ncs_non_hr_education_plan_smoke_v1",
        "requires_education_plan_contract": True,
    },
}
NON_HR_SURFACE_SENSITIVE_VALUE_MARKERS = {
    "authKey",
    "serviceKey",
    "service_key",
    "apiKey",
    "api_key",
    "certKey",
    "NCS_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
    "Authorization",
    "Bearer",
}
NON_HR_SURFACE_EXACT_SENSITIVE_MARKERS = {
    "source_payload",
    "source_json",
    "source_rows",
    "relation_id",
    "raw_payload",
    "raw_response",
}
NON_HR_SURFACE_SENSITIVE_MARKERS = (
    NON_HR_SURFACE_SENSITIVE_VALUE_MARKERS | NON_HR_SURFACE_EXACT_SENSITIVE_MARKERS
)
NON_HR_SURFACE_REQUIRED_GUIDE_TRACE_CODES = {
    "job_scope",
    "task_ksa",
    "course_link",
    "required_optional",
    "level_delivery",
    "human_review",
}
NON_HR_SURFACE_REQUIRED_GUIDE_WORKFLOW_STAGE_CODES = {
    "C1-1",
    "C1-2",
    "C2-1",
    "C2-2",
}
NON_HR_SURFACE_REQUIRED_REVIEW_POLICY_FLAGS = {
    "report_only_smoke_check",
    "uses_readonly_sqlite_connection",
    "recommendation_calls_use_save_false",
    "do_not_write_human_reviewed_accepted_reviewed",
    "recommendations_are_not_official_approval",
    "sqf_and_learning_modules_must_not_be_active_sources",
    "raw_ncs_tables_not_mutated",
}
ONTOLOGY_READINESS_TABLES = [
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    "ontology_concepts",
    "ontology_concept_label_candidates",
    "ksa_concept_links",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "ksa_meaning_candidates",
    "task_ksa_concept_relations",
    "task_similarity_links",
    "ncs_training_courses",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
    "education_recommendation_evidence",
]


def _trusted_label_review_status_sql() -> str:
    return ",".join("?" for _ in TRUSTED_LABEL_REVIEW_STATUSES)


def _human_trusted_label_review_status_sql() -> str:
    return ",".join("?" for _ in HUMAN_TRUSTED_LABEL_REVIEW_STATUSES)


def _audited_label_review_exists_sql() -> str:
    automated_placeholders = ",".join("?" for _ in AUTOMATED_REVIEWER_IDS)
    return f"""
        EXISTS (
            SELECT 1
            FROM review_audit_log audit
            WHERE audit.entity_type = 'ontology_concept_label_candidate'
              AND audit.entity_id = CAST(label.label_id AS TEXT)
              AND audit.new_status = label.review_status
              AND TRIM(COALESCE(audit.reviewer_id, '')) <> ''
              AND LOWER(TRIM(audit.reviewer_id)) NOT IN ({automated_placeholders})
              AND TRIM(COALESCE(audit.notes, '')) <> ''
        )
    """

QUALITY_GATE_TABLES = sorted(
    set(
        ONTOLOGY_READINESS_TABLES
        + [
            "quality_issues",
            "refinement_jobs",
            "classifications",
            "competency_elements",
            "ncs_career_paths",
            "ncs_qualification_items",
            "ncs_unit_qualification_links",
            "ncs_qualification_collection_status",
            "ncs_job_base_competencies",
            "ncs_job_base_factors",
            "ncs_unit_job_base_links",
            "training_transition_gold_scenarios",
        ]
    )
)


def _add_gate(
    gates: list[dict[str, Any]],
    *,
    name: str,
    status: str,
    message: str,
    value: Any = None,
    threshold: Any = None,
    details: dict[str, Any] | None = None,
) -> None:
    gate: dict[str, Any] = {
        "name": name,
        "status": status,
        "message": message,
    }
    if value is not None:
        gate["value"] = value
    if threshold is not None:
        gate["threshold"] = threshold
    if details:
        gate["details"] = details
    gates.append(gate)


def _read_json_object(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return payload, text


def _non_hr_surface_sensitive_markers(text: str) -> list[str]:
    lowered_text = text.lower()
    markers: set[str] = set()
    for marker in NON_HR_SURFACE_SENSITIVE_MARKERS:
        marker_lower = marker.lower()
        if marker in NON_HR_SURFACE_EXACT_SENSITIVE_MARKERS:
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(marker)}(?![A-Za-z0-9_])", text):
                markers.add(marker)
            continue
        if marker_lower in lowered_text:
            markers.add(marker)
    return sorted(markers)


def _overall_status(gates: list[dict[str, Any]]) -> str:
    if any(gate["status"] == FAIL for gate in gates):
        return FAIL
    if any(gate["status"] == WARN for gate in gates):
        return WARN
    return PASS


def _status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return {
        str(row.get("collection_status") or "unknown"): int(row.get("unit_count") or 0)
        for row in rows
    }


def _non_hr_surface_smoke_issues(
    name: str,
    payload: dict[str, Any],
    *,
    artifact_text: str,
) -> list[str]:
    contract = NON_HR_SURFACE_SMOKE_CONTRACTS[name]
    issues: list[str] = []
    artifact_sensitive_markers = _non_hr_surface_sensitive_markers(artifact_text)
    if artifact_sensitive_markers:
        issues.append(
            "artifact_sensitive_markers:"
            + ",".join(artifact_sensitive_markers[:10])
        )
    if payload.get("schema") != contract["schema"]:
        issues.append(f"schema:{payload.get('schema')}")
    if payload.get("ok") is not True:
        issues.append(f"ok:{payload.get('ok')}")
    if payload.get("report_only") is not True:
        issues.append(f"report_only:{payload.get('report_only')}")
    for field in ("db_writes", "status_update_allowed", "approval_claim"):
        if payload.get(field) is not False:
            issues.append(f"{field}:{payload.get(field)}")
    if payload.get("human_decision_required") is not False:
        issues.append(f"human_decision_required:{payload.get('human_decision_required')}")
    review_policy = payload.get("review_policy")
    if not isinstance(review_policy, dict):
        issues.append("review_policy")
    else:
        for flag in sorted(NON_HR_SURFACE_REQUIRED_REVIEW_POLICY_FLAGS):
            if review_policy.get(flag) is not True:
                issues.append(f"review_policy.{flag}:{review_policy.get(flag)}")
    if payload.get("source_payload_exposed") is not False:
        issues.append(f"source_payload_exposed:{payload.get('source_payload_exposed')}")
    if payload.get("sensitive_markers"):
        issues.append("sensitive_markers")
    if int(payload.get("case_count") or 0) <= 0:
        issues.append("case_count")
    if int(payload.get("failed_count") or 0) != 0:
        issues.append(f"failed_count:{payload.get('failed_count')}")

    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        issues.append("rows")
        return issues

    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append(f"row_{index}:not_object")
            continue
        if row.get("ok") is not True:
            issues.append(f"row_{index}:ok:{row.get('ok')}")
        for field in ("db_writes", "status_update_allowed", "approval_claim"):
            if row.get(field) is not False:
                issues.append(f"row_{index}:{field}:{row.get(field)}")
        if row.get("sqf_used") is not False:
            issues.append(f"row_{index}:sqf_used:{row.get('sqf_used')}")
        if row.get("learning_modules_used") is not False:
            issues.append(f"row_{index}:learning_modules_used:{row.get('learning_modules_used')}")
        if contract["requires_education_plan_contract"]:
            if row.get("plan_ok") is not True:
                issues.append(f"row_{index}:plan_ok:{row.get('plan_ok')}")
            if row.get("guide_trace_schema") != "aihr_training_system_guide_trace_v1":
                issues.append(f"row_{index}:guide_trace_schema:{row.get('guide_trace_schema')}")
            guide_trace_codes = {
                str(code)
                for code in (row.get("guide_trace_check_codes") or [])
                if str(code)
            } if isinstance(row.get("guide_trace_check_codes"), list) else set()
            if not guide_trace_codes:
                issues.append(f"row_{index}:guide_trace_check_codes")
            for code in sorted(NON_HR_SURFACE_REQUIRED_GUIDE_TRACE_CODES - guide_trace_codes):
                issues.append(f"row_{index}:guide_trace_check_codes_missing:{code}")
            guide_stage_codes = {
                str(code)
                for code in (row.get("guide_workflow_stage_codes") or [])
                if str(code)
            } if isinstance(row.get("guide_workflow_stage_codes"), list) else set()
            if not guide_stage_codes:
                issues.append(f"row_{index}:guide_workflow_stage_codes")
            for code in sorted(
                NON_HR_SURFACE_REQUIRED_GUIDE_WORKFLOW_STAGE_CODES - guide_stage_codes
            ):
                issues.append(f"row_{index}:guide_workflow_stage_codes_missing:{code}")
            if int(row.get("matrix_rows") or 0) <= 0:
                issues.append(f"row_{index}:matrix_rows")
            if int(row.get("recommended_path_stage_count") or 0) <= 0:
                issues.append(f"row_{index}:recommended_path_stage_count")
            if row.get("query_route_schema") != "ncs_query_route_v1":
                issues.append(f"row_{index}:query_route_schema:{row.get('query_route_schema')}")
            if row.get("query_route_tool") != "plan_ncs_education_path":
                issues.append(f"row_{index}:query_route_tool:{row.get('query_route_tool')}")
            if row.get("query_route_contract_schema") != "ncs_query_route_v1":
                issues.append(
                    f"row_{index}:query_route_contract_schema:{row.get('query_route_contract_schema')}"
                )
            if row.get("query_route_contract_primary_tool") != "plan_ncs_education_path":
                issues.append(
                    "row_"
                    f"{index}:query_route_contract_primary_tool:"
                    f"{row.get('query_route_contract_primary_tool')}"
                )
            route_fingerprint = row.get("query_route_fingerprint")
            contract_fingerprint = row.get("query_route_contract_fingerprint")
            if not route_fingerprint:
                issues.append(f"row_{index}:query_route_fingerprint")
            if not contract_fingerprint:
                issues.append(f"row_{index}:query_route_contract_fingerprint")
            if route_fingerprint and contract_fingerprint and route_fingerprint != contract_fingerprint:
                issues.append(f"row_{index}:query_route_fingerprint_mismatch")
            expected_chain = row.get("query_route_expected_tool_chain")
            if not isinstance(expected_chain, list):
                issues.append(f"row_{index}:query_route_expected_tool_chain")
            else:
                for tool_name in ("plan_ncs_education_path", "recommend_training_transition"):
                    if tool_name not in expected_chain:
                        issues.append(f"row_{index}:query_route_expected_tool_chain_missing:{tool_name}")
            for field in (
                "missing_matrix_fields",
                "missing_plan_fields",
                "missing_guide_trace_fields",
                "missing_query_route_fields",
            ):
                if row.get(field):
                    issues.append(f"row_{index}:{field}")
    return issues


def _add_non_hr_surface_smoke_gates(
    gates: list[dict[str, Any]],
    artifact_paths: dict[str, Path] | None,
) -> dict[str, Any] | None:
    if not artifact_paths:
        return None
    evidence: dict[str, Any] = {}
    for name, contract in NON_HR_SURFACE_SMOKE_CONTRACTS.items():
        path = artifact_paths.get(name)
        if path is None:
            _add_gate(
                gates,
                name=f"non_hr_surface:{name}",
                status=FAIL,
                message="Non-HR surface smoke artifact was not supplied.",
                value=None,
                threshold="supplied artifact",
            )
            evidence[name] = {
                "supplied": False,
                "ok": False,
                "required_schema": contract["schema"],
            }
            continue
        try:
            payload, artifact_text = _read_json_object(path)
            issues = _non_hr_surface_smoke_issues(
                name,
                payload,
                artifact_text=artifact_text,
            )
        except Exception as exc:
            _add_gate(
                gates,
                name=f"non_hr_surface:{name}",
                status=FAIL,
                message="Non-HR surface smoke artifact could not be read.",
                value=str(path),
                details={"error": type(exc).__name__, "message": str(exc)},
            )
            evidence[name] = {
                "supplied": True,
                "path": str(path),
                "ok": False,
                "error": type(exc).__name__,
            }
            continue
        status = PASS if not issues else FAIL
        _add_gate(
            gates,
            name=f"non_hr_surface:{name}",
            status=status,
            message=(
                "Non-HR surface smoke artifact satisfies report-only contract."
                if status == PASS
                else "Non-HR surface smoke artifact violates report-only contract."
            ),
            value=int(payload.get("ok_count") or 0),
            threshold=f"schema={contract['schema']}, failed_count=0",
            details={
                "path": str(path),
                "schema": payload.get("schema"),
                "case_count": payload.get("case_count"),
                "ok_count": payload.get("ok_count"),
                "failed_count": payload.get("failed_count"),
                "source_payload_exposed": payload.get("source_payload_exposed"),
                "issues": issues[:20],
            },
        )
        evidence[name] = {
            "supplied": True,
            "path": str(path),
            "ok": status == PASS,
            "schema": payload.get("schema"),
            "case_count": payload.get("case_count"),
            "ok_count": payload.get("ok_count"),
            "failed_count": payload.get("failed_count"),
            "source_payload_exposed": payload.get("source_payload_exposed"),
            "issues": issues,
        }
    return evidence


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _missing_tables(conn: sqlite3.Connection, table_names: list[str]) -> list[str]:
    rows = conn.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
        """
    ).fetchall()
    present = {str(row["name"]) for row in rows}
    return [table for table in table_names if table not in present]


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


def _resolve_review_packet_artifact(source_decision_packet: str | None) -> Path | None:
    return resolve_repo_reports_artifact(
        source_decision_packet,
        extensions=TRUSTED_REVIEW_PACKET_EXTENSIONS,
    )


def _evidence_refs_json_is_packet_backing_list(value: str | None) -> bool:
    return evidence_refs_json_is_nonempty_string_list(value)


def _table_count(conn: sqlite3.Connection, table_name: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])


def _validate_ontology_readiness_readonly(conn: sqlite3.Connection) -> dict[str, Any]:
    missing = _missing_tables(conn, ONTOLOGY_READINESS_TABLES)
    if missing:
        return {
            "ok": False,
            "counts": {},
            "metrics": {},
            "issues": [
                {
                    "severity": "error",
                    "check": "schema",
                    "detail": f"Missing ontology readiness tables: {', '.join(missing)}",
                }
            ],
            "note": "Read-only ontology readiness validation could not run because schema tables are missing.",
        }

    counts = {
        table: _table_count(conn, table)
        for table in [
            "competency_units",
            "competency_elements",
            "performance_criteria",
            "ksa_items",
            "ontology_concepts",
            "ontology_concept_label_candidates",
            "ksa_atomic_items",
            "ksa_atomic_concept_links",
            "ksa_meaning_candidates",
            "task_ksa_concept_relations",
            "task_similarity_links",
            "ncs_training_courses",
            "ncs_training_course_concept_links",
            "ncs_training_course_element_links",
            "training_goal_concept_links",
            "training_delivery_relations",
        ]
    }
    issues: list[dict[str, Any]] = []

    unlinked_ksa_items = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_items ki
            LEFT JOIN ksa_concept_links link ON link.ksa_id = ki.ksa_id
            WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
              AND link.link_id IS NULL
            """
        ).fetchone()[0]
    )
    if unlinked_ksa_items:
        issues.append(
            {
                "severity": "error",
                "check": "ksa_concept_links",
                "detail": f"{unlinked_ksa_items} non-empty KSA rows are not linked to ontology concepts.",
            }
        )

    atomic_without_concepts = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_atomic_items atom
            LEFT JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
            WHERE link.link_id IS NULL
            """
        ).fetchone()[0]
    )
    if atomic_without_concepts:
        issues.append(
            {
                "severity": "error",
                "check": "atomic_ksa_concept_links",
                "detail": f"{atomic_without_concepts} atomic KSA rows are not linked to concepts.",
            }
        )

    tasks_with_similarity = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT source_criteria_id)
            FROM task_similarity_links
            """
        ).fetchone()[0]
    )
    task_similarity_coverage = (
        round(tasks_with_similarity / counts["performance_criteria"], 4)
        if counts["performance_criteria"]
        else 0.0
    )
    if counts["performance_criteria"] and task_similarity_coverage == 0:
        issues.append(
            {
                "severity": "error",
                "check": "task_similarity_links",
                "detail": "No task similarity links are available.",
            }
        )

    concepts_with_meanings = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT concept_id)
            FROM ksa_meaning_candidates
            """
        ).fetchone()[0]
    )
    ksa_meaning_coverage = (
        round(concepts_with_meanings / counts["ontology_concepts"], 4)
        if counts["ontology_concepts"]
        else 0.0
    )
    llm_reviewed_meaning_candidate_statuses = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE review_status = 'llm_reviewed'
            """
        ).fetchone()[0]
    )
    needs_review_meaning_candidate_statuses = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE review_status = 'needs_review'
            """
        ).fetchone()[0]
    )
    candidate_meaning_candidate_statuses = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE review_status = 'candidate'
            """
        ).fetchone()[0]
    )
    candidate_definitions = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE definition_status = 'candidate'
            """
        ).fetchone()[0]
    )
    concepts_with_label_candidates = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT concept_id)
            FROM ontology_concept_label_candidates
            """
        ).fetchone()[0]
    )
    shortened_label_candidates = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE source_method = 'rule_based_short_label_candidate'
            """
        ).fetchone()[0]
    )
    label_candidates_missing_provenance = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE (source_ksa_id IS NULL AND source_atomic_id IS NULL)
               OR TRIM(COALESCE(source_text, '')) = ''
               OR TRIM(COALESCE(label_text, '')) = ''
            """
        ).fetchone()[0]
    )
    trusted_label_status_sql = _trusted_label_review_status_sql()
    human_trusted_label_status_sql = _human_trusted_label_review_status_sql()
    audited_label_review_exists_sql = _audited_label_review_exists_sql()
    trusted_label_candidate_statuses = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            WHERE label.review_status IN ({trusted_label_status_sql})
            """,
            TRUSTED_LABEL_REVIEW_STATUSES,
        ).fetchone()[0]
    )
    llm_reviewed_label_candidate_statuses = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            WHERE label.review_status = 'llm_reviewed'
            """
        ).fetchone()[0]
    )
    audited_trusted_label_candidate_statuses = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            WHERE label.review_status IN ({human_trusted_label_status_sql})
              AND {audited_label_review_exists_sql}
            """,
            (*HUMAN_TRUSTED_LABEL_REVIEW_STATUSES, *AUTOMATED_REVIEWER_IDS),
        ).fetchone()[0]
    )
    unaudited_trusted_label_candidate_statuses = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates label
            WHERE label.review_status IN ({human_trusted_label_status_sql})
              AND NOT {audited_label_review_exists_sql}
            """,
            (*HUMAN_TRUSTED_LABEL_REVIEW_STATUSES, *AUTOMATED_REVIEWER_IDS),
        ).fetchone()[0]
    )
    if counts["ontology_concept_label_candidates"] and label_candidates_missing_provenance:
        issues.append(
            {
                "severity": "error",
                "check": "ontology_concept_label_candidates.provenance",
                "detail": (
                    f"{label_candidates_missing_provenance} label candidates are missing "
                    "source ids, source text, or label text."
                ),
            }
        )
    if unaudited_trusted_label_candidate_statuses:
        issues.append(
            {
                "severity": "error",
                "check": "ontology_concept_label_candidates.review_status",
                "detail": (
                    f"{unaudited_trusted_label_candidate_statuses} label candidates have trusted "
                    "review statuses without matching human review audit evidence."
                ),
            }
        )

    linked_training_courses = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT training_course_id)
            FROM ncs_training_course_concept_links
            """
        ).fetchone()[0]
    )
    element_linked_training_courses = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT training_course_id)
            FROM ncs_training_course_element_links
            """
        ).fetchone()[0]
    )
    goal_linked_training_courses = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT training_course_id)
            FROM training_goal_concept_links
            """
        ).fetchone()[0]
    )
    delivery_linked_training_courses = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT training_course_id)
            FROM training_delivery_relations
            """
        ).fetchone()[0]
    )
    training_course_concept_coverage = (
        round(linked_training_courses / counts["ncs_training_courses"], 4)
        if counts["ncs_training_courses"]
        else 0.0
    )
    training_course_element_coverage = (
        round(element_linked_training_courses / counts["ncs_training_courses"], 4)
        if counts["ncs_training_courses"]
        else 0.0
    )
    training_goal_concept_coverage = (
        round(goal_linked_training_courses / counts["ncs_training_courses"], 4)
        if counts["ncs_training_courses"]
        else 0.0
    )
    training_delivery_coverage = (
        round(delivery_linked_training_courses / counts["ncs_training_courses"], 4)
        if counts["ncs_training_courses"]
        else 0.0
    )
    if counts["ncs_training_courses"] and linked_training_courses == 0:
        issues.append(
            {
                "severity": "warning",
                "check": "training_course_concept_links",
                "detail": "No training courses are linked to KSA concepts.",
            }
        )
    if counts["ncs_training_courses"] and element_linked_training_courses == 0:
        issues.append(
            {
                "severity": "warning",
                "check": "training_course_element_links",
                "detail": "No training courses are linked to competency elements.",
            }
        )
    if counts["ncs_training_courses"] and delivery_linked_training_courses == 0:
        issues.append(
            {
                "severity": "warning",
                "check": "training_delivery_relations",
                "detail": "No training delivery relations are available.",
            }
        )

    saved_training_recommendations = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM education_recommendation_evidence
            WHERE source_table = 'ncs_training_courses'
            """
        ).fetchone()[0]
    )
    training_goal_link_evidence = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM education_recommendation_evidence
            WHERE source_table = 'training_goal_concept_links'
              AND TRIM(COALESCE(source_id, '')) <> ''
            """
        ).fetchone()[0]
    )
    orphan_training_goal_link_evidence = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM education_recommendation_evidence e
            WHERE e.source_table = 'training_goal_concept_links'
              AND TRIM(COALESCE(e.source_id, '')) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM training_goal_concept_links gl
                  WHERE CAST(gl.link_id AS TEXT) = CAST(e.source_id AS TEXT)
              )
            """
        ).fetchone()[0]
    )

    return {
        "ok": not any(issue["severity"] == "error" for issue in issues),
        "counts": counts,
        "metrics": {
            "unlinked_ksa_items": unlinked_ksa_items,
            "atomic_without_concepts": atomic_without_concepts,
            "tasks_with_similarity": tasks_with_similarity,
            "task_similarity_coverage": task_similarity_coverage,
            "concepts_with_ksa_meanings": concepts_with_meanings,
            "ksa_meaning_coverage": ksa_meaning_coverage,
            "llm_reviewed_meaning_candidate_statuses": llm_reviewed_meaning_candidate_statuses,
            "needs_review_meaning_candidate_statuses": needs_review_meaning_candidate_statuses,
            "candidate_meaning_candidate_statuses": candidate_meaning_candidate_statuses,
            "candidate_definitions": candidate_definitions,
            "concepts_with_label_candidates": concepts_with_label_candidates,
            "shortened_label_candidates": shortened_label_candidates,
            "label_candidates_missing_provenance": label_candidates_missing_provenance,
            "trusted_label_candidate_statuses": trusted_label_candidate_statuses,
            "llm_reviewed_label_candidate_statuses": llm_reviewed_label_candidate_statuses,
            "audited_trusted_label_candidate_statuses": audited_trusted_label_candidate_statuses,
            "unaudited_trusted_label_candidate_statuses": unaudited_trusted_label_candidate_statuses,
            "linked_training_courses": linked_training_courses,
            "training_course_concept_coverage": training_course_concept_coverage,
            "element_linked_training_courses": element_linked_training_courses,
            "training_course_element_coverage": training_course_element_coverage,
            "goal_linked_training_courses": goal_linked_training_courses,
            "training_goal_concept_coverage": training_goal_concept_coverage,
            "delivery_linked_training_courses": delivery_linked_training_courses,
            "training_delivery_coverage": training_delivery_coverage,
            "saved_training_recommendations": saved_training_recommendations,
            "training_goal_link_evidence": training_goal_link_evidence,
            "orphan_training_goal_link_evidence": orphan_training_goal_link_evidence,
        },
        "issues": issues,
        "note": "This validates NCS task/KSA/training-course recommendation readiness without schema writes.",
    }


def _transition_scenario_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        str(row["review_status"] or "unknown"): int(row["count"] or 0)
        for row in conn.execute(
            """
            SELECT review_status, COUNT(*) AS count
            FROM training_transition_gold_scenarios
            WHERE review_status != 'rejected'
            GROUP BY review_status
            ORDER BY review_status
            """
        ).fetchall()
    }


def _review_counts(conn) -> dict[str, int]:
    return {
        "human_reviewed_concepts": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concepts
                WHERE review_status = 'human_reviewed'
                """
            ).fetchone()[0]
        ),
        "human_reviewed_goal_links": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM training_goal_concept_links
                WHERE review_status = 'human_reviewed'
                """
            ).fetchone()[0]
        ),
        "human_reviewed_task_relations": int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM task_ksa_concept_relations
                WHERE review_status = 'human_reviewed'
                """
            ).fetchone()[0]
        ),
    }


def _open_quality_issue_counts(conn) -> dict[str, dict[str, int]]:
    counts: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        """
        SELECT issue_type, severity, COUNT(*) AS count
        FROM quality_issues
        WHERE resolved_at IS NULL
        GROUP BY issue_type, severity
        ORDER BY issue_type, severity
        """
    ).fetchall():
        issue_type = str(row["issue_type"] or "unknown")
        severity = str(row["severity"] or "unknown")
        counts.setdefault(issue_type, {})[severity] = int(row["count"] or 0)
    return counts


def _api_element_issue_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {
        API_ELEMENT_UNMATCHED_ISSUE_TYPE: 0,
        API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE: 0,
        "legacy_issue_type_rows": 0,
        "new_issue_type_rows": 0,
    }
    rows = conn.execute(
        """
        SELECT
            qi.issue_type,
            qi.issue_detail,
            COUNT(*) AS count,
            ce.api_match_status
        FROM quality_issues qi
        LEFT JOIN competency_elements ce
          ON ce.element_id = CAST(qi.target_id AS INTEGER)
        WHERE qi.resolved_at IS NULL
          AND qi.target_type = 'element'
          AND qi.issue_type IN ('api_element_unmatched', 'api_element_collection_failure')
        GROUP BY qi.issue_type, qi.issue_detail, ce.api_match_status
        """
    ).fetchall()
    for row in rows:
        source_issue_type = str(row["issue_type"] or "")
        issue_count = int(row["count"] or 0)
        category = normalize_api_element_issue_type(
            source_issue_type,
            issue_detail=row["issue_detail"],
            api_match_status=row["api_match_status"],
        )
        counts[category] = counts.get(category, 0) + issue_count
        if source_issue_type == API_ELEMENT_UNMATCHED_ISSUE_TYPE:
            counts["legacy_issue_type_rows"] += issue_count
        elif source_issue_type == API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE:
            counts["new_issue_type_rows"] += issue_count
    return counts


def _issue_total(counts: dict[str, dict[str, int]], issue_type: str) -> int:
    return sum(counts.get(issue_type, {}).values())


def _active_error_issue_total(counts: dict[str, dict[str, int]]) -> int:
    return sum(severities.get("error", 0) for severities in counts.values())


def _career_path_evidence_counts(conn: sqlite3.Connection) -> dict[str, Any]:
    total = int(conn.execute("SELECT COUNT(*) FROM ncs_career_paths").fetchone()[0])
    matched_units = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ncs_career_paths
            WHERE matched_unit_code IS NOT NULL
            """
        ).fetchone()[0]
    )
    distinct_matched_units = int(
        conn.execute(
            """
            SELECT COUNT(DISTINCT matched_unit_code)
            FROM ncs_career_paths
            WHERE matched_unit_code IS NOT NULL
            """
        ).fetchone()[0]
    )
    return {
        "career_path_count": total,
        "matched_unit_count": matched_units,
        "distinct_matched_unit_count": distinct_matched_units,
        "unit_match_rate": round(matched_units / total, 4) if total else 0.0,
    }


def _add_ontology_gates(
    gates: list[dict[str, Any]],
    validation: dict[str, Any],
) -> None:
    counts = validation.get("counts") or {}
    metrics = validation.get("metrics") or {}
    issues = validation.get("issues") or []

    _add_gate(
        gates,
        name="ontology_validator_ok",
        status=PASS if validation.get("ok") else FAIL,
        message="Ontology validator has no error-severity readiness issues."
        if validation.get("ok")
        else "Ontology validator reported error-severity readiness issues.",
        value={"issue_count": len(issues)},
    )

    for table_name in ("competency_units", "performance_criteria", "ksa_items"):
        count = int(counts.get(table_name) or 0)
        _add_gate(
            gates,
            name=f"core_data_present:{table_name}",
            status=PASS if count > 0 else FAIL,
            message=f"{table_name} has rows." if count > 0 else f"{table_name} is empty.",
            value=count,
            threshold="> 0",
        )

    hard_metric_thresholds = {
        "unlinked_ksa_items": ("== 0", 0),
        "atomic_without_concepts": ("== 0", 0),
    }
    for metric_name, (threshold_label, expected) in hard_metric_thresholds.items():
        value = int(metrics.get(metric_name) or 0)
        _add_gate(
            gates,
            name=f"ontology_metric:{metric_name}",
            status=PASS if value == expected else FAIL,
            message=f"{metric_name} is {expected}." if value == expected else f"{metric_name} must be {expected}.",
            value=value,
            threshold=threshold_label,
        )

    coverage_thresholds = {
        "task_similarity_coverage": 0.995,
        "training_course_concept_coverage": 0.995,
        "training_course_element_coverage": 0.995,
        "training_goal_concept_coverage": 0.995,
        "training_delivery_coverage": 0.999,
    }
    has_tasks = int(counts.get("performance_criteria") or 0) > 0
    has_courses = int(counts.get("ncs_training_courses") or 0) > 0
    for metric_name, threshold in coverage_thresholds.items():
        value = float(metrics.get(metric_name) or 0.0)
        applicable = has_tasks if metric_name == "task_similarity_coverage" else has_courses
        if not applicable:
            status = WARN
            message = f"{metric_name} is not applicable because source rows are empty."
        else:
            status = PASS if value >= threshold else FAIL
            message = (
                f"{metric_name} meets the minimum coverage."
                if status == PASS
                else f"{metric_name} is below the minimum coverage."
            )
        _add_gate(
            gates,
            name=f"ontology_coverage:{metric_name}",
            status=status,
            message=message,
            value=value,
            threshold=f">= {threshold}",
        )

    concept_count = int(counts.get("ontology_concepts") or 0)
    candidate_definitions = int(metrics.get("candidate_definitions") or 0)
    candidate_ratio = round(candidate_definitions / concept_count, 4) if concept_count else 0.0
    _add_gate(
        gates,
        name="review_debt:candidate_definition_ratio",
        status=WARN if concept_count and candidate_ratio > 0.99 else PASS,
        message="Almost all ontology definitions are still candidate definitions."
        if concept_count and candidate_ratio > 0.99
        else "Candidate definition ratio is within the current gate.",
        value=candidate_ratio,
        threshold="<= 0.99",
        details={"candidate_definitions": candidate_definitions, "ontology_concepts": concept_count},
    )

    saved_training_recommendations = int(metrics.get("saved_training_recommendations") or 0)
    _add_gate(
        gates,
        name="recommendation_evidence:saved_training_recommendations",
        status=WARN if saved_training_recommendations == 0 else PASS,
        message="No saved training recommendation evidence exists."
        if saved_training_recommendations == 0
        else "Saved training recommendation evidence exists.",
        value=saved_training_recommendations,
        threshold="> 0",
    )

    training_goal_link_evidence = int(metrics.get("training_goal_link_evidence") or 0)
    orphan_training_goal_link_evidence = int(metrics.get("orphan_training_goal_link_evidence") or 0)
    _add_gate(
        gates,
        name="recommendation_evidence:training_goal_link_references",
        status=WARN if orphan_training_goal_link_evidence else PASS,
        message=(
            "Saved recommendation evidence references missing training-goal concept links."
            if orphan_training_goal_link_evidence
            else "Saved training-goal recommendation evidence references are valid."
        ),
        value=orphan_training_goal_link_evidence,
        threshold="== 0",
        details={"training_goal_link_evidence": training_goal_link_evidence},
    )


def _add_quality_issue_gates(
    gates: list[dict[str, Any]],
    quality_issue_counts: dict[str, dict[str, int]],
    api_element_issue_counts: dict[str, int],
) -> None:
    error_total = _active_error_issue_total(quality_issue_counts)
    _add_gate(
        gates,
        name="quality_issues:active_errors",
        status=FAIL if error_total else PASS,
        message="Active error-severity quality issues exist." if error_total else "No active error-severity quality issues.",
        value=error_total,
        threshold="== 0",
    )

    missing_required_value = _issue_total(quality_issue_counts, "missing_required_value")
    _add_gate(
        gates,
        name="quality_issues:missing_required_value",
        status=FAIL if missing_required_value else PASS,
        message="Missing required values are present." if missing_required_value else "No missing required value issues.",
        value=missing_required_value,
        threshold="== 0",
    )

    warning_thresholds = {
        "criteria_format_issue": 5000,
    }
    for issue_type, threshold in warning_thresholds.items():
        value = _issue_total(quality_issue_counts, issue_type)
        _add_gate(
            gates,
            name=f"quality_issues:{issue_type}",
            status=WARN if value > threshold else PASS,
            message=f"{issue_type} exceeds the warning threshold."
            if value > threshold
            else f"{issue_type} is within the warning threshold.",
            value=value,
            threshold=f"<= {threshold}",
        )

    unmatched_value = int(api_element_issue_counts.get(API_ELEMENT_UNMATCHED_ISSUE_TYPE) or 0)
    collection_failure_value = int(
        api_element_issue_counts.get(API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE) or 0
    )
    _add_gate(
        gates,
        name=f"quality_issues:{API_ELEMENT_UNMATCHED_ISSUE_TYPE}",
        status=WARN if unmatched_value > 200 else PASS,
        message=(
            "True element mismatches exceed the warning threshold."
            if unmatched_value > 200
            else "True element mismatches are within the warning threshold."
        ),
        value=unmatched_value,
        threshold="<= 200",
        details={
            "legacy_issue_type_rows": int(api_element_issue_counts.get("legacy_issue_type_rows") or 0),
            "new_issue_type_rows": int(api_element_issue_counts.get("new_issue_type_rows") or 0),
            "collection_failure_rows": collection_failure_value,
        },
    )
    _add_gate(
        gates,
        name=f"quality_issues:{API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE}",
        status=WARN if collection_failure_value > 200 else PASS,
        message=(
            "API collection failures exceed the warning threshold."
            if collection_failure_value > 200
            else "API collection failures are within the warning threshold."
        ),
        value=collection_failure_value,
        threshold="<= 200",
        details={
            "legacy_issue_type_rows": int(api_element_issue_counts.get("legacy_issue_type_rows") or 0),
            "new_issue_type_rows": int(api_element_issue_counts.get("new_issue_type_rows") or 0),
        },
    )

    suspected_typo = _issue_total(quality_issue_counts, "suspected_typo")
    _add_gate(
        gates,
        name="quality_issues:suspected_typo",
        status=WARN if suspected_typo else PASS,
        message="Suspected typos are present." if suspected_typo else "No suspected typo issues.",
        value=suspected_typo,
        threshold="== 0",
    )

    api_mismatches = _issue_total(quality_issue_counts, "api_value_mismatch") + _issue_total(
        quality_issue_counts,
        "api_element_value_mismatch",
    )
    _add_gate(
        gates,
        name="quality_issues:api_value_mismatches",
        status=WARN if api_mismatches > 20 else PASS,
        message="API value mismatch issues exceed the warning threshold."
        if api_mismatches > 20
        else "API value mismatch issues are within the warning threshold.",
        value=api_mismatches,
        threshold="<= 20",
    )


def _add_review_debt_gates(
    gates: list[dict[str, Any]],
    stats: dict[str, Any],
    review_counts: dict[str, int],
) -> None:
    pending = int((stats.get("refinement_jobs") or {}).get("review_required") or 0)
    _add_gate(
        gates,
        name="review_debt:refinement_jobs_review_required",
        status=WARN if pending > 6000 else PASS,
        message="Pending refinement review jobs exceed the warning threshold."
        if pending > 6000
        else "Pending refinement review jobs are within the warning threshold.",
        value=pending,
        threshold="<= 6000",
    )

    for key, count in review_counts.items():
        _add_gate(
            gates,
            name=f"review_debt:{key}",
            status=WARN if count == 0 else PASS,
            message=f"{key} is still zero." if count == 0 else f"{key} has reviewed evidence.",
            value=count,
            threshold="> 0",
        )


def _add_qualification_gates(
    gates: list[dict[str, Any]],
    summary: dict[str, Any],
    *,
    total_unit_count: int,
    retry_hygiene: dict[str, Any] | None = None,
) -> None:
    status_counts = _status_counts(summary.get("collection_status") or [])
    error_count = int(status_counts.get("error", 0))
    collected_count = int(status_counts.get("collected", 0))
    attempted_count = sum(status_counts.values())
    collection_coverage = round(attempted_count / total_unit_count, 4) if total_unit_count else 0.0

    if total_unit_count == 0:
        coverage_status = WARN
        coverage_message = "Qualification coverage is not applicable because competency units are empty."
    elif collection_coverage < 0.10:
        coverage_status = FAIL
        coverage_message = "Qualification collection coverage is below the fail threshold."
    elif collection_coverage < 0.90:
        coverage_status = WARN
        coverage_message = "Qualification collection coverage is below the warning threshold."
    else:
        coverage_status = PASS
        coverage_message = "Qualification collection coverage is within the current gate."
    _add_gate(
        gates,
        name="qualification:collection_coverage",
        status=coverage_status,
        message=coverage_message,
        value=collection_coverage,
        threshold="warn < 0.90, fail < 0.10",
        details={
            "attempted_unit_count": attempted_count,
            "total_unit_count": total_unit_count,
            "status_counts": status_counts,
        },
    )

    error_share = round(error_count / attempted_count, 4) if attempted_count else None

    if attempted_count == 0:
        status = WARN
        message = "No qualification collection attempts are available; error share is not meaningful."
    elif error_share is not None and error_share > 0.60:
        status = FAIL
        message = "Qualification collection error share exceeds the fail threshold."
    elif error_share is not None and (error_share > 0.35 or error_count > collected_count):
        status = WARN
        message = "Qualification collection error share needs attention."
    else:
        status = PASS
        message = "Qualification collection error share is within the current gate."
    _add_gate(
        gates,
        name="qualification:error_share",
        status=status,
        message=message,
        value=error_share,
        threshold="warn > 0.35 or errors > collected, fail > 0.60",
        details=status_counts,
    )

    if retry_hygiene is not None:
        metadata_gaps = retry_hygiene.get("metadata_gaps") or {}
        metadata_gap_total = sum(int(value or 0) for value in metadata_gaps.values())
        if error_count == 0:
            status = PASS
            message = "No qualification error rows need retry metadata."
        elif metadata_gap_total:
            status = WARN
            message = "Qualification retry metadata has gaps; run qualification-retry-hygiene before broad retries."
        else:
            status = PASS
            message = "Qualification retry metadata is ready for controlled retry."
        _add_gate(
            gates,
            name="qualification:retry_metadata",
            status=status,
            message=message,
            value=metadata_gap_total,
            threshold="0 metadata gaps",
            details={
                "metadata_gaps": metadata_gaps,
                "retry_ready_unit_count": retry_hygiene.get("retry_ready_unit_count"),
                "retry_waiting_unit_count": retry_hygiene.get("retry_waiting_unit_count"),
                "broad_retry_risk": retry_hygiene.get("broad_retry_risk"),
            },
        )


def _add_job_base_gates(
    gates: list[dict[str, Any]],
    summary: dict[str, Any],
) -> None:
    checks = {
        "job_base_competency_count": ("== 10", int(summary.get("job_base_competency_count") or 0), 10, "eq"),
        "job_base_factor_count": ("== 34", int(summary.get("job_base_factor_count") or 0), 34, "eq"),
        "unit_job_base_link_count": (">= 220000", int(summary.get("unit_job_base_link_count") or 0), 220000, "gte"),
    }
    for name, (threshold_label, value, threshold, operator) in checks.items():
        passed = value == threshold if operator == "eq" else value >= threshold
        _add_gate(
            gates,
            name=f"job_base:{name}",
            status=PASS if passed else FAIL,
            message=f"{name} meets the current gate." if passed else f"{name} is outside the current gate.",
            value=value,
            threshold=threshold_label,
        )


def _add_career_path_gates(
    gates: list[dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    total = int(evidence.get("career_path_count") or 0)
    matched_units = int(evidence.get("matched_unit_count") or 0)
    distinct_matched_units = int(evidence.get("distinct_matched_unit_count") or 0)

    if total == 0:
        status = WARN
        message = "No NCS career path rows are available for transition recommendation evidence."
    elif matched_units == 0:
        status = WARN
        message = "NCS career path rows exist, but none are matched to competency units."
    else:
        status = PASS
        message = "NCS career path evidence is available for transition recommendations."
    _add_gate(
        gates,
        name="transition_evidence:career_paths",
        status=status,
        message=message,
        value=matched_units,
        threshold="> 0 matched unit rows",
        details={
            "career_path_count": total,
            "distinct_matched_unit_count": distinct_matched_units,
            "unit_match_rate": evidence.get("unit_match_rate", 0.0),
            "source_table": "ncs_career_paths",
        },
    )


def _transition_packet_backed_trusted_scenario_provenance(
    conn: sqlite3.Connection,
    status_counts: dict[str, int],
) -> dict[str, Any]:
    raw_trusted_count = sum(
        int(status_counts.get(status) or 0)
        for status in TRUSTED_TRANSITION_REVIEW_STATUSES
    )
    required_fields = [
        "reviewer_id",
        "source_decision_packet",
        "source_artifact_hash",
        "rationale",
        "evidence_refs_json",
    ]
    packet_backed_requires = [
        "packet_backed_human_review_action",
        *required_fields,
        "same_audit_row",
        "new_status_matches_scenario",
        "human_reviewer_id",
        "source_decision_packet_resolves_to_reports_artifact",
        "source_artifact_hash_matches_reports_artifact",
        "evidence_refs_json_valid",
    ]
    summary: dict[str, Any] = {
        "schema": "ncs_transition_trusted_scenario_provenance_v1",
        "policy": "packet_backed_required_for_quality_gate",
        "source_table": "training_transition_gold_scenarios",
        "audit_table": "review_audit_log",
        "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
        "packet_backed_requires": packet_backed_requires,
        "raw_trusted_scenario_count": raw_trusted_count,
        "packet_backed_scenario_count": 0,
        "legacy_trusted_scenario_count": raw_trusted_count,
        "packet_backed_scenario_ids": [],
        "legacy_trusted_scenario_ids": [],
        "audit_table_present": False,
        "audit_columns_present": False,
    }
    if not raw_trusted_count:
        return summary
    if not _table_exists(conn, "review_audit_log"):
        summary["missing_audit_columns"] = ["review_audit_log"]
        return summary

    audit_columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(review_audit_log)").fetchall()
    }
    required_audit_columns = {
        "entity_type",
        "entity_id",
        "action",
        "new_status",
        *required_fields,
    }
    missing_columns = sorted(required_audit_columns - audit_columns)
    summary["audit_table_present"] = True
    summary["audit_columns_present"] = not missing_columns
    if missing_columns:
        summary["missing_audit_columns"] = missing_columns
        return summary

    placeholders = ",".join("?" for _ in TRUSTED_TRANSITION_REVIEW_STATUSES)
    query = f"""
        SELECT
            scenario.scenario_id,
            scenario.review_status,
            audit.id AS audit_id,
            audit.action AS audit_action,
            audit.new_status AS audit_new_status,
            audit.reviewer_id AS audit_reviewer_id,
            audit.source_decision_packet AS audit_source_decision_packet,
            audit.source_artifact_hash AS audit_source_artifact_hash,
            audit.rationale AS audit_rationale,
            audit.evidence_refs_json AS audit_evidence_refs_json
        FROM training_transition_gold_scenarios scenario
        LEFT JOIN review_audit_log audit
          ON audit.entity_type = 'training_transition_gold_scenario'
         AND audit.entity_id = CAST(scenario.scenario_id AS TEXT)
        WHERE scenario.review_status IN ({placeholders})
        ORDER BY scenario.scenario_id, audit.id
    """

    def _audit_row_blockers(row: sqlite3.Row) -> list[str]:
        if row["audit_id"] is None:
            return ["review_audit_log"]
        blockers: list[str] = []
        audit_action = str(row["audit_action"] or "").strip()
        if audit_action != TRUSTED_TRANSITION_REVIEW_AUDIT_ACTION:
            blockers.append("packet_backed_human_review_action")
        audit_new_status = str(row["audit_new_status"] or "").strip()
        review_status = str(row["review_status"] or "").strip()
        if audit_new_status != review_status:
            blockers.append("new_status_matches_scenario")
        reviewer_id = str(row["audit_reviewer_id"] or "").strip()
        if not reviewer_id:
            blockers.append("reviewer_id")
        elif reviewer_id.lower() in AUTOMATED_REVIEWER_IDS:
            blockers.append("human_reviewer_id")
        source_packet = str(row["audit_source_decision_packet"] or "").strip()
        if not source_packet:
            blockers.append("source_decision_packet")
            packet_path = None
        else:
            packet_path = _resolve_review_packet_artifact(source_packet)
            if packet_path is None:
                blockers.append("source_decision_packet_resolves_to_reports_artifact")
        source_hash = str(row["audit_source_artifact_hash"] or "").strip()
        if not source_hash:
            blockers.append("source_artifact_hash")
        elif not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", source_hash):
            blockers.append("source_artifact_hash_matches_reports_artifact")
        elif packet_path is not None:
            expected_hash = "sha256:" + review_packet_sha256(packet_path)
            if source_hash.lower() != expected_hash:
                blockers.append("source_artifact_hash_matches_reports_artifact")
        if not str(row["audit_rationale"] or "").strip():
            blockers.append("rationale")
        evidence_refs = str(row["audit_evidence_refs_json"] or "").strip()
        if not evidence_refs:
            blockers.append("evidence_refs_json")
        elif not _evidence_refs_json_is_packet_backing_list(evidence_refs):
            blockers.append("evidence_refs_json_valid")
        return blockers

    rows_by_scenario: dict[int, list[sqlite3.Row]] = {}
    for row in conn.execute(query, tuple(TRUSTED_TRANSITION_REVIEW_STATUSES)):
        rows_by_scenario.setdefault(int(row["scenario_id"]), []).append(row)

    packet_backed_ids: list[int] = []
    legacy_ids: list[int] = []
    missing_field_counts: Counter[str] = Counter()
    same_row_splittable_fields = set(required_fields)
    for scenario_id, rows in rows_by_scenario.items():
        row_blockers = [_audit_row_blockers(row) for row in rows]
        if any(not blockers for blockers in row_blockers):
            packet_backed_ids.append(scenario_id)
            continue
        legacy_ids.append(scenario_id)
        best_blockers = min(row_blockers, key=len) if row_blockers else ["review_audit_log"]
        blocker_sets = [set(blockers) for blockers in row_blockers if blockers]
        has_split_required_fields = (
            len(blocker_sets) > 1
            and all(blockers <= same_row_splittable_fields for blockers in blocker_sets)
            and not set.intersection(*blocker_sets)
        )
        if has_split_required_fields:
            missing_field_counts["same_audit_row"] += 1
        for field in best_blockers:
            missing_field_counts[field] += 1

    summary.update(
        {
            "packet_backed_scenario_count": len(packet_backed_ids),
            "legacy_trusted_scenario_count": len(legacy_ids),
            "packet_backed_scenario_ids": packet_backed_ids,
            "legacy_trusted_scenario_ids": legacy_ids,
            "missing_packet_backed_field_counts": dict(sorted(missing_field_counts.items())),
        }
    )
    return summary


def _add_transition_evaluation_gates(
    gates: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> None:
    scenario_count = int(evaluation.get("scenario_count") or 0)
    provenance = (
        evaluation.get("trusted_scenario_provenance")
        if isinstance(evaluation.get("trusted_scenario_provenance"), dict)
        else {}
    )
    if scenario_count == 0:
        raw_trusted_count = int(provenance.get("raw_trusted_scenario_count") or 0)
        if raw_trusted_count:
            message = (
                "Trusted transition gold scenarios require packet-backed provenance "
                "before hard gating."
            )
        else:
            message = "No trusted transition gold scenarios are available for hard gating."
        _add_gate(
            gates,
            name="transition_eval:trusted_scenarios",
            status=WARN,
            message=message,
            value=0,
            threshold="> 0 packet-backed",
            details={
                "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                "status_counts": evaluation.get("status_counts") or {},
                "trusted_scenario_provenance": provenance,
            },
        )
        return

    _add_gate(
        gates,
        name="transition_eval:trusted_scenarios",
        status=PASS,
        message="Packet-backed trusted transition gold scenarios are available for gating.",
        value=scenario_count,
        threshold="> 0 packet-backed",
        details={
            "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
            "trusted_scenario_provenance": provenance,
        },
    )
    failed_cases = len([case for case in evaluation.get("cases", []) if not case.get("ok")])
    _add_gate(
        gates,
        name="transition_eval:case_errors",
        status=FAIL if failed_cases else PASS,
        message="Some gold scenarios failed to produce recommendations."
        if failed_cases
        else "All gold scenarios produced recommendation results.",
        value=failed_cases,
        threshold="== 0",
    )
    thresholds = {
        "current_scope_accuracy": (0.99, FAIL),
        "target_scope_accuracy": (0.99, FAIL),
        "expected_course_recall_at_k": (TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K, FAIL),
        "top1_expected_hit_rate": (0.95, FAIL),
        "ndcg_at_k": (0.99, WARN),
    }
    for metric_name, (threshold, below_status) in thresholds.items():
        raw_value = evaluation.get(metric_name)
        value = float(raw_value) if raw_value is not None else None
        status = below_status if value is None or value < threshold else PASS
        _add_gate(
            gates,
            name=f"transition_eval:{metric_name}",
            status=status,
            message=f"{metric_name} is below the current gate."
            if status != PASS
            else f"{metric_name} meets the current gate.",
            value=value,
            threshold=f">= {threshold}",
        )
    precision_threshold = 0.30
    precision_value = float(evaluation.get("precision_at_k") or 0.0)
    precision_upper_bound = float(evaluation.get("precision_at_k_upper_bound") or 0.0)
    precision_relative = evaluation.get("precision_at_k_relative_to_upper_bound")
    precision_relative_value = float(precision_relative) if precision_relative is not None else None
    sparse_label_bound_applies = 0.0 < precision_upper_bound < precision_threshold
    reaches_sparse_label_bound = (
        sparse_label_bound_applies
        and precision_relative_value is not None
        and precision_relative_value >= 0.999
    )
    precision_status = PASS if precision_value >= precision_threshold or reaches_sparse_label_bound else WARN
    if precision_status == PASS and reaches_sparse_label_bound:
        precision_message = "precision_at_k reaches the sparse-label upper bound."
        precision_threshold_label = f">= {precision_threshold} or == sparse-label upper bound"
    elif precision_status == PASS:
        precision_message = "precision_at_k meets the current gate."
        precision_threshold_label = f">= {precision_threshold}"
    else:
        precision_message = "precision_at_k is below the current gate."
        precision_threshold_label = f">= {precision_threshold}"
    _add_gate(
        gates,
        name="transition_eval:precision_at_k",
        status=precision_status,
        message=precision_message,
        value=precision_value,
        threshold=precision_threshold_label,
        details={
            "precision_at_k_upper_bound": precision_upper_bound,
            "precision_at_k_relative_to_upper_bound": precision_relative_value,
            "recommended_course_total": evaluation.get("recommended_course_total"),
            "expected_course_total": evaluation.get("expected_course_total"),
            "possible_expected_course_hit_count": evaluation.get("possible_expected_course_hit_count"),
        },
    )
    _add_transition_recommendation_signal_gates(gates, evaluation)


def _transition_recommendation_signal_summary(evaluation: dict[str, Any]) -> dict[str, Any]:
    quality_issue_counts: Counter[str] = Counter()
    quality_review_flag_counts: Counter[str] = Counter()
    job_base_status_counts: Counter[str] = Counter()
    quality_penalty_course_names: list[str] = []
    job_base_signal_course_names: list[str] = []
    recommended_course_evidence_count = 0
    quality_penalty_course_count = 0
    job_base_signal_field_count = 0
    job_base_signal_course_count = 0
    job_base_target_hit_count = 0
    job_base_gap_hit_count = 0
    for case in evaluation.get("cases") or []:
        if not isinstance(case, dict):
            continue
        rows = case.get("recommended_course_evidence") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            recommended_course_evidence_count += 1
            course_name = str(row.get("course_name") or "").strip()
            penalty = row.get("quality_issue_penalty") if isinstance(row.get("quality_issue_penalty"), dict) else {}
            issue_types = [
                str(issue_type).strip()
                for issue_type in penalty.get("issue_types", [])
                if str(issue_type).strip()
            ]
            if penalty.get("applied") or issue_types:
                quality_penalty_course_count += 1
                if course_name:
                    quality_penalty_course_names.append(course_name)
                for issue_type in issue_types:
                    quality_issue_counts[issue_type] += 1
            for flag in row.get("review_flags") or []:
                flag_text = str(flag).strip()
                if flag_text.startswith("quality_issue:"):
                    quality_review_flag_counts[flag_text] += 1

            signal = row.get("job_base_signal") if isinstance(row.get("job_base_signal"), dict) else {}
            if signal:
                job_base_signal_field_count += 1
                status = str(signal.get("status") or "unknown").strip() or "unknown"
                job_base_status_counts[status] += 1
                target_hits = int(signal.get("target_hit_count") or 0)
                gap_hits = int(signal.get("gap_hit_count") or 0)
                job_base_target_hit_count += target_hits
                job_base_gap_hit_count += gap_hits
                meaningful_signal = bool(target_hits or gap_hits) or status in {
                    "gap_bridge",
                    "target_scope_signal",
                }
                if meaningful_signal:
                    job_base_signal_course_count += 1
                    if course_name:
                        job_base_signal_course_names.append(course_name)

    return {
        "recommended_course_evidence_count": recommended_course_evidence_count,
        "quality_penalty_course_count": quality_penalty_course_count,
        "quality_issue_counts": dict(sorted(quality_issue_counts.items())),
        "quality_review_flag_counts": dict(sorted(quality_review_flag_counts.items())),
        "quality_penalty_course_names": list(dict.fromkeys(quality_penalty_course_names))[:10],
        "job_base_signal_field_count": job_base_signal_field_count,
        "job_base_signal_course_count": job_base_signal_course_count,
        "job_base_status_counts": dict(sorted(job_base_status_counts.items())),
        "job_base_target_hit_count": job_base_target_hit_count,
        "job_base_gap_hit_count": job_base_gap_hit_count,
        "job_base_signal_course_names": list(dict.fromkeys(job_base_signal_course_names))[:10],
        "scoring_policy": {
            "quality_issue_penalty": "downweight_and_review_surface",
            "job_base_signal": "auxiliary_tie_breaker_not_primary_evidence",
        },
    }


def _add_transition_recommendation_signal_gates(
    gates: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> None:
    summary = _transition_recommendation_signal_summary(evaluation)
    penalty_count = int(summary.get("quality_penalty_course_count") or 0)
    _add_gate(
        gates,
        name="transition_eval:quality_issue_penalty_review_surface",
        status=WARN if penalty_count else PASS,
        message=(
            "Recommended courses include KSA quality penalties and need review prioritization."
            if penalty_count
            else "No KSA quality penalty was surfaced in evaluated recommendations."
        ),
        value=penalty_count,
        threshold="review when > 0",
        details={
            "quality_issue_counts": summary.get("quality_issue_counts") or {},
            "quality_review_flag_counts": summary.get("quality_review_flag_counts") or {},
            "course_names": summary.get("quality_penalty_course_names") or [],
            "scoring_policy": (summary.get("scoring_policy") or {}).get("quality_issue_penalty"),
        },
    )

    course_evidence_count = int(summary.get("recommended_course_evidence_count") or 0)
    job_base_count = int(summary.get("job_base_signal_course_count") or 0)
    job_base_status = WARN if course_evidence_count and job_base_count == 0 else PASS
    _add_gate(
        gates,
        name="transition_eval:job_base_signal_surface",
        status=job_base_status,
        message=(
            "Job-base competency auxiliary signals are missing from evaluated recommendations."
            if job_base_status == WARN
            else "Job-base competency auxiliary signals are surfaced for evaluated recommendations."
        ),
        value=job_base_count,
        threshold="> 0 when recommendation evidence exists",
        details={
            "recommended_course_evidence_count": course_evidence_count,
            "job_base_signal_field_count": summary.get("job_base_signal_field_count"),
            "job_base_status_counts": summary.get("job_base_status_counts") or {},
            "job_base_target_hit_count": summary.get("job_base_target_hit_count"),
            "job_base_gap_hit_count": summary.get("job_base_gap_hit_count"),
            "course_names": summary.get("job_base_signal_course_names") or [],
            "scoring_policy": (summary.get("scoring_policy") or {}).get("job_base_signal"),
        },
    )


def write_quality_gate_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# NCS Quality Gates",
        "",
        f"- status: {report['status']}",
        f"- ok: {report['ok']}",
        f"- fail_count: {report['summary']['fail_count']}",
        f"- warn_count: {report['summary']['warn_count']}",
        f"- pass_count: {report['summary']['pass_count']}",
        "",
        "## Gates",
        "",
    ]
    for gate in report["gates"]:
        lines.append(
            f"- {gate['status']}: {gate['name']} - {gate['message']} "
            f"(value={gate.get('value')}, threshold={gate.get('threshold')})"
        )
        if (
            gate["status"] != PASS
            or gate.get("name") in ALWAYS_SHOW_DETAIL_GATE_NAMES
        ) and gate.get("details"):
            details = json.dumps(gate["details"], ensure_ascii=False, sort_keys=True)
            lines.append(f"  - details: `{details}`")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def evaluate_quality_gates(
    db_path: Path,
    *,
    include_transition_evaluation: bool = False,
    transition_limit: int = 5,
    transition_scenario_limit: int | None = None,
    non_hr_surface_artifact_paths: dict[str, Path] | None = None,
    out_path: Path | None = None,
    markdown_path: Path | None = None,
) -> dict[str, Any]:
    gates: list[dict[str, Any]] = []
    try:
        conn = _connect_readonly(db_path)
    except FileNotFoundError as exc:
        _add_gate(
            gates,
            name="database:exists",
            status=FAIL,
            message=str(exc),
            value=str(db_path),
        )
        return _finish_quality_gate_report(
            gates,
            evidence={},
            out_path=out_path,
            markdown_path=markdown_path,
        )

    try:
        missing = _missing_tables(conn, QUALITY_GATE_TABLES)
        if missing:
            _add_gate(
                gates,
                name="database:schema",
                status=FAIL,
                message="Required quality-gate tables are missing.",
                value=len(missing),
                details={"missing_tables": missing},
            )
            return _finish_quality_gate_report(
                gates,
                evidence={"missing_tables": missing},
                out_path=out_path,
                markdown_path=markdown_path,
            )

        validation = _validate_ontology_readiness_readonly(conn)
        _add_ontology_gates(gates, validation)
        stats = refinement_stats(conn)
        quality_issue_counts = _open_quality_issue_counts(conn)
        api_element_issue_counts = _api_element_issue_counts(conn)
        reviews = _review_counts(conn)
        qualifications = qualification_summary(conn, limit=20)
        qualification_retry_hygiene = qualification_retry_hygiene_report(conn, limit=20)
        job_base = job_base_summary(conn, limit=20)
        career_paths = _career_path_evidence_counts(conn)
        status_counts = _transition_scenario_status_counts(conn)
        if include_transition_evaluation:
            trusted_provenance = _transition_packet_backed_trusted_scenario_provenance(
                conn,
                status_counts,
            )
            trusted_scenario_ids = [
                int(item)
                for item in trusted_provenance.get("packet_backed_scenario_ids") or []
            ]
            transition_evaluation = (
                evaluate_training_transition_scenarios(
                    conn,
                    limit=transition_limit,
                    scenario_limit=transition_scenario_limit,
                    review_statuses=list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                    scenario_ids=trusted_scenario_ids,
                )
                if trusted_scenario_ids
                else {
                    "ok": True,
                    "scenario_count": 0,
                    "status_counts": status_counts,
                    "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                    "skipped": True,
                    "skip_reason": "no_packet_backed_trusted_transition_gold_scenarios",
                }
            )
            transition_evaluation.setdefault("status_counts", status_counts)
            transition_evaluation.setdefault(
                "trusted_review_statuses",
                list(TRUSTED_TRANSITION_REVIEW_STATUSES),
            )
            transition_evaluation.setdefault(
                "trusted_scenario_provenance",
                trusted_provenance,
            )
        else:
            transition_evaluation = None
    finally:
        conn.close()

    _add_quality_issue_gates(gates, quality_issue_counts, api_element_issue_counts)
    _add_review_debt_gates(gates, stats, reviews)
    _add_qualification_gates(
        gates,
        qualifications,
        total_unit_count=int((validation.get("counts") or {}).get("competency_units") or 0),
        retry_hygiene=qualification_retry_hygiene,
    )
    _add_job_base_gates(gates, job_base)
    _add_career_path_gates(gates, career_paths)
    if transition_evaluation is not None:
        _add_transition_evaluation_gates(gates, transition_evaluation)
    non_hr_surface_smoke = _add_non_hr_surface_smoke_gates(
        gates,
        non_hr_surface_artifact_paths,
    )

    return _finish_quality_gate_report(
        gates,
        evidence={
            "ontology": validation,
            "refinement": stats,
            "quality_issue_counts": quality_issue_counts,
            "review_counts": reviews,
            "qualification": qualifications,
            "qualification_retry_hygiene": qualification_retry_hygiene,
            "job_base": job_base,
            "career_paths": career_paths,
            "transition_evaluation": transition_evaluation,
            "non_hr_surface_smoke": non_hr_surface_smoke,
        },
        out_path=out_path,
        markdown_path=markdown_path,
    )


def _finish_quality_gate_report(
    gates: list[dict[str, Any]],
    *,
    evidence: dict[str, Any],
    out_path: Path | None,
    markdown_path: Path | None,
) -> dict[str, Any]:
    status = _overall_status(gates)
    report = {
        "ok": status != FAIL,
        "status": status,
        "report_only": True,
        "db_writes": False,
        "status_update_allowed": False,
        "approval_claim": False,
        "summary": {
            "pass_count": len([gate for gate in gates if gate["status"] == PASS]),
            "warn_count": len([gate for gate in gates if gate["status"] == WARN]),
            "fail_count": len([gate for gate in gates if gate["status"] == FAIL]),
        },
        "gates": gates,
        "evidence": evidence,
        "note": "Read-only NCS quality gates for ontology and training recommendation readiness.",
    }
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["out_path"] = str(out_path)
    if markdown_path is not None:
        write_quality_gate_markdown(report, markdown_path)
        report["markdown_path"] = str(markdown_path)
    return report
