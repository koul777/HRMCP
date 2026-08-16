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

CREATE TABLE IF NOT EXISTS ncs_learning_modules (
    learn_module_seq TEXT PRIMARY KEY,
    learn_module_name TEXT NOT NULL,
    learn_module_text TEXT,
    ncs_lclas_cd TEXT,
    ncs_lclas_name TEXT,
    ncs_mclas_cd TEXT,
    ncs_mclas_name TEXT,
    ncs_sclas_cd TEXT,
    ncs_sclas_name TEXT,
    ncs_subd_cd TEXT,
    ncs_subd_name TEXT,
    source_payload TEXT NOT NULL,
    api_fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncs_training_courses (
    training_course_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ncs_cl_cd TEXT NOT NULL,
    compe_unit_name TEXT,
    compe_unit_level TEXT,
    ncs_lclas_cd TEXT,
    ncs_lclas_cdnm TEXT,
    ncs_mclas_cd TEXT,
    ncs_mclas_cdnm TEXT,
    ncs_sclas_cd TEXT,
    ncs_sclas_cdnm TEXT,
    ncs_subd_cd TEXT,
    ncs_subd_cdnm TEXT,
    train_goal TEXT,
    train_time TEXT,
    fac_name TEXT,
    meth_name TEXT,
    source_payload TEXT,
    api_fetched_at TEXT NOT NULL,
    UNIQUE (ncs_cl_cd, train_goal, train_time, fac_name, meth_name)
);

CREATE TABLE IF NOT EXISTS ncs_unit_standard_training (
    unit_standard_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    unit_code_raw TEXT NOT NULL,
    unit_name TEXT NOT NULL,
    unit_level TEXT,
    standard_training_hours REAL,
    matched_unit_code TEXT REFERENCES competency_units(unit_code),
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    source_payload TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_file, source_row_number)
);

CREATE TABLE IF NOT EXISTS ncs_occupation_code_mappings (
    mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    ncs_code_raw TEXT NOT NULL,
    ncs_code_normalized TEXT NOT NULL,
    ncs_code_level TEXT NOT NULL,
    ncs_code_name TEXT,
    national_job_code TEXT,
    national_job_name TEXT,
    keco_code TEXT,
    keco_name TEXT,
    matched_classification_id INTEGER REFERENCES classifications(classification_id),
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    source_payload TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_file, source_row_number)
);

CREATE TABLE IF NOT EXISTS ncs_external_training_zip_courses (
    external_training_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    external_sequence TEXT,
    course_name TEXT NOT NULL,
    business_type TEXT,
    institution_name TEXT,
    ncs_code_raw TEXT,
    ncs_code_normalized TEXT,
    ncs_code_level TEXT,
    ncs_major_code TEXT,
    ncs_middle_code TEXT,
    ncs_small_code TEXT,
    ncs_sub_code TEXT,
    ncs_major_name TEXT,
    ncs_middle_name TEXT,
    ncs_small_name TEXT,
    training_method TEXT,
    training_hours REAL,
    matched_classification_id INTEGER REFERENCES classifications(classification_id),
    match_status TEXT NOT NULL DEFAULT 'unmatched',
    source_payload TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_file, source_row_number)
);

CREATE TABLE IF NOT EXISTS ncs_training_course_unit_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 1.0,
    review_status TEXT NOT NULL DEFAULT 'reviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, unit_code, link_method)
);

CREATE TABLE IF NOT EXISTS ncs_training_course_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    unit_code TEXT REFERENCES competency_units(unit_code),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_text TEXT,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, unit_code, concept_id, link_method)
);

CREATE TABLE IF NOT EXISTS ncs_training_course_element_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_text TEXT,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, element_id, link_method)
);

CREATE TABLE IF NOT EXISTS training_goal_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    unit_code TEXT REFERENCES competency_units(unit_code),
    element_id INTEGER REFERENCES competency_elements(element_id),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_text TEXT,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, element_id, concept_id, link_method)
);

CREATE TABLE IF NOT EXISTS training_delivery_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    relation_type TEXT NOT NULL,
    relation_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    numeric_value REAL,
    evidence_text TEXT,
    confidence_score REAL NOT NULL DEFAULT 1.0,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, relation_type, normalized_value)
);

CREATE TABLE IF NOT EXISTS ncs_qualification_items (
    jm_cd TEXT PRIMARY KEY,
    jm_nm TEXT NOT NULL,
    exam_insti_nm TEXT,
    source_payload TEXT,
    api_fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncs_unit_qualification_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    jm_cd TEXT NOT NULL REFERENCES ncs_qualification_items(jm_cd),
    organ_std_ver_cd TEXT,
    edu_trng_std_tm_sum INTEGER,
    job_basis_ablt_std_tm INTEGER,
    mand_ablt_unit_std_tm INTEGER,
    sel_ablt_unit_std_tm INTEGER,
    compe_unit_name TEXT,
    ablt_unit_typ_cd TEXT,
    ablt_unit_typ_nm TEXT,
    min_edu_trng_tm INTEGER,
    link_method TEXT NOT NULL DEFAULT 'ncs_cl_cd_exact',
    confidence_score REAL NOT NULL DEFAULT 1.0,
    source_payload TEXT,
    api_fetched_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'reviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (unit_code, jm_cd, organ_std_ver_cd, ablt_unit_typ_cd, min_edu_trng_tm)
);

CREATE TABLE IF NOT EXISTS ncs_qualification_collection_status (
    unit_code TEXT PRIMARY KEY REFERENCES competency_units(unit_code),
    collection_status TEXT NOT NULL,
    rows_collected INTEGER NOT NULL DEFAULT 0,
    pages_processed INTEGER NOT NULL DEFAULT 0,
    last_result_code TEXT,
    last_result_msg TEXT,
    last_error TEXT,
    last_error_type TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_retry_at TEXT,
    collected_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncs_job_base_competencies (
    job_base_competency_id INTEGER PRIMARY KEY AUTOINCREMENT,
    competency_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncs_job_base_factors (
    job_base_factor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_base_competency_id INTEGER NOT NULL REFERENCES ncs_job_base_competencies(job_base_competency_id),
    factor_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (job_base_competency_id, normalized_key)
);

CREATE TABLE IF NOT EXISTS ncs_unit_job_base_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    job_base_competency_id INTEGER NOT NULL REFERENCES ncs_job_base_competencies(job_base_competency_id),
    job_base_factor_id INTEGER REFERENCES ncs_job_base_factors(job_base_factor_id),
    ncs_lclas_cd TEXT,
    ncs_lclas_cdnm TEXT,
    ncs_mclas_cd TEXT,
    ncs_mclas_cdnm TEXT,
    ncs_sclas_cd TEXT,
    ncs_sclas_cdnm TEXT,
    ncs_subd_cd TEXT,
    ncs_subd_cdnm TEXT,
    compe_unit_name TEXT,
    link_method TEXT NOT NULL DEFAULT 'ncs_cl_cd_exact',
    confidence_score REAL NOT NULL DEFAULT 1.0,
    source_payload TEXT,
    api_fetched_at TEXT NOT NULL,
    review_status TEXT NOT NULL DEFAULT 'reviewed',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (unit_code, job_base_competency_id, job_base_factor_id)
);

CREATE TABLE IF NOT EXISTS learning_module_unit_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    learn_module_seq TEXT NOT NULL REFERENCES ncs_learning_modules(learn_module_seq),
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_text TEXT,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (learn_module_seq, unit_code, link_method)
);

CREATE TABLE IF NOT EXISTS learning_module_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    learn_module_seq TEXT NOT NULL REFERENCES ncs_learning_modules(learn_module_seq),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_text TEXT,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (learn_module_seq, concept_id, link_method)
);

CREATE TABLE IF NOT EXISTS ncs_reference_documents (
    document_id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source_path TEXT,
    source_hash TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL DEFAULT 'html',
    import_status TEXT NOT NULL DEFAULT 'imported',
    page_count INTEGER NOT NULL DEFAULT 0,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ncs_reference_pages (
    page_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES ncs_reference_documents(document_id),
    page_no INTEGER NOT NULL,
    width REAL,
    height REAL,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL DEFAULT 0,
    text_nodes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, page_no)
);

CREATE TABLE IF NOT EXISTS ncs_reference_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES ncs_reference_documents(document_id),
    chunk_index INTEGER NOT NULL,
    page_start INTEGER,
    page_end INTEGER,
    text TEXT NOT NULL,
    char_count INTEGER NOT NULL,
    token_estimate INTEGER NOT NULL,
    summary TEXT NOT NULL,
    location_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (document_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS ncs_reference_entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES ncs_reference_documents(document_id),
    chunk_id INTEGER NOT NULL REFERENCES ncs_reference_chunks(chunk_id),
    page_no INTEGER,
    entity_type TEXT NOT NULL,
    entity_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    start_offset INTEGER,
    end_offset INTEGER,
    extraction_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_summary TEXT,
    metadata_json TEXT,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    UNIQUE (document_id, chunk_id, entity_type, normalized_text, start_offset)
);

CREATE TABLE IF NOT EXISTS ncs_reference_entity_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES ncs_reference_entities(entity_id),
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL DEFAULT 'mentions',
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    evidence_summary TEXT,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (entity_id, target_type, target_id, relation, link_method)
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

CREATE TABLE IF NOT EXISTS ncs_query_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_text TEXT NOT NULL,
    normalized_query TEXT NOT NULL,
    major_code TEXT,
    middle_code TEXT,
    small_code TEXT,
    sub_code TEXT,
    unit_code TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.8,
    source_method TEXT NOT NULL DEFAULT 'seed',
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_transition_gold_scenarios (
    scenario_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name TEXT NOT NULL UNIQUE,
    current_query TEXT NOT NULL,
    target_query TEXT NOT NULL,
    major_code TEXT,
    expected_current_match_text TEXT,
    expected_target_match_text TEXT,
    expected_course_names_json TEXT NOT NULL DEFAULT '[]',
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_transition_scenario_reviews (
    review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_id INTEGER NOT NULL,
    review_method TEXT NOT NULL,
    source_review_status TEXT,
    target_review_status TEXT NOT NULL,
    applied INTEGER NOT NULL DEFAULT 0,
    eligible INTEGER NOT NULL DEFAULT 0,
    status_updated INTEGER NOT NULL DEFAULT 0,
    blockers_json TEXT NOT NULL DEFAULT '[]',
    criteria_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    expected_course_hits_json TEXT NOT NULL DEFAULT '[]',
    recommended_courses_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    FOREIGN KEY(scenario_id) REFERENCES training_transition_gold_scenarios(scenario_id)
);

CREATE TABLE IF NOT EXISTS ncs_career_paths (
    career_path_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file TEXT NOT NULL,
    source_row_number INTEGER NOT NULL,
    major_code_raw TEXT NOT NULL,
    middle_code_raw TEXT NOT NULL,
    small_code_raw TEXT NOT NULL,
    job_code_raw TEXT NOT NULL,
    job_name TEXT NOT NULL,
    competency_code_raw TEXT NOT NULL,
    competency_level_raw TEXT,
    competency_name TEXT NOT NULL,
    position_level_raw TEXT,
    position_name TEXT,
    major_code TEXT NOT NULL,
    middle_code TEXT NOT NULL,
    small_code TEXT NOT NULL,
    sub_code TEXT NOT NULL,
    matched_classification_id INTEGER REFERENCES classifications(classification_id),
    matched_unit_code TEXT REFERENCES competency_units(unit_code),
    classification_match_method TEXT,
    unit_match_method TEXT,
    confidence_score REAL NOT NULL DEFAULT 0,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_file, source_row_number)
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

CREATE TABLE IF NOT EXISTS ontology_concepts (
    concept_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_name TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    concept_type TEXT NOT NULL,
    definition TEXT,
    definition_source TEXT,
    definition_status TEXT NOT NULL DEFAULT 'missing',
    relation_status TEXT NOT NULL DEFAULT 'unlinked',
    review_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (concept_type, normalized_key)
);

CREATE TABLE IF NOT EXISTS ontology_concept_aliases (
    alias_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    alias_text TEXT NOT NULL,
    normalized_alias_key TEXT NOT NULL,
    alias_source TEXT NOT NULL DEFAULT 'raw_ksa',
    created_at TEXT NOT NULL,
    UNIQUE (concept_id, normalized_alias_key)
);

CREATE TABLE IF NOT EXISTS ontology_concept_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    relation_type TEXT NOT NULL,
    target_concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    relation_label TEXT,
    review_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (source_concept_id, relation_type, target_concept_id)
);

CREATE TABLE IF NOT EXISTS ksa_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ksa_id INTEGER NOT NULL REFERENCES ksa_items(ksa_id),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    link_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (ksa_id)
);

CREATE TABLE IF NOT EXISTS criteria_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    criteria_id INTEGER NOT NULL REFERENCES performance_criteria(criteria_id),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    relation_type TEXT NOT NULL DEFAULT 'related',
    link_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (criteria_id, concept_id, relation_type)
);

CREATE TABLE IF NOT EXISTS ksa_atomic_items (
    atomic_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ksa_id INTEGER NOT NULL REFERENCES ksa_items(ksa_id),
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    ksa_type_name TEXT NOT NULL,
    atom_index INTEGER NOT NULL,
    atom_text TEXT NOT NULL,
    normalized_key TEXT NOT NULL,
    split_method TEXT NOT NULL DEFAULT 'rule_based',
    review_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (ksa_id, atom_index, normalized_key)
);

CREATE TABLE IF NOT EXISTS ksa_atomic_concept_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    atomic_id INTEGER NOT NULL REFERENCES ksa_atomic_items(atomic_id),
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    link_status TEXT NOT NULL DEFAULT 'raw',
    created_at TEXT NOT NULL,
    UNIQUE (atomic_id, concept_id)
);

CREATE TABLE IF NOT EXISTS ksa_meaning_candidates (
    meaning_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    concept_type TEXT NOT NULL,
    meaning_role TEXT NOT NULL,
    meaning_text TEXT NOT NULL,
    source_method TEXT NOT NULL,
    evidence_text TEXT,
    unit_code TEXT REFERENCES competency_units(unit_code),
    element_id INTEGER REFERENCES competency_elements(element_id),
    criteria_id INTEGER REFERENCES performance_criteria(criteria_id),
    ksa_id INTEGER REFERENCES ksa_items(ksa_id),
    confidence_score REAL NOT NULL DEFAULT 0.6,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (concept_id, meaning_role, source_method)
);

CREATE TABLE IF NOT EXISTS task_ksa_concept_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    criteria_id INTEGER NOT NULL REFERENCES performance_criteria(criteria_id),
    element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    source_concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    relation_type TEXT NOT NULL,
    target_concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    source_atomic_id INTEGER NOT NULL REFERENCES ksa_atomic_items(atomic_id),
    target_atomic_id INTEGER NOT NULL REFERENCES ksa_atomic_items(atomic_id),
    evidence_text TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.55,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    UNIQUE (
        criteria_id, source_concept_id, relation_type, target_concept_id,
        source_atomic_id, target_atomic_id
    )
);

CREATE TABLE IF NOT EXISTS task_similarity_links (
    similarity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_criteria_id INTEGER NOT NULL REFERENCES performance_criteria(criteria_id),
    target_criteria_id INTEGER NOT NULL REFERENCES performance_criteria(criteria_id),
    source_element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    target_element_id INTEGER NOT NULL REFERENCES competency_elements(element_id),
    source_unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    target_unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    relation_type TEXT NOT NULL,
    similarity_score REAL NOT NULL,
    shared_concept_count INTEGER NOT NULL,
    source_concept_count INTEGER NOT NULL,
    target_concept_count INTEGER NOT NULL,
    source_only_count INTEGER NOT NULL,
    target_only_count INTEGER NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    UNIQUE (source_criteria_id, target_criteria_id, relation_type)
);

CREATE TABLE IF NOT EXISTS education_recommendation_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    target_source_key TEXT,
    request_payload TEXT NOT NULL,
    target_payload TEXT NOT NULL,
    summary_payload TEXT NOT NULL,
    audit_payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS education_recommendation_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES education_recommendation_runs(run_id),
    rank INTEGER NOT NULL,
    learn_module_seq TEXT,
    learn_module_name TEXT,
    recommendation_payload TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 0,
    confidence_grade TEXT NOT NULL DEFAULT 'insufficient',
    created_at TEXT NOT NULL,
    UNIQUE (run_id, rank)
);

CREATE TABLE IF NOT EXISTS education_recommendation_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES education_recommendation_runs(run_id),
    item_id INTEGER NOT NULL REFERENCES education_recommendation_items(item_id),
    evidence_type TEXT NOT NULL,
    source_table TEXT,
    source_id TEXT,
    chunk_id INTEGER,
    match_id INTEGER,
    unit_code TEXT,
    concept_id INTEGER,
    learn_module_seq TEXT,
    evidence_text TEXT,
    evidence_summary TEXT,
    confidence_score REAL,
    created_at TEXT NOT NULL
);
"""


INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_raw_unit_code ON raw_excel_rows(unit_code);
CREATE INDEX IF NOT EXISTS idx_raw_element_code ON raw_excel_rows(element_code);
CREATE INDEX IF NOT EXISTS idx_classifications_codes ON classifications(major_code, middle_code, small_code, sub_code);
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
CREATE INDEX IF NOT EXISTS idx_learning_modules_major ON ncs_learning_modules(ncs_lclas_cd);
CREATE INDEX IF NOT EXISTS idx_learning_modules_name ON ncs_learning_modules(learn_module_name);
CREATE INDEX IF NOT EXISTS idx_training_courses_ncs ON ncs_training_courses(ncs_cl_cd);
CREATE INDEX IF NOT EXISTS idx_training_courses_major ON ncs_training_courses(ncs_lclas_cd);
CREATE INDEX IF NOT EXISTS idx_training_courses_scope ON ncs_training_courses(ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd);
CREATE INDEX IF NOT EXISTS idx_unit_standard_code ON ncs_unit_standard_training(unit_code_raw);
CREATE INDEX IF NOT EXISTS idx_unit_standard_match ON ncs_unit_standard_training(match_status, matched_unit_code);
CREATE INDEX IF NOT EXISTS idx_occupation_mapping_ncs ON ncs_occupation_code_mappings(ncs_code_normalized, ncs_code_level);
CREATE INDEX IF NOT EXISTS idx_occupation_mapping_match ON ncs_occupation_code_mappings(match_status, matched_classification_id);
CREATE INDEX IF NOT EXISTS idx_occupation_mapping_keco ON ncs_occupation_code_mappings(keco_code);
CREATE INDEX IF NOT EXISTS idx_external_training_ncs ON ncs_external_training_zip_courses(ncs_code_normalized, ncs_code_level);
CREATE INDEX IF NOT EXISTS idx_external_training_match ON ncs_external_training_zip_courses(match_status, matched_classification_id);
CREATE INDEX IF NOT EXISTS idx_external_training_business ON ncs_external_training_zip_courses(business_type);
CREATE INDEX IF NOT EXISTS idx_external_training_method ON ncs_external_training_zip_courses(training_method);
CREATE INDEX IF NOT EXISTS idx_training_course_links_course ON ncs_training_course_unit_links(training_course_id);
CREATE INDEX IF NOT EXISTS idx_training_course_links_unit ON ncs_training_course_unit_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_training_course_concepts_course ON ncs_training_course_concept_links(training_course_id);
CREATE INDEX IF NOT EXISTS idx_training_course_concepts_unit ON ncs_training_course_concept_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_training_course_concepts_concept ON ncs_training_course_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_training_course_elements_course ON ncs_training_course_element_links(training_course_id);
CREATE INDEX IF NOT EXISTS idx_training_course_elements_unit ON ncs_training_course_element_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_training_course_elements_element ON ncs_training_course_element_links(element_id);
CREATE INDEX IF NOT EXISTS idx_training_goal_concepts_course ON training_goal_concept_links(training_course_id);
CREATE INDEX IF NOT EXISTS idx_training_goal_concepts_element ON training_goal_concept_links(element_id);
CREATE INDEX IF NOT EXISTS idx_training_goal_concepts_concept ON training_goal_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_training_goal_concepts_review ON training_goal_concept_links(review_status, link_method, confidence_score);
CREATE INDEX IF NOT EXISTS idx_training_delivery_course ON training_delivery_relations(training_course_id);
CREATE INDEX IF NOT EXISTS idx_training_delivery_type ON training_delivery_relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_learning_unit_module ON learning_module_unit_links(learn_module_seq);
CREATE INDEX IF NOT EXISTS idx_learning_unit_unit ON learning_module_unit_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_learning_concept_module ON learning_module_concept_links(learn_module_seq);
CREATE INDEX IF NOT EXISTS idx_learning_concept_concept ON learning_module_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_documents_hash ON ncs_reference_documents(source_hash);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_pages_document ON ncs_reference_pages(document_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_chunks_document ON ncs_reference_chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_chunks_pages ON ncs_reference_chunks(page_start, page_end);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_entities_document ON ncs_reference_entities(document_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_entities_chunk ON ncs_reference_entities(chunk_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_entities_type ON ncs_reference_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_entity_links_entity ON ncs_reference_entity_links(entity_id);
CREATE INDEX IF NOT EXISTS idx_ncs_ref_entity_links_target ON ncs_reference_entity_links(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_concepts_type ON ontology_concepts(concept_type);
CREATE INDEX IF NOT EXISTS idx_concepts_key ON ontology_concepts(normalized_key);
CREATE INDEX IF NOT EXISTS idx_aliases_key ON ontology_concept_aliases(normalized_alias_key);
CREATE INDEX IF NOT EXISTS idx_relations_source ON ontology_concept_relations(source_concept_id);
CREATE INDEX IF NOT EXISTS idx_relations_target ON ontology_concept_relations(target_concept_id);
CREATE INDEX IF NOT EXISTS idx_ksa_concepts_ksa ON ksa_concept_links(ksa_id);
CREATE INDEX IF NOT EXISTS idx_ksa_concepts_concept ON ksa_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_criteria_concepts_criteria ON criteria_concept_links(criteria_id);
CREATE INDEX IF NOT EXISTS idx_criteria_concepts_concept ON criteria_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_atomic_items_ksa ON ksa_atomic_items(ksa_id);
CREATE INDEX IF NOT EXISTS idx_atomic_items_element ON ksa_atomic_items(element_id);
CREATE INDEX IF NOT EXISTS idx_atomic_items_key ON ksa_atomic_items(normalized_key);
CREATE INDEX IF NOT EXISTS idx_atomic_concepts_atomic ON ksa_atomic_concept_links(atomic_id);
CREATE INDEX IF NOT EXISTS idx_atomic_concepts_concept ON ksa_atomic_concept_links(concept_id);
CREATE INDEX IF NOT EXISTS idx_ksa_meaning_concept ON ksa_meaning_candidates(concept_id);
CREATE INDEX IF NOT EXISTS idx_ksa_meaning_type ON ksa_meaning_candidates(concept_type);
CREATE INDEX IF NOT EXISTS idx_ksa_meaning_unit ON ksa_meaning_candidates(unit_code);
CREATE INDEX IF NOT EXISTS idx_ksa_meaning_status ON ksa_meaning_candidates(review_status);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_criteria ON task_ksa_concept_relations(criteria_id);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_element ON task_ksa_concept_relations(element_id);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_source ON task_ksa_concept_relations(source_concept_id);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_target ON task_ksa_concept_relations(target_concept_id);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_type ON task_ksa_concept_relations(relation_type);
CREATE INDEX IF NOT EXISTS idx_task_ksa_rel_review ON task_ksa_concept_relations(review_status, relation_type, confidence_score);
CREATE INDEX IF NOT EXISTS idx_task_similarity_source ON task_similarity_links(source_criteria_id);
CREATE INDEX IF NOT EXISTS idx_task_similarity_target ON task_similarity_links(target_criteria_id);
CREATE INDEX IF NOT EXISTS idx_task_similarity_type ON task_similarity_links(relation_type);
CREATE INDEX IF NOT EXISTS idx_task_similarity_score ON task_similarity_links(similarity_score);
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
CREATE INDEX IF NOT EXISTS idx_recommendation_runs_target ON education_recommendation_runs(target_source_key);
CREATE INDEX IF NOT EXISTS idx_recommendation_items_run ON education_recommendation_items(run_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_run ON education_recommendation_evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_item ON education_recommendation_evidence(item_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_match ON education_recommendation_evidence(match_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_evidence_chunk ON education_recommendation_evidence(chunk_id);
CREATE INDEX IF NOT EXISTS idx_qualification_items_name ON ncs_qualification_items(jm_nm);
CREATE INDEX IF NOT EXISTS idx_unit_qualification_unit ON ncs_unit_qualification_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_unit_qualification_jm ON ncs_unit_qualification_links(jm_cd);
CREATE INDEX IF NOT EXISTS idx_unit_qualification_type ON ncs_unit_qualification_links(ablt_unit_typ_cd);
CREATE INDEX IF NOT EXISTS idx_qualification_collection_status ON ncs_qualification_collection_status(collection_status);
CREATE INDEX IF NOT EXISTS idx_qualification_collection_retry ON ncs_qualification_collection_status(collection_status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_job_base_competency_name ON ncs_job_base_competencies(competency_name);
CREATE INDEX IF NOT EXISTS idx_job_base_factor_name ON ncs_job_base_factors(factor_name);
CREATE INDEX IF NOT EXISTS idx_unit_job_base_unit ON ncs_unit_job_base_links(unit_code);
CREATE INDEX IF NOT EXISTS idx_unit_job_base_competency ON ncs_unit_job_base_links(job_base_competency_id);
CREATE INDEX IF NOT EXISTS idx_unit_job_base_factor ON ncs_unit_job_base_links(job_base_factor_id);
CREATE INDEX IF NOT EXISTS idx_query_alias_text ON ncs_query_aliases(alias_text);
CREATE INDEX IF NOT EXISTS idx_query_alias_normalized ON ncs_query_aliases(normalized_query);
CREATE INDEX IF NOT EXISTS idx_transition_gold_status ON training_transition_gold_scenarios(review_status);
CREATE INDEX IF NOT EXISTS idx_transition_reviews_scenario ON training_transition_scenario_reviews(scenario_id);
CREATE INDEX IF NOT EXISTS idx_transition_reviews_created ON training_transition_scenario_reviews(created_at);
CREATE INDEX IF NOT EXISTS idx_career_path_codes ON ncs_career_paths(major_code, middle_code, small_code, sub_code);
CREATE INDEX IF NOT EXISTS idx_career_path_job ON ncs_career_paths(job_name);
CREATE INDEX IF NOT EXISTS idx_career_path_competency ON ncs_career_paths(competency_name);
CREATE INDEX IF NOT EXISTS idx_career_path_unit ON ncs_career_paths(matched_unit_code);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _seed_default_query_aliases(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(UTC).isoformat()
    aliases = [
        ("노무", "노무관리", "02", "02", "02", "02", None, 0.95),
        ("노무관리", "노무관리", "02", "02", "02", "02", None, 0.98),
        ("노사", "노무관리", "02", "02", "02", "02", None, 0.9),
        ("노사관리", "노무관리", "02", "02", "02", "02", None, 0.9),
        ("노동관계", "노무관리", "02", "02", "02", "02", None, 0.85),
        ("근로관계", "노무관리", "02", "02", "02", "02", None, 0.85),
        ("총무", "총무", "02", "02", "01", "01", None, 0.98),
        ("총무업무", "총무", "02", "02", "01", "01", None, 0.9),
        ("인사기획", "인사기획", "02", "02", "02", "01", "0202020101_23v3", 0.98),
        ("인사전략", "인사기획", "02", "02", "02", "01", "0202020101_23v3", 0.9),
        ("HR planning", "인사기획", "02", "02", "02", "01", "0202020101_23v3", 0.9),
        ("HRBP", "인사기획", "02", "02", "02", "01", "0202020101_23v3", 0.75),
        ("채용", "인력채용", "02", "02", "02", "01", "0202020102_23v3", 0.9),
        ("인력채용", "인력채용", "02", "02", "02", "01", "0202020102_23v3", 0.95),
        ("임금", "임금관리", "02", "02", "02", "01", None, 0.9),
        ("임금관리", "임금관리", "02", "02", "02", "01", None, 0.95),
        ("복무", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.78),
        ("복무관리", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.86),
        ("근태", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.92),
        ("근태관리", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.96),
        ("출퇴근관리", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.85),
        ("휴가관리", "근태관리", "02", "02", "02", "01", "0202020109_23v5", 0.82),
        ("복리후생", "복리후생관리", "02", "02", "02", "01", None, 0.9),
        ("인력개발", "교육훈련운영", "02", "02", "02", "01", "0202020107_23v4", 0.85),
        ("HRD", "교육훈련운영", "02", "02", "02", "01", "0202020107_23v4", 0.8),
        ("교육훈련", "교육훈련운영", "02", "02", "02", "01", "0202020107_23v4", 0.85),
    ]
    for alias in aliases:
        conn.execute(
            """
            INSERT INTO ncs_query_aliases(
                alias_text, normalized_query, major_code, middle_code,
                small_code, sub_code, unit_code, confidence_score,
                source_method, review_status, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, 'seed', 'candidate', ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM ncs_query_aliases
                WHERE alias_text = ? AND normalized_query = ?
            )
            """,
            (*alias, timestamp, timestamp, alias[0], alias[1]),
        )


def _seed_default_transition_gold_scenarios(conn: sqlite3.Connection) -> None:
    timestamp = datetime.now(UTC).isoformat()
    scenarios = [
        (
            "general_affairs_to_labor_management",
            "총무",
            "노무관리",
            "02",
            "총무",
            "노무관리",
            '["노사관계 계획","단체교섭","단체교섭준비","노동쟁의 대응","노사협의회 운영","노사갈등 해결"]',
        ),
        (
            "labor_management_to_hr_planning",
            "노무관리",
            "인사기획",
            "02",
            "노무관리",
            "인사기획",
            '["인사기획","직무관리","인력채용","임금관리"]',
        ),
        (
            "recruiting_to_hr_development",
            "인력채용",
            "교육훈련운영",
            "02",
            "인력채용",
            "교육훈련운영",
            '["교육훈련운영","핵심인재관리"]',
        ),
        (
            "general_affairs_to_hr_planning",
            "총무",
            "인사기획",
            "02",
            "총무",
            "인사기획",
            '["인사기획","직무관리","인력채용"]',
        ),
        (
            "general_affairs_to_recruiting",
            "총무",
            "인력채용",
            "02",
            "총무",
            "인력채용",
            '["인력채용","인사기획","직무관리"]',
        ),
        (
            "general_affairs_to_compensation",
            "총무",
            "임금관리",
            "02",
            "총무",
            "임금관리",
            '["임금관리","급여지급","인사기획"]',
        ),
        (
            "general_affairs_to_benefits",
            "총무",
            "복리후생관리",
            "02",
            "총무",
            "복리후생관리",
            '["복리후생관리","임금관리","인사기획"]',
        ),
        (
            "general_affairs_to_hrd",
            "총무",
            "교육훈련운영",
            "02",
            "총무",
            "교육훈련운영",
            '["교육훈련운영","핵심인재관리","인사기획"]',
        ),
        (
            "labor_management_to_compensation",
            "노무관리",
            "임금관리",
            "02",
            "노무관리",
            "임금관리",
            '["임금관리","급여지급","단체교섭준비"]',
        ),
        (
            "labor_management_to_recruiting",
            "노무관리",
            "인력채용",
            "02",
            "노무관리",
            "인력채용",
            '["인력채용","인사기획","직무관리"]',
        ),
        (
            "labor_management_to_hrd",
            "노무관리",
            "교육훈련운영",
            "02",
            "노무관리",
            "교육훈련운영",
            '["교육훈련운영","노사관계 교육훈련","핵심인재관리"]',
        ),
        (
            "labor_management_to_benefits",
            "노무관리",
            "복리후생관리",
            "02",
            "노무관리",
            "복리후생관리",
            '["복리후생관리","임금관리","인사기획"]',
        ),
        (
            "recruiting_to_hr_planning",
            "인력채용",
            "인사기획",
            "02",
            "인력채용",
            "인사기획",
            '["인사기획","직무관리","인력채용"]',
        ),
        (
            "recruiting_to_compensation",
            "인력채용",
            "임금관리",
            "02",
            "인력채용",
            "임금관리",
            '["임금관리","급여지급","인사기획"]',
        ),
        (
            "recruiting_to_benefits",
            "인력채용",
            "복리후생관리",
            "02",
            "인력채용",
            "복리후생관리",
            '["복리후생관리","인사기획","임금관리"]',
        ),
        (
            "compensation_to_hr_planning",
            "임금관리",
            "인사기획",
            "02",
            "임금관리",
            "인사기획",
            '["인사기획","직무관리","인력채용"]',
        ),
        (
            "compensation_to_labor_management",
            "임금관리",
            "노무관리",
            "02",
            "임금관리",
            "노무관리",
            '["노사관계 계획","단체교섭준비","단체교섭"]',
        ),
        (
            "compensation_to_benefits",
            "임금관리",
            "복리후생관리",
            "02",
            "임금관리",
            "복리후생관리",
            '["복리후생관리","임금관리","급여지급"]',
        ),
        (
            "benefits_to_compensation",
            "복리후생관리",
            "임금관리",
            "02",
            "복리후생관리",
            "임금관리",
            '["임금관리","급여지급","복리후생관리"]',
        ),
        (
            "benefits_to_hr_planning",
            "복리후생관리",
            "인사기획",
            "02",
            "복리후생관리",
            "인사기획",
            '["인사기획","직무관리","임금관리"]',
        ),
        (
            "benefits_to_labor_management",
            "복리후생관리",
            "노무관리",
            "02",
            "복리후생관리",
            "노무관리",
            '["노사관계 계획","단체교섭준비","노사협의회 운영"]',
        ),
        (
            "hrd_to_hr_planning",
            "교육훈련운영",
            "인사기획",
            "02",
            "교육훈련운영",
            "인사기획",
            '["인사기획","직무관리","인력채용"]',
        ),
        (
            "hrd_to_recruiting",
            "교육훈련운영",
            "인력채용",
            "02",
            "교육훈련운영",
            "인력채용",
            '["인력채용","인사기획","직무관리"]',
        ),
        (
            "hrd_to_compensation",
            "교육훈련운영",
            "임금관리",
            "02",
            "교육훈련운영",
            "임금관리",
            '["임금관리","급여지급","인사기획"]',
        ),
        (
            "hr_planning_to_recruiting",
            "인사기획",
            "인력채용",
            "02",
            "인사기획",
            "인력채용",
            '["인력채용","인사기획","직무관리"]',
        ),
        (
            "hr_planning_to_compensation",
            "인사기획",
            "임금관리",
            "02",
            "인사기획",
            "임금관리",
            '["임금관리","급여지급","인사기획"]',
        ),
        (
            "hr_planning_to_hrd",
            "인사기획",
            "교육훈련운영",
            "02",
            "인사기획",
            "교육훈련운영",
            '["교육훈련운영","핵심인재관리","직무관리"]',
        ),
        (
            "hr_planning_to_labor_management",
            "인사기획",
            "노무관리",
            "02",
            "인사기획",
            "노무관리",
            '["노사관계 계획","단체교섭준비","노사협의회 운영"]',
        ),
        (
            "hrbp_to_hr_planning",
            "HRBP",
            "인사기획",
            "02",
            "인사기획",
            "인사기획",
            '["인사기획","직무관리","인력채용"]',
        ),
        (
            "hrbp_to_labor_management",
            "HRBP",
            "노무관리",
            "02",
            "인사기획",
            "노무관리",
            '["노사관계 계획","단체교섭준비","단체교섭"]',
        ),
    ]
    for scenario in scenarios:
        conn.execute(
            """
            INSERT INTO training_transition_gold_scenarios(
                scenario_name, current_query, target_query, major_code,
                expected_current_match_text, expected_target_match_text,
                expected_course_names_json, review_status, created_at, updated_at
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?
            WHERE NOT EXISTS (
                SELECT 1
                FROM training_transition_gold_scenarios
                WHERE scenario_name = ?
            )
            """,
            (*scenario, timestamp, timestamp, scenario[0]),
        )
        conn.execute(
            """
            UPDATE training_transition_gold_scenarios
            SET
                current_query = ?,
                target_query = ?,
                major_code = ?,
                expected_current_match_text = ?,
                expected_target_match_text = ?,
                expected_course_names_json = ?,
                updated_at = ?
            WHERE scenario_name = ?
              AND review_status = 'candidate'
            """,
            (
                scenario[1],
                scenario[2],
                scenario[3],
                scenario[4],
                scenario[5],
                scenario[6],
                timestamp,
                scenario[0],
            ),
        )


def initialize_database(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    migrate_unit_standard_training_unique_constraint(conn)
    normalize_supplemental_source_files(conn)
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
    ensure_column(conn, "ncs_reference_documents", "source_path", "TEXT")
    ensure_column(conn, "ncs_reference_documents", "source_hash", "TEXT")
    ensure_column(
        conn,
        "ncs_reference_documents",
        "source_type",
        "TEXT NOT NULL DEFAULT 'html'",
    )
    ensure_column(
        conn,
        "ncs_reference_documents",
        "import_status",
        "TEXT NOT NULL DEFAULT 'imported'",
    )
    ensure_column(
        conn,
        "ncs_reference_documents",
        "page_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(
        conn,
        "ncs_reference_documents",
        "chunk_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(conn, "ncs_reference_documents", "metadata_json", "TEXT")
    ensure_column(conn, "ncs_reference_documents", "updated_at", "TEXT")
    ensure_column(conn, "ncs_reference_pages", "width", "REAL")
    ensure_column(conn, "ncs_reference_pages", "height", "REAL")
    ensure_column(
        conn,
        "ncs_reference_pages",
        "text_nodes_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    ensure_column(conn, "ncs_reference_chunks", "summary", "TEXT NOT NULL DEFAULT ''")
    ensure_column(
        conn,
        "ncs_reference_chunks",
        "location_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    ensure_column(conn, "ncs_reference_entities", "metadata_json", "TEXT")
    ensure_column(
        conn,
        "ncs_reference_entities",
        "review_status",
        "TEXT NOT NULL DEFAULT 'candidate'",
    )
    ensure_column(
        conn,
        "ncs_reference_entity_links",
        "review_status",
        "TEXT NOT NULL DEFAULT 'candidate'",
    )
    ensure_column(conn, "refinement_jobs", "source_issue_id", "INTEGER")
    ensure_column(conn, "refinement_jobs", "raw_text", "TEXT")
    ensure_column(conn, "refinement_jobs", "refined_text", "TEXT")
    ensure_column(conn, "refinement_jobs", "rationale", "TEXT")
    ensure_column(conn, "refinement_jobs", "confidence", "REAL")
    ensure_column(conn, "refinement_jobs", "applied_at", "TEXT")
    ensure_column(conn, "ncs_qualification_collection_status", "last_error_type", "TEXT")
    ensure_column(
        conn,
        "ncs_qualification_collection_status",
        "attempt_count",
        "INTEGER NOT NULL DEFAULT 0",
    )
    ensure_column(conn, "ncs_qualification_collection_status", "next_retry_at", "TEXT")
    conn.execute(
        """
        UPDATE sqf_ncs_matches
        SET scope_tag = 'management_support_hr_mvp'
        WHERE source_id IN (
            SELECT source_key
            FROM sqf_duties
            WHERE ncs_lclas_cd = '02'
              AND sqf_field_name = '경영관리'
              AND job_name IN ('경영지원', '인사')
        )
          AND (
              scope_tag IS NULL
              OR TRIM(scope_tag) = ''
              OR scope_tag IN ('management_support', 'business_accounting_office_02')
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
                     AND sd.job_name IN ('경영지원', '인사')
                    THEN 'management_support_hr_mvp'
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
        ("schema_version", "0.9.0"),
    )
    _seed_default_query_aliases(conn)
    _seed_default_transition_gold_scenarios(conn)
    conn.commit()


def migrate_unit_standard_training_unique_constraint(conn: sqlite3.Connection) -> None:
    table = "ncs_unit_standard_training"
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if not table_exists:
        return
    needs_rebuild = False
    for index_row in conn.execute(f"PRAGMA index_list({table})").fetchall():
        index_name = index_row["name"]
        if not index_row["unique"]:
            continue
        columns = [row["name"] for row in conn.execute(f"PRAGMA index_info({index_name})").fetchall()]
        if columns == ["unit_code_raw"]:
            needs_rebuild = True
            break
    if not needs_rebuild:
        return
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ncs_unit_standard_training_new (
            unit_standard_id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_file TEXT NOT NULL,
            source_row_number INTEGER NOT NULL,
            unit_code_raw TEXT NOT NULL,
            unit_name TEXT NOT NULL,
            unit_level TEXT,
            standard_training_hours REAL,
            matched_unit_code TEXT REFERENCES competency_units(unit_code),
            match_status TEXT NOT NULL DEFAULT 'unmatched',
            source_payload TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (source_file, source_row_number)
        );
        INSERT OR IGNORE INTO ncs_unit_standard_training_new(
            unit_standard_id, source_file, source_row_number, unit_code_raw,
            unit_name, unit_level, standard_training_hours, matched_unit_code,
            match_status, source_payload, created_at, updated_at
        )
        SELECT
            unit_standard_id, source_file, source_row_number, unit_code_raw,
            unit_name, unit_level, standard_training_hours, matched_unit_code,
            match_status, source_payload, created_at, updated_at
        FROM ncs_unit_standard_training;
        DROP TABLE ncs_unit_standard_training;
        ALTER TABLE ncs_unit_standard_training_new RENAME TO ncs_unit_standard_training;
        """
    )


def normalize_supplemental_source_files(conn: sqlite3.Connection) -> None:
    tables = (
        "ncs_unit_standard_training",
        "ncs_occupation_code_mappings",
        "ncs_external_training_zip_courses",
    )
    for table in tables:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if not table_exists:
            continue
        rows = conn.execute(f"SELECT DISTINCT source_file FROM {table}").fetchall()
        for row in rows:
            source_file = row["source_file"]
            try:
                normalized = str(Path(source_file).expanduser().resolve()).casefold()
            except OSError:
                normalized = str(Path(source_file).expanduser().absolute()).casefold()
            if normalized != source_file:
                conn.execute(
                    f"UPDATE {table} SET source_file = ? WHERE source_file = ?",
                    (normalized, source_file),
                )


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


def normalize_concept_key(value: str) -> str:
    return re.sub(r"\s+", "", normalize_spaces(value)).lower()


def concept_type_from_ksa(ksa_type_name: str) -> str:
    mapping = {
        "지식": "knowledge",
        "기술": "skill",
        "태도": "attitude",
    }
    return mapping.get(ksa_type_name.strip(), "knowledge")


def _meaning_role_for_concept_type(concept_type: str) -> str:
    return {
        "knowledge": "task_knowledge_significance",
        "skill": "task_skill_significance",
        "attitude": "task_attitude_significance",
    }.get(concept_type, "task_ksa_significance")


def _meaning_text_for_context(row: sqlite3.Row) -> str:
    concept_name = normalize_spaces(row["concept_name"])
    unit_name = normalize_spaces(row["unit_name_raw"] or "")
    element_name = normalize_spaces(row["element_name_raw"] or "")
    criteria_text = normalize_spaces(row["criteria_text_raw"] or "")
    if row["concept_type"] == "knowledge":
        role = "과업의 판단, 분석, 의사결정에 필요한 지식 근거"
    elif row["concept_type"] == "skill":
        role = "과업을 실제로 수행, 분석, 작성, 운영하는 데 적용되는 실행 기술"
    elif row["concept_type"] == "attitude":
        role = "과업 수행의 품질과 일관성을 뒷받침하는 업무 태도"
    else:
        role = "과업 수행에 필요한 KSA 요소"
    context = f"{unit_name} / {element_name}" if element_name else unit_name
    if not context:
        return f"{concept_name}은(는) NCS KSA 전처리에서 도출된 {role} 후보이며, 원천 과업 연결은 추가 검토가 필요하다."
    if criteria_text:
        return f"{concept_name}은(는) {context}에서 '{criteria_text}'를 수행하기 위한 {role}이다."
    return f"{concept_name}은(는) {context} 과업을 수행하기 위한 {role}이다."


def build_ksa_meaning_candidates(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    reset: bool = False,
    limit: int | None = None,
    apply_to_definitions: bool = False,
) -> dict[str, Any]:
    """Build reviewable KSA significance candidates without filling concept definitions."""
    timestamp = now_utc()
    params: list[Any] = []
    scope_clause = ""
    if major_code:
        scope_clause = "AND c.major_code = ?"
        params.append(major_code)
    if reset:
        if major_code:
            conn.execute(
                """
                DELETE FROM ksa_meaning_candidates
                WHERE review_status != 'human_reviewed'
                  AND unit_code IN (
                      SELECT cu.unit_code
                      FROM competency_units cu
                      JOIN classifications c ON c.classification_id = cu.classification_id
                      WHERE c.major_code = ?
                  )
                """,
                (major_code,),
            )
        else:
            conn.execute("DELETE FROM ksa_meaning_candidates WHERE review_status != 'human_reviewed'")

    before = int(conn.execute("SELECT COUNT(*) FROM ksa_meaning_candidates").fetchone()[0])
    limit_clause = "LIMIT ?" if limit is not None else ""
    query_params = [*params, *params, major_code]
    if limit is not None:
        query_params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        WITH raw_context AS (
            SELECT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                ki.ksa_id,
                ki.ksa_text_raw,
                NULL AS atom_text,
                ce.element_id,
                ce.element_name_raw,
                cu.unit_code,
                cu.unit_name_raw,
                pc.criteria_id,
                pc.criteria_text_raw,
                0 AS source_rank
            FROM ontology_concepts oc
            JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
            JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
            JOIN competency_elements ce ON ce.element_id = ki.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN element_criteria_ksa_links eck ON eck.ksa_id = ki.ksa_id
            LEFT JOIN performance_criteria pc ON pc.criteria_id = eck.criteria_id
            WHERE oc.concept_type IN ('knowledge', 'skill', 'attitude')
              AND TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
              {scope_clause}
        ),
        atomic_context AS (
            SELECT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                ki.ksa_id,
                ki.ksa_text_raw,
                atom.atom_text,
                ce.element_id,
                ce.element_name_raw,
                cu.unit_code,
                cu.unit_name_raw,
                pc.criteria_id,
                pc.criteria_text_raw,
                1 AS source_rank
            FROM ontology_concepts oc
            JOIN ksa_atomic_concept_links acl ON acl.concept_id = oc.concept_id
            JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
            JOIN ksa_items ki ON ki.ksa_id = atom.ksa_id
            JOIN competency_elements ce ON ce.element_id = atom.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            LEFT JOIN element_criteria_ksa_links eck ON eck.ksa_id = ki.ksa_id
            LEFT JOIN performance_criteria pc ON pc.criteria_id = eck.criteria_id
            WHERE oc.concept_type IN ('knowledge', 'skill', 'attitude')
              AND TRIM(COALESCE(atom.atom_text, '')) <> ''
              {scope_clause}
        ),
        unlinked_context AS (
            SELECT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                NULL AS ksa_id,
                oc.concept_name AS ksa_text_raw,
                NULL AS atom_text,
                NULL AS element_id,
                NULL AS element_name_raw,
                NULL AS unit_code,
                NULL AS unit_name_raw,
                NULL AS criteria_id,
                NULL AS criteria_text_raw,
                2 AS source_rank
            FROM ontology_concepts oc
            LEFT JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
            LEFT JOIN ksa_atomic_concept_links acl ON acl.concept_id = oc.concept_id
            WHERE oc.concept_type IN ('knowledge', 'skill', 'attitude')
              AND TRIM(COALESCE(oc.concept_name, '')) <> ''
              AND kcl.link_id IS NULL
              AND acl.link_id IS NULL
              AND ? IS NULL
        ),
        concept_context AS (
            SELECT
                concept_id,
                concept_name,
                concept_type,
                ksa_id,
                COALESCE(NULLIF(TRIM(atom_text), ''), ksa_text_raw) AS ksa_text_raw,
                element_id,
                element_name_raw,
                unit_code,
                unit_name_raw,
                criteria_id,
                criteria_text_raw,
                ROW_NUMBER() OVER (
                    PARTITION BY concept_id
                    ORDER BY
                        source_rank,
                        CASE WHEN criteria_id IS NULL THEN 1 ELSE 0 END,
                        unit_code,
                        element_id,
                        ksa_id
                ) AS rn
            FROM (
                SELECT * FROM raw_context
                UNION ALL
                SELECT * FROM atomic_context
                UNION ALL
                SELECT * FROM unlinked_context
            )
        )
        SELECT *
        FROM concept_context
        WHERE rn = 1
        ORDER BY concept_id
        {limit_clause}
        """,
        query_params,
    ).fetchall()

    processed = 0
    for row in rows:
        meaning_role = _meaning_role_for_concept_type(row["concept_type"])
        evidence_parts = [
            f"능력단위: {normalize_spaces(row['unit_name_raw'] or '')}",
            f"능력단위요소: {normalize_spaces(row['element_name_raw'] or '')}",
            f"수행준거: {normalize_spaces(row['criteria_text_raw'] or '')}",
            f"KSA 원문: {normalize_spaces(row['ksa_text_raw'] or '')}",
        ]
        evidence_text = " | ".join(part for part in evidence_parts if not part.endswith(": "))
        confidence = 0.72 if row["criteria_id"] is not None else 0.45 if row["unit_code"] is None else 0.6
        source_method = "task_context_template" if row["unit_code"] is not None else "unlinked_concept_fallback"
        conn.execute(
            """
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
            ON CONFLICT(concept_id, meaning_role, source_method)
            DO UPDATE SET
                concept_type = excluded.concept_type,
                meaning_text = excluded.meaning_text,
                evidence_text = excluded.evidence_text,
                unit_code = excluded.unit_code,
                element_id = excluded.element_id,
                criteria_id = excluded.criteria_id,
                ksa_id = excluded.ksa_id,
                confidence_score = excluded.confidence_score,
                updated_at = excluded.updated_at
            WHERE ksa_meaning_candidates.review_status != 'human_reviewed'
            """,
            (
                row["concept_id"],
                row["concept_type"],
                meaning_role,
                _meaning_text_for_context(row),
                source_method,
                evidence_text,
                row["unit_code"],
                row["element_id"],
                row["criteria_id"],
                row["ksa_id"],
                confidence,
                timestamp,
                timestamp,
            ),
        )
        processed += 1
    definitions_updated = 0
    if apply_to_definitions:
        definition_scope = ""
        definition_params: list[Any] = []
        if major_code:
            definition_scope = """
              AND kmc.unit_code IN (
                  SELECT cu.unit_code
                  FROM competency_units cu
                  JOIN classifications c ON c.classification_id = cu.classification_id
                  WHERE c.major_code = ?
              )
            """
            definition_params.append(major_code)
        cur = conn.execute(
            f"""
            UPDATE ontology_concepts
            SET
                definition = (
                    SELECT kmc.meaning_text
                    FROM ksa_meaning_candidates kmc
                    WHERE kmc.concept_id = ontology_concepts.concept_id
                      {definition_scope}
                    ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                    LIMIT 1
                ),
                definition_source = 'ksa_meaning_candidates.task_context_template',
                definition_status = 'candidate',
                review_status = 'model_preprocessed',
                updated_at = ?
            WHERE concept_id IN (
                SELECT kmc.concept_id
                FROM ksa_meaning_candidates kmc
                WHERE kmc.review_status != 'rejected'
                  {definition_scope}
            )
              AND review_status != 'human_reviewed'
            """,
            (*definition_params, timestamp, *definition_params),
        )
        definitions_updated = int(cur.rowcount if cur.rowcount is not None else 0)
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ksa_meaning_candidates_built_at", timestamp),
    )
    conn.commit()
    after = int(conn.execute("SELECT COUNT(*) FROM ksa_meaning_candidates").fetchone()[0])
    by_type = {
        row["concept_type"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT concept_type, COUNT(*) AS count
            FROM ksa_meaning_candidates
            GROUP BY concept_type
            ORDER BY concept_type
            """
        ).fetchall()
    }
    human_reviewed = int(
        conn.execute(
            "SELECT COUNT(*) FROM ksa_meaning_candidates WHERE review_status = 'human_reviewed'"
        ).fetchone()[0]
    )
    return {
        "major_code": major_code,
        "meanings_before": before,
        "meanings_after": after,
        "meanings_inserted": max(0, after - before),
        "meaning_contexts_processed": processed,
        "definitions_updated": definitions_updated,
        "definitions_applied": apply_to_definitions,
        "meanings_by_type": by_type,
        "human_reviewed_preserved": human_reviewed,
        "note": (
            "These are reviewable task-context significance candidates. "
            "When applied to ontology_concepts.definition they are marked as candidate/model_preprocessed."
        ),
    }


def _sql_normalized_key(expression: str) -> str:
    normalized = f"TRIM({expression})"
    for codepoint in (32, 9, 10, 11, 12, 13, 160, 8194, 8195, 8201, 8202, 8239, 8287, 12288):
        normalized = f"REPLACE({normalized}, CHAR({codepoint}), '')"
    return f"LOWER({normalized})"


def ensure_ontology_seeded(conn: sqlite3.Connection) -> dict[str, int]:
    """Create initial ontology concept nodes from raw KSA items if needed."""
    ksa_count = int(conn.execute("SELECT COUNT(*) FROM ksa_items").fetchone()[0])
    if ksa_count == 0:
        return {"concepts": 0, "aliases": 0, "ksa_links": 0, "criteria_links": 0}

    non_empty_ksa_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM ksa_items WHERE TRIM(COALESCE(ksa_text_raw, '')) <> ''"
        ).fetchone()[0]
    )
    existing_links = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ksa_items ki
            JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
            WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
            """
        ).fetchone()[0]
    )
    criteria_without_links = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM performance_criteria pc
            LEFT JOIN criteria_concept_links ccl ON ccl.criteria_id = pc.criteria_id
            WHERE ccl.link_id IS NULL
            """
        ).fetchone()[0]
    )
    if existing_links >= non_empty_ksa_count and criteria_without_links == 0:
        return {
            "concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
            "aliases": int(conn.execute("SELECT COUNT(*) FROM ontology_concept_aliases").fetchone()[0]),
            "ksa_links": existing_links,
            "criteria_links": int(conn.execute("SELECT COUNT(*) FROM criteria_concept_links").fetchone()[0]),
        }

    timestamp = now_utc()
    rows = conn.execute(
        """
        SELECT DISTINCT ksa_type_name, ksa_text_raw
        FROM ksa_items
        WHERE TRIM(ksa_text_raw) <> ''
        """
    ).fetchall()
    for row in rows:
        concept_name = normalize_spaces(row["ksa_text_raw"])
        concept_type = concept_type_from_ksa(row["ksa_type_name"])
        normalized_key = normalize_concept_key(concept_name)
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concepts(
                concept_name, normalized_key, concept_type,
                definition_status, relation_status, review_status,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
            """,
            (concept_name, normalized_key, concept_type, timestamp, timestamp),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO ontology_concept_aliases(
            concept_id, alias_text, normalized_alias_key, alias_source, created_at
        )
        SELECT DISTINCT
            oc.concept_id,
            TRIM(ki.ksa_text_raw),
            LOWER(REPLACE(TRIM(ki.ksa_text_raw), ' ', '')),
            'raw_ksa',
            ?
        FROM ksa_items ki
        JOIN ontology_concepts oc
          ON oc.concept_type = CASE ki.ksa_type_name
              WHEN '지식' THEN 'knowledge'
              WHEN '기술' THEN 'skill'
              WHEN '태도' THEN 'attitude'
              ELSE 'knowledge'
          END
         AND oc.normalized_key = """ + _sql_normalized_key("ki.ksa_text_raw") + """
        WHERE TRIM(ki.ksa_text_raw) <> ''
        """,
        (timestamp,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
        SELECT
            ki.ksa_id,
            oc.concept_id,
            'raw',
            ?
        FROM ksa_items ki
        JOIN ontology_concepts oc
          ON oc.concept_type = CASE ki.ksa_type_name
              WHEN '지식' THEN 'knowledge'
              WHEN '기술' THEN 'skill'
              WHEN '태도' THEN 'attitude'
              ELSE 'knowledge'
          END
         AND oc.normalized_key = """ + _sql_normalized_key("ki.ksa_text_raw") + """
        WHERE TRIM(ki.ksa_text_raw) <> ''
        """,
        (timestamp,),
    )
    missing_rows = conn.execute(
        """
        SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw
        FROM ksa_items ki
        LEFT JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
        WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
          AND kcl.link_id IS NULL
        """
    ).fetchall()
    for row in missing_rows:
        concept_type = concept_type_from_ksa(row["ksa_type_name"])
        concept_name = normalize_spaces(row["ksa_text_raw"])
        normalized_key = normalize_concept_key(concept_name)
        concept = conn.execute(
            """
            SELECT concept_id
            FROM ontology_concepts
            WHERE concept_type = ?
              AND normalized_key = ?
            """,
            (concept_type, normalized_key),
        ).fetchone()
        if concept is None:
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
                """,
                (concept_name, normalized_key, concept_type, timestamp, timestamp),
            )
            concept_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        else:
            concept_id = int(concept["concept_id"])
        conn.execute(
            """
            INSERT OR IGNORE INTO ontology_concept_aliases(
                concept_id, alias_text, normalized_alias_key, alias_source, created_at
            ) VALUES (?, ?, ?, 'raw_ksa', ?)
            """,
            (concept_id, concept_name, normalized_key, timestamp),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
            VALUES (?, ?, 'raw', ?)
            """,
            (row["ksa_id"], concept_id, timestamp),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO criteria_concept_links(
            criteria_id, concept_id, relation_type, link_status, created_at
        )
        SELECT DISTINCT
            eck.criteria_id,
            kcl.concept_id,
            'related',
            'raw',
            ?
        FROM element_criteria_ksa_links eck
        JOIN ksa_concept_links kcl ON kcl.ksa_id = eck.ksa_id
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ontology_seeded_at", timestamp),
    )
    conn.commit()
    return {
        "concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
        "aliases": int(conn.execute("SELECT COUNT(*) FROM ontology_concept_aliases").fetchone()[0]),
        "ksa_links": int(conn.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0]),
        "criteria_links": int(conn.execute("SELECT COUNT(*) FROM criteria_concept_links").fetchone()[0]),
    }


def ensure_ncs_ontology_relations(
    conn: sqlite3.Connection,
    *,
    relations_per_concept: int = 2,
    reset: bool = False,
) -> dict[str, int]:
    """Create bounded concept relation candidates from the NCS element structure.

    This does not invent definitions. It adds candidate relations between concepts
    that are required together inside the same competency element, capped per
    source concept so the materialized graph remains queryable.
    """
    if relations_per_concept < 1:
        raise ValueError("relations_per_concept must be at least 1")

    timestamp = now_utc()
    if reset:
        conn.execute(
            "DELETE FROM ontology_concept_relations WHERE relation_type = 'co_required_in_element'"
        )

    before_count = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_relations").fetchone()[0]
    )
    conn.execute("DROP TABLE IF EXISTS temp.tmp_concept_element")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_concept_element AS
        SELECT DISTINCT
            kcl.concept_id,
            ki.element_id,
            oc.concept_type
        FROM ksa_concept_links kcl
        JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
        JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_concept_element_element ON tmp_concept_element(element_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_concept_element_concept ON tmp_concept_element(concept_id)")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_relation_candidates")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_relation_candidates AS
        SELECT source_concept_id, target_concept_id
        FROM (
            SELECT
                ce.concept_id AS source_concept_id,
                peer.concept_id AS target_concept_id,
                ROW_NUMBER() OVER (
                    PARTITION BY ce.concept_id
                    ORDER BY
                        CASE WHEN peer.concept_type <> ce.concept_type THEN 0 ELSE 1 END,
                        ce.element_id,
                        peer.concept_id
                ) AS relation_rank
            FROM tmp_concept_element ce
            JOIN tmp_concept_element peer
              ON peer.element_id = ce.element_id
             AND peer.concept_id <> ce.concept_id
        )
        WHERE relation_rank <= ?
        """,
        (relations_per_concept,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ontology_concept_relations(
            source_concept_id, relation_type, target_concept_id,
            relation_label, review_status, created_at
        )
        SELECT DISTINCT
            source_concept_id,
            'co_required_in_element',
            target_concept_id,
            'Required together in the same NCS competency element.',
            'candidate',
            ?
        FROM tmp_relation_candidates
        WHERE target_concept_id IS NOT NULL
        """,
        (timestamp,),
    )
    conn.execute(
        """
        UPDATE ontology_concepts
        SET relation_status = 'linked',
            updated_at = ?
        WHERE concept_id IN (
            SELECT source_concept_id FROM ontology_concept_relations
            UNION
            SELECT target_concept_id FROM ontology_concept_relations
        )
        """,
        (timestamp,),
    )
    conn.execute(
        """
        UPDATE ontology_concepts
        SET relation_status = 'unlinked',
            updated_at = ?
        WHERE concept_id NOT IN (
            SELECT source_concept_id FROM ontology_concept_relations
            UNION
            SELECT target_concept_id FROM ontology_concept_relations
        )
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ncs_ontology_relations_seeded_at", timestamp),
    )
    conn.commit()
    after_count = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_relations").fetchone()[0]
    )
    linked_concepts = int(
        conn.execute(
            "SELECT COUNT(*) FROM ontology_concepts WHERE relation_status = 'linked'"
        ).fetchone()[0]
    )
    return {
        "relations_before": before_count,
        "relations_after": after_count,
        "relations_inserted": max(0, after_count - before_count),
        "linked_concepts": linked_concepts,
        "unlinked_concepts": int(
            conn.execute(
                "SELECT COUNT(*) FROM ontology_concepts WHERE relation_status = 'unlinked'"
            ).fetchone()[0]
        ),
    }


_KSA_BULLET_RE = re.compile(r"(?:^|\n)\s*(?:[-ㆍ•·*]|\d+[.)]|[가-하][.)])\s*")


def split_ksa_atomic_text(value: str) -> list[str]:
    """Split a KSA source string into atomic candidate terms.

    The source string itself remains unchanged in ksa_items. This function only
    creates preprocessing candidates for ontology work.
    """
    text = normalize_spaces(value.replace("\u00a0", " ").replace("\u202f", " ").replace("\u3000", " "))
    if not text:
        return []
    raw_parts: list[str] = []
    if "\n" in value or re.search(r"\s[-ㆍ•·*]\s*", value):
        normalized_lines = value.replace("\u00a0", " ").replace("\u202f", " ").replace("\u3000", " ")
        for line in normalized_lines.splitlines():
            line = normalize_spaces(line)
            if not line:
                continue
            pieces = _KSA_BULLET_RE.split("\n" + line)
            for piece in pieces:
                piece = normalize_spaces(piece)
                if piece:
                    raw_parts.append(piece)
    if not raw_parts:
        raw_parts = [text]

    atoms: list[str] = []
    seen: set[str] = set()
    for part in raw_parts:
        cleaned = normalize_spaces(
            re.sub(r"^\s*(?:[-ㆍ•·*]|\d+[.)]|[가-하][.)])\s*", "", part).strip(" -ㆍ•·*;:,")
        )
        if not cleaned:
            continue
        key = normalize_concept_key(cleaned)
        if not key or key in seen:
            continue
        atoms.append(cleaned)
        seen.add(key)
    return atoms or [text]


def preprocess_ksa_atomic_items(
    conn: sqlite3.Connection,
    *,
    reset: bool = False,
    batch_size: int = 5000,
) -> dict[str, int]:
    if reset:
        conn.execute("DELETE FROM task_similarity_links")
        conn.execute("DELETE FROM task_ksa_concept_relations")
        conn.execute("DELETE FROM ksa_atomic_concept_links")
        conn.execute("DELETE FROM ksa_atomic_items")
        conn.commit()

    timestamp = now_utc()
    rows = conn.execute(
        """
        SELECT ki.ksa_id, ki.element_id, ki.ksa_type_name, ki.ksa_text_raw
        FROM ksa_items ki
        LEFT JOIN ksa_atomic_items atom ON atom.ksa_id = ki.ksa_id
        WHERE TRIM(COALESCE(ki.ksa_text_raw, '')) <> ''
          AND atom.atomic_id IS NULL
        ORDER BY ki.ksa_id
        """
    ).fetchall()
    atom_rows: list[tuple[Any, ...]] = []
    processed = 0
    atoms_generated = 0
    for row in rows:
        atoms = split_ksa_atomic_text(row["ksa_text_raw"])
        for index, atom in enumerate(atoms, start=1):
            atom_rows.append(
                (
                    row["ksa_id"],
                    row["element_id"],
                    row["ksa_type_name"],
                    index,
                    atom,
                    normalize_concept_key(atom),
                    "rule_based",
                    "raw",
                    timestamp,
                )
            )
        processed += 1
        atoms_generated += len(atoms)
        if len(atom_rows) >= batch_size:
            conn.executemany(
                """
                INSERT OR IGNORE INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                atom_rows,
            )
            conn.commit()
            atom_rows.clear()
    if atom_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO ksa_atomic_items(
                ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                normalized_key, split_method, review_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            atom_rows,
        )
        conn.commit()

    concept_rows = conn.execute(
        """
        SELECT DISTINCT atom.ksa_type_name, atom.atom_text, atom.normalized_key
        FROM ksa_atomic_items atom
        LEFT JOIN ontology_concepts oc
          ON oc.concept_type = CASE atom.ksa_type_name
              WHEN '지식' THEN 'knowledge'
              WHEN '기술' THEN 'skill'
              WHEN '태도' THEN 'attitude'
              ELSE 'knowledge'
          END
         AND oc.normalized_key = atom.normalized_key
        WHERE oc.concept_id IS NULL
        """
    ).fetchall()
    conn.executemany(
        """
        INSERT OR IGNORE INTO ontology_concepts(
            concept_name, normalized_key, concept_type,
            definition_status, relation_status, review_status, created_at, updated_at
        ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
        """,
        [
            (
                row["atom_text"],
                row["normalized_key"],
                concept_type_from_ksa(row["ksa_type_name"]),
                timestamp,
                timestamp,
            )
            for row in concept_rows
        ],
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ontology_concept_aliases(
            concept_id, alias_text, normalized_alias_key, alias_source, created_at
        )
        SELECT DISTINCT
            oc.concept_id,
            atom.atom_text,
            atom.normalized_key,
            'atomic_ksa',
            ?
        FROM ksa_atomic_items atom
        JOIN ontology_concepts oc
          ON oc.concept_type = CASE atom.ksa_type_name
              WHEN '지식' THEN 'knowledge'
              WHEN '기술' THEN 'skill'
              WHEN '태도' THEN 'attitude'
              ELSE 'knowledge'
          END
         AND oc.normalized_key = atom.normalized_key
        """,
        (timestamp,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ksa_atomic_concept_links(
            atomic_id, concept_id, link_status, created_at
        )
        SELECT
            atom.atomic_id,
            oc.concept_id,
            'raw',
            ?
        FROM ksa_atomic_items atom
        JOIN ontology_concepts oc
          ON oc.concept_type = CASE atom.ksa_type_name
              WHEN '지식' THEN 'knowledge'
              WHEN '기술' THEN 'skill'
              WHEN '태도' THEN 'attitude'
              ELSE 'knowledge'
          END
         AND oc.normalized_key = atom.normalized_key
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ksa_atomic_preprocessed_at", timestamp),
    )
    conn.commit()
    return {
        "ksa_processed": processed,
        "atoms_generated_in_run": atoms_generated,
        "atomic_items": int(conn.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0]),
        "atomic_concept_links": int(
            conn.execute("SELECT COUNT(*) FROM ksa_atomic_concept_links").fetchone()[0]
        ),
        "ontology_concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
    }


def build_task_ksa_concept_relations(
    conn: sqlite3.Connection,
    *,
    reset: bool = False,
) -> dict[str, int]:
    timestamp = now_utc()
    if reset:
        conn.execute("DELETE FROM task_ksa_concept_relations")
        conn.execute(
            """
            DELETE FROM ontology_concept_relations
            WHERE relation_type IN (
                'knowledge_enables_skill',
                'attitude_supports_skill',
                'knowledge_informs_attitude'
            )
            """
        )
        conn.commit()

    before_task_relations = int(
        conn.execute("SELECT COUNT(*) FROM task_ksa_concept_relations").fetchone()[0]
    )
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_task_atoms")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_task_atoms AS
        SELECT DISTINCT
            eck.criteria_id,
            eck.element_id,
            atom.atomic_id,
            acl.concept_id,
            atom.ksa_type_name
        FROM element_criteria_ksa_links eck
        JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
        JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_atoms_criteria ON tmp_task_atoms(criteria_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_atoms_type ON tmp_task_atoms(ksa_type_name)")

    relation_specs = [
        ("지식", "기술", "knowledge_enables_skill", 0.62),
        ("태도", "기술", "attitude_supports_skill", 0.58),
        ("지식", "태도", "knowledge_informs_attitude", 0.54),
    ]
    inserted_by_type: dict[str, int] = {}
    for source_type, target_type, relation_type, confidence in relation_specs:
        before = int(
            conn.execute(
                "SELECT COUNT(*) FROM task_ksa_concept_relations WHERE relation_type = ?",
                (relation_type,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO task_ksa_concept_relations(
                criteria_id, element_id, source_concept_id, relation_type,
                target_concept_id, source_atomic_id, target_atomic_id,
                evidence_text, confidence_score, review_status, created_at
            )
            SELECT
                source.criteria_id,
                source.element_id,
                source.concept_id,
                ?,
                target.concept_id,
                source.atomic_id,
                target.atomic_id,
                pc.criteria_text_raw,
                ?,
                'candidate',
                ?
            FROM tmp_task_atoms source
            JOIN tmp_task_atoms target
              ON target.criteria_id = source.criteria_id
             AND target.element_id = source.element_id
             AND target.concept_id <> source.concept_id
            JOIN performance_criteria pc ON pc.criteria_id = source.criteria_id
            WHERE source.ksa_type_name = ?
              AND target.ksa_type_name = ?
            """,
            (relation_type, confidence, timestamp, source_type, target_type),
        )
        conn.commit()
        after = int(
            conn.execute(
                "SELECT COUNT(*) FROM task_ksa_concept_relations WHERE relation_type = ?",
                (relation_type,),
            ).fetchone()[0]
        )
        inserted_by_type[relation_type] = max(0, after - before)

    before_graph_relations = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_relations").fetchone()[0]
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO ontology_concept_relations(
            source_concept_id, relation_type, target_concept_id,
            relation_label, review_status, created_at
        )
        SELECT DISTINCT
            source_concept_id,
            relation_type,
            target_concept_id,
            CASE relation_type
                WHEN 'knowledge_enables_skill' THEN 'Knowledge enables task skill performance.'
                WHEN 'attitude_supports_skill' THEN 'Attitude supports task skill performance.'
                WHEN 'knowledge_informs_attitude' THEN 'Knowledge informs the task attitude.'
                ELSE 'KSA task relation.'
            END,
            'candidate',
            ?
        FROM task_ksa_concept_relations
        WHERE relation_type IN (
            'knowledge_enables_skill',
            'attitude_supports_skill',
            'knowledge_informs_attitude'
        )
        """,
        (timestamp,),
    )
    conn.execute(
        """
        UPDATE ontology_concepts
        SET relation_status = 'linked',
            updated_at = ?
        WHERE concept_id IN (
            SELECT source_concept_id FROM ontology_concept_relations
            UNION
            SELECT target_concept_id FROM ontology_concept_relations
        )
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("task_ksa_relations_built_at", timestamp),
    )
    conn.commit()
    after_task_relations = int(
        conn.execute("SELECT COUNT(*) FROM task_ksa_concept_relations").fetchone()[0]
    )
    after_graph_relations = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_relations").fetchone()[0]
    )
    return {
        "task_relations_before": before_task_relations,
        "task_relations_after": after_task_relations,
        "task_relations_inserted": max(0, after_task_relations - before_task_relations),
        "inserted_by_type": inserted_by_type,
        "ontology_relations_before": before_graph_relations,
        "ontology_relations_after": after_graph_relations,
        "ontology_relations_inserted": max(0, after_graph_relations - before_graph_relations),
    }


def build_task_similarity_links(
    conn: sqlite3.Connection,
    *,
    max_links_per_task: int = 10,
    min_shared_concepts: int = 2,
    max_concept_task_frequency: int = 120,
    reset: bool = False,
) -> dict[str, int]:
    """Build bounded task-to-task links for upskilling/reskilling recommendations."""
    if max_links_per_task < 1:
        raise ValueError("max_links_per_task must be at least 1")
    if min_shared_concepts < 1:
        raise ValueError("min_shared_concepts must be at least 1")
    if max_concept_task_frequency < 2:
        raise ValueError("max_concept_task_frequency must be at least 2")

    timestamp = now_utc()
    if reset:
        conn.execute("DELETE FROM task_similarity_links")
        conn.commit()

    before_count = int(conn.execute("SELECT COUNT(*) FROM task_similarity_links").fetchone()[0])
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("DROP TABLE IF EXISTS temp.tmp_task_concepts")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_task_concepts AS
        SELECT DISTINCT
            eck.criteria_id,
            eck.element_id,
            ce.unit_code,
            cu.classification_id,
            acl.concept_id
        FROM element_criteria_ksa_links eck
        JOIN competency_elements ce ON ce.element_id = eck.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
        JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_concepts_concept ON tmp_task_concepts(concept_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_concepts_criteria ON tmp_task_concepts(criteria_id)")

    conn.execute("DROP TABLE IF EXISTS temp.tmp_task_counts")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_task_counts AS
        SELECT criteria_id, COUNT(*) AS concept_count
        FROM tmp_task_concepts
        GROUP BY criteria_id
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_counts_criteria ON tmp_task_counts(criteria_id)")

    conn.execute("DROP TABLE IF EXISTS temp.tmp_concept_frequency")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_concept_frequency AS
        SELECT concept_id, COUNT(DISTINCT criteria_id) AS task_frequency
        FROM tmp_task_concepts
        GROUP BY concept_id
        HAVING task_frequency BETWEEN 2 AND ?
        """,
        (max_concept_task_frequency,),
    )
    conn.execute("CREATE INDEX IF NOT EXISTS temp.idx_tmp_concept_frequency_concept ON tmp_concept_frequency(concept_id)")

    conn.execute("DROP TABLE IF EXISTS temp.tmp_task_pair_candidates")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_task_pair_candidates AS
        SELECT
            source.criteria_id AS source_criteria_id,
            target.criteria_id AS target_criteria_id,
            source.element_id AS source_element_id,
            target.element_id AS target_element_id,
            source.unit_code AS source_unit_code,
            target.unit_code AS target_unit_code,
            source.classification_id AS source_classification_id,
            target.classification_id AS target_classification_id,
            COUNT(*) AS shared_concept_count
        FROM tmp_task_concepts source
        JOIN tmp_concept_frequency freq ON freq.concept_id = source.concept_id
        JOIN tmp_task_concepts target
          ON target.concept_id = source.concept_id
         AND target.criteria_id <> source.criteria_id
        GROUP BY
            source.criteria_id, target.criteria_id,
            source.element_id, target.element_id,
            source.unit_code, target.unit_code,
            source.classification_id, target.classification_id
        HAVING shared_concept_count >= ?
        """,
        (min_shared_concepts,),
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS temp.idx_tmp_task_pair_candidates_source ON tmp_task_pair_candidates(source_criteria_id)"
    )

    conn.execute("DROP TABLE IF EXISTS temp.tmp_ranked_task_pairs")
    conn.execute(
        """
        CREATE TEMP TABLE tmp_ranked_task_pairs AS
        SELECT *
        FROM (
            SELECT
                pair.*,
                source_count.concept_count AS source_concept_count,
                target_count.concept_count AS target_concept_count,
                CAST(pair.shared_concept_count AS REAL)
                    / (source_count.concept_count + target_count.concept_count - pair.shared_concept_count)
                    AS similarity_score,
                ROW_NUMBER() OVER (
                    PARTITION BY pair.source_criteria_id
                    ORDER BY
                        CAST(pair.shared_concept_count AS REAL)
                            / (source_count.concept_count + target_count.concept_count - pair.shared_concept_count) DESC,
                        pair.shared_concept_count DESC,
                        CASE WHEN pair.source_unit_code = pair.target_unit_code THEN 0 ELSE 1 END,
                        pair.target_criteria_id
                ) AS rank_no
            FROM tmp_task_pair_candidates pair
            JOIN tmp_task_counts source_count ON source_count.criteria_id = pair.source_criteria_id
            JOIN tmp_task_counts target_count ON target_count.criteria_id = pair.target_criteria_id
        )
        WHERE rank_no <= ?
        """,
        (max_links_per_task,),
    )

    conn.execute(
        """
        INSERT OR IGNORE INTO task_similarity_links(
            source_criteria_id, target_criteria_id,
            source_element_id, target_element_id,
            source_unit_code, target_unit_code,
            relation_type, similarity_score, shared_concept_count,
            source_concept_count, target_concept_count,
            source_only_count, target_only_count,
            evidence_json, review_status, created_at
        )
        SELECT
            source_criteria_id,
            target_criteria_id,
            source_element_id,
            target_element_id,
            source_unit_code,
            target_unit_code,
            CASE
                WHEN source_unit_code = target_unit_code THEN 'upskilling_same_unit_task'
                WHEN source_classification_id = target_classification_id THEN 'upskilling_same_classification_task'
                ELSE 'reskilling_transfer_task'
            END,
            similarity_score,
            shared_concept_count,
            source_concept_count,
            target_concept_count,
            source_concept_count - shared_concept_count,
            target_concept_count - shared_concept_count,
            json_object(
                'method', 'atomic_ksa_jaccard',
                'min_shared_concepts', ?,
                'max_concept_task_frequency', ?,
                'max_links_per_task', ?
            ),
            'candidate',
            ?
        FROM tmp_ranked_task_pairs
        """,
        (min_shared_concepts, max_concept_task_frequency, max_links_per_task, timestamp),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("task_similarity_links_built_at", timestamp),
    )
    conn.commit()
    after_count = int(conn.execute("SELECT COUNT(*) FROM task_similarity_links").fetchone()[0])
    by_type = {
        row["relation_type"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT relation_type, COUNT(*) AS count
            FROM task_similarity_links
            GROUP BY relation_type
            """
        ).fetchall()
    }
    covered_tasks = int(
        conn.execute(
            "SELECT COUNT(DISTINCT source_criteria_id) FROM task_similarity_links"
        ).fetchone()[0]
    )
    total_tasks = int(conn.execute("SELECT COUNT(*) FROM performance_criteria").fetchone()[0])
    return {
        "similarity_links_before": before_count,
        "similarity_links_after": after_count,
        "similarity_links_inserted": max(0, after_count - before_count),
        "covered_source_tasks": covered_tasks,
        "total_tasks": total_tasks,
        "coverage": round(covered_tasks / total_tasks, 4) if total_tasks else 0,
        "links_by_type": by_type,
        "parameters": {
            "max_links_per_task": max_links_per_task,
            "min_shared_concepts": min_shared_concepts,
            "max_concept_task_frequency": max_concept_task_frequency,
        },
    }


def _task_concepts(
    conn: sqlite3.Connection,
    criteria_id: int,
    *,
    limit: int = 200,
) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            SELECT DISTINCT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type
            FROM element_criteria_ksa_links eck
            JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
            JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
            JOIN ontology_concepts oc ON oc.concept_id = link.concept_id
            WHERE eck.criteria_id = ?
            ORDER BY oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (criteria_id, limit),
        ).fetchall()
    )


def _shared_task_concepts(
    conn: sqlite3.Connection,
    source_criteria_id: int,
    target_criteria_id: int,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            WITH source_concepts AS (
                SELECT DISTINCT link.concept_id
                FROM element_criteria_ksa_links eck
                JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
                JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                WHERE eck.criteria_id = ?
            ),
            target_concepts AS (
                SELECT DISTINCT link.concept_id
                FROM element_criteria_ksa_links eck
                JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
                JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                WHERE eck.criteria_id = ?
            )
            SELECT oc.concept_id, oc.concept_name, oc.concept_type
            FROM source_concepts source
            JOIN target_concepts target ON target.concept_id = source.concept_id
            JOIN ontology_concepts oc ON oc.concept_id = source.concept_id
            ORDER BY oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (source_criteria_id, target_criteria_id, limit),
        ).fetchall()
    )


def _target_only_task_concepts(
    conn: sqlite3.Connection,
    source_criteria_id: int,
    target_criteria_id: int,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    return rows_to_dicts(
        conn.execute(
            """
            WITH source_concepts AS (
                SELECT DISTINCT link.concept_id
                FROM element_criteria_ksa_links eck
                JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
                JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                WHERE eck.criteria_id = ?
            ),
            target_concepts AS (
                SELECT DISTINCT link.concept_id
                FROM element_criteria_ksa_links eck
                JOIN ksa_atomic_items atom ON atom.ksa_id = eck.ksa_id
                JOIN ksa_atomic_concept_links link ON link.atomic_id = atom.atomic_id
                WHERE eck.criteria_id = ?
            )
            SELECT oc.concept_id, oc.concept_name, oc.concept_type
            FROM target_concepts target
            LEFT JOIN source_concepts source ON source.concept_id = target.concept_id
            JOIN ontology_concepts oc ON oc.concept_id = target.concept_id
            WHERE source.concept_id IS NULL
            ORDER BY oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (source_criteria_id, target_criteria_id, limit),
        ).fetchall()
    )


def resolve_task_criteria(
    conn: sqlite3.Connection,
    *,
    criteria_id: int | None = None,
    query: str | None = None,
    unit_code: str | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, Any] | None:
    clauses: list[str] = []
    params: list[Any] = []
    if criteria_id is not None:
        clauses.append("pc.criteria_id = ?")
        params.append(criteria_id)
    if unit_code:
        clauses.append("ce.unit_code = ?")
        params.append(unit_code)
    if major_code:
        clauses.append("c.major_code = ?")
        params.append(major_code)
    if middle_code:
        clauses.append("c.middle_code = ?")
        params.append(middle_code)
    if small_code:
        clauses.append("c.small_code = ?")
        params.append(small_code)
    if sub_code:
        clauses.append("c.sub_code = ?")
        params.append(sub_code)
    if query:
        clauses.append(
            """
            (
                pc.criteria_text_raw LIKE ?
                OR ce.element_name_raw LIKE ?
                OR cu.unit_name_raw LIKE ?
                OR c.major_name LIKE ?
                OR c.middle_name LIKE ?
                OR c.small_name LIKE ?
                OR c.sub_name LIKE ?
            )
            """
        )
        like = f"%{query}%"
        params.extend([like, like, like, like, like, like, like])
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    hr_query_terms = (
        "노무",
        "노사",
        "근로",
        "노동",
        "임금",
        "복리후생",
        "인사",
        "인력",
    )
    prefer_hr_scope = (
        bool(query)
        and not criteria_id
        and not unit_code
        and not major_code
        and any(term in query for term in hr_query_terms)
    )
    row = conn.execute(
        f"""
        SELECT
            pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
            ce.element_id, ce.element_no, ce.element_name_raw,
            cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
            c.major_code, c.major_name, c.middle_code, c.middle_name,
            c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        {where}
        ORDER BY
            CASE WHEN pc.criteria_id = ? THEN 0 ELSE 1 END,
            CASE WHEN ? = 1 AND c.major_code = '02' THEN 0 ELSE 1 END,
            CASE WHEN ? = 1 AND c.middle_code = '02' THEN 0 ELSE 1 END,
            CASE WHEN ? = 1 AND c.small_code = '02' THEN 0 ELSE 1 END,
            CASE WHEN ? = 1 AND c.sub_name = '노무관리' THEN 0 ELSE 1 END,
            CASE WHEN ? IS NOT NULL AND c.sub_name = ? THEN 0 ELSE 1 END,
            CASE WHEN ? IS NOT NULL AND cu.unit_name_raw = ? THEN 0 ELSE 1 END,
            CASE WHEN ? IS NOT NULL AND ce.element_name_raw = ? THEN 0 ELSE 1 END,
            pc.criteria_id
        LIMIT 1
        """,
        (
            *params,
            criteria_id or -1,
            1 if prefer_hr_scope else 0,
            1 if prefer_hr_scope else 0,
            1 if prefer_hr_scope else 0,
            1 if prefer_hr_scope else 0,
            query,
            query,
            query,
            query,
            query,
            query,
        ),
    ).fetchone()
    return row_to_dict(row)


def recommend_task_transitions(
    conn: sqlite3.Connection,
    *,
    criteria_id: int | None = None,
    query: str | None = None,
    unit_code: str | None = None,
    mode: str = "all",
    limit: int = 10,
    evidence_limit: int = 12,
) -> dict[str, Any]:
    if mode not in {"all", "upskilling", "reskilling"}:
        return {"ok": False, "error": {"code": "unsupported_mode", "mode": mode}}
    query = normalize_spaces(query or "") or None
    unit_code = normalize_spaces(unit_code or "") or None
    if criteria_id is None and not query and not unit_code:
        return {
            "ok": False,
            "error": {
                "code": "missing_task_locator",
                "message": "Provide criteria_id, query, or unit_code to select an NCS task.",
            },
        }
    source = resolve_task_criteria(conn, criteria_id=criteria_id, query=query, unit_code=unit_code)
    if source is None:
        return {"ok": False, "error": {"code": "TASK_NOT_FOUND"}}
    max_rows = clamp_limit(limit, default=10, maximum=50)
    evidence_rows = clamp_limit(evidence_limit, default=12, maximum=50)
    clauses = ["tsl.source_criteria_id = ?"]
    params: list[Any] = [source["criteria_id"]]
    if mode == "upskilling":
        clauses.append("tsl.relation_type LIKE 'upskilling_%'")
    elif mode == "reskilling":
        clauses.append("tsl.relation_type = 'reskilling_transfer_task'")
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT
            tsl.*,
            target_pc.criteria_no AS target_criteria_no,
            target_pc.criteria_text_raw AS target_criteria_text,
            target_ce.element_no AS target_element_no,
            target_ce.element_name_raw AS target_element_name,
            target_cu.unit_name_raw AS target_unit_name,
            target_c.major_code AS target_major_code,
            target_c.major_name AS target_major_name,
            target_c.middle_code AS target_middle_code,
            target_c.middle_name AS target_middle_name,
            target_c.small_code AS target_small_code,
            target_c.small_name AS target_small_name,
            target_c.sub_code AS target_sub_code,
            target_c.sub_name AS target_sub_name
        FROM task_similarity_links tsl
        JOIN performance_criteria target_pc ON target_pc.criteria_id = tsl.target_criteria_id
        JOIN competency_elements target_ce ON target_ce.element_id = tsl.target_element_id
        JOIN competency_units target_cu ON target_cu.unit_code = tsl.target_unit_code
        JOIN classifications target_c ON target_c.classification_id = target_cu.classification_id
        WHERE {where}
        ORDER BY tsl.similarity_score DESC, tsl.shared_concept_count DESC, tsl.target_criteria_id
        LIMIT ?
        """,
        (*params, max_rows),
    ).fetchall()
    recommendations: list[dict[str, Any]] = []
    for rank, row in enumerate(rows, start=1):
        shared = _shared_task_concepts(
            conn,
            source["criteria_id"],
            row["target_criteria_id"],
            limit=evidence_rows,
        )
        gaps = _target_only_task_concepts(
            conn,
            source["criteria_id"],
            row["target_criteria_id"],
            limit=evidence_rows,
        )
        recommendations.append(
            {
                "rank": rank,
                "relation_type": row["relation_type"],
                "similarity_score": row["similarity_score"],
                "shared_concept_count": row["shared_concept_count"],
                "source_only_count": row["source_only_count"],
                "target_only_count": row["target_only_count"],
                "target_task": {
                    "criteria_id": row["target_criteria_id"],
                    "criteria_no": row["target_criteria_no"],
                    "criteria_text": row["target_criteria_text"],
                    "element_id": row["target_element_id"],
                    "element_no": row["target_element_no"],
                    "element_name": row["target_element_name"],
                    "unit_code": row["target_unit_code"],
                    "unit_name": row["target_unit_name"],
                    "classification": {
                        "major_code": row["target_major_code"],
                        "major_name": row["target_major_name"],
                        "middle_code": row["target_middle_code"],
                        "middle_name": row["target_middle_name"],
                        "small_code": row["target_small_code"],
                        "small_name": row["target_small_name"],
                        "sub_code": row["target_sub_code"],
                        "sub_name": row["target_sub_name"],
                    },
                },
                "evidence": {
                    "shared_ksa_concepts": shared,
                    "target_gap_ksa_concepts": gaps,
                    "method": "atomic_ksa_jaccard",
                },
            }
        )
    return {
        "ok": True,
        "source_task": {
            "criteria_id": source["criteria_id"],
            "criteria_no": source["criteria_no"],
            "criteria_text": source["criteria_text_raw"],
            "element_id": source["element_id"],
            "element_no": source["element_no"],
            "element_name": source["element_name_raw"],
            "unit_code": source["unit_code"],
            "unit_name": source["unit_name_raw"],
            "classification": {
                "major_code": source["major_code"],
                "major_name": source["major_name"],
                "middle_code": source["middle_code"],
                "middle_name": source["middle_name"],
                "small_code": source["small_code"],
                "small_name": source["small_name"],
                "sub_code": source["sub_code"],
                "sub_name": source["sub_name"],
            },
        },
        "source_ksa_concepts": _task_concepts(conn, source["criteria_id"], limit=evidence_rows),
        "summary": {
            "mode": mode,
            "recommendation_count": len(recommendations),
            "upskilling_count": sum(
                1 for item in recommendations if item["relation_type"].startswith("upskilling_")
            ),
            "reskilling_count": sum(
                1 for item in recommendations if item["relation_type"] == "reskilling_transfer_task"
            ),
        },
        "recommendations": recommendations,
    }


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


HR_HUMAN_REVIEW_ISSUE_TYPES = [
    "hr_core_concept_human_review_required",
    "hr_training_goal_link_human_review_required",
]


ONTOLOGY_HUMAN_REVIEW_ISSUE_TYPES = [
    "ontology_core_concept_human_review_required",
    "ontology_training_goal_link_human_review_required",
    "ontology_task_ksa_relation_human_review_required",
]


def _classification_scope_sql(
    *,
    alias: str,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("major_code", major_code),
        ("middle_code", middle_code),
        ("small_code", small_code),
        ("sub_code", sub_code),
    ):
        if value:
            clauses.append(f"{alias}.{column} = ?")
            params.append(value)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def _training_course_scope_sql(
    *,
    alias: str,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("ncs_lclas_cd", major_code),
        ("ncs_mclas_cd", middle_code),
        ("ncs_sclas_cd", small_code),
        ("ncs_subd_cd", sub_code),
    ):
        if value:
            clauses.append(f"{alias}.{column} = ?")
            params.append(value)
    if not clauses:
        return "", []
    return " AND " + " AND ".join(clauses), params


def prepare_ontology_human_review_queue(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    concept_limit: int = 250,
    goal_link_limit: int = 250,
    relation_limit: int = 250,
    min_confidence: float = 0.75,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create all-domain human review issues for high-impact weak ontology links."""
    if not dry_run:
        clear_quality_issues(conn, ONTOLOGY_HUMAN_REVIEW_ISSUE_TYPES)
    max_concepts = clamp_limit(concept_limit, default=250, maximum=5000)
    max_goal_links = clamp_limit(goal_link_limit, default=250, maximum=5000)
    max_relations = clamp_limit(relation_limit, default=250, maximum=5000)
    confidence_floor = max(0.0, min(float(min_confidence), 1.0))

    concept_scope, concept_params = _classification_scope_sql(
        alias="c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    conn.execute("DROP TABLE IF EXISTS temp.tmp_ontology_review_scope_atoms")
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_ontology_review_scope_atoms AS
        SELECT
            acl.concept_id,
            atom.ksa_id,
            atom.element_id,
            cu.unit_code,
            cu.unit_name_raw
        FROM classifications c
        JOIN competency_units cu ON cu.classification_id = c.classification_id
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        JOIN ksa_atomic_items atom ON atom.element_id = ce.element_id
        JOIN ksa_atomic_concept_links acl ON acl.atomic_id = atom.atomic_id
        WHERE 1 = 1
          {concept_scope}
        """,
        concept_params,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS temp.idx_tmp_ontology_review_scope_atoms_concept "
        "ON tmp_ontology_review_scope_atoms(concept_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS temp.idx_tmp_ontology_review_scope_atoms_ksa "
        "ON tmp_ontology_review_scope_atoms(ksa_id)"
    )
    concept_rows = conn.execute(
        """
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition_status,
            oc.review_status,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT scoped.unit_code) AS unit_count,
            GROUP_CONCAT(DISTINCT scoped.unit_name_raw) AS unit_names
        FROM ontology_concepts oc
        JOIN tmp_ontology_review_scope_atoms scoped ON scoped.concept_id = oc.concept_id
        LEFT JOIN element_criteria_ksa_links eck
          ON eck.ksa_id = scoped.ksa_id
         AND eck.element_id = scoped.element_id
        LEFT JOIN performance_criteria pc ON pc.criteria_id = eck.criteria_id
        WHERE oc.review_status != 'human_reviewed'
          AND oc.concept_type IN ('knowledge', 'skill', 'attitude')
        GROUP BY oc.concept_id
        ORDER BY
            CASE oc.definition_status
                WHEN 'missing' THEN 0
                WHEN 'candidate' THEN 1
                ELSE 2
            END,
            criteria_count DESC,
            unit_count DESC,
            oc.concept_type,
            oc.concept_name
        LIMIT ?
        """,
        (max_concepts,),
    ).fetchall()
    for row in concept_rows:
        if dry_run:
            continue
        insert_quality_issue(
            conn,
            target_type="ontology_concept",
            target_id=row["concept_id"],
            issue_type="ontology_core_concept_human_review_required",
            severity="high" if int(row["criteria_count"] or 0) >= 5 else "medium",
            issue_detail=(
                f"High-impact ontology concept needs review: {row['concept_name']} "
                f"({row['concept_type']}, definition_status={row['definition_status']}, "
                f"criteria_count={row['criteria_count']}, unit_count={row['unit_count']}, "
                f"units={row['unit_names'] or ''})"
            ),
            suggested_action=(
                "Confirm the representative concept, definition, and aliases. "
                "If valid, mark definition_status='defined' and review_status='human_reviewed'."
            ),
        )

    goal_scope, goal_params = _training_course_scope_sql(
        alias="tc",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    weak_goal_methods = (
        "training_goal_unit_core_concept",
        "training_goal_element_implied_concept",
        "training_goal_concept_token",
    )
    goal_rows = conn.execute(
        f"""
        SELECT
            gl.link_id,
            gl.training_course_id,
            gl.concept_id,
            gl.link_method,
            gl.confidence_score,
            tc.compe_unit_name,
            tc.ncs_cl_cd,
            tc.train_goal,
            oc.concept_name,
            oc.concept_type
        FROM training_goal_concept_links gl
        JOIN ncs_training_courses tc ON tc.training_course_id = gl.training_course_id
        JOIN ontology_concepts oc ON oc.concept_id = gl.concept_id
        WHERE gl.review_status != 'human_reviewed'
          AND gl.review_status != 'rejected'
          {goal_scope}
          AND (
              gl.link_method IN ({",".join("?" for _ in weak_goal_methods)})
              OR gl.confidence_score < ?
          )
        ORDER BY
            CASE gl.link_method
                WHEN 'training_goal_unit_core_concept' THEN 0
                WHEN 'training_goal_element_implied_concept' THEN 1
                WHEN 'training_goal_concept_token' THEN 2
                ELSE 3
            END,
            gl.confidence_score,
            tc.ncs_lclas_cd,
            tc.ncs_mclas_cd,
            tc.ncs_sclas_cd,
            tc.ncs_subd_cd,
            tc.compe_unit_name,
            oc.concept_name
        LIMIT ?
        """,
        (*goal_params, *weak_goal_methods, confidence_floor, max_goal_links),
    ).fetchall()
    for row in goal_rows:
        if dry_run:
            continue
        severity = "high" if float(row["confidence_score"] or 0.0) < 0.55 else "medium"
        insert_quality_issue(
            conn,
            target_type="training_goal_concept_link",
            target_id=row["link_id"],
            issue_type="ontology_training_goal_link_human_review_required",
            severity=severity,
            issue_detail=(
                f"Weak training-goal to KSA link needs review: course={row['compe_unit_name']} "
                f"({row['ncs_cl_cd']}), concept={row['concept_name']} ({row['concept_type']}), "
                f"method={row['link_method']}, confidence={row['confidence_score']}, "
                f"goal={normalize_spaces(row['train_goal'] or '')}"
            ),
            suggested_action=(
                "Confirm whether the training goal directly covers the KSA. "
                "Mark the link human_reviewed if valid, or rejected if it is only a generic/indirect match."
            ),
        )

    relation_scope, relation_params = _classification_scope_sql(
        alias="c",
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    conn.execute("DROP TABLE IF EXISTS temp.tmp_ontology_review_scope_elements")
    conn.execute(
        f"""
        CREATE TEMP TABLE tmp_ontology_review_scope_elements AS
        SELECT ce.element_id, cu.unit_code, cu.unit_name_raw
        FROM classifications c
        JOIN competency_units cu ON cu.classification_id = c.classification_id
        JOIN competency_elements ce ON ce.unit_code = cu.unit_code
        WHERE 1 = 1
          {relation_scope}
        """,
        relation_params,
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS temp.idx_tmp_ontology_review_scope_elements_element "
        "ON tmp_ontology_review_scope_elements(element_id)"
    )
    relation_rows = conn.execute(
        """
        SELECT
            r.relation_id,
            r.relation_type,
            r.confidence_score,
            r.evidence_text,
            pc.criteria_text_raw,
            ce.element_name_raw,
            scoped.unit_code,
            scoped.unit_name_raw,
            source.concept_name AS source_concept_name,
            source.concept_type AS source_concept_type,
            target.concept_name AS target_concept_name,
            target.concept_type AS target_concept_type
        FROM task_ksa_concept_relations r
        JOIN tmp_ontology_review_scope_elements scoped ON scoped.element_id = r.element_id
        JOIN performance_criteria pc ON pc.criteria_id = r.criteria_id
        JOIN competency_elements ce ON ce.element_id = r.element_id
        JOIN ontology_concepts source ON source.concept_id = r.source_concept_id
        JOIN ontology_concepts target ON target.concept_id = r.target_concept_id
        WHERE r.review_status != 'human_reviewed'
          AND r.review_status != 'rejected'
          AND (
              r.confidence_score < ?
              OR r.relation_type = 'co_required_in_element'
          )
        ORDER BY
            CASE r.relation_type
                WHEN 'co_required_in_element' THEN 0
                ELSE 1
            END,
            r.confidence_score,
            scoped.unit_code,
            r.relation_id
        LIMIT ?
        """,
        (confidence_floor, max_relations),
    ).fetchall()
    for row in relation_rows:
        if dry_run:
            continue
        severity = "high" if row["relation_type"] != "co_required_in_element" else "medium"
        insert_quality_issue(
            conn,
            target_type="task_ksa_concept_relation",
            target_id=row["relation_id"],
            issue_type="ontology_task_ksa_relation_human_review_required",
            severity=severity,
            issue_detail=(
                f"Task-KSA concept relation needs review: unit={row['unit_name_raw']} "
                f"({row['unit_code']}), element={normalize_spaces(row['element_name_raw'] or '')}, "
                f"task={normalize_spaces(row['criteria_text_raw'] or '')}, "
                f"source={row['source_concept_name']} ({row['source_concept_type']}), "
                f"relation={row['relation_type']}, target={row['target_concept_name']} "
                f"({row['target_concept_type']}), confidence={row['confidence_score']}"
            ),
            suggested_action=(
                "Confirm whether this KSA relation is task-essential. "
                "Mark human_reviewed if valid, or rejected if it is only co-occurrence noise."
            ),
        )

    if not dry_run:
        conn.commit()
    return {
        "ok": True,
        "dry_run": dry_run,
        "scope": {
            "major_code": major_code,
            "middle_code": middle_code,
            "small_code": small_code,
            "sub_code": sub_code,
        },
        "concept_review_issues_created": len(concept_rows),
        "training_goal_link_review_issues_created": len(goal_rows),
        "task_ksa_relation_review_issues_created": len(relation_rows),
        "min_confidence": confidence_floor,
        "issue_types": ONTOLOGY_HUMAN_REVIEW_ISSUE_TYPES,
    }


def prepare_hr_human_review_queue(
    conn: sqlite3.Connection,
    *,
    major_code: str = "02",
    middle_code: str = "02",
    small_code: str = "02",
    concept_limit: int = 250,
    goal_link_limit: int = 250,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Create review-required issues for high-impact HR concepts and training-goal links.

    This does not mark anything as human-reviewed. It only prepares a focused queue
    so human reviewers can confirm definitions and weak/derived training-goal links.
    """
    if not dry_run:
        clear_quality_issues(conn, HR_HUMAN_REVIEW_ISSUE_TYPES)
    max_concepts = clamp_limit(concept_limit, default=250, maximum=2000)
    max_goal_links = clamp_limit(goal_link_limit, default=250, maximum=2000)

    concept_rows = conn.execute(
        """
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition_status,
            oc.review_status,
            COUNT(DISTINCT pc.criteria_id) AS criteria_count,
            COUNT(DISTINCT cu.unit_code) AS unit_count,
            GROUP_CONCAT(DISTINCT cu.unit_name_raw) AS unit_names
        FROM ontology_concepts oc
        JOIN ksa_atomic_concept_links acl ON acl.concept_id = oc.concept_id
        JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
        JOIN element_criteria_ksa_links eck ON eck.ksa_id = atom.ksa_id
        JOIN performance_criteria pc ON pc.criteria_id = eck.criteria_id
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
          AND c.middle_code = ?
          AND c.small_code = ?
          AND oc.review_status != 'human_reviewed'
        GROUP BY oc.concept_id
        ORDER BY
            CASE oc.definition_status
                WHEN 'missing' THEN 0
                WHEN 'candidate' THEN 1
                ELSE 2
            END,
            criteria_count DESC,
            unit_count DESC,
            oc.concept_type,
            oc.concept_name
        LIMIT ?
        """,
        (major_code, middle_code, small_code, max_concepts),
    ).fetchall()
    for row in concept_rows:
        if dry_run:
            continue
        insert_quality_issue(
            conn,
            target_type="ontology_concept",
            target_id=row["concept_id"],
            issue_type="hr_core_concept_human_review_required",
            severity="high" if int(row["criteria_count"] or 0) >= 5 else "medium",
            issue_detail=(
                f"핵심 HR KSA 개념 검수 필요: {row['concept_name']} "
                f"({row['concept_type']}, definition_status={row['definition_status']}, "
                f"criteria_count={row['criteria_count']}, units={row['unit_names'] or ''})"
            ),
            suggested_action=(
                "review_ontology_concept로 정의를 확정하고 definition_status='defined', "
                "review_status='human_reviewed'로 승인한다."
            ),
        )

    goal_rows = conn.execute(
        """
        SELECT
            gl.link_id,
            gl.training_course_id,
            gl.concept_id,
            gl.link_method,
            gl.confidence_score,
            tc.compe_unit_name,
            tc.ncs_cl_cd,
            tc.train_goal,
            oc.concept_name,
            oc.concept_type
        FROM training_goal_concept_links gl
        JOIN ncs_training_courses tc ON tc.training_course_id = gl.training_course_id
        JOIN ontology_concepts oc ON oc.concept_id = gl.concept_id
        WHERE tc.ncs_lclas_cd = ?
          AND tc.ncs_mclas_cd = ?
          AND tc.ncs_sclas_cd = ?
          AND gl.review_status != 'human_reviewed'
          AND (
              gl.link_method IN (
                  'training_goal_unit_core_concept',
                  'training_goal_element_implied_concept',
                  'training_goal_concept_token'
              )
              OR gl.confidence_score < 0.75
          )
        ORDER BY
            CASE gl.link_method
                WHEN 'training_goal_unit_core_concept' THEN 0
                WHEN 'training_goal_element_implied_concept' THEN 1
                WHEN 'training_goal_concept_token' THEN 2
                ELSE 3
            END,
            gl.confidence_score,
            tc.compe_unit_name,
            oc.concept_name
        LIMIT ?
        """,
        (major_code, middle_code, small_code, max_goal_links),
    ).fetchall()
    for row in goal_rows:
        if dry_run:
            continue
        insert_quality_issue(
            conn,
            target_type="training_goal_concept_link",
            target_id=row["link_id"],
            issue_type="hr_training_goal_link_human_review_required",
            severity="medium",
            issue_detail=(
                f"HR 훈련목표-KSA 링크 검수 필요: 과정={row['compe_unit_name']} "
                f"({row['ncs_cl_cd']}), 개념={row['concept_name']} ({row['concept_type']}), "
                f"method={row['link_method']}, confidence={row['confidence_score']}, "
                f"goal={normalize_spaces(row['train_goal'] or '')}"
            ),
            suggested_action=(
                "훈련목표가 해당 KSA를 실제로 커버하는지 검토하고, 맞으면 링크를 human_reviewed로 "
                "승인하고 아니면 rejected로 제외한다."
            ),
        )

    if not dry_run:
        conn.commit()
    return {
        "ok": True,
        "dry_run": dry_run,
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "concept_review_issues_created": len(concept_rows),
        "training_goal_link_review_issues_created": len(goal_rows),
        "issue_types": HR_HUMAN_REVIEW_ISSUE_TYPES,
    }
