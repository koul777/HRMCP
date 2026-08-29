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
SMOKE_UNIT_CODE = "0202020101_23v3"


def _markdown_tool_text(name: str, arguments: dict[str, Any]) -> str:
    if name == "ncs_search":
        body = "\n".join(
            [
                "## NCS 검색 결과: 인사기획",
                "1건 중 1건 표시",
                "",
                "| 능력단위명 | 수준 | 분류경로 | 능력단위코드 |",
                "| --- | --- | --- | --- |",
                f"| 인사기획 | 6 | 경영·회계·사무 > 인사·조직 | `{SMOKE_UNIT_CODE}` |",
            ]
        )
    elif name == "ncs_unit_detail":
        body = "\n".join(
            [
                "## 능력단위 상세: 인사기획",
                "",
                "| 능력단위코드 | 수준 | 분류경로 |",
                "| --- | --- | --- |",
                f"| `{arguments.get('unit_code')}` | 6 | 경영·회계·사무 > 인사·조직 |",
            ]
        )
    elif name == "ncs_training":
        body = "\n".join(
            [
                "## NCS 훈련과정",
                "1건 중 1건 표시",
                "",
                "| 과정ID | 과정명 | 훈련시간 | 훈련방법 |",
                "| --- | --- | --- | --- |",
                "| course-1 | 인사기획 | 40 | 집체교육 |",
            ]
        )
    elif name == "ncs_analysis" and arguments.get("mode") == "qualification":
        body = "\n".join(
            [
                "## 자격 연계 분석",
                "1건 중 1건 표시",
                "",
                "| 자격코드 | 자격명 | 능력단위코드 | 최소시간 |",
                "| --- | --- | --- | --- |",
                f"| jm-1 | 인사 자격 | `{SMOKE_UNIT_CODE}` | 40 |",
            ]
        )
    elif name == "ncs_analysis":
        mode = str(arguments.get("mode") or "analysis")
        body = f"## NCS 분석: {mode}\n\n검증 데이터 1건"
    else:
        body = f"## {name}\n\n검증 데이터 1건"
    return f"{body}\n\n{verifier.PUBLIC_SOURCE_FOOTER}"


def _successful_request_factory(
    *,
    selected_protocol: str,
    wire_format: str = "markdown",
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
                "payload": {
                    "result": {
                        "protocolVersion": selected_protocol,
                        "serverInfo": {
                            "name": "ncs-mcp",
                            "version": "0.1.0+git.0123456789abcdef",
                        },
                    }
                },
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
                tool_payload["results"] = [{"type": "unit", "id": SMOKE_UNIT_CODE}]
            if name == "ncs_analysis" and arguments.get("mode") == "qualification":
                tool_payload["qualification_links"] = [
                    {"unit_code": SMOKE_UNIT_CODE, "qualification_name": "sample"}
                ]
            text = (
                _markdown_tool_text(name, arguments)
                if wire_format == "markdown"
                else json.dumps(tool_payload)
            )
            return {
                "status": 200,
                "duration_seconds": 0.01,
                "payload": {
                    "result": {
                        "isError": False,
                        "content": [
                            {
                                "type": "text",
                                "text": text,
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

        self.assertEqual(workflow.count("--request-timeout 30"), 3)
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
        self.assertIn(
            "The current public production MCP URL failed transport or public-tool smoke verification.",
            workflow,
        )
        self.assertIn('"NCS_MCP_BUILD_ID=$env:GITHUB_SHA"', workflow)
        self.assertLess(
            workflow.index("No source projection change was detected; deployment and baseline promotion were skipped."),
            workflow.index("The current public production MCP URL failed transport or public-tool smoke verification."),
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
        self.assertEqual(
            report["checks"]["initialize"]["server_version"],
            "0.1.0+git.0123456789abcdef",
        )
        self.assertTrue(report["checks"]["initialize"]["build_identifier_present"])
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
        self.assertEqual(unit_detail_arguments["unit_code"], SMOKE_UNIT_CODE)
        self.assertEqual(
            report["checks"]["tools_call_search"]["response_format"],
            "markdown",
        )
        self.assertTrue(
            report["checks"]["tools_call_search"]["source_footer_present"]
        )
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

    def test_legacy_json_text_responses_remain_supported(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION,
            wire_format="json",
        )

        with patch.object(verifier, "_request", side_effect=fake_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertTrue(report["ok"], report)
        self.assertEqual(
            report["checks"]["tools_call_search"]["response_format"],
            "json_text",
        )
        self.assertFalse(
            report["checks"]["tools_call_search"]["source_footer_present"]
        )

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

    def test_job_base_payload_and_latency_limits_block_release(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def oversized_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "job_base"
            ):
                text = (
                    "## 직업기초능력 분석\n\n"
                    + ("x" * 2_100)
                    + f"\n\n{verifier.PUBLIC_SOURCE_FOOTER}"
                )
                return {
                    "status": 200,
                    "duration_seconds": 1.01,
                    "payload": {
                        "result": {
                            "isError": False,
                            "content": [
                                {
                                    "type": "text",
                                    "text": text,
                                }
                            ],
                        }
                    },
                }
            return response

        with patch.object(verifier, "_request", side_effect=oversized_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn(
            "tools_call_analysis_job_base_payload_size", report["failures"]
        )
        self.assertIn("tools_call_analysis_job_base_duration", report["failures"])

    def test_empty_qualification_result_blocks_release_gate(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def empty_qualification_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "qualification"
            ):
                text = "\n".join(
                    [
                        "## 자격 연계 분석",
                        "0건 중 0건 표시",
                        "",
                        "| 자격코드 | 자격명 | 능력단위코드 | 최소시간 |",
                        "| --- | --- | --- | --- |",
                        "",
                        verifier.PUBLIC_SOURCE_FOOTER,
                    ]
                )
                return {
                    "status": 200,
                    "duration_seconds": 0.01,
                    "payload": {
                        "result": {
                            "isError": False,
                            "content": [
                                {"type": "text", "text": text}
                            ],
                        }
                    },
                }
            return response

        with patch.object(verifier, "_request", side_effect=empty_qualification_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn(
            "tools_call_analysis_qualification_empty", report["failures"]
        )
        self.assertFalse(
            report["checks"]["tools_call_analysis_qualification"][
                "expected_data_present"
            ]
        )

    def test_structured_content_blocks_release_even_with_valid_markdown(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def duplicated_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "qualification"
            ):
                response["payload"]["result"]["structuredContent"] = {
                    "ok": True,
                    "qualification_links": [{"unit_code": SMOKE_UNIT_CODE}],
                }
            return response

        with patch.object(verifier, "_request", side_effect=duplicated_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn(
            "tools_call_analysis_qualification_structured_content",
            report["failures"],
        )
        self.assertTrue(
            report["checks"]["tools_call_analysis_qualification"][
                "structured_content_present"
            ]
        )

    def test_meta_tool_structured_content_is_observed_without_blocking(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def meta_structured_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_discover_tools"
            ):
                response["payload"]["result"]["structuredContent"] = {
                    "ok": True,
                    "tool": "ncs_discover_tools",
                }
                response["payload"]["result"]["content"][0]["text"] = (
                    "meta tool output without a footer"
                )
            return response

        with patch.object(verifier, "_request", side_effect=meta_structured_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertTrue(report["ok"], report)
        check = report["checks"]["tools_call_discover"]
        self.assertTrue(check["structured_content_present"])
        self.assertFalse(check["structured_content_forbidden"])
        self.assertFalse(check["markdown_footer_required"])
        self.assertFalse(check["source_footer_present"])

    def test_markdown_without_fixed_source_footer_fails_semantic_gate(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def footerless_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "career_path"
            ):
                response["payload"]["result"]["content"][0]["text"] = (
                    "## 경력개발경로 분석\n\n검증 데이터 1건"
                )
            return response

        with patch.object(verifier, "_request", side_effect=footerless_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn(
            "tools_call_analysis_career_path_semantic",
            report["failures"],
        )
        self.assertFalse(
            report["checks"]["tools_call_analysis_career_path"][
                "source_footer_present"
            ]
        )

    def test_not_found_markdown_fails_without_leaking_response_text(self) -> None:
        fake_request, _observed_protocols, _tool_calls = _successful_request_factory(
            selected_protocol=verifier.PROTOCOL_VERSION
        )

        def not_found_request(*args: Any, **kwargs: Any) -> dict[str, Any]:
            response = fake_request(*args, **kwargs)
            body = kwargs.get("body")
            if not body or body == b"{not-json":
                return response
            payload = json.loads(body)
            params = payload.get("params") or {}
            arguments = params.get("arguments") or {}
            if (
                payload.get("method") == "tools/call"
                and params.get("name") == "ncs_analysis"
                and arguments.get("mode") == "ontology"
            ):
                response["payload"]["result"]["content"][0]["text"] = "\n".join(
                    [
                        "[NOT_FOUND] ontology 분석 결과가 없습니다.",
                        "LLM은 추측 또는 생성을 하지 마세요.",
                        "",
                        verifier.PUBLIC_SOURCE_FOOTER,
                    ]
                )
            return response

        with patch.object(verifier, "_request", side_effect=not_found_request):
            report = verifier.verify("https://example.test/api/mcp", concurrency=1)

        self.assertFalse(report["ok"])
        self.assertIn("tools_call_analysis_ontology_semantic", report["failures"])
        self.assertTrue(
            report["checks"]["tools_call_analysis_ontology"]["not_found_detected"]
        )
        self.assertNotIn(verifier.NOT_FOUND_MARKER, json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
