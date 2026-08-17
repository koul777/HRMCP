from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ncs_mcp.db import clamp_limit, now_utc, row_to_dict, rows_to_dicts
from ncs_mcp.ontology import (
    MVP_JOB_NAME,
    MVP_MAJOR_CODE,
    analyze_sqf_gap,
    direct_sqf_conditions,
    explain_mapping,
    get_or_generate_matches,
    query_sqf_duties,
    recommend_next_ncs_units,
    search_sqf_jobs_summary,
    sqf_summary,
)
from ncs_mcp.ncs_reference import (
    recommend_learning_modules_by_ncs as ncs_reference_recommend_learning_modules,
    search_ncs_reference_chunks as ncs_reference_search_chunks,
)
from ncs_mcp.recommendation import (
    explain_education_recommendation as recommendation_explain_education,
    get_learning_module as recommendation_get_learning_module,
    get_learning_path_for_sqf_job as recommendation_get_learning_path,
    search_learning_modules as recommendation_search_learning_modules,
)
from ncs_mcp.sqf_sqlite import sqf_model_summary


OpenDb = Callable[[], Any]
QualityFor = Callable[[Any, str, str | int], list[dict[str, Any]]]
INTERNAL_PAYLOAD_FIELDS = {
    "source_payload",
    "raw_payload",
    "raw_response",
    "source_json",
}


def _exact_filter(clauses: list[str], params: list[Any], column: str, value: str | None) -> None:
    if value:
        clauses.append(f"{column} = ?")
        params.append(value)


def _strip_internal_payload_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_internal_payload_fields(item)
            for key, item in value.items()
            if key not in INTERNAL_PAYLOAD_FIELDS
        }
    if isinstance(value, list):
        return [_strip_internal_payload_fields(item) for item in value]
    return value


def get_sqf_ontology_summary_payload(db_path: Path) -> dict[str, Any]:
    """Return counts for the normalized SQF ontology and document layer."""
    return sqf_model_summary(db_path)


def compare_raw_refined_payload(
    open_db: OpenDb,
    quality_for: QualityFor,
    *,
    target_type: str,
    target_id: str,
) -> dict[str, Any]:
    """Compare raw and refined criteria/KSA text with associated quality issues."""
    with open_db() as conn:
        if target_type == "criteria":
            row = conn.execute(
                """
                SELECT criteria_id AS id, criteria_text_raw AS raw_text,
                       criteria_text_refined AS refined_text, review_status
                FROM performance_criteria
                WHERE criteria_id = ?
                """,
                (target_id,),
            ).fetchone()
        elif target_type == "ksa":
            row = conn.execute(
                """
                SELECT ksa_id AS id, ksa_text_raw AS raw_text,
                       ksa_text_refined AS refined_text, review_status
                FROM ksa_items
                WHERE ksa_id = ?
                """,
                (target_id,),
            ).fetchone()
        else:
            return {"error": "unsupported_target_type", "supported": ["criteria", "ksa"]}
        if row is None:
            return {"error": "not_found", "target_type": target_type, "target_id": target_id}
        return {
            "target_type": target_type,
            "target_id": target_id,
            "comparison": row_to_dict(row),
            "quality_issues": quality_for(conn, target_type, target_id),
        }


def get_api_join_status_payload(
    open_db: OpenDb,
    *,
    unit_code: str | None = None,
    classification_filter: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return cached NCS API join status for competency units."""
    clauses: list[str] = []
    params: list[Any] = []
    if unit_code:
        clauses.append("cu.unit_code = ?")
        params.append(unit_code)
    if classification_filter:
        clauses.append(
            "(c.major_name LIKE ? OR c.middle_name LIKE ? OR c.small_name LIKE ? OR c.sub_name LIKE ?)"
        )
        params.extend([f"%{classification_filter}%"] * 4)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            cu.unit_code,
            cu.unit_name_raw,
            cu.unit_level_raw,
            cu.api_unit_name,
            cu.api_unit_level,
            cu.api_definition,
            cu.api_match_status,
            c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY cu.api_match_status, cu.unit_code
        LIMIT ?
    """
    params.append(clamp_limit(limit))
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"api_join_status": rows_to_dicts(rows)}


def get_sqf_duties_payload(
    open_db: OpenDb,
    *,
    major_code: str | None = None,
    keyword: str | None = None,
    duty_level: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Return legacy SQF duty profiles collected from /openapi26."""
    clauses: list[str] = []
    params: list[Any] = []
    _exact_filter(clauses, params, "sd.ncs_lclas_cd", major_code)
    _exact_filter(clauses, params, "sd.duty_level", duty_level)
    if keyword:
        clauses.append(
            """
            (
                sd.sqf_field_name LIKE ?
                OR sd.job_name LIKE ?
                OR sd.duty_name LIKE ?
                OR sd.duty_definition LIKE ?
                OR sd.duty_education_training LIKE ?
                OR sd.duty_qualification LIKE ?
            )
            """
        )
        params.extend([f"%{keyword}%"] * 6)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            sd.source_key, sd.ncs_lclas_cd, sd.ncs_lclas_name,
            sd.sqf_field_name, sd.sqf_sub_field_name, sd.job_name,
            sd.duty_name, sd.duty_level, sd.duty_level_name,
            sd.duty_level_definition, sd.duty_definition,
            sd.autonomy_responsibility, sd.duty_acarr,
            sd.duty_education_training, sd.duty_qualification,
            sd.duty_career, sd.duty_license, sd.duty_remark,
            COUNT(cu.unit_code) AS ncs_unit_count
        FROM sqf_duties sd
        LEFT JOIN classifications c ON c.major_code = sd.ncs_lclas_cd
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        {where}
        GROUP BY sd.source_key
        ORDER BY sd.ncs_lclas_cd, sd.sqf_field_name, sd.job_name, sd.duty_name, sd.duty_level
        LIMIT ?
    """
    params.append(clamp_limit(limit))
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"sqf_duties": rows_to_dicts(rows)}


def search_sqf_jobs_payload(
    open_db: OpenDb,
    *,
    keyword: str | None = None,
    major_code: str | None = MVP_MAJOR_CODE,
    mvp_only: bool = False,
    limit: int = 50,
) -> dict[str, Any]:
    """Search SQF jobs by keyword, grouped by industry field and job."""
    with open_db() as conn:
        jobs = search_sqf_jobs_summary(
            conn,
            keyword=keyword,
            major_code=major_code,
            mvp_only=mvp_only,
            limit=limit,
        )
    return {
        "query": keyword,
        "major_code": major_code,
        "mvp_only": mvp_only,
        "sqf_jobs": jobs,
    }


def get_sqf_job_level_payload(
    open_db: OpenDb,
    *,
    source_key: str | None = None,
    job_name: str = MVP_JOB_NAME,
    duty_name: str | None = None,
    duty_level: str | None = None,
    include_mappings: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """Return an SQF job/duty level with direct SQF evidence and NCS mapping candidates."""
    with open_db() as conn:
        if source_key:
            duties = query_sqf_duties(conn, source_key=source_key, limit=1)
        else:
            mvp_only = job_name == MVP_JOB_NAME
            duties = query_sqf_duties(
                conn,
                job_name=None if mvp_only else job_name,
                duty_name=duty_name,
                duty_level=duty_level,
                mvp_only=mvp_only,
                keyword=None if duty_name or duty_level else job_name,
                limit=limit,
            )
        result = []
        for duty in duties:
            item: dict[str, Any] = {
                "sqf_duty": sqf_summary(duty),
                "direct_sqf_conditions": direct_sqf_conditions(duty),
            }
            if include_mappings:
                mapping_status, matches = get_or_generate_matches(conn, duty, limit=limit)
                item["mapping_status"] = mapping_status
                item["ncs_matches"] = matches
            result.append(item)
    return {
        "target": {
            "source_key": source_key,
            "job_name": job_name,
            "duty_name": duty_name,
            "duty_level": duty_level,
        },
        "sqf_job_levels": result,
    }


def analyze_sqf_gap_payload(
    open_db: OpenDb,
    *,
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Analyze missing NCS units for a target SQF duty level."""
    with open_db() as conn:
        return analyze_sqf_gap(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            mvp_only=target_job_name == MVP_JOB_NAME and target_source_key is None,
            limit=limit,
        )


def recommend_next_ncs_units_payload(
    open_db: OpenDb,
    *,
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Recommend next NCS units to close a legacy SQF duty gap."""
    with open_db() as conn:
        return recommend_next_ncs_units(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            limit=limit,
        )


def explain_mapping_payload(
    open_db: OpenDb,
    *,
    sqf_source_key: str,
    ncs_unit_code: str,
) -> dict[str, Any]:
    """Explain why one legacy SQF duty level maps to one NCS competency unit."""
    with open_db() as conn:
        return explain_mapping(
            conn,
            sqf_source_key=sqf_source_key,
            ncs_unit_code=ncs_unit_code,
        )


def search_learning_modules_payload(
    open_db: OpenDb,
    *,
    query: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached legacy NCS learning modules collected from openapi21."""
    with open_db() as conn:
        modules = recommendation_search_learning_modules(
            conn,
            query=query,
            major_code=major_code,
            limit=limit,
        )
    return {
        "ok": True,
        "query": query,
        "major_code": major_code,
        "modules": modules,
        "audit": {
            "data_sources": ["ncs_learning_modules"],
            "returned": len(modules),
        },
    }


def get_learning_module_payload(
    open_db: OpenDb,
    *,
    learn_module_seq: str,
) -> dict[str, Any]:
    """Return one cached legacy NCS learning module and its links."""
    with open_db() as conn:
        result = recommendation_get_learning_module(conn, learn_module_seq)
    if "error" in result:
        return {"ok": False, **result}
    return {"ok": True, **_strip_internal_payload_fields(result)}


def get_learning_path_for_sqf_job_payload(
    open_db: OpenDb,
    *,
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    current_concepts: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Build a legacy staged learning path for SQF job levels."""
    with open_db() as conn:
        return recommendation_get_learning_path(
            conn,
            query=query,
            major_code=major_code,
            target_source_key=target_source_key,
            target_level=target_level,
            current_concepts=current_concepts,
            limit=limit,
        )


def recommend_learning_modules_by_ncs_payload(
    open_db: OpenDb,
    *,
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
    """Recommend legacy learning modules without persisting recommendation runs."""
    with open_db() as conn:
        result = ncs_reference_recommend_learning_modules(
            conn,
            query=query,
            unit_code=unit_code,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
            trust_mode=trust_mode,
            limit=limit,
            save=False,
        )
    sanitized = _strip_internal_payload_fields(result)
    if save:
        sanitized.setdefault("audit", {})
        sanitized["audit"]["save_requested_ignored"] = True
        sanitized["audit"]["save_policy"] = "legacy_mcp_wrapper_forces_save_false"
    return sanitized


def search_ncs_reference_chunks_payload(
    open_db: OpenDb,
    *,
    query: str,
    document_id: int | None = None,
    limit: int = 10,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Search imported NCS reference chunks without exposing raw reference documents."""
    with open_db() as conn:
        chunks = ncs_reference_search_chunks(
            conn,
            query=query,
            document_id=document_id,
            limit=limit,
        )
    return (
        {"ok": True, "query": query, "document_id": document_id, "chunks": chunks},
        {
            "data_sources": ["ncs_reference_chunks"],
            "returned": len(chunks),
            "generated_at": now_utc(),
        },
    )


def explain_education_recommendation_payload(
    open_db: OpenDb,
    *,
    recommendation_item_id: int | None = None,
    recommendation_run_id: int | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    """Explain a saved legacy education recommendation from its evidence chain."""
    with open_db() as conn:
        return recommendation_explain_education(
            conn,
            recommendation_item_id=recommendation_item_id,
            recommendation_run_id=recommendation_run_id,
            rank=rank,
        )


def search_sqf_document_chunks_payload(
    open_db: OpenDb,
    *,
    query: str,
    ontology_tag: str | None = None,
    limit: int = 10,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Search preprocessed SQF report chunks extracted from PDF/ZIP sources."""
    clauses = ["dc.text LIKE ?"]
    params: list[Any] = [f"%{query}%"]
    if ontology_tag:
        clauses.append("dc.ontology_tags_json LIKE ?")
        params.append(f"%{ontology_tag}%")
    with open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                dc.chunk_id, dc.asset_id, dc.chunk_index,
                dc.page_start, dc.page_end, dc.char_count,
                dc.keywords_json, dc.ontology_tags_json,
                substr(dc.text, 1, 900) AS snippet,
                da.asset_name, da.asset_path,
                ds.document_id, ds.title, ds.ontology_role
            FROM sqf_document_chunks dc
            JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
            JOIN sqf_document_sources ds ON ds.document_id = da.document_id
            WHERE {' AND '.join(clauses)}
            ORDER BY dc.char_count DESC, dc.chunk_id
            LIMIT ?
            """,
            params + [clamp_limit(limit, default=10, maximum=50)],
        ).fetchall()
    return (
        {
            "query": query,
            "ontology_tag": ontology_tag,
            "chunks": [
                {
                    **{
                        key: value
                        for key, value in dict(row).items()
                        if key != "asset_path"
                    },
                    "document_title": row["title"],
                    "asset_filename": row["asset_name"],
                    "chunk_text_summary": row["snippet"],
                    "evidence_relation": "document_candidate",
                }
                for row in rows
            ],
            "note": (
                "Chunks are extracted evidence from SQF library files, "
                "not official recognition decisions."
            ),
        },
        {
            "data_sources": [
                "sqf_document_chunks",
                "sqf_document_assets",
                "sqf_document_sources",
            ],
        },
    )


def search_sqf_precision_matches_payload(
    open_db: OpenDb,
    *,
    query: str | None = None,
    source_key: str | None = None,
    min_score: float = 9.0,
    limit: int = 20,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Search candidate evidence matches between SQF report chunks and SQF job levels."""
    clauses = ["m.score >= ?", "m.review_status != 'rejected'"]
    params: list[Any] = [min_score]
    if query:
        clauses.append(
            """
            (
                dc.text LIKE ?
                OR jl.duty_name LIKE ?
                OR j.job_name LIKE ?
                OR s.sector_name LIKE ?
                OR s.sqf_field_name LIKE ?
                OR s.sqf_sub_field_name LIKE ?
            )
            """
        )
        like = f"%{query}%"
        params.extend([like, like, like, like, like, like])
    if source_key:
        clauses.append("m.sqf_source_key = ?")
        params.append(source_key)
    with open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                m.match_id, m.chunk_id, m.sqf_job_level_id, m.sqf_source_key,
                m.relation, m.score, m.method, m.evidence_text,
                m.matched_terms_json, m.review_status,
                jl.duty_name, jl.sqf_level, jl.level_name,
                j.job_name, s.sector_name, s.sqf_field_name, s.sqf_sub_field_name,
                dc.page_start, dc.page_end,
                da.asset_name, ds.document_id, ds.title, ds.ontology_role
            FROM sqf_chunk_job_level_matches m
            JOIN sqf_job_levels_normalized jl ON jl.sqf_job_level_id = m.sqf_job_level_id
            JOIN sqf_jobs_normalized j ON j.sqf_job_id = jl.sqf_job_id
            JOIN sqf_industry_sectors s ON s.sector_id = j.sector_id
            JOIN sqf_document_chunks dc ON dc.chunk_id = m.chunk_id
            JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
            JOIN sqf_document_sources ds ON ds.document_id = da.document_id
            WHERE {' AND '.join(clauses)}
            ORDER BY m.score DESC, m.match_id
            LIMIT ?
            """,
            params + [clamp_limit(limit, default=20, maximum=100)],
        ).fetchall()
    return (
        {
            "query": query,
            "source_key": source_key,
            "min_score": min_score,
            "matches": rows_to_dicts(rows),
            "note": (
                "These are candidate evidence links from report OCR/text chunks, "
                "not official recognition decisions."
            ),
        },
        {
            "data_sources": [
                "sqf_chunk_job_level_matches",
                "sqf_document_chunks",
                "sqf_job_levels_normalized",
            ],
        },
    )


def get_sqf_ontology_job_level_payload(
    open_db: OpenDb,
    *,
    source_key: str,
) -> dict[str, Any] | None:
    """Return normalized SQF job-level ontology node and linked document evidence."""
    with open_db() as conn:
        job_level = conn.execute(
            """
            SELECT
                jl.*, j.job_name, j.job_definition,
                s.sector_id, s.sector_name, s.ncs_lclas_cd, s.ncs_lclas_name,
                s.sqf_field_name, s.sqf_sub_field_name
            FROM sqf_job_levels_normalized jl
            JOIN sqf_jobs_normalized j ON j.sqf_job_id = jl.sqf_job_id
            JOIN sqf_industry_sectors s ON s.sector_id = j.sector_id
            WHERE jl.sqf_source_key = ?
            """,
            (source_key,),
        ).fetchone()
        if job_level is None:
            return None
        evidence = conn.execute(
            """
            SELECT evidence_type, evidence_text, source_field, source, review_status
            FROM sqf_recognition_evidence
            WHERE sqf_job_level_id = ?
            ORDER BY evidence_type, evidence_id
            """,
            (job_level["sqf_job_level_id"],),
        ).fetchall()
        document_links = conn.execute(
            """
            SELECT l.target_type, l.target_id, l.relation, l.evidence_note,
                   l.confidence, ds.document_id, ds.title, ds.ontology_role
            FROM sqf_document_evidence_links l
            JOIN sqf_document_sources ds ON ds.document_id = l.document_id
            WHERE (l.target_type = 'sqf_job' AND l.target_id = ?)
               OR (l.target_type = 'sqf_sector' AND l.target_id = ?)
            ORDER BY ds.document_id, l.relation
            LIMIT 50
            """,
            (job_level["sqf_job_id"], job_level["sector_id"]),
        ).fetchall()
        chunk_matches = conn.execute(
            """
            SELECT
                m.match_id, m.chunk_id, m.relation, m.score, m.method,
                m.evidence_text, m.matched_terms_json, m.review_status,
                dc.page_start, dc.page_end,
                da.asset_name, ds.document_id, ds.title, ds.ontology_role
            FROM sqf_chunk_job_level_matches m
            JOIN sqf_document_chunks dc ON dc.chunk_id = m.chunk_id
            JOIN sqf_document_assets da ON da.asset_id = dc.asset_id
            JOIN sqf_document_sources ds ON ds.document_id = da.document_id
            WHERE m.sqf_job_level_id = ?
              AND m.review_status != 'rejected'
            ORDER BY m.score DESC, m.match_id
            LIMIT 30
            """,
            (job_level["sqf_job_level_id"],),
        ).fetchall()
    return {
        "job_level": row_to_dict(job_level),
        "recognition_evidence": rows_to_dicts(evidence),
        "document_links": rows_to_dicts(document_links),
        "document_chunk_matches": rows_to_dicts(chunk_matches),
    }
