"""Deep overnight diagnostics for the NCS MCP recommendation system."""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.config import load_settings
from ncs_mcp.db import connect, initialize_database
from ncs_mcp.training_recommendation import (
    evaluate_training_transition_scenarios,
    recommend_training_transition,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "reports" / "overnight_analysis"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def fetch_counts(conn) -> dict[str, int]:
    tables = [
        "classifications",
        "competency_units",
        "competency_elements",
        "performance_criteria",
        "ksa_items",
        "ksa_atomic_items",
        "ontology_concepts",
        "ksa_meaning_candidates",
        "task_ksa_concept_relations",
        "task_similarity_links",
        "ncs_training_courses",
        "ncs_training_course_unit_links",
        "ncs_training_course_concept_links",
        "ncs_training_course_element_links",
        "training_goal_concept_links",
        "training_delivery_relations",
        "ncs_qualification_items",
        "ncs_unit_qualification_links",
        "ncs_job_base_competencies",
        "ncs_job_base_factors",
        "ncs_unit_job_base_links",
        "training_transition_gold_scenarios",
        "quality_issues",
        "refinement_jobs",
    ]
    counts: dict[str, int] = {}
    for table in tables:
        counts[table] = int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
    return counts


def grouped_counts(conn, sql: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql).fetchall()]


def fetch_ontology_snapshot(conn) -> dict[str, Any]:
    return {
        "concepts_by_type_status": grouped_counts(
            conn,
            """
            SELECT concept_type, definition_status, review_status, COUNT(*) AS count
            FROM ontology_concepts
            GROUP BY concept_type, definition_status, review_status
            ORDER BY count DESC
            """,
        ),
        "meaning_candidates_by_status": grouped_counts(
            conn,
            """
            SELECT concept_type, source_method, review_status, COUNT(*) AS count
            FROM ksa_meaning_candidates
            GROUP BY concept_type, source_method, review_status
            ORDER BY count DESC
            """,
        ),
        "training_goal_links_by_method_status": grouped_counts(
            conn,
            """
            SELECT link_method, review_status, COUNT(*) AS count
            FROM training_goal_concept_links
            GROUP BY link_method, review_status
            ORDER BY count DESC
            """,
        ),
        "course_links_by_method_status": grouped_counts(
            conn,
            """
            SELECT link_method, review_status, COUNT(*) AS count
            FROM ncs_training_course_concept_links
            GROUP BY link_method, review_status
            ORDER BY count DESC
            """,
        ),
        "task_relation_by_type_status": grouped_counts(
            conn,
            """
            SELECT relation_type, review_status, COUNT(*) AS count
            FROM task_ksa_concept_relations
            GROUP BY relation_type, review_status
            ORDER BY count DESC
            """,
        ),
        "broad_training_concepts": grouped_counts(
            conn,
            """
            SELECT
                c.concept_id,
                c.concept_name,
                c.concept_type,
                c.review_status,
                COUNT(DISTINCT l.training_course_id) AS course_count,
                COUNT(DISTINCT l.unit_code) AS unit_count,
                COUNT(*) AS link_count
            FROM ncs_training_course_concept_links l
            JOIN ontology_concepts c ON c.concept_id = l.concept_id
            GROUP BY c.concept_id
            HAVING unit_count >= 10
            ORDER BY unit_count DESC, course_count DESC
            LIMIT 50
            """,
        ),
    }


def fetch_quality_snapshot(conn) -> dict[str, Any]:
    return {
        "quality_by_severity_type": grouped_counts(
            conn,
            """
            SELECT severity, issue_type, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY severity, issue_type
            ORDER BY count DESC
            LIMIT 50
            """,
        ),
        "quality_by_target_type": grouped_counts(
            conn,
            """
            SELECT target_type, severity, COUNT(*) AS count
            FROM quality_issues
            WHERE resolved_at IS NULL
            GROUP BY target_type, severity
            ORDER BY count DESC
            LIMIT 50
            """,
        ),
        "refinement_by_status": grouped_counts(
            conn,
            """
            SELECT review_status, COUNT(*) AS count
            FROM refinement_jobs
            GROUP BY review_status
            ORDER BY count DESC
            """,
        ),
    }


def current_tool_surface() -> dict[str, Any]:
    server_path = ROOT / "src" / "ncs_mcp" / "server.py"
    source = server_path.read_text(encoding="utf-8")
    names = []
    for match in re.finditer(r"@mcp\.tool\(\)\s*\ndef\s+([a-zA-Z_][a-zA-Z0-9_]*)", source):
        names.append(match.group(1))
    legacy_names = re.findall(r"\ndef\s+(_legacy_[a-zA-Z_][a-zA-Z0-9_]*)", source)
    return {
        "tool_count": len(names),
        "tools": names,
        "legacy_function_count": len(legacy_names),
        "legacy_sample": legacy_names[:30],
    }


def compact_recommendation(item: dict[str, Any]) -> dict[str, Any]:
    course = item.get("training_course") or {}
    match = item.get("match") or {}
    components = item.get("score_components") or {}
    return {
        "rank": item.get("rank"),
        "training_course_id": course.get("training_course_id"),
        "course_name": course.get("compe_unit_name"),
        "level": course.get("compe_unit_level"),
        "major": course.get("ncs_lclas_cdnm"),
        "sub": course.get("ncs_subd_cdnm"),
        "hours": course.get("train_time"),
        "method": course.get("meth_name"),
        "confidence_score": item.get("confidence_score"),
        "confidence_grade": item.get("confidence_grade"),
        "final_score": components.get("final_score"),
        "reasons": ";".join(match.get("reasons") or []),
        "direct_unit_evidence": match.get("direct_unit_evidence"),
        "sibling_scope_evidence": match.get("sibling_scope_evidence"),
        "goal_direct_hits": match.get("goal_direct_concept_hits"),
        "goal_token_hits": match.get("goal_token_concept_hits"),
        "goal_element_hits": match.get("goal_element_implied_concept_hits"),
        "goal_unit_core_hits": match.get("goal_unit_core_concept_hits"),
        "training_goal_ksa_score": components.get("training_goal_ksa_score"),
        "gap_ksa_score": components.get("gap_ksa_score"),
        "support_score": components.get("support_score"),
        "penalty_score": components.get("penalty_score"),
    }


def analyze_gold_scenarios(conn, limit: int) -> dict[str, Any]:
    evaluation = evaluate_training_transition_scenarios(conn, limit=limit)
    rows = conn.execute(
        """
        SELECT *
        FROM training_transition_gold_scenarios
        WHERE review_status != 'rejected'
        ORDER BY scenario_id
        """
    ).fetchall()
    case_rows: list[dict[str, Any]] = []
    recommendation_rows: list[dict[str, Any]] = []
    reason_counter: Counter[str] = Counter()
    confidence_counter: Counter[str] = Counter()
    for row in rows:
        result = recommend_training_transition(
            conn,
            current_query=row["current_query"],
            target_query=row["target_query"],
            major_code=row["major_code"],
            limit=limit,
            save=False,
        )
        expected = json.loads(row["expected_course_names_json"] or "[]")
        if not result.get("ok"):
            case_rows.append(
                {
                    "scenario_id": row["scenario_id"],
                    "scenario_name": row["scenario_name"],
                    "major_code": row["major_code"],
                    "review_status": row["review_status"],
                    "current_query": row["current_query"],
                    "target_query": row["target_query"],
                    "ok": False,
                    "error_code": (result.get("error") or {}).get("code"),
                    "error_message": (result.get("error") or {}).get("message"),
                    "expected_courses": "|".join(expected),
                }
            )
            continue
        recommendations = result.get("recommendations", [])
        names = [
            (item.get("training_course") or {}).get("compe_unit_name")
            for item in recommendations
        ]
        hits = [name for name in expected if name in names]
        low_count = sum(1 for item in recommendations if item.get("confidence_grade") == "low")
        medium_count = sum(1 for item in recommendations if item.get("confidence_grade") == "medium")
        high_count = sum(1 for item in recommendations if item.get("confidence_grade") == "high")
        summary = result["transition"]["summary"]
        case_rows.append(
            {
                "scenario_id": row["scenario_id"],
                "scenario_name": row["scenario_name"],
                "major_code": row["major_code"],
                "review_status": row["review_status"],
                "current_query": row["current_query"],
                "target_query": row["target_query"],
                "ok": True,
                "current_match": result["transition"]["current_scope"]["match_text"],
                "target_match": result["transition"]["target_scope"]["match_text"],
                "expected_courses": "|".join(expected),
                "recommended_courses": "|".join(str(name) for name in names),
                "hit_courses": "|".join(hits),
                "missed_expected_courses": "|".join(name for name in expected if name not in hits),
                "hit_count": len(hits),
                "recommended_count": len(names),
                "case_precision": round(len(hits) / len(names), 4) if names else 0.0,
                "case_recall": round(len(hits) / len(expected), 4) if expected else None,
                "high_confidence_count": high_count,
                "medium_confidence_count": medium_count,
                "low_confidence_count": low_count,
                "transferability_ratio": summary.get("transferability_ratio"),
                "current_ksa_count": summary.get("current_ksa_concept_count"),
                "target_ksa_count": summary.get("target_ksa_concept_count"),
                "transferable_ksa_count": summary.get("transferable_ksa_concept_count"),
                "gap_ksa_count": summary.get("gap_ksa_concept_count"),
            }
        )
        for item in recommendations:
            compact = compact_recommendation(item)
            for reason in (item.get("match") or {}).get("reasons") or []:
                reason_counter[reason] += 1
            confidence_counter[str(item.get("confidence_grade"))] += 1
            recommendation_rows.append(
                {
                    "scenario_id": row["scenario_id"],
                    "scenario_name": row["scenario_name"],
                    "current_query": row["current_query"],
                    "target_query": row["target_query"],
                    "is_expected": compact["course_name"] in expected,
                    **compact,
                }
            )
    low_precision_cases = sorted(
        [row for row in case_rows if row.get("ok") and (row.get("case_precision") or 0) < 0.5],
        key=lambda item: (item.get("case_precision") or 0, item.get("scenario_id") or 0),
    )
    return {
        "evaluation": evaluation,
        "cases": case_rows,
        "recommendations": recommendation_rows,
        "reason_counts": reason_counter.most_common(),
        "confidence_counts": dict(confidence_counter),
        "low_precision_cases": low_precision_cases[:30],
    }


def search_training_candidates(conn, keyword: str, limit: int = 20) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                training_course_id,
                compe_unit_name,
                compe_unit_level,
                ncs_lclas_cdnm,
                ncs_mclas_cdnm,
                ncs_sclas_cdnm,
                ncs_subd_cdnm,
                train_time,
                meth_name
            FROM ncs_training_courses
            WHERE compe_unit_name LIKE ?
               OR train_goal LIKE ?
               OR ncs_subd_cdnm LIKE ?
            ORDER BY ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd, compe_unit_name
            LIMIT ?
            """,
            (f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", limit),
        ).fetchall()
    ]


def analyze_hr_analytics_bridge(conn, limit: int) -> dict[str, Any]:
    keywords = [
        "인사",
        "인사평가",
        "직무관리",
        "데이터",
        "통계",
        "회귀",
        "빅데이터",
        "비즈니스 인텔리전스",
    ]
    candidate_rows: list[dict[str, Any]] = []
    for keyword in keywords:
        for row in search_training_candidates(conn, keyword, limit=20):
            candidate_rows.append({"keyword": keyword, **row})
    transition_queries = [
        ("인사기획", "HR Analytics"),
        ("인사기획", "통계조사"),
        ("인사기획", "빅데이터분석"),
        ("인사기획", "고객데이터 분석"),
        ("인사기획", "비즈니스 인텔리전스 지원"),
    ]
    transitions = []
    for current_query, target_query in transition_queries:
        result = recommend_training_transition(
            conn,
            current_query=current_query,
            target_query=target_query,
            limit=limit,
            save=False,
        )
        item: dict[str, Any] = {
            "current_query": current_query,
            "target_query": target_query,
            "ok": result.get("ok"),
            "error": result.get("error"),
        }
        if result.get("ok"):
            item["summary"] = result["transition"]["summary"]
            item["gap_ksa"] = [
                {
                    "concept_name": row.get("concept_name"),
                    "concept_type": row.get("concept_type"),
                    "review_status": row.get("review_status"),
                    "definition_status": row.get("definition_status"),
                }
                for row in result["transition"].get("gap_ksa", [])[:20]
            ]
            item["transferable_ksa"] = [
                {
                    "concept_name": row.get("concept_name"),
                    "concept_type": row.get("concept_type"),
                    "review_status": row.get("review_status"),
                    "definition_status": row.get("definition_status"),
                }
                for row in result["transition"].get("transferable_ksa", [])[:20]
            ]
            item["recommendations"] = [
                compact_recommendation(rec) for rec in result.get("recommendations", [])
            ]
        else:
            item["content"] = result.get("content")
        transitions.append(item)
    return {
        "training_candidate_rows": candidate_rows,
        "transition_queries": transitions,
    }


def write_markdown_report(path: Path, evidence: dict[str, Any]) -> None:
    evaluation = evidence["gold_analysis"]["evaluation"]
    breakdown = evaluation.get("breakdown") or {}
    hr = breakdown.get("major:02") or {}
    low_precision = evidence["gold_analysis"]["low_precision_cases"][:10]
    tool_surface = evidence["tool_surface"]
    quality = evidence["quality_snapshot"]
    lines = [
        "# Overnight Deep Analysis Evidence",
        "",
        f"Generated: {evidence['generated_at']}",
        "",
        "## Executive Snapshot",
        "",
        f"- MCP exposed tools: {tool_surface['tool_count']}",
        f"- Gold scenarios: {evaluation.get('scenario_count')}",
        f"- Overall precision@5: {evaluation.get('precision_at_k')}",
        f"- Overall recall@5: {evaluation.get('expected_course_recall_at_k')}",
        f"- Overall top1 hit rate: {evaluation.get('top1_expected_hit_rate')}",
        f"- HR major:02 precision@5: {hr.get('precision_at_k')}",
        f"- HR major:02 recall@5: {hr.get('expected_course_recall_at_k')}",
        "",
        "## Tool Surface",
        "",
        ", ".join(tool_surface["tools"]),
        "",
        "## Recommendation Reason Distribution",
        "",
    ]
    for reason, count in evidence["gold_analysis"]["reason_counts"][:20]:
        lines.append(f"- {reason}: {count}")
    lines.extend(["", "## Confidence Distribution", ""])
    for grade, count in sorted(evidence["gold_analysis"]["confidence_counts"].items()):
        lines.append(f"- {grade}: {count}")
    lines.extend(["", "## Low Precision Cases", ""])
    for row in low_precision:
        lines.append(
            "- "
            f"{row['scenario_name']}: {row['current_query']} -> {row['target_query']}, "
            f"precision={row['case_precision']}, recommended={row['recommended_courses']}, "
            f"expected={row['expected_courses']}"
        )
    lines.extend(["", "## Quality Issue Snapshot", ""])
    for row in quality["quality_by_severity_type"][:15]:
        lines.append(f"- {row['severity']} / {row['issue_type']}: {row['count']}")
    lines.extend(["", "## HR Analytics Bridge Finding", ""])
    lines.append(
        "`HR Analytics` remains a non-NCS target and must not be hallucinated as an NCS unit. "
        "The safer product behavior is to return NOT_FOUND plus bridge candidates such as "
        "`통계조사`, `빅데이터분석`, `고객데이터 분석`, and `비즈니스 인텔리전스 지원`."
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(out_dir: Path, limit: int) -> dict[str, Any]:
    settings = load_settings()
    conn = connect(settings.db_path)
    initialize_database(conn)
    try:
        evidence = {
            "generated_at": utc_now(),
            "db_path": str(settings.db_path),
            "counts": fetch_counts(conn),
            "tool_surface": current_tool_surface(),
            "quality_snapshot": fetch_quality_snapshot(conn),
            "ontology_snapshot": fetch_ontology_snapshot(conn),
            "gold_analysis": analyze_gold_scenarios(conn, limit=limit),
            "hr_analytics_bridge": analyze_hr_analytics_bridge(conn, limit=limit),
        }
    finally:
        conn.close()

    write_json(out_dir / "overnight_evidence.json", evidence)
    write_csv(out_dir / "transition_cases.csv", evidence["gold_analysis"]["cases"])
    write_csv(out_dir / "transition_recommendations.csv", evidence["gold_analysis"]["recommendations"])
    write_csv(out_dir / "hr_bridge_training_candidates.csv", evidence["hr_analytics_bridge"]["training_candidate_rows"])
    write_markdown_report(out_dir / "overnight_deep_analysis.md", evidence)
    return evidence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    evidence = run(args.out_dir, args.limit)
    summary = {
        "generated_at": evidence["generated_at"],
        "out_dir": str(args.out_dir),
        "tool_count": evidence["tool_surface"]["tool_count"],
        "scenario_count": evidence["gold_analysis"]["evaluation"]["scenario_count"],
        "precision_at_k": evidence["gold_analysis"]["evaluation"]["precision_at_k"],
        "recall_at_k": evidence["gold_analysis"]["evaluation"]["expected_course_recall_at_k"],
        "low_precision_case_count": len(evidence["gold_analysis"]["low_precision_cases"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
