from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.db import (
    clamp_limit,
    normalize_concept_key,
    normalize_spaces,
    now_utc,
    rows_to_dicts,
)


CAREER_PATH_COLUMNS = {
    "major_code": "대분류코드",
    "middle_code": "중분류코드",
    "small_code": "소분류코드",
    "job_code": "직무코드",
    "job_name": "직무명",
    "competency_code": "직무역량코드",
    "competency_level": "직무역량수준(능력단위수준 이면서 세분류의 자식)",
    "competency_name": "직무역량명",
    "position_level": "수준(직급수준)",
    "position_name": "직급명",
}


CAREER_PATH_COLUMN_ALIASES = {
    "major_code": (CAREER_PATH_COLUMNS["major_code"], "대분류", "대분류 코드", "NCS대분류코드"),
    "middle_code": (CAREER_PATH_COLUMNS["middle_code"], "중분류", "중분류 코드", "NCS중분류코드"),
    "small_code": (CAREER_PATH_COLUMNS["small_code"], "소분류", "소분류 코드", "NCS소분류코드"),
    "job_code": (CAREER_PATH_COLUMNS["job_code"], "직무 코드", "세분류자식코드"),
    "job_name": (CAREER_PATH_COLUMNS["job_name"], "직무 명", "세분류자식명"),
    "competency_code": (
        CAREER_PATH_COLUMNS["competency_code"],
        "직무 역량 코드",
        "능력단위분류번호",
        "능력단위코드",
    ),
    "competency_level": (
        CAREER_PATH_COLUMNS["competency_level"],
        "직무역량수준",
        "능력단위수준",
        "수준(능력단위수준)",
    ),
    "competency_name": (
        CAREER_PATH_COLUMNS["competency_name"],
        "직무 역량 명",
        "능력단위명",
        "능력단위명칭",
    ),
    "position_level": (CAREER_PATH_COLUMNS["position_level"], "직급수준", "수준"),
    "position_name": (CAREER_PATH_COLUMNS["position_name"], "직급 명", "직급"),
}


def _clean(value: Any) -> str:
    return normalize_spaces("" if value is None else str(value))


def _code(value: Any) -> str:
    text = _clean(value)
    return text.zfill(2) if text.isdigit() else text


def _header_key(value: str) -> str:
    return re.sub(r"[\W_]+", "", normalize_spaces(value), flags=re.UNICODE).lower()


def _resolve_column_map(fieldnames: list[str]) -> tuple[dict[str, str], list[str]]:
    fieldname_set = set(fieldnames)
    normalized_fieldnames = {_header_key(name): name for name in fieldnames}
    column_map: dict[str, str] = {}
    missing: list[str] = []
    for key, aliases in CAREER_PATH_COLUMN_ALIASES.items():
        matched = next((alias for alias in aliases if alias in fieldname_set), None)
        if matched is None:
            matched = next(
                (
                    normalized_fieldnames[_header_key(alias)]
                    for alias in aliases
                    if _header_key(alias) in normalized_fieldnames
                ),
                None,
            )
        if matched is None:
            missing.append(CAREER_PATH_COLUMNS[key])
        else:
            column_map[key] = matched
    return column_map, missing


def _row_value(row: dict[str, str], key: str, column_map: dict[str, str]) -> str:
    return _clean(row.get(column_map[key], ""))


def _match_classification(
    conn: sqlite3.Connection,
    *,
    major_code: str,
    middle_code: str,
    small_code: str,
    sub_code: str,
    job_name: str,
) -> tuple[int | None, str | None, float]:
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
        (major_code, middle_code, small_code, sub_code),
    ).fetchone()
    if row:
        return int(row["classification_id"]), "code_exact", 1.0
    row = conn.execute(
        """
        SELECT classification_id
        FROM classifications
        WHERE major_code = ?
          AND middle_code = ?
          AND small_code = ?
          AND sub_name = ?
        LIMIT 1
        """,
        (major_code, middle_code, small_code, job_name),
    ).fetchone()
    if row:
        return int(row["classification_id"]), "code_name_fallback", 0.9
    return None, None, 0.0


def _match_unit(
    conn: sqlite3.Connection,
    *,
    classification_id: int | None,
    competency_name: str,
) -> tuple[str | None, str | None, float]:
    if classification_id is None:
        return None, None, 0.0
    row = conn.execute(
        """
        SELECT unit_code
        FROM competency_units
        WHERE classification_id = ?
          AND unit_name_raw = ?
        ORDER BY unit_code
        LIMIT 1
        """,
        (classification_id, competency_name),
    ).fetchone()
    if row:
        return row["unit_code"], "unit_name_exact", 1.0
    target_key = normalize_concept_key(competency_name)
    candidates = conn.execute(
        """
        SELECT unit_code, unit_name_raw
        FROM competency_units
        WHERE classification_id = ?
        ORDER BY unit_code
        """,
        (classification_id,),
    ).fetchall()
    for candidate in candidates:
        if normalize_concept_key(candidate["unit_name_raw"]) == target_key:
            return candidate["unit_code"], "unit_name_normalized", 0.96
    for candidate in candidates:
        candidate_key = normalize_concept_key(candidate["unit_name_raw"])
        if target_key and (target_key in candidate_key or candidate_key in target_key):
            return candidate["unit_code"], "unit_name_contains", 0.86
    return None, None, 0.0


def import_career_paths_csv(
    conn: sqlite3.Connection,
    csv_path: Path | str,
    *,
    encoding: str = "cp949",
    reset: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    path = Path(csv_path)
    if reset:
        conn.execute("DELETE FROM ncs_career_paths WHERE source_file = ?", (str(path),))
    timestamp = now_utc()
    before = int(conn.execute("SELECT COUNT(*) FROM ncs_career_paths").fetchone()[0])
    inserted_or_updated = 0
    matched_classifications = 0
    matched_units = 0
    missing_columns: list[str] = []
    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.DictReader(handle)
        column_map, missing_columns = _resolve_column_map(reader.fieldnames or [])
        if missing_columns:
            return {
                "ok": False,
                "error": {
                    "code": "missing_columns",
                    "missing_columns": missing_columns,
                },
            }
        for row_number, row in enumerate(reader, start=2):
            if limit is not None and inserted_or_updated >= limit:
                break
            major_raw = _row_value(row, "major_code", column_map)
            middle_raw = _row_value(row, "middle_code", column_map)
            small_raw = _row_value(row, "small_code", column_map)
            job_raw = _row_value(row, "job_code", column_map)
            job_name = _row_value(row, "job_name", column_map)
            competency_name = _row_value(row, "competency_name", column_map)
            major_code = _code(major_raw)
            middle_code = _code(middle_raw)
            small_code = _code(small_raw)
            sub_code = _code(job_raw)
            classification_id, classification_method, classification_confidence = _match_classification(
                conn,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
                job_name=job_name,
            )
            unit_code, unit_method, unit_confidence = _match_unit(
                conn,
                classification_id=classification_id,
                competency_name=competency_name,
            )
            confidence = round((classification_confidence * 0.4) + (unit_confidence * 0.6), 4)
            if classification_id is not None:
                matched_classifications += 1
            if unit_code is not None:
                matched_units += 1
            conn.execute(
                """
                INSERT INTO ncs_career_paths(
                    source_file, source_row_number,
                    major_code_raw, middle_code_raw, small_code_raw, job_code_raw,
                    job_name, competency_code_raw, competency_level_raw,
                    competency_name, position_level_raw, position_name,
                    major_code, middle_code, small_code, sub_code,
                    matched_classification_id, matched_unit_code,
                    classification_match_method, unit_match_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                ON CONFLICT(source_file, source_row_number)
                DO UPDATE SET
                    major_code_raw = excluded.major_code_raw,
                    middle_code_raw = excluded.middle_code_raw,
                    small_code_raw = excluded.small_code_raw,
                    job_code_raw = excluded.job_code_raw,
                    job_name = excluded.job_name,
                    competency_code_raw = excluded.competency_code_raw,
                    competency_level_raw = excluded.competency_level_raw,
                    competency_name = excluded.competency_name,
                    position_level_raw = excluded.position_level_raw,
                    position_name = excluded.position_name,
                    major_code = excluded.major_code,
                    middle_code = excluded.middle_code,
                    small_code = excluded.small_code,
                    sub_code = excluded.sub_code,
                    matched_classification_id = excluded.matched_classification_id,
                    matched_unit_code = excluded.matched_unit_code,
                    classification_match_method = excluded.classification_match_method,
                    unit_match_method = excluded.unit_match_method,
                    confidence_score = excluded.confidence_score,
                    updated_at = excluded.updated_at
                """,
                (
                    str(path),
                    row_number,
                    major_raw,
                    middle_raw,
                    small_raw,
                    job_raw,
                    job_name,
                    _row_value(row, "competency_code", column_map),
                    _row_value(row, "competency_level", column_map),
                    competency_name,
                    _row_value(row, "position_level", column_map),
                    _row_value(row, "position_name", column_map),
                    major_code,
                    middle_code,
                    small_code,
                    sub_code,
                    classification_id,
                    unit_code,
                    classification_method,
                    unit_method,
                    confidence,
                    timestamp,
                    timestamp,
                ),
            )
            inserted_or_updated += 1
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ncs_career_paths_imported_at", timestamp),
    )
    conn.commit()
    after = int(conn.execute("SELECT COUNT(*) FROM ncs_career_paths").fetchone()[0])
    return {
        "ok": True,
        "source_file": str(path),
        "rows_processed": inserted_or_updated,
        "rows_before": before,
        "rows_after": after,
        "matched_classifications": matched_classifications,
        "matched_units": matched_units,
        "classification_match_rate": round(matched_classifications / inserted_or_updated, 4)
        if inserted_or_updated
        else 0.0,
        "unit_match_rate": round(matched_units / inserted_or_updated, 4) if inserted_or_updated else 0.0,
        "encoding": encoding,
        "column_map": column_map,
    }


def career_path_summary(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    total = int(conn.execute("SELECT COUNT(*) FROM ncs_career_paths").fetchone()[0])
    matched_units = int(
        conn.execute("SELECT COUNT(*) FROM ncs_career_paths WHERE matched_unit_code IS NOT NULL").fetchone()[0]
    )
    by_job = rows_to_dicts(
        conn.execute(
            """
            SELECT job_name, COUNT(*) AS count
            FROM ncs_career_paths
            GROUP BY job_name
            ORDER BY count DESC, job_name
            LIMIT ?
            """,
            (clamp_limit(limit, default=20, maximum=100),),
        ).fetchall()
    )
    unmatched = rows_to_dicts(
        conn.execute(
            """
            SELECT job_name, competency_name, major_code, middle_code, small_code, sub_code
            FROM ncs_career_paths
            WHERE matched_unit_code IS NULL
            ORDER BY major_code, middle_code, small_code, sub_code, competency_name
            LIMIT ?
            """,
            (clamp_limit(limit, default=20, maximum=100),),
        ).fetchall()
    )
    return {
        "ok": True,
        "career_path_count": total,
        "matched_unit_count": matched_units,
        "unit_match_rate": round(matched_units / total, 4) if total else 0.0,
        "top_jobs": by_job,
        "sample_unmatched": unmatched,
    }


def career_paths_for_units(
    conn: sqlite3.Connection,
    unit_codes: set[str],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not unit_codes:
        return []
    rows = conn.execute(
        """
        SELECT *
        FROM ncs_career_paths
        WHERE matched_unit_code IN (SELECT value FROM json_each(?))
        ORDER BY
            CAST(COALESCE(position_level_raw, '0') AS INTEGER) DESC,
            CAST(COALESCE(competency_level_raw, '0') AS INTEGER) DESC,
            job_name,
            competency_name
        LIMIT ?
        """,
        (json.dumps(sorted(unit_codes)), clamp_limit(limit, default=100, maximum=500)),
    ).fetchall()
    return rows_to_dicts(rows)
