from __future__ import annotations

import json

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    clamp_limit,
    connect,
    initialize_database,
    normalize_concept_key,
    now_utc,
    row_to_dict,
    rows_to_dicts,
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
from ncs_mcp.recommendation import (
    explain_education_recommendation as recommendation_explain_education,
    get_learning_path_for_sqf_job as recommendation_get_learning_path,
    get_learning_module as recommendation_get_learning_module,
    recommend_education_for_duty as recommendation_recommend_education,
    search_learning_modules as recommendation_search_learning_modules,
)
from ncs_mcp.sqf_sqlite import sqf_model_summary


mcp = FastMCP("ncs-mcp")


def db():
    conn = connect(load_settings().db_path)
    initialize_database(conn)
    return conn


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
    return tool_response(
        {"error": {"code": code, **fields}},
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


@mcp.tool()
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


@mcp.tool()
def search_ncs_units(
    keyword: str,
    major_code: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search NCS competency units by keyword. Alias for ontology-facing clients."""
    return get_competency_units(major_code=major_code, keyword=keyword, limit=limit)


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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
    return {"quality_issues": rows_to_dicts(rows)}


@mcp.tool()
def compare_raw_refined(target_type: str, target_id: str) -> dict[str, Any]:
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


@mcp.tool()
def get_api_join_status(
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


@mcp.tool()
def get_sqf_duties(
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


@mcp.tool()
def search_sqf_jobs(
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


@mcp.tool()
def get_sqf_job_level(
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


@mcp.tool()
def build_sqf_ncs_mapping_candidates(
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


@mcp.tool()
def map_sqf_to_ncs(
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


@mcp.tool()
def analyze_gap(
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


@mcp.tool()
def recommend_next_ncs_units(
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


@mcp.tool()
def explain_mapping(
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


@mcp.tool()
def search_learning_modules(
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


@mcp.tool()
def get_learning_module(learn_module_seq: str) -> dict[str, Any]:
    """Return one cached NCS learning module with unit and ontology concept links."""
    with open_db() as conn:
        result = recommendation_get_learning_module(conn, learn_module_seq)
    if "error" in result:
        return tool_response({"ok": False, **result})
    return tool_response({"ok": True, **result})


@mcp.tool()
def get_learning_path_for_sqf_job(
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


@mcp.tool()
def recommend_education_for_duty(
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


@mcp.tool()
def explain_education_recommendation(
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


@mcp.tool()
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


@mcp.tool()
def review_sqf_ncs_match(
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


@mcp.tool()
def get_sqf_ontology_summary() -> dict[str, Any]:
    """Return counts for the normalized SQF ontology and preprocessed document layer."""
    return tool_response(sqf_model_summary(load_settings().db_path))


@mcp.tool()
def search_sqf_document_chunks(
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


@mcp.tool()
def search_sqf_precision_matches(
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


@mcp.tool()
def get_sqf_ontology_job_level(source_key: str) -> dict[str, Any]:
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


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
