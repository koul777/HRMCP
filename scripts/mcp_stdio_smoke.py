from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _read_response(process: subprocess.Popen[str], request_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = process.stdout.readline() if process.stdout else ""
        if not line:
            if process.poll() is not None:
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"MCP server exited before response {request_id}: {stderr}")
            time.sleep(0.05)
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("id") == request_id:
            return payload
    raise TimeoutError(f"Timed out waiting for MCP response id={request_id}")


def _send(process: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise RuntimeError("MCP process stdin is not available")
    process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    process.stdin.flush()


def run_stdio_smoke(*, python_executable: str, timeout: float) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    process = subprocess.Popen(
        [python_executable, "-m", "ncs_mcp.server"],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "ncs-mcp-stdio-smoke", "version": "1.0"},
                },
            },
        )
        init_response = _read_response(process, "init", timeout)
        if "error" in init_response:
            raise RuntimeError(f"initialize failed: {init_response['error']}")
        _send(process, {"jsonrpc": "2.0", "method": "notifications/initialized"})

        _send(process, {"jsonrpc": "2.0", "id": "tools", "method": "tools/list"})
        tools_response = _read_response(process, "tools", timeout)
        if "error" in tools_response:
            raise RuntimeError(f"tools/list failed: {tools_response['error']}")
        tools = tools_response.get("result", {}).get("tools", [])
        tool_names = sorted(tool.get("name") for tool in tools)

        _send(
            process,
            {
                "jsonrpc": "2.0",
                "id": "discover",
                "method": "tools/call",
                "params": {
                    "name": "ncs_discover_tools",
                    "arguments": {"intent": "career transition training recommendation"},
                },
            },
        )
        discover_response = _read_response(process, "discover", timeout)
        if "error" in discover_response:
            raise RuntimeError(f"tools/call ncs_discover_tools failed: {discover_response['error']}")

        required = {"ncs_discover_tools", "ncs_execute_tool", "recommend_training_transition"}
        missing = sorted(required - set(tool_names))
        return {
            "ok": not missing,
            "tool_count": len(tool_names),
            "missing_required_tools": missing,
            "tool_names": tool_names,
            "discover_result_present": bool(discover_response.get("result")),
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an MCP STDIO protocol smoke test.")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    result = run_stdio_smoke(python_executable=args.python, timeout=args.timeout)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()

