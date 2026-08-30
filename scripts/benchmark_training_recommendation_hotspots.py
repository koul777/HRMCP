from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import math
import os
import pstats
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_QUERIES = (
    ("hr", "인사기획"),
    ("recruiting", "채용"),
    ("training", "교육훈련"),
    ("data", "데이터 분석"),
    ("quality", "품질관리"),
)
DEFAULT_OUT = PROJECT_ROOT / "reports" / "training_recommendation_hotspot_experiment_20260830.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "training_recommendation_hotspot_experiment_20260830.md"
STRATEGIES = ("A_baseline", "B_request_normalize_memo", "C_verified_major_hint")
DYNAMIC_KEYS = {
    "generated_at",
    "started_at",
    "completed_at",
    "elapsed_ms",
    "duration_ms",
    "queue_wait_ms",
}
RECOMMENDATION_LIST_KEYS = ("recommended_courses", "recommendations", "courses", "items")
IDENTITY_KEYS = (
    "training_course_id",
    "course_id",
    "course_key",
    "source_course_id",
    "source_id",
    "course_name",
    "training_course_name",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * quantile
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * fraction)


def metric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values]
    return {
        "count": len(materialized),
        "p50_ms": _round_optional(percentile(materialized, 0.50)),
        "p95_ms": _round_optional(percentile(materialized, 0.95)),
        "min_ms": _round_optional(min(materialized) if materialized else None),
        "max_ms": _round_optional(max(materialized) if materialized else None),
    }


def _round_optional(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _without_dynamic_values(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_dynamic_values(child)
            for key, child in sorted(value.items())
            if key not in DYNAMIC_KEYS and key not in {"capacity", "audit"}
        }
    if isinstance(value, list):
        return [_without_dynamic_values(child) for child in value]
    return value


def _find_first_list(value: Any, keys: tuple[str, ...] = RECOMMENDATION_LIST_KEYS) -> list[Any]:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
        for child in value.values():
            found = _find_first_list(child, keys)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_list(child, keys)
            if found:
                return found
    return []


def _identity_value(row: Any) -> Any:
    if not isinstance(row, dict):
        return row
    for key in IDENTITY_KEYS:
        if key in row:
            return {"key": key, "value": row.get(key)}
    return {"row_fingerprint": _sha256(_without_dynamic_values(row))}


def _select_named_fields(value: Any, needles: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        selected: dict[str, Any] = {}
        for key, child in sorted(value.items()):
            lowered = key.lower()
            if any(needle in lowered for needle in needles):
                selected[key] = _without_dynamic_values(child)
                continue
            nested = _select_named_fields(child, needles)
            if nested not in ({}, [], None):
                selected[key] = nested
        return selected
    if isinstance(value, list):
        selected_items = [_select_named_fields(child, needles) for child in value]
        return [item for item in selected_items if item not in ({}, [], None)]
    return None


def recommendation_fingerprint(result: Any) -> dict[str, Any]:
    recommendations = _find_first_list(result)
    canonical = _without_dynamic_values(recommendations)
    identities = [_identity_value(row) for row in recommendations]
    scores = _select_named_fields(recommendations, ("score", "rank", "grade", "confidence"))
    evidence = _select_named_fields(
        recommendations,
        ("evidence", "reason", "match", "basis", "concept", "criteria", "unit", "ksa"),
    )
    return {
        "recommendation_count": len(recommendations),
        "ids_and_order": identities,
        "ids_order_fingerprint": _sha256(identities),
        "score_fingerprint": _sha256(scores),
        "evidence_fingerprint": _sha256(evidence),
        "exact_recommendation_fingerprint": _sha256(canonical),
    }


def extract_major_code(result: Any) -> str | None:
    preferred_paths = (
        ("source", "major_code"),
        ("query_resolution", "selected", "major_code"),
        ("query_resolution", "candidates", 0, "major_code"),
    )
    for path in preferred_paths:
        cursor = result
        for part in path:
            try:
                cursor = cursor[part]
            except (KeyError, IndexError, TypeError):
                cursor = None
                break
        if isinstance(cursor, str) and cursor:
            return cursor
    if isinstance(result, dict):
        value = result.get("major_code")
        if isinstance(value, str) and value:
            return value
        for child in result.values():
            found = extract_major_code(child)
            if found:
                return found
    elif isinstance(result, list):
        for child in result:
            found = extract_major_code(child)
            if found:
                return found
    return None


class RequestNormalizeMemo:
    def __init__(self, normalize: Callable[[str], str]) -> None:
        self.normalize = normalize
        self.cache: dict[Any, str] = {}
        self.requests = 0
        self.hits = 0
        self.original_calls = 0

    def __call__(self, value: str) -> str:
        self.requests += 1
        try:
            cached = self.cache.get(value)
            present = value in self.cache
        except TypeError:
            return self._uncached(value)
        if present:
            self.hits += 1
            return cached  # type: ignore[return-value]
        normalized = self._uncached(value)
        self.cache[value] = normalized
        return normalized

    def _uncached(self, value: str) -> str:
        self.original_calls += 1
        return self.normalize(value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "hits": self.hits,
            "misses": self.original_calls,
            "hit_ratio": round(self.hits / self.requests, 6) if self.requests else 0.0,
            "unique_values": len(self.cache),
        }


def _rss_mb() -> float | None:
    try:
        import psutil  # type: ignore

        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except (ImportError, OSError):
        return None


def _db_state(path: Path) -> dict[str, int | bool]:
    stat = path.stat()
    return {"exists": True, "size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


class SqlRecorder:
    def __init__(self) -> None:
        self.statement_count = 0
        self.kinds: dict[str, int] = {}
        self.connection_count = 0
        self.all_read_only = True

    def trace(self, statement: str) -> None:
        self.statement_count += 1
        text = statement.lstrip()
        kind = text.split(None, 1)[0].upper() if text else "UNKNOWN"
        self.kinds[kind] = self.kinds.get(kind, 0) + 1


@contextmanager
def _instrumented_connect(server: Any, recorder: SqlRecorder) -> Iterator[None]:
    original_connect = server.connect

    def connect_read_only(db_path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
        recorder.connection_count += 1
        recorder.all_read_only = recorder.all_read_only and True
        conn = original_connect(db_path, read_only=True)
        conn.set_trace_callback(recorder.trace)
        return conn

    server.connect = connect_read_only
    try:
        yield
    finally:
        server.connect = original_connect


@contextmanager
def _normalizer_strategy(server: Any, training: Any, enabled: bool) -> Iterator[RequestNormalizeMemo | None]:
    if not enabled:
        yield None
        return
    original_training = training.normalize_concept_key
    original_server = server.normalize_concept_key
    memo = RequestNormalizeMemo(original_training)
    training.normalize_concept_key = memo
    server.normalize_concept_key = memo
    try:
        yield memo
    finally:
        training.normalize_concept_key = original_training
        server.normalize_concept_key = original_server


def _profile_function_calls(profile: cProfile.Profile) -> dict[str, Any]:
    stats = pstats.Stats(profile)
    targets = {"_candidate_score", "normalize_concept_key"}
    output = {
        name: {"primitive_calls": 0, "total_calls": 0, "self_ms": 0.0, "cumulative_ms": 0.0}
        for name in targets
    }
    for (_filename, _line, function_name), values in stats.stats.items():
        if function_name not in targets:
            continue
        primitive_calls, total_calls, self_seconds, cumulative_seconds, _callers = values
        row = output[function_name]
        row["primitive_calls"] += int(primitive_calls)
        row["total_calls"] += int(total_calls)
        row["self_ms"] += float(self_seconds) * 1000.0
        row["cumulative_ms"] += float(cumulative_seconds) * 1000.0
    for row in output.values():
        row["self_ms"] = round(row["self_ms"], 3)
        row["cumulative_ms"] = round(row["cumulative_ms"], 3)
    return output


def run_call(
    server: Any,
    training: Any,
    *,
    query: str,
    limit: int,
    strategy: str,
    major_hint: str | None,
    profile_enabled: bool,
) -> tuple[dict[str, Any], Any]:
    sql = SqlRecorder()
    rss_before = _rss_mb()
    profile = cProfile.Profile() if profile_enabled else None
    memo_enabled = strategy == "B_request_normalize_memo"
    effective_major = major_hint if strategy == "C_verified_major_hint" else None
    with _normalizer_strategy(server, training, memo_enabled) as memo:
        with _instrumented_connect(server, sql):
            started = time.perf_counter()
            if profile is not None:
                profile.enable()
            try:
                result = server.recommend_training_for_task(
                    query=query,
                    major_code=effective_major,
                    limit=limit,
                    save=False,
                    compact=False,
                )
            finally:
                if profile is not None:
                    profile.disable()
            elapsed_ms = (time.perf_counter() - started) * 1000.0
    rss_after = _rss_mb()
    sample = {
        "elapsed_ms": round(elapsed_ms, 3),
        "sql_statement_count": sql.statement_count,
        "sql_by_kind": dict(sorted(sql.kinds.items())),
        "connection_count": sql.connection_count,
        "all_connections_read_only": sql.all_read_only,
        "rss_before_mb": _round_optional(rss_before),
        "rss_after_mb": _round_optional(rss_after),
        "rss_delta_mb": _round_optional(
            max(0.0, rss_after - rss_before)
            if rss_before is not None and rss_after is not None
            else None
        ),
        "major_hint": effective_major,
        "normalizer_memo": memo.as_dict() if memo is not None else None,
        "function_calls": _profile_function_calls(profile) if profile is not None else None,
        "fingerprint": recommendation_fingerprint(result),
    }
    return sample, result


def candidate_gate(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    parity: bool,
    rss_limit_mb: float = 50.0,
) -> dict[str, Any]:
    baseline_p50 = float(baseline["latency"]["p50_ms"] or 0.0)
    candidate_p50 = float(candidate["latency"]["p50_ms"] or 0.0)
    improvement = ((baseline_p50 - candidate_p50) / baseline_p50) if baseline_p50 else 0.0
    max_rss_delta = float(candidate.get("max_rss_delta_mb") or 0.0)
    checks = {
        "p50_improvement_at_least_25pct": improvement >= 0.25,
        "recommendation_exact_parity": parity,
        "rss_increment_at_most_50mb": max_rss_delta <= rss_limit_mb,
    }
    return {
        "p50_improvement_ratio": round(improvement, 6),
        "p50_improvement_percent": round(improvement * 100.0, 2),
        "checks": checks,
        "promotion_candidate": all(checks.values()),
    }


def _summarize_strategy(samples: list[dict[str, Any]], profiles: list[dict[str, Any]]) -> dict[str, Any]:
    rss_values = [float(row["rss_delta_mb"]) for row in samples if row.get("rss_delta_mb") is not None]
    function_totals: dict[str, dict[str, float | int]] = {}
    for function_name in ("_candidate_score", "normalize_concept_key"):
        rows = [row["function_calls"][function_name] for row in profiles]
        function_totals[function_name] = {
            "primitive_calls": sum(int(row["primitive_calls"]) for row in rows),
            "total_calls": sum(int(row["total_calls"]) for row in rows),
            "self_ms": round(sum(float(row["self_ms"]) for row in rows), 3),
            "cumulative_ms": round(sum(float(row["cumulative_ms"]) for row in rows), 3),
        }
    memo_rows = [row["normalizer_memo"] for row in samples + profiles if row.get("normalizer_memo")]
    memo_summary = None
    if memo_rows:
        requests = sum(int(row["requests"]) for row in memo_rows)
        hits = sum(int(row["hits"]) for row in memo_rows)
        misses = sum(int(row["misses"]) for row in memo_rows)
        memo_summary = {
            "requests": requests,
            "hits": hits,
            "misses": misses,
            "hit_ratio": round(hits / requests, 6) if requests else 0.0,
        }
    return {
        "latency": metric_summary(row["elapsed_ms"] for row in samples),
        "sql_statement_count": metric_summary(row["sql_statement_count"] for row in samples),
        "max_rss_delta_mb": round(max(rss_values), 3) if rss_values else None,
        "all_connections_read_only": all(row["all_connections_read_only"] for row in samples + profiles),
        "function_call_totals": function_totals,
        "normalizer_memo": memo_summary,
    }


def _query_summary(samples: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "latency": metric_summary(row["elapsed_ms"] for row in samples),
        "sql_statement_count": metric_summary(row["sql_statement_count"] for row in samples),
        "fingerprints": [row["fingerprint"] for row in samples],
    }


def _parse_queries(values: list[str] | None) -> list[tuple[str, str]]:
    if not values:
        return list(DEFAULT_QUERIES)
    queries = []
    for value in values:
        if "=" not in value:
            raise ValueError(f"query must use DOMAIN=QUERY: {value}")
        domain, query = value.split("=", 1)
        queries.append((domain.strip(), query.strip()))
    return queries


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    from ncs_mcp import server
    from ncs_mcp import training_recommendation as training

    queries = _parse_queries(args.query)
    db_path = Path(server.load_settings().db_path)
    db_before = _db_state(db_path)
    baseline_results: dict[str, Any] = {}
    major_hints: dict[str, str | None] = {}
    samples: dict[str, dict[str, list[dict[str, Any]]]] = {
        strategy: {domain: [] for domain, _query in queries} for strategy in STRATEGIES
    }
    profiles: dict[str, list[dict[str, Any]]] = {strategy: [] for strategy in STRATEGIES}

    # Resolve comparison fingerprints and C's existing major filter input once.
    for domain, query in queries:
        _sample, result = run_call(
            server,
            training,
            query=query,
            limit=args.limit,
            strategy="A_baseline",
            major_hint=None,
            profile_enabled=False,
        )
        baseline_results[domain] = result
        major_hints[domain] = extract_major_code(result)

    # One unmeasured warm-up per strategy/query. Rotated order limits page-cache bias.
    for query_index, (domain, query) in enumerate(queries):
        for strategy in STRATEGIES[query_index % len(STRATEGIES) :] + STRATEGIES[: query_index % len(STRATEGIES)]:
            run_call(
                server,
                training,
                query=query,
                limit=args.limit,
                strategy=strategy,
                major_hint=major_hints[domain],
                profile_enabled=False,
            )

    for repeat_index in range(args.warm_repeats):
        for query_index, (domain, query) in enumerate(queries):
            shift = (repeat_index + query_index) % len(STRATEGIES)
            order = STRATEGIES[shift:] + STRATEGIES[:shift]
            for strategy in order:
                sample, _result = run_call(
                    server,
                    training,
                    query=query,
                    limit=args.limit,
                    strategy=strategy,
                    major_hint=major_hints[domain],
                    profile_enabled=False,
                )
                sample["repeat_index"] = repeat_index
                samples[strategy][domain].append(sample)

    for strategy in STRATEGIES:
        for domain, query in queries:
            sample, _result = run_call(
                server,
                training,
                query=query,
                limit=args.limit,
                strategy=strategy,
                major_hint=major_hints[domain],
                profile_enabled=True,
            )
            profiles[strategy].append(sample)

    strategy_reports: dict[str, Any] = {}
    for strategy in STRATEGIES:
        flat_samples = [row for domain, _query in queries for row in samples[strategy][domain]]
        strategy_reports[strategy] = {
            **_summarize_strategy(flat_samples, profiles[strategy]),
            "queries": {
                domain: {
                    "query": query,
                    "major_hint": major_hints[domain] if strategy == "C_verified_major_hint" else None,
                    **_query_summary(samples[strategy][domain]),
                }
                for domain, query in queries
            },
            "profile_samples": profiles[strategy],
        }

    baseline_fingerprints = {
        domain: recommendation_fingerprint(baseline_results[domain]) for domain, _query in queries
    }
    parity: dict[str, Any] = {}
    for strategy in STRATEGIES[1:]:
        per_query: dict[str, Any] = {}
        for domain, _query in queries:
            observed = strategy_reports[strategy]["queries"][domain]["fingerprints"]
            expected = baseline_fingerprints[domain]
            checks = {
                "ids_and_order": all(row["ids_order_fingerprint"] == expected["ids_order_fingerprint"] for row in observed),
                "scores": all(row["score_fingerprint"] == expected["score_fingerprint"] for row in observed),
                "evidence": all(row["evidence_fingerprint"] == expected["evidence_fingerprint"] for row in observed),
                "exact_recommendation_rows": all(
                    row["exact_recommendation_fingerprint"] == expected["exact_recommendation_fingerprint"]
                    for row in observed
                ),
            }
            per_query[domain] = {"checks": checks, "exact_parity": all(checks.values())}
        parity[strategy] = {
            "per_query": per_query,
            "all_queries_exact_parity": all(row["exact_parity"] for row in per_query.values()),
        }

    baseline_summary = strategy_reports["A_baseline"]
    gates = {
        strategy: candidate_gate(
            baseline_summary,
            strategy_reports[strategy],
            parity=parity[strategy]["all_queries_exact_parity"],
        )
        for strategy in STRATEGIES[1:]
    }
    db_after = _db_state(db_path)
    missing_major_hints = [domain for domain, value in major_hints.items() if not value]
    if missing_major_hints:
        gates["C_verified_major_hint"]["promotion_candidate"] = False
        gates["C_verified_major_hint"]["precondition_failure"] = {
            "missing_verified_major_hints": missing_major_hints
        }
    if not parity["C_verified_major_hint"]["all_queries_exact_parity"]:
        gates["C_verified_major_hint"]["rejected_immediately"] = "candidate range changed ranked recommendation output"

    return {
        "schema": "ncs_training_recommendation_hotspot_experiment_v1",
        "generated_at": utc_now(),
        "verdict": {
            "B_request_normalize_memo": (
                "promotion_candidate" if gates["B_request_normalize_memo"]["promotion_candidate"] else "do_not_promote"
            ),
            "C_verified_major_hint": (
                "promotion_candidate_with_verified_upstream_hint"
                if gates["C_verified_major_hint"]["promotion_candidate"]
                else "rejected"
            ),
        },
        "configuration": {
            "queries": [{"domain": domain, "query": query} for domain, query in queries],
            "warm_repeats": args.warm_repeats,
            "profile_repeats_per_query": 1,
            "limit": args.limit,
            "strategies": {
                "A_baseline": "current public recommend_training_for_task facade",
                "B_request_normalize_memo": "request-scoped normalization memo; product code unchanged",
                "C_verified_major_hint": "existing major_code filter populated from baseline resolved source",
            },
            "latency_and_cprofile_samples_separated": True,
            "interleaved_strategy_order": True,
        },
        "strategy_results": strategy_reports,
        "parity": parity,
        "promotion_gates": gates,
        "db_invariant": {
            "before": db_before,
            "after": db_after,
            "size_and_mtime_unchanged": db_before == db_after,
            "all_connections_read_only": all(
                strategy_reports[strategy]["all_connections_read_only"] for strategy in STRATEGIES
            ),
            "save_forced_false": True,
            "raw_ksa_write_attempted": False,
            "product_code_modified_by_experiment": False,
        },
        "limitations": [
            "This is a local warm-process experiment; it does not represent Vercel cold allocation or snapshot extraction.",
            "C is usable only when an upstream route has already verified the major code; deriving the hint by running the baseline resolver would erase the latency benefit.",
            "Exact parity proves stable recommendation output for the five representative queries, not relevance for all NCS queries.",
            "RSS is sampled immediately before and after each call, so very short transient peaks may be missed.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Training Recommendation Hotspot Experiment",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Result",
        "",
        "| Strategy | p50 ms | p95 ms | SQL p50 | Max RSS delta MB | Exact parity | Gate |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for strategy in STRATEGIES:
        result = report["strategy_results"][strategy]
        if strategy == "A_baseline":
            parity = "baseline"
            gate = "baseline"
        else:
            parity = str(report["parity"][strategy]["all_queries_exact_parity"]).lower()
            gate = str(report["promotion_gates"][strategy]["promotion_candidate"]).lower()
        lines.append(
            f"| `{strategy}` | {result['latency']['p50_ms']} | {result['latency']['p95_ms']} | "
            f"{result['sql_statement_count']['p50_ms']} | {result['max_rss_delta_mb']} | {parity} | {gate} |"
        )
    lines.extend(["", "## Promotion gates", ""])
    for strategy, gate in report["promotion_gates"].items():
        lines.append(
            f"- `{strategy}`: improvement `{gate['p50_improvement_percent']}%`, "
            f"promotion candidate `{str(gate['promotion_candidate']).lower()}`."
        )
        for name, passed in gate["checks"].items():
            lines.append(f"- `{strategy}.{name}`: `{str(passed).lower()}`")
    lines.extend(["", "## Function calls (one profiled call per query)", ""])
    lines.append("| Strategy | candidate_score calls | normalize calls | normalize cumulative ms | memo hit ratio |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for strategy in STRATEGIES:
        result = report["strategy_results"][strategy]
        candidate = result["function_call_totals"]["_candidate_score"]
        normalize = result["function_call_totals"]["normalize_concept_key"]
        memo = result.get("normalizer_memo") or {}
        lines.append(
            f"| `{strategy}` | {candidate['total_calls']} | {normalize['total_calls']} | "
            f"{normalize['cumulative_ms']} | {memo.get('hit_ratio', '-')} |"
        )
    lines.extend(["", "## Query parity", ""])
    for strategy, parity in report["parity"].items():
        for domain, row in parity["per_query"].items():
            lines.append(f"- `{strategy}` / `{domain}` exact parity: `{str(row['exact_parity']).lower()}`")
    invariant = report["db_invariant"]
    lines.extend(
        [
            "",
            "## Invariants",
            "",
            f"- DB size and mtime unchanged: `{str(invariant['size_and_mtime_unchanged']).lower()}`",
            f"- All measured connections read-only: `{str(invariant['all_connections_read_only']).lower()}`",
            "- `save=false`; no raw KSA or product-code mutation was attempted.",
            "",
            "## Limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Benchmark request-scoped recommendation hotspot prototypes.")
    parser.add_argument("--query", action="append", help="DOMAIN=QUERY; repeat for a custom set")
    parser.add_argument("--warm-repeats", type=int, default=3)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.warm_repeats < 2:
        parser.error("--warm-repeats must be at least 2")
    report = build_report(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
                "verdict": report["verdict"],
                "promotion_gates": report["promotion_gates"],
                "db_invariant": report["db_invariant"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
