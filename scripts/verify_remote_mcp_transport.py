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
STRUCTURED_CONTENT_FORBIDDEN_TOOLS = {
    "ncs_analysis",
    "ncs_search",
    "ncs_training",
    "ncs_unit_detail",
    "recommend_training_for_task",
}
MARKDOWN_FOOTER_REQUIRED_TOOLS = {
    "ncs_analysis",
    "ncs_search",
    "ncs_training",
    "ncs_unit_detail",
}
RAW_EXCEPTION_MARKERS = (
    "error executing tool",
    "traceback (most recent call last)",
    "operationalerror",
    "no such table:",
    "python exception",
)
PUBLIC_SOURCE_FOOTER = (
    "출처: 한국산업인력공단 NCS (공공데이터포털). 표준 원문: ncs.go.kr"
)
NOT_FOUND_MARKER = "[NOT_FOUND]"
MARKDOWN_ERROR_PREFIX = "오류 코드:"
SMOKE_UNIT_QUERY = "인사기획"
SMOKE_UNIT_CODE_PLACEHOLDER = "__DISCOVERED_SMOKE_UNIT_CODE__"
SMOKE_QUALIFICATION_SEARCH_QUERY_PLACEHOLDER = (
    "__DISCOVERED_QUALIFICATION_SUMMARY_UNIT_CODE__"
)
SMOKE_QUALIFICATION_UNIT_CODE_PLACEHOLDER = (
    "__DISCOVERED_SMOKE_QUALIFICATION_UNIT_CODE__"
)
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
        "analysis_qualification_summary",
        "ncs_analysis",
        {"mode": "qualification", "limit": 1},
    ),
    (
        "qualification_unit_search",
        "ncs_search",
        {
            "query": SMOKE_QUALIFICATION_SEARCH_QUERY_PLACEHOLDER,
            "scope": "unit",
            "limit": 1,
        },
    ),
    (
        "analysis_qualification",
        "ncs_analysis",
        {
            "mode": "qualification",
            "unit_code": SMOKE_QUALIFICATION_UNIT_CODE_PLACEHOLDER,
            "limit": 1,
        },
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
TOOL_SMOKE_REQUIRED_NONEMPTY_FIELDS = {
    "analysis_qualification_summary": "qualification_links",
    "analysis_qualification": "qualification_links",
}


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


def _tool_text_content(rpc_payload: Any) -> str | None:
    """Return private tools/call text content without adding it to reports."""
    if not isinstance(rpc_payload, dict):
        return None
    result = rpc_payload.get("result")
    if not isinstance(result, dict):
        return None
    parts: list[str] = []
    for block in result.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts) or None


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


def _markdown_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not (stripped.startswith("|") and stripped.endswith("|")):
        return []
    return [
        cell.replace(r"\|", "|").strip()
        for cell in re.split(r"(?<!\\)\|", stripped[1:-1])
    ]


def _is_markdown_separator(cells: list[str]) -> bool:
    return bool(cells) and all(
        re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) is not None
        for cell in cells
    )


def _markdown_tables(text: str | None) -> list[tuple[list[str], list[list[str]]]]:
    if not text:
        return []
    lines = text.splitlines()
    tables: list[tuple[list[str], list[list[str]]]] = []
    index = 0
    while index + 1 < len(lines):
        headers = _markdown_cells(lines[index])
        separator = _markdown_cells(lines[index + 1])
        if (
            not headers
            or len(separator) != len(headers)
            or not _is_markdown_separator(separator)
        ):
            index += 1
            continue
        rows: list[list[str]] = []
        cursor = index + 2
        while cursor < len(lines):
            cells = _markdown_cells(lines[cursor])
            if len(cells) != len(headers):
                break
            rows.append(cells)
            cursor += 1
        tables.append((headers, rows))
        index = cursor
    return tables


def _clean_markdown_identifier(value: str) -> str | None:
    candidate = value.strip().strip("`").strip()
    if candidate in {"", "-"}:
        return None
    if re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z._-]{2,63}", candidate) is None:
        return None
    return candidate


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
        if value:
            unit_code = _clean_markdown_identifier(str(value))
            if unit_code:
                return unit_code
    return None


def _first_unit_code_from_response(rpc_payload: Any) -> str | None:
    legacy_code = _first_unit_code(_tool_content_payload(rpc_payload))
    if legacy_code:
        return legacy_code
    text = _tool_text_content(rpc_payload)
    for headers, rows in _markdown_tables(text):
        normalized_headers = [header.casefold().replace(" ", "") for header in headers]
        try:
            unit_code_index = normalized_headers.index("능력단위코드")
        except ValueError:
            try:
                unit_code_index = normalized_headers.index("unit_code")
            except ValueError:
                continue
        for row in rows:
            unit_code = _clean_markdown_identifier(row[unit_code_index])
            if unit_code:
                return unit_code
    return None


def _first_qualification_unit_code_from_response(rpc_payload: Any) -> str | None:
    tool_payload = _tool_content_payload(rpc_payload)
    if isinstance(tool_payload, dict):
        rows = tool_payload.get("qualification_links")
        data = tool_payload.get("data")
        if not isinstance(rows, list) and isinstance(data, dict):
            rows = data.get("qualification_links")
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, dict) or not row.get("unit_code"):
                    continue
                unit_code = _clean_markdown_identifier(str(row["unit_code"]))
                if unit_code:
                    return unit_code
    return _first_unit_code_from_response(rpc_payload)


def _markdown_qualification_data_present(text: str | None) -> bool:
    if not text or "## 자격 연계 분석" not in text:
        return False
    count_present = any(
        int(match.group("total")) > 0 and int(match.group("returned")) > 0
        for match in re.finditer(
            r"(?P<total>\d+)건\s+중\s+(?P<returned>\d+)건\s+표시",
            text,
        )
    )
    if not count_present:
        return False
    for headers, rows in _markdown_tables(text):
        normalized_headers = [header.casefold().replace(" ", "") for header in headers]
        identity_headers = ("자격코드", "자격명", "능력단위코드")
        if not set(identity_headers).issubset(normalized_headers):
            continue
        identity_indexes = [normalized_headers.index(header) for header in identity_headers]
        if any(
            any(row[index].strip().strip("`") not in {"", "-"} for index in identity_indexes)
            for row in rows
        ):
            return True
    return False


def _resolve_smoke_arguments(
    arguments: dict[str, Any],
    *,
    discovered_unit_code: str | None,
    qualification_summary_unit_code: str | None,
    discovered_qualification_unit_code: str | None,
) -> dict[str, Any]:
    resolved = dict(arguments)
    if resolved.get("unit_code") == SMOKE_UNIT_CODE_PLACEHOLDER:
        resolved["unit_code"] = discovered_unit_code or ""
    if resolved.get("query") == SMOKE_QUALIFICATION_SEARCH_QUERY_PLACEHOLDER:
        resolved["query"] = qualification_summary_unit_code or ""
    if resolved.get("unit_code") == SMOKE_QUALIFICATION_UNIT_CODE_PLACEHOLDER:
        resolved["unit_code"] = discovered_qualification_unit_code or ""
    return resolved


def _tool_call_assessment(
    response: dict[str, Any],
    *,
    expected_nonempty_field: str | None = None,
    structured_content_forbidden: bool = True,
    markdown_footer_required: bool = True,
) -> dict[str, Any]:
    rpc_payload = _response_payload(response)
    rpc_error = isinstance(rpc_payload, dict) and isinstance(rpc_payload.get("error"), dict)
    rpc_result = rpc_payload.get("result") if isinstance(rpc_payload, dict) else None
    tool_is_error = isinstance(rpc_result, dict) and rpc_result.get("isError") is True
    tool_is_explicit_success = (
        isinstance(rpc_result, dict) and rpc_result.get("isError") is False
    )
    structured_content_present = (
        isinstance(rpc_result, dict) and "structuredContent" in rpc_result
    )
    text_content = _tool_text_content(rpc_payload)
    tool_payload = _tool_content_payload(rpc_payload)
    raw_exception = _contains_raw_exception(rpc_payload)
    not_found_detected = bool(text_content and NOT_FOUND_MARKER in text_content)
    markdown_error_detected = bool(
        text_content and text_content.lstrip().startswith(MARKDOWN_ERROR_PREFIX)
    )
    source_footer_present = bool(
        text_content and text_content.rstrip().endswith(PUBLIC_SOURCE_FOOTER)
    )
    legacy_json_ok = isinstance(tool_payload, dict) and tool_payload.get("ok") is True
    markdown_ok = bool(
        text_content
        and tool_is_explicit_success
        and (source_footer_present or not markdown_footer_required)
        and not not_found_detected
        and not markdown_error_detected
    )
    semantic_ok = bool(
        not rpc_error
        and not tool_is_error
        and not (structured_content_forbidden and structured_content_present)
        and not raw_exception
        and not not_found_detected
        and (legacy_json_ok or markdown_ok)
    )
    payload_chars = (
        len(text_content)
        if text_content is not None
        else (
            len(json.dumps(tool_payload, ensure_ascii=False, separators=(",", ":")))
            if isinstance(tool_payload, dict)
            else None
        )
    )
    expected_value: Any = None
    if isinstance(tool_payload, dict) and expected_nonempty_field:
        expected_value = tool_payload.get(expected_nonempty_field)
        if expected_value is None and isinstance(tool_payload.get("data"), dict):
            expected_value = tool_payload["data"].get(expected_nonempty_field)
    expected_data_present: bool | None = None
    if expected_nonempty_field:
        expected_data_present = isinstance(expected_value, list) and len(expected_value) > 0
        if (
            not expected_data_present
            and expected_nonempty_field == "qualification_links"
        ):
            expected_data_present = _markdown_qualification_data_present(text_content)
    response_format = (
        "json_text"
        if legacy_json_ok and text_content is not None
        else "markdown"
        if text_content is not None
        else "structured"
        if structured_content_present
        else "unknown"
    )
    return {
        **_safe_response(response),
        "jsonrpc_error": rpc_error,
        "tool_result_is_error": tool_is_error,
        "semantic_ok": semantic_ok,
        "payload_chars": payload_chars,
        "raw_exception_detected": raw_exception,
        "not_found_detected": not_found_detected,
        "markdown_error_detected": markdown_error_detected,
        "source_footer_present": source_footer_present,
        "markdown_footer_required": markdown_footer_required,
        "structured_content_present": structured_content_present,
        "structured_content_forbidden": structured_content_forbidden,
        "response_format": response_format,
        "expected_nonempty_field": expected_nonempty_field,
        "expected_data_present": expected_data_present,
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
    qualification_summary_unit_code: str | None = None
    discovered_qualification_unit_code: str | None = None
    qualification_search_chained_from_summary = False
    qualification_unit_chained_from_search = False
    for request_id, (check_name, tool_name, arguments) in enumerate(
        TOOL_SMOKE_CALLS,
        start=10,
    ):
        resolved_arguments = _resolve_smoke_arguments(
            arguments,
            discovered_unit_code=discovered_unit_code,
            qualification_summary_unit_code=qualification_summary_unit_code,
            discovered_qualification_unit_code=discovered_qualification_unit_code,
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
            discovered_unit_code = _first_unit_code_from_response(
                _response_payload(response)
            )
        elif check_name == "analysis_qualification_summary":
            qualification_summary_unit_code = (
                _first_qualification_unit_code_from_response(
                    _response_payload(response)
                )
            )
        elif check_name == "qualification_unit_search":
            qualification_search_chained_from_summary = bool(
                qualification_summary_unit_code
                and resolved_arguments.get("query")
                == qualification_summary_unit_code
            )
            discovered_qualification_unit_code = _first_unit_code_from_response(
                _response_payload(response)
            )
        elif check_name == "analysis_qualification":
            qualification_unit_chained_from_search = bool(
                discovered_qualification_unit_code
                and resolved_arguments.get("unit_code")
                == discovered_qualification_unit_code
            )
        check = {
            "tool_name": tool_name,
            **_tool_call_assessment(
                response,
                expected_nonempty_field=TOOL_SMOKE_REQUIRED_NONEMPTY_FIELDS.get(
                    check_name
                ),
                structured_content_forbidden=(
                    tool_name in STRUCTURED_CONTENT_FORBIDDEN_TOOLS
                ),
                markdown_footer_required=(
                    tool_name in MARKDOWN_FOOTER_REQUIRED_TOOLS
                ),
            ),
        }
        if check_name == "analysis_qualification":
            check.update(
                {
                    "unit_code_argument_present": bool(
                        resolved_arguments.get("unit_code")
                    ),
                    "unit_code_source": "tools_call_qualification_unit_search",
                    "unit_code_matches_search_result": (
                        qualification_unit_chained_from_search
                    ),
                    "unit_code_value_logged": False,
                }
            )
        checks[f"tools_call_{check_name}"] = check

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
    if qualification_summary_unit_code is None:
        failures.append("qualification_summary_unit_discovery")
    if not qualification_search_chained_from_summary:
        failures.append("qualification_summary_to_search_chaining")
    if discovered_qualification_unit_code is None:
        failures.append("qualification_smoke_unit_discovery")
    if not qualification_unit_chained_from_search:
        failures.append("qualification_smoke_unit_chaining")
    for check_name, _tool_name, _arguments in TOOL_SMOKE_CALLS:
        item = checks[f"tools_call_{check_name}"]
        failure_prefix = f"tools_call_{check_name}"
        if item["status"] != 200:
            failures.append(f"{failure_prefix}_http")
        if item["jsonrpc_error"]:
            failures.append(f"{failure_prefix}_jsonrpc")
        if item["tool_result_is_error"]:
            failures.append(f"{failure_prefix}_tool_error")
        if item["structured_content_forbidden"] and item["structured_content_present"]:
            failures.append(f"{failure_prefix}_structured_content")
        if not item["semantic_ok"]:
            failures.append(f"{failure_prefix}_semantic")
        if item["raw_exception_detected"]:
            failures.append(f"{failure_prefix}_raw_exception")
        if item.get("expected_nonempty_field") and not item.get(
            "expected_data_present"
        ):
            failures.append(f"{failure_prefix}_empty")
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
        "qualification_unit_search_strategy": (
            "broad_qualification_to_exact_unit_code_search"
        ),
        "qualification_summary_unit_discovered": (
            qualification_summary_unit_code is not None
        ),
        "qualification_search_chained_from_summary": (
            qualification_search_chained_from_summary
        ),
        "qualification_unit_discovered": (
            discovered_qualification_unit_code is not None
        ),
        "qualification_unit_chained_from_search": (
            qualification_unit_chained_from_search
        ),
        "qualification_unit_code_value_logged": False,
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
