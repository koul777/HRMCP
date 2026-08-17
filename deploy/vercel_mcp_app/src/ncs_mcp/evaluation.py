from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.mapping_policy import DEFAULT_MAPPING_FILTER, REVIEWED_STATUSES
from ncs_mcp.ontology import (
    MVP_JOB_NAMES,
    MVP_MAJOR_CODE,
    MVP_SQF_FIELD_NAME,
    SCOPE_BUSINESS_02,
    SCOPE_MANAGEMENT_SUPPORT,
    SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
)


def run_evaluation(db_path: Path, *, scope_tag: str | None = None, run_name: str = "mvp") -> dict[str, Any]:
    conn = connect(db_path)
    initialize_database(conn)
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if scope_tag:
            clauses.append("scope_tag = ?")
            params.append(scope_tag)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(conn.execute(f"SELECT COUNT(*) FROM sqf_ncs_matches {where}", params).fetchone()[0])

        low_params = list(params)
        relation_params = list(params)
        rejected_params = list(params)
        low_confidence = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sqf_ncs_matches
                {where}
                {'AND' if where else 'WHERE'} review_status NOT IN ('accepted', 'reviewed', 'human_reviewed', 'rejected')
                  AND score < ?
                """,
                low_params + [DEFAULT_MAPPING_FILTER.min_score],
            ).fetchone()[0]
        )
        related = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sqf_ncs_matches
                {where}
                {'AND' if where else 'WHERE'} review_status NOT IN ('accepted', 'reviewed', 'human_reviewed', 'rejected')
                  AND relation = 'related'
                """,
                relation_params,
            ).fetchone()[0]
        )
        rejected = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sqf_ncs_matches
                {where}
                {'AND' if where else 'WHERE'} review_status = 'rejected'
                """,
                rejected_params,
            ).fetchone()[0]
        )
        sqf_where = ""
        if scope_tag == SCOPE_BUSINESS_02:
            sqf_where = "WHERE ncs_lclas_cd = '02'"
        elif scope_tag == SCOPE_MANAGEMENT_SUPPORT:
            sqf_where = (
                "WHERE ncs_lclas_cd = '02' "
                "AND sqf_field_name = '경영관리' "
                "AND job_name = '경영지원'"
            )
        elif scope_tag == SCOPE_MANAGEMENT_SUPPORT_HR_MVP:
            placeholders = ",".join("'" + job_name + "'" for job_name in MVP_JOB_NAMES)
            sqf_where = (
                f"WHERE ncs_lclas_cd = '{MVP_MAJOR_CODE}' "
                f"AND sqf_field_name = '{MVP_SQF_FIELD_NAME}' "
                f"AND job_name IN ({placeholders})"
            )
        sqf_total = int(conn.execute(f"SELECT COUNT(*) FROM sqf_duties {sqf_where}").fetchone()[0])
        sqf_with_direct = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM sqf_duties
                {sqf_where}
                {'AND' if sqf_where else 'WHERE'} (
                    TRIM(COALESCE(duty_education_training, '')) != ''
                    OR TRIM(COALESCE(duty_qualification, '')) != ''
                    OR TRIM(COALESCE(duty_career, '')) != ''
                )
                """
            ).fetchone()[0]
        )
        rec_scope_condition = (
            f"r.target_source_key IN (SELECT source_key FROM sqf_duties {sqf_where})"
            if sqf_where
            else ""
        )

        def rec_where(extra: list[str] | None = None) -> str:
            rec_clauses = list(extra or [])
            if rec_scope_condition:
                rec_clauses.append(rec_scope_condition)
            return f"WHERE {' AND '.join(rec_clauses)}" if rec_clauses else ""

        recommendation_run_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM education_recommendation_runs r
                {rec_where()}
                """
            ).fetchone()[0]
        )
        recommendation_item_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM education_recommendation_items i
                JOIN education_recommendation_runs r ON r.run_id = i.run_id
                {rec_where()}
                """
            ).fetchone()[0]
        )
        recommendation_evidence_count = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                FROM education_recommendation_evidence e
                JOIN education_recommendation_runs r ON r.run_id = e.run_id
                {rec_where()}
                """
            ).fetchone()[0]
        )
        recommendation_items_with_evidence = int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT i.item_id)
                FROM education_recommendation_items i
                JOIN education_recommendation_runs r ON r.run_id = i.run_id
                JOIN education_recommendation_evidence e ON e.item_id = i.item_id
                {rec_where()}
                """
            ).fetchone()[0]
        )
        trusted_placeholders = ",".join("?" for _ in REVIEWED_STATUSES)
        candidate_leakage_count = int(
            conn.execute(
                f"""
                SELECT COUNT(DISTINCT e.evidence_id)
                FROM education_recommendation_evidence e
                JOIN education_recommendation_runs r ON r.run_id = e.run_id
                JOIN sqf_ncs_matches m ON m.match_id = e.match_id
                {rec_where(["e.source_table = 'sqf_ncs_matches'", f"m.review_status NOT IN ({trusted_placeholders})"])}
                """,
                list(REVIEWED_STATUSES),
            ).fetchone()[0]
        )

        def evidence_item_count(*evidence_types: str) -> int:
            placeholders = ",".join("?" for _ in evidence_types)
            return int(
                conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT e.item_id)
                    FROM education_recommendation_evidence e
                    JOIN education_recommendation_runs r ON r.run_id = e.run_id
                    {rec_where([f"e.evidence_type IN ({placeholders})"])}
                    """,
                    list(evidence_types),
                ).fetchone()[0]
            )

        def item_rate(count: int) -> float:
            return round(count / recommendation_item_count, 4) if recommendation_item_count else 0.0

        direct_sqf_items = evidence_item_count("sqf_direct")
        sqf_document_items = evidence_item_count("sqf_document")
        ncs_supplement_items = evidence_item_count("ncs_mapping", "ontology_concept")
        ontology_concept_items = evidence_item_count("ontology_concept")
        metrics = {
            "scope_tag": scope_tag,
            "mapping_count": total,
            "low_confidence_leak_count_if_unfiltered": low_confidence + related,
            "rejected_mapping_count": rejected,
            "sqf_duty_count": sqf_total,
            "sqf_direct_evidence_count": sqf_with_direct,
            "sqf_direct_evidence_rate": round(sqf_with_direct / sqf_total, 4) if sqf_total else 0.0,
            "recommendation_run_count": recommendation_run_count,
            "recommendation_item_count": recommendation_item_count,
            "recommendation_evidence_count": recommendation_evidence_count,
            "candidate_leakage_count": candidate_leakage_count,
            "candidate_leakage_rate": (
                round(candidate_leakage_count / recommendation_evidence_count, 4)
                if recommendation_evidence_count
                else 0.0
            ),
            "recommendation_items_with_evidence_rate": item_rate(recommendation_items_with_evidence),
            "direct_sqf_evidence_recommendation_rate": item_rate(direct_sqf_items),
            "sqf_document_evidence_recommendation_rate": item_rate(sqf_document_items),
            "ncs_supplement_evidence_recommendation_rate": item_rate(ncs_supplement_items),
            "ontology_concept_evidence_rate": item_rate(ontology_concept_items),
            "human_review_precision_at_5": None,
            "human_review_precision_at_5_status": "baseline_pending",
            "mapping_filter": DEFAULT_MAPPING_FILTER.as_dict(),
            "caveat_required": True,
        }
        conn.execute(
            """
            INSERT INTO evaluation_runs(run_name, scope_tag, metrics_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (run_name, scope_tag, json.dumps(metrics, ensure_ascii=False, sort_keys=True), now_utc()),
        )
        conn.commit()
        return metrics
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    settings = load_settings()
    parser = argparse.ArgumentParser(description="Evaluate NCS-SQF ontology MCP quality metrics.")
    parser.add_argument("--db-path", type=Path, default=settings.db_path)
    parser.add_argument("--scope-tag")
    parser.add_argument("--run-name", default="mvp")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(json.dumps(run_evaluation(args.db_path, scope_tag=args.scope_tag, run_name=args.run_name), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
