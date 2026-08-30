from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sqlite3
import sys
import tempfile
import time
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


SCHEMA = "ncs_search_baseline_v1"
MAX_SNAPSHOT_BYTES = 480_000_000
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_ARCHIVE = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
DEFAULT_MANIFEST = (
    ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.manifest.json"
)
DEFAULT_OUT = ROOT / "reports" / "ncs_search_baseline_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_search_baseline_20260830.md"
DEFAULT_SCOPES = ("all", "unit", "element", "criteria", "ksa")

DEFAULT_QUERY_CASES = (
    {"query": "채용", "case": "short_exact_alias"},
    {"query": "인사기획", "case": "exact_unit"},
    {"query": "노무관리", "case": "exact_unit"},
    {"query": "인력수요예측기술", "case": "exact_ksa"},
    {"query": "신입사원 채용 면접", "case": "multiword_natural_language"},
    {"query": "데이터 분석가", "case": "multiword_natural_language"},
    {"query": "품질관리 담당자 교육", "case": "multiword_natural_language"},
    {"query": "교육 훈련 운영", "case": "spaced_variant"},
    {"query": "HR planning", "case": "registered_alias"},
    {"query": "HRBP", "case": "registered_alias"},
    {"query": "HRD", "case": "registered_alias"},
    {"query": "휴가관리", "case": "registered_alias"},
)

SearchFunction = Callable[[str, str, int], dict[str, Any]]


def generated_at() -> str:
    return datetime.now(UTC).isoformat()


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    rank = max(1, math.ceil((percentile_value / 100.0) * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "samples": [],
            "sample_count": 0,
            "p50": None,
            "p95": None,
            "min": None,
            "max": None,
        }
    rounded = [round(value, 3) for value in values]
    return {
        "samples": rounded,
        "sample_count": len(rounded),
        "p50": percentile(rounded, 50),
        "p95": percentile(rounded, 95),
        "min": round(min(rounded), 3),
        "max": round(max(rounded), 3),
    }


def _file_metadata(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False, "bytes": None}
    return {"path": str(path), "exists": path.is_file(), "bytes": stat.st_size}


def database_metadata(path: Path) -> dict[str, Any]:
    metadata = _file_metadata(path)
    if not metadata["exists"]:
        metadata.update({"readable": False, "error": "database_not_found"})
        return metadata
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True) as conn:
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            objects = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchone()[0]
            )
            manifest_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='serving_snapshot_manifest'"
            ).fetchone()
            embedded_schema = None
            if manifest_table is not None:
                row = conn.execute(
                    "SELECT manifest_value FROM serving_snapshot_manifest WHERE manifest_key='schema'"
                ).fetchone()
                embedded_schema = str(row[0]) if row else None
        metadata.update(
            {
                "readable": True,
                "open_mode": "mode=ro&immutable=1",
                "sqlite_user_version": user_version,
                "schema_object_count": objects,
                "embedded_schema": embedded_schema,
            }
        )
    except (OSError, sqlite3.DatabaseError) as exc:
        metadata.update(
            {"readable": False, "error": type(exc).__name__, "error_message": str(exc)}
        )
    return metadata


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def measure_cold_start(archive_path: Path, manifest_path: Path) -> dict[str, Any]:
    report: dict[str, Any] = {
        "supported": False,
        "method": "temporary_directory_staged_measurement",
        "archive": _file_metadata(archive_path),
        "manifest": _file_metadata(manifest_path),
        "stages_ms": {},
        "writes": "temporary_directory_only",
    }
    if not archive_path.is_file() or not manifest_path.is_file():
        report["reason"] = "archive_or_manifest_missing"
        return report

    from ncs_mcp import vercel_snapshot

    total_started = time.perf_counter()
    try:
        started = time.perf_counter()
        manifest, member = vercel_snapshot.inspect_compact_archive(
            archive_path, manifest_path
        )
        report["stages_ms"]["inspect_manifest_archive"] = round(
            (time.perf_counter() - started) * 1000, 3
        )

        with tempfile.TemporaryDirectory(prefix="ncs_search_cold_start_") as raw_dir:
            destination = Path(raw_dir) / vercel_snapshot.COMPACT_SNAPSHOT_NAME

            started = time.perf_counter()
            with zipfile.ZipFile(archive_path, "r") as archive:
                with archive.open(member, "r") as source, destination.open("wb") as output:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        output.write(block)
                    output.flush()
                    os.fsync(output.fileno())
            report["stages_ms"]["extract_and_fsync"] = round(
                (time.perf_counter() - started) * 1000, 3
            )

            started = time.perf_counter()
            computed_sha256 = _sha256_file(destination)
            report["stages_ms"]["sha256"] = round(
                (time.perf_counter() - started) * 1000, 3
            )

            started = time.perf_counter()
            verified = vercel_snapshot._validate_compact_database(
                destination,
                manifest,
                computed_sha256=computed_sha256,
            )
            report["stages_ms"]["content_verify"] = round(
                (time.perf_counter() - started) * 1000, 3
            )
            report["destination_bytes"] = destination.stat().st_size

        report.update(
            {
                "supported": True,
                "ok": bool(verified),
                "sqlite_bytes": int(manifest["sqlite_bytes"]),
                "sqlite_sha256_matches": computed_sha256 == manifest["sqlite_sha256"],
                "runtime_parity_note": (
                    "Production hashes while extracting; stages are separated here to expose "
                    "their individual local costs."
                ),
            }
        )
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        report.update(
            {
                "supported": True,
                "ok": False,
                "error": type(exc).__name__,
                "error_message": str(exc),
            }
        )
    report["total_elapsed_ms"] = round((time.perf_counter() - total_started) * 1000, 3)
    return report


def load_runtime_search(db_path: Path) -> SearchFunction:
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp.server import search_ncs

    return search_ncs


def benchmark_searches(
    search_fn: SearchFunction,
    query_cases: list[dict[str, str]],
    *,
    scopes: tuple[str, ...],
    runs: int,
    limit: int,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for case in query_cases:
        query = case["query"]
        for scope in scopes:
            elapsed_values: list[float] = []
            result_count_samples: list[int] = []
            last_results: list[dict[str, Any]] = []
            for _run_index in range(runs):
                started = time.perf_counter()
                payload = search_fn(query, scope, limit)
                elapsed_values.append((time.perf_counter() - started) * 1000)
                raw_results = payload.get("results")
                last_results = raw_results if isinstance(raw_results, list) else []
                result_count_samples.append(len(last_results))
            counts_by_type = Counter(
                str(item.get("type") or "unknown") for item in last_results
            )
            stable_ids = [
                f"{item.get('type') or 'unknown'}:{item.get('id')}"
                for item in last_results[:5]
            ]
            records.append(
                {
                    "query": query,
                    "case": case["case"],
                    "scope": scope,
                    "limit": limit,
                    "elapsed_ms": latency_summary(elapsed_values),
                    "result_count": result_count_samples[-1],
                    "result_count_samples": result_count_samples,
                    "result_count_stable": len(set(result_count_samples)) <= 1,
                    "counts_by_type": dict(sorted(counts_by_type.items())),
                    "zero_hit": result_count_samples[-1] == 0,
                    "preview_stable_ids": stable_ids,
                }
            )
    return records


def _query_case_map(custom_queries: list[str] | None) -> list[dict[str, str]]:
    if not custom_queries:
        return [dict(item) for item in DEFAULT_QUERY_CASES]
    return [{"query": query, "case": "custom"} for query in custom_queries]


def acceptance_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    all_scope = [record for record in records if record["scope"] == "all"]
    zero_hit_queries = [record["query"] for record in all_scope if record["zero_hit"]]
    probes = {
        query: {
            "zero_hit": record["zero_hit"],
            "result_count": record["result_count"],
            "p50_ms": record["elapsed_ms"]["p50"],
            "p95_ms": record["elapsed_ms"]["p95"],
        }
        for query in ("채용", "신입사원 채용 면접", "데이터 분석가", "품질관리 담당자 교육")
        for record in all_scope
        if record["query"] == query
    }
    return {
        "baseline_only": True,
        "improvement_claim": False,
        "search_case_count": len(records),
        "all_scope_query_count": len(all_scope),
        "all_scope_zero_hit_count": len(zero_hit_queries),
        "all_scope_zero_hit_rate": (
            round(len(zero_hit_queries) / len(all_scope), 4) if all_scope else None
        ),
        "all_scope_zero_hit_queries": zero_hit_queries,
        "required_probe_results": probes,
        "result_counts_stable": all(
            bool(record["result_count_stable"]) for record in records
        ),
        "interpretation": (
            "Candidate recall only. Returned rows and latency do not establish ranking relevance, "
            "Recall@K, MRR, or NDCG."
        ),
    }


def build_report(
    *,
    db_path: Path,
    archive_path: Path,
    manifest_path: Path,
    query_cases: list[dict[str, str]],
    scopes: tuple[str, ...],
    runs: int,
    limit: int,
    search_fn: SearchFunction | None = None,
) -> dict[str, Any]:
    db_info = database_metadata(db_path)
    if not db_info.get("readable"):
        raise RuntimeError(f"database is not readable: {db_path}")
    cold_start = measure_cold_start(archive_path, manifest_path)
    runtime_search = search_fn or load_runtime_search(db_path)
    records = benchmark_searches(
        runtime_search,
        query_cases,
        scopes=scopes,
        runs=runs,
        limit=limit,
    )
    snapshot_bytes = cold_start.get("sqlite_bytes")
    if not isinstance(snapshot_bytes, int):
        snapshot_bytes = None
    budget_headroom = (
        MAX_SNAPSHOT_BYTES - snapshot_bytes if snapshot_bytes is not None else None
    )
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "mode": "read_only_baseline",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "caveat": (
                "Search latency is local warm filesystem/cache data. Cold-start stages are local "
                "disk measurements and are not Vercel absolute latency."
            ),
        },
        "database": db_info,
        "deployment_artifacts": {
            "archive": _file_metadata(archive_path),
            "manifest": _file_metadata(manifest_path),
            "size_budget": {
                "max_snapshot_bytes": MAX_SNAPSHOT_BYTES,
                "snapshot_bytes": snapshot_bytes,
                "headroom_bytes": budget_headroom,
                "within_budget": (
                    snapshot_bytes is not None and 0 < snapshot_bytes < MAX_SNAPSHOT_BYTES
                ),
            },
        },
        "cold_start": cold_start,
        "benchmark": {
            "search_contract": "ncs_mcp.server.search_ncs",
            "search_behavior": "current_single_substring_like_baseline",
            "runs_per_case": runs,
            "limit": limit,
            "scopes": list(scopes),
            "query_count": len(query_cases),
            "records": records,
        },
        "acceptance_metrics": acceptance_metrics(records),
        "commands": {
            "reproduce": (
                "python scripts/benchmark_ncs_search.py "
                f"--db \"{db_path}\" --archive \"{archive_path}\" "
                f"--manifest \"{manifest_path}\" --runs {runs} --limit {limit}"
            ),
            "remote_follow_up": (
                "Run the same probe set against a fresh Vercel instance and a warm instance; "
                "do not compare local absolute latency directly."
            ),
        },
        "safety": {
            "database_open_mode": "read_only",
            "database_writes": False,
            "status_updates": False,
            "human_review_claim": False,
            "temporary_writes_only_for_cold_start": True,
        },
    }


def _record_index(report: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {
        (record["query"], record["scope"]): record
        for record in report["benchmark"]["records"]
    }


def render_markdown(report: dict[str, Any]) -> str:
    metrics = report["acceptance_metrics"]
    cold = report["cold_start"]
    budget = report["deployment_artifacts"]["size_budget"]
    index = _record_index(report)
    lines = [
        "# NCS Search Baseline",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Schema: `{report['schema']}`",
        f"- Database: `{report['database']['path']}`",
        f"- Runs per case: `{report['benchmark']['runs_per_case']}`",
        f"- Result limit: `{report['benchmark']['limit']}`",
        f"- All-scope zero-hit rate: `{metrics['all_scope_zero_hit_rate']}`",
        "- Interpretation: baseline only; no improvement or relevance claim.",
        "",
        "## Query Results",
        "",
        "| Query | Case | all | unit | element | criteria | KSA | all p50 ms | all p95 ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    query_cases = []
    seen: set[str] = set()
    for record in report["benchmark"]["records"]:
        if record["query"] not in seen:
            seen.add(record["query"])
            query_cases.append((record["query"], record["case"]))
    for query, case in query_cases:
        scoped = {scope: index.get((query, scope)) for scope in DEFAULT_SCOPES}
        all_record = scoped["all"]
        values = [
            query,
            case,
            str(scoped["all"]["result_count"] if scoped["all"] else "n/a"),
            str(scoped["unit"]["result_count"] if scoped["unit"] else "n/a"),
            str(scoped["element"]["result_count"] if scoped["element"] else "n/a"),
            str(scoped["criteria"]["result_count"] if scoped["criteria"] else "n/a"),
            str(scoped["ksa"]["result_count"] if scoped["ksa"] else "n/a"),
            str(all_record["elapsed_ms"]["p50"] if all_record else "n/a"),
            str(all_record["elapsed_ms"]["p95"] if all_record else "n/a"),
        ]
        lines.append("| " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Deployment Size Budget",
            "",
            f"- Snapshot bytes: `{budget['snapshot_bytes']}`",
            f"- Maximum bytes: `{budget['max_snapshot_bytes']}`",
            f"- Headroom bytes: `{budget['headroom_bytes']}`",
            f"- Within budget: `{budget['within_budget']}`",
            "",
            "## Cold Start",
            "",
            f"- Supported: `{cold.get('supported')}`",
            f"- OK: `{cold.get('ok')}`",
            f"- Total elapsed ms: `{cold.get('total_elapsed_ms')}`",
        ]
    )
    for stage, elapsed in cold.get("stages_ms", {}).items():
        lines.append(f"- `{stage}`: `{elapsed}` ms")
    if cold.get("reason"):
        lines.append(f"- Reason: `{cold['reason']}`")

    lines.extend(
        [
            "",
            "## Caveats and Acceptance",
            "",
            f"- {report['environment']['caveat']}",
            f"- Zero-hit queries: `{', '.join(metrics['all_scope_zero_hit_queries'])}`",
            f"- Result counts stable across runs: `{metrics['result_counts_stable']}`",
            f"- {metrics['interpretation']}",
            "- Database writes and human-review status updates: `false`",
            "",
            "## Reproduce",
            "",
            "```powershell",
            report["commands"]["reproduce"],
            "```",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure the read-only pre-change NCS search and Vercel snapshot baseline."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--query", action="append", dest="queries")
    parser.add_argument(
        "--scope",
        action="append",
        choices=DEFAULT_SCOPES,
        dest="scopes",
        help="Repeat to override the default all/unit/element/criteria/ksa scope set.",
    )
    args = parser.parse_args(argv)
    if args.runs < 1 or args.runs > 50:
        parser.error("--runs must be between 1 and 50")
    if args.limit < 1 or args.limit > 100:
        parser.error("--limit must be between 1 and 100")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    query_cases = _query_case_map(args.queries)
    scopes = tuple(args.scopes or DEFAULT_SCOPES)
    report = build_report(
        db_path=args.db.resolve(),
        archive_path=args.archive.resolve(),
        manifest_path=args.manifest.resolve(),
        query_cases=query_cases,
        scopes=scopes,
        runs=args.runs,
        limit=args.limit,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report["acceptance_metrics"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
