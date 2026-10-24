from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT / "src", ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import mcp_surface_gate as gate
from ncs_mcp import server, tool_registry
from ncs_mcp.query_router import route_ncs_query


def definition(name: str, description: str = "x" * 40, properties: dict | None = None, required: list | None = None) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties or {}, "required": required or []}}


class SurfaceGateTests(unittest.TestCase):
    def test_release_surface_passes_the_gate(self) -> None:
        report = gate.run_gate()
        self.assertTrue(report["ok"], report["failed_checks"])
        self.assertEqual(report["schema"], gate.SCHEMA)

    def test_schema_shapes_weak_clients_mishandle_are_reported(self) -> None:
        schema = {
            "type": "object",
            "required": ["a", "b", "c", "d"],
            "properties": {
                "ref": {"$ref": "#/$defs/x"},
                "union": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                "untyped": {"anyOf": [{}, {"type": "null"}]},
                "nullable": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                "filters": {"type": "object", "additionalProperties": True},
                "rows": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
                "not": {"type": "string"},
            },
        }
        issues = gate.schema_issues("demo", schema)
        self.assertIn("required>3:4", issues)
        self.assertTrue(any(issue.startswith("forbidden:$ref") for issue in issues))
        self.assertIn("anyOf_not_nullable_pair@/properties/union", issues)
        self.assertIn("anyOf_not_nullable_pair@/properties/untyped", issues)
        self.assertIn("object_param_not_allowlisted:filters", issues)
        self.assertIn("object_param_not_allowlisted:rows", issues)  # nested objects count too
        self.assertFalse(any("/properties/nullable" in issue for issue in issues))
        self.assertFalse(any(issue.startswith("forbidden:not") for issue in issues))  # a parameter name, not a keyword

    def test_definition_limits_fail_closed_and_extra_tools_are_not_filtered(self) -> None:
        definitions = [definition(f"tool_{index}") for index in range(gate.MAX_ACTIVE_TOOLS + 1)]
        definitions.append(definition("Bad-Name", description="short"))
        checks = {check["id"]: check for check in gate.check_definitions(definitions, {"tool_0", "tool_x"})}
        self.assertEqual(checks["tool_count"]["status"], "fail")
        self.assertEqual(checks["release_profile_matches_registry"]["status"], "fail")
        self.assertIn("Bad-Name", checks["release_profile_matches_registry"]["measured"]["unexpected"])
        self.assertEqual(checks["release_profile_matches_registry"]["measured"]["missing"], ["tool_x"])
        self.assertEqual(checks["tool_name_format"]["status"], "fail")
        self.assertEqual(checks["description_length"]["status"], "fail")
        self.assertEqual(checks["parameter_descriptions"]["status"], "pass")

    def test_reading_or_forwarding_a_write_flag_fails(self) -> None:
        sources = {
            "safe_tool": "def safe_tool(query=None, save=False):\n    return run(query, save=False)\n",
            "unsafe_tool": "def unsafe_tool(query=None, save=False):\n    return run(query, save=save)\n",
            "locals_tool": "def locals_tool(save=False):\n    return run(**locals())\n",
            "kwargs_tool": "def kwargs_tool(**kwargs):\n    return run(**kwargs)\n",
            "posonly_tool": "def posonly_tool(save=False, /):\n    return run(save)\n",
            "path_tool": "def path_tool(db_path=None):\n    return db_path\n",
            "missing_tool": None,
        }
        checks = {check["id"]: check for check in gate.check_read_only(sources, [definition(name) for name in sources])}
        self.assertEqual(checks["write_flags_never_read"]["measured"], ["unsafe_tool.save@line2", "posonly_tool.save@line2"])
        self.assertIn("locals_tool:locals()@line2", checks["no_dynamic_argument_forwarding"]["measured"])
        self.assertIn("kwargs_tool:*args/**kwargs", checks["no_dynamic_argument_forwarding"]["measured"])
        self.assertEqual(checks["no_path_parameters"]["measured"], ["path_tool.db_path"])
        self.assertEqual(checks["public_tools_found_in_source"]["measured"], ["missing_tool"])

    def test_meta_execution_detects_a_swapped_handler(self) -> None:
        def ncs_analysis(limit: int = 1):
            return {}

        def get_quality_issues(limit: int = 1):
            return {}

        fake_server = SimpleNamespace(NCS_EXECUTABLE_TOOL_HANDLERS={"ncs_analysis": get_quality_issues},
                                      open_db=None, ncs_execute_tool=lambda *args, **kwargs: None)
        registry = SimpleNamespace(NCS_EXECUTABLE_TOOL_NAMES={"ncs_analysis"}, OPERATOR_MCP_TOOLS={"get_quality_issues"},
                                   LEGACY_MCP_TOOLS=set(), NCS_META_READ_ONLY_SAVE_FORCED_TOOLS=set())
        checks = {check["id"]: check for check in gate.check_meta_execution(fake_server, registry, {"ncs_analysis"})}
        self.assertEqual(checks["meta_execute_handler_identity"]["measured"], ["ncs_analysis->get_quality_issues"])
        self.assertIsNotNone(ncs_analysis)

    def test_meta_execution_that_forwards_save_true_fails(self) -> None:
        def recommend_training_for_task(unit_code=None, save: bool = False):
            return {}

        handlers = {"recommend_training_for_task": recommend_training_for_task}

        def leaky_execute(name, params):
            return handlers[name](**params)

        fake_server = SimpleNamespace(NCS_EXECUTABLE_TOOL_HANDLERS=handlers, open_db=None, ncs_execute_tool=leaky_execute)
        registry = SimpleNamespace(NCS_EXECUTABLE_TOOL_NAMES={"recommend_training_for_task"}, OPERATOR_MCP_TOOLS=set(),
                                   LEGACY_MCP_TOOLS=set(),
                                   NCS_META_READ_ONLY_SAVE_FORCED_TOOLS={"recommend_training_for_task"})
        checks = {check["id"]: check for check in gate.check_meta_execution(fake_server, registry,
                                                                           {"recommend_training_for_task"})}
        self.assertEqual(checks["meta_execute_forces_save_false"]["status"], "fail")
        self.assertEqual(checks["meta_execute_forces_save_false"]["measured"], {"recommend_training_for_task": True})

    def test_next_action_integrity_rejects_incomplete_or_mistyped_calls(self) -> None:
        definitions = [definition("ncs_unit_detail", properties={"unit_code": {"type": "string"}}, required=["unit_code"])]

        def builder(tool, arguments, result):
            return [{"tool": "ncs_unit_detail", "arguments": {}, "purpose": "detail"},
                    {"tool": "ncs_unit_detail", "arguments": {"unit_code": 7}, "purpose": "detail"},
                    {"tool": "hidden_tool", "arguments": {}, "purpose": "hidden"}]

        check = gate.check_next_actions(builder, definitions)[0]
        self.assertEqual(check["status"], "fail")
        details = " ".join(check["details"])
        self.assertIn("missing_required:unit_code", details)
        self.assertIn("bad_type:unit_code", details)
        self.assertIn("hidden_tool:not_listed", details)
        self.assertEqual(gate.check_next_actions(None, definitions)[0]["status"], "fail")

    def test_token_estimate_counts_hangul_as_one_token(self) -> None:
        self.assertEqual(gate.estimate_tokens("능력단위"), 4)
        self.assertEqual(gate.estimate_tokens("abcdefgh"), 2)


class RouteChainTests(unittest.TestCase):
    def test_disabled_follow_up_tools_leave_the_chain_but_not_the_fingerprint(self) -> None:
        public = tool_registry.mcp_tools_for_mode(advanced_tools_enabled=False)
        route = route_ncs_query("인력채용 훈련과정 추천", available_tool_names=public)
        meta = route_ncs_query("인력채용 훈련과정 추천", available_tool_names=tool_registry.NCS_EXECUTABLE_TOOL_NAMES)
        self.assertNotIn("get_concept_evidence", route["expected_tool_chain"])
        self.assertEqual(route["unavailable_chain_tools"], ["get_concept_evidence"])
        self.assertIn("get_concept_evidence", meta["expected_tool_chain"])
        self.assertEqual(route["route_fingerprint"], meta["route_fingerprint"])


class PublicNextActionTests(unittest.TestCase):
    def test_search_hits_point_to_unit_detail_then_training(self) -> None:
        result = {"ok": True, "results": [
            {"type": "unit", "id": "0202020109_23v5", "text": "급여지급"},
            {"type": "criteria", "id": 1, "path": {"unit_code": "0202020109_23v5"}},
            {"type": "ksa", "id": 2, "path": {"unit_code": "0202020108_23v4"}},
        ]}
        actions = server._public_next_actions("ncs_search", {"query": "급여"}, result)
        self.assertEqual([action["tool"] for action in actions],
                         ["ncs_unit_detail", "ncs_unit_detail", "recommend_training_for_task"])
        self.assertEqual(actions[1]["arguments"], {"unit_code": "0202020108_23v4"})

    def test_not_found_search_suggests_a_retry_route_and_says_absence_is_not_proof(self) -> None:
        result = {"ok": False, "error": {"code": "NOT_FOUND"}, "audit": {"generated_at": "t"}}
        actions = server._public_next_actions("ncs_search", {"query": "없는말"}, result)
        self.assertEqual(actions, [{"tool": "ncs_discover_tools", "arguments": {"intent": "없는말"},
                                    "purpose": "질의에 맞는 도구와 분류 범위 다시 확인"}])
        markdown = server._render_tool_response_markdown({**result, "next_actions": actions}, renderer="ncs_search")
        self.assertIn("결과 없음이 자료가 없다는 뜻은 아닙니다", markdown)
        self.assertTrue(markdown.rstrip().endswith(server.PUBLIC_SOURCE_FOOTER))
        self.assertLess(markdown.index("### 다음 단계"), markdown.index("audit.generated_at"))

    def test_filtered_not_found_keeps_the_hard_scope_in_the_retry(self) -> None:
        scope = {"major_code": "20"}
        result = {"ok": False, "error": {"code": "NOT_FOUND"}, "classification_filter": scope, "audit": {}}
        actions = server._public_next_actions(
            "ncs_search", {"query": "인사기획", "job_scope": "인사", "context_text": "채용 담당"}, result
        )
        self.assertEqual(actions[0]["arguments"], {"intent": "인사기획", "classification_filter": scope,
                                                   "job_scope": "인사", "context_text": "채용 담당"})
        markdown = server._render_tool_response_markdown({**result, "next_actions": actions}, renderer="ncs_search")
        self.assertIn("범위 밖에서 찾은 결과를 이 범위의 근거로 쓰지 마세요", markdown)
        self.assertNotIn("결과 없음이 자료가 없다는 뜻은 아닙니다", markdown)

    def test_other_errors_and_detail_lookups_get_no_hints(self) -> None:
        scope_error = {"ok": False, "error": {"code": "route_context_required"}}
        self.assertEqual(server._public_next_actions("ncs_search", {"query": "인사"}, scope_error), [])
        detail = {"ok": True, "content": [{"type": "text", "text": "course"}]}
        self.assertEqual(server._public_next_actions("ncs_training", {"training_course_id": 7}, detail), [])

    def test_unit_detail_with_training_already_included_skips_that_hint(self) -> None:
        actions = server._public_next_actions(
            "ncs_unit_detail", {"unit_code": "0202020109_23v5", "include": ["training", "qualification"]}, {"ok": True}
        )
        self.assertEqual([action["tool"] for action in actions], ["recommend_training_for_task", "ncs_analysis"])
        self.assertLessEqual(len(actions), server.MAX_PUBLIC_NEXT_ACTIONS)


if __name__ == "__main__":
    unittest.main()
