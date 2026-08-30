"""Measure first-request and warm latency on isolated Vercel previews.

The first HTTP request to each preview is deliberately ``/api/ready``.  A
deployment is inspected through the Vercel control plane before that request,
which does not intentionally invoke the deployed function.  The resulting
measurement is a *fresh deployment first-request proxy*, not proof that the
platform did not prewarm an instance internally.

Only allowlisted readiness fields, hashes, timings, status codes, and bounded
error summaries are persisted.  Raw response bodies, response headers, CLI
output, environment values, and credentials are never written to the report.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


SCHEMA = "ncs_vercel_fresh_instance_measurements_v1"
COLD_CLAIM = "fresh_deployment_first_request"
MAX_DEPLOYMENTS = 3
DEFAULT_MAX_DURATION_SECONDS = 30
MAX_RESPONSE_BYTES = 1_000_000
REPO_ROOT = Path(__file__).resolve().parents[1]
URL_PATTERN = re.compile(r"https://[a-zA-Z0-9][a-zA-Z0-9.-]*\.vercel\.app")
SENSITIVE_PATTERN = re.compile(
    r"(authorization|bearer|cookie|password|secret|token|api[_-]?key)", re.IGNORECASE
)
WINDOWS_HOME_PATTERN = re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+", re.IGNORECASE)
UNIX_HOME_PATTERN = re.compile(r"/(?:home|Users)/[^/\s]+")


def _default_deploy_root() -> Path:
    for candidate in (
        REPO_ROOT,
        REPO_ROOT / "deploy" / "vercel_mcp_app",
    ):
        if (candidate / ".vercel" / "project.json").is_file() and (
            candidate / "api" / "ncs_ontology_compact.zip"
        ).is_file():
            return candidate
    return REPO_ROOT


DEFAULT_DEPLOY_ROOT = _default_deploy_root()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _round(value: float) -> float:
    return round(float(value), 3)


def percentile(values: Sequence[float], fraction: float) -> float | None:
    """Return a linearly interpolated percentile for a non-empty sequence."""

    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return _round(ordered[0])
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return _round(ordered[lower])
    weight = position - lower
    return _round(ordered[lower] + (ordered[upper] - ordered[lower]) * weight)


def latency_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    samples = [float(value) for value in values]
    if not samples:
        return {
            "sample_count": 0,
            "min_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "max_ms": None,
            "mean_ms": None,
            "stdev_ms": None,
            "coefficient_of_variation": None,
        }
    mean = statistics.fmean(samples)
    stdev = statistics.pstdev(samples)
    return {
        "sample_count": len(samples),
        "min_ms": _round(min(samples)),
        "p50_ms": percentile(samples, 0.50),
        "p95_ms": percentile(samples, 0.95),
        "max_ms": _round(max(samples)),
        "mean_ms": _round(mean),
        "stdev_ms": _round(stdev),
        "coefficient_of_variation": round(stdev / mean, 6) if mean else None,
    }


def safe_error_text(value: object, *, limit: int = 500) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ")
    text = WINDOWS_HOME_PATTERN.sub(r"C:\\Users\\<redacted>", text)
    text = UNIX_HOME_PATTERN.sub("/home/<redacted>", text)
    text = re.sub(
        r"(?i)(authorization|bearer|cookie|password|secret|token|api[_-]?key)\s*[:=]\s*\S+",
        r"\1=<redacted>",
        text,
    )
    return text[:limit]


def _numeric_mapping(value: object) -> dict[str, float]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _round(float(item))
        for key, item in value.items()
        if isinstance(item, (int, float)) and not isinstance(item, bool)
    }


def _safe_local_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "archive_bytes",
        "sqlite_bytes",
        "compressed_bytes",
        "extracted_bytes",
        "cache_hit",
        "verified_stamp_hit",
        "readiness_fast_path_configured",
    }
    result: dict[str, object] = {}
    for key in allowed:
        item = value.get(key)
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, (int, float)):
            result[key] = item
    return result


def _extract_rss_metrics(value: object, prefix: str = "") -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child = f"{prefix}.{key_text}" if prefix else key_text
            if "rss" in key_text.lower() and isinstance(item, (int, float)):
                found.append({"metric_path": child, "value": item})
            elif not SENSITIVE_PATTERN.search(key_text):
                found.extend(_extract_rss_metrics(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value[:20]):
            found.extend(_extract_rss_metrics(item, f"{prefix}[{index}]"))
    return found


def redact_ready_payload(payload: object) -> dict[str, object]:
    """Return the minimal safe subset of a readiness/health JSON response."""

    if not isinstance(payload, Mapping):
        return {"json_object": False}
    result: dict[str, object] = {"json_object": True}
    for key in ("status", "name"):
        if isinstance(payload.get(key), str):
            result[key] = payload[key]

    bootstrap = payload.get("bootstrap")
    if isinstance(bootstrap, Mapping):
        safe_bootstrap: dict[str, object] = {}
        for key in (
            "schema",
            "status",
            "source",
            "ready",
            "elapsed_ms",
            "process_level_metrics",
            "request_level_metrics",
            "read_only_configuration",
            "readiness_fast_path_configured",
        ):
            item = bootstrap.get(key)
            if isinstance(item, (str, bool, int, float)) or item is None:
                safe_bootstrap[key] = item
        safe_bootstrap["stages_ms"] = _numeric_mapping(bootstrap.get("stages_ms"))
        local_snapshot = _safe_local_snapshot(bootstrap.get("local_snapshot"))
        if local_snapshot:
            safe_bootstrap["local_snapshot"] = local_snapshot
        result["bootstrap"] = safe_bootstrap

    runtime = payload.get("runtime")
    if isinstance(runtime, Mapping):
        safe_runtime: dict[str, object] = {}
        database = runtime.get("database")
        if isinstance(database, Mapping):
            safe_database: dict[str, object] = {}
            for key in (
                "ready",
                "public_tools_ready",
                "read_only",
                "readiness_count_source",
            ):
                item = database.get(key)
                if isinstance(item, (str, bool, int, float)) or item is None:
                    safe_database[key] = item
            safe_runtime["database"] = safe_database
        rss = _extract_rss_metrics(runtime, "runtime")
        if rss:
            safe_runtime["rss_metrics"] = rss
        result["runtime"] = safe_runtime
    return result


def parse_json_output(text: str) -> dict[str, object]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("command did not return a JSON object")
    value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("command JSON is not an object")
    return value


def _walk_find(mapping: object, keys: set[str]) -> object | None:
    if isinstance(mapping, Mapping):
        for key, value in mapping.items():
            if str(key).lower() in keys and value not in (None, ""):
                return value
        for value in mapping.values():
            found = _walk_find(value, keys)
            if found is not None:
                return found
    elif isinstance(mapping, list):
        for value in mapping:
            found = _walk_find(value, keys)
            if found is not None:
                return found
    return None


def sanitize_inspect_payload(payload: Mapping[str, object]) -> dict[str, object]:
    deployment_id = _walk_find(payload, {"id", "deploymentid", "deployment_id"})
    state = _walk_find(payload, {"state", "readystate", "ready_state"})
    target = _walk_find(payload, {"target"})
    created_at = _walk_find(payload, {"createdat", "created_at"})
    result: dict[str, object] = {}
    if isinstance(deployment_id, str):
        result["deployment_id"] = deployment_id
    if isinstance(state, str):
        result["ready_state"] = state
    if isinstance(target, str):
        result["target"] = target
    if isinstance(created_at, (str, int, float)):
        result["created_at"] = created_at
    return result


def _command(
    args: Sequence[str], *, cwd: Path, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )


def deploy_preview(
    *, deploy_root: Path, vercel_command: str, timeout_seconds: float
) -> str:
    result = _command(
        [vercel_command, "--yes", "--force", "--no-color"],
        cwd=deploy_root,
        timeout_seconds=timeout_seconds,
    )
    combined = f"{result.stdout}\n{result.stderr}"
    urls = URL_PATTERN.findall(combined)
    if result.returncode != 0 or not urls:
        raise RuntimeError(
            f"preview deployment failed (exit={result.returncode}): "
            f"{safe_error_text(combined[-1000:])}"
        )
    return urls[-1].rstrip("/")


def inspect_deployment(
    *,
    deployment_url: str,
    deploy_root: Path,
    vercel_command: str,
    timeout_seconds: float,
) -> dict[str, object]:
    result = _command(
        [
            vercel_command,
            "inspect",
            deployment_url,
            "--wait",
            "--timeout",
            f"{int(timeout_seconds)}s",
            "--json",
            "--no-color",
        ],
        cwd=deploy_root,
        timeout_seconds=timeout_seconds + 30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"deployment inspection failed (exit={result.returncode}): "
            f"{safe_error_text((result.stdout + result.stderr)[-1000:])}"
        )
    payload = parse_json_output(result.stdout)
    safe = sanitize_inspect_payload(payload)
    if str(safe.get("ready_state", "")).upper() != "READY":
        raise RuntimeError(
            f"deployment did not reach READY: {safe_error_text(safe.get('ready_state'))}"
        )
    return safe


def http_probe(
    url: str,
    *,
    timeout_seconds: float,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "User-Agent": "ncs-mcp-fresh-instance-audit/1",
        },
    )
    started = time.perf_counter()
    body = b""
    status: int | None = None
    content_type: str | None = None
    error_kind: str | None = None
    error_message: str | None = None
    try:
        with opener(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            content_type = response.headers.get_content_type()
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        content_type = exc.headers.get_content_type() if exc.headers else None
        body = exc.read(MAX_RESPONSE_BYTES + 1)
        error_kind = "http_error"
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        error_kind = type(exc).__name__
        error_message = safe_error_text(exc)
    elapsed_ms = _round((time.perf_counter() - started) * 1000)
    truncated = len(body) > MAX_RESPONSE_BYTES
    if truncated:
        body = body[:MAX_RESPONSE_BYTES]
    parsed: object = None
    if body and content_type == "application/json":
        try:
            parsed = json.loads(body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            parsed = None
    result: dict[str, object] = {
        "status": status,
        "elapsed_ms": elapsed_ms,
        "content_type": content_type,
        "response_bytes_observed": len(body),
        "response_truncated": truncated,
        "response_body_sha256": hashlib.sha256(body).hexdigest() if body else None,
        "error_kind": error_kind,
    }
    if error_message:
        result["error_message"] = error_message
    if parsed is not None:
        result["safe_payload"] = redact_ready_payload(parsed)
    return result


def _probe_series(
    *,
    url: str,
    count: int,
    expected_status: int,
    timeout_seconds: float,
    delay_seconds: float,
) -> dict[str, object]:
    samples: list[dict[str, object]] = []
    for index in range(count):
        if index:
            time.sleep(delay_seconds)
        sample = http_probe(url, timeout_seconds=timeout_seconds)
        sample.pop("safe_payload", None)
        samples.append(sample)
    return {
        "expected_status": expected_status,
        "samples": samples,
        "latency": latency_summary(
            sample["elapsed_ms"]
            for sample in samples
            if isinstance(sample.get("elapsed_ms"), (int, float))
        ),
        "statuses": [sample.get("status") for sample in samples],
        "contract_status_ok": all(sample.get("status") == expected_status for sample in samples),
    }


def measure_one(
    *,
    sequence: int,
    deployment_url: str,
    inspect_metadata: Mapping[str, object],
    bundle_bytes: int,
    timeout_seconds: float,
    delay_seconds: float,
    warm_ready_count: int,
    health_count: int,
) -> dict[str, object]:
    first = http_probe(
        f"{deployment_url}/api/ready", timeout_seconds=timeout_seconds
    )
    time.sleep(delay_seconds)
    warm_ready = _probe_series(
        url=f"{deployment_url}/api/ready",
        count=warm_ready_count,
        expected_status=200,
        timeout_seconds=timeout_seconds,
        delay_seconds=delay_seconds,
    )
    time.sleep(delay_seconds)
    health = _probe_series(
        url=f"{deployment_url}/api/health",
        count=health_count,
        expected_status=200,
        timeout_seconds=timeout_seconds,
        delay_seconds=delay_seconds,
    )
    time.sleep(delay_seconds)
    mcp_get = http_probe(
        f"{deployment_url}/api/mcp", timeout_seconds=timeout_seconds
    )
    mcp_get.pop("safe_payload", None)

    warm_p50 = warm_ready["latency"].get("p50_ms")
    first_elapsed = first.get("elapsed_ms")
    delta: float | None = None
    if isinstance(warm_p50, (int, float)) and isinstance(first_elapsed, (int, float)):
        delta = _round(float(first_elapsed) - float(warm_p50))

    return {
        "sequence": sequence,
        "deployment_url": deployment_url,
        "deployment": dict(inspect_metadata),
        "bundle_bytes": bundle_bytes,
        "bundle_bytes_source": "local_deploy_archive_stat",
        "first_request": {
            "endpoint": "/api/ready",
            "cold_claim": COLD_CLAIM,
            **first,
        },
        "warm_ready": warm_ready,
        "health": health,
        "mcp_get": {
            "expected_status": 405,
            **mcp_get,
            "contract_status_ok": mcp_get.get("status") == 405,
        },
        "first_vs_warm_ready_p50_delta_ms": delta,
    }


def _bootstrap_stage_summary(deployments: Sequence[Mapping[str, object]]) -> dict[str, object]:
    by_stage: dict[str, list[float]] = {}
    elapsed: list[float] = []
    rss: list[dict[str, object]] = []
    for deployment in deployments:
        first = deployment.get("first_request")
        if not isinstance(first, Mapping):
            continue
        safe_payload = first.get("safe_payload")
        if not isinstance(safe_payload, Mapping):
            continue
        bootstrap = safe_payload.get("bootstrap")
        if isinstance(bootstrap, Mapping):
            value = bootstrap.get("elapsed_ms")
            if isinstance(value, (int, float)):
                elapsed.append(float(value))
            stages = bootstrap.get("stages_ms")
            if isinstance(stages, Mapping):
                for name, stage_value in stages.items():
                    if isinstance(stage_value, (int, float)):
                        by_stage.setdefault(str(name), []).append(float(stage_value))
        runtime = safe_payload.get("runtime")
        if isinstance(runtime, Mapping) and isinstance(runtime.get("rss_metrics"), list):
            rss.extend(runtime["rss_metrics"])

    stage_stats = {
        name: latency_summary(values) for name, values in sorted(by_stage.items())
    }
    dominant_stage: dict[str, object] | None = None
    candidates = [
        (name, stats.get("p50_ms"))
        for name, stats in stage_stats.items()
        if isinstance(stats.get("p50_ms"), (int, float))
    ]
    if candidates:
        name, value = max(candidates, key=lambda item: float(item[1]))
        dominant_stage = {"name": name, "p50_ms": value}
    return {
        "bootstrap_elapsed_ms": latency_summary(elapsed),
        "stages_ms": stage_stats,
        "dominant_stage": dominant_stage,
        "rss_metrics": rss,
    }


def build_report(
    *,
    deployments: Sequence[Mapping[str, object]],
    source_commit: str | None,
    deploy_root: str,
    bundle_bytes: int,
    delay_seconds: float,
    timeout_seconds: float,
    max_duration_seconds: int = DEFAULT_MAX_DURATION_SECONDS,
    errors: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    first_latencies = [
        float(item["first_request"]["elapsed_ms"])
        for item in deployments
        if isinstance(item.get("first_request"), Mapping)
        and isinstance(item["first_request"].get("elapsed_ms"), (int, float))
    ]
    warm_latencies: list[float] = []
    deltas: list[float] = []
    for item in deployments:
        warm = item.get("warm_ready")
        if isinstance(warm, Mapping) and isinstance(warm.get("samples"), list):
            warm_latencies.extend(
                float(sample["elapsed_ms"])
                for sample in warm["samples"]
                if isinstance(sample, Mapping)
                and isinstance(sample.get("elapsed_ms"), (int, float))
            )
        delta = item.get("first_vs_warm_ready_p50_delta_ms")
        if isinstance(delta, (int, float)):
            deltas.append(float(delta))

    expected_contracts_ok = bool(deployments) and all(
        isinstance(item.get("first_request"), Mapping)
        and item["first_request"].get("status") == 200
        and isinstance(item.get("warm_ready"), Mapping)
        and item["warm_ready"].get("contract_status_ok") is True
        and isinstance(item.get("health"), Mapping)
        and item["health"].get("contract_status_ok") is True
        and isinstance(item.get("mcp_get"), Mapping)
        and item["mcp_get"].get("contract_status_ok") is True
        for item in deployments
    )
    max_first = max(first_latencies) if first_latencies else None
    margin_ms = (
        _round(max_duration_seconds * 1000 - max_first)
        if max_first is not None
        else None
    )
    bootstrap = _bootstrap_stage_summary(deployments)
    observed_margin_ok = bool(
        expected_contracts_ok and margin_ms is not None and margin_ms > 0
    )
    return {
        "schema": SCHEMA,
        "generated_at": utc_now(),
        "source": {
            "commit": source_commit,
            "deploy_root": deploy_root,
            "bundle_bytes": bundle_bytes,
            "bundle_bytes_source": "local_deploy_archive_stat",
        },
        "measurement_contract": {
            "preview_only": True,
            "production_alias_changed": False,
            "deployment_count_requested_max": MAX_DEPLOYMENTS,
            "deployment_count_observed": len(deployments),
            "first_http_request_endpoint": "/api/ready",
            "cold_claim": COLD_CLAIM,
            "cold_claim_is_platform_cold_proof": False,
            "platform_internal_prewarm_possible": True,
            "inspect_before_first_request": True,
            "raw_response_bodies_persisted": False,
            "raw_cli_output_persisted": False,
            "request_delay_seconds": delay_seconds,
            "request_timeout_seconds": timeout_seconds,
        },
        "summary": {
            "contract_ok": expected_contracts_ok,
            "first_request_latency": latency_summary(first_latencies),
            "warm_ready_latency": latency_summary(warm_latencies),
            "first_vs_warm_ready_p50_delta": latency_summary(deltas),
            "bootstrap": bootstrap,
            "max_duration_seconds": max_duration_seconds,
            "observed_max_first_request_ms": _round(max_first) if max_first is not None else None,
            "observed_max_duration_margin_ms": margin_ms,
            "observed_max_duration_margin_ok": observed_margin_ok,
            "max_duration_assessment": (
                "observed_safe_for_fresh_deployment_first_request_proxy"
                if observed_margin_ok
                else "not_proven_safe"
            ),
            "error_count": len(errors),
        },
        "deployments": list(deployments),
        "errors": list(errors),
        "limitations": [
            "A fresh deployment URL isolates deployments but does not prove Vercel did not internally prewarm the function.",
            "Bootstrap metrics are process-level; endpoint latency is request-level.",
            "The 30-second margin is observational for this sample and is not a platform availability guarantee.",
            "Bundle bytes are the local archive included in each function bundle, not total platform storage accounting.",
        ],
    }


def markdown_report(report: Mapping[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, Mapping)
    first = summary["first_request_latency"]
    warm = summary["warm_ready_latency"]
    assert isinstance(first, Mapping) and isinstance(warm, Mapping)
    bootstrap = summary.get("bootstrap")
    dominant = bootstrap.get("dominant_stage") if isinstance(bootstrap, Mapping) else None
    dominant_text = "not exposed"
    if isinstance(dominant, Mapping):
        dominant_text = f"{dominant.get('name')} ({dominant.get('p50_ms')} ms p50)"
    lines = [
        "# Vercel fresh deployment first-request measurements",
        "",
        f"- Generated: `{report.get('generated_at')}`",
        f"- Contract: `{COLD_CLAIM}` (platform cold proof: `false`)",
        f"- Preview deployments: `{report.get('measurement_contract', {}).get('deployment_count_observed')}`",
        f"- Contract OK: `{str(summary.get('contract_ok')).lower()}`",
        f"- First `/api/ready` p50 / p95: `{first.get('p50_ms')} / {first.get('p95_ms')} ms`",
        f"- Warm `/api/ready` p50 / p95: `{warm.get('p50_ms')} / {warm.get('p95_ms')} ms`",
        f"- First-request CV: `{first.get('coefficient_of_variation')}`",
        f"- Observed 30s margin: `{summary.get('observed_max_duration_margin_ms')} ms`",
        f"- Dominant bootstrap stage: `{dominant_text}`",
        "",
        "## Per deployment",
        "",
        "| # | Deployment ID | First ready ms | Warm ready p50 ms | Delta ms | Status |",
        "|---:|---|---:|---:|---:|---:|",
    ]
    for item in report.get("deployments", []):
        if not isinstance(item, Mapping):
            continue
        deployment = item.get("deployment")
        first_request = item.get("first_request")
        warm_ready = item.get("warm_ready")
        lines.append(
            "| {sequence} | {deployment_id} | {first_ms} | {warm_p50} | {delta} | {status} |".format(
                sequence=item.get("sequence"),
                deployment_id=(deployment or {}).get("deployment_id") if isinstance(deployment, Mapping) else None,
                first_ms=(first_request or {}).get("elapsed_ms") if isinstance(first_request, Mapping) else None,
                warm_p50=(warm_ready or {}).get("latency", {}).get("p50_ms") if isinstance(warm_ready, Mapping) else None,
                delta=item.get("first_vs_warm_ready_p50_delta_ms"),
                status=(first_request or {}).get("status") if isinstance(first_request, Mapping) else None,
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            f"- Max-duration assessment: `{summary.get('max_duration_assessment')}`.",
            "- Fresh preview isolation is a strong proxy, but Vercel may prewarm internally.",
            "- No production alias was changed and no raw response or CLI output was retained.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deploy-root", default=str(DEFAULT_DEPLOY_ROOT))
    parser.add_argument("--deployments", type=int, default=3)
    parser.add_argument("--warm-ready", type=int, default=5)
    parser.add_argument("--health", type=int, default=3)
    parser.add_argument("--delay-seconds", type=float, default=0.5)
    parser.add_argument("--request-timeout-seconds", type=float, default=35.0)
    parser.add_argument("--deploy-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--inspect-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--source-commit")
    parser.add_argument("--out", required=True)
    parser.add_argument("--markdown-out", required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.deployments <= MAX_DEPLOYMENTS:
        raise SystemExit(f"--deployments must be between 1 and {MAX_DEPLOYMENTS}")
    if args.warm_ready < 1 or args.health < 1:
        raise SystemExit("--warm-ready and --health must be positive")
    deploy_root = Path(args.deploy_root).resolve()
    bundle_path = deploy_root / "api" / "ncs_ontology_compact.zip"
    if not deploy_root.is_dir() or not bundle_path.is_file():
        raise SystemExit("deploy root or compact bundle is missing")
    vercel_command = shutil.which("vercel") or shutil.which("vercel.cmd")
    if not vercel_command:
        raise SystemExit("Vercel CLI is not available")
    bundle_bytes = bundle_path.stat().st_size

    deployments: list[dict[str, object]] = []
    errors: list[dict[str, object]] = []
    for sequence in range(1, args.deployments + 1):
        try:
            deployment_url = deploy_preview(
                deploy_root=deploy_root,
                vercel_command=vercel_command,
                timeout_seconds=args.deploy_timeout_seconds,
            )
            inspect_metadata = inspect_deployment(
                deployment_url=deployment_url,
                deploy_root=deploy_root,
                vercel_command=vercel_command,
                timeout_seconds=args.inspect_timeout_seconds,
            )
            deployments.append(
                measure_one(
                    sequence=sequence,
                    deployment_url=deployment_url,
                    inspect_metadata=inspect_metadata,
                    bundle_bytes=bundle_bytes,
                    timeout_seconds=args.request_timeout_seconds,
                    delay_seconds=args.delay_seconds,
                    warm_ready_count=args.warm_ready,
                    health_count=args.health,
                )
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
            errors.append(
                {
                    "sequence": sequence,
                    "error_kind": type(exc).__name__,
                    "message": safe_error_text(exc),
                }
            )
            # Each sequence gets one deployment attempt.  Continue only to the
            # next bounded sequence; never retry a failed deployment in place.

    report = build_report(
        deployments=deployments,
        source_commit=args.source_commit,
        deploy_root=str(deploy_root),
        bundle_bytes=bundle_bytes,
        delay_seconds=args.delay_seconds,
        timeout_seconds=args.request_timeout_seconds,
        errors=errors,
    )
    out_path = Path(args.out)
    markdown_path = Path(args.markdown_out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    markdown_path.write_text(markdown_report(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "deployments": len(deployments),
                "errors": len(errors),
                "contract_ok": report["summary"]["contract_ok"],
                "out": str(out_path),
                "markdown_out": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if deployments and not errors else 1


if __name__ == "__main__":
    sys.exit(main())
