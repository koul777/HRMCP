from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    clamp_limit,
    now_utc,
    row_to_dict,
    rows_to_dicts,
    prepare_ontology_human_review_queue as db_prepare_ontology_human_review_queue,
)
from ncs_mcp.job_base_api import (
    collect_job_base_competencies as job_base_collect_competencies,
)
from ncs_mcp.ontology import MVP_JOB_NAME, MVP_MAJOR_CODE
from ncs_mcp.ontology import (
    build_sqf_mapping_candidates as ontology_build_sqf_mapping_candidates,
    generate_mapping_candidates,
    get_filtered_matches,
    get_sqf_duty,
    query_sqf_duties,
    sqf_summary,
)
from ncs_mcp.mapping_policy import REVIEWED_STATUSES, apply_mapping_filter
from ncs_mcp.ncs_reference import (
    build_ncs_derived_learning_plans as ncs_ref_build_derived_plans,
    build_report_training_courses as ncs_ref_build_report_training_courses,
    extract_ncs_reference_entities as ncs_ref_extract_entities,
    import_ncs_reference_docx as ncs_ref_import_docx,
    import_ncs_reference_html as ncs_ref_import_html,
    link_reference_entities_to_ncs as ncs_ref_link_entities,
    recommend_education_by_concepts as ncs_ref_recommend_by_concepts,
    review_exact_learning_module_name_links as ncs_ref_review_exact_module_links,
)
from ncs_mcp.server_legacy_facade import (
    analyze_sqf_gap_payload as legacy_analyze_sqf_gap_payload,
    compare_raw_refined_payload as legacy_compare_raw_refined_payload,
    explain_education_recommendation_payload as legacy_explain_education_recommendation_payload,
    explain_mapping_payload as legacy_explain_mapping_payload,
    get_api_join_status_payload as legacy_get_api_join_status_payload,
    get_learning_module_payload as legacy_get_learning_module_payload,
    get_learning_path_for_sqf_job_payload as legacy_get_learning_path_for_sqf_job_payload,
    get_sqf_duties_payload as legacy_get_sqf_duties_payload,
    get_sqf_job_level_payload as legacy_get_sqf_job_level_payload,
    get_sqf_ontology_job_level_payload as legacy_get_sqf_ontology_job_level_payload,
    get_sqf_ontology_summary_payload as legacy_get_sqf_ontology_summary_payload,
    recommend_next_ncs_units_payload as legacy_recommend_next_ncs_units_payload,
    search_learning_modules_payload as legacy_search_learning_modules_payload,
    search_sqf_document_chunks_payload as legacy_search_sqf_document_chunks_payload,
    search_sqf_jobs_payload as legacy_search_sqf_jobs_payload,
    search_sqf_precision_matches_payload as legacy_search_sqf_precision_matches_payload,
)
from ncs_mcp.qualification_api import (
    collect_qualification_links as qualification_collect_links,
)
from ncs_mcp.recommendation import recommend_education_for_duty as recommendation_recommend_education
from ncs_mcp.review_safety import (
    REVIEW_PACKET_EXTENSIONS,
    normalize_source_decision_packet_ref,
    resolve_repo_reports_artifact,
    review_packet_sha256 as shared_review_packet_sha256,
)

TRUSTED_REVIEW_STATUSES = {"human_reviewed", "reviewed", "accepted"}
AUTOMATED_REVIEWER_IDS = {"", "automated_eval_gate", "automation", "mcp", "system"}
TRUSTED_REVIEW_PACKET_EXTENSIONS = REVIEW_PACKET_EXTENSIONS


def review_packet_sha256(path: Path) -> str:
    return shared_review_packet_sha256(path)


def resolve_review_packet_artifact(source_decision_packet: str | None) -> Path | None:
    return resolve_repo_reports_artifact(
        source_decision_packet,
        extensions=TRUSTED_REVIEW_PACKET_EXTENSIONS,
    )


def trusted_review_provenance_blockers(
    *,
    review_status: str,
    reviewer_id: str | None,
    source_decision_packet: str | None,
    source_artifact_hash: str | None,
    rationale: str | None,
) -> list[str]:
    if review_status not in TRUSTED_REVIEW_STATUSES:
        return []
    blockers: list[str] = []
    if (reviewer_id or "").strip().lower() in AUTOMATED_REVIEWER_IDS:
        blockers.append("trusted_status_requires_explicit_human_reviewer_id")
    if not (source_decision_packet or "").strip():
        blockers.append("trusted_status_requires_source_decision_packet")
    packet_path = resolve_review_packet_artifact(source_decision_packet)
    if source_decision_packet and packet_path is None:
        blockers.append("trusted_status_requires_packet_backed_source_decision_packet")
    if not (source_artifact_hash or "").strip():
        blockers.append("trusted_status_requires_source_artifact_hash")
    elif not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(source_artifact_hash)):
        blockers.append("trusted_status_requires_sha256_source_artifact_hash")
    elif packet_path is not None:
        expected_hash = "sha256:" + review_packet_sha256(packet_path)
        if str(source_artifact_hash).lower() != expected_hash:
            blockers.append("trusted_status_requires_matching_source_artifact_hash")
    if not (rationale or "").strip():
        blockers.append("trusted_status_requires_rationale")
    return blockers


def build_read_only_legacy_handlers(
    *,
    open_db: Callable[[], Any],
    quality_for: Callable[[Any, str, str | int], list[dict[str, Any]]],
    tool_response: Callable[..., dict[str, Any]],
    error_response: Callable[..., dict[str, Any]],
    now_utc: Callable[[], str],
    db_path_getter: Callable[[], Any],
) -> SimpleNamespace:
    """Build read-only legacy handlers without importing the MCP server module."""

    def compare_raw_refined(target_type: str, target_id: str) -> dict[str, Any]:
        """Compare raw and refined text for criteria or KSA targets."""
        return legacy_compare_raw_refined_payload(
            open_db,
            quality_for,
            target_type=target_type,
            target_id=target_id,
        )

    def get_api_join_status(
        unit_code: str | None = None,
        classification_filter: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get API join status for competency units."""
        return legacy_get_api_join_status_payload(
            open_db,
            unit_code=unit_code,
            classification_filter=classification_filter,
            limit=limit,
        )

    def get_sqf_duties(
        major_code: str | None = None,
        keyword: str | None = None,
        duty_level: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Get SQF duty profiles collected from /openapi26."""
        return legacy_get_sqf_duties_payload(
            open_db,
            major_code=major_code,
            keyword=keyword,
            duty_level=duty_level,
            limit=limit,
        )

    def search_sqf_jobs(
        keyword: str | None = None,
        major_code: str | None = MVP_MAJOR_CODE,
        mvp_only: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search SQF jobs by keyword, grouped by industry field and job."""
        payload = legacy_search_sqf_jobs_payload(
            open_db,
            keyword=keyword,
            major_code=major_code,
            mvp_only=mvp_only,
            limit=limit,
        )
        return tool_response(payload)

    def get_sqf_job_level(
        source_key: str | None = None,
        job_name: str = MVP_JOB_NAME,
        duty_name: str | None = None,
        duty_level: str | None = None,
        include_mappings: bool = True,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Get an SQF job/duty level with direct SQF evidence and NCS mapping candidates."""
        payload = legacy_get_sqf_job_level_payload(
            open_db,
            source_key=source_key,
            job_name=job_name,
            duty_name=duty_name,
            duty_level=duty_level,
            include_mappings=include_mappings,
            limit=limit,
        )
        return tool_response(payload)

    def analyze_gap(
        current_ncs_unit_codes: list[str],
        target_source_key: str | None = None,
        target_job_name: str = MVP_JOB_NAME,
        target_duty_name: str | None = None,
        target_level: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Analyze missing NCS units for a target SQF duty level."""
        result = legacy_analyze_sqf_gap_payload(
            open_db,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            limit=limit,
        )
        return tool_response(result)

    def recommend_next_ncs_units(
        current_ncs_unit_codes: list[str],
        target_source_key: str | None = None,
        target_job_name: str = MVP_JOB_NAME,
        target_duty_name: str | None = None,
        target_level: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Recommend next NCS units to close the gap toward an SQF duty level."""
        result = legacy_recommend_next_ncs_units_payload(
            open_db,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            limit=limit,
        )
        return tool_response(result)

    def explain_mapping(sqf_source_key: str, ncs_unit_code: str) -> dict[str, Any]:
        """Explain why one SQF duty level maps to one NCS competency unit."""
        result = legacy_explain_mapping_payload(
            open_db,
            sqf_source_key=sqf_source_key,
            ncs_unit_code=ncs_unit_code,
        )
        return tool_response(result)

    def search_learning_modules(
        query: str | None = None,
        major_code: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search cached NCS learning modules collected from openapi21."""
        payload = legacy_search_learning_modules_payload(
            open_db,
            query=query,
            major_code=major_code,
            limit=limit,
        )
        return tool_response(payload)

    def get_learning_module(learn_module_seq: str) -> dict[str, Any]:
        """Return one cached NCS learning module with unit and ontology concept links."""
        payload = legacy_get_learning_module_payload(open_db, learn_module_seq=learn_module_seq)
        return tool_response(payload)

    def get_learning_path_for_sqf_job(
        query: str,
        major_code: str | None = None,
        target_source_key: str | None = None,
        target_level: str | None = None,
        current_concepts: list[str] | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        """Build a staged learning path for SQF job levels using trusted mappings and modules."""
        result = legacy_get_learning_path_for_sqf_job_payload(
            open_db,
            query=query,
            major_code=major_code,
            target_source_key=target_source_key,
            target_level=target_level,
            current_concepts=current_concepts,
            limit=limit,
        )
        return tool_response(result)

    def explain_education_recommendation(
        recommendation_item_id: int | None = None,
        recommendation_run_id: int | None = None,
        rank: int | None = None,
    ) -> dict[str, Any]:
        """Explain a saved education recommendation item from its audit evidence chain."""
        result = legacy_explain_education_recommendation_payload(
            open_db,
            recommendation_item_id=recommendation_item_id,
            recommendation_run_id=recommendation_run_id,
            rank=rank,
        )
        return tool_response(result)

    def get_sqf_ontology_summary() -> dict[str, Any]:
        """Return counts for the normalized SQF ontology and preprocessed document layer."""
        return tool_response(legacy_get_sqf_ontology_summary_payload(db_path_getter()))

    def search_sqf_document_chunks(
        query: str,
        ontology_tag: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search preprocessed SQF report chunks extracted from PDF/ZIP sources."""
        payload, audit = legacy_search_sqf_document_chunks_payload(
            open_db,
            query=query,
            ontology_tag=ontology_tag,
            limit=limit,
        )
        return tool_response(payload, audit={**audit, "generated_at": now_utc()})

    def search_sqf_precision_matches(
        query: str | None = None,
        source_key: str | None = None,
        min_score: float = 9.0,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Search candidate evidence matches between SQF report chunks and SQF job levels."""
        payload, audit = legacy_search_sqf_precision_matches_payload(
            open_db,
            query=query,
            source_key=source_key,
            min_score=min_score,
            limit=limit,
        )
        return tool_response(payload, audit={**audit, "generated_at": now_utc()})

    def get_sqf_ontology_job_level(source_key: str) -> dict[str, Any]:
        """Return normalized SQF job-level ontology node, recognition evidence, and document links."""
        payload = legacy_get_sqf_ontology_job_level_payload(open_db, source_key=source_key)
        if payload is None:
            return error_response("sqf_job_level_not_found", source_key=source_key)
        return tool_response(payload)

    return SimpleNamespace(
        compare_raw_refined=compare_raw_refined,
        get_api_join_status=get_api_join_status,
        get_sqf_duties=get_sqf_duties,
        search_sqf_jobs=search_sqf_jobs,
        get_sqf_job_level=get_sqf_job_level,
        analyze_gap=analyze_gap,
        recommend_next_ncs_units=recommend_next_ncs_units,
        explain_mapping=explain_mapping,
        search_learning_modules=search_learning_modules,
        get_learning_module=get_learning_module,
        get_learning_path_for_sqf_job=get_learning_path_for_sqf_job,
        explain_education_recommendation=explain_education_recommendation,
        get_sqf_ontology_summary=get_sqf_ontology_summary,
        search_sqf_document_chunks=search_sqf_document_chunks,
        search_sqf_precision_matches=search_sqf_precision_matches,
        get_sqf_ontology_job_level=get_sqf_ontology_job_level,
    )


def build_legacy_operation_handlers(
    *,
    open_db: Callable[[], Any],
    tool_response: Callable[..., dict[str, Any]],
    error_response: Callable[..., dict[str, Any]],
    now_utc: Callable[[], str],
    db_path_getter: Callable[[], Any],
) -> SimpleNamespace:
    """Build legacy operational handlers without importing the MCP server module."""

    def build_sqf_ncs_mapping_candidates(
        mvp_only: bool = True,
        major_code: str | None = None,
        keyword: str | None = None,
        source_key: str | None = None,
        limit_per_duty: int = 10,
    ) -> dict[str, Any]:
        with open_db() as conn:
            return ontology_build_sqf_mapping_candidates(
                conn,
                mvp_only=mvp_only,
                major_code=major_code,
                keyword=keyword,
                source_key=source_key,
                limit_per_duty=limit_per_duty,
            )

    def map_sqf_to_ncs(
        source_key: str,
        persist: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        with open_db() as conn:
            duty = get_sqf_duty(conn, source_key)
            if duty is None:
                return error_response("sqf_target_not_found", source_key=source_key)
            if persist:
                summary = ontology_build_sqf_mapping_candidates(
                    conn,
                    source_key=source_key,
                    mvp_only=False,
                    limit_per_duty=limit,
                )
                mapping_status, matches, metadata = get_filtered_matches(conn, duty, limit=limit)
                return tool_response(
                    {
                        "sqf_duty": sqf_summary(duty),
                        "mapping_status": mapping_status,
                        "build_summary": summary,
                        "ncs_matches": matches,
                        "metadata": {
                            "data_source": "SQLite NCS/SQF knowledge base",
                            "query_scope": "single_sqf_duty",
                            "used_refined_policy": "refined_if_approved",
                            **metadata,
                        },
                    }
                )
            candidates = generate_mapping_candidates(conn, duty, limit=max(limit, 50))
            filtered = apply_mapping_filter(candidates)
        return tool_response(
            {
                "sqf_duty": sqf_summary(duty),
                "mapping_status": "generated_candidate",
                "ncs_matches": filtered["matches"][: clamp_limit(limit, default=10, maximum=100)],
                "metadata": {
                    "data_source": "SQLite NCS/SQF knowledge base",
                    "query_scope": "single_sqf_duty",
                    "used_refined_policy": "refined_if_approved",
                    **filtered["metadata"],
                },
                "note": "Set persist=true to save candidates into sqf_ncs_matches for dashboard review.",
            }
        )

    def prepare_ontology_review_queue(
        major_code: str | None = None,
        middle_code: str | None = None,
        small_code: str | None = None,
        sub_code: str | None = None,
        concept_limit: int = 250,
        goal_link_limit: int = 250,
        relation_limit: int = 250,
        min_confidence: float = 0.75,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = db_prepare_ontology_human_review_queue(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                concept_limit=concept_limit,
                goal_link_limit=goal_link_limit,
                relation_limit=relation_limit,
                min_confidence=min_confidence,
                dry_run=dry_run,
            )
        return tool_response(
            result,
            audit={
                "data_sources": [
                    "ontology_concepts",
                    "training_goal_concept_links",
                    "task_ksa_concept_relations",
                    "quality_issues",
                ],
                "generated_at": now_utc(),
            },
        )

    def collect_qualification_items(
        unit_code: str | None = None,
        major_code: str | None = None,
        all_units: bool = False,
        limit_units: int | None = None,
        page_no: int = 1,
        num_of_rows: int = 50,
        max_pages: int | None = None,
        timeout: int = 30,
        refresh: bool = False,
        request_delay: float = 0.2,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> dict[str, Any]:
        settings = load_settings()
        if not settings.qualification_service_key:
            return error_response("qualification_service_key_missing")
        try:
            result = qualification_collect_links(
                settings.db_path,
                settings.qualification_service_key,
                unit_codes=[unit_code] if unit_code else None,
                major_code=major_code,
                all_units=all_units,
                limit_units=limit_units,
                page_no=page_no,
                num_of_rows=num_of_rows,
                max_pages=max_pages,
                timeout=timeout,
                resume=not refresh,
                request_delay=request_delay,
                max_retries=max_retries,
                retry_backoff_seconds=retry_backoff_seconds,
            )
        except ValueError as exc:
            return error_response("qualification_collection_scope_required", detail=str(exc))
        return tool_response(
            result,
            audit={
                "data_sources": [
                    "ncsClCdJm/getNcsClCdJmList",
                    "ncs_qualification_items",
                    "ncs_unit_qualification_links",
                    "ncs_qualification_collection_status",
                ],
                "generated_at": now_utc(),
            },
        )

    def collect_job_base_competencies(
        major_code: str = "02",
        module_name: str | None = None,
        page_no: int = 1,
        num_of_rows: int = 500,
        max_pages: int | None = None,
        timeout: int = 30,
    ) -> dict[str, Any]:
        settings = load_settings()
        if not settings.job_base_service_key:
            return error_response("job_base_service_key_missing")
        result = job_base_collect_competencies(
            settings.db_path,
            settings.job_base_service_key,
            major_code=major_code,
            module_name=module_name,
            page_no=page_no,
            num_of_rows=num_of_rows,
            max_pages=max_pages,
            timeout=timeout,
        )
        return tool_response(
            result,
            audit={
                "data_sources": [
                    "ncsJobBase/openapi19",
                    "ncs_job_base_competencies",
                    "ncs_job_base_factors",
                    "ncs_unit_job_base_links",
                ],
                "generated_at": now_utc(),
            },
        )

    def recommend_education_for_duty(
        query: str,
        major_code: str | None = None,
        target_source_key: str | None = None,
        target_level: str | None = None,
        current_concepts: list[str] | None = None,
        limit: int = 5,
        save: bool = True,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = recommendation_recommend_education(
                conn,
                query=query,
                major_code=major_code,
                target_source_key=target_source_key,
                target_level=target_level,
                current_concepts=current_concepts,
                limit=limit,
                save=save,
            )
        return tool_response(result)

    def import_ncs_reference_html(
        input_path: str,
        title: str,
        chunk_min_chars: int = 500,
        chunk_max_chars: int = 1200,
        extract_entities: bool = False,
        link_entities: bool = False,
        major_code: str | None = None,
        middle_code: str | None = None,
        small_code: str | None = None,
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_import_html(
                conn,
                input_path,
                title=title,
                chunk_min_chars=chunk_min_chars,
                chunk_max_chars=chunk_max_chars,
            )
            if extract_entities:
                result["entity_extraction"] = ncs_ref_extract_entities(
                    conn,
                    document_id=result["document_id"],
                    major_code=major_code,
                    middle_code=middle_code,
                    small_code=small_code,
                    sub_code=sub_code,
                    sub_codes=sub_codes,
                )
            if link_entities:
                result["entity_links"] = ncs_ref_link_entities(
                    conn,
                    document_id=result["document_id"],
                    major_code=major_code,
                    middle_code=middle_code,
                    small_code=small_code,
                    sub_code=sub_code,
                    sub_codes=sub_codes,
                )
        return tool_response(
            {"ok": True, **result},
            audit={
                "data_sources": [
                    "ncs_reference_documents",
                    "ncs_reference_pages",
                    "ncs_reference_chunks",
                ],
                "generated_at": now_utc(),
            },
        )

    def import_ncs_reference_docx(
        input_path: str,
        title: str,
        chunk_min_chars: int = 500,
        chunk_max_chars: int = 1200,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_import_docx(
                conn,
                input_path,
                title=title,
                chunk_min_chars=chunk_min_chars,
                chunk_max_chars=chunk_max_chars,
            )
        return tool_response(
            {"ok": True, **result},
            audit={
                "data_sources": [
                    "ncs_reference_documents",
                    "ncs_reference_pages",
                    "ncs_reference_chunks",
                ],
                "generated_at": now_utc(),
            },
        )

    def extract_ncs_reference_entities(
        document_id: int | None = None,
        limit_chunks: int | None = None,
        major_code: str | None = None,
        middle_code: str | None = None,
        small_code: str | None = None,
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_extract_entities(
                conn,
                document_id=document_id,
                limit_chunks=limit_chunks,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            )
        return tool_response(
            {"ok": True, **result},
            audit={
                "data_sources": ["ncs_reference_chunks", "ncs_reference_entities"],
                "generated_at": now_utc(),
            },
        )

    def link_reference_entities_to_ncs(
        document_id: int | None = None,
        major_code: str | None = None,
        middle_code: str | None = None,
        small_code: str | None = None,
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_link_entities(
                conn,
                document_id=document_id,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
            )
        return tool_response(
            {"ok": True, **result},
            audit={
                "data_sources": ["ncs_reference_entities", "ncs_reference_entity_links"],
                "generated_at": now_utc(),
            },
        )

    def recommend_learning_modules_by_ncs(
        query: str | None = None,
        unit_code: str | None = None,
        major_code: str | None = "02",
        middle_code: str | None = None,
        small_code: str | None = None,
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
        trust_mode: str = "trusted",
        limit: int = 5,
        save: bool = False,
    ) -> dict[str, Any]:
        result = legacy_recommend_learning_modules_by_ncs_payload(
            open_db,
            query=query,
            unit_code=unit_code,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
            trust_mode=trust_mode,
            limit=limit,
            save=save,
        )
        return tool_response(result)

    def review_exact_learning_module_name_links(
        major_code: str | None = "02",
        middle_code: str | None = "02",
        small_code: str | None = "02",
        sub_codes: list[str] | None = None,
        reviewer_id: str = "mcp",
        source_decision_packet: str | None = None,
        source_artifact_hash: str | None = None,
        rationale: str | None = None,
        evidence_refs: list[str] | None = None,
        run_artifact: str | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_review_exact_module_links(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_codes=sub_codes or ["01", "02"],
                reviewer_id=reviewer_id,
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                rationale=rationale,
                evidence_refs=evidence_refs,
                run_artifact=run_artifact,
            )
        return tool_response(result)

    def build_ncs_derived_learning_plans(
        major_code: str | None = "02",
        middle_code: str | None = "02",
        small_code: str | None = "02",
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
        review_status: str = "auto_linked",
        reviewer_id: str = "mcp",
        notes: str = "",
        source_decision_packet: str | None = None,
        source_artifact_hash: str | None = None,
        rationale: str | None = None,
        evidence_refs: list[str] | None = None,
        run_artifact: str | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_build_derived_plans(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes,
                review_status=review_status,
                reviewer_id=reviewer_id,
                notes=notes,
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                rationale=rationale,
                evidence_refs=evidence_refs,
                run_artifact=run_artifact,
            )
        return tool_response(result)

    def build_report_training_courses(
        document_id: int | None = None,
        major_code: str = "02",
        middle_code: str = "02",
        small_code: str = "02",
        sub_code: str | None = None,
        sub_codes: list[str] | None = None,
        review_status: str = "auto_linked",
        reviewer_id: str = "mcp",
        notes: str = "",
        source_decision_packet: str | None = None,
        source_artifact_hash: str | None = None,
        rationale: str | None = None,
        evidence_refs: list[str] | None = None,
        run_artifact: str | None = None,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_build_report_training_courses(
                conn,
                document_id=document_id,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                sub_codes=sub_codes or ["01", "02"],
                review_status=review_status,
                reviewer_id=reviewer_id,
                notes=notes,
                source_decision_packet=source_decision_packet,
                source_artifact_hash=source_artifact_hash,
                rationale=rationale,
                evidence_refs=evidence_refs,
                run_artifact=run_artifact,
            )
        return tool_response(result)

    def recommend_education_by_concepts(
        concepts: list[str] | None = None,
        query: str | None = None,
        trust_mode: str = "trusted",
        limit: int = 5,
        save: bool = True,
    ) -> dict[str, Any]:
        with open_db() as conn:
            result = ncs_ref_recommend_by_concepts(
                conn,
                concepts=concepts,
                query=query,
                trust_mode=trust_mode,
                limit=limit,
                save=save,
            )
        return tool_response(result)

    def review_sqf_ncs_match(
        match_id: int,
        new_status: str,
        reviewer_id: str = "mcp",
        notes: str = "",
        relation: str | None = None,
        source_decision_packet: str | None = None,
        source_artifact_hash: str | None = None,
        rationale: str | None = None,
        evidence_refs: list[str] | None = None,
        run_artifact: str | None = None,
    ) -> dict[str, Any]:
        status = new_status.strip()
        allowed_statuses = {
            "accepted",
            "reviewed",
            "human_reviewed",
            "rejected",
            "low_confidence",
            "low_score",
            "related-only",
            "candidate",
        }
        if status not in allowed_statuses:
            return error_response(
                "unsupported_review_status",
                new_status=new_status,
                allowed=sorted(allowed_statuses),
            )
        provenance_blockers = trusted_review_provenance_blockers(
            review_status=status,
            reviewer_id=reviewer_id,
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale,
        )
        if provenance_blockers:
            return error_response(
                "trusted_review_provenance_required",
                new_status=status,
                blockers=provenance_blockers,
            )
        with open_db() as conn:
            row = conn.execute("SELECT * FROM sqf_ncs_matches WHERE match_id = ?", (match_id,)).fetchone()
            if row is None:
                return error_response("sqf_ncs_match_not_found", match_id=match_id)
            final_relation = relation.strip() if relation else row["relation"]
            if status == "related-only":
                final_relation = "related"
            eligible = status in REVIEWED_STATUSES and final_relation != "related"
            if eligible:
                filter_status = "eligible"
                exclusion_reason = None
            elif status == "rejected":
                filter_status = "excluded"
                exclusion_reason = "rejected"
            elif status in {"low_confidence", "low_score"}:
                filter_status = "excluded"
                exclusion_reason = status
            elif final_relation == "related":
                filter_status = "excluded"
                exclusion_reason = "relation:related"
            else:
                filter_status = "review_required"
                exclusion_reason = None
            timestamp = now_utc()
            conn.execute(
                """
                UPDATE sqf_ncs_matches
                SET review_status = ?,
                    relation = ?,
                    filter_status = ?,
                    exclusion_reason = ?,
                    reviewer_id = ?,
                    reviewed_at = ?,
                    reviewer_notes = ?,
                    updated_at = ?
                WHERE match_id = ?
                """,
                (
                    status,
                    final_relation,
                    filter_status,
                    exclusion_reason,
                    reviewer_id,
                    timestamp,
                    notes,
                    timestamp,
                    match_id,
                ),
            )
            evidence_refs_json = json.dumps(evidence_refs or [], ensure_ascii=False)
            stored_source_decision_packet = normalize_source_decision_packet_ref(
                source_decision_packet,
                extensions=TRUSTED_REVIEW_PACKET_EXTENSIONS,
            )
            conn.execute(
                """
                INSERT INTO review_audit_log(
                    entity_type, entity_id, action, previous_status,
                    new_status, reviewer_id, notes, source_decision_packet,
                    source_artifact_hash, rationale, evidence_refs_json,
                    created_by_tool, run_artifact, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "sqf_ncs_match",
                    str(match_id),
                    "review_sqf_ncs_match",
                    row["review_status"],
                    status,
                    reviewer_id,
                    notes,
                    stored_source_decision_packet,
                    source_artifact_hash,
                    rationale or notes,
                    evidence_refs_json,
                    "ncs_mcp.server.review_sqf_ncs_match",
                    run_artifact,
                    now_utc(),
                ),
            )
            updated = conn.execute("SELECT * FROM sqf_ncs_matches WHERE match_id = ?", (match_id,)).fetchone()
            conn.commit()
        return tool_response(
            {
                "match_id": match_id,
                "previous_status": row["review_status"],
                "new_status": status,
                "recommendation_eligible": eligible,
                "mapping": row_to_dict(updated),
            },
            audit={
                "data_sources": ["sqf_ncs_matches", "review_audit_log"],
                "generated_at": now_utc(),
                "reviewer_id": reviewer_id,
            },
        )

    return SimpleNamespace(
        build_sqf_ncs_mapping_candidates=build_sqf_ncs_mapping_candidates,
        map_sqf_to_ncs=map_sqf_to_ncs,
        prepare_ontology_review_queue=prepare_ontology_review_queue,
        collect_qualification_items=collect_qualification_items,
        collect_job_base_competencies=collect_job_base_competencies,
        recommend_education_for_duty=recommend_education_for_duty,
        import_ncs_reference_html=import_ncs_reference_html,
        import_ncs_reference_docx=import_ncs_reference_docx,
        extract_ncs_reference_entities=extract_ncs_reference_entities,
        link_reference_entities_to_ncs=link_reference_entities_to_ncs,
        recommend_learning_modules_by_ncs=recommend_learning_modules_by_ncs,
        review_exact_learning_module_name_links=review_exact_learning_module_name_links,
        build_ncs_derived_learning_plans=build_ncs_derived_learning_plans,
        build_report_training_courses=build_report_training_courses,
        recommend_education_by_concepts=recommend_education_by_concepts,
        review_sqf_ncs_match=review_sqf_ncs_match,
    )
