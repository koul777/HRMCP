from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, create_indexes, initialize_database, now_utc, rows_to_dicts


SQF_CONCEPTS = [
    ("KQF", "Korean Qualifications Framework", "National framework connecting learning, qualifications, field experience, and training outcomes."),
    ("SQF", "Sectoral Qualifications Framework", "Sector-level framework that structures jobs, levels, competencies, and recognition evidence."),
    ("SECTOR", "Sector", "Labor-market activity area where career movement is generally possible."),
    ("QUALIFICATION", "Qualification", "Officially recognized capability such as degree, certificate, training completion, or equivalent evidence."),
    ("FRAMEWORK", "Framework", "Structural frame used to attach degrees, qualifications, training, and career evidence."),
    ("SQF_JOB", "SQF Job", "Group of work with similar knowledge and skills where vertical career movement commonly occurs."),
    ("SQF_LEVEL", "SQF Level", "Criterion distinguishing jobs by difficulty and complexity of required knowledge and skills."),
    ("SQF_JOB_LEVEL", "SQF Job Level", "Unit that divides an SQF job by SQF level and can support HR management decisions."),
    ("JOB_COMPETENCY", "Job Competency", "Capability required at a job level, including knowledge, skills, autonomy, and evidence."),
    ("RECOGNITION_REQUIREMENT", "Recognition Requirement", "Criteria and methods for recognizing whether a person holds SQF job competency."),
]

CONCEPT_LINKS_BY_ROLE = {
    "competency_framework": ["SQF", "SQF_JOB", "SQF_LEVEL", "SQF_JOB_LEVEL", "JOB_COMPETENCY"],
    "training_design": ["JOB_COMPETENCY", "RECOGNITION_REQUIREMENT"],
    "university_curriculum_recognition": ["QUALIFICATION", "RECOGNITION_REQUIREMENT"],
    "competency_recognition": ["QUALIFICATION", "RECOGNITION_REQUIREMENT"],
    "development_manual": ["SQF", "SECTOR", "SQF_JOB", "SQF_LEVEL", "SQF_JOB_LEVEL"],
    "legacy_research": ["SQF", "SECTOR", "SQF_JOB", "SQF_LEVEL"],
    "case_study": ["SQF", "RECOGNITION_REQUIREMENT"],
    "reference": ["SQF"],
}


def stable_id(prefix: str, *parts: Any) -> str:
    text = "|".join("" if part is None else str(part).strip() for part in parts)
    return f"{prefix}:{hashlib.sha1(text.encode('utf-8', errors='ignore')).hexdigest()[:20]}"


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def maybe_int(value: Any) -> int | None:
    match = re.search(r"\d+", clean_text(value))
    return int(match.group(0)) if match else None


def has_value(value: Any) -> bool:
    text = clean_text(value)
    return bool(text) and text not in {"-", "N/A", "n/a", "none", "None"}


def build_sector_name(field_name: str | None, sub_field_name: str | None) -> str:
    field = clean_text(field_name) or "unclassified"
    sub = clean_text(sub_field_name)
    return f"{field} > {sub}" if sub else field


def base_duty_name(value: str | None) -> str:
    text = clean_text(value)
    text = re.sub(r"\([^)]*\)", " ", text)
    text = re.sub(r"\bL?\d+\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def reset_sqf_derived_tables(conn: sqlite3.Connection) -> None:
    for table in [
        "sqf_chunk_job_level_matches",
        "sqf_document_evidence_links",
        "sqf_recognition_evidence",
        "sqf_job_levels_normalized",
        "sqf_levels",
        "sqf_jobs_normalized",
        "sqf_industry_sectors",
        "sqf_framework_concepts",
    ]:
        conn.execute(f"DELETE FROM {table}")


def insert_concepts(conn: sqlite3.Connection) -> int:
    timestamp = now_utc()
    for code, name, definition in SQF_CONCEPTS:
        conn.execute(
            """
            INSERT INTO sqf_framework_concepts(
                concept_code, concept_name, definition, source_note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(concept_code) DO UPDATE SET
                concept_name = excluded.concept_name,
                definition = excluded.definition,
                source_note = excluded.source_note,
                updated_at = excluded.updated_at
            """,
            (code, name, definition, "Seeded from SQF overview and project requirements.", timestamp, timestamp),
        )
    return len(SQF_CONCEPTS)


def insert_sqf_normalized_from_api(conn: sqlite3.Connection) -> dict[str, int]:
    timestamp = now_utc()
    rows = conn.execute("SELECT * FROM sqf_duties ORDER BY ncs_lclas_cd, sqf_field_name, job_name").fetchall()
    sectors: set[str] = set()
    jobs: set[str] = set()
    levels: set[int] = set()
    job_levels = 0
    evidence_count = 0

    for row in rows:
        sector_id = stable_id("sqf-sector", row["ncs_lclas_cd"], row["sqf_field_name"], row["sqf_sub_field_name"])
        conn.execute(
            """
            INSERT INTO sqf_industry_sectors(
                sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, sector_name, source_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(ncs_lclas_cd, sqf_field_name, sqf_sub_field_name) DO UPDATE SET
                ncs_lclas_name = excluded.ncs_lclas_name,
                sector_name = excluded.sector_name,
                source_count = sqf_industry_sectors.source_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                sector_id,
                row["ncs_lclas_cd"],
                row["ncs_lclas_name"],
                row["sqf_field_name"] or "",
                row["sqf_sub_field_name"],
                build_sector_name(row["sqf_field_name"], row["sqf_sub_field_name"]),
                timestamp,
            ),
        )
        sectors.add(sector_id)

        job_name = clean_text(row["job_name"]) or "unclassified"
        job_id = stable_id("sqf-job", sector_id, job_name)
        conn.execute(
            """
            INSERT INTO sqf_jobs_normalized(
                sqf_job_id, sector_id, job_name, job_definition,
                vertical_mobility_note, horizontal_mobility_note, source_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(sector_id, job_name) DO UPDATE SET
                job_definition = COALESCE(NULLIF(excluded.job_definition, ''), sqf_jobs_normalized.job_definition),
                source_count = sqf_jobs_normalized.source_count + 1,
                updated_at = excluded.updated_at
            """,
            (
                job_id,
                sector_id,
                job_name,
                None,
                "Vertical career movement is generally modeled within the same SQF job.",
                "Horizontal movement can be reviewed through adjacent SQF jobs or levels.",
                timestamp,
            ),
        )
        jobs.add(job_id)

        level = maybe_int(row["duty_level"])
        if level is not None:
            conn.execute(
                """
                INSERT INTO sqf_levels(sqf_level, level_name, definition, kqf_based, updated_at)
                VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(sqf_level) DO UPDATE SET
                    level_name = COALESCE(NULLIF(excluded.level_name, ''), sqf_levels.level_name),
                    definition = COALESCE(NULLIF(excluded.definition, ''), sqf_levels.definition),
                    updated_at = excluded.updated_at
                """,
                (level, row["duty_level_name"], row["duty_level_definition"], timestamp),
            )
            levels.add(level)

        job_level_id = stable_id("sqf-job-level", row["source_key"])
        conn.execute(
            """
            INSERT INTO sqf_job_levels_normalized(
                sqf_job_level_id, sqf_job_id, sqf_source_key, duty_name,
                sqf_level, level_name, job_level_definition, duty_definition,
                autonomy_responsibility, source_payload, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sqf_source_key) DO UPDATE SET
                sqf_job_id = excluded.sqf_job_id,
                duty_name = excluded.duty_name,
                sqf_level = excluded.sqf_level,
                level_name = excluded.level_name,
                job_level_definition = excluded.job_level_definition,
                duty_definition = excluded.duty_definition,
                autonomy_responsibility = excluded.autonomy_responsibility,
                source_payload = excluded.source_payload,
                updated_at = excluded.updated_at
            """,
            (
                job_level_id,
                job_id,
                row["source_key"],
                row["duty_name"],
                level,
                row["duty_level_name"],
                row["duty_level_definition"],
                row["duty_definition"],
                row["autonomy_responsibility"],
                row["source_payload"],
                timestamp,
            ),
        )
        job_levels += 1

        for evidence_type, source_field in [
            ("academic_career", "duty_acarr"),
            ("training", "duty_education_training"),
            ("qualification", "duty_qualification"),
            ("career", "duty_career"),
            ("license", "duty_license"),
            ("remark", "duty_remark"),
        ]:
            if not has_value(row[source_field]):
                continue
            conn.execute(
                """
                INSERT INTO sqf_recognition_evidence(
                    sqf_job_level_id, evidence_type, evidence_text,
                    source_field, source, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, 'raw', ?)
                ON CONFLICT(sqf_job_level_id, evidence_type, evidence_text, source_field)
                DO UPDATE SET source = excluded.source
                """,
                (job_level_id, evidence_type, clean_text(row[source_field]), source_field, "SQF openapi26", timestamp),
            )
            evidence_count += 1

    return {
        "api_source_rows": len(rows),
        "sectors": len(sectors),
        "jobs": len(jobs),
        "levels": len(levels),
        "job_levels": job_levels,
        "recognition_evidence": evidence_count,
    }


def link_documents_to_ontology(conn: sqlite3.Connection) -> dict[str, int]:
    timestamp = now_utc()
    documents = conn.execute("SELECT document_id, title, ontology_role FROM sqf_document_sources ORDER BY document_id").fetchall()
    concepts = 0
    sectors = 0
    jobs = 0
    for doc in documents:
        title = clean_text(doc["title"])
        role = clean_text(doc["ontology_role"]) or "reference"
        for concept_code in CONCEPT_LINKS_BY_ROLE.get(role, ["SQF"]):
            conn.execute(
                """
                INSERT INTO sqf_document_evidence_links(
                    document_id, target_type, target_id, relation,
                    evidence_note, confidence, created_at
                ) VALUES (?, 'sqf_concept', ?, 'supportsDefinition', ?, 'document_role_rule', ?)
                ON CONFLICT(document_id, target_type, target_id, relation) DO UPDATE SET
                    evidence_note = excluded.evidence_note,
                    confidence = excluded.confidence
                """,
                (doc["document_id"], concept_code, f"Document role '{role}' supports concept '{concept_code}'.", timestamp),
            )
            concepts += 1

        for sector in conn.execute(
            """
            SELECT sector_id, sector_name, sqf_field_name, sqf_sub_field_name
            FROM sqf_industry_sectors
            WHERE (? LIKE '%' || sqf_field_name || '%')
               OR (sqf_sub_field_name IS NOT NULL AND ? LIKE '%' || sqf_sub_field_name || '%')
            LIMIT 20
            """,
            (title, title),
        ).fetchall():
            conn.execute(
                """
                INSERT INTO sqf_document_evidence_links(
                    document_id, target_type, target_id, relation,
                    evidence_note, confidence, created_at
                ) VALUES (?, 'sqf_sector', ?, 'mentionsSector', ?, 'document_title_rule', ?)
                ON CONFLICT(document_id, target_type, target_id, relation) DO UPDATE SET
                    evidence_note = excluded.evidence_note
                """,
                (doc["document_id"], sector["sector_id"], f"Document title mentions sector '{sector['sector_name']}'.", timestamp),
            )
            sectors += 1

        for job in conn.execute(
            """
            SELECT sqf_job_id, job_name
            FROM sqf_jobs_normalized
            WHERE LENGTH(job_name) >= 2
              AND ? LIKE '%' || job_name || '%'
            LIMIT 20
            """,
            (title,),
        ).fetchall():
            conn.execute(
                """
                INSERT INTO sqf_document_evidence_links(
                    document_id, target_type, target_id, relation,
                    evidence_note, confidence, created_at
                ) VALUES (?, 'sqf_job', ?, 'mentionsJob', ?, 'document_title_rule', ?)
                ON CONFLICT(document_id, target_type, target_id, relation) DO UPDATE SET
                    evidence_note = excluded.evidence_note
                """,
                (doc["document_id"], job["sqf_job_id"], f"Document title mentions SQF job '{job['job_name']}'.", timestamp),
            )
            jobs += 1
    return {"document_concept_links": concepts, "document_sector_links": sectors, "document_job_links": jobs}


def build_sqf_sqlite_model(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    create_indexes(conn)
    try:
        reset_sqf_derived_tables(conn)
        concept_count = insert_concepts(conn)
        api_counts = insert_sqf_normalized_from_api(conn)
        doc_links = link_documents_to_ontology(conn)
        conn.commit()
        counts = {
            table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in [
                "sqf_framework_concepts",
                "sqf_industry_sectors",
                "sqf_jobs_normalized",
                "sqf_levels",
                "sqf_job_levels_normalized",
                "sqf_recognition_evidence",
                "sqf_document_evidence_links",
            ]
        }
        return {
            "concepts_seeded": concept_count,
            "api_normalized": api_counts,
            "document_links": doc_links,
            "counts": counts,
            "note": "Derived SQF ontology tables were rebuilt from sqf_duties and sqf_document_sources.",
        }
    finally:
        conn.close()


def sqf_model_summary(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    try:
        role_counts = {
            row["ontology_role"]: row["count"]
            for row in conn.execute(
                """
                SELECT ontology_role, COUNT(*) AS count
                FROM sqf_document_sources
                GROUP BY ontology_role
                ORDER BY count DESC
                """
            )
        }
        return {
            "counts": {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in [
                    "sqf_duties",
                    "sqf_framework_concepts",
                    "sqf_industry_sectors",
                    "sqf_jobs_normalized",
                    "sqf_levels",
                    "sqf_job_levels_normalized",
                    "sqf_recognition_evidence",
                    "sqf_document_sources",
                    "sqf_document_assets",
                    "sqf_document_pages",
                    "sqf_document_chunks",
                    "sqf_chunk_job_level_matches",
                    "sqf_document_evidence_links",
                ]
            },
            "document_role_counts": role_counts,
            "concepts": rows_to_dicts(
                conn.execute(
                    """
                    SELECT concept_code, concept_name, definition
                    FROM sqf_framework_concepts
                    ORDER BY concept_code
                    """
                ).fetchall()
            ),
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Build normalized SQF SQLite ontology tables.")
    parser.add_argument("--db", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--summary", action="store_true")
    args = parser.parse_args()
    result = sqf_model_summary(args.db) if args.summary else build_sqf_sqlite_model(args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
