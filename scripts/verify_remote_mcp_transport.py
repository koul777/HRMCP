from __future__ import annotations

import argparse
import json
import re
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"
EXPECTED_PUBLIC_TOOLS = {
    "ncs_analysis",
    "ncs_discover_tools",
    "ncs_execute_tool",
    "ncs_search",
    "ncs_training",
    "ncs_unit_detail",
    "recommend_training_for_task",
}
RAW_EXCEPTION_MARKERS = (
    "error executing tool",
    "traceback (most recent call last)",
    "operationalerror",
    "no such table:",
    "python exception",
)
SMOKE_UNIT_QUERY = "인사기획"
SMOKE_UNIT_CODE_PLACEHOLDER = "__DISCOVERED_SMOKE_UNIT_CODE__"
TOOL_SMOKE_CALLS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    (
        "discover",
        "ncs_discover_tools",
        {"intent": "NCS-based HR job and training design"},
    ),
    (
        "search",
        "ncs_search",
        {"query": SMOKE_UNIT_QUERY, "scope": "unit", "limit": 1},
    ),
    (
        "execute_search",
        "ncs_execute_tool",
        {
            "tool_name": "ncs_search",
            "params": {"query": SMOKE_UNIT_QUERY, "scope": "unit", "limit": 1},
        },
    ),
    (
        "unit_detail",
        "ncs_unit_detail",
        {"unit_code": SMOKE_UNIT_CODE_PLACEHOLDER, "include": ["elements"]},
    ),
    (
        "training",
        "ncs_training",
        {"limit": 1},
    ),
    (
        "analysis_career_path",
        "ncs_analysis",
        {"mode": "career_path", "query": SMOKE_UNIT_QUERY, "limit": 1},
    ),
    (
        "analysis_qualification",
        "ncs_analysis",
        {"mode": "qualification", "limit": 1},
    ),
    (
        "analysis_job_base",
        "ncs_analysis",
        {"mode": "job_base", "query": SMOKE_UNIT_QUERY, "limit": 1},
    ),
    (
        "analysis_ontology",
        "ncs_analysis",
        {"mode": "ontology", "query": "인사기획", "limit": 1},
    ),
    (
        "recommend_training_for_task",
        "recommend_training_for_task",
        {
            "query": SMOKE_UNIT_QUERY,
            "limit": 1,
            "save": False,
            "compact": True,
        },
    ),
)


def _rpc(method: str, *, request_id: int | None = None, params: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _request(
    url: str,
    *,
    method: str,
    body: bytes | None = None,
    accept: str = "application/json, text/event-stream",
    protocol_version: str | None = None,
    timeout: float = 35.0,
) -> dict[str, Any]:
    headers = {"Accept": accept}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    started = time.perf_counter()
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        response = exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError) as exc:
        return {
            "status": 0,
            "duration_seconds": round(time.perf_counter() - started, 6),
            "content_type": None,
            "allow": None,
            "mcp_session_id_present": False,
            "body_bytes": 0,
            "transport_error": type(exc).__name__,
        }
    with response:
        raw_body = response.read()
        result: dict[str, Any] = {
            "status": int(response.status),
            "duration_seconds": round(time.perf_counter() - started, 6),
            "content_type": response.headers.get("Content-Type"),
            "allow": response.headers.get("Allow"),
            "mcp_session_id_present": bool(response.headers.get("Mcp-Session-Id")),
            "body_bytes": len(raw_body),
        }
    if raw_body:
        try:
            # Kept private for validation and removed before the report is emitted.
            result["_payload"] = json.loads(raw_body)
        except json.JSONDecodeError:
            result["invalid_json_body"] = True
    return result


def _response_payload(response: dict[str, Any]) -> Any:
    """Read real or test-double payloads without exposing them in reports."""
    if "_payload" in response:
        return response["_payload"]
    return response.get("payload")


def _safe_response(response: dict[str, Any]) -> dict[str, Any]:
    """Return transport metadata only; response bodies and session values stay private."""
    allowed = {
        "status",
        "duration_seconds",
        "content_type",
        "allow",
        "mcp_session_id_present",
        "body_bytes",
        "transport_error",
        "invalid_json_body",
    }
    return {key: value for key, value in response.items() if key in allowed}


def _json_text(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return ""


def _contains_raw_exception(value: Any) -> bool:
    rendered = _json_text(value).casefold()
    return any(marker in rendered for marker in RAW_EXCEPTION_MARKERS)


def _tool_content_payload(rpc_payload: Any) -> dict[str, Any] | None:
    if not isinstance(rpc_payload, dict):
        return None
    result = rpc_payload.get("result")
    if not isinstance(result, dict):
        return None
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    return None


def _first_unit_code(tool_payload: dict[str, Any] | None) -> str | None:
    if not isinstance(tool_payload, dict):
        return None
    data = tool_payload.get("data")
    rows = tool_payload.get("results")
    if not isinstance(rows, list) and isinstance(data, dict):
        rows = data.get("results")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in {"unit", "competency_unit"}:
            continue
        value = row.get("unit_code") or row.get("id")
        if value and str(value).strip():
            return str(value).strip()
    return None


def _resolve_smoke_arguments(
    arguments: dict[str, Any],
    *,
    discovered_unit_code: str | None,
) -> dict[str, Any]:
    resolved = dict(arguments)
    if resolved.get("unit_code") == SMOKE_UNIT_CODE_PLACEHOLDER:
        resolved["unit_code"] = discovered_unit_code or ""
    return resolved


def _tool_call_assessment(response: dict[str, Any]) -> dict[str, Any]:
    rpc_payload = _response_payload(response)
    rpc_error = isinstance(rpc_payload, dict) and isinstance(rpc_payload.get("error"), dict)
    rpc_result = rpc_payload.get("result") if isinstance(rpc_payload, dict) else None
    tool_is_error = isinstance(rpc_result, dict) and rpc_result.get("isError") is True
    tool_payload = _tool_content_payload(rpc_payload)
    semantic_ok = isinstance(tool_payload, dict) and tool_payload.get("ok") is True
    payload_chars = (
        len(json.dumps(tool_payload, ensure_ascii=False, separators=(",", ":")))
        if isinstance(tool_payload, dict)
        else None
    )
    raw_exception = _contains_raw_exception(rpc_payload)
    return {
        **_safe_response(response),
        "jsonrpc_error": rpc_error,
        "tool_result_is_error": tool_is_error,
        "semantic_ok": semantic_ok,
        "payload_chars": payload_chars,
        "raw_exception_detected": raw_exception,
        "response_body_logged": False,
    }


def _tool_call(
    url: str,
    request_id: int,
    name: str,
    arguments: dict[str, Any],
    *,
    protocol_version: str,
    timeout: float,
) -> dict[str, Any]:
    return _request(
        url,
        method="POST",
        body=_rpc(
            "tools/call",
            request_id=request_id,
            params={"name": name, "arguments": arguments},
        ),
        protocol_version=protocol_version,
        timeout=timeout,
    )


def verify(
    url: str,
    *,
    concurrency: int,
    protocol_version: str = PROTOCOL_VERSION,
    request_timeout: float = 30.0,
) -> dict[str, Any]:
    url = url.rstrip("/")
    report: dict[str, Any] = {
        "schema": "ncs_remote_mcp_transport_verification_v1",
        "url": url,
        "protocol_version": protocol_version,
        "checks": {},
    }
    checks: dict[str, Any] = report["checks"]

    get_responses = {
        "plain_get": _request(url, method="GET", accept="*/*", timeout=request_timeout),
        "sse_get": _request(
            url,
            method="GET",
            accept="text/event-stream",
            timeout=request_timeout,
        ),
        "json_get": _request(
            url,
            method="GET",
            accept="application/json",
            timeout=request_timeout,
        ),
        "warm_sse_get": _request(
            url,
            method="GET",
            accept="text/event-stream",
            timeout=request_timeout,
        ),
    }
    for name, response in get_responses.items():
        checks[name] = _safe_response(response)

    initialize_response = _request(
        url,
        method="POST",
        body=_rpc(
            "initialize",
            request_id=1,
            params={
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "ncs-vercel-verifier", "version": "1.0"},
            },
        ),
        timeout=request_timeout,
    )
    initialize_payload = _response_payload(initialize_response)
    initialize_result = (
        initialize_payload.get("result") if isinstance(initialize_payload, dict) else None
    )
    server_info = (
        initialize_result.get("serverInfo")
        if isinstance(initialize_result, dict)
        and isinstance(initialize_result.get("serverInfo"), dict)
        else {}
    )
    checks["initialize"] = {
        **_safe_response(initialize_response),
        "jsonrpc_error": bool(
            isinstance(initialize_payload, dict)
            and isinstance(initialize_payload.get("error"), dict)
        ),
        "selected_protocol_version": (
            initialize_result.get("protocolVersion")
            if isinstance(initialize_result, dict)
            else None
        ),
        "server_name": server_info.get("name"),
        "server_version": server_info.get("version"),
        "build_identifier_present": bool(
            re.fullmatch(
                r"[^+]+\+(?:git|deploy|snapshot)\.[0-9A-Za-z.-]+",
                str(server_info.get("version") or ""),
            )
        ),
        "raw_exception_detected": _contains_raw_exception(initialize_payload),
        "response_body_logged": False,
    }

    initialized_response = _request(
        url,
        method="POST",
        body=_rpc("notifications/initialized"),
        protocol_version=protocol_version,
        timeout=request_timeout,
    )
    checks["initialized_notification"] = _safe_response(initialized_response)

    tools_list_response = _request(
        url,
        method="POST",
        body=_rpc("tools/list", request_id=2),
        protocol_version=protocol_version,
        timeout=request_timeout,
    )
    tools_list_payload = _response_payload(tools_list_response)
    tools_list_result = (
        tools_list_payload.get("result") if isinstance(tools_list_payload, dict) else None
    )
    listed_names = {
        str(tool.get("name"))
        for tool in (
            tools_list_result.get("tools", []) if isinstance(tools_list_result, dict) else []
        )
        if isinstance(tool, dict) and tool.get("name")
    }
    checks["tools_list"] = {
        **_safe_response(tools_list_response),
        "jsonrpc_error": bool(
            isinstance(tools_list_payload, dict)
            and isinstance(tools_list_payload.get("error"), dict)
        ),
        "tool_names": sorted(listed_names),
        "expected_tool_names": sorted(EXPECTED_PUBLIC_TOOLS),
        "missing_tool_names": sorted(EXPECTED_PUBLIC_TOOLS - listed_names),
        "unexpected_tool_names": sorted(listed_names - EXPECTED_PUBLIC_TOOLS),
        "raw_exception_detected": _contains_raw_exception(tools_list_payload),
        "response_body_logged": False,
    }

    discovered_unit_code: str | None = None
    for request_id, (check_name, tool_name, arguments) in enumerate(
        TOOL_SMOKE_CALLS,
        start=10,
    ):
        resolved_arguments = _resolve_smoke_arguments(
            arguments,
            discovered_unit_code=discovered_unit_code,
        )
        response = _tool_call(
            url,
            request_id,
            tool_name,
            resolved_arguments,
            protocol_version=protocol_version,
            timeout=request_timeout,
        )
        if check_name == "search":
            discovered_unit_code = _first_unit_code(
                _tool_content_payload(_response_payload(response))
            )
        checks[f"tools_call_{check_name}"] = {
            "tool_name": tool_name,
            **_tool_call_assessment(response),
        }

    checks["unsupported_protocol"] = _safe_response(_request(
        url,
        method="POST",
        body=_rpc("tools/list", request_id=100),
        protocol_version="2099-01-01",
        timeout=request_timeout,
    ))
    checks["invalid_json"] = _safe_response(
        _request(url, method="POST", body=b"{not-json", timeout=request_timeout)
    )
    checks["unsupported_method"] = _safe_response(
        _request(url, method="PUT", timeout=request_timeout)
    )

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        concurrent_results = list(
            executor.map(
                lambda _index: _request(
                    url,
                    method="GET",
                    accept="text/event-stream",
                    timeout=request_timeout,
                ),
                range(concurrency),
            )
        )
    checks["concurrent_gets"] = {
        "request_count": concurrency,
        "statuses": [item["status"] for item in concurrent_results],
        "max_duration_seconds": max(item["duration_seconds"] for item in concurrent_results),
        "wall_duration_seconds": round(time.perf_counter() - started, 6),
    }

    failures: list[str] = []
    for name in ("plain_get", "sse_get", "json_get", "warm_sse_get"):
        item = checks[name]
        if item["status"] != 405 or item.get("allow") != "POST":
            failures.append(name)
        if item["duration_seconds"] >= 10:
            failures.append(f"{name}_duration")
    initialize = checks["initialize"]
    if initialize["status"] != 200 or initialize["jsonrpc_error"]:
        failures.append("initialize")
    elif initialize.get("selected_protocol_version") != protocol_version:
        failures.append("initialize_protocol")
    if not initialize.get("build_identifier_present"):
        failures.append("initialize_build_identifier")
    if initialize["raw_exception_detected"]:
        failures.append("initialize_raw_exception")
    if checks["initialized_notification"]["status"] != 202:
        failures.append("initialized_notification")
    if checks["tools_list"]["status"] != 200 or checks["tools_list"]["jsonrpc_error"]:
        failures.append("tools_list")
    for missing in checks["tools_list"]["missing_tool_names"]:
        failures.append(f"tools_list_missing_{missing}")
    for unexpected in checks["tools_list"]["unexpected_tool_names"]:
        failures.append(f"tools_list_unexpected_{unexpected}")
    if checks["tools_list"]["raw_exception_detected"]:
        failures.append("tools_list_raw_exception")
    if discovered_unit_code is None:
        failures.append("smoke_unit_discovery")
    for check_name, _tool_name, _arguments in TOOL_SMOKE_CALLS:
        item = checks[f"tools_call_{check_name}"]
        failure_prefix = f"tools_call_{check_name}"
        if item["status"] != 200:
            failures.append(f"{failure_prefix}_http")
        if item["jsonrpc_error"]:
            failures.append(f"{failure_prefix}_jsonrpc")
        if item["tool_result_is_error"]:
            failures.append(f"{failure_prefix}_tool_error")
        if not item["semantic_ok"]:
            failures.append(f"{failure_prefix}_semantic")
        if item["raw_exception_detected"]:
            failures.append(f"{failure_prefix}_raw_exception")
        if check_name == "analysis_job_base":
            if (
                not isinstance(item.get("payload_chars"), int)
                or item["payload_chars"] > 2_000
            ):
                failures.append(f"{failure_prefix}_payload_size")
            if item["duration_seconds"] >= 1.0:
                failures.append(f"{failure_prefix}_duration")
    if checks["unsupported_protocol"]["status"] != 400:
        failures.append("unsupported_protocol")
    if checks["invalid_json"]["status"] != 400:
        failures.append("invalid_json")
    if checks["unsupported_method"]["status"] != 405:
        failures.append("unsupported_method")
    if checks["concurrent_gets"]["statuses"] != [405] * concurrency:
        failures.append("concurrent_gets")

    report["failures"] = failures
    report["tool_smoke"] = {
        "expected_public_tool_count": len(EXPECTED_PUBLIC_TOOLS),
        "listed_public_tool_count": len(listed_names),
        "tools_call_count": len(TOOL_SMOKE_CALLS),
        "analysis_modes_checked": ["career_path", "qualification", "job_base", "ontology"],
        "smoke_unit_discovered": discovered_unit_code is not None,
        "all_response_bodies_redacted": True,
        "session_id_values_logged": False,
    }
    report["ok"] = not failures
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deployed stateless Streamable HTTP MCP endpoint.")
    parser.add_argument("url", help="Full MCP URL, for example https://example.vercel.app/api/mcp")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--protocol-version", default=PROTOCOL_VERSION)
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=30.0,
        help="Per-request timeout in seconds (clamped to 1-60).",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = verify(
        args.url,
        concurrency=max(1, min(50, args.concurrency)),
        protocol_version=args.protocol_version,
        request_timeout=max(1.0, min(60.0, args.request_timeout)),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
