from __future__ import annotations

if __package__ in {None, ""}:  # pragma: no cover - direct script support
    import sys
    from pathlib import Path

    sys.path.append(str(Path(__file__).resolve().parents[1]))

import argparse
import hashlib
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html import unescape as html_unescape
from math import ceil
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import requests

from ncs_mcp.config import load_settings
from ncs_mcp.api_quality import (
    API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE,
    API_ELEMENT_FAILURE_ISSUE_TYPES,
)
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
DEFAULT_SQF_API_BASE = "https://apis.data.go.kr/B490007/ncsSqfDuty"
API_ISSUE_TYPES = [
    "api_unmatched",
    "api_value_mismatch",
    "api_element_unmatched",
    "api_element_collection_failure",
    "api_element_value_mismatch",
]

API_UNMATCHED_DIAGNOSIS_ISSUE_TYPES = (
    "api_element_unmatched",
    "api_element_collection_failure",
    "api_element_value_mismatch",
    "api_value_mismatch",
)


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


def extract_sqf_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    items = data.get("row") if isinstance(data, dict) else find_value(payload, "row")
    if items is None:
        return []
    if isinstance(items, dict):
        return [items]
    if isinstance(items, list):
        return [item for item in items if isinstance(item, dict)]
    return []


def extract_sqf_data_info(payload: dict[str, Any]) -> dict[str, Any]:
    data_info = payload.get("dataInfo") if isinstance(payload, dict) else None
    return data_info if isinstance(data_info, dict) else {}


def as_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def normalize_api_compare_text(value: Any) -> str:
    text = html_unescape(as_text(value)).replace("&;", "&")
    return normalize_spaces(text)


def parse_json_api_response(response: requests.Response, *, url: str, context: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        content_type = response.headers.get("Content-Type", "") if response.headers else ""
        raise RuntimeError(
            "API response was not JSON: "
            f"url={url}, {context}, status={response.status_code}, "
            f"content_type={content_type or 'unknown'}"
        ) from None
    if not isinstance(payload, dict):
        raise RuntimeError(
            "API response JSON root was not an object: "
            f"url={url}, {context}, status={response.status_code}, "
            f"json_type={type(payload).__name__}"
        )
    return payload


def insert_api_quality_issue_once(
    conn,
    *,
    target_type: str,
    target_id: str | int,
    issue_type: str,
    severity: str,
    issue_detail: str,
    suggested_action: str,
) -> bool:
    exists = conn.execute(
        """
        SELECT 1
        FROM quality_issues
        WHERE target_type = ?
          AND target_id = ?
          AND issue_type = ?
          AND issue_detail = ?
          AND resolved_at IS NULL
        LIMIT 1
        """,
        (target_type, str(target_id), issue_type, issue_detail),
    ).fetchone()
    if exists:
        return False
    insert_quality_issue(
        conn,
        target_type=target_type,
        target_id=target_id,
        issue_type=issue_type,
        severity=severity,
        issue_detail=issue_detail,
        suggested_action=suggested_action,
    )
    return True


def as_int(value: Any, default: int = 0) -> int:
    text = as_text(value).replace(",", "")
    return int(text) if text.isdigit() else default


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
            f"API request failed: url={api_url}, page_no={page_no}, "
            f"error={type(exc).__name__}, detail={exc}"
        ) from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        raise RuntimeError(
            f"API request failed: url={api_url}, page_no={page_no}, status={response.status_code}"
        ) from None
    return parse_json_api_response(response, url=api_url, context=f"page_no={page_no}")


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
            f"API request failed: url={request_url}, params={safe_params}, "
            f"error={type(exc).__name__}, detail={exc}"
        ) from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
        raise RuntimeError(
            f"API request failed: url={request_url}, params={safe_params}, status={response.status_code}"
        ) from None
    safe_params = {key: value for key, value in params.items() if key != "serviceKey"}
    return parse_json_api_response(response, url=request_url, context=f"params={safe_params}")


def save_raw_response(
    conn,
    api_url: str,
    page_no: int,
    num_of_rows: int,
    payload: dict[str, Any],
) -> tuple[int, str | None, str | None]:
    total_count = find_value(payload, "totalCount") or find_value(payload, "totCnt")
    result_code = as_text(find_value(payload, "resultCode") or find_value(payload, "code")) or None
    result_msg = as_text(find_value(payload, "resultMsg") or find_value(payload, "message")) or None
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
        conn.execute(
            """
            UPDATE quality_issues
            SET resolved_at = ?
            WHERE target_type = 'element'
              AND target_id = ?
              AND issue_type IN (?, ?)
              AND resolved_at IS NULL
            """,
            (now_utc(), str(element["element_id"]), *API_ELEMENT_FAILURE_ISSUE_TYPES),
        )
        comparisons = [
            ("element_name", element["element_name_raw"], api_name),
            ("element_level", element["element_level_raw"], api_level),
        ]
        for label, excel_value, api_value in comparisons:
            if normalize_api_compare_text(excel_value) != normalize_api_compare_text(api_value):
                inserted = insert_api_quality_issue_once(
                    conn,
                    target_type="element",
                    target_id=element["element_id"],
                    issue_type="api_element_value_mismatch",
                    severity="warning",
                    issue_detail=f"{label} mismatch: excel='{excel_value}', api='{api_value}'",
                    suggested_action="Check whether the Excel DB and API version differ.",
                )
                if inserted:
                    mismatches += 1
    return {
        "items": count,
        "matched": matched,
        "orphan": orphan,
        "mismatches": mismatches,
    }


def resolve_element_unmatched_issue(conn, element_id: str | int) -> None:
    conn.execute(
        """
        UPDATE quality_issues
        SET resolved_at = ?
        WHERE target_type = 'element'
          AND target_id = ?
          AND issue_type IN (?, ?)
          AND resolved_at IS NULL
        """,
        (now_utc(), str(element_id), *API_ELEMENT_FAILURE_ISSUE_TYPES),
    )


def field_text(item: dict[str, Any], *names: str) -> str:
    for name in names:
        value = as_text(item.get(name))
        if value:
            return value
    return ""


def sqf_source_key(row: dict[str, str]) -> str:
    parts = [
        row["ncs_lclas_cd"],
        row["sqf_field_name"],
        row["sqf_sub_field_name"],
        row["job_name"],
        row["duty_name"],
        row["duty_level"],
        row["duty_level_name"],
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"sqf:{digest}"


def normalize_sqf_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "ncs_lclas_cd": field_text(item, "ncsLclasCd"),
        "ncs_lclas_name": field_text(item, "ncsLclasCdnm"),
        "sqf_field_name": field_text(item, "sqfFldCdnm"),
        "sqf_sub_field_name": field_text(
            item,
            "sqfSubFldCdnm",
            "sqfFldSubCdnm",
            "subSqfFldCdnm",
            "sqfSclasCdnm",
        ),
        "job_name": field_text(item, "jobCdnm"),
        "duty_name": field_text(item, "dutyNm"),
        "duty_level": field_text(item, "dutyLevel"),
        "duty_level_name": field_text(item, "dutyLevelNm"),
        "duty_level_definition": field_text(item, "dutyLevelDef", "dutyLevelDefinition"),
        "duty_definition": field_text(item, "dutyDef"),
        "autonomy_responsibility": field_text(item, "autoResp"),
        "duty_acarr": field_text(item, "dutyAcarr"),
        "duty_education_training": field_text(item, "dutyEduTrain"),
        "duty_qualification": field_text(item, "dutyQualf"),
        "duty_career": field_text(item, "dutyCarr"),
        "duty_license": field_text(item, "dutyLice"),
        "duty_remark": field_text(item, "dutyRemk"),
    }


def upsert_sqf_items(conn, items: list[dict[str, Any]]) -> int:
    count = 0
    fetched_at = now_utc()
    for item in items:
        row = normalize_sqf_item(item)
        if not row["ncs_lclas_cd"] or not row["duty_name"]:
            continue
        source_key = sqf_source_key(row)
        conn.execute(
            """
            INSERT INTO sqf_duties(
                source_key, ncs_lclas_cd, ncs_lclas_name,
                sqf_field_name, sqf_sub_field_name, job_name,
                duty_name, duty_level, duty_level_name, duty_level_definition,
                duty_definition, autonomy_responsibility, duty_acarr,
                duty_education_training, duty_qualification, duty_career,
                duty_license, duty_remark, source_payload, api_fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                ncs_lclas_name = excluded.ncs_lclas_name,
                sqf_field_name = excluded.sqf_field_name,
                sqf_sub_field_name = excluded.sqf_sub_field_name,
                job_name = excluded.job_name,
                duty_name = excluded.duty_name,
                duty_level = excluded.duty_level,
                duty_level_name = excluded.duty_level_name,
                duty_level_definition = excluded.duty_level_definition,
                duty_definition = excluded.duty_definition,
                autonomy_responsibility = excluded.autonomy_responsibility,
                duty_acarr = excluded.duty_acarr,
                duty_education_training = excluded.duty_education_training,
                duty_qualification = excluded.duty_qualification,
                duty_career = excluded.duty_career,
                duty_license = excluded.duty_license,
                duty_remark = excluded.duty_remark,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                source_key,
                row["ncs_lclas_cd"],
                row["ncs_lclas_name"],
                row["sqf_field_name"],
                row["sqf_sub_field_name"],
                row["job_name"],
                row["duty_name"],
                row["duty_level"],
                row["duty_level_name"],
                row["duty_level_definition"],
                row["duty_definition"],
                row["autonomy_responsibility"],
                row["duty_acarr"],
                row["duty_education_training"],
                row["duty_qualification"],
                row["duty_career"],
                row["duty_license"],
                row["duty_remark"],
                json.dumps(item, ensure_ascii=False),
                fetched_at,
            ),
        )
        count += 1
    return count


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
            if normalize_api_compare_text(excel_value) != normalize_api_compare_text(api_value):
                inserted = insert_api_quality_issue_once(
                    conn,
                    target_type="unit",
                    target_id=unit["unit_code"],
                    issue_type="api_value_mismatch",
                    severity="warning",
                    issue_detail=f"{label} 불일치: excel='{excel_value}', api='{api_value}'",
                    suggested_action="API와 엑셀 기준일 또는 버전 차이를 확인한다.",
                )
                if inserted:
                    mismatches += 1

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


API_MISMATCH_DETAIL_PATTERN = re.compile(
    r"^(?P<label>.+?) (?:mismatch|불일치): excel='(?P<excel>.*)', api='(?P<api>.*)'$"
)


def _api_quality_issue_counts(conn) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT issue_type, severity, COUNT(*) AS count
        FROM quality_issues
        WHERE issue_type IN (
            'api_unmatched',
            'api_value_mismatch',
            'api_element_unmatched',
            'api_element_collection_failure',
            'api_element_value_mismatch'
        )
          AND resolved_at IS NULL
        GROUP BY issue_type, severity
        ORDER BY issue_type, severity
        """
    ).fetchall()
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        counts.setdefault(row["issue_type"], {})[row["severity"]] = int(row["count"])
    return counts


def _api_quality_hygiene_candidates(conn, *, limit: int = 50) -> list[dict[str, Any]]:
    candidates: dict[int, dict[str, Any]] = {}
    duplicate_rows = conn.execute(
        """
        SELECT issue_type, target_type, target_id, issue_detail,
               MIN(issue_id) AS keep_issue_id,
               GROUP_CONCAT(issue_id) AS issue_ids,
               COUNT(*) AS duplicate_count
        FROM quality_issues
        WHERE issue_type IN (
            'api_unmatched',
            'api_value_mismatch',
            'api_element_unmatched',
            'api_element_collection_failure',
            'api_element_value_mismatch'
        )
          AND resolved_at IS NULL
        GROUP BY issue_type, target_type, target_id, issue_detail
        HAVING COUNT(*) > 1
        ORDER BY duplicate_count DESC, keep_issue_id
        """
    ).fetchall()
    for row in duplicate_rows:
        issue_ids = sorted(int(value) for value in str(row["issue_ids"]).split(",") if value)
        keep_issue_id = int(row["keep_issue_id"])
        for issue_id in issue_ids:
            if issue_id == keep_issue_id:
                continue
            candidates[issue_id] = {
                "issue_id": issue_id,
                "action": "resolve_duplicate",
                "reason": "duplicate open API quality issue",
                "keep_issue_id": keep_issue_id,
                "issue_type": row["issue_type"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "issue_detail": row["issue_detail"],
            }

    mismatch_rows = conn.execute(
        """
        SELECT issue_id, issue_type, target_type, target_id, issue_detail
        FROM quality_issues
        WHERE issue_type IN ('api_value_mismatch', 'api_element_value_mismatch')
          AND resolved_at IS NULL
        ORDER BY issue_id
        """
    ).fetchall()
    for row in mismatch_rows:
        if int(row["issue_id"]) in candidates:
            continue
        match = API_MISMATCH_DETAIL_PATTERN.match(row["issue_detail"] or "")
        if not match:
            continue
        if normalize_api_compare_text(match.group("excel")) != normalize_api_compare_text(match.group("api")):
            continue
        candidates[int(row["issue_id"])] = {
            "issue_id": int(row["issue_id"]),
            "action": "resolve_normalized_equal",
            "reason": "Excel/API values match after HTML entity and whitespace normalization",
            "issue_type": row["issue_type"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "issue_detail": row["issue_detail"],
        }

    no_data_element_ids = [
        str(row["element_id"])
        for row in conn.execute(
            """
            SELECT element_id
            FROM competency_elements
            WHERE api_match_status = 'no_data'
            """
        ).fetchall()
    ]
    terminal_no_data_rows = []
    if no_data_element_ids:
        placeholders = ",".join("?" for _ in no_data_element_ids)
        terminal_no_data_rows = conn.execute(
            f"""
            SELECT issue_id, issue_type, target_type, target_id, issue_detail
            FROM quality_issues
            WHERE issue_type IN ('api_element_unmatched', 'api_element_collection_failure')
              AND target_type = 'element'
              AND resolved_at IS NULL
              AND target_id IN ({placeholders})
            ORDER BY issue_id
            """,
            no_data_element_ids,
        ).fetchall()
    for row in terminal_no_data_rows:
        if int(row["issue_id"]) in candidates:
            continue
        candidates[int(row["issue_id"])] = {
            "issue_id": int(row["issue_id"]),
            "action": "resolve_terminal_no_data",
            "reason": "Element API returned terminal no_data after a prior unmatched failure",
            "issue_type": row["issue_type"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "issue_detail": row["issue_detail"],
        }

    stale_matched_rows = conn.execute(
        """
        SELECT
            qi.issue_id,
            qi.issue_type,
            qi.target_type,
            qi.target_id,
            qi.issue_detail,
            ce.api_match_status
        FROM quality_issues qi
        JOIN competency_elements ce
          ON ce.element_id = CAST(qi.target_id AS INTEGER)
        WHERE qi.issue_type IN ('api_element_unmatched', 'api_element_collection_failure')
          AND qi.target_type = 'element'
          AND qi.resolved_at IS NULL
          AND ce.api_match_status = 'matched'
        ORDER BY qi.issue_id
        """
    ).fetchall()
    for row in stale_matched_rows:
        if int(row["issue_id"]) in candidates:
            continue
        candidates[int(row["issue_id"])] = {
            "issue_id": int(row["issue_id"]),
            "action": "resolve_stale_matched_element_api_issue",
            "reason": "Element API issue is stale because the target element is now matched",
            "issue_type": row["issue_type"],
            "target_type": row["target_type"],
            "target_id": row["target_id"],
            "issue_detail": row["issue_detail"],
            "api_match_status": row["api_match_status"],
        }

    return list(candidates.values())[: max(0, int(limit))]


def api_quality_hygiene_report(conn, *, limit: int = 50) -> dict[str, Any]:
    candidates = _api_quality_hygiene_candidates(conn, limit=limit)
    summary: dict[str, int] = {}
    for candidate in candidates:
        summary[candidate["action"]] = summary.get(candidate["action"], 0) + 1
    return {
        "ok": True,
        "apply": False,
        "before_counts": _api_quality_issue_counts(conn),
        "candidate_count": len(candidates),
        "candidate_action_counts": summary,
        "candidates": candidates,
    }


def api_unmatched_diagnosis_report(conn, *, limit: int = 5) -> dict[str, Any]:
    max_rows = max(1, int(limit or 5))
    summary: dict[str, dict[str, int]] = {}
    dominant_details: dict[str, list[dict[str, Any]]] = {}
    for issue_type in API_UNMATCHED_DIAGNOSIS_ISSUE_TYPES:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total_rows,
                SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS open_rows,
                SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved_rows,
                COUNT(DISTINCT target_id) AS distinct_targets,
                COUNT(DISTINCT CASE WHEN resolved_at IS NULL THEN target_id END) AS open_distinct_targets
            FROM quality_issues
            WHERE issue_type = ?
            """,
            (issue_type,),
        ).fetchone()
        summary[issue_type] = {
            "total_rows": int(row["total_rows"] or 0),
            "open_rows": int(row["open_rows"] or 0),
            "resolved_rows": int(row["resolved_rows"] or 0),
            "distinct_targets": int(row["distinct_targets"] or 0),
            "open_distinct_targets": int(row["open_distinct_targets"] or 0),
        }
        dominant_details[issue_type] = [
            dict(item)
            for item in conn.execute(
                """
                SELECT
                    issue_detail,
                    COUNT(*) AS total_rows,
                    SUM(CASE WHEN resolved_at IS NULL THEN 1 ELSE 0 END) AS open_rows,
                    SUM(CASE WHEN resolved_at IS NOT NULL THEN 1 ELSE 0 END) AS resolved_rows
                FROM quality_issues
                WHERE issue_type = ?
                GROUP BY issue_detail
                ORDER BY open_rows DESC, resolved_rows DESC, total_rows DESC, issue_detail
                LIMIT ?
                """,
                (issue_type, max_rows),
            ).fetchall()
        ]
    root_cause = (
        "The backlog is mostly resolved retry-noise bookkeeping, not a pure live "
        "unmatched-element problem."
    )
    recommended_action = (
        "Prepare a report-only cleanup proposal for resolved api_element_unmatched "
        "and api_element_collection_failure rows before any DB mutation is considered."
    )
    return {
        "ok": True,
        "generated_at": now_utc(),
        "summary": summary,
        "dominant_details": dominant_details,
        "root_cause": root_cause,
        "recommended_action": recommended_action,
    }


def api_unmatched_cleanup_proposal_report(
    diagnosis_report: dict[str, Any],
) -> dict[str, Any]:
    summary = diagnosis_report.get("summary") or {}
    unmatched_summary = summary.get("api_element_unmatched") or {}
    failure_summary = summary.get("api_element_collection_failure") or {}
    proposal = {
        "ok": True,
        "schema": "api_unmatched_cleanup_proposal_v1",
        "generated_at": now_utc(),
        "diagnosis_snapshot": {
            "api_element_unmatched_total_rows": int(unmatched_summary.get("total_rows") or 0),
            "api_element_unmatched_open_rows": int(unmatched_summary.get("open_rows") or 0),
            "api_element_unmatched_resolved_rows": int(unmatched_summary.get("resolved_rows") or 0),
            "api_element_collection_failure_total_rows": int(failure_summary.get("total_rows") or 0),
            "api_element_collection_failure_open_rows": int(failure_summary.get("open_rows") or 0),
            "api_element_collection_failure_resolved_rows": int(failure_summary.get("resolved_rows") or 0),
        },
        "proposed_delete_filter": {
            "issue_types": [
                "api_element_unmatched",
                "api_element_collection_failure",
            ],
            "resolved_at": "NOT NULL",
        },
        "dry_run_impact": {
            "resolved_api_element_unmatched_rows_removed": int(unmatched_summary.get("resolved_rows") or 0),
            "resolved_api_element_collection_failure_rows_removed": int(failure_summary.get("resolved_rows") or 0),
            "remaining_open_api_element_unmatched_rows": int(unmatched_summary.get("open_rows") or 0),
            "remaining_open_api_element_collection_failure_rows": int(failure_summary.get("open_rows") or 0),
            "remaining_open_api_element_value_mismatch_rows": int(
                (summary.get("api_element_value_mismatch") or {}).get("open_rows") or 0
            ),
            "remaining_open_api_value_mismatch_rows": int(
                (summary.get("api_value_mismatch") or {}).get("open_rows") or 0
            ),
        },
        "recommendation": (
            "Approve a report-only cleanup proposal for resolved retry-noise issue rows first; "
            "do not apply any DB mutation until reviewed."
        ),
        "snapshot_note": "Counts reflect the diagnosis snapshot passed into this report.",
    }
    return proposal


def _api_unmatched_md_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def write_api_unmatched_diagnosis_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# API Unmatched Diagnosis",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        "",
        "## Summary",
        "",
        "| Issue Type | Total | Open | Resolved | Distinct Targets | Open Distinct Targets |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    summary = report.get("summary") or {}
    for issue_type in API_UNMATCHED_DIAGNOSIS_ISSUE_TYPES:
        counts = summary.get(issue_type) or {}
        lines.append(
            "| {issue_type} | {total} | {open} | {resolved} | {distinct} | {open_distinct} |".format(
                issue_type=_api_unmatched_md_cell(issue_type),
                total=int(counts.get("total_rows") or 0),
                open=int(counts.get("open_rows") or 0),
                resolved=int(counts.get("resolved_rows") or 0),
                distinct=int(counts.get("distinct_targets") or 0),
                open_distinct=int(counts.get("open_distinct_targets") or 0),
            )
        )
    lines.extend(["", "## Dominant Details", ""])
    dominant_details = report.get("dominant_details") or {}
    for issue_type in API_UNMATCHED_DIAGNOSIS_ISSUE_TYPES:
        lines.extend(
            [
                f"### {_api_unmatched_md_cell(issue_type)}",
                "",
                "| Issue Detail | Total | Open | Resolved |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        rows = dominant_details.get(issue_type) or []
        if rows:
            for row in rows:
                lines.append(
                    "| {detail} | {total} | {open} | {resolved} |".format(
                        detail=_api_unmatched_md_cell(row.get("issue_detail")),
                        total=int(row.get("total_rows") or 0),
                        open=int(row.get("open_rows") or 0),
                        resolved=int(row.get("resolved_rows") or 0),
                    )
                )
        else:
            lines.append("| none | 0 | 0 | 0 |")
        lines.append("")
    lines.extend(
        [
            "## Root Cause",
            "",
            _api_unmatched_md_cell(report.get("root_cause")),
            "",
            "## Recommended Action",
            "",
            _api_unmatched_md_cell(report.get("recommended_action")),
            "",
        ]
    )
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_api_unmatched_cleanup_proposal_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = report.get("diagnosis_snapshot") or {}
    impact = report.get("dry_run_impact") or {}
    lines = [
        "# API Unmatched Cleanup Proposal",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        "",
        "## Diagnosis Snapshot",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
    ]
    for key in (
        "api_element_unmatched_total_rows",
        "api_element_unmatched_open_rows",
        "api_element_unmatched_resolved_rows",
        "api_element_collection_failure_total_rows",
        "api_element_collection_failure_open_rows",
        "api_element_collection_failure_resolved_rows",
    ):
        lines.append(f"| `{_api_unmatched_md_cell(key)}` | {int(snapshot.get(key) or 0)} |")
    lines.extend(
        [
            "",
            "## Proposed Delete Filter",
            "",
            "| Field | Value |",
            "| --- | --- |",
        ]
    )
    proposed = report.get("proposed_delete_filter") or {}
    lines.append(
        f"| `issue_types` | `{_api_unmatched_md_cell('; '.join(proposed.get('issue_types') or []))}` |"
    )
    lines.append(f"| `resolved_at` | `{_api_unmatched_md_cell(proposed.get('resolved_at'))}` |")
    lines.extend(
        [
            "",
            "## Dry Run Impact",
            "",
            "| Metric | Value |",
            "| --- | ---: |",
        ]
    )
    for key in (
        "resolved_api_element_unmatched_rows_removed",
        "resolved_api_element_collection_failure_rows_removed",
        "remaining_open_api_element_unmatched_rows",
        "remaining_open_api_element_collection_failure_rows",
        "remaining_open_api_element_value_mismatch_rows",
        "remaining_open_api_value_mismatch_rows",
    ):
        lines.append(f"| `{_api_unmatched_md_cell(key)}` | {int(impact.get(key) or 0)} |")
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            _api_unmatched_md_cell(report.get("recommendation")),
            "",
            "## Snapshot Note",
            "",
            _api_unmatched_md_cell(report.get("snapshot_note")),
            "",
        ]
    )
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def apply_api_quality_hygiene(
    conn,
    *,
    limit: int = 50,
    max_updates: int | None = None,
) -> dict[str, Any]:
    before = api_quality_hygiene_report(conn, limit=limit)
    candidates = before["candidates"]
    if max_updates is not None:
        candidates = candidates[: max(0, int(max_updates))]
    timestamp = now_utc()
    resolved_ids: list[int] = []
    for candidate in candidates:
        conn.execute(
            "UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ? AND resolved_at IS NULL",
            (timestamp, candidate["issue_id"]),
        )
        resolved_ids.append(int(candidate["issue_id"]))
    conn.commit()
    after = api_quality_hygiene_report(conn, limit=limit)
    after.update(
        {
            "apply": True,
            "before_counts": before["before_counts"],
            "resolved_count": len(resolved_ids),
            "resolved_issue_ids": resolved_ids,
            "applied_candidates": candidates,
            "after_counts": after["before_counts"],
        }
    )
    return after


def write_api_quality_hygiene_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# API Quality Hygiene",
        "",
        f"- apply: {report.get('apply')}",
        f"- candidate_count: {report.get('candidate_count')}",
        f"- resolved_count: {report.get('resolved_count', 0)}",
        "",
        "## Counts",
        "",
        f"- before: `{json.dumps(report.get('before_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
        f"- after: `{json.dumps(report.get('after_counts') or report.get('before_counts') or {}, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Candidate Actions",
        "",
    ]
    for action, count in sorted((report.get("candidate_action_counts") or {}).items()):
        lines.append(f"- `{action}`: {count}")
    lines.extend(["", "## Sample Candidates", ""])
    for candidate in (report.get("applied_candidates") or report.get("candidates") or [])[:20]:
        lines.append(
            f"- #{candidate.get('issue_id')} `{candidate.get('action')}` "
            f"{candidate.get('issue_type')} {candidate.get('target_type')}:{candidate.get('target_id')} - "
            f"{candidate.get('reason')}"
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def standard_element_completion_status(
    conn, source_url: str, num_of_rows: int
) -> str | None:
    rows = conn.execute(
        """
        SELECT page_no, total_count, result_code
        FROM api_raw_responses
        WHERE source_url = ?
        """,
        (source_url,),
    ).fetchall()
    if not rows:
        return None
    first = next((row for row in rows if int(row["page_no"]) == 1), None)
    if first is None:
        return None
    first_code = as_text(first["result_code"])
    if first_code == "03":
        return "no_data"
    if first_code not in {"00", "0"}:
        return None
    total_count = int(first["total_count"] or 0)
    pages = max(1, ceil(total_count / num_of_rows)) if total_count else 1
    success_pages = {
        int(row["page_no"])
        for row in rows
        if as_text(row["result_code"]) in {"00", "0"}
    }
    return (
        "matched"
        if all(page_no in success_pages for page_no in range(1, pages + 1))
        else None
    )


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


def collect_sqf_api(
    db_path: Path,
    reports_dir: Path,
    service_key: str,
    api_base_url: str = DEFAULT_SQF_API_BASE,
    num_of_rows: int = 100,
    timeout: int = 30,
    major_code: str | None = None,
    major_limit: int | None = None,
) -> dict[str, int]:
    """Collect SQF duty profiles from /openapi26 by NCS major code."""
    conn = connect(db_path)
    initialize_database(conn)
    if major_code:
        major_codes = [major_code]
    else:
        rows = conn.execute(
            """
            SELECT major_code
            FROM classifications
            GROUP BY major_code
            ORDER BY major_code
            """
        ).fetchall()
        major_codes = [row["major_code"] for row in rows]
    if major_limit is not None:
        major_codes = major_codes[:major_limit]

    endpoint = "openapi26"
    requested_majors = len(major_codes)
    successful_majors = 0
    empty_majors = 0
    failed_majors = 0
    total_items = 0
    total_count_seen = 0

    for code in major_codes:
        params = {"ncsLclasCd": code}
        source_url = source_url_for_standard(api_base_url, endpoint, params)
        page_no = 1
        pages = 1
        major_success = False
        reported_total = 0
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
            data_info = extract_sqf_data_info(payload)
            result_code = as_text(data_info.get("code")) or result_code
            result_msg = as_text(data_info.get("message")) or result_msg
            result_code_upper = result_code.upper() if result_code else ""
            if result_code_upper == "002":
                empty_majors += 1
                conn.commit()
                break
            if result_code and result_code_upper not in {"000", "00", "0", "200", "INFO-000"}:
                failed_majors += 1
                conn.commit()
                break
            items = extract_sqf_items(payload)
            total_items += upsert_sqf_items(conn, items)
            if page_no == 1:
                reported_total = total_count or as_int(data_info.get("totCnt"))
            total_page = as_int(data_info.get("totalPage"))
            pages = total_page or (max(1, ceil(total_count / num_of_rows)) if total_count else 1)
            page_no += 1
            major_success = True
            conn.commit()
        if major_success:
            successful_majors += 1
            total_count_seen += reported_total

    duties_total = conn.execute("SELECT COUNT(*) FROM sqf_duties").fetchone()[0]
    summary = {
        "sqf_major_codes_requested": requested_majors,
        "sqf_major_codes_successful": successful_majors,
        "sqf_major_codes_empty": empty_majors,
        "sqf_major_codes_failed": failed_majors,
        "sqf_items_upserted": total_items,
        "sqf_total_count_seen": total_count_seen,
        "sqf_duties_total": int(duties_total),
    }
    write_report(summary, reports_dir, filename="api_sqf_report.md")
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
    only_failed: bool = False,
    only_open_unmatched: bool = False,
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
    if only_failed:
        clauses.append("ce.api_match_status = 'api_failed'")
    elif only_uncollected:
        if retry_failed:
            clauses.append("ce.api_match_status IN ('not_collected', 'api_failed')")
        else:
            clauses.append("ce.api_match_status = 'not_collected'")
    if only_open_unmatched:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM quality_issues qi
                WHERE qi.target_type = 'element'
                  AND qi.target_id = CAST(ce.element_id AS TEXT)
                  AND qi.issue_type = 'api_element_unmatched'
                  AND qi.resolved_at IS NULL
            )
            """
        )
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
    no_data_elements = 0
    rate_limited_elements = 0
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
        completed_status = standard_element_completion_status(conn, source_url, num_of_rows)
        if completed_status:
            skipped_elements += 1
            if completed_status == "no_data":
                no_data_elements += 1
                conn.execute(
                    "UPDATE competency_elements SET api_match_status = ? WHERE element_id = ?",
                    ("no_data", row["element_id"]),
                )
                resolve_element_unmatched_issue(conn, row["element_id"])
                conn.commit()
            else:
                successful_elements += 1
        else:
            pending_rows.append(row)

    def process_result(result: dict[str, Any]) -> None:
        nonlocal failed_elements
        nonlocal no_data_elements
        nonlocal rate_limited_elements
        nonlocal successful_elements
        nonlocal total_items
        nonlocal matched_items
        nonlocal orphan_items
        nonlocal mismatches
        if result["error"]:
            if "status=429" in result["error"]:
                rate_limited_elements += 1
                return
            failed_elements += 1
            conn.execute(
                "UPDATE competency_elements SET api_match_status = ? WHERE element_id = ?",
                ("api_failed", result["element_id"]),
            )
            insert_api_quality_issue_once(
                conn,
                target_type="element",
                target_id=result["element_id"],
                issue_type=API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE,
                severity="warning",
                issue_detail=f"NCS006 request failed after retries: {result['error']}",
                suggested_action="Retry later with --only-uncollected.",
            )
            conn.commit()
            return

        payload = result["payload"]
        _total_count, result_code, result_msg = save_raw_response(
            conn, result["source_url"], 1, num_of_rows, payload
        )
        if result_code and result_code not in {"00", "0"}:
            status = "no_data" if result_code == "03" else "api_failed"
            if status == "no_data":
                no_data_elements += 1
            else:
                failed_elements += 1
            conn.execute(
                "UPDATE competency_elements SET api_match_status = ? WHERE element_id = ?",
                (status, result["element_id"]),
            )
            if status == "no_data":
                resolve_element_unmatched_issue(conn, result["element_id"])
            else:
                insert_api_quality_issue_once(
                    conn,
                    target_type="element",
                    target_id=result["element_id"],
                    issue_type=API_ELEMENT_COLLECTION_FAILURE_ISSUE_TYPE,
                    severity="warning",
                    issue_detail=f"NCS006 returned resultCode={result_code}: {result_msg or ''}",
                    suggested_action="Retry later with --only-uncollected or inspect the API response.",
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
        "elements_no_data": no_data_elements,
        "elements_rate_limited": rate_limited_elements,
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
    parser.add_argument("--sqf-service-key", default=settings.sqf_service_key)
    parser.add_argument(
        "--mode",
        choices=["standards", "subd", "elements", "sqf", "ncs1info"],
        default="standards",
        help=(
            "standards uses approved hrdkapi /NCS005. "
            "subd uses approved hrdkapi /NCS004. "
            "elements uses approved hrdkapi /NCS006. "
            "sqf uses SQF duty /openapi26. "
            "ncs1info uses legacy c.q-net endpoint."
        ),
    )
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    parser.add_argument("--api-base-url", default=DEFAULT_STANDARDS_API_BASE)
    parser.add_argument("--sqf-api-base-url", default=DEFAULT_SQF_API_BASE)
    parser.add_argument("--num-of-rows", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--classification-limit", type=int)
    parser.add_argument("--ncs-lclas-cd")
    parser.add_argument("--sqf-major-limit", type=int)
    parser.add_argument("--element-limit", type=int)
    parser.add_argument("--element-offset", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--major-code")
    parser.add_argument("--middle-code")
    parser.add_argument("--small-code")
    parser.add_argument("--sub-code")
    parser.add_argument("--only-uncollected", action="store_true")
    parser.add_argument("--only-failed", action="store_true")
    parser.add_argument("--only-open-unmatched", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.mode == "sqf":
        if not args.sqf_service_key:
            raise SystemExit(
                "NCS_SQF_SERVICE_KEY is required. Set it in .env or pass --sqf-service-key."
            )
        summary = collect_sqf_api(
            db_path=args.db_path,
            reports_dir=args.reports_dir,
            service_key=args.sqf_service_key,
            api_base_url=args.sqf_api_base_url,
            num_of_rows=args.num_of_rows,
            timeout=args.timeout,
            major_code=args.ncs_lclas_cd,
            major_limit=args.sqf_major_limit,
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
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
            only_failed=args.only_failed,
            only_open_unmatched=args.only_open_unmatched,
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
