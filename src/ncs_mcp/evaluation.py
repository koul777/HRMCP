from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.mapping_policy import DEFAULT_MAPPING_FILTER


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
        if scope_tag == "business_accounting_office_02":
            sqf_where = "WHERE ncs_lclas_cd = '02'"
        elif scope_tag == "management_support":
            sqf_where = "WHERE ncs_lclas_cd = '02' AND sqf_field_name = '경영관리' AND job_name = '경영지원'"
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
        metrics = {
            "scope_tag": scope_tag,
            "mapping_count": total,
            "low_confidence_leak_count_if_unfiltered": low_confidence + related,
            "rejected_mapping_count": rejected,
            "sqf_duty_count": sqf_total,
            "sqf_direct_evidence_count": sqf_with_direct,
            "sqf_direct_evidence_rate": round(sqf_with_direct / sqf_total, 4) if sqf_total else 0.0,
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
