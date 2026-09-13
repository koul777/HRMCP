"""Read-only structural relevance gate for the task/KSA shadow experiment.

Hierarchy probes and counterfactuals are independently defined diagnostics,
not independent semantic judgments. This executable cannot promote public
ranking: the absent semantic evaluation is an explicit, non-overridable HOLD.
No holdout fixture, relationship table, review status, or alias is read as a label.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import statistics
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "task_ksa_shadow_for_relevance", ROOT / "scripts/profile_task_ksa_search_rerank.py")
shadow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shadow)

DEFAULT_OUT = ROOT / "reports/task_ksa_relevance_gate_20260913.json"
THRESHOLDS = {
    "minimum_major_coverage_ratio": 1.0,
    "minimum_probes_per_major": 2,
    "minimum_supported_candidates": 1,
    "minimum_changed_queries": 1,
    "minimum_mean_ndcg_delta": 0.0,
    "minimum_mean_mrr_delta": 0.0,
    "maximum_regressed_queries": 0,
    "minimum_counterfactual_rejection_rate": 1.0,
    "maximum_public_mutations": 0,
    "semantic_requirement": {
        "minimum_independent_queries": 100,
        "minimum_distinct_majors": 24,
        "minimum_mean_ndcg_delta_exclusive": 0.0,
        "minimum_mean_mrr_delta_exclusive": 0.0,
        "minimum_paired_bootstrap_ndcg_delta_95pct_lower_bound_exclusive": 0.0,
        "maximum_per_query_regressions": 0,
        "provenance": "externally adjudicated, frozen before scoring, source-disjoint queries and labels",
        "status": "required_not_available; cannot be satisfied by these structural probes",
    },
}


def select_probes(conn: sqlite3.Connection, per_major: int = 2) -> tuple[list[dict], list[str]]:
    """Choose source titles BEFORE any scoring, independently of relation presence.

Stable hash order, one element per distinct unit; no score-driven replacement.
Two whitespace-separated words avoid a wholly single-token probe population.
Eligibility is defined only by title length, not shadow tokenizer behavior.
"""
    rows = conn.execute("""
        SELECT ce.element_id,ce.unit_code,ce.element_name_raw,c.major_code
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code=ce.unit_code
        JOIN classifications c ON c.classification_id=cu.classification_id
        ORDER BY c.major_code,ce.unit_code,ce.element_id
    """).fetchall()
    groups = defaultdict(list)
    majors = set()
    for row in rows:
        r = dict(row)
        major = str(r["major_code"] or "")
        if not major:
            continue
        majors.add(major)
        title = str(r["element_name_raw"] or "").strip()
        if len(title.split()) < 2 or not 4 <= len(title) <= 100:
            continue
        key = f"task-ksa-relevance-v1:{major}:{r['unit_code']}:{r['element_id']}"
        groups[major].append((hashlib.sha256(key.encode()).hexdigest(), r, title))
    probes = []
    for major in sorted(majors):
        units = set()
        for key, row, title in sorted(groups[major], key=lambda x: x[0]):
            if row["unit_code"] in units:
                continue
            units.add(row["unit_code"])
            probes.append({"id": "structure-" + key[:16], "query": title,
                           "anchor_element_id": row["element_id"],
                           "anchor_unit_code": row["unit_code"], "major_code": major,
                           "classification_filter": {"major_code": major}})
            if len(units) >= per_major:
                break
    return probes, sorted(majors)


def hierarchy_label(conn: sqlite3.Connection, item: dict, probe: dict) -> dict:
    """Grade from source FK ancestry, never returned path or shadow evidence.

This is scope consistency: grade 3 = anchored element (or one of its rows),
grade 1 = same unit, grade 0 = different unit or missing source identity.
It deliberately makes no semantic relevance claim about another element.
"""
    statements = {
        "unit": "SELECT unit_code,NULL AS element_id FROM competency_units WHERE unit_code=?",
        "element": "SELECT unit_code,element_id FROM competency_elements WHERE element_id=?",
        "criteria": "SELECT ce.unit_code,ce.element_id FROM performance_criteria pc JOIN competency_elements ce ON ce.element_id=pc.element_id WHERE pc.criteria_id=?",
        "ksa": "SELECT ce.unit_code,ce.element_id FROM ksa_items ki JOIN competency_elements ce ON ce.element_id=ki.element_id WHERE ki.ksa_id=?",
    }
    sql = statements.get(item.get("type"))
    row = conn.execute(sql, (item["id"],)).fetchone() if sql else None
    grade = 0
    if row and row["unit_code"] == probe["anchor_unit_code"]:
        grade = 3 if row["element_id"] == probe["anchor_element_id"] else 1
    return {"grade": grade, "source_identity_exists": row is not None,
            "basis": "source_fk_ancestry_only"}


def metrics(grades: list[int], k: int) -> dict:
    """Conditional NDCG over the fixed retrieved pool; missing retrieval separate."""
    def dcg(values):
        return sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(values[:k]))
    ideal = dcg(sorted(grades, reverse=True))
    return {"ndcg": dcg(grades) / ideal if ideal else 0.0,
            "mrr": next((1 / (i + 1) for i, g in enumerate(grades[:k]) if g > 0), 0.0),
            "anchor_hit": any(g == 3 for g in grades[:k]),
            "same_unit_precision": sum(g > 0 for g in grades[:k]) / max(k, 1)}


def split_layer_counterfactual(rows: list[dict]) -> list[dict]:
    """Preserve every text/token/count; disconnect task and KSA element IDs.

In-memory only, intentionally structurally impossible. This is a necessary
anti-spurious-cooccurrence control, not a natural negative relevance label.
"""
    result = copy.deepcopy(rows)
    for row in result:
        if row["layer"] == "ksa":
            row["element_id"] = f"counterfactual-ksa:{row['element_id']}"
    return result


def score_probe(conn: sqlite3.Connection, public: dict, probe: dict, k: int) -> dict:
    before = shadow.fingerprint(public)
    sql, params = shadow.evidence_sql(public["results"])
    evidence = [dict(r) for r in conn.execute(sql, params)] if sql else []
    scored = shadow.score_evidence(public, evidence)
    cf = shadow.score_evidence(public, split_layer_counterfactual(evidence))
    reversed_score = shadow.score_evidence(public, list(reversed(evidence)))
    labels = [hierarchy_label(conn, row, probe) for row in public["results"]]
    baseline_grades = [r["grade"] for r in labels]
    ordered = sorted(scored["candidates"], key=lambda c: c["shadow_rank"])
    reranked_grades = [baseline_grades[c["public_rank"] - 1] for c in ordered]
    baseline = metrics(baseline_grades, k)
    reranked = metrics(reranked_grades, k)
    positives = [c["public_rank"] - 1 for c in scored["candidates"] if c["shadow_score"] > 0]
    # Query source title is not read by the score function as task/KSA evidence.
    # Exact whole-title echoes in raw task/KSA text are removed in this ablation.
    # Remaining paraphrases/ancestry are still correlated, hence no semantic PASS.
    normalize = shadow.normalize_search_text
    q = normalize(probe["query"])
    no_echo = [r for r in evidence if not (
        r["layer"] in ("criteria", "ksa") and q and q in normalize(r["evidence_text"] or ""))]
    ablated = shadow.score_evidence(public, no_echo)
    return {"probe": probe, "population": "generated_non_holdout_structure_probe",
            "candidate_count": len(labels), "source_identity_missing": sum(not r["source_identity_exists"] for r in labels),
            "baseline": baseline, "shadow": reranked,
            "ndcg_delta": reranked["ndcg"] - baseline["ndcg"],
            "mrr_delta": reranked["mrr"] - baseline["mrr"],
            "supported_candidates": len(positives),
            "changed_candidates": scored["hypothetical_moved_candidates"],
            "counterfactual_tested_candidates": len(positives),
            "counterfactual_rejected_candidates": sum(cf["candidates"][i]["shadow_score"] == 0 for i in positives),
            "counterfactual_positive_candidates": sum(c["shadow_score"] > 0 for c in cf["candidates"]),
            "evidence_permutation_invariant": all(
                a["shadow_score"] == b["shadow_score"] and a["shadow_rank"] == b["shadow_rank"]
                for a, b in zip(scored["candidates"], reversed_score["candidates"])),
            "title_echo_ablation": {"removed_rows": len(evidence) - len(no_echo),
                                    "remaining_supported_candidates": sum(c["shadow_score"] > 0 for c in ablated["candidates"])},
            "public_unchanged": before == shadow.fingerprint(public),
            "classification_filter_preserved": all(c["classification_filter_preserved"] for c in scored["candidates"]),
            "labels_and_ranks": [{"type": c["type"], "id": c["id"],
                                 "grade": labels[c["public_rank"] - 1]["grade"],
                                 "public_rank": c["public_rank"], "shadow_rank": c["shadow_rank"],
                                 "shadow_score": c["shadow_score"]} for c in scored["candidates"]]}


def aggregate(cases: list[dict]) -> dict:
    tested = sum(c["counterfactual_tested_candidates"] for c in cases)
    rejected = sum(c["counterfactual_rejected_candidates"] for c in cases)
    return {"queries": len(cases), "candidates": sum(c["candidate_count"] for c in cases),
            "supported_candidates": sum(c["supported_candidates"] for c in cases),
            "changed_queries": sum(c["changed_candidates"] > 0 for c in cases),
            "changed_candidates": sum(c["changed_candidates"] for c in cases),
            "baseline_mean_ndcg": statistics.fmean(c["baseline"]["ndcg"] for c in cases) if cases else 0.0,
            "shadow_mean_ndcg": statistics.fmean(c["shadow"]["ndcg"] for c in cases) if cases else 0.0,
            "baseline_mean_mrr": statistics.fmean(c["baseline"]["mrr"] for c in cases) if cases else 0.0,
            "shadow_mean_mrr": statistics.fmean(c["shadow"]["mrr"] for c in cases) if cases else 0.0,
            "mean_ndcg_delta": statistics.fmean(c["ndcg_delta"] for c in cases) if cases else 0.0,
            "mean_mrr_delta": statistics.fmean(c["mrr_delta"] for c in cases) if cases else 0.0,
            "regressed_queries": sum(c["ndcg_delta"] < -1e-12 or c["mrr_delta"] < -1e-12 for c in cases),
            "empty_queries": sum(c["candidate_count"] == 0 for c in cases),
            "baseline_anchor_hits": sum(c["baseline"]["anchor_hit"] for c in cases),
            "shadow_anchor_hits": sum(c["shadow"]["anchor_hit"] for c in cases),
            "counterfactual_tested_candidates": tested,
            "counterfactual_rejected_candidates": rejected,
            "counterfactual_rejection_rate": rejected / tested if tested else None,
            "title_echo_removed_rows": sum(c["title_echo_ablation"]["removed_rows"] for c in cases),
            "title_echo_ablation_remaining_support": sum(c["title_echo_ablation"]["remaining_supported_candidates"] for c in cases),
            "public_mutations": sum(not c["public_unchanged"] for c in cases),
            "source_identity_missing": sum(c["source_identity_missing"] for c in cases)}


def promotion_gate(cases: list[dict], available_majors: list[str]) -> dict:
    m = aggregate(cases)
    counts = Counter(c["probe"]["major_code"] for c in cases)
    t = THRESHOLDS
    checks = {
        "all_available_majors_sampled": bool(available_majors) and set(available_majors) <= set(counts),
        "minimum_probes_per_major": bool(available_majors) and all(counts[x] >= t["minimum_probes_per_major"] for x in available_majors),
        "nonzero_shadow_support": m["supported_candidates"] >= t["minimum_supported_candidates"],
        "nonzero_ranking_effect": m["changed_queries"] >= t["minimum_changed_queries"],
        "nonnegative_structural_ndcg_delta": m["mean_ndcg_delta"] >= t["minimum_mean_ndcg_delta"] - 1e-12,
        "nonnegative_structural_mrr_delta": m["mean_mrr_delta"] >= t["minimum_mean_mrr_delta"] - 1e-12,
        "zero_structural_per_query_regressions": m["regressed_queries"] <= t["maximum_regressed_queries"],
        "counterfactual_rejection": m["counterfactual_rejection_rate"] is not None and m["counterfactual_rejection_rate"] >= t["minimum_counterfactual_rejection_rate"],
        "evidence_permutation_invariance": bool(cases) and all(c["evidence_permutation_invariant"] for c in cases),
        "public_contract_preserved": bool(cases) and m["public_mutations"] == 0,
        "classification_filter_preserved": bool(cases) and all(c["classification_filter_preserved"] for c in cases),
        "source_identities_resolved": bool(cases) and m["source_identity_missing"] == 0,
    }
    return {"promotion_decision": "HOLD", "structural_decision": "PASS" if all(checks.values()) else "HOLD",
            "checks": checks, "independent_semantic_validation": False,
            "hold_reasons": [name for name, passed in checks.items() if not passed] + ["independent_semantic_labels_absent"],
            "pass_rule": "Every structural check AND a separate frozen source-disjoint semantic evaluation meeting threshold.semantic_requirement must PASS. This script has no semantic-label override.",
            "rollout_authority": False, "threshold": copy.deepcopy(t), "metrics": m}


def build_report(db: Path, per_major: int = 2, limit: int = 16, k: int = 10,
                 expected_sha256: str = shadow.EXPECTED_SHA256,
                 expected_size_bytes: int = shadow.EXPECTED_SIZE) -> dict:
    before = shadow.file_identity(db)
    if before["sha256"] != expected_sha256 or before["size_bytes"] != expected_size_bytes:
        raise ValueError("Builder DB identity mismatch")
    with shadow.readonly_connection(db) as conn:
        probes, majors = select_probes(conn, per_major)
        session = shadow.SearchSession(db)
        cases = []
        for probe in probes:
            result, _, _ = session.search(probe, limit)
            cases.append(score_probe(conn, result, probe, k))
    after = shadow.file_identity(db)
    if before != after:
        raise ValueError("DB identity changed; report invalid")
    gate = promotion_gate(cases, majors)
    return {"schema": "task_ksa_relevance_gate_v1", "generated_at": datetime.now(timezone.utc).isoformat(),
            "promotion_decision": gate["promotion_decision"], "promotion_gate": gate,
            "threshold": copy.deepcopy(THRESHOLDS), "db_before": before, "db_after": after,
            "db_identity_unchanged": True,
            "policy": {"db_writes": False, "public_ranking_changed": False, "holdout_opened": False,
                       "alias_added": False, "human_status_writes": False, "approval_claim": False,
                       "framework_reference_used_as_scored_data": False},
            "design": {"label_source": "source foreign-key ancestry only; not task/KSA relation tables or scores",
                       "query_source": "competency_elements.element_name_raw, stable hash sample by major and distinct unit",
                       "source_split": "column split: title query and FK labels versus raw criteria/KSA score text; NOT independent corpus split",
                       "grade_3": "same source element", "grade_1": "same source unit", "grade_0": "other unit or missing identity",
                       "scope": "all available majors, explicit major filter, generated non-holdout probes",
                       "selection_before_scoring": True, "label_relation_tables_read": [],
                       "counterfactual": "same text and row counts; KSA element IDs moved to disjoint namespace in memory",
                       "metric_population": "fixed returned candidate pool, conditional graded NDCG@k and same-unit MRR@k; anchor-hit separately detects misses",
                       "guide_mapping": "C1-1 task/KSA scope consistency; does not validate C1-2 necessity or C2 course/delivery plans"},
            "leakage_caveat": [
                "Hierarchy and scorer share the same NCS source ancestry. A column split is not independent semantic relevance validation.",
                "Exact source-title queries favor existing lexical retrieval; title echo ablation still shares vocabulary and ancestry.",
                "If title_echo_removed_rows is zero, the echo ablation made no intervention and supplies no additional evidence of independence.",
                "Same-unit/element membership is a structural proxy; an outside-unit result may be semantically useful and an inside-unit row may be irrelevant.",
                "Counterfactual rejection tests the scorer's structural rule on impossible rows; it is not an estimate of natural-query precision.",
                "Only returned candidates are reranked. Conditional NDCG does not measure full-corpus recall.",
                "Source rows selected by this probe generator are development diagnostics; they must not later be described as untouched holdout labels.",
                "No independent semantic labels exist in this run; positive structural changes cannot clear HOLD.",
                "Semantic thresholds are proposed engineering acceptance targets, not calibrated statistical power guarantees. Freeze evaluator, thresholds, split, and DB before a separately authorized semantic evaluation.",
                "Latency and concurrent-serving checks remain separate requirements; this offline multi-query evaluator is not a latency benchmark."],
            "reproducibility": {"db_sha256": before["sha256"], "probes_sha256": shadow.fingerprint(probes),
                                "result_sha256": shadow.fingerprint(cases), "threshold_sha256": shadow.fingerprint(THRESHOLDS),
                                "script_sha256": shadow.file_identity(Path(__file__))["sha256"],
                                "shadow_profiler_sha256": shadow.file_identity(Path(shadow.__file__))["sha256"],
                                "search_core_sha256": shadow.file_identity(ROOT / "src/ncs_mcp/search/core.py")["sha256"],
                                "per_major": per_major, "candidate_limit": limit, "metric_k": k},
            "available_majors": majors, "by_major": {m: aggregate([c for c in cases if c["probe"]["major_code"] == m]) for m in majors},
            "cases": cases}


def markdown(report: dict) -> str:
    gate = report["promotion_gate"]
    lines = ["# Task/KSA relevance promotion gate", "", f"Promotion: **{gate['promotion_decision']}**. Structural diagnostics: {gate['structural_decision']}.", "",
             "Independent semantic judgments are absent. This report grants no ranking rollout or human approval.", "",
             "## Metrics", "", "| Metric | Value |", "| --- | ---: |"]
    lines += [f"| {name} | {value} |" for name, value in gate["metrics"].items()]
    lines += ["", "## Checks", ""]
    lines += [f"- {name}: {'PASS' if passed else 'HOLD'}" for name, passed in gate["checks"].items()]
    lines += ["- independent_semantic_validation: HOLD", "", "## Thresholds and PASS rule", "", gate["pass_rule"], "", "```json", json.dumps(report["threshold"], indent=2), "```", "",
              "## Leakage and limits", ""]
    lines += [f"- {item}" for item in report["leakage_caveat"]]
    lines += ["", "## Reproduce", "", "```powershell", "python scripts/profile_task_ksa_rerank_relevance.py", "```", "",
              f"DB SHA-256: `{report['db_before']['sha256']}`. Before/after identical; mode=ro/query_only.",
              f"Probe digest: `{report['reproducibility']['probes_sha256']}`.",
              f"Result digest: `{report['reproducibility']['result_sha256']}`.", "",
              "Case-level source IDs, FK labels, ranks, counterfactual results, and per-major summaries are in the sibling JSON. These are generated development probes, not holdout cases.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=shadow.DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--per-major", type=int, default=2)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--expected-sha256", default=shadow.EXPECTED_SHA256)
    parser.add_argument("--expected-size-bytes", type=int, default=shadow.EXPECTED_SIZE)
    parser.add_argument("--strict", action="store_true", help="Return 2 for HOLD (default 0 means report generated).")
    args = parser.parse_args()
    if not 1 <= args.per_major <= 10 or not 1 <= args.k <= args.limit <= shadow.MAX_CANDIDATES:
        parser.error("per-major must be 1..10; 1 <= k <= limit <= 24")
    if args.out.resolve() == args.db.resolve() or args.out.with_suffix(".md").resolve() == args.db.resolve():
        parser.error("report path must not overwrite database")
    report = build_report(args.db, args.per_major, args.limit, args.k,
                          args.expected_sha256.lower(), args.expected_size_bytes)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"promotion_decision": report["promotion_decision"],
                      "promotion_gate": report["promotion_gate"], "out": str(args.out)}, indent=2))
    return 2 if args.strict and report["promotion_decision"] != "PASS" else 0


if __name__ == "__main__":
    raise SystemExit(main())
