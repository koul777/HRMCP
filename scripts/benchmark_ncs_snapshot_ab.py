"""Compare two compact NCS search snapshots with interleaved read-only calls.

This is a local latency and response-parity gate. It does not publish either
snapshot or make a human-labeled recommendation-quality claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import statistics
import sys
import time
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.db import connect as connect_db  # noqa: E402
from ncs_mcp.search import core as search_core  # noqa: E402

SCHEMA = "ncs_snapshot_search_ab_v1"
DEFAULT_QUERIES = (
    "채용",
    "신입사원 채용 면접",
    "데이터 분석가",
    "품질관리 담당자 교육",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percent / 100 * len(ordered)) - 1)]


def _database_record(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Not a SQLite file: {resolved}")
    with closing(sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro&immutable=1", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"SQLite quick_check failed: {resolved}")
        embedded = {}
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='serving_snapshot_manifest'"
        ).fetchone():
            embedded = dict(conn.execute(
                "SELECT manifest_key, manifest_value FROM serving_snapshot_manifest "
                "WHERE manifest_key IN ('schema', 'raw_ksa_sha256', "
                "'search_normalization_schema', 'ksa_search_fts_schema', "
                "'lexical_prefix_fts_schema', 'lexical_prefix_fts_boundary_policy', "
                "'lexical_prefix_fts_lengths')"
            ).fetchall())
        counts = dict(conn.execute(
            "SELECT object_name || ':' || count_kind, row_count "
            "FROM serving_snapshot_table_counts "
            "WHERE object_name NOT LIKE 'ksa_search_fts%' "
            "AND object_name NOT LIKE 'ksa_prefix_fts%' "
            "AND object_name NOT LIKE 'criteria_prefix_fts%'"
        ).fetchall())
        normalized = search_core._normalized_search_storage(conn)
        available_indexes = [
            name for name, available in (
                ("ksa_trigram", search_core._compact_ksa_search_fts_available(conn, normalized)),
                ("lexical_prefix", search_core._compact_lexical_prefix_available(conn, normalized)),
            ) if available
        ]
    return {
        "path": str(resolved),
        "bytes": resolved.stat().st_size,
        "sha256_before": _sha256(resolved),
        "embedded_manifest": embedded,
        "snapshot_table_counts": counts,
        "available_candidate_indexes": available_indexes,
        "read_only": True,
    }


def benchmark(
    baseline_db: Path,
    candidate_db: Path,
    queries: list[str],
    *,
    runs: int,
    scope: str,
    limit: int,
    disable_fts_baseline: bool = False,
    classification_filter: dict[str, str] | None = None,
) -> dict[str, Any]:
    if runs < 3 or runs > 20:
        raise ValueError("runs must be between 3 and 20")
    if not queries or any(not query.strip() for query in queries):
        raise ValueError("At least one non-empty query is required")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    databases = {
        "baseline": _database_record(baseline_db),
        "candidate": _database_record(candidate_db),
    }
    same_path = databases["baseline"]["path"] == databases["candidate"]["path"]
    if same_path != disable_fts_baseline:
        raise ValueError(
            "Use one database path only with --disable-fts-baseline; "
            "otherwise supply distinct snapshots"
        )
    if disable_fts_baseline and not databases["candidate"]["available_candidate_indexes"]:
        raise ValueError("Same-DB FTS toggle requires an available, attested candidate index")
    baseline_manifest = databases["baseline"]["embedded_manifest"]
    comparable_data = (
        bool(baseline_manifest.get("schema"))
        and bool(baseline_manifest.get("raw_ksa_sha256"))
        and bool(databases["baseline"]["snapshot_table_counts"])
        and databases["baseline"]["snapshot_table_counts"]
        == databases["candidate"]["snapshot_table_counts"]
        and baseline_manifest["schema"]
        == databases["candidate"]["embedded_manifest"].get("schema")
        and baseline_manifest["raw_ksa_sha256"]
        == databases["candidate"]["embedded_manifest"].get("raw_ksa_sha256")
    )
    if not comparable_data:
        raise ValueError("Snapshot schema, source KSA hash, or data table counts differ")

    selected = "baseline"
    original_open_db = server.open_db
    original_fts_available = search_core._compact_ksa_search_fts_available
    original_prefix_available = search_core._compact_lexical_prefix_available

    def fts_available(conn: Any, normalized: bool | str) -> bool:
        return selected != "baseline" and original_fts_available(conn, normalized)

    def prefix_available(conn: Any, normalized: bool | str) -> bool:
        return selected != "baseline" and original_prefix_available(conn, normalized)

    @contextmanager
    def open_selected_db() -> Iterator[sqlite3.Connection]:
        path = Path(databases[selected]["path"])
        conn = connect_db(path, read_only=True)
        try:
            yield conn
        finally:
            conn.close()

    def call(query: str, label: str) -> tuple[dict[str, Any], float]:
        nonlocal selected
        selected = label
        started = time.perf_counter()
        result = server.search_ncs(
            query=query, scope=scope, limit=limit,
            classification_filter=classification_filter,
        )
        return result, round((time.perf_counter() - started) * 1000, 3)

    records = []
    server.open_db = open_selected_db
    if disable_fts_baseline:
        search_core._compact_ksa_search_fts_available = fts_available
        search_core._compact_lexical_prefix_available = prefix_available
    try:
        for query in queries:
            call(query, "baseline")
            call(query, "candidate")
            samples: dict[str, list[float]] = {"baseline": [], "candidate": []}
            response_hashes: dict[str, set[str]] = {"baseline": set(), "candidate": set()}
            returned_by_condition: dict[str, set[int]] = {"baseline": set(), "candidate": set()}
            for index in range(runs):
                order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
                for label in order:
                    result, elapsed = call(query, label)
                    samples[label].append(elapsed)
                    returned_by_condition[label].add(int(result.get("returned", 0)))
                    payload = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")
                    response_hashes[label].add(hashlib.sha256(payload).hexdigest())
            baseline_hashes = response_hashes["baseline"]
            candidate_hashes = response_hashes["candidate"]
            records.append({
                "query": query,
                "response_equal": len(baseline_hashes) == len(candidate_hashes) == 1
                and baseline_hashes == candidate_hashes,
                "returned": next(iter(returned_by_condition["candidate"]))
                if len(returned_by_condition["candidate"]) == 1 else None,
                "response_sha256": next(iter(baseline_hashes)) if len(baseline_hashes) == 1 else None,
                "samples_ms": samples,
                "p50_ms": {label: round(statistics.median(values), 3) for label, values in samples.items()},
            })
    finally:
        server.open_db = original_open_db
        search_core._compact_ksa_search_fts_available = original_fts_available
        search_core._compact_lexical_prefix_available = original_prefix_available

    pooled = {
        label: [value for record in records for value in record["samples_ms"][label]]
        for label in databases
    }
    for label, record in databases.items():
        record["sha256_after"] = _sha256(Path(record["path"]))
        record["unchanged"] = record["sha256_before"] == record["sha256_after"]
    query_medians = {
        label: statistics.median(record["p50_ms"][label] for record in records)
        for label in databases
    }
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": (
            "interleaved_local_read_only_same_snapshot_fts_toggle"
            if disable_fts_baseline else "interleaved_local_read_only_compact_snapshots"
        ),
        "conditions": {
            "runs_per_query": runs, "scope": scope, "limit": limit,
            "disable_fts_baseline": disable_fts_baseline,
            "classification_filter": classification_filter,
        },
        "databases": databases,
        "records": records,
        "all_responses_equal": all(record["response_equal"] for record in records),
        "nonempty_query_count": sum(bool(record["returned"]) for record in records),
        "all_databases_unchanged": all(record["unchanged"] for record in databases.values()),
        "snapshot_data_counts_equal": comparable_data,
        "query_p50_median_ms": {label: round(value, 3) for label, value in query_medians.items()},
        "pooled_p95_ms": {
            label: round(_percentile(values, 95), 3) for label, values in pooled.items()
        },
        "human_labels_read": False,
        "recall_claim_allowed": False,
        "deployment_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-db", type=Path, required=True)
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument(
        "--query-file", type=Path,
        help="UTF-8 text file with one non-empty query per line; # comments are ignored",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--scope", choices=("all", "unit", "element", "criteria", "ksa"), default="all")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--major-code", help="Optional NCS major classification filter")
    parser.add_argument("--disable-fts-baseline", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.query_file and args.queries:
        parser.error("Use either --query or --query-file")
    try:
        queries = args.queries or list(DEFAULT_QUERIES)
        if args.query_file:
            queries = [
                line.strip()
                for line in args.query_file.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            ]
        report = benchmark(
            args.baseline_db, args.candidate_db,
            queries,
            runs=args.runs, scope=args.scope, limit=args.limit,
            disable_fts_baseline=args.disable_fts_baseline,
            classification_filter={"major_code": args.major_code} if args.major_code else None,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "out": str(args.out),
        "all_responses_equal": report["all_responses_equal"],
        "nonempty_query_count": report["nonempty_query_count"],
        "all_databases_unchanged": report["all_databases_unchanged"],
        "query_p50_median_ms": report["query_p50_median_ms"],
        "pooled_p95_ms": report["pooled_p95_ms"],
    }, ensure_ascii=False))
    return 0 if report["all_responses_equal"] and report["all_databases_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
