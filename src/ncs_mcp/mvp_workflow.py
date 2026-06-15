from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.db import connect, ensure_ontology_seeded, initialize_database, now_utc
from ncs_mcp.evaluation import run_evaluation
from ncs_mcp.ontology import (
    MVP_JOB_NAMES,
    MVP_MAJOR_CODE,
    MVP_SQF_FIELD_NAME,
    SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
    generate_mapping_candidates,
    insert_mapping_candidate,
)


MVP_REVIEWER_ID = "system:mvp_bootstrap"
MVP_REVIEW_METHOD = "mvp_bootstrap_review_v1"


def mvp_scope_clause(alias: str = "sd") -> tuple[str, list[Any]]:
    prefix = f"{alias}." if alias else ""
    placeholders = ",".join("?" for _ in MVP_JOB_NAMES)
    return (
        f"""
        {prefix}ncs_lclas_cd = ?
        AND {prefix}sqf_field_name = ?
        AND {prefix}job_name IN ({placeholders})
        """,
        [MVP_MAJOR_CODE, MVP_SQF_FIELD_NAME, *MVP_JOB_NAMES],
    )


def mvp_sqf_duties(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    clause, params = mvp_scope_clause("")
    return conn.execute(
        f"""
        SELECT *
        FROM sqf_duties
        WHERE {clause}
        ORDER BY job_name, CAST(NULLIF(duty_level, '') AS INTEGER), duty_name, source_key
        """,
        params,
    ).fetchall()


def build_mvp_mapping_candidates(
    conn: sqlite3.Connection,
    *,
    limit_per_duty: int = 10,
) -> dict[str, Any]:
    duties = mvp_sqf_duties(conn)
    candidates_generated = 0
    candidates_upserted = 0
    for duty in duties:
        candidates = generate_mapping_candidates(conn, duty, limit=limit_per_duty)
        candidates_generated += len(candidates)
        for candidate in candidates:
            insert_mapping_candidate(
                conn,
                candidate,
                scope_tag=SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
            )
            candidates_upserted += 1
    conn.commit()
    return {
        "scope_tag": SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
        "sqf_duties_seen": len(duties),
        "candidates_generated": candidates_generated,
        "candidates_upserted": candidates_upserted,
        "limit_per_duty": limit_per_duty,
    }


def _audit_review_change(
    conn: sqlite3.Connection,
    *,
    match_id: int,
    previous_status: str,
    new_status: str,
    notes: str,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO review_audit_log(
            entity_type, entity_id, action, previous_status, new_status,
            reviewer_id, notes, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sqf_ncs_match",
            str(match_id),
            "mvp_policy_review",
            previous_status,
            new_status,
            MVP_REVIEWER_ID,
            notes,
            timestamp,
        ),
    )


def review_mvp_mapping_candidates(
    conn: sqlite3.Connection,
    *,
    accept_top_n: int = 3,
    min_accept_score: float = 7.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    duties = mvp_sqf_duties(conn)
    source_ids = [row["source_key"] for row in duties]
    if not source_ids:
        return {
            "scope_tag": SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
            "sqf_duties_seen": 0,
            "accepted": 0,
            "rejected": 0,
            "unchanged": 0,
            "dry_run": dry_run,
        }

    accepted = 0
    rejected = 0
    unchanged = 0
    reviewed_sources = 0
    timestamp = now_utc()
    placeholders = ",".join("?" for _ in source_ids)
    rows = conn.execute(
        f"""
        SELECT *
        FROM sqf_ncs_matches
        WHERE source_type = 'sqf_duty'
          AND target_type = 'ncs_competency_unit'
          AND source_id IN ({placeholders})
          AND review_status NOT IN ('human_reviewed', 'reviewed')
        ORDER BY source_id, score DESC, target_id
        """,
        source_ids,
    ).fetchall()

    rows_by_source: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        rows_by_source.setdefault(row["source_id"], []).append(row)

    for source_id, source_rows in rows_by_source.items():
        reviewed_sources += 1
        for rank, row in enumerate(source_rows, start=1):
            relation = row["relation"] or ""
            score = float(row["score"] or 0)
            should_accept = (
                rank <= accept_top_n
                and score >= min_accept_score
                and relation != "related"
            )
            new_status = "accepted" if should_accept else "rejected"
            filter_status = "eligible" if should_accept else "excluded"
            exclusion_reason = None if should_accept else (
                "mvp_rank_or_score_policy"
                if relation != "related"
                else "relation:related"
            )
            notes = (
                f"{MVP_REVIEW_METHOD}; rank={rank}; score={score:.2f}; "
                f"relation={relation}; scope={SCOPE_MANAGEMENT_SUPPORT_HR_MVP}; "
                "policy-assisted review, not human_reviewed"
            )
            if row["review_status"] == new_status:
                unchanged += 1
                continue
            if new_status == "accepted":
                accepted += 1
            else:
                rejected += 1
            if dry_run:
                continue
            conn.execute(
                """
                UPDATE sqf_ncs_matches
                SET review_status = ?,
                    filter_status = ?,
                    exclusion_reason = ?,
                    reviewer_id = ?,
                    reviewed_at = ?,
                    reviewer_notes = ?,
                    updated_at = ?
                WHERE match_id = ?
                """,
                (
                    new_status,
                    filter_status,
                    exclusion_reason,
                    MVP_REVIEWER_ID,
                    timestamp,
                    notes,
                    timestamp,
                    row["match_id"],
                ),
            )
            _audit_review_change(
                conn,
                match_id=int(row["match_id"]),
                previous_status=row["review_status"],
                new_status=new_status,
                notes=notes,
                timestamp=timestamp,
            )
    if not dry_run:
        conn.commit()
    return {
        "scope_tag": SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
        "sqf_duties_seen": len(duties),
        "reviewed_sources": reviewed_sources,
        "accepted": accepted,
        "rejected": rejected,
        "unchanged": unchanged,
        "accept_top_n": accept_top_n,
        "min_accept_score": min_accept_score,
        "reviewer_id": MVP_REVIEWER_ID,
        "dry_run": dry_run,
    }


def ensure_mvp_ksa_concepts(conn: sqlite3.Connection) -> dict[str, Any]:
    before = {
        "concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
        "ksa_links": int(conn.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0]),
    }
    seeded = ensure_ontology_seeded(conn)
    clause, params = mvp_scope_clause("sd")
    accepted_units = conn.execute(
        f"""
        SELECT DISTINCT m.target_id AS unit_code
        FROM sqf_ncs_matches m
        JOIN sqf_duties sd ON sd.source_key = m.source_id
        WHERE {clause}
          AND m.review_status IN ('accepted', 'reviewed', 'human_reviewed')
          AND m.target_type = 'ncs_competency_unit'
        """,
        params,
    ).fetchall()
    unit_codes = [row["unit_code"] for row in accepted_units if row["unit_code"]]
    if unit_codes:
        placeholders = ",".join("?" for _ in unit_codes)
        mvp_ksa_total = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                WHERE ce.unit_code IN ({placeholders})
                """,
                unit_codes,
            ).fetchone()[0]
        )
        mvp_ksa_linked = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
                WHERE ce.unit_code IN ({placeholders})
                """,
                unit_codes,
            ).fetchone()[0]
        )
    else:
        mvp_ksa_total = 0
        mvp_ksa_linked = 0
    after = {
        "concepts": int(conn.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]),
        "ksa_links": int(conn.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0]),
    }
    return {
        "seeded": seeded,
        "before": before,
        "after": after,
        "accepted_unit_count": len(unit_codes),
        "accepted_unit_ksa_total": mvp_ksa_total,
        "accepted_unit_ksa_linked": mvp_ksa_linked,
    }


def mvp_status(conn: sqlite3.Connection) -> dict[str, Any]:
    clause, params = mvp_scope_clause("sd")
    review_rows = conn.execute(
        f"""
        SELECT m.review_status, COUNT(*) AS count
        FROM sqf_ncs_matches m
        JOIN sqf_duties sd ON sd.source_key = m.source_id
        WHERE {clause}
        GROUP BY m.review_status
        ORDER BY m.review_status
        """,
        params,
    ).fetchall()
    direct_count = int(
        conn.execute(
            f"""
            SELECT COUNT(*)
            FROM sqf_duties sd
            WHERE {clause}
              AND (
                TRIM(COALESCE(sd.duty_education_training, '')) != ''
                OR TRIM(COALESCE(sd.duty_qualification, '')) != ''
                OR TRIM(COALESCE(sd.duty_career, '')) != ''
                OR TRIM(COALESCE(sd.duty_license, '')) != ''
              )
            """,
            params,
        ).fetchone()[0]
    )
    return {
        "scope_tag": SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
        "sqf_duty_count": len(mvp_sqf_duties(conn)),
        "sqf_duties_with_direct_evidence": direct_count,
        "review_status_counts": {row["review_status"]: row["count"] for row in review_rows},
    }


def run_mvp_bootstrap(
    db_path: Path,
    *,
    limit_per_duty: int = 10,
    accept_top_n: int = 3,
    min_accept_score: float = 7.0,
    dry_run: bool = False,
) -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    try:
        before = mvp_status(conn)
        build_summary = build_mvp_mapping_candidates(conn, limit_per_duty=limit_per_duty)
        review_summary = review_mvp_mapping_candidates(
            conn,
            accept_top_n=accept_top_n,
            min_accept_score=min_accept_score,
            dry_run=dry_run,
        )
        ksa_summary = ensure_mvp_ksa_concepts(conn)
        after = mvp_status(conn)
    finally:
        conn.close()
    evaluation = run_evaluation(
        db_path,
        scope_tag=SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
        run_name="mvp_bootstrap",
    )
    return {
        "scope": {
            "major_code": MVP_MAJOR_CODE,
            "sqf_field_name": MVP_SQF_FIELD_NAME,
            "job_names": list(MVP_JOB_NAMES),
            "scope_tag": SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
        },
        "before": before,
        "build_mappings": build_summary,
        "review_mappings": review_summary,
        "ksa_concepts": ksa_summary,
        "after": after,
        "evaluation": evaluation,
    }
