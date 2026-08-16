from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.http_client import get_with_retries


DEFAULT_TRAINING_COURSE_URL = "http://apis.data.go.kr/B490007/ncsTrainingCource/openapi18"


class TrainingCourseApiError(RuntimeError):
    pass


def _text(element: ElementTree.Element, tag: str) -> str:
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def parse_training_course_xml(xml_text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(xml_text)
    data = root.find("data")
    data_info = root.find("dataInfo")
    rows = []
    if data is not None:
        for row in data.findall("row"):
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
                    "compe_unit_level": _text(row, "compeUnitLevel"),
                    "train_goal": _text(row, "trainGoal"),
                    "train_time": _text(row, "trainTime"),
                    "fac_name": _text(row, "facName"),
                    "meth_name": _text(row, "methName"),
                }
            )
    return {
        "code": _text(data_info if data_info is not None else root, "code"),
        "message": _text(data_info if data_info is not None else root, "message"),
        "num_of_rows": _text(data_info if data_info is not None else root, "numOfRows"),
        "page_no": _text(data_info if data_info is not None else root, "pageNo"),
        "total_count": int(_text(data_info if data_info is not None else root, "totCnt") or 0),
        "total_page": int(_text(data_info if data_info is not None else root, "totalPage") or 0),
        "rows": rows,
    }


def fetch_training_course_page(
    service_key: str,
    *,
    major_code: str,
    module_name: str | None = None,
    page_no: int = 1,
    num_of_rows: int = 100,
    timeout: int = 30,
    api_url: str = DEFAULT_TRAINING_COURSE_URL,
    max_retries: int = 2,
    retry_backoff_seconds: float = 1.0,
) -> dict[str, Any]:
    import requests

    params = {
        "serviceKey": unquote(service_key),
        "pageNo": page_no,
        "numOfRows": num_of_rows,
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
        raise TrainingCourseApiError(
            f"NCS training course API request failed: status=request_error, major_code={major_code}, "
            f"page_no={page_no}, error={type(exc).__name__}"
        ) from None
    if response.status_code >= 400:
        raise TrainingCourseApiError(
            f"NCS training course API request failed: status={response.status_code}, "
            f"major_code={major_code}, page_no={page_no}"
        )
    parsed = parse_training_course_xml(response.text)
    parsed["request"] = {
        "major_code": major_code,
        "module_name": module_name,
        "page_no": page_no,
        "num_of_rows": num_of_rows,
    }
    return parsed


def upsert_training_courses(conn, rows: list[dict[str, Any]], *, source_payload: dict[str, Any] | None = None) -> int:
    timestamp = now_utc()
    count = 0
    for row in rows:
        if not row.get("ncs_cl_cd"):
            continue
        payload = dict(source_payload or {})
        payload["row"] = row
        conn.execute(
            """
            INSERT INTO ncs_training_courses(
                ncs_cl_cd, compe_unit_name, compe_unit_level,
                ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm,
                ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm,
                train_goal, train_time, fac_name, meth_name,
                source_payload, api_fetched_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ncs_cl_cd, train_goal, train_time, fac_name, meth_name)
            DO UPDATE SET
                compe_unit_name = excluded.compe_unit_name,
                compe_unit_level = excluded.compe_unit_level,
                ncs_lclas_cd = excluded.ncs_lclas_cd,
                ncs_lclas_cdnm = excluded.ncs_lclas_cdnm,
                ncs_mclas_cd = excluded.ncs_mclas_cd,
                ncs_mclas_cdnm = excluded.ncs_mclas_cdnm,
                ncs_sclas_cd = excluded.ncs_sclas_cd,
                ncs_sclas_cdnm = excluded.ncs_sclas_cdnm,
                ncs_subd_cd = excluded.ncs_subd_cd,
                ncs_subd_cdnm = excluded.ncs_subd_cdnm,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            (
                row.get("ncs_cl_cd"),
                row.get("compe_unit_name"),
                row.get("compe_unit_level"),
                row.get("ncs_lclas_cd"),
                row.get("ncs_lclas_cdnm"),
                row.get("ncs_mclas_cd"),
                row.get("ncs_mclas_cdnm"),
                row.get("ncs_sclas_cd"),
                row.get("ncs_sclas_cdnm"),
                row.get("ncs_subd_cd"),
                row.get("ncs_subd_cdnm"),
                row.get("train_goal"),
                row.get("train_time"),
                row.get("fac_name"),
                row.get("meth_name"),
                json.dumps(payload, ensure_ascii=False),
                timestamp,
            ),
        )
        count += 1
    conn.execute(
        """
        INSERT OR IGNORE INTO ncs_training_course_unit_links(
            training_course_id, unit_code, link_method, confidence_score,
            review_status, created_at, updated_at
        )
        SELECT
            tc.training_course_id,
            cu.unit_code,
            'ncs_cl_cd_exact',
            1.0,
            'reviewed',
            ?,
            ?
        FROM ncs_training_courses tc
        JOIN competency_units cu ON cu.unit_code = tc.ncs_cl_cd
        """,
        (timestamp, timestamp),
    )
    conn.commit()
    return count


def collect_training_courses(
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
    rows_upserted = 0
    total_count = 0
    total_page = 0
    page = page_no
    while True:
        payload = fetch_training_course_page(
            service_key,
            major_code=major_code,
            module_name=module_name,
            page_no=page,
            num_of_rows=num_of_rows,
            timeout=timeout,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        if payload["code"] not in {"000", ""}:
            break
        total_count = payload["total_count"]
        total_page = payload["total_page"]
        rows_upserted += upsert_training_courses(
            conn,
            payload["rows"],
            source_payload={"api": "ncsTrainingCource/openapi18", "request": payload["request"]},
        )
        pages_processed += 1
        if max_pages and pages_processed >= max_pages:
            break
        if page >= total_page or not payload["rows"]:
            break
        page += 1
    linked = conn.execute("SELECT COUNT(*) FROM ncs_training_course_unit_links").fetchone()[0]
    total_rows = conn.execute("SELECT COUNT(*) FROM ncs_training_courses").fetchone()[0]
    conn.close()
    return {
        "major_code": major_code,
        "module_name": module_name,
        "pages_processed": pages_processed,
        "rows_upserted": rows_upserted,
        "reported_total_count": total_count,
        "reported_total_page": total_page,
        "training_courses_total": int(total_rows),
        "training_course_unit_links_total": int(linked),
    }


if __name__ == "__main__":
    settings = load_settings()
    if not settings.training_course_service_key:
        raise SystemExit("NCS_TRAINING_COURSE_SERVICE_KEY or NCS_SERVICE_KEY is required")
    print(
        json.dumps(
            collect_training_courses(
                settings.db_path,
                settings.training_course_service_key,
                major_code="02",
                max_pages=1,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
