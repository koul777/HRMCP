from __future__ import annotations

import argparse
import inspect
import json
import sqlite3

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from ncs_mcp.config import load_settings
from ncs_mcp.career_path import (
    career_path_summary as ncs_career_path_summary,
    import_career_paths_csv as ncs_import_career_paths_csv,
)
from ncs_mcp.db import (
    clamp_limit,
    connect,
    initialize_database,
    normalize_concept_key,
    now_utc,
    prepare_ontology_human_review_queue as db_prepare_ontology_human_review_queue,
    recommend_task_transitions as db_recommend_task_transitions,
    row_to_dict,
    rows_to_dicts,
)
from ncs_mcp.error_codes import error_metadata
from ncs_mcp.helpers import DISCLAIMER, mask_sensitive_payload, not_found_response
from ncs_mcp.job_base_api import (
    collect_job_base_competencies as job_base_collect_competencies,
    fetch_job_base_page as job_base_fetch_page,
    job_base_summary as job_base_cached_summary,
    search_job_base_links as job_base_search_links,
)
from ncs_mcp.ontology import (
    MVP_JOB_NAME,
    MVP_MAJOR_CODE,
    MVP_SQF_FIELD_NAME,
    ONTOLOGY_SCHEMA,
    TRUSTED_MAPPING_FILTER,
    analyze_sqf_gap as ontology_analyze_sqf_gap,
    build_learning_objectives_for_units,
    build_sqf_mapping_candidates as ontology_build_sqf_mapping_candidates,
    direct_sqf_conditions,
    explain_mapping as ontology_explain_mapping,
    generate_mapping_candidates,
    get_filtered_matches,
    get_or_generate_matches,
    get_sqf_duty,
    query_sqf_duties,
    recommend_next_ncs_units as ontology_recommend_next_ncs_units,
    search_sqf_jobs_summary,
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
    recommend_learning_modules_by_ncs as ncs_ref_recommend_modules,
    review_exact_learning_module_name_links as ncs_ref_review_exact_module_links,
    review_learning_module_ncs_link as ncs_ref_review_module_link,
    search_ncs_reference_chunks as ncs_ref_search_chunks,
)
from ncs_mcp.qualification_api import (
    collect_qualification_links as qualification_collect_links,
    fetch_qualification_page as qualification_fetch_page,
    qualification_error_report as qualification_cached_error_report,
    qualification_summary as qualification_cached_summary,
    retry_qualification_error_units as qualification_retry_error_units,
    search_qualification_links as qualification_search_links,
)
from ncs_mcp.recommendation import (
    explain_education_recommendation as recommendation_explain_education,
    get_learning_path_for_sqf_job as recommendation_get_learning_path,
    get_learning_module as recommendation_get_learning_module,
    recommend_education_for_duty as recommendation_recommend_education,
    search_learning_modules as recommendation_search_learning_modules,
)
from ncs_mcp.sqf_sqlite import sqf_model_summary
from ncs_mcp.training_recommendation import (
    build_training_course_ontology_links as training_build_course_links,
    compact_training_task_response as training_compact_task_response,
    compact_training_transition_response as training_compact_transition_response,
    get_training_course as training_get_course,
    recommend_training_for_task as training_recommend_for_task,
    recommend_training_transition as training_recommend_transition,
    resolve_ncs_query_scope as training_resolve_ncs_query_scope,
    search_training_courses as training_search_courses,
)
from ncs_mcp import tool_registry


mcp = FastMCP("ncs-mcp")

CURRENT_TRANSPORT = "stdio"


READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_request: Any) -> JSONResponse:
    surface = current_mcp_tool_surface()
    runtime = runtime_health_metadata()
    transport = current_transport_metadata()
    return JSONResponse(
        {
            "status": "ok" if runtime["database"]["ready"] else "degraded",
            "name": "ncs-mcp",
            "transport": transport["transport"],
            "endpoint": transport["endpoint"],
            "tools": {
                "exposed": len(surface["all_tools"]),
                "user": len(surface["user_tools"]),
                "operator": len(surface["operator_tools"]),
                "legacy_present": len(surface["legacy_tools_present"]),
                "unexpected": len(surface["unexpected_tools"]),
            },
            "runtime": runtime,
        }
    )


@mcp.custom_route("/ready", methods=["GET"], include_in_schema=False)
async def readiness_check(_request: Any) -> JSONResponse:
    runtime = runtime_health_metadata()
    ready = bool(runtime["database"]["ready"])
    return JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "name": "ncs-mcp",
            "runtime": runtime,
        },
        status_code=200 if ready else 503,
    )


def db():
    conn = connect(load_settings().db_path)
    initialize_database(conn)
    return conn


def runtime_health_metadata() -> dict[str, Any]:
    settings = load_settings()
    api_keys = {
        "service_key_present": bool(settings.service_key),
        "training_course_service_key_present": bool(settings.training_course_service_key),
        "qualification_service_key_present": bool(settings.qualification_service_key),
        "job_base_service_key_present": bool(settings.job_base_service_key),
        "sqf_service_key_present": bool(settings.sqf_service_key),
        "study_module_service_key_present": bool(settings.study_module_service_key),
    }
    return {
        "database": database_readiness_metadata(settings.db_path),
        "operator_tools_enabled": settings.operator_tools_enabled,
        "api_keys": api_keys,
        "api_key_present_count": sum(1 for present in api_keys.values() if present),
    }


def current_transport_metadata() -> dict[str, str | None]:
    if CURRENT_TRANSPORT == "streamable-http":
        endpoint = mcp.settings.streamable_http_path
    elif CURRENT_TRANSPORT == "sse":
        endpoint = mcp.settings.sse_path
    else:
        endpoint = None
    return {"transport": CURRENT_TRANSPORT, "endpoint": endpoint}


def database_readiness_metadata(db_path) -> dict[str, Any]:
    configured = bool(db_path)
    exists = bool(configured and db_path.exists())
    result: dict[str, Any] = {
        "configured": configured,
        "exists": exists,
        "openable": False,
        "ready": False,
        "core_tables": {},
    }
    if not configured:
        result["error"] = {"code": "database_not_configured"}
        return result
    if not exists:
        result["error"] = {"code": "database_missing"}
        return result
    try:
        db_uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(db_uri, uri=True)
        try:
            result["openable"] = True
            for table_name in READINESS_CORE_TABLES:
                exists_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                    (table_name,),
                ).fetchone()
                if exists_row is None:
                    result["core_tables"][table_name] = {"exists": False, "row_count": None}
                    continue
                row_count = int(conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
                result["core_tables"][table_name] = {"exists": True, "row_count": row_count}
            result["ready"] = all(
                item.get("exists") and int(item.get("row_count") or 0) > 0
                for item in result["core_tables"].values()
            ) and len(result["core_tables"]) == len(READINESS_CORE_TABLES)
            if not result["ready"]:
                result["error"] = {"code": "database_not_ready"}
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive health path
        result["error"] = {"code": "database_unopenable", "type": type(exc).__name__}
    return result


@contextmanager
def open_db():
    conn = db()
    try:
        yield conn
    finally:
        conn.close()


def text_value(raw: str | None, refined: str | None, version: str) -> str | dict[str, str | None]:
    if version == "both":
        return {"raw": raw, "refined": refined}
    if version == "refined":
        return refined or raw or ""
    return raw or ""


def like_filter(clauses: list[str], params: list[Any], column: str, value: str | None) -> None:
    if value:
        clauses.append(f"{column} LIKE ?")
        params.append(f"%{value}%")


def exact_filter(clauses: list[str], params: list[Any], column: str, value: str | None) -> None:
    if value:
        clauses.append(f"{column} = ?")
        params.append(value)


def tool_response(
    payload: dict[str, Any],
    *,
    data: Any | None = None,
    audit: dict[str, Any] | None = None,
    ok: bool | None = None,
) -> dict[str, Any]:
    """Add the v1 MCP ok/data/error/audit envelope without removing legacy keys."""
    result = dict(payload)
    raw_error = result.get("error")
    if isinstance(raw_error, str):
        result["error"] = {"code": raw_error}
    elif raw_error is None:
        result["error"] = None
    if ok is None:
        ok = raw_error is None and result.get("ok", True) is not False
    result["ok"] = bool(ok)
    if not result["ok"] and result["error"] is None:
        result["error"] = {"code": "TOOL_ERROR"}
    if data is None:
        data = result.get(
            "data",
            {
                key: value
                for key, value in result.items()
                if key not in {"ok", "data", "error", "audit"}
            },
        )
    result["data"] = data
    result["audit"] = audit or result.get(
        "audit",
        {
            "data_sources": ["SQLite NCS/SQF knowledge base"],
            "generated_at": now_utc(),
        },
    )
    return result


def error_response(code: str, **fields: Any) -> dict[str, Any]:
    safe_fields = mask_sensitive_payload(fields)
    metadata = error_metadata(code)
    reserved = {"code", "category", "retryable", "known", "severity", "description"}
    structured_fields = {key: value for key, value in safe_fields.items() if key not in reserved}
    if "not_found" in code.lower() or code.upper() == "NOT_FOUND":
        detail = ", ".join(f"{key}={value}" for key, value in safe_fields.items())
        message = code if not detail else f"{code}: {detail}"
        response = not_found_response(message)
        suggestions = response.get("error", {}).get("suggestions", [])
        not_found_fields = {
            key: value
            for key, value in structured_fields.items()
            if key not in {"message", "suggestions"}
        }
        response["error"] = {
            "code": code,
            **metadata,
            "message": message,
            "suggestions": suggestions,
            **not_found_fields,
        }
        if len(not_found_fields) != len(safe_fields):
            response["error"]["details"] = safe_fields
        response["data"] = {**response.get("data", {}), **safe_fields}
        return response
    error_payload = {"code": code, **metadata, **structured_fields}
    if len(structured_fields) != len(safe_fields):
        error_payload["details"] = safe_fields
    return tool_response(
        {"error": error_payload},
        data={},
        audit={"data_sources": ["SQLite NCS/SQF knowledge base"], "generated_at": now_utc()},
        ok=False,
    )


def quality_for(conn, target_type: str, target_id: str | int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT issue_id, issue_type, severity, issue_detail, suggested_action, detected_at
        FROM quality_issues
        WHERE target_type = ? AND target_id = ?
        ORDER BY issue_id
        """,
        (target_type, str(target_id)),
    ).fetchall()
    return rows_to_dicts(rows)


def unit_path(row) -> dict[str, Any]:
    return {
        "major_code": row["major_code"],
        "major": row["major_name"],
        "middle_code": row["middle_code"],
        "middle": row["middle_name"],
        "small_code": row["small_code"],
        "small": row["small_name"],
        "sub_code": row["sub_code"],
        "sub": row["sub_name"],
        "duty_definition": row["duty_def_api"],
        "duty_order": row["duty_order"],
    }


def has_not_found_error(result: dict[str, Any]) -> bool:
    error = result.get("error")
    code = ""
    if isinstance(error, dict):
        code = str(error.get("code", ""))
    elif error:
        code = str(error)
    return code.upper() == "NOT_FOUND" or "not_found" in code.lower()


@mcp.resource("ontology://schema")
def ontology_schema() -> str:
    """Return the NCS-SQF ontology schema and MVP modeling principles."""
    return json.dumps(ONTOLOGY_SCHEMA, ensure_ascii=False, indent=2)


@mcp.resource("sqf://mvp/management-support")
def management_support_mvp() -> str:
    """Return the first NCS-SQF ontology MVP scope: SQF management support duties."""
    with open_db() as conn:
        duties = query_sqf_duties(conn, mvp_only=True, limit=100)
    return json.dumps(
        {
            "mvp": ONTOLOGY_SCHEMA["mvp"],
            "sqf_duties": [sqf_summary(row) for row in duties],
            "note": (
                "경영지원 MVP는 SQF 02 경영·회계·사무 > 경영관리 > 경영지원과 "
                "NCS 02 경영·회계·사무를 먼저 연결한다."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.prompt()
def sqf_gap_report_prompt(
    target_job_name: str = MVP_JOB_NAME,
    target_level: str = "",
) -> str:
    """Create a Korean report prompt for NCS-SQF gap analysis."""
    level_text = target_level or "전체 관련 수준"
    return f"""
사용자의 보유 NCS 능력단위와 목표 SQF 직무수준을 비교해 역량 갭 분석 보고서를 작성하라.

목표 SQF 직무: {target_job_name}
목표 SQF 수준: {level_text}

보고서에는 다음을 포함하라:
1. 매칭된 SQF 직무수준과 직무정의
2. 충족한 NCS 능력단위
3. 부족한 NCS 능력단위와 우선순위
4. SQF 교육훈련, 자격, 경력 직접 근거
5. SQF 직접 근거가 없을 때 NCS 능력단위/KSA를 학습목표로 전환한 보완 추천
6. 매핑 confidence, review_status, evidence_text
7. 문서/OCR/HWP 청크 근거가 있으면 보고서 제목과 페이지 범위

주의: 이 결과는 공식 인정 판정이 아니라 근거 기반 추천/갭분석 보조 결과로 작성한다.
""".strip()


@mcp.tool()
def ncs_search(query: str = "", scope: str = "all", limit: int = 20) -> dict[str, Any]:
    """Search NCS classification/unit/element/criteria/KSA records through one tool."""
    normalized_scope = scope if scope in {"unit", "element", "criteria", "all"} else "all"
    if not query:
        result = list_classifications(limit=limit)
        rows = result.get("classifications", [])
        if not rows:
            return not_found_response("NCS 분류 목록을 찾을 수 없습니다.")
        return tool_response(
            {"query": query, "scope": "classification", "classifications": rows},
            audit={
                "data_sources": ["classifications", "competency_units"],
                "returned": len(rows),
                "generated_at": now_utc(),
            },
        )
    result = search_ncs(query=query, scope=normalized_scope, limit=limit)
    rows = result.get("results", [])
    if not rows:
        return not_found_response(f"NCS 검색 결과가 없습니다: {query}")
    return tool_response(
        result,
        audit={
            "data_sources": [
                "classifications",
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ksa_items",
            ],
            "returned": len(rows),
            "generated_at": now_utc(),
        },
    )


@mcp.tool()
def ncs_unit_detail(
    unit_code: str,
    include: list[str] | None = None,
    text_version: str = "raw",
) -> dict[str, Any]:
    """Return one NCS unit with selected elements, criteria, KSA, training, and qualification evidence."""
    include_set = set(include or ["elements", "criteria", "ksa"])
    result = get_unit_structure(unit_code, text_version=text_version)
    if has_not_found_error(result):
        return not_found_response(f"NCS 능력단위를 찾을 수 없습니다: {unit_code}")
    if "elements" not in include_set:
        result.pop("elements", None)
    elif result.get("elements"):
        for element in result["elements"]:
            if "criteria" not in include_set:
                element.pop("performance_criteria", None)
            if "ksa" not in include_set:
                element.pop("ksa", None)
    with open_db() as conn:
        if "training" in include_set:
            result["training_courses"] = training_search_courses(conn, unit_code=unit_code, limit=20)
        if "qualification" in include_set:
            result["qualification_links"] = qualification_search_links(conn, unit_code=unit_code, limit=20)
    return tool_response(
        result,
        audit={
            "data_sources": [
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ksa_items",
                "ncs_training_courses",
                "ncs_qualification_items",
            ],
            "generated_at": now_utc(),
        },
    )


@mcp.tool()
def ncs_training(
    query: str | None = None,
    training_course_id: int | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search NCS training courses or return one course by id."""
    if training_course_id is not None:
        result = get_training_course(training_course_id)
        if has_not_found_error(result):
            return not_found_response(f"훈련과정을 찾을 수 없습니다: {training_course_id}")
        return result
    result = search_training_courses(query=query, limit=limit)
    rows = result.get("training_courses") or result.get("data", {}).get("training_courses", [])
    if not rows:
        return not_found_response(f"훈련과정 검색 결과가 없습니다: {query or ''}".strip())
    return result


@mcp.tool()
def ncs_analysis(
    mode: str,
    query: str | None = None,
    unit_code: str | None = None,
    concept_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search career path, qualification, job-base, or ontology evidence through one analysis tool."""
    if mode == "career_path":
        result = get_career_path_summary(limit=limit)
        items = result.get("career_paths") or result.get("data", {}).get("career_paths", [])
    elif mode == "qualification":
        result = search_qualification_items(
            unit_code=unit_code,
            qualification_name=query,
            limit=limit,
        )
        items = result.get("qualification_links") or result.get("data", {}).get("qualification_links", [])
    elif mode == "job_base":
        result = search_job_base_competencies(
            unit_code=unit_code,
            competency_name=query,
            limit=limit,
        )
        items = result.get("job_base_links") or result.get("data", {}).get("job_base_links", [])
    elif mode == "ontology":
        result = search_ontology_concepts(
            query=query,
            concept_type=concept_type,
            limit=limit,
        )
        items = result.get("concepts") or result.get("data", {}).get("concepts", [])
    else:
        return error_response(
            "unsupported_analysis_mode",
            mode=mode,
            allowed=["career_path", "qualification", "job_base", "ontology"],
        )
    if not items:
        return not_found_response(f"{mode} 분석 결과가 없습니다.")
    return result


@mcp.tool()
def ncs_discover_tools(intent: str = "") -> dict[str, Any]:
    """Discover the compact NCS MCP tool surface by user intent or category."""
    surface = current_mcp_tool_surface()
    matches = tool_registry.discover_tools_for_intent(
        intent,
        executable_tool_names=tool_registry.NCS_EXECUTABLE_TOOL_NAMES,
        available_tool_names=set(surface["all_tools"]),
    )
    return tool_response(
        {
            "intent": intent,
            "matched_categories": matches,
            "exposed_tool_count": len(surface["all_tools"]),
            "execution_note": (
                "Use ncs_execute_tool for read-only user tools. Operator/review tools must be called directly."
            ),
            "hidden_operator_note": (
                "Review/operator tools are hidden unless NCS_MCP_ENABLE_OPERATOR_TOOLS=1 is set before server start."
            ),
            "hidden_legacy_note": "SQF and learning-module legacy tools are not part of the active recommendation path.",
        },
        audit={
            "data_sources": ["NCS MCP tool registry"],
            "generated_at": now_utc(),
        },
    )


@mcp.tool()
def ncs_execute_tool(tool_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a read-only user NCS MCP tool discovered by ncs_discover_tools."""
    if tool_name in {"ncs_discover_tools", "ncs_execute_tool"}:
        return error_response("meta_tool_recursion_blocked", tool_name=tool_name)
    if tool_name not in tool_registry.NCS_EXECUTABLE_TOOL_NAMES:
        return error_response(
            "tool_not_executable_via_meta",
            tool_name=tool_name,
            executable_tools=sorted(tool_registry.NCS_EXECUTABLE_TOOL_NAMES),
            note="Operator/review tools and hidden legacy tools are blocked from ncs_execute_tool.",
        )
    tool_params = dict(params or {})
    if tool_name in tool_registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS:
        tool_params["save"] = False
    if tool_name in tool_registry.NCS_META_COMPACT_DEFAULT_TOOLS:
        tool_params.setdefault("compact", True)
    handler = NCS_EXECUTABLE_TOOL_HANDLERS[tool_name]
    try:
        inspect.signature(handler).bind(**tool_params)
    except TypeError as exc:
        return error_response(
            "invalid_tool_parameters",
            tool_name=tool_name,
            message=str(exc),
        )
    try:
        result = handler(**tool_params)
    except Exception as exc:
        return error_response(
            "tool_execution_failed",
            tool_name=tool_name,
            message=str(exc),
        )
    if isinstance(result, dict):
        result.setdefault("meta_execution", {})
        result["meta_execution"].update(
            {
                "tool_name": tool_name,
                "save_forced_false": tool_name in tool_registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS,
                "compact_defaulted": (
                    tool_name in tool_registry.NCS_META_COMPACT_DEFAULT_TOOLS
                    and "compact" not in (params or {})
                ),
            }
        )
        return result
    return tool_response(
        {
            "tool_name": tool_name,
            "result": result,
            "meta_execution": {
                "tool_name": tool_name,
                "save_forced_false": tool_name in tool_registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS,
                "compact_defaulted": (
                    tool_name in tool_registry.NCS_META_COMPACT_DEFAULT_TOOLS
                    and "compact" not in (params or {})
                ),
            },
        },
        audit={
            "data_sources": ["NCS MCP tool registry"],
            "generated_at": now_utc(),
        },
    )


def list_classifications(
    major_code: str | None = None,
    major_name: str | None = None,
    middle_code: str | None = None,
    middle_name: str | None = None,
    small_code: str | None = None,
    small_name: str | None = None,
    sub_code: str | None = None,
    sub_name: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List NCS classifications and competency unit counts."""
    clauses: list[str] = []
    params: list[Any] = []
    exact_filter(clauses, params, "c.major_code", major_code)
    like_filter(clauses, params, "c.major_name", major_name)
    exact_filter(clauses, params, "c.middle_code", middle_code)
    like_filter(clauses, params, "c.middle_name", middle_name)
    exact_filter(clauses, params, "c.small_code", small_code)
    like_filter(clauses, params, "c.small_name", small_name)
    exact_filter(clauses, params, "c.sub_code", sub_code)
    like_filter(clauses, params, "c.sub_name", sub_name)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            c.classification_id,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name,
            c.duty_def_api, c.duty_order,
            COUNT(cu.unit_code) AS unit_count
        FROM classifications c
        LEFT JOIN competency_units cu ON cu.classification_id = c.classification_id
        {where}
        GROUP BY c.classification_id
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code
        LIMIT ?
    """
    params.append(clamp_limit(limit, default=100))
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"classifications": rows_to_dicts(rows)}


def get_competency_units(
    major_code: str | None = None,
    major_name: str | None = None,
    middle_code: str | None = None,
    middle_name: str | None = None,
    small_code: str | None = None,
    small_name: str | None = None,
    sub_code: str | None = None,
    sub_name: str | None = None,
    level_min: int | None = None,
    level_max: int | None = None,
    keyword: str | None = None,
    api_match_status: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get competency units by classification, level, keyword, or API match status."""
    clauses: list[str] = []
    params: list[Any] = []
    exact_filter(clauses, params, "c.major_code", major_code)
    like_filter(clauses, params, "c.major_name", major_name)
    exact_filter(clauses, params, "c.middle_code", middle_code)
    like_filter(clauses, params, "c.middle_name", middle_name)
    exact_filter(clauses, params, "c.small_code", small_code)
    like_filter(clauses, params, "c.small_name", small_name)
    exact_filter(clauses, params, "c.sub_code", sub_code)
    like_filter(clauses, params, "c.sub_name", sub_name)
    if level_min is not None:
        clauses.append("CAST(cu.unit_level_raw AS INTEGER) >= ?")
        params.append(level_min)
    if level_max is not None:
        clauses.append("CAST(cu.unit_level_raw AS INTEGER) <= ?")
        params.append(level_max)
    if keyword:
        clauses.append("(cu.unit_code LIKE ? OR cu.unit_name_raw LIKE ? OR cu.api_definition LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
    if api_match_status:
        clauses.append("cu.api_match_status = ?")
        params.append(api_match_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            cu.unit_code,
            cu.unit_name_raw AS unit_name,
            cu.unit_level_raw AS level,
            cu.api_definition,
            cu.api_match_status,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        LIMIT ?
    """
    params.append(clamp_limit(limit))
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {"units": rows_to_dicts(rows)}


def search_ncs_units(
    keyword: str,
    major_code: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search NCS competency units by keyword. Alias for ontology-facing clients."""
    return get_competency_units(major_code=major_code, keyword=keyword, limit=limit)


def get_unit_structure(
    unit_code: str,
    text_version: str = "raw",
    include_quality_issues: bool = False,
) -> dict[str, Any]:
    """Get a full competency unit hierarchy: elements, performance criteria, and KSA."""
    version = text_version if text_version in {"raw", "refined", "both"} else "raw"
    with open_db() as conn:
        unit = conn.execute(
            """
            SELECT
                cu.*,
                c.major_code, c.major_name,
                c.middle_code, c.middle_name,
                c.small_code, c.small_name,
                c.sub_code, c.sub_name,
                c.duty_def_api, c.duty_order
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (unit_code,),
        ).fetchone()
        if unit is None:
            return {"error": "not_found", "unit_code": unit_code}
        elements = conn.execute(
            """
            SELECT *
            FROM competency_elements
            WHERE unit_code = ?
            ORDER BY CAST(element_no AS INTEGER), element_code_raw
            """,
            (unit_code,),
        ).fetchall()
        result_elements: list[dict[str, Any]] = []
        for element in elements:
            criteria_rows = conn.execute(
                """
                SELECT *
                FROM performance_criteria
                WHERE element_id = ?
                ORDER BY CAST(criteria_no AS INTEGER), criteria_id
                """,
                (element["element_id"],),
            ).fetchall()
            ksa_rows = conn.execute(
                """
                SELECT *
                FROM ksa_items
                WHERE element_id = ?
                ORDER BY ksa_type_code, CAST(ksa_no AS INTEGER), ksa_id
                """,
                (element["element_id"],),
            ).fetchall()
            criteria = []
            for row in criteria_rows:
                item = {
                    "criteria_id": row["criteria_id"],
                    "criteria_no": row["criteria_no"],
                    "text": text_value(row["criteria_text_raw"], row["criteria_text_refined"], version),
                    "review_status": row["review_status"],
                }
                if include_quality_issues:
                    item["quality_issues"] = quality_for(conn, "criteria", row["criteria_id"])
                criteria.append(item)
            ksa = []
            for row in ksa_rows:
                item = {
                    "ksa_id": row["ksa_id"],
                    "ksa_type": row["ksa_type_name"],
                    "ksa_no": row["ksa_no"],
                    "text": text_value(row["ksa_text_raw"], row["ksa_text_refined"], version),
                    "review_status": row["review_status"],
                }
                if include_quality_issues:
                    item["quality_issues"] = quality_for(conn, "ksa", row["ksa_id"])
                ksa.append(item)
            element_item = {
                "element_id": element["element_id"],
                "element_no": element["element_no"],
                "element_code": element["element_code_raw"],
                "element_name": element["element_name_raw"],
                "element_level": element["element_level_raw"],
                "api_element_name": element["api_element_name"],
                "api_element_level": element["api_element_level"],
                "api_match_status": element["api_match_status"],
                "performance_criteria": criteria,
                "ksa": ksa,
            }
            if include_quality_issues:
                element_item["quality_issues"] = quality_for(conn, "element", element["element_id"])
            result_elements.append(element_item)

        result = {
            "unit": {
                "unit_code": unit["unit_code"],
                "unit_name": unit["unit_name_raw"],
                "unit_level": unit["unit_level_raw"],
                "classification": unit_path(unit),
                "api_definition": unit["api_definition"],
                "api_match_status": unit["api_match_status"],
            },
            "elements": result_elements,
        }
        if include_quality_issues:
            result["unit"]["quality_issues"] = quality_for(conn, "unit", unit["unit_code"])
        return result


def get_element_detail(
    element_id: int,
    text_version: str = "raw",
    include_quality_issues: bool = False,
) -> dict[str, Any]:
    """Get one competency element with its performance criteria and KSA."""
    with open_db() as conn:
        element = conn.execute(
            """
            SELECT ce.*, cu.unit_name_raw, cu.unit_level_raw
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ce.element_id = ?
            """,
            (element_id,),
        ).fetchone()
        if element is None:
            return {"error": "not_found", "element_id": element_id}
    unit_result = get_unit_structure(element["unit_code"], text_version, include_quality_issues)
    for item in unit_result.get("elements", []):
        if item["element_id"] == element_id:
            return {"unit": unit_result["unit"], "element": item}
    return {"error": "not_found", "element_id": element_id}


def get_performance_criteria(
    unit_code: str | None = None,
    element_id: int | None = None,
    keyword: str | None = None,
    text_version: str = "raw",
    limit: int = 50,
) -> dict[str, Any]:
    """Get performance criteria by unit, element, or keyword."""
    clauses: list[str] = []
    params: list[Any] = []
    if unit_code:
        clauses.append("ce.unit_code = ?")
        params.append(unit_code)
    if element_id:
        clauses.append("pc.element_id = ?")
        params.append(element_id)
    if keyword:
        clauses.append("(pc.criteria_text_raw LIKE ? OR pc.criteria_text_refined LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            pc.*, ce.unit_code, ce.element_name_raw, cu.unit_name_raw
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        {where}
        ORDER BY ce.unit_code, ce.element_id, CAST(pc.criteria_no AS INTEGER), pc.criteria_id
        LIMIT ?
    """
    params.append(clamp_limit(limit))
    version = text_version if text_version in {"raw", "refined", "both"} else "raw"
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {
        "criteria": [
            {
                "criteria_id": row["criteria_id"],
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "element_id": row["element_id"],
                "element_name": row["element_name_raw"],
                "criteria_no": row["criteria_no"],
                "text": text_value(row["criteria_text_raw"], row["criteria_text_refined"], version),
                "review_status": row["review_status"],
            }
            for row in rows
        ]
    }


def get_ksa(
    unit_code: str | None = None,
    element_id: int | None = None,
    ksa_type: str | None = None,
    keyword: str | None = None,
    text_version: str = "raw",
    limit: int = 50,
) -> dict[str, Any]:
    """Get KSA items by unit, element, KSA type, or keyword."""
    clauses: list[str] = []
    params: list[Any] = []
    if unit_code:
        clauses.append("ce.unit_code = ?")
        params.append(unit_code)
    if element_id:
        clauses.append("ki.element_id = ?")
        params.append(element_id)
    if ksa_type:
        clauses.append("(ki.ksa_type_name = ? OR ki.ksa_type_code = ?)")
        params.extend([ksa_type, ksa_type])
    if keyword:
        clauses.append("(ki.ksa_text_raw LIKE ? OR ki.ksa_text_refined LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
            ki.*, ce.unit_code, ce.element_name_raw, cu.unit_name_raw
        FROM ksa_items ki
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        {where}
        ORDER BY ce.unit_code, ce.element_id, ki.ksa_type_code, CAST(ki.ksa_no AS INTEGER), ki.ksa_id
        LIMIT ?
    """
    params.append(clamp_limit(limit))
    version = text_version if text_version in {"raw", "refined", "both"} else "raw"
    with open_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return {
        "ksa": [
            {
                "ksa_id": row["ksa_id"],
                "unit_code": row["unit_code"],
                "unit_name": row["unit_name_raw"],
                "element_id": row["element_id"],
                "element_name": row["element_name_raw"],
                "ksa_type": row["ksa_type_name"],
                "ksa_no": row["ksa_no"],
                "text": text_value(row["ksa_text_raw"], row["ksa_text_refined"], version),
                "review_status": row["review_status"],
            }
            for row in rows
        ]
    }


def search_ncs(query: str, scope: str = "all", limit: int = 50) -> dict[str, Any]:
    """Search NCS units, elements, criteria, and KSA text."""
    max_rows = clamp_limit(limit)
    pattern = f"%{query}%"
    results: list[dict[str, Any]] = []
    with open_db() as conn:
        if scope in {"all", "unit"} and len(results) < max_rows:
            rows = conn.execute(
                """
                SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name,
                       c.duty_def_api, c.duty_order
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE cu.unit_code LIKE ? OR cu.unit_name_raw LIKE ? OR cu.api_definition LIKE ?
                LIMIT ?
                """,
                (pattern, pattern, pattern, max_rows - len(results)),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "type": "unit",
                        "id": row["unit_code"],
                        "text": row["unit_name_raw"],
                        "path": unit_path(row),
                        "api_definition": row["api_definition"],
                    }
                )
        if scope in {"all", "element"} and len(results) < max_rows:
            rows = conn.execute(
                """
                SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ce.element_name_raw LIKE ?
                LIMIT ?
                """,
                (pattern, max_rows - len(results)),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "type": "element",
                        "id": row["element_id"],
                        "text": row["element_name_raw"],
                        "path": {"unit_code": row["unit_code"], "unit_name": row["unit_name_raw"]},
                    }
                )
        if scope in {"all", "criteria"} and len(results) < max_rows:
            rows = conn.execute(
                """
                SELECT pc.criteria_id, pc.criteria_text_raw, ce.element_id,
                       ce.element_name_raw, ce.unit_code, cu.unit_name_raw
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE pc.criteria_text_raw LIKE ? OR pc.criteria_text_refined LIKE ?
                LIMIT ?
                """,
                (pattern, pattern, max_rows - len(results)),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "type": "criteria",
                        "id": row["criteria_id"],
                        "text": row["criteria_text_raw"],
                        "path": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "element_id": row["element_id"],
                            "element_name": row["element_name_raw"],
                        },
                    }
                )
        if scope in {"all", "ksa"} and len(results) < max_rows:
            rows = conn.execute(
                """
                SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ce.element_id,
                       ce.element_name_raw, ce.unit_code, cu.unit_name_raw
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ki.ksa_text_raw LIKE ? OR ki.ksa_text_refined LIKE ?
                LIMIT ?
                """,
                (pattern, pattern, max_rows - len(results)),
            ).fetchall()
            for row in rows:
                results.append(
                    {
                        "type": "ksa",
                        "id": row["ksa_id"],
                        "text": row["ksa_text_raw"],
                        "ksa_type": row["ksa_type_name"],
                        "path": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "element_id": row["element_id"],
                            "element_name": row["element_name_raw"],
                        },
                    }
                )
    return {"query": query, "scope": scope, "results": results}


@mcp.tool()
def get_quality_issues(
    target_type: str | None = None,
    unit_code: str | None = None,
    issue_type: str | None = None,
    severity: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get quality issues, optionally scoped to a target type, unit, issue type, or severity."""
    clauses: list[str] = []
    params: list[Any] = []
    target_ids: list[str] = []
    with open_db() as conn:
        if unit_code:
            target_ids.append(unit_code)
            element_rows = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = ?", (unit_code,)
            ).fetchall()
            element_ids = [str(row["element_id"]) for row in element_rows]
            target_ids.extend(element_ids)
            if element_ids:
                placeholders = ",".join("?" for _ in element_ids)
                criteria_rows = conn.execute(
                    f"SELECT criteria_id FROM performance_criteria WHERE element_id IN ({placeholders})",
                    element_ids,
                ).fetchall()
                ksa_rows = conn.execute(
                    f"SELECT ksa_id FROM ksa_items WHERE element_id IN ({placeholders})",
                    element_ids,
                ).fetchall()
                target_ids.extend(str(row["criteria_id"]) for row in criteria_rows)
                target_ids.extend(str(row["ksa_id"]) for row in ksa_rows)
        if target_type:
            clauses.append("target_type = ?")
            params.append(target_type)
        if target_ids:
            placeholders = ",".join("?" for _ in target_ids)
            clauses.append(f"target_id IN ({placeholders})")
            params.extend(target_ids)
        if issue_type:
            clauses.append("issue_type = ?")
            params.append(issue_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"""
            SELECT *
            FROM quality_issues
            {where}
            ORDER BY issue_id
            LIMIT ?
            """,
            params + [clamp_limit(limit)],
        ).fetchall()
    issues = rows_to_dicts(rows)
    if not issues:
        return not_found_response("품질 이슈 조회 결과가 없습니다.")
    return tool_response(
        {"quality_issues": issues},
        audit={
            "data_sources": ["quality_issues"],
            "returned": len(issues),
            "generated_at": now_utc(),
        },
    )


def _legacy_compare_raw_refined(target_type: str, target_id: str) -> dict[str, Any]:
    """Compare raw and refined text for criteria or KSA targets."""
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


def _legacy_get_api_join_status(
    unit_code: str | None = None,
    classification_filter: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get API join status for competency units."""
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


def _legacy_get_sqf_duties(
    major_code: str | None = None,
    keyword: str | None = None,
    duty_level: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Get SQF duty profiles collected from /openapi26."""
    clauses: list[str] = []
    params: list[Any] = []
    exact_filter(clauses, params, "sd.ncs_lclas_cd", major_code)
    exact_filter(clauses, params, "sd.duty_level", duty_level)
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


def _legacy_search_sqf_jobs(
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
    return tool_response({
        "query": keyword,
        "major_code": major_code,
        "mvp_only": mvp_only,
        "sqf_jobs": jobs,
    })


def _legacy_get_sqf_job_level(
    source_key: str | None = None,
    job_name: str = MVP_JOB_NAME,
    duty_name: str | None = None,
    duty_level: str | None = None,
    include_mappings: bool = True,
    limit: int = 10,
) -> dict[str, Any]:
    """Get an SQF job/duty level with direct SQF evidence and NCS mapping candidates."""
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
    return tool_response({
        "target": {
            "source_key": source_key,
            "job_name": job_name,
            "duty_name": duty_name,
            "duty_level": duty_level,
        },
        "sqf_job_levels": result,
    })


def _legacy_build_sqf_ncs_mapping_candidates(
    mvp_only: bool = True,
    major_code: str | None = None,
    keyword: str | None = None,
    source_key: str | None = None,
    limit_per_duty: int = 10,
) -> dict[str, Any]:
    """Build and store SQF-NCS mapping candidates with evidence and review status."""
    with open_db() as conn:
        return ontology_build_sqf_mapping_candidates(
            conn,
            mvp_only=mvp_only,
            major_code=major_code,
            keyword=keyword,
            source_key=source_key,
            limit_per_duty=limit_per_duty,
        )


def _legacy_map_sqf_to_ncs(
    source_key: str,
    persist: bool = False,
    limit: int = 10,
) -> dict[str, Any]:
    """Map one SQF duty level to NCS competency unit candidates."""
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
            return tool_response({
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
            })
        candidates = generate_mapping_candidates(conn, duty, limit=max(limit, 50))
        filtered = apply_mapping_filter(candidates)
    return tool_response({
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
    })


def _legacy_analyze_gap(
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Analyze missing NCS units for a target SQF duty level."""
    with open_db() as conn:
        result = ontology_analyze_sqf_gap(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            mvp_only=target_job_name == MVP_JOB_NAME and target_source_key is None,
            limit=limit,
        )
    return tool_response(result)


def _legacy_recommend_next_ncs_units(
    current_ncs_unit_codes: list[str],
    target_source_key: str | None = None,
    target_job_name: str = MVP_JOB_NAME,
    target_duty_name: str | None = None,
    target_level: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Recommend next NCS units to close the gap toward an SQF duty level."""
    with open_db() as conn:
        result = ontology_recommend_next_ncs_units(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            limit=limit,
        )
    return tool_response(result)


def _legacy_explain_mapping(
    sqf_source_key: str,
    ncs_unit_code: str,
) -> dict[str, Any]:
    """Explain why one SQF duty level maps to one NCS competency unit."""
    with open_db() as conn:
        result = ontology_explain_mapping(
            conn,
            sqf_source_key=sqf_source_key,
            ncs_unit_code=ncs_unit_code,
        )
    return tool_response(result)


def related_units_for_sqf(conn, sqf_row, query: str, limit: int = 5) -> list[dict[str, Any]]:
    terms = [query, sqf_row["duty_name"], sqf_row["job_name"]]
    terms = [term for term in terms if term]
    params: list[Any] = [sqf_row["ncs_lclas_cd"]]
    term_clauses: list[str] = []
    for term in terms:
        pattern = f"%{term}%"
        term_clauses.append(
            """
            (
                cu.unit_name_raw LIKE ?
                OR cu.api_definition LIKE ?
                OR c.small_name LIKE ?
                OR c.sub_name LIKE ?
            )
            """
        )
        params.extend([pattern, pattern, pattern, pattern])
    where_terms = f"AND ({' OR '.join(term_clauses)})" if term_clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            cu.unit_code, cu.unit_name_raw AS unit_name,
            cu.unit_level_raw AS unit_level, cu.api_definition,
            c.major_code, c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        {where_terms}
        ORDER BY
            CASE WHEN cu.unit_name_raw LIKE ? THEN 0 ELSE 1 END,
            c.middle_code, c.small_code, c.sub_code, cu.unit_code
        LIMIT ?
        """,
        params + [f"%{sqf_row['duty_name']}%", clamp_limit(limit, default=5, maximum=20)],
    ).fetchall()
    if rows:
        return rows_to_dicts(rows)
    rows = conn.execute(
        """
        SELECT
            cu.unit_code, cu.unit_name_raw AS unit_name,
            cu.unit_level_raw AS unit_level, cu.api_definition,
            c.major_code, c.major_name, c.middle_name, c.small_name, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        ORDER BY c.middle_code, c.small_code, c.sub_code, cu.unit_code
        LIMIT ?
        """,
        (sqf_row["ncs_lclas_cd"], clamp_limit(limit, default=5, maximum=20)),
    ).fetchall()
    return rows_to_dicts(rows)


def _legacy_search_learning_modules(
    query: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached NCS learning modules collected from openapi21."""
    with open_db() as conn:
        modules = recommendation_search_learning_modules(
            conn,
            query=query,
            major_code=major_code,
            limit=limit,
        )
    return tool_response({
        "ok": True,
        "query": query,
        "major_code": major_code,
        "modules": modules,
        "audit": {
            "data_sources": ["ncs_learning_modules"],
            "returned": len(modules),
        },
    })


def _legacy_get_learning_module(learn_module_seq: str) -> dict[str, Any]:
    """Return one cached NCS learning module with unit and ontology concept links."""
    with open_db() as conn:
        result = recommendation_get_learning_module(conn, learn_module_seq)
    if "error" in result:
        return tool_response({"ok": False, **result})
    return tool_response({"ok": True, **result})


def resolve_ncs_query_scope(
    query: str,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Resolve a natural-language query against NCS hierarchy, tasks, and KSA concepts."""
    with open_db() as conn:
        result = training_resolve_ncs_query_scope(
            conn,
            query,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            limit=limit,
        )
    return tool_response(
        result,
        audit={
            "data_sources": [
                "classifications",
                "competency_units",
                "competency_elements",
                "performance_criteria",
                "ontology_concepts",
            ],
            "generated_at": now_utc(),
        },
    )


def search_training_courses(
    query: str | None = None,
    major_code: str | None = None,
    unit_code: str | None = None,
    concept_query: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached NCS training courses collected from openapi18."""
    with open_db() as conn:
        courses = training_search_courses(
            conn,
            query=query,
            major_code=major_code,
            unit_code=unit_code,
            concept_query=concept_query,
            limit=limit,
        )
    return tool_response(
        {
            "ok": True,
            "query": query,
            "major_code": major_code,
            "unit_code": unit_code,
            "concept_query": concept_query,
            "training_courses": courses,
        },
        audit={
            "data_sources": [
                "ncs_training_courses",
                "ncs_training_course_unit_links",
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
            ],
            "returned": len(courses),
            "generated_at": now_utc(),
            "sqf_used": False,
            "learning_modules_used": False,
        },
    )


def get_training_course(training_course_id: int) -> dict[str, Any]:
    """Return one cached NCS training course with NCS unit and KSA concept links."""
    with open_db() as conn:
        result = training_get_course(conn, training_course_id)
    return tool_response(result)


def build_training_course_ontology_links(
    major_code: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    """Build links from NCS training courses to KSA ontology concepts."""
    with open_db() as conn:
        result = training_build_course_links(conn, major_code=major_code, reset=reset)
    return tool_response(
        {"ok": True, **result},
        audit={
            "data_sources": [
                "ncs_training_courses",
                "ncs_training_course_unit_links",
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
                "ontology_concepts",
            ],
            "generated_at": now_utc(),
        },
    )


def _legacy_prepare_ontology_review_queue(
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
    """Create all-domain review issues for weak ontology concepts, goal links, and KSA relations."""
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


def import_career_paths(
    csv_path: str,
    encoding: str = "cp949",
    reset: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Import an NCS career development path CSV and link rows to NCS classifications and units."""
    with open_db() as conn:
        result = ncs_import_career_paths_csv(
            conn,
            csv_path,
            encoding=encoding,
            reset=reset,
            limit=limit,
        )
    return tool_response(
        result,
        audit={
            "data_sources": ["NCS career development path CSV", "ncs_career_paths"],
            "generated_at": now_utc(),
        },
    )


def get_career_path_summary(limit: int = 20) -> dict[str, Any]:
    """Summarize imported NCS career development paths and unmatched competency rows."""
    with open_db() as conn:
        result = ncs_career_path_summary(conn, limit=limit)
    return tool_response(
        result,
        audit={
            "data_sources": ["ncs_career_paths"],
            "generated_at": now_utc(),
        },
    )


def query_qualification_items(
    unit_code: str,
    page_no: int = 1,
    num_of_rows: int = 10,
    timeout: int = 30,
) -> dict[str, Any]:
    """Query live qualification items linked to one NCS competency unit code."""
    settings = load_settings()
    if not settings.qualification_service_key:
        return error_response("qualification_service_key_missing")
    result = qualification_fetch_page(
        settings.qualification_service_key,
        unit_code=unit_code,
        page_no=page_no,
        num_of_rows=num_of_rows,
        timeout=timeout,
    )
    return tool_response(
        result,
        audit={
            "data_sources": ["ncsClCdJm/getNcsClCdJmList"],
            "generated_at": now_utc(),
        },
    )


def _legacy_collect_qualification_items(
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
    """Collect qualification item links into SQLite for one unit, a major code, or all units."""
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


def get_qualification_error_report(limit: int = 50) -> dict[str, Any]:
    """Report cached NCS qualification API collection errors by status, major, and sample unit."""
    with open_db() as conn:
        result = qualification_cached_error_report(conn, limit=limit)
    return tool_response(
        result,
        audit={
            "data_sources": ["ncs_qualification_collection_status", "competency_units", "classifications"],
            "generated_at": now_utc(),
        },
    )


def retry_qualification_errors(
    major_code: str | None = None,
    limit_units: int | None = None,
    page_no: int = 1,
    num_of_rows: int = 50,
    max_pages: int | None = None,
    timeout: int = 30,
    request_delay: float = 1.0,
    max_retries: int = 5,
    retry_backoff_seconds: float = 5.0,
    retry_ready_only: bool = True,
) -> dict[str, Any]:
    """Retry qualification API error units, respecting next_retry_at by default."""
    settings = load_settings()
    if not settings.qualification_service_key:
        return error_response("qualification_service_key_missing")
    result = qualification_retry_error_units(
        settings.db_path,
        settings.qualification_service_key,
        major_code=major_code,
        limit_units=limit_units,
        page_no=page_no,
        num_of_rows=num_of_rows,
        max_pages=max_pages,
        timeout=timeout,
        request_delay=request_delay,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        retry_ready_only=retry_ready_only,
    )
    return tool_response(
        result,
        audit={
            "data_sources": [
                "ncsClCdJm/getNcsClCdJmList",
                "ncs_qualification_collection_status",
                "ncs_qualification_items",
                "ncs_unit_qualification_links",
            ],
            "generated_at": now_utc(),
        },
    )


def search_qualification_items(
    unit_code: str | None = None,
    qualification_name: str | None = None,
    qualification_code: str | None = None,
    unit_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached qualification items linked to NCS competency units."""
    with open_db() as conn:
        links = qualification_search_links(
            conn,
            unit_code=unit_code,
            qualification_name=qualification_name,
            qualification_code=qualification_code,
            unit_type=unit_type,
            limit=limit,
        )
        summary = qualification_cached_summary(conn, limit=10)
    return tool_response(
        {
            "qualification_links": links,
            "summary": summary,
        },
        audit={
            "data_sources": ["ncs_qualification_items", "ncs_unit_qualification_links"],
            "generated_at": now_utc(),
        },
    )


def query_job_base_competencies(
    major_code: str = "02",
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 10,
    timeout: int = 30,
) -> dict[str, Any]:
    """Query live NCS job base competencies by major code and optional unit-name keyword."""
    settings = load_settings()
    if not settings.job_base_service_key:
        return error_response("job_base_service_key_missing")
    result = job_base_fetch_page(
        settings.job_base_service_key,
        major_code=major_code,
        module_name=module_name,
        page_no=page_no,
        num_of_rows=num_of_rows,
        timeout=timeout,
    )
    return tool_response(
        result,
        audit={
            "data_sources": ["ncsJobBase/openapi19"],
            "generated_at": now_utc(),
        },
    )


def _legacy_collect_job_base_competencies(
    major_code: str = "02",
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 500,
    max_pages: int | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    """Collect NCS job base competencies into SQLite."""
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


def search_job_base_competencies(
    unit_code: str | None = None,
    competency_name: str | None = None,
    factor_name: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached NCS job base competencies linked to NCS competency units."""
    with open_db() as conn:
        links = job_base_search_links(
            conn,
            unit_code=unit_code,
            competency_name=competency_name,
            factor_name=factor_name,
            major_code=major_code,
            limit=limit,
        )
        summary = job_base_cached_summary(conn, limit=10)
    return tool_response(
        {
            "job_base_links": links,
            "summary": summary,
        },
        audit={
            "data_sources": [
                "ncs_job_base_competencies",
                "ncs_job_base_factors",
                "ncs_unit_job_base_links",
            ],
            "generated_at": now_utc(),
        },
    )


@mcp.tool()
def recommend_training_for_task(
    criteria_id: int | None = None,
    unit_code: str | None = None,
    query: str | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    mode: str = "all",
    current_concepts: list[str] | None = None,
    preferred_max_hours: float | None = None,
    preferred_methods: list[str] | None = None,
    limit: int = 5,
    save: bool = True,
    compact: bool = False,
) -> dict[str, Any]:
    """Recommend NCS training courses from a task's KSA ontology and task transitions."""
    with open_db() as conn:
        result = training_recommend_for_task(
            conn,
            criteria_id=criteria_id,
            unit_code=unit_code,
            query=query,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            mode=mode,
            current_concepts=current_concepts,
            preferred_max_hours=preferred_max_hours,
            preferred_methods=preferred_methods,
            limit=limit,
            save=save,
        )
    if compact:
        result = training_compact_task_response(result, recommendation_limit=limit)
    if result.get("ok"):
        result.setdefault("disclaimer", DISCLAIMER)
    return tool_response(result)


@mcp.tool()
def recommend_training_transition(
    current_query: str,
    target_query: str,
    major_code: str | None = None,
    current_major_code: str | None = None,
    target_major_code: str | None = None,
    current_middle_code: str | None = None,
    target_middle_code: str | None = None,
    current_small_code: str | None = None,
    target_small_code: str | None = None,
    current_sub_code: str | None = None,
    target_sub_code: str | None = None,
    mode: str = "all",
    preferred_max_hours: float | None = None,
    preferred_methods: list[str] | None = None,
    limit: int = 5,
    save: bool = True,
    compact: bool = False,
) -> dict[str, Any]:
    """Recommend training for moving from one NCS scope to another using KSA gap analysis."""
    with open_db() as conn:
        result = training_recommend_transition(
            conn,
            current_query=current_query,
            target_query=target_query,
            major_code=major_code,
            current_major_code=current_major_code,
            target_major_code=target_major_code,
            current_middle_code=current_middle_code,
            target_middle_code=target_middle_code,
            current_small_code=current_small_code,
            target_small_code=target_small_code,
            current_sub_code=current_sub_code,
            target_sub_code=target_sub_code,
            mode=mode,
            preferred_max_hours=preferred_max_hours,
            preferred_methods=preferred_methods,
            limit=limit,
            save=save,
        )
    if compact:
        result = training_compact_transition_response(result, recommendation_limit=limit)
    if result.get("ok"):
        result.setdefault("disclaimer", DISCLAIMER)
    return tool_response(result)


def _legacy_get_learning_path_for_sqf_job(
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    current_concepts: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Build a staged learning path for SQF job levels using trusted mappings and modules."""
    with open_db() as conn:
        result = recommendation_get_learning_path(
            conn,
            query=query,
            major_code=major_code,
            target_source_key=target_source_key,
            target_level=target_level,
            current_concepts=current_concepts,
            limit=limit,
        )
    return tool_response(result)


def _legacy_recommend_education_for_duty(
    query: str,
    major_code: str | None = None,
    target_source_key: str | None = None,
    target_level: str | None = None,
    current_concepts: list[str] | None = None,
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    """Recommend education with SQF, trusted NCS mappings, KSA concepts, and learning modules."""
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


def _legacy_explain_education_recommendation(
    recommendation_item_id: int | None = None,
    recommendation_run_id: int | None = None,
    rank: int | None = None,
) -> dict[str, Any]:
    """Explain a saved education recommendation item from its audit evidence chain."""
    with open_db() as conn:
        result = recommendation_explain_education(
            conn,
            recommendation_item_id=recommendation_item_id,
            recommendation_run_id=recommendation_run_id,
            rank=rank,
        )
    return tool_response(result)


def _legacy_import_ncs_reference_html(
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
    """Import report.html-style NCS reference evidence into page/chunk tables."""
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


def search_ncs_reference_chunks(
    query: str,
    document_id: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search imported NCS reference chunks and return summaries with page/chunk locations."""
    with open_db() as conn:
        chunks = ncs_ref_search_chunks(
            conn,
            query=query,
            document_id=document_id,
            limit=limit,
        )
    return tool_response(
        {"ok": True, "query": query, "document_id": document_id, "chunks": chunks},
        audit={
            "data_sources": ["ncs_reference_chunks"],
            "returned": len(chunks),
            "generated_at": now_utc(),
        },
    )


def _legacy_import_ncs_reference_docx(
    input_path: str,
    title: str,
    chunk_min_chars: int = 500,
    chunk_max_chars: int = 1200,
) -> dict[str, Any]:
    """Import a DOCX NCS/API reference document into page/chunk tables."""
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


def _legacy_extract_ncs_reference_entities(
    document_id: int | None = None,
    limit_chunks: int | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Extract NCS unit, element, criteria, KSA, and training-standard candidates from imported chunks."""
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


def _legacy_link_reference_entities_to_ncs(
    document_id: int | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Link extracted NCS reference entities to canonical NCS tables as candidate evidence."""
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


def _legacy_recommend_learning_modules_by_ncs(
    query: str | None = None,
    unit_code: str | None = None,
    major_code: str | None = "02",
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    sub_codes: list[str] | None = None,
    trust_mode: str = "trusted",
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    """Recommend learning modules directly from an NCS unit/query using trusted module-NCS links."""
    with open_db() as conn:
        result = ncs_ref_recommend_modules(
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
            save=save,
        )
    return tool_response(result)


def _legacy_review_exact_learning_module_name_links(
    major_code: str | None = "02",
    middle_code: str | None = "02",
    small_code: str | None = "02",
    sub_codes: list[str] | None = None,
    reviewer_id: str = "mcp",
) -> dict[str, Any]:
    """Mark exact learning-module-name to NCS-unit-name links as reviewed within a scoped MVP."""
    with open_db() as conn:
        result = ncs_ref_review_exact_module_links(
            conn,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_codes=sub_codes or ["01", "02"],
            reviewer_id=reviewer_id,
        )
    return tool_response(result)


def _legacy_build_ncs_derived_learning_plans(
    major_code: str | None = "02",
    middle_code: str | None = "02",
    small_code: str | None = "02",
    sub_code: str | None = None,
    sub_codes: list[str] | None = None,
    review_status: str = "reviewed",
) -> dict[str, Any]:
    """Create trusted NCS-derived education-plan rows for units without official study-module links."""
    with open_db() as conn:
        result = ncs_ref_build_derived_plans(
            conn,
            major_code=major_code,
            middle_code=middle_code,
            small_code=small_code,
            sub_code=sub_code,
            sub_codes=sub_codes,
            review_status=review_status,
        )
    return tool_response(result)


def _legacy_build_report_training_courses(
    document_id: int | None = None,
    major_code: str = "02",
    middle_code: str = "02",
    small_code: str = "02",
    sub_code: str | None = None,
    sub_codes: list[str] | None = None,
    review_status: str = "reviewed",
) -> dict[str, Any]:
    """Extract report education/training course candidates and link them to NCS ontology concepts."""
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
        )
    return tool_response(result)


def _legacy_recommend_education_by_concepts(
    concepts: list[str] | None = None,
    query: str | None = None,
    trust_mode: str = "trusted",
    limit: int = 5,
    save: bool = True,
) -> dict[str, Any]:
    """Recommend learning modules or NCS-derived plans from ontology concept names."""
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


@mcp.tool()
def recommend_task_transitions(
    criteria_id: int | None = None,
    query: str | None = None,
    unit_code: str | None = None,
    mode: str = "all",
    limit: int = 10,
    evidence_limit: int = 12,
) -> dict[str, Any]:
    """Recommend nearby NCS tasks for upskilling/reskilling from atomic KSA ontology links."""
    with open_db() as conn:
        result = db_recommend_task_transitions(
            conn,
            criteria_id=criteria_id,
            query=query,
            unit_code=unit_code,
            mode=mode,
            limit=limit,
            evidence_limit=evidence_limit,
        )
    return tool_response(
        result,
        audit={
            "data_sources": [
                "task_similarity_links",
                "task_ksa_concept_relations",
                "ksa_atomic_items",
                "ontology_concepts",
            ],
            "generated_at": now_utc(),
        },
    )


def insert_review_audit(
    conn,
    *,
    entity_type: str,
    entity_id: str,
    action: str,
    previous_status: str | None,
    new_status: str | None,
    reviewer_id: str | None,
    notes: str | None,
) -> None:
    conn.execute(
        """
        INSERT INTO review_audit_log(
            entity_type, entity_id, action, previous_status,
            new_status, reviewer_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_type,
            entity_id,
            action,
            previous_status,
            new_status,
            reviewer_id,
            notes,
            now_utc(),
        ),
    )


REVIEWABLE_LINK_STATUSES = {
    "human_reviewed",
    "reviewed",
    "accepted",
    "rejected",
    "candidate",
    "candidate_auto",
    "auto_linked",
    "review_required",
}


def normalize_review_link_status(review_status: str) -> str | None:
    status = " ".join(str(review_status or "").strip().split())
    return status if status in REVIEWABLE_LINK_STATUSES else None


def normalize_optional_confidence(confidence_score: float | None) -> float | None:
    if confidence_score is None:
        return None
    value = float(confidence_score)
    if value < 0 or value > 1:
        raise ValueError("confidence_score must be between 0 and 1")
    return value


def concept_with_aliases(conn, concept_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT *
        FROM ontology_concepts
        WHERE concept_id = ?
        """,
        (concept_id,),
    ).fetchone()
    if row is None:
        return None
    aliases = conn.execute(
        """
        SELECT alias_id, alias_text, normalized_alias_key, alias_source, created_at
        FROM ontology_concept_aliases
        WHERE concept_id = ?
        ORDER BY alias_text
        """,
        (concept_id,),
    ).fetchall()
    return {**row_to_dict(row), "aliases": rows_to_dicts(aliases)}


def search_ontology_concepts(
    query: str | None = None,
    concept_type: str | None = None,
    definition_status: str | None = None,
    review_status: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search ontology concept nodes with alias and evidence counts."""
    clauses: list[str] = []
    params: list[Any] = []
    if concept_type:
        clauses.append("oc.concept_type = ?")
        params.append(concept_type)
    if definition_status:
        clauses.append("oc.definition_status = ?")
        params.append(definition_status)
    if review_status:
        clauses.append("oc.review_status = ?")
        params.append(review_status)
    if query:
        like = f"%{query}%"
        clauses.append(
            """
            (
                oc.concept_name LIKE ?
                OR oc.definition LIKE ?
                OR EXISTS (
                    SELECT 1
                    FROM ontology_concept_aliases alias
                    WHERE alias.concept_id = oc.concept_id
                      AND alias.alias_text LIKE ?
                )
            )
            """
        )
        params.extend([like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT
                oc.concept_id, oc.concept_name, oc.normalized_key,
                oc.concept_type, oc.definition, oc.definition_status,
                oc.relation_status, oc.review_status,
                COUNT(DISTINCT alias.alias_id) AS alias_count,
                COUNT(DISTINCT rel.relation_id) AS relation_count,
                COUNT(DISTINCT kcl.ksa_id) AS ksa_link_count,
                COUNT(DISTINCT ccl.criteria_id) AS criteria_link_count,
                COUNT(DISTINCT lmcl.learn_module_seq) AS learning_module_count
            FROM ontology_concepts oc
            LEFT JOIN ontology_concept_aliases alias ON alias.concept_id = oc.concept_id
            LEFT JOIN ontology_concept_relations rel ON rel.source_concept_id = oc.concept_id
            LEFT JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
            LEFT JOIN criteria_concept_links ccl ON ccl.concept_id = oc.concept_id
            LEFT JOIN learning_module_concept_links lmcl ON lmcl.concept_id = oc.concept_id
            {where}
            GROUP BY oc.concept_id
            ORDER BY oc.review_status, oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            params + [clamp_limit(limit, default=20, maximum=100)],
        ).fetchall()
    return tool_response(
        {
            "query": query,
            "concept_type": concept_type,
            "concepts": rows_to_dicts(rows),
        },
        audit={
            "data_sources": [
                "ontology_concepts",
                "ontology_concept_aliases",
                "ksa_concept_links",
                "criteria_concept_links",
                "learning_module_concept_links",
            ],
            "generated_at": now_utc(),
        },
    )


@mcp.tool()
def get_concept_evidence(concept_id: int, limit: int = 20) -> dict[str, Any]:
    """Return source KSA, criteria, learning-module, relation, and recommendation evidence for a concept."""
    max_rows = clamp_limit(limit, default=20, maximum=100)
    with open_db() as conn:
        concept = concept_with_aliases(conn, concept_id)
        if concept is None:
            return error_response("concept_not_found", concept_id=concept_id)
        outgoing = conn.execute(
            """
            SELECT
                rel.relation_id, rel.relation_type, rel.relation_label,
                rel.review_status, target.concept_id AS target_concept_id,
                target.concept_name AS target_concept_name,
                target.concept_type AS target_concept_type
            FROM ontology_concept_relations rel
            JOIN ontology_concepts target ON target.concept_id = rel.target_concept_id
            WHERE rel.source_concept_id = ?
            ORDER BY rel.relation_type, target.concept_name
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
        incoming = conn.execute(
            """
            SELECT
                rel.relation_id, rel.relation_type, rel.relation_label,
                rel.review_status, source.concept_id AS source_concept_id,
                source.concept_name AS source_concept_name,
                source.concept_type AS source_concept_type
            FROM ontology_concept_relations rel
            JOIN ontology_concepts source ON source.concept_id = rel.source_concept_id
            WHERE rel.target_concept_id = ?
            ORDER BY rel.relation_type, source.concept_name
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
        ksa_rows = conn.execute(
            """
            SELECT
                kcl.link_id, kcl.link_status, ki.ksa_id, ki.ksa_type_name,
                ki.ksa_no, ki.ksa_text_raw, ki.ksa_text_refined,
                ce.element_id, ce.element_name_raw, ce.unit_code,
                cu.unit_name_raw, c.major_code, c.major_name,
                c.middle_code, c.middle_name, c.small_code, c.small_name,
                c.sub_code, c.sub_name
            FROM ksa_concept_links kcl
            JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE kcl.concept_id = ?
            ORDER BY ce.unit_code, ce.element_id, ki.ksa_type_code, ki.ksa_id
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
        criteria_rows = conn.execute(
            """
            SELECT
                ccl.link_id, ccl.relation_type, ccl.link_status,
                pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                pc.criteria_text_refined, ce.element_id, ce.element_name_raw,
                ce.unit_code, cu.unit_name_raw
            FROM criteria_concept_links ccl
            JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ccl.concept_id = ?
            ORDER BY ce.unit_code, ce.element_id, pc.criteria_id
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
        module_rows = conn.execute(
            """
            SELECT
                lmcl.link_id, lmcl.link_method, lmcl.confidence_score,
                lm.learn_module_seq, lm.learn_module_name,
                lm.ncs_lclas_cd, lm.ncs_lclas_name,
                lm.ncs_mclas_cd, lm.ncs_mclas_name,
                lm.ncs_sclas_cd, lm.ncs_sclas_name,
                lm.ncs_subd_cd, lm.ncs_subd_name
            FROM learning_module_concept_links lmcl
            JOIN ncs_learning_modules lm ON lm.learn_module_seq = lmcl.learn_module_seq
            WHERE lmcl.concept_id = ?
            ORDER BY lmcl.confidence_score DESC, lm.learn_module_seq
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
        recommendation_rows = conn.execute(
            """
            SELECT
                e.evidence_id, e.run_id, e.item_id, e.evidence_type,
                e.source_table, e.source_id, e.evidence_summary,
                e.confidence_score, i.rank, i.learn_module_seq,
                i.learn_module_name, r.query, r.created_at AS run_created_at
            FROM education_recommendation_evidence e
            JOIN education_recommendation_items i ON i.item_id = e.item_id
            JOIN education_recommendation_runs r ON r.run_id = e.run_id
            WHERE e.concept_id = ?
            ORDER BY e.run_id DESC, i.rank, e.evidence_id
            LIMIT ?
            """,
            (concept_id, max_rows),
        ).fetchall()
    return tool_response(
        {
            "concept": concept,
            "relations": {
                "outgoing": rows_to_dicts(outgoing),
                "incoming": rows_to_dicts(incoming),
            },
            "source_ksa": rows_to_dicts(ksa_rows),
            "source_criteria": rows_to_dicts(criteria_rows),
            "learning_modules": rows_to_dicts(module_rows),
            "recommendation_evidence": rows_to_dicts(recommendation_rows),
        },
        audit={
            "data_sources": [
                "ontology_concepts",
                "ontology_concept_relations",
                "ksa_items",
                "performance_criteria",
                "ncs_learning_modules",
                "education_recommendation_evidence",
            ],
            "generated_at": now_utc(),
        },
    )


def _legacy_review_sqf_ncs_match(
    match_id: int,
    new_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    relation: str | None = None,
) -> dict[str, Any]:
    """Human-review one SQF-NCS mapping and record recommendation eligibility audit."""
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
        insert_review_audit(
            conn,
            entity_type="sqf_ncs_match",
            entity_id=str(match_id),
            action="review_sqf_ncs_match",
            previous_status=row["review_status"],
            new_status=status,
            reviewer_id=reviewer_id,
            notes=notes,
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


@mcp.tool()
def review_learning_module_ncs_link(
    link_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    """Human-review one learning-module to NCS unit link for trusted recommendations."""
    with open_db() as conn:
        result = ncs_ref_review_module_link(
            conn,
            link_id=link_id,
            review_status=review_status,
            reviewer_id=reviewer_id,
            notes=notes,
            confidence_score=confidence_score,
        )
    return tool_response(result)


@mcp.tool()
def review_training_goal_concept_link(
    link_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    """Human-review one training-goal to KSA concept link used by ranking."""
    status = normalize_review_link_status(review_status)
    if status is None:
        return error_response(
            "unsupported_review_status",
            review_status=review_status,
            allowed=sorted(REVIEWABLE_LINK_STATUSES),
        )
    try:
        confidence = normalize_optional_confidence(confidence_score)
    except ValueError as exc:
        return error_response("invalid_tool_parameters", message=str(exc))
    with open_db() as conn:
        row = conn.execute(
            """
            SELECT l.*, c.compe_unit_name AS course_name, oc.concept_name, oc.concept_type
            FROM training_goal_concept_links l
            JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id
            JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
            WHERE l.link_id = ?
            """,
            (link_id,),
        ).fetchone()
        if row is None:
            return error_response("training_goal_concept_link_not_found", link_id=link_id)
        timestamp = now_utc()
        if confidence is None:
            conn.execute(
                """
                UPDATE training_goal_concept_links
                SET review_status = ?,
                    updated_at = ?
                WHERE link_id = ?
                """,
                (status, timestamp, link_id),
            )
        else:
            conn.execute(
                """
                UPDATE training_goal_concept_links
                SET review_status = ?,
                    confidence_score = ?,
                    updated_at = ?
                WHERE link_id = ?
                """,
                (status, confidence, timestamp, link_id),
            )
        insert_review_audit(
            conn,
            entity_type="training_goal_concept_link",
            entity_id=str(link_id),
            action="review_training_goal_concept_link",
            previous_status=row["review_status"],
            new_status=status,
            reviewer_id=reviewer_id,
            notes=notes,
        )
        updated = conn.execute(
            """
            SELECT l.*, c.compe_unit_name AS course_name, oc.concept_name, oc.concept_type
            FROM training_goal_concept_links l
            JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id
            JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
            WHERE l.link_id = ?
            """,
            (link_id,),
        ).fetchone()
        conn.commit()
    return tool_response(
        {
            "link_id": link_id,
            "previous_status": row["review_status"],
            "new_status": status,
            "link_usable_for_recommendation": status != "rejected",
            "trusted_for_recommendation": status in {"human_reviewed", "reviewed", "accepted"},
            "link": row_to_dict(updated),
        },
        audit={
            "data_sources": ["training_goal_concept_links", "review_audit_log"],
            "generated_at": now_utc(),
            "reviewer_id": reviewer_id,
        },
    )


@mcp.tool()
def review_task_ksa_concept_relation(
    relation_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
) -> dict[str, Any]:
    """Human-review one task-KSA concept relation used by transition reasoning."""
    status = normalize_review_link_status(review_status)
    if status is None:
        return error_response(
            "unsupported_review_status",
            review_status=review_status,
            allowed=sorted(REVIEWABLE_LINK_STATUSES),
        )
    try:
        confidence = normalize_optional_confidence(confidence_score)
    except ValueError as exc:
        return error_response("invalid_tool_parameters", message=str(exc))
    with open_db() as conn:
        row = conn.execute(
            """
            SELECT r.*,
                   sc.concept_name AS source_concept_name,
                   tc.concept_name AS target_concept_name,
                   pc.criteria_text_raw,
                   ce.unit_code,
                   ce.element_name_raw
            FROM task_ksa_concept_relations r
            JOIN ontology_concepts sc ON sc.concept_id = r.source_concept_id
            JOIN ontology_concepts tc ON tc.concept_id = r.target_concept_id
            JOIN performance_criteria pc ON pc.criteria_id = r.criteria_id
            JOIN competency_elements ce ON ce.element_id = r.element_id
            WHERE r.relation_id = ?
            """,
            (relation_id,),
        ).fetchone()
        if row is None:
            return error_response("task_ksa_concept_relation_not_found", relation_id=relation_id)
        if confidence is None:
            conn.execute(
                """
                UPDATE task_ksa_concept_relations
                SET review_status = ?
                WHERE relation_id = ?
                """,
                (status, relation_id),
            )
        else:
            conn.execute(
                """
                UPDATE task_ksa_concept_relations
                SET review_status = ?,
                    confidence_score = ?
                WHERE relation_id = ?
                """,
                (status, confidence, relation_id),
            )
        insert_review_audit(
            conn,
            entity_type="task_ksa_concept_relation",
            entity_id=str(relation_id),
            action="review_task_ksa_concept_relation",
            previous_status=row["review_status"],
            new_status=status,
            reviewer_id=reviewer_id,
            notes=notes,
        )
        updated = conn.execute(
            """
            SELECT r.*,
                   sc.concept_name AS source_concept_name,
                   tc.concept_name AS target_concept_name,
                   pc.criteria_text_raw,
                   ce.unit_code,
                   ce.element_name_raw
            FROM task_ksa_concept_relations r
            JOIN ontology_concepts sc ON sc.concept_id = r.source_concept_id
            JOIN ontology_concepts tc ON tc.concept_id = r.target_concept_id
            JOIN performance_criteria pc ON pc.criteria_id = r.criteria_id
            JOIN competency_elements ce ON ce.element_id = r.element_id
            WHERE r.relation_id = ?
            """,
            (relation_id,),
        ).fetchone()
        conn.commit()
    return tool_response(
        {
            "relation_id": relation_id,
            "previous_status": row["review_status"],
            "new_status": status,
            "relation_usable_for_transition": status != "rejected",
            "trusted_for_transition": status in {"human_reviewed", "reviewed", "accepted"},
            "relation": row_to_dict(updated),
        },
        audit={
            "data_sources": ["task_ksa_concept_relations", "review_audit_log"],
            "generated_at": now_utc(),
            "reviewer_id": reviewer_id,
        },
    )


@mcp.tool()
def review_ontology_concept(
    concept_id: int,
    concept_name: str | None = None,
    concept_type: str | None = None,
    definition: str | None = None,
    definition_status: str | None = None,
    relation_status: str | None = None,
    review_status: str = "human_reviewed",
    aliases: list[str] | None = None,
    reviewer_id: str = "mcp",
    notes: str = "",
) -> dict[str, Any]:
    """Human-review an ontology concept without mutating raw KSA source text."""
    with open_db() as conn:
        row = conn.execute("SELECT * FROM ontology_concepts WHERE concept_id = ?", (concept_id,)).fetchone()
        if row is None:
            return error_response("concept_not_found", concept_id=concept_id)
        target_type = (concept_type or row["concept_type"]).strip()
        if target_type not in {"knowledge", "skill", "attitude"}:
            return error_response("unsupported_concept_type", concept_type=target_type)
        target_name = (concept_name if concept_name is not None else row["concept_name"]).strip()
        if not target_name:
            return error_response("concept_name_required", concept_id=concept_id)
        normalized_key = normalize_concept_key(target_name)
        duplicate = conn.execute(
            """
            SELECT concept_id
            FROM ontology_concepts
            WHERE concept_type = ?
              AND normalized_key = ?
              AND concept_id <> ?
            """,
            (target_type, normalized_key, concept_id),
        ).fetchone()
        if duplicate is not None:
            return error_response(
                "duplicate_concept",
                concept_id=concept_id,
                duplicate_concept_id=duplicate["concept_id"],
            )
        definition_value = definition.strip() if definition is not None else row["definition"]
        if definition_status is None and definition is not None:
            definition_status = "defined" if definition_value else "missing"
        timestamp = now_utc()
        conn.execute(
            """
            UPDATE ontology_concepts
            SET concept_name = ?,
                normalized_key = ?,
                concept_type = ?,
                definition = ?,
                definition_source = ?,
                definition_status = ?,
                relation_status = ?,
                review_status = ?,
                updated_at = ?
            WHERE concept_id = ?
            """,
            (
                target_name,
                normalized_key,
                target_type,
                definition_value or None,
                "manual" if definition is not None else row["definition_source"],
                definition_status or row["definition_status"],
                relation_status or row["relation_status"],
                review_status or row["review_status"],
                timestamp,
                concept_id,
            ),
        )
        for alias in aliases or []:
            alias_text = " ".join(str(alias).strip().split())
            if not alias_text:
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO ontology_concept_aliases(
                    concept_id, alias_text, normalized_alias_key, alias_source, created_at
                ) VALUES (?, ?, ?, 'manual', ?)
                """,
                (concept_id, alias_text, normalize_concept_key(alias_text), timestamp),
            )
        insert_review_audit(
            conn,
            entity_type="ontology_concept",
            entity_id=str(concept_id),
            action="review_ontology_concept",
            previous_status=row["review_status"],
            new_status=review_status,
            reviewer_id=reviewer_id,
            notes=notes,
        )
        updated = concept_with_aliases(conn, concept_id)
        conn.commit()
    return tool_response(
        {
            "concept_id": concept_id,
            "previous_status": row["review_status"],
            "new_status": review_status,
            "concept": updated,
        },
        audit={
            "data_sources": ["ontology_concepts", "ontology_concept_aliases", "review_audit_log"],
            "generated_at": now_utc(),
            "reviewer_id": reviewer_id,
        },
    )


def _legacy_get_sqf_ontology_summary() -> dict[str, Any]:
    """Return counts for the normalized SQF ontology and preprocessed document layer."""
    return tool_response(sqf_model_summary(load_settings().db_path))


def _legacy_search_sqf_document_chunks(
    query: str,
    ontology_tag: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
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
    return tool_response({
        "query": query,
        "ontology_tag": ontology_tag,
        "chunks": [
            {
                **dict(row),
                "document_title": row["title"],
                "asset_filename": row["asset_name"],
                "chunk_text_summary": row["snippet"],
                "evidence_relation": "document_candidate",
            }
            for row in rows
        ],
        "note": "Chunks are extracted evidence from SQF library files, not official recognition decisions.",
    }, audit={
        "data_sources": ["sqf_document_chunks", "sqf_document_assets", "sqf_document_sources"],
        "generated_at": now_utc(),
    })


def _legacy_search_sqf_precision_matches(
    query: str | None = None,
    source_key: str | None = None,
    min_score: float = 9.0,
    limit: int = 20,
) -> dict[str, Any]:
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
    return tool_response({
        "query": query,
        "source_key": source_key,
        "min_score": min_score,
        "matches": rows_to_dicts(rows),
        "note": "These are candidate evidence links from report OCR/text chunks, not official recognition decisions.",
    }, audit={
        "data_sources": [
            "sqf_chunk_job_level_matches",
            "sqf_document_chunks",
            "sqf_job_levels_normalized",
        ],
        "generated_at": now_utc(),
    })


def _legacy_get_sqf_ontology_job_level(source_key: str) -> dict[str, Any]:
    """Return normalized SQF job-level ontology node, recognition evidence, and document links."""
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
            return error_response("sqf_job_level_not_found", source_key=source_key)
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
    return tool_response({
        "job_level": row_to_dict(job_level),
        "recognition_evidence": rows_to_dicts(evidence),
        "document_links": rows_to_dicts(document_links),
        "document_chunk_matches": rows_to_dicts(chunk_matches),
    })


# Backward-compatible direct-call aliases. These names are intentionally not
# decorated as MCP tools.
compare_raw_refined = _legacy_compare_raw_refined
get_api_join_status = _legacy_get_api_join_status
get_sqf_duties = _legacy_get_sqf_duties
search_sqf_jobs = _legacy_search_sqf_jobs
get_sqf_job_level = _legacy_get_sqf_job_level
build_sqf_ncs_mapping_candidates = _legacy_build_sqf_ncs_mapping_candidates
map_sqf_to_ncs = _legacy_map_sqf_to_ncs
analyze_gap = _legacy_analyze_gap
recommend_next_ncs_units = _legacy_recommend_next_ncs_units
explain_mapping = _legacy_explain_mapping
search_learning_modules = _legacy_search_learning_modules
get_learning_module = _legacy_get_learning_module
prepare_ontology_review_queue = _legacy_prepare_ontology_review_queue
collect_qualification_items = _legacy_collect_qualification_items
collect_job_base_competencies = _legacy_collect_job_base_competencies
get_learning_path_for_sqf_job = _legacy_get_learning_path_for_sqf_job
recommend_education_for_duty = _legacy_recommend_education_for_duty
explain_education_recommendation = _legacy_explain_education_recommendation
import_ncs_reference_html = _legacy_import_ncs_reference_html
import_ncs_reference_docx = _legacy_import_ncs_reference_docx
extract_ncs_reference_entities = _legacy_extract_ncs_reference_entities
link_reference_entities_to_ncs = _legacy_link_reference_entities_to_ncs
recommend_learning_modules_by_ncs = _legacy_recommend_learning_modules_by_ncs
review_exact_learning_module_name_links = _legacy_review_exact_learning_module_name_links
build_ncs_derived_learning_plans = _legacy_build_ncs_derived_learning_plans
build_report_training_courses = _legacy_build_report_training_courses
recommend_education_by_concepts = _legacy_recommend_education_by_concepts
review_sqf_ncs_match = _legacy_review_sqf_ncs_match
get_sqf_ontology_summary = _legacy_get_sqf_ontology_summary
search_sqf_document_chunks = _legacy_search_sqf_document_chunks
search_sqf_precision_matches = _legacy_search_sqf_precision_matches
get_sqf_ontology_job_level = _legacy_get_sqf_ontology_job_level


NCS_EXECUTABLE_TOOL_HANDLERS = {
    "ncs_search": ncs_search,
    "ncs_unit_detail": ncs_unit_detail,
    "ncs_training": ncs_training,
    "ncs_analysis": ncs_analysis,
    "recommend_training_for_task": recommend_training_for_task,
    "recommend_training_transition": recommend_training_transition,
    "recommend_task_transitions": recommend_task_transitions,
    "get_concept_evidence": get_concept_evidence,
}


def current_mcp_tool_surface() -> dict[str, Any]:
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    names = sorted(tools) if isinstance(tools, dict) else []
    name_set = set(names)
    operator_enabled = load_settings().operator_tools_enabled
    configured_tools = tool_registry.mcp_tools_for_mode(operator_tools_enabled=operator_enabled)
    return {
        "user_tools": sorted(tool_registry.USER_MCP_TOOLS & name_set),
        "operator_tools": sorted(tool_registry.OPERATOR_MCP_TOOLS & name_set),
        "operator_tools_enabled": operator_enabled,
        "hidden_operator_tools": sorted(tool_registry.OPERATOR_MCP_TOOLS - name_set),
        "legacy_tools_present": sorted(tool_registry.LEGACY_MCP_TOOLS & name_set),
        "unexpected_tools": sorted(name_set - configured_tools),
        "all_tools": names,
    }


def remove_inactive_mcp_tools() -> None:
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if isinstance(tools, dict):
        for name in tool_registry.LEGACY_MCP_TOOLS:
            tools.pop(name, None)
        if not load_settings().operator_tools_enabled:
            for name in tool_registry.OPERATOR_MCP_TOOLS:
                tools.pop(name, None)


remove_inactive_mcp_tools()


def configure_transport(
    *,
    transport: str,
    host: str | None = None,
    port: int | None = None,
    stateful_http: bool = False,
    json_response: bool | None = None,
) -> None:
    global CURRENT_TRANSPORT
    CURRENT_TRANSPORT = transport
    if host:
        mcp.settings.host = host
    if port is not None:
        mcp.settings.port = port
    if transport == "streamable-http":
        mcp.settings.stateless_http = not stateful_http
        mcp.settings.json_response = True if json_response is None else bool(json_response)
    elif json_response is not None:
        mcp.settings.json_response = bool(json_response)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the NCS MCP server.")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport. Defaults to stdio for local clients.",
    )
    parser.add_argument("--host", default=None, help="HTTP host for sse or streamable-http transports.")
    parser.add_argument("--port", type=int, default=None, help="HTTP port for sse or streamable-http transports.")
    parser.add_argument(
        "--stateful-http",
        action="store_true",
        help="Use stateful streamable HTTP sessions. Default streamable-http mode is stateless.",
    )
    parser.add_argument(
        "--no-json-response",
        action="store_true",
        help="Disable JSON responses in streamable-http mode.",
    )
    args = parser.parse_args(argv)
    configure_transport(
        transport=args.transport,
        host=args.host,
        port=args.port,
        stateful_http=args.stateful_http,
        json_response=False if args.no_json_response else None,
    )
    mcp.run(args.transport)


if __name__ == "__main__":
    main()
