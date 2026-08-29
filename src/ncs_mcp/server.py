from __future__ import annotations

import argparse
import ipaddress
import inspect
import json
import os
import re
import sqlite3
import threading
import time
from functools import wraps

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from ncs_mcp.config import load_settings
from ncs_mcp.compact_postings import (
    concept_criteria_ids,
    has_compact_criteria_postings,
    has_compact_ontology_postings,
    ontology_relation_rows,
    sqlite_object_exists,
)
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
    recommend_task_transitions as db_recommend_task_transitions,
    row_to_dict,
    rows_to_dicts,
)
from ncs_mcp.error_codes import error_metadata
from ncs_mcp.helpers import DISCLAIMER, mask_sensitive_payload, not_found_response
from ncs_mcp.job_base_api import (
    fetch_job_base_page as job_base_fetch_page,
    job_base_summary as job_base_cached_summary,
    search_job_base_links as job_base_search_links,
)
from ncs_mcp.ontology import MVP_JOB_NAME, MVP_MAJOR_CODE, ONTOLOGY_SCHEMA, query_sqf_duties, sqf_summary
from ncs_mcp.qualification_api import (
    fetch_qualification_page as qualification_fetch_page,
    qualification_error_report as qualification_cached_error_report,
    qualification_summary as qualification_cached_summary,
    retry_qualification_error_units as qualification_retry_error_units,
    search_qualification_links as qualification_search_links,
)
from ncs_mcp.contracts import PLAN_NCS_EDUCATION_PATH_TOOL, QUERY_ROUTE_SCHEMA
from ncs_mcp.query_router import aihr_plan_route_evidence, route_ncs_query
from ncs_mcp.review_safety import (
    REVIEW_PACKET_EXTENSIONS,
    normalize_source_decision_packet_ref,
    resolve_repo_reports_artifact,
    review_packet_sha256 as shared_review_packet_sha256,
)
from ncs_mcp.server_legacy_facade import search_ncs_reference_chunks_payload as legacy_search_ncs_reference_chunks_payload
from ncs_mcp.server_legacy_wrappers import (
    build_legacy_operation_handlers,
    build_read_only_legacy_handlers,
)
from ncs_mcp.training_recommendation import (
    DEFAULT_COURSE_LINK_LIMIT,
    build_training_course_ontology_links as training_build_course_links,
    compact_ncs_education_plan_response as training_compact_education_plan_response,
    compact_training_task_response as training_compact_task_response,
    compact_training_transition_response as training_compact_transition_response,
    get_training_course as training_get_course,
    recommend_training_for_task as training_recommend_for_task,
    recommend_training_transition as training_recommend_transition,
    resolve_ncs_query_scope as training_resolve_ncs_query_scope,
    search_training_courses as training_search_courses,
)
from ncs_mcp import tool_registry


MCP_INSTRUCTIONS = """
HRMCP는 국가직무능력표준(NCS)의 분류, 능력단위, 능력단위요소, 수행준거,
지식·기술·태도(KSA), 훈련과정과 온톨로지 근거를 조회하는 읽기 중심 서버입니다.
도구 선택이 불확실하면 ncs_discover_tools를 먼저 호출하세요.
ncs_search로 NCS 구조를 찾고 ncs_unit_detail로 수행준거와 KSA 근거를 확인합니다.
ncs_training은 훈련과정을 검색하거나 과정 ID로 제한된 상세 링크를 반환합니다.
ncs_analysis는 career_path, qualification, job_base, ontology 근거를 조회합니다.
recommend_training_for_task는 NCS 과업과 KSA를 바탕으로 교육 후보를 제안합니다.
결과는 HR 담당자가 검토할 초안과 근거이며 공식 NCS 정의, 자격 인정 또는 채용 판정이 아닙니다.
""".strip()


mcp = FastMCP("ncs-mcp", instructions=MCP_INSTRUCTIONS)

CURRENT_TRANSPORT = "stdio"


READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)
READINESS_PUBLIC_TOOL_TABLES = (
    "classifications",
    "competency_elements",
    "ncs_training_course_unit_links",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
    "ncs_career_paths",
    "ncs_qualification_items",
    "ncs_unit_qualification_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_unit_job_base_links",
    "ontology_concepts",
    "ontology_concept_aliases",
)
READINESS_CAPABILITY_TABLES = {
    "structure_search": ("classifications", "competency_elements"),
    "training": (
        "ncs_training_courses",
        "ncs_training_course_unit_links",
        "ncs_training_course_concept_links",
        "ncs_training_course_element_links",
        "training_goal_concept_links",
        "training_delivery_relations",
    ),
    "career_path": ("ncs_career_paths",),
    "qualification": ("ncs_qualification_items", "ncs_unit_qualification_links"),
    "job_base": (
        "ncs_job_base_competencies",
        "ncs_job_base_factors",
        "ncs_unit_job_base_links",
    ),
    "ontology": ("ontology_concepts", "ontology_concept_aliases"),
}
READINESS_EXTRA_TABLES_ENV = "NCS_MCP_READINESS_EXTRA_TABLES"
_SQLITE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PUBLIC_UNIT_DETAIL_MAX_CHARS = 7_600
PUBLIC_UNIT_ELEMENT_FIELDS = (
    "element_id",
    "element_no",
    "element_code",
    "element_name",
    "element_level",
)
PUBLIC_CRITERIA_FIELDS = ("criteria_id", "criteria_no", "text")
PUBLIC_KSA_FIELDS = ("ksa_id", "ksa_type", "ksa_no", "text")

_RECOMMENDATION_LIMITER_LOCK = threading.Lock()
_RECOMMENDATION_LIMITERS: dict[int, threading.BoundedSemaphore] = {}


def _recommendation_limiter(limit: int) -> threading.BoundedSemaphore:
    with _RECOMMENDATION_LIMITER_LOCK:
        return _RECOMMENDATION_LIMITERS.setdefault(
            limit, threading.BoundedSemaphore(limit)
        )


@contextmanager
def recommendation_capacity_slot():
    settings = load_settings()
    limit = max(
        1,
        min(32, int(getattr(settings, "max_concurrent_recommendations", 2))),
    )
    timeout = max(
        0.1,
        min(
            300.0,
            float(getattr(settings, "recommendation_queue_timeout_seconds", 30.0)),
        ),
    )
    limiter = _recommendation_limiter(limit)
    started = time.monotonic()
    acquired = limiter.acquire(timeout=timeout)
    slot = {
        "acquired": acquired,
        "limit": limit,
        "queue_timeout_seconds": timeout,
        "queue_wait_ms": round((time.monotonic() - started) * 1000, 3),
    }
    try:
        yield slot
    finally:
        if acquired:
            limiter.release()


def recommendation_capacity_error(slot: dict[str, Any]) -> dict[str, Any]:
    response = error_response(
        "service_busy",
        message="Recommendation capacity is busy. Retry after a short delay.",
        retry_after_seconds=1,
        capacity=slot,
    )
    response["capacity"] = slot
    return response


def recommendation_execution_error(
    tool_name: str,
    exc: Exception,
    capacity: dict[str, Any] | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "tool_name": tool_name,
        "message": "Recommendation workflow failed during execution.",
        "exception_type": type(exc).__name__,
    }
    if capacity is not None:
        fields["capacity"] = capacity
    response = error_response("tool_execution_failed", **fields)
    if capacity is not None:
        response["capacity"] = capacity
    return response


def execute_capacity_bound_recommendation(
    tool_name: str,
    operation: Callable[[Any], dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any] | None]:
    capacity: dict[str, Any] = {
        "acquired": False,
        "status": "capacity_not_initialized",
    }
    try:
        with recommendation_capacity_slot() as capacity:
            if not capacity["acquired"]:
                return None, capacity, recommendation_capacity_error(capacity)
            with open_db() as conn:
                return operation(conn), capacity, None
    except Exception as exc:
        return None, capacity, recommendation_execution_error(
            tool_name,
            exc,
            capacity,
        )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_request: Any) -> JSONResponse:
    surface = current_mcp_tool_surface()
    runtime = runtime_health_metadata()
    transport = current_transport_metadata()
    return JSONResponse(
        {
            "status": (
                "ok"
                if runtime["database"]["ready"]
                and runtime["database"].get("public_tools_ready", False)
                else "degraded"
            ),
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
    settings = load_settings()
    read_only_mode = bool(getattr(settings, "read_only_mode", False))
    conn = connect(settings.db_path, read_only=read_only_mode)
    if not read_only_mode:
        initialize_database(conn)
    return conn


def runtime_health_metadata() -> dict[str, Any]:
    settings = load_settings()
    read_only_mode = bool(getattr(settings, "read_only_mode", False))
    operator_tools_requested = bool(settings.operator_tools_enabled)
    operator_tools_enabled = operator_tools_requested and not read_only_mode
    max_concurrent_recommendations = int(
        getattr(settings, "max_concurrent_recommendations", 2)
    )
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
        "operator_tools_enabled": operator_tools_enabled,
        "operator_tools_requested": operator_tools_requested,
        "operator_tools_blocked_by_read_only": operator_tools_requested and read_only_mode,
        "read_only_mode": read_only_mode,
        "max_concurrent_recommendations": max_concurrent_recommendations,
        "recommendation_queue_timeout_seconds": float(
            getattr(settings, "recommendation_queue_timeout_seconds", 30.0)
        ),
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


def _readiness_required_tables() -> tuple[tuple[str, ...], list[str]]:
    required_tables = list(READINESS_CORE_TABLES)
    seen = {table_name.casefold() for table_name in required_tables}
    invalid_extra_tables: list[str] = []
    invalid_seen: set[str] = set()

    for raw_table_name in os.environ.get(READINESS_EXTRA_TABLES_ENV, "").split(","):
        table_name = raw_table_name.strip()
        if not table_name:
            continue
        if _SQLITE_IDENTIFIER_RE.fullmatch(table_name) is None:
            if table_name not in invalid_seen:
                invalid_extra_tables.append(table_name)
                invalid_seen.add(table_name)
            continue
        normalized_name = table_name.casefold()
        if normalized_name in seen:
            continue
        required_tables.append(table_name)
        seen.add(normalized_name)

    return tuple(required_tables), invalid_extra_tables


def database_readiness_metadata(db_path) -> dict[str, Any]:
    required_tables, invalid_extra_tables = _readiness_required_tables()
    configured = bool(db_path)
    exists = bool(configured and db_path.exists())
    result: dict[str, Any] = {
        "configured": configured,
        "exists": exists,
        "openable": False,
        "ready": False,
        "required_tables": list(required_tables),
        "core_tables": {},
        "public_tool_tables": {},
    }
    if invalid_extra_tables:
        result["invalid_extra_tables"] = invalid_extra_tables
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
            for table_name in required_tables:
                exists_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if exists_row is None:
                    result["core_tables"][table_name] = {"exists": False, "row_count": None}
                    continue
                row_count = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                )
                result["core_tables"][table_name] = {"exists": True, "row_count": row_count}
            for table_name in READINESS_PUBLIC_TOOL_TABLES:
                exists_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if exists_row is None:
                    result["public_tool_tables"][table_name] = {
                        "exists": False,
                        "has_rows": False,
                    }
                    continue
                has_rows = conn.execute(
                    f'SELECT 1 FROM "{table_name}" LIMIT 1'
                ).fetchone() is not None
                result["public_tool_tables"][table_name] = {
                    "exists": True,
                    "has_rows": has_rows,
                }
            core_ready = all(
                item.get("exists") and int(item.get("row_count") or 0) > 0
                for item in result["core_tables"].values()
            ) and len(result["core_tables"]) == len(required_tables)
            capabilities: dict[str, dict[str, Any]] = {}
            degraded_capabilities: list[str] = []
            for capability, table_names in READINESS_CAPABILITY_TABLES.items():
                missing_tables: list[str] = []
                empty_tables: list[str] = []
                for table_name in table_names:
                    table_state = (
                        result["core_tables"].get(table_name)
                        or result["public_tool_tables"].get(table_name)
                        or {}
                    )
                    if not table_state.get("exists"):
                        missing_tables.append(table_name)
                        continue
                    has_rows = table_state.get("has_rows")
                    if has_rows is None:
                        has_rows = int(table_state.get("row_count") or 0) > 0
                    if not has_rows:
                        empty_tables.append(table_name)
                available = not missing_tables and not empty_tables
                capabilities[capability] = {
                    "available": available,
                    "missing_tables": missing_tables,
                    "empty_tables": empty_tables,
                }
                if not available:
                    degraded_capabilities.append(capability)
            public_tools_ready = not degraded_capabilities
            # Core readiness controls /ready. Optional public capabilities are
            # surfaced separately and enforced by the post-deploy tools/call gate.
            result["ready"] = core_ready
            result["core_ready"] = core_ready
            result["public_tools_ready"] = public_tools_ready
            result["capabilities"] = capabilities
            result["degraded_capabilities"] = degraded_capabilities
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
    include_data_alias: bool = True,
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
    if include_data_alias:
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
    else:
        result.pop("data", None)
    result["audit"] = audit or result.get(
        "audit",
        {
            "data_sources": ["SQLite NCS/SQF knowledge base"],
            "generated_at": now_utc(),
        },
    )
    return result


def _missing_aihr_plan_query_route_fields(route: Any) -> list[str]:
    if not isinstance(route, dict):
        return ["query_route"]
    missing: list[str] = []
    if route.get("schema") != QUERY_ROUTE_SCHEMA:
        missing.append("query_route.schema")
    if route.get("scenario") != "education_system_design":
        missing.append(f"query_route.scenario:{route.get('scenario')}")
    if route.get("tool") != PLAN_NCS_EDUCATION_PATH_TOOL:
        missing.append(f"query_route.tool:{route.get('tool')}")
    if route.get("available") is not True:
        missing.append(f"query_route.available:{route.get('available')}")
    if not route.get("route_fingerprint"):
        missing.append("query_route.route_fingerprint")
    if "guard_flags" not in route or not isinstance(route.get("guard_flags"), list):
        missing.append("query_route.guard_flags")
    expected_chain = route.get("expected_tool_chain")
    if not isinstance(expected_chain, list) or PLAN_NCS_EDUCATION_PATH_TOOL not in expected_chain:
        missing.append(f"query_route.expected_tool_chain.{PLAN_NCS_EDUCATION_PATH_TOOL}")
    if not isinstance(expected_chain, list) or "recommend_training_transition" not in expected_chain:
        missing.append("query_route.expected_tool_chain.recommend_training_transition")
    route_contract = route.get("route_contract")
    if not isinstance(route_contract, dict):
        missing.append("query_route.route_contract")
    else:
        if route_contract.get("schema") != QUERY_ROUTE_SCHEMA:
            missing.append("query_route.route_contract.schema")
        if route_contract.get("route_first") is not True:
            missing.append("query_route.route_contract.route_first")
        if route_contract.get("primary_tool") != route.get("tool"):
            missing.append("query_route.route_contract.primary_tool")
        if route_contract.get("route_fingerprint") != route.get("route_fingerprint"):
            missing.append("query_route.route_contract.route_fingerprint")
    return missing


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


def guard_public_tool(func):
    """Convert unexpected public-tool failures into sanitized MCP errors."""

    @wraps(func)
    def guarded(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            return error_response(
                "tool_execution_failed",
                tool_name=func.__name__,
                message="The tool failed while reading its configured NCS data.",
            )

    return guarded


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


READ_ONLY_LEGACY_HANDLERS = build_read_only_legacy_handlers(
    open_db=open_db,
    quality_for=quality_for,
    tool_response=tool_response,
    error_response=error_response,
    now_utc=now_utc,
    db_path_getter=lambda: load_settings().db_path,
)

LEGACY_OPERATION_HANDLERS = build_legacy_operation_handlers(
    open_db=open_db,
    tool_response=tool_response,
    error_response=error_response,
    now_utc=now_utc,
    db_path_getter=lambda: load_settings().db_path,
)

_legacy_build_sqf_ncs_mapping_candidates = LEGACY_OPERATION_HANDLERS.build_sqf_ncs_mapping_candidates
_legacy_map_sqf_to_ncs = LEGACY_OPERATION_HANDLERS.map_sqf_to_ncs
_legacy_prepare_ontology_review_queue = LEGACY_OPERATION_HANDLERS.prepare_ontology_review_queue
_legacy_collect_qualification_items = LEGACY_OPERATION_HANDLERS.collect_qualification_items
_legacy_collect_job_base_competencies = LEGACY_OPERATION_HANDLERS.collect_job_base_competencies
_legacy_recommend_education_for_duty = LEGACY_OPERATION_HANDLERS.recommend_education_for_duty
_legacy_import_ncs_reference_html = LEGACY_OPERATION_HANDLERS.import_ncs_reference_html
_legacy_import_ncs_reference_docx = LEGACY_OPERATION_HANDLERS.import_ncs_reference_docx
_legacy_extract_ncs_reference_entities = LEGACY_OPERATION_HANDLERS.extract_ncs_reference_entities
_legacy_link_reference_entities_to_ncs = LEGACY_OPERATION_HANDLERS.link_reference_entities_to_ncs
_legacy_recommend_learning_modules_by_ncs = LEGACY_OPERATION_HANDLERS.recommend_learning_modules_by_ncs
_legacy_review_exact_learning_module_name_links = (
    LEGACY_OPERATION_HANDLERS.review_exact_learning_module_name_links
)
_legacy_build_ncs_derived_learning_plans = LEGACY_OPERATION_HANDLERS.build_ncs_derived_learning_plans
_legacy_build_report_training_courses = LEGACY_OPERATION_HANDLERS.build_report_training_courses
_legacy_recommend_education_by_concepts = LEGACY_OPERATION_HANDLERS.recommend_education_by_concepts
_legacy_review_sqf_ncs_match = LEGACY_OPERATION_HANDLERS.review_sqf_ncs_match


def unit_path(row, *, include_duty_definition: bool = False) -> dict[str, Any]:
    path = {
        "major_code": row["major_code"],
        "major": row["major_name"],
        "middle_code": row["middle_code"],
        "middle": row["middle_name"],
        "small_code": row["small_code"],
        "small": row["small_name"],
        "sub_code": row["sub_code"],
        "sub": row["sub_name"],
        "duty_order": row["duty_order"],
    }
    if include_duty_definition:
        path["duty_definition"] = row["duty_def_api"]
    return path


def _project_public_unit_detail(
    result: dict[str, Any],
    *,
    max_chars: int = PUBLIC_UNIT_DETAIL_MAX_CHARS,
) -> dict[str, Any]:
    """Keep public unit detail useful while enforcing a bounded JSON response."""

    source_elements = result.get("elements")
    if not isinstance(source_elements, list):
        return result

    projected_elements: list[dict[str, Any]] = []
    for source_element in source_elements:
        element = {
            field: source_element.get(field)
            for field in PUBLIC_UNIT_ELEMENT_FIELDS
        }
        if "performance_criteria" in source_element:
            element["performance_criteria"] = [
                {field: row.get(field) for field in PUBLIC_CRITERIA_FIELDS}
                for row in source_element.get("performance_criteria", [])
            ]
        if "ksa" in source_element:
            element["ksa"] = [
                {field: row.get(field) for field in PUBLIC_KSA_FIELDS}
                for row in source_element.get("ksa", [])
            ]
        projected_elements.append(element)

    projected = {**result, "elements": projected_elements}
    projected["detail_meta"] = {"projection": "compact", "truncated": False}
    if len(json.dumps(projected, ensure_ascii=True)) <= max_chars:
        return projected

    list_keys = ("training_courses", "qualification_links")
    source_lists = {
        key: list(projected.get(key, []))
        for key in list_keys
        if isinstance(projected.get(key), list)
    }
    compact = {
        key: value
        for key, value in projected.items()
        if key != "elements" and key not in source_lists
    }
    compact["elements"] = []
    for key in source_lists:
        compact[key] = []

    total_criteria = sum(
        len(element.get("performance_criteria", []))
        for element in projected_elements
    )
    total_ksa = sum(len(element.get("ksa", [])) for element in projected_elements)
    counts: dict[str, dict[str, Any]] = {
        "elements": {
            "total_count": len(projected_elements),
            "returned_count": 0,
            "truncated": bool(projected_elements),
        },
        "performance_criteria": {
            "total_count": total_criteria,
            "returned_count": 0,
            "truncated": bool(total_criteria),
        },
        "ksa": {
            "total_count": total_ksa,
            "returned_count": 0,
            "truncated": bool(total_ksa),
        },
    }
    for key, rows in source_lists.items():
        counts[key] = {
            "total_count": len(rows),
            "returned_count": 0,
            "truncated": bool(rows),
        }
    compact["detail_meta"] = {
        "projection": "compact",
        "max_serialized_chars": max_chars,
        "counts": counts,
        "truncated": True,
    }

    included_sources: list[dict[str, Any]] = []
    for source_element in projected_elements:
        header = {
            key: value
            for key, value in source_element.items()
            if key not in {"performance_criteria", "ksa"}
        }
        if "performance_criteria" in source_element:
            header["performance_criteria"] = []
        if "ksa" in source_element:
            header["ksa"] = []
        compact["elements"].append(header)
        counts["elements"]["returned_count"] += 1
        counts["elements"]["truncated"] = (
            counts["elements"]["returned_count"] < counts["elements"]["total_count"]
        )
        if len(json.dumps(compact, ensure_ascii=True)) > max_chars:
            compact["elements"].pop()
            counts["elements"]["returned_count"] -= 1
            counts["elements"]["truncated"] = True
            break
        included_sources.append(source_element)

    streams: list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]] = []
    for key, rows in source_lists.items():
        streams.append((compact[key], rows, key))
    for target_element, source_element in zip(compact["elements"], included_sources):
        for source_key, count_key in (
            ("performance_criteria", "performance_criteria"),
            ("ksa", "ksa"),
        ):
            if source_key in target_element:
                streams.append(
                    (
                        target_element[source_key],
                        source_element.get(source_key, []),
                        count_key,
                    )
                )

    positions = [0] * len(streams)
    active = [True] * len(streams)
    while any(active):
        progressed = False
        for stream_index, (target, source, count_key) in enumerate(streams):
            if not active[stream_index]:
                continue
            position = positions[stream_index]
            if position >= len(source):
                active[stream_index] = False
                continue
            target.append(source[position])
            counts[count_key]["returned_count"] += 1
            counts[count_key]["truncated"] = (
                counts[count_key]["returned_count"] < counts[count_key]["total_count"]
            )
            if len(json.dumps(compact, ensure_ascii=True)) > max_chars:
                target.pop()
                counts[count_key]["returned_count"] -= 1
                counts[count_key]["truncated"] = True
                active[stream_index] = False
                continue
            positions[stream_index] += 1
            progressed = True
        if not progressed:
            break

    compact["detail_meta"]["truncated"] = any(
        item["truncated"] for item in counts.values()
    )
    return compact


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


@mcp.tool(structured_output=False)
@guard_public_tool
def ncs_search(query: str = "", scope: str = "all", limit: int = 20) -> dict[str, Any]:
    """NCS 분류·능력단위·요소·수행준거·KSA를 검색합니다. Search NCS structure and evidence."""
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
            include_data_alias=False,
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
        include_data_alias=False,
    )


@mcp.tool(structured_output=False)
@guard_public_tool
def ncs_unit_detail(
    unit_code: str,
    include: list[str] | None = None,
    text_version: str = "raw",
) -> dict[str, Any]:
    """능력단위의 수행준거·KSA·훈련·자격 근거를 조회합니다. Return one NCS unit in detail."""
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
            result["training_courses"] = training_search_courses(
                conn,
                unit_code=unit_code,
                limit=3,
                compact=True,
            )
        if "qualification" in include_set:
            qualification_links = qualification_search_links(conn, unit_code=unit_code, limit=5)
            qualification_fields = (
                "unit_code",
                "jm_cd",
                "jm_nm",
                "exam_insti_nm",
                "compe_unit_name",
                "ablt_unit_typ_cd",
                "ablt_unit_typ_nm",
                "min_edu_trng_tm",
                "confidence_score",
                "review_status",
            )
            result["qualification_links"] = [
                {field: row.get(field) for field in qualification_fields}
                for row in qualification_links
            ]
    result = _project_public_unit_detail(result)
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
        include_data_alias=False,
    )


@mcp.tool(structured_output=False)
@guard_public_tool
def ncs_training(
    query: str | None = None,
    training_course_id: int | None = None,
    limit: int = 20,
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
) -> dict[str, Any]:
    """NCS 훈련과정을 검색하거나 과정 ID로 상세 근거를 조회합니다. Search NCS training courses."""
    if training_course_id is not None:
        result = get_training_course(training_course_id, link_limit=link_limit)
        if has_not_found_error(result):
            return not_found_response(f"훈련과정을 찾을 수 없습니다: {training_course_id}")
        return result
    result = search_training_courses(query=query, limit=limit, link_limit=link_limit)
    rows = result.get("training_courses") or result.get("data", {}).get("training_courses", [])
    if not rows:
        return not_found_response(f"훈련과정 검색 결과가 없습니다: {query or ''}".strip())
    return result


@mcp.tool(structured_output=False)
@guard_public_tool
def ncs_analysis(
    mode: str,
    query: str | None = None,
    unit_code: str | None = None,
    concept_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """경력개발·자격·직업기초능력·온톨로지 근거를 조회합니다. Analyze supporting NCS evidence."""
    if mode == "career_path":
        result = search_career_paths(query=query, unit_code=unit_code, limit=limit)
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
        if not items and query and not unit_code:
            unit_matches = search_ncs(query=query, scope="unit", limit=1).get("results", [])
            resolved_unit_code = next(
                (
                    str(row.get("id"))
                    for row in unit_matches
                    if row.get("type") == "unit" and row.get("id")
                ),
                None,
            )
            if resolved_unit_code:
                result = search_job_base_competencies(
                    unit_code=resolved_unit_code,
                    limit=limit,
                )
                items = result.get("job_base_links") or result.get("data", {}).get(
                    "job_base_links", []
                )
                if items:
                    result["query_resolution"] = {
                        "input_query": query,
                        "resolved_unit_code": resolved_unit_code,
                        "method": "ncs_unit_name_fallback",
                    }
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
    # Preserve the established public envelope, including the legacy ``data``
    # alias.  ``structured_output=False`` removes FastMCP's wire-level
    # ``structuredContent`` duplicate without changing this tool's JSON shape.
    return result


@mcp.tool()
@guard_public_tool
def ncs_discover_tools(intent: str = "") -> dict[str, Any]:
    """한국어 사용자 의도에 맞는 HRMCP 도구와 호출 순서를 안내합니다. Discover the right tool."""
    surface = current_mcp_tool_surface()
    query_route = route_ncs_query(intent, available_tool_names=set(surface["all_tools"]))
    matches = tool_registry.discover_tools_for_intent(
        intent,
        executable_tool_names=tool_registry.NCS_EXECUTABLE_TOOL_NAMES,
        available_tool_names=set(surface["all_tools"]),
    )
    return tool_response(
        {
            "intent": intent,
            "query_route": query_route,
            "matched_categories": matches,
            "exposed_tool_count": len(surface["all_tools"]),
            "execution_note": (
                "Use ncs_execute_tool for read-only user tools. Operator/review tools must be called directly."
            ),
            "hidden_operator_note": (
                "Review/operator tools are hidden unless NCS_MCP_ENABLE_OPERATOR_TOOLS=1 is set before server start."
            ),
            "hidden_advanced_note": (
                "Advanced ontology/education-integration/transition tools are hidden unless "
                "NCS_MCP_ENABLE_ADVANCED_TOOLS=1 is set before server start."
            ),
            "hidden_legacy_note": "SQF and learning-module legacy tools are not part of the active recommendation path.",
        },
        audit={
            "data_sources": ["NCS MCP tool registry"],
            "generated_at": now_utc(),
        },
    )


def _route_value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _route_allowed_tools(query_route: dict[str, Any] | None) -> list[str]:
    if not isinstance(query_route, dict):
        return []
    chain = query_route.get("expected_tool_chain")
    if isinstance(chain, list):
        tools = [str(item) for item in chain if item]
    else:
        tools = []
    route_tool = query_route.get("tool")
    if route_tool and str(route_tool) not in tools:
        tools.insert(0, str(route_tool))
    return tools


def _route_missing_required_params(query_route: dict[str, Any], tool_params: dict[str, Any]) -> list[str]:
    route_params = query_route.get("params") if isinstance(query_route.get("params"), dict) else {}
    required = query_route.get("required_params") or query_route.get("missing_params") or []
    missing: list[str] = []
    for name in required:
        key = str(name)
        if _route_value_missing(tool_params.get(key)) and _route_value_missing(route_params.get(key)):
            missing.append(key)
    return missing


def _route_execution_metadata(
    *,
    tool_name: str,
    query_route: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    route_tool = query_route.get("tool") if isinstance(query_route, dict) else None
    allowed_tools = _route_allowed_tools(query_route)
    return {
        "tool_name": tool_name,
        "save_forced_false": tool_name in tool_registry.NCS_META_READ_ONLY_SAVE_FORCED_TOOLS,
        "compact_defaulted": (
            tool_name in tool_registry.NCS_META_COMPACT_DEFAULT_TOOLS
            and "compact" not in (params or {})
        ),
        "query_route": query_route,
        "route_contract_schema": (
            query_route.get("schema") if isinstance(query_route, dict) else None
        ),
        "route_fingerprint": (
            query_route.get("route_fingerprint") if isinstance(query_route, dict) else None
        ),
        "route_allowed_tools": allowed_tools,
        "route_tool_allowed": bool(not query_route or tool_name in allowed_tools),
        "route_tool_mismatch": bool(query_route and route_tool != tool_name),
        "route_guard_flags": (
            query_route.get("guard_flags") if isinstance(query_route, dict) else []
        ),
    }


@mcp.tool()
@guard_public_tool
def ncs_execute_tool(tool_name: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """발견된 읽기 전용 HRMCP 도구를 실행합니다. Execute a discovered read-only tool."""
    if tool_name in {"ncs_discover_tools", "ncs_execute_tool"}:
        return error_response("meta_tool_recursion_blocked", tool_name=tool_name)
    advanced_enabled = bool(getattr(load_settings(), "advanced_tools_enabled", False))
    executable_tools = tool_registry.executable_tool_names_for_mode(
        advanced_tools_enabled=advanced_enabled
    )
    if tool_name not in executable_tools:
        if tool_name in tool_registry.ADVANCED_MCP_TOOLS:
            return error_response(
                "tool_disabled",
                tool_name=tool_name,
                executable_tools=sorted(executable_tools),
                note=(
                    "Advanced ontology/education-integration tools are disabled on this "
                    "deployment. Set NCS_MCP_ENABLE_ADVANCED_TOOLS=1 to enable them."
                ),
            )
        return error_response(
            "tool_not_executable_via_meta",
            tool_name=tool_name,
            executable_tools=sorted(executable_tools),
            note="Operator/review tools and hidden legacy tools are blocked from ncs_execute_tool.",
        )
    tool_params = dict(params or {})
    route_query = tool_params.pop("_route_query", None)
    route_fingerprint = tool_params.pop("_route_fingerprint", None)
    query_route = (
        route_ncs_query(str(route_query), available_tool_names=tool_registry.NCS_EXECUTABLE_TOOL_NAMES)
        if route_query
        else None
    )
    if route_fingerprint and not query_route:
        return error_response(
            "route_query_required_for_fingerprint",
            tool_name=tool_name,
            route_fingerprint=route_fingerprint,
            message="_route_fingerprint requires _route_query so the route can be recomputed.",
        )
    if query_route:
        expected_fingerprint = query_route.get("route_fingerprint")
        if route_fingerprint and str(route_fingerprint) != str(expected_fingerprint):
            return error_response(
                "route_fingerprint_mismatch",
                tool_name=tool_name,
                expected_route_fingerprint=expected_fingerprint,
                received_route_fingerprint=route_fingerprint,
                route_query=route_query,
            )
        allowed_tools = _route_allowed_tools(query_route)
        if tool_name not in allowed_tools:
            return error_response(
                "route_tool_mismatch",
                tool_name=tool_name,
                routed_tool=query_route.get("tool"),
                allowed_tools=allowed_tools,
                route_fingerprint=expected_fingerprint,
                message="The requested tool is not in the routed primary tool or expected tool chain.",
            )
        if not query_route.get("available", True):
            return error_response(
                "routed_tool_unavailable",
                tool_name=tool_name,
                routed_tool=query_route.get("tool"),
                route_fingerprint=expected_fingerprint,
            )
        if tool_name == query_route.get("tool"):
            route_params = query_route.get("params") if isinstance(query_route.get("params"), dict) else {}
            for key, value in route_params.items():
                tool_params.setdefault(key, value)
        missing_required = _route_missing_required_params(query_route, tool_params)
        if missing_required:
            return error_response(
                "route_required_params_missing",
                tool_name=tool_name,
                missing_params=missing_required,
                route_fingerprint=expected_fingerprint,
            )
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
            _route_execution_metadata(
                tool_name=tool_name,
                query_route=query_route,
                params=params,
            )
        )
        return result
    return tool_response(
        {
            "tool_name": tool_name,
            "result": result,
            "meta_execution": _route_execution_metadata(
                tool_name=tool_name,
                query_route=query_route,
                params=params,
            ),
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
                "classification": unit_path(unit, include_duty_definition=True),
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


def _ncs_search_markdown(query: str, results: list[dict[str, Any]]) -> str:
    lines = [f"## NCS 검색 결과: {query}"]
    for index, item in enumerate(results[:5], start=1):
        item_type = str(item.get("type") or "result")
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        lines.append(f"{index}. **{text}** (`{item_type}` · `{item_id}`)")
    if len(results) > 5:
        lines.append(f"- 그 밖의 결과 {len(results) - 5}건은 `results`에서 확인할 수 있습니다.")
    return "\n".join(lines)


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
                       c.duty_order
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE cu.unit_code LIKE :pattern
                   OR cu.unit_name_raw LIKE :pattern
                   OR cu.api_definition LIKE :pattern
                   OR c.major_name LIKE :pattern
                   OR c.middle_name LIKE :pattern
                   OR c.small_name LIKE :pattern
                   OR c.sub_name LIKE :pattern
                   OR cu.unit_code IN (
                       SELECT DISTINCT alias.unit_code
                       FROM ncs_query_aliases alias
                       WHERE alias.unit_code IS NOT NULL
                         AND (alias.alias_text LIKE :pattern OR alias.normalized_query LIKE :pattern)
                   )
                ORDER BY
                    CASE
                        WHEN cu.unit_code = :exact THEN 0
                        WHEN TRIM(cu.unit_name_raw) = TRIM(:exact) COLLATE NOCASE THEN 0
                        WHEN cu.unit_name_raw LIKE :prefix THEN 1
                        WHEN cu.unit_name_raw LIKE :pattern THEN 2
                        WHEN c.major_name LIKE :pattern
                          OR c.middle_name LIKE :pattern
                          OR c.small_name LIKE :pattern
                          OR c.sub_name LIKE :pattern THEN 3
                        WHEN cu.api_definition LIKE :pattern THEN 4
                        ELSE 5
                    END,
                    LENGTH(cu.unit_name_raw),
                    cu.unit_code
                LIMIT :row_limit
                """,
                {
                    "pattern": pattern,
                    "exact": query,
                    "prefix": f"{query}%",
                    "row_limit": max_rows - len(results),
                },
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
    return {
        "query": query,
        "scope": scope,
        "markdown_summary": _ncs_search_markdown(query, results),
        "results": results,
    }


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
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
    compact: bool = True,
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
            link_limit=link_limit,
            compact=compact,
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
            "data_sources": (
                ["ncs_training_courses", "NCS training link tables"]
                if compact
                else [
                    "ncs_training_courses",
                    "ncs_training_course_unit_links",
                    "ncs_training_course_concept_links",
                    "ncs_training_course_element_links",
                    "training_goal_concept_links",
                    "training_delivery_relations",
                ]
            ),
            "returned": len(courses),
            "generated_at": now_utc(),
            "sqf_used": False,
            "learning_modules_used": False,
        },
        include_data_alias=False,
    )


def get_training_course(
    training_course_id: int,
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
) -> dict[str, Any]:
    """Return one cached NCS training course with NCS unit and KSA concept links."""
    with open_db() as conn:
        result = training_get_course(conn, training_course_id, link_limit=link_limit)
    return tool_response(result, include_data_alias=False)


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


def search_career_paths(
    query: str | None = None,
    unit_code: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search career-development rows instead of returning an unrelated global summary."""
    clauses: list[str] = []
    params: list[Any] = []
    if query:
        clauses.append(
            "(job_name LIKE ? OR competency_name LIKE ? OR position_name LIKE ?)"
        )
        pattern = f"%{query}%"
        params.extend([pattern, pattern, pattern])
    if unit_code:
        clauses.append("matched_unit_code = ?")
        params.append(unit_code)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT career_path_id, job_code_raw, job_name,
                   competency_code_raw, competency_level_raw, competency_name,
                   position_level_raw, position_name,
                   major_code, middle_code, small_code, sub_code,
                   matched_unit_code, confidence_score, review_status
            FROM ncs_career_paths
            {where}
            ORDER BY
                CASE WHEN ? IS NOT NULL AND job_name = ? THEN 0
                     WHEN ? IS NOT NULL AND competency_name = ? THEN 1
                     WHEN ? IS NOT NULL AND job_name LIKE ? THEN 2
                     WHEN ? IS NOT NULL AND competency_name LIKE ? THEN 3
                     ELSE 4 END,
                job_name, competency_name, career_path_id
            LIMIT ?
            """,
            (
                *params,
                query,
                query,
                query,
                query,
                query,
                f"{query}%" if query else None,
                query,
                f"{query}%" if query else None,
                clamp_limit(limit, default=20, maximum=100),
            ),
        ).fetchall()
    return tool_response(
        {"query": query, "unit_code": unit_code, "career_paths": rows_to_dicts(rows)},
        audit={"data_sources": ["ncs_career_paths"], "generated_at": now_utc()},
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
        raw_links = qualification_search_links(
            conn,
            unit_code=unit_code,
            qualification_name=qualification_name,
            qualification_code=qualification_code,
            unit_type=unit_type,
            limit=limit,
        )
        qualification_fields = (
            "unit_code",
            "jm_cd",
            "jm_nm",
            "exam_insti_nm",
            "compe_unit_name",
            "ablt_unit_typ_cd",
            "ablt_unit_typ_nm",
            "min_edu_trng_tm",
            "unit_name",
            "major_code",
            "major_name",
            "confidence_score",
            "review_status",
        )
        links = [
            {field: row.get(field) for field in qualification_fields}
            for row in raw_links
        ]
        if sqlite_object_exists(conn, "ncs_qualification_collection_status"):
            summary = qualification_cached_summary(conn, limit=10)
            summary["collection_status_available"] = True
        else:
            summary = {
                "ok": True,
                "qualification_item_count": int(
                    conn.execute("SELECT COUNT(*) FROM ncs_qualification_items").fetchone()[0]
                ),
                "unit_qualification_link_count": int(
                    conn.execute("SELECT COUNT(*) FROM ncs_unit_qualification_links").fetchone()[0]
                ),
                "collection_status_available": False,
                "missing_optional_tables": ["ncs_qualification_collection_status"],
            }
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


def search_job_base_competencies(
    unit_code: str | None = None,
    competency_name: str | None = None,
    factor_name: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search cached NCS job base competencies linked to NCS competency units."""
    with open_db() as conn:
        raw_links = job_base_search_links(
            conn,
            unit_code=unit_code,
            competency_name=competency_name,
            factor_name=factor_name,
            major_code=major_code,
            limit=limit,
        )
        job_base_fields = (
            "unit_code",
            "job_base_competency_id",
            "job_base_factor_id",
            "competency_name",
            "factor_name",
            "unit_name",
            "major_code",
            "major_name",
            "link_method",
            "confidence_score",
            "review_status",
        )
        links = [
            {field: row.get(field) for field in job_base_fields}
            for row in raw_links
        ]
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


@mcp.tool(structured_output=False)
@guard_public_tool
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
    """과업의 수행준거와 KSA 근거로 훈련과정을 추천합니다. Recommend training from task evidence."""
    result, capacity, failure = execute_capacity_bound_recommendation(
        "recommend_training_for_task",
        lambda conn: training_recommend_for_task(
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
            save=False,
        ),
    )
    if failure is not None:
        return failure
    try:
        assert result is not None
        if compact:
            result = training_compact_task_response(result, recommendation_limit=limit)
        if result.get("ok"):
            result.setdefault("disclaimer", DISCLAIMER)
        result["capacity"] = capacity
        return tool_response(result)
    except Exception as exc:
        return recommendation_execution_error(
            "recommend_training_for_task",
            exc,
            capacity,
        )


@mcp.tool()
@guard_public_tool
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
    result, capacity, failure = execute_capacity_bound_recommendation(
        "recommend_training_transition",
        lambda conn: training_recommend_transition(
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
            save=False,
        ),
    )
    if failure is not None:
        return failure
    try:
        assert result is not None
        if compact:
            result = training_compact_transition_response(
                result,
                recommendation_limit=limit,
            )
        if result.get("ok"):
            result.setdefault("disclaimer", DISCLAIMER)
        result["capacity"] = capacity
        return tool_response(result)
    except Exception as exc:
        return recommendation_execution_error(
            "recommend_training_transition",
            exc,
            capacity,
        )


@mcp.tool()
@guard_public_tool
def plan_ncs_education_path(
    current_query: str,
    target_query: str,
    plan_objective: str | None = None,
    target_population: str | None = None,
    scenario: str | None = None,
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
    preferred_facilities: list[str] | None = None,
    limit: int = 5,
    save: bool = False,
) -> dict[str, Any]:
    """Build an NCS education-system plan with KSA evidence and delivery-fit matrix."""
    try:
        route_evidence = aihr_plan_route_evidence(
            current_query,
            target_query,
            available_tool_names=set(
                tool_registry.mcp_tools_for_mode(operator_tools_enabled=False)
            ),
        )
        route_contract_schema = (
            route_evidence.get("route_contract", {}).get("schema")
            if isinstance(route_evidence.get("route_contract"), dict)
            else route_evidence.get("schema")
        )
        route_fingerprint = route_evidence.get("route_fingerprint")
        missing_route_fields = _missing_aihr_plan_query_route_fields(route_evidence)
    except Exception as exc:
        return recommendation_execution_error(
            "plan_ncs_education_path",
            exc,
            None,
        )
    if missing_route_fields:
        route_failure_payload = {
            "ok": False,
            "error": {
                "code": "missing_query_route_contract",
                "message": "AI-HR education planner route contract is incomplete.",
                "missing_fields": missing_route_fields,
            },
            "query_route": route_evidence,
            "route_contract_schema": route_contract_schema,
            "route_fingerprint": route_fingerprint,
            "missing_query_route_fields": missing_route_fields,
            "disclaimer": DISCLAIMER,
        }
        route_failure_data = {
            "query_route": route_evidence,
            "route_contract_schema": route_contract_schema,
            "route_fingerprint": route_fingerprint,
            "missing_query_route_fields": missing_route_fields,
        }
        return tool_response(route_failure_payload, data=route_failure_data)
    result, capacity, failure = execute_capacity_bound_recommendation(
        "plan_ncs_education_path",
        lambda conn: training_recommend_transition(
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
            preferred_facilities=preferred_facilities,
            limit=limit,
            save=False,
        ),
    )
    if failure is not None:
        return failure
    try:
        assert result is not None
        if result.get("ok"):
            result = training_compact_transition_response(
                result,
                recommendation_limit=limit,
            )
            result = training_compact_education_plan_response(
                result,
                plan_objective=plan_objective,
                target_population=target_population,
                scenario=scenario,
                recommendation_limit=limit,
            )
            result["query_route"] = route_evidence
            result["route_contract_schema"] = route_contract_schema
            result["route_fingerprint"] = route_fingerprint
            missing_route_fields = []
            result["missing_query_route_fields"] = missing_route_fields
            result.setdefault("disclaimer", DISCLAIMER)
        result["capacity"] = capacity
        return tool_response(result)
    except Exception as exc:
        return recommendation_execution_error(
            "plan_ncs_education_path",
            exc,
            capacity,
        )


def search_ncs_reference_chunks(
    query: str,
    document_id: int | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search imported NCS reference chunks and return summaries with page/chunk locations."""
    payload, audit = legacy_search_ncs_reference_chunks_payload(
        open_db,
        query=query,
        document_id=document_id,
        limit=limit,
    )
    return tool_response(payload, audit=audit)


@mcp.tool()
@guard_public_tool
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
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[Any] | None = None,
    created_by_tool: str | None = None,
    run_artifact: str | None = None,
) -> None:
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
            entity_type,
            entity_id,
            action,
            previous_status,
            new_status,
            reviewer_id,
            notes,
            stored_source_decision_packet,
            source_artifact_hash,
            rationale,
            evidence_refs_json,
            created_by_tool,
            run_artifact,
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
TRUSTED_REVIEW_STATUSES = {"human_reviewed", "reviewed", "accepted"}
AUTOMATED_REVIEWER_IDS = {"", "automated_eval_gate", "automation", "mcp", "system"}


def normalize_review_link_status(review_status: str) -> str | None:
    status = " ".join(str(review_status or "").strip().split())
    return status if status in REVIEWABLE_LINK_STATUSES else None


TRUSTED_REVIEW_PACKET_EXTENSIONS = REVIEW_PACKET_EXTENSIONS


def _review_packet_sha256(path: Path) -> str:
    return shared_review_packet_sha256(path)


def _resolve_review_packet_artifact(source_decision_packet: str | None) -> Path | None:
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
    packet_path = _resolve_review_packet_artifact(source_decision_packet)
    if source_decision_packet and packet_path is None:
        blockers.append("trusted_status_requires_packet_backed_source_decision_packet")
    if not (source_artifact_hash or "").strip():
        blockers.append("trusted_status_requires_source_artifact_hash")
    elif not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(source_artifact_hash)):
        blockers.append("trusted_status_requires_sha256_source_artifact_hash")
    elif packet_path is not None:
        expected_hash = "sha256:" + _review_packet_sha256(packet_path)
        if str(source_artifact_hash).lower() != expected_hash:
            blockers.append("trusted_status_requires_matching_source_artifact_hash")
    if not (rationale or "").strip():
        blockers.append("trusted_status_requires_rationale")
    return blockers


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
    if query:
        ordering = """
            CASE
                WHEN TRIM(oc.concept_name) = TRIM(?) COLLATE NOCASE THEN 0
                WHEN EXISTS (
                    SELECT 1 FROM ontology_concept_aliases alias
                    WHERE alias.concept_id = oc.concept_id
                      AND TRIM(alias.alias_text) = TRIM(?) COLLATE NOCASE
                ) THEN 1
                WHEN oc.concept_name LIKE ? THEN 2
                WHEN EXISTS (
                    SELECT 1 FROM ontology_concept_aliases alias
                    WHERE alias.concept_id = oc.concept_id
                      AND alias.alias_text LIKE ?
                ) THEN 3
                WHEN oc.concept_name LIKE ? THEN 4
                WHEN EXISTS (
                    SELECT 1 FROM ontology_concept_aliases alias
                    WHERE alias.concept_id = oc.concept_id
                      AND alias.alias_text LIKE ?
                ) THEN 5
                WHEN oc.definition LIKE ? THEN 6
                ELSE 7
            END,
            CASE oc.review_status
                WHEN 'human_reviewed' THEN 0
                WHEN 'reviewed' THEN 1
                WHEN 'accepted' THEN 1
                ELSE 2
            END,
            oc.concept_type,
            LENGTH(oc.concept_name),
            oc.concept_name
        """
        ordering_params: list[Any] = [
            query,
            query,
            f"{query}%",
            f"{query}%",
            like,
            like,
            like,
        ]
    else:
        ordering = "oc.review_status, oc.concept_type, oc.concept_name"
        ordering_params = []
    with open_db() as conn:
        # Hosted ontology snapshots replace the large relation/link tables with
        # postings.  Keep the public row shape identical while using posting
        # metadata for counts; counts must not require decoding every edge.
        compact_ontology = has_compact_ontology_postings(conn)
        compact_criteria = has_compact_criteria_postings(conn)
        if compact_ontology:
            relation_count_sql = """
                COALESCE((
                    SELECT SUM(post.target_count)
                    FROM ontology_relation_outgoing AS post
                    WHERE post.source_concept_id = oc.concept_id
                ), 0)
            """
        elif sqlite_object_exists(conn, "ontology_concept_relations"):
            relation_count_sql = """
                (SELECT COUNT(DISTINCT rel.relation_id)
                 FROM ontology_concept_relations AS rel
                 WHERE rel.source_concept_id = oc.concept_id)
            """
        else:
            relation_count_sql = "0"
        if compact_criteria:
            criteria_count_sql = """
                COALESCE((
                    SELECT post.criteria_count
                    FROM criteria_concept_inverse AS post
                    WHERE post.concept_id = oc.concept_id
                ), 0)
            """
        elif sqlite_object_exists(conn, "criteria_concept_links"):
            criteria_count_sql = """
                (SELECT COUNT(DISTINCT ccl.criteria_id)
                 FROM criteria_concept_links AS ccl
                 WHERE ccl.concept_id = oc.concept_id)
            """
        else:
            criteria_count_sql = "0"
        alias_count_sql = (
            "(SELECT COUNT(DISTINCT alias.alias_id) FROM "
            "ontology_concept_aliases AS alias WHERE alias.concept_id = oc.concept_id)"
            if sqlite_object_exists(conn, "ontology_concept_aliases")
            else "0"
        )
        ksa_count_sql = (
            "(SELECT COUNT(DISTINCT kcl.ksa_id) FROM ksa_concept_links AS kcl "
            "WHERE kcl.concept_id = oc.concept_id)"
            if sqlite_object_exists(conn, "ksa_concept_links")
            else "0"
        )
        learning_module_count_sql = (
            "(SELECT COUNT(DISTINCT lmcl.learn_module_seq) "
            "FROM learning_module_concept_links AS lmcl "
            "WHERE lmcl.concept_id = oc.concept_id)"
            if sqlite_object_exists(conn, "learning_module_concept_links")
            else "0"
        )
        rows = conn.execute(
            f"""
            SELECT
                oc.concept_id, oc.concept_name, oc.normalized_key,
                oc.concept_type, oc.definition, oc.definition_status,
                oc.relation_status, oc.review_status,
                {alias_count_sql} AS alias_count,
                {relation_count_sql} AS relation_count,
                {ksa_count_sql} AS ksa_link_count,
                {criteria_count_sql} AS criteria_link_count,
                {learning_module_count_sql} AS learning_module_count
            FROM ontology_concepts oc
            {where}
            ORDER BY {ordering}
            LIMIT ?
            """,
            params + ordering_params + [clamp_limit(limit, default=20, maximum=100)],
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
@guard_public_tool
def get_concept_evidence(concept_id: int, limit: int = 20) -> dict[str, Any]:
    """Return source KSA, criteria, learning-module, relation, and recommendation evidence for a concept."""
    max_rows = clamp_limit(limit, default=20, maximum=100)
    with open_db() as conn:
        concept = concept_with_aliases(conn, concept_id)
        if concept is None:
            return error_response("concept_not_found", concept_id=concept_id)
        compact_ontology = has_compact_ontology_postings(conn)
        if compact_ontology:
            relation_rows = ontology_relation_rows
            outgoing_rows = relation_rows(conn, source_ids=[concept_id])
            incoming_rows = relation_rows(conn, target_ids=[concept_id])
            endpoint_ids = {
                int(row["target_concept_id"])
                for row in outgoing_rows
            } | {
                int(row["source_concept_id"])
                for row in incoming_rows
            }
            names: dict[int, dict[str, Any]] = {}
            if endpoint_ids:
                placeholders = ",".join("?" for _ in endpoint_ids)
                endpoint_rows = conn.execute(
                    f"""
                    SELECT concept_id, concept_name, concept_type
                    FROM ontology_concepts
                    WHERE concept_id IN ({placeholders})
                    """,
                    sorted(endpoint_ids),
                ).fetchall()
                names = {
                    int(row[0]): {
                        "concept_name": row[1],
                        "concept_type": row[2],
                    }
                    for row in endpoint_rows
                }
            outgoing = [
                {
                    "relation_id": row["relation_id"],
                    "relation_type": row["relation_type"],
                    "relation_label": row["relation_label"],
                    "review_status": row["review_status"],
                    "target_concept_id": row["target_concept_id"],
                    "target_concept_name": names.get(
                        int(row["target_concept_id"]), {}
                    ).get("concept_name"),
                    "target_concept_type": names.get(
                        int(row["target_concept_id"]), {}
                    ).get("concept_type"),
                }
                for row in outgoing_rows
            ]
            incoming = [
                {
                    "relation_id": row["relation_id"],
                    "relation_type": row["relation_type"],
                    "relation_label": row["relation_label"],
                    "review_status": row["review_status"],
                    "source_concept_id": row["source_concept_id"],
                    "source_concept_name": names.get(
                        int(row["source_concept_id"]), {}
                    ).get("concept_name"),
                    "source_concept_type": names.get(
                        int(row["source_concept_id"]), {}
                    ).get("concept_type"),
                }
                for row in incoming_rows
            ]
            outgoing.sort(key=lambda row: (row["relation_type"], row["target_concept_name"] or ""))
            incoming.sort(key=lambda row: (row["relation_type"], row["source_concept_name"] or ""))
            outgoing = outgoing[:max_rows]
            incoming = incoming[:max_rows]
        else:
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
        if has_compact_criteria_postings(conn):
            criteria_ids = concept_criteria_ids(conn, [concept_id]).get(concept_id, [])
            if criteria_ids:
                placeholders = ",".join("?" for _ in criteria_ids)
                criteria_rows = conn.execute(
                    f"""
                    SELECT
                        NULL AS link_id, 'criteria_concept_posting' AS relation_type,
                        'raw' AS link_status,
                        pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                        pc.criteria_text_refined, ce.element_id, ce.element_name_raw,
                        ce.unit_code, cu.unit_name_raw
                    FROM performance_criteria pc
                    JOIN competency_elements ce ON ce.element_id = pc.element_id
                    JOIN competency_units cu ON cu.unit_code = ce.unit_code
                    WHERE pc.criteria_id IN ({placeholders})
                    ORDER BY ce.unit_code, ce.element_id, pc.criteria_id
                    LIMIT ?
                    """,
                    [*criteria_ids, max_rows],
                ).fetchall()
            else:
                criteria_rows = []
        elif sqlite_object_exists(conn, "criteria_concept_links"):
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
        else:
            criteria_rows = []
        if sqlite_object_exists(conn, "learning_module_concept_links") and sqlite_object_exists(
            conn, "ncs_learning_modules"
        ):
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
        else:
            module_rows = []
        if all(
            sqlite_object_exists(conn, table)
            for table in (
                "education_recommendation_evidence",
                "education_recommendation_items",
                "education_recommendation_runs",
            )
        ):
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
        else:
            recommendation_rows = []
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


@mcp.tool()
def review_learning_module_ncs_link(
    link_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    run_artifact: str | None = None,
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
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale,
            evidence_refs=evidence_refs,
            run_artifact=run_artifact,
        )
    return tool_response(result)


@mcp.tool()
def review_training_goal_concept_link(
    link_id: int,
    review_status: str,
    reviewer_id: str = "mcp",
    notes: str = "",
    confidence_score: float | None = None,
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    run_artifact: str | None = None,
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
            review_status=status,
            blockers=provenance_blockers,
        )
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
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale or notes,
            evidence_refs=evidence_refs,
            created_by_tool="mcp.review_training_goal_concept_link",
            run_artifact=run_artifact,
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
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    run_artifact: str | None = None,
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
            review_status=status,
            blockers=provenance_blockers,
        )
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
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale or notes,
            evidence_refs=evidence_refs,
            created_by_tool="mcp.review_task_ksa_concept_relation",
            run_artifact=run_artifact,
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
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[str] | None = None,
    run_artifact: str | None = None,
) -> dict[str, Any]:
    """Human-review an ontology concept without mutating raw KSA source text."""
    with open_db() as conn:
        row = conn.execute("SELECT * FROM ontology_concepts WHERE concept_id = ?", (concept_id,)).fetchone()
        if row is None:
            return error_response("concept_not_found", concept_id=concept_id)
        target_review_status = review_status or row["review_status"]
        provenance_blockers = trusted_review_provenance_blockers(
            review_status=target_review_status,
            reviewer_id=reviewer_id,
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale,
        )
        if provenance_blockers:
            return error_response(
                "trusted_review_provenance_required",
                review_status=target_review_status,
                blockers=provenance_blockers,
            )
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
                target_review_status,
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
            new_status=target_review_status,
            reviewer_id=reviewer_id,
            notes=notes,
            source_decision_packet=source_decision_packet,
            source_artifact_hash=source_artifact_hash,
            rationale=rationale or notes,
            evidence_refs=evidence_refs,
            created_by_tool="mcp.review_ontology_concept",
            run_artifact=run_artifact,
        )
        updated = concept_with_aliases(conn, concept_id)
        conn.commit()
    return tool_response(
        {
            "concept_id": concept_id,
            "previous_status": row["review_status"],
            "new_status": target_review_status,
            "concept": updated,
        },
        audit={
            "data_sources": ["ontology_concepts", "ontology_concept_aliases", "review_audit_log"],
            "generated_at": now_utc(),
            "reviewer_id": reviewer_id,
        },
    )


# Backward-compatible direct-call aliases. These names are intentionally not
# decorated as MCP tools.
compare_raw_refined = READ_ONLY_LEGACY_HANDLERS.compare_raw_refined
get_api_join_status = READ_ONLY_LEGACY_HANDLERS.get_api_join_status
get_sqf_duties = READ_ONLY_LEGACY_HANDLERS.get_sqf_duties
search_sqf_jobs = READ_ONLY_LEGACY_HANDLERS.search_sqf_jobs
get_sqf_job_level = READ_ONLY_LEGACY_HANDLERS.get_sqf_job_level
build_sqf_ncs_mapping_candidates = _legacy_build_sqf_ncs_mapping_candidates
map_sqf_to_ncs = _legacy_map_sqf_to_ncs
analyze_gap = READ_ONLY_LEGACY_HANDLERS.analyze_gap
recommend_next_ncs_units = READ_ONLY_LEGACY_HANDLERS.recommend_next_ncs_units
explain_mapping = READ_ONLY_LEGACY_HANDLERS.explain_mapping
search_learning_modules = READ_ONLY_LEGACY_HANDLERS.search_learning_modules
get_learning_module = READ_ONLY_LEGACY_HANDLERS.get_learning_module
prepare_ontology_review_queue = _legacy_prepare_ontology_review_queue
collect_qualification_items = _legacy_collect_qualification_items
collect_job_base_competencies = _legacy_collect_job_base_competencies
get_learning_path_for_sqf_job = READ_ONLY_LEGACY_HANDLERS.get_learning_path_for_sqf_job
recommend_education_for_duty = _legacy_recommend_education_for_duty
explain_education_recommendation = READ_ONLY_LEGACY_HANDLERS.explain_education_recommendation
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
get_sqf_ontology_summary = READ_ONLY_LEGACY_HANDLERS.get_sqf_ontology_summary
search_sqf_document_chunks = READ_ONLY_LEGACY_HANDLERS.search_sqf_document_chunks
search_sqf_precision_matches = READ_ONLY_LEGACY_HANDLERS.search_sqf_precision_matches
get_sqf_ontology_job_level = READ_ONLY_LEGACY_HANDLERS.get_sqf_ontology_job_level


NCS_EXECUTABLE_TOOL_HANDLERS = {
    "ncs_search": ncs_search,
    "ncs_unit_detail": ncs_unit_detail,
    "ncs_training": ncs_training,
    "ncs_analysis": ncs_analysis,
    "recommend_training_for_task": recommend_training_for_task,
    "recommend_training_transition": recommend_training_transition,
    "plan_ncs_education_path": plan_ncs_education_path,
    "recommend_task_transitions": recommend_task_transitions,
    "get_concept_evidence": get_concept_evidence,
}


def current_mcp_tool_surface() -> dict[str, Any]:
    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    names = sorted(tools) if isinstance(tools, dict) else []
    name_set = set(names)
    settings = load_settings()
    operator_requested = bool(settings.operator_tools_enabled)
    read_only_mode = bool(getattr(settings, "read_only_mode", False))
    operator_enabled = operator_requested and not read_only_mode
    advanced_enabled = bool(getattr(settings, "advanced_tools_enabled", False))
    configured_tools = tool_registry.mcp_tools_for_mode(
        operator_tools_enabled=operator_enabled,
        advanced_tools_enabled=advanced_enabled,
    )
    return {
        "user_tools": sorted(tool_registry.USER_MCP_TOOLS & name_set),
        "operator_tools": sorted(tool_registry.OPERATOR_MCP_TOOLS & name_set),
        "operator_tools_enabled": operator_enabled,
        "operator_tools_requested": operator_requested,
        "operator_tools_blocked_by_read_only": operator_requested and read_only_mode,
        "hidden_operator_tools": sorted(tool_registry.OPERATOR_MCP_TOOLS - name_set),
        "advanced_tools_enabled": advanced_enabled,
        "hidden_advanced_tools": sorted(tool_registry.ADVANCED_MCP_TOOLS - name_set),
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
        settings = load_settings()
        operator_enabled = bool(settings.operator_tools_enabled) and not bool(
            getattr(settings, "read_only_mode", False)
        )
        if not operator_enabled:
            for name in tool_registry.OPERATOR_MCP_TOOLS:
                tools.pop(name, None)
        if not bool(getattr(settings, "advanced_tools_enabled", False)):
            for name in tool_registry.ADVANCED_MCP_TOOLS:
                tools.pop(name, None)


remove_inactive_mcp_tools()


def is_loopback_bind_host(host: str | None) -> bool:
    value = str(host or "").strip().lower()
    if value in {"", "localhost"}:
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def configure_transport(
    *,
    transport: str,
    host: str | None = None,
    port: int | None = None,
    stateful_http: bool = False,
    json_response: bool | None = None,
    allow_remote_bind: bool = False,
) -> None:
    global CURRENT_TRANSPORT
    effective_host = host or str(mcp.settings.host or "")
    if (
        transport in {"sse", "streamable-http"}
        and not allow_remote_bind
        and not is_loopback_bind_host(effective_host)
    ):
        raise ValueError(
            "Refusing to bind unauthenticated NCS MCP HTTP transport to a "
            "non-loopback host without --allow-remote-bind."
        )
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
        "--allow-remote-bind",
        action="store_true",
        help=(
            "Allow an unauthenticated HTTP transport to bind beyond loopback. "
            "Use only behind an institution-controlled private gateway."
        ),
    )
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
        allow_remote_bind=args.allow_remote_bind,
    )
    mcp.run(args.transport)


if __name__ == "__main__":
    main()
