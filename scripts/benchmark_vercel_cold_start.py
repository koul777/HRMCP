"""Profile the compact Vercel snapshot cold path without mutating source artifacts."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import vercel_snapshot  # noqa: E402


SCHEMA = "ncs_vercel_cold_start_stages_v1"
REMOTE_SAMPLE_SCHEMA = "ncs_vercel_cold_start_remote_sample_v1"
DEFAULT_ARCHIVE = (
    ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
)
DEFAULT_MANIFEST = (
    ROOT
    / "deploy"
    / "vercel_mcp_app"
    / "api"
    / "ncs_ontology_compact.manifest.json"
)
DEFAULT_OUT = ROOT / "reports" / "vercel_cold_start_stages_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "vercel_cold_start_stages_20260830.md"
DOMINANT_STAGE_THRESHOLD = 0.40
HIGH_VARIANCE_CV_THRESHOLD = 0.25
_T = TypeVar("_T")


def generated_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return round(ordered[rank - 1], 3)


def latency_summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "samples": [],
            "sample_count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "p50": None,
            "p95": None,
            "population_stdev": None,
            "coefficient_of_variation": None,
        }
    mean = statistics.fmean(values)
    stdev = statistics.pstdev(values)
    return {
        "samples": [round(value, 3) for value in values],
        "sample_count": len(values),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "mean": round(mean, 3),
        "p50": _nearest_rank(values, 0.50),
        "p95": _nearest_rank(values, 0.95),
        "population_stdev": round(stdev, 3),
        "coefficient_of_variation": round(stdev / mean, 4) if mean else 0.0,
    }


def _timed(call: Callable[[], _T]) -> tuple[_T, float]:
    started = time.perf_counter()
    value = call()
    return value, _elapsed_ms(started)


def _file_metadata(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except OSError:
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": path.is_file(),
        "bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _process_memory() -> dict[str, Any]:
    if os.name == "nt":
        try:
            class ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                ]

            counters = ProcessMemoryCountersEx()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            )
            if ok:
                return {
                    "available": True,
                    "source": "windows_process_memory_counters_ex",
                    "working_set_bytes": int(counters.WorkingSetSize),
                    "peak_rss_bytes": int(counters.PeakWorkingSetSize),
                    "private_bytes": int(counters.PrivateUsage),
                }
        except (AttributeError, OSError, ValueError):
            pass
    else:
        try:
            import resource

            usage = resource.getrusage(resource.RUSAGE_SELF)
            multiplier = 1 if sys.platform == "darwin" else 1024
            return {
                "available": True,
                "source": "resource_ru_maxrss_process_lifetime",
                "working_set_bytes": None,
                "peak_rss_bytes": int(usage.ru_maxrss * multiplier),
                "private_bytes": None,
            }
        except (ImportError, OSError, ValueError):
            pass
    return {
        "available": False,
        "source": "unavailable",
        "working_set_bytes": None,
        "peak_rss_bytes": None,
        "private_bytes": None,
    }


def _readonly_sqlite_open_probe(path: Path) -> dict[str, Any]:
    database_uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return {"object_count": int(row[0]) if row else 0}


def _measure_once_in_directory(
    archive_path: Path,
    manifest_path: Path,
    run_dir: Path,
    *,
    run_number: int,
) -> dict[str, Any]:
    stages: dict[str, float] = {}
    diagnostics: dict[str, float] = {}
    memory_before = _process_memory()
    materialization_started = time.perf_counter()
    lock_fd: int | None = None
    lock_path: Path | None = None
    temp_path: Path | None = None
    destination = run_dir / vercel_snapshot.COMPACT_SNAPSHOT_NAME
    error: str | None = None
    manifest: dict[str, Any] | None = None
    stream_digest = ""

    try:
        def discover() -> dict[str, int]:
            return {
                "archive_bytes": archive_path.stat().st_size,
                "manifest_bytes": manifest_path.stat().st_size,
            }

        artifact_sizes, stages["archive_discover_stat"] = _timed(discover)
        inspected, stages["inspect_manifest_archive"] = _timed(
            lambda: vercel_snapshot.inspect_compact_archive(
                archive_path, manifest_path
            )
        )
        manifest, expected_member = inspected

        def prepare_destination() -> tuple[int | None, Path]:
            destination.parent.mkdir(parents=True, exist_ok=True)
            candidate_lock = destination.with_suffix(destination.suffix + ".lock")
            acquired = vercel_snapshot._acquire_lock(
                candidate_lock, timeout_seconds=1.0
            )
            return acquired, candidate_lock

        prepared, stages["destination_prepare_lock"] = _timed(prepare_destination)
        lock_fd, lock_path = prepared
        if lock_fd is None:
            raise RuntimeError("unable to acquire isolated benchmark lock")

        def prepare_temp() -> Path:
            temp_fd, temp_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
            )
            os.close(temp_fd)
            return Path(temp_name)

        temp_path, stages["tempfile_prepare"] = _timed(prepare_temp)
        digest = hashlib.sha256()
        extract_started = time.perf_counter()
        with zipfile.ZipFile(archive_path, "r") as archive:
            member = archive.getinfo(vercel_snapshot.COMPACT_SNAPSHOT_NAME)
            if (
                member.CRC != expected_member.CRC
                or member.file_size != expected_member.file_size
                or member.compress_size != expected_member.compress_size
            ):
                raise RuntimeError("archive member changed after inspection")
            with archive.open(member, "r") as source, temp_path.open("wb") as output:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    output.write(block)
                stages["extract_stream_write_sha256"] = _elapsed_ms(extract_started)
                fsync_started = time.perf_counter()
                output.flush()
                os.fsync(output.fileno())
                stages["extracted_file_flush_fsync"] = _elapsed_ms(fsync_started)
        stream_digest = digest.hexdigest()

        valid, stages["sqlite_validation_open"] = _timed(
            lambda: vercel_snapshot._validate_compact_database(
                temp_path, manifest, computed_sha256=stream_digest
            )
        )
        if not valid:
            raise RuntimeError("extracted compact snapshot failed validation")

        _, stages["publish_rename"] = _timed(
            lambda: os.replace(temp_path, destination)
        )
        temp_path = None
        _, stages["verified_stamp_fsync_rename"] = _timed(
            lambda: vercel_snapshot._write_verified_stamp(destination, manifest)
        )
    except (OSError, RuntimeError, ValueError, zipfile.BadZipFile) as exc:
        error = f"{type(exc).__name__}: {exc}"
        artifact_sizes = {}
    finally:
        cleanup_started = time.perf_counter()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        if lock_fd is not None:
            os.close(lock_fd)
        if lock_path is not None:
            lock_path.unlink(missing_ok=True)
        stages["lock_release_cleanup"] = _elapsed_ms(cleanup_started)

    materialization_total_ms = _elapsed_ms(materialization_started)
    sqlite_probe: dict[str, Any] | None = None
    if error is None and destination.is_file():
        sqlite_probe, stages["serving_sqlite_open_probe"] = _timed(
            lambda: _readonly_sqlite_open_probe(destination)
        )
    cold_path_total_ms = round(
        materialization_total_ms + stages.get("serving_sqlite_open_probe", 0.0), 3
    )

    archive_sha256: str | None = None
    extracted_sha256: str | None = None
    diagnostic_started = time.perf_counter()
    if error is None and destination.is_file() and manifest is not None:
        archive_sha256, diagnostics["archive_sha256_diagnostic"] = _timed(
            lambda: _sha256_file(archive_path)
        )
        extracted_sha256, diagnostics[
            "extracted_db_sha256_second_pass_diagnostic"
        ] = _timed(lambda: _sha256_file(destination))
    diagnostic_total_ms = _elapsed_ms(diagnostic_started)
    measurement_total_ms = round(cold_path_total_ms + diagnostic_total_ms, 3)
    memory_after = _process_memory()
    peak_before = memory_before.get("peak_rss_bytes")
    peak_after = memory_after.get("peak_rss_bytes")
    peak_delta = (
        max(0, int(peak_after) - int(peak_before))
        if isinstance(peak_before, int) and isinstance(peak_after, int)
        else None
    )

    runtime_stage_sum = round(sum(stages.values()), 3)
    unattributed = round(max(0.0, cold_path_total_ms - runtime_stage_sum), 3)
    stage_share = {
        name: round(value / cold_path_total_ms, 4) if cold_path_total_ms else None
        for name, value in stages.items()
    }
    stage_share["unattributed_runtime_overhead"] = (
        round(unattributed / cold_path_total_ms, 4) if cold_path_total_ms else None
    )
    diagnostic_share = {
        name: round(value / measurement_total_ms, 4) if measurement_total_ms else None
        for name, value in diagnostics.items()
    }
    return {
        "run_number": run_number,
        "run_class": (
            "process_first_touch" if run_number == 1 else "fresh_destination_page_cache_unknown"
        ),
        "ok": error is None,
        "error": error,
        "artifact_sizes": artifact_sizes,
        "runtime_stages_ms": stages,
        "runtime_stage_share_of_cold_path": stage_share,
        "runtime_unattributed_overhead_ms": unattributed,
        "diagnostic_only_stages_ms": diagnostics,
        "diagnostic_share_of_measurement": diagnostic_share,
        "materialization_total_ms": materialization_total_ms,
        "cold_path_total_ms": cold_path_total_ms,
        "diagnostic_total_ms": diagnostic_total_ms,
        "measurement_total_ms": measurement_total_ms,
        "stream_extracted_sha256": stream_digest or None,
        "archive_sha256": archive_sha256,
        "extracted_db_sha256_second_pass": extracted_sha256,
        "stream_sha256_matches_manifest": bool(
            manifest and stream_digest == manifest.get("sqlite_sha256")
        ),
        "second_pass_sha256_matches_manifest": bool(
            manifest and extracted_sha256 == manifest.get("sqlite_sha256")
        ),
        "serving_sqlite_probe": sqlite_probe,
        "memory_before": memory_before,
        "memory_after": memory_after,
        "peak_rss_delta_bytes": peak_delta,
        "destination_bytes": (
            destination.stat().st_size if destination.is_file() else None
        ),
    }


def measure_once(
    archive_path: Path,
    manifest_path: Path,
    *,
    run_number: int,
    temp_root: Path | None = None,
) -> dict[str, Any]:
    raw_dir = tempfile.mkdtemp(
        prefix="ncs_vercel_cold_stage_",
        dir=str(temp_root) if temp_root is not None else None,
    )
    run_dir = Path(raw_dir)
    try:
        result = _measure_once_in_directory(
            archive_path, manifest_path, run_dir, run_number=run_number
        )
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    result["temporary_directory_removed"] = not run_dir.exists()
    return result


def _summaries_by_stage(
    runs: list[dict[str, Any]], field: str
) -> dict[str, dict[str, Any]]:
    names = sorted(
        {
            name
            for run in runs
            for name in (run.get(field) or {})
        }
    )
    return {
        name: latency_summary(
            [
                float(run[field][name])
                for run in runs
                if isinstance(run.get(field), dict) and name in run[field]
            ]
        )
        for name in names
    }


def _dominance_and_gate(
    runtime_stages: dict[str, dict[str, Any]],
    cold_total: dict[str, Any],
) -> dict[str, Any]:
    total_p50 = cold_total.get("p50")
    contributions: dict[str, float | None] = {}
    for name, summary in runtime_stages.items():
        stage_p50 = summary.get("p50")
        contributions[name] = (
            round(float(stage_p50) / float(total_p50), 4)
            if isinstance(stage_p50, (int, float))
            and isinstance(total_p50, (int, float))
            and total_p50 > 0
            else None
        )
    ranked = sorted(
        (
            (name, share)
            for name, share in contributions.items()
            if isinstance(share, float)
        ),
        key=lambda item: (-item[1], item[0]),
    )
    dominant_name = ranked[0][0] if ranked else None
    dominant_share = ranked[0][1] if ranked else None
    threshold_met = bool(
        isinstance(dominant_share, float)
        and dominant_share >= DOMINANT_STAGE_THRESHOLD
    )
    candidate_map = {
        "archive_discover_stat": "Reduce redundant path metadata work only if remote profiling confirms it.",
        "inspect_manifest_archive": "Cache safe manifest/archive metadata within one warm instance; preserve validation on a fresh instance.",
        "destination_prepare_lock": "Review lock and destination setup only after concurrent-cold behavior is measured.",
        "tempfile_prepare": "Review temporary-file setup only if remote ephemeral storage confirms the cost.",
        "extract_stream_write_sha256": "Compare extraction/compression variants under the 480 MB snapshot cap while preserving the streaming SHA-256 check.",
        "extracted_file_flush_fsync": "Measure Vercel ephemeral-storage durability cost; do not remove fsync without an explicit safety decision.",
        "sqlite_validation_open": "Profile validation queries or safe metadata caching without weakening schema, count, or hash checks.",
        "publish_rename": "Review atomic publish behavior only if remote rename cost is dominant.",
        "verified_stamp_fsync_rename": "Review verified-stamp persistence only if remote profiling confirms it; preserve content binding.",
        "lock_release_cleanup": "Review lock cleanup only if remote profiling confirms it.",
        "serving_sqlite_open_probe": "Evaluate read-only SQLite connection PRAGMAs only with latency and RSS evidence.",
    }
    return {
        "runtime_stage_p50_share": contributions,
        "dominant_runtime_stage": dominant_name,
        "dominant_runtime_stage_p50_share": dominant_share,
        "dominant_threshold": DOMINANT_STAGE_THRESHOLD,
        "dominant_threshold_met": threshold_met,
        "local_candidate": (
            candidate_map.get(dominant_name) if threshold_met and dominant_name else None
        ),
        "remote_measurement_status": "not_measured",
        "remote_confirmation_required": True,
        "promotion_allowed": False,
        "promotion_blocker": "fresh Vercel instance stage measurements are not collected",
        "diagnostic_hashes_are_optimization_targets": False,
    }


def _remote_collection_contract(runs: int) -> dict[str, Any]:
    return {
        "schema": REMOTE_SAMPLE_SCHEMA,
        "status": "not_measured",
        "required_run_count": max(3, runs),
        "required_run_fields": [
            "run_number",
            "instance_id_hash",
            "request_id",
            "fresh_instance_evidence",
            "runtime_stages_ms",
            "materialization_total_ms",
            "cold_path_total_ms",
            "peak_rss_bytes",
            "archive_bytes",
            "sqlite_bytes",
        ],
        "required_summary_fields": [
            "runtime_stage_p50_p95",
            "runtime_stage_contribution",
            "runtime_stage_coefficient_of_variation",
            "dominant_runtime_stage",
        ],
        "controlled_runtime_command": (
            "python scripts/benchmark_vercel_cold_start.py --runs "
            f"{max(3, runs)} --environment-label vercel-fresh-instance "
            "--out reports/vercel_cold_start_remote.json "
            "--markdown-out reports/vercel_cold_start_remote.md"
        ),
        "collection_notes": [
            "Run inside a controlled Vercel-equivalent or instrumented fresh function instance; this command is not an HTTP remote caller.",
            "Record provider request/instance evidence without secrets or raw identifiers.",
            "Do not label warm endpoint latency as a cold materialization sample.",
            "Do not promote an optimization until remote p50/p95, contribution, variance, RSS, and size are compared.",
        ],
    }


def build_report(
    *,
    archive_path: Path,
    manifest_path: Path,
    runs: int,
    environment_label: str,
    temp_root: Path | None = None,
) -> dict[str, Any]:
    source_before = {
        "archive": _file_metadata(archive_path),
        "manifest": _file_metadata(manifest_path),
    }
    if not source_before["archive"].get("exists") or not source_before[
        "manifest"
    ].get("exists"):
        return {
            "schema": SCHEMA,
            "version": 1,
            "generated_at": generated_at(),
            "ok": False,
            "error": "archive_or_manifest_missing",
            "source_artifacts_before": source_before,
            "remote_collection_contract": _remote_collection_contract(runs),
            "safety": {
                "source_artifacts_read_only": True,
                "database_writes": False,
                "status_updates": False,
                "human_review_claim": False,
            },
        }

    measured_runs = [
        measure_once(
            archive_path,
            manifest_path,
            run_number=run_number,
            temp_root=temp_root,
        )
        for run_number in range(1, runs + 1)
    ]
    successful_runs = [run for run in measured_runs if run.get("ok") is True]
    runtime_stages = _summaries_by_stage(successful_runs, "runtime_stages_ms")
    diagnostic_stages = _summaries_by_stage(
        successful_runs, "diagnostic_only_stages_ms"
    )
    cold_total = latency_summary(
        [float(run["cold_path_total_ms"]) for run in successful_runs]
    )
    materialization_total = latency_summary(
        [float(run["materialization_total_ms"]) for run in successful_runs]
    )
    measurement_total = latency_summary(
        [float(run["measurement_total_ms"]) for run in successful_runs]
    )
    source_after = {
        "archive": _file_metadata(archive_path),
        "manifest": _file_metadata(manifest_path),
    }
    gate = _dominance_and_gate(runtime_stages, cold_total)
    stage_cvs = {
        name: summary.get("coefficient_of_variation")
        for name, summary in runtime_stages.items()
    }
    total_cv = cold_total.get("coefficient_of_variation")
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "ok": len(successful_runs) == runs,
        "mode": "read_only_local_process_cold_materialization",
        "environment_label": environment_label,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "page_cache_controlled": False,
            "caveat": (
                "Each run uses a fresh destination, but the operating-system page cache is not "
                "cleared. These are local stage measurements, not Vercel absolute cold latency."
            ),
        },
        "source_artifacts_before": source_before,
        "source_artifacts_after": source_after,
        "source_artifacts_unchanged": source_before == source_after,
        "archive_bytes": source_before["archive"].get("bytes"),
        "manifest_bytes": source_before["manifest"].get("bytes"),
        "sqlite_bytes": next(
            (
                run.get("destination_bytes")
                for run in successful_runs
                if run.get("destination_bytes") is not None
            ),
            None,
        ),
        "run_count_requested": runs,
        "run_count_successful": len(successful_runs),
        "runs": measured_runs,
        "summary": {
            "materialization_total_ms": materialization_total,
            "cold_path_total_ms": cold_total,
            "measurement_total_including_diagnostics_ms": measurement_total,
            "runtime_stages_ms": runtime_stages,
            "diagnostic_only_stages_ms": diagnostic_stages,
            "repeat_variance": {
                "cold_path_coefficient_of_variation": total_cv,
                "runtime_stage_coefficient_of_variation": stage_cvs,
                "high_variance_threshold": HIGH_VARIANCE_CV_THRESHOLD,
                "high_variance": bool(
                    isinstance(total_cv, (int, float))
                    and total_cv > HIGH_VARIANCE_CV_THRESHOLD
                ),
            },
            "optimization_gate": gate,
        },
        "stage_contract": {
            "runtime_critical": list(runtime_stages),
            "diagnostic_only": list(diagnostic_stages),
            "archive_sha256_runtime_behavior": (
                "not performed by materialize_compact_snapshot; measured after the cold path for diagnostics only"
            ),
            "extracted_sha256_runtime_behavior": (
                "computed during extraction; the second file pass is diagnostic only"
            ),
            "sqlite_open_behavior": (
                "sqlite_validation_open mirrors compact metadata validation; serving_sqlite_open_probe records the following read-only open"
            ),
        },
        "remote_collection_contract": _remote_collection_contract(runs),
        "safety": {
            "source_artifacts_read_only": True,
            "temporary_destination_only": True,
            "temporary_directories_removed": all(
                run.get("temporary_directory_removed") is True for run in measured_runs
            ),
            "database_writes": False,
            "status_updates": False,
            "human_review_claim": False,
            "remote_calls": False,
            "deployment_changes": False,
        },
        "commands": {
            "reproduce": (
                "python scripts/benchmark_vercel_cold_start.py "
                f"--archive \"{archive_path}\" --manifest \"{manifest_path}\" "
                f"--runs {runs} --environment-label \"{environment_label}\""
            )
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    if not report.get("ok") and "summary" not in report:
        return (
            "# Vercel Cold Start Stage Profile\n\n"
            f"- Status: `failed`\n- Error: `{report.get('error')}`\n"
            "- Remote measurement status: `not_measured`\n"
        )
    summary = report["summary"]
    gate = summary["optimization_gate"]
    lines = [
        "# Vercel Cold Start Stage Profile",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Environment: `{report['environment_label']}`",
        f"- Successful runs: `{report['run_count_successful']}/{report['run_count_requested']}`",
        f"- Archive bytes: `{report['archive_bytes']}`",
        f"- SQLite bytes: `{report['sqlite_bytes']}`",
        f"- Cold-path p50/p95: `{summary['cold_path_total_ms']['p50']}` / `{summary['cold_path_total_ms']['p95']}` ms",
        f"- Page cache controlled: `{report['environment']['page_cache_controlled']}`",
        f"- Remote measurement status: `{gate['remote_measurement_status']}`",
        "",
        "## Runtime-Critical Stages",
        "",
        "| Stage | p50 ms | p95 ms | p50 contribution | CV |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name, stage in summary["runtime_stages_ms"].items():
        share = gate["runtime_stage_p50_share"].get(name)
        lines.append(
            f"| `{name}` | {stage['p50']} | {stage['p95']} | {share} | {stage['coefficient_of_variation']} |"
        )
    lines.extend(
        [
            "",
            "## Diagnostic-Only Hash Passes",
            "",
            "These passes are excluded from runtime cold-path totals and cannot justify a runtime optimization.",
            "",
            "| Stage | p50 ms | p95 ms | CV |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for name, stage in summary["diagnostic_only_stages_ms"].items():
        lines.append(
            f"| `{name}` | {stage['p50']} | {stage['p95']} | {stage['coefficient_of_variation']} |"
        )
    lines.extend(
        [
            "",
            "## Repeats",
            "",
            "| Run | Class | Cold path ms | Materialization ms | Diagnostic ms | Peak RSS delta bytes | OK |",
            "| ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for run in report["runs"]:
        lines.append(
            "| "
            f"{run['run_number']} | `{run['run_class']}` | {run['cold_path_total_ms']} | "
            f"{run['materialization_total_ms']} | {run['diagnostic_total_ms']} | "
            f"{run['peak_rss_delta_bytes']} | {run['ok']} |"
        )
    lines.extend(
        [
            "",
            "## Optimization Gate",
            "",
            f"- Dominant runtime stage: `{gate['dominant_runtime_stage']}`",
            f"- Dominant p50 contribution: `{gate['dominant_runtime_stage_p50_share']}`",
            f"- Dominance threshold met: `{gate['dominant_threshold_met']}`",
            f"- Local candidate: `{gate['local_candidate']}`",
            f"- Promotion allowed: `{gate['promotion_allowed']}`",
            f"- Blocker: `{gate['promotion_blocker']}`",
            f"- Cold-path CV: `{summary['repeat_variance']['cold_path_coefficient_of_variation']}`",
            f"- High variance: `{summary['repeat_variance']['high_variance']}`",
            "",
            "## Interpretation",
            "",
            f"- {report['environment']['caveat']}",
            "- The runtime stream already calculates the extracted SQLite SHA-256; the second-pass hash is diagnostic only.",
            "- The archive SHA-256 pass is diagnostic only because the current materializer does not perform it.",
            "- No optimization is eligible for promotion until fresh-instance Vercel stage values are collected.",
            "",
            "## Remote Collection Contract",
            "",
            f"- Schema: `{report['remote_collection_contract']['schema']}`",
            f"- Status: `{report['remote_collection_contract']['status']}`",
            "- Controlled command:",
            "",
            "```powershell",
            report["remote_collection_contract"]["controlled_runtime_command"],
            "```",
            "",
            "## Safety",
            "",
            f"- Source artifacts unchanged: `{report['source_artifacts_unchanged']}`",
            f"- Temporary directories removed: `{report['safety']['temporary_directories_removed']}`",
            "- DB writes, status updates, human-review claims, remote calls, deployment changes: `false`",
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
        description="Profile compact Vercel snapshot cold-start stages read-only."
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--environment-label", default="local-workstation")
    parser.add_argument("--temp-root", type=Path)
    args = parser.parse_args(argv)
    if args.runs < 3 or args.runs > 20:
        parser.error("--runs must be between 3 and 20")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        archive_path=args.archive.resolve(),
        manifest_path=args.manifest.resolve(),
        runs=args.runs,
        environment_label=str(args.environment_label),
        temp_root=args.temp_root.resolve() if args.temp_root else None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report.get("summary", report), ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
