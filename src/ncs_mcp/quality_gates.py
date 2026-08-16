from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.job_base_api import job_base_summary
from ncs_mcp.qualification_api import qualification_retry_hygiene_report, qualification_summary
from ncs_mcp.refinement import refinement_stats
from ncs_mcp.training_recommendation import (
    TRUSTED_TRANSITION_REVIEW_STATUSES,
    TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
    evaluate_training_transition_scenarios,
)


PASS = "pass"
WARN = "warn"
FAIL = "fail"
ONTOLOGY_READINESS_TABLES = [
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    "ontology_concepts",
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
    candidate_definitions = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concepts
            WHERE definition_status = 'candidate'
            """
        ).fetchone()[0]
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
            "candidate_definitions": candidate_definitions,
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
        "api_element_unmatched": 200,
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


def _add_transition_evaluation_gates(
    gates: list[dict[str, Any]],
    evaluation: dict[str, Any],
) -> None:
    scenario_count = int(evaluation.get("scenario_count") or 0)
    if scenario_count == 0:
        _add_gate(
            gates,
            name="transition_eval:trusted_scenarios",
            status=WARN,
            message="No trusted transition gold scenarios are available for hard gating.",
            value=0,
            threshold="> 0",
            details={
                "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                "status_counts": evaluation.get("status_counts") or {},
            },
        )
        return

    _add_gate(
        gates,
        name="transition_eval:trusted_scenarios",
        status=PASS,
        message="Trusted transition gold scenarios are available for gating.",
        value=scenario_count,
        threshold="> 0",
        details={"trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES)},
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
        if gate["status"] != PASS and gate.get("details"):
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
        reviews = _review_counts(conn)
        qualifications = qualification_summary(conn, limit=20)
        qualification_retry_hygiene = qualification_retry_hygiene_report(conn, limit=20)
        job_base = job_base_summary(conn, limit=20)
        career_paths = _career_path_evidence_counts(conn)
        status_counts = _transition_scenario_status_counts(conn)
        if include_transition_evaluation:
            trusted_count = sum(status_counts.get(status, 0) for status in TRUSTED_TRANSITION_REVIEW_STATUSES)
            transition_evaluation = (
                evaluate_training_transition_scenarios(
                    conn,
                    limit=transition_limit,
                    scenario_limit=transition_scenario_limit,
                    review_statuses=list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                )
                if trusted_count
                else {
                    "ok": True,
                    "scenario_count": 0,
                    "status_counts": status_counts,
                    "trusted_review_statuses": list(TRUSTED_TRANSITION_REVIEW_STATUSES),
                    "skipped": True,
                    "skip_reason": "no_trusted_transition_gold_scenarios",
                }
            )
            transition_evaluation.setdefault("status_counts", status_counts)
            transition_evaluation.setdefault(
                "trusted_review_statuses",
                list(TRUSTED_TRANSITION_REVIEW_STATUSES),
            )
        else:
            transition_evaluation = None
    finally:
        conn.close()

    _add_quality_issue_gates(gates, quality_issue_counts)
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
