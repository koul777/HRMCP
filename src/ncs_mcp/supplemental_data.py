from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.db import clamp_limit, normalize_spaces, now_utc, rows_to_dicts


COL_UNIT_CODE = "분류번호"
COL_UNIT_NAME = "명칭"
COL_UNIT_LEVEL = "수준"
COL_UNIT_HOURS = "훈련시간"

COL_MAPPING_NCS_CODE = "NCS 코드"
COL_MAPPING_NCS_NAME = "NCS 코드명"
COL_MAPPING_NATIONAL_CODE = "국가기간직종 코드"
COL_MAPPING_NATIONAL_NAME = "국가기간직종 코드명"
COL_MAPPING_KECO_CODE = "KECO 코드"
COL_MAPPING_KECO_NAME = "KECO 코드명"

COL_ZIP_SEQ = "연번"
COL_ZIP_COURSE = "훈련과정명"
COL_ZIP_BUSINESS = "사업구분"
COL_ZIP_INSTITUTION = "훈련기관명"
COL_ZIP_NCS_CODE = "국가직무능력표준(NCS) 코드"
COL_ZIP_MAJOR_CODE = "국가직무능력표준(NCS) 코드1"
COL_ZIP_MIDDLE_CODE = "국가직무능력표준(NCS) 코드2"
COL_ZIP_SMALL_CODE = "국가직무능력표준(NCS) 코드3"
COL_ZIP_MAJOR_NAME = "국가직무능력표준(NCS) 코드명1"
COL_ZIP_MIDDLE_NAME = "국가직무능력표준(NCS) 코드명2"
COL_ZIP_SMALL_NAME = "국가직무능력표준(NCS) 코드명3"
COL_ZIP_METHOD = "훈련방법"
COL_ZIP_HOURS = "훈련시간"


def _clean(value: Any) -> str:
    return normalize_spaces("" if value is None else str(value))


def _number(value: Any) -> float | None:
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _digits(value: Any) -> str:
    return "".join(ch for ch in _clean(value) if ch.isdigit())


def normalize_ncs_code(value: Any) -> str:
    digits = _digits(value)
    if not digits:
        return ""
    if len(digits) in {1, 3, 5, 7}:
        return digits.zfill(len(digits) + 1)
    return digits


def ncs_code_level(code: str) -> str:
    if len(code) == 2:
        return "major"
    if len(code) == 4:
        return "middle"
    if len(code) == 6:
        return "small"
    if len(code) == 8:
        return "sub"
    return "unknown"


def _required_columns(fieldnames: list[str] | None, required: list[str]) -> list[str]:
    existing = set(fieldnames or [])
    return [column for column in required if column not in existing]


def _source_file(path: Path) -> str:
    try:
        return str(path.expanduser().resolve()).casefold()
    except OSError:
        return str(path.expanduser().absolute()).casefold()


def _match_unit(conn: sqlite3.Connection, unit_code: str) -> tuple[str | None, str]:
    row = conn.execute(
        "SELECT unit_code FROM competency_units WHERE unit_code = ?",
        (unit_code,),
    ).fetchone()
    if row:
        return str(row["unit_code"]), "matched_unit_exact"
    return None, "unmatched_unit"


def _classification_exists(conn: sqlite3.Connection, code: str) -> tuple[int | None, str]:
    level = ncs_code_level(code)
    if level == "sub":
        row = conn.execute(
            """
            SELECT classification_id
            FROM classifications
            WHERE major_code = ?
              AND middle_code = ?
              AND small_code = ?
              AND sub_code = ?
            LIMIT 1
            """,
            (code[0:2], code[2:4], code[4:6], code[6:8]),
        ).fetchone()
        if row:
            return int(row["classification_id"]), "matched_sub_exact"
        return None, "unmatched_sub"
    if level == "small":
        row = conn.execute(
            """
            SELECT classification_id
            FROM classifications
            WHERE major_code = ?
              AND middle_code = ?
              AND small_code = ?
            LIMIT 1
            """,
            (code[0:2], code[2:4], code[4:6]),
        ).fetchone()
        if row:
            return None, "matched_small_scope"
        return None, "unmatched_small"
    if level == "middle":
        row = conn.execute(
            """
            SELECT classification_id
            FROM classifications
            WHERE major_code = ?
              AND middle_code = ?
            LIMIT 1
            """,
            (code[0:2], code[2:4]),
        ).fetchone()
        if row:
            return None, "matched_middle_scope"
        return None, "unmatched_middle"
    if level == "major":
        row = conn.execute(
            "SELECT classification_id FROM classifications WHERE major_code = ? LIMIT 1",
            (code,),
        ).fetchone()
        if row:
            return None, "matched_major_scope"
        return None, "unmatched_major"
    return None, "unmatched_unknown_code"


def import_unit_standard_training_csv(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    encoding: str = "cp949",
    reset: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    path = Path(csv_path)
    source_file = _source_file(path)
    if reset:
        conn.execute("DELETE FROM ncs_unit_standard_training WHERE source_file = ?", (source_file,))
    timestamp = now_utc()
    processed = 0
    matched = 0
    missing_columns: list[str] = []
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        required = [COL_UNIT_CODE, COL_UNIT_NAME, COL_UNIT_LEVEL, COL_UNIT_HOURS]
        missing_columns = _required_columns(reader.fieldnames, required)
        if missing_columns:
            return {"ok": False, "error": {"code": "missing_columns", "missing_columns": missing_columns}}
        for row_number, row in enumerate(reader, start=2):
            if limit is not None and processed >= limit:
                break
            unit_code = _clean(row.get(COL_UNIT_CODE))
            if not unit_code:
                continue
            matched_unit_code, match_status = _match_unit(conn, unit_code)
            if matched_unit_code:
                matched += 1
            conn.execute(
                """
                INSERT INTO ncs_unit_standard_training(
                    source_file, source_row_number, unit_code_raw, unit_name,
                    unit_level, standard_training_hours, matched_unit_code,
                    match_status, source_payload, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_file, source_row_number)
                DO UPDATE SET
                    unit_code_raw = excluded.unit_code_raw,
                    unit_name = excluded.unit_name,
                    unit_level = excluded.unit_level,
                    standard_training_hours = excluded.standard_training_hours,
                    matched_unit_code = excluded.matched_unit_code,
                    match_status = excluded.match_status,
                    source_payload = excluded.source_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    source_file,
                    row_number,
                    unit_code,
                    _clean(row.get(COL_UNIT_NAME)),
                    _clean(row.get(COL_UNIT_LEVEL)),
                    _number(row.get(COL_UNIT_HOURS)),
                    matched_unit_code,
                    match_status,
                    json.dumps(row, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            processed += 1
    conn.commit()
    return {
        "ok": True,
        "source_file": source_file,
        "rows_processed": processed,
        "matched_units": matched,
        "unmatched_units": processed - matched,
        "table_total": int(conn.execute("SELECT COUNT(*) FROM ncs_unit_standard_training").fetchone()[0]),
    }


def import_occupation_code_mapping_csv(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    encoding: str = "utf-8-sig",
    reset: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    path = Path(csv_path)
    source_file = _source_file(path)
    if reset:
        conn.execute("DELETE FROM ncs_occupation_code_mappings WHERE source_file = ?", (source_file,))
    timestamp = now_utc()
    processed = 0
    matched = 0
    levels: dict[str, int] = {}
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        required = [
            COL_MAPPING_NCS_CODE,
            COL_MAPPING_NCS_NAME,
            COL_MAPPING_NATIONAL_CODE,
            COL_MAPPING_NATIONAL_NAME,
            COL_MAPPING_KECO_CODE,
            COL_MAPPING_KECO_NAME,
        ]
        missing_columns = _required_columns(reader.fieldnames, required)
        if missing_columns:
            return {"ok": False, "error": {"code": "missing_columns", "missing_columns": missing_columns}}
        for row_number, row in enumerate(reader, start=2):
            if limit is not None and processed >= limit:
                break
            raw_code = _clean(row.get(COL_MAPPING_NCS_CODE))
            code = normalize_ncs_code(raw_code)
            if not code:
                continue
            level = ncs_code_level(code)
            levels[level] = levels.get(level, 0) + 1
            classification_id, match_status = _classification_exists(conn, code)
            if match_status.startswith("matched_"):
                matched += 1
            conn.execute(
                """
                INSERT INTO ncs_occupation_code_mappings(
                    source_file, source_row_number, ncs_code_raw,
                    ncs_code_normalized, ncs_code_level, ncs_code_name,
                    national_job_code, national_job_name, keco_code, keco_name,
                    matched_classification_id, match_status, source_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_file, source_row_number)
                DO UPDATE SET
                    ncs_code_raw = excluded.ncs_code_raw,
                    ncs_code_normalized = excluded.ncs_code_normalized,
                    ncs_code_level = excluded.ncs_code_level,
                    ncs_code_name = excluded.ncs_code_name,
                    national_job_code = excluded.national_job_code,
                    national_job_name = excluded.national_job_name,
                    keco_code = excluded.keco_code,
                    keco_name = excluded.keco_name,
                    matched_classification_id = excluded.matched_classification_id,
                    match_status = excluded.match_status,
                    source_payload = excluded.source_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    source_file,
                    row_number,
                    raw_code,
                    code,
                    level,
                    _clean(row.get(COL_MAPPING_NCS_NAME)),
                    _clean(row.get(COL_MAPPING_NATIONAL_CODE)),
                    _clean(row.get(COL_MAPPING_NATIONAL_NAME)),
                    _clean(row.get(COL_MAPPING_KECO_CODE)),
                    _clean(row.get(COL_MAPPING_KECO_NAME)),
                    classification_id,
                    match_status,
                    json.dumps(row, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            processed += 1
    conn.commit()
    return {
        "ok": True,
        "source_file": source_file,
        "rows_processed": processed,
        "matched_classification_scope_rows": matched,
        "unmatched_rows": processed - matched,
        "rows_by_ncs_code_level": levels,
        "table_total": int(conn.execute("SELECT COUNT(*) FROM ncs_occupation_code_mappings").fetchone()[0]),
    }


def _zip_code_from_columns(row: dict[str, str]) -> str:
    code = normalize_ncs_code(row.get(COL_ZIP_NCS_CODE))
    if code:
        return code
    major = _digits(row.get(COL_ZIP_MAJOR_CODE)).zfill(2)
    middle = _digits(row.get(COL_ZIP_MIDDLE_CODE)).zfill(2)
    small = _digits(row.get(COL_ZIP_SMALL_CODE)).zfill(2)
    return major + middle + small if major.strip("0") or middle.strip("0") or small.strip("0") else ""


def import_external_training_zip_csv(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    encoding: str = "cp949",
    reset: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    path = Path(csv_path)
    source_file = _source_file(path)
    if reset:
        conn.execute("DELETE FROM ncs_external_training_zip_courses WHERE source_file = ?", (source_file,))
    timestamp = now_utc()
    processed = 0
    matched = 0
    business_types: dict[str, int] = {}
    methods: dict[str, int] = {}
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        required = [
            COL_ZIP_SEQ,
            COL_ZIP_COURSE,
            COL_ZIP_BUSINESS,
            COL_ZIP_INSTITUTION,
            COL_ZIP_METHOD,
            COL_ZIP_HOURS,
        ]
        missing_columns = _required_columns(reader.fieldnames, required)
        fieldnames = set(reader.fieldnames or [])
        has_combined_code = COL_ZIP_NCS_CODE in fieldnames
        has_split_code = {COL_ZIP_MAJOR_CODE, COL_ZIP_MIDDLE_CODE, COL_ZIP_SMALL_CODE}.issubset(fieldnames)
        if not has_combined_code and not has_split_code:
            missing_columns.append(
                f"{COL_ZIP_NCS_CODE} or {COL_ZIP_MAJOR_CODE}/{COL_ZIP_MIDDLE_CODE}/{COL_ZIP_SMALL_CODE}"
            )
        if missing_columns:
            return {"ok": False, "error": {"code": "missing_columns", "missing_columns": missing_columns}}
        for row_number, row in enumerate(reader, start=2):
            if limit is not None and processed >= limit:
                break
            course_name = _clean(row.get(COL_ZIP_COURSE))
            if not course_name:
                continue
            code = _zip_code_from_columns(row)
            level = ncs_code_level(code)
            classification_id, match_status = _classification_exists(conn, code)
            if match_status.startswith("matched_"):
                matched += 1
            business_type = _clean(row.get(COL_ZIP_BUSINESS))
            method = _clean(row.get(COL_ZIP_METHOD))
            if business_type:
                business_types[business_type] = business_types.get(business_type, 0) + 1
            if method:
                methods[method] = methods.get(method, 0) + 1
            conn.execute(
                """
                INSERT INTO ncs_external_training_zip_courses(
                    source_file, source_row_number, external_sequence,
                    course_name, business_type, institution_name,
                    ncs_code_raw, ncs_code_normalized, ncs_code_level,
                    ncs_major_code, ncs_middle_code, ncs_small_code, ncs_sub_code,
                    ncs_major_name, ncs_middle_name, ncs_small_name,
                    training_method, training_hours,
                    matched_classification_id, match_status, source_payload,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_file, source_row_number)
                DO UPDATE SET
                    external_sequence = excluded.external_sequence,
                    course_name = excluded.course_name,
                    business_type = excluded.business_type,
                    institution_name = excluded.institution_name,
                    ncs_code_raw = excluded.ncs_code_raw,
                    ncs_code_normalized = excluded.ncs_code_normalized,
                    ncs_code_level = excluded.ncs_code_level,
                    ncs_major_code = excluded.ncs_major_code,
                    ncs_middle_code = excluded.ncs_middle_code,
                    ncs_small_code = excluded.ncs_small_code,
                    ncs_sub_code = excluded.ncs_sub_code,
                    ncs_major_name = excluded.ncs_major_name,
                    ncs_middle_name = excluded.ncs_middle_name,
                    ncs_small_name = excluded.ncs_small_name,
                    training_method = excluded.training_method,
                    training_hours = excluded.training_hours,
                    matched_classification_id = excluded.matched_classification_id,
                    match_status = excluded.match_status,
                    source_payload = excluded.source_payload,
                    updated_at = excluded.updated_at
                """,
                (
                    source_file,
                    row_number,
                    _clean(row.get(COL_ZIP_SEQ)),
                    course_name,
                    business_type,
                    _clean(row.get(COL_ZIP_INSTITUTION)),
                    _clean(row.get(COL_ZIP_NCS_CODE)),
                    code,
                    level,
                    code[0:2] if len(code) >= 2 else "",
                    code[2:4] if len(code) >= 4 else "",
                    code[4:6] if len(code) >= 6 else "",
                    code[6:8] if len(code) >= 8 else "",
                    _clean(row.get(COL_ZIP_MAJOR_NAME)),
                    _clean(row.get(COL_ZIP_MIDDLE_NAME)),
                    _clean(row.get(COL_ZIP_SMALL_NAME)),
                    method,
                    _number(row.get(COL_ZIP_HOURS)),
                    classification_id,
                    match_status,
                    json.dumps(row, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
            processed += 1
    conn.commit()
    return {
        "ok": True,
        "source_file": source_file,
        "rows_processed": processed,
        "matched_classification_scope_rows": matched,
        "unmatched_rows": processed - matched,
        "business_types": business_types,
        "training_methods": methods,
        "table_total": int(conn.execute("SELECT COUNT(*) FROM ncs_external_training_zip_courses").fetchone()[0]),
    }


def supplemental_data_summary(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    limit = clamp_limit(limit, default=20, maximum=100)

    def count(table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def grouped(table: str, column: str) -> list[dict[str, Any]]:
        return rows_to_dicts(
            conn.execute(
                f"""
                SELECT {column} AS value, COUNT(*) AS count
                FROM {table}
                GROUP BY {column}
                ORDER BY count DESC, value
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        )

    return {
        "unit_standard_training_count": count("ncs_unit_standard_training"),
        "occupation_code_mapping_count": count("ncs_occupation_code_mappings"),
        "external_training_zip_course_count": count("ncs_external_training_zip_courses"),
        "unit_standard_match_status": grouped("ncs_unit_standard_training", "match_status"),
        "occupation_mapping_match_status": grouped("ncs_occupation_code_mappings", "match_status"),
        "occupation_mapping_ncs_levels": grouped("ncs_occupation_code_mappings", "ncs_code_level"),
        "external_training_match_status": grouped("ncs_external_training_zip_courses", "match_status"),
        "external_training_business_types": grouped("ncs_external_training_zip_courses", "business_type"),
        "external_training_methods": grouped("ncs_external_training_zip_courses", "training_method"),
    }
