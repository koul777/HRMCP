from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402


DEFAULT_CANDIDATES = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_DB = ROOT / "tmp" / "ncs_ontology_compact_v2_20260829.db"
DEFAULT_JSON = ROOT / "reports" / "ncs_search_sql_profile_20260830.json"
DEFAULT_MARKDOWN = ROOT / "reports" / "ncs_search_sql_profile_20260830.md"
SEARCH_TYPES = ("unit", "element", "criteria", "ksa")


def percentile(values: Iterable[float], fraction: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def latency_summary(values: Iterable[float]) -> dict[str, Any]:
    samples = [float(value) for value in values]
    return {
        "samples": len(samples),
        "p50_ms": round(percentile(samples, 0.50) or 0.0, 3),
        "p95_ms": round(percentile(samples, 0.95) or 0.0, 3),
        "mean_ms": round(statistics.fmean(samples), 3) if samples else 0.0,
        "total_ms": round(sum(samples), 3),
    }


def stable_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def result_order_fingerprint(result: dict[str, Any]) -> str:
    return stable_hash(
        [
            {"type": row.get("type"), "id": row.get("id")}
            for row in result.get("results", [])
        ]
    )


def result_contract_fingerprint(result: dict[str, Any]) -> str:
    return stable_hash(result)


def identify_search_type(sql: str) -> str:
    normalized = " ".join(sql.lower().split())
    if "from ksa_items " in normalized:
        return "ksa"
    if "from performance_criteria " in normalized:
        return "criteria"
    if "from competency_elements " in normalized:
        return "element"
    if "from competency_units " in normalized:
        return "unit"
    return "other"


def classify_query_plan(details: Iterable[str]) -> dict[str, Any]:
    normalized = [str(detail).upper() for detail in details]
    full_scan_steps = [
        detail
        for detail in normalized
        if "SCAN " in detail
        and "USING INDEX" not in detail
        and "USING COVERING INDEX" not in detail
    ]
    index_steps = [
        detail
        for detail in normalized
        if "USING INDEX" in detail
        or "USING COVERING INDEX" in detail
        or detail.startswith("SEARCH ")
        or " SEARCH " in detail
    ]
    temp_btree_steps = [detail for detail in normalized if "TEMP B-TREE" in detail]
    return {
        "full_scan": bool(full_scan_steps),
        "index_access": bool(index_steps),
        "temp_btree": bool(temp_btree_steps),
        "full_scan_steps": full_scan_steps,
        "index_steps": index_steps,
        "temp_btree_steps": temp_btree_steps,
    }


def expected_round_robin_counts(limit: int, types: Iterable[str]) -> dict[str, int]:
    ordered = list(types)
    counts = {item_type: 0 for item_type in ordered}
    if not ordered:
        return counts
    for index in range(max(int(limit), 0)):
        counts[ordered[index % len(ordered)]] += 1
    return counts


def promotion_gate(
    *,
    baseline_p50_ms: float,
    candidate_p50_ms: float,
    exact_contract_parity: bool,
    threshold_percent: float = 25.0,
) -> dict[str, Any]:
    improvement = (
        ((baseline_p50_ms - candidate_p50_ms) / baseline_p50_ms) * 100.0
        if baseline_p50_ms > 0
        else 0.0
    )
    return {
        "exact_contract_parity": bool(exact_contract_parity),
        "p50_improvement_percent": round(improvement, 3),
        "threshold_percent": float(threshold_percent),
        "promotion_candidate": bool(
            exact_contract_parity and improvement >= threshold_percent
        ),
    }


def candidate_groups(candidate: dict[str, Any]) -> list[str]:
    tags = set(candidate.get("tags") or [])
    groups: list[str] = []
    if "punctuation" in tags:
        groups.append("punctuation")
    if "two_syllable" in tags:
        groups.append("two_syllable")
    if "off_scope_candidate" in tags or "negative_control" in tags:
        groups.append("off_scope")
    if not groups:
        groups.append("other")
    return groups


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProfiledCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        recorder: "StatementRecorder",
        sql: str,
        params: Any,
        execute_ms: float,
    ) -> None:
        self._cursor = cursor
        self._recorder = recorder
        self._sql = sql
        self._params = params
        self._execute_ms = execute_ms
        self._recorded = False

    def _record(self, rows: list[Any], fetch_ms: float) -> None:
        if not self._recorded:
            self._recorder.record(
                sql=self._sql,
                params=self._params,
                elapsed_ms=self._execute_ms + fetch_ms,
                rows_returned=len(rows),
            )
            self._recorded = True

    def fetchall(self) -> list[Any]:
        started = time.perf_counter_ns()
        rows = self._cursor.fetchall()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        self._record(rows, elapsed_ms)
        return rows

    def fetchone(self) -> Any:
        started = time.perf_counter_ns()
        row = self._cursor.fetchone()
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        self._record([] if row is None else [row], elapsed_ms)
        return row

    def __iter__(self):
        return iter(self.fetchall())


class ProfiledConnection:
    def __init__(self, raw: sqlite3.Connection, recorder: "StatementRecorder") -> None:
        self.raw = raw
        self.recorder = recorder

    def execute(self, sql: str, params: Any = ()) -> ProfiledCursor:
        started = time.perf_counter_ns()
        cursor = self.raw.execute(sql, params)
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
        return ProfiledCursor(cursor, self.recorder, sql, params, elapsed_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.raw, name)


class StatementRecorder:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self.plan_cache: dict[str, dict[str, Any]] = {}
        self.connection: sqlite3.Connection | None = None

    def set_context(self, **values: Any) -> None:
        self.context = dict(values)

    def record(
        self,
        *,
        sql: str,
        params: Any,
        elapsed_ms: float,
        rows_returned: int,
    ) -> None:
        item_type = identify_search_type(sql)
        if item_type == "other":
            return
        param_map = params if isinstance(params, dict) else {}
        tier = param_map.get("match_tier")
        plan_key = stable_hash(
            {
                "sql": sql,
                "parameter_names": sorted(param_map),
                "fallback_token_count": sum(
                    1 for name in param_map if str(name).startswith("token_")
                ),
            }
        )
        if plan_key not in self.plan_cache and self.connection is not None:
            try:
                plan_rows = self.connection.execute(
                    "EXPLAIN QUERY PLAN " + sql, params
                ).fetchall()
                details = [str(row[3]) for row in plan_rows]
                self.plan_cache[plan_key] = {
                    "details": details,
                    **classify_query_plan(details),
                }
            except sqlite3.Error as exc:
                self.plan_cache[plan_key] = {
                    "details": [],
                    "error": str(exc),
                    **classify_query_plan([]),
                }
        candidate_limit = param_map.get("candidate_limit")
        self.records.append(
            {
                **self.context,
                "search_type": item_type,
                "match_tier": int(tier) if tier is not None else None,
                "statement_kind": (
                    "fast_reject_probe"
                    if "profile_fast_reject" in sql
                    else "search"
                ),
                "elapsed_ms": round(float(elapsed_ms), 6),
                "rows_returned": int(rows_returned),
                "candidate_limit": (
                    int(candidate_limit) if candidate_limit is not None else None
                ),
                "result_sufficient": bool(
                    candidate_limit is not None
                    and rows_returned >= int(candidate_limit)
                ),
                "plan_key": plan_key,
            }
        )


class SearchHarness:
    def __init__(self, db_path: Path, recorder: StatementRecorder) -> None:
        self.db_path = db_path.resolve()
        self.recorder = recorder
        self._original_open_db = server.open_db
        self._original_executor = server._execute_ncs_search_tiers

    @contextmanager
    def open_db(self):
        uri = f"file:{self.db_path.as_posix()}?mode=ro&immutable=1"
        raw = sqlite3.connect(uri, uri=True)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA query_only = ON")
        self.recorder.connection = raw
        try:
            yield ProfiledConnection(raw, self.recorder)
        finally:
            self.recorder.connection = None
            raw.close()

    def __enter__(self) -> "SearchHarness":
        server.open_db = self.open_db
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        server.open_db = self._original_open_db
        server._execute_ncs_search_tiers = self._original_executor

    def normal_search(self, query: str, scope: str, limit: int) -> dict[str, Any]:
        server._execute_ncs_search_tiers = self._original_executor
        return server.search_ncs(query=query, scope=scope, limit=limit, offset=0)

    def adaptive_limit_search(
        self, query: str, scope: str, limit: int
    ) -> tuple[dict[str, Any], bool, int]:
        if scope != "all":
            return self.normal_search(query, scope, limit), False, limit + 1
        expected = expected_round_robin_counts(limit, SEARCH_TYPES)
        first_pass_limit = max(expected.values(), default=0) + 1

        def limited_executor(conn, sql_template, tiers, base_params):
            narrowed = dict(base_params)
            if "candidate_limit" in narrowed:
                narrowed["candidate_limit"] = min(
                    int(narrowed["candidate_limit"]), first_pass_limit
                )
            return self._original_executor(
                conn, sql_template, tiers, narrowed
            )

        server._execute_ncs_search_tiers = limited_executor
        first = server.search_ncs(query=query, scope=scope, limit=limit, offset=0)
        enough = all(
            int(first.get("counts_by_type", {}).get(item_type, 0)) >= needed
            for item_type, needed in expected.items()
        )
        if enough:
            server._execute_ncs_search_tiers = self._original_executor
            return first, False, first_pass_limit
        self.recorder.context["phase"] = "fallback_full_limit"
        server._execute_ncs_search_tiers = self._original_executor
        return self.normal_search(query, scope, limit), True, first_pass_limit

    def fast_reject_has_any(
        self,
        query: str,
        scope: str,
        type_order: Iterable[str],
    ) -> bool:
        phrase, _, fallback_tokens = server._normalize_ncs_search_query(query)
        if not phrase:
            return False
        requested = set(SEARCH_TYPES if scope == "all" else (scope,))
        with self.open_db() as conn:
            for item_type in type_order:
                if item_type not in requested:
                    continue
                if item_type == "unit":
                    columns = (
                        "cu.unit_code",
                        "cu.unit_name_raw",
                        "cu.api_definition",
                        "c.major_name",
                        "c.middle_name",
                        "c.small_name",
                        "c.sub_name",
                        "aliases.alias_search_text",
                    )
                    from_sql = """
                        WITH alias_search AS (
                            SELECT unit_code,
                                   GROUP_CONCAT(
                                       COALESCE(alias_text, '') || ' ' ||
                                       COALESCE(normalized_query, ''), ' '
                                   ) AS alias_search_text
                            FROM ncs_query_aliases
                            WHERE unit_code IS NOT NULL
                            GROUP BY unit_code
                        )
                        SELECT 1
                        FROM competency_units cu
                        JOIN classifications c
                          ON c.classification_id = cu.classification_id
                        LEFT JOIN alias_search aliases
                          ON aliases.unit_code = cu.unit_code
                    """
                elif item_type == "element":
                    columns = ("ce.element_name_raw",)
                    from_sql = "SELECT 1 FROM competency_elements ce"
                elif item_type == "criteria":
                    columns = ("pc.criteria_text_raw", "pc.criteria_text_refined")
                    from_sql = "SELECT 1 FROM performance_criteria pc"
                else:
                    columns = ("ki.ksa_text_raw", "ki.ksa_text_refined")
                    from_sql = "SELECT 1 FROM ksa_items ki"
                tier, where_clause, tier_params = server._ncs_search_tier_predicates(
                    columns, phrase, fallback_tokens
                )[-1]
                params = dict(tier_params)
                params["match_tier"] = tier
                sql = (
                    "/* profile_fast_reject */\n"
                    + from_sql
                    + "\nWHERE "
                    + where_clause
                    + "\nLIMIT 1"
                )
                if conn.execute(sql, params).fetchone() is not None:
                    return True
        return False

    def fast_reject_search(
        self,
        query: str,
        scope: str,
        limit: int,
        type_order: Iterable[str],
    ) -> tuple[dict[str, Any], bool]:
        if self.fast_reject_has_any(query, scope, type_order):
            return self.normal_search(query, scope, limit), False
        phrase, query_tokens, _ = server._normalize_ncs_search_query(query)
        requested = SEARCH_TYPES if scope == "all" else (scope,)
        counts = {item_type: 0 for item_type in requested}
        more = {item_type: False for item_type in requested}
        match_modes = {item_type: None for item_type in requested}
        empty = {
            "query": query,
            "normalized_query": phrase,
            "query_tokens": query_tokens,
            "scope": scope,
            "match_mode": None,
            "match_mode_by_type": match_modes,
            "counts_by_type": counts,
            "has_more_by_type": more,
            "returned": 0,
            "offset": 0,
            "next_offset": None,
            "results": [],
        }
        empty["markdown_summary"] = server._ncs_search_markdown(
            query,
            [],
            counts_by_type=counts,
            offset=0,
            next_offset=None,
        )
        return empty, True


def _run_strategy(
    harness: SearchHarness,
    recorder: StatementRecorder,
    candidates: list[dict[str, Any]],
    *,
    strategy: str,
    runs: int,
    limit: int,
    baseline: dict[str, dict[str, str]],
    fast_reject_order: list[str],
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for candidate in candidates:
        case_id = str(candidate["case_id"])
        query = str(candidate["query"])
        scope = str(candidate.get("scope_candidate") or "all")
        for run_index in range(runs):
            recorder.set_context(
                strategy=strategy,
                case_id=case_id,
                run=run_index + 1,
                phase="primary",
            )
            before = len(recorder.records)
            started = time.perf_counter_ns()
            fallback = False
            rejected = False
            first_pass_limit = None
            if strategy == "baseline":
                result = harness.normal_search(query, scope, limit)
            elif strategy == "adaptive_limit_sizing":
                result, fallback, first_pass_limit = harness.adaptive_limit_search(
                    query, scope, limit
                )
            elif strategy == "no_result_fast_reject":
                result, rejected = harness.fast_reject_search(
                    query, scope, limit, fast_reject_order
                )
            else:
                raise ValueError(f"unknown strategy: {strategy}")
            elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
            statement_slice = recorder.records[before:]
            order_hash = result_order_fingerprint(result)
            contract_hash = result_contract_fingerprint(result)
            samples.append(
                {
                    "case_id": case_id,
                    "query": query,
                    "scope": scope,
                    "tags": list(candidate.get("tags") or []),
                    "groups": candidate_groups(candidate),
                    "run": run_index + 1,
                    "elapsed_ms": round(elapsed_ms, 6),
                    "returned": int(result.get("returned", 0)),
                    "statement_count": len(statement_slice),
                    "order_fingerprint": order_hash,
                    "contract_fingerprint": contract_hash,
                    "baseline_order_parity": (
                        True
                        if strategy == "baseline"
                        else order_hash == baseline[case_id]["order"]
                    ),
                    "baseline_contract_parity": (
                        True
                        if strategy == "baseline"
                        else contract_hash == baseline[case_id]["contract"]
                    ),
                    "fallback_full_limit": fallback,
                    "fast_rejected": rejected,
                    "first_pass_candidate_limit": first_pass_limit,
                }
            )
    return samples


def _aggregate_statements(
    records: list[dict[str, Any]], strategy: str
) -> dict[str, Any]:
    chosen = [row for row in records if row.get("strategy") == strategy]
    by_type_tier: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in chosen:
        by_type_tier[(row["search_type"], row["match_tier"])].append(row)
    tier_rows: list[dict[str, Any]] = []
    for (item_type, tier), rows in sorted(
        by_type_tier.items(), key=lambda pair: (pair[0][0], pair[0][1] or -1)
    ):
        latencies = [row["elapsed_ms"] for row in rows]
        tier_rows.append(
            {
                "search_type": item_type,
                "match_tier": tier,
                **latency_summary(latencies),
                "rows_returned_total": sum(row["rows_returned"] for row in rows),
                "rows_returned_mean": round(
                    statistics.fmean(row["rows_returned"] for row in rows), 3
                ),
                "result_sufficiency_rate": round(
                    sum(bool(row["result_sufficient"]) for row in rows) / len(rows), 4
                ),
                "plan_keys": sorted({row["plan_key"] for row in rows}),
            }
        )

    grouped: dict[tuple[str, int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in chosen:
        if row["statement_kind"] == "search":
            grouped[(row["case_id"], row["run"], row["search_type"])].append(row)
    early_stop = []
    cumulative_by_tier: dict[tuple[str, int], list[float]] = defaultdict(list)
    for (_, _, item_type), rows in grouped.items():
        elapsed = 0.0
        tiers = []
        for row in rows:
            if row["match_tier"] is None:
                continue
            elapsed += float(row["elapsed_ms"])
            tiers.append(int(row["match_tier"]))
            cumulative_by_tier[(item_type, int(row["match_tier"]))].append(elapsed)
        if tiers:
            early_stop.append(max(tiers) < 2)
    for tier_row in tier_rows:
        cumulative = cumulative_by_tier.get(
            (tier_row["search_type"], tier_row["match_tier"]), []
        )
        tier_row["cumulative_to_tier"] = latency_summary(cumulative)

    type_totals = Counter()
    for row in chosen:
        type_totals[row["search_type"]] += float(row["elapsed_ms"])
    total = sum(type_totals.values())
    return {
        "statement_count": len(chosen),
        "tiers": tier_rows,
        "early_stop": {
            "type_run_count": len(early_stop),
            "count": sum(early_stop),
            "rate": round(sum(early_stop) / len(early_stop), 4)
            if early_stop
            else 0.0,
        },
        "type_latency_contribution": {
            item_type: {
                "total_ms": round(type_totals[item_type], 3),
                "share": round(type_totals[item_type] / total, 4) if total else 0.0,
            }
            for item_type in SEARCH_TYPES
        },
    }


def _aggregate_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        for group in sample["groups"]:
            by_group[group].append(sample)
    return {
        "latency": latency_summary(sample["elapsed_ms"] for sample in samples),
        "statement_count": latency_summary(
            sample["statement_count"] for sample in samples
        ),
        "zero_hit_samples": sum(sample["returned"] == 0 for sample in samples),
        "order_parity_rate": round(
            sum(bool(sample["baseline_order_parity"]) for sample in samples)
            / len(samples),
            4,
        ),
        "contract_parity_rate": round(
            sum(bool(sample["baseline_contract_parity"]) for sample in samples)
            / len(samples),
            4,
        ),
        "fallback_count": sum(sample["fallback_full_limit"] for sample in samples),
        "fast_reject_count": sum(sample["fast_rejected"] for sample in samples),
        "groups": {
            group: {
                "sample_count": len(rows),
                "latency": latency_summary(row["elapsed_ms"] for row in rows),
                "zero_hit_samples": sum(row["returned"] == 0 for row in rows),
                "contract_parity_rate": round(
                    sum(bool(row["baseline_contract_parity"]) for row in rows)
                    / len(rows),
                    4,
                ),
            }
            for group, rows in sorted(by_group.items())
        },
    }


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# NCS Search SQL Profile (2026-08-30)",
        "",
        "## Scope and safety",
        "",
        f"- Candidate queries: {report['evaluation']['candidate_count']}",
        f"- Runs per query: {report['evaluation']['runs_per_query']}",
        f"- Compact snapshot: `{report['database']['path']}`",
        f"- Snapshot bytes: {report['database']['bytes']:,}",
        "- Database mode: read-only + immutable + `PRAGMA query_only=ON`",
        "- Product server, schema, indexes, raw KSA, review statuses: unchanged",
        "",
        "## Strategy decision",
        "",
        "| Strategy | p50 ms | p95 ms | p50 change | Contract parity | Promote |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    baseline_p50 = report["strategies"]["baseline"]["samples"]["latency"]["p50_ms"]
    for name, data in report["strategies"].items():
        latency = data["samples"]["latency"]
        if name == "baseline":
            change = 0.0
            parity = 1.0
            promote = False
        else:
            gate = data["promotion_gate"]
            change = gate["p50_improvement_percent"]
            parity = data["samples"]["contract_parity_rate"]
            promote = gate["promotion_candidate"]
        lines.append(
            f"| {name} | {latency['p50_ms']:.3f} | {latency['p95_ms']:.3f} | "
            f"{change:.2f}% | {parity:.2%} | {'YES' if promote else 'NO'} |"
        )
    lines.extend(
        [
            "",
            "Promotion requires both exact contract parity across every sample and at least 25% p50 improvement.",
            "",
            "## Baseline SQL behavior",
            "",
            f"- SQL statements: {report['strategies']['baseline']['statements']['statement_count']}",
            f"- Type-run early-stop rate: {report['strategies']['baseline']['statements']['early_stop']['rate']:.2%}",
            "",
            "| Type | Tier | Samples | p50 ms | p95 ms | cumulative p50 ms | rows mean | sufficient |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["strategies"]["baseline"]["statements"]["tiers"]:
        lines.append(
            f"| {row['search_type']} | {row['match_tier']} | {row['samples']} | "
            f"{row['p50_ms']:.3f} | {row['p95_ms']:.3f} | "
            f"{row['cumulative_to_tier']['p50_ms']:.3f} | "
            f"{row['rows_returned_mean']:.2f} | {row['result_sufficiency_rate']:.2%} |"
        )
    lines.extend(["", "### Type latency contribution", ""])
    for item_type, item in report["strategies"]["baseline"]["statements"][
        "type_latency_contribution"
    ].items():
        lines.append(
            f"- `{item_type}`: {item['total_ms']:.3f} ms ({item['share']:.2%})"
        )
    lines.extend(["", "### Query groups", ""])
    for group, item in report["strategies"]["baseline"]["samples"]["groups"].items():
        lines.append(
            f"- `{group}`: {item['sample_count']} samples, p50 {item['latency']['p50_ms']:.3f} ms, "
            f"p95 {item['latency']['p95_ms']:.3f} ms, zero-hit {item['zero_hit_samples']}"
        )
    lines.extend(
        [
            "",
            "## Query-plan evidence",
            "",
            f"- Unique plans: {report['query_plans']['unique_plan_count']}",
            f"- Plans with full scan: {report['query_plans']['full_scan_count']}",
            f"- Plans with index access: {report['query_plans']['index_access_count']}",
            f"- Plans with temp B-tree: {report['query_plans']['temp_btree_count']}",
            "",
            "## Non-promoted ideas",
            "",
            "- Reordering the four serial type queries alone cannot lower their summed latency and would risk changing round-robin ordering if the public type order changed.",
            "- SELECT-column reduction was not reported as an end-to-end win: the observed full scans and ORDER BY temp B-trees dominate, while a second ID-to-detail lookup is required to preserve the public payload.",
            "- No FTS/index/schema/DB-size changes were made.",
            "",
            "## Conclusion",
            "",
            report["decision"]["summary"],
            "",
            f"Baseline p50 reference: {baseline_p50:.3f} ms.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    db_path = Path(args.db).resolve()
    candidate_path = Path(args.candidates).resolve()
    source = json.loads(candidate_path.read_text(encoding="utf-8"))
    candidates = list(source["candidates"])
    if args.candidate_limit is not None:
        candidates = candidates[: max(int(args.candidate_limit), 0)]
    if not db_path.exists():
        raise FileNotFoundError(db_path)
    before_hash = sha256_file(db_path)
    before_stat = db_path.stat()
    recorder = StatementRecorder()
    all_samples: dict[str, list[dict[str, Any]]] = {}

    with SearchHarness(db_path, recorder) as harness:
        for candidate in candidates[: min(4, len(candidates))]:
            recorder.set_context(
                strategy="warmup",
                case_id=candidate["case_id"],
                run=0,
                phase="warmup",
            )
            harness.normal_search(
                str(candidate["query"]),
                str(candidate.get("scope_candidate") or "all"),
                int(args.limit),
            )
        recorder.records.clear()

        baseline_samples = _run_strategy(
            harness,
            recorder,
            candidates,
            strategy="baseline",
            runs=int(args.runs),
            limit=int(args.limit),
            baseline={},
            fast_reject_order=list(SEARCH_TYPES),
        )
        all_samples["baseline"] = baseline_samples
        baseline: dict[str, dict[str, str]] = {}
        for sample in baseline_samples:
            baseline.setdefault(
                sample["case_id"],
                {
                    "order": sample["order_fingerprint"],
                    "contract": sample["contract_fingerprint"],
                },
            )
        baseline_type_ms = Counter()
        for row in recorder.records:
            if row.get("strategy") == "baseline":
                baseline_type_ms[row["search_type"]] += float(row["elapsed_ms"])
        fast_order = [
            item_type
            for item_type, _ in sorted(
                baseline_type_ms.items(), key=lambda pair: pair[1]
            )
        ]
        for item_type in SEARCH_TYPES:
            if item_type not in fast_order:
                fast_order.append(item_type)

        for strategy in ("adaptive_limit_sizing", "no_result_fast_reject"):
            all_samples[strategy] = _run_strategy(
                harness,
                recorder,
                candidates,
                strategy=strategy,
                runs=int(args.runs),
                limit=int(args.limit),
                baseline=baseline,
                fast_reject_order=fast_order,
            )

    after_stat = db_path.stat()
    after_hash = sha256_file(db_path)
    strategy_reports: dict[str, Any] = {}
    baseline_aggregate = _aggregate_samples(all_samples["baseline"])
    for name, samples in all_samples.items():
        aggregate = _aggregate_samples(samples)
        item: dict[str, Any] = {
            "samples": aggregate,
            "statements": _aggregate_statements(recorder.records, name),
            "case_samples": samples,
        }
        if name != "baseline":
            exact = all(sample["baseline_contract_parity"] for sample in samples)
            item["promotion_gate"] = promotion_gate(
                baseline_p50_ms=baseline_aggregate["latency"]["p50_ms"],
                candidate_p50_ms=aggregate["latency"]["p50_ms"],
                exact_contract_parity=exact,
            )
        strategy_reports[name] = item

    plans = list(recorder.plan_cache.values())
    promotable = [
        name
        for name, item in strategy_reports.items()
        if name != "baseline"
        and item.get("promotion_gate", {}).get("promotion_candidate")
    ]
    report = {
        "schema": "ncs_search_sql_profile_v1",
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_compact_snapshot_sql_profile",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "server_source_sha256": sha256_file(Path(server.__file__).resolve()),
            "latency_caveat": "Local warm filesystem measurements; not remote Vercel cold latency.",
        },
        "database": {
            "path": str(db_path),
            "bytes": before_stat.st_size,
            "sha256_before": before_hash,
            "sha256_after": after_hash,
            "size_before": before_stat.st_size,
            "size_after": after_stat.st_size,
            "mtime_ns_before": before_stat.st_mtime_ns,
            "mtime_ns_after": after_stat.st_mtime_ns,
            "unchanged": bool(
                before_hash == after_hash
                and before_stat.st_size == after_stat.st_size
                and before_stat.st_mtime_ns == after_stat.st_mtime_ns
            ),
            "open_mode": "mode=ro&immutable=1; PRAGMA query_only=ON",
        },
        "evaluation": {
            "candidate_source": str(candidate_path),
            "candidate_count": len(candidates),
            "runs_per_query": int(args.runs),
            "limit": int(args.limit),
            "candidate_status": "candidate_eval",
            "human_labels_present": False,
            "recall_claim_allowed": False,
        },
        "strategies": strategy_reports,
        "query_plans": {
            "unique_plan_count": len(plans),
            "full_scan_count": sum(bool(plan["full_scan"]) for plan in plans),
            "index_access_count": sum(bool(plan["index_access"]) for plan in plans),
            "temp_btree_count": sum(bool(plan["temp_btree"]) for plan in plans),
            "plans": recorder.plan_cache,
        },
        "prototype_notes": {
            "no_result_fast_reject_type_order": fast_order,
            "type_execution_order": {
                "measured_candidate": False,
                "reason": "The production path executes all requested types serially; permutation leaves the sum unchanged, while changing public type order changes round-robin results.",
            },
            "select_column_reduction": {
                "measured_candidate": False,
                "reason": "Projection-only timing would omit the mandatory ID-to-detail lookup and would not be an exact end-to-end parity measurement.",
            },
        },
        "decision": {
            "promotable_strategies": promotable,
            "summary": (
                "At least one harness prototype met exact payload parity and the 25% p50 gate; production promotion still requires independent review."
                if promotable
                else "No harness prototype met both exact payload parity and the 25% p50 improvement gate; do not change production search SQL from this profile alone."
            ),
        },
        "safety": {
            "database_writes": False,
            "schema_changes": False,
            "new_indexes": False,
            "fts": False,
            "product_files_changed": False,
            "raw_ksa_changes": False,
            "human_review_status_changes": False,
            "approval_claim": False,
        },
        "commands": {
            "reproduce": (
                f'python scripts/profile_ncs_search_sql.py --db "{db_path}" '
                f'--candidates "{candidate_path}" --runs {args.runs} '
                f'--limit {args.limit} --out "{Path(args.out).resolve()}" '
                f'--markdown-out "{Path(args.markdown_out).resolve()}"'
            )
        },
    }
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Profile current lazy-tier NCS search SQL on a read-only compact snapshot."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--candidates", default=str(DEFAULT_CANDIDATES))
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--candidate-limit", type=int)
    parser.add_argument("--out", default=str(DEFAULT_JSON))
    parser.add_argument("--markdown-out", default=str(DEFAULT_MARKDOWN))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(args)
    out = Path(args.out)
    markdown_out = Path(args.markdown_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_out.write_text(_render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out.resolve()),
                "markdown_out": str(markdown_out.resolve()),
                "candidate_count": report["evaluation"]["candidate_count"],
                "promotable_strategies": report["decision"]["promotable_strategies"],
                "database_unchanged": report["database"]["unchanged"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
