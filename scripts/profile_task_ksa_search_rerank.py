"""Bounded, read-only task/KSA evidence experiment; never changes public ranking.

The fixture is synthetic and unlabeled. Coverage is evidence availability in a
bounded sample, not relevance accuracy or a trusted criteria-to-KSA relation.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ncs_mcp import server  # noqa: E402
from ncs_mcp.search.core import (  # noqa: E402
    _NCS_SEARCH_GENERIC_TOKENS,
    _ncs_search_boundary_match_normalized,
    _normalize_ncs_search_query,
)
from ncs_mcp.search.normalization import normalize_search_text  # noqa: E402

DEFAULT_DB = ROOT / ".state/ncs-data-builder/versions/20260912_231258_f2fee72e/release/compact.db"
EXPECTED_SHA256 = "4fe15cdcdf129bb2436215be83b48eec66706232a1cc70f24d0e4d2b83fafdd2"
EXPECTED_SIZE = 478756864
FIXTURE = ROOT / "tests/fixtures/ncs_search_task_ksa_synthetic.json"
DEFAULT_OUT = ROOT / "reports/overnight_sessions/task_ksa_rerank_shadow_20260913.json"
MAX_CANDIDATES = 24
MAX_ELEMENTS = 6
MAX_ROWS_PER_ELEMENT = 8
MAX_TEXT_CHARS = 512
MAX_TOKENS = 8
SQL_BUDGET_MS = 25.0


def file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return {"path": str(path.resolve()), "size_bytes": path.stat().st_size,
            "sha256": digest.hexdigest()}


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def summary(values: list[float]) -> dict[str, Any]:
    ordered = sorted(values)
    def percentile(q: float) -> float:
        if not ordered:
            return 0.0
        pos = (len(ordered) - 1) * q
        lo = int(pos)
        return ordered[lo] + (ordered[min(lo + 1, len(ordered) - 1)] - ordered[lo]) * (pos - lo)
    return {"samples": len(values), "p50_ms": round(percentile(.5), 3),
            "p95_ms": round(percentile(.95), 3),
            "mean_ms": round(statistics.fmean(values), 3) if values else 0.0,
            "max_ms": round(max(values, default=0), 3)}


@contextmanager
def readonly_connection(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


class SearchSession:
    """Count all executed SQL, including search helper SQL; exclude setup PRAGMA."""
    def __init__(self, path: Path):
        self.path = path
        self.statements = 0

    @contextmanager
    def open_db(self):
        with readonly_connection(self.path) as conn:
            conn.set_trace_callback(self.trace)
            yield conn

    def trace(self, statement: str) -> None:
        self.statements += 1

    def search(self, case: dict[str, Any], limit: int) -> tuple[dict[str, Any], float, int]:
        self.statements = 0
        start = time.perf_counter()
        with patch.object(server, "open_db", self.open_db):
            result = server.search_ncs(query=case["query"], scope="all", limit=limit,
                                       classification_filter=case.get("classification_filter"))
        return result, (time.perf_counter() - start) * 1000, self.statements


def evidence_sql(results: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    if not results:
        return "", []
    if len(results) > MAX_CANDIDATES:
        raise ValueError("candidate budget exceeded")
    params: list[Any] = []
    for rank, row in enumerate(results, 1):
        path = row.get("path") or {}
        unit = row["id"] if row["type"] == "unit" else path.get("unit_code")
        element = row["id"] if row["type"] == "element" else path.get("element_id")
        params.extend((rank, str(unit or ""), element))
    values = ",".join("(?,?,?)" for _ in results)
    # Unit expansion and direct element lookup use separate indexed branches.
    # Per-element row caps avoid multiplying criteria and KSA into a cross join.
    sql = f"""
    WITH targets(candidate_rank,unit_code,element_id) AS (VALUES {values}),
    elements AS (
      SELECT t.candidate_rank, ce.element_id, ce.unit_code, ce.element_name_raw
      FROM targets t JOIN competency_elements ce ON ce.element_id=t.element_id
      WHERE t.element_id IS NOT NULL AND ce.unit_code=t.unit_code
      UNION ALL
      SELECT t.candidate_rank, ce.element_id, ce.unit_code, ce.element_name_raw
      FROM targets t JOIN competency_elements ce ON ce.element_id IN (
        SELECT element_id FROM competency_elements WHERE unit_code=t.unit_code
        ORDER BY element_id LIMIT {MAX_ELEMENTS})
      WHERE t.element_id IS NULL
    )
    SELECT t.candidate_rank, 'unit' AS layer, cu.unit_code AS evidence_id,
      NULL AS element_id, substr(cu.unit_name_raw,1,{MAX_TEXT_CHARS}) AS evidence_text,
      length(cu.unit_name_raw)>{MAX_TEXT_CHARS} AS text_truncated,
      c.major_code,c.middle_code,c.small_code,c.sub_code,
      c.major_name,c.middle_name,c.small_name,c.sub_name
    FROM targets t JOIN competency_units cu ON cu.unit_code=t.unit_code
    JOIN classifications c ON c.classification_id=cu.classification_id
    UNION ALL
    SELECT e.candidate_rank,'element',e.element_id,e.element_id,
      substr(e.element_name_raw,1,{MAX_TEXT_CHARS}),length(e.element_name_raw)>{MAX_TEXT_CHARS},
      NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL FROM elements e
    UNION ALL
    SELECT e.candidate_rank,'criteria',pc.criteria_id,e.element_id,
      substr(pc.criteria_text_raw,1,{MAX_TEXT_CHARS}),length(pc.criteria_text_raw)>{MAX_TEXT_CHARS},
      NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
    FROM elements e JOIN performance_criteria pc ON pc.criteria_id IN (
      SELECT criteria_id FROM performance_criteria WHERE element_id=e.element_id
      ORDER BY criteria_id LIMIT {MAX_ROWS_PER_ELEMENT})
    UNION ALL
    SELECT e.candidate_rank,'ksa',ki.ksa_id,e.element_id,
      substr(ki.ksa_text_raw,1,{MAX_TEXT_CHARS}),length(ki.ksa_text_raw)>{MAX_TEXT_CHARS},
      NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL
    FROM elements e JOIN ksa_items ki ON ki.ksa_id IN (
      SELECT ksa_id FROM ksa_items WHERE element_id=e.element_id
      ORDER BY ksa_id LIMIT {MAX_ROWS_PER_ELEMENT})
    """
    return sql, params


def score_evidence(result: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    tokens = list(dict.fromkeys(_normalize_ncs_search_query(result["query"])[2]))[:MAX_TOKENS]
    specific = [token for token in tokens if token.casefold() not in _NCS_SEARCH_GENERIC_TOKENS]
    normalized_tokens = {token: normalize_search_text(token) for token in tokens}
    # A unit and its element/criteria/KSA candidates often share evidence rows.
    # Normalize and boundary-match each distinct text once per shadow request.
    text_hits: dict[str, list[str]] = {}
    by_candidate: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_candidate[int(row["candidate_rank"])].append(row)
    candidates = []
    for rank, item in enumerate(result["results"], 1):
        evidence = by_candidate[rank]
        layers = Counter(row["layer"] for row in evidence)
        matches: dict[Any, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
        refs = []
        for row in evidence:
            text = row["evidence_text"] or ""
            if text not in text_hits:
                normalized_text = normalize_search_text(text)
                text_hits[text] = [t for t in tokens if _ncs_search_boundary_match_normalized(normalized_text, normalized_tokens[t])]
            hit = text_hits[text]
            if row["layer"] in ("criteria", "ksa"):
                matches[row["element_id"]][row["layer"]].update(hit)
            if hit and len(refs) < 8:
                refs.append({"layer": row["layer"], "id": row["evidence_id"],
                             "element_id": row["element_id"], "tokens": hit})
        best = 0.0
        for group in matches.values():
            task_hits, ksa_hits = group["criteria"], group["ksa"]
            joined = task_hits | ksa_hits
            # Two non-generic tokens and both layers in the SAME element are
            # needed. Co-location is still support, not a proven task-KSA edge.
            if task_hits and ksa_hits and len(joined.intersection(specific)) >= 2:
                best = max(best, len(joined.intersection(specific)) / max(len(specific), 1))
        unit_rows = [row for row in evidence if row["layer"] == "unit"]
        hard_filter = result.get("classification_filter") or {}
        filter_ok = bool(unit_rows) and all(
            str(unit_rows[0].get(key) or "") == str(value)
            for key, value in hard_filter.items())
        candidates.append({"type": item["type"], "id": item["id"],
                           "public_rank": rank, "shadow_score": round(best, 6),
                           "sample_counts": dict(layers),
                           "all_four_layers": all(layers[x] > 0 for x in ("unit", "element", "criteria", "ksa")),
                           "classification_filter_preserved": filter_ok,
                           "matched_evidence_refs": refs,
                           "support_kind": "same_element_cooccurrence_unreviewed"})
    # Preserve type interleaving and match-mode positions even in hypothetical
    # ranking. No result object, field, classification filter, or source changes.
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for index, item in enumerate(result["results"]):
        groups[(item["type"], str(item.get("match_mode")))].append(index)
    shadow = list(range(len(candidates)))
    for positions in groups.values():
        ranked = sorted(positions, key=lambda i: (-candidates[i]["shadow_score"], i))
        for target, source in zip(positions, ranked):
            shadow[target] = source
    for shadow_rank, index in enumerate(shadow, 1):
        candidates[index]["shadow_rank"] = shadow_rank
    return {"tokens": tokens, "specific_tokens": specific, "candidates": candidates,
            "evidence_rows": len(rows),
            "distinct_evidence_texts": len(text_hits),
            "text_truncated_rows": sum(bool(row["text_truncated"]) for row in rows),
            "hypothetical_moved_candidates": sum(c["public_rank"] != c["shadow_rank"] for c in candidates)}


def shadow_profile(path: Path, result: dict[str, Any], budget_ms: float = SQL_BUDGET_MS) -> dict[str, Any]:
    before = fingerprint(result)
    start = time.perf_counter()
    sql, params = evidence_sql(result["results"])
    statements = 0
    rows = []
    budget_exceeded = False
    sql_ms = 0.0
    if sql:
        with readonly_connection(path) as conn:
            deadline = time.perf_counter() + budget_ms / 1000
            conn.set_progress_handler(lambda: int(time.perf_counter() >= deadline), 1000)
            statements = 1
            sql_start = time.perf_counter()
            try:
                rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            except sqlite3.OperationalError as exc:
                if "interrupted" not in str(exc).lower():
                    raise
                budget_exceeded = True
            finally:
                sql_ms = (time.perf_counter() - sql_start) * 1000
                conn.set_progress_handler(None, 0)
    score_start = time.perf_counter()
    payload = score_evidence(result, rows)
    score_ms = (time.perf_counter() - score_start) * 1000
    payload.update({"elapsed_ms": round((time.perf_counter() - start) * 1000, 3),
                    "sql_fetch_ms": round(sql_ms, 3), "python_score_ms": round(score_ms, 3),
                    "additional_sql_count": statements, "sql_budget_exceeded": budget_exceeded,
                    "public_contract_unchanged": before == fingerprint(result)})
    return payload


def promotion_gate(runs: list[dict[str, Any]]) -> dict[str, Any]:
    nonempty = [r for r in runs if r["shadow"]["additional_sql_count"]]
    added = summary([r["shadow"]["elapsed_ms"] for r in nonempty])
    checks = {
        "nonempty_measurements_present": bool(nonempty),
        "public_contract_parity": all(r["shadow"]["public_contract_unchanged"] and r["repeat_public_parity"] for r in runs),
        "classification_filter_parity": all(c["classification_filter_preserved"] for r in runs for c in r["shadow"]["candidates"]),
        "sql_increment_at_most_one": all(r["shadow"]["additional_sql_count"] <= 1 for r in runs),
        "no_sql_timeouts": all(not r["shadow"]["sql_budget_exceeded"] for r in runs),
        "added_p50_at_most_10ms": added["p50_ms"] <= 10,
        "added_p95_at_most_25ms": added["p95_ms"] <= 25,
        "independent_relevance_validation": False,
    }
    # Never infer GO for public ranking from unlabeled synthetic availability.
    return {"decision": "GO" if all(checks.values()) else "HOLD",
            "latency_gate_population": "warm nonempty requests; empty controls cannot dilute latency",
            "cost_gate_pass": all(v for k, v in checks.items() if k != "independent_relevance_validation"),
            "checks": checks,
            "requirements": ["Independent untouched relevance evaluation: no per-case regression and positive aggregate NDCG/MRR change.",
                             "Public order and classification-filter contract tests pass before any separate rollout decision.",
                             "Additional SQL <=1; added p50 <=10 ms and p95 <=25 ms; no timeout.",
                             "All-scope and concurrent-serving validation; sampled coverage is not full-corpus recall."],
            "reason": "Synthetic evidence coverage does not establish ranking quality; no public promotion performed."}


def build_report(db: Path, repetitions: int = 3, limit: int = 16, *,
                 expected_sha256: str = EXPECTED_SHA256,
                 expected_size_bytes: int = EXPECTED_SIZE) -> dict[str, Any]:
    before = file_identity(db)
    if (before["sha256"] != expected_sha256
            or before["size_bytes"] != expected_size_bytes):
        raise ValueError("Builder DB identity mismatch: experiment stopped")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if fixture.get("non_holdout") is not True:
        raise ValueError("synthetic-only fixture required")
    session = SearchSession(db)
    runs = []
    cold = []
    for case in fixture["cases"]:
        initial, elapsed, statements = session.search(case, limit)
        cold.append({"case_id": case["id"], "elapsed_ms": round(elapsed, 3), "sql_count": statements})
        # Warm both baseline and shadow once. Report cold SQL timings separately.
        cold_shadow = shadow_profile(db, initial)
        cold[-1]["shadow"] = {key: cold_shadow[key] for key in ("elapsed_ms", "additional_sql_count", "sql_budget_exceeded")}
        for repeat in range(repetitions):
            result, baseline_ms, baseline_sql = session.search(case, limit)
            shadow = shadow_profile(db, result)
            runs.append({"case_id": case["id"], "query": case["query"], "repeat": repeat,
                         "classification_filter": result.get("classification_filter"),
                         "baseline_ms": round(baseline_ms, 3), "baseline_sql_count": baseline_sql,
                         "baseline_plus_shadow_ms": round(baseline_ms + shadow["elapsed_ms"], 3),
                         "public_order_fingerprint": fingerprint([(r["type"], r["id"]) for r in result["results"]]),
                         "repeat_public_parity": fingerprint(result) == fingerprint(initial), "shadow": shadow})
    after = file_identity(db)
    if after != before:
        raise ValueError("Builder DB changed during experiment: results invalid")
    first_runs = [run for run in runs if run["repeat"] == 0]
    candidates = [c for run in first_runs for c in run["shadow"]["candidates"]]
    by_type = {}
    for kind in ("unit", "element", "criteria", "ksa"):
        group = [c for c in candidates if c["type"] == kind]
        by_type[kind] = {"candidates": len(group), "all_four_layers": sum(c["all_four_layers"] for c in group),
                         "eligible_same_element_support": sum(c["shadow_score"] > 0 for c in group)}
    report = {"schema": "ncs_search_task_ksa_shadow_profile_v1", "generated_at": datetime.now(timezone.utc).isoformat(),
              "decision": "HOLD", "db_before": before, "db_after": after, "db_identity_unchanged": before == after,
              "policy": {"shadow_only": True, "db_writes": False, "public_ranking_changed": False,
                         "alias_added": False, "holdout_opened": False, "human_status_writes": False,
                         "framework_reference_used_as_scored_data": False},
              "fixture": {"path": str(FIXTURE.relative_to(ROOT)), "sha256": file_identity(FIXTURE)["sha256"],
                          "case_count": len(fixture["cases"]), "provenance": fixture["provenance"]},
              "implementation": {"profiler_sha256": file_identity(Path(__file__))["sha256"],
                                 "search_core_sha256": file_identity(ROOT / "src/ncs_mcp/search/core.py")["sha256"]},
              "bounds": {"candidate_limit": limit, "maximum_candidates": MAX_CANDIDATES,
                         "elements_per_unit": MAX_ELEMENTS, "criteria_and_ksa_each_per_element": MAX_ROWS_PER_ELEMENT,
                         "chars_per_evidence": MAX_TEXT_CHARS, "tokens": MAX_TOKENS, "sql_deadline_ms": SQL_BUDGET_MS,
                         "sampling_order": "source primary key; evidence availability is a bounded lower bound"},
              "measurement": {"repetitions": repetitions, "cold_runs": cold,
                              "cache_definition": "cold means first call per query; OS cache was not flushed. Each call opens a fresh read-only connection.",
                              "warm_baseline": summary([r["baseline_ms"] for r in runs]),
                              "warm_additional_shadow": summary([r["shadow"]["elapsed_ms"] for r in runs]),
                              "warm_nonempty_additional_shadow": summary([r["shadow"]["elapsed_ms"] for r in runs if r["shadow"]["additional_sql_count"]]),
                              "warm_sql_fetch": summary([r["shadow"]["sql_fetch_ms"] for r in runs]),
                              "warm_python_score": summary([r["shadow"]["python_score_ms"] for r in runs]),
                              "warm_baseline_plus_shadow": summary([r["baseline_plus_shadow_ms"] for r in runs]),
                              "baseline_sql_counts": sorted(set(r["baseline_sql_count"] for r in runs)),
                              "additional_sql_counts": sorted(set(r["shadow"]["additional_sql_count"] for r in runs)),
                              "timeout_count": sum(r["shadow"]["sql_budget_exceeded"] for r in runs)},
              "candidate_coverage": {"denominator": "returned public candidates, first warm repetition only",
                                     "candidates": len(candidates), "by_type": by_type,
                                     "all_four_layers_ratio": sum(c["all_four_layers"] for c in candidates) / max(len(candidates), 1),
                                     "eligible_support_ratio": sum(c["shadow_score"] > 0 for c in candidates) / max(len(candidates), 1)},
              "promotion_gate": promotion_gate(runs),
              "limitations": ["No relevance labels or holdout cases inspected; no precision/MRR/NDCG improvement claim.",
                              "Only the returned page is scored; candidates absent from public retrieval cannot be recovered.",
                              "Same-element co-occurrence is unreviewed supporting context, not a direct criteria-KSA relation.",
                              "Fixed source-ID samples and 512-character truncation can omit relevant evidence.",
                              "SQL is cancellable; connection setup and Python scoring are measured but not preempted.",
                              "Separate read-only connection cost is included; host OS cache and concurrent work affect timings.",
                              "HR-focused synthetic scope is not all-major relevance validation.",
                              "Prior context shadow +110 ms / +1 SQL HOLD is background supplied by task, not remeasured here."],
              "runs": runs}
    return report


def markdown(report: dict[str, Any]) -> str:
    m, coverage = report["measurement"], report["candidate_coverage"]
    lines = ["# Task/KSA search rerank shadow — HOLD", "", report["promotion_gate"]["reason"], "",
             "Public search output is unchanged. SQLite was opened mode=ro/query_only; no aliases or review statuses were written.", "",
             f"DB SHA-256 before/after: `{report['db_before']['sha256']}`; size {report['db_before']['size_bytes']:,} bytes; identical.", "",
             "| Warm measurement | p50 ms | p95 ms |", "| --- | ---: | ---: |"]
    for key in ("warm_baseline", "warm_additional_shadow", "warm_nonempty_additional_shadow", "warm_sql_fetch", "warm_python_score", "warm_baseline_plus_shadow"):
        lines.append(f"| {key} | {m[key]['p50_ms']} | {m[key]['p95_ms']} |")
    lines += ["", f"Baseline SQL counts: {m['baseline_sql_counts']}; added SQL: {m['additional_sql_counts']}; timeouts: {m['timeout_count']}.", "",
              report["promotion_gate"]["latency_gate_population"], "", m["cache_definition"], "",
              (f"Bounds: at most {report['bounds']['maximum_candidates']} candidates "
               f"({report['bounds']['candidate_limit']} measured), "
               f"{report['bounds']['elements_per_unit']} elements per unit, "
               f"{report['bounds']['criteria_and_ksa_each_per_element']} criteria and KSA rows per element, "
               f"{report['bounds']['chars_per_evidence']} characters per row, "
               f"{report['bounds']['tokens']} query tokens. SQL progress deadline: "
               f"{report['bounds']['sql_deadline_ms']:g} ms."), "",
              f"Candidate coverage: {coverage['candidates']} returned candidates; all four evidence layers {coverage['all_four_layers_ratio']:.1%}; eligible same-element support {coverage['eligible_support_ratio']:.1%}.", "",
              "| Type | Candidates | Four layers | Eligible support |", "| --- | ---: | ---: | ---: |"]
    for kind, row in coverage["by_type"].items():
        lines.append(f"| {kind} | {row['candidates']} | {row['all_four_layers']} | {row['eligible_same_element_support']} |")
    lines += ["", "## Promotion checks", ""]
    lines.extend(f"- {key}: {value}" for key, value in report["promotion_gate"]["checks"].items())
    lines += ["", "## Required before promotion", ""]
    lines.extend(f"- {value}" for value in report["promotion_gate"]["requirements"])
    lines += ["", "## Limits", ""]
    lines.extend(f"- {value}" for value in report["limitations"])
    lines += ["", "Reproduce: `python scripts/profile_task_ksa_search_rerank.py --repetitions 3 --limit 16`", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--expected-sha256", default=EXPECTED_SHA256)
    parser.add_argument("--expected-size-bytes", type=int, default=EXPECTED_SIZE)
    args = parser.parse_args()
    if not 1 <= args.limit <= MAX_CANDIDATES or not 1 <= args.repetitions <= 10:
        parser.error("limit must be 1..24 and repetitions 1..10")
    if len(args.expected_sha256) != 64 or args.expected_size_bytes <= 0:
        parser.error("expected Builder DB identity is invalid")
    report = build_report(
        args.db,
        args.repetitions,
        args.limit,
        expected_sha256=args.expected_sha256.lower(),
        expected_size_bytes=args.expected_size_bytes,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.out.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps({"decision": report["decision"], "measurement": report["measurement"],
                      "candidate_coverage": report["candidate_coverage"], "gate": report["promotion_gate"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
