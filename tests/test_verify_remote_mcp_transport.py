from __future__ import annotations

import json
import threading
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

from scripts import verify_remote_mcp_transport as verifier


ROOT = Path(__file__).resolve().parents[1]


def _successful_request_factory(
    *,
    selected_protocol: str,
) -> tuple[
    Callable[..., dict[str, Any]],
    list[str | None],
    list[tuple[str, dict[str, Any]]],
]:
    observed_protocols: list[str | None] = []
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    lock = threading.Lock()

    def fake_request(
        _url: str,
        *,
        method: str,
        body: bytes | None = None,
        accept: str = "application/json, text/event-stream",
        protocol_version: str | None = None,
        timeout: float = 35.0,
    ) -> dict[str, Any]:
        del accept, timeout
        with lock:
            observed_protocols.append(protocol_version)
        if method == "GET":
            return {"status": 405, "allow": "POST", "duration_seconds": 0.01}
        if method == "PUT":
            return {"status": 405, "duration_seconds": 0.01}
        if body == b"{not-json":
            return {"status": 400, "duration_seconds": 0.01}

        payload = json.loads(body or b"{}")
        rpc_method = payload.get("method")
        if protocol_version == "2099-01-01":
            return {"status": 400, "duration_seconds": 0.01}
        if rpc_method == "initialize":
            return {
                "status": 200,
                "duration_seconds": 0.01,
                "payload": {"result": {"protocolVersion": selected_protocol}},
            }
        if rpc_method == "notifications/initialized":
            return {"status": 202, "duration_seconds": 0.01}
        if rpc_method == "tools/list":
            return {
                "status": 200,
                "duration_seconds": 0.01,
                "payload": {
                    "result": {
                        "tools": [
                            {"name": name} for name in sorted(verifier.EXPECTED_PUBLIC_TOOLS)
                        ]
                    }
                },
            }
        if rpc_method == "tools/call":
            params = payload["params"]
            name = str(params["name"])
            arguments = dict(params.get("arguments") or {})
            with lock:
                tool_calls.append((name, arguments))
            tool_payload: dict[str, Any] = {"ok": True, "tool": name}
            if name == "ncs_search":
                tool_payload["results"] = [
                    {"type": "unit", "id": "dynamic-unit-code"}
                ]
            return {
                "status": 200,
                "duration_seconds": 0.01,
                "payload": {
                    "result": {
                        "isError": False,
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(tool_payload),
                            }
                        ],
                    }
                },
            }
        raise AssertionError((method, payload, protocol_version))

    return fake_request, observed_protocols, tool_calls


class RemoteMcpTransportVerifierTests(unittest.TestCase):
    def test_release_workflow_fails_before_promotion_when_smoke_gate_fails(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "vercel-snapshot-release.yml"
        ).read_text(encoding="utf-8")

        self.assertEqual(workflow.count("--request-timeout 30"), 2)
        self.assertIn(
            "The staged deployment failed transport or public-tool smoke verification.",
            workflow,
        )
        self.assertIn(
            "The public production MCP URL failed transport or public-tool smoke verification.",
            workflow,
        )
        self.assertLess(
            workflow.index("The staged deployment failed transport or public-tool smoke verification."),
            workflow.index("vercel promote"),
        )

    def test_all_public_tools_and_analysis_modes_are_smoked_without_body_logging(self) -> None:
        selected_protocol = "2025-06-18"
        fake_request, observed_protocols, tool_calls = _successful_request_factory(
            selected_protocol=selected_protocol
        )

        with patch.object(verifier, "_request", side_effect=fake_request):
            report = verifier.verify(
                "https://example.test/api/mcp",
                concurrency=3,
                protocol_version=selected_protocol,
                request_timeout=7.0,
            )

        self.assertTrue(report["ok"], report)
        self.assertEqual(report["protocol_version"], selected_protocol)
        self.assertGreaterEqual(observed_protocols.count(selected_protocol), 12)
        self.assertIn("2099-01-01", observed_protocols)
        self.assertEqual(
            {name for name, _arguments in tool_calls},
            verifier.EXPECTED_PUBLIC_TOOLS,
        )
        analysis_modes = {
            arguments["mode"]
            for name, arguments in tool_calls
            if name == "ncs_analysis"
        }
        self.assertEqual(
            analysis_modes,
            {"career_path", "qualification", "job_base", "ontology"},
        )
        analysis_arguments = [
            arguments
            for name, arguments in tool_calls
            if name == "ncs_analysis"
        ]
        for mode in ("career_path", "job_base"):
            self.assertIn(
                {"mode": mode, "query": verifier.SMOKE_UNIT_QUERY, "limit": 1},
                analysis_arguments,
            )
        self.assertEqual(report["tool_smoke"]["tools_call_count"], 10)
        self.assertTrue(report["tool_smoke"]["smoke_unit_discovered"])
        unit_detail_arguments = next(
            arguments for name, arguments in tool_calls if name == "ncs_unit_detail"
        )
        self.assertEqual(unit_detail_arguments["unit_code"], "dynamic-unit-code")
        self.assertTrue(report["tool_smoke"]["all_response_bodies_redacted"])
        self.assertFalse(report["tool_smoke"]["session_id_values_logged"])
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn('\"payload\"', rendered)
        self.assertNotIn('\"_payload\"', rendered)
        self.assertNotIn("Mcp-Session-Id", rendered)

    def test_raw_python_exception_in_tool_result_fails_release_gate(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def failing_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if body == b"{not-json":
                return response
            if body:
                payload = json.loads(body)
                params = payload.get("params") or {}
                arguments = params.get("arguments") or {}
                if (
                    payload.get("method") == "tools/call"
                    and params.get("name") == "ncs_analysis"
                    and arguments.get("mode") == "qualification"
                ):
                    return {
                        "status": 200,
                        "duration_seconds": 0.01,
                        "payload": {
                            "result": {
                                "isError": True,
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Error executing tool ncs_analysis: "
                                            "no such table: qualification_status"
                                        ),
                                    }
                                ],
                            }
                        },
                    }
            return response

        with patch.object(verifier, "_request", side_effect=failing_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn("tools_call_analysis_qualification_tool_error", report["failures"])
        self.assertIn("tools_call_analysis_qualification_semantic", report["failures"])
        self.assertIn("tools_call_analysis_qualification_raw_exception", report["failures"])
        rendered = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("qualification_status", rendered)
        self.assertNotIn("Error executing tool", rendered)

    def test_http_jsonrpc_and_semantic_failures_all_block_release(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def failing_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if body == b"{not-json":
                return response
            if not body:
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if payload.get("method") != "tools/call":
                return response
            if params.get("name") == "ncs_unit_detail":
                return {"status": 503, "duration_seconds": 0.01}
            if params.get("name") == "ncs_training":
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {"error": {"code": -32603, "message": "redacted"}},
                }
            if (
                params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "career_path"
            ):
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {
                        "result": {
                            "isError": False,
                            "content": [
                                {"type": "text", "text": json.dumps({"ok": False})}
                            ],
                        }
                    },
                }
            return response

        with patch.object(verifier, "_request", side_effect=failing_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn("tools_call_unit_detail_http", report["failures"])
        self.assertIn("tools_call_training_jsonrpc", report["failures"])
        self.assertIn("tools_call_training_semantic", report["failures"])
        self.assertIn("tools_call_analysis_career_path_semantic", report["failures"])


if __name__ == "__main__":
    unittest.main()
