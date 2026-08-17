from __future__ import annotations

import csv
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


HUMAN_TRUSTED_LABEL_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL = "'human_reviewed', 'accepted', 'reviewed'"
DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES = (*HUMAN_TRUSTED_LABEL_REVIEW_STATUSES, "rejected")
DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL = "'human_reviewed', 'accepted', 'reviewed', 'rejected'"
MACHINE_SCREENED_LABEL_REVIEW_STATUSES = ("llm_reviewed",)
MACHINE_SCREENED_LABEL_REVIEW_STATUS_SQL = "'llm_reviewed'"
TRUSTED_LABEL_REVIEW_STATUSES = HUMAN_TRUSTED_LABEL_REVIEW_STATUSES
TRUSTED_LABEL_REVIEW_STATUS_SQL = HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL
KSA_MEANING_MACHINE_REVIEW_ELIGIBLE_SOURCE_METHODS = (
    "term_definition_template",
    "task_context_template",
    "unlinked_concept_fallback",
)
KSA_MEANING_TASK_CONTEXT_ROLES = {
    "knowledge": "task_knowledge_significance",
    "skill": "task_skill_significance",
    "attitude": "task_attitude_significance",
}
KSA_DEFINITION_BOILERPLATE_PREFIXES = {
    "knowledge": "업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.",
    "skill": "업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력.",
    "attitude": "업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도.",
}
KSA_DEFINITION_BOILERPLATE_SAMPLE_NAMES = {
    "knowledge": (
        "template_\uc9c0\uc2dd",
        "template_\uc774\ud574",
        "template_\uac1c\ub150",
    ),
    "skill": (
        "template_\ub2a5\ub825",
        "template_\uae30\uc220",
        "template_\uc2e4\ud589",
    ),
    "attitude": (
        "template_\uc758\uc9c0",
        "template_\ud0dc\ub3c4",
    ),
}
_KSA_DEFINITION_BOILERPLATE_BODY_CACHE: dict[str, tuple[str, ...]] = {}


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
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
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
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
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
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
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
    source_decision_packet TEXT,
    source_artifact_hash TEXT,
    rationale TEXT,
    evidence_refs_json TEXT,
    created_by_tool TEXT,
    run_artifact TEXT,
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

CREATE TABLE IF NOT EXISTS ontology_concept_label_candidates (
    label_id INTEGER PRIMARY KEY AUTOINCREMENT,
    concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
    source_ksa_id INTEGER REFERENCES ksa_items(ksa_id),
    source_atomic_id INTEGER REFERENCES ksa_atomic_items(atomic_id),
    source_scope_key TEXT NOT NULL DEFAULT 'unknown',
    concept_type TEXT NOT NULL,
    source_text TEXT NOT NULL,
    label_text TEXT NOT NULL,
    normalized_label_key TEXT NOT NULL,
    label_role TEXT NOT NULL DEFAULT 'short_representative_label',
    source_method TEXT NOT NULL,
    candidate_rank INTEGER NOT NULL DEFAULT 1,
    evidence_text TEXT,
    confidence_score REAL NOT NULL DEFAULT 0.6,
    review_status TEXT NOT NULL DEFAULT 'candidate',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (concept_id, source_scope_key, source_method, normalized_label_key)
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
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_concept ON ontology_concept_label_candidates(concept_id);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_scope ON ontology_concept_label_candidates(source_scope_key);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_key ON ontology_concept_label_candidates(normalized_label_key);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_status ON ontology_concept_label_candidates(review_status);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_review_scope ON ontology_concept_label_candidates(review_status, concept_id, source_scope_key, source_ksa_id, source_atomic_id);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_source_ksa ON ontology_concept_label_candidates(source_ksa_id, review_status);
CREATE INDEX IF NOT EXISTS idx_concept_label_candidates_source_atomic ON ontology_concept_label_candidates(source_atomic_id, review_status);
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


def connect(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if read_only:
        uri = path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, timeout=30, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    if read_only:
        conn.execute("PRAGMA query_only = ON")
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
        ("인사팀", "인사", "02", "02", "02", "01", None, 0.9),
        ("인사팀장", "인사", "02", "02", "02", "01", None, 0.92),
        ("인사부서장", "인사", "02", "02", "02", "01", None, 0.9),
        ("인사총괄", "인사", "02", "02", "02", "01", None, 0.9),
        ("HR manager", "인사", "02", "02", "02", "01", None, 0.86),
        ("HR lead", "인사", "02", "02", "02", "01", None, 0.84),
        ("채용", "인력채용", "02", "02", "02", "01", "0202020103_23v4", 0.9),
        ("인력채용", "인력채용", "02", "02", "02", "01", "0202020103_23v4", 0.95),
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
    conn.execute(
        """
        UPDATE ncs_query_aliases
        SET unit_code = '0202020103_23v4',
            updated_at = ?
        WHERE source_method = 'seed'
          AND review_status = 'candidate'
          AND unit_code = '0202020102_23v3'
          AND (alias_text IN ('채용', '인력채용') OR normalized_query = '인력채용')
        """,
        (timestamp,),
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
    migrate_auto_link_review_status_defaults(conn)
    migrate_ksa_label_candidate_source_scope_key(conn)
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
    ensure_column(conn, "review_audit_log", "source_decision_packet", "TEXT")
    ensure_column(conn, "review_audit_log", "source_artifact_hash", "TEXT")
    ensure_column(conn, "review_audit_log", "rationale", "TEXT")
    ensure_column(conn, "review_audit_log", "evidence_refs_json", "TEXT")
    ensure_column(conn, "review_audit_log", "created_by_tool", "TEXT")
    ensure_column(conn, "review_audit_log", "run_artifact", "TEXT")
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
    create_indexes(conn)
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


AUTO_LINK_REVIEW_STATUS_TABLE_SCHEMAS = {
    "ncs_training_course_unit_links": """
CREATE TABLE ncs_training_course_unit_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_course_id INTEGER NOT NULL REFERENCES ncs_training_courses(training_course_id),
    unit_code TEXT NOT NULL REFERENCES competency_units(unit_code),
    link_method TEXT NOT NULL,
    confidence_score REAL NOT NULL DEFAULT 1.0,
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (training_course_id, unit_code, link_method)
)
""",
    "ncs_unit_qualification_links": """
CREATE TABLE ncs_unit_qualification_links (
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
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (unit_code, jm_cd, organ_std_ver_cd, ablt_unit_typ_cd, min_edu_trng_tm)
)
""",
    "ncs_unit_job_base_links": """
CREATE TABLE ncs_unit_job_base_links (
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
    review_status TEXT NOT NULL DEFAULT 'auto_linked',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (unit_code, job_base_competency_id, job_base_factor_id)
)
""",
}


def migrate_auto_link_review_status_defaults(conn: sqlite3.Connection) -> None:
    """Replace legacy `reviewed` defaults on machine-generated link tables."""
    for table, create_sql in AUTO_LINK_REVIEW_STATUS_TABLE_SCHEMAS.items():
        columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not columns:
            continue
        review_column = next((row for row in columns if row["name"] == "review_status"), None)
        if not review_column or review_column["dflt_value"] != "'reviewed'":
            continue
        index_sql = [
            row["sql"]
            for row in conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                (table,),
            ).fetchall()
            if row["sql"]
        ]
        temp_table = f"_{table}_auto_link_default_migration"
        column_names = [row["name"] for row in columns]
        column_list = ", ".join(f'"{name}"' for name in column_names)
        conn.execute(f"ALTER TABLE {table} RENAME TO {temp_table}")
        conn.execute(create_sql)
        conn.execute(f"INSERT INTO {table} ({column_list}) SELECT {column_list} FROM {temp_table}")
        conn.execute(f"DROP TABLE {temp_table}")
        for sql in index_sql:
            conn.execute(sql)


def migrate_ksa_label_candidate_source_scope_key(conn: sqlite3.Connection) -> None:
    """Make KSA short-label candidates source-scope aware."""
    table_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ontology_concept_label_candidates'"
    ).fetchone()
    if not table_exists:
        return
    table_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'ontology_concept_label_candidates'"
    ).fetchone()
    table_sql = str(table_sql_row["sql"] or "") if table_sql_row else ""
    columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(ontology_concept_label_candidates)").fetchall()
    }
    expected_unique = "UNIQUE (concept_id, source_scope_key, source_method, normalized_label_key)"
    if "source_scope_key" in columns and expected_unique in table_sql:
        return

    temp_table = "_ontology_concept_label_candidates_scope_migration"
    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
    conn.execute(f"ALTER TABLE ontology_concept_label_candidates RENAME TO {temp_table}")
    conn.execute(
        """
        CREATE TABLE ontology_concept_label_candidates (
            label_id INTEGER PRIMARY KEY AUTOINCREMENT,
            concept_id INTEGER NOT NULL REFERENCES ontology_concepts(concept_id),
            source_ksa_id INTEGER REFERENCES ksa_items(ksa_id),
            source_atomic_id INTEGER REFERENCES ksa_atomic_items(atomic_id),
            source_scope_key TEXT NOT NULL DEFAULT 'unknown',
            concept_type TEXT NOT NULL,
            source_text TEXT NOT NULL,
            label_text TEXT NOT NULL,
            normalized_label_key TEXT NOT NULL,
            label_role TEXT NOT NULL DEFAULT 'short_representative_label',
            source_method TEXT NOT NULL,
            candidate_rank INTEGER NOT NULL DEFAULT 1,
            evidence_text TEXT,
            confidence_score REAL NOT NULL DEFAULT 0.6,
            review_status TEXT NOT NULL DEFAULT 'candidate',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (concept_id, source_scope_key, source_method, normalized_label_key)
        )
        """
    )
    computed_scope_expr = """
        CASE
            WHEN old.source_atomic_id IS NOT NULL THEN COALESCE((
                SELECT
                    label_c.major_code || ':' || label_c.middle_code || ':' ||
                    label_c.small_code || ':' || label_c.sub_code
                FROM ksa_atomic_items label_atom
                JOIN competency_elements label_ce ON label_ce.element_id = label_atom.element_id
                JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                WHERE label_atom.atomic_id = old.source_atomic_id
            ), 'atomic:' || old.source_atomic_id)
            WHEN old.source_ksa_id IS NOT NULL THEN COALESCE((
                SELECT
                    label_c.major_code || ':' || label_c.middle_code || ':' ||
                    label_c.small_code || ':' || label_c.sub_code
                FROM ksa_items label_ki
                JOIN competency_elements label_ce ON label_ce.element_id = label_ki.element_id
                JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                WHERE label_ki.ksa_id = old.source_ksa_id
            ), 'ksa:' || old.source_ksa_id)
            ELSE 'missing:' || old.label_id
        END
    """
    if "source_scope_key" in columns:
        scope_expr = f"COALESCE(NULLIF(old.source_scope_key, ''), {computed_scope_expr})"
    else:
        scope_expr = computed_scope_expr
    conn.execute(
        f"""
        INSERT OR IGNORE INTO ontology_concept_label_candidates(
            label_id, concept_id, source_ksa_id, source_atomic_id, source_scope_key,
            concept_type, source_text, label_text, normalized_label_key, label_role,
            source_method, candidate_rank, evidence_text, confidence_score,
            review_status, created_at, updated_at
        )
        SELECT
            old.label_id, old.concept_id, old.source_ksa_id, old.source_atomic_id,
            {scope_expr},
            old.concept_type, old.source_text, old.label_text, old.normalized_label_key,
            old.label_role, old.source_method, old.candidate_rank, old.evidence_text,
            old.confidence_score, old.review_status, old.created_at, old.updated_at
        FROM {temp_table} old
        """
    )
    conn.execute(f"DROP TABLE {temp_table}")


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


def _strip_leading_concept_name_from_meaning_text(meaning_text: str, concept_name: str) -> str:
    normalized_text = normalize_spaces(meaning_text)
    normalized_name = normalize_spaces(concept_name)
    if not normalized_text or not normalized_name:
        return normalized_text
    stripped = re.sub(
        rf"^\s*{re.escape(normalized_name)}(?:[\s:：\-–—~·,，.、()\[\]{{}}<>]*)\s*",
        "",
        normalized_text,
        count=1,
        flags=re.IGNORECASE,
    )
    return stripped if stripped != normalized_text else normalized_text


def _generated_ksa_definition_boilerplate_bodies(concept_type: str) -> tuple[str, ...]:
    cached = _KSA_DEFINITION_BOILERPLATE_BODY_CACHE.get(concept_type)
    if cached is not None:
        return cached
    bodies: list[str] = []
    for sample_name in KSA_DEFINITION_BOILERPLATE_SAMPLE_NAMES.get(concept_type, ()):
        sample_text = _term_definition_text_for_concept(
            {
                "concept_name": sample_name,
                "concept_type": concept_type,
            }
        )
        sample_body = normalize_spaces(
            _strip_leading_concept_name_from_meaning_text(sample_text, sample_name)
        )
        if sample_body and sample_body not in bodies:
            bodies.append(sample_body)
    cached = tuple(bodies)
    _KSA_DEFINITION_BOILERPLATE_BODY_CACHE[concept_type] = cached
    return cached


def _is_ksa_definition_boilerplate(concept_type: str, concept_name: str, meaning_text: str) -> bool:
    boilerplate_prefix = KSA_DEFINITION_BOILERPLATE_PREFIXES.get(concept_type)
    generated_text = normalize_spaces(
        _term_definition_text_for_concept(
            {
                "concept_name": concept_name,
                "concept_type": concept_type,
            }
        )
    )
    if generated_text and normalize_spaces(meaning_text) == generated_text:
        return True
    body = _strip_leading_concept_name_from_meaning_text(meaning_text, concept_name)
    normalized_body = normalize_spaces(body)
    if not normalized_body:
        return True
    if boilerplate_prefix and normalized_body.startswith(boilerplate_prefix):
        return True
    for sample_body in _generated_ksa_definition_boilerplate_bodies(concept_type):
        if normalized_body.startswith(sample_body):
            return True
    return False


POSSESSIVE_UI_PROTECTED_TERMS = {
    "건의",
    "결의",
    "논의",
    "문의",
    "정의",
    "합의",
    "협의",
    "회의",
}

CONJUNCTION_PARTICLE_PROTECTED_TERMS = {
    "결과",
    "성과",
    "효과",
    "인과",
    "여과",
    "경과",
    "통과",
    "허가",
}


def _strip_possessive_ui_particle(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        stem = token[:-1]
        if token in POSSESSIVE_UI_PROTECTED_TERMS or len(stem) < 2:
            return token + " "
        return stem + " "

    return re.sub(r"([가-힣A-Za-z0-9]+의)\s+", repl, text)


def _replace_korean_conjunction_particles(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        token = match.group(1)
        particle = match.group(2)
        stem = token[: -len(particle)]
        if (
            token in CONJUNCTION_PARTICLE_PROTECTED_TERMS
            or any(token.endswith(term) for term in CONJUNCTION_PARTICLE_PROTECTED_TERMS)
            or len(stem) < 2
        ):
            return token + " "
        return stem + " 및 "

    text = re.sub(r"([가-힣A-Za-z0-9]+(과|와))\s+(?!함께)", repl, text)
    return re.sub(r"\s*및\s*", " 및 ", text)


GENERIC_KSA_LABEL_KEYS = {
    normalize_concept_key(value)
    for value in (
        "knowledge",
        "skill",
        "attitude",
        "management",
        "analysis",
        "planning",
        "operation",
        "support",
        "communication",
        "지식",
        "기술",
        "능력",
        "태도",
        "개념",
        "관리",
        "분석",
        "기획",
        "계획",
        "운영",
        "지원",
        "작성",
        "활용",
        "측정",
        "검사",
        "평가",
        "수립",
        "이해",
        "협상",
        "공유",
    )
}


def _has_unbalanced_parentheses(text: str) -> bool:
    pairs = (
        ("(", ")"),
        ("[", "]"),
        ("{", "}"),
        ("（", "）"),
        ("【", "】"),
        ("「", "」"),
        ("｢", "｣"),
    )
    return any(text.count(opening) != text.count(closing) for opening, closing in pairs)


def _strip_balanced_parenthetical_expansions(text: str) -> str:
    def strip_pair(value: str, opening: str, closing: str) -> tuple[str, bool]:
        depth = 0
        removed = False
        output: list[str] = []
        for char in value:
            if char == opening:
                depth += 1
                removed = True
                continue
            if char == closing and depth:
                depth -= 1
                continue
            if depth == 0:
                output.append(char)
        if depth:
            return value, False
        return "".join(output), removed

    cleaned = text
    changed = False
    for opening, closing in (("(", ")"), ("（", "）")):
        cleaned, removed = strip_pair(cleaned, opening, closing)
        changed = changed or removed
    return normalize_spaces(cleaned) if changed else normalize_spaces(text)



def _strip_dangling_label_suffix(text: str) -> str:
    cleaned = re.sub(r"(?:[\s,;/·]*(?:등|및))+$", "", normalize_spaces(text))
    return normalize_spaces(cleaned.strip(" ,;:/·"))


def _strip_unbalanced_parenthetical_tail(text: str) -> str:
    cleaned = re.sub(r"\([^)]*$", "", text)
    cleaned = re.sub(r"（[^）]*$", "", cleaned)
    cleaned = re.sub(r"\[[^\]]*$", "", cleaned)
    cleaned = re.sub(r"【[^】]*$", "", cleaned)
    cleaned = re.sub(r"「[^」]*$", "", cleaned)
    cleaned = re.sub(r"｢[^｣]*$", "", cleaned)
    return normalize_spaces(cleaned)


def _remove_unmatched_parenthesis_marks(text: str) -> str:
    def remove_pair(value: str, opening: str, closing: str) -> str:
        chars = list(value)
        stack: list[int] = []
        remove: set[int] = set()
        for index, char in enumerate(chars):
            if char == opening:
                stack.append(index)
            elif char == closing:
                if stack:
                    stack.pop()
                else:
                    remove.add(index)
        remove.update(stack)
        return "".join(char for index, char in enumerate(chars) if index not in remove)

    cleaned = text
    for opening, closing in (
        ("(", ")"),
        ("（", "）"),
        ("[", "]"),
        ("【", "】"),
        ("「", "」"),
        ("｢", "｣"),
    ):
        cleaned = remove_pair(cleaned, opening, closing)
    return normalize_spaces(cleaned)


def _is_generic_ksa_label(label_text: str) -> bool:
    label = normalize_spaces(label_text)
    if not label:
        return True
    return len(label) <= 1 or normalize_concept_key(label) in GENERIC_KSA_LABEL_KEYS


def ksa_label_quality_flags(
    source_text: str,
    label_text: str,
    concept_type: str = "knowledge",
) -> list[str]:
    """Return review-only quality flags for a short KSA label candidate."""
    source = normalize_spaces(source_text)
    label = normalize_spaces(label_text)
    flags: list[str] = []
    if not label:
        return ["empty_label"]

    if _is_generic_ksa_label(label):
        flags.append("generic_or_low_specificity")
    if re.search(r"(?:등|및)\s*$", label):
        flags.append("dangling_enum_suffix")
    if _has_unbalanced_parentheses(label):
        flags.append("unbalanced_parentheses")

    non_space = [char for char in label if not char.isspace()]
    if non_space:
        symbol_count = sum(
            1
            for char in non_space
            if not (char.isalnum() or "\uac00" <= char <= "\ud7a3")
        )
        digit_count = sum(1 for char in non_space if char.isdigit())
        if symbol_count / len(non_space) >= 0.35:
            flags.append("symbol_heavy")
        if digit_count >= 3 and digit_count / len(non_space) >= 0.30:
            flags.append("digit_heavy")

    if re.fullmatch(r"[A-Z0-9]{2,5}", label) and any(char.isalpha() for char in label):
        flags.append("short_acronym_needs_context")

    residual_clause_like_label = bool(
        re.search(
            r"(?:할\s*수\s*있|수\s*있|하려는|하고자|하기\s*위한|수행하기|판단하는|"
            r"실행하는|설계하는|피드백\s*하는|이끌어\s*낼|강화할|높일|예방하고|"
            r"직결된다는|연결된다는|기반\s+성과|책임있는|능동적\s+대응)",
            label,
        )
    )
    if residual_clause_like_label:
        flags.append("residual_sentence_like_label")
    if len(label) > 34:
        flags.append("overlong_word_label")

    if source and label != source:
        ratio = len(label) / max(1, len(source))
        related_law_canonicalization = bool(
            re.match(r"^\s*관련\s*(법규|법령)\s*\(", source)
            and re.search(r"\s관련\s*(법규|법령)$", label)
        )
        clause_like_label = bool(
            re.search(
                r"(?:할\s*수\s*있는|수\s*있는|하려는|하고자|하는\s*(?:능력|기술|지식|자세|태도|의지|노력|마음가짐|성실성|희생정신)?|하기\s*위한|해\s*나가|될\s*수\s*있도록|없도록|통해|미치는|따른)",
                label,
            )
        )
        if ratio < 0.15:
            flags.append("very_low_label_source_ratio")
        elif ratio >= 0.90 and clause_like_label and not related_law_canonicalization:
            flags.append("changed_near_full_length")

    if (
        concept_type == "skill"
        and _is_generic_ksa_label(label)
        and re.search(r"(능력|기술)\s*$", source)
        and label != source
    ):
        flags.append("skill_suffix_stripped_to_generic")

    return list(dict.fromkeys(flags))


def concept_type_from_ksa(ksa_type_name: str) -> str:
    mapping = {
        "지식": "knowledge",
        "기술": "skill",
        "태도": "attitude",
    }
    return mapping.get(ksa_type_name.strip(), "knowledge")


def _split_top_level_parenthetical_items(value: str) -> list[str]:
    """Split comma-like lists while preserving commas inside nested parentheses."""
    items: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    closing_for = {"(": ")", "[": "]", "{": "}", "「": "」", "『": "』", "〔": "〕"}
    closings = set(closing_for.values())
    index = 0
    while index < len(value):
        char = value[index]
        if char in closing_for:
            stack.append(closing_for[char])
            current.append(char)
            index += 1
            continue
        if char in closings and stack and char == stack[-1]:
            stack.pop()
            current.append(char)
            index += 1
            continue
        if not stack and char in ",;/·":
            item = normalize_spaces("".join(current))
            if item:
                items.append(item)
            current = []
            index += 1
            continue
        if not stack:
            matched_word_separator = False
            for separator in (" 및 ", " 또는 "):
                if value.startswith(separator, index):
                    item = normalize_spaces("".join(current))
                    if item:
                        items.append(item)
                    current = []
                    index += len(separator)
                    matched_word_separator = True
                    break
            if matched_word_separator:
                continue
        current.append(char)
        index += 1

    item = normalize_spaces("".join(current))
    if item:
        items.append(item)
    return items


def _normalize_law_name_item(value: str) -> tuple[str, bool]:
    part = normalize_spaces(value.strip(" ,;:/·"))
    has_etc = bool(re.search(r"(?:^|\s)등$", part))
    part = normalize_spaces(re.sub(r"(?:^|\s)등$", "", part))
    part = normalize_spaces(_strip_balanced_parenthetical_expansions(part))
    part = part.strip("「」『』〔〕\"'")
    return normalize_spaces(part), has_etc


def _looks_like_law_name(value: str) -> bool:
    if _is_generic_ksa_label(value):
        return False
    return bool(re.search(r"(?:법|법률|령|규칙|조례|고시|훈령|예규|규정|지침)$", value))


def _compact_attitude_action_phrase(value: str) -> str:
    label = normalize_spaces(value)
    label = re.sub(
        r"\s+(?:적극적|적극적인|열린|적극적이고\s+창의적인|적극적이고\s+책임감\s+있는|책임감\s+있는|꼼곰한|꼼꼼한|철저한\s+사전\s+준비|철저한|창의적이고\s+유연한|개방적)\s+(?=자세|태도|의지|노력|마음가짐|성실성|희생정신|책임감|사고|꼼꼼함|적극성|진취성)",
        " ",
        label,
    )
    action_noun_map = {
        "배우": "학습",
        "돕": "지원",
        "도와주": "지원",
        "완료시키": "완료",
        "완료시": "완료",
        "기록": "기록",
        "파악": "파악",
        "고려": "고려",
        "검토": "검토",
        "확인": "확인",
        "준수": "준수",
        "유지": "유지",
        "수용": "수용",
        "관리": "관리",
        "점검": "점검",
        "체크": "체크",
        "관찰": "관찰",
        "해결": "해결",
        "개선": "개선",
        "공유": "공유",
        "협업": "협업",
        "소통": "소통",
        "분석": "분석",
        "판단": "판단",
        "작성": "작성",
        "수립": "수립",
        "사고": "사고",
        "설정": "설정",
        "수용": "수용",
        "활용": "활용",
        "진행": "진행",
        "테스트": "테스트",
        "반영": "반영",
        "적용": "적용",
        "이해": "이해",
        "강구": "강구",
        "운영": "운영",
        "제작": "제작",
        "준비": "준비",
        "전달": "전달",
        "사용": "사용",
        "경청": "경청",
        "점검·관리": "점검·관리",
        "우선시": "우선시",
        "대처": "대처",
        "완성": "완성",
        "예방": "예방",
        "조사": "조사",
        "운용": "운용",
        "노력": "노력",
        "도출": "도출",
        "처리": "처리",
        "설치": "설치",
        "달성": "달성",
        "취급": "취급",
        "향상": "향상",
        "계산": "계산",
        "대비": "대비",
        "작업": "작업",
        "비교 검토": "비교 검토",
        "평가": "평가",
        "수집": "수집",
        "분류": "분류",
        "검증": "검증",
        "문서화": "문서화",
        "구현": "구현",
        "선별": "선별",
        "확보": "확보",
        "조치": "조치",
        "설계": "설계",
        "최소화": "최소화",
        "비교": "비교",
        "선택": "선택",
        "구성": "구성",
        "창조": "창조",
        "고도화": "고도화",
        "선정": "선정",
        "추진": "추진",
        "최우선": "최우선",
        "설명": "설명",
        "연계": "연계",
        "존중": "존중",
        "대응": "대응",
        "중시": "중시",
        "우선": "우선",
        "접근": "접근",
        "이행": "이행",
        "피드백": "피드백",
        "유도": "유도",
        "반영": "반영",
    }

    stem_match = re.match(
        r"^(.+?)(?:해\s*나가려는|하고자\s*하는|하?려는|려는|할\s*수\s*있는|하는)\s+(?:자세|태도|의지|노력|마음가짐|성실성|희생정신|책임감|사고|꼼꼼함|적극성|진취성|능력|기술)$",
        label,
    )
    if not stem_match:
        return label

    stem = normalize_spaces(stem_match.group(1))
    matched_action = ""
    action_noun = ""
    for suffix, noun in sorted(action_noun_map.items(), key=lambda item: len(item[0]), reverse=True):
        if stem.endswith(suffix):
            matched_action = suffix
            action_noun = noun
            break
    if not matched_action:
        return label

    target = normalize_spaces(stem[: -len(matched_action)])
    target = re.sub(
        r"(?:명확히|명확하게|정확히|정확하게|세밀히|세밀하게|꼼꼼하게|공정하게|꾸준히|정중하게|주기적으로|세심하게|적극적으로|합리적으로|성공적으로|지속적으로|주의\s*깊게)\s+",
        "",
        target,
    )
    target = re.sub(r"(?:을|를)\s*$", "", target)
    coordinate_match = re.search(r"\s([가-힣]{2,12})하고$", target)
    if coordinate_match:
        coordinate_action = coordinate_match.group(1)
        target = normalize_spaces(target[: coordinate_match.start()])
        action_noun = f"{coordinate_action}·{action_noun}"
    target = _normalize_wordlike_ksa_phrase(target)
    if not target:
        return normalize_spaces(f"{action_noun} 태도")

    return normalize_spaces(f"{target} {action_noun} 태도")


def _compact_related_law_parenthetical_label(value: str) -> str | None:
    """Keep named laws when a generic related-law phrase carries law-name examples."""
    match = re.match(r"^\s*관련\s*(법규|법령)\s*\((.+)\)\s*$", value)
    if not match:
        return None

    law_kind = match.group(1)
    inner = normalize_spaces(match.group(2))
    raw_parts = _split_top_level_parenthetical_items(inner)
    law_names: list[str] = []
    has_etc = False
    for raw_part in raw_parts:
        part, part_has_etc = _normalize_law_name_item(raw_part)
        has_etc = has_etc or part_has_etc
        if not part:
            continue
        if not _looks_like_law_name(part):
            return None
        if part not in law_names:
            law_names.append(part)

    if not law_names:
        return None

    displayed = law_names[:2]
    suffix = " 등" if has_etc or len(law_names) > len(displayed) else ""
    return f"{'·'.join(displayed)}{suffix} 관련 {law_kind}"


def _normalize_wordlike_ksa_phrase(value: str) -> str:
    """Normalize Korean KSA clauses into reusable ontology-term style noun phrases."""
    label = normalize_spaces(value)
    if not label:
        return label
    label = re.sub(r"(?<!\S)(?:각|각종|주어진)\s+", "", label)
    label = re.sub(r"(?<!\S)전체적인\s+", "전체 ", label)
    label = re.sub(r"(?<!\S)통합적인\s+", "통합 ", label)
    label = re.sub(r"하고자하는", "하고자 하는", label)
    label = re.sub(r"논리적으로\s+(?!(?:사고|생각))", "", label)
    label = re.sub(
        r"(?:명확히|명확하게|정확히|정확하게|세밀히|세밀하게|꼼꼼하게|꼼곰하게|공정하게|평등하게|체계적으로|구체적으로|절절하게|꾸준히|정중하게|주기적으로|세심하게|안전하게|효율적으로|객관적으로|종합적으로|현실적으로|철저히|적극적으로|충실히|합리적으로|성공적으로|지속적으로|주의\s*깊게|원활한)\s+",
        "",
        label,
    )
    label = re.sub(r"\s*(?:을|를)\s+통해\s+", " 기반 ", label)
    label = re.sub(r"\s+통해\s+", " 기반 ", label)
    label = re.sub(r"\s*에\s+필요한\s+", " 필요 ", label)
    label = re.sub(r"\s*에\s+따른\s+", " 기준 ", label)
    label = re.sub(r"\s*에\s+따라\s+", " 기준 ", label)
    label = re.sub(r"\s*에\s+(?=대처|대비|대응|참고|적합|반영)", " ", label)
    label = re.sub(r"\s*에\s+영향을\s+미치는\s+", " 영향 ", label)
    label = re.sub(r"\s*에\s+영향\s+미치는\s+", " 영향 ", label)
    label = re.sub(r"\s*에\s+미치는\s+", " ", label)
    label = re.sub(r"\s*에\s+준해\s+", " 기준 ", label)
    label = re.sub(r"\s+위해\s+", " ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)하지\s+않도록", r"\1 방지", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)가\s+될\s+수\s+있도록", r"\1", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)이\s+없도록", r"\1 방지", label)
    label = re.sub(r"(?<!\S)계획된\s+", "계획 ", label)
    label = re.sub(r"\s+중심으로\s+", " 중심 ", label)
    label = re.sub(r"\s+최우선으로\s+", " 최우선 ", label)
    label = re.sub(r"방법으로\s+", "방법 ", label)
    label = re.sub(r"\s+(활용|이용|사용|적용|고려)한\s+", r" \1 ", label)
    action_terms = (
        "활용|이용|사용|적용|고려|대처|대응|대비|참고|반영|관리|평가|분석|수집|작성|제출|"
        "설정|확인|기록|유지|정리|전달|탐색|모델링|수행|운영|이해|토론|전환|감내|연결|"
        "선정|연구|도면화|청결|책임|제공|규정|요구|설치|취급|유도|요청|변화|구성|포장|"
        "제안|표현|서술|부합|연동|작동|도출|배제|준비|배려|관찰|시도|확신|파악|분류|"
        "참여|활동|보급|확산|개발|개선|결정|보장|검토|처치|휴원|제작|완성|상담|등록|"
        "판단|포함|확보|설득|창조|제작|상승자세\\s+유지|핵심키워드\\s+활용|연계|준수|"
        "선택|시각화|배치|실행|응대|조율|안내|보수|수렴|조성|질의|처리|보호|수립|"
        "추구|최소화|재확인|점검|코딩|구분|폐기|인지|방지|발견|확보|예측|조절|허용"
    )
    label = re.sub(
        rf"(?<!\S)({action_terms})하는\s+",
        r"\1 ",
        label,
    )
    label = re.sub(
        rf"\s+({action_terms})하는\s+",
        r" \1 ",
        label,
    )
    label = re.sub(
        rf"\s+({action_terms}|참여|진행|증진)하여\s+",
        r" \1 ",
        label,
    )
    label = re.sub(
        r"\s+(분석|검토|확인|평가|수집|정리|비교|분류|판단)하고\s+(정리|분석|검토|평가|처리|분류|판단)\s+",
        r" \1 및 \2 ",
        label,
    )
    label = re.sub(r"\s+바라볼\s+수\s+있는\s+", " 관찰 ", label)
    label = re.sub(r"\s+줄\s+수\s+있는\s+", " 제공 ", label)
    label = re.sub(r"\s+책임질\s+수\s+있는\s+", " 책임 ", label)
    label = re.sub(r"\s+멈출\s+수\s+있는\s+", " 중지 ", label)
    label = re.sub(r"\s+도출될\s+수\s+있는\s+", " 도출 ", label)
    label = re.sub(r"\s+정리해\s+낼\s+수\s+있는\s+", " 정리 ", label)
    label = re.sub(r"\s+높일\s+수\s+있는\s+", " 향상 ", label)
    label = re.sub(r"\s+응하게\s+하는\s+", " 응대 ", label)
    label = re.sub(r"\s+이끌어갈\s+수\s+있는\s+", " 지도 ", label)
    label = re.sub(r"\s+바탕으로\s+하는\s+", " 기반 ", label)
    label = re.sub(r"\s+운영될\s+수\s+있도록\s+지원하는\s+", " 운영 지원 ", label)
    label = re.sub(r"\s+받아들일\s+수\s+있는\s+", " 수용 ", label)
    label = re.sub(r"\s+내릴\s+수\s+있는\s+", " 판단 ", label)
    label = re.sub(r"\s+나눌\s+수\s+있는\s+", " 분류 ", label)
    label = re.sub(r"\s+돕고자\s+하는\s+", " 지원 ", label)
    label = re.sub(r"\s+찾아\s+", " 탐색 ", label)
    label = re.sub(r"\s+찾아내려고\s+하는\s+", " 탐색 ", label)
    label = re.sub(
        r"\s+(분석|검토|확인|평가|수집|정리|비교|분류|판단)하고\s+(정리|분석|검토|평가|처리|분류|판단)\s+",
        r" \1 및 \2 ",
        label,
    )
    label = re.sub(
        r"\s*에\s+(책임|관찰|제공|응대|지도|참여|활동|보급|확산|개발|개선|결정|보장|검토|처치|휴원|제작|완성|상담|등록|설치|취급|요구|규정|요청|제안|표현|서술|포장)\s+",
        r" \1 ",
        label,
    )
    label = re.sub(r"\s+대하는\s+", " ", label)
    label = re.sub(r"\s+필요로\s+하는\s+", " 필요 ", label)
    label = re.sub(r"\s+요하는\s+", " 필요 ", label)
    label = re.sub(r"\s+영향\s+미치는\s+", " 영향 ", label)
    label = re.sub(r"\s+발생하는\s+", " 발생 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)해야\s+하는\s+", r"\1 필요 ", label)
    label = re.sub(r"\s+해야\s+하는\s+", " 필요 ", label)
    label = re.sub(r"\s+함께\s+하는\s+", " 함께하는 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)하는데\s+필요한\s+", r"\1 필요 ", label)
    label = re.sub(r"\s+하는데\s+필요한\s+", " 필요 ", label)
    label = re.sub(r"\s+할\s*수\s+있는\s+", " ", label)
    label = re.sub(r"\s+제작할수\s+있는\s+", " 제작 ", label)
    label = re.sub(r"\s+만들\s+수\s+있는\s+", " 제작 ", label)
    label = re.sub(r"\s+만들어\s+낼\s+수\s+있는\s+", " 구상 ", label)
    label = re.sub(r"\s+보여줄\s+수\s+있는\s+", " 표현 ", label)
    label = re.sub(r"\s+높여줄\s+수\s+있는\s+", " 향상 ", label)
    label = re.sub(
        r"([가-힣A-Za-z0-9]+)(?:이|가)\s+(제시|영위|참여|규정|요구|관리|평가|분석|수행)하는\s+",
        r"\1 \2 ",
        label,
    )
    label = re.sub(r"\s+(고려|보존|검토|확인|분석|확정|체크|수정|선정|비교|분류|정리|평가)하여\s+", r" \1 ", label)
    label = re.sub(r"\s*(?:을|를)\s+통한\s+", " 기반 ", label)
    label = re.sub(r"\s*(?:에서|에서의)\s+", " ", label)
    label = re.sub(r"\s*(?:을|를)\s+위한\s+", " ", label)
    label = re.sub(r"\s*하기\s+위한\s+", " ", label)
    label = re.sub(r"\s*해야\s+할\s+", " ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)들(?:을|를)\s+", r"\1 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)\s*할\s+수\s+있는\s+", r"\1 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)하고자\s+하는\s+", r"\1 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)\s*하려는(자세|태도|의지|노력)", r"\1 \2", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)\s*하려는\s+", r"\1 ", label)
    label = re.sub(
        r"([가-힣A-Za-z0-9]+)\s*하는\s+(방법|태도|자세|노력|의지|능력|기술|지식|책임단위)",
        r"\1 \2",
        label,
    )
    label = re.sub(
        r"([가-힣A-Za-z0-9]+)\s*하는\s+((?:유연한|침착하고\s+유연한|통찰력\s+있는|신중하고\s+냉철한|미학적|긍정적인|긍정적|창의적인|창의적|객관적인|객관적|성실한|관리자적)\s+태도)",
        r"\1 \2",
        label,
    )
    label = re.sub(r"\s+(긍정적인|긍정적)\s+태도$", " 긍정적 태도", label)
    label = re.sub(r"\s+(창의적인|창의적)\s+태도$", " 창의적 태도", label)
    label = re.sub(r"\s+(객관적인|객관적)\s+태도$", " 객관적 태도", label)
    label = re.sub(r"\s+능력\s+및\s+", " 및 ", label)
    label = re.sub(r"([가-힣A-Za-z0-9]+)(?:들과의|와의|과의)\s+", r"\1 ", label)
    label = re.sub(r"\s+(?:와|과)\s+", " ", label)
    label = re.sub(r"\s*(?:이나|나|또는)\s+", "·", label)
    label = re.sub(r"\s*(?:그리고|및)\s+", " 및 ", label)
    label = re.sub(r"\s+있어서\s+", " ", label)
    label = re.sub(r"\s+사전\s+준비하고\s+실행\s+", " 준비·실행 ", label)
    label = re.sub(r"\s+사전\s+준비\s+및\s+실행\s+", " 준비·실행 ", label)
    label = re.sub(r"\s+피드백\s+하는\s+", " 피드백 ", label)
    label = re.sub(r"\s*(?:을|를)\s+", " ", label)
    label = re.sub(
        r"^(.+?\s+다양성)\s+이해하고\s+(?:존경|존중)\s+태도$",
        r"\1 존중 태도",
        label,
    )
    label = re.sub(
        r"\s+이해하고\s+(활용|반영|선정|적용|존중|관리|검토|판단|분석|대응)\s+(태도|자세|의지|노력)$",
        r" 이해·\1 태도",
        label,
    )
    label = re.sub(
        r"\s+([가-힣]{2,12})하고\s+(반영|선정|적용|존중|관리|검토|판단|분석|대응|보존|공유)\s+(태도|자세|의지|노력)$",
        r" \1·\2 태도",
        label,
    )
    label = re.sub(r"\s+(성실성|책임감|적극성|진취성|꼼꼼함)$", " 태도", label)
    label = re.sub(
        r"\s+(?:적극적인|적극적|책임감\s+있는)\s+실행\s+(?:의지|태도|자세|노력)$",
        " 태도",
        label,
    )
    label = re.sub(
        r"\s+(식별|판단|관리|평가|분석|작성|수립|운영|처리|확인|검토|수집|정리|해석|활용|적용|제시|도출|측정|조정|협의|소통|기록|보고)\s*$",
        r" \1",
        label,
    )
    label = re.sub(r"\s+", " ", label).strip(" ,;:/·")
    return normalize_spaces(label)


def _meaning_role_for_concept_type(concept_type: str) -> str:
    return {
        "knowledge": "task_knowledge_significance",
        "skill": "task_skill_significance",
        "attitude": "task_attitude_significance",
    }.get(concept_type, "task_ksa_significance")


def _strip_ksa_definition_term_suffix(concept_name: str, concept_type: str) -> str:
    term = normalize_spaces(concept_name)
    if concept_type == "skill":
        term = re.sub(r"\s*(능력|기술)\s*$", "", term)
    elif concept_type == "attitude":
        term = re.sub(r"\s*(태도|자세|의지)\s*$", "", term)
    elif concept_type == "knowledge":
        term = re.sub(r"\s*(지식|이해)\s*$", "", term)
    term = re.sub(r"\s*(에\s*대한|에\s*관한|관련)\s*$", "", term)
    term = re.sub(r"\s*(할\s*수\s*있는|수\s*있는|하는|하려는|하기\s*위한|위한)\s*$", "", term)
    term = re.sub(r"(\S+)(을|를)\s+([가-힣A-Za-z0-9()]+)$", r"\1 \3", term)
    term = re.sub(r"적으로\s+([가-힣]+)$", r"적 \1", term)
    term = normalize_spaces(term)
    return term or normalize_spaces(concept_name)


def _contains_any(value: str, needles: tuple[str, ...]) -> bool:
    return any(needle in value for needle in needles)


def _korean_object_particle(value: str) -> str:
    normalized = normalize_spaces(value)
    particle_reference = re.sub(r"\s*\([A-Za-z0-9 ._+/:-]+\)\s*$", "", normalized).strip() or normalized
    for char in reversed(particle_reference):
        codepoint = ord(char)
        if 0xAC00 <= codepoint <= 0xD7A3:
            return "을" if (codepoint - 0xAC00) % 28 else "를"
        if char.isalnum():
            return "을"
    return "을"


def _with_korean_object_particle(value: str) -> str:
    return f"{value}{_korean_object_particle(value)}"


def _is_regulatory_knowledge_term(value: str) -> bool:
    term = normalize_spaces(value)
    if _contains_any(term, ("법령", "법률", "규정", "기준", "지침", "제도")):
        return True
    if term.endswith(
        (
            "방법",
            "기법",
            "분석법",
            "평가법",
            "진단법",
            "검토법",
            "조사법",
            "계산법",
            "측정법",
            "작성법",
            "활용법",
            "운영법",
            "설계법",
            "수립법",
            "분류법",
            "처리법",
            "해석법",
        )
    ):
        return False
    if re.search(r"(보호|기본|근로|노동|고용|산업|안전|회계|민|형|상|행정|저작권|공정거래|보험|계약|조세)법$", term):
        return True
    return False


def _term_definition_text_for_concept(row: sqlite3.Row) -> str:
    concept_name = normalize_spaces(row["concept_name"])
    concept_type = row["concept_type"]
    core_term = _strip_ksa_definition_term_suffix(concept_name, concept_type)
    object_term = _with_korean_object_particle(core_term)
    if concept_type == "knowledge":
        if _is_regulatory_knowledge_term(core_term):
            body = f"{core_term}의 적용 요건과 준수 기준을 과업 맥락에서 판단하는 지식."
        elif _contains_any(core_term, ("분석", "진단", "평가", "검토")):
            body = f"{core_term}에 필요한 자료와 판단 기준을 해석하여 과업 의사결정에 활용하는 지식."
        elif _contains_any(core_term, ("계획", "기획", "전략", "설계")):
            body = f"{core_term}의 목적, 범위, 실행 조건을 이해하여 과업 방향을 정하는 지식."
        elif _contains_any(core_term, ("관리", "운영", "처리")):
            body = f"{core_term}의 절차, 기준, 확인 항목을 이해하여 과업을 안정적으로 수행하는 지식."
        else:
            body = f"{core_term}의 의미, 적용 조건, 판단 기준을 과업 맥락에서 이해하는 지식."
        return f"{concept_name}: {body}"
    if concept_type == "skill":
        if _contains_any(core_term, ("분석", "진단", "평가", "검토")):
            body = f"{object_term} 위해 자료를 수집, 정리, 해석하여 과업 판단에 적용하는 능력."
        elif _contains_any(core_term, ("계획", "기획", "설계", "수립")):
            body = f"{object_term} 위해 목표, 절차, 자원, 일정을 구조화하여 실행안을 만드는 능력."
        elif _contains_any(core_term, ("작성", "문서", "보고")):
            body = f"{object_term} 위해 필요한 정보를 구조화하고 산출물로 표현하는 능력."
        elif _contains_any(core_term, ("관리", "운영", "처리")):
            body = f"{object_term} 위해 현황을 확인하고 기준에 따라 조정, 실행, 기록하는 능력."
        elif _contains_any(core_term, ("소통", "협상", "조정", "협의")):
            body = f"{object_term} 위해 이해관계자 정보를 교환하고 합의점을 도출하는 능력."
        else:
            body = f"{object_term} 과업 상황에 맞게 실행하거나 적용하는 능력."
        return f"{concept_name}: {body}"
    if concept_type == "attitude":
        if _contains_any(core_term, ("고객", "사용자", "민원")):
            body = f"{object_term} 기준으로 요구와 상황을 세심하게 확인하려는 태도."
        elif _contains_any(core_term, ("품질", "정확", "검증", "준수")):
            body = f"{object_term} 기준으로 결과의 정확성과 기준 준수를 유지하려는 태도."
        elif _contains_any(core_term, ("협업", "소통", "공유")):
            body = f"{object_term} 기준으로 필요한 정보를 공유하고 함께 문제를 해결하려는 태도."
        else:
            body = f"{object_term} 기준으로 업무 품질, 협업, 책임 있는 실행을 유지하려는 태도."
        return f"{concept_name}: {body}"
    return f"{concept_name}: NCS 과업 수행에서 {object_term} 적용하는 KSA 개념."


def compact_ksa_representative_label(value: str, concept_type: str = "knowledge") -> dict[str, Any]:
    """Return a reviewable short label candidate without changing the source concept."""
    original = normalize_spaces(value)
    if not original:
        return {
            "label_text": "",
            "source_method": "empty_source",
            "confidence_score": 0.0,
            "changed": False,
            "method_details": "empty_source",
        }

    label = original
    methods: list[str] = []

    enumerated_prefix = re.match(r"^(.{2,90}?)\s*등의\s+(.+)$", label)
    if enumerated_prefix:
        prefix = enumerated_prefix.group(1)
        tail = enumerated_prefix.group(2)
        if "," in prefix or " 및 " in prefix or "와 " in prefix or "과 " in prefix:
            label = tail
            methods.append("drop_enumerated_actor_prefix")

    before_particle_cleanup = label
    related_law_label = _compact_related_law_parenthetical_label(label)
    if related_law_label:
        label = related_law_label
        methods.append("compact_related_law_parenthetical")
    else:
        label = re.sub(r"\s*등에\s*대한\s*", " ", label)
        label = re.sub(r"\s*에\s*대한\s*", " ", label)
        label = re.sub(r"\s*에\s*관한\s*", " ", label)
        label = re.sub(r"\s*관련\s*", " ", label)
        label = re.sub(r"^한국의\s+(?=협력대상국)", "", label)
        label = _strip_possessive_ui_particle(label)
        label = _replace_korean_conjunction_particles(label)
        label = normalize_spaces(label)
        if label != before_particle_cleanup:
            methods.append("normalize_particles")

    parenthetical_removed = _strip_balanced_parenthetical_expansions(label)
    if parenthetical_removed and parenthetical_removed != label:
        label = parenthetical_removed
        methods.append("drop_parenthetical_expansion")

    wordlike_label = _normalize_wordlike_ksa_phrase(label)
    if wordlike_label != label:
        label = wordlike_label
        methods.append("normalize_wordlike_phrase")

    if concept_type == "skill":
        shortened = re.sub(r"\s*능력$", "", label)
        shortened = re.sub(r"\s*기술$", "", shortened)
        shortened = re.sub(r"\s*할\s*수\s*있는$", "", shortened)
        shortened = re.sub(r"\s*수\s*있는$", "", shortened)
        shortened = re.sub(r"\s*하기\s*위한$", "", shortened)
        shortened = re.sub(r"\s*위한$", "", shortened)
        shortened = re.sub(r"\s*하는$", "", shortened)
        shortened = re.sub(r"([가-힣A-Za-z0-9]+)할\s*$", r"\1", shortened)
        shortened = re.sub(r"\s*,\s*", "·", shortened)
        if 2 <= len(shortened) < len(label):
            generic_skill_labels = {
                "기술",
                "활용",
                "작성",
                "운영",
                "측정",
                "검사",
                "분석",
                "발표",
                "협상",
            }
            candidate_label = normalize_spaces(shortened)
            if normalize_concept_key(candidate_label) not in {
                normalize_concept_key(value) for value in generic_skill_labels
            }:
                label = candidate_label
                methods.append("drop_skill_suffix")
    elif concept_type == "attitude":
        shortened = label.replace("적이며 ", "적·")
        shortened = shortened.replace("이면서도 ", "·")
        diversity_match = re.match(r"^(.{2,40}?)(?:의)?\s*다양성\s+이해하고\s+존경하려는\s+태도$", shortened)
        if diversity_match:
            shortened = f"{normalize_spaces(diversity_match.group(1))} 다양성 존중 태도"
            methods.append("compact_attitude_diversity_phrase")
        understand_respect_match = re.match(
            r"^(.{2,60}?)\s+이해하고\s+(?:존중|존경)하려는\s+태도$",
            shortened,
        )
        if understand_respect_match:
            shortened = f"{normalize_spaces(understand_respect_match.group(1))} 이해·존중 태도"
            methods.append("compact_attitude_respect_phrase")
        shortened = re.sub(r"적으로\s+사고하려는\s*(?:의지|자세|태도)$", "적 사고 태도", shortened)
        shortened = re.sub(r"적으로\s+사고", "적 사고", shortened)
        shortened = normalize_spaces(shortened).replace("· ", "·")
        action_shortened = _compact_attitude_action_phrase(shortened)
        if action_shortened != shortened:
            shortened = action_shortened
            methods.append("compact_attitude_action_phrase")
        else:
            shortened = re.sub(r"\s*(?:의지|자세)$", " 태도", shortened)
        if 2 <= len(shortened) <= len(label) + 2:
            label = shortened
            if label != before_particle_cleanup:
                methods.append("normalize_attitude_phrase")
    else:
        shortened = re.sub(r"사례현황$", "사례", label)
        shortened = re.sub(r"현황\s+및\s+사례현황$", "현황 및 사례", shortened)
        knowledge_candidate = re.sub(
            r"(?:\s+지식|기초지식|해독지식|분석지식|관련지식|\s+이해)$",
            "",
            shortened,
        )
        if (
            2 <= len(knowledge_candidate) < len(shortened)
            and not _is_generic_ksa_label(knowledge_candidate)
        ):
            shortened = normalize_spaces(knowledge_candidate)
            methods.append("drop_knowledge_suffix")
        if shortened != label:
            label = normalize_spaces(shortened)
            methods.append("normalize_knowledge_suffix")

    if (
        "법체계" in label
        and "역사" in label
        and "정치경제" in label
        and "국제관계" in label
        and ("사회정책" in label or "문화" in label)
    ):
        domain_match = re.match(r"^(.{2,30}?)[\s,]+법체계", label)
        domain = normalize_spaces(domain_match.group(1)) if domain_match else ""
        label = normalize_spaces(f"{domain} 제도·사회문화 환경") if domain else "제도·사회문화 환경"
        methods.append("collapse_environment_enumeration")
    elif label.count(",") >= 2 and len(label) > 34:
        parts = [normalize_spaces(part) for part in label.split(",") if normalize_spaces(part)]
        if len(parts) >= 3:
            label = f"{parts[0]} 등"
            methods.append("collapse_long_enumeration")
    elif " - " in label and len(label) > 34:
        first_segment = normalize_spaces(label.split(" - ", 1)[0])
        if len(first_segment) >= 4 and not _is_generic_ksa_label(first_segment):
            label = first_segment
            methods.append("collapse_dash_enumeration")

    label = re.sub(r"\s+", " ", label).strip(" ,;:/")
    repaired = _strip_dangling_label_suffix(label)
    tail_repaired = _strip_unbalanced_parenthetical_tail(repaired)
    repaired = tail_repaired if tail_repaired else _remove_unmatched_parenthesis_marks(repaired)
    repaired = _remove_unmatched_parenthesis_marks(repaired)
    repaired = _strip_dangling_label_suffix(repaired)
    if repaired and repaired != label:
        label = repaired
        methods.append("repair_label_quality")

    flags = ksa_label_quality_flags(original, label, concept_type)
    if "skill_suffix_stripped_to_generic" in flags:
        label = original
        methods.append("fallback_generic_skill_suffix")
        flags = []

    if len(label) < 2:
        label = original
        methods = []

    changed = label != original
    if not methods:
        methods.append("already_short_label")
    confidence = 0.78 if changed else 0.52
    if len(label) <= 28 and changed:
        confidence += 0.05
    if "collapse_long_enumeration" in methods:
        confidence -= 0.08
    if flags:
        confidence -= 0.10
    confidence = max(0.35, min(confidence, 0.88))
    return {
        "label_text": label,
        "source_method": "rule_based_short_label_candidate" if changed else "already_short_label",
        "confidence_score": confidence,
        "changed": changed,
        "method_details": ",".join(dict.fromkeys(methods)),
        "quality_flags": flags,
    }


def build_ksa_label_candidates(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    reset: bool = False,
    limit: int | None = None,
    machine_review: bool = False,
) -> dict[str, Any]:
    """Build candidate short representative labels for ontology concepts."""
    timestamp = now_utc()
    if reset:
        if major_code:
            conn.execute(
                f"""
                DELETE FROM ontology_concept_label_candidates
                WHERE review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                  AND (
                    source_scope_key LIKE ?
                    OR source_ksa_id IN (
                        SELECT label_ki.ksa_id
                        FROM ksa_items label_ki
                        JOIN competency_elements label_ce ON label_ce.element_id = label_ki.element_id
                        JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                        JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                        WHERE label_c.major_code = ?
                    )
                    OR source_atomic_id IN (
                        SELECT label_atom.atomic_id
                        FROM ksa_atomic_items label_atom
                        JOIN competency_elements label_ce ON label_ce.element_id = label_atom.element_id
                        JOIN competency_units label_cu ON label_cu.unit_code = label_ce.unit_code
                        JOIN classifications label_c ON label_c.classification_id = label_cu.classification_id
                        WHERE label_c.major_code = ?
                    )
                  )
                """,
                (f"{major_code}:%", major_code, major_code),
            )
        else:
            conn.execute(
                f"""
                DELETE FROM ontology_concept_label_candidates
                WHERE review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                """
            )

    before = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_label_candidates").fetchone()[0]
    )
    limit_clause = "LIMIT ?" if limit is not None else ""
    query_params: list[Any] = [major_code, major_code, major_code, major_code]
    if limit is not None:
        query_params.append(max(1, int(limit)))
    rows = conn.execute(
        f"""
        WITH source_candidates AS (
            SELECT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                atom.atomic_id AS source_atomic_id,
                atom.ksa_id AS source_ksa_id,
                atom.atom_text AS source_text,
                c_src.major_code,
                c_src.middle_code,
                c_src.small_code,
                c_src.sub_code,
                0 AS source_priority
            FROM ontology_concepts oc
            JOIN ksa_atomic_concept_links acl ON acl.concept_id = oc.concept_id
            JOIN ksa_atomic_items atom ON atom.atomic_id = acl.atomic_id
            JOIN competency_elements ce_src ON ce_src.element_id = atom.element_id
            JOIN competency_units cu_src ON cu_src.unit_code = ce_src.unit_code
            JOIN classifications c_src ON c_src.classification_id = cu_src.classification_id
            WHERE oc.concept_type IN ('knowledge', 'skill', 'attitude')
              AND TRIM(COALESCE(oc.concept_name, '')) <> ''
              AND (? IS NULL OR c_src.major_code = ?)
            UNION ALL
            SELECT
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                NULL AS source_atomic_id,
                ki.ksa_id AS source_ksa_id,
                ki.ksa_text_raw AS source_text,
                c_src.major_code,
                c_src.middle_code,
                c_src.small_code,
                c_src.sub_code,
                1 AS source_priority
            FROM ontology_concepts oc
            JOIN ksa_concept_links kcl ON kcl.concept_id = oc.concept_id
            JOIN ksa_items ki ON ki.ksa_id = kcl.ksa_id
            JOIN competency_elements ce_src ON ce_src.element_id = ki.element_id
            JOIN competency_units cu_src ON cu_src.unit_code = ce_src.unit_code
            JOIN classifications c_src ON c_src.classification_id = cu_src.classification_id
            WHERE oc.concept_type IN ('knowledge', 'skill', 'attitude')
              AND TRIM(COALESCE(oc.concept_name, '')) <> ''
              AND (? IS NULL OR c_src.major_code = ?)
        ),
        ranked_sources AS (
            SELECT
                source_candidates.*,
                COALESCE(major_code, '') || ':' || COALESCE(middle_code, '') || ':' ||
                    COALESCE(small_code, '') || ':' || COALESCE(sub_code, '') AS source_scope_key,
                ROW_NUMBER() OVER (
                    PARTITION BY concept_id, major_code, middle_code, small_code, sub_code
                    ORDER BY source_priority, LENGTH(COALESCE(source_text, '')),
                             COALESCE(source_atomic_id, source_ksa_id)
                ) AS source_rank
            FROM source_candidates
        )
        SELECT
            concept_id,
            concept_name,
            concept_type,
            source_atomic_id,
            source_ksa_id,
            source_text,
            source_scope_key
        FROM ranked_sources
        WHERE source_rank = 1
          AND TRIM(COALESCE(source_text, '')) <> ''
          AND NOT EXISTS (
            SELECT 1
            FROM ontology_concept_label_candidates trusted_label
            WHERE trusted_label.concept_id = ranked_sources.concept_id
              AND trusted_label.source_scope_key = ranked_sources.source_scope_key
              AND trusted_label.review_status IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
          )
        ORDER BY concept_id, source_scope_key
        {limit_clause}
        """,
        query_params,
    ).fetchall()

    processed = 0
    shortened = 0
    unchanged = 0
    for row in rows:
        source_text = normalize_spaces(row["source_text"] or row["concept_name"])
        candidate = compact_ksa_representative_label(source_text, row["concept_type"])
        label_text = normalize_spaces(candidate["label_text"])
        if not label_text:
            continue
        if row["source_ksa_id"] is None and row["source_atomic_id"] is None:
            continue
        if candidate["changed"]:
            shortened += 1
        else:
            unchanged += 1
        quality_flags = list(candidate.get("quality_flags") or [])
        review_status = "candidate"
        if machine_review:
            review_status = "needs_review" if quality_flags else "llm_reviewed"
        evidence_text = (
            f"concept_name: {normalize_spaces(row['concept_name'])} | "
            f"source_text: {source_text} | method_details: {candidate.get('method_details', '')}"
        )
        conn.execute(
            f"""
            INSERT INTO ontology_concept_label_candidates(
                concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                concept_type, source_text, label_text, normalized_label_key, label_role,
                source_method, candidate_rank, evidence_text, confidence_score,
                review_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'short_representative_label', ?, 1, ?, ?, ?, ?, ?)
            ON CONFLICT(concept_id, source_scope_key, source_method, normalized_label_key)
            DO UPDATE SET
                source_ksa_id = excluded.source_ksa_id,
                source_atomic_id = excluded.source_atomic_id,
                source_scope_key = excluded.source_scope_key,
                concept_type = excluded.concept_type,
                source_text = excluded.source_text,
                label_text = excluded.label_text,
                candidate_rank = excluded.candidate_rank,
                evidence_text = excluded.evidence_text,
                confidence_score = excluded.confidence_score,
                review_status = excluded.review_status,
                updated_at = excluded.updated_at
            WHERE ontology_concept_label_candidates.review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
            """,
            (
                row["concept_id"],
                row["source_ksa_id"],
                row["source_atomic_id"],
                row["source_scope_key"],
                row["concept_type"],
                source_text,
                label_text,
                normalize_concept_key(label_text),
                candidate["source_method"],
                evidence_text,
                candidate["confidence_score"],
                review_status,
                timestamp,
                timestamp,
            ),
        )
        processed += 1

    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ksa_label_candidates_built_at", timestamp),
    )
    conn.commit()
    after = int(
        conn.execute("SELECT COUNT(*) FROM ontology_concept_label_candidates").fetchone()[0]
    )
    by_method = {
        row["source_method"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT source_method, COUNT(*) AS count
            FROM ontology_concept_label_candidates
            GROUP BY source_method
            ORDER BY count DESC, source_method
            """
        ).fetchall()
    }
    by_type = {
        row["concept_type"]: int(row["count"])
        for row in conn.execute(
            """
            SELECT concept_type, COUNT(*) AS count
            FROM ontology_concept_label_candidates
            GROUP BY concept_type
            ORDER BY concept_type
            """
        ).fetchall()
    }
    human_reviewed = int(
        conn.execute(
            """
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE review_status = 'human_reviewed'
            """
        ).fetchone()[0]
    )
    trusted_preserved = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE review_status IN ({TRUSTED_LABEL_REVIEW_STATUS_SQL})
            """
        ).fetchone()[0]
    )
    machine_screened_preserved = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ontology_concept_label_candidates
            WHERE review_status IN ({MACHINE_SCREENED_LABEL_REVIEW_STATUS_SQL})
            """
        ).fetchone()[0]
    )
    return {
        "major_code": major_code,
        "label_candidates_before": before,
        "label_candidates_after": after,
        "label_candidates_inserted": max(0, after - before),
        "concepts_processed": processed,
        "shortened_label_candidates": shortened,
        "unchanged_label_candidates": unchanged,
        "label_candidates_by_method": by_method,
        "label_candidates_by_type": by_type,
        "human_reviewed_preserved": human_reviewed,
        "trusted_status_preserved": trusted_preserved,
        "machine_screened_status_preserved": machine_screened_preserved,
        "machine_review": machine_review,
        "note": (
            "Short representative labels are candidate review artifacts. "
            "Raw KSA, atomic KSA, and ontology_concepts.concept_name are not overwritten."
        ),
    }


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
    include_unlinked: bool = False,
) -> dict[str, Any]:
    """Build reviewable KSA significance candidates without filling concept definitions."""
    if apply_to_definitions:
        raise ValueError(
            "apply_to_definitions is disabled. KSA definition candidates must remain in "
            "ksa_meaning_candidates until a separate guarded definition-promotion workflow "
            "is explicitly approved."
        )
    timestamp = now_utc()
    params: list[Any] = []
    scope_clause = ""
    if major_code:
        scope_clause = "AND c.major_code = ?"
        params.append(major_code)
    if reset:
        if major_code:
            conn.execute(
                f"""
                DELETE FROM ksa_meaning_candidates
                WHERE review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
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
            conn.execute(
                f"""
                DELETE FROM ksa_meaning_candidates
                WHERE review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                """
            )

    before = int(conn.execute("SELECT COUNT(*) FROM ksa_meaning_candidates").fetchone()[0])
    limit_clause = "LIMIT ?" if limit is not None else ""
    include_unlinked_context = 1 if include_unlinked and major_code is None else 0
    query_params = [*params, *params, include_unlinked_context]
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
              AND ? = 1
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

    unlinked_definitions_cleared = 0
    if reset and apply_to_definitions and not include_unlinked and major_code is None:
        cur = conn.execute(
            f"""
            UPDATE ontology_concepts
            SET
                definition = NULL,
                definition_source = NULL,
                definition_status = 'missing',
                review_status = 'raw',
                updated_at = ?
            WHERE review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
              AND definition_source = 'ksa_meaning_candidates.term_definition_template'
              AND NOT EXISTS (
                  SELECT 1
                  FROM ksa_concept_links kcl
                  WHERE kcl.concept_id = ontology_concepts.concept_id
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM ksa_atomic_concept_links acl
                  WHERE acl.concept_id = ontology_concepts.concept_id
              )
            """,
            (timestamp,),
        )
        unlinked_definitions_cleared = int(cur.rowcount if cur.rowcount is not None else 0)

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
            f"""
            INSERT INTO ksa_meaning_candidates(
                concept_id, concept_type, meaning_role, meaning_text,
                source_method, evidence_text, unit_code, element_id,
                criteria_id, ksa_id, confidence_score, review_status,
                created_at, updated_at
            ) VALUES (?, ?, 'term_definition_candidate', ?, 'term_definition_template', ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
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
                review_status = CASE
                    WHEN TRIM(COALESCE(ksa_meaning_candidates.meaning_text, '')) <> TRIM(COALESCE(excluded.meaning_text, ''))
                    THEN 'candidate'
                    ELSE ksa_meaning_candidates.review_status
                END,
                updated_at = excluded.updated_at
            WHERE ksa_meaning_candidates.review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
            """,
            (
                row["concept_id"],
                row["concept_type"],
                _term_definition_text_for_concept(row),
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
        conn.execute(
            f"""
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
                review_status = CASE
                    WHEN TRIM(COALESCE(ksa_meaning_candidates.meaning_text, '')) <> TRIM(COALESCE(excluded.meaning_text, ''))
                    THEN 'candidate'
                    ELSE ksa_meaning_candidates.review_status
                END,
                updated_at = excluded.updated_at
            WHERE ksa_meaning_candidates.review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
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
                      AND kmc.source_method = 'term_definition_template'
                      {definition_scope}
                    ORDER BY kmc.confidence_score DESC, kmc.meaning_id
                    LIMIT 1
                ),
                definition_source = 'ksa_meaning_candidates.term_definition_template',
                definition_status = 'candidate',
                review_status = 'model_preprocessed',
                updated_at = ?
            WHERE concept_id IN (
                SELECT kmc.concept_id
                FROM ksa_meaning_candidates kmc
                WHERE kmc.review_status != 'rejected'
                  AND kmc.source_method = 'term_definition_template'
                  {definition_scope}
            )
              AND review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
              AND (
                  COALESCE(TRIM(definition), '') = ''
                  OR (
                      definition_status IN ('missing', 'candidate')
                      AND (
                          COALESCE(TRIM(definition_source), '') = ''
                          OR definition_source LIKE 'ksa_meaning_candidates.%'
                      )
                  )
              )
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
        "unlinked_context_included": bool(include_unlinked_context),
        "unlinked_definitions_cleared": unlinked_definitions_cleared,
        "definitions_applied": apply_to_definitions,
        "meanings_by_type": by_type,
        "human_reviewed_preserved": human_reviewed,
        "note": (
            "Term-definition candidates and task-context significance candidates are stored separately. "
            "Only term-definition candidates are applied to empty or previously machine-preprocessed "
            "ontology_concepts.definition rows, marked as candidate/model_preprocessed."
        ),
    }


def _ksa_meaning_review_scope_sql(major_code: str | None) -> tuple[str, list[Any]]:
    if not major_code:
        return "", []
    return (
        """
          AND unit_code IN (
              SELECT cu.unit_code
              FROM competency_units cu
              JOIN classifications c ON c.classification_id = cu.classification_id
              WHERE c.major_code = ?
          )
        """,
        [major_code],
    )


def _ksa_meaning_status_counts(
    conn: sqlite3.Connection,
    scope_sql: str = "",
    scope_params: list[Any] | None = None,
) -> dict[str, int]:
    rows = conn.execute(
        f"""
        SELECT review_status, COUNT(*) AS count
        FROM ksa_meaning_candidates
        WHERE 1 = 1
        {scope_sql}
        GROUP BY review_status
        ORDER BY review_status
        """,
        tuple(scope_params or []),
    ).fetchall()
    return {str(row["review_status"] or "unknown"): int(row["count"] or 0) for row in rows}


def machine_review_ksa_meaning_candidates(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
) -> dict[str, Any]:
    """Mark generated KSA meaning candidates as machine reviewed without human approval."""
    timestamp = now_utc()
    scope_sql, scope_params = _ksa_meaning_review_scope_sql(major_code)
    source_method_placeholders = ",".join(
        "?" for _ in KSA_MEANING_MACHINE_REVIEW_ELIGIBLE_SOURCE_METHODS
    )
    source_method_params = list(KSA_MEANING_MACHINE_REVIEW_ELIGIBLE_SOURCE_METHODS)
    locked_status_placeholders = ",".join("?" for _ in DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES)
    locked_status_params = list(DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES)
    before_counts = _ksa_meaning_status_counts(conn, scope_sql, scope_params)
    total_before = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE 1 = 1
            {scope_sql}
            """,
            tuple(scope_params),
        ).fetchone()[0]
    )
    eligible_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE review_status NOT IN ({locked_status_placeholders})
              AND source_method IN ({source_method_placeholders})
              {scope_sql}
            """,
            (*locked_status_params, *source_method_params, *scope_params),
        ).fetchone()[0]
    )
    task_context_condition = """
        source_method = 'task_context_template'
        AND TRIM(COALESCE(meaning_text, '')) <> ''
        AND TRIM(COALESCE(evidence_text, '')) <> ''
        AND unit_code IS NOT NULL
        AND element_id IS NOT NULL
        AND ksa_id IS NOT NULL
        AND (
            (concept_type = 'knowledge' AND meaning_role = 'task_knowledge_significance')
            OR (concept_type = 'skill' AND meaning_role = 'task_skill_significance')
            OR (concept_type = 'attitude' AND meaning_role = 'task_attitude_significance')
        )
    """
    conn.execute(
        f"""
        UPDATE ksa_meaning_candidates
        SET review_status = CASE
                WHEN {task_context_condition} THEN 'llm_reviewed'
                ELSE 'needs_review'
            END,
            updated_at = ?
        WHERE review_status NOT IN ({locked_status_placeholders})
          AND source_method IN ({source_method_placeholders})
          {scope_sql}
        """,
        (timestamp, *locked_status_params, *source_method_params, *scope_params),
    )
    conn.execute(
        "INSERT OR REPLACE INTO schema_metadata(key, value) VALUES (?, ?)",
        ("ksa_meaning_candidates_machine_reviewed_at", timestamp),
    )
    conn.commit()
    after_counts = _ksa_meaning_status_counts(conn, scope_sql, scope_params)
    locked_preserved = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM ksa_meaning_candidates
            WHERE review_status IN ({locked_status_placeholders})
              {scope_sql}
            """,
            (*locked_status_params, *scope_params),
        ).fetchone()[0]
    )
    return {
        "major_code": major_code,
        "machine_review": True,
        "meanings_before": total_before,
        "eligible_meanings_screened": eligible_count,
        "review_status_counts_before": before_counts,
        "review_status_counts_after": after_counts,
        "llm_reviewed_after": int(after_counts.get("llm_reviewed", 0)),
        "needs_review_after": int(after_counts.get("needs_review", 0)),
        "candidate_after": int(after_counts.get("candidate", 0)),
        "locked_status_preserved": locked_preserved,
        "human_review_status_updates": False,
        "definitions_updated": 0,
        "definition_promotion_attempted": False,
        "note": (
            "Task-context KSA meaning candidates with complete task evidence are marked "
            "llm_reviewed. Term-definition templates and unlinked fallback meanings remain "
            "needs_review because they are draft review aids, not human-approved definitions."
        ),
    }


def _promote_ksa_definitions_matching(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 5000,
    concept_ids: set[int] | None = None,
    promoted_review_status: str = "llm_reviewed",
) -> dict[str, int]:
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = 5000
    batch_size = max(1, batch_size)
    timestamp = now_utc()
    last_meaning_id = 0
    promoted = 0
    skipped_boilerplate = 0
    skipped_human_lock = 0
    processed_concepts: set[int] = set()

    while True:
        rows = conn.execute(
            """
            SELECT
                kmc.meaning_id,
                kmc.concept_id,
                kmc.concept_type,
                kmc.meaning_text,
                oc.concept_name,
                oc.definition_status,
                oc.review_status
            FROM ksa_meaning_candidates kmc
            JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
            WHERE kmc.meaning_id > ?
              AND kmc.meaning_role = 'term_definition_candidate'
              AND kmc.review_status = 'llm_reviewed'
            ORDER BY kmc.meaning_id
            LIMIT ?
            """,
            (last_meaning_id, batch_size),
        ).fetchall()
        if not rows:
            break

        update_params: list[tuple[str, str, str, int]] = []
        for row in rows:
            concept_id = int(row["concept_id"])
            if concept_ids is not None and concept_id not in concept_ids:
                continue
            if concept_id in processed_concepts:
                continue
            if row["definition_status"] == "defined" or row["review_status"] in DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES:
                skipped_human_lock += 1
                processed_concepts.add(concept_id)
                continue
            meaning_text = normalize_spaces(row["meaning_text"] or "")
            if _is_ksa_definition_boilerplate(row["concept_type"], row["concept_name"], meaning_text):
                skipped_boilerplate += 1
                continue
            update_params.append((row["meaning_text"], promoted_review_status, timestamp, concept_id))
            processed_concepts.add(concept_id)

        if update_params:
            before_changes = conn.total_changes
            conn.executemany(
                f"""
                UPDATE ontology_concepts
                SET
                    definition = ?,
                    definition_status = 'candidate',
                    definition_source = 'ksa_meaning_candidate_promotion',
                    review_status = ?,
                    updated_at = ?
                WHERE concept_id = ?
                  AND definition_status <> 'defined'
                  AND review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
                """,
                update_params,
            )
            promoted += int(conn.total_changes - before_changes)
            conn.commit()

        last_meaning_id = int(rows[-1]["meaning_id"])

    conn.commit()
    return {
        "promoted": promoted,
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_human_lock": skipped_human_lock,
    }


def promote_ksa_definitions(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 5000,
) -> dict[str, int]:
    return _promote_ksa_definitions_matching(
        conn,
        batch_size=batch_size,
        promoted_review_status="llm_reviewed",
    )


def promote_top_concepts_by_frequency(
    conn: sqlite3.Connection,
    *,
    top_n: int = 5000,
) -> dict[str, int]:
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        top_n = 5000
    top_n = max(0, top_n)
    if top_n == 0:
        return {
            "promoted": 0,
            "auto_promoted": 0,
            "skipped_boilerplate": 0,
            "skipped_human_lock": 0,
            "top_concepts_considered": 0,
        }

    top_rows = rank_concepts_by_recommendation_frequency(conn, limit=top_n)
    top_concept_ids = {int(row["concept_id"]) for row in top_rows}
    if not top_concept_ids:
        return {
            "promoted": 0,
            "auto_promoted": 0,
            "skipped_boilerplate": 0,
            "skipped_human_lock": 0,
            "top_concepts_considered": 0,
        }
    result = _promote_ksa_definitions_matching(
        conn,
        concept_ids=top_concept_ids,
        promoted_review_status="auto_promoted",
    )
    result["auto_promoted"] = result["promoted"]
    result["top_concepts_considered"] = len(top_concept_ids)
    return result


def rank_concepts_by_recommendation_frequency(
    conn: sqlite3.Connection,
    *,
    limit: int = 20000,
    high_frequency_override: int = 50,
) -> list[dict[str, Any]]:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 20000
    limit = max(1, limit)
    try:
        high_frequency_override = int(high_frequency_override)
    except (TypeError, ValueError):
        high_frequency_override = 50
    high_frequency_override = max(0, high_frequency_override)

    candidate_scan_limit = min(max(limit * 5, 1000), 250000)
    rows = conn.execute(
        f"""
        WITH concept_frequency AS (
            SELECT source_concept_id AS concept_id, COUNT(*) AS appearance_count
            FROM task_ksa_concept_relations
            GROUP BY source_concept_id
            UNION ALL
            SELECT target_concept_id AS concept_id, COUNT(*) AS appearance_count
            FROM task_ksa_concept_relations
            GROUP BY target_concept_id
        ),
        ranked_concepts AS (
            SELECT concept_id, SUM(appearance_count) AS appearance_count
            FROM concept_frequency
            GROUP BY concept_id
        )
        SELECT
            oc.concept_id,
            oc.concept_name,
            oc.concept_type,
            oc.definition_status,
            oc.review_status,
            ranked_concepts.appearance_count
        FROM ranked_concepts
        JOIN ontology_concepts oc ON oc.concept_id = ranked_concepts.concept_id
        WHERE oc.review_status NOT IN ({DEFINITION_PROMOTION_LOCKED_REVIEW_STATUS_SQL})
        ORDER BY ranked_concepts.appearance_count DESC, oc.concept_id
        LIMIT ?
        """,
        (candidate_scan_limit,),
    ).fetchall()
    ranked_rows = rows_to_dicts(rows)
    if not ranked_rows:
        return []

    candidate_ids = [int(row["concept_id"]) for row in ranked_rows]
    conn.execute("DROP TABLE IF EXISTS temp_rank_candidate_concepts")
    conn.execute("CREATE TEMP TABLE temp_rank_candidate_concepts(concept_id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO temp_rank_candidate_concepts(concept_id) VALUES (?)",
        [(concept_id,) for concept_id in candidate_ids],
    )
    issue_rows = conn.execute(
        """
        WITH duplicate_noncanonical AS (
            SELECT DISTINCT source_concept_id AS concept_id
            FROM ontology_concept_relations
            WHERE relation_type = 'same_as'
              AND review_status != 'rejected'
        )
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN ksa_concept_links kcl ON kcl.concept_id = tc.concept_id
        JOIN quality_issues qi
          ON qi.target_type = 'ksa'
         AND qi.target_id = CAST(kcl.ksa_id AS TEXT)
         AND qi.issue_type = 'short_ksa'
         AND qi.resolved_at IS NULL
        UNION
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN ksa_atomic_concept_links kacl ON kacl.concept_id = tc.concept_id
        JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
        JOIN quality_issues qi
          ON qi.target_type = 'ksa'
         AND qi.target_id = CAST(kai.ksa_id AS TEXT)
         AND qi.issue_type = 'short_ksa'
         AND qi.resolved_at IS NULL
        UNION
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN quality_issues qi
          ON qi.target_type IN ('concept', 'ontology_concept')
         AND qi.target_id = CAST(tc.concept_id AS TEXT)
         AND qi.issue_type = 'short_ksa'
         AND qi.resolved_at IS NULL
        UNION
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN duplicate_noncanonical dn ON dn.concept_id = tc.concept_id
        JOIN ksa_concept_links kcl ON kcl.concept_id = tc.concept_id
        JOIN quality_issues qi
          ON qi.target_type = 'ksa'
         AND qi.target_id = CAST(kcl.ksa_id AS TEXT)
         AND qi.issue_type = 'duplicate_text'
         AND qi.resolved_at IS NULL
        UNION
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN duplicate_noncanonical dn ON dn.concept_id = tc.concept_id
        JOIN ksa_atomic_concept_links kacl ON kacl.concept_id = tc.concept_id
        JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
        JOIN quality_issues qi
          ON qi.target_type = 'ksa'
         AND qi.target_id = CAST(kai.ksa_id AS TEXT)
         AND qi.issue_type = 'duplicate_text'
         AND qi.resolved_at IS NULL
        UNION
        SELECT DISTINCT tc.concept_id
        FROM temp_rank_candidate_concepts tc
        JOIN duplicate_noncanonical dn ON dn.concept_id = tc.concept_id
        JOIN quality_issues qi
          ON qi.target_type IN ('concept', 'ontology_concept')
         AND qi.target_id = CAST(tc.concept_id AS TEXT)
         AND qi.issue_type = 'duplicate_text'
         AND qi.resolved_at IS NULL
        """
    ).fetchall()
    issue_concept_ids = {int(row["concept_id"]) for row in issue_rows}
    conn.execute("DROP TABLE IF EXISTS temp_rank_candidate_concepts")

    filtered: list[dict[str, Any]] = []
    for row in ranked_rows:
        if (
            int(row["concept_id"]) in issue_concept_ids
            and int(row["appearance_count"] or 0) < high_frequency_override
        ):
            continue
        filtered.append(row)
        if len(filtered) >= limit:
            break
    return filtered


def _concept_quality_issue_counts(conn: sqlite3.Connection, concept_id: int) -> dict[str, int]:
    rows = conn.execute(
        """
        WITH concept_ksa AS (
            SELECT ksa_id
            FROM ksa_concept_links
            WHERE concept_id = ?
            UNION
            SELECT kai.ksa_id
            FROM ksa_atomic_concept_links kacl
            JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
            WHERE kacl.concept_id = ?
        ),
        concept_issues AS (
            SELECT qi.issue_type
            FROM concept_ksa ck
            JOIN quality_issues qi
              ON qi.target_type = 'ksa'
             AND qi.target_id = CAST(ck.ksa_id AS TEXT)
             AND qi.issue_type IN ('short_ksa', 'duplicate_text')
             AND qi.resolved_at IS NULL
            UNION ALL
            SELECT qi.issue_type
            FROM quality_issues qi
            WHERE qi.target_type IN ('concept', 'ontology_concept')
              AND qi.target_id = CAST(? AS TEXT)
              AND qi.issue_type IN ('short_ksa', 'duplicate_text')
              AND qi.resolved_at IS NULL
        )
        SELECT issue_type, COUNT(*) AS count
        FROM concept_issues
        GROUP BY issue_type
        ORDER BY count DESC, issue_type
        """,
        (concept_id, concept_id, concept_id),
    ).fetchall()
    return {str(row["issue_type"]): int(row["count"] or 0) for row in rows}


def _quality_issue_counts_for_concepts(
    conn: sqlite3.Connection,
    concept_ids: list[int],
) -> dict[int, dict[str, int]]:
    concept_ids = [int(concept_id) for concept_id in concept_ids if int(concept_id or 0)]
    if not concept_ids:
        return {}

    def batches(values: list[Any], size: int = 900) -> list[list[Any]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    concept_to_ksa_ids: dict[int, set[int]] = {concept_id: set() for concept_id in concept_ids}
    for batch in batches(concept_ids):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT concept_id, ksa_id
            FROM ksa_concept_links
            WHERE concept_id IN ({placeholders})
            """,
            batch,
        ).fetchall():
            concept_to_ksa_ids.setdefault(int(row["concept_id"]), set()).add(int(row["ksa_id"]))
        for row in conn.execute(
            f"""
            SELECT kacl.concept_id, kai.ksa_id
            FROM ksa_atomic_concept_links kacl
            JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
            WHERE kacl.concept_id IN ({placeholders})
            """,
            batch,
        ).fetchall():
            concept_to_ksa_ids.setdefault(int(row["concept_id"]), set()).add(int(row["ksa_id"]))

    ksa_to_concept_ids: dict[int, set[int]] = {}
    for concept_id, ksa_ids in concept_to_ksa_ids.items():
        for ksa_id in ksa_ids:
            ksa_to_concept_ids.setdefault(ksa_id, set()).add(concept_id)

    issue_counts: dict[int, dict[str, int]] = {concept_id: {} for concept_id in concept_ids}
    ksa_id_strings = [str(ksa_id) for ksa_id in sorted(ksa_to_concept_ids)]
    for batch in batches(ksa_id_strings):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT target_id, issue_type
            FROM quality_issues
            WHERE target_type = 'ksa'
              AND target_id IN ({placeholders})
              AND issue_type IN ('short_ksa', 'duplicate_text')
              AND resolved_at IS NULL
            """,
            batch,
        ).fetchall():
            try:
                ksa_id = int(row["target_id"])
            except (TypeError, ValueError):
                continue
            for concept_id in ksa_to_concept_ids.get(ksa_id, set()):
                bucket = issue_counts.setdefault(concept_id, {})
                issue_type = str(row["issue_type"])
                bucket[issue_type] = bucket.get(issue_type, 0) + 1

    concept_id_strings = [str(concept_id) for concept_id in concept_ids]
    for batch in batches(concept_id_strings):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT target_id, issue_type
            FROM quality_issues
            WHERE target_type IN ('concept', 'ontology_concept')
              AND target_id IN ({placeholders})
              AND issue_type IN ('short_ksa', 'duplicate_text')
              AND resolved_at IS NULL
            """,
            batch,
        ).fetchall():
            try:
                concept_id = int(row["target_id"])
            except (TypeError, ValueError):
                continue
            bucket = issue_counts.setdefault(concept_id, {})
            issue_type = str(row["issue_type"])
            bucket[issue_type] = bucket.get(issue_type, 0) + 1
    return {concept_id: counts for concept_id, counts in issue_counts.items() if counts}


def _build_definition_draft_candidate(
    concept_name: str,
    concept_type: str,
    evidence_samples: list[dict[str, Any]],
    breadth: dict[str, Any],
) -> dict[str, Any]:
    concept_name = normalize_spaces(concept_name)
    concept_type = normalize_spaces(concept_type).lower()
    evidence_samples = [sample for sample in evidence_samples if isinstance(sample, dict)]
    first_sample = evidence_samples[0] if evidence_samples else {}
    unit_name = normalize_spaces(str(first_sample.get("unit_name_raw") or ""))
    element_name = normalize_spaces(str(first_sample.get("element_name_raw") or ""))
    criteria_text = normalize_spaces(str(first_sample.get("criteria_text_raw") or ""))
    if len(criteria_text) > 140:
        criteria_text = criteria_text[:137].rstrip() + "..."

    type_role = {
        "knowledge": "업무 판단과 문제 해결에 필요한 기준, 절차, 법령, 사례를 이해하고 적용하기 위한 지식",
        "skill": "과업 수행에 필요한 절차, 도구, 자료를 분석·작성·운영하는 실행 능력",
        "attitude": "과업 수행 과정에서 안전, 품질, 협업, 책임성을 일관되게 유지하려는 태도",
    }.get(concept_type, "과업 수행에 필요한 역량")
    unit_count = int(breadth.get("unit_count") or 0)
    major_count = int(breadth.get("major_count") or 0)
    if unit_name and element_name:
        scope_phrase = f"{unit_name} / {element_name}"
    elif unit_name:
        scope_phrase = unit_name
    elif major_count >= 5:
        scope_phrase = "여러 NCS 직무 범위"
    else:
        scope_phrase = "관련 NCS 과업"

    if criteria_text:
        draft_definition = (
            f"'{concept_name}'는 {scope_phrase} 등에서 '{criteria_text}'와 같은 수행준거를 "
            f"충족하기 위해 필요한 {type_role}이다."
        )
    else:
        draft_definition = f"'{concept_name}'는 {scope_phrase}에서 과업 수행을 뒷받침하는 {type_role}이다."
    confidence = "low" if major_count >= 5 or unit_count >= 50 else "medium"
    if not evidence_samples:
        confidence = "low"
    return {
        "schema": "ncs_ksa_definition_draft_candidate_v1",
        "draft_definition": draft_definition,
        "source_method": "task_evidence_template_review_assist",
        "review_policy": "review_assist_only_not_a_human_decision",
        "confidence": confidence,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "evidence_basis": [
            {
                "criteria_id": sample.get("criteria_id"),
                "unit_code": sample.get("unit_code"),
                "unit_name_raw": sample.get("unit_name_raw"),
                "element_name_raw": sample.get("element_name_raw"),
            }
            for sample in evidence_samples[:3]
        ],
        "notes": [
            "Draft definition is generated from task evidence for reviewer convenience only.",
            "It must not be written to ontology_concepts.definition without an explicit human decision.",
        ],
    }


def build_ksa_definition_priority_review_pack(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    high_frequency_override: int = 50,
    evidence_limit: int = 3,
) -> dict[str, Any]:
    try:
        evidence_limit = int(evidence_limit)
    except (TypeError, ValueError):
        evidence_limit = 3
    evidence_limit = max(0, min(evidence_limit, 10))
    ranked_rows = rank_concepts_by_recommendation_frequency(
        conn,
        limit=limit,
        high_frequency_override=high_frequency_override,
    )
    concept_ids = [int(row["concept_id"]) for row in ranked_rows]
    if not concept_ids:
        return {
            "schema": "ncs_ksa_definition_priority_review_pack_v1",
            "generated_at": now_utc(),
            "ok": True,
            "limit": limit,
            "high_frequency_override": high_frequency_override,
            "evidence_limit": evidence_limit,
            "row_count": 0,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "rows": [],
            "notes": [
                "Read-only review pack for high-frequency KSA definition debt.",
                "Use as human review context only; it does not approve or write trusted review statuses.",
                "Broad high-frequency hubs may need a generic/specificity policy before automatic promotion.",
            ],
        }
    conn.execute("DROP TABLE IF EXISTS temp_ksa_definition_review_concepts")
    conn.execute("CREATE TEMP TABLE temp_ksa_definition_review_concepts(concept_id INTEGER PRIMARY KEY)")
    conn.executemany(
        "INSERT OR IGNORE INTO temp_ksa_definition_review_concepts(concept_id) VALUES (?)",
        [(concept_id,) for concept_id in concept_ids],
    )
    concept_details = {
        int(row["concept_id"]): dict(row)
        for row in conn.execute(
            """
            SELECT concept_id, definition, definition_source, definition_status, review_status
            FROM ontology_concepts
            WHERE concept_id IN (SELECT concept_id FROM temp_ksa_definition_review_concepts)
        """
        ).fetchall()
    }
    conn.execute("DROP TABLE IF EXISTS temp_ksa_definition_review_relation_hits")
    conn.execute(
        """
        CREATE TEMP TABLE temp_ksa_definition_review_relation_hits(
            concept_id INTEGER NOT NULL,
            criteria_id INTEGER NOT NULL,
            element_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            evidence_text TEXT,
            confidence_score REAL,
            relation_side TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO temp_ksa_definition_review_relation_hits(
            concept_id, criteria_id, element_id, relation_type, evidence_text, confidence_score, relation_side
        )
        SELECT
            rel.source_concept_id,
            rel.criteria_id,
            rel.element_id,
            rel.relation_type,
            rel.evidence_text,
            rel.confidence_score,
            'source'
        FROM task_ksa_concept_relations rel
        JOIN temp_ksa_definition_review_concepts tc ON tc.concept_id = rel.source_concept_id
        """
    )
    conn.execute(
        """
        INSERT INTO temp_ksa_definition_review_relation_hits(
            concept_id, criteria_id, element_id, relation_type, evidence_text, confidence_score, relation_side
        )
        SELECT
            rel.target_concept_id,
            rel.criteria_id,
            rel.element_id,
            rel.relation_type,
            rel.evidence_text,
            rel.confidence_score,
            'target'
        FROM task_ksa_concept_relations rel
        JOIN temp_ksa_definition_review_concepts tc ON tc.concept_id = rel.target_concept_id
        """
    )
    conn.execute(
        "CREATE INDEX temp_idx_ksa_definition_review_relation_concept "
        "ON temp_ksa_definition_review_relation_hits(concept_id)"
    )
    conn.execute(
        "CREATE INDEX temp_idx_ksa_definition_review_relation_side "
        "ON temp_ksa_definition_review_relation_hits(relation_side, concept_id)"
    )
    conn.execute(
        "CREATE INDEX temp_idx_ksa_definition_review_relation_element "
        "ON temp_ksa_definition_review_relation_hits(element_id, concept_id)"
    )
    source_counts = {
        int(row["concept_id"]): int(row["source_count"] or 0)
        for row in conn.execute(
            """
            SELECT concept_id, COUNT(*) AS source_count
            FROM temp_ksa_definition_review_relation_hits
            WHERE relation_side = 'source'
            GROUP BY concept_id
            """
        ).fetchall()
    }
    target_counts = {
        int(row["concept_id"]): int(row["target_count"] or 0)
        for row in conn.execute(
            """
            SELECT concept_id, COUNT(*) AS target_count
            FROM temp_ksa_definition_review_relation_hits
            WHERE relation_side = 'target'
            GROUP BY concept_id
            """
        ).fetchall()
    }
    relation_type_counts = {
        int(row["concept_id"]): int(row["relation_type_count"] or 0)
        for row in conn.execute(
            """
            SELECT concept_id, COUNT(DISTINCT relation_type) AS relation_type_count
            FROM temp_ksa_definition_review_relation_hits
            GROUP BY concept_id
            """
        ).fetchall()
    }
    breadth_by_concept = {
        int(row["concept_id"]): {
            "unit_count": int(row["unit_count"] or 0),
            "major_count": int(row["major_count"] or 0),
            "sub_scope_count": int(row["sub_scope_count"] or 0),
        }
        for row in conn.execute(
            """
            SELECT
                hits.concept_id,
                COUNT(DISTINCT cu.unit_code) AS unit_count,
                COUNT(DISTINCT c.major_code) AS major_count,
                COUNT(DISTINCT c.major_code || ':' || c.middle_code || ':' || c.small_code || ':' || c.sub_code) AS sub_scope_count
            FROM (
                SELECT DISTINCT concept_id, element_id
                FROM temp_ksa_definition_review_relation_hits
            ) hits
            JOIN competency_elements ce ON ce.element_id = hits.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
            GROUP BY hits.concept_id
            """
        ).fetchall()
    }
    quality_issue_counts_by_concept = _quality_issue_counts_for_concepts(conn, concept_ids)
    candidate_rows_by_concept: dict[int, list[dict[str, Any]]] = {}
    candidate_screening_by_concept: dict[int, dict[str, Any]] = {}
    for row in rows_to_dicts(
        conn.execute(
            """
            SELECT
                kmc.meaning_id,
                kmc.concept_id,
                kmc.concept_type,
                kmc.meaning_text,
                kmc.confidence_score,
                kmc.review_status,
                kmc.source_method,
                oc.concept_name
            FROM ksa_meaning_candidates kmc
            JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
            WHERE kmc.concept_id IN (SELECT concept_id FROM temp_ksa_definition_review_concepts)
              AND kmc.meaning_role = 'term_definition_candidate'
            ORDER BY
                kmc.concept_id,
                CASE kmc.review_status
                    WHEN 'human_reviewed' THEN 0
                    WHEN 'llm_reviewed' THEN 1
                    WHEN 'candidate' THEN 2
                    ELSE 3
                END,
                kmc.confidence_score DESC,
                kmc.meaning_id
            """
        ).fetchall()
    ):
        concept_id = int(row["concept_id"])
        meaning_text = normalize_spaces(str(row.get("meaning_text") or ""))
        boilerplate_candidate = _is_ksa_definition_boilerplate(
            str(row.get("concept_type") or ""),
            str(row.get("concept_name") or ""),
            meaning_text,
        )
        promotion_eligible = (
            str(row.get("review_status") or "") == "llm_reviewed"
            and not boilerplate_candidate
        )
        screening = candidate_screening_by_concept.setdefault(
            concept_id,
            {
                "candidate_count": 0,
                "promotion_eligible_count": 0,
                "boilerplate_candidate_count": 0,
                "source_method_counts": {},
                "review_status_counts": {},
            },
        )
        screening["candidate_count"] += 1
        screening["promotion_eligible_count"] += int(promotion_eligible)
        screening["boilerplate_candidate_count"] += int(boilerplate_candidate)
        source_method_counts = screening["source_method_counts"]
        source_method = str(row.get("source_method") or "")
        source_method_counts[source_method] = int(source_method_counts.get(source_method, 0)) + 1
        review_status_counts = screening["review_status_counts"]
        review_status = str(row.get("review_status") or "")
        review_status_counts[review_status] = int(review_status_counts.get(review_status, 0)) + 1
        bucket = candidate_rows_by_concept.setdefault(concept_id, [])
        if len(bucket) < evidence_limit:
            candidate_payload = {
                key: value
                for key, value in row.items()
                if key not in {"concept_id", "concept_name"}
            }
            candidate_payload["boilerplate_candidate"] = boilerplate_candidate
            candidate_payload["promotion_eligible"] = promotion_eligible
            candidate_payload["promotion_block_reason"] = (
                "generated_template_boilerplate"
                if boilerplate_candidate
                else "review_status_not_llm_reviewed"
                if str(row.get("review_status") or "") != "llm_reviewed"
                else None
            )
            bucket.append(candidate_payload)
    evidence_samples_by_concept: dict[int, list[dict[str, Any]]] = {}
    for row in rows_to_dicts(
        conn.execute(
            """
            WITH ranked_samples AS (
                SELECT
                    hits.concept_id,
                    hits.criteria_id,
                    cu.unit_code,
                    cu.unit_name_raw,
                    ce.element_name_raw,
                    pc.criteria_text_raw,
                    hits.relation_type,
                    hits.evidence_text,
                    hits.confidence_score,
                    ROW_NUMBER() OVER (
                        PARTITION BY hits.concept_id
                        ORDER BY hits.confidence_score DESC, hits.criteria_id
                    ) AS rn
                FROM (
                    SELECT DISTINCT
                        concept_id,
                        criteria_id,
                        element_id,
                        relation_type,
                        evidence_text,
                        confidence_score
                    FROM temp_ksa_definition_review_relation_hits
                ) hits
                JOIN performance_criteria pc ON pc.criteria_id = hits.criteria_id
                JOIN competency_elements ce ON ce.element_id = hits.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
            )
            SELECT *
            FROM ranked_samples
            WHERE rn <= ?
            ORDER BY concept_id, rn
            """,
            (evidence_limit,),
        ).fetchall()
    ):
        bucket = evidence_samples_by_concept.setdefault(int(row["concept_id"]), [])
        bucket.append({key: value for key, value in row.items() if key not in {"concept_id", "rn"}})
    rows: list[dict[str, Any]] = []
    for ranked in ranked_rows:
        concept_id = int(ranked["concept_id"])
        concept_row = concept_details.get(concept_id) or {}
        definition = concept_row.get("definition")
        definition_source = concept_row.get("definition_source")
        breadth = breadth_by_concept.get(concept_id, {})
        task_evidence_samples = evidence_samples_by_concept.get(concept_id, [])
        draft_definition_candidate = _build_definition_draft_candidate(
            str(ranked.get("concept_name") or ""),
            str(ranked.get("concept_type") or ""),
            task_evidence_samples,
            breadth,
        )
        template_definition = bool(
            definition_source
            and str(definition_source).startswith("ksa_meaning_candidates.term_definition_template")
        )
        quality_issue_counts = quality_issue_counts_by_concept.get(concept_id, {})
        candidate_screening = candidate_screening_by_concept.get(
            concept_id,
            {
                "candidate_count": 0,
                "promotion_eligible_count": 0,
                "boilerplate_candidate_count": 0,
                "source_method_counts": {},
                "review_status_counts": {},
            },
        )
        if not candidate_screening.get("candidate_count"):
            recommended_review_action = "write_manual_definition"
        elif candidate_screening.get("promotion_eligible_count"):
            recommended_review_action = "review_promotion_candidate"
        elif candidate_screening.get("boilerplate_candidate_count") == candidate_screening.get("candidate_count"):
            recommended_review_action = "draft_for_human_review_only"
        else:
            recommended_review_action = "inspect_definition_candidate_status"
        rows.append(
            {
                **ranked,
                "definition": definition,
                "definition_source": definition_source,
                "boilerplate_definition": bool(
                    definition
                    and _is_ksa_definition_boilerplate(
                        str(ranked.get("concept_type") or ""),
                        str(ranked.get("concept_name") or ""),
                        str(definition),
                    )
                ),
                "template_definition": template_definition,
                "breadth": {
                    "unit_count": int(breadth.get("unit_count") or 0),
                    "major_count": int(breadth.get("major_count") or 0),
                    "sub_scope_count": int(breadth.get("sub_scope_count") or 0),
                },
                "relation_counts": {
                    "source_count": source_counts.get(concept_id, 0),
                    "target_count": target_counts.get(concept_id, 0),
                    "relation_type_count": relation_type_counts.get(concept_id, 0),
                },
                "quality_issue_counts": quality_issue_counts,
                "definition_candidate_screening": candidate_screening,
                "recommended_review_action": recommended_review_action,
                "term_definition_candidates": candidate_rows_by_concept.get(concept_id, []),
                "task_evidence_samples": task_evidence_samples,
                "draft_definition_candidate": draft_definition_candidate,
                "review_focus": [
                    item
                    for item, enabled in (
                        ("definition_template_or_boilerplate", template_definition or not definition),
                        ("no_promotable_definition_candidate", not candidate_screening.get("promotion_eligible_count")),
                        ("high_breadth_generic_hub", int(breadth.get("major_count") or 0) >= 5),
                        ("quality_issue_present", bool(quality_issue_counts)),
                    )
                    if enabled
                ],
                "status_update_allowed": False,
            }
        )
    conn.execute("DROP TABLE IF EXISTS temp_ksa_definition_review_relation_hits")
    conn.execute("DROP TABLE IF EXISTS temp_ksa_definition_review_concepts")
    return {
        "schema": "ncs_ksa_definition_priority_review_pack_v1",
        "generated_at": now_utc(),
        "ok": True,
        "limit": limit,
        "high_frequency_override": high_frequency_override,
        "evidence_limit": evidence_limit,
        "row_count": len(rows),
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "rows": rows,
        "notes": [
            "Read-only review pack for high-frequency KSA definition debt.",
            "Use as human review context only; it does not approve or write trusted review statuses.",
            "Broad high-frequency hubs may need a generic/specificity policy before automatic promotion.",
        ],
    }


def write_ksa_definition_priority_review_pack_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# KSA Definition Priority Review Pack",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- row_count: `{report.get('row_count')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "| Concept ID | Concept | Type | Appearances | Units | Majors | Definition Status | Review Status | Focus |",
        "| ---: | --- | --- | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for row in report.get("rows") or []:
        breadth = row.get("breadth") or {}
        lines.append(
            "| {concept_id} | {concept_name} | {concept_type} | {appearance_count} | {unit_count} | {major_count} | {definition_status} | {review_status} | {focus} |".format(
                concept_id=int(row.get("concept_id") or 0),
                concept_name=_md_cell(row.get("concept_name")),
                concept_type=_md_cell(row.get("concept_type")),
                appearance_count=int(row.get("appearance_count") or 0),
                unit_count=int(breadth.get("unit_count") or 0),
                major_count=int(breadth.get("major_count") or 0),
                definition_status=_md_cell(row.get("definition_status")),
                review_status=_md_cell(row.get("review_status")),
                focus=_md_cell(", ".join(row.get("review_focus") or [])),
            )
        )
    for row in report.get("rows") or []:
        lines.extend(
            [
                "",
                f"## {int(row.get('concept_id') or 0)} {_md_cell(row.get('concept_name'))}",
                "",
                f"- recommended_review_action: `{_md_cell(row.get('recommended_review_action'))}`",
                f"- definition_candidate_screening: `{_md_cell(row.get('definition_candidate_screening'))}`",
                f"- definition_source: `{_md_cell(row.get('definition_source'))}`",
                f"- boilerplate_definition: `{bool(row.get('boilerplate_definition'))}`",
                f"- template_definition: `{bool(row.get('template_definition'))}`",
                f"- quality_issue_counts: `{_md_cell(row.get('quality_issue_counts'))}`",
                "",
                "Draft definition candidate:",
            ]
        )
        draft = row.get("draft_definition_candidate") if isinstance(row.get("draft_definition_candidate"), dict) else {}
        if draft.get("draft_definition"):
            lines.extend(
                [
                    f"- definition: {_md_cell(draft.get('draft_definition'))}",
                    f"- confidence: `{_md_cell(draft.get('confidence'))}`",
                    f"- review_policy: `{_md_cell(draft.get('review_policy'))}`",
                    f"- status_update_allowed: `{draft.get('status_update_allowed')}`",
                    f"- db_writes: `{draft.get('db_writes')}`",
                    f"- approval_claim: `{draft.get('approval_claim')}`",
                    "",
                ]
            )
        else:
            lines.extend(["- none", ""])
        lines.extend(
            [
                "Definition candidates:",
            ]
        )
        for candidate in row.get("term_definition_candidates") or []:
            lines.append(
                f"- `{candidate.get('meaning_id')}` {_md_cell(candidate.get('review_status'))} eligible={bool(candidate.get('promotion_eligible'))} block={_md_cell(candidate.get('promotion_block_reason'))}: {_md_cell(candidate.get('meaning_text'))}"
            )
        if not row.get("term_definition_candidates"):
            lines.append("- none")
        lines.append("")
        lines.append("Evidence samples:")
        for sample in row.get("task_evidence_samples") or []:
            lines.append(
                f"- `{sample.get('unit_code')}` {_md_cell(sample.get('unit_name_raw'))} / {_md_cell(sample.get('criteria_text_raw'))}"
            )
        if not row.get("task_evidence_samples"):
            lines.append("- none")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


KSA_DEFINITION_PRIORITY_REVIEW_CSV_FIELDS = [
    "schema",
    "concept_id",
    "concept_name",
    "concept_type",
    "appearance_count",
    "unit_count",
    "major_count",
    "definition_status",
    "review_status",
    "current_definition",
    "definition_source",
    "boilerplate_definition",
    "recommended_review_action",
    "review_focus",
    "draft_definition",
    "draft_confidence",
    "draft_review_policy",
    "evidence_summary",
    "decision",
    "approved_definition",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "human_decision_required",
    "status_update_allowed",
    "db_writes",
    "approval_claim",
]


def _operator_csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def write_ksa_definition_priority_review_pack_csv(report: dict[str, Any], out_path: Path) -> dict[str, Any]:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record_count = 0
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=KSA_DEFINITION_PRIORITY_REVIEW_CSV_FIELDS)
        writer.writeheader()
        for row in report.get("rows") or []:
            if not isinstance(row, dict):
                continue
            breadth = row.get("breadth") if isinstance(row.get("breadth"), dict) else {}
            draft = (
                row.get("draft_definition_candidate")
                if isinstance(row.get("draft_definition_candidate"), dict)
                else {}
            )
            evidence_samples = [
                sample for sample in row.get("task_evidence_samples") or [] if isinstance(sample, dict)
            ]
            evidence_summary = " | ".join(
                normalize_spaces(
                    f"{sample.get('unit_code') or ''} {sample.get('unit_name_raw') or ''} / "
                    f"{sample.get('criteria_text_raw') or ''}"
                )
                for sample in evidence_samples[:3]
            )
            csv_row = {
                "schema": "ncs_ksa_definition_priority_review_decision_row_v1",
                "concept_id": row.get("concept_id"),
                "concept_name": row.get("concept_name"),
                "concept_type": row.get("concept_type"),
                "appearance_count": row.get("appearance_count"),
                "unit_count": breadth.get("unit_count"),
                "major_count": breadth.get("major_count"),
                "definition_status": row.get("definition_status"),
                "review_status": row.get("review_status"),
                "current_definition": row.get("definition"),
                "definition_source": row.get("definition_source"),
                "boilerplate_definition": bool(row.get("boilerplate_definition")),
                "recommended_review_action": row.get("recommended_review_action"),
                "review_focus": ", ".join(str(item) for item in row.get("review_focus") or []),
                "draft_definition": draft.get("draft_definition"),
                "draft_confidence": draft.get("confidence"),
                "draft_review_policy": draft.get("review_policy"),
                "evidence_summary": evidence_summary,
                "decision": "",
                "approved_definition": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "human_decision_required": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            writer.writerow(
                {
                    field: _operator_csv_cell(csv_row.get(field))
                    for field in KSA_DEFINITION_PRIORITY_REVIEW_CSV_FIELDS
                }
            )
            record_count += 1
    return {
        "ok": True,
        "schema": "ncs_ksa_definition_priority_review_csv_summary_v1",
        "csv_path": str(out_path),
        "record_count": record_count,
        "decision_blank_count": record_count,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


KSA_DEFINITION_REVIEW_DECISION_VALUES = {
    "",
    "approve_definition",
    "reject_draft",
    "needs_revision",
    "defer",
}


def _csv_false(value: Any) -> bool:
    return str(value if value is not None else "").strip().lower() in {"false", "0", ""}


def _load_json_source(path: Path | None, expected_schema: str, source_name: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if path is None:
        return None, []
    findings: list[dict[str, Any]] = []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - defensive artifact guard
        return None, [
            {
                "severity": "error",
                "code": f"{source_name}_read_failed",
                "message": str(exc),
            }
        ]
    if not isinstance(payload, dict):
        return None, [
            {
                "severity": "error",
                "code": f"{source_name}_not_object",
                "message": f"{source_name} must be a JSON object.",
            }
        ]
    if payload.get("schema") != expected_schema:
        findings.append(
            {
                "severity": "error",
                "code": f"{source_name}_schema_mismatch",
                "expected_schema": expected_schema,
                "actual_schema": payload.get("schema"),
            }
        )
    for flag in ("status_update_allowed", "db_writes", "approval_claim"):
        if payload.get(flag) is not False:
            findings.append(
                {
                    "severity": "error",
                    "code": f"{source_name}_{flag}_not_false",
                    "actual": payload.get(flag),
                }
            )
    return payload, findings


def audit_ksa_definition_review_decision_csv(
    csv_path: Path,
    *,
    source_packet_path: Path | None = None,
    source_review_pack_path: Path | None = None,
) -> dict[str, Any]:
    packet, source_findings = _load_json_source(
        source_packet_path,
        "ncs_ksa_definition_review_operator_packet_v1",
        "source_packet",
    )
    review_pack, review_findings = _load_json_source(
        source_review_pack_path,
        "ncs_ksa_definition_priority_review_pack_v1",
        "source_review_pack",
    )
    source_findings.extend(review_findings)
    review_pack_concept_ids: set[int] = set()
    if isinstance(review_pack, dict):
        for row in review_pack.get("rows") or []:
            if isinstance(row, dict) and row.get("concept_id") is not None:
                try:
                    review_pack_concept_ids.add(int(row["concept_id"]))
                except (TypeError, ValueError):
                    continue
    rows: list[dict[str, Any]] = []
    try:
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            csv_rows = list(csv.DictReader(handle))
    except Exception as exc:  # pragma: no cover - defensive artifact guard
        return {
            "schema": "ncs_ksa_definition_review_decision_audit_v1",
            "ok": False,
            "source_csv": str(csv_path),
            "generated_at": now_utc(),
            "report_only": True,
            "human_decision_required": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "acceptance_claim": False,
            "trusted_status_write_allowed": False,
            "error": {"code": "csv_read_failed", "message": str(exc)},
            "rows": [],
            "findings": source_findings,
        }
    invalid_count = 0
    pending_count = 0
    completed_count = 0
    unsafe_flag_count = 0
    source_mismatch_count = 0
    action_eligible_count = 0
    for row_number, raw in enumerate(csv_rows, start=2):
        issues: list[str] = []
        if str(raw.get("schema") or "") != "ncs_ksa_definition_priority_review_decision_row_v1":
            issues.append("schema_mismatch")
        for flag in ("status_update_allowed", "db_writes", "approval_claim"):
            if not _csv_false(raw.get(flag)):
                issues.append(f"{flag}_not_false")
                unsafe_flag_count += 1
        decision = normalize_spaces(str(raw.get("decision") or "")).lower()
        if decision in HUMAN_TRUSTED_LABEL_REVIEW_STATUSES:
            issues.append("trusted_status_used_as_decision")
        if decision not in KSA_DEFINITION_REVIEW_DECISION_VALUES:
            issues.append("invalid_decision")
        concept_id: int | None = None
        try:
            concept_id = int(str(raw.get("concept_id") or "").strip())
        except ValueError:
            issues.append("invalid_concept_id")
        if review_pack_concept_ids and concept_id not in review_pack_concept_ids:
            issues.append("concept_not_in_source_review_pack")
            source_mismatch_count += 1
        approved_definition = normalize_spaces(str(raw.get("approved_definition") or ""))
        reviewer_id = normalize_spaces(str(raw.get("reviewer_id") or ""))
        reviewed_at = normalize_spaces(str(raw.get("reviewed_at") or ""))
        rationale = normalize_spaces(str(raw.get("rationale") or ""))
        completed = bool(decision)
        if completed:
            completed_count += 1
            if not reviewer_id:
                issues.append("missing_reviewer_id")
            if not reviewed_at:
                issues.append("missing_reviewed_at")
            if not rationale:
                issues.append("missing_rationale")
        else:
            pending_count += 1
        if decision == "approve_definition" and not approved_definition:
            issues.append("missing_approved_definition")
        valid = not issues
        if not valid:
            invalid_count += 1
        action_eligible = valid and decision == "approve_definition"
        if action_eligible:
            action_eligible_count += 1
        rows.append(
            {
                "row_number": row_number,
                "concept_id": concept_id,
                "concept_name": raw.get("concept_name"),
                "concept_type": raw.get("concept_type"),
                "decision": decision,
                "approved_definition": approved_definition,
                "reviewer_id": reviewer_id,
                "reviewed_at": reviewed_at,
                "rationale": rationale,
                "completed": completed,
                "valid": valid,
                "action_eligible": action_eligible,
                "issues": issues,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    source_error_count = sum(1 for finding in source_findings if finding.get("severity") == "error")
    ok = invalid_count == 0 and unsafe_flag_count == 0 and source_error_count == 0
    next_step = (
        "fix_invalid_decision_rows"
        if invalid_count or source_error_count
        else "review_readonly_action_plan"
        if action_eligible_count
        else "fill_definition_review_decision_fields"
        if pending_count
        else "no_actionable_definition_updates"
    )
    return {
        "schema": "ncs_ksa_definition_review_decision_audit_v1",
        "ok": ok,
        "source_csv": str(csv_path),
        "source_packet": str(source_packet_path) if source_packet_path else None,
        "source_review_pack": str(source_review_pack_path) if source_review_pack_path else None,
        "generated_at": now_utc(),
        "report_only": True,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "trusted_status_write_allowed": False,
        "row_count": len(rows),
        "completed_decision_count": completed_count,
        "pending_decision_count": pending_count,
        "invalid_decision_count": invalid_count,
        "unsafe_flag_count": unsafe_flag_count,
        "source_mismatch_count": source_mismatch_count,
        "action_eligible_count": action_eligible_count,
        "next_step": next_step,
        "source_contract": {
            "packet_schema_ok": packet is None or packet.get("schema") == "ncs_ksa_definition_review_operator_packet_v1",
            "review_pack_schema_ok": review_pack is None or review_pack.get("schema") == "ncs_ksa_definition_priority_review_pack_v1",
            "source_error_count": source_error_count,
        },
        "findings": source_findings,
        "rows": rows,
    }


def write_ksa_definition_review_decision_audit_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# KSA Definition Review Decision Audit",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- row_count: `{report.get('row_count')}`",
        f"- completed_decision_count: `{report.get('completed_decision_count')}`",
        f"- pending_decision_count: `{report.get('pending_decision_count')}`",
        f"- invalid_decision_count: `{report.get('invalid_decision_count')}`",
        f"- action_eligible_count: `{report.get('action_eligible_count')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- acceptance_claim: `{report.get('acceptance_claim')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        f"- next_step: `{_md_cell(report.get('next_step'))}`",
        "",
        "| Row | Concept | Decision | Valid | Action Eligible | Issues |",
        "| ---: | --- | --- | --- | --- | --- |",
    ]
    for row in report.get("rows") or []:
        if not isinstance(row, dict):
            continue
        lines.append(
            "| {row_number} | `{concept_id}` {concept_name} | `{decision}` | `{valid}` | `{action_eligible}` | {issues} |".format(
                row_number=int(row.get("row_number") or 0),
                concept_id=_md_cell(row.get("concept_id")),
                concept_name=_md_cell(row.get("concept_name")),
                decision=_md_cell(row.get("decision")),
                valid=row.get("valid"),
                action_eligible=row.get("action_eligible"),
                issues=_md_cell(", ".join(row.get("issues") or [])),
            )
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_ksa_definition_review_decision_action_plan(
    csv_path: Path,
    *,
    source_packet_path: Path | None = None,
    source_review_pack_path: Path | None = None,
) -> dict[str, Any]:
    audit = audit_ksa_definition_review_decision_csv(
        csv_path,
        source_packet_path=source_packet_path,
        source_review_pack_path=source_review_pack_path,
    )
    actions: list[dict[str, Any]] = []
    if audit.get("ok"):
        for row in audit.get("rows") or []:
            if not isinstance(row, dict) or not row.get("action_eligible"):
                continue
            actions.append(
                {
                    "action_type": "prepare_definition_update",
                    "concept_id": row.get("concept_id"),
                    "concept_name": row.get("concept_name"),
                    "approved_definition": row.get("approved_definition"),
                    "reviewer_id": row.get("reviewer_id"),
                    "reviewed_at": row.get("reviewed_at"),
                    "rationale": row.get("rationale"),
                    "target_fields": {
                        "definition": row.get("approved_definition"),
                        "definition_status": "defined",
                        "definition_source": "human_definition_review_csv",
                        "review_status": "human_reviewed",
                    },
                    "requires_explicit_operator_apply": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                }
            )
    return {
        "schema": "ncs_ksa_definition_review_action_plan_v1",
        "ok": bool(audit.get("ok")),
        "source_csv": str(csv_path),
        "source_packet": str(source_packet_path) if source_packet_path else None,
        "source_review_pack": str(source_review_pack_path) if source_review_pack_path else None,
        "generated_at": now_utc(),
        "report_only": True,
        "human_decision_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "trusted_status_write_allowed": False,
        "blocked_by_invalid_audit": not bool(audit.get("ok")),
        "audit_summary": {
            "row_count": audit.get("row_count"),
            "completed_decision_count": audit.get("completed_decision_count"),
            "pending_decision_count": audit.get("pending_decision_count"),
            "invalid_decision_count": audit.get("invalid_decision_count"),
            "action_eligible_count": audit.get("action_eligible_count"),
            "next_step": audit.get("next_step"),
        },
        "action_count": len(actions),
        "actions": actions,
        "operator_steps": [
            "Review this plan before any guarded operator apply step.",
            "This plan is read-only and does not write ontology_concepts.",
            "Only rows with approve_definition and complete human fields become prepared actions.",
            "Do not mutate ksa_items.ksa_text_raw.",
        ],
    }


def write_ksa_definition_review_action_plan_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    audit_summary = report.get("audit_summary") if isinstance(report.get("audit_summary"), dict) else {}
    lines = [
        "# KSA Definition Review Action Plan",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- action_count: `{report.get('action_count')}`",
        f"- blocked_by_invalid_audit: `{report.get('blocked_by_invalid_audit')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- acceptance_claim: `{report.get('acceptance_claim')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        f"- pending_decision_count: `{audit_summary.get('pending_decision_count')}`",
        f"- invalid_decision_count: `{audit_summary.get('invalid_decision_count')}`",
        "",
        "| # | Concept | Definition Status | Review Status | Requires Operator Apply |",
        "| ---: | --- | --- | --- | --- |",
    ]
    for index, action in enumerate(report.get("actions") or [], start=1):
        if not isinstance(action, dict):
            continue
        target_fields = action.get("target_fields") if isinstance(action.get("target_fields"), dict) else {}
        lines.append(
            "| {index} | `{concept_id}` {concept_name} | `{definition_status}` | `{review_status}` | `{requires_apply}` |".format(
                index=index,
                concept_id=_md_cell(action.get("concept_id")),
                concept_name=_md_cell(action.get("concept_name")),
                definition_status=_md_cell(target_fields.get("definition_status")),
                review_status=_md_cell(target_fields.get("review_status")),
                requires_apply=action.get("requires_explicit_operator_apply"),
            )
        )
    if not report.get("actions"):
        lines.append("| 0 | none |  |  |  |")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_duplicate_concept_relations(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    concept_rows = conn.execute(
        """
        SELECT concept_id, concept_name, concept_type, definition_status, review_status
        FROM ontology_concepts
        WHERE TRIM(COALESCE(concept_name, '')) <> ''
        ORDER BY concept_id
        """
    ).fetchall()
    concept_meta: dict[int, dict[str, str]] = {}
    groups: dict[tuple[str, str], list[int]] = {}
    for row in concept_rows:
        concept_id = int(row["concept_id"])
        concept_meta[concept_id] = {
            "definition_status": str(row["definition_status"] or ""),
            "review_status": str(row["review_status"] or ""),
        }
        key = normalize_concept_key(row["concept_name"])
        if not key:
            continue
        groups.setdefault((str(row["concept_type"] or ""), key), []).append(concept_id)
    duplicate_groups = [ids for ids in groups.values() if len(ids) > 1]

    frequency_rows = conn.execute(
        """
        WITH concept_frequency AS (
            SELECT source_concept_id AS concept_id, COUNT(*) AS appearance_count
            FROM task_ksa_concept_relations
            GROUP BY source_concept_id
            UNION ALL
            SELECT target_concept_id AS concept_id, COUNT(*) AS appearance_count
            FROM task_ksa_concept_relations
            GROUP BY target_concept_id
        )
        SELECT concept_id, SUM(appearance_count) AS appearance_count
        FROM concept_frequency
        GROUP BY concept_id
        """
    ).fetchall()
    appearance_counts = {
        int(row["concept_id"]): int(row["appearance_count"] or 0)
        for row in frequency_rows
    }
    timestamp = now_utc()
    pairs_inserted = 0
    for concept_ids in duplicate_groups:
        canonical_id = sorted(
            concept_ids,
            key=lambda concept_id: (
                0
                if concept_meta.get(concept_id, {}).get("review_status") in HUMAN_TRUSTED_LABEL_REVIEW_STATUSES
                else 1
                if concept_meta.get(concept_id, {}).get("definition_status") == "defined"
                else 2,
                -appearance_counts.get(concept_id, 0),
                concept_id,
            ),
        )[0]
        if not dry_run:
            placeholders = ",".join("?" for _ in concept_ids)
            conn.execute(
                f"""
                UPDATE ontology_concept_relations
                SET review_status = 'rejected'
                WHERE relation_type = 'same_as'
                  AND relation_label = 'duplicate_normalized_key'
                  AND review_status = 'candidate'
                  AND source_concept_id IN ({placeholders})
                  AND (
                      source_concept_id = ?
                      OR target_concept_id != ?
                  )
                """,
                (*concept_ids, canonical_id, canonical_id),
            )
        for concept_id in concept_ids:
            if concept_id == canonical_id:
                continue
            existing = conn.execute(
                """
                SELECT 1
                FROM ontology_concept_relations
                WHERE source_concept_id = ?
                  AND relation_type = 'same_as'
                  AND target_concept_id = ?
                  AND review_status != 'rejected'
                LIMIT 1
                """,
                (concept_id, canonical_id),
            ).fetchone()
            if existing:
                continue
            if dry_run:
                pairs_inserted += 1
                continue
            before_changes = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO ontology_concept_relations(
                    source_concept_id, relation_type, target_concept_id,
                    relation_label, review_status, created_at
                ) VALUES (?, 'same_as', ?, 'duplicate_normalized_key', 'candidate', ?)
                """,
                (concept_id, canonical_id, timestamp),
            )
            pairs_inserted += int(conn.total_changes - before_changes)
    if not dry_run:
        conn.commit()
    return {
        "groups_found": len(duplicate_groups),
        "pairs_inserted": pairs_inserted,
    }


def ksa_definition_promotion_status(
    conn: sqlite3.Connection,
    *,
    batch_size: int = 5000,
    sample_limit: int = 10,
) -> dict[str, Any]:
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = 5000
    batch_size = max(1, batch_size)
    try:
        sample_limit = int(sample_limit)
    except (TypeError, ValueError):
        sample_limit = 10
    sample_limit = max(0, sample_limit)
    last_meaning_id = 0
    candidate_rows_scanned = 0
    promotable = 0
    skipped_boilerplate = 0
    skipped_human_lock = 0
    processed_concepts: set[int] = set()
    promotable_by_type: dict[str, int] = {}
    skipped_boilerplate_by_type: dict[str, int] = {}
    skipped_human_lock_by_type: dict[str, int] = {}
    samples = {
        "promotable": [],
        "skipped_boilerplate": [],
        "skipped_human_lock": [],
    }

    def _bump(bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def _append_sample(bucket: list[dict[str, Any]], row: sqlite3.Row, reason: str, meaning_text: str) -> None:
        if len(bucket) >= sample_limit:
            return
        bucket.append(
            {
                "meaning_id": int(row["meaning_id"]),
                "concept_id": int(row["concept_id"]),
                "concept_name": row["concept_name"],
                "concept_type": row["concept_type"],
                "definition_status": row["definition_status"],
                "review_status": row["review_status"],
                "reason": reason,
                "meaning_text": meaning_text,
            }
        )

    while True:
        rows = conn.execute(
            """
            SELECT
                kmc.meaning_id,
                kmc.concept_id,
                kmc.concept_type,
                kmc.meaning_text,
                oc.concept_name,
                oc.definition_status,
                oc.review_status
            FROM ksa_meaning_candidates kmc
            JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
            WHERE kmc.meaning_id > ?
              AND kmc.meaning_role = 'term_definition_candidate'
              AND kmc.review_status = 'llm_reviewed'
            ORDER BY kmc.meaning_id
            LIMIT ?
            """,
            (last_meaning_id, batch_size),
        ).fetchall()
        if not rows:
            break

        for row in rows:
            candidate_rows_scanned += 1
            concept_id = int(row["concept_id"])
            if concept_id in processed_concepts:
                continue
            concept_type = str(row["concept_type"] or "")
            meaning_text = normalize_spaces(row["meaning_text"] or "")
            if row["definition_status"] == "defined" or row["review_status"] in DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES:
                skipped_human_lock += 1
                _bump(skipped_human_lock_by_type, concept_type)
                _append_sample(samples["skipped_human_lock"], row, "human_lock", meaning_text)
                processed_concepts.add(concept_id)
                continue
            if _is_ksa_definition_boilerplate(row["concept_type"], row["concept_name"], meaning_text):
                skipped_boilerplate += 1
                _bump(skipped_boilerplate_by_type, concept_type)
                _append_sample(samples["skipped_boilerplate"], row, "boilerplate_prefix", meaning_text)
                continue
            promotable += 1
            _bump(promotable_by_type, concept_type)
            _append_sample(samples["promotable"], row, "promotable", meaning_text)
            processed_concepts.add(concept_id)

        last_meaning_id = int(rows[-1]["meaning_id"])

    return {
        "schema": "ncs_ksa_definition_promotion_status_v1",
        "generated_at": now_utc(),
        "ok": True,
        "criteria": {
            "meaning_role": "term_definition_candidate",
            "review_status": "llm_reviewed",
            "definition_status_lock": ["defined", *DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES],
            "boilerplate_prefixes": dict(KSA_DEFINITION_BOILERPLATE_PREFIXES),
            "generated_template_sample_names": dict(KSA_DEFINITION_BOILERPLATE_SAMPLE_NAMES),
            "generated_template_body_counts": {
                concept_type: len(_generated_ksa_definition_boilerplate_bodies(concept_type))
                for concept_type in sorted(KSA_DEFINITION_BOILERPLATE_SAMPLE_NAMES)
            },
        },
        "batch_size": batch_size,
        "sample_limit": sample_limit,
        "candidate_rows_scanned": candidate_rows_scanned,
        "promotable": promotable,
        "skipped_boilerplate": skipped_boilerplate,
        "skipped_human_lock": skipped_human_lock,
        "promotable_by_concept_type": dict(sorted(promotable_by_type.items())),
        "skipped_boilerplate_by_concept_type": dict(sorted(skipped_boilerplate_by_type.items())),
        "skipped_human_lock_by_concept_type": dict(sorted(skipped_human_lock_by_type.items())),
        "samples": samples,
        "notes": [
            "This is a read-only summary of the same decision logic used by promote_ksa_definitions().",
            "Rows with definition_status='defined' or locked review_status values, including rejected, are treated as locked.",
            "Boilerplate detection strips the concept name prefix before checking fixed prefixes and generated template variants.",
        ],
    }


BOILERPLATE_RETRACT_ELIGIBLE_DEFINITION_SOURCES = (
    "term_definition_template",
    "term_definition_candidate_template",
    "ksa_definition_template",
    "generated_template",
    "ksa_meaning_candidates.term_definition_template",
    "ksa_meaning_candidate_promotion",
)


def retract_boilerplate_definitions(
    conn: sqlite3.Connection,
    *,
    dry_run: bool = True,
    batch_size: int = 5000,
    sample_limit: int = 10,
) -> dict[str, Any]:
    try:
        batch_size = int(batch_size)
    except (TypeError, ValueError):
        batch_size = 5000
    batch_size = max(1, batch_size)
    try:
        sample_limit = int(sample_limit)
    except (TypeError, ValueError):
        sample_limit = 10
    sample_limit = max(0, sample_limit)

    last_concept_id = 0
    rows_scanned = 0
    retract_eligible = 0
    skipped_human_lock = 0
    skipped_not_boilerplate = 0
    skipped_defined = 0
    retracted = 0
    retract_eligible_by_type: dict[str, int] = {}
    skipped_human_lock_by_type: dict[str, int] = {}
    samples: dict[str, list[dict[str, Any]]] = {
        "retract_eligible": [],
        "skipped_human_lock": [],
        "skipped_not_boilerplate": [],
    }

    def _bump(bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def _append_sample(bucket: list[dict[str, Any]], row: sqlite3.Row, reason: str) -> None:
        if len(bucket) >= sample_limit:
            return
        bucket.append(
            {
                "concept_id": int(row["concept_id"]),
                "concept_name": row["concept_name"],
                "concept_type": row["concept_type"],
                "definition_status": row["definition_status"],
                "review_status": row["review_status"],
                "definition_source": row["definition_source"],
                "reason": reason,
                "definition_preview": normalize_spaces(row["definition"] or "")[:160],
            }
        )

    while True:
        rows = conn.execute(
            f"""
            SELECT
                concept_id,
                concept_name,
                concept_type,
                definition,
                definition_source,
                definition_status,
                review_status
            FROM ontology_concepts
            WHERE concept_id > ?
              AND definition IS NOT NULL
              AND TRIM(definition) != ''
            ORDER BY concept_id
            LIMIT ?
            """,
            (last_concept_id, batch_size),
        ).fetchall()
        if not rows:
            break

        for row in rows:
            rows_scanned += 1
            concept_type = str(row["concept_type"] or "")
            definition_text = normalize_spaces(row["definition"] or "")
            review_status = str(row["review_status"] or "")
            definition_status = str(row["definition_status"] or "")
            definition_source = str(row["definition_source"] or "")

            if review_status in DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES:
                skipped_human_lock += 1
                _bump(skipped_human_lock_by_type, concept_type)
                _append_sample(samples["skipped_human_lock"], row, "human_lock")
                continue
            if definition_status == "defined":
                skipped_defined += 1
                continue
            if not _is_ksa_definition_boilerplate(
                row["concept_type"],
                row["concept_name"],
                definition_text,
            ):
                skipped_not_boilerplate += 1
                _append_sample(samples["skipped_not_boilerplate"], row, "not_boilerplate")
                continue
            if definition_source and definition_source not in BOILERPLATE_RETRACT_ELIGIBLE_DEFINITION_SOURCES:
                skipped_not_boilerplate += 1
                _append_sample(samples["skipped_not_boilerplate"], row, "source_not_eligible")
                continue

            retract_eligible += 1
            _bump(retract_eligible_by_type, concept_type)
            _append_sample(samples["retract_eligible"], row, "retract_eligible")
            if not dry_run:
                timestamp = now_utc()
                conn.execute(
                    """
                    UPDATE ontology_concepts
                    SET definition = NULL,
                        definition_source = NULL,
                        definition_status = 'missing',
                        updated_at = ?
                    WHERE concept_id = ?
                    """,
                    (timestamp, int(row["concept_id"])),
                )
                retracted += 1

        last_concept_id = int(rows[-1]["concept_id"])

    if not dry_run:
        conn.commit()

    return {
        "schema": "ncs_retract_boilerplate_definitions_v1",
        "generated_at": now_utc(),
        "ok": True,
        "dry_run": bool(dry_run),
        "db_writes": not dry_run,
        "status_update_allowed": False,
        "approval_claim": False,
        "criteria": {
            "boilerplate_detection": "_is_ksa_definition_boilerplate",
            "eligible_definition_sources": list(BOILERPLATE_RETRACT_ELIGIBLE_DEFINITION_SOURCES),
            "locked_review_statuses": list(DEFINITION_PROMOTION_LOCKED_REVIEW_STATUSES),
            "defined_status_lock": True,
        },
        "batch_size": batch_size,
        "sample_limit": sample_limit,
        "rows_scanned": rows_scanned,
        "retract_eligible": retract_eligible,
        "retracted": retracted,
        "skipped_human_lock": skipped_human_lock,
        "skipped_defined": skipped_defined,
        "skipped_not_boilerplate": skipped_not_boilerplate,
        "retract_eligible_by_concept_type": dict(sorted(retract_eligible_by_type.items())),
        "skipped_human_lock_by_concept_type": dict(sorted(skipped_human_lock_by_type.items())),
        "samples": samples,
        "notes": [
            "Dry-run by default. Apply only after reviewing this report and passing explicit approval.",
            "Retract clears definition/definition_source and sets definition_status='missing'.",
            "review_status is not changed and human_reviewed is never set by this operation.",
        ],
    }


def write_retract_boilerplate_definitions_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Retract Boilerplate Ontology Definitions",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- dry_run: `{report.get('dry_run')}`",
        f"- rows_scanned: `{report.get('rows_scanned')}`",
        f"- retract_eligible: `{report.get('retract_eligible')}`",
        f"- retracted: `{report.get('retracted')}`",
        f"- skipped_human_lock: `{report.get('skipped_human_lock')}`",
        f"- skipped_defined: `{report.get('skipped_defined')}`",
        f"- skipped_not_boilerplate: `{report.get('skipped_not_boilerplate')}`",
        "",
        "## Retract Eligible Samples",
        "",
        "| concept_id | concept_type | concept_name | definition_status | review_status |",
        "| --- | --- | --- | --- | --- |",
    ]
    for sample in (report.get("samples") or {}).get("retract_eligible") or []:
        lines.append(
            "| "
            + " | ".join(
                _md_cell(sample.get(key))
                for key in (
                    "concept_id",
                    "concept_type",
                    "concept_name",
                    "definition_status",
                    "review_status",
                )
            )
            + " |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ksa_definition_promotion_status_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# KSA Definition Promotion Status",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- batch_size: `{report.get('batch_size')}`",
        f"- sample_limit: `{report.get('sample_limit')}`",
        f"- candidate_rows_scanned: `{report.get('candidate_rows_scanned')}`",
        f"- promotable: `{report.get('promotable')}`",
        f"- skipped_boilerplate: `{report.get('skipped_boilerplate')}`",
        f"- skipped_human_lock: `{report.get('skipped_human_lock')}`",
        f"- generated_template_body_counts: `{_md_cell(report.get('criteria', {}).get('generated_template_body_counts') or {})}`",
        "",
        "## By Concept Type",
        "",
        "| Bucket | Concept Type | Count |",
        "| --- | --- | ---: |",
    ]
    for bucket_name in (
        "promotable_by_concept_type",
        "skipped_boilerplate_by_concept_type",
        "skipped_human_lock_by_concept_type",
    ):
        bucket = report.get(bucket_name) or {}
        label = bucket_name.replace("_by_concept_type", "")
        if bucket:
            for concept_type in sorted(bucket):
                lines.append(f"| `{label}` | `{_md_cell(concept_type)}` | {bucket[concept_type]} |")
        else:
            lines.append(f"| `{label}` |  | 0 |")
    lines.extend(
        [
            "",
            "## Samples",
            "",
        ]
    )
    samples = report.get("samples") or {}
    for bucket_name, heading in (
        ("promotable", "Promotable"),
        ("skipped_boilerplate", "Skipped Boilerplate"),
        ("skipped_human_lock", "Skipped Human Lock"),
    ):
        lines.extend(
            [
                f"### {heading}",
                "",
                "| Meaning ID | Concept ID | Concept Name | Concept Type | Definition Status | Review Status | Reason | Meaning Text |",
                "| --- | ---: | ---: | --- | --- | --- | --- | --- |",
            ]
        )
        bucket_rows = samples.get(bucket_name) or []
        if bucket_rows:
            for row in bucket_rows:
                lines.append(
                    "| {meaning_id} | {concept_id} | {concept_name} | {concept_type} | {definition_status} | {review_status} | {reason} | {meaning_text} |".format(
                        meaning_id=int(row.get("meaning_id") or 0),
                        concept_id=int(row.get("concept_id") or 0),
                        concept_name=_md_cell(row.get("concept_name")),
                        concept_type=_md_cell(row.get("concept_type")),
                        definition_status=_md_cell(row.get("definition_status")),
                        review_status=_md_cell(row.get("review_status")),
                        reason=_md_cell(row.get("reason")),
                        meaning_text=_md_cell(row.get("meaning_text")),
                    )
                )
        else:
            lines.append("| none | 0 | 0 |  |  |  |  |  |")
        lines.append("")
    lines.extend(["## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {_md_cell(note)}")
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


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


_KSA_CONJUNCTION_SPLIT_RE = re.compile(r"\s+(?:\ubc0f|\ub610\ub294)\s+")
_KSA_NO_CONJUNCTION_SPLIT_MARKERS = (
    "\ubc95\ub960",
    "\ubc95\uaddc",
    "\uaddc\uc815",
    "\uc9c0\uce68",
    "\uc2dc\ud589\ub839",
    "\uc2dc\ud589\uaddc\uce59",
)
_KSA_NO_CONJUNCTION_SPLIT_PHRASES = (
    "\uc2e4\uc801 \ubc0f \uc0ac\ub840\ud604\ud669",
    "\uc2e4\uc801 \ubc0f \uc0ac\ub840",
)
_KSA_TERMINAL_SUFFIXES = ("\uc9c0\uc2dd", "\uae30\uc220", "\ud0dc\ub3c4", "\ub2a5\ub825")
_KSA_QUOTE_PAIRS = {'"': '"', "'": "'", "\u201c": "\u201d", "\u2018": "\u2019"}
_KSA_QUOTE_CLOSINGS = set(_KSA_QUOTE_PAIRS.values())


def _update_nested_text_state(
    char: str,
    *,
    depth: int,
    quote: str | None,
) -> tuple[int, str | None]:
    if quote:
        if char == quote:
            return depth, None
        return depth, quote
    if char in _KSA_QUOTE_PAIRS:
        return depth, _KSA_QUOTE_PAIRS[char]
    if char in _KSA_QUOTE_CLOSINGS:
        return depth, None
    if char in {"(", "[", "{", "\uff08"}:
        return depth + 1, quote
    if char in {")", "]", "}", "\uff09"} and depth > 0:
        return depth - 1, quote
    return depth, quote


def _split_top_level_commas(value: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    for char in value:
        if char == "," and depth == 0 and quote is None:
            part = normalize_spaces("".join(current)).strip(" -;:,")
            if part:
                parts.append(part)
            current = []
            continue
        current.append(char)
        depth, quote = _update_nested_text_state(char, depth=depth, quote=quote)
    tail = normalize_spaces("".join(current)).strip(" -;:,")
    if tail:
        parts.append(tail)
    return parts if len(parts) > 1 else [normalize_spaces(value)]


def _split_korean_conjunctions(value: str) -> list[str]:
    if any(marker in value for marker in _KSA_NO_CONJUNCTION_SPLIT_MARKERS):
        return [value]
    if any(phrase in value for phrase in _KSA_NO_CONJUNCTION_SPLIT_PHRASES):
        return [value]
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    index = 0
    markers = ("\ubc0f", "\ub610\ub294")
    while index < len(value):
        matched_marker = None
        for marker in markers:
            if not value.startswith(marker, index):
                continue
            before = value[index - 1] if index > 0 else ""
            after_index = index + len(marker)
            after = value[after_index] if after_index < len(value) else ""
            if depth == 0 and quote is None and before.isspace() and after.isspace():
                matched_marker = marker
                break
        if matched_marker:
            part = normalize_spaces("".join(current)).strip(" -;:,")
            if part:
                parts.append(part)
            current = []
            index += len(matched_marker)
            continue
        char = value[index]
        current.append(char)
        depth, quote = _update_nested_text_state(char, depth=depth, quote=quote)
        index += 1
    tail = normalize_spaces("".join(current)).strip(" -;:,")
    if tail:
        parts.append(tail)
    parts = [part for part in parts if part]
    if len(parts) <= 1:
        return [value]
    if any(len(normalize_concept_key(part)) < 2 for part in parts):
        return [value]
    return parts


def _restore_terminal_ksa_suffix(parts: list[str]) -> list[str]:
    if len(parts) <= 1:
        return parts
    suffix = next(
        (
            item
            for item in _KSA_TERMINAL_SUFFIXES
            if normalize_spaces(parts[-1]).endswith(item)
        ),
        "",
    )
    if not suffix:
        return parts
    restored: list[str] = []
    for index, part in enumerate(parts):
        normalized = normalize_spaces(part)
        if index < len(parts) - 1 and not normalized.endswith(_KSA_TERMINAL_SUFFIXES):
            restored.append(normalize_spaces(f"{normalized} {suffix}"))
        else:
            restored.append(normalized)
    return restored


def _split_ksa_list_candidates(value: str) -> list[str]:
    if len(normalize_spaces(value)) < 15:
        return [value]
    comma_parts = _split_top_level_commas(value)
    if len(comma_parts) > 12:
        return [value]
    split_parts: list[str] = []
    for comma_part in comma_parts:
        normalized_part = normalize_spaces(comma_part)
        normalized_part = re.sub(r"^(?:\ubc0f|\ub610\ub294)\s+", "", normalized_part)
        split_parts.extend(_split_korean_conjunctions(normalized_part))
    if len(split_parts) <= 1:
        return [value]
    if any(len(normalize_spaces(part)) < 3 for part in split_parts):
        return [value]
    return _restore_terminal_ksa_suffix(split_parts)


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

    if len(raw_parts) == 1:
        raw_parts = _split_ksa_list_candidates(raw_parts[0])

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
        human_trusted_label_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM ontology_concept_label_candidates
                WHERE source_atomic_id IS NOT NULL
                  AND review_status IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                """
            ).fetchone()[0]
        )
        if human_trusted_label_count:
            raise RuntimeError(
                "Cannot reset atomic KSA while trusted label candidates still "
                "reference source_atomic_id. Resolve or export those reviews first."
            )
        human_trusted_task_relation_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_ksa_concept_relations
                WHERE review_status IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                """
            ).fetchone()[0]
        )
        if human_trusted_task_relation_count:
            raise RuntimeError(
                "Cannot reset atomic KSA while human-reviewed task KSA relations exist. "
                "Export or remap those reviews before resetting atomic KSA."
            )
        human_trusted_task_similarity_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM task_similarity_links
                WHERE review_status IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                """
            ).fetchone()[0]
        )
        if human_trusted_task_similarity_count:
            raise RuntimeError(
                "Cannot reset atomic KSA while human-reviewed task similarity links exist. "
                "Export or remap those reviews before resetting atomic KSA."
            )
        conn.execute(
            f"""
            DELETE FROM ontology_concept_label_candidates
            WHERE review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
            """
        )
        conn.commit()
        conn.execute("DELETE FROM task_similarity_links")
        conn.commit()
        conn.execute("DELETE FROM task_ksa_concept_relations")
        conn.commit()
        conn.execute("DELETE FROM ksa_atomic_concept_links")
        conn.commit()
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
        raw_text = row["ksa_text_raw"]
        atoms = split_ksa_atomic_text(raw_text)
        split_method = (
            "rule_based_comma_split"
            if len(atoms) > 1
            and "\n" not in raw_text
            and len(normalize_spaces(raw_text)) >= 15
            else "rule_based"
        )
        for index, atom in enumerate(atoms, start=1):
            atom_rows.append(
                (
                    row["ksa_id"],
                    row["element_id"],
                    row["ksa_type_name"],
                    index,
                    atom,
                    normalize_concept_key(atom),
                    split_method,
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
                f"HR-scoped concept needs review: {row['concept_name']} "
                f"({row['concept_type']}, definition_status={row['definition_status']}, "
                f"criteria_count={row['criteria_count']}, unit_count={row['unit_count']}, "
                f"units={row['unit_names'] or ''})"
            ),
            suggested_action=(
                "Confirm the representative concept, definition, and aliases. "
                "If valid, mark definition_status='defined' and review_status='human_reviewed'."
            ),
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
          AND tc.ncs_lclas_cd = ?
          AND tc.ncs_mclas_cd = ?
          AND tc.ncs_sclas_cd = ?
          AND (
              gl.link_method IN ({",".join("?" for _ in weak_goal_methods)})
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
        (major_code, middle_code, small_code, *weak_goal_methods, max_goal_links),
    ).fetchall()
    for row in goal_rows:
        if dry_run:
            continue
        severity = "high" if float(row["confidence_score"] or 0.0) < 0.55 else "medium"
        insert_quality_issue(
            conn,
            target_type="training_goal_concept_link",
            target_id=row["link_id"],
            issue_type="hr_training_goal_link_human_review_required",
            severity=severity,
            issue_detail=(
                f"Weak HR training-goal to KSA link needs review: course={row['compe_unit_name']} "
                f"({row['ncs_cl_cd']}), concept={row['concept_name']} ({row['concept_type']}), "
                f"method={row['link_method']}, confidence={row['confidence_score']}, "
                f"goal={normalize_spaces(row['train_goal'] or '')}"
            ),
            suggested_action=(
                "Confirm whether the training goal directly covers the KSA. "
                "Mark the link human_reviewed if valid, or rejected if it is only a generic/indirect match."
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
        },
        "concept_review_issues_created": len(concept_rows),
        "training_goal_link_review_issues_created": len(goal_rows),
        "issue_types": HR_HUMAN_REVIEW_ISSUE_TYPES,
    }
