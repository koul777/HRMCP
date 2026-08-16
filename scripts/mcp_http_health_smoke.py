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
    error = ""
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
            stdout, stderr = process.communicate(timeout=3) if process.poll() is not None else ("", "")
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "health_timeout",
                        "last_error": error,
                        "returncode": process.poll(),
                        "stdout_tail": stdout[-1000:],
                        "stderr_tail": stderr[-1000:],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        payload_text = json.dumps(payload, ensure_ascii=False)
        ready_payload = fetch_json(ready_url, timeout=2)
        ok = (
            payload.get("status") == "ok"
            and payload.get("endpoint") == "/mcp"
            and payload.get("tools", {}).get("exposed") == expected_tools
            and payload.get("runtime", {}).get("database", {}).get("ready") is True
            and ready_payload.get("status") == "ready"
            and secret not in payload_text
            and payload.get("runtime", {}).get("api_keys", {}).get("service_key_present") is True
        )
        print(
            json.dumps(
                {
                    "ok": ok,
                    "url": url,
                    "status": payload.get("status"),
                    "endpoint": payload.get("endpoint"),
                    "tool_count": payload.get("tools", {}).get("exposed"),
                    "expected_tool_count": expected_tools,
                    "operator_tools_enabled": payload.get("runtime", {}).get("operator_tools_enabled"),
                    "database_ready": payload.get("runtime", {}).get("database", {}).get("ready"),
                    "ready_status": ready_payload.get("status"),
                    "secret_leaked": secret in payload_text,
                    "service_key_present": payload.get("runtime", {})
                    .get("api_keys", {})
                    .get("service_key_present"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0 if ok else 1
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        temp_dir.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
