from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONTRACT_PATH = ROOT / "mcp" / "ncs-tool-contract.json"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.smoke_data import create_ready_smoke_db


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def expected_tool_count() -> int:
    if not CONTRACT_PATH.exists():
        return 0
    payload = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return int(payload.get("surface", {}).get("active_tool_count") or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Start NCS MCP over HTTP and verify /health.")
    parser.add_argument("--port", type=int, default=0, help="HTTP port. Defaults to a free local port.")
    parser.add_argument("--timeout", type=float, default=20.0, help="Overall startup timeout in seconds.")
    args = parser.parse_args(argv)

    port = args.port or free_port()
    secret = "http-health-secret-123456"
    expected_tools = expected_tool_count()
    temp_dir = tempfile.TemporaryDirectory()
    db_path = Path(temp_dir.name) / "ncs-smoke.db"
    create_ready_smoke_db(db_path)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SRC)
    env["NCS_DB_PATH"] = str(db_path)
    env["NCS_SERVICE_KEY"] = secret
    env["NCS_MCP_READ_ONLY"] = "1"
    env["NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS"] = "2"
    env.pop("NCS_MCP_ENABLE_OPERATOR_TOOLS", None)

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ncs_mcp.server",
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"http://127.0.0.1:{port}/health"
    ready_url = f"http://127.0.0.1:{port}/ready"
    deadline = time.monotonic() + args.timeout
    payload: dict[str, Any] | None = None
    ready_payload: dict[str, Any] | None = None
    error = ""
    report: dict[str, Any]
    return_code = 1
    server_stdout = ""
    server_stderr = ""
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                payload = fetch_json(url, timeout=2)
                break
            except Exception as exc:  # pragma: no cover - polling branch
                error = str(exc)
                time.sleep(0.25)
        if payload is None:
            report = {
                "ok": False,
                "error": "health_timeout",
                "last_error": error,
                "returncode": process.poll(),
            }
        else:
            ready_payload = fetch_json(ready_url, timeout=2)
            response_text = json.dumps(
                {"health": payload, "ready": ready_payload}, ensure_ascii=False
            )
            response_secret_leaked = secret in response_text
            ok = (
                payload.get("status") == "ok"
                and payload.get("endpoint") == "/mcp"
                and payload.get("tools", {}).get("exposed") == expected_tools
                and payload.get("runtime", {}).get("database", {}).get("ready") is True
                and ready_payload.get("status") == "ready"
                and payload.get("runtime", {}).get("read_only_mode") is True
                and payload.get("runtime", {}).get("max_concurrent_recommendations") == 2
                and not response_secret_leaked
                and payload.get("runtime", {}).get("api_keys", {}).get("service_key_present") is True
            )
            report = {
                "ok": ok,
                "url": url,
                "status": payload.get("status"),
                "endpoint": payload.get("endpoint"),
                "tool_count": payload.get("tools", {}).get("exposed"),
                "expected_tool_count": expected_tools,
                "operator_tools_enabled": payload.get("runtime", {}).get("operator_tools_enabled"),
                "database_ready": payload.get("runtime", {}).get("database", {}).get("ready"),
                "ready_status": ready_payload.get("status"),
                "read_only_mode": payload.get("runtime", {}).get("read_only_mode"),
                "max_concurrent_recommendations": payload.get("runtime", {}).get(
                    "max_concurrent_recommendations"
                ),
                "response_secret_leaked": response_secret_leaked,
                "service_key_present": payload.get("runtime", {})
                .get("api_keys", {})
                .get("service_key_present"),
            }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        server_stdout, server_stderr = process.communicate()
        temp_dir.cleanup()

    output_secret_leaked = secret in server_stdout or secret in server_stderr
    report["output_secret_leaked"] = output_secret_leaked
    report["secret_leaked"] = bool(
        report.get("response_secret_leaked") or output_secret_leaked
    )
    if output_secret_leaked:
        report["ok"] = False
    if not report.get("ok"):
        report["stdout_tail"] = server_stdout[-1000:].replace(secret, "[REDACTED]")
        report["stderr_tail"] = server_stderr[-1000:].replace(secret, "[REDACTED]")
    return_code = 0 if report.get("ok") else 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
