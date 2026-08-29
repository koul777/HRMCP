from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    clamp_limit,
    connect,
    initialize_database,
    normalize_concept_key,
    normalize_spaces,
    now_utc,
    rows_to_dicts,
)
from ncs_mcp.http_client import get_with_retries


DEFAULT_JOB_BASE_API_URL = "http://apis.data.go.kr/B490007/ncsJobBase/openapi19"


class JobBaseApiError(RuntimeError):
    pass


def _text(element: ElementTree.Element | None, tag: str) -> str:
    if element is None:
        return ""
    child = element.find(tag)
    return normalize_spaces(child.text or "") if child is not None else ""


def _int_value(value: Any) -> int:
    text = "" if value is None else str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else 0


def _split_factors(value: str) -> list[str]:
    seen: set[str] = set()
    factors: list[str] = []
    for part in re.split(r"[,，\n;/]+", value or ""):
        factor = normalize_spaces(part)
        if not factor:
            continue
        key = normalize_concept_key(factor)
        if key in seen:
            continue
        seen.add(key)
        factors.append(factor)
    return factors


def parse_job_base_xml(xml_text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(xml_text)
    data_info = root.find("dataInfo")
    rows = []
    for row in root.findall(".//row"):
        factor_text = _text(row, "jobBasCompeFactrNm")
        rows.append(
            {
                "ncs_lclas_cd": _text(row, "ncsLclasCd"),
                "ncs_lclas_cdnm": _text(row, "ncsLclasCdnm"),
                "ncs_mclas_cd": _text(row, "ncsMclasCd"),
                "ncs_mclas_cdnm": _text(row, "ncsMclasCdnm"),
                "ncs_sclas_cd": _text(row, "ncsSclasCd"),
                "ncs_sclas_cdnm": _text(row, "ncsSclasCdnm"),
                "ncs_subd_cd": _text(row, "ncsSubdCd"),
                "ncs_subd_cdnm": _text(row, "ncsSubdCdnm"),
                "ncs_cl_cd": _text(row, "ncsClCd"),
                "compe_unit_name": _text(row, "compeUnitName"),
                "job_base_competency_name": _text(row, "jobBasCompeName"),
                "job_base_factor_text": factor_text,
                "job_base_factors": _split_factors(factor_text),
            }
        )
    return {
        "code": _text(data_info, "code"),
        "message": _text(data_info, "message"),
        "total_page": _int_value(_text(data_info, "totalPage")),
        "page_no": _int_value(_text(data_info, "pageNo")),
        "num_of_rows": _int_value(_text(data_info, "numOfRows")),
        "total_count": _int_value(_text(data_info, "totCnt")),
        "rows": rows,
    }


def fetch_job_base_page(
    service_key: str,
    *,
    major_code: str,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 100,
    timeout: int = 30,
    api_url: str = DEFAULT_JOB_BASE_API_URL,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    import requests

    params: dict[str, Any] = {
        "serviceKey": unquote(service_key),
        "numOfRows": num_of_rows,
        "pageNo": page_no,
        "returnType": "xml",
        "ncsLclasCd": major_code,
    }
    if module_name:
        params["cdName"] = module_name
    try:
        response = get_with_retries(
            api_url,
            params=params,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
    except requests.RequestException as exc:
        raise JobBaseApiError(
            f"NCS job base API request failed: status=request_error, major_code={major_code}, "
            f"page_no={page_no}, error={type(exc).__name__}"
        ) from None
    if response.status_code >= 400:
        raise JobBaseApiError(
            f"NCS job base API request failed: status={response.status_code}, "
            f"major_code={major_code}, page_no={page_no}"
        )
    parsed = parse_job_base_xml(response.text)
    parsed["request"] = {
        "major_code": major_code,
        "module_name": module_name,
        "page_no": page_no,
        "num_of_rows": num_of_rows,
    }
    return parsed


def _upsert_competency(conn: sqlite3.Connection, competency_name: str, timestamp: str) -> int:
    normalized_key = normalize_concept_key(competency_name)
    conn.execute(
        """
        INSERT INTO ncs_job_base_competencies(
            competency_name, normalized_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?)
        ON CONFLICT(normalized_key) DO UPDATE SET
            competency_name = excluded.competency_name,
            updated_at = excluded.updated_at
        """,
        (competency_name, normalized_key, timestamp, timestamp),
    )
    return int(
        conn.execute(
            "SELECT job_base_competency_id FROM ncs_job_base_competencies WHERE normalized_key = ?",
            (normalized_key,),
        ).fetchone()["job_base_competency_id"]
    )


def _upsert_factor(
    conn: sqlite3.Connection,
    *,
    competency_id: int,
    factor_name: str,
    timestamp: str,
) -> int:
    normalized_key = normalize_concept_key(factor_name)
    conn.execute(
        """
        INSERT INTO ncs_job_base_factors(
            job_base_competency_id, factor_name, normalized_key, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(job_base_competency_id, normalized_key) DO UPDATE SET
            factor_name = excluded.factor_name,
            updated_at = excluded.updated_at
        """,
        (competency_id, factor_name, normalized_key, timestamp, timestamp),
    )
    return int(
        conn.execute(
            """
            SELECT job_base_factor_id
            FROM ncs_job_base_factors
            WHERE job_base_competency_id = ? AND normalized_key = ?
            """,
            (competency_id, normalized_key),
        ).fetchone()["job_base_factor_id"]
    )


def upsert_job_base_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    source_payload: dict[str, Any] | None = None,
) -> dict[str, int]:
    timestamp = now_utc()
    row_count = 0
    links_upserted = 0
    missing_local_units = 0
    for row in rows:
        unit_code = row.get("ncs_cl_cd")
        competency_name = row.get("job_base_competency_name")
        if not unit_code or not competency_name:
            continue
        competency_id = _upsert_competency(conn, competency_name, timestamp)
        factor_names = row.get("job_base_factors") or [""]
        factor_ids: list[int | None] = []
        for factor_name in factor_names:
            factor_ids.append(
                _upsert_factor(
                    conn,
                    competency_id=competency_id,
                    factor_name=factor_name,
                    timestamp=timestamp,
                )
                if factor_name
                else None
            )
        unit_exists = conn.execute(
            "SELECT 1 FROM competency_units WHERE unit_code = ?",
            (unit_code,),
        ).fetchone()
        if unit_exists is None:
            missing_local_units += 1
            row_count += 1
            continue
        payload = dict(source_payload or {})
        payload["row"] = row
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        for factor_id in factor_ids:
            conn.execute(
                """
                INSERT INTO ncs_unit_job_base_links(
                    unit_code, job_base_competency_id, job_base_factor_id,
                    ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm,
                    ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm,
                    compe_unit_name, link_method, confidence_score,
                    source_payload, api_fetched_at, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'ncs_cl_cd_exact', 1.0, ?, ?, 'auto_linked', ?, ?)
                ON CONFLICT(unit_code, job_base_competency_id, job_base_factor_id)
                DO UPDATE SET
                    ncs_lclas_cd = excluded.ncs_lclas_cd,
                    ncs_lclas_cdnm = excluded.ncs_lclas_cdnm,
                    ncs_mclas_cd = excluded.ncs_mclas_cd,
                    ncs_mclas_cdnm = excluded.ncs_mclas_cdnm,
                    ncs_sclas_cd = excluded.ncs_sclas_cd,
                    ncs_sclas_cdnm = excluded.ncs_sclas_cdnm,
                    ncs_subd_cd = excluded.ncs_subd_cd,
                    ncs_subd_cdnm = excluded.ncs_subd_cdnm,
                    compe_unit_name = excluded.compe_unit_name,
                    source_payload = excluded.source_payload,
                    api_fetched_at = excluded.api_fetched_at,
                    updated_at = excluded.updated_at
                """,
                (
                    unit_code,
                    competency_id,
                    factor_id,
                    row.get("ncs_lclas_cd"),
                    row.get("ncs_lclas_cdnm"),
                    row.get("ncs_mclas_cd"),
                    row.get("ncs_mclas_cdnm"),
                    row.get("ncs_sclas_cd"),
                    row.get("ncs_sclas_cdnm"),
                    row.get("ncs_subd_cd"),
                    row.get("ncs_subd_cdnm"),
                    row.get("compe_unit_name"),
                    payload_json,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            links_upserted += 1
        row_count += 1
    conn.commit()
    return {
        "rows_processed": row_count,
        "links_upserted": links_upserted,
        "missing_local_units": missing_local_units,
    }


def job_base_summary(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    max_rows = clamp_limit(limit, default=20, maximum=100)
    competency_count = int(conn.execute("SELECT COUNT(*) FROM ncs_job_base_competencies").fetchone()[0])
    factor_count = int(conn.execute("SELECT COUNT(*) FROM ncs_job_base_factors").fetchone()[0])
    link_count = int(conn.execute("SELECT COUNT(*) FROM ncs_unit_job_base_links").fetchone()[0])
    unit_count = int(conn.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0])
    linked_unit_count = int(
        conn.execute("SELECT COUNT(DISTINCT unit_code) FROM ncs_unit_job_base_links").fetchone()[0]
    )
    factorless_link_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ncs_unit_job_base_links
            WHERE job_base_factor_id IS NULL
            """
        ).fetchone()[0]
    )
    review_status_counts = {
        str(row["review_status"] or "unknown"): int(row["count"] or 0)
        for row in conn.execute(
            """
            SELECT COALESCE(review_status, 'unknown') AS review_status, COUNT(*) AS count
            FROM ncs_unit_job_base_links
            GROUP BY COALESCE(review_status, 'unknown')
            ORDER BY review_status
            """
        ).fetchall()
    }
    by_competency = rows_to_dicts(
        conn.execute(
            """
            SELECT c.job_base_competency_id, c.competency_name, COUNT(l.link_id) AS unit_link_count
            FROM ncs_job_base_competencies c
            LEFT JOIN ncs_unit_job_base_links l ON l.job_base_competency_id = c.job_base_competency_id
            GROUP BY c.job_base_competency_id
            ORDER BY unit_link_count DESC, c.competency_name
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
    )
    top_factors = rows_to_dicts(
        conn.execute(
            """
            SELECT
                c.job_base_competency_id,
                c.competency_name,
                f.job_base_factor_id,
                f.factor_name,
                COUNT(l.link_id) AS unit_link_count
            FROM ncs_unit_job_base_links l
            JOIN ncs_job_base_competencies c
              ON c.job_base_competency_id = l.job_base_competency_id
            JOIN ncs_job_base_factors f
              ON f.job_base_factor_id = l.job_base_factor_id
            GROUP BY c.job_base_competency_id, f.job_base_factor_id
            ORDER BY unit_link_count DESC, c.competency_name, f.factor_name
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
    )
    return {
        "ok": True,
        "job_base_competency_count": competency_count,
        "job_base_factor_count": factor_count,
        "unit_job_base_link_count": link_count,
        "linked_unit_count": linked_unit_count,
        "unit_count": unit_count,
        "unit_job_base_coverage": round(linked_unit_count / unit_count, 6) if unit_count else 0.0,
        "factorless_link_count": factorless_link_count,
        "links_with_factor_count": link_count - factorless_link_count,
        "avg_factors_per_linked_unit": round(link_count / linked_unit_count, 6) if linked_unit_count else 0.0,
        "review_status_counts": review_status_counts,
        "top_competencies": by_competency,
        "top_factors": top_factors,
    }


def _job_base_link_filters(
    *,
    unit_code: str | None,
    competency_name: str | None,
    factor_name: str | None,
    major_code: str | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if unit_code:
        clauses.append("l.unit_code = ?")
        params.append(unit_code)
    if competency_name:
        clauses.append("c.competency_name LIKE ?")
        params.append(f"%{competency_name}%")
    if factor_name:
        clauses.append("f.factor_name LIKE ?")
        params.append(f"%{factor_name}%")
    if major_code:
        clauses.append("cls.major_code = ?")
        params.append(major_code)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def count_job_base_links(
    conn: sqlite3.Connection,
    *,
    unit_code: str | None = None,
    competency_name: str | None = None,
    factor_name: str | None = None,
    major_code: str | None = None,
) -> int:
    """Count job-base links for bounded public pagination metadata."""

    where, params = _job_base_link_filters(
        unit_code=unit_code,
        competency_name=competency_name,
        factor_name=factor_name,
        major_code=major_code,
    )
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM ncs_unit_job_base_links l
        JOIN ncs_job_base_competencies c
          ON c.job_base_competency_id = l.job_base_competency_id
        LEFT JOIN ncs_job_base_factors f
          ON f.job_base_factor_id = l.job_base_factor_id
        LEFT JOIN competency_units cu ON cu.unit_code = l.unit_code
        LEFT JOIN classifications cls ON cls.classification_id = cu.classification_id
        {where}
        """,
        params,
    ).fetchone()
    return int(row[0] or 0) if row else 0


def search_job_base_links(
    conn: sqlite3.Connection,
    *,
    unit_code: str | None = None,
    competency_name: str | None = None,
    factor_name: str | None = None,
    major_code: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    where, params = _job_base_link_filters(
        unit_code=unit_code,
        competency_name=competency_name,
        factor_name=factor_name,
        major_code=major_code,
    )
    rows = conn.execute(
        f"""
        SELECT
            l.link_id, l.unit_code,
            l.job_base_competency_id, l.job_base_factor_id,
            l.link_method, l.confidence_score, l.review_status,
            c.competency_name, f.factor_name,
            cu.unit_name_raw AS unit_name,
            cls.major_code, cls.major_name,
            cls.middle_code, cls.middle_name,
            cls.small_code, cls.small_name,
            cls.sub_code, cls.sub_name
        FROM ncs_unit_job_base_links l
        JOIN ncs_job_base_competencies c
          ON c.job_base_competency_id = l.job_base_competency_id
        LEFT JOIN ncs_job_base_factors f
          ON f.job_base_factor_id = l.job_base_factor_id
        LEFT JOIN competency_units cu ON cu.unit_code = l.unit_code
        LEFT JOIN classifications cls ON cls.classification_id = cu.classification_id
        {where}
        ORDER BY c.competency_name, f.factor_name, l.unit_code
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=20, maximum=200)],
    ).fetchall()
    return rows_to_dicts(rows)


def job_base_profile_for_units(
    conn: sqlite3.Connection,
    unit_codes: set[str],
    *,
    limit: int = 300,
) -> list[dict[str, Any]]:
    if not unit_codes:
        return []
    rows = conn.execute(
        """
        SELECT
            l.link_id, l.unit_code, l.job_base_competency_id, l.job_base_factor_id,
            c.competency_name, f.factor_name,
            COUNT(*) OVER (PARTITION BY l.job_base_competency_id, l.job_base_factor_id) AS scope_frequency
        FROM ncs_unit_job_base_links l
        JOIN ncs_job_base_competencies c
          ON c.job_base_competency_id = l.job_base_competency_id
        LEFT JOIN ncs_job_base_factors f
          ON f.job_base_factor_id = l.job_base_factor_id
        WHERE l.unit_code IN (SELECT value FROM json_each(?))
        ORDER BY c.competency_name, f.factor_name, l.unit_code
        LIMIT ?
        """,
        (json.dumps(sorted(unit_codes)), clamp_limit(limit, default=300, maximum=1000)),
    ).fetchall()
    return rows_to_dicts(rows)


def collect_job_base_competencies(
    db_path: Path,
    service_key: str,
    *,
    major_code: str,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 500,
    max_pages: int | None = None,
    timeout: int = 30,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    pages_processed = 0
    rows_processed = 0
    links_upserted = 0
    missing_local_units = 0
    total_count = 0
    total_page = 0
    page = page_no
    errors: list[dict[str, str]] = []
    try:
        while True:
            try:
                payload = fetch_job_base_page(
                    service_key,
                    major_code=major_code,
                    module_name=module_name,
                    page_no=page,
                    num_of_rows=num_of_rows,
                    timeout=timeout,
                    max_retries=max_retries,
                    retry_backoff_seconds=retry_backoff_seconds,
                )
            except JobBaseApiError as exc:
                errors.append({"major_code": major_code, "page_no": str(page), "error": str(exc)})
                break
            if payload["code"] not in {"000", ""}:
                if payload["code"] != "002":
                    errors.append(
                        {
                            "major_code": major_code,
                            "page_no": str(page),
                            "error": payload["message"] or payload["code"],
                        }
                    )
                break
            total_count = payload["total_count"]
            total_page = payload["total_page"]
            upserted = upsert_job_base_rows(
                conn,
                payload["rows"],
                source_payload={"api": "ncsJobBase/openapi19", "request": payload["request"]},
            )
            rows_processed += upserted["rows_processed"]
            links_upserted += upserted["links_upserted"]
            missing_local_units += upserted["missing_local_units"]
            pages_processed += 1
            if max_pages and pages_processed >= max_pages:
                break
            if page >= total_page or not payload["rows"]:
                break
            page += 1
        summary = job_base_summary(conn, limit=10)
    finally:
        conn.close()
    return {
        "ok": not errors,
        "major_code": major_code,
        "module_name": module_name,
        "pages_processed": pages_processed,
        "rows_processed": rows_processed,
        "links_upserted": links_upserted,
        "missing_local_units": missing_local_units,
        "reported_total_count": total_count,
        "reported_total_page": total_page,
        "errors": errors[:20],
        "error_count": len(errors),
        "summary": summary,
    }


if __name__ == "__main__":
    settings = load_settings()
    if not settings.job_base_service_key:
        raise SystemExit("NCS_JOB_BASE_SERVICE_KEY or NCS_SERVICE_KEY is required")
    print(
        json.dumps(
            fetch_job_base_page(
                settings.job_base_service_key,
                major_code="02",
                num_of_rows=5,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
