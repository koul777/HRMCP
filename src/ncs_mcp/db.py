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

CREATE TABLE IF NOT EXISTS sqf_duties (
    source_key TEXT PRIMARY KEY,
    ncs_lclas_cd TEXT NOT NULL,
    ncs_lclas_name TEXT,
    sqf_field_name TEXT,
    sqf_sub_field_name TEXT,
    job_name TEXT,
    duty_name TEXT NOT NULL,
    duty_level TEXT,
    duty_level_name TEXT,
    duty_level_definition TEXT,
    duty_definition TEXT,
    autonomy_responsibility TEXT,
    duty_acarr TEXT,
    duty_education_training TEXT,
    duty_qualification TEXT,
    duty_career TEXT,
    duty_license TEXT,
    duty_remark TEXT,
    source_payload TEXT NOT NULL,
    api_fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sqf_ncs_matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL DEFAULT 'sqf_duty',
    source_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    confidence TEXT NOT NULL DEFAULT 'lexical',
    match_method TEXT NOT NULL,
    evidence_text TEXT,
    evidence_source TEXT,
    source_version TEXT,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    scope_tag TEXT,
    filter_status TEXT,
    reviewer_id TEXT,
    reviewed_at TEXT,
    reviewer_notes TEXT,
    exclusion_reason TEXT,
    created_by_method TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_type, source_id, target_type, target_id, relation, match_method)
);

CREATE TABLE IF NOT EXISTS sqf_library_posts (
    lib_seq TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT,
    list_page INTEGER,
    detail_url TEXT,
    source_url TEXT NOT NULL,
    published_at TEXT,
    updated_at TEXT,
    view_count INTEGER,
    source_html_hash TEXT,
    collected_at TEXT NOT NULL,
    ontology_role TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'metadata_collected',
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sqf_library_files (
    file_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lib_seq TEXT NOT NULL REFERENCES sqf_library_posts(lib_seq),
    sys_dstin_cd TEXT NOT NULL,
    file_mstky TEXT NOT NULL,
    file_detl_seq TEXT NOT NULL,
    downl_dstin_cd TEXT DEFAULT '09',
    original_filename TEXT,
    content_type TEXT,
    file_size INTEGER,
    local_path TEXT,
    content_hash TEXT,
    download_status TEXT NOT NULL DEFAULT 'pending',
    downloaded_at TEXT,
    error_message TEXT,
    UNIQUE (lib_seq, sys_dstin_cd, file_mstky, file_detl_seq)
);

CREATE TABLE IF NOT EXISTS sqf_document_sources (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    lib_seq TEXT NOT NULL REFERENCES sqf_library_posts(lib_seq),
    file_id INTEGER REFERENCES sqf_library_files(file_id),
    title TEXT NOT NULL,
    ontology_role TEXT,
    local_path TEXT,
    content_hash TEXT,
    text_extraction_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    UNIQUE (lib_seq, file_id)
);

CREATE TABLE IF NOT EXISTS sqf_framework_concepts (
    concept_code TEXT PRIMARY KEY,
    concept_name TEXT NOT NULL,
    definition TEXT NOT NULL,
    source_note TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sqf_industry_sectors (
    sector_id TEXT PRIMARY KEY,
    ncs_lclas_cd TEXT NOT NULL,
    ncs_lclas_name TEXT,
    sqf_field_name TEXT NOT NULL,
    sqf_sub_field_name TEXT,
    sector_name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'SQF openapi26',
    source_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (ncs_lclas_cd, sqf_field_name, sqf_sub_field_name)
);

CREATE TABLE IF NOT EXISTS sqf_jobs_normalized (
    sqf_job_id TEXT PRIMARY KEY,
    sector_id TEXT NOT NULL REFERENCES sqf_industry_sectors(sector_id),
    job_name TEXT NOT NULL,
    job_definition TEXT,
    vertical_mobility_note TEXT,
    horizontal_mobility_note TEXT,
    source_count INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    UNIQUE (sector_id, job_name)
);

CREATE TABLE IF NOT EXISTS sqf_levels (
    sqf_level INTEGER PRIMARY KEY,
    level_name TEXT,
    definition TEXT,
    kqf_based INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sqf_job_levels_normalized (
    sqf_job_level_id TEXT PRIMARY KEY,
    sqf_job_id TEXT NOT NULL REFERENCES sqf_jobs_normalized(sqf_job_id),
    sqf_source_key TEXT NOT NULL UNIQUE REFERENCES sqf_duties(source_key),
    duty_name TEXT NOT NULL,
    sqf_level INTEGER REFERENCES sqf_levels(sqf_level),
    level_name TEXT,
    job_level_definition TEXT,
    duty_definition TEXT,
    autonomy_responsibility TEXT,
    source_payload TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sqf_recognition_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    sqf_job_level_id TEXT NOT NULL REFERENCES sqf_job_levels_normalized(sqf_job_level_id),
    evidence_type TEXT NOT NULL CHECK (
        evidence_type IN (
            'degree', 'training', 'qualification', 'career',
            'license', 'remark', 'academic_career'
        )
    ),
    evidence_text TEXT NOT NULL,
    source_field TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'SQF openapi26',
    review_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (sqf_job_level_id, evidence_type, evidence_text, source_field)
);

CREATE TABLE IF NOT EXISTS sqf_document_evidence_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES sqf_document_sources(document_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'supportsDefinition',
    evidence_note TEXT,
    confidence TEXT NOT NULL DEFAULT 'document_title_rule',
    created_at TEXT NOT NULL,
    UNIQUE (document_id, target_type, target_id, relation)
);

CREATE TABLE IF NOT EXISTS sqf_document_assets (
    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES sqf_document_sources(document_id),
    asset_path TEXT NOT NULL UNIQUE,
    parent_archive_path TEXT,
    asset_name TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    content_hash TEXT,
    file_size INTEGER,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    extraction_engine TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sqf_document_pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES sqf_document_assets(asset_id),
    page_no INTEGER NOT NULL,
    text TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    extraction_status TEXT NOT NULL DEFAULT 'extracted',
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, page_no)
);

CREATE TABLE IF NOT EXISTS sqf_document_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES sqf_document_assets(asset_id),
    chunk_index INTEGER NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    keywords_json TEXT,
    ontology_tags_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (asset_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS sqf_chunk_job_level_matches (
    match_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id INTEGER NOT NULL REFERENCES sqf_document_chunks(chunk_id),
    sqf_job_level_id TEXT NOT NULL REFERENCES sqf_job_levels_normalized(sqf_job_level_id),
    sqf_source_key TEXT NOT NULL REFERENCES sqf_duties(source_key),
    relation TEXT NOT NULL DEFAULT 'evidenceForJobLevel',
    score REAL NOT NULL,
    method TEXT NOT NULL,
    evidence_text TEXT,
    matched_terms_json TEXT,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    UNIQUE (chunk_id, sqf_job_level_id, method)
);

CREATE TABLE IF NOT EXISTS review_audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    action TEXT NOT NULL,
    previous_status TEXT,
    new_status TEXT,
    reviewer_id TEXT,
    notes TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_name TEXT NOT NULL,
    scope_tag TEXT,
    metrics_json TEXT NOT NULL,
    created_at TEXT NOT NULL
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
    source_issue_id INTEGER REFERENCES quality_issues(issue_id),
    model_name TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    raw_text TEXT,
    refined_text TEXT,
    rationale TEXT,
    confidence REAL,
    output_text TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'review_required',
    created_at TEXT NOT NULL,
    applied_at TEXT
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
CREATE INDEX IF NOT EXISTS idx_sqf_major ON sqf_duties(ncs_lclas_cd);
CREATE INDEX IF NOT EXISTS idx_sqf_duty ON sqf_duties(duty_name);
CREATE INDEX IF NOT EXISTS idx_sqf_job ON sqf_duties(job_name);
CREATE INDEX IF NOT EXISTS idx_sqf_matches_source ON sqf_ncs_matches(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_sqf_matches_target ON sqf_ncs_matches(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_sqf_matches_review ON sqf_ncs_matches(review_status);
CREATE INDEX IF NOT EXISTS idx_sqf_matches_scope ON sqf_ncs_matches(scope_tag);
CREATE INDEX IF NOT EXISTS idx_sqf_library_title ON sqf_library_posts(title);
CREATE INDEX IF NOT EXISTS idx_sqf_library_role ON sqf_library_posts(ontology_role);
CREATE INDEX IF NOT EXISTS idx_sqf_library_files_status ON sqf_library_files(download_status);
CREATE INDEX IF NOT EXISTS idx_sqf_document_sources_role ON sqf_document_sources(ontology_role);
CREATE INDEX IF NOT EXISTS idx_sqf_document_sources_status ON sqf_document_sources(text_extraction_status);
CREATE INDEX IF NOT EXISTS idx_sqf_sectors_ncs ON sqf_industry_sectors(ncs_lclas_cd);
CREATE INDEX IF NOT EXISTS idx_sqf_jobs_sector ON sqf_jobs_normalized(sector_id);
CREATE INDEX IF NOT EXISTS idx_sqf_job_levels_job ON sqf_job_levels_normalized(sqf_job_id);
CREATE INDEX IF NOT EXISTS idx_sqf_job_levels_source ON sqf_job_levels_normalized(sqf_source_key);
CREATE INDEX IF NOT EXISTS idx_sqf_recognition_job_level ON sqf_recognition_evidence(sqf_job_level_id);
CREATE INDEX IF NOT EXISTS idx_sqf_recognition_type ON sqf_recognition_evidence(evidence_type);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_links_target ON sqf_document_evidence_links(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_assets_document ON sqf_document_assets(document_id);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_assets_status ON sqf_document_assets(extraction_status);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_pages_asset ON sqf_document_pages(asset_id);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_chunks_asset ON sqf_document_chunks(asset_id);
CREATE INDEX IF NOT EXISTS idx_sqf_doc_chunks_pages ON sqf_document_chunks(page_start, page_end);
CREATE INDEX IF NOT EXISTS idx_sqf_chunk_matches_chunk ON sqf_chunk_job_level_matches(chunk_id);
CREATE INDEX IF NOT EXISTS idx_sqf_chunk_matches_level ON sqf_chunk_job_level_matches(sqf_job_level_id);
CREATE INDEX IF NOT EXISTS idx_sqf_chunk_matches_source ON sqf_chunk_job_level_matches(sqf_source_key);
CREATE INDEX IF NOT EXISTS idx_sqf_chunk_matches_score ON sqf_chunk_job_level_matches(score);
CREATE INDEX IF NOT EXISTS idx_review_audit_entity ON review_audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_evaluation_scope ON evaluation_runs(scope_tag);
CREATE INDEX IF NOT EXISTS idx_refinement_target ON refinement_jobs(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_refinement_status ON refinement_jobs(review_status);
CREATE INDEX IF NOT EXISTS idx_refinement_issue ON refinement_jobs(source_issue_id);
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
    ensure_column(conn, "sqf_duties", "sqf_sub_field_name", "TEXT")
    ensure_column(conn, "sqf_duties", "duty_level_definition", "TEXT")
    ensure_column(conn, "sqf_duties", "duty_license", "TEXT")
    ensure_column(conn, "sqf_duties", "duty_remark", "TEXT")
    ensure_column(
        conn,
        "sqf_ncs_matches",
        "source_type",
        "TEXT NOT NULL DEFAULT 'sqf_duty'",
    )
    ensure_column(conn, "sqf_ncs_matches", "source_id", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "target_type", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "target_id", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "relation", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "score", "REAL NOT NULL DEFAULT 0")
    ensure_column(
        conn,
        "sqf_ncs_matches",
        "confidence",
        "TEXT NOT NULL DEFAULT 'lexical'",
    )
    ensure_column(conn, "sqf_ncs_matches", "match_method", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "evidence_text", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "evidence_source", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "source_version", "TEXT")
    ensure_column(
        conn,
        "sqf_ncs_matches",
        "review_status",
        "TEXT NOT NULL DEFAULT 'candidate'",
    )
    ensure_column(conn, "sqf_ncs_matches", "scope_tag", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "filter_status", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "reviewer_id", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "reviewed_at", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "reviewer_notes", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "exclusion_reason", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "created_by_method", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "created_at", "TEXT")
    ensure_column(conn, "sqf_ncs_matches", "updated_at", "TEXT")
    ensure_column(conn, "sqf_library_posts", "category", "TEXT")
    ensure_column(conn, "sqf_library_posts", "list_page", "INTEGER")
    ensure_column(conn, "sqf_library_posts", "detail_url", "TEXT")
    ensure_column(conn, "sqf_library_posts", "source_url", "TEXT")
    ensure_column(conn, "sqf_library_posts", "published_at", "TEXT")
    ensure_column(conn, "sqf_library_posts", "updated_at", "TEXT")
    ensure_column(conn, "sqf_library_posts", "view_count", "INTEGER")
    ensure_column(conn, "sqf_library_posts", "source_html_hash", "TEXT")
    ensure_column(conn, "sqf_library_posts", "collected_at", "TEXT")
    ensure_column(conn, "sqf_library_posts", "ontology_role", "TEXT")
    ensure_column(
        conn,
        "sqf_library_posts",
        "extraction_status",
        "TEXT NOT NULL DEFAULT 'metadata_collected'",
    )
    ensure_column(conn, "sqf_library_posts", "notes", "TEXT")
    ensure_column(conn, "sqf_library_files", "downl_dstin_cd", "TEXT DEFAULT '09'")
    ensure_column(conn, "sqf_library_files", "original_filename", "TEXT")
    ensure_column(conn, "sqf_library_files", "content_type", "TEXT")
    ensure_column(conn, "sqf_library_files", "file_size", "INTEGER")
    ensure_column(conn, "sqf_library_files", "local_path", "TEXT")
    ensure_column(conn, "sqf_library_files", "content_hash", "TEXT")
    ensure_column(
        conn,
        "sqf_library_files",
        "download_status",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    ensure_column(conn, "sqf_library_files", "downloaded_at", "TEXT")
    ensure_column(conn, "sqf_library_files", "error_message", "TEXT")
    ensure_column(conn, "sqf_document_sources", "ontology_role", "TEXT")
    ensure_column(conn, "sqf_document_sources", "local_path", "TEXT")
    ensure_column(conn, "sqf_document_sources", "content_hash", "TEXT")
    ensure_column(
        conn,
        "sqf_document_sources",
        "text_extraction_status",
        "TEXT NOT NULL DEFAULT 'pending'",
    )
    ensure_column(conn, "sqf_document_sources", "updated_at", "TEXT")
    ensure_column(
        conn,
        "sqf_chunk_job_level_matches",
        "review_status",
        "TEXT NOT NULL DEFAULT 'candidate'",
    )
    ensure_column(conn, "refinement_jobs", "source_issue_id", "INTEGER")
    ensure_column(conn, "refinement_jobs", "raw_text", "TEXT")
    ensure_column(conn, "refinement_jobs", "refined_text", "TEXT")
    ensure_column(conn, "refinement_jobs", "rationale", "TEXT")
    ensure_column(conn, "refinement_jobs", "confidence", "REAL")
    ensure_column(conn, "refinement_jobs", "applied_at", "TEXT")
    conn.execute(
        """
        UPDATE sqf_ncs_matches
        SET scope_tag = 'management_support'
        WHERE source_id IN (
            SELECT source_key
            FROM sqf_duties
            WHERE ncs_lclas_cd = '02'
              AND sqf_field_name = '경영관리'
              AND job_name = '경영지원'
        )
        """
    )
    conn.execute(
        """
        UPDATE sqf_ncs_matches
        SET scope_tag = (
            SELECT
                CASE
                    WHEN sd.ncs_lclas_cd = '02'
                     AND sd.sqf_field_name = '경영관리'
                     AND sd.job_name = '경영지원'
                    THEN 'management_support'
                    WHEN sd.ncs_lclas_cd = '02'
                    THEN 'business_accounting_office_02'
                    ELSE 'sqf_major_' || COALESCE(sd.ncs_lclas_cd, 'unknown')
                END
            FROM sqf_duties sd
            WHERE sd.source_key = sqf_ncs_matches.source_id
        )
        WHERE scope_tag IS NULL OR TRIM(scope_tag) = ''
        """
    )
    conn.execute(
        """
        UPDATE sqf_ncs_matches
        SET filter_status =
            CASE
                WHEN review_status = 'rejected' THEN 'excluded'
                WHEN review_status IN ('accepted', 'reviewed', 'human_reviewed') THEN 'eligible'
                WHEN relation = 'related' THEN 'excluded'
                WHEN score < 7 THEN 'excluded'
                ELSE 'eligible'
            END,
            exclusion_reason =
            CASE
                WHEN review_status = 'rejected' THEN 'rejected'
                WHEN review_status IN ('accepted', 'reviewed', 'human_reviewed') THEN NULL
                WHEN relation = 'related' THEN 'relation:related'
                WHEN score < 7 THEN 'score_below_threshold'
                ELSE NULL
            END
        WHERE filter_status IS NULL OR TRIM(filter_status) = ''
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("schema_version", "0.7.0"),
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
