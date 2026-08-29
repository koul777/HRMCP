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


class VercelMcpTransportTests(unittest.TestCase):
    def test_get_terminates_and_post_contract_remains_available(self) -> None:
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
            env["NCS_MCP_READ_ONLY"] = "1"
            env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "0"
            env["NCS_MCP_ENABLE_ADVANCED_TOOLS"] = "0"
            env["NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION"] = "1"
            local_python = ROOT / ".venv" / "Scripts" / "python.exe"
            python_executable = local_python if local_python.exists() else Path(sys.executable)
            completed = subprocess.run(
                [str(python_executable), "-c", _TRANSPORT_CONTRACT_SCRIPT],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        result = json.loads(completed.stdout)

        for name in ("plain_get", "sse_get", "json_get"):
            response = result[name]
            self.assertEqual(response["status"], 405, response)
            self.assertEqual(response["headers"]["allow"], "POST")
            self.assertEqual(response["headers"]["content-type"], "application/json")
            self.assertLess(response["duration_seconds"], 1.0, response)
            self.assertFalse(response["stream_open"], response)
            self.assertEqual(response["receive_calls"], 0, response)
        self.assertFalse(result["get_imported_mcp_module"])

        self.assertEqual(result["initialize"]["status"], 200)
        self.assertEqual(result["initialize"]["payload"]["result"]["protocolVersion"], "2025-11-25")
        self.assertEqual(result["initialized_notification"]["status"], 202)
        self.assertEqual(result["tools_list"]["status"], 200)
        self.assertIn(
            "ncs_discover_tools",
            [tool["name"] for tool in result["tools_list"]["payload"]["result"]["tools"]],
        )
        self.assertEqual(result["tools_call"]["status"], 200)
        self.assertFalse(result["tools_call"]["payload"]["result"].get("isError", False))
        self.assertEqual(result["unsupported_protocol"]["status"], 400)
        self.assertEqual(result["invalid_json"]["status"], 400)
        self.assertEqual(result["unsupported_method"]["status"], 405)
        self.assertEqual(result["concurrent_gets"]["statuses"], [405] * 20)
        self.assertLess(result["concurrent_gets"]["duration_seconds"], 1.0)
        self.assertLessEqual(result["concurrent_gets"]["persistent_task_delta"], 0)


_TRANSPORT_CONTRACT_SCRIPT = r'''
import asyncio
import json
import sys
import time

from api.index import app


def rpc(method, *, request_id=None, params=None):
    payload = {"jsonrpc": "2.0", "method": method}
    if request_id is not None:
        payload["id"] = request_id
    if params is not None:
        payload["params"] = params
    return json.dumps(payload).encode("utf-8")


async def request(method, *, body=b"", accept="application/json, text/event-stream", protocol=None):
    request_sent = False
    response_complete = asyncio.Event()
    sent = []
    receive_calls = 0

    async def receive():
        nonlocal request_sent, receive_calls
        receive_calls += 1
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        await response_complete.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            response_complete.set()

    headers = [
        (b"host", b"ncs.example"),
        (b"accept", accept.encode("ascii")),
    ]
    if body:
        headers.extend(
            [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ]
        )
    if protocol:
        headers.append((b"mcp-protocol-version", protocol.encode("ascii")))

    started = time.perf_counter()
    await asyncio.wait_for(
        app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": method,
                "scheme": "https",
                "path": "/api/mcp",
                "raw_path": b"/api/mcp",
                "query_string": b"",
                "headers": headers,
                "client": ("127.0.0.1", 50000),
                "server": ("ncs.example", 443),
            },
            receive,
            send,
        ),
        timeout=5.0,
    )
    duration = time.perf_counter() - started
    start_message = next(item for item in sent if item["type"] == "http.response.start")
    response_headers = {
        key.decode("latin-1"): value.decode("latin-1")
        for key, value in start_message.get("headers", [])
    }
    response_body = b"".join(
        item.get("body", b"") for item in sent if item["type"] == "http.response.body"
    )
    result = {
        "status": start_message["status"],
        "headers": response_headers,
        "duration_seconds": duration,
        "receive_calls": receive_calls,
        "stream_open": any(
            item.get("more_body", False) for item in sent if item["type"] == "http.response.body"
        ),
    }
    if response_body:
        try:
            result["payload"] = json.loads(response_body)
        except json.JSONDecodeError:
            result["body"] = response_body.decode("utf-8", errors="replace")
    return result


async def main():
    results = {}
    results["plain_get"] = await request("GET", accept="*/*")
    results["sse_get"] = await request("GET", accept="text/event-stream")
    results["json_get"] = await request("GET", accept="application/json")
    results["get_imported_mcp_module"] = "api.mcp" in sys.modules

    results["initialize"] = await request(
        "POST",
        body=rpc(
            "initialize",
            request_id=1,
            params={
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "vercel-transport-test", "version": "1.0"},
            },
        ),
    )
    results["initialized_notification"] = await request(
        "POST",
        body=rpc("notifications/initialized"),
        protocol="2025-11-25",
    )
    results["tools_list"] = await request(
        "POST",
        body=rpc("tools/list", request_id=2),
        protocol="2025-11-25",
    )
    results["tools_call"] = await request(
        "POST",
        body=rpc(
            "tools/call",
            request_id=3,
            params={"name": "ncs_discover_tools", "arguments": {"intent": "직무기술서 작성"}},
        ),
        protocol="2025-11-25",
    )
    results["unsupported_protocol"] = await request(
        "POST",
        body=rpc("tools/list", request_id=4),
        protocol="2099-01-01",
    )
    results["invalid_json"] = await request("POST", body=b"{not-json")
    results["unsupported_method"] = await request("PUT")

    tasks_before = len(asyncio.all_tasks())
    started = time.perf_counter()
    concurrent = await asyncio.gather(
        *(request("GET", accept="text/event-stream") for _ in range(20))
    )
    await asyncio.sleep(0)
    results["concurrent_gets"] = {
        "statuses": [item["status"] for item in concurrent],
        "duration_seconds": time.perf_counter() - started,
        "persistent_task_delta": len(asyncio.all_tasks()) - tasks_before,
    }
    print(json.dumps(results, ensure_ascii=False))


asyncio.run(main())
'''


if __name__ == "__main__":
    unittest.main()
