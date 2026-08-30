"""Compare compact-snapshot extraction kernels without changing runtime code.

The benchmark reads the production compact ZIP and manifest, writes only to an
isolated temporary directory, and separates locally safe candidates from
diagnostic upper bounds that weaken durability or content verification.
"""

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
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psutil as _psutil
except ImportError:  # pragma: no cover - exercised by dependency-light runtimes
    _psutil = None


ROOT = Path(__file__).resolve().parents[1]
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
DEFAULT_OUT = ROOT / "reports" / "vercel_cold_optimization_experiments_20260830.json"
DEFAULT_MARKDOWN_OUT = (
    ROOT / "reports" / "vercel_cold_optimization_experiments_20260830.md"
)
SCHEMA = "ncs_vercel_cold_optimization_experiments_v1"
REMOTE_STATUS = "not_measured"
MIN_LOCAL_P50_IMPROVEMENT_PERCENT = 5.0
MAX_P95_REGRESSION_PERCENT = 5.0
MAX_CV = 0.25
MAX_RSS_DELTA_BYTES = 50 * 1024 * 1024


@dataclass(frozen=True)
class Variant:
    variant_id: str
    label: str
    buffer_bytes: int
    use_readinto: bool
    compute_stream_sha256: bool
    fsync_extracted_file: bool
    integrity_guarantee_level: str
    crash_recovery_risk: str
    candidate_class: str
    default_promotion_prohibited: bool


def experiment_variants() -> list[Variant]:
    return [
        Variant(
            "baseline_1m_stream_hash_fsync",
            "Baseline: 1 MiB read/write, streaming SHA-256, fsync",
            1024 * 1024,
            False,
            True,
            True,
            "cryptographic_content_binding_and_explicit_flush_barrier",
            "low_local_temp_publish_risk; provider semantics still require remote confirmation",
            "baseline",
            False,
        ),
        Variant(
            "safe_readinto_4m_stream_hash_fsync",
            "Safe candidate: 4 MiB readinto, streaming SHA-256, fsync",
            4 * 1024 * 1024,
            True,
            True,
            True,
            "cryptographic_content_binding_and_explicit_flush_barrier",
            "low_local_temp_publish_risk; provider semantics still require remote confirmation",
            "safe_candidate",
            False,
        ),
        Variant(
            "diagnostic_readinto_4m_stream_hash_no_fsync",
            "Diagnostic: 4 MiB readinto, streaming SHA-256, no fsync",
            4 * 1024 * 1024,
            True,
            True,
            False,
            "cryptographic_content_binding_without_explicit_flush_barrier",
            "elevated_if_process_or_instance_stops before ephemeral storage is durable",
            "durability_tradeoff",
            True,
        ),
        Variant(
            "diagnostic_readinto_4m_no_hash_fsync",
            "Diagnostic: 4 MiB readinto, ZIP CRC/size only, fsync",
            4 * 1024 * 1024,
            True,
            False,
            True,
            "zip_crc_and_size_only_no_cryptographic_content_binding",
            "flush barrier retained, but wrong-yet-CRC-valid content is not bound to the manifest",
            "integrity_upper_bound",
            True,
        ),
        Variant(
            "diagnostic_readinto_4m_no_hash_no_fsync",
            "Diagnostic upper bound: 4 MiB readinto, no SHA-256, no fsync",
            4 * 1024 * 1024,
            True,
            False,
            False,
            "zip_crc_and_size_only_no_cryptographic_content_binding_or_flush_barrier",
            "highest: neither manifest content binding nor explicit flush barrier is retained",
            "unsafe_performance_upper_bound",
            True,
        ),
    ]


def generated_at() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


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


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)


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


def _current_rss_sample() -> tuple[int | None, str]:
    if _psutil is not None:
        try:
            return int(_psutil.Process(os.getpid()).memory_info().rss), "psutil_process_rss"
        except (AttributeError, OSError, ValueError):
            pass
    if os.name == "nt":
        try:
            class ProcessMemoryCounters(ctypes.Structure):
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
                ]

            counters = ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(
                handle, ctypes.byref(counters), counters.cb
            ):
                return int(counters.WorkingSetSize), "windows_process_working_set"
        except (AttributeError, OSError, ValueError):
            pass
    try:
        statm = Path("/proc/self/statm")
        if statm.is_file():
            resident_pages = int(statm.read_text(encoding="ascii").split()[1])
            return (
                resident_pages * int(os.sysconf("SC_PAGE_SIZE")),
                "proc_self_statm_resident",
            )
    except (OSError, ValueError, IndexError):
        pass
    return None, "unavailable"


class _RssSampler:
    def __init__(self, interval_seconds: float = 0.01) -> None:
        self.interval_seconds = interval_seconds
        self.before_bytes, self.measurement_method = _current_rss_sample()
        self.peak_bytes = self.before_bytes
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            current, method = _current_rss_sample()
            if current is not None:
                self.measurement_method = method
            if current is not None and (
                self.peak_bytes is None or current > self.peak_bytes
            ):
                self.peak_bytes = current

    def __enter__(self) -> "_RssSampler":
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        current, method = _current_rss_sample()
        if current is not None:
            self.measurement_method = method
        if current is not None and (
            self.peak_bytes is None or current > self.peak_bytes
        ):
            self.peak_bytes = current

    @property
    def delta_bytes(self) -> int | None:
        if self.before_bytes is None or self.peak_bytes is None:
            return None
        return max(0, self.peak_bytes - self.before_bytes)


def _find_database_member(archive: zipfile.ZipFile) -> zipfile.ZipInfo:
    candidates = [
        item for item in archive.infolist() if not item.is_dir() and item.filename.endswith(".db")
    ]
    if len(candidates) != 1:
        raise RuntimeError(f"expected exactly one SQLite member, found {len(candidates)}")
    return candidates[0]


def _sqlite_probe(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only = ON")
        row = conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    return {"object_count": int(row[0]) if row else 0}


def measure_variant_once(
    archive_path: Path,
    manifest: dict[str, Any],
    variant: Variant,
    *,
    run_number: int,
    temp_root: Path | None = None,
) -> dict[str, Any]:
    raw_dir = tempfile.mkdtemp(
        prefix=f"ncs_cold_opt_{variant.variant_id}_",
        dir=str(temp_root) if temp_root else None,
    )
    run_dir = Path(raw_dir)
    destination = run_dir / "ncs_ontology_compact.db"
    stages: dict[str, float] = {}
    digest_hex: str | None = None
    error: str | None = None
    probe: dict[str, Any] | None = None
    expected_sha = manifest.get("sqlite_sha256")
    expected_bytes = manifest.get("sqlite_bytes")
    total_started = time.perf_counter()
    sampler = _RssSampler()
    try:
        with sampler:
            extract_started = time.perf_counter()
            digest = hashlib.sha256() if variant.compute_stream_sha256 else None
            with zipfile.ZipFile(archive_path, "r") as archive:
                member = _find_database_member(archive)
                with archive.open(member, "r") as source, destination.open(
                    "wb", buffering=variant.buffer_bytes
                ) as output:
                    if variant.use_readinto:
                        buffer = bytearray(variant.buffer_bytes)
                        view = memoryview(buffer)
                        while True:
                            count = source.readinto(buffer)
                            if not count:
                                break
                            block = view[:count]
                            if digest is not None:
                                digest.update(block)
                            output.write(block)
                    else:
                        while True:
                            block = source.read(variant.buffer_bytes)
                            if not block:
                                break
                            if digest is not None:
                                digest.update(block)
                            output.write(block)
                    stages["extract_stream_write_hash"] = _elapsed_ms(extract_started)
                    flush_started = time.perf_counter()
                    output.flush()
                    if variant.fsync_extracted_file:
                        os.fsync(output.fileno())
                    stages["flush_and_optional_fsync"] = _elapsed_ms(flush_started)
            digest_hex = digest.hexdigest() if digest is not None else None
            if isinstance(expected_bytes, int) and destination.stat().st_size != expected_bytes:
                raise RuntimeError("extracted byte count does not match manifest")
            if digest_hex is not None and isinstance(expected_sha, str) and digest_hex != expected_sha:
                raise RuntimeError("streaming SHA-256 does not match manifest")
            probe_started = time.perf_counter()
            probe = _sqlite_probe(destination)
            stages["readonly_sqlite_probe"] = _elapsed_ms(probe_started)
    except (OSError, RuntimeError, ValueError, sqlite3.Error, zipfile.BadZipFile) as exc:
        error = f"{type(exc).__name__}: {exc}"
    total_ms = _elapsed_ms(total_started)
    result = {
        "run_number": run_number,
        "ok": error is None,
        "error": error,
        "total_ms": total_ms,
        "stages_ms": stages,
        "destination_bytes": destination.stat().st_size if destination.is_file() else None,
        "stream_sha256": digest_hex,
        "stream_sha256_matches_manifest": (
            digest_hex == expected_sha if digest_hex is not None and isinstance(expected_sha, str) else None
        ),
        "fsync_performed": variant.fsync_extracted_file,
        "sqlite_probe": probe,
        "rss_before_bytes": sampler.before_bytes,
        "peak_rss_bytes": sampler.peak_bytes,
        "peak_rss_delta_bytes": sampler.delta_bytes,
        "rss_measurement_method": sampler.measurement_method,
    }
    shutil.rmtree(run_dir, ignore_errors=True)
    result["temporary_directory_removed"] = not run_dir.exists()
    return result


def _stage_summaries(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    stage_names = sorted({name for run in runs for name in run.get("stages_ms", {})})
    return {
        name: latency_summary(
            [float(run["stages_ms"][name]) for run in runs if name in run.get("stages_ms", {})]
        )
        for name in stage_names
    }


def _percent_change(candidate: float | None, baseline: float | None) -> float | None:
    if candidate is None or baseline in (None, 0):
        return None
    return round((candidate - baseline) / baseline * 100, 2)


def _candidate_summary(
    variant: Variant,
    runs: list[dict[str, Any]],
    *,
    archive_bytes: int,
    sqlite_bytes: int,
) -> dict[str, Any]:
    successful = [run for run in runs if run.get("ok") is True]
    total = latency_summary([float(run["total_ms"]) for run in successful])
    peak_values = [
        int(run["peak_rss_bytes"])
        for run in successful
        if isinstance(run.get("peak_rss_bytes"), int)
    ]
    peak_delta_values = [
        int(run["peak_rss_delta_bytes"])
        for run in successful
        if isinstance(run.get("peak_rss_delta_bytes"), int)
    ]
    rss_methods = sorted(
        {
            str(run["rss_measurement_method"])
            for run in successful
            if run.get("rss_measurement_method")
        }
    )
    return {
        "variant": asdict(variant),
        "archive_bytes": archive_bytes,
        "uncompressed_sqlite_bytes": sqlite_bytes,
        "run_count": len(runs),
        "successful_run_count": len(successful),
        "runs": runs,
        "total_ms": total,
        "stages_ms": _stage_summaries(successful),
        "peak_rss_bytes_max": max(peak_values) if peak_values else None,
        "peak_rss_delta_bytes_max": max(peak_delta_values) if peak_delta_values else None,
        "rss_measurement_methods": rss_methods,
        "remote_measurement_status": REMOTE_STATUS,
    }


def _attach_delta_and_gate(
    summary: dict[str, Any], baseline: dict[str, Any]
) -> None:
    candidate_p50 = summary["total_ms"]["p50"]
    candidate_p95 = summary["total_ms"]["p95"]
    baseline_p50 = baseline["total_ms"]["p50"]
    baseline_p95 = baseline["total_ms"]["p95"]
    p50_change = _percent_change(candidate_p50, baseline_p50)
    p95_change = _percent_change(candidate_p95, baseline_p95)
    summary["delta_vs_baseline"] = {
        "p50_ms": round(candidate_p50 - baseline_p50, 3)
        if candidate_p50 is not None and baseline_p50 is not None
        else None,
        "p95_ms": round(candidate_p95 - baseline_p95, 3)
        if candidate_p95 is not None and baseline_p95 is not None
        else None,
        "p50_change_percent": p50_change,
        "p95_change_percent": p95_change,
        "p50_improvement_percent": round(-p50_change, 2) if p50_change is not None else None,
    }
    variant = summary["variant"]
    p50_gate = bool(p50_change is not None and p50_change <= -MIN_LOCAL_P50_IMPROVEMENT_PERCENT)
    p95_gate = bool(p95_change is not None and p95_change <= MAX_P95_REGRESSION_PERCENT)
    cv = summary["total_ms"]["coefficient_of_variation"]
    cv_gate = bool(isinstance(cv, (int, float)) and cv <= MAX_CV)
    rss_delta = summary["peak_rss_delta_bytes_max"]
    rss_gate = rss_delta is None or rss_delta <= MAX_RSS_DELTA_BYTES
    integrity_gate = bool(variant["compute_stream_sha256"])
    durability_gate = bool(variant["fsync_extracted_file"])
    prohibited = bool(variant["default_promotion_prohibited"])
    local_gate = all(
        [p50_gate, p95_gate, cv_gate, rss_gate, integrity_gate, durability_gate, not prohibited]
    )
    summary["promotion_gate"] = {
        "minimum_local_p50_improvement_percent": MIN_LOCAL_P50_IMPROVEMENT_PERCENT,
        "maximum_p95_regression_percent": MAX_P95_REGRESSION_PERCENT,
        "maximum_cv": MAX_CV,
        "maximum_peak_rss_delta_bytes": MAX_RSS_DELTA_BYTES,
        "p50_gate_pass": p50_gate,
        "p95_gate_pass": p95_gate,
        "variance_gate_pass": cv_gate,
        "rss_gate_pass": rss_gate,
        "cryptographic_integrity_gate_pass": integrity_gate,
        "explicit_fsync_gate_pass": durability_gate,
        "default_promotion_prohibited": prohibited,
        "local_gate_pass": local_gate,
        "remote_confirmation_required": True,
        "remote_gate_pass": False,
        "promotion_allowed": False,
        "promotion_blocker": "fresh Vercel instance comparison is not measured",
    }


def build_report(
    *,
    archive_path: Path,
    manifest_path: Path,
    runs: int,
    environment_label: str,
    temp_root: Path | None = None,
    warmup: bool = True,
) -> dict[str, Any]:
    source_before = {
        "archive": _file_metadata(archive_path),
        "manifest": _file_metadata(manifest_path),
    }
    if not source_before["archive"].get("exists") or not source_before["manifest"].get("exists"):
        return {
            "schema": SCHEMA,
            "version": 1,
            "generated_at": generated_at(),
            "ok": False,
            "error": "archive_or_manifest_missing",
            "source_artifacts_before": source_before,
            "remote_measurement_status": REMOTE_STATUS,
        }
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    with zipfile.ZipFile(archive_path, "r") as archive:
        member = _find_database_member(archive)
        archive_bytes = archive_path.stat().st_size
        sqlite_bytes = member.file_size
    variants = experiment_variants()
    warmup_result: dict[str, Any] | None = None
    if warmup:
        warmup_result = measure_variant_once(
            archive_path, manifest, variants[0], run_number=0, temp_root=temp_root
        )
    runs_by_variant: dict[str, list[dict[str, Any]]] = {
        variant.variant_id: [] for variant in variants
    }
    for repetition in range(runs):
        rotated = variants[repetition % len(variants) :] + variants[: repetition % len(variants)]
        for variant in rotated:
            runs_by_variant[variant.variant_id].append(
                measure_variant_once(
                    archive_path,
                    manifest,
                    variant,
                    run_number=repetition + 1,
                    temp_root=temp_root,
                )
            )
    summaries = [
        _candidate_summary(
            variant,
            runs_by_variant[variant.variant_id],
            archive_bytes=archive_bytes,
            sqlite_bytes=sqlite_bytes,
        )
        for variant in variants
    ]
    baseline = summaries[0]
    for summary in summaries:
        _attach_delta_and_gate(summary, baseline)
    safe_remote_candidates = [
        summary
        for summary in summaries[1:]
        if summary["promotion_gate"]["local_gate_pass"]
    ]
    safe_remote_candidates.sort(key=lambda item: item["total_ms"]["p50"])
    source_after = {
        "archive": _file_metadata(archive_path),
        "manifest": _file_metadata(manifest_path),
    }
    all_runs = [run for summary in summaries for run in summary["runs"]]
    recommendation = (
        f"Remote-test {safe_remote_candidates[0]['variant']['variant_id']} first; do not promote before fresh-instance evidence."
        if safe_remote_candidates
        else "Keep the runtime baseline; no integrity-preserving local candidate passed every local gate."
    )
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "ok": all(run.get("ok") is True for run in all_runs),
        "mode": "read_only_local_extraction_kernel_ab",
        "environment_label": environment_label,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "page_cache_controlled": False,
            "candidate_order_rotated": True,
            "caveat": "Fresh destinations are used, but OS page cache is not cleared; values are not Vercel absolute cold latency.",
        },
        "archive_bytes": archive_bytes,
        "uncompressed_sqlite_bytes": sqlite_bytes,
        "run_count_per_variant": runs,
        "warmup": warmup_result,
        "source_artifacts_before": source_before,
        "source_artifacts_after": source_after,
        "source_artifacts_unchanged": source_before == source_after,
        "variants": summaries,
        "rss_measurement_methods": sorted(
            {
                method
                for summary in summaries
                for method in summary["rss_measurement_methods"]
            }
        ),
        "recommendation": recommendation,
        "promotion_policy": {
            "safe_candidate_must_preserve_streaming_sha256": True,
            "safe_candidate_must_preserve_fsync": True,
            "hash_omission_default_promotion_prohibited": True,
            "fsync_omission_requires_explicit_provider_durability_decision": True,
            "remote_measurement_status": REMOTE_STATUS,
            "promotion_allowed": False,
            "promotion_blocker": "fresh Vercel instance p50/p95/CV/RSS comparison is absent",
        },
        "safety": {
            "source_artifacts_read_only": True,
            "temporary_destination_only": True,
            "temporary_directories_removed": all(
                run.get("temporary_directory_removed") is True for run in all_runs
            ),
            "runtime_files_modified": False,
            "database_writes": False,
            "status_updates": False,
            "human_review_claim": False,
            "remote_calls": False,
            "deployment_changes": False,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    if not report.get("ok") and "variants" not in report:
        return (
            "# Vercel Cold Optimization Experiments\n\n"
            f"- Status: `failed`\n- Error: `{report.get('error')}`\n"
            "- Remote measurement: `not_measured`\n"
        )
    lines = [
        "# Vercel Cold Optimization Experiments",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Environment: `{report['environment_label']}`",
        f"- Runs per variant: `{report['run_count_per_variant']}`",
        f"- Archive / SQLite bytes: `{report['archive_bytes']}` / `{report['uncompressed_sqlite_bytes']}`",
        f"- Source artifacts unchanged: `{report['source_artifacts_unchanged']}`",
        "- Remote measurement: `not_measured`",
        "- Promotion allowed: `false`",
        "",
        "## Comparison",
        "",
        "| Variant | Class | p50 ms | p95 ms | CV | p50 improvement | Peak RSS bytes | Integrity | Fsync | Local gate |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- | --- |",
    ]
    for item in report["variants"]:
        variant = item["variant"]
        delta = item["delta_vs_baseline"]
        lines.append(
            f"| `{variant['variant_id']}` | `{variant['candidate_class']}` | "
            f"{item['total_ms']['p50']} | {item['total_ms']['p95']} | "
            f"{item['total_ms']['coefficient_of_variation']} | "
            f"{delta['p50_improvement_percent']}% | {item['peak_rss_bytes_max']} | "
            f"`{variant['integrity_guarantee_level']}` | "
            f"{variant['fsync_extracted_file']} | {item['promotion_gate']['local_gate_pass']} |"
        )
    lines.extend(
        [
            "",
            "## Safety Interpretation",
            "",
            "- Hash omission is a diagnostic upper bound only and is prohibited from default promotion.",
            "- Fsync omission retains the streaming hash but weakens the explicit durability barrier; promotion needs an explicit provider/runtime safety decision.",
            "- Only the 4 MiB readinto candidate preserves both manifest-bound SHA-256 and fsync.",
            "- All variants use isolated temporary destinations; the production ZIP, DB, manifest, runtime, and deployment settings are unchanged.",
            "",
            "## Recommendation",
            "",
            report["recommendation"],
            "",
            "Even a local winner remains blocked until at least three proven-fresh Vercel instances provide p50, p95, CV, peak RSS, and stage contribution evidence.",
            "",
            "## Environment Caveat",
            "",
            f"- {report['environment']['caveat']}",
        ]
    )
    return "\n".join(lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="A/B compact snapshot extraction candidates read-only."
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
    compact = {
        "ok": report.get("ok"),
        "recommendation": report.get("recommendation"),
        "variants": [
            {
                "variant_id": item["variant"]["variant_id"],
                "p50": item["total_ms"]["p50"],
                "p95": item["total_ms"]["p95"],
                "cv": item["total_ms"]["coefficient_of_variation"],
                "p50_improvement_percent": item["delta_vs_baseline"]["p50_improvement_percent"],
                "local_gate_pass": item["promotion_gate"]["local_gate_pass"],
            }
            for item in report.get("variants", [])
        ],
        "remote_measurement_status": REMOTE_STATUS,
        "promotion_allowed": False,
    }
    print(json.dumps(compact, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
