from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


class VercelApiContractTests(unittest.TestCase):
    def test_mcp_entrypoints_have_byte_parity(self) -> None:
        self.assertEqual(
            (ROOT / "api" / "mcp.py").read_bytes(),
            (ROOT / "deploy" / "vercel_mcp_app" / "api" / "mcp.py").read_bytes(),
        )

    def test_mcp_get_short_circuits_before_mcp_or_database_import(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join((str(SRC), str(ROOT)))
        local_python = ROOT / ".venv" / "Scripts" / "python.exe"
        python_executable = local_python if local_python.exists() else Path(sys.executable)
        completed = subprocess.run(
            [str(python_executable), "-c", _GET_SHORT_CIRCUIT_SCRIPT],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], 405)
        self.assertEqual(result["allow"], "POST")
        self.assertFalse(result["mcp_module_loaded"])
        self.assertFalse(result["server_module_loaded"])

    def test_mcp_initialize_starts_nested_app_lifespan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = sqlite3.connect(db_path)
            for table in (
                "competency_units",
                "performance_criteria",
                "ksa_items",
                "ncs_training_courses",
            ):
                conn.execute(f"CREATE TABLE {table}(value TEXT)")
                conn.execute(f"INSERT INTO {table}(value) VALUES ('ready')")
            conn.commit()
            conn.close()

            env = os.environ.copy()
            env["PYTHONPATH"] = os.pathsep.join((str(SRC), str(ROOT)))
            env["NCS_DB_PATH"] = str(db_path)
            env["NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE"] = "1"
            env["VERCEL"] = "1"
            env["NCS_MCP_READ_ONLY"] = "1"
            env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "0"
            env["NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION"] = "1"
            local_python = ROOT / ".venv" / "Scripts" / "python.exe"
            python_executable = local_python if local_python.exists() else Path(sys.executable)
            completed = subprocess.run(
                [str(python_executable), "-c", _INITIALIZE_SMOKE_SCRIPT],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["payload"]["jsonrpc"], "2.0")
        self.assertEqual(result["payload"]["id"], 1)
        self.assertEqual(result["payload"]["result"]["serverInfo"]["name"], "ncs-mcp")

    def test_mcp_fails_closed_when_vercel_compact_bundle_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = self._vercel_probe_environment(tmp)
            completed = subprocess.run(
                [str(self._python_executable()), "-c", _POST_PROBE_SCRIPT],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["payload"]["error"]["code"], -32603)
        self.assertIn("no verified NCS database snapshot", result["payload"]["error"]["message"])

    def test_mcp_fails_closed_when_vercel_compact_bundle_is_corrupt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_root = Path(tmp) / "package"
            api_root = package_root / "api"
            api_root.mkdir(parents=True)
            (api_root / "__init__.py").write_text("", encoding="utf-8")
            (api_root / "mcp.py").write_text(
                (ROOT / "api" / "mcp.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (api_root / "ncs_ontology_compact.zip").write_bytes(b"not a zip archive")
            (api_root / "ncs_ontology_compact.manifest.json").write_text(
                "{}", encoding="utf-8"
            )
            env = self._vercel_probe_environment(tmp, package_root=package_root)
            completed = subprocess.run(
                [str(self._python_executable()), "-c", _POST_PROBE_SCRIPT],
                cwd=package_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["payload"]["error"]["code"], -32603)

    @staticmethod
    def _python_executable() -> Path:
        local_python = ROOT / ".venv" / "Scripts" / "python.exe"
        return local_python if local_python.exists() else Path(sys.executable)

    @staticmethod
    def _vercel_probe_environment(tmp: str, *, package_root: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        import_root = package_root or ROOT
        env["PYTHONPATH"] = os.pathsep.join((str(import_root), str(SRC), str(ROOT)))
        env["NCS_DB_PATH"] = str(Path(tmp) / "missing.db")
        env["NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE"] = "0"
        env["NCS_MCP_READ_ONLY"] = "1"
        env["VERCEL"] = "1"
        env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "0"
        env["NCS_MCP_ENABLE_ADVANCED_TOOLS"] = "0"
        env["NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION"] = "1"
        env.pop("NCS_DB_URL", None)
        env.pop("NCS_MCP_READINESS_EXTRA_TABLES", None)
        env.pop("NCS_MCP_READINESS_MIN_ROWS", None)
        return env


_INITIALIZE_SMOKE_SCRIPT = r'''
import asyncio
import json

from api.index import app


async def main():
    request_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "vercel-contract-test", "version": "1.0"},
        },
    }
    request_body = json.dumps(request_payload).encode("utf-8")
    request_sent = False
    response_complete = asyncio.Event()
    sent = []

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/mcp",
            "raw_path": b"/api/mcp",
            "query_string": b"",
            "headers": [
                (b"host", b"ncs.example"),
                (b"content-type", b"application/json"),
                (b"accept", b"application/json, text/event-stream"),
                (b"content-length", str(len(request_body)).encode("ascii")),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("ncs.example", 443),
        },
        receive,
        send,
    )

    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    print(json.dumps({"status": status, "payload": json.loads(body)}))


asyncio.run(main())
'''


_POST_PROBE_SCRIPT = r'''
import asyncio
import json

from api.mcp import app


async def main():
    request_body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "vercel-failfast-test", "version": "1.0"},
        },
    }).encode("utf-8")
    request_sent = False
    response_complete = asyncio.Event()
    sent = []

    async def receive():
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/mcp",
            "raw_path": b"/api/mcp",
            "query_string": b"",
            "headers": [(b"host", b"ncs.example"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("ncs.example", 443),
        },
        receive,
        send,
    )
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    print(json.dumps({"status": status, "payload": json.loads(body)}))


asyncio.run(main())
'''


_GET_SHORT_CIRCUIT_SCRIPT = r'''
import asyncio
import json
import sys

from api.index import app


async def main():
    sent = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/api/mcp",
            "raw_path": b"/api/mcp",
            "query_string": b"",
            "headers": [(b"host", b"ncs.example")],
            "client": ("127.0.0.1", 50000),
            "server": ("ncs.example", 443),
        },
        receive,
        send,
    )

    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    headers = dict(next(message["headers"] for message in sent if message["type"] == "http.response.start"))
    print(
        json.dumps(
            {
                "status": status,
                "allow": headers[b"allow"].decode("ascii"),
                "mcp_module_loaded": "api.mcp" in sys.modules,
                "server_module_loaded": "ncs_mcp.server" in sys.modules,
            }
        )
    )


asyncio.run(main())
'''


if __name__ == "__main__":
    unittest.main()
