from __future__ import annotations

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    clear_quality_issues,
    connect,
    initialize_database,
    insert_quality_issue,
    normalize_spaces,
    now_utc,
)


DEFAULT_API_URL = "https://c.q-net.or.kr/openapi/Ncs1info/ncsinfo.do"
DEFAULT_STANDARDS_API_BASE = "https://apis.data.go.kr/B490007/hrdkapi"
API_ISSUE_TYPES = [
    "api_unmatched",
    "api_value_mismatch",
    "api_element_unmatched",
    "api_element_value_mismatch",
]


def find_value(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for value in obj.values():
            found = find_value(value, key)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_value(value, key)
            if found is not None:
                return found
    return None


def extract_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = find_value(payload, "ncsInfo")
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def extract_standard_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = find_value(payload, "item")
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def prefer_latest_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = [item for item in items if as_text(item.get("USG_YN")).upper() == "Y"]
    return latest or items


def fetch_page(api_url: str, service_key: str, page_no: int, num_of_rows: int, timeout: int) -> dict[str, Any]:
    # data.go.kr exposes both encoded and decoded keys. requests encodes params,
    # so decode once here to avoid double-encoding encoded keys.
    normalized_key = unquote(service_key)
    params = {
        "type": "json",
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "ServiceKey": normalized_key,
    }
    try:
        response = requests.get(api_url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"API request failed: url={api_url}, page_no={page_no}, error={type(exc).__name__}"
        ) from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise RuntimeError(
            f"API request failed: url={api_url}, page_no={page_no}, status={response.status_code}"
        ) from None
    return response.json()


def fetch_standard_page(
    api_base_url: str,
    endpoint: str,
    service_key: str,
    page_no: int,
    num_of_rows: int,
    timeout: int,
    extra_params: dict[str, Any],
) -> dict[str, Any]:
    normalized_key = unquote(service_key)
    params = {
        "serviceKey": normalized_key,
        "pageNo": page_no,
        "numOfRows": num_of_rows,
        "returnType": "JSON",
    }
    params.update(extra_params)
    request_url = f"{api_base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    try:
        response = requests.get(request_url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
        raise RuntimeError(
            f"API request failed: url={request_url}, params={safe_params}, error={type(exc).__name__}"
        ) from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
        raise RuntimeError(
            f"API request failed: url={request_url}, params={safe_params}, status={response.status_code}"
        ) from None
    return response.json()


def save_raw_response(
    conn,
    api_url: str,
    page_no: int,
    num_of_rows: int,
    payload: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    total_count = find_value(payload, "totalCount")
    result_code = as_text(find_value(payload, "resultCode")) or None
    result_msg = as_text(find_value(payload, "resultMsg")) or None
    parsed_total = int(total_count) if str(total_count or "").isdigit() else None
    conn.execute(
        """
        INSERT INTO api_raw_responses(
            source_url, page_no, num_of_rows, total_count,
            result_code, result_msg, response_json, fetched_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_url, page_no, num_of_rows) DO UPDATE SET
            total_count = excluded.total_count,
            result_code = excluded.result_code,
            result_msg = excluded.result_msg,
            response_json = excluded.response_json,
            fetched_at = excluded.fetched_at
        """,
        (
            api_url,
            page_no,
            num_of_rows,
            parsed_total,
            result_code,
            result_msg,
            json.dumps(payload, ensure_ascii=False),
            now_utc(),
        ),
    )
    return parsed_total or 0, result_code, result_msg


def upsert_api_items(conn, items: list[dict[str, Any]]) -> int:
    count = 0
    fetched_at = now_utc()
    for item in items:
        ncs_cl_cd = as_text(item.get("ncsClCd"))
        if not ncs_cl_cd:
            continue
        conn.execute(
            """
            INSERT INTO api_competency_units(
                ncs_cl_cd, compe_unit_name, compe_unit_level,
                ncs_lclas_cdnm, ncs_mclas_cdnm, ncs_sclas_cdnm,
                ncs_subd_cdnm, compe_unit_def, api_fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ncs_cl_cd) DO UPDATE SET
                compe_unit_name = excluded.compe_unit_name,
                compe_unit_level = excluded.compe_unit_level,
                ncs_lclas_cdnm = excluded.ncs_lclas_cdnm,
                ncs_mclas_cdnm = excluded.ncs_mclas_cdnm,
                ncs_sclas_cdnm = excluded.ncs_sclas_cdnm,
                ncs_subd_cdnm = excluded.ncs_subd_cdnm,
                compe_unit_def = excluded.compe_unit_def,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                ncs_cl_cd,
                as_text(item.get("compeUnitName")),
                as_text(item.get("compeUnitLevel")),
                as_text(item.get("ncsLclasCdnm")),
                as_text(item.get("ncsMclasCdnm")),
                as_text(item.get("ncsSclasCdnm")),
                as_text(item.get("ncsSubdCdnm")),
                as_text(item.get("compeUnitDef")),
                fetched_at,
            ),
        )
        count += 1
    return count


def upsert_standard_unit_items(conn, items: list[dict[str, Any]]) -> int:
    count = 0
    fetched_at = now_utc()
    for item in prefer_latest_items(items):
        ncs_cl_cd = as_text(item.get("NCS_CL_CD"))
        if not ncs_cl_cd:
            continue
        conn.execute(
            """
            INSERT INTO api_competency_units(
                ncs_cl_cd, compe_unit_name, compe_unit_level,
                ncs_lclas_cdnm, ncs_mclas_cdnm, ncs_sclas_cdnm,
                ncs_subd_cdnm, compe_unit_def, api_fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ncs_cl_cd) DO UPDATE SET
                compe_unit_name = excluded.compe_unit_name,
                compe_unit_level = excluded.compe_unit_level,
                ncs_lclas_cdnm = excluded.ncs_lclas_cdnm,
                ncs_mclas_cdnm = excluded.ncs_mclas_cdnm,
                ncs_sclas_cdnm = excluded.ncs_sclas_cdnm,
                ncs_subd_cdnm = excluded.ncs_subd_cdnm,
                compe_unit_def = excluded.compe_unit_def,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                ncs_cl_cd,
                as_text(item.get("COMPE_UNIT_NAME")),
                as_text(item.get("COMPE_UNIT_LEVEL")),
                as_text(item.get("NCS_LCLAS_CDNM")),
                as_text(item.get("NCS_MCLAS_CDNM")),
                as_text(item.get("NCS_SCLAS_CDNM")),
                as_text(item.get("NCS_SUBD_CDNM")),
                as_text(item.get("COMPE_UNIT_DEF")),
                fetched_at,
            ),
        )
        count += 1
    return count


def upsert_standard_subd_items(conn, items: list[dict[str, Any]]) -> dict[str, int]:
    count = 0
    matched = 0
    orphan = 0
    for item in prefer_latest_items(items):
        codes = (
            as_text(item.get("NCS_LCLAS_CD")),
            as_text(item.get("NCS_MCLAS_CD")),
            as_text(item.get("NCS_SCLAS_CD")),
            as_text(item.get("NCS_SUBD_CD")),
        )
        if not all(codes):
            continue
        cursor = conn.execute(
            """
            UPDATE classifications
            SET duty_def_api = ?,
                duty_order = ?,
                api_ncs_degr = ?,
                api_usg_yn = ?
            WHERE major_code = ?
              AND middle_code = ?
              AND small_code = ?
              AND sub_code = ?
            """,
            (
                as_text(item.get("DUTY_DEF")),
                as_text(item.get("DUTY_ORD")),
                as_text(item.get("NCS_DEGR")),
                as_text(item.get("USG_YN")),
                *codes,
            ),
        )
        count += 1
        if cursor.rowcount:
            matched += 1
        else:
            orphan += 1
    return {"items": count, "matched": matched, "orphan": orphan}


def upsert_standard_element_items(conn, items: list[dict[str, Any]]) -> dict[str, int]:
    count = 0
    matched = 0
    orphan = 0
    mismatches = 0
    for item in prefer_latest_items(items):
        unit_code = as_text(item.get("NCS_CL_CD"))
        element_no = as_text(item.get("COMPE_UNIT_FACTR_NO"))
        if not unit_code or not element_no:
            continue
        element = conn.execute(
            """
            SELECT element_id, element_name_raw, element_level_raw
            FROM competency_elements
            WHERE unit_code = ? AND element_no = ?
            """,
            (unit_code, element_no),
        ).fetchone()
        count += 1
        api_name = as_text(item.get("COMPE_UNIT_FACTR_NAME"))
        api_level = as_text(item.get("COMPE_UNIT_FACTR_LEVEL"))
        if element is None:
            orphan += 1
            continue
        matched += 1
        conn.execute(
            """
            UPDATE competency_elements
            SET api_element_name = ?,
                api_element_level = ?,
                api_match_status = 'matched'
            WHERE element_id = ?
            """,
            (api_name, api_level, element["element_id"]),
        )
        comparisons = [
            ("element_name", element["element_name_raw"], api_name),
            ("element_level", element["element_level_raw"], api_level),
        ]
        for label, excel_value, api_value in comparisons:
            if normalize_spaces(as_text(excel_value)) != normalize_spaces(as_text(api_value)):
                mismatches += 1
                insert_quality_issue(
                    conn,
                    target_type="element",
                    target_id=element["element_id"],
                    issue_type="api_element_value_mismatch",
                    severity="warning",
                    issue_detail=f"{label} mismatch: excel='{excel_value}', api='{api_value}'",
                    suggested_action="Check whether the Excel DB and API version differ.",
                )
    return {
        "items": count,
        "matched": matched,
        "orphan": orphan,
        "mismatches": mismatches,
    }


def apply_api_join(conn) -> dict[str, int]:
    clear_quality_issues(conn, API_ISSUE_TYPES)
    conn.execute(
        """
        UPDATE competency_units
        SET api_unit_name = NULL,
            api_unit_level = NULL,
            api_definition = NULL,
            api_match_status = 'unmatched',
            updated_at = ?
        """,
        (now_utc(),),
    )

    api_rows = conn.execute("SELECT * FROM api_competency_units").fetchall()
    matched = 0
    orphan = 0
    mismatches = 0
    for api in api_rows:
        unit = conn.execute(
            """
            SELECT cu.*, c.major_name, c.middle_name, c.small_name, c.sub_name
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?
            """,
            (api["ncs_cl_cd"],),
        ).fetchone()
        if unit is None:
            orphan += 1
            continue
        matched += 1
        conn.execute(
            """
            UPDATE competency_units
            SET api_unit_name = ?,
                api_unit_level = ?,
                api_definition = ?,
                api_match_status = 'matched',
                updated_at = ?
            WHERE unit_code = ?
            """,
            (
                api["compe_unit_name"],
                api["compe_unit_level"],
                api["compe_unit_def"],
                now_utc(),
                api["ncs_cl_cd"],
            ),
        )
        comparisons = [
            ("능력단위명", unit["unit_name_raw"], api["compe_unit_name"]),
            ("능력단위수준", unit["unit_level_raw"], api["compe_unit_level"]),
            ("대분류명", unit["major_name"], api["ncs_lclas_cdnm"]),
            ("중분류명", unit["middle_name"], api["ncs_mclas_cdnm"]),
            ("소분류명", unit["small_name"], api["ncs_sclas_cdnm"]),
            ("세분류명", unit["sub_name"], api["ncs_subd_cdnm"]),
        ]
        for label, excel_value, api_value in comparisons:
            if normalize_spaces(as_text(excel_value)) != normalize_spaces(as_text(api_value)):
                mismatches += 1
                insert_quality_issue(
                    conn,
                    target_type="unit",
                    target_id=unit["unit_code"],
                    issue_type="api_value_mismatch",
                    severity="warning",
                    issue_detail=f"{label} 불일치: excel='{excel_value}', api='{api_value}'",
                    suggested_action="API와 엑셀 기준일 또는 버전 차이를 확인한다.",
                )

    unmatched_rows = conn.execute(
        "SELECT unit_code FROM competency_units WHERE api_match_status = 'unmatched'"
    ).fetchall()
    for row in unmatched_rows:
        insert_quality_issue(
            conn,
            target_type="unit",
            target_id=row["unit_code"],
            issue_type="api_unmatched",
            severity="info",
            issue_detail="Ncs1info API에서 동일한 능력단위코드를 찾지 못했다.",
            suggested_action="엑셀 원문을 기준으로 유지하고 API 커버리지 한계로 기록한다.",
        )

    conn.commit()
    return {
        "api_units": len(api_rows),
        "matched_units": matched,
        "api_orphan_units": orphan,
        "unmatched_excel_units": len(unmatched_rows),
        "api_value_mismatches": mismatches,
    }


def write_report(summary: dict[str, int], reports_dir: Path, filename: str = "api_join_report.md") -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "# NCS API 조인 리포트",
        "",
        f"- 생성시각: {now_utc()}",
        "",
        "| 항목 | 건수 |",
        "|---|---:|",
    ]
    for key, value in summary.items():
        lines.append(f"| `{key}` | {value:,} |")
    (reports_dir / filename).write_text("\n".join(lines) + "\n", encoding="utf-8")


def collect_api(
    db_path: Path,
    reports_dir: Path,
    service_key: str,
    api_url: str = DEFAULT_API_URL,
    num_of_rows: int = 100,
    timeout: int = 30,
) -> dict[str, int]:
    conn = connect(db_path)
    initialize_database(conn)
    total_count = 0
    total_items = 0

    first_payload = fetch_page(api_url, service_key, 1, num_of_rows, timeout)
    total_count, result_code, result_msg = save_raw_response(
        conn, api_url, 1, num_of_rows, first_payload
    )
    if result_code and result_code not in {"00", "0"}:
        raise RuntimeError(f"API returned {result_code}: {result_msg}")
    first_items = extract_items(first_payload)
    total_items += upsert_api_items(conn, first_items)

    pages = max(1, ceil(total_count / num_of_rows)) if total_count else 1
    for page_no in range(2, pages + 1):
        payload = fetch_page(api_url, service_key, page_no, num_of_rows, timeout)
        save_raw_response(conn, api_url, page_no, num_of_rows, payload)
        total_items += upsert_api_items(conn, extract_items(payload))
        conn.commit()

    conn.commit()
    summary = apply_api_join(conn)
    summary["api_items_upserted"] = total_items
    summary["api_total_count"] = total_count
    write_report(summary, reports_dir)
    conn.close()
    return summary


def classification_params(row: sqlite3.Row) -> dict[str, str]:
    return {
        "NCS_LCLAS_CD": row["major_code"],
        "NCS_MCLAS_CD": row["middle_code"],
        "NCS_SCLAS_CD": row["small_code"],
        "NCS_SUBD_CD": row["sub_code"],
    }


def subd_parent_params(row: sqlite3.Row) -> dict[str, str]:
    return {
        "NCS_LCLAS_CD": row["major_code"],
        "NCS_MCLAS_CD": row["middle_code"],
        "NCS_SCLAS_CD": row["small_code"],
    }


def element_params(row: sqlite3.Row) -> dict[str, str]:
    return {
        "NCS_CL_CD": row["unit_code"],
        "COMPE_UNIT_FACTR_NO": row["element_no"],
        "USG_YN": "Y",
    }


def source_url_for_standard(api_base_url: str, endpoint: str, params: dict[str, Any]) -> str:
    query = "&".join(f"{key}={params[key]}" for key in sorted(params))
    return f"{api_base_url.rstrip('/')}/{endpoint}?{query}"


def standard_classification_completed(conn, source_url: str, num_of_rows: int) -> bool:
    rows = conn.execute(
        """
        SELECT page_no, total_count, result_code
        FROM api_raw_responses
        WHERE source_url = ?
        """,
        (source_url,),
    ).fetchall()
    if not rows:
        return False
    success_pages = {
        int(row["page_no"])
        for row in rows
        if as_text(row["result_code"]) in {"00", "0"}
    }
    first = next((row for row in rows if int(row["page_no"]) == 1), None)
    if first is None or as_text(first["result_code"]) not in {"00", "0"}:
        return False
    total_count = int(first["total_count"] or 0)
    pages = max(1, ceil(total_count / num_of_rows)) if total_count else 1
    return all(page_no in success_pages for page_no in range(1, pages + 1))


def collect_standard_api(
    db_path: Path,
    reports_dir: Path,
    service_key: str,
    api_base_url: str = DEFAULT_STANDARDS_API_BASE,
    num_of_rows: int = 100,
    timeout: int = 30,
    classification_limit: int | None = None,
) -> dict[str, int]:
    """Collect approved HRDK NCS standards API (/NCS005) and join unit definitions.

    /NCS005 requires uppercase classification parameters:
    NCS_LCLAS_CD, NCS_MCLAS_CD, NCS_SCLAS_CD, NCS_SUBD_CD.
    """
    conn = connect(db_path)
    initialize_database(conn)
    rows = conn.execute(
        """
        SELECT major_code, middle_code, small_code, sub_code
        FROM classifications
        ORDER BY major_code, middle_code, small_code, sub_code
        """
    ).fetchall()
    if classification_limit is not None:
        rows = rows[:classification_limit]

    total_api_items = 0
    successful_classifications = 0
    failed_classifications = 0
    skipped_classifications = 0
    endpoint = "NCS005"

    for row in rows:
        params = classification_params(row)
        source_url = source_url_for_standard(api_base_url, endpoint, params)
        if standard_classification_completed(conn, source_url, num_of_rows):
            skipped_classifications += 1
            successful_classifications += 1
            continue
        page_no = 1
        pages = 1
        classification_success = False
        while page_no <= pages:
            payload = fetch_standard_page(
                api_base_url=api_base_url,
                endpoint=endpoint,
                service_key=service_key,
                page_no=page_no,
                num_of_rows=num_of_rows,
                timeout=timeout,
                extra_params=params,
            )
            total_count, result_code, result_msg = save_raw_response(
                conn, source_url, page_no, num_of_rows, payload
            )
            if result_code and result_code not in {"00", "0"}:
                failed_classifications += 1
                break
            items = extract_standard_items(payload)
            total_api_items += upsert_standard_unit_items(conn, items)
            pages = max(1, ceil(total_count / num_of_rows)) if total_count else 1
            page_no += 1
            classification_success = True
            conn.commit()
        if classification_success:
            successful_classifications += 1

    conn.commit()
    summary = apply_api_join(conn)
    summary["api_items_upserted"] = total_api_items
    summary["api_total_count"] = total_api_items
    summary["standards_classifications_requested"] = len(rows)
    summary["standards_classifications_successful"] = successful_classifications
    summary["standards_classifications_failed"] = failed_classifications
    summary["standards_classifications_skipped"] = skipped_classifications
    write_report(summary, reports_dir)
    conn.close()
    return summary


def collect_subd_api(
    db_path: Path,
    reports_dir: Path,
    service_key: str,
    api_base_url: str = DEFAULT_STANDARDS_API_BASE,
    num_of_rows: int = 100,
    timeout: int = 30,
    classification_limit: int | None = None,
) -> dict[str, int]:
    """Collect approved HRDK NCS standards API (/NCS004) and enrich duty definitions."""
    conn = connect(db_path)
    initialize_database(conn)
    rows = conn.execute(
        """
        SELECT major_code, middle_code, small_code
        FROM classifications
        GROUP BY major_code, middle_code, small_code
        ORDER BY major_code, middle_code, small_code
        """
    ).fetchall()
    if classification_limit is not None:
        rows = rows[:classification_limit]

    endpoint = "NCS004"
    successful_classifications = 0
    failed_classifications = 0
    skipped_classifications = 0
    total_items = 0
    matched_items = 0
    orphan_items = 0

    for row in rows:
        params = subd_parent_params(row)
        source_url = source_url_for_standard(api_base_url, endpoint, params)
        if standard_classification_completed(conn, source_url, num_of_rows):
            skipped_classifications += 1
            successful_classifications += 1
            continue
        page_no = 1
        pages = 1
        classification_success = False
        while page_no <= pages:
            payload = fetch_standard_page(
                api_base_url=api_base_url,
                endpoint=endpoint,
                service_key=service_key,
                page_no=page_no,
                num_of_rows=num_of_rows,
                timeout=timeout,
                extra_params=params,
            )
            total_count, result_code, result_msg = save_raw_response(
                conn, source_url, page_no, num_of_rows, payload
            )
            if result_code and result_code not in {"00", "0"}:
                failed_classifications += 1
                break
            stats = upsert_standard_subd_items(conn, extract_standard_items(payload))
            total_items += stats["items"]
            matched_items += stats["matched"]
            orphan_items += stats["orphan"]
            pages = max(1, ceil(total_count / num_of_rows)) if total_count else 1
            page_no += 1
            classification_success = True
            conn.commit()
        if classification_success:
            successful_classifications += 1

    conn.commit()
    missing_duty_def = conn.execute(
        """
        SELECT COUNT(*)
        FROM classifications
        WHERE duty_def_api IS NULL OR TRIM(duty_def_api) = ''
        """
    ).fetchone()[0]
    summary = {
        "subd_parent_classifications_requested": len(rows),
        "subd_parent_classifications_successful": successful_classifications,
        "subd_parent_classifications_failed": failed_classifications,
        "subd_parent_classifications_skipped": skipped_classifications,
        "subd_items_upserted": total_items,
        "subd_items_matched": matched_items,
        "subd_items_orphan": orphan_items,
        "classifications_missing_duty_def": int(missing_duty_def),
    }
    write_report(summary, reports_dir, filename="api_subd_report.md")
    conn.close()
    return summary


def collect_elements_api(
    db_path: Path,
    reports_dir: Path,
    service_key: str,
    api_base_url: str = DEFAULT_STANDARDS_API_BASE,
    num_of_rows: int = 100,
    timeout: int = 30,
    element_limit: int | None = None,
    element_offset: int = 0,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    only_uncollected: bool = False,
    retry_failed: bool = False,
    concurrency: int = 1,
    max_retries: int = 2,
) -> dict[str, int]:
    """Collect approved HRDK NCS standards API (/NCS006) and enrich elements.

    /NCS006 is one call per competency element, so use element_limit/offset
    for full-database batches under the daily API traffic limit.
    """
    conn = connect(db_path)
    initialize_database(conn)
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in [
        ("c.major_code", major_code),
        ("c.middle_code", middle_code),
        ("c.small_code", small_code),
        ("c.sub_code", sub_code),
    ]:
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if only_uncollected:
        if retry_failed:
            clauses.append("ce.api_match_status IN ('not_collected', 'api_failed')")
        else:
            clauses.append("ce.api_match_status = 'not_collected'")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if element_limit is not None:
        limit_sql = "LIMIT ? OFFSET ?"
        params.extend([element_limit, element_offset])
    rows = conn.execute(
        f"""
        SELECT ce.element_id, ce.unit_code, ce.element_no
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY ce.unit_code, CAST(ce.element_no AS INTEGER), ce.element_id
        {limit_sql}
        """,
        params,
    ).fetchall()

    endpoint = "NCS006"
    requested_elements = len(rows)
    successful_elements = 0
    failed_elements = 0
    skipped_elements = 0
    total_items = 0
    matched_items = 0
    orphan_items = 0
    mismatches = 0

    def fetch_one(row: sqlite3.Row) -> dict[str, Any]:
        params_for_api = element_params(row)
        source_url = source_url_for_standard(api_base_url, endpoint, params_for_api)
        last_error = ""
        for attempt in range(max_retries + 1):
            try:
                payload = fetch_standard_page(
                    api_base_url=api_base_url,
                    endpoint=endpoint,
                    service_key=service_key,
                    page_no=1,
                    num_of_rows=num_of_rows,
                    timeout=timeout,
                    extra_params=params_for_api,
                )
                return {
                    "element_id": row["element_id"],
                    "source_url": source_url,
                    "payload": payload,
                    "error": None,
                }
            except RuntimeError as exc:
                last_error = str(exc)
                if attempt < max_retries:
                    time.sleep(min(2**attempt, 8))
        return {
            "element_id": row["element_id"],
            "source_url": source_url,
            "payload": None,
            "error": last_error,
        }

    pending_rows: list[sqlite3.Row] = []
    for row in rows:
        params_for_api = element_params(row)
        source_url = source_url_for_standard(api_base_url, endpoint, params_for_api)
        if standard_classification_completed(conn, source_url, num_of_rows):
            skipped_elements += 1
            successful_elements += 1
        else:
            pending_rows.append(row)

    def process_result(result: dict[str, Any]) -> None:
        nonlocal failed_elements
        nonlocal successful_elements
        nonlocal total_items
        nonlocal matched_items
        nonlocal orphan_items
        nonlocal mismatches
        if result["error"]:
            failed_elements += 1
            conn.execute(
                "UPDATE competency_elements SET api_match_status = ? WHERE element_id = ?",
                ("api_failed", result["element_id"]),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=result["element_id"],
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry later with --only-uncollected.",
            )
            conn.commit()
            return

        payload = result["payload"]
        _total_count, result_code, result_msg = save_raw_response(
            conn, result["source_url"], 1, num_of_rows, payload
        )
        if result_code and result_code not in {"00", "0"}:
            failed_elements += 1
            status = "no_data" if result_code == "03" else "api_failed"
            conn.execute(
                "UPDATE competency_elements SET api_match_status = ? WHERE element_id = ?",
                (status, result["element_id"]),
            )
            conn.commit()
            return
        stats = upsert_standard_element_items(conn, extract_standard_items(payload))
        total_items += stats["items"]
        matched_items += stats["matched"]
        orphan_items += stats["orphan"]
        mismatches += stats["mismatches"]
        successful_elements += 1
        conn.commit()

    workers = max(1, min(int(concurrency or 1), 10))
    if workers == 1:
        for row in pending_rows:
            process_result(fetch_one(row))
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_one, row) for row in pending_rows]
            for future in as_completed(futures):
                process_result(future.result())

    missing = conn.execute(
        """
        SELECT COUNT(*)
        FROM competency_elements
        WHERE api_match_status != 'matched'
        """
    ).fetchone()[0]
    summary = {
        "elements_requested": requested_elements,
        "elements_successful": successful_elements,
        "elements_failed": failed_elements,
        "elements_skipped": skipped_elements,
        "element_concurrency": workers,
        "element_items_upserted": total_items,
        "element_items_matched": matched_items,
        "element_items_orphan": orphan_items,
        "element_value_mismatches": mismatches,
        "elements_not_matched_total": int(missing),
    }
    write_report(summary, reports_dir, filename="api_elements_report.md")
    conn.close()
    return summary


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Collect NCS API and join SQLite DB.")
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    parser.add_argument("--reports-dir", type=Path, default=settings.reports_dir)
    parser.add_argument("--service-key", default=settings.service_key)
    parser.add_argument(
        "--mode",
        choices=["standards", "subd", "elements", "ncs1info"],
        default="standards",
        help=(
            "standards uses approved hrdkapi /NCS005. "
            "subd uses approved hrdkapi /NCS004. "
            "elements uses approved hrdkapi /NCS006. "
            "ncs1info uses legacy c.q-net endpoint."
        ),
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--api-base-url", default=DEFAULT_STANDARDS_API_BASE)
    parser.add_argument("--num-of-rows", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--classification-limit", type=int)
    parser.add_argument("--element-limit", type=int)
    parser.add_argument("--element-offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--major-code")
    parser.add_argument("--middle-code")
    parser.add_argument("--small-code")
    parser.add_argument("--sub-code")
    parser.add_argument("--only-uncollected", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.service_key:
        raise SystemExit("NCS_SERVICE_KEY is required. Set it in .env or pass --service-key.")
    if args.mode == "ncs1info":
        summary = collect_api(
            db_path=args.db_path,
            reports_dir=args.reports_dir,
            service_key=args.service_key,
            api_url=args.api_url,
            num_of_rows=args.num_of_rows,
            timeout=args.timeout,
        )
    elif args.mode == "subd":
        summary = collect_subd_api(
            db_path=args.db_path,
            reports_dir=args.reports_dir,
            service_key=args.service_key,
            api_base_url=args.api_base_url,
            num_of_rows=args.num_of_rows,
            timeout=args.timeout,
            classification_limit=args.classification_limit,
        )
    elif args.mode == "elements":
        summary = collect_elements_api(
            db_path=args.db_path,
            reports_dir=args.reports_dir,
            service_key=args.service_key,
            api_base_url=args.api_base_url,
            num_of_rows=args.num_of_rows,
            timeout=args.timeout,
            element_limit=args.element_limit,
            element_offset=args.element_offset,
            major_code=args.major_code,
            middle_code=args.middle_code,
            small_code=args.small_code,
            sub_code=args.sub_code,
            only_uncollected=args.only_uncollected,
            retry_failed=args.retry_failed,
            concurrency=args.concurrency,
            max_retries=args.max_retries,
        )
    else:
        summary = collect_standard_api(
            db_path=args.db_path,
            reports_dir=args.reports_dir,
            service_key=args.service_key,
            api_base_url=args.api_base_url,
            num_of_rows=args.num_of_rows,
            timeout=args.timeout,
            classification_limit=args.classification_limit,
        )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
