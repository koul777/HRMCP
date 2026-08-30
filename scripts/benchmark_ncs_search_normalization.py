from __future__ import annotations

import argparse
import json
import math
import os
import platform
import sqlite3
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


SCHEMA = "ncs_search_normalization_experiment_v1"
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_OUT = ROOT / "reports" / "ncs_search_normalization_experiment_20260830.json"
DEFAULT_MARKDOWN_OUT = (
    ROOT / "reports" / "ncs_search_normalization_experiment_20260830.md"
)

# These are query variants, not relevance labels. Each actual pair points at a
# source string observed in the local NCS DB. The synthetic pair exercises the
# short ASCII-token edge case without asserting that a matching row should exist.
DEFAULT_QUERY_PAIRS: tuple[dict[str, str], ...] = (
    {
        "pair_id": "actual_slash_unit",
        "kind": "actual",
        "feature": "slash",
        "scope": "unit",
        "source_example": "의료정보/의무기록 열람·제공·교류",
        "a": "의료정보/의무기록",
        "b": "의료정보 의무기록",
    },
    {
        "pair_id": "actual_hyphen_unit",
        "kind": "actual",
        "feature": "hyphen",
        "scope": "unit",
        "source_example": "캐릭터홍보-마케팅",
        "a": "캐릭터홍보-마케팅",
        "b": "캐릭터홍보 마케팅",
    },
    {
        "pair_id": "actual_middle_dot_unit",
        "kind": "actual",
        "feature": "middle_dot",
        "scope": "unit",
        "source_example": "의료정보/의무기록 열람·제공·교류",
        "a": "열람·제공·교류",
        "b": "열람 제공 교류",
    },
    {
        "pair_id": "actual_parentheses_unit",
        "kind": "actual",
        "feature": "parentheses",
        "scope": "unit",
        "source_example": "수요예측(Book Building)",
        "a": "수요예측(Book Building)",
        "b": "수요예측 Book Building",
    },
    {
        "pair_id": "actual_multiple_space_element",
        "kind": "actual",
        "feature": "multiple_space",
        "scope": "element",
        "source_example": "교육/훈련 인지정 요건 적용하기",
        "a": "교육/훈련 인지정",
        "b": "교육   훈련   인지정",
    },
    {
        "pair_id": "actual_nfkc_fullwidth_unit",
        "kind": "actual",
        "feature": "nfkc_fullwidth",
        "scope": "unit",
        "source_example": "가상현실 UI/UX 디자인",
        "a": "ＵＩ／ＵＸ 디자인",
        "b": "UI/UX 디자인",
    },
    {
        "pair_id": "actual_two_syllable_korean",
        "kind": "actual",
        "feature": "two_syllable_korean",
        "scope": "unit",
        "source_example": "채용",
        "a": "채용",
        "b": "  채용  ",
    },
    {
        "pair_id": "synthetic_short_ascii_slash",
        "kind": "synthetic",
        "feature": "short_ascii_tokens",
        "scope": "unit",
        "source_example": "synthetic normalization probe only",
        "a": "A/B",
        "b": "A B",
    },
)

STRATEGIES = (
    "token_fallback_control",
    "query_pattern_expansion",
    "db_expression_normalization",
)


def generated_at() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil((percentile_value / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    rounded = [round(float(value), 3) for value in values]
    return {
        "samples": rounded,
        "sample_count": len(rounded),
        "p50": percentile(rounded, 50),
        "p95": percentile(rounded, 95),
        "min": round(min(rounded), 3) if rounded else None,
        "max": round(max(rounded), 3) if rounded else None,
    }


def integer_summary(values: list[int]) -> dict[str, Any]:
    return {
        "samples": list(values),
        "sample_count": len(values),
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "stable": len(set(values)) <= 1,
    }


def result_ids(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    return [f"{row.get('type') or 'unknown'}:{row.get('id')}" for row in rows]


def result_match_signature(payload: dict[str, Any]) -> list[str]:
    rows = payload.get("results")
    if not isinstance(rows, list):
        return []
    return [str(row.get("match_mode") or "unknown") for row in rows]


class RuntimeSearchHarness:
    def __init__(self, server: Any) -> None:
        self.server = server
        self.original_open_db = server.open_db
        self.original_tier_predicates = server._ncs_search_tier_predicates
        self.original_like_any = server._ncs_search_like_any
        self.sql_statement_count = 0

        @contextmanager
        def traced_open_db() -> Any:
            with self.original_open_db() as conn:
                conn.create_function(
                    "NCS_NORM",
                    1,
                    server._normalize_ncs_search_text,
                    deterministic=True,
                )

                def trace(statement: str) -> None:
                    normalized = statement.lstrip().upper()
                    if normalized.startswith(("SELECT", "WITH")):
                        self.sql_statement_count += 1

                conn.set_trace_callback(trace)
                try:
                    yield conn
                finally:
                    conn.set_trace_callback(None)

        self.traced_open_db = traced_open_db

    def restore(self) -> None:
        self.server.open_db = self.original_open_db
        self.server._ncs_search_tier_predicates = self.original_tier_predicates
        self.server._ncs_search_like_any = self.original_like_any

    def activate(self, strategy: str) -> None:
        self.restore()
        self.server.open_db = self.traced_open_db
        if strategy == "token_fallback_control":
            return
        if strategy == "query_pattern_expansion":
            original = self.original_tier_predicates
            server = self.server

            def expanded_tier_predicates(
                columns: tuple[str, ...],
                phrase: str,
                fallback_tokens: list[str],
            ) -> list[tuple[int, str, dict[str, Any]]]:
                tiers = original(columns, phrase, fallback_tokens)
                if len(fallback_tokens) < 2:
                    return tiers
                first_tier, phrase_clause, phrase_params = tiers[0]
                expanded_params = dict(phrase_params)
                expanded_params["separator_variant_pattern"] = "%" + "%".join(
                    server._escape_ncs_search_like(token)
                    for token in fallback_tokens
                ) + "%"
                variant_clause = server._ncs_search_like_any(
                    columns, "separator_variant_pattern"
                )
                return [
                    (
                        first_tier,
                        f"({variant_clause} OR {phrase_clause})",
                        expanded_params,
                    ),
                    *tiers[1:],
                ]

            self.server._ncs_search_tier_predicates = expanded_tier_predicates
            return
        if strategy == "db_expression_normalization":

            def normalized_like_any(
                columns: tuple[str, ...], parameter: str
            ) -> str:
                return "(" + " OR ".join(
                    "NCS_NORM(COALESCE(" + column + ", '')) "
                    f"LIKE :{parameter} ESCAPE '\\'"
                    for column in columns
                ) + ")"

            self.server._ncs_search_like_any = normalized_like_any
            return
        raise ValueError(f"unknown strategy: {strategy}")

    def search(
        self,
        *,
        strategy: str,
        query: str,
        scope: str,
        limit: int,
    ) -> tuple[dict[str, Any], float, int]:
        self.activate(strategy)
        self.sql_statement_count = 0
        started = time.perf_counter()
        try:
            payload = self.server.search_ncs(
                query=query,
                scope=scope,
                limit=limit,
                offset=0,
            )
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            sql_calls = self.sql_statement_count
            self.restore()
        return payload, elapsed_ms, sql_calls


def benchmark_variant(
    harness: RuntimeSearchHarness,
    *,
    strategy: str,
    pair: dict[str, str],
    variant_label: str,
    query: str,
    runs: int,
    limit: int,
) -> dict[str, Any]:
    # One unrecorded warm-up keeps this a local warm comparison. It is not a
    # Vercel cold-start measurement.
    harness.search(
        strategy=strategy,
        query=query,
        scope=pair["scope"],
        limit=limit,
    )
    elapsed_samples: list[float] = []
    sql_samples: list[int] = []
    id_samples: list[list[str]] = []
    mode_samples: list[list[str]] = []
    payload: dict[str, Any] = {}
    for _ in range(runs):
        payload, elapsed_ms, sql_calls = harness.search(
            strategy=strategy,
            query=query,
            scope=pair["scope"],
            limit=limit,
        )
        elapsed_samples.append(elapsed_ms)
        sql_samples.append(sql_calls)
        id_samples.append(result_ids(payload))
        mode_samples.append(result_match_signature(payload))
    ids = id_samples[-1] if id_samples else []
    modes = mode_samples[-1] if mode_samples else []
    return {
        "strategy": strategy,
        "pair_id": pair["pair_id"],
        "pair_kind": pair["kind"],
        "feature": pair["feature"],
        "variant": variant_label,
        "query": query,
        "scope": pair["scope"],
        "source_example": pair["source_example"],
        "normalized_query": payload.get("normalized_query"),
        "query_tokens": payload.get("query_tokens"),
        "result_count": len(ids),
        "zero_hit": not ids,
        "exact_ids_in_order": ids,
        "preview_ids": ids[:5],
        "match_tier_proxy": {
            "payload_match_mode": payload.get("match_mode"),
            "result_match_mode_counts": dict(sorted(Counter(modes).items())),
            "note": "Public match_mode is the non-private proxy for SQL _match_tier.",
        },
        "elapsed_ms": latency_summary(elapsed_samples),
        "sql_call_proxy": integer_summary(sql_samples),
        "deterministic_ids": len({tuple(item) for item in id_samples}) <= 1,
        "deterministic_match_modes": len({tuple(item) for item in mode_samples}) <= 1,
    }


def compare_pair_records(
    records: list[dict[str, Any]],
    query_pairs: list[dict[str, str]],
) -> list[dict[str, Any]]:
    index = {
        (record["strategy"], record["pair_id"], record["variant"]): record
        for record in records
    }
    comparisons: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        for pair in query_pairs:
            a = index[(strategy, pair["pair_id"], "a")]
            b = index[(strategy, pair["pair_id"], "b")]
            a_ids = a["exact_ids_in_order"]
            b_ids = b["exact_ids_in_order"]
            union = set(a_ids) | set(b_ids)
            intersection = set(a_ids) & set(b_ids)
            comparisons.append(
                {
                    "strategy": strategy,
                    "pair_id": pair["pair_id"],
                    "feature": pair["feature"],
                    "same_exact_ids": set(a_ids) == set(b_ids),
                    "same_exact_order": a_ids == b_ids,
                    "result_jaccard": (
                        round(len(intersection) / len(union), 4) if union else 1.0
                    ),
                    "same_zero_hit": a["zero_hit"] == b["zero_hit"],
                    "same_match_tier_distribution": (
                        a["match_tier_proxy"]["result_match_mode_counts"]
                        == b["match_tier_proxy"]["result_match_mode_counts"]
                    ),
                }
            )
    return comparisons


def compare_strategies(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index = {
        (record["strategy"], record["pair_id"], record["variant"]): record
        for record in records
    }
    comparisons: list[dict[str, Any]] = []
    for strategy in STRATEGIES[1:]:
        for key, control in index.items():
            if key[0] != "token_fallback_control":
                continue
            candidate = index[(strategy, key[1], key[2])]
            control_ids = control["exact_ids_in_order"]
            candidate_ids = candidate["exact_ids_in_order"]
            control_p50 = control["elapsed_ms"]["p50"]
            candidate_p50 = candidate["elapsed_ms"]["p50"]
            latency_delta_pct = None
            if control_p50 not in {None, 0} and candidate_p50 is not None:
                latency_delta_pct = round(
                    ((candidate_p50 - control_p50) / control_p50) * 100.0, 2
                )
            comparisons.append(
                {
                    "strategy": strategy,
                    "pair_id": key[1],
                    "variant": key[2],
                    "same_exact_ids": set(control_ids) == set(candidate_ids),
                    "same_exact_order": control_ids == candidate_ids,
                    "added_ids": [item for item in candidate_ids if item not in control_ids],
                    "removed_ids": [item for item in control_ids if item not in candidate_ids],
                    "zero_hit_changed": control["zero_hit"] != candidate["zero_hit"],
                    "match_tier_distribution_changed": (
                        control["match_tier_proxy"]["result_match_mode_counts"]
                        != candidate["match_tier_proxy"]["result_match_mode_counts"]
                    ),
                    "p50_latency_delta_pct": latency_delta_pct,
                    "sql_call_proxy_delta": (
                        (candidate["sql_call_proxy"]["max"] or 0)
                        - (control["sql_call_proxy"]["max"] or 0)
                    ),
                }
            )
    return comparisons


def aggregate_metrics(
    records: list[dict[str, Any]],
    pair_comparisons: list[dict[str, Any]],
    strategy_comparisons: list[dict[str, Any]],
) -> dict[str, Any]:
    by_strategy: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        strategy_records = [row for row in records if row["strategy"] == strategy]
        strategy_pairs = [
            row for row in pair_comparisons if row["strategy"] == strategy
        ]
        p50_values = [
            row["elapsed_ms"]["p50"]
            for row in strategy_records
            if row["elapsed_ms"]["p50"] is not None
        ]
        by_strategy[strategy] = {
            "variant_count": len(strategy_records),
            "zero_hit_count": sum(bool(row["zero_hit"]) for row in strategy_records),
            "deterministic_variant_count": sum(
                bool(row["deterministic_ids"]) for row in strategy_records
            ),
            "pair_exact_order_parity_count": sum(
                bool(row["same_exact_order"]) for row in strategy_pairs
            ),
            "pair_count": len(strategy_pairs),
            "median_variant_p50_ms": percentile(p50_values, 50),
            "max_sql_calls_per_search": max(
                (row["sql_call_proxy"]["max"] or 0 for row in strategy_records),
                default=0,
            ),
        }

    candidate_summary: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES[1:]:
        rows = [row for row in strategy_comparisons if row["strategy"] == strategy]
        deltas = [
            row["p50_latency_delta_pct"]
            for row in rows
            if row["p50_latency_delta_pct"] is not None
        ]
        candidate_summary[strategy] = {
            "comparison_count": len(rows),
            "result_set_change_count": sum(
                not bool(row["same_exact_ids"]) for row in rows
            ),
            "order_change_count": sum(
                not bool(row["same_exact_order"]) for row in rows
            ),
            "zero_hit_change_count": sum(bool(row["zero_hit_changed"]) for row in rows),
            "match_tier_change_count": sum(
                bool(row["match_tier_distribution_changed"]) for row in rows
            ),
            "median_p50_latency_delta_pct": percentile(deltas, 50),
            "max_sql_call_proxy_delta": max(
                (int(row["sql_call_proxy_delta"]) for row in rows), default=0
            ),
        }
    return {"by_strategy": by_strategy, "candidate_summary": candidate_summary}


def decide(metrics: dict[str, Any]) -> dict[str, Any]:
    candidates = metrics["candidate_summary"]
    qualifying: list[str] = []
    for strategy, summary in candidates.items():
        latency_delta = summary["median_p50_latency_delta_pct"]
        if (
            summary["zero_hit_change_count"] > 0
            and summary["result_set_change_count"] > 0
            and summary["max_sql_call_proxy_delta"] <= 0
            and latency_delta is not None
            and latency_delta <= 10.0
        ):
            qualifying.append(strategy)
    if qualifying:
        recommendation = "conditional_candidate_requires_relevance_judgment"
        rationale = (
            "A candidate changed zero-hit behavior within the mechanical latency/SQL guardrail, "
            "but no human relevance labels exist. Validate Recall@K/MRR before promotion."
        )
    else:
        recommendation = "keep_current_token_fallback"
        rationale = (
            "No candidate demonstrated a judged accuracy gain within the latency and SQL-call "
            "guardrail. Token fallback remains the safer deterministic behavior."
        )
    return {
        "recommendation": recommendation,
        "qualifying_candidates": qualifying,
        "promotion_approved": False,
        "rationale": rationale,
        "required_before_promotion": [
            "human-labeled punctuation relevance cases",
            "Recall@5/10 and MRR@10 comparison",
            "remote warm and cold latency comparison",
        ],
    }


def build_report(
    *,
    db_path: Path,
    runs: int,
    limit: int,
    query_pairs: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(db_path)
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp import server

    pairs = query_pairs or [dict(item) for item in DEFAULT_QUERY_PAIRS]
    harness = RuntimeSearchHarness(server)
    records: list[dict[str, Any]] = []
    try:
        for strategy in STRATEGIES:
            for pair in pairs:
                for variant_label in ("a", "b"):
                    records.append(
                        benchmark_variant(
                            harness,
                            strategy=strategy,
                            pair=pair,
                            variant_label=variant_label,
                            query=pair[variant_label],
                            runs=runs,
                            limit=limit,
                        )
                    )
    finally:
        harness.restore()
    pair_comparisons = compare_pair_records(records, pairs)
    strategy_comparisons = compare_strategies(records)
    metrics = aggregate_metrics(records, pair_comparisons, strategy_comparisons)
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "mode": "read_only_local_warm_experiment",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "database_path": str(db_path),
            "database_bytes": db_path.stat().st_size,
            "caveat": (
                "Result counts and IDs are candidate-recall proxies, not relevance judgments. "
                "Latency is local warm storage and is not a Vercel absolute measurement."
            ),
        },
        "experiment": {
            "runs_per_variant": runs,
            "limit": limit,
            "strategies": list(STRATEGIES),
            "strategy_definitions": {
                "token_fallback_control": (
                    "Current NFKC/punctuation query normalization with SQL phrase, token-AND, "
                    "and token-OR fallback."
                ),
                "query_pattern_expansion": (
                    "Adds a separator-tolerant %token1%token2% pattern as phrase tier while "
                    "retaining current token fallback."
                ),
                "db_expression_normalization": (
                    "Applies the current normalization function to DB search columns through "
                    "a deterministic SQLite UDF while retaining current token fallback."
                ),
            },
            "query_pairs": pairs,
            "records": records,
            "pair_comparisons": pair_comparisons,
            "strategy_comparisons": strategy_comparisons,
        },
        "metrics": metrics,
        "decision": decide(metrics),
        "safety": {
            "database_open_mode": "read_only",
            "database_writes": False,
            "product_code_changes": False,
            "status_updates": False,
            "human_review_claim": False,
            "monkeypatch_scope": "process_local_and_restored_after_each_search",
        },
        "commands": {
            "reproduce": (
                "python scripts/benchmark_ncs_search_normalization.py "
                f"--db \"{db_path}\" --runs {runs} --limit {limit}"
            )
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NCS search punctuation/normalization experiment",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Schema: `{report['schema']}`",
        f"- DB: `{report['environment']['database_path']}`",
        f"- Runs per variant: `{report['experiment']['runs_per_variant']}`",
        f"- Decision: **{report['decision']['recommendation']}**",
        f"- Promotion approved: `{str(report['decision']['promotion_approved']).lower()}`",
        "",
        "## Strategy summary",
        "",
        "| Strategy | Zero hits | Pair order parity | Median p50 (ms) | Max SQL calls |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for strategy in STRATEGIES:
        metric = report["metrics"]["by_strategy"][strategy]
        lines.append(
            f"| {strategy} | {metric['zero_hit_count']} | "
            f"{metric['pair_exact_order_parity_count']}/{metric['pair_count']} | "
            f"{metric['median_variant_p50_ms']} | {metric['max_sql_calls_per_search']} |"
        )
    lines.extend(
        [
            "",
            "## Candidate delta from current control",
            "",
            "| Candidate | Result-set changes | Order changes | Tier changes | "
            "Median p50 delta | Max SQL delta |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for strategy in STRATEGIES[1:]:
        metric = report["metrics"]["candidate_summary"][strategy]
        lines.append(
            f"| {strategy} | {metric['result_set_change_count']} | "
            f"{metric['order_change_count']} | {metric['match_tier_change_count']} | "
            f"{metric['median_p50_latency_delta_pct']}% | "
            f"{metric['max_sql_call_proxy_delta']} |"
        )
    lines.extend(
        [
            "",
            "## Pair parity by strategy",
            "",
            "| Strategy | Pair | Feature | Same IDs | Same order | Same tier mix | Jaccard |",
            "| --- | --- | --- | --- | --- | --- | ---: |",
        ]
    )
    for row in report["experiment"]["pair_comparisons"]:
        lines.append(
            f"| {row['strategy']} | {row['pair_id']} | {row['feature']} | "
            f"{row['same_exact_ids']} | {row['same_exact_order']} | "
            f"{row['same_match_tier_distribution']} | {row['result_jaccard']} |"
        )
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            report["decision"]["rationale"],
            "",
            "This experiment does not claim Recall@K, MRR, or relevance improvement. "
            "Result IDs, zero-hit, and match tiers are mechanical proxies only.",
            "",
            "## Safety",
            "",
            "- Database writes: `false`",
            "- Product code changes: `false`",
            "- Human-review/status claims: `false`",
            "- Raw KSA mutation: `false`",
            "",
            "## Reproduce",
            "",
            f"`{report['commands']['reproduce']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare NCS search punctuation normalization strategies read-only."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.runs < 1:
        raise ValueError("--runs must be >= 1")
    if args.limit < 1:
        raise ValueError("--limit must be >= 1")
    report = build_report(
        db_path=args.db.resolve(),
        runs=args.runs,
        limit=args.limit,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["decision"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
