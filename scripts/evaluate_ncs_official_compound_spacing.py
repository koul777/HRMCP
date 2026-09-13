#!/usr/bin/env python3
"""Evaluate conservative spacing variants of official NCS unit compounds.

The report is aggregate-only. It never reads a holdout fixture and never
persists generated queries, official names, unit codes, or search results.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from typing import Any, Iterator, Sequence
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.search import core as search_core  # noqa: E402


SCHEMA = "ncs_search_official_compound_spacing_eval_v1"
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_OUT = ROOT / "reports" / "ncs_search_official_compound_spacing_eval_20260913.json"
DEFAULT_MARKDOWN_OUT = (
    ROOT / "reports" / "ncs_search_official_compound_spacing_eval_20260913.md"
)
DEFAULT_SUFFIXES = ("관리", "운영", "업무", "직무", "실무")
FORBIDDEN_REPORT_KEYS = frozenset(
    {"case", "cases", "case_records", "query", "queries", "result", "results"}
)


@dataclass(frozen=True)
class OfficialSpacingCase:
    digest: str
    official_name: str
    query: str


@dataclass(frozen=True)
class Measurement:
    elapsed_ms: float
    sql_statement_count: int
    rank: int | None


class ReadOnlyDbFactory:
    """Open the evaluation DB with both SQLite read-only guards enabled."""

    def __init__(self, db_path: Path):
        self.db_path = db_path.resolve()
        self.open_count = 0
        self.query_only_verified_count = 0
        self._active_trace: list[str] | None = None

    @contextmanager
    def capture_statements(self) -> Iterator[list[str]]:
        if self._active_trace is not None:
            raise RuntimeError("nested SQL trace capture is not supported")
        statements: list[str] = []
        self._active_trace = statements
        try:
            yield statements
        finally:
            self._active_trace = None

    @contextmanager
    def open(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.db_path.as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
            query_only = int(conn.execute("PRAGMA query_only").fetchone()[0] or 0)
            if query_only != 1:
                raise RuntimeError("SQLite query_only could not be enabled")
            self.open_count += 1
            self.query_only_verified_count += 1
            if self._active_trace is not None:
                conn.set_trace_callback(self._active_trace.append)
            yield conn
        finally:
            conn.close()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, *, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def db_identity(path: Path, *, include_sha256: bool = False) -> dict[str, Any]:
    resolved = path.resolve()
    stat = resolved.stat()
    return {
        "resolved_path": str(resolved),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "sha256": sha256_file(resolved) if include_sha256 else None,
        "sha256_status": "computed" if include_sha256 else "not_requested",
    }


def identities_equal(before: dict[str, Any], after: dict[str, Any]) -> bool:
    keys = ("resolved_path", "size_bytes", "mtime_ns", "device", "inode", "sha256")
    return all(before.get(key) == after.get(key) for key in keys)


def validate_expected_identity(
    identity: dict[str, Any],
    *,
    expected_size: int | None,
    expected_sha256: str | None,
) -> None:
    if expected_size is not None and identity["size_bytes"] != expected_size:
        raise ValueError(
            f"DB size mismatch: expected {expected_size}, got {identity['size_bytes']}"
        )
    if expected_sha256 is not None:
        normalized = expected_sha256.strip().casefold()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("expected DB SHA-256 must be 64 hexadecimal characters")
        if identity.get("sha256") != normalized:
            raise ValueError("DB SHA-256 mismatch")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))


def validate_output_paths(db_path: Path, out: Path, markdown_out: Path) -> None:
    if _same_path(db_path, out) or _same_path(db_path, markdown_out):
        raise ValueError("report output path must not be the evaluation DB path")
    if _same_path(out, markdown_out):
        raise ValueError("JSON and Markdown output paths must be distinct")


def build_spacing_cases(
    official_names: Sequence[str],
    *,
    suffixes: Sequence[str] = DEFAULT_SUFFIXES,
    sample_size: int = 60,
) -> tuple[list[OfficialSpacingCase], int]:
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    normalized_suffixes = tuple(dict.fromkeys(str(value) for value in suffixes if value))
    if not normalized_suffixes:
        raise ValueError("at least one non-empty suffix is required")
    eligible: list[OfficialSpacingCase] = []
    for name in sorted(set(str(value) for value in official_names if value)):
        if any(character.isspace() for character in name):
            continue
        for suffix in normalized_suffixes:
            if not name.endswith(suffix) or len(name) <= len(suffix) + 1:
                continue
            base = name[: -len(suffix)]
            eligible.append(
                OfficialSpacingCase(
                    digest=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                    official_name=name,
                    query=f"{base} {suffix}",
                )
            )
            break
    eligible.sort(key=lambda item: (item.digest, item.official_name))
    return eligible[:sample_size], len(eligible)


def _unit_path(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "unit_code": row["unit_code"],
        "major_code": row["major_code"],
        "middle_code": row["middle_code"],
        "small_code": row["small_code"],
        "sub_code": row["sub_code"],
    }


@contextmanager
def configured_search(factory: ReadOnlyDbFactory) -> Iterator[None]:
    """Temporarily bind the evaluator DB without leaking global test state."""
    previous = (
        search_core._OPEN_DB_FACTORY,
        search_core._CLAMP_LIMIT,
        search_core._UNIT_PATH,
        search_core._TIER_PREDICATES,
        search_core._TIER_EXECUTOR,
        search_core._TOKEN_EXPANDER,
    )
    search_core.configure_search_runtime(
        open_db_factory=factory.open,
        clamp_limit=lambda value: max(1, min(int(value), 100)),
        unit_path=_unit_path,
    )
    try:
        yield
    finally:
        search_core.configure_search_runtime(
            open_db_factory=previous[0],
            clamp_limit=previous[1],
            unit_path=previous[2],
            tier_predicates=previous[3],
            tier_executor=previous[4],
            token_expander=previous[5],
        )


def load_official_unit_names(factory: ReadOnlyDbFactory) -> list[str]:
    with factory.open() as conn:
        rows = conn.execute(
            """
            SELECT unit_name_raw
            FROM competency_units
            WHERE unit_name_raw IS NOT NULL
            GROUP BY unit_name_raw
            """
        ).fetchall()
    return [str(row[0]) for row in rows]


def measure_case(
    factory: ReadOnlyDbFactory,
    case: OfficialSpacingCase,
    *,
    baseline: bool,
    limit: int,
) -> Measurement:
    helper_context = (
        patch.object(search_core, "_ncs_search_joined_compound_phrase", return_value="")
        if baseline
        else nullcontext()
    )
    with factory.capture_statements() as statements, helper_context:
        started = time.perf_counter()
        response = search_core.search_ncs(case.query, scope="unit", limit=limit)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
    result_names = [str(item.get("text") or "") for item in response.get("results", [])]
    rank = (
        result_names.index(case.official_name) + 1
        if case.official_name in result_names[:limit]
        else None
    )
    return Measurement(
        elapsed_ms=elapsed_ms,
        sql_statement_count=len(statements),
        rank=rank,
    )


def evaluate_cases(
    factory: ReadOnlyDbFactory,
    cases: Sequence[OfficialSpacingCase],
    *,
    limit: int = 3,
) -> tuple[list[Measurement], list[Measurement]]:
    if limit < 3:
        raise ValueError("limit must be at least 3 for Hit@3")
    before: list[Measurement] = []
    after: list[Measurement] = []
    for index, case in enumerate(cases):
        execution_order = (True, False) if index % 2 == 0 else (False, True)
        for baseline in execution_order:
            measured = measure_case(factory, case, baseline=baseline, limit=limit)
            (before if baseline else after).append(measured)
    return before, after


def _nearest_percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        raise ValueError("values must not be empty")
    return ordered[round((len(ordered) - 1) * quantile)]


def summarize_measurements(measurements: Sequence[Measurement]) -> dict[str, Any]:
    if not measurements:
        raise ValueError("measurements must not be empty")
    ranks = [item.rank for item in measurements]
    latency = [item.elapsed_ms for item in measurements]
    statements = [item.sql_statement_count for item in measurements]
    count = len(measurements)
    return {
        "hit_at_1": round(sum(rank == 1 for rank in ranks) / count, 4),
        "hit_at_3": round(
            sum(rank is not None and rank <= 3 for rank in ranks) / count,
            4,
        ),
        "mrr_at_3": round(
            sum(0.0 if rank is None or rank > 3 else 1.0 / rank for rank in ranks)
            / count,
            4,
        ),
        "latency_ms": {
            "median": round(statistics.median(latency), 3),
            "p95": round(_nearest_percentile(latency, 0.95), 3),
            "mean": round(statistics.fmean(latency), 3),
        },
        "sql_statements": {
            "min": min(statements),
            "median": statistics.median(statements),
            "max": max(statements),
            "mean": round(statistics.fmean(statements), 3),
        },
    }


def build_report(
    *,
    db_path: Path,
    suffixes: Sequence[str],
    eligible_count: int,
    evaluated_count: int,
    sample_size: int,
    limit: int,
    before_summary: dict[str, Any],
    after_summary: dict[str, Any],
    identity_before: dict[str, Any],
    identity_after: dict[str, Any],
    expected_size: int | None,
    expected_sha256: str | None,
    factory: ReadOnlyDbFactory,
) -> dict[str, Any]:
    unchanged = identities_equal(identity_before, identity_after)
    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "provenance": {
            "database_path": str(db_path.resolve()),
            "source_table": "competency_units",
            "source_field": "unit_name_raw",
            "suffixes": list(suffixes),
            "variant_rule": "insert one space immediately before the terminal suffix",
            "sampling": {
                "algorithm": "ascending_sha256_of_official_name",
                "requested_sample_size": sample_size,
                "eligible_case_count": eligible_count,
                "evaluated_case_count": evaluated_count,
            },
            "baseline": "same search code with _ncs_search_joined_compound_phrase disabled",
            "after": "same search code with _ncs_search_joined_compound_phrase enabled",
            "execution_order": "alternating_before_after_then_after_before",
            "scope": "unit",
            "result_limit": limit,
        },
        "database_identity": {
            "before": identity_before,
            "after": identity_after,
            "unchanged": unchanged,
            "expected": {
                "size_bytes": expected_size,
                "sha256": expected_sha256.casefold() if expected_sha256 else None,
            },
        },
        "before": before_summary,
        "after": after_summary,
        "safety": {
            "database_uri_mode": "ro",
            "query_only_enabled": True,
            "query_only_verified_for_every_open": (
                factory.open_count == factory.query_only_verified_count
            ),
            "database_open_count": factory.open_count,
            "database_writes": False,
            "holdout_files_read": False,
            "case_level_data_persisted": False,
            "aliases_added": False,
        },
    }


def assert_aggregate_only(report: dict[str, Any]) -> None:
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in FORBIDDEN_REPORT_KEYS:
                    raise ValueError(f"case-level report key is forbidden: {key}")
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(report)


def render_markdown(report: dict[str, Any]) -> str:
    before = report["before"]
    after = report["after"]
    sampling = report["provenance"]["sampling"]
    identity = report["database_identity"]
    return "\n".join(
        [
            "# NCS official-compound spacing evaluation",
            "",
            "Aggregate-only, non-holdout evaluation generated from official ",
            "`competency_units.unit_name_raw` values. No query, official name, unit ",
            "code, or result row is persisted.",
            "",
            f"- Eligible names: `{sampling['eligible_case_count']}`",
            f"- Deterministic SHA-256 sample: `{sampling['evaluated_case_count']}`",
            f"- DB identity unchanged: `{str(identity['unchanged']).lower()}`",
            f"- SQLite guards: `mode=ro`, `query_only=ON`",
            "",
            "| Metric | Before | After |",
            "|---|---:|---:|",
            f"| Hit@1 | {before['hit_at_1']:.4f} | {after['hit_at_1']:.4f} |",
            f"| Hit@3 | {before['hit_at_3']:.4f} | {after['hit_at_3']:.4f} |",
            f"| MRR@3 | {before['mrr_at_3']:.4f} | {after['mrr_at_3']:.4f} |",
            f"| Median latency (ms) | {before['latency_ms']['median']:.3f} | {after['latency_ms']['median']:.3f} |",
            f"| P95 latency (ms) | {before['latency_ms']['p95']:.3f} | {after['latency_ms']['p95']:.3f} |",
            f"| Mean SQL statements | {before['sql_statements']['mean']:.3f} | {after['sql_statements']['mean']:.3f} |",
            "",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate deterministic spacing variants of official NCS unit names."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--sample-size", type=int, default=60)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--suffix", action="append", dest="suffixes")
    parser.add_argument("--expected-db-size", type=int)
    parser.add_argument("--expected-db-sha256")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db_path = args.db.resolve()
    out = args.out.resolve()
    markdown_out = args.markdown_out.resolve()
    suffixes = tuple(args.suffixes or DEFAULT_SUFFIXES)
    validate_output_paths(db_path, out, markdown_out)
    if not db_path.is_file():
        raise FileNotFoundError(db_path)

    include_sha256 = bool(args.expected_db_sha256)
    identity_before = db_identity(db_path, include_sha256=include_sha256)
    validate_expected_identity(
        identity_before,
        expected_size=args.expected_db_size,
        expected_sha256=args.expected_db_sha256,
    )

    factory = ReadOnlyDbFactory(db_path)
    with configured_search(factory):
        official_names = load_official_unit_names(factory)
        cases, eligible_count = build_spacing_cases(
            official_names,
            suffixes=suffixes,
            sample_size=args.sample_size,
        )
        if not cases:
            raise ValueError("no eligible official compound spacing cases")
        before, after = evaluate_cases(factory, cases, limit=args.limit)

    identity_after = db_identity(db_path, include_sha256=include_sha256)
    validate_expected_identity(
        identity_after,
        expected_size=args.expected_db_size,
        expected_sha256=args.expected_db_sha256,
    )
    if not identities_equal(identity_before, identity_after):
        raise RuntimeError("evaluation DB identity changed during read-only evaluation")

    report = build_report(
        db_path=db_path,
        suffixes=suffixes,
        eligible_count=eligible_count,
        evaluated_count=len(cases),
        sample_size=args.sample_size,
        limit=args.limit,
        before_summary=summarize_measurements(before),
        after_summary=summarize_measurements(after),
        identity_before=identity_before,
        identity_after=identity_after,
        expected_size=args.expected_db_size,
        expected_sha256=args.expected_db_sha256,
        factory=factory,
    )
    assert_aggregate_only(report)
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "evaluated_case_count": len(cases),
                "before": report["before"],
                "after": report["after"],
                "database_identity_unchanged": True,
                "out": str(out),
                "markdown_out": str(markdown_out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
