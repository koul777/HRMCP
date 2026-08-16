from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import (
    api_quality_hygiene_report,
    apply_api_quality_hygiene,
    collect_elements_api,
    collect_sqf_api,
    collect_standard_api,
    collect_subd_api,
    write_api_quality_hygiene_markdown,
)
from ncs_mcp.collect_sqf_library import collect_sqf_library
from ncs_mcp.config import load_settings
from ncs_mcp.career_path import career_path_summary, import_career_paths_csv
from ncs_mcp.db import (
    build_ksa_meaning_candidates,
    build_task_ksa_concept_relations,
    build_task_similarity_links,
    connect,
    ensure_ncs_ontology_relations,
    ensure_ontology_seeded,
    initialize_database,
    prepare_hr_human_review_queue,
    prepare_ontology_human_review_queue,
    preprocess_ksa_atomic_items,
    recommend_task_transitions,
)
from ncs_mcp.evaluation import run_evaluation
from ncs_mcp.handoff import export_handoff_package
from ncs_mcp.import_ontology_sources import register_local_ontology_source
from ncs_mcp.job_base_api import (
    collect_job_base_competencies,
    fetch_job_base_page,
    job_base_summary,
)
from ncs_mcp.mvp_workflow import run_mvp_bootstrap
from ncs_mcp.ontology import build_sqf_mapping_candidates
from ncs_mcp.ontology_export import export_ontology_jsonld, validate_ontology_readiness
from ncs_mcp.preprocess_excel import preprocess_excel
from ncs_mcp.preprocess_sqf_documents import preprocess_sqf_documents
from ncs_mcp.quality import run_quality_checks
from ncs_mcp.quality_gates import evaluate_quality_gates
from ncs_mcp.qualification_api import (
    apply_qualification_retry_hygiene,
    collect_qualification_links,
    fetch_qualification_page,
    qualification_error_report,
    qualification_retry_hygiene_report,
    qualification_summary,
    retry_qualification_error_units,
    write_qualification_retry_hygiene_markdown,
)
from ncs_mcp.recommendation_evidence import (
    apply_recommendation_evidence_hygiene,
    recommendation_evidence_hygiene_report,
    write_recommendation_evidence_hygiene_markdown,
)
from ncs_mcp.refinement import parse_csv, run_refinement_harness
from ncs_mcp.review_priority import review_priority_summary_from_db, write_review_priority_markdown
from ncs_mcp.review_seedpack import (
    export_review_seedpack_from_db,
    export_transition_scenario_seedpack_from_db,
    write_review_seedpack_markdown,
    write_transition_scenario_seedpack_markdown,
)
from ncs_mcp.review_triage import build_review_triage_from_files, write_review_triage_markdown
from ncs_mcp.server import get_competency_units, get_unit_structure
from ncs_mcp.sqf_precision_matching import build_sqf_chunk_job_level_matches
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model, sqf_model_summary
from ncs_mcp.study_module_api import collect_study_modules, fetch_study_modules
from ncs_mcp.supplemental_data import (
    import_external_training_zip_csv,
    import_occupation_code_mapping_csv,
    import_unit_standard_training_csv,
    supplemental_data_summary,
)
from ncs_mcp.training_course_api import collect_training_courses, fetch_training_course_page
from ncs_mcp.training_recommendation import (
    TRUSTED_TRANSITION_REVIEW_STATUSES,
    TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
    available_major_codes,
    build_training_course_ontology_links,
    compact_training_task_response,
    compact_training_transition_response,
    evaluate_training_transition_scenarios,
    generate_training_transition_eval_scenarios,
    recommend_training_for_task,
    recommend_training_transition,
    review_training_transition_scenarios,
    resolve_ncs_query_scope,
)


CANDIDATE_TRANSITION_REVIEW_STATUSES = ("candidate", "candidate_auto")
ALLOWED_TRANSITION_REVIEW_STATUSES = tuple(
    dict.fromkeys(
        (
            *TRUSTED_TRANSITION_REVIEW_STATUSES,
            *CANDIDATE_TRANSITION_REVIEW_STATUSES,
            "rejected",
        )
    )
)


def explicit_collection_major_codes(
    conn,
    *,
    major_code: str | None,
    all_majors: bool,
    command_name: str,
) -> list[str]:
    if all_majors:
        return available_major_codes(conn)
    if major_code:
        return [major_code]
    raise ValueError(
        f"{command_name} requires --all-majors or --major-code. "
        "Use --all-majors for full NCS collection, or --major-code only for a scoped debug refresh."
    )


def transition_review_status_filter(
    *,
    trusted_only: bool = False,
    review_statuses: list[str] | None = None,
) -> list[str] | None:
    if trusted_only:
        return list(TRUSTED_TRANSITION_REVIEW_STATUSES)
    parsed: list[str] = []
    for value in review_statuses or []:
        parsed.extend(parse_csv(value) or [])
    invalid = sorted(set(parsed) - set(ALLOWED_TRANSITION_REVIEW_STATUSES))
    if invalid:
        raise ValueError(
            "Unsupported transition review status: "
            + ", ".join(invalid)
            + ". Allowed values: "
            + ", ".join(ALLOWED_TRANSITION_REVIEW_STATUSES)
        )
    return parsed or None


def task_locator_error_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "code": "missing_task_locator",
            "message": (
                "NCS 과업 선택을 위해 --criteria-id, --query, 또는 --unit-code 중 하나를 입력하세요."
            ),
            "examples": [
                'python scripts\\ncs_harness.py recommend-training-for-task --query "인사기획"',
                "python scripts\\ncs_harness.py recommend-training-for-task --unit-code 0202020101_23v3",
            ],
        },
    }


def has_task_locator(*, criteria_id: int | None, query: str | None, unit_code: str | None) -> bool:
    return criteria_id is not None or bool((query or "").strip()) or bool((unit_code or "").strip())


def evaluation_summary_without_cases(evaluation: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in evaluation.items() if key != "cases"}


def training_transition_eval_set_payload(
    *,
    generation: dict[str, Any],
    evaluation: dict[str, Any],
    trusted_evaluation: dict[str, Any],
    candidate_evaluation: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    return {
        "ok": True,
        "generation": generation,
        "all_non_rejected_evaluation": evaluation_summary_without_cases(evaluation),
        "evaluations": {
            "all_non_rejected": evaluation_summary_without_cases(evaluation),
            "trusted_reviewed": evaluation_summary_without_cases(trusted_evaluation),
            "candidate_or_auto": evaluation_summary_without_cases(candidate_evaluation),
        },
        "report_path": str(report_path),
    }


def configure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


configure_utf8_stdio()


CORE_TABLES = [
    "raw_excel_rows",
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    "element_criteria_ksa_links",
    "ksa_meaning_candidates",
    "api_raw_responses",
    "api_competency_units",
    "ncs_learning_modules",
    "learning_module_unit_links",
    "learning_module_concept_links",
    "ncs_training_courses",
    "ncs_training_course_unit_links",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
    "ncs_qualification_items",
    "ncs_unit_qualification_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_unit_job_base_links",
    "sqf_duties",
    "sqf_ncs_matches",
    "sqf_library_posts",
    "sqf_library_files",
    "sqf_document_sources",
    "sqf_framework_concepts",
    "sqf_industry_sectors",
    "sqf_jobs_normalized",
    "sqf_levels",
    "sqf_job_levels_normalized",
    "sqf_recognition_evidence",
    "sqf_document_assets",
    "sqf_document_pages",
    "sqf_document_chunks",
    "sqf_chunk_job_level_matches",
    "sqf_document_evidence_links",
    "quality_issues",
    "refinement_jobs",
    "education_recommendation_runs",
    "education_recommendation_items",
    "education_recommendation_evidence",
]


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def write_training_transition_evaluation_report(
    report_path: Path,
    *,
    generation: dict[str, Any] | None,
    evaluation: dict[str, Any] | None = None,
    evaluations: dict[str, dict[str, Any]] | None = None,
) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    evaluation_sets = evaluations or {"evaluation": evaluation or {}}
    lines = [
        "# Training Transition Evaluation",
        "",
        "Candidate and auto-generated scenarios are useful for ranking exploration. "
        "Trusted readiness should be read from the trusted_reviewed section.",
        "",
    ]
    for label, item in evaluation_sets.items():
        lines.extend(
            [
                f"## Summary: {label}",
                "",
                f"- review_status_filter: `{json.dumps(item.get('review_status_filter', []), ensure_ascii=False)}`",
                f"- scenario_limit: {item.get('scenario_limit')}",
                f"- scenario_count: {item.get('scenario_count')}",
                f"- current_scope_accuracy: {item.get('current_scope_accuracy')}",
                f"- target_scope_accuracy: {item.get('target_scope_accuracy')}",
                f"- expected_course_recall_at_k: {item.get('expected_course_recall_at_k')}",
                f"- precision_at_k: {item.get('precision_at_k')}",
                f"- top1_expected_hit_rate: {item.get('top1_expected_hit_rate')}",
                f"- mrr_at_k: {item.get('mrr_at_k')}",
                f"- map_at_k: {item.get('map_at_k')}",
                f"- ndcg_at_k: {item.get('ndcg_at_k')}",
                f"- expected_course_hits: {item.get('expected_course_hit_count')}/{item.get('expected_course_total')}",
                "",
            ]
        )
    if generation:
        lines.extend(
            [
                "## Generated Scenarios",
                "",
                f"- selected_count: {generation.get('selected_count')}",
                f"- auto_non_hr_scenario_count: {generation.get('auto_non_hr_scenario_count')}",
                f"- major_counts: `{json.dumps(generation.get('major_counts', {}), ensure_ascii=False)}`",
                "",
            ]
        )
    for label, evaluation_item in evaluation_sets.items():
        lines.extend([f"## Breakdown: {label}", ""])
        for key, item in (evaluation_item.get("breakdown") or {}).items():
            lines.append(
                "- "
                + key
                + ": "
                + f"scenarios={item.get('scenario_count')}, "
                + f"scope={item.get('current_scope_accuracy')}/{item.get('target_scope_accuracy')}, "
                + f"recall={item.get('expected_course_recall_at_k')}, "
                + f"precision={item.get('precision_at_k')}, "
                + f"top1={item.get('top1_expected_hit_rate')}, "
                + f"mrr={item.get('mrr_at_k')}, "
                + f"ndcg={item.get('ndcg_at_k')}, "
                + f"hits={item.get('expected_course_hit_count')}/{item.get('expected_course_total')}"
            )
        lines.extend(["", f"## Low Recall Cases: {label}", ""])
        for case in evaluation_item.get("cases", []):
            recall = case.get("expected_recall_at_k")
            if recall is None or recall >= 1.0:
                continue
            lines.extend(
                [
                    f"### {case.get('scenario_name')}",
                    "",
                    f"- current_match: {case.get('current_match')}",
                    f"- target_match: {case.get('target_match')}",
                    f"- expected_recall_at_k: {recall}",
                    f"- precision_at_k: {case.get('precision_at_k')}",
                    f"- first_expected_rank: {case.get('first_expected_rank')}",
                    f"- expected_courses: `{json.dumps(case.get('expected_courses', []), ensure_ascii=False)}`",
                    f"- recommended_courses: `{json.dumps(case.get('recommended_courses', []), ensure_ascii=False)}`",
                    "",
                ]
            )
        lines.extend(["", f"## Low Precision Or Ranking Cases: {label}", ""])
        for case in evaluation_item.get("cases", []):
            if not case.get("ok"):
                continue
            precision = case.get("precision_at_k")
            first_rank = case.get("first_expected_rank")
            if precision is None or (precision >= 0.4 and first_rank in (1, None)):
                continue
            lines.extend(
                [
                    f"### {case.get('scenario_name')}",
                    "",
                    f"- current_match: {case.get('current_match')}",
                    f"- target_match: {case.get('target_match')}",
                    f"- precision_at_k: {precision}",
                    f"- expected_recall_at_k: {case.get('expected_recall_at_k')}",
                    f"- first_expected_rank: {first_rank}",
                    f"- reciprocal_rank: {case.get('reciprocal_rank')}",
                    f"- ndcg_at_k: {case.get('ndcg_at_k')}",
                    f"- expected_courses: `{json.dumps(case.get('expected_courses', []), ensure_ascii=False)}`",
                    f"- recommended_courses: `{json.dumps(case.get('recommended_courses', []), ensure_ascii=False)}`",
                    "",
                ]
            )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_training_transition_review_report(report_path: Path, review: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Training Transition Scenario Review",
        "",
        "This report records automated scenario checks. It does not mark scenarios as human reviewed.",
        "",
        "## Summary",
        "",
        f"- apply: {review.get('apply')}",
        f"- review_method: {review.get('review_method')}",
        f"- source_review_statuses: `{json.dumps(review.get('source_review_statuses', []), ensure_ascii=False)}`",
        f"- target_review_status: {review.get('target_review_status')}",
        f"- evaluated_count: {review.get('evaluated_count')}",
        f"- eligible_count: {review.get('eligible_count')}",
        f"- updated_count: {review.get('updated_count')}",
        f"- criteria: `{json.dumps(review.get('criteria', {}), ensure_ascii=False)}`",
        "",
        "## Cases",
        "",
    ]
    for case in review.get("cases") or []:
        lines.extend(
            [
                f"### {case.get('scenario_name')}",
                "",
                f"- scenario_id: {case.get('scenario_id')}",
                f"- source_review_status: {case.get('source_review_status')}",
                f"- eligible: {case.get('eligible')}",
                f"- blockers: `{json.dumps(case.get('blockers', []), ensure_ascii=False)}`",
                f"- current_scope_hit: {case.get('current_scope_hit')}",
                f"- target_scope_hit: {case.get('target_scope_hit')}",
                f"- top1_expected_hit: {case.get('top1_expected_hit')}",
                f"- precision_at_k: {case.get('precision_at_k')}",
                f"- expected_recall_at_k: {case.get('expected_recall_at_k')}",
                f"- first_expected_rank: {case.get('first_expected_rank')}",
                f"- expected_course_hits: `{json.dumps(case.get('expected_course_hits', []), ensure_ascii=False)}`",
                f"- recommended_courses: `{json.dumps(case.get('recommended_courses', []), ensure_ascii=False)}`",
                "",
            ]
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def write_qualification_error_report(report_path: Path, report: dict[str, Any]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Qualification API Error Report",
        "",
        "## Summary",
        "",
        f"- error_unit_count: {report.get('error_unit_count')}",
        f"- retry_ready_unit_count: {report.get('retry_ready_unit_count')}",
        f"- retry_waiting_unit_count: {report.get('retry_waiting_unit_count')}",
        f"- sample_error_type_counts: `{json.dumps(report.get('sample_error_type_counts', {}), ensure_ascii=False)}`",
        "",
        "## Collection Status",
        "",
    ]
    for row in report.get("status_counts") or []:
        lines.append(f"- {row.get('collection_status')}: {row.get('unit_count')}")
    lines.extend(["", "## Major Error Counts", ""])
    for row in report.get("major_error_counts") or []:
        major = f"{row.get('major_code') or ''} {row.get('major_name') or ''}".strip() or "(unknown)"
        lines.append(f"- {major}: {row.get('error_unit_count')}")
    lines.extend(["", "## Sample Errors", ""])
    for row in report.get("sample_errors") or []:
        unit = f"{row.get('unit_code')} {row.get('unit_name') or ''}".strip()
        major = f"{row.get('major_code') or ''} {row.get('major_name') or ''}".strip()
        lines.extend(
            [
                f"### {unit}",
                "",
                f"- error_type: {row.get('error_type')}",
                f"- attempt_count: {row.get('attempt_count')}",
                f"- next_retry_at: {row.get('next_retry_at') or ''}",
                f"- major: {major}",
                f"- last_result: {row.get('last_result_code') or ''} {row.get('last_result_msg') or ''}".strip(),
                f"- last_error: {row.get('last_error') or ''}",
                f"- updated_at: {row.get('updated_at') or ''}",
                "",
            ]
        )
    report_path.write_text("\n".join(lines), encoding="utf-8")


def table_counts(db_path: Path) -> dict[str, int]:
    if not db_path.exists():
        return {}
    conn = connect(db_path)
    initialize_database(conn)
    counts = {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in CORE_TABLES
    }
    conn.close()
    return counts


def collect_training_courses_for_scope(
    *,
    db_path: Path,
    service_key: str,
    major_code: str | None,
    all_majors: bool,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 500,
    max_pages: int | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    majors = explicit_collection_major_codes(
        conn,
        major_code=major_code,
        all_majors=all_majors,
        command_name="collect-training-courses",
    )
    conn.close()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for code in majors:
        try:
            results.append(
                collect_training_courses(
                    db_path,
                    service_key,
                    major_code=code,
                    module_name=module_name,
                    page_no=page_no,
                    num_of_rows=num_of_rows,
                    max_pages=max_pages,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "major_code": code,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return {
        "all_majors": all_majors,
        "major_codes": majors,
        "succeeded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
        "rows_upserted": sum(int(item.get("rows_upserted", 0)) for item in results),
    }


def collect_study_modules_for_scope(
    *,
    db_path: Path,
    service_key: str,
    major_code: str | None,
    all_majors: bool,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 200,
    max_pages: int | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    majors = explicit_collection_major_codes(
        conn,
        major_code=major_code,
        all_majors=all_majors,
        command_name="collect-study-modules",
    )
    conn.close()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for code in majors:
        try:
            results.append(
                collect_study_modules(
                    db_path,
                    service_key,
                    major_code=code,
                    module_name=module_name,
                    page_no=page_no,
                    num_of_rows=num_of_rows,
                    timeout=timeout,
                    max_pages=max_pages,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "major_code": code,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return {
        "all_majors": all_majors,
        "major_codes": majors,
        "succeeded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
        "rows_upserted": sum(int(item.get("rows_upserted", 0)) for item in results),
    }


def collect_job_base_for_scope(
    *,
    db_path: Path,
    service_key: str,
    major_code: str | None,
    all_majors: bool,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 500,
    max_pages: int | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    majors = explicit_collection_major_codes(
        conn,
        major_code=major_code,
        all_majors=all_majors,
        command_name="collect-job-base",
    )
    conn.close()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for code in majors:
        try:
            results.append(
                collect_job_base_competencies(
                    db_path,
                    service_key,
                    major_code=code,
                    module_name=module_name,
                    page_no=page_no,
                    num_of_rows=num_of_rows,
                    max_pages=max_pages,
                    timeout=timeout,
                )
            )
        except Exception as exc:
            errors.append(
                {
                    "major_code": code,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
            )
    return {
        "all_majors": all_majors,
        "major_codes": majors,
        "succeeded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
        "rows_processed": sum(int(item.get("rows_processed", 0)) for item in results),
        "links_upserted": sum(int(item.get("links_upserted", 0)) for item in results),
        "missing_local_units": sum(int(item.get("missing_local_units", 0)) for item in results),
    }


def inspect_project() -> dict[str, Any]:
    settings = load_settings()
    db_counts = table_counts(settings.db_path)
    payload: dict[str, Any] = {
        "root": str(ROOT),
        "excel_path": str(settings.excel_path) if settings.excel_path else None,
        "excel_exists": bool(settings.excel_path and settings.excel_path.exists()),
        "db_path": str(settings.db_path),
        "db_exists": settings.db_path.exists(),
        "reports_dir": str(settings.reports_dir),
        "service_key_present": bool(settings.service_key),
        "training_course_service_key_present": bool(settings.training_course_service_key),
        "qualification_service_key_present": bool(settings.qualification_service_key),
        "job_base_service_key_present": bool(settings.job_base_service_key),
        "counts": db_counts,
    }
    if settings.db_path.exists():
        conn = connect(settings.db_path)
        initialize_database(conn)
        payload["unit_api_status"] = {
            row["api_match_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT api_match_status, COUNT(*) AS count
                FROM competency_units
                GROUP BY api_match_status
                """
            )
        }
        payload["element_api_status"] = {
            row["api_match_status"]: row["count"]
            for row in conn.execute(
                """
                SELECT api_match_status, COUNT(*) AS count
                FROM competency_elements
                GROUP BY api_match_status
                """
            )
        }
        payload["missing_duty_definitions"] = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM classifications
                WHERE duty_def_api IS NULL OR TRIM(duty_def_api) = ''
                """
            ).fetchone()[0]
        )
        payload["ontology_status"] = {
            "sqf_ncs_matches": int(
                conn.execute("SELECT COUNT(*) FROM sqf_ncs_matches").fetchone()[0]
            ),
            "sqf_ncs_reviewed_matches": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqf_ncs_matches
                    WHERE review_status IN ('human_reviewed', 'reviewed', 'accepted')
                    """
                ).fetchone()[0]
            ),
            "management_support_sqf_duties": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM sqf_duties
                    WHERE ncs_lclas_cd = '02'
                      AND sqf_field_name = '경영관리'
                      AND job_name = '경영지원'
                    """
                ).fetchone()[0]
            ),
        }
        payload["ncs_training_ontology_status"] = {
            "ontology_concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
            "ksa_atomic_items": int(conn.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0]),
            "ksa_meaning_candidates": int(
                conn.execute("SELECT COUNT(*) FROM ksa_meaning_candidates").fetchone()[0]
            ),
            "task_ksa_concept_relations": int(
                conn.execute("SELECT COUNT(*) FROM task_ksa_concept_relations").fetchone()[0]
            ),
            "task_similarity_links": int(conn.execute("SELECT COUNT(*) FROM task_similarity_links").fetchone()[0]),
            "training_courses": int(conn.execute("SELECT COUNT(*) FROM ncs_training_courses").fetchone()[0]),
            "training_course_unit_links": int(
                conn.execute("SELECT COUNT(*) FROM ncs_training_course_unit_links").fetchone()[0]
            ),
            "training_course_concept_links": int(
                conn.execute("SELECT COUNT(*) FROM ncs_training_course_concept_links").fetchone()[0]
            ),
            "training_course_element_links": int(
                conn.execute("SELECT COUNT(*) FROM ncs_training_course_element_links").fetchone()[0]
            ),
            "training_goal_concept_links": int(
                conn.execute("SELECT COUNT(*) FROM training_goal_concept_links").fetchone()[0]
            ),
            "training_delivery_relations": int(
                conn.execute("SELECT COUNT(*) FROM training_delivery_relations").fetchone()[0]
            ),
            "qualification_items": int(
                conn.execute("SELECT COUNT(*) FROM ncs_qualification_items").fetchone()[0]
            ),
            "unit_qualification_links": int(
                conn.execute("SELECT COUNT(*) FROM ncs_unit_qualification_links").fetchone()[0]
            ),
            "job_base_competencies": int(
                conn.execute("SELECT COUNT(*) FROM ncs_job_base_competencies").fetchone()[0]
            ),
            "job_base_factors": int(
                conn.execute("SELECT COUNT(*) FROM ncs_job_base_factors").fetchone()[0]
            ),
            "unit_job_base_links": int(
                conn.execute("SELECT COUNT(*) FROM ncs_unit_job_base_links").fetchone()[0]
            ),
        }
        payload["refinement_status"] = {
            "jobs": int(conn.execute("SELECT COUNT(*) FROM refinement_jobs").fetchone()[0]),
            "pending_jobs": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM refinement_jobs
                    WHERE review_status = 'review_required'
                    """
                ).fetchone()[0]
            ),
            "applied_jobs": int(
                conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM refinement_jobs
                    WHERE review_status = 'applied'
                    """
                ).fetchone()[0]
            ),
        }
        conn.close()
    return payload


def require_ready_for_preprocess(reset: bool, allow_append: bool) -> None:
    settings = load_settings()
    if settings.excel_path is None or not settings.excel_path.exists():
        raise SystemExit("NCS_EXCEL_PATH is missing or does not exist.")
    if settings.db_path.exists() and not reset and not allow_append:
        counts = table_counts(settings.db_path)
        if counts.get("raw_excel_rows", 0) > 0:
            raise SystemExit(
                "Refusing to append to an existing DB. Use --reset or --allow-append."
            )


def run_smoke_check(
    major_code: str = "02",
    middle_code: str = "02",
    small_code: str = "02",
    sub_code: str = "01",
) -> dict[str, Any]:
    units = get_competency_units(
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        limit=50,
    )["units"]
    if not units:
        raise SystemExit("Smoke check failed: no competency units returned.")
    structure = get_unit_structure(units[0]["unit_code"])
    if "error" in structure:
        raise SystemExit(f"Smoke check failed: {structure}")
    elements = structure["elements"]
    criteria_total = sum(len(item["performance_criteria"]) for item in elements)
    ksa_total = sum(len(item["ksa"]) for item in elements)
    if not elements or not criteria_total or not ksa_total:
        raise SystemExit("Smoke check failed: incomplete unit hierarchy.")
    return {
        "classification": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
        },
        "unit_count": len(units),
        "sample_unit": structure["unit"]["unit_code"],
        "sample_unit_name": structure["unit"]["unit_name"],
        "duty_definition_present": bool(
            structure["unit"]["classification"].get("duty_definition")
        ),
        "sample_elements": len(elements),
        "sample_criteria": criteria_total,
        "sample_ksa": ksa_total,
        "element_api_statuses": sorted(
            {item.get("api_match_status") for item in elements if item.get("api_match_status")}
        ),
    }


def plan_element_batches(batch_size: int, concurrency: int) -> dict[str, Any]:
    settings = load_settings()
    conn = connect(settings.db_path)
    initialize_database(conn)
    total = int(conn.execute("SELECT COUNT(*) FROM competency_elements").fetchone()[0])
    remaining = int(
        conn.execute(
            "SELECT COUNT(*) FROM competency_elements WHERE api_match_status != 'matched'"
        ).fetchone()[0]
    )
    conn.close()
    command = (
        "python src\\ncs_mcp\\collect_api.py --mode elements "
        f"--element-limit {batch_size} --only-uncollected --timeout 90 "
        f"--concurrency {concurrency} --max-retries 2"
    )
    return {
        "total_elements": total,
        "remaining_elements": remaining,
        "batch_size": batch_size,
        "concurrency": concurrency,
        "estimated_batches": math.ceil(remaining / batch_size) if batch_size else 0,
        "repeat_command": command,
    }


def add_issue(issues: list[dict[str, str]], severity: str, check: str, detail: str) -> None:
    issues.append({"severity": severity, "check": check, "detail": detail})


def scan_text_files() -> list[Path]:
    candidates: list[Path] = []
    for folder in [ROOT, ROOT / "src", ROOT / "tests", ROOT / "scripts", ROOT / "docs"]:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".py", ".md", ".toml", ".txt"}:
                candidates.append(path)
    return candidates


def lint_repo(strict: bool = False) -> dict[str, Any]:
    settings = load_settings()
    issues: list[dict[str, str]] = []

    required_paths = [
        ROOT / "AGENTS.md",
        ROOT / "ARCHITECTURE.md",
        ROOT / "docs" / "HARNESS_ENGINEERING.md",
        ROOT / "docs" / "NCS_MCP_PRD.md",
        ROOT / "src" / "ncs_mcp" / "db.py",
        ROOT / "src" / "ncs_mcp" / "preprocess_excel.py",
        ROOT / "src" / "ncs_mcp" / "collect_api.py",
        ROOT / "src" / "ncs_mcp" / "server.py",
    ]
    for path in required_paths:
        if not path.exists():
            add_issue(issues, "error", "required_path", f"Missing {path.relative_to(ROOT)}")

    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8") if (ROOT / "AGENTS.md").exists() else ""
    for expected in ["ARCHITECTURE.md", "HARNESS_ENGINEERING.md", "ncs_harness.py"]:
        if expected not in agents:
            add_issue(issues, "warning", "agents_map", f"AGENTS.md does not reference {expected}")

    forbidden_imports = {
        "src/ncs_mcp/server.py": ["requests", "openpyxl"],
        "src/ncs_mcp/preprocess_excel.py": ["requests", "mcp.server"],
        "src/ncs_mcp/collect_api.py": ["openpyxl", "mcp.server"],
        "src/ncs_mcp/db.py": ["requests", "openpyxl", "mcp.server"],
    }
    for rel_path, forbidden in forbidden_imports.items():
        path = ROOT / rel_path
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in text:
                add_issue(
                    issues,
                    "error",
                    "module_boundary",
                    f"{rel_path} must not depend on {token}",
                )

    secret_values = {
        value
        for value in [
            settings.service_key,
            settings.training_course_service_key,
        ]
        if value
    }
    for secret_value in secret_values:
        for path in scan_text_files():
            if secret_value in path.read_text(encoding="utf-8", errors="ignore"):
                add_issue(
                    issues,
                    "error",
                    "secret_scan",
                    f"Service key appears in {path.relative_to(ROOT)}",
                )
    if settings.db_path.exists():
        conn = connect(settings.db_path)
        initialize_database(conn)
        raw_count = int(conn.execute("SELECT COUNT(*) FROM raw_excel_rows").fetchone()[0])
        link_count = int(
            conn.execute("SELECT COUNT(*) FROM element_criteria_ksa_links").fetchone()[0]
        )
        if raw_count != link_count:
            add_issue(
                issues,
                "error",
                "db_integrity",
                f"raw_excel_rows={raw_count} but links={link_count}",
            )
        ksa_count = int(conn.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0])
        if raw_count and not ksa_count:
            add_issue(issues, "error", "db_integrity", "DB has raw rows but no KSA rows")
        unmatched_units = int(
            conn.execute(
                "SELECT COUNT(*) FROM competency_units WHERE api_match_status != 'matched'"
            ).fetchone()[0]
        )
        if strict and unmatched_units:
            add_issue(
                issues,
                "error",
                "api_coverage",
                f"{unmatched_units} competency units are not API matched",
            )
        conn.close()
    elif strict:
        add_issue(issues, "error", "db_integrity", "SQLite DB does not exist")

    errors = [issue for issue in issues if issue["severity"] == "error"]
    warnings = [issue for issue in issues if issue["severity"] == "warning"]
    return {
        "ok": not errors,
        "errors": len(errors),
        "warnings": len(warnings),
        "issues": issues,
    }


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    settings = load_settings()
    summary: dict[str, Any] = {"stages": {}}
    if args.preprocess:
        require_ready_for_preprocess(args.reset, args.allow_append)
        summary["stages"]["preprocess"] = preprocess_excel(
            excel_path=settings.excel_path,
            db_path=settings.db_path,
            reports_dir=settings.reports_dir,
            reset=args.reset,
            sheets=set(args.sheets.split(",")) if args.sheets else None,
            max_rows=args.max_rows,
        )
    if args.quality:
        summary["stages"]["quality"] = run_quality_checks(settings.db_path, settings.reports_dir)
    if args.api_standards:
        summary["stages"]["api_standards"] = collect_standard_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
        )
    if args.api_subd:
        summary["stages"]["api_subd"] = collect_subd_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
        )
    if args.api_elements_hr:
        summary["stages"]["api_elements_hr"] = collect_elements_api(
            settings.db_path,
            settings.reports_dir,
            settings.service_key or "",
            timeout=args.timeout,
            major_code="02",
            middle_code="02",
            small_code="02",
            sub_code="01",
        )
    if args.api_sqf:
        summary["stages"]["api_sqf"] = collect_sqf_api(
            settings.db_path,
            settings.reports_dir,
            settings.sqf_service_key or "",
            timeout=args.timeout,
            major_code=args.sqf_major_code,
            major_limit=args.sqf_major_limit,
        )
    if args.collect_study_modules:
        if not settings.study_module_service_key:
            raise SystemExit("NCS_STUDY_MODULE_SERVICE_KEY is required for --collect-study-modules.")
        summary["stages"]["collect_study_modules"] = collect_study_modules_for_scope(
            db_path=settings.db_path,
            service_key=settings.study_module_service_key,
            major_code=args.study_module_major_code,
            all_majors=args.study_module_all_majors,
            module_name=args.study_module_name,
            num_of_rows=args.study_module_num_of_rows,
            timeout=args.timeout,
            max_pages=args.study_module_max_pages,
        )
    if args.collect_training_courses:
        if not settings.training_course_service_key:
            raise SystemExit("NCS_TRAINING_COURSE_SERVICE_KEY or NCS_SERVICE_KEY is required.")
        summary["stages"]["collect_training_courses"] = collect_training_courses_for_scope(
            db_path=settings.db_path,
            service_key=settings.training_course_service_key,
            major_code=args.training_course_major_code,
            all_majors=args.training_course_all_majors,
            num_of_rows=args.training_course_num_of_rows,
            timeout=args.timeout,
            max_pages=args.training_course_max_pages,
        )
    if args.training_course_links:
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            summary["stages"]["training_course_links"] = build_training_course_ontology_links(
                conn,
                major_code=args.training_course_major_code,
                reset=args.reset_training_course_links,
            )
        finally:
            conn.close()
    if args.collect_job_base:
        if not settings.job_base_service_key:
            raise SystemExit("NCS_JOB_BASE_SERVICE_KEY is required for --collect-job-base.")
        summary["stages"]["collect_job_base"] = collect_job_base_for_scope(
            db_path=settings.db_path,
            service_key=settings.job_base_service_key,
            major_code=args.job_base_major_code,
            all_majors=args.job_base_all_majors,
            module_name=args.job_base_module_name,
            num_of_rows=args.job_base_num_of_rows,
            timeout=args.timeout,
            max_pages=args.job_base_max_pages,
        )
    if args.collect_sqf_library:
        summary["stages"]["collect_sqf_library"] = collect_sqf_library(
            settings.db_path,
            raw_dir=ROOT / "data" / "raw" / "sqf_docs",
            start_page=args.sqf_library_start_page,
            end_page=args.sqf_library_end_page,
            download=args.download_sqf_library,
            timeout=args.timeout,
            overwrite=args.overwrite_sqf_library,
            delay=args.sqf_library_delay,
        )
    if args.build_sqf_sqlite_model:
        summary["stages"]["build_sqf_sqlite_model"] = build_sqf_sqlite_model(settings.db_path)
    if args.preprocess_sqf_documents:
        summary["stages"]["preprocess_sqf_documents"] = preprocess_sqf_documents(
            settings.db_path,
            extracted_dir=ROOT / "data" / "raw" / "sqf_docs_extracted",
            chunk_chars=args.sqf_chunk_chars,
            overlap_chars=args.sqf_overlap_chars,
            ocr_empty=args.sqf_ocr_empty,
            ocr_lang=args.sqf_ocr_lang,
            ocr_dpi=args.sqf_ocr_dpi,
            ocr_max_pages=args.sqf_ocr_max_pages,
            only_unprocessed=args.sqf_only_unprocessed,
        )
    if args.build_sqf_precision_matches:
        summary["stages"]["build_sqf_precision_matches"] = build_sqf_chunk_job_level_matches(
            settings.db_path,
            min_score=args.sqf_precision_min_score,
            max_matches_per_chunk=args.sqf_precision_max_matches_per_chunk,
            asset_id=args.sqf_precision_asset_id,
            include_framework_references=args.sqf_precision_include_framework_references,
        )
    if args.build_sqf_mappings:
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            summary["stages"]["build_sqf_mappings"] = build_sqf_mapping_candidates(
                conn,
                mvp_only=not args.all_sqf_mappings,
                major_code=args.mapping_major_code,
                keyword=args.mapping_keyword,
                source_key=args.mapping_source_key,
                limit_per_duty=args.mapping_limit_per_duty,
                duty_limit=args.mapping_duty_limit,
            )
        finally:
            conn.close()
    if args.mvp_bootstrap:
        summary["stages"]["mvp_bootstrap"] = run_mvp_bootstrap(
            settings.db_path,
            limit_per_duty=args.mvp_limit_per_duty,
            accept_top_n=args.mvp_accept_top_n,
            min_accept_score=args.mvp_min_accept_score,
            dry_run=args.mvp_dry_run,
        )
    if args.validate_ontology:
        summary["stages"]["validate_ontology"] = validate_ontology_readiness(settings.db_path)
    if args.export_ontology_jsonld:
        summary["stages"]["export_ontology_jsonld"] = export_ontology_jsonld(
            settings.db_path,
            Path(args.ontology_jsonld_out),
            include_excluded_mappings=args.ontology_include_excluded_mappings,
            include_chunk_evidence=not args.ontology_no_chunk_evidence,
            chunk_evidence_limit=args.ontology_chunk_evidence_limit,
            include_document_chunks=not args.ontology_no_document_chunks,
            document_chunk_limit=args.ontology_document_chunk_limit,
        )
    if args.refine:
        summary["stages"]["refine_generate"] = run_refinement_harness(
            settings.db_path,
            action="generate",
            issue_types=parse_csv(args.refine_issue_types),
            target_types=parse_csv(args.refine_target_types),
            limit=args.refine_limit,
        )
    if args.apply_refinements:
        summary["stages"]["refine_apply"] = run_refinement_harness(
            settings.db_path,
            action="apply",
            target_types=parse_csv(args.refine_target_types),
            limit=args.refine_limit,
        )
    if args.smoke:
        os.environ["NCS_DB_PATH"] = str(settings.db_path)
        summary["stages"]["smoke"] = run_smoke_check()
    if args.lint:
        summary["stages"]["lint"] = lint_repo(strict=args.strict_lint)
    summary["snapshot"] = inspect_project()
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="NCS MCP project harness.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("inspect", help="Show environment, path, DB, and API status.")

    smoke = subparsers.add_parser("smoke", help="Run MCP-layer smoke checks.")
    smoke.add_argument("--major-code", default="02")
    smoke.add_argument("--middle-code", default="02")
    smoke.add_argument("--small-code", default="02")
    smoke.add_argument("--sub-code", default="01")

    plan = subparsers.add_parser("plan-elements", help="Plan /NCS006 batch collection.")
    plan.add_argument("--batch-size", type=int, default=8000)
    plan.add_argument("--concurrency", type=int, default=8)

    study_modules = subparsers.add_parser("query-study-modules", help=argparse.SUPPRESS)
    study_modules.add_argument("--major-code", default="02")
    study_modules.add_argument("--module-name")
    study_modules.add_argument("--page-no", type=int, default=1)
    study_modules.add_argument("--num-of-rows", type=int, default=10)
    study_modules.add_argument("--timeout", type=int, default=30)

    collect_study = subparsers.add_parser("collect-study-modules", help=argparse.SUPPRESS)
    collect_study.add_argument("--major-code")
    collect_study.add_argument("--module-name")
    collect_study.add_argument("--page-no", type=int, default=1)
    collect_study.add_argument("--num-of-rows", type=int, default=200)
    collect_study.add_argument("--timeout", type=int, default=30)
    collect_study.add_argument("--max-pages", type=int)
    collect_study.add_argument("--all-majors", action="store_true")

    training_courses = subparsers.add_parser(
        "query-training-courses",
        help="Query the NCS training course openapi18 endpoint without storing rows.",
    )
    training_courses.add_argument("--major-code", default="02")
    training_courses.add_argument("--module-name")
    training_courses.add_argument("--page-no", type=int, default=1)
    training_courses.add_argument("--num-of-rows", type=int, default=10)
    training_courses.add_argument("--timeout", type=int, default=30)

    collect_training = subparsers.add_parser(
        "collect-training-courses",
        help="Collect and link NCS training course rows from openapi18.",
    )
    collect_training.add_argument("--major-code")
    collect_training.add_argument("--module-name")
    collect_training.add_argument("--page-no", type=int, default=1)
    collect_training.add_argument("--num-of-rows", type=int, default=500)
    collect_training.add_argument("--max-pages", type=int)
    collect_training.add_argument("--timeout", type=int, default=30)
    collect_training.add_argument("--all-majors", action="store_true")

    query_qualification = subparsers.add_parser(
        "query-qualification-items",
        help="Query the NCS unit qualification item API for one competency unit.",
    )
    query_qualification.add_argument("--unit-code", required=True)
    query_qualification.add_argument("--page-no", type=int, default=1)
    query_qualification.add_argument("--num-of-rows", type=int, default=10)
    query_qualification.add_argument("--timeout", type=int, default=30)

    collect_qualification = subparsers.add_parser(
        "collect-qualification-items",
        help="Collect qualification item links for NCS competency units.",
    )
    collect_qualification.add_argument("--unit-code", action="append", default=[])
    collect_qualification.add_argument("--major-code")
    collect_qualification.add_argument("--all-units", action="store_true")
    collect_qualification.add_argument("--limit-units", type=int)
    collect_qualification.add_argument("--page-no", type=int, default=1)
    collect_qualification.add_argument("--num-of-rows", type=int, default=50)
    collect_qualification.add_argument("--max-pages", type=int)
    collect_qualification.add_argument("--timeout", type=int, default=30)
    collect_qualification.add_argument("--refresh", action="store_true")
    collect_qualification.add_argument("--request-delay", type=float, default=0.2)
    collect_qualification.add_argument("--max-retries", type=int, default=3)
    collect_qualification.add_argument("--retry-backoff-seconds", type=float, default=2.0)
    collect_qualification.add_argument(
        "--stop-after-rate-limit-errors",
        type=int,
        default=0,
        help="Stop the current collection batch after this many rate-limit errors. 0 disables the guard.",
    )

    qualification_errors = subparsers.add_parser(
        "qualification-error-report",
        help="Report failed NCS qualification API collection units.",
    )
    qualification_errors.add_argument("--limit", type=int, default=50)
    qualification_errors.add_argument(
        "--report-path",
        default=str(ROOT / "reports" / "qualification_error_report.md"),
    )

    qualification_hygiene = subparsers.add_parser(
        "qualification-retry-hygiene",
        help="Read-only dry-run report for qualification API error retry metadata.",
    )
    qualification_hygiene.add_argument("--limit", type=int, default=50)
    qualification_hygiene.add_argument("--retry-backoff-seconds", type=float, default=5.0)
    qualification_hygiene.add_argument("--apply", action="store_true")
    qualification_hygiene.add_argument("--max-updates", type=int)
    qualification_hygiene.add_argument("--out", type=Path)
    qualification_hygiene.add_argument("--markdown-out", type=Path)

    recommendation_evidence_hygiene = subparsers.add_parser(
        "recommendation-evidence-hygiene",
        help="Report or repair saved recommendation evidence references.",
    )
    recommendation_evidence_hygiene.add_argument("--limit", type=int, default=50)
    recommendation_evidence_hygiene.add_argument("--apply", action="store_true")
    recommendation_evidence_hygiene.add_argument("--max-updates", type=int)
    recommendation_evidence_hygiene.add_argument("--out", type=Path)
    recommendation_evidence_hygiene.add_argument("--markdown-out", type=Path)

    api_quality_hygiene = subparsers.add_parser(
        "api-quality-hygiene",
        help="Resolve duplicate or normalized-equal cached API quality issues without calling external APIs.",
    )
    api_quality_hygiene.add_argument("--limit", type=int, default=50)
    api_quality_hygiene.add_argument("--apply", action="store_true")
    api_quality_hygiene.add_argument("--max-updates", type=int)
    api_quality_hygiene.add_argument("--out", type=Path)
    api_quality_hygiene.add_argument("--markdown-out", type=Path)

    retry_qualification = subparsers.add_parser(
        "retry-qualification-errors",
        help="Retry only qualification API units whose collection status is error.",
    )
    retry_qualification.add_argument("--major-code")
    retry_qualification.add_argument("--limit-units", type=int)
    retry_qualification.add_argument("--page-no", type=int, default=1)
    retry_qualification.add_argument("--num-of-rows", type=int, default=50)
    retry_qualification.add_argument("--max-pages", type=int)
    retry_qualification.add_argument("--timeout", type=int, default=30)
    retry_qualification.add_argument("--request-delay", type=float, default=1.0)
    retry_qualification.add_argument("--max-retries", type=int, default=5)
    retry_qualification.add_argument("--retry-backoff-seconds", type=float, default=5.0)
    retry_qualification.add_argument(
        "--stop-after-rate-limit-errors",
        type=int,
        default=0,
        help="Stop the retry batch after this many rate-limit errors. 0 disables the guard.",
    )
    retry_qualification.add_argument(
        "--include-not-due",
        action="store_true",
        help="Retry error units even when next_retry_at is still in the future.",
    )
    retry_qualification.add_argument(
        "--report-path",
        default=str(ROOT / "reports" / "qualification_error_report.md"),
    )

    qualification = subparsers.add_parser(
        "qualification-summary",
        help="Summarize cached NCS unit qualification item links.",
    )
    qualification.add_argument("--limit", type=int, default=20)

    query_job_base = subparsers.add_parser(
        "query-job-base",
        help="Query the NCS job base competency openapi19 endpoint without storing rows.",
    )
    query_job_base.add_argument("--major-code", default="02")
    query_job_base.add_argument("--module-name")
    query_job_base.add_argument("--page-no", type=int, default=1)
    query_job_base.add_argument("--num-of-rows", type=int, default=10)
    query_job_base.add_argument("--timeout", type=int, default=30)

    collect_job_base = subparsers.add_parser(
        "collect-job-base",
        help="Collect and link NCS job base competencies from openapi19.",
    )
    collect_job_base.add_argument("--major-code")
    collect_job_base.add_argument("--module-name")
    collect_job_base.add_argument("--page-no", type=int, default=1)
    collect_job_base.add_argument("--num-of-rows", type=int, default=500)
    collect_job_base.add_argument("--max-pages", type=int)
    collect_job_base.add_argument("--timeout", type=int, default=30)
    collect_job_base.add_argument("--all-majors", action="store_true")

    job_base = subparsers.add_parser(
        "job-base-summary",
        help="Summarize cached NCS job base competency links.",
    )
    job_base.add_argument("--limit", type=int, default=20)

    quality_gates = subparsers.add_parser(
        "quality-gates",
        help="Evaluate read-only NCS ontology and training recommendation quality gates.",
    )
    quality_gates.add_argument("--include-transition-eval", action="store_true")
    quality_gates.add_argument("--transition-limit", type=int, default=5)
    quality_gates.add_argument("--transition-scenario-limit", type=int)
    quality_gates.add_argument("--out", type=Path)
    quality_gates.add_argument("--markdown-out", type=Path)

    ncs_ontology = subparsers.add_parser(
        "preprocess-ncs-ontology",
        help="Seed full NCS KSA ontology, atomic KSA preprocessing, task KSA relations, and task similarity.",
    )
    ncs_ontology.add_argument("--relations-per-concept", type=int, default=2)
    ncs_ontology.add_argument("--reset-relations", action="store_true")
    ncs_ontology.add_argument("--no-relations", action="store_true")
    ncs_ontology.add_argument("--atomic-ksa", action="store_true")
    ncs_ontology.add_argument("--reset-atomic-ksa", action="store_true")
    ncs_ontology.add_argument("--task-ksa-relations", action="store_true")
    ncs_ontology.add_argument("--reset-task-ksa-relations", action="store_true")
    ncs_ontology.add_argument("--task-similarity", action="store_true")
    ncs_ontology.add_argument("--reset-task-similarity", action="store_true")
    ncs_ontology.add_argument("--max-links-per-task", type=int, default=10)
    ncs_ontology.add_argument("--min-shared-concepts", type=int, default=2)
    ncs_ontology.add_argument("--max-concept-task-frequency", type=int, default=120)
    ncs_ontology.add_argument("--ksa-meanings", action="store_true")
    ncs_ontology.add_argument("--reset-ksa-meanings", action="store_true")
    ncs_ontology.add_argument("--ksa-meaning-major-code")
    ncs_ontology.add_argument("--ksa-meaning-limit", type=int)
    ncs_ontology.add_argument("--apply-ksa-meaning-definitions", action="store_true")
    ncs_ontology.add_argument("--training-course-links", action="store_true")
    ncs_ontology.add_argument("--reset-training-course-links", action="store_true")
    ncs_ontology.add_argument("--training-course-major-code")

    recommend_tasks = subparsers.add_parser(
        "recommend-task-transitions",
        help="Recommend nearby NCS tasks for upskilling/reskilling using atomic KSA similarity.",
    )
    recommend_tasks.add_argument("--criteria-id", type=int)
    recommend_tasks.add_argument("--query")
    recommend_tasks.add_argument("--unit-code")
    recommend_tasks.add_argument("--mode", choices=["all", "upskilling", "reskilling"], default="all")
    recommend_tasks.add_argument("--limit", type=int, default=10)
    recommend_tasks.add_argument("--evidence-limit", type=int, default=12)

    recommend_training = subparsers.add_parser(
        "recommend-training-for-task",
        help="Recommend NCS training courses for a task using KSA ontology links.",
    )
    recommend_training.add_argument("--criteria-id", type=int)
    recommend_training.add_argument("--query")
    recommend_training.add_argument("--unit-code")
    recommend_training.add_argument("--major-code")
    recommend_training.add_argument("--middle-code")
    recommend_training.add_argument("--small-code")
    recommend_training.add_argument("--sub-code")
    recommend_training.add_argument("--mode", choices=["all", "upskilling", "reskilling"], default="all")
    recommend_training.add_argument("--preferred-max-hours", type=float)
    recommend_training.add_argument("--preferred-method", action="append", default=[])
    recommend_training.add_argument("--limit", type=int, default=5)
    recommend_training.add_argument("--compact", action="store_true")
    recommend_training.add_argument("--no-save", action="store_true")

    recommend_transition = subparsers.add_parser(
        "recommend-training-transition",
        help="Recommend NCS training courses for moving from one NCS scope to another.",
    )
    recommend_transition.add_argument("--current-query", required=True)
    recommend_transition.add_argument("--target-query", required=True)
    recommend_transition.add_argument("--major-code")
    recommend_transition.add_argument("--current-major-code")
    recommend_transition.add_argument("--target-major-code")
    recommend_transition.add_argument("--current-middle-code")
    recommend_transition.add_argument("--target-middle-code")
    recommend_transition.add_argument("--current-small-code")
    recommend_transition.add_argument("--target-small-code")
    recommend_transition.add_argument("--current-sub-code")
    recommend_transition.add_argument("--target-sub-code")
    recommend_transition.add_argument("--mode", choices=["all", "upskilling", "reskilling"], default="all")
    recommend_transition.add_argument("--preferred-max-hours", type=float)
    recommend_transition.add_argument("--preferred-method", action="append", default=[])
    recommend_transition.add_argument("--limit", type=int, default=5)
    recommend_transition.add_argument("--compact", action="store_true")
    recommend_transition.add_argument("--no-save", action="store_true")

    evaluate_transition = subparsers.add_parser(
        "evaluate-training-transitions",
        help="Evaluate transition training recommendations against seeded gold scenarios.",
    )
    evaluate_transition.add_argument("--limit", type=int, default=5)
    evaluate_transition.add_argument("--scenario-limit", type=int)
    evaluate_transition.add_argument(
        "--trusted-only",
        action="store_true",
        help="Evaluate only trusted transition scenarios: human_reviewed, reviewed, or accepted.",
    )
    evaluate_transition.add_argument(
        "--review-status",
        action="append",
        default=[],
        help="Limit evaluation to one or more review statuses. Can be repeated or comma separated.",
    )

    generate_transition_eval = subparsers.add_parser(
        "generate-training-transition-eval-set",
        help="Generate non-HR transition evaluation scenarios from NCS units and training courses.",
    )
    generate_transition_eval.add_argument("--target-non-hr-count", type=int, default=70)
    generate_transition_eval.add_argument("--per-major-limit", type=int, default=8)
    generate_transition_eval.add_argument("--per-classification-limit", type=int, default=3)
    generate_transition_eval.add_argument("--reset-auto", action="store_true")
    generate_transition_eval.add_argument("--limit", type=int, default=5)
    generate_transition_eval.add_argument("--scenario-limit", type=int)
    generate_transition_eval.add_argument(
        "--report-path",
        default=str(ROOT / "reports" / "training_transition_evaluation.md"),
    )
    review_transition_scenarios = subparsers.add_parser(
        "review-training-transition-scenarios",
        help="Review candidate transition scenarios with deterministic evaluation gates.",
    )
    review_transition_scenarios.add_argument("--limit", type=int, default=5)
    review_transition_scenarios.add_argument("--scenario-limit", type=int)
    review_transition_scenarios.add_argument(
        "--source-review-status",
        action="append",
        default=[],
        help="Source statuses to review. Defaults to candidate. Can be repeated or comma separated.",
    )
    review_transition_scenarios.add_argument(
        "--target-review-status",
        choices=["reviewed", "accepted"],
        default="reviewed",
    )
    review_transition_scenarios.add_argument("--min-precision-at-k", type=float, default=0.0)
    review_transition_scenarios.add_argument(
        "--min-expected-recall-at-k",
        type=float,
        default=TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
        help="Minimum per-scenario expected course recall required before status promotion.",
    )
    review_transition_scenarios.add_argument(
        "--allow-top1-miss",
        action="store_true",
        help="Do not require the first recommended course to be in the expected course set.",
    )
    review_transition_scenarios.add_argument(
        "--apply",
        action="store_true",
        help="Apply eligible review-status updates. Without this flag the command is a dry run.",
    )
    review_transition_scenarios.add_argument(
        "--report-path",
        default=str(ROOT / "reports" / "training_transition_scenario_review.md"),
    )

    hr_review = subparsers.add_parser(
        "prepare-hr-review-queue",
        help="Create quality issues for human review of high-impact HR ontology concepts and training-goal links.",
    )
    hr_review.add_argument("--major-code", default="02")
    hr_review.add_argument("--middle-code", default="02")
    hr_review.add_argument("--small-code", default="02")
    hr_review.add_argument("--concept-limit", type=int, default=250)
    hr_review.add_argument("--goal-link-limit", type=int, default=250)
    hr_review.add_argument("--dry-run", action="store_true")

    ontology_review = subparsers.add_parser(
        "prepare-ontology-review-queue",
        help="Create all-domain quality issues for human review of weak ontology links.",
    )
    ontology_review.add_argument("--major-code")
    ontology_review.add_argument("--middle-code")
    ontology_review.add_argument("--small-code")
    ontology_review.add_argument("--sub-code")
    ontology_review.add_argument("--concept-limit", type=int, default=250)
    ontology_review.add_argument("--goal-link-limit", type=int, default=250)
    ontology_review.add_argument("--relation-limit", type=int, default=250)
    ontology_review.add_argument("--min-confidence", type=float, default=0.75)
    ontology_review.add_argument("--dry-run", action="store_true")

    review_priority = subparsers.add_parser(
        "review-priority",
        help="List read-only human-review priorities from open quality issues.",
    )
    review_priority.add_argument("--limit", type=int, default=20)
    review_priority.add_argument("--per-issue-type-limit", type=int, default=5)
    review_priority.add_argument("--issue-types", help="Comma separated issue types.")
    review_priority.add_argument("--out", type=Path)
    review_priority.add_argument("--markdown-out", type=Path)

    review_seedpack = subparsers.add_parser(
        "export-review-seedpack",
        help="Export a JSONL human-review seedpack from open quality issues.",
    )
    review_seedpack.add_argument("--limit", type=int, default=50)
    review_seedpack.add_argument("--per-issue-type-limit", type=int, default=5)
    review_seedpack.add_argument("--issue-types", help="Comma separated issue types.")
    review_seedpack.add_argument("--out", type=Path, required=True)
    review_seedpack.add_argument("--markdown-out", type=Path)
    review_seedpack.add_argument("--source-report-path")

    transition_seedpack = subparsers.add_parser(
        "export-transition-scenario-seedpack",
        help="Export a JSONL human-review seedpack for transition gold scenarios.",
    )
    transition_seedpack.add_argument("--review-status", action="append")
    transition_seedpack.add_argument("--scenario-limit", type=int, default=20)
    transition_seedpack.add_argument("--recommendation-limit", type=int, default=5)
    transition_seedpack.add_argument("--out", type=Path, required=True)
    transition_seedpack.add_argument("--markdown-out", type=Path)
    transition_seedpack.add_argument("--source-report-path")

    review_triage = subparsers.add_parser(
        "review-triage",
        help="Build a read-only triage report from quality gates, review priority, and transition seedpack artifacts.",
    )
    review_triage.add_argument(
        "--quality-report",
        type=Path,
        default=Path("reports/quality_gates_with_transition.json"),
    )
    review_triage.add_argument(
        "--review-priority-report",
        type=Path,
        default=Path("reports/review_priority.json"),
    )
    review_triage.add_argument("--transition-seedpack", type=Path)
    review_triage.add_argument("--review-item-limit", type=int, default=20)
    review_triage.add_argument("--transition-item-limit", type=int, default=20)
    review_triage.add_argument("--out", type=Path)
    review_triage.add_argument("--markdown-out", type=Path)

    career_import = subparsers.add_parser(
        "import-career-paths",
        help="Import NCS career development path CSV and link it to NCS units.",
    )
    career_import.add_argument("--csv-path", required=True)
    career_import.add_argument("--encoding", default="cp949")
    career_import.add_argument("--reset", action="store_true")
    career_import.add_argument("--limit", type=int)

    career_summary = subparsers.add_parser(
        "career-path-summary",
        help="Summarize imported NCS career development paths.",
    )
    career_summary.add_argument("--limit", type=int, default=20)

    supplemental_import = subparsers.add_parser(
        "import-supplemental-ncs-data",
        help="Import supplemental NCS CSV sources into separate reference tables.",
    )
    supplemental_import.add_argument("--unit-standard-csv")
    supplemental_import.add_argument("--unit-standard-encoding", default="cp949")
    supplemental_import.add_argument("--occupation-mapping-csv")
    supplemental_import.add_argument("--occupation-mapping-encoding", default="utf-8-sig")
    supplemental_import.add_argument("--training-zip-csv")
    supplemental_import.add_argument("--training-zip-encoding", default="cp949")
    supplemental_import.add_argument("--reset", action="store_true")
    supplemental_import.add_argument("--limit", type=int)

    supplemental_summary = subparsers.add_parser(
        "supplemental-data-summary",
        help="Summarize imported supplemental NCS reference data.",
    )
    supplemental_summary.add_argument("--limit", type=int, default=20)

    resolve_scope = subparsers.add_parser(
        "resolve-ncs-query-scope",
        help="Resolve a natural-language query against the NCS hierarchy and ontology.",
    )
    resolve_scope.add_argument("--query", required=True)
    resolve_scope.add_argument("--major-code")
    resolve_scope.add_argument("--middle-code")
    resolve_scope.add_argument("--small-code")
    resolve_scope.add_argument("--sub-code")
    resolve_scope.add_argument("--limit", type=int, default=10)

    dashboard = subparsers.add_parser("dashboard", help="Print the dashboard command.")
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)

    mappings = subparsers.add_parser("build-sqf-mappings", help=argparse.SUPPRESS)
    mappings.add_argument("--all-sqf", action="store_true")
    mappings.add_argument("--major-code")
    mappings.add_argument("--keyword")
    mappings.add_argument("--source-key")
    mappings.add_argument("--limit-per-duty", type=int, default=10)
    mappings.add_argument("--duty-limit", type=int, default=5000)

    mvp_bootstrap = subparsers.add_parser(
        "mvp-bootstrap",
        help="Prepare the 02 경영관리 > 경영지원/인사 MVP review and recommendation baseline.",
    )
    mvp_bootstrap.add_argument("--refresh-sqf", action="store_true")
    mvp_bootstrap.add_argument("--timeout", type=int, default=90)
    mvp_bootstrap.add_argument("--limit-per-duty", type=int, default=10)
    mvp_bootstrap.add_argument("--accept-top-n", type=int, default=3)
    mvp_bootstrap.add_argument("--min-accept-score", type=float, default=7.0)
    mvp_bootstrap.add_argument("--dry-run", action="store_true")

    export_package = subparsers.add_parser(
        "export-package",
        help="Create a handoff package with schema, dictionary, sample queries, and optional DB.",
    )
    export_package.add_argument("--out", default=str(ROOT / "exports" / "ncs_sqf_output"))
    export_package.add_argument(
        "--db-mode",
        choices=["none", "copy", "hardlink"],
        default="none",
        help="none writes docs only; hardlink creates data/db/ncs_sqf.sqlite without full copy; copy makes an independent DB file.",
    )
    export_package.add_argument("--zip", action="store_true")

    sqf_library = subparsers.add_parser("collect-sqf-library", help=argparse.SUPPRESS)
    sqf_library.add_argument("--start-page", type=int, default=0)
    sqf_library.add_argument("--end-page", type=int, default=10)
    sqf_library.add_argument("--download", action="store_true")
    sqf_library.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw" / "sqf_docs")
    sqf_library.add_argument("--overwrite", action="store_true")
    sqf_library.add_argument("--timeout", type=int, default=30)
    sqf_library.add_argument("--delay", type=float, default=0.2)

    local_source = subparsers.add_parser(
        "import-ontology-source",
        help="Register a local PDF/HWP/ZIP as an ontology source document.",
    )
    local_source.add_argument("--input", type=Path, required=True)
    local_source.add_argument("--raw-dir", type=Path, default=ROOT / "data" / "raw" / "ontology_sources")
    local_source.add_argument("--title")
    local_source.add_argument("--role", default="framework_reference")
    local_source.add_argument("--source-url")
    local_source.add_argument("--notes")

    sqf_model = subparsers.add_parser("build-sqf-sqlite-model", help=argparse.SUPPRESS)
    sqf_model.add_argument("--summary", action="store_true")

    sqf_docs = subparsers.add_parser("preprocess-sqf-documents", help=argparse.SUPPRESS)
    sqf_docs.add_argument("--extracted-dir", type=Path, default=ROOT / "data" / "raw" / "sqf_docs_extracted")
    sqf_docs.add_argument("--chunk-chars", type=int, default=2400)
    sqf_docs.add_argument("--overlap-chars", type=int, default=250)
    sqf_docs.add_argument("--limit", type=int)
    sqf_docs.add_argument("--ocr-empty", action="store_true")
    sqf_docs.add_argument("--ocr-lang", default="kor+eng")
    sqf_docs.add_argument("--ocr-dpi", type=int, default=180)
    sqf_docs.add_argument("--ocr-max-pages", type=int)
    sqf_docs.add_argument("--only-unprocessed", action="store_true")

    sqf_precision = subparsers.add_parser("build-sqf-precision-matches", help=argparse.SUPPRESS)
    sqf_precision.add_argument("--min-score", type=float, default=9.0)
    sqf_precision.add_argument("--max-matches-per-chunk", type=int, default=8)
    sqf_precision.add_argument("--limit-chunks", type=int)
    sqf_precision.add_argument("--asset-id", type=int)
    sqf_precision.add_argument("--no-reset", action="store_true")
    sqf_precision.add_argument("--include-framework-references", action="store_true")

    evaluate = subparsers.add_parser("evaluate", help=argparse.SUPPRESS)
    evaluate.add_argument("--scope-tag")
    evaluate.add_argument("--run-name", default="mvp")

    ontology_export = subparsers.add_parser(
        "ontology",
        help="Validate or export the NCS ontology graph.",
    )
    ontology_export.add_argument("action", choices=["validate", "export-jsonld"])
    ontology_export.add_argument("--out", type=Path, default=ROOT / "exports" / "ncs_training_ontology.jsonld")
    ontology_export.add_argument("--include-excluded-mappings", action="store_true")
    ontology_export.add_argument("--no-chunk-evidence", action="store_true")
    ontology_export.add_argument("--chunk-evidence-limit", type=int, default=50000)
    ontology_export.add_argument("--no-document-chunks", action="store_true")
    ontology_export.add_argument("--document-chunk-limit", type=int, default=20000)

    refine = subparsers.add_parser(
        "refine",
        help="Generate/apply LLM-ready refinement jobs from quality issues.",
    )
    refine.add_argument("action", choices=["generate", "apply", "stats", "export-jsonl", "import-jsonl"])
    refine.add_argument("--issue-types", help="Comma separated issue types.")
    refine.add_argument("--target-types", help="Comma separated target types.")
    refine.add_argument("--severity")
    refine.add_argument("--provider", default="local-rule")
    refine.add_argument("--limit", type=int, default=50)
    refine.add_argument("--min-confidence", type=float, default=0.95)
    refine.add_argument("--dry-run", action="store_true")
    refine.add_argument("--out", type=Path)
    refine.add_argument("--input", type=Path)

    lint = subparsers.add_parser("lint", help="Check docs, boundaries, secrets, and DB invariants.")
    lint.add_argument("--strict", action="store_true")

    pipeline = subparsers.add_parser("pipeline", help="Run selected pipeline stages.")
    pipeline.add_argument("--preprocess", action="store_true")
    pipeline.add_argument("--reset", action="store_true")
    pipeline.add_argument("--allow-append", action="store_true")
    pipeline.add_argument("--sheets")
    pipeline.add_argument("--max-rows", type=int)
    pipeline.add_argument("--quality", action="store_true")
    pipeline.add_argument("--api-standards", action="store_true")
    pipeline.add_argument("--api-subd", action="store_true")
    pipeline.add_argument("--api-elements-hr", action="store_true")
    pipeline.add_argument("--api-sqf", action="store_true")
    pipeline.add_argument("--collect-study-modules", action="store_true")
    pipeline.add_argument("--study-module-major-code")
    pipeline.add_argument("--study-module-all-majors", action="store_true")
    pipeline.add_argument("--study-module-name")
    pipeline.add_argument("--study-module-num-of-rows", type=int, default=200)
    pipeline.add_argument("--study-module-max-pages", type=int)
    pipeline.add_argument("--collect-training-courses", action="store_true")
    pipeline.add_argument("--training-course-major-code")
    pipeline.add_argument("--training-course-all-majors", action="store_true")
    pipeline.add_argument("--training-course-num-of-rows", type=int, default=500)
    pipeline.add_argument("--training-course-max-pages", type=int)
    pipeline.add_argument("--collect-job-base", action="store_true")
    pipeline.add_argument("--job-base-major-code")
    pipeline.add_argument("--job-base-all-majors", action="store_true")
    pipeline.add_argument("--job-base-module-name")
    pipeline.add_argument("--job-base-num-of-rows", type=int, default=500)
    pipeline.add_argument("--job-base-max-pages", type=int)
    pipeline.add_argument("--training-course-links", action="store_true")
    pipeline.add_argument("--reset-training-course-links", action="store_true")
    pipeline.add_argument("--sqf-major-code")
    pipeline.add_argument("--sqf-major-limit", type=int)
    pipeline.add_argument("--collect-sqf-library", action="store_true")
    pipeline.add_argument("--download-sqf-library", action="store_true")
    pipeline.add_argument("--overwrite-sqf-library", action="store_true")
    pipeline.add_argument("--sqf-library-start-page", type=int, default=0)
    pipeline.add_argument("--sqf-library-end-page", type=int, default=10)
    pipeline.add_argument("--sqf-library-delay", type=float, default=0.2)
    pipeline.add_argument("--build-sqf-sqlite-model", action="store_true")
    pipeline.add_argument("--preprocess-sqf-documents", action="store_true")
    pipeline.add_argument("--sqf-chunk-chars", type=int, default=2400)
    pipeline.add_argument("--sqf-overlap-chars", type=int, default=250)
    pipeline.add_argument("--sqf-ocr-empty", action="store_true")
    pipeline.add_argument("--sqf-ocr-lang", default="kor+eng")
    pipeline.add_argument("--sqf-ocr-dpi", type=int, default=180)
    pipeline.add_argument("--sqf-ocr-max-pages", type=int)
    pipeline.add_argument("--sqf-only-unprocessed", action="store_true")
    pipeline.add_argument("--build-sqf-precision-matches", action="store_true")
    pipeline.add_argument("--sqf-precision-min-score", type=float, default=9.0)
    pipeline.add_argument("--sqf-precision-max-matches-per-chunk", type=int, default=8)
    pipeline.add_argument("--sqf-precision-asset-id", type=int)
    pipeline.add_argument("--sqf-precision-include-framework-references", action="store_true")
    pipeline.add_argument("--build-sqf-mappings", action="store_true")
    pipeline.add_argument("--all-sqf-mappings", action="store_true")
    pipeline.add_argument("--mapping-major-code")
    pipeline.add_argument("--mapping-keyword")
    pipeline.add_argument("--mapping-source-key")
    pipeline.add_argument("--mapping-limit-per-duty", type=int, default=10)
    pipeline.add_argument("--mapping-duty-limit", type=int, default=5000)
    pipeline.add_argument("--mvp-bootstrap", action="store_true")
    pipeline.add_argument("--mvp-limit-per-duty", type=int, default=10)
    pipeline.add_argument("--mvp-accept-top-n", type=int, default=3)
    pipeline.add_argument("--mvp-min-accept-score", type=float, default=7.0)
    pipeline.add_argument("--mvp-dry-run", action="store_true")
    pipeline.add_argument("--validate-ontology", action="store_true")
    pipeline.add_argument("--export-ontology-jsonld", action="store_true")
    pipeline.add_argument("--ontology-jsonld-out", default=str(ROOT / "exports" / "ncs_training_ontology.jsonld"))
    pipeline.add_argument("--ontology-include-excluded-mappings", action="store_true")
    pipeline.add_argument("--ontology-no-chunk-evidence", action="store_true")
    pipeline.add_argument("--ontology-chunk-evidence-limit", type=int, default=50000)
    pipeline.add_argument("--ontology-no-document-chunks", action="store_true")
    pipeline.add_argument("--ontology-document-chunk-limit", type=int, default=20000)
    pipeline.add_argument("--refine", action="store_true")
    pipeline.add_argument("--refine-issue-types")
    pipeline.add_argument("--refine-target-types")
    pipeline.add_argument("--refine-limit", type=int, default=50)
    pipeline.add_argument("--apply-refinements", action="store_true")
    pipeline.add_argument("--smoke", action="store_true")
    pipeline.add_argument("--lint", action="store_true")
    pipeline.add_argument("--strict-lint", action="store_true")
    pipeline.add_argument("--timeout", type=int, default=90)
    legacy_commands = {
        "query-study-modules",
        "collect-study-modules",
        "build-sqf-mappings",
        "mvp-bootstrap",
        "collect-sqf-library",
        "build-sqf-sqlite-model",
        "preprocess-sqf-documents",
        "build-sqf-precision-matches",
        "evaluate",
        "export-package",
        "import-ontology-source",
    }
    for action in getattr(subparsers, "_choices_actions", []):
        if getattr(action, "dest", None) in legacy_commands:
            action.help = argparse.SUPPRESS
    if hasattr(subparsers, "_choices_actions"):
        subparsers._choices_actions[:] = [
            action
            for action in subparsers._choices_actions
            if getattr(action, "dest", None) not in legacy_commands
        ]
    subparsers.metavar = (
        "{inspect,smoke,plan-elements,query-training-courses,"
        "collect-training-courses,query-qualification-items,"
        "collect-qualification-items,qualification-error-report,"
        "qualification-retry-hygiene,recommendation-evidence-hygiene,api-quality-hygiene,"
        "retry-qualification-errors,qualification-summary,"
        "query-job-base,collect-job-base,job-base-summary,"
        "preprocess-ncs-ontology,"
        "recommend-task-transitions,recommend-training-for-task,"
        "recommend-training-transition,resolve-ncs-query-scope,"
        "evaluate-training-transitions,generate-training-transition-eval-set,"
        "prepare-hr-review-queue,prepare-ontology-review-queue,"
        "review-priority,export-review-seedpack,export-transition-scenario-seedpack,"
        "review-triage,"
        "import-career-paths,career-path-summary,"
        "dashboard,ontology,refine,lint,pipeline}"
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.command == "inspect":
        print_json(inspect_project())
    elif args.command == "smoke":
        print_json(
            run_smoke_check(
                major_code=args.major_code,
                middle_code=args.middle_code,
                small_code=args.small_code,
                sub_code=args.sub_code,
            )
        )
    elif args.command == "plan-elements":
        print_json(plan_element_batches(args.batch_size, args.concurrency))
    elif args.command == "dashboard":
        print_json(
            {
                "command": f"python scripts\\ncs_dashboard.py --host {args.host} --port {args.port}",
                "url": f"http://{args.host}:{args.port}",
            }
        )
    elif args.command == "build-sqf-mappings":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                build_sqf_mapping_candidates(
                    conn,
                    mvp_only=not args.all_sqf,
                    major_code=args.major_code,
                    keyword=args.keyword,
                    source_key=args.source_key,
                    limit_per_duty=args.limit_per_duty,
                    duty_limit=args.duty_limit,
                )
            )
        finally:
            conn.close()
    elif args.command == "mvp-bootstrap":
        settings = load_settings()
        summary: dict[str, Any] = {"stages": {}}
        if args.refresh_sqf:
            if not settings.sqf_service_key:
                raise SystemExit("NCS_SQF_SERVICE_KEY is required for --refresh-sqf.")
            summary["stages"]["api_sqf"] = collect_sqf_api(
                settings.db_path,
                settings.reports_dir,
                settings.sqf_service_key,
                timeout=args.timeout,
                major_code="02",
            )
            summary["stages"]["build_sqf_sqlite_model"] = build_sqf_sqlite_model(settings.db_path)
        summary["stages"]["mvp_bootstrap"] = run_mvp_bootstrap(
            settings.db_path,
            limit_per_duty=args.limit_per_duty,
            accept_top_n=args.accept_top_n,
            min_accept_score=args.min_accept_score,
            dry_run=args.dry_run,
        )
        summary["snapshot"] = inspect_project()
        print_json(summary)
    elif args.command == "query-study-modules":
        settings = load_settings()
        if not settings.study_module_service_key:
            raise SystemExit(
                "NCS_STUDY_MODULE_SERVICE_KEY is required. Set it in .env before querying openapi21."
            )
        print_json(
            fetch_study_modules(
                settings.study_module_service_key,
                major_code=args.major_code,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                timeout=args.timeout,
            )
        )
    elif args.command == "collect-study-modules":
        settings = load_settings()
        if not settings.study_module_service_key:
            raise SystemExit(
                "NCS_STUDY_MODULE_SERVICE_KEY is required. Set it in .env before collecting openapi21."
            )
        print_json(
            collect_study_modules_for_scope(
                db_path=settings.db_path,
                service_key=settings.study_module_service_key,
                major_code=args.major_code,
                all_majors=args.all_majors,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                timeout=args.timeout,
                max_pages=args.max_pages,
            )
        )
    elif args.command == "query-training-courses":
        settings = load_settings()
        if not settings.training_course_service_key:
            raise SystemExit(
                "NCS_TRAINING_COURSE_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before querying openapi18."
            )
        print_json(
            fetch_training_course_page(
                settings.training_course_service_key,
                major_code=args.major_code,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                timeout=args.timeout,
            )
        )
    elif args.command == "collect-training-courses":
        settings = load_settings()
        if not settings.training_course_service_key:
            raise SystemExit(
                "NCS_TRAINING_COURSE_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before collecting openapi18."
            )
        print_json(
            collect_training_courses_for_scope(
                db_path=settings.db_path,
                service_key=settings.training_course_service_key,
                major_code=args.major_code,
                all_majors=args.all_majors,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                max_pages=args.max_pages,
                timeout=args.timeout,
            )
        )
    elif args.command == "query-qualification-items":
        settings = load_settings()
        if not settings.qualification_service_key:
            raise SystemExit(
                "NCS_QUALIFICATION_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before querying ncsClCdJm."
            )
        print_json(
            fetch_qualification_page(
                settings.qualification_service_key,
                unit_code=args.unit_code,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                timeout=args.timeout,
            )
        )
    elif args.command == "collect-qualification-items":
        settings = load_settings()
        if not settings.qualification_service_key:
            raise SystemExit(
                "NCS_QUALIFICATION_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before collecting ncsClCdJm."
            )
        print_json(
            collect_qualification_links(
                settings.db_path,
                settings.qualification_service_key,
                unit_codes=args.unit_code or None,
                major_code=args.major_code,
                all_units=args.all_units,
                limit_units=args.limit_units,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                max_pages=args.max_pages,
                timeout=args.timeout,
                resume=not args.refresh,
                request_delay=args.request_delay,
                max_retries=args.max_retries,
                retry_backoff_seconds=args.retry_backoff_seconds,
                stop_after_rate_limit_errors=args.stop_after_rate_limit_errors,
            )
        )
    elif args.command == "qualification-error-report":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            report = qualification_error_report(conn, limit=args.limit)
        finally:
            conn.close()
        report_path = Path(args.report_path)
        write_qualification_error_report(report_path, report)
        report["report_path"] = str(report_path)
        print_json(report)
    elif args.command == "qualification-retry-hygiene":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            if args.apply:
                report = apply_qualification_retry_hygiene(
                    conn,
                    limit=args.limit,
                    retry_backoff_seconds=args.retry_backoff_seconds,
                    max_updates=args.max_updates,
                )
            else:
                report = qualification_retry_hygiene_report(
                    conn,
                    limit=args.limit,
                    retry_backoff_seconds=args.retry_backoff_seconds,
                )
        finally:
            conn.close()
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["out_path"] = str(args.out)
        if args.markdown_out:
            write_qualification_retry_hygiene_markdown(report, args.markdown_out)
            report["markdown_path"] = str(args.markdown_out)
        print_json(report)
    elif args.command == "recommendation-evidence-hygiene":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            if args.apply:
                report = apply_recommendation_evidence_hygiene(
                    conn,
                    limit=args.limit,
                    max_updates=args.max_updates,
                )
            else:
                report = recommendation_evidence_hygiene_report(conn, limit=args.limit)
        finally:
            conn.close()
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["out_path"] = str(args.out)
        if args.markdown_out:
            write_recommendation_evidence_hygiene_markdown(report, args.markdown_out)
            report["markdown_path"] = str(args.markdown_out)
        print_json(report)
    elif args.command == "api-quality-hygiene":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            if args.apply:
                report = apply_api_quality_hygiene(
                    conn,
                    limit=args.limit,
                    max_updates=args.max_updates,
                )
            else:
                report = api_quality_hygiene_report(conn, limit=args.limit)
        finally:
            conn.close()
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            report["out_path"] = str(args.out)
        if args.markdown_out:
            write_api_quality_hygiene_markdown(report, args.markdown_out)
            report["markdown_path"] = str(args.markdown_out)
        print_json(report)
    elif args.command == "retry-qualification-errors":
        settings = load_settings()
        if not settings.qualification_service_key:
            raise SystemExit(
                "NCS_QUALIFICATION_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before retrying ncsClCdJm errors."
            )
        result = retry_qualification_error_units(
            settings.db_path,
            settings.qualification_service_key,
            major_code=args.major_code,
            limit_units=args.limit_units,
            page_no=args.page_no,
            num_of_rows=args.num_of_rows,
            max_pages=args.max_pages,
            timeout=args.timeout,
            request_delay=args.request_delay,
            max_retries=args.max_retries,
            retry_backoff_seconds=args.retry_backoff_seconds,
            retry_ready_only=not args.include_not_due,
            stop_after_rate_limit_errors=args.stop_after_rate_limit_errors,
        )
        report_path = Path(args.report_path)
        write_qualification_error_report(report_path, result.get("error_report") or {})
        result["report_path"] = str(report_path)
        print_json(result)
    elif args.command == "qualification-summary":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(qualification_summary(conn, limit=args.limit))
        finally:
            conn.close()
    elif args.command == "query-job-base":
        settings = load_settings()
        if not settings.job_base_service_key:
            raise SystemExit(
                "NCS_JOB_BASE_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before querying openapi19."
            )
        print_json(
            fetch_job_base_page(
                settings.job_base_service_key,
                major_code=args.major_code,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                timeout=args.timeout,
            )
        )
    elif args.command == "collect-job-base":
        settings = load_settings()
        if not settings.job_base_service_key:
            raise SystemExit(
                "NCS_JOB_BASE_SERVICE_KEY or NCS_SERVICE_KEY is required. "
                "Set it in .env before collecting openapi19."
            )
        print_json(
            collect_job_base_for_scope(
                db_path=settings.db_path,
                service_key=settings.job_base_service_key,
                major_code=args.major_code,
                all_majors=args.all_majors,
                module_name=args.module_name,
                page_no=args.page_no,
                num_of_rows=args.num_of_rows,
                max_pages=args.max_pages,
                timeout=args.timeout,
            )
        )
    elif args.command == "job-base-summary":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(job_base_summary(conn, limit=args.limit))
        finally:
            conn.close()
    elif args.command == "quality-gates":
        settings = load_settings()
        result = evaluate_quality_gates(
            settings.db_path,
            include_transition_evaluation=args.include_transition_eval,
            transition_limit=args.transition_limit,
            transition_scenario_limit=args.transition_scenario_limit,
            out_path=args.out,
            markdown_path=args.markdown_out,
        )
        print_json(result)
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "preprocess-ncs-ontology":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            seeded = ensure_ontology_seeded(conn)
            relations = None
            if not args.no_relations:
                relations = ensure_ncs_ontology_relations(
                    conn,
                    relations_per_concept=args.relations_per_concept,
                    reset=args.reset_relations,
                )
            atomic = None
            if args.atomic_ksa:
                atomic = preprocess_ksa_atomic_items(conn, reset=args.reset_atomic_ksa)
            task_relations = None
            if args.task_ksa_relations:
                task_relations = build_task_ksa_concept_relations(
                    conn,
                    reset=args.reset_task_ksa_relations,
                )
            task_similarity = None
            if args.task_similarity:
                task_similarity = build_task_similarity_links(
                    conn,
                    max_links_per_task=args.max_links_per_task,
                    min_shared_concepts=args.min_shared_concepts,
                    max_concept_task_frequency=args.max_concept_task_frequency,
                    reset=args.reset_task_similarity,
                )
            ksa_meanings = None
            if args.ksa_meanings:
                ksa_meanings = build_ksa_meaning_candidates(
                    conn,
                    major_code=args.ksa_meaning_major_code,
                    reset=args.reset_ksa_meanings,
                    limit=args.ksa_meaning_limit,
                    apply_to_definitions=args.apply_ksa_meaning_definitions,
                )
            training_course_links = None
            if args.training_course_links:
                training_course_links = build_training_course_ontology_links(
                    conn,
                    major_code=args.training_course_major_code,
                    reset=args.reset_training_course_links,
                )
            cur = conn.cursor()
            validation = {
                "non_empty_ksa_items": int(cur.execute(
                    "SELECT COUNT(*) FROM ksa_items WHERE TRIM(COALESCE(ksa_text_raw, '')) <> ''"
                ).fetchone()[0]),
                "unlinked_ksa_items": int(cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM ksa_items ki
                    LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
                    WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
                      AND kcl.link_id IS NULL
                    """
                ).fetchone()[0]),
                "performance_criteria": int(cur.execute("SELECT COUNT(*) FROM performance_criteria").fetchone()[0]),
                "criteria_without_concept_links": int(cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM performance_criteria pc
                    LEFT JOIN criteria_concept_links ccl ON ccl.criteria_id = pc.criteria_id
                    WHERE ccl.link_id IS NULL
                    """
                ).fetchone()[0]),
                "ontology_concepts": int(cur.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
                "candidate_definitions": int(cur.execute(
                    "SELECT COUNT(*) FROM ontology_concepts WHERE definition_status = 'candidate'"
                ).fetchone()[0]),
                "ontology_concept_relations": int(cur.execute("SELECT COUNT(*) FROM ontology_concept_relations").fetchone()[0]),
                "ksa_atomic_items": int(cur.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0]),
                "unprocessed_ksa_for_atomic": int(cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM ksa_items ki
                    LEFT JOIN ksa_atomic_items atom ON atom.ksa_id = ki.ksa_id
                    WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
                      AND atom.atomic_id IS NULL
                    """
                ).fetchone()[0]),
                "atomic_items_without_concept": int(cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM ksa_atomic_items atom
                    LEFT JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                    WHERE link.link_id IS NULL
                    """
                ).fetchone()[0]),
                "task_ksa_concept_relations": int(cur.execute("SELECT COUNT(*) FROM task_ksa_concept_relations").fetchone()[0]),
                "task_similarity_links": int(cur.execute("SELECT COUNT(*) FROM task_similarity_links").fetchone()[0]),
                "ksa_meaning_candidates": int(cur.execute("SELECT COUNT(*) FROM ksa_meaning_candidates").fetchone()[0]),
                "training_course_concept_links": int(cur.execute("SELECT COUNT(*) FROM ncs_training_course_concept_links").fetchone()[0]),
                "training_course_element_links": int(cur.execute("SELECT COUNT(*) FROM ncs_training_course_element_links").fetchone()[0]),
                "training_goal_concept_links": int(cur.execute("SELECT COUNT(*) FROM training_goal_concept_links").fetchone()[0]),
                "training_delivery_relations": int(cur.execute("SELECT COUNT(*) FROM training_delivery_relations").fetchone()[0]),
                "task_similarity_source_tasks": int(cur.execute(
                    "SELECT COUNT(DISTINCT source_criteria_id) FROM task_similarity_links"
                ).fetchone()[0]),
            }
            validation["ok"] = (
                validation["unlinked_ksa_items"] == 0
                and validation["criteria_without_concept_links"] == 0
                and (not args.atomic_ksa or validation["unprocessed_ksa_for_atomic"] == 0)
                and (not args.atomic_ksa or validation["atomic_items_without_concept"] == 0)
                and (not args.task_ksa_relations or validation["task_ksa_concept_relations"] > 0)
                and (not args.task_similarity or validation["task_similarity_links"] > 0)
            )
            print_json({
                "seeded": seeded,
                "relations": relations,
                "atomic_ksa": atomic,
                "task_ksa_relations": task_relations,
                "task_similarity": task_similarity,
                "ksa_meanings": ksa_meanings,
                "training_course_links": training_course_links,
                "validation": validation,
                "note": (
                    "Raw KSA text is preserved. KSA meaning preprocessing can apply task-context "
                    "candidate definitions with definition_status='candidate'."
                ),
            })
        finally:
            conn.close()
    elif args.command == "recommend-task-transitions":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                recommend_task_transitions(
                    conn,
                    criteria_id=args.criteria_id,
                    query=args.query,
                    unit_code=args.unit_code,
                    mode=args.mode,
                    limit=args.limit,
                    evidence_limit=args.evidence_limit,
                )
            )
        finally:
            conn.close()
    elif args.command == "recommend-training-for-task":
        if not has_task_locator(
            criteria_id=args.criteria_id,
            query=args.query,
            unit_code=args.unit_code,
        ):
            print_json(task_locator_error_payload())
            raise SystemExit(1)
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            result = recommend_training_for_task(
                conn,
                criteria_id=args.criteria_id,
                query=args.query,
                unit_code=args.unit_code,
                major_code=args.major_code,
                middle_code=args.middle_code,
                small_code=args.small_code,
                sub_code=args.sub_code,
                mode=args.mode,
                preferred_max_hours=args.preferred_max_hours,
                preferred_methods=args.preferred_method,
                limit=args.limit,
                save=not args.no_save,
            )
            if args.compact:
                result = compact_training_task_response(result, recommendation_limit=args.limit)
            print_json(result)
        finally:
            conn.close()
    elif args.command == "recommend-training-transition":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            result = recommend_training_transition(
                conn,
                current_query=args.current_query,
                target_query=args.target_query,
                major_code=args.major_code,
                current_major_code=args.current_major_code,
                target_major_code=args.target_major_code,
                current_middle_code=args.current_middle_code,
                target_middle_code=args.target_middle_code,
                current_small_code=args.current_small_code,
                target_small_code=args.target_small_code,
                current_sub_code=args.current_sub_code,
                target_sub_code=args.target_sub_code,
                mode=args.mode,
                preferred_max_hours=args.preferred_max_hours,
                preferred_methods=args.preferred_method,
                limit=args.limit,
                save=not args.no_save,
            )
            if args.compact:
                result = compact_training_transition_response(result, recommendation_limit=args.limit)
            print_json(result)
        finally:
            conn.close()
    elif args.command == "resolve-ncs-query-scope":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                resolve_ncs_query_scope(
                    conn,
                    args.query,
                    major_code=args.major_code,
                    middle_code=args.middle_code,
                    small_code=args.small_code,
                    sub_code=args.sub_code,
                    limit=args.limit,
                )
            )
        finally:
            conn.close()
    elif args.command == "evaluate-training-transitions":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            try:
                review_statuses = transition_review_status_filter(
                    trusted_only=args.trusted_only,
                    review_statuses=args.review_status,
                )
            except ValueError as exc:
                raise SystemExit(str(exc)) from exc
            print_json(
                evaluate_training_transition_scenarios(
                    conn,
                    limit=args.limit,
                    scenario_limit=args.scenario_limit,
                    review_statuses=review_statuses,
                )
            )
        finally:
            conn.close()
    elif args.command == "generate-training-transition-eval-set":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            generation = generate_training_transition_eval_scenarios(
                conn,
                target_non_hr_count=args.target_non_hr_count,
                per_major_limit=args.per_major_limit,
                per_classification_limit=args.per_classification_limit,
                reset_auto=args.reset_auto,
            )
            evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=args.limit,
                scenario_limit=args.scenario_limit,
            )
            trusted_evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=args.limit,
                scenario_limit=args.scenario_limit,
                review_statuses=list(TRUSTED_TRANSITION_REVIEW_STATUSES),
            )
            candidate_evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=args.limit,
                scenario_limit=args.scenario_limit,
                review_statuses=list(CANDIDATE_TRANSITION_REVIEW_STATUSES),
            )
            report_path = Path(args.report_path)
            write_training_transition_evaluation_report(
                report_path,
                generation=generation,
                evaluations={
                    "all_non_rejected": evaluation,
                    "trusted_reviewed": trusted_evaluation,
                    "candidate_or_auto": candidate_evaluation,
                },
            )
            print_json(
                training_transition_eval_set_payload(
                    generation=generation,
                    evaluation=evaluation,
                    trusted_evaluation=trusted_evaluation,
                    candidate_evaluation=candidate_evaluation,
                    report_path=report_path,
                )
            )
        finally:
            conn.close()
    elif args.command == "review-training-transition-scenarios":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            source_statuses = transition_review_status_filter(
                review_statuses=args.source_review_status,
            ) or ["candidate"]
            result = review_training_transition_scenarios(
                conn,
                limit=args.limit,
                scenario_limit=args.scenario_limit,
                source_review_statuses=source_statuses,
                target_review_status=args.target_review_status,
                require_top1_expected_hit=not args.allow_top1_miss,
                min_precision_at_k=args.min_precision_at_k,
                min_expected_recall_at_k=args.min_expected_recall_at_k,
                apply=args.apply,
            )
            report_path = Path(args.report_path)
            write_training_transition_review_report(report_path, result)
            result["report_path"] = str(report_path)
            print_json(result)
        finally:
            conn.close()
    elif args.command == "prepare-hr-review-queue":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                prepare_hr_human_review_queue(
                    conn,
                    major_code=args.major_code,
                    middle_code=args.middle_code,
                    small_code=args.small_code,
                    concept_limit=args.concept_limit,
                    goal_link_limit=args.goal_link_limit,
                    dry_run=args.dry_run,
                )
            )
        finally:
            conn.close()
    elif args.command == "prepare-ontology-review-queue":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                prepare_ontology_human_review_queue(
                    conn,
                    major_code=args.major_code,
                    middle_code=args.middle_code,
                    small_code=args.small_code,
                    sub_code=args.sub_code,
                    concept_limit=args.concept_limit,
                    goal_link_limit=args.goal_link_limit,
                    relation_limit=args.relation_limit,
                    min_confidence=args.min_confidence,
                    dry_run=args.dry_run,
                )
            )
        finally:
            conn.close()
    elif args.command == "review-priority":
        settings = load_settings()
        result = review_priority_summary_from_db(
            settings.db_path,
            limit=args.limit,
            per_issue_type_limit=args.per_issue_type_limit,
            issue_types=parse_csv(args.issue_types),
        )
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["out_path"] = str(args.out)
        if args.markdown_out:
            write_review_priority_markdown(result, args.markdown_out)
            result["markdown_path"] = str(args.markdown_out)
        print_json(result)
    elif args.command == "export-review-seedpack":
        settings = load_settings()
        selection_command = (
            "export-review-seedpack "
            f"--limit {args.limit} --per-issue-type-limit {args.per_issue_type_limit} "
            f"--out {args.out}"
        )
        if args.issue_types:
            selection_command += f" --issue-types {args.issue_types}"
        if args.source_report_path:
            selection_command += f" --source-report-path {args.source_report_path}"
        result = export_review_seedpack_from_db(
            settings.db_path,
            out_path=args.out,
            limit=args.limit,
            per_issue_type_limit=args.per_issue_type_limit,
            issue_types=parse_csv(args.issue_types),
            source_report_path=args.source_report_path,
            selection_command=selection_command,
        )
        if args.markdown_out:
            write_review_seedpack_markdown(result, args.out, args.markdown_out)
            result["markdown_path"] = str(args.markdown_out)
        print_json(result)
    elif args.command == "export-transition-scenario-seedpack":
        settings = load_settings()
        try:
            review_statuses = transition_review_status_filter(
                trusted_only=False,
                review_statuses=args.review_status,
            )
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        selection_command = (
            "export-transition-scenario-seedpack "
            f"--scenario-limit {args.scenario_limit} "
            f"--recommendation-limit {args.recommendation_limit} --out {args.out}"
        )
        if args.review_status:
            selection_command += f" --review-status {','.join(args.review_status)}"
        if args.source_report_path:
            selection_command += f" --source-report-path {args.source_report_path}"
        result = export_transition_scenario_seedpack_from_db(
            settings.db_path,
            out_path=args.out,
            review_statuses=review_statuses,
            scenario_limit=args.scenario_limit,
            recommendation_limit=args.recommendation_limit,
            source_report_path=args.source_report_path,
            selection_command=selection_command,
        )
        if args.markdown_out:
            write_transition_scenario_seedpack_markdown(result, args.out, args.markdown_out)
            result["markdown_path"] = str(args.markdown_out)
        print_json(result)
    elif args.command == "review-triage":
        try:
            result = build_review_triage_from_files(
                quality_report_path=args.quality_report,
                review_priority_path=args.review_priority_report,
                transition_seedpack_path=args.transition_seedpack,
                review_item_limit=args.review_item_limit,
                transition_item_limit=args.transition_item_limit,
            )
        except ValueError as exc:
            print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "invalid_review_triage_input",
                        "message": str(exc),
                    },
                }
            )
            raise SystemExit(1)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            result["out_path"] = str(args.out)
        if args.markdown_out:
            write_review_triage_markdown(result, args.markdown_out)
            result["markdown_path"] = str(args.markdown_out)
        print_json(result)
    elif args.command == "import-career-paths":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(
                import_career_paths_csv(
                    conn,
                    args.csv_path,
                    encoding=args.encoding,
                    reset=args.reset,
                    limit=args.limit,
                )
            )
        finally:
            conn.close()
    elif args.command == "career-path-summary":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(career_path_summary(conn, limit=args.limit))
        finally:
            conn.close()
    elif args.command == "import-supplemental-ncs-data":
        if not (args.unit_standard_csv or args.occupation_mapping_csv or args.training_zip_csv):
            print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "missing_input_csv",
                        "message": "Provide at least one supplemental CSV path.",
                    },
                }
            )
            raise SystemExit(1)
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            result: dict[str, Any] = {"ok": True, "imports": {}}
            if args.unit_standard_csv:
                result["imports"]["unit_standard_training"] = import_unit_standard_training_csv(
                    conn,
                    args.unit_standard_csv,
                    encoding=args.unit_standard_encoding,
                    reset=args.reset,
                    limit=args.limit,
                )
            if args.occupation_mapping_csv:
                result["imports"]["occupation_code_mapping"] = import_occupation_code_mapping_csv(
                    conn,
                    args.occupation_mapping_csv,
                    encoding=args.occupation_mapping_encoding,
                    reset=args.reset,
                    limit=args.limit,
                )
            if args.training_zip_csv:
                result["imports"]["external_training_zip_courses"] = import_external_training_zip_csv(
                    conn,
                    args.training_zip_csv,
                    encoding=args.training_zip_encoding,
                    reset=args.reset,
                    limit=args.limit,
                )
            result["summary"] = supplemental_data_summary(conn, limit=20)
            failed_imports = {
                name: payload
                for name, payload in result["imports"].items()
                if isinstance(payload, dict) and payload.get("ok") is False
            }
            if failed_imports:
                result["ok"] = False
                result["error"] = {
                    "code": "supplemental_import_failed",
                    "failed_imports": failed_imports,
                }
            print_json(result)
            if failed_imports:
                raise SystemExit(1)
        finally:
            conn.close()
    elif args.command == "supplemental-data-summary":
        settings = load_settings()
        conn = connect(settings.db_path)
        initialize_database(conn)
        try:
            print_json(supplemental_data_summary(conn, limit=args.limit))
        finally:
            conn.close()
    elif args.command == "export-package":
        settings = load_settings()
        print_json(
            export_handoff_package(
                settings.db_path,
                Path(args.out),
                db_mode=args.db_mode,
                zip_output=args.zip,
            )
        )
    elif args.command == "collect-sqf-library":
        settings = load_settings()
        print_json(
            collect_sqf_library(
                settings.db_path,
                raw_dir=args.raw_dir,
                start_page=args.start_page,
                end_page=args.end_page,
                download=args.download,
                timeout=args.timeout,
                overwrite=args.overwrite,
                delay=args.delay,
            )
        )
    elif args.command == "import-ontology-source":
        settings = load_settings()
        print_json(
            register_local_ontology_source(
                settings.db_path,
                args.input,
                raw_dir=args.raw_dir,
                title=args.title,
                ontology_role=args.role,
                source_url=args.source_url,
                notes=args.notes,
            )
        )
    elif args.command == "build-sqf-sqlite-model":
        settings = load_settings()
        print_json(sqf_model_summary(settings.db_path) if args.summary else build_sqf_sqlite_model(settings.db_path))
    elif args.command == "preprocess-sqf-documents":
        settings = load_settings()
        print_json(
            preprocess_sqf_documents(
                settings.db_path,
                extracted_dir=args.extracted_dir,
                chunk_chars=args.chunk_chars,
                overlap_chars=args.overlap_chars,
                limit=args.limit,
                ocr_empty=args.ocr_empty,
                ocr_lang=args.ocr_lang,
                ocr_dpi=args.ocr_dpi,
                ocr_max_pages=args.ocr_max_pages,
                only_unprocessed=args.only_unprocessed,
            )
        )
    elif args.command == "build-sqf-precision-matches":
        settings = load_settings()
        print_json(
            build_sqf_chunk_job_level_matches(
                settings.db_path,
                min_score=args.min_score,
                max_matches_per_chunk=args.max_matches_per_chunk,
                limit_chunks=args.limit_chunks,
                asset_id=args.asset_id,
                reset=not args.no_reset,
                include_framework_references=args.include_framework_references,
            )
        )
    elif args.command == "evaluate":
        settings = load_settings()
        print_json(run_evaluation(settings.db_path, scope_tag=args.scope_tag, run_name=args.run_name))
    elif args.command == "ontology":
        settings = load_settings()
        if args.action == "validate":
            print_json(validate_ontology_readiness(settings.db_path))
        else:
            print_json(
                export_ontology_jsonld(
                    settings.db_path,
                    args.out,
                    include_excluded_mappings=args.include_excluded_mappings,
                    include_chunk_evidence=not args.no_chunk_evidence,
                    chunk_evidence_limit=args.chunk_evidence_limit,
                    include_document_chunks=not args.no_document_chunks,
                    document_chunk_limit=args.document_chunk_limit,
                )
            )
    elif args.command == "refine":
        settings = load_settings()
        print_json(
            run_refinement_harness(
                settings.db_path,
                action=args.action,
                issue_types=parse_csv(args.issue_types),
                target_types=parse_csv(args.target_types),
                severity=args.severity,
                provider=args.provider,
                limit=args.limit,
                min_confidence=args.min_confidence,
                dry_run=args.dry_run,
                out_path=args.out,
                input_path=args.input,
            )
        )
    elif args.command == "lint":
        result = lint_repo(strict=args.strict)
        print_json(result)
        if not result["ok"]:
            raise SystemExit(1)
    elif args.command == "pipeline":
        print_json(run_pipeline(args))


if __name__ == "__main__":
    main()
