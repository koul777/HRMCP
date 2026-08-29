from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = "2025-11-25"


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
            result["payload"] = json.loads(raw_body)
        except json.JSONDecodeError:
            result["body_preview"] = raw_body[:500].decode("utf-8", errors="replace")
    return result


def _tool_call(url: str, request_id: int, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return _request(
        url,
        method="POST",
        body=_rpc(
            "tools/call",
            request_id=request_id,
            params={"name": name, "arguments": arguments},
        ),
        protocol_version=PROTOCOL_VERSION,
    )


def verify(url: str, *, concurrency: int) -> dict[str, Any]:
    url = url.rstrip("/")
    report: dict[str, Any] = {
        "schema": "ncs_remote_mcp_transport_verification_v1",
        "url": url,
        "protocol_version": PROTOCOL_VERSION,
        "checks": {},
    }
    checks = report["checks"]

    checks["plain_get"] = _request(url, method="GET", accept="*/*")
    checks["sse_get"] = _request(url, method="GET", accept="text/event-stream")
    checks["json_get"] = _request(url, method="GET", accept="application/json")
    checks["warm_sse_get"] = _request(url, method="GET", accept="text/event-stream")

    checks["initialize"] = _request(
        url,
        method="POST",
        body=_rpc(
            "initialize",
            request_id=1,
            params={
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "ncs-vercel-verifier", "version": "1.0"},
            },
        ),
    )
    checks["initialized_notification"] = _request(
        url,
        method="POST",
        body=_rpc("notifications/initialized"),
        protocol_version=PROTOCOL_VERSION,
    )
    checks["tools_list"] = _request(
        url,
        method="POST",
        body=_rpc("tools/list", request_id=2),
        protocol_version=PROTOCOL_VERSION,
    )
    checks["tools_call_discover"] = _tool_call(
        url,
        3,
        "ncs_discover_tools",
        {"intent": "인사기획 직무기술서 작성"},
    )
    checks["tools_call_search"] = _tool_call(
        url,
        4,
        "ncs_search",
        {"query": "인사기획", "scope": "all", "limit": 3},
    )
    checks["tools_call_ontology"] = _tool_call(
        url,
        5,
        "ncs_analysis",
        {"mode": "ontology", "query": "인사기획", "limit": 3},
    )
    checks["unsupported_protocol"] = _request(
        url,
        method="POST",
        body=_rpc("tools/list", request_id=6),
        protocol_version="2099-01-01",
    )
    checks["invalid_json"] = _request(url, method="POST", body=b"{not-json")
    checks["unsupported_method"] = _request(url, method="PUT")

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        concurrent_results = list(
            executor.map(
                lambda _index: _request(url, method="GET", accept="text/event-stream"),
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
    if initialize["status"] != 200:
        failures.append("initialize")
    elif initialize.get("payload", {}).get("result", {}).get("protocolVersion") != PROTOCOL_VERSION:
        failures.append("initialize_protocol")
    if checks["initialized_notification"]["status"] != 202:
        failures.append("initialized_notification")
    if checks["tools_list"]["status"] != 200:
        failures.append("tools_list")
    else:
        names = {
            tool.get("name")
            for tool in checks["tools_list"].get("payload", {}).get("result", {}).get("tools", [])
        }
        checks["tools_list"]["tool_names"] = sorted(name for name in names if name)
        for required in {"ncs_search", "ncs_analysis", "ncs_discover_tools"}:
            if required not in names:
                failures.append(f"tools_list_missing_{required}")
    for name in ("tools_call_discover", "tools_call_search", "tools_call_ontology"):
        item = checks[name]
        if item["status"] != 200 or item.get("payload", {}).get("result", {}).get("isError") is True:
            failures.append(name)
    if checks["unsupported_protocol"]["status"] != 400:
        failures.append("unsupported_protocol")
    if checks["invalid_json"]["status"] != 400:
        failures.append("invalid_json")
    if checks["unsupported_method"]["status"] != 405:
        failures.append("unsupported_method")
    if checks["concurrent_gets"]["statuses"] != [405] * concurrency:
        failures.append("concurrent_gets")

    report["failures"] = failures
    report["ok"] = not failures
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a deployed stateless Streamable HTTP MCP endpoint.")
    parser.add_argument("url", help="Full MCP URL, for example https://example.vercel.app/api/mcp")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = verify(args.url, concurrency=max(1, min(50, args.concurrency)))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
