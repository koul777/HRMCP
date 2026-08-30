#!/usr/bin/env python3
"""Benchmark read-only SQLite PRAGMA choices against the compact Vercel DB.

The controller extracts the compact snapshot into a temporary directory and
runs every variant in an isolated process.  Each worker executes the production
``search_ncs`` function for the 50 candidate-evaluation queries, 25 readiness
``COUNT(*)`` statements, seeded detail lookups, and connection-open probes.

"Cold" in this report means the first workload pass in a fresh worker process
and fresh SQLite connection sequence.  The script does not flush the operating
system page cache, so it must not be presented as a physical cold-disk test.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
DEFAULT_MANIFEST = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.manifest.json"
DEFAULT_EVAL_PACK = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_JSON_OUT = ROOT / "reports" / "sqlite_pragma_experiment_20260830.json"
DEFAULT_MD_OUT = ROOT / "reports" / "sqlite_pragma_experiment_20260830.md"
RSS_VETO_BYTES = 50 * 1024 * 1024
P95_RATIO_VETO = 1.25
P95_ABSOLUTE_SLOP_MS = 5.0
DETAIL_SAMPLE_SEED = 20260830
DETAIL_SAMPLES_PER_TYPE = 25
CONNECTION_REPETITIONS = 25
READINESS_COUNT_REPETITIONS = 25
READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)


@dataclass(frozen=True)
class Variant:
    name: str
    query_only: bool = False
    mmap_bytes: int | None = None
    cache_kib: int | None = None
    family: str = "baseline"


class ClosingConnection(sqlite3.Connection):
    """SQLite connection whose context exit also releases the file handle."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def build_variants() -> list[Variant]:
    variants = [
        Variant("baseline", family="baseline"),
        Variant("query_only", query_only=True, family="query_only"),
    ]
    variants.extend(
        Variant(
            f"mmap_{mib}mb",
            query_only=True,
            mmap_bytes=mib * 1024 * 1024,
            family="mmap",
        )
        for mib in (0, 64, 128, 256)
    )
    variants.extend(
        Variant(
            f"cache_{mib}mb",
            query_only=True,
            cache_kib=mib * 1024,
            family="cache",
        )
        for mib in (8, 16, 32, 64)
    )
    variants.extend(
        (
            Variant(
                "combo_64mmap_16cache",
                query_only=True,
                mmap_bytes=64 * 1024 * 1024,
                cache_kib=16 * 1024,
                family="safe_combination",
            ),
            Variant(
                "combo_128mmap_32cache",
                query_only=True,
                mmap_bytes=128 * 1024 * 1024,
                cache_kib=32 * 1024,
                family="safe_combination",
            ),
            Variant(
                "combo_256mmap_64cache",
                query_only=True,
                mmap_bytes=256 * 1024 * 1024,
                cache_kib=64 * 1024,
                family="safe_combination",
            ),
        )
    )
    return variants


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def timing_summary(values: Sequence[float]) -> dict[str, Any]:
    return {
        "samples": len(values),
        "p50_ms": round(percentile(values, 0.50), 3),
        "p95_ms": round(percentile(values, 0.95), 3),
        "max_ms": round(max(values), 3) if values else 0.0,
        "total_ms": round(sum(values), 3),
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_uri(path: Path, *, immutable: bool = True) -> str:
    suffix = "?mode=ro"
    if immutable:
        suffix += "&immutable=1"
    return f"file:{path.resolve().as_posix()}{suffix}"


def open_read_only_connection(path: Path, variant: Variant) -> sqlite3.Connection:
    conn = sqlite3.connect(
        database_uri(path), uri=True, timeout=30.0, factory=ClosingConnection
    )
    conn.row_factory = sqlite3.Row
    if variant.query_only:
        conn.execute("PRAGMA query_only = ON")
    if variant.mmap_bytes is not None:
        conn.execute(f"PRAGMA mmap_size = {int(variant.mmap_bytes)}").fetchone()
    if variant.cache_kib is not None:
        conn.execute(f"PRAGMA cache_size = {-int(variant.cache_kib)}").fetchone()
    return conn


def effective_pragmas(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "query_only": int(conn.execute("PRAGMA query_only").fetchone()[0]),
        "mmap_size_bytes": int(conn.execute("PRAGMA mmap_size").fetchone()[0]),
        "cache_size_pages_or_negative_kib": int(
            conn.execute("PRAGMA cache_size").fetchone()[0]
        ),
        "page_size_bytes": int(conn.execute("PRAGMA page_size").fetchone()[0]),
    }


def memory_snapshot() -> dict[str, Any]:
    try:
        import psutil  # type: ignore

        memory = psutil.Process(os.getpid()).memory_info()
        return {
            "method": "psutil",
            "rss_bytes": int(memory.rss),
            "peak_rss_bytes": int(getattr(memory, "peak_wset", memory.rss)),
        }
    except Exception:
        pass

    if os.name == "nt":
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        get_current_process = ctypes.windll.kernel32.GetCurrentProcess
        get_process_memory_info = ctypes.windll.psapi.GetProcessMemoryInfo
        ok = get_process_memory_info(
            get_current_process(), ctypes.byref(counters), ctypes.sizeof(counters)
        )
        if ok:
            return {
                "method": "windows_psapi",
                "rss_bytes": int(counters.WorkingSetSize),
                "peak_rss_bytes": int(counters.PeakWorkingSetSize),
            }

    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        if sys.platform != "darwin":
            peak *= 1024
        return {"method": "resource_ru_maxrss", "rss_bytes": None, "peak_rss_bytes": peak}
    except Exception:
        return {"method": "unavailable", "rss_bytes": None, "peak_rss_bytes": None}


def timed(callable_obj: Any) -> tuple[Any, float]:
    started = time.perf_counter_ns()
    value = callable_obj()
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000.0
    return value, elapsed_ms


def load_eval_cases(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    cases = data.get("candidates") or data.get("cases") or data.get("queries") or []
    if len(cases) != 50:
        raise ValueError(f"expected exactly 50 candidate_eval cases, found {len(cases)}")
    if any(case.get("evaluation_status") != "candidate_eval" for case in cases):
        raise ValueError("all search cases must retain evaluation_status=candidate_eval")
    return cases


DETAIL_SPECS: dict[str, tuple[str, str, str]] = {
    "unit": (
        "competency_units",
        "unit_code",
        """SELECT cu.unit_code, cu.unit_name_raw, cu.unit_level_raw,
                  c.major_code, c.major_name, c.middle_code, c.middle_name,
                  c.small_code, c.small_name, c.sub_code, c.sub_name
             FROM competency_units cu
             JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE cu.unit_code = ?""",
    ),
    "element": (
        "competency_elements",
        "element_id",
        """SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw
             FROM competency_elements ce
             JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ce.element_id = ?""",
    ),
    "criteria": (
        "performance_criteria",
        "criteria_id",
        """SELECT pc.criteria_id, pc.criteria_text_raw, pc.criteria_text_refined,
                  ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw
             FROM performance_criteria pc
             JOIN competency_elements ce ON ce.element_id = pc.element_id
             JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE pc.criteria_id = ?""",
    ),
    "ksa": (
        "ksa_items",
        "ksa_id",
        """SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.ksa_text_refined,
                  ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw
             FROM ksa_items ki
             JOIN competency_elements ce ON ce.element_id = ki.element_id
             JOIN competency_units cu ON cu.unit_code = ce.unit_code
            WHERE ki.ksa_id = ?""",
    ),
}


def build_detail_plan(db_path: Path) -> list[dict[str, Any]]:
    rng = random.Random(DETAIL_SAMPLE_SEED)
    baseline = Variant("detail_plan")
    plan: list[dict[str, Any]] = []
    with open_read_only_connection(db_path, baseline) as conn:
        for result_type, (table_name, key_column, sql) in DETAIL_SPECS.items():
            count = int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
            if count <= 0:
                continue
            offsets = [rng.randrange(count) for _ in range(DETAIL_SAMPLES_PER_TYPE)]
            for offset in offsets:
                row = conn.execute(
                    f'SELECT "{key_column}" FROM "{table_name}" LIMIT 1 OFFSET ?',
                    (offset,),
                ).fetchone()
                plan.append(
                    {
                        "type": result_type,
                        "key": row[0],
                        "sql": sql,
                    }
                )
    return plan


def search_signature(result: dict[str, Any]) -> dict[str, Any]:
    rows = [
        {
            "type": row.get("type"),
            "id": str(row.get("id")),
            "match_mode": row.get("match_mode"),
            "matched_tokens": list(row.get("matched_tokens") or []),
        }
        for row in result.get("results", [])
    ]
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return {
        "returned": int(result.get("returned", len(rows))),
        "match_mode": result.get("match_mode"),
        "counts_by_type": result.get("counts_by_type", {}),
        "rows_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def run_search_pass(
    cases: Sequence[dict[str, Any]],
    search_ncs: Any,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    elapsed: list[float] = []
    signatures: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    for case in cases:
        case_id = str(case.get("case_id"))
        try:
            result, duration = timed(
                lambda case=case: search_ncs(
                    str(case.get("query", "")),
                    scope=str(case.get("scope_candidate") or "all"),
                    limit=20,
                    offset=0,
                )
            )
            elapsed.append(duration)
            signatures[case_id] = search_signature(result)
        except Exception as exc:
            errors.append({"case_id": case_id, "error_type": type(exc).__name__})
    summary = timing_summary(elapsed)
    summary.update(
        {
            "expected_cases": 50,
            "completed_cases": len(signatures),
            "error_count": len(errors),
            "errors": errors,
        }
    )
    return summary, signatures


def run_readiness_pass(
    db_path: Path, variant: Variant, table_names: Sequence[str]
) -> dict[str, Any]:
    elapsed: list[float] = []
    counts: dict[str, int] = {}
    with open_read_only_connection(db_path, variant) as conn:
        for table_name in table_names:
            value, duration = timed(
                lambda table_name=table_name: int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                )
            )
            elapsed.append(duration)
            counts[table_name] = value
    result = timing_summary(elapsed)
    result.update(
        {
            "count_query_count": len(table_names),
            "counts_sha256": hashlib.sha256(
                json.dumps(counts, sort_keys=True).encode("utf-8")
            ).hexdigest(),
            "counts": counts,
        }
    )
    return result


def run_detail_pass(
    db_path: Path, variant: Variant, detail_plan: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    elapsed: list[float] = []
    found = 0
    with open_read_only_connection(db_path, variant) as conn:
        for item in detail_plan:
            row, duration = timed(
                lambda item=item: conn.execute(item["sql"], (item["key"],)).fetchone()
            )
            elapsed.append(duration)
            found += int(row is not None)
    result = timing_summary(elapsed)
    result.update({"lookup_count": len(detail_plan), "found_count": found})
    return result


def run_connection_probe(db_path: Path, variant: Variant) -> dict[str, Any]:
    elapsed: list[float] = []
    for _ in range(CONNECTION_REPETITIONS):
        _, duration = timed(
            lambda: _open_select_close(db_path, variant)
        )
        elapsed.append(duration)
    return timing_summary(elapsed)


def _open_select_close(db_path: Path, variant: Variant) -> int:
    with open_read_only_connection(db_path, variant) as conn:
        return int(conn.execute("SELECT 1").fetchone()[0])


def write_probe(db_path: Path, variant: Variant) -> dict[str, Any]:
    with open_read_only_connection(db_path, variant) as conn:
        try:
            conn.execute("CREATE TABLE __ncs_pragma_write_probe(value INTEGER)")
            return {"blocked": False, "error_type": None}
        except sqlite3.Error as exc:
            return {
                "blocked": True,
                "error_type": type(exc).__name__,
                "message": str(exc)[:160],
            }


def worker_main(args: argparse.Namespace) -> int:
    db_path = Path(args.db)
    variant = Variant(**json.loads(args.variant_json))
    cases = load_eval_cases(Path(args.eval_pack))
    table_names = json.loads(Path(args.readiness_tables).read_text(encoding="utf-8"))
    detail_plan = json.loads(Path(args.detail_plan).read_text(encoding="utf-8"))

    sys.path.insert(0, str(ROOT / "src"))
    from ncs_mcp import server

    server.open_db = lambda: open_read_only_connection(db_path, variant)
    with open_read_only_connection(db_path, variant) as conn:
        pragmas = effective_pragmas(conn)
    started_memory = memory_snapshot()
    passes: dict[str, Any] = {}
    signatures: dict[str, Any] = {}
    for pass_name in ("cold", "warm"):
        search, search_signatures = run_search_pass(cases, server.search_ncs)
        readiness = run_readiness_pass(db_path, variant, table_names)
        details = run_detail_pass(db_path, variant, detail_plan)
        passes[pass_name] = {
            "search_50_candidate_eval": search,
            "readiness_25_count": readiness,
            "random_detail_lookup": details,
            "memory_after": memory_snapshot(),
        }
        signatures[pass_name] = search_signatures

    result = {
        "variant": asdict(variant),
        "uri_contract": {
            "mode": "ro",
            "immutable": True,
            "query_only": variant.query_only,
        },
        "effective_pragmas": pragmas,
        "write_probe": write_probe(db_path, variant),
        "connection_creation": run_connection_probe(db_path, variant),
        "passes": passes,
        "search_signatures": signatures,
        "memory_started": started_memory,
        "memory_final": memory_snapshot(),
    }
    Path(args.worker_out).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0


def compare_variant(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> dict[str, Any]:
    baseline_peak = baseline["memory_final"].get("peak_rss_bytes")
    candidate_peak = candidate["memory_final"].get("peak_rss_bytes")
    rss_delta = (
        int(candidate_peak) - int(baseline_peak)
        if baseline_peak is not None and candidate_peak is not None
        else None
    )
    quality_differences: list[str] = []
    baseline_signatures = baseline["search_signatures"]["warm"]
    for pass_name in ("cold", "warm"):
        current = candidate["search_signatures"][pass_name]
        for case_id, expected in baseline_signatures.items():
            if current.get(case_id) != expected:
                quality_differences.append(f"{pass_name}:{case_id}")

    unstable_workloads: list[str] = []
    for workload in (
        "search_50_candidate_eval",
        "readiness_25_count",
        "random_detail_lookup",
    ):
        baseline_p95 = float(baseline["passes"]["warm"][workload]["p95_ms"])
        candidate_p95 = float(candidate["passes"]["warm"][workload]["p95_ms"])
        threshold = max(
            baseline_p95 * P95_RATIO_VETO,
            baseline_p95 + P95_ABSOLUTE_SLOP_MS,
        )
        if candidate_p95 > threshold:
            unstable_workloads.append(workload)

    reasons: list[str] = []
    if quality_differences:
        reasons.append("search_result_or_metadata_difference")
    if rss_delta is not None and rss_delta > RSS_VETO_BYTES:
        reasons.append("peak_rss_delta_over_50mib")
    if unstable_workloads:
        reasons.append("unstable_warm_p95")
    if not candidate.get("write_probe", {}).get("blocked"):
        reasons.append("read_only_write_probe_failed")
    return {
        "veto": bool(reasons),
        "veto_reasons": reasons,
        "quality_difference_count": len(quality_differences),
        "quality_differences": quality_differences,
        "peak_rss_delta_bytes": rss_delta,
        "unstable_p95_workloads": unstable_workloads,
    }


def _ms(record: dict[str, Any], pass_name: str, workload: str, metric: str) -> float:
    return float(record["passes"][pass_name][workload][metric])


def choose_recommendation(results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    safe = [
        item
        for item in results
        if item["variant"]["family"] == "safe_combination"
        and not item.get("comparison_to_baseline", {}).get("veto")
    ]
    if not safe:
        return {
            "status": "no_safe_combination_promoted",
            "recommended_variant": "baseline",
            "reason": "Every tested combination hit a quality, RSS, or p95 veto.",
        }
    winner = min(
        safe,
        key=lambda item: (
            _ms(item, "warm", "search_50_candidate_eval", "p50_ms"),
            _ms(item, "warm", "search_50_candidate_eval", "p95_ms"),
        ),
    )
    baseline = results[0]
    baseline_p50 = _ms(baseline, "warm", "search_50_candidate_eval", "p50_ms")
    winner_p50 = _ms(winner, "warm", "search_50_candidate_eval", "p50_ms")
    improvement = ((baseline_p50 - winner_p50) / baseline_p50) if baseline_p50 else 0.0
    if improvement < 0.05:
        return {
            "status": "no_material_gain",
            "recommended_variant": "baseline_query_only_contract",
            "best_safe_combination": winner["variant"]["name"],
            "warm_search_p50_improvement_ratio": round(improvement, 4),
            "reason": "The best non-vetoed combination improved warm search p50 by less than 5%.",
        }
    return {
        "status": "candidate_for_product_ab_test",
        "recommended_variant": winner["variant"]["name"],
        "warm_search_p50_improvement_ratio": round(improvement, 4),
        "reason": "Non-vetoed combination with the lowest warm search p50; remote Vercel A/B is still required.",
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# SQLite Read-only PRAGMA Experiment",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Compact SQLite bytes: `{report['source']['sqlite_bytes']:,}`",
        f"- Search cases: `{report['method']['search_case_count']}` candidate_eval rows",
        f"- Readiness statements per pass: `{report['method']['readiness_count_query_count']}` COUNT queries",
        f"- Detail lookups per pass: `{report['method']['detail_lookup_count']}`",
        "- Cold definition: first pass in a fresh worker; OS page cache was not flushed.",
        "",
        "## Results",
        "",
        "| variant | effective mmap MiB | cache PRAGMA | warm search p50 | warm search p95 | warm ready p95 | warm detail p95 | peak RSS MiB | veto |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report["results"]:
        pragmas = item["effective_pragmas"]
        peak = item["memory_final"].get("peak_rss_bytes")
        comparison = item.get("comparison_to_baseline", {})
        lines.append(
            "| {name} | {mmap:.1f} | {cache} | {sp50:.3f} ms | {sp95:.3f} ms | "
            "{rp95:.3f} ms | {dp95:.3f} ms | {rss} | {veto} |".format(
                name=item["variant"]["name"],
                mmap=pragmas["mmap_size_bytes"] / (1024 * 1024),
                cache=pragmas["cache_size_pages_or_negative_kib"],
                sp50=_ms(item, "warm", "search_50_candidate_eval", "p50_ms"),
                sp95=_ms(item, "warm", "search_50_candidate_eval", "p95_ms"),
                rp95=_ms(item, "warm", "readiness_25_count", "p95_ms"),
                dp95=_ms(item, "warm", "random_detail_lookup", "p95_ms"),
                rss=(f"{peak / (1024 * 1024):.1f}" if peak is not None else "n/a"),
                veto=(", ".join(comparison.get("veto_reasons", [])) or "no"),
            )
        )
    recommendation = report["recommendation"]
    lines.extend(
        [
            "",
            "## Recommendation",
            "",
            f"- Status: `{recommendation['status']}`",
            f"- Recommended variant: `{recommendation['recommended_variant']}`",
            f"- Reason: {recommendation['reason']}",
            "",
            "## Safety and Interpretation",
            "",
            "- `mode=ro` is the OS/SQLite open contract that blocks database writes.",
            "- `immutable=1` asserts that the extracted fingerprinted snapshot will not change; it is not safe for a mutable database.",
            "- `PRAGMA query_only=ON` is connection-scoped defense in depth and does not replace `mode=ro`.",
            "- Negative `cache_size` values are kibibyte targets per connection, not a process-wide hard memory cap.",
            "- `mmap_size` reserves an address-space window and residency depends on touched pages; local Windows RSS is not a Vercel memory guarantee.",
            "- A remote Vercel A/B is required before product promotion because `/tmp`, Linux mmap, and serverless concurrency differ from local Windows.",
            "- No relevance claim is made: all 50 inputs remain candidate_eval and have no human labels.",
            "",
            "## Artifact Integrity",
            "",
            f"- Original archive unchanged: `{report['safety']['original_archive_unchanged']}`",
            f"- Extracted SQLite unchanged: `{report['safety']['extracted_database_unchanged']}`",
            f"- Read-only write probes all blocked: `{report['safety']['all_write_probes_blocked']}`",
            f"- Temporary directory cleaned: `{report['safety']['temporary_directory_cleaned']}`",
        ]
    )
    return "\n".join(lines) + "\n"


def controller_main(args: argparse.Namespace) -> int:
    archive = Path(args.archive).resolve()
    manifest_path = Path(args.manifest).resolve()
    eval_pack = Path(args.eval_pack).resolve()
    json_out = Path(args.out).resolve()
    md_out = Path(args.markdown_out).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = load_eval_cases(eval_pack)
    readiness_tables = [
        READINESS_CORE_TABLES[index % len(READINESS_CORE_TABLES)]
        for index in range(READINESS_COUNT_REPETITIONS)
    ]
    archive_stat_before = archive.stat()
    archive_sha_before = sha256_file(archive)
    variants = build_variants()
    results: list[dict[str, Any]] = []
    temp_path: Path | None = None
    extracted_sha_before = ""
    extracted_sha_after = ""
    sidecars_after: list[str] = []

    with tempfile.TemporaryDirectory(prefix="ncs_sqlite_pragma_") as temp_name:
        temp_path = Path(temp_name)
        db_path = temp_path / str(manifest["archive_member"])
        with zipfile.ZipFile(archive) as bundle:
            member = str(manifest["archive_member"])
            if member not in bundle.namelist():
                raise ValueError(f"archive member missing: {member}")
            with bundle.open(member) as source, db_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
        extracted_sha_before = sha256_file(db_path)
        if extracted_sha_before != manifest.get("sqlite_sha256"):
            raise ValueError("extracted SQLite SHA-256 does not match manifest")

        detail_plan = build_detail_plan(db_path)
        detail_plan_path = temp_path / "detail_plan.json"
        detail_plan_path.write_text(
            json.dumps(detail_plan, ensure_ascii=False), encoding="utf-8"
        )
        readiness_path = temp_path / "readiness_tables.json"
        readiness_path.write_text(json.dumps(readiness_tables), encoding="utf-8")

        for index, variant in enumerate(variants):
            worker_out = temp_path / f"worker_{index:02d}_{variant.name}.json"
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--db",
                str(db_path),
                "--variant-json",
                json.dumps(asdict(variant)),
                "--eval-pack",
                str(eval_pack),
                "--readiness-tables",
                str(readiness_path),
                "--detail-plan",
                str(detail_plan_path),
                "--worker-out",
                str(worker_out),
            ]
            environment = dict(os.environ)
            environment["PYTHONPATH"] = str(ROOT / "src")
            environment["PYTHONIOENCODING"] = "utf-8"
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=args.worker_timeout_seconds,
                check=False,
            )
            if completed.returncode != 0 or not worker_out.exists():
                raise RuntimeError(
                    f"worker failed for {variant.name}: rc={completed.returncode}; "
                    f"stderr={completed.stderr[-1000:]}"
                )
            results.append(json.loads(worker_out.read_text(encoding="utf-8")))

        baseline = results[0]
        for item in results:
            item["comparison_to_baseline"] = compare_variant(baseline, item)
        extracted_sha_after = sha256_file(db_path)
        sidecars_after = [
            path.name
            for path in temp_path.iterdir()
            if path.name.startswith(db_path.name + "-")
        ]

    assert temp_path is not None
    archive_stat_after = archive.stat()
    archive_sha_after = sha256_file(archive)
    report: dict[str, Any] = {
        "schema": "ncs_sqlite_pragma_experiment_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_local_windows_compact_snapshot_experiment",
        "source": {
            "archive_path": str(archive),
            "manifest_path": str(manifest_path),
            "eval_pack_path": str(eval_pack),
            "archive_sha256": archive_sha_before,
            "sqlite_sha256": extracted_sha_before,
            "sqlite_bytes": int(manifest["sqlite_bytes"]),
            "database_schema": manifest.get("database_schema"),
        },
        "method": {
            "search_contract": "ncs_mcp.server.search_ncs",
            "search_case_count": len(cases),
            "search_case_status": "candidate_eval_no_human_labels",
            "readiness_contract": (
                "25 SELECT COUNT(*) statements round-robin over the four "
                "production readiness core tables"
            ),
            "readiness_count_query_count": len(readiness_tables),
            "detail_lookup_count": DETAIL_SAMPLES_PER_TYPE * len(DETAIL_SPECS),
            "detail_seed": DETAIL_SAMPLE_SEED,
            "connection_repetitions": CONNECTION_REPETITIONS,
            "cold_definition": "first pass in a fresh worker process; OS page cache not flushed",
            "warm_definition": "second identical pass in the same worker after the cold pass",
            "veto_policy": {
                "quality_or_result_difference": "any difference vetoes",
                "peak_rss_delta_bytes_gt": RSS_VETO_BYTES,
                "warm_p95": (
                    "candidate > max(baseline*1.25, baseline+5ms) for any workload"
                ),
            },
        },
        "variants": [asdict(variant) for variant in variants],
        "results": results,
        "recommendation": choose_recommendation(results),
        "compatibility_risks": {
            "windows_local": [
                "OS page cache is shared across sequential variant workers and was not flushed.",
                "Windows mmap and working-set behavior do not reproduce Linux serverless exactly.",
            ],
            "vercel": [
                "Each SQLite connection receives its own cache target; concurrent invocations can multiply memory use.",
                "mmap_size is an address-space ceiling, while resident memory depends on accessed pages.",
                "immutable=1 is valid only after snapshot fingerprint verification and while /tmp content cannot change.",
                "Remote /tmp filesystem and cold extraction costs are outside this PRAGMA-only benchmark.",
            ],
        },
        "safety": {
            "database_uri_mode": "mode=ro&immutable=1",
            "query_only_interpretation": "connection-scoped defense in depth, not a mode=ro replacement",
            "raw_ksa_mutation": False,
            "status_updates": False,
            "human_reviewed_written": False,
            "accepted_written": False,
            "reviewed_written": False,
            "original_archive_unchanged": (
                archive_sha_before == archive_sha_after
                and archive_stat_before.st_size == archive_stat_after.st_size
                and archive_stat_before.st_mtime_ns == archive_stat_after.st_mtime_ns
            ),
            "extracted_database_unchanged": extracted_sha_before == extracted_sha_after,
            "all_write_probes_blocked": all(
                item.get("write_probe", {}).get("blocked") for item in results
            ),
            "sqlite_sidecars_created": sidecars_after,
            "temporary_directory_cleaned": not temp_path.exists(),
        },
        "commands": {
            "reproduce": (
                "python scripts/benchmark_sqlite_pragmas.py "
                "--out reports/sqlite_pragma_experiment_20260830.json "
                "--markdown-out reports/sqlite_pragma_experiment_20260830.md"
            ),
            "focused_tests": "python -m unittest tests.test_benchmark_sqlite_pragmas -v",
        },
    }
    json_out.parent.mkdir(parents=True, exist_ok=True)
    md_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({
        "out": str(json_out),
        "markdown_out": str(md_out),
        "recommendation": report["recommendation"],
        "safety": report["safety"],
    }, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", default=str(DEFAULT_ARCHIVE))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--eval-pack", default=str(DEFAULT_EVAL_PACK))
    parser.add_argument("--out", default=str(DEFAULT_JSON_OUT))
    parser.add_argument("--markdown-out", default=str(DEFAULT_MD_OUT))
    parser.add_argument("--worker-timeout-seconds", type=int, default=1800)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--db", help=argparse.SUPPRESS)
    parser.add_argument("--variant-json", help=argparse.SUPPRESS)
    parser.add_argument("--readiness-tables", help=argparse.SUPPRESS)
    parser.add_argument("--detail-plan", help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return worker_main(args) if args.worker else controller_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
