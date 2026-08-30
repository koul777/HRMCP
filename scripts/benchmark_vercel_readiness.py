from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

import psutil


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = PROJECT_ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
DEFAULT_MANIFEST = PROJECT_ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.manifest.json"
DEFAULT_CONFIG = PROJECT_ROOT / "deploy" / "vercel_mcp_app" / "vercel.json"
DEFAULT_JSON_OUT = PROJECT_ROOT / "reports" / "vercel_readiness_benchmark_20260830.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "vercel_readiness_benchmark_20260830.md"
CANDIDATES = ("A_count", "B_manifest", "C_process_cache", "D_stat_only")
_PROCESS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def latency_summary(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {"count": 0, "min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0, "mean": 0.0, "population_stdev": 0.0, "coefficient_of_variation": 0.0}
    ordered = sorted(float(value) for value in values)

    def percentile(fraction: float) -> float:
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
        return ordered[index]

    mean = statistics.fmean(ordered)
    stdev = statistics.pstdev(ordered)
    return {
        "count": len(ordered),
        "min": round(ordered[0], 3),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(ordered[-1], 3),
        "mean": round(mean, 3),
        "population_stdev": round(stdev, 3),
        "coefficient_of_variation": round(stdev / mean if mean else 0.0, 6),
    }


def _rss_mb() -> float:
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _peak_rss_mb() -> float:
    info = psutil.Process().memory_info()
    return float(getattr(info, "peak_wset", info.rss)) / (1024 * 1024)


def load_vercel_environment(config_path: Path) -> dict[str, str]:
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    return {str(key): str(value) for key, value in dict(payload.get("env") or {}).items()}


@contextlib.contextmanager
def applied_environment(values: Mapping[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    try:
        os.environ.update(values)
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _runtime_readiness():
    source = str(PROJECT_ROOT / "src")
    if source not in sys.path:
        sys.path.insert(0, source)
    from ncs_mcp import runtime_readiness

    return runtime_readiness


def _database_fingerprint(db_path: Path, manifest: Mapping[str, Any] | None = None) -> tuple[Any, ...]:
    stat = db_path.stat()
    return (
        str(db_path.resolve()),
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ino", 0),
        str((manifest or {}).get("sqlite_sha256") or ""),
    )


def _read_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("sqlite_bytes", "sqlite_sha256", "physical_counts", "servable_counts"):
        if field not in payload:
            raise ValueError(f"manifest missing {field}")
    return payload


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@contextlib.contextmanager
def prepare_verified_snapshot(archive_path: Path, manifest_path: Path, temp_root: Path | None = None) -> Iterator[tuple[Path, dict[str, Any], Path]]:
    manifest = _read_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix="ncs-readiness-", dir=temp_root) as raw_dir:
        workspace = Path(raw_dir)
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != manifest.get("archive_member"):
                raise ValueError("compact archive member contract mismatch")
            if members[0].file_size != int(manifest["sqlite_bytes"]):
                raise ValueError("compact archive size contract mismatch")
            db_path = workspace / Path(members[0].filename).name
            digest = hashlib.sha256()
            with archive.open(members[0], "r") as source, db_path.open("wb") as target:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
                    target.write(block)
        if digest.hexdigest() != str(manifest["sqlite_sha256"]):
            raise ValueError("compact snapshot sha256 mismatch")
        yield db_path, manifest, workspace


def _embedded_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {"physical": {}, "logical": {}, "servable": {}}
    rows = conn.execute(
        "SELECT object_name, row_count, count_kind FROM serving_snapshot_table_counts"
    ).fetchall()
    for raw_name, raw_count, raw_kind in rows:
        name, kind, count = str(raw_name), str(raw_kind), int(raw_count)
        if kind not in result or count < 0 or name in result[kind]:
            raise ValueError("invalid embedded count metadata")
        result[kind][name] = count
    return result


def _manifest_counts(manifest: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    return {
        kind: {str(key): int(value) for key, value in dict(manifest.get(f"{kind}_counts") or {}).items()}
        for kind in ("physical", "logical", "servable")
    }


def _selected_count(counts: Mapping[str, Mapping[str, int]], table_name: str) -> int | None:
    for kind in ("servable", "physical", "logical"):
        if table_name in counts.get(kind, {}):
            return int(counts[kind][table_name])
    return None


def _actual_runtime(db_path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    runtime_readiness = _runtime_readiness()
    effective = dict(env)
    effective.update({"NCS_DB_PATH": str(db_path), "NCS_MCP_READ_ONLY": "1"})
    with applied_environment(effective):
        # A is the forced SQL baseline even after the product fast path has
        # been configured in this process.
        runtime_readiness.clear_verified_readiness_counts()
        runtime = runtime_readiness.runtime_health_metadata()
    database = dict(runtime["database"])
    database["candidate_mode"] = "current_runtime_health_metadata"
    database["cache_hit"] = False
    database["used_scan_fallback"] = False
    return database


def _metadata_candidate(
    db_path: Path,
    manifest_path: Path,
    env: Mapping[str, str],
    *,
    source_kind: str,
    trusted_compact: bool,
) -> dict[str, Any]:
    if source_kind != "bundled_compact" or not trusted_compact:
        fallback = _actual_runtime(db_path, env)
        fallback.update(
            {
                "candidate_mode": "manifest_rejected_scan_fallback",
                "used_scan_fallback": True,
                "fallback_reason": "untrusted_or_nonbundled_source",
            }
        )
        return fallback

    runtime_readiness = _runtime_readiness()
    manifest = _read_manifest(manifest_path)
    if db_path.stat().st_size != int(manifest["sqlite_bytes"]):
        fallback = _actual_runtime(db_path, env)
        fallback.update({"candidate_mode": "manifest_rejected_scan_fallback", "used_scan_fallback": True, "fallback_reason": "file_size_mismatch"})
        return fallback

    effective = dict(env)
    effective.update({"NCS_DB_PATH": str(db_path), "NCS_MCP_READ_ONLY": "1"})
    with applied_environment(effective):
        required_tables, invalid_extra_tables = runtime_readiness._readiness_required_tables()
    manifest_counts = _manifest_counts(manifest)
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        objects = {
            str(name): str(kind)
            for name, kind in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        try:
            embedded = _embedded_counts(conn)
        except (sqlite3.DatabaseError, ValueError):
            embedded = {}
        if embedded != manifest_counts:
            fallback = _actual_runtime(db_path, env)
            fallback.update({"candidate_mode": "manifest_rejected_scan_fallback", "used_scan_fallback": True, "fallback_reason": "metadata_manifest_mismatch"})
            return fallback

        core_tables: dict[str, dict[str, Any]] = {}
        for table_name in required_tables:
            if table_name not in objects:
                core_tables[table_name] = {"exists": False, "row_count": None}
                continue
            row_count = _selected_count(manifest_counts, table_name)
            if row_count is None:
                row_count = int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
            core_tables[table_name] = {"exists": True, "row_count": row_count}

        public_tables: dict[str, dict[str, bool]] = {}
        for table_name in runtime_readiness.READINESS_PUBLIC_TOOL_TABLES:
            if table_name not in objects:
                public_tables[table_name] = {"exists": False, "has_rows": False}
                continue
            row_count = _selected_count(manifest_counts, table_name)
            has_rows = bool(row_count) if row_count is not None else conn.execute(
                f'SELECT 1 FROM "{table_name}" LIMIT 1'
            ).fetchone() is not None
            public_tables[table_name] = {"exists": True, "has_rows": has_rows}

    core_ready = len(core_tables) == len(required_tables) and all(
        state["exists"] and int(state["row_count"] or 0) > 0 for state in core_tables.values()
    )
    public_ready = all(state["exists"] and state["has_rows"] for state in public_tables.values())
    result: dict[str, Any] = {
        "configured": True,
        "exists": True,
        "openable": True,
        "ready": core_ready,
        "public_tools_ready": public_ready,
        "required_tables": list(required_tables),
        "core_tables": core_tables,
        "public_tool_tables": public_tables,
        "candidate_mode": "content_bound_manifest_counts",
        "cache_hit": False,
        "used_scan_fallback": False,
    }
    if invalid_extra_tables:
        result["invalid_extra_tables"] = invalid_extra_tables
    return result


def _cached_candidate(
    db_path: Path,
    manifest_path: Path,
    env: Mapping[str, str],
    *,
    source_kind: str,
    trusted_compact: bool,
) -> dict[str, Any]:
    if source_kind != "bundled_compact" or not trusted_compact:
        fallback = _actual_runtime(db_path, env)
        fallback.update({"candidate_mode": "cache_disabled_scan_fallback", "used_scan_fallback": True, "fallback_reason": "mutable_or_external_source"})
        return fallback
    manifest = _read_manifest(manifest_path)
    key = _database_fingerprint(db_path, manifest)
    cached = _PROCESS_CACHE.get(key)
    if cached is not None:
        result = copy.deepcopy(cached)
        result.update({"candidate_mode": "fingerprint_process_cache", "cache_hit": True, "used_scan_fallback": False})
        return result
    result = _actual_runtime(db_path, env)
    _PROCESS_CACHE.clear()
    _PROCESS_CACHE[key] = copy.deepcopy(result)
    result.update({"candidate_mode": "fingerprint_process_cache", "cache_hit": False, "used_scan_fallback": False})
    return result


def _stat_only_candidate(db_path: Path, env: Mapping[str, str]) -> dict[str, Any]:
    runtime_readiness = _runtime_readiness()
    effective = dict(env)
    effective.update({"NCS_DB_PATH": str(db_path), "NCS_MCP_READ_ONLY": "1"})
    with applied_environment(effective):
        required_tables, _invalid = runtime_readiness._readiness_required_tables()
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        objects = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
        has_stat1 = "sqlite_stat1" in objects
        estimates: dict[str, int] = {}
        if has_stat1:
            for table_name, raw_stat in conn.execute("SELECT tbl, stat FROM sqlite_stat1"):
                token = str(raw_stat).split(" ", 1)[0]
                if token.isdigit():
                    estimates[str(table_name)] = int(token)
        core_tables: dict[str, dict[str, Any]] = {}
        for table_name in required_tables:
            exists = table_name in objects
            has_rows = bool(exists and conn.execute(f'SELECT 1 FROM "{table_name}" LIMIT 1').fetchone())
            core_tables[table_name] = {
                "exists": exists,
                "has_rows": has_rows,
                "row_count": estimates.get(table_name),
                "count_is_estimate": table_name in estimates,
            }
    return {
        "configured": True,
        "exists": True,
        "openable": True,
        "ready": len(core_tables) == len(required_tables) and all(state["exists"] and state["has_rows"] for state in core_tables.values()),
        "public_tools_ready": None,
        "required_tables": list(required_tables),
        "core_tables": core_tables,
        "public_tool_tables": {},
        "candidate_mode": "sqlite_schema_and_limit_one",
        "cache_hit": False,
        "used_scan_fallback": False,
        "semantic_exact": False,
        "sqlite_stat1_present": has_stat1,
    }


def run_candidate(
    candidate: str,
    db_path: Path,
    manifest_path: Path,
    env: Mapping[str, str],
    *,
    source_kind: str = "bundled_compact",
    trusted_compact: bool = True,
) -> dict[str, Any]:
    if candidate == "A_count":
        return _actual_runtime(db_path, env)
    if candidate == "B_manifest":
        return _metadata_candidate(db_path, manifest_path, env, source_kind=source_kind, trusted_compact=trusted_compact)
    if candidate == "C_process_cache":
        return _cached_candidate(db_path, manifest_path, env, source_kind=source_kind, trusted_compact=trusted_compact)
    if candidate == "D_stat_only":
        return _stat_only_candidate(db_path, env)
    raise ValueError(f"unknown candidate: {candidate}")


def contract_signature(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ready": result.get("ready"),
        "public_tools_ready": result.get("public_tools_ready"),
        "required_tables": result.get("required_tables"),
        "core_tables": result.get("core_tables"),
        "public_tool_tables": result.get("public_tool_tables"),
    }


def audit_metadata_against_actual(db_path: Path, manifest: Mapping[str, Any], required_tables: list[str]) -> dict[str, Any]:
    counts = _manifest_counts(manifest)
    mismatches: list[dict[str, Any]] = []
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
        for table_name in required_tables:
            expected = _selected_count(counts, table_name)
            if expected is None:
                mismatches.append({"table": table_name, "expected": None, "actual": None, "reason": "metadata_missing"})
                continue
            actual = int(conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0])
            if actual != expected:
                mismatches.append({"table": table_name, "expected": expected, "actual": actual, "reason": "count_mismatch"})
    return {"ok": not mismatches, "checked_table_count": len(required_tables), "mismatches": mismatches}


def _worker_payload(args: argparse.Namespace) -> dict[str, Any]:
    env = load_vercel_environment(args.config)
    start_rss = _rss_mb()
    started = time.perf_counter()
    result = run_candidate(args.candidate, args.db, args.manifest, env, source_kind=args.source_kind, trusted_compact=args.trusted_compact)
    elapsed_ms = (time.perf_counter() - started) * 1000
    end_rss = _rss_mb()
    return {
        "candidate": args.candidate,
        "operation_elapsed_ms": round(elapsed_ms, 3),
        "rss_start_mb": round(start_rss, 3),
        "rss_end_mb": round(end_rss, 3),
        "rss_delta_mb": round(end_rss - start_rss, 3),
        "rss_peak_mb": round(_peak_rss_mb(), 3),
        "result": result,
    }


def _cold_runs(candidate: str, db_path: Path, manifest_path: Path, config_path: Path, runs: int) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for _index in range(runs):
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--candidate",
            candidate,
            "--db",
            str(db_path),
            "--manifest",
            str(manifest_path),
            "--config",
            str(config_path),
            "--source-kind",
            "bundled_compact",
            "--trusted-compact",
        ]
        started = time.perf_counter()
        completed = subprocess.run(command, check=True, capture_output=True, text=True, cwd=PROJECT_ROOT)
        process_wall_ms = (time.perf_counter() - started) * 1000
        payload = json.loads(completed.stdout.strip().splitlines()[-1])
        payload["process_wall_ms"] = round(process_wall_ms, 3)
        results.append(payload)
    return results


def _warm_runs(candidate: str, db_path: Path, manifest_path: Path, env: Mapping[str, str], runs: int) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    prime: dict[str, Any] | None = None
    if candidate == "C_process_cache":
        _PROCESS_CACHE.clear()
        started = time.perf_counter()
        prime_result = run_candidate(candidate, db_path, manifest_path, env)
        prime = {"elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "cache_hit": prime_result.get("cache_hit", False)}
    results: list[dict[str, Any]] = []
    for _index in range(runs):
        before = _rss_mb()
        started = time.perf_counter()
        result = run_candidate(candidate, db_path, manifest_path, env)
        elapsed_ms = (time.perf_counter() - started) * 1000
        after = _rss_mb()
        results.append(
            {
                "operation_elapsed_ms": round(elapsed_ms, 3),
                "rss_start_mb": round(before, 3),
                "rss_end_mb": round(after, 3),
                "rss_delta_mb": round(after - before, 3),
                "rss_peak_mb": round(_peak_rss_mb(), 3),
                "result": result,
            }
        )
    return results, prime


def _phase_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "latency_ms": latency_summary([float(row["operation_elapsed_ms"]) for row in rows]),
        "process_wall_ms": latency_summary([float(row["process_wall_ms"]) for row in rows]) if rows and "process_wall_ms" in rows[0] else None,
        "rss_end_mb": latency_summary([float(row["rss_end_mb"]) for row in rows]),
        "rss_delta_mb": latency_summary([float(row["rss_delta_mb"]) for row in rows]),
        "rss_peak_mb": latency_summary([float(row["rss_peak_mb"]) for row in rows]),
        "cache_hit_count": sum(bool(row["result"].get("cache_hit")) for row in rows),
        "scan_fallback_count": sum(bool(row["result"].get("used_scan_fallback")) for row in rows),
    }


def build_report(archive_path: Path, manifest_path: Path, config_path: Path, *, runs: int = 5, temp_root: Path | None = None) -> dict[str, Any]:
    if runs < 5:
        raise ValueError("runs must be at least 5")
    source_before = {
        str(path): {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in (archive_path, manifest_path, config_path)
    }
    extracted_workspace: Path | None = None
    with prepare_verified_snapshot(archive_path, manifest_path, temp_root) as (db_path, manifest, workspace):
        extracted_workspace = workspace
        env = load_vercel_environment(config_path)
        baseline = run_candidate("A_count", db_path, manifest_path, env)
        required_tables = list(baseline["required_tables"])
        metadata_audit = audit_metadata_against_actual(db_path, manifest, required_tables)
        experiments: dict[str, Any] = {}
        signatures: dict[str, dict[str, Any]] = {"A_count": contract_signature(baseline)}
        for candidate in CANDIDATES:
            cold = _cold_runs(candidate, db_path, manifest_path, config_path, runs)
            warm, prime = _warm_runs(candidate, db_path, manifest_path, env, runs)
            signatures[candidate] = contract_signature(warm[-1]["result"])
            experiments[candidate] = {
                "cold": _phase_summary(cold),
                "warm": _phase_summary(warm),
                "warm_prime": prime,
                "cold_runs": cold,
                "warm_runs": warm,
            }

        baseline_warm = float(experiments["A_count"]["warm"]["latency_ms"]["p50"])
        for candidate, data in experiments.items():
            candidate_warm = float(data["warm"]["latency_ms"]["p50"])
            data["warm_p50_improvement_percent_vs_A"] = round(
                ((baseline_warm - candidate_warm) / baseline_warm * 100) if baseline_warm else 0.0,
                3,
            )
            data["contract_signature_matches_A"] = signatures[candidate] == signatures["A_count"]

        explicit_override_b = run_candidate("B_manifest", db_path, manifest_path, env, source_kind="explicit_override", trusted_compact=False)
        explicit_override_c = run_candidate("C_process_cache", db_path, manifest_path, env, source_kind="full_database", trusted_compact=False)
        b = experiments["B_manifest"]
        b_safe = bool(metadata_audit["ok"] and b["contract_signature_matches_A"] and b["warm"]["scan_fallback_count"] == 0)
        b_fast = float(b["warm_p50_improvement_percent_vs_A"]) >= 25.0
        recommendation = {
            "promote": "B_manifest" if b_safe and b_fast else None,
            "scope": "bundled compact snapshot only" if b_safe and b_fast else "none",
            "safe": b_safe,
            "time_saving_clear": b_fast,
            "requirements": [
                "Trust counts only after archive SHA, embedded metadata, and external manifest validation.",
                "Preserve SELECT COUNT(*) fallback for explicit overrides, full databases, missing metadata, and mismatches.",
                "Invalidate any process result cache on verified compact fingerprint change.",
                "Do not use sqlite_stat1/stat-only estimates where exact row_count or configured minimum rows are contractual.",
            ],
            "candidate_C": "not preferred; B removes the expensive scans without retaining mutable cache state",
            "candidate_D": "reject; nonempty/estimated metadata does not preserve exact row_count semantics",
        }
        report = {
            "schema": "ncs_vercel_readiness_benchmark_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "ok": metadata_audit["ok"] and len(required_tables) >= 20,
            "environment": {
                "label": "local Windows; compact DB extracted once; cold=fresh Python process/connection with OS page cache uncontrolled",
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "run_count_per_phase": runs,
            },
            "source": {
                "archive": str(archive_path),
                "manifest": str(manifest_path),
                "config": str(config_path),
                "sqlite_bytes": int(manifest["sqlite_bytes"]),
                "sqlite_sha256": str(manifest["sqlite_sha256"]),
                "required_table_count": len(required_tables),
                "required_tables": required_tables,
                "runtime_function": "ncs_mcp.runtime_readiness.runtime_health_metadata",
            },
            "metadata_actual_count_audit": metadata_audit,
            "experiments": experiments,
            "contract_signatures": signatures,
            "fallback_evidence": {
                "explicit_override_B": {
                    "candidate_mode": explicit_override_b.get("candidate_mode"),
                    "used_scan_fallback": explicit_override_b.get("used_scan_fallback"),
                    "signature_matches_A": contract_signature(explicit_override_b) == signatures["A_count"],
                },
                "full_database_C": {
                    "candidate_mode": explicit_override_c.get("candidate_mode"),
                    "used_scan_fallback": explicit_override_c.get("used_scan_fallback"),
                    "signature_matches_A": contract_signature(explicit_override_c) == signatures["A_count"],
                },
            },
            "promotion_recommendation": recommendation,
            "safety": {
                "source_artifacts_unchanged": False,
                "temporary_directory_removed": False,
                "database_writes": False,
                "raw_ksa_modified": False,
                "human_review_status_written": False,
            },
        }
    report["safety"]["temporary_directory_removed"] = bool(extracted_workspace is not None and not extracted_workspace.exists())
    source_after = {
        str(path): {"size": path.stat().st_size, "mtime_ns": path.stat().st_mtime_ns}
        for path in (archive_path, manifest_path, config_path)
    }
    report["safety"]["source_artifacts_unchanged"] = source_before == source_after
    report["ok"] = bool(report["ok"] and report["safety"]["temporary_directory_removed"] and report["safety"]["source_artifacts_unchanged"])
    return report


def render_markdown(report: Mapping[str, Any]) -> str:
    recommendation = report["promotion_recommendation"]
    lines = [
        "# Vercel Readiness COUNT Benchmark",
        "",
        f"- Verdict: `{'PASS' if report['ok'] else 'FAIL'}`",
        f"- Required tables: `{report['source']['required_table_count']}`",
        f"- Runs per cold/warm phase: `{report['environment']['run_count_per_phase']}`",
        f"- Promotion recommendation: `{recommendation['promote'] or 'none'}` ({recommendation['scope']})",
        f"- Measurement boundary: {report['environment']['label']}",
        "",
        "## Candidate Results",
        "",
        "| Candidate | Cold p50 / p95 ms | Cold CV | Warm p50 / p95 ms | Warm CV | Warm delta vs A | Contract exact | RSS peak p95 MB |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
    ]
    for candidate in CANDIDATES:
        data = report["experiments"][candidate]
        cold = data["cold"]["latency_ms"]
        warm = data["warm"]["latency_ms"]
        peak = data["cold"]["rss_peak_mb"]
        lines.append(
            f"| {candidate} | {cold['p50']:.3f} / {cold['p95']:.3f} | {cold['coefficient_of_variation']:.4f} | "
            f"{warm['p50']:.3f} / {warm['p95']:.3f} | {warm['coefficient_of_variation']:.4f} | "
            f"{data['warm_p50_improvement_percent_vs_A']:.2f}% | {data['contract_signature_matches_A']} | {peak['p95']:.3f} |"
        )
    lines.extend(
        [
            "",
            "## Safety and Semantics",
            "",
            f"- Embedded/manifest counts vs actual COUNT audit: `{report['metadata_actual_count_audit']['ok']}` across `{report['metadata_actual_count_audit']['checked_table_count']}` required tables.",
            f"- Explicit override B scan fallback: `{report['fallback_evidence']['explicit_override_B']['used_scan_fallback']}`.",
            f"- Full DB C cache disabled/scan fallback: `{report['fallback_evidence']['full_database_C']['used_scan_fallback']}`.",
            f"- Source artifacts unchanged: `{report['safety']['source_artifacts_unchanged']}`.",
            f"- Temporary extraction removed: `{report['safety']['temporary_directory_removed']}`.",
            "- Candidate D can preserve existence/nonempty readiness only; it cannot preserve exact row counts or minimum-row contracts because sqlite_stat1 is optional and approximate.",
            "- Candidate C is safe only for the immutable, verified compact path; mutable/full/override DB paths must bypass the cache.",
            "",
            "## Decision",
            "",
            f"- Safe: `{recommendation['safe']}`",
            f"- Time saving clear (warm p50 >=25%): `{recommendation['time_saving_clear']}`",
            f"- Promote: `{recommendation['promote'] or 'none'}`",
            f"- Candidate C: {recommendation['candidate_C']}.",
            f"- Candidate D: {recommendation['candidate_D']}.",
            "",
            "## Required Guardrails",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in recommendation["requirements"])
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark Vercel readiness COUNT strategies")
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--json-out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--candidate", choices=CANDIDATES)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--source-kind", default="bundled_compact")
    parser.add_argument("--trusted-compact", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.worker:
        if not args.candidate or not args.db:
            raise SystemExit("--worker requires --candidate and --db")
        print(json.dumps(_worker_payload(args), ensure_ascii=False))
        return 0
    report = build_report(args.archive, args.manifest, args.config, runs=args.runs)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "json_out": str(args.json_out), "markdown_out": str(args.markdown_out), "promotion": report["promotion_recommendation"]["promote"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
