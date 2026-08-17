from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import institutional_chat as chat


def sample_route(
    *,
    scenario: str = "task_training",
    tool: str = "recommend_training_for_task",
    params: dict | None = None,
    required: list[str] | None = None,
) -> dict:
    required = required if required is not None else ["query"]
    params = params if params is not None else {"query": "HR planning", "limit": 5}
    return {
        "schema": "ncs_query_route_v1",
        "scenario": scenario,
        "tool": tool,
        "params": params,
        "required_params": required,
        "missing_params": [name for name in required if not params.get(name)],
        "available": True,
        "confidence": 0.9,
        "expected_tool_chain": [tool],
        "route_fingerprint": "route-123",
        "guard_flags": [],
        "risk_flags": [],
    }


def successful_result() -> dict:
    return {
        "ok": True,
        "current_scope": {"resolved_as": "General affairs"},
        "target_scope": {"resolved_as": "HR planning"},
        "training_system_matrix": [
            {
                "rank": 1,
                "course_name": "HR planning practice",
                "required_optional": "required",
                "planner_grouping": "core_gap_training",
                "why_recommended": "Direct task/KSA evidence",
                "human_review": {"status": "review_required"},
            }
        ],
        "disclaimer": "Planning guidance only.",
    }


class InstitutionalChatServiceTests(unittest.TestCase):
    def test_korean_direction_particle_uses_final_consonant_and_rieul_rules(self) -> None:
        self.assertEqual(chat._direction_particle("인사기획"), "으로")
        self.assertEqual(chat._direction_particle("총무"), "로")
        self.assertEqual(chat._direction_particle("서울"), "로")
        self.assertEqual(chat._direction_particle("HR"), "로")

        result = successful_result()
        result["current_scope"] = {"resolved_as": "총무"}
        result["target_scope"] = {"resolved_as": "인사기획"}
        message, _ = chat.summarize_tool_result(
            "plan_ncs_education_path",
            result,
        )

        self.assertIn("총무에서 인사기획으로의 전환", message)
        self.assertIn("HR planning practice", message)
        self.assertIn("범위 검토가 필요한 참고 과정", message)

    def test_executes_only_routed_public_tool_and_forces_route_metadata(self) -> None:
        calls: list[tuple[str, dict]] = []

        def executor(tool_name: str, params: dict | None) -> dict:
            calls.append((tool_name, dict(params or {})))
            return successful_result()

        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(),
            executor=executor,
        )
        result, status = service.process(
            "Recommend task training",
            context={"limit": 3, "save": True, "_route_query": "spoofed"},
            identity="local-user",
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "completed")
        self.assertEqual(result["route"]["tool"], "recommend_training_for_task")
        self.assertEqual(result["evidence"]["course_count"], 1)
        self.assertEqual(
            result["evidence"]["courses"][0]["human_review"]["status"],
            "review_required",
        )
        self.assertEqual(
            result["evidence"]["courses"][0]["why_recommended"],
            "Direct task/KSA evidence",
        )
        self.assertEqual(calls[0][0], "recommend_training_for_task")
        self.assertEqual(calls[0][1]["limit"], 3)
        self.assertEqual(calls[0][1]["_route_query"], "Recommend task training")
        self.assertEqual(calls[0][1]["_route_fingerprint"], "route-123")
        self.assertNotIn("save", calls[0][1])
        self.assertEqual(result["context"]["applied_fields"], ["limit"])
        self.assertEqual(
            result["context"]["ignored_fields"],
            ["_route_query", "save"],
        )

    def test_operator_route_is_blocked_before_executor(self) -> None:
        executed = False

        def executor(_tool_name: str, _params: dict | None) -> dict:
            nonlocal executed
            executed = True
            return successful_result()

        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(
                scenario=chat.OPERATOR_REVIEW,
                tool="get_quality_issues",
                params={},
                required=[],
            ),
            executor=executor,
        )
        result, status = service.process(
            "Open the operator review queue",
            context=None,
            identity="local-user",
        )

        self.assertEqual(status, 403)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "operator_route_blocked")
        self.assertFalse(executed)

    def test_missing_route_parameters_return_clarification_without_execution(self) -> None:
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(
                scenario="training_transition",
                tool="recommend_training_transition",
                params={"target_query": "HR planning"},
                required=["current_query", "target_query"],
            ),
            executor=lambda *_args, **_kwargs: self.fail("executor must not run"),
        )
        result, status = service.process(
            "Recommend transition training",
            context=None,
            identity="local-user",
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(result["state"], "clarification_required")
        self.assertEqual(result["clarification"]["missing_params"], ["current_query"])

    def test_context_can_supply_missing_route_parameter(self) -> None:
        calls: list[dict] = []
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(
                scenario="training_transition",
                tool="recommend_training_transition",
                params={"target_query": "HR planning"},
                required=["current_query", "target_query"],
            ),
            executor=lambda _name, params: calls.append(dict(params or {})) or successful_result(),
        )
        result, status = service.process(
            "Recommend transition training",
            context={"current_query": "General affairs"},
            identity="local-user",
        )

        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertEqual(calls[0]["current_query"], "General affairs")

    def test_context_fields_for_a_different_tool_are_ignored(self) -> None:
        calls: list[dict] = []
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(
                scenario="structure_search",
                tool="ncs_search",
                params={"query": "HR planning", "scope": "all", "limit": 20},
                required=["query"],
            ),
            executor=lambda _name, params: calls.append(dict(params or {})) or {
                "ok": True,
                "results": [],
            },
        )
        result, status = service.process(
            "Search NCS",
            context={"current_query": "General affairs", "target_query": "HR planning"},
            identity="local-user",
        )

        self.assertEqual(status, 200)
        self.assertNotIn("current_query", calls[0])
        self.assertNotIn("target_query", calls[0])
        self.assertEqual(
            result["context"]["ignored_fields"],
            ["current_query", "target_query"],
        )

    def test_raw_executor_exception_is_not_exposed(self) -> None:
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(),
            executor=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("private database detail")
            ),
        )
        result, status = service.process(
            "Recommend task training",
            context=None,
            identity="local-user",
        )

        self.assertEqual(status, 422)
        serialized = json.dumps(result)
        self.assertNotIn("private database detail", serialized)
        self.assertIn("tool_execution_failed", serialized)

    def test_structured_tool_execution_failure_is_sanitized(self) -> None:
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(),
            router=lambda *_args, **_kwargs: sample_route(),
            executor=lambda *_args, **_kwargs: {
                "ok": False,
                "error": {
                    "code": "tool_execution_failed",
                    "message": "private SQL and filesystem detail",
                    "details": {"path": "C:/private/ncs.db"},
                    "tool_name": "recommend_training_for_task",
                },
                "content": [{"type": "text", "text": "private SQL and filesystem detail"}],
            },
        )
        result, status = service.process(
            "Recommend task training",
            context=None,
            identity="local-user",
        )

        self.assertEqual(status, 422)
        serialized = json.dumps(result)
        self.assertNotIn("private SQL", serialized)
        self.assertNotIn("C:/private", serialized)
        self.assertEqual(
            result["result"]["error"]["message"],
            "The requested NCS tool could not complete.",
        )

    def test_audit_log_hashes_identity_and_excludes_prompt_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            config = chat.ChatRuntimeConfig(
                auth_mode="gateway",
                gateway_secret="gateway-secret",
                allowed_origins=("https://chat.example.invalid",),
                audit_log_path=path,
                audit_hash_salt="audit-salt",
            )
            audit = chat.AuditLog(path, hash_salt="audit-salt", required=True)
            audit.preflight()
            service = chat.InstitutionalChatService(
                config,
                audit_log=audit,
                router=lambda *_args, **_kwargs: sample_route(),
                executor=lambda *_args, **_kwargs: successful_result(),
            )
            result, status = service.process(
                "sensitive employee prompt",
                context=None,
                identity="employee@example.invalid",
                groups=("hrd-users",),
            )
            event = json.loads(path.read_text(encoding="utf-8").strip())

        self.assertEqual(status, 200)
        self.assertTrue(result["audit"]["logged"])
        self.assertTrue(event["identity_hash"].startswith("hmac-sha256:"))
        serialized = json.dumps(event)
        self.assertNotIn("employee@example.invalid", serialized)
        self.assertNotIn("sensitive employee prompt", serialized)
        self.assertNotIn("HR planning practice", serialized)
        self.assertFalse(event["db_writes"])
        self.assertFalse(event["operator_tool_execution"])

    def test_required_audit_failure_withholds_response(self) -> None:
        audit = chat.AuditLog(Path("unused"), hash_salt="salt", required=True)
        with patch.object(audit, "write", return_value=False):
            service = chat.InstitutionalChatService(
                chat.ChatRuntimeConfig(
                    auth_mode="gateway",
                    gateway_secret="secret",
                    allowed_origins=("https://chat.example.invalid",),
                    audit_log_path=Path("unused"),
                    audit_hash_salt="salt",
                ),
                audit_log=audit,
                router=lambda *_args, **_kwargs: sample_route(),
                executor=lambda *_args, **_kwargs: successful_result(),
            )
            result, status = service.process(
                "Recommend task training",
                context=None,
                identity="employee",
            )

        self.assertEqual(status, 503)
        self.assertEqual(result["error"]["code"], "audit_log_unavailable")

    def test_required_audit_failure_withholds_rejection_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = chat.ChatRuntimeConfig(
                auth_mode="gateway",
                gateway_secret="gateway-secret",
                allowed_origins=("https://chat.example.invalid",),
                audit_log_path=Path(tmp) / "audit.jsonl",
                audit_hash_salt="salt",
            )
            audit = chat.AuditLog(
                config.audit_log_path,
                hash_salt="salt",
                required=True,
            )
            service = chat.InstitutionalChatService(config, audit_log=audit)
            httpd = chat.InstitutionalChatHTTPServer(("127.0.0.1", 0), service)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            with patch.object(audit, "write", return_value=False):
                thread.start()
                base = f"http://127.0.0.1:{httpd.server_address[1]}"
                request = urllib.request.Request(
                    base + "/api/chat",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                try:
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        urllib.request.urlopen(request, timeout=5)
                    payload = json.loads(error.exception.read().decode("utf-8"))
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)

        self.assertEqual(error.exception.code, 503)
        self.assertEqual(payload["error"]["code"], "audit_log_unavailable")
        self.assertNotIn("origin_not_allowed", json.dumps(payload))

    def test_input_limits_are_enforced(self) -> None:
        service = chat.InstitutionalChatService(
            chat.ChatRuntimeConfig(max_message_chars=10),
            router=lambda *_args, **_kwargs: self.fail("router must not run"),
        )
        result, status = service.process(
            "x" * 11,
            context=None,
            identity="local-user",
        )
        self.assertEqual(status, 400)
        self.assertEqual(result["error"]["code"], "message_too_long")


class InstitutionalChatRuntimeTests(unittest.TestCase):
    def test_file_backed_gateway_secrets_are_supported_without_env_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret_file = root / "gateway-secret"
            salt_file = root / "audit-salt"
            secret_file.write_text("gateway-value\n", encoding="utf-8")
            salt_file.write_text("audit-value\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {
                    "NCS_CHAT_GATEWAY_SECRET_FILE": str(secret_file),
                    "NCS_CHAT_AUDIT_HASH_SALT_FILE": str(salt_file),
                },
                clear=True,
            ):
                self.assertEqual(
                    chat._runtime_secret(
                        "NCS_CHAT_GATEWAY_SECRET",
                        "NCS_CHAT_GATEWAY_SECRET_FILE",
                    ),
                    "gateway-value",
                )
                self.assertEqual(
                    chat._runtime_secret(
                        "NCS_CHAT_AUDIT_HASH_SALT",
                        "NCS_CHAT_AUDIT_HASH_SALT_FILE",
                    ),
                    "audit-value",
                )

            with patch.dict(
                os.environ,
                {
                    "NCS_CHAT_GATEWAY_SECRET": "direct-value",
                    "NCS_CHAT_GATEWAY_SECRET_FILE": str(secret_file),
                },
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    chat._runtime_secret(
                        "NCS_CHAT_GATEWAY_SECRET",
                        "NCS_CHAT_GATEWAY_SECRET_FILE",
                    )

            secret_file.write_text("line-one\nline-two\n", encoding="utf-8")
            with patch.dict(
                os.environ,
                {"NCS_CHAT_GATEWAY_SECRET_FILE": str(secret_file)},
                clear=True,
            ):
                with self.assertRaises(ValueError):
                    chat._runtime_secret(
                        "NCS_CHAT_GATEWAY_SECRET",
                        "NCS_CHAT_GATEWAY_SECRET_FILE",
                    )

    def settings(self, *, read_only: bool = True, operator: bool = False) -> SimpleNamespace:
        return SimpleNamespace(read_only_mode=read_only, operator_tools_enabled=operator)

    def test_runtime_requires_read_only_and_operator_disabled(self) -> None:
        with patch.object(
            chat,
            "load_settings",
            return_value=self.settings(read_only=False, operator=True),
        ):
            issues = chat.validate_chat_runtime(chat.ChatRuntimeConfig())

        self.assertTrue(any("READ_ONLY" in issue for issue in issues))
        self.assertTrue(any("OPERATOR_TOOLS" in issue for issue in issues))

    def test_remote_binding_requires_full_gateway_boundary(self) -> None:
        config = chat.ChatRuntimeConfig(host="0.0.0.0", allow_remote_bind=True)
        with patch.object(chat, "load_settings", return_value=self.settings()):
            issues = chat.validate_chat_runtime(config)

        joined = " ".join(issues)
        self.assertIn("auth-mode gateway", joined)

        complete = chat.ChatRuntimeConfig(
            host="0.0.0.0",
            allow_remote_bind=True,
            auth_mode="gateway",
            gateway_secret="secret",
            allowed_origins=("https://chat.example.invalid",),
            audit_log_path=Path("audit.jsonl"),
            audit_hash_salt="salt",
        )
        with patch.object(chat, "load_settings", return_value=self.settings()):
            self.assertEqual(chat.validate_chat_runtime(complete), [])

    def test_gateway_mode_requires_secret_origin_audit_and_hash_salt(self) -> None:
        with patch.object(chat, "load_settings", return_value=self.settings()):
            issues = chat.validate_chat_runtime(
                chat.ChatRuntimeConfig(auth_mode="gateway")
            )
        joined = " ".join(issues)
        for marker in (
            "NCS_CHAT_GATEWAY_SECRET",
            "NCS_CHAT_ALLOWED_ORIGINS",
            "NCS_CHAT_AUDIT_LOG_PATH",
            "NCS_CHAT_AUDIT_HASH_SALT",
        ):
            self.assertIn(marker, joined)

    def test_html_is_same_origin_and_escapes_api_content_via_text_content(self) -> None:
        page = chat.render_chat_html()
        self.assertIn("/api/chat", page)
        self.assertIn("textContent", page)
        self.assertNotIn("innerHTML", page)
        self.assertNotIn("https://cdn", page)
        self.assertIn("@media (max-width:780px)", page)
        self.assertIn('nonce="static-test-nonce"', page)
        self.assertIn("범위 검토 필요", page)
        self.assertIn("검토 후 확정", page)
        self.assertIn("course-reason", page)
        self.assertIn("aside { display:block", page)

    def test_package_exposes_institutional_chat_console_script(self) -> None:
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            'ncs-institutional-chat = "ncs_mcp.institutional_chat:main"',
            text,
        )


class InstitutionalChatHTTPTests(unittest.TestCase):
    def start_server(self, config: chat.ChatRuntimeConfig, *, executor=None):
        service = chat.InstitutionalChatService(
            config,
            router=lambda *_args, **_kwargs: sample_route(),
            executor=executor or (lambda *_args, **_kwargs: successful_result()),
        )
        httpd = chat.InstitutionalChatHTTPServer(("127.0.0.1", 0), service)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_address[1]}"
        for _ in range(50):
            try:
                with urllib.request.urlopen(base + "/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.01)
        return httpd, thread, base

    def stop_server(self, httpd, thread) -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    def request(self, url: str, *, data: dict | None = None, headers: dict | None = None):
        body = json.dumps(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(url, data=body, headers=headers or {})
        return urllib.request.urlopen(request, timeout=5)

    def test_local_http_chat_and_security_headers(self) -> None:
        httpd, thread, base = self.start_server(chat.ChatRuntimeConfig())
        try:
            with self.request(base + "/") as response:
                page = response.read().decode("utf-8")
                self.assertIn("NCS 교육설계", page)
                self.assertEqual(response.headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
                self.assertNotIn("unsafe-inline", response.headers["Content-Security-Policy"])
                self.assertIn("camera=()", response.headers["Permissions-Policy"])
            with self.request(
                base + "/api/chat",
                data={"message": "Recommend task training"},
                headers={"Content-Type": "application/json"},
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["route"]["tool"], "recommend_training_for_task")
        finally:
            self.stop_server(httpd, thread)

    def test_gateway_auth_origin_identity_and_group_are_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = chat.ChatRuntimeConfig(
                auth_mode="gateway",
                gateway_secret="gateway-secret",
                allowed_origins=("https://chat.example.invalid",),
                allowed_groups=("hrd-users",),
                audit_log_path=Path(tmp) / "audit.jsonl",
                audit_hash_salt="salt",
            )
            httpd, thread, base = self.start_server(config)
            try:
                with self.assertRaises(urllib.error.HTTPError) as missing:
                    self.request(
                        base + "/api/chat",
                        data={"message": "Recommend task training"},
                        headers={"Content-Type": "application/json"},
                    )
                self.assertEqual(missing.exception.code, 403)

                with self.request(
                    base + "/",
                    headers={
                        chat.GATEWAY_SECRET_HEADER: "gateway-secret",
                        chat.IDENTITY_HEADER: "employee",
                        chat.GROUPS_HEADER: "hrd-users",
                    },
                ) as response:
                    self.assertIn("NCS 교육설계", response.read().decode("utf-8"))

                headers = {
                    "Content-Type": "application/json",
                    "Origin": "https://chat.example.invalid",
                    chat.GATEWAY_SECRET_HEADER: "gateway-secret",
                    chat.IDENTITY_HEADER: "employee",
                    chat.GROUPS_HEADER: "hrd-users",
                }
                invalid_request = urllib.request.Request(
                    base + "/api/chat",
                    data=b"not-json",
                    headers={**headers, "Content-Type": "text/plain"},
                )
                with self.assertRaises(urllib.error.HTTPError) as invalid:
                    urllib.request.urlopen(invalid_request, timeout=5)
                self.assertEqual(invalid.exception.code, 415)

                with self.request(
                    base + "/api/chat",
                    data={"message": "Recommend task training"},
                    headers=headers,
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                    self.assertTrue(payload["ok"])
                events = [
                    json.loads(line)
                    for line in config.audit_log_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                self.assertEqual(
                    [event["error_code"] for event in events],
                    ["origin_not_allowed", "json_content_type_required", None],
                )
                self.assertEqual(events[0]["outcome"], "rejected")
                self.assertNotIn("message", events[0])
                self.assertNotIn("prompt", events[0])
            finally:
                self.stop_server(httpd, thread)

    def test_http_worker_limit_applies_backpressure_without_extra_threads(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_executor(_name: str, _params: dict | None) -> dict:
            entered.set()
            release.wait(timeout=5)
            return successful_result()

        config = chat.ChatRuntimeConfig(
            max_http_workers=1,
            request_socket_timeout_seconds=2.0,
        )
        httpd, thread, base = self.start_server(config, executor=blocking_executor)
        first_result: list[object] = []
        second_result: list[object] = []

        def first_request() -> None:
            try:
                with self.request(
                    base + "/api/chat",
                    data={"message": "Recommend task training"},
                    headers={"Content-Type": "application/json"},
                ) as response:
                    first_result.append(response.status)
            except Exception as exc:  # pragma: no cover - assertion captures failure
                first_result.append(exc)

        requester = threading.Thread(target=first_request)
        requester.start()

        def second_request() -> None:
            try:
                with self.request(
                    base + "/api/chat",
                    data={"message": "Recommend task training"},
                    headers={"Content-Type": "application/json"},
                ) as response:
                    second_result.append(response.status)
            except Exception as exc:  # pragma: no cover - assertion captures failure
                second_result.append(exc)

        second_requester = threading.Thread(target=second_request)
        try:
            self.assertTrue(entered.wait(timeout=3))
            second_requester.start()
            time.sleep(0.1)
            self.assertTrue(second_requester.is_alive())
            self.assertEqual(second_result, [])
        finally:
            release.set()
            requester.join(timeout=5)
            second_requester.join(timeout=5)
            self.stop_server(httpd, thread)

        self.assertEqual(first_result, [200])
        self.assertEqual(second_result, [200])

    def test_oversized_http_body_is_rejected_before_read(self) -> None:
        config = chat.ChatRuntimeConfig(max_body_bytes=1024)
        httpd, thread, base = self.start_server(config)
        try:
            request = urllib.request.Request(
                base + "/api/chat",
                data=b"{}",
                headers={"Content-Type": "application/json", "Content-Length": "2048"},
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=5)
            self.assertEqual(error.exception.code, 413)
        finally:
            self.stop_server(httpd, thread)


if __name__ == "__main__":
    unittest.main()
