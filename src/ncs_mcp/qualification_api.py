from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from ncs_mcp.config import load_settings
from ncs_mcp.db import clamp_limit, connect, initialize_database, now_utc, rows_to_dicts


DEFAULT_QUALIFICATION_API_URL = "http://apis.data.go.kr/B490007/ncsClCdJm/getNcsClCdJmList"


class QualificationApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        rate_limit_attempts: int = 0,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.rate_limit_attempts = max(0, int(rate_limit_attempts or 0))
        self.status_code = status_code


def _text(element: ElementTree.Element | None, tag: str) -> str:
    if element is None:
        return ""
    child = element.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _int_value(value: Any) -> int | None:
    text = "" if value is None else str(value).strip().replace(",", "")
    return int(text) if text.isdigit() else None


def parse_qualification_xml(xml_text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(xml_text)
    header = root.find("header")
    body = root.find("body")
    rows = []
    for item in root.findall(".//item"):
        rows.append(
            {
                "jm_cd": _text(item, "jmCd"),
                "jm_nm": _text(item, "jmNm"),
                "organ_std_ver_cd": _text(item, "organStdVerCd"),
                "edu_trng_std_tm_sum": _int_value(_text(item, "eduTrngStdTmSum")),
                "job_basis_ablt_std_tm": _int_value(_text(item, "jobBasisAbltStdTm")),
                "mand_ablt_unit_std_tm": _int_value(_text(item, "mandAbltUnitStdTm")),
                "sel_ablt_unit_std_tm": _int_value(_text(item, "selAbltUnitStdTm")),
                "exam_insti_nm": _text(item, "examInstiNm"),
                "ncs_cl_cd": _text(item, "ncsClCd"),
                "compe_unit_name": _text(item, "compeUnitName"),
                "ablt_unit_typ_cd": _text(item, "abltUnitTypCd"),
                "ablt_unit_typ_nm": _text(item, "abltUnitTypNm"),
                "min_edu_trng_tm": _int_value(_text(item, "minEduTrngTm")),
            }
        )
    return {
        "result_code": _text(header, "resultCode"),
        "result_msg": _text(header, "resultMsg"),
        "num_of_rows": _int_value(_text(body, "numOfRows")) or 0,
        "page_no": _int_value(_text(body, "pageNo")) or 0,
        "total_count": _int_value(_text(body, "totalCount")) or 0,
        "rows": rows,
    }


def fetch_qualification_page(
    service_key: str,
    *,
    unit_code: str,
    page_no: int = 1,
    num_of_rows: int = 50,
    timeout: int = 30,
    api_url: str = DEFAULT_QUALIFICATION_API_URL,
    max_retries: int = 3,
    retry_backoff_seconds: float = 2.0,
) -> dict[str, Any]:
    import requests

    safe_num_of_rows = max(1, min(int(num_of_rows), 50))
    params = {
        "serviceKey": unquote(service_key),
        "numOfRows": safe_num_of_rows,
        "pageNo": page_no,
        "dataFormat": "xml",
        "ncsClCd": unit_code,
    }
    response = None
    attempts = max(1, int(max_retries) + 1)
    rate_limit_attempts = 0
    for attempt in range(attempts):
        try:
            response = requests.get(api_url, params=params, timeout=timeout)
        except requests.RequestException as exc:
            if attempt + 1 >= attempts:
                raise QualificationApiError(
                    f"NCS qualification API request failed: status=request_error, "
                    f"unit_code={unit_code}, page_no={page_no}, error={type(exc).__name__}",
                    rate_limit_attempts=rate_limit_attempts,
                ) from None
            time.sleep(max(0.0, retry_backoff_seconds) * (attempt + 1))
            continue
        if response.status_code == 429:
            rate_limit_attempts += 1
        if response.status_code not in {429, 502, 503, 504}:
            break
        if attempt + 1 >= attempts:
            break
        retry_after = response.headers.get("Retry-After")
        delay = float(retry_after) if retry_after and retry_after.isdigit() else retry_backoff_seconds * (attempt + 1)
        time.sleep(max(0.0, delay))
    if response is None:
        raise QualificationApiError(
            f"NCS qualification API request failed: status=request_error, unit_code={unit_code}, page_no={page_no}",
            rate_limit_attempts=rate_limit_attempts,
        )
    if response.status_code >= 400:
        raise QualificationApiError(
            f"NCS qualification API request failed: status={response.status_code}, "
            f"unit_code={unit_code}, page_no={page_no}",
            rate_limit_attempts=rate_limit_attempts,
            status_code=response.status_code,
        )
    parsed = parse_qualification_xml(response.text)
    parsed["request"] = {
        "unit_code": unit_code,
        "page_no": page_no,
        "num_of_rows": safe_num_of_rows,
    }
    return parsed


def upsert_qualification_rows(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    source_payload: dict[str, Any] | None = None,
) -> int:
    timestamp = now_utc()
    count = 0
    for row in rows:
        unit_code = row.get("ncs_cl_cd")
        jm_cd = row.get("jm_cd")
        jm_nm = row.get("jm_nm")
        if not unit_code or not jm_cd or not jm_nm:
            continue
        payload = dict(source_payload or {})
        payload["row"] = row
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO ncs_qualification_items(
                jm_cd, jm_nm, exam_insti_nm, source_payload, api_fetched_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(jm_cd) DO UPDATE SET
                jm_nm = excluded.jm_nm,
                exam_insti_nm = excluded.exam_insti_nm,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at
            """,
            (jm_cd, jm_nm, row.get("exam_insti_nm"), payload_json, timestamp),
        )
        unit_exists = conn.execute(
            "SELECT 1 FROM competency_units WHERE unit_code = ?",
            (unit_code,),
        ).fetchone()
        if unit_exists is None:
            count += 1
            continue
        conn.execute(
            """
            INSERT INTO ncs_unit_qualification_links(
                unit_code, jm_cd, organ_std_ver_cd,
                edu_trng_std_tm_sum, job_basis_ablt_std_tm,
                mand_ablt_unit_std_tm, sel_ablt_unit_std_tm,
                compe_unit_name, ablt_unit_typ_cd, ablt_unit_typ_nm,
                min_edu_trng_tm, link_method, confidence_score,
                source_payload, api_fetched_at, review_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      'ncs_cl_cd_exact', 1.0, ?, ?, 'reviewed', ?, ?)
            ON CONFLICT(unit_code, jm_cd, organ_std_ver_cd, ablt_unit_typ_cd, min_edu_trng_tm)
            DO UPDATE SET
                edu_trng_std_tm_sum = excluded.edu_trng_std_tm_sum,
                job_basis_ablt_std_tm = excluded.job_basis_ablt_std_tm,
                mand_ablt_unit_std_tm = excluded.mand_ablt_unit_std_tm,
                sel_ablt_unit_std_tm = excluded.sel_ablt_unit_std_tm,
                compe_unit_name = excluded.compe_unit_name,
                ablt_unit_typ_nm = excluded.ablt_unit_typ_nm,
                source_payload = excluded.source_payload,
                api_fetched_at = excluded.api_fetched_at,
                updated_at = excluded.updated_at
            """,
            (
                unit_code,
                jm_cd,
                row.get("organ_std_ver_cd"),
                row.get("edu_trng_std_tm_sum"),
                row.get("job_basis_ablt_std_tm"),
                row.get("mand_ablt_unit_std_tm"),
                row.get("sel_ablt_unit_std_tm"),
                row.get("compe_unit_name"),
                row.get("ablt_unit_typ_cd"),
                row.get("ablt_unit_typ_nm"),
                row.get("min_edu_trng_tm"),
                payload_json,
                timestamp,
                timestamp,
                timestamp,
            ),
        )
        count += 1
    conn.commit()
    return count


def qualification_summary(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    max_rows = clamp_limit(limit, default=20, maximum=100)
    total_unit_count = int(conn.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0])
    item_count = int(conn.execute("SELECT COUNT(*) FROM ncs_qualification_items").fetchone()[0])
    link_count = int(conn.execute("SELECT COUNT(*) FROM ncs_unit_qualification_links").fetchone()[0])
    by_type = rows_to_dicts(
        conn.execute(
            """
            SELECT ablt_unit_typ_cd, ablt_unit_typ_nm, COUNT(*) AS count
            FROM ncs_unit_qualification_links
            GROUP BY ablt_unit_typ_cd, ablt_unit_typ_nm
            ORDER BY count DESC, ablt_unit_typ_cd
            """
        ).fetchall()
    )
    top_qualifications = rows_to_dicts(
        conn.execute(
            """
            SELECT qi.jm_cd, qi.jm_nm, qi.exam_insti_nm, COUNT(l.link_id) AS unit_link_count
            FROM ncs_qualification_items qi
            JOIN ncs_unit_qualification_links l ON l.jm_cd = qi.jm_cd
            GROUP BY qi.jm_cd
            ORDER BY unit_link_count DESC, qi.jm_nm
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
    )
    collection_status = rows_to_dicts(
        conn.execute(
            """
            SELECT collection_status, COUNT(*) AS unit_count
            FROM ncs_qualification_collection_status
            GROUP BY collection_status
            ORDER BY unit_count DESC, collection_status
            """
        ).fetchall()
    )
    attempted_unit_count = sum(int(row.get("unit_count") or 0) for row in collection_status)
    collection_coverage = round(attempted_unit_count / total_unit_count, 4) if total_unit_count else 0.0
    errors_by_major = rows_to_dicts(
        conn.execute(
            """
            SELECT c.major_code, c.major_name, COUNT(*) AS error_unit_count
            FROM ncs_qualification_collection_status status
            JOIN competency_units cu ON cu.unit_code = status.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE status.collection_status = 'error'
            GROUP BY c.major_code, c.major_name
            ORDER BY error_unit_count DESC, c.major_code
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
    )
    return {
        "ok": True,
        "qualification_item_count": item_count,
        "unit_qualification_link_count": link_count,
        "total_unit_count": total_unit_count,
        "attempted_unit_count": attempted_unit_count,
        "unattempted_unit_count": max(total_unit_count - attempted_unit_count, 0),
        "collection_coverage": collection_coverage,
        "collection_status": collection_status,
        "errors_by_major": errors_by_major,
        "by_unit_type": by_type,
        "top_qualifications": top_qualifications,
    }


def _classify_qualification_error(error: str | None) -> str:
    text = (error or "").lower()
    if not text:
        return "unknown"
    if "status=429" in text or "too many" in text or "rate" in text:
        return "rate_limited"
    if "status=500" in text or "status=502" in text or "status=503" in text or "status=504" in text:
        return "server_error"
    if "timeout" in text:
        return "timeout"
    if "request_error" in text or "connection" in text:
        return "request_error"
    if "service key" in text or "servicedecode" in text or "auth" in text:
        return "auth_or_key"
    return "api_or_parse_error"


def _retry_delay_seconds(error_type: str, attempt_count: int, base_seconds: float) -> float:
    base = max(1.0, float(base_seconds))
    attempt = max(1, int(attempt_count))
    multiplier = min(8, 2 ** max(0, attempt - 1))
    if error_type == "rate_limited":
        return min(max(base * multiplier, 60.0), 3600.0)
    if error_type == "server_error":
        return min(max(base * multiplier, 30.0), 1800.0)
    if error_type in {"timeout", "request_error"}:
        return min(max(base * multiplier, 15.0), 900.0)
    if error_type == "auth_or_key":
        return 24 * 3600.0
    return min(max(base * multiplier, 10.0), 600.0)


def _next_retry_at(error_type: str, attempt_count: int, base_seconds: float) -> str:
    delay = _retry_delay_seconds(error_type, attempt_count, base_seconds)
    return (datetime.now(UTC).replace(microsecond=0) + timedelta(seconds=delay)).isoformat()


def _parse_retry_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _retry_timestamp_is_ready(value: Any, *, now_dt: datetime) -> bool:
    if not value:
        return True
    parsed = _parse_retry_timestamp(value)
    return parsed is None or parsed <= now_dt


def qualification_error_report(conn: sqlite3.Connection, *, limit: int = 50) -> dict[str, Any]:
    max_rows = clamp_limit(limit, default=50, maximum=500)
    error_rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
                status.unit_code,
                status.rows_collected,
                status.pages_processed,
                status.last_result_code,
                status.last_result_msg,
                status.last_error,
                status.last_error_type,
                status.attempt_count,
                status.next_retry_at,
                status.updated_at,
                cu.unit_name_raw AS unit_name,
                c.major_code,
                c.major_name,
                c.middle_code,
                c.middle_name,
                c.small_code,
                c.small_name,
                c.sub_code,
                c.sub_name
            FROM ncs_qualification_collection_status status
            LEFT JOIN competency_units cu ON cu.unit_code = status.unit_code
            LEFT JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE status.collection_status = 'error'
            ORDER BY status.updated_at DESC, status.unit_code
            LIMIT ?
            """,
            (max_rows,),
        ).fetchall()
    )
    by_error_type: dict[str, int] = {}
    for row in error_rows:
        error_type = row.get("last_error_type") or _classify_qualification_error(
            row.get("last_error") or row.get("last_result_msg")
        )
        row["error_type"] = error_type
        by_error_type[error_type] = by_error_type.get(error_type, 0) + 1
    status_counts = rows_to_dicts(
        conn.execute(
            """
            SELECT collection_status, COUNT(*) AS unit_count
            FROM ncs_qualification_collection_status
            GROUP BY collection_status
            ORDER BY unit_count DESC, collection_status
            """
        ).fetchall()
    )
    major_counts = rows_to_dicts(
        conn.execute(
            """
            SELECT c.major_code, c.major_name, COUNT(*) AS error_unit_count
            FROM ncs_qualification_collection_status status
            LEFT JOIN competency_units cu ON cu.unit_code = status.unit_code
            LEFT JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE status.collection_status = 'error'
            GROUP BY c.major_code, c.major_name
            ORDER BY error_unit_count DESC, c.major_code
            """
        ).fetchall()
    )
    error_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM ncs_qualification_collection_status WHERE collection_status = 'error'"
        ).fetchone()[0]
    )
    retry_ready_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ncs_qualification_collection_status
            WHERE collection_status = 'error'
              AND (
                  next_retry_at IS NULL
                  OR julianday(next_retry_at) IS NULL
                  OR julianday(next_retry_at) <= julianday(?)
              )
            """,
            (now_utc(),),
        ).fetchone()[0]
    )
    return {
        "ok": True,
        "error_unit_count": error_count,
        "retry_ready_unit_count": retry_ready_count,
        "retry_waiting_unit_count": max(0, error_count - retry_ready_count),
        "status_counts": status_counts,
        "major_error_counts": major_counts,
        "sample_errors": error_rows,
        "sample_error_type_counts": dict(sorted(by_error_type.items())),
    }


def qualification_retry_hygiene_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    retry_backoff_seconds: float = 5.0,
) -> dict[str, Any]:
    max_rows = clamp_limit(limit, default=50, maximum=500)
    error_rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
                status.unit_code,
                status.rows_collected,
                status.pages_processed,
                status.last_result_code,
                status.last_result_msg,
                status.last_error,
                status.last_error_type,
                status.attempt_count,
                status.next_retry_at,
                status.updated_at,
                cu.unit_name_raw AS unit_name,
                c.major_code,
                c.major_name
            FROM ncs_qualification_collection_status status
            LEFT JOIN competency_units cu ON cu.unit_code = status.unit_code
            LEFT JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE status.collection_status = 'error'
            ORDER BY status.updated_at DESC, status.unit_code
            """
        ).fetchall()
    )
    now_dt = _parse_retry_timestamp(now_utc()) or datetime.now(UTC)
    error_type_counts: dict[str, int] = {}
    major_error_counts: dict[tuple[str | None, str | None], int] = {}
    missing_error_type_count = 0
    zero_attempt_count = 0
    missing_next_retry_at_count = 0
    invalid_next_retry_at_count = 0
    retry_ready_count = 0
    dry_run_updates: list[dict[str, Any]] = []
    for row in error_rows:
        inferred_error_type = row.get("last_error_type") or _classify_qualification_error(
            row.get("last_error") or row.get("last_result_msg")
        )
        attempt_count = int(row.get("attempt_count") or 0)
        proposed_attempt_count = max(1, attempt_count)
        next_retry_at = row.get("next_retry_at")
        invalid_next_retry_at = bool(next_retry_at) and _parse_retry_timestamp(next_retry_at) is None
        proposed_next_retry_at = (
            (None if invalid_next_retry_at else next_retry_at)
            or _next_retry_at(inferred_error_type, proposed_attempt_count, retry_backoff_seconds)
        )
        error_type_counts[inferred_error_type] = error_type_counts.get(inferred_error_type, 0) + 1
        major_key = (row.get("major_code"), row.get("major_name"))
        major_error_counts[major_key] = major_error_counts.get(major_key, 0) + 1
        if not row.get("last_error_type"):
            missing_error_type_count += 1
        if attempt_count == 0:
            zero_attempt_count += 1
        if not next_retry_at:
            missing_next_retry_at_count += 1
        if invalid_next_retry_at:
            invalid_next_retry_at_count += 1
        if _retry_timestamp_is_ready(next_retry_at, now_dt=now_dt):
            retry_ready_count += 1
        if (
            len(dry_run_updates) < max_rows
            and (
                not row.get("last_error_type")
                or attempt_count == 0
                or not next_retry_at
                or invalid_next_retry_at
            )
        ):
            dry_run_updates.append(
                {
                    "unit_code": row.get("unit_code"),
                    "unit_name": row.get("unit_name"),
                    "major_code": row.get("major_code"),
                    "major_name": row.get("major_name"),
                    "current_error_type": row.get("last_error_type"),
                    "inferred_error_type": inferred_error_type,
                    "current_attempt_count": attempt_count,
                    "proposed_attempt_count": proposed_attempt_count,
                    "current_next_retry_at": next_retry_at,
                    "proposed_next_retry_at": proposed_next_retry_at,
                    "invalid_next_retry_at": invalid_next_retry_at,
                    "last_error": row.get("last_error") or row.get("last_result_msg"),
                }
            )
    major_counts = [
        {
            "major_code": major_code,
            "major_name": major_name,
            "error_unit_count": count,
        }
        for (major_code, major_name), count in major_error_counts.items()
    ]
    major_counts.sort(key=lambda item: (-int(item["error_unit_count"]), str(item.get("major_code") or "")))
    rate_limited_count = error_type_counts.get("rate_limited", 0)
    error_count = len(error_rows)
    broad_retry_risk = (
        "high"
        if error_count and rate_limited_count / error_count >= 0.5
        else "medium"
        if rate_limited_count
        else "low"
    )
    return {
        "ok": True,
        "mode": "dry_run",
        "error_unit_count": error_count,
        "retry_ready_unit_count": retry_ready_count,
        "retry_waiting_unit_count": max(0, error_count - retry_ready_count),
        "error_type_counts": dict(sorted(error_type_counts.items())),
        "major_error_counts": major_counts,
        "metadata_gaps": {
            "missing_error_type_count": missing_error_type_count,
            "zero_attempt_count": zero_attempt_count,
            "missing_next_retry_at_count": missing_next_retry_at_count,
            "invalid_next_retry_at_count": invalid_next_retry_at_count,
        },
        "broad_retry_risk": broad_retry_risk,
        "recommended_policy": {
            "do_not_call_api": True,
            "next_collection_command": (
                "Use retry-qualification-errors only with a small --limit-units, "
                "--retry-ready-only semantics, request delay, and low max retries."
            ),
            "if_rate_limited": (
                "Prefer waiting for next_retry_at and use --request-delay >= 2 "
                "--max-retries 1 before increasing scope."
            ),
            "if_auth_or_key": "Stop retries and verify service key/configuration.",
        },
        "dry_run_updates": dry_run_updates,
    }


def apply_qualification_retry_hygiene(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    retry_backoff_seconds: float = 5.0,
    max_updates: int | None = None,
) -> dict[str, Any]:
    before = qualification_retry_hygiene_report(
        conn,
        limit=limit,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    if max_updates is not None and max_updates < 0:
        raise ValueError("max_updates must be non-negative when provided.")
    rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
                unit_code,
                last_result_msg,
                last_error,
                last_error_type,
                attempt_count,
                next_retry_at
            FROM ncs_qualification_collection_status
            WHERE collection_status = 'error'
            ORDER BY updated_at DESC, unit_code
            """
        ).fetchall()
    )
    updated_unit_count = 0
    updated_at = now_utc()
    for row in rows:
        if max_updates is not None and updated_unit_count >= max_updates:
            break
        current_error_type = row.get("last_error_type")
        current_attempt_count = int(row.get("attempt_count") or 0)
        current_next_retry_at = row.get("next_retry_at")
        invalid_next_retry_at = bool(current_next_retry_at) and _parse_retry_timestamp(current_next_retry_at) is None
        needs_update = (
            not current_error_type
            or current_attempt_count == 0
            or not current_next_retry_at
            or invalid_next_retry_at
        )
        if not needs_update:
            continue
        inferred_error_type = current_error_type or _classify_qualification_error(
            row.get("last_error") or row.get("last_result_msg")
        )
        proposed_attempt_count = max(1, current_attempt_count)
        proposed_next_retry_at = (
            None if invalid_next_retry_at else current_next_retry_at
        ) or _next_retry_at(inferred_error_type, proposed_attempt_count, retry_backoff_seconds)
        conn.execute(
            """
            UPDATE ncs_qualification_collection_status
            SET last_error_type = ?,
                attempt_count = ?,
                next_retry_at = ?,
                updated_at = ?
            WHERE unit_code = ?
              AND collection_status = 'error'
            """,
            (
                inferred_error_type,
                proposed_attempt_count,
                proposed_next_retry_at,
                updated_at,
                row.get("unit_code"),
            ),
        )
        updated_unit_count += 1
    conn.commit()
    after = qualification_retry_hygiene_report(
        conn,
        limit=limit,
        retry_backoff_seconds=retry_backoff_seconds,
    )
    return {
        "ok": True,
        "mode": "applied",
        "updated_unit_count": updated_unit_count,
        "max_updates": max_updates,
        "before": before,
        "after": after,
    }


def write_qualification_retry_hygiene_markdown(report: dict[str, Any], out_path: Path) -> None:
    if report.get("mode") == "applied":
        before = report.get("before") or {}
        after = report.get("after") or {}
        lines = [
            "# Qualification Retry Hygiene",
            "",
            "- mode: applied",
            f"- updated_unit_count: {report.get('updated_unit_count')}",
            f"- max_updates: {report.get('max_updates')}",
            "",
            "## Before",
            "",
            f"- error_unit_count: {before.get('error_unit_count')}",
            f"- retry_ready_unit_count: {before.get('retry_ready_unit_count')}",
            f"- broad_retry_risk: {before.get('broad_retry_risk')}",
            f"- metadata_gaps: {json.dumps(before.get('metadata_gaps') or {}, ensure_ascii=False)}",
            "",
            "## After",
            "",
            f"- error_unit_count: {after.get('error_unit_count')}",
            f"- retry_ready_unit_count: {after.get('retry_ready_unit_count')}",
            f"- broad_retry_risk: {after.get('broad_retry_risk')}",
            f"- metadata_gaps: {json.dumps(after.get('metadata_gaps') or {}, ensure_ascii=False)}",
            "",
            "## Policy",
            "",
            "- This operation does not call the qualification API.",
            "- It only backfills retry metadata on existing error status rows.",
        ]
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return

    lines = [
        "# Qualification Retry Hygiene",
        "",
        f"- mode: {report.get('mode')}",
        f"- error_unit_count: {report.get('error_unit_count')}",
        f"- retry_ready_unit_count: {report.get('retry_ready_unit_count')}",
        f"- broad_retry_risk: {report.get('broad_retry_risk')}",
        "",
        "## Metadata Gaps",
        "",
    ]
    for key, value in (report.get("metadata_gaps") or {}).items():
        lines.append(f"- {key}: {value}")
    lines.extend(["", "## Error Types", ""])
    for error_type, count in (report.get("error_type_counts") or {}).items():
        lines.append(f"- {error_type}: {count}")
    lines.extend(["", "## Major Error Counts", ""])
    for item in (report.get("major_error_counts") or [])[:20]:
        lines.append(
            f"- {item.get('major_code') or ''} {item.get('major_name') or ''}: "
            f"{item.get('error_unit_count')}"
        )
    lines.extend(["", "## Dry-Run Metadata Updates", ""])
    for item in report.get("dry_run_updates") or []:
        lines.append(
            f"- {item.get('unit_code')}: "
            f"{item.get('current_error_type') or ''} -> {item.get('inferred_error_type')}, "
            f"attempt {item.get('current_attempt_count')} -> {item.get('proposed_attempt_count')}, "
            f"next_retry_at {item.get('current_next_retry_at') or ''} -> {item.get('proposed_next_retry_at')}"
        )
    lines.extend(
        [
            "",
            "## Policy",
            "",
            "- This report is read-only and does not call the qualification API.",
            "- Do not run broad retries while rate-limit errors dominate.",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def search_qualification_links(
    conn: sqlite3.Connection,
    *,
    unit_code: str | None = None,
    qualification_name: str | None = None,
    qualification_code: str | None = None,
    unit_type: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if unit_code:
        clauses.append("l.unit_code = ?")
        params.append(unit_code)
    if qualification_name:
        clauses.append("qi.jm_nm LIKE ?")
        params.append(f"%{qualification_name}%")
    if qualification_code:
        clauses.append("qi.jm_cd = ?")
        params.append(qualification_code)
    if unit_type:
        clauses.append("(l.ablt_unit_typ_cd = ? OR l.ablt_unit_typ_nm = ?)")
        params.extend([unit_type, unit_type])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
            l.*, qi.jm_nm, qi.exam_insti_nm,
            cu.unit_name_raw AS unit_name,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM ncs_unit_qualification_links l
        JOIN ncs_qualification_items qi ON qi.jm_cd = l.jm_cd
        LEFT JOIN competency_units cu ON cu.unit_code = l.unit_code
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY
            CASE WHEN l.ablt_unit_typ_cd = 'MAND' THEN 0 ELSE 1 END,
            qi.jm_nm, l.unit_code
        LIMIT ?
        """,
        params + [clamp_limit(limit, default=20, maximum=200)],
    ).fetchall()
    return rows_to_dicts(rows)


def qualification_profile_for_units(
    conn: sqlite3.Connection,
    unit_codes: set[str],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not unit_codes:
        return []
    rows = conn.execute(
        """
        SELECT
            l.*, qi.jm_nm, qi.exam_insti_nm,
            cu.unit_name_raw AS unit_name,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM ncs_unit_qualification_links l
        JOIN ncs_qualification_items qi ON qi.jm_cd = l.jm_cd
        LEFT JOIN competency_units cu ON cu.unit_code = l.unit_code
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE l.unit_code IN (SELECT value FROM json_each(?))
        ORDER BY
            CASE WHEN l.ablt_unit_typ_cd = 'MAND' THEN 0 ELSE 1 END,
            qi.jm_nm,
            l.unit_code
        LIMIT ?
        """,
        (json.dumps(sorted(unit_codes)), clamp_limit(limit, default=100, maximum=500)),
    ).fetchall()
    return rows_to_dicts(rows)


def _unit_codes_for_collection(
    conn: sqlite3.Connection,
    *,
    unit_codes: list[str] | None,
    major_code: str | None,
    all_units: bool,
    limit_units: int | None,
    resume: bool,
    collection_statuses: list[str] | None = None,
    retry_ready_only: bool = False,
) -> list[str]:
    if unit_codes:
        return sorted(set(unit_codes))
    if not major_code and not all_units:
        raise ValueError("Specify unit_codes, major_code, or all_units=True for qualification collection.")
    clauses: list[str] = []
    params: list[Any] = []
    if major_code:
        clauses.append("c.major_code = ?")
        params.append(major_code)
    if collection_statuses:
        placeholders = ",".join("?" for _ in collection_statuses)
        retry_clause = ""
        if retry_ready_only:
            retry_clause = (
                "AND (status.next_retry_at IS NULL "
                "OR julianday(status.next_retry_at) IS NULL "
                "OR julianday(status.next_retry_at) <= julianday(?))"
            )
        clauses.append(
            f"""
            EXISTS (
                SELECT 1
                FROM ncs_qualification_collection_status status
                WHERE status.unit_code = cu.unit_code
                  AND status.collection_status IN ({placeholders})
                  {retry_clause}
            )
            """
        )
        params.extend(collection_statuses)
        if retry_ready_only:
            params.append(now_utc())
    if resume:
        clauses.append(
            """
            NOT EXISTS (
                SELECT 1
                FROM ncs_qualification_collection_status status
                WHERE status.unit_code = cu.unit_code
                  AND status.collection_status IN ('collected', 'empty')
            )
            """
        )
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit_units is not None:
        limit_sql = "LIMIT ?"
        params.append(limit_units)
    rows = conn.execute(
        f"""
        SELECT cu.unit_code
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        {limit_sql}
        """,
        params,
    ).fetchall()
    return [row["unit_code"] for row in rows]


def _record_collection_status(
    conn: sqlite3.Connection,
    *,
    unit_code: str,
    status: str,
    rows_collected: int,
    pages_processed: int,
    result_code: str | None = None,
    result_msg: str | None = None,
    error: str | None = None,
    retry_backoff_seconds: float = 2.0,
) -> None:
    timestamp = now_utc()
    collected_at = timestamp if status in {"collected", "empty"} else None
    previous = conn.execute(
        "SELECT attempt_count FROM ncs_qualification_collection_status WHERE unit_code = ?",
        (unit_code,),
    ).fetchone()
    previous_attempts = int(previous["attempt_count"] or 0) if previous else 0
    error_type = _classify_qualification_error(error or result_msg) if status == "error" else None
    attempt_count = previous_attempts + 1 if status == "error" else previous_attempts
    next_retry_at = (
        _next_retry_at(error_type or "unknown", attempt_count, retry_backoff_seconds)
        if status == "error"
        else None
    )
    conn.execute(
        """
        INSERT INTO ncs_qualification_collection_status(
            unit_code, collection_status, rows_collected, pages_processed,
            last_result_code, last_result_msg, last_error, last_error_type,
            attempt_count, next_retry_at, collected_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(unit_code) DO UPDATE SET
            collection_status = excluded.collection_status,
            rows_collected = excluded.rows_collected,
            pages_processed = excluded.pages_processed,
            last_result_code = excluded.last_result_code,
            last_result_msg = excluded.last_result_msg,
            last_error = excluded.last_error,
            last_error_type = excluded.last_error_type,
            attempt_count = excluded.attempt_count,
            next_retry_at = excluded.next_retry_at,
            collected_at = excluded.collected_at,
            updated_at = excluded.updated_at
        """,
        (
            unit_code,
            status,
            rows_collected,
            pages_processed,
            result_code,
            result_msg,
            error,
            error_type,
            attempt_count,
            next_retry_at,
            collected_at,
            timestamp,
        ),
    )
    conn.commit()


def collect_qualification_links(
    db_path: Path,
    service_key: str,
    *,
    unit_codes: list[str] | None = None,
    major_code: str | None = None,
    all_units: bool = False,
    limit_units: int | None = None,
    page_no: int = 1,
    num_of_rows: int = 50,
    max_pages: int | None = None,
    timeout: int = 30,
    resume: bool = True,
    request_delay: float = 0.2,
    max_retries: int = 3,
    retry_backoff_seconds: float = 2.0,
    collection_statuses: list[str] | None = None,
    retry_ready_only: bool = False,
    stop_after_rate_limit_errors: int = 0,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    selected_unit_codes = _unit_codes_for_collection(
        conn,
        unit_codes=unit_codes,
        major_code=major_code,
        all_units=all_units,
        limit_units=limit_units,
        resume=resume,
        collection_statuses=collection_statuses,
        retry_ready_only=retry_ready_only,
    )
    units_processed = 0
    pages_processed = 0
    rows_upserted = 0
    empty_units = 0
    errors: list[dict[str, str]] = []
    rate_limit_error_count = 0
    stopped_early = False
    stop_reason = None
    try:
        for unit_code in selected_unit_codes:
            page = page_no
            unit_rows = 0
            unit_pages = 0
            last_result_code = None
            last_result_msg = None
            unit_error = None
            while True:
                try:
                    payload = fetch_qualification_page(
                        service_key,
                        unit_code=unit_code,
                        page_no=page,
                        num_of_rows=num_of_rows,
                        timeout=timeout,
                        max_retries=max_retries,
                        retry_backoff_seconds=retry_backoff_seconds,
                    )
                except QualificationApiError as exc:
                    unit_error = str(exc)
                    errors.append({"unit_code": unit_code, "error": unit_error})
                    rate_limit_attempts = int(getattr(exc, "rate_limit_attempts", 0) or 0)
                    if rate_limit_attempts:
                        rate_limit_error_count += rate_limit_attempts
                    elif _classify_qualification_error(unit_error) == "rate_limited":
                        rate_limit_error_count += 1
                    break
                last_result_code = payload.get("result_code")
                last_result_msg = payload.get("result_msg")
                if payload["result_code"] not in {"00", ""}:
                    unit_error = payload["result_msg"] or payload["result_code"]
                    errors.append(
                        {
                            "unit_code": unit_code,
                            "error": unit_error,
                        }
                    )
                    if _classify_qualification_error(unit_error) == "rate_limited":
                        rate_limit_error_count += 1
                    break
                rows_upserted += upsert_qualification_rows(
                    conn,
                    payload["rows"],
                    source_payload={
                        "api": "ncsClCdJm/getNcsClCdJmList",
                        "request": payload["request"],
                    },
                )
                unit_rows += len(payload["rows"])
                unit_pages += 1
                pages_processed += 1
                total_count = int(payload.get("total_count") or 0)
                page_size = int(
                    (payload.get("request") or {}).get("num_of_rows")
                    or payload.get("num_of_rows")
                    or num_of_rows
                    or 1
                )
                if max_pages and (page - page_no + 1) >= max_pages:
                    break
                if not payload["rows"] or page * page_size >= total_count:
                    break
                page += 1
                if request_delay > 0:
                    time.sleep(request_delay)
            if unit_rows == 0 and not unit_error:
                empty_units += 1
            _record_collection_status(
                conn,
                unit_code=unit_code,
                status="error" if unit_error else ("collected" if unit_rows else "empty"),
                rows_collected=unit_rows,
                pages_processed=unit_pages,
                result_code=last_result_code,
                result_msg=last_result_msg,
                error=unit_error,
                retry_backoff_seconds=retry_backoff_seconds,
            )
            units_processed += 1
            if (
                stop_after_rate_limit_errors > 0
                and rate_limit_error_count >= stop_after_rate_limit_errors
            ):
                stopped_early = True
                stop_reason = "rate_limited"
                break
            if request_delay > 0:
                time.sleep(request_delay)
        summary = qualification_summary(conn, limit=10)
    finally:
        conn.close()
    return {
        "ok": not errors,
        "unit_codes_requested": len(selected_unit_codes),
        "collection_status_filter": collection_statuses or [],
        "retry_ready_only": retry_ready_only,
        "units_processed": units_processed,
        "empty_units": empty_units,
        "pages_processed": pages_processed,
        "rows_upserted": rows_upserted,
        "errors": errors[:20],
        "error_count": len(errors),
        "rate_limit_error_count": rate_limit_error_count,
        "stopped_early": stopped_early,
        "stop_reason": stop_reason,
        "summary": summary,
    }


def retry_qualification_error_units(
    db_path: Path,
    service_key: str,
    *,
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
    stop_after_rate_limit_errors: int = 0,
) -> dict[str, Any]:
    result = collect_qualification_links(
        db_path,
        service_key,
        major_code=major_code,
        all_units=True,
        limit_units=limit_units,
        page_no=page_no,
        num_of_rows=num_of_rows,
        max_pages=max_pages,
        timeout=timeout,
        resume=False,
        request_delay=request_delay,
        max_retries=max_retries,
        retry_backoff_seconds=retry_backoff_seconds,
        collection_statuses=["error"],
        retry_ready_only=retry_ready_only,
        stop_after_rate_limit_errors=stop_after_rate_limit_errors,
    )
    conn = connect(db_path)
    initialize_database(conn)
    try:
        result["error_report"] = qualification_error_report(conn, limit=50)
    finally:
        conn.close()
    return result


if __name__ == "__main__":
    settings = load_settings()
    if not settings.qualification_service_key:
        raise SystemExit("NCS_QUALIFICATION_SERVICE_KEY or NCS_SERVICE_KEY is required")
    print(
        json.dumps(
            fetch_qualification_page(
                settings.qualification_service_key,
                unit_code="1501020207_14v2",
                num_of_rows=5,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
