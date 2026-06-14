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
from ncs_mcp.db import clamp_limit, connect, initialize_database, row_to_dict, rows_to_dicts
from ncs_mcp.ontology import (
    MVP_JOB_NAME,
    MVP_MAJOR_CODE,
    MVP_SQF_FIELD_NAME,
    ONTOLOGY_SCHEMA,
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
from ncs_mcp.mapping_policy import apply_mapping_filter
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
    return {
        "query": keyword,
        "major_code": major_code,
        "mvp_only": mvp_only,
        "sqf_jobs": jobs,
    }


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
    return {
        "target": {
            "source_key": source_key,
            "job_name": job_name,
            "duty_name": duty_name,
            "duty_level": duty_level,
        },
        "sqf_job_levels": result,
    }


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
            return {"error": "sqf_target_not_found", "source_key": source_key}
        if persist:
            summary = ontology_build_sqf_mapping_candidates(
                conn,
                source_key=source_key,
                mvp_only=False,
                limit_per_duty=limit,
            )
            mapping_status, matches, metadata = get_filtered_matches(conn, duty, limit=limit)
            return {
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
        candidates = generate_mapping_candidates(conn, duty, limit=max(limit, 50))
        filtered = apply_mapping_filter(candidates)
    return {
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
        return ontology_analyze_sqf_gap(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            mvp_only=target_job_name == MVP_JOB_NAME and target_source_key is None,
            limit=limit,
        )


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
        return ontology_recommend_next_ncs_units(
            conn,
            current_ncs_unit_codes=current_ncs_unit_codes,
            target_source_key=target_source_key,
            target_job_name=target_job_name,
            target_duty_name=target_duty_name,
            target_level=target_level,
            limit=limit,
        )


@mcp.tool()
def explain_mapping(
    sqf_source_key: str,
    ncs_unit_code: str,
) -> dict[str, Any]:
    """Explain why one SQF duty level maps to one NCS competency unit."""
    with open_db() as conn:
        return ontology_explain_mapping(
            conn,
            sqf_source_key=sqf_source_key,
            ncs_unit_code=ncs_unit_code,
        )


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
def recommend_education_for_duty(
    query: str,
    major_code: str | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Recommend education evidence for a desired duty using SQF duty profiles and NCS units."""
    clauses: list[str] = []
    params: list[Any] = []
    exact_filter(clauses, params, "sd.ncs_lclas_cd", major_code)
    pattern = f"%{query}%"
    clauses.append(
        """
        (
            sd.sqf_field_name LIKE ?
            OR sd.job_name LIKE ?
            OR sd.duty_name LIKE ?
            OR sd.duty_definition LIKE ?
            OR sd.autonomy_responsibility LIKE ?
            OR sd.duty_education_training LIKE ?
            OR sd.duty_qualification LIKE ?
        )
        """
    )
    params.extend([pattern] * 7)
    where = f"WHERE {' AND '.join(clauses)}"
    max_rows = clamp_limit(limit, default=5, maximum=20)
    with open_db() as conn:
        rows = conn.execute(
            f"""
            SELECT *
            FROM sqf_duties sd
            {where}
            ORDER BY
                CASE WHEN sd.duty_name LIKE ? THEN 0 ELSE 1 END,
                CASE WHEN sd.job_name LIKE ? THEN 0 ELSE 1 END,
                sd.ncs_lclas_cd, sd.duty_level, sd.duty_name
            LIMIT ?
            """,
            params + [pattern, pattern, max_rows],
        ).fetchall()
        recommendations = []
        for row in rows:
            mapping_status, matches, mapping_metadata = get_filtered_matches(conn, row, limit=5)
            direct_conditions = direct_sqf_conditions(row)
            unit_codes = [
                match["target"]["unit_code"]
                for match in matches
                if match.get("target", {}).get("unit_code")
            ]
            learning_objectives = build_learning_objectives_for_units(
                conn,
                unit_codes,
                limit_per_unit=3,
            )
            if direct_conditions and learning_objectives:
                recommendation_type = "mixed"
            elif direct_conditions:
                recommendation_type = "sqf_direct"
            else:
                recommendation_type = "ncs_derived"
            recommendations.append(
                {
                    "sqf_duty": {
                        "source_key": row["source_key"],
                        "ncs_lclas_cd": row["ncs_lclas_cd"],
                        "ncs_lclas_name": row["ncs_lclas_name"],
                        "sqf_field_name": row["sqf_field_name"],
                        "job_name": row["job_name"],
                        "duty_name": row["duty_name"],
                        "duty_level": row["duty_level"],
                        "duty_level_name": row["duty_level_name"],
                        "duty_definition": row["duty_definition"],
                    },
                    "recommendation_type": recommendation_type,
                    "source_sqf_fields": direct_conditions,
                    "education": row["duty_education_training"],
                    "qualification": row["duty_qualification"],
                    "career": row["duty_career"],
                    "license": row["duty_license"],
                    "mapping_status": mapping_status,
                    "related_ncs_units": [
                        {**match["target"], "mapping": match["mapping"]}
                        for match in matches
                    ],
                    "learning_objectives": learning_objectives,
                    "metadata": {
                        "data_source": "SQLite NCS/SQF knowledge base",
                        "query_scope": major_code or "all_sqf",
                        "used_refined_policy": "refined_if_approved",
                        **mapping_metadata,
                    },
                }
            )
    return {
        "query": query,
        "recommendations": recommendations,
        "note": (
            "SQF duty education fields are direct API evidence. "
            "When direct fields are sparse, NCS unit elements, performance criteria, and KSA "
            "are converted into learning objectives. This is not an official recognition decision."
        ),
    }


@mcp.tool()
def get_sqf_ontology_summary() -> dict[str, Any]:
    """Return counts for the normalized SQF ontology and preprocessed document layer."""
    return sqf_model_summary(load_settings().db_path)


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
    return {
        "query": query,
        "ontology_tag": ontology_tag,
        "chunks": rows_to_dicts(rows),
        "note": "Chunks are extracted evidence from SQF library files, not official recognition decisions.",
    }


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
    return {
        "query": query,
        "source_key": source_key,
        "min_score": min_score,
        "matches": rows_to_dicts(rows),
        "note": "These are candidate evidence links from report OCR/text chunks, not official recognition decisions.",
    }


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
            return {"error": "sqf_job_level_not_found", "source_key": source_key}
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


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
