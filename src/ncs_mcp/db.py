from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_excel_rows (
    raw_row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    sheet_name TEXT NOT NULL,
    sheet_row_number INTEGER NOT NULL,
    major_code TEXT NOT NULL,
    major_name TEXT NOT NULL,
    middle_code TEXT NOT NULL,
    middle_name TEXT NOT NULL,
    small_code TEXT NOT NULL,
    small_name TEXT NOT NULL,
    sub_code TEXT NOT NULL,
    sub_name TEXT NOT NULL,
    unit_code TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    unit_level TEXT NOT NULL,
    element_code TEXT NOT NULL,
    element_name TEXT NOT NULL,
    element_level TEXT NOT NULL,
    criteria_no TEXT NOT NULL,
    criteria_text TEXT NOT NULL,
    ksa_type_code TEXT NOT NULL,
    ksa_type_name TEXT NOT NULL,
    ksa_no TEXT NOT NULL,
    ksa_text TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS classifications (
    classification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    major_code TEXT NOT NULL,
    major_name TEXT NOT NULL,
    middle_code TEXT NOT NULL,
    middle_name TEXT NOT NULL,
    small_code TEXT NOT NULL,
    small_name TEXT NOT NULL,
    sub_code TEXT NOT NULL,
    sub_name TEXT NOT NULL,
    duty_def_api TEXT,
    duty_def_refined TEXT,
    duty_order TEXT,
    api_ncs_degr TEXT,
    api_usg_yn TEXT,
    review_status TEXT NOT NULL DEFAULT 'raw',
    UNIQUE (major_code, middle_code, small_code, sub_code)
);

CREATE TABLE IF NOT EXISTS competency_units (
    unit_code TEXT PRIMARY KEY,
    base_unit_code TEXT NOT NULL,
    unit_version TEXT NOT NULL,
    unit_name_raw TEXT NOT NULL,
    unit_name_refined TEXT,
    unit_level_raw TEXT NOT NULL,
    classification_id INTEGER NOT NULL REFERENCES classifications(classification_id),
    api_unit_name TEXT,
    api_unit_level TEXT,
    api_definition TEXT,
    api_definition_refined TEXT,
    api_match_status TEXT NOT NULL DEFAULT 'not_collected',
    review_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS competency_elements (
    element_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    element_no TEXT NOT NULL,
    element_code_raw TEXT NOT NULL,
    element_name_raw TEXT NOT NULL,
    element_name_refined TEXT,
    element_level_raw TEXT NOT NULL,
    api_element_name TEXT,
    api_element_level TEXT,
    api_match_status TEXT NOT NULL DEFAULT 'not_collected',
    review_status TEXT NOT NULL DEFAULT 'raw',
    UNIQUE (unit_code, element_code_raw, element_name_raw)
);

CREATE TABLE IF NOT EXISTS performance_criteria (
    criteria_id INTEGER PRIMARY KEY AUTOINCREMENT,
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    criteria_no TEXT NOT NULL,
    criteria_text_raw TEXT NOT NULL,
    criteria_text_refined TEXT,
    review_status TEXT NOT NULL DEFAULT 'raw',
    UNIQUE (element_id, criteria_no, criteria_text_raw)
);

CREATE TABLE IF NOT EXISTS ksa_items (
    ksa_id INTEGER PRIMARY KEY AUTOINCREMENT,
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    ksa_type_code TEXT NOT NULL,
    ksa_type_name TEXT NOT NULL,
    ksa_no TEXT NOT NULL,
    ksa_text_raw TEXT NOT NULL,
    ksa_text_refined TEXT,
    review_status TEXT NOT NULL DEFAULT 'raw',
    UNIQUE (element_id, ksa_type_code, ksa_no, ksa_text_raw)
);

CREATE TABLE IF NOT EXISTS element_criteria_ksa_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_row_id INTEGER NOT NULL REFERENCES raw_excel_rows(raw_row_id),
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    criteria_id INTEGER NOT NULL REFERENCES performance_criteria(criteria_id),
    ksa_id INTEGER NOT NULL REFERENCES ksa_items(ksa_id),
    UNIQUE (raw_row_id)
);

CREATE TABLE IF NOT EXISTS api_raw_responses (
    api_response_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    page_no INTEGER NOT NULL,
    num_of_rows INTEGER NOT NULL,
    total_count INTEGER,
    result_code TEXT,
    result_msg TEXT,
    response_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE (source_url, page_no, num_of_rows)
);

CREATE TABLE IF NOT EXISTS api_competency_units (
    ncs_cl_cd TEXT PRIMARY KEY,
    compe_unit_name TEXT,
    compe_unit_level TEXT,
    ncs_lclas_cdnm TEXT,
    ncs_mclas_cdnm TEXT,
    ncs_sclas_cdnm TEXT,
    ncs_subd_cdnm TEXT,
    compe_unit_def TEXT,
    api_fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quality_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    issue_detail TEXT NOT NULL,
    suggested_action TEXT,
    detected_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS refinement_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    output_text TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'review_required',
    created_at TEXT NOT NULL
);
"""


INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_raw_unit_code ON raw_excel_rows(unit_code);
CREATE INDEX IF NOT EXISTS idx_raw_element_code ON raw_excel_rows(element_code);
CREATE INDEX IF NOT EXISTS idx_units_classification ON competency_units(classification_id);
CREATE INDEX IF NOT EXISTS idx_units_name ON competency_units(unit_name_raw);
CREATE INDEX IF NOT EXISTS idx_elements_unit ON competency_elements(unit_code);
CREATE INDEX IF NOT EXISTS idx_criteria_element ON performance_criteria(element_id);
CREATE INDEX IF NOT EXISTS idx_criteria_text ON performance_criteria(criteria_text_raw);
CREATE INDEX IF NOT EXISTS idx_ksa_element ON ksa_items(element_id);
CREATE INDEX IF NOT EXISTS idx_ksa_type ON ksa_items(ksa_type_name);
CREATE INDEX IF NOT EXISTS idx_ksa_text ON ksa_items(ksa_text_raw);
CREATE INDEX IF NOT EXISTS idx_links_element ON element_criteria_ksa_links(element_id);
CREATE INDEX IF NOT EXISTS idx_links_criteria ON element_criteria_ksa_links(criteria_id);
CREATE INDEX IF NOT EXISTS idx_links_ksa ON element_criteria_ksa_links(ksa_id);
CREATE INDEX IF NOT EXISTS idx_quality_target ON quality_issues(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_quality_type ON quality_issues(issue_type);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    ensure_column(conn, "classifications", "duty_def_api", "TEXT")
    ensure_column(conn, "classifications", "duty_def_refined", "TEXT")
    ensure_column(conn, "classifications", "duty_order", "TEXT")
    ensure_column(conn, "classifications", "api_ncs_degr", "TEXT")
    ensure_column(conn, "classifications", "api_usg_yn", "TEXT")
    ensure_column(
        conn,
        "classifications",
        "review_status",
        "TEXT NOT NULL DEFAULT 'raw'",
    )
    ensure_column(conn, "competency_units", "unit_name_refined", "TEXT")
    ensure_column(conn, "competency_units", "api_definition_refined", "TEXT")
    ensure_column(
        conn,
        "competency_units",
        "review_status",
        "TEXT NOT NULL DEFAULT 'raw'",
    )
    ensure_column(conn, "competency_elements", "api_element_name", "TEXT")
    ensure_column(conn, "competency_elements", "api_element_level", "TEXT")
    ensure_column(
        conn,
        "competency_elements",
        "api_match_status",
        "TEXT NOT NULL DEFAULT 'not_collected'",
    )
    ensure_column(conn, "competency_elements", "element_name_refined", "TEXT")
    ensure_column(
        conn,
        "competency_elements",
        "review_status",
        "TEXT NOT NULL DEFAULT 'raw'",
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("schema_version", "0.1.0"),
    )
    conn.commit()


def ensure_column(conn: sqlite3.Connection, table: str, column: str, column_type: str) -> None:
    columns = {
        row["name"]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")


def create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(INDEX_SQL)
    conn.commit()


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def normalize_spaces(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def split_unit_code(unit_code: str) -> tuple[str, str]:
    if "_" not in unit_code:
        return unit_code, ""
    base, version = unit_code.split("_", 1)
    return base, version


def parse_element_no(element_code: str, unit_code: str) -> str:
    prefix = f"{unit_code} "
    if element_code.startswith(prefix):
        return element_code[len(prefix) :].strip()
    parts = element_code.split()
    return parts[-1] if parts else ""


def clamp_limit(limit: int | None, default: int = 50, maximum: int = 500) -> int:
    if limit is None:
        return default
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(parsed, maximum))


def clear_quality_issues(conn: sqlite3.Connection, issue_types: list[str]) -> None:
    if not issue_types:
        return
    placeholders = ",".join("?" for _ in issue_types)
    conn.execute(f"DELETE FROM quality_issues WHERE issue_type IN ({placeholders})", issue_types)


def insert_quality_issue(
    conn: sqlite3.Connection,
    *,
    target_type: str,
    target_id: str | int,
    issue_type: str,
    severity: str,
    issue_detail: str,
    suggested_action: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO quality_issues(
            target_type, target_id, issue_type, severity,
            issue_detail, suggested_action, detected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            target_type,
            str(target_id),
            issue_type,
            severity,
            issue_detail,
            suggested_action,
            now_utc(),
        ),
    )
