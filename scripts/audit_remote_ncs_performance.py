#!/usr/bin/env python3
"""Rate-limited, read-only performance and contract audit for the public NCS MCP.

The report intentionally persists only aggregate latency, contract fields, counts,
hashes, and whitelisted bootstrap metrics. Raw HTTP/MCP bodies are never written.
The first observed request is not evidence of a cold start.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


BASE_URL_ENV_KEY = "NCS_REMOTE_AUDIT_BASE_URL"
DEFAULT_OUT = "reports/ncs_remote_performance_current_20260830.json"
DEFAULT_MARKDOWN_OUT = "reports/ncs_remote_performance_current_20260830.md"
CORE_SEARCHES = (
    "\ucc44\uc6a9",
    "\uc2e0\uc785\uc0ac\uc6d0 \ucc44\uc6a9 \uba74\uc811",
    "\ub370\uc774\ud130 \ubd84\uc11d\uac00",
    "\ud488\uc9c8\uad00\ub9ac \ub2f4\ub2f9\uc790 \uad50\uc721",
)
MATCH_METADATA_KEYS = {
    "match_tier",
    "matched_tokens",
    "query_tokens",
    "matched_token_count",
    "match_mode",
    "score",
}
BOOTSTRAP_KEY_PARTS = (
    "bootstrap",
    "archive",
    "snapshot",
    "extract",
    "decompress",
    "hash",
    "duration",
    "elapsed",
    "stage",
    "timing",
)


@dataclass
class HttpObservation:
    status: int | None
    elapsed_ms: float
    content_type: str | None
    body: bytes
    error_kind: str | None = None
    attempts: int = 1


RequestFn = Callable[[str, str, Mapping[str, Any] | None], HttpObservation]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def percentile(values: Sequence[float], quantile: float) -> float:
    if not values:
        raise ValueError("values must not be empty")
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


def latency_summary(values: Sequence[float]) -> dict[str, float | int | None]:
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
        "min_ms": round(min(samples), 3),
        "p50_ms": round(percentile(samples, 0.50), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "max_ms": round(max(samples), 3),
        "mean_ms": round(mean, 3),
        "stdev_ms": round(stdev, 3),
        "coefficient_of_variation": round(stdev / mean, 6) if mean else None,
    }


def body_sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def parse_json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def jsonrpc_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if not isinstance(result, dict):
        return ""
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
    return "\n".join(parts)


def _whitelisted_match_metadata(value: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key, item in node.items():
                if key in MATCH_METADATA_KEYS:
                    if isinstance(item, (str, int, float, bool)) or item is None:
                        found[key] = item
                    elif isinstance(item, list) and all(
                        isinstance(part, (str, int, float, bool)) or part is None
                        for part in item
                    ):
                        found[key] = item
                visit(item)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(value)
    return found


def parse_search_contract(payload: Any) -> dict[str, Any]:
    text = jsonrpc_text(payload)
    count_match = re.search(r"(\d+)\uac74 \uc911 (\d+)\uac74 \ud45c\uc2dc", text)
    type_match = re.search(
        r"unit (\d+)\uac74, element (\d+)\uac74, criteria (\d+)\uac74, ksa (\d+)\uac74",
        text,
    )
    offsets = [int(value) for value in re.findall(r"offset=(\d+)", text)]
    type_counts = {
        "unit": int(type_match.group(1)) if type_match else 0,
        "element": int(type_match.group(2)) if type_match else 0,
        "criteria": int(type_match.group(3)) if type_match else 0,
        "ksa": int(type_match.group(4)) if type_match else 0,
    }
    structured = None
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        structured = payload["result"].get("structuredContent")
    metadata = _whitelisted_match_metadata(structured)
    labels_in_text = sorted(
        key for key in MATCH_METADATA_KEYS if re.search(rf"\b{re.escape(key)}\b", text)
    )
    preview_rows = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|") or "---" in stripped:
            continue
        if re.search(r"\d{8,10}_\d{2}v\d", stripped) or re.match(
            r"\|\s*(?:unit|element|criteria|ksa)\s*\|", stripped
        ):
            preview_rows.append(stripped)
    canonical_text = re.sub(
        r"audit\.generated_at:\s*`[^`]+`",
        "audit.generated_at: `<redacted>`",
        text,
    )
    found = "[NOT_FOUND]" not in text and bool(count_match and int(count_match.group(1)) > 0)
    return {
        "found": found,
        "zero_hit": not found,
        "result_count": int(count_match.group(1)) if count_match else 0,
        "preview_count": int(count_match.group(2)) if count_match else 0,
        "counts_by_type": type_counts,
        "types_present": [name for name, count in type_counts.items() if count > 0],
        "types_missing": [name for name, count in type_counts.items() if count == 0],
        "current_offset": offsets[0] if offsets else None,
        "next_offset": offsets[1] if len(offsets) > 1 else None,
        "match_metadata": {
            "present": bool(metadata or labels_in_text),
            "structured_fields": metadata,
            "markdown_labels": labels_in_text,
        },
        "response_sha256": hashlib.sha256(canonical_text.encode("utf-8")).hexdigest(),
        "preview_result_sha256": hashlib.sha256(
            "\n".join(preview_rows).encode("utf-8")
        ).hexdigest()
        if preview_rows
        else None,
    }


def extract_bootstrap_metrics(payload: Any, source: str) -> list[dict[str, Any]]:
    """Return numeric/boolean bootstrap metrics only; never persist sibling values."""
    metrics: list[dict[str, Any]] = []

    def visit(node: Any, path: tuple[str, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = path + (str(key),)
                lowered = ".".join(child_path).lower()
                if (
                    isinstance(value, (int, float, bool))
                    and not isinstance(value, str)
                    and any(part in lowered for part in BOOTSTRAP_KEY_PARTS)
                ):
                    metrics.append(
                        {
                            "source": source,
                            "metric_path": ".".join(child_path),
                            "value": value,
                            "measurement_scope": "process_level",
                        }
                    )
                visit(value, child_path)
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, path + (str(index),))

    visit(payload, ())
    return metrics


class UrlLibTransport:
    def __init__(
        self,
        timeout_seconds: float,
        max_retries: int,
        retry_backoff_seconds: float,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.sleeper = sleeper

    def __call__(
        self, url: str, method: str, payload: Mapping[str, Any] | None
    ) -> HttpObservation:
        encoded = None
        if payload is not None:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
                "ascii"
            )
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "User-Agent": "ncs-remote-performance-audit/1.0",
        }
        last: HttpObservation | None = None
        for attempt in range(1, self.max_retries + 2):
            request = urllib.request.Request(
                url, data=encoded, method=method, headers=headers
            )
            started = time.perf_counter()
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    observation = HttpObservation(
                        status=response.status,
                        elapsed_ms=(time.perf_counter() - started) * 1000,
                        content_type=response.headers.get("Content-Type"),
                        body=body,
                        attempts=attempt,
                    )
            except urllib.error.HTTPError as error:
                observation = HttpObservation(
                    status=error.code,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    content_type=error.headers.get("Content-Type"),
                    body=error.read(),
                    error_kind="http_error",
                    attempts=attempt,
                )
            except (urllib.error.URLError, TimeoutError) as error:
                observation = HttpObservation(
                    status=None,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    content_type=None,
                    body=b"",
                    error_kind=type(error).__name__,
                    attempts=attempt,
                )
            last = observation
            transient = observation.status in {429, 500, 502, 503, 504} or observation.status is None
            if not transient or attempt > self.max_retries:
                return observation
            self.sleeper(self.retry_backoff_seconds)
        assert last is not None
        return last


def _public_observation(observation: HttpObservation) -> dict[str, Any]:
    return {
        "status": observation.status,
        "elapsed_ms": round(observation.elapsed_ms, 3),
        "content_type": observation.content_type,
        "error_kind": observation.error_kind,
        "attempts": observation.attempts,
        "response_body_sha256": body_sha256(observation.body),
    }


def measure_scenario(
    request_fn: RequestFn,
    url: str,
    method: str,
    payload_factory: Callable[[int], Mapping[str, Any] | None],
    expected_status: int,
    warm_runs: int,
    request_delay_seconds: float,
    sleeper: Callable[[float], None],
) -> tuple[dict[str, Any], HttpObservation, list[HttpObservation]]:
    first = request_fn(url, method, payload_factory(0))
    warm: list[HttpObservation] = []
    for index in range(warm_runs):
        if request_delay_seconds:
            sleeper(request_delay_seconds)
        warm.append(request_fn(url, method, payload_factory(index + 1)))
    success_samples = [item.elapsed_ms for item in warm if item.status == expected_status]
    public = {
        "expected_status": expected_status,
        "first_observed_request": _public_observation(first),
        "first_observed_is_cold_claim": False,
        "warm_latency": latency_summary(success_samples),
        "warm_statuses": [item.status for item in warm],
        "warm_error_kinds": [item.error_kind for item in warm],
        "contract_status_ok": first.status == expected_status
        and all(item.status == expected_status for item in warm),
    }
    return public, first, warm


def _rpc_payload(request_id: int, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}


def _search_payload(request_id: int, query: str, offset: int) -> dict[str, Any]:
    return _rpc_payload(
        request_id,
        "tools/call",
        {
            "name": "ncs_search",
            "arguments": {"query": query, "scope": "all", "limit": 20, "offset": offset},
        },
    )


def _safe_commit_from_version(version: str | None) -> str | None:
    if not version:
        return None
    match = re.search(r"\+git\.([0-9a-fA-F]{7,40})", version)
    return match.group(1).lower() if match else None


def _endpoint_contract(payload: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"json_object": False, "status_value": None}
    expected_value = "ok" if endpoint == "health" else "ready"
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    database = runtime.get("database") if isinstance(runtime.get("database"), dict) else {}
    bootstrap = payload.get("bootstrap") if isinstance(payload.get("bootstrap"), dict) else {}
    local_snapshot = (
        bootstrap.get("local_snapshot")
        if isinstance(bootstrap.get("local_snapshot"), dict)
        else {}
    )
    return {
        "json_object": True,
        "status_value": payload.get("status"),
        "status_value_ok": payload.get("status") == expected_value,
        "database_ready": database.get("ready"),
        "readiness_count_source": database.get("readiness_count_source"),
        "bootstrap": {
            "schema": bootstrap.get("schema"),
            "status": bootstrap.get("status"),
            "source": bootstrap.get("source"),
            "elapsed_ms": bootstrap.get("elapsed_ms"),
            "process_level_metrics": bootstrap.get("process_level_metrics"),
            "request_level_metrics": bootstrap.get("request_level_metrics"),
            "readiness_fast_path_configured": local_snapshot.get(
                "readiness_fast_path_configured"
            ),
        },
    }


def _nested_number(root: Mapping[str, Any], *path: str) -> float | None:
    value: Any = root
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def add_baseline_comparison(
    report: dict[str, Any],
    baseline: Mapping[str, Any],
    expected_commit: str | None,
    expected_tool_count: int,
) -> None:
    baseline_ready = _nested_number(
        baseline, "endpoints", "ready", "warm_latency", "p50_ms"
    )
    current_ready = _nested_number(
        report, "endpoints", "ready", "warm_latency", "p50_ms"
    )
    improvement = None
    if baseline_ready and current_ready is not None:
        improvement = round((baseline_ready - current_ready) / baseline_ready * 100, 3)

    baseline_search = {
        item.get("query"): _nested_number(item, "warm_latency", "p50_ms")
        for item in baseline.get("searches", [])
        if isinstance(item, Mapping) and isinstance(item.get("query"), str)
    }
    search_comparison = []
    for item in report.get("searches", []):
        query = item.get("query")
        current = _nested_number(item, "warm_latency", "p50_ms")
        prior = baseline_search.get(query)
        delta = None
        if prior and current is not None:
            delta = round((prior - current) / prior * 100, 3)
        search_comparison.append(
            {
                "query": query,
                "baseline_p50_ms": prior,
                "current_p50_ms": current,
                "improvement_percent": delta,
            }
        )

    ready_contract = report["endpoints"]["ready"].get("response_contract", {})
    bootstrap_contract = ready_contract.get("bootstrap", {})
    count_source = ready_contract.get("readiness_count_source")
    fast_path = bootstrap_contract.get("readiness_fast_path_configured")
    actual_commit = report["deployment_evidence"].get("git_commit")
    commit_ok = (
        True
        if not expected_commit
        else isinstance(actual_commit, str) and actual_commit.startswith(expected_commit.lower())
    )
    tool_count = report["mcp"]["tools_list"]["response_contract"].get("tool_count")
    baseline_zero_hits = baseline.get("summary", {}).get("search_zero_hit_count")
    current_zero_hits = report.get("summary", {}).get("search_zero_hit_count")

    triggers: list[dict[str, Any]] = []

    def trigger(code: str, severity: str, observed: Any, expected: Any) -> None:
        triggers.append(
            {
                "code": code,
                "severity": severity,
                "observed": observed,
                "expected": expected,
            }
        )

    if not report.get("summary", {}).get("contract_ok"):
        trigger("public_contract_regression", "critical", False, True)
    if not commit_ok:
        trigger("deployment_commit_mismatch", "critical", actual_commit, expected_commit)
    if tool_count != expected_tool_count:
        trigger("public_tool_count_changed", "critical", tool_count, expected_tool_count)
    if not report.get("pagination", {}).get("contract_ok"):
        trigger("pagination_contract_regression", "critical", False, True)
    if isinstance(baseline_zero_hits, int) and isinstance(current_zero_hits, int) and current_zero_hits > baseline_zero_hits:
        trigger(
            "search_zero_hit_regression",
            "critical",
            current_zero_hits,
            f"<= {baseline_zero_hits}",
        )
    if fast_path is not True:
        trigger("readiness_fast_path_not_configured", "high", fast_path, True)
    if count_source != "verified_snapshot_metadata":
        trigger(
            "readiness_count_source_unexpected",
            "high",
            count_source,
            "verified_snapshot_metadata",
        )
    if improvement is not None and improvement < -20:
        trigger(
            "ready_p50_regressed_over_20_percent",
            "high",
            improvement,
            ">= -20 percent",
        )
    severity_order = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    severity = "none"
    for item in triggers:
        if severity_order[item["severity"]] > severity_order[severity]:
            severity = item["severity"]

    report["baseline_comparison"] = {
        "baseline_schema": baseline.get("schema"),
        "baseline_generated_at": baseline.get("generated_at"),
        "ready_p50_ms": {
            "baseline": baseline_ready,
            "current": current_ready,
            "improvement_percent": improvement,
        },
        "search_p50": search_comparison,
        "readiness_count_source": count_source,
        "bootstrap_fast_path_configured": fast_path,
        "bootstrap_process_elapsed_ms": bootstrap_contract.get("elapsed_ms"),
        "expected_commit": expected_commit,
        "actual_commit": actual_commit,
        "commit_matches": commit_ok,
        "expected_tool_count": expected_tool_count,
        "actual_tool_count": tool_count,
    }
    report["release_assessment"] = {
        "severity": severity,
        "rollback_triggered": severity in {"high", "critical"},
        "rollback_triggers": triggers,
    }


def add_additional_baseline_comparison(
    report: dict[str, Any],
    label: str,
    baseline: Mapping[str, Any],
    expected_commit: str | None,
    expected_tool_count: int,
) -> None:
    working = json.loads(json.dumps(report))
    add_baseline_comparison(
        working,
        baseline,
        expected_commit=expected_commit,
        expected_tool_count=expected_tool_count,
    )
    report.setdefault("additional_baseline_comparisons", {})[label] = {
        "comparison": working["baseline_comparison"],
        "assessment": working["release_assessment"],
    }


def build_audit(
    base_url: str,
    request_fn: RequestFn,
    warm_runs: int,
    request_delay_seconds: float,
    timeout_seconds: float,
    max_retries: int,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    api_mcp = base_url + "/api/mcp"
    endpoints: dict[str, Any] = {}
    bootstrap_metrics: list[dict[str, Any]] = []

    for name, path, expected in (
        ("health", "/api/health", 200),
        ("ready", "/api/ready", 200),
        ("mcp_get", "/api/mcp", 405),
    ):
        measured, first, _ = measure_scenario(
            request_fn,
            base_url + path,
            "GET",
            lambda _index: None,
            expected,
            warm_runs,
            request_delay_seconds,
            sleeper,
        )
        payload = parse_json(first.body)
        if name in {"health", "ready"}:
            measured["response_contract"] = _endpoint_contract(payload, name)
            bootstrap_metrics.extend(extract_bootstrap_metrics(payload, name))
        else:
            measured["response_contract"] = {
                "jsonrpc_error": isinstance(payload, dict)
                and isinstance(payload.get("error"), dict),
                "error_code": payload.get("error", {}).get("code")
                if isinstance(payload, dict)
                else None,
            }
        endpoints[name] = measured

    initialize, initialize_first, _ = measure_scenario(
        request_fn,
        api_mcp,
        "POST",
        lambda index: _rpc_payload(
            1000 + index,
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "ncs-performance-audit", "version": "1.0"},
            },
        ),
        200,
        warm_runs,
        request_delay_seconds,
        sleeper,
    )
    initialize_payload = parse_json(initialize_first.body)
    initialize_result = (
        initialize_payload.get("result", {}) if isinstance(initialize_payload, dict) else {}
    )
    server_info = initialize_result.get("serverInfo", {}) if isinstance(initialize_result, dict) else {}
    server_version = server_info.get("version") if isinstance(server_info, dict) else None
    initialize["response_contract"] = {
        "jsonrpc": initialize_payload.get("jsonrpc") if isinstance(initialize_payload, dict) else None,
        "protocol_version": initialize_result.get("protocolVersion")
        if isinstance(initialize_result, dict)
        else None,
        "server_name": server_info.get("name") if isinstance(server_info, dict) else None,
        "server_version": server_version,
        "result_present": bool(initialize_result),
    }

    tools_list, tools_first, _ = measure_scenario(
        request_fn,
        api_mcp,
        "POST",
        lambda index: _rpc_payload(2000 + index, "tools/list", {}),
        200,
        warm_runs,
        request_delay_seconds,
        sleeper,
    )
    tools_payload = parse_json(tools_first.body)
    tools = []
    if isinstance(tools_payload, dict):
        candidate = tools_payload.get("result", {}).get("tools", [])
        if isinstance(candidate, list):
            tools = candidate
    tool_names = [item.get("name") for item in tools if isinstance(item, dict) and isinstance(item.get("name"), str)]
    ncs_search_schema = next(
        (
            item.get("inputSchema")
            for item in tools
            if isinstance(item, dict) and item.get("name") == "ncs_search"
        ),
        None,
    )
    search_properties = (
        sorted(ncs_search_schema.get("properties", {}).keys())
        if isinstance(ncs_search_schema, dict)
        and isinstance(ncs_search_schema.get("properties"), dict)
        else []
    )
    tools_list["response_contract"] = {
        "jsonrpc": tools_payload.get("jsonrpc") if isinstance(tools_payload, dict) else None,
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "ncs_search_present": "ncs_search" in tool_names,
        "ncs_search_input_properties": search_properties,
        "offset_supported": "offset" in search_properties,
    }

    searches: list[dict[str, Any]] = []
    page_zero_contract: dict[str, Any] | None = None
    for query_index, query in enumerate(CORE_SEARCHES):
        measured, first, warm = measure_scenario(
            request_fn,
            api_mcp,
            "POST",
            lambda index, q=query, qi=query_index: _search_payload(
                3000 + qi * 100 + index, q, 0
            ),
            200,
            warm_runs,
            request_delay_seconds,
            sleeper,
        )
        contract = parse_search_contract(parse_json(first.body))
        warm_contracts = [parse_search_contract(parse_json(item.body)) for item in warm]
        measured.update(
            {
                "query": query,
                "scope": "all",
                "limit": 20,
                "offset": 0,
                "response_contract": contract,
                "warm_zero_hit_count": sum(item["zero_hit"] for item in warm_contracts),
                "warm_result_counts": [item["result_count"] for item in warm_contracts],
                "response_stable_across_warm_runs": len(
                    {item["response_sha256"] for item in warm_contracts}
                )
                == 1,
            }
        )
        searches.append(measured)
        if query == CORE_SEARCHES[0]:
            page_zero_contract = contract

    page_twenty, page_twenty_first, page_twenty_warm = measure_scenario(
        request_fn,
        api_mcp,
        "POST",
        lambda index: _search_payload(4000 + index, CORE_SEARCHES[0], 20),
        200,
        warm_runs,
        request_delay_seconds,
        sleeper,
    )
    page_twenty_contract = parse_search_contract(parse_json(page_twenty_first.body))
    page_twenty_warm_contracts = [
        parse_search_contract(parse_json(item.body)) for item in page_twenty_warm
    ]
    page_twenty.update(
        {
            "query": CORE_SEARCHES[0],
            "scope": "all",
            "limit": 20,
            "offset": 20,
            "response_contract": page_twenty_contract,
            "response_stable_across_warm_runs": len(
                {item["response_sha256"] for item in page_twenty_warm_contracts}
            )
            == 1,
        }
    )
    pagination = {
        "query": CORE_SEARCHES[0],
        "limit": 20,
        "page_zero": page_zero_contract,
        "page_twenty": page_twenty,
        "page_fingerprints_distinct": bool(
            page_zero_contract
            and page_zero_contract["preview_result_sha256"]
            and page_twenty_contract["preview_result_sha256"]
            and page_zero_contract["preview_result_sha256"]
            != page_twenty_contract["preview_result_sha256"]
        ),
        "contract_ok": bool(
            page_zero_contract
            and page_zero_contract.get("current_offset") == 0
            and page_zero_contract.get("next_offset") == 20
            and page_twenty_contract.get("current_offset") == 20
            and page_zero_contract["preview_result_sha256"]
            and page_twenty_contract["preview_result_sha256"]
            and page_zero_contract["preview_result_sha256"]
            != page_twenty_contract["preview_result_sha256"]
        ),
    }

    contract_checks = [item["contract_status_ok"] for item in endpoints.values()]
    contract_checks.extend(
        [
            initialize["contract_status_ok"],
            initialize["response_contract"]["result_present"],
            tools_list["contract_status_ok"],
            tools_list["response_contract"]["ncs_search_present"],
            tools_list["response_contract"]["offset_supported"],
            all(item["contract_status_ok"] for item in searches),
            pagination["contract_ok"],
        ]
    )
    zero_hit_queries = [
        item["query"] for item in searches if item["response_contract"]["zero_hit"]
    ]
    return {
        "schema": "ncs_remote_performance_audit_v1",
        "generated_at": utc_now(),
        "target": {
            "base_url": base_url,
            "mcp_endpoint": "/api/mcp",
            "health_endpoint": "/api/health",
            "ready_endpoint": "/api/ready",
        },
        "safety": {
            "read_only_requests_only": True,
            "deployment_mutation": False,
            "raw_response_bodies_persisted": False,
            "request_delay_seconds": request_delay_seconds,
            "timeout_seconds": timeout_seconds,
            "max_retries": max_retries,
            "cold_claim": False,
        },
        "measurement_semantics": {
            "first_observed_request_label": "first_observed_request",
            "first_observed_request_is_cold_evidence": False,
            "warm_samples_per_scenario": warm_runs,
            "request_latency_scope": "request_level",
            "response_bootstrap_metric_scope": "process_level",
        },
        "deployment_evidence": {
            "source": "mcp_initialize_server_info_version",
            "server_name": server_info.get("name") if isinstance(server_info, dict) else None,
            "server_version": server_version,
            "git_commit": _safe_commit_from_version(server_version),
            "vercel_inspect_used": False,
        },
        "summary": {
            "ok": all(contract_checks) and not zero_hit_queries,
            "contract_ok": all(contract_checks),
            "search_zero_hit_count": len(zero_hit_queries),
            "search_zero_hit_queries": zero_hit_queries,
            "core_search_count": len(searches),
        },
        "endpoints": endpoints,
        "mcp": {"initialize": initialize, "tools_list": tools_list},
        "searches": searches,
        "pagination": pagination,
        "response_bootstrap_metrics": bootstrap_metrics,
        "limitations": [
            "The first observed request is not a controlled Vercel cold start.",
            "Warm labels mean repeated observations in this audit, not proof of process reuse.",
            "Search relevance is represented by hit/count/type metadata, not human relevance judgments.",
            "Raw HTTP and MCP response bodies are deliberately excluded from the report.",
        ],
    }


def _fmt_ms(value: Any) -> str:
    return "-" if value is None else f"{float(value):.1f}"


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report["summary"]
    deployment = report["deployment_evidence"]
    lines = [
        "# NCS remote performance and contract audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Target: `{report['target']['base_url']}`",
        f"- Schema: `{report['schema']}`",
        f"- Contract OK: `{str(summary['contract_ok']).lower()}`",
        f"- Core search zero hits: `{summary['search_zero_hit_count']}` / `{summary['core_search_count']}`",
        f"- Server version: `{deployment.get('server_version')}`",
        f"- Deployment commit evidence: `{deployment.get('git_commit')}`",
        "- Cold claim: `false` (first request is only `first_observed_request`)",
        "",
        "## Endpoint latency",
        "",
        "| Scenario | Expected | First observed ms | Warm n | p50 ms | p95 ms | max ms | CV | Contract |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    scenarios: list[tuple[str, Mapping[str, Any]]] = list(report["endpoints"].items())
    scenarios.extend(
        [
            ("initialize", report["mcp"]["initialize"]),
            ("tools/list", report["mcp"]["tools_list"]),
        ]
    )
    for name, item in scenarios:
        warm = item["warm_latency"]
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    str(item["expected_status"]),
                    _fmt_ms(item["first_observed_request"]["elapsed_ms"]),
                    str(warm["sample_count"]),
                    _fmt_ms(warm["p50_ms"]),
                    _fmt_ms(warm["p95_ms"]),
                    _fmt_ms(warm["max_ms"]),
                    "-" if warm["coefficient_of_variation"] is None else f"{warm['coefficient_of_variation']:.3f}",
                    str(item["contract_status_ok"]).lower(),
                ]
            )
            + " |"
        )
    comparison = report.get("baseline_comparison")
    if isinstance(comparison, Mapping):
        ready = comparison["ready_p50_ms"]
        assessment = report.get("release_assessment", {})
        lines.extend(
            [
                "",
                "## Readiness fast path and baseline comparison",
                "",
                f"- Baseline ready p50: `{_fmt_ms(ready.get('baseline'))} ms`",
                f"- Current ready p50: `{_fmt_ms(ready.get('current'))} ms`",
                f"- Ready p50 improvement: `{ready.get('improvement_percent')}%`",
                f"- Readiness count source: `{comparison.get('readiness_count_source')}`",
                f"- Bootstrap fast path configured: `{str(comparison.get('bootstrap_fast_path_configured')).lower()}`",
                f"- Process-level bootstrap elapsed: `{_fmt_ms(comparison.get('bootstrap_process_elapsed_ms'))} ms`",
                f"- Expected / actual commit: `{comparison.get('expected_commit')}` / `{comparison.get('actual_commit')}`",
                f"- Release severity: `{assessment.get('severity')}`",
                f"- Rollback triggered: `{str(assessment.get('rollback_triggered')).lower()}`",
                "",
                "| Query | Baseline p50 ms | Current p50 ms | Improvement |",
                "| --- | ---: | ---: | ---: |",
            ]
        )
        for item in comparison["search_p50"]:
            lines.append(
                f"| {item['query']} | {_fmt_ms(item['baseline_p50_ms'])} | {_fmt_ms(item['current_p50_ms'])} | {item['improvement_percent']}% |"
            )
        triggers = assessment.get("rollback_triggers", [])
        if triggers:
            lines.extend(["", "### Rollback triggers", ""])
            for item in triggers:
                lines.append(
                    f"- `{item['severity']}` `{item['code']}`: observed `{item['observed']}`, expected `{item['expected']}`"
                )
    additional = report.get("additional_baseline_comparisons")
    if isinstance(additional, Mapping) and additional:
        lines.extend(
            [
                "",
                "## Additional historical baselines",
                "",
                "| Baseline | Generated | Ready baseline p50 ms | Current p50 ms | Improvement | Severity | Rollback |",
                "| --- | --- | ---: | ---: | ---: | --- | --- |",
            ]
        )
        for label, item in additional.items():
            baseline_comparison = item["comparison"]
            ready = baseline_comparison["ready_p50_ms"]
            assessment = item["assessment"]
            lines.append(
                f"| {label} | `{baseline_comparison.get('baseline_generated_at')}` | {_fmt_ms(ready.get('baseline'))} | {_fmt_ms(ready.get('current'))} | {ready.get('improvement_percent')}% | {assessment.get('severity')} | {str(assessment.get('rollback_triggered')).lower()} |"
            )
    lines.extend(
        [
            "",
            "## Search latency and coverage",
            "",
            "| Query | First observed ms | Warm n | p50 ms | p95 ms | max ms | Zero hit | unit | element | criteria | ksa | Match metadata |",
            "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in report["searches"]:
        warm = item["warm_latency"]
        contract = item["response_contract"]
        counts = contract["counts_by_type"]
        lines.append(
            "| "
            + " | ".join(
                [
                    str(item["query"]),
                    _fmt_ms(item["first_observed_request"]["elapsed_ms"]),
                    str(warm["sample_count"]),
                    _fmt_ms(warm["p50_ms"]),
                    _fmt_ms(warm["p95_ms"]),
                    _fmt_ms(warm["max_ms"]),
                    str(contract["zero_hit"]).lower(),
                    str(counts["unit"]),
                    str(counts["element"]),
                    str(counts["criteria"]),
                    str(counts["ksa"]),
                    str(contract["match_metadata"]["present"]).lower(),
                ]
            )
            + " |"
        )
    pagination = report["pagination"]
    lines.extend(
        [
            "",
            "## Pagination",
            "",
            f"- Query: `{pagination['query']}`",
            f"- Page 0 reports current/next offset: `{pagination['page_zero']['current_offset']}` / `{pagination['page_zero']['next_offset']}`",
            f"- Page 20 reports current offset: `{pagination['page_twenty']['response_contract']['current_offset']}`",
            f"- Page fingerprints distinct: `{str(pagination['page_fingerprints_distinct']).lower()}`",
            f"- Pagination contract OK: `{str(pagination['contract_ok']).lower()}`",
            "",
            "## Bootstrap metrics",
            "",
        ]
    )
    metrics = report["response_bootstrap_metrics"]
    if metrics:
        lines.extend(
            [
                "Only response-provided numeric/boolean metrics are retained; scope is process-level.",
                "",
                "| Source | Metric | Value | Scope |",
                "| --- | --- | ---: | --- |",
            ]
        )
        for metric in metrics:
            lines.append(
                f"| {metric['source']} | `{metric['metric_path']}` | {metric['value']} | {metric['measurement_scope']} |"
            )
    else:
        lines.append("No response-provided bootstrap stage metrics were exposed.")
    lines.extend(
        [
            "",
            "## Safety and limitations",
            "",
            "- The audit sends public, read-only GET and MCP POST requests only.",
            "- It does not change Vercel configuration, deployments, login state, or project files outside its report outputs.",
            "- Raw bodies and secrets are not persisted; response hashes are retained for stability and pagination checks.",
        ]
    )
    for limitation in report["limitations"]:
        lines.append(f"- {limitation}")
    return "\n".join(lines) + "\n"


def _validate_base_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("base URL must be an absolute http(s) URL")
    if parsed.query or parsed.fragment:
        raise argparse.ArgumentTypeError("base URL must not contain query or fragment")
    return value.rstrip("/")


def resolve_base_url(value: str | None) -> str:
    candidate = value or os.environ.get(BASE_URL_ENV_KEY)
    if not candidate:
        raise SystemExit(
            f"--base-url is required unless {BASE_URL_ENV_KEY} is set"
        )
    return _validate_base_url(candidate)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url")
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--baseline")
    parser.add_argument(
        "--additional-baseline",
        action="append",
        default=[],
        metavar="LABEL=PATH",
    )
    parser.add_argument("--expected-commit")
    parser.add_argument("--expected-tool-count", type=int, default=7)
    parser.add_argument("--warm-runs", type=int, default=5)
    parser.add_argument("--request-delay-seconds", type=float, default=0.20)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--retry-backoff-seconds", type=float, default=0.50)
    parser.add_argument("--vercel-deployment-id")
    parser.add_argument("--vercel-target")
    parser.add_argument("--vercel-ready-state")
    parser.add_argument("--vercel-runtime")
    parser.add_argument("--vercel-memory-mb", type=int)
    parser.add_argument("--vercel-timeout-seconds", type=int)
    parser.add_argument("--vercel-bundle-bytes", type=int)
    parser.add_argument(
        "--vercel-git-deployment-enabled",
        choices=("true", "false", "unknown"),
        default="unknown",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.warm_runs < 5:
        raise SystemExit("--warm-runs must be at least 5")
    if args.max_retries < 0 or args.max_retries > 2:
        raise SystemExit("--max-retries must be between 0 and 2")
    if args.request_delay_seconds < 0.05:
        raise SystemExit("--request-delay-seconds must be at least 0.05")
    transport = UrlLibTransport(
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
        retry_backoff_seconds=args.retry_backoff_seconds,
    )
    base_url = resolve_base_url(args.base_url)
    report = build_audit(
        base_url=base_url,
        request_fn=transport,
        warm_runs=args.warm_runs,
        request_delay_seconds=args.request_delay_seconds,
        timeout_seconds=args.timeout_seconds,
        max_retries=args.max_retries,
    )
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        add_baseline_comparison(
            report,
            baseline,
            expected_commit=args.expected_commit,
            expected_tool_count=args.expected_tool_count,
        )
    for specification in args.additional_baseline:
        if "=" not in specification:
            raise SystemExit("--additional-baseline must use LABEL=PATH")
        label, path = specification.split("=", 1)
        if not label.strip() or not path.strip():
            raise SystemExit("--additional-baseline must use non-empty LABEL=PATH")
        additional_baseline = json.loads(Path(path).read_text(encoding="utf-8"))
        add_additional_baseline_comparison(
            report,
            label.strip(),
            additional_baseline,
            expected_commit=args.expected_commit,
            expected_tool_count=args.expected_tool_count,
        )
    if args.vercel_deployment_id:
        report["deployment_evidence"]["vercel_inspect_used"] = True
        report["deployment_evidence"]["vercel_inspect"] = {
            "deployment_id": args.vercel_deployment_id,
            "target": args.vercel_target,
            "ready_state": args.vercel_ready_state,
            "runtime": args.vercel_runtime,
            "memory_mb": args.vercel_memory_mb,
            "timeout_seconds": args.vercel_timeout_seconds,
            "bundle_bytes": args.vercel_bundle_bytes,
            "git_deployment_enabled": (
                None
                if args.vercel_git_deployment_enabled == "unknown"
                else args.vercel_git_deployment_enabled == "true"
            ),
            "commit_linkage": "server_version_git_suffix",
        }
    out = Path(args.out)
    markdown_out = Path(args.markdown_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(out),
                "markdown_out": str(markdown_out),
                "contract_ok": report["summary"]["contract_ok"],
                "zero_hits": report["summary"]["search_zero_hit_count"],
                "cold_claim": report["safety"]["cold_claim"],
                "server_version": report["deployment_evidence"]["server_version"],
                "endpoint_warm_p50_ms": {
                    name: item["warm_latency"]["p50_ms"]
                    for name, item in report["endpoints"].items()
                },
                "search_warm_p50_ms": {
                    item["query"]: item["warm_latency"]["p50_ms"]
                    for item in report["searches"]
                },
                "pagination_contract_ok": report["pagination"]["contract_ok"],
                "ready_p50_improvement_percent": report.get("baseline_comparison", {})
                .get("ready_p50_ms", {})
                .get("improvement_percent"),
                "readiness_count_source": report.get("baseline_comparison", {}).get(
                    "readiness_count_source"
                ),
                "bootstrap_fast_path_configured": report.get(
                    "baseline_comparison", {}
                ).get("bootstrap_fast_path_configured"),
                "release_severity": report.get("release_assessment", {}).get("severity"),
                "rollback_triggered": report.get("release_assessment", {}).get(
                    "rollback_triggered"
                ),
                "additional_ready_p50_improvement_percent": {
                    label: item["comparison"]["ready_p50_ms"]["improvement_percent"]
                    for label, item in report.get(
                        "additional_baseline_comparisons", {}
                    ).items()
                },
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["summary"]["contract_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
