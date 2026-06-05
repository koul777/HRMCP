from __future__ import annotations

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

from contextlib import contextmanager
from typing import Any

from mcp.server.fastmcp import FastMCP

from ncs_mcp.config import load_settings
from ncs_mcp.db import clamp_limit, connect, row_to_dict, rows_to_dicts


mcp = FastMCP("ncs-mcp")


def db():
    return connect(load_settings().db_path)


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


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
