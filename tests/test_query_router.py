from __future__ import annotations

import unittest
import sys
import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.query_router import (
    aihr_plan_route_evidence,
    route_ncs_query,
    risk_flags_for_query,
)


class NcsQueryRouterTests(unittest.TestCase):
    def test_no_context_structure_route_keeps_v1_fingerprint_exactly(self) -> None:
        route = route_ncs_query("워크숍 행사 준비")
        whitespace = route_ncs_query(
            "워크숍 행사 준비",
            context_text="  ",
            job_scope="\t",
        )

        self.assertEqual(route["route_fingerprint"], "03217e16805204af6cf48e1d")
        self.assertEqual(route["route_contract"]["fingerprint_version"], "route-fingerprint-v1")
        self.assertEqual(whitespace["route_fingerprint"], route["route_fingerprint"])
        self.assertNotIn("job_scope", route["params"])
        self.assertIsNone(route["classification_context"]["filter"])

    def test_explicit_context_uses_v2_without_query_only_scope_inference(self) -> None:
        plain = route_ncs_query("워크숍 행사 준비")
        contextual = route_ncs_query(
            "워크숍 행사 준비",
            context_text="총무팀 내부 행사 운영 배경",
            job_scope="총무",
        )
        repeated = route_ncs_query(
            "워크숍 행사 준비",
            context_text="  총무팀   내부 행사 운영 배경  ",
            job_scope=" 총무 ",
        )

        self.assertEqual(contextual["route_contract"]["fingerprint_version"], "route-fingerprint-v2")
        self.assertEqual(contextual["classification_context"]["schema"], "ncs_search_context_v1")
        self.assertFalse(contextual["classification_context"]["policy"]["query_inference_allowed"])
        self.assertEqual(contextual["params"]["job_scope"], "총무")
        self.assertEqual(contextual["route_fingerprint"], repeated["route_fingerprint"])
        self.assertNotEqual(contextual["route_fingerprint"], plain["route_fingerprint"])
        serialized = json.dumps(contextual, ensure_ascii=False)
        self.assertNotIn("총무팀 내부 행사 운영 배경", serialized)
        self.assertNotIn("context_text\": \"", serialized)

    def test_context_changes_are_bound_to_v2_fingerprint(self) -> None:
        base = route_ncs_query("능력단위 검색", job_scope="총무")
        changed_scope = route_ncs_query("능력단위 검색", job_scope="회계")
        changed_context = route_ncs_query(
            "능력단위 검색", job_scope="총무", context_text="별도 조직 배경"
        )

        self.assertNotEqual(base["route_fingerprint"], changed_scope["route_fingerprint"])
        self.assertNotEqual(base["route_fingerprint"], changed_context["route_fingerprint"])

    def test_aihr_plan_route_evidence_binds_explicit_scope_to_fingerprint(self) -> None:
        unscoped = aihr_plan_route_evidence("labor management", "HR planning")
        scoped = aihr_plan_route_evidence(
            "labor management",
            "HR planning",
            major_code="02",
            current_major_code="01",
            target_major_code="02",
            target_middle_code="02",
        )
        repeated = aihr_plan_route_evidence(
            "labor management",
            "HR planning",
            major_code="02",
            current_major_code="01",
            target_major_code="02",
            target_middle_code="02",
        )

        self.assertTrue(unscoped["classification_context"]["supported"])
        self.assertFalse(unscoped["classification_context"]["provided"])
        self.assertTrue(scoped["classification_context"]["provided"])
        self.assertEqual(
            scoped["classification_context"]["current_filter"],
            {"major_code": "01"},
        )
        self.assertEqual(
            scoped["classification_context"]["target_filter"],
            {"major_code": "02", "middle_code": "02"},
        )
        self.assertEqual(scoped["params"]["current_major_code"], "01")
        self.assertEqual(scoped["params"]["target_middle_code"], "02")
        self.assertIn(
            "current_major_code",
            scoped["route_contract"]["provided_params"],
        )
        self.assertIn(
            "target_middle_code",
            scoped["route_contract"]["provided_params"],
        )
        self.assertNotEqual(scoped["route_fingerprint"], unscoped["route_fingerprint"])
        self.assertEqual(scoped["route_fingerprint"], repeated["route_fingerprint"])
        self.assertEqual(
            scoped["route_contract"]["route_fingerprint"],
            scoped["route_fingerprint"],
        )

    def test_routes_education_system_transition_to_plan_tool(self) -> None:
        query = (
            "\ub178\ubb34\uad00\ub9ac\uc5d0\uc11c "
            "\uc778\uc0ac\uae30\ud68d\uc73c\ub85c "
            "\uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub9cc\ub4e4\uc5b4\uc918"
        )

        route = route_ncs_query(query)
        repeated = route_ncs_query(query)

        self.assertEqual(route["schema"], "ncs_query_route_v1")
        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertEqual(route["params"]["current_query"], "\ub178\ubb34\uad00\ub9ac")
        self.assertEqual(route["params"]["target_query"], "\uc778\uc0ac\uae30\ud68d")
        self.assertEqual(route["required_params"], ["current_query", "target_query"])
        self.assertEqual(route["missing_params"], [])
        self.assertGreater(route["confidence"], 0)
        self.assertEqual(route["route_contract"]["schema"], "ncs_query_route_v1")
        self.assertEqual(route["route_contract"]["primary_tool"], "plan_ncs_education_path")
        self.assertIn("recommend_training_transition", route["expected_tool_chain"])
        self.assertEqual(
            route["guide_prompt_template"]["id"],
            "education_system_from_transition",
        )
        self.assertEqual(
            route["route_contract"]["guide_prompt_template"]["expected_tool"],
            "plan_ncs_education_path",
        )
        self.assertEqual(route["guide_reference"]["reference_role"], "framework_reference")
        self.assertEqual(
            route["route_contract"]["guide_reference"]["source_hash_sha256"],
            route["guide_reference"]["source_hash_sha256"],
        )
        self.assertEqual(
            route["route_contract"]["route_fingerprint"],
            route["route_fingerprint"],
        )
        self.assertEqual(route["route_fingerprint"], repeated["route_fingerprint"])
        self.assertIn("recommend_training_transition", [step["tool"] for step in route["pipeline"]])

    def test_routes_guide_transition_prompt_with_actor_to_plan_tool(self) -> None:
        route = route_ncs_query(
            "\ub178\ubb34\uad00\ub9ac \ub2f4\ub2f9\uc790\uac00 "
            "\uc778\uc0ac\uae30\ud68d\uc73c\ub85c \uc804\ud658\ud558\uae30 \uc704\ud55c "
            "\uad50\uc721\ud6c8\ub828\uccb4\uacc4\ub97c \uc218\ub9bd\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertEqual(route["params"]["current_query"], "\ub178\ubb34\uad00\ub9ac")
        self.assertEqual(route["params"]["target_query"], "\uc778\uc0ac\uae30\ud68d")
        self.assertEqual(route["missing_params"], [])

    def test_routes_annual_operation_plan_prompt_to_plan_tool(self) -> None:
        route = route_ncs_query(
            "\ucd94\ucc9c\ub41c \uad50\uc721\uacfc\uc815\uc744 "
            "\uc5f0\uac04 \uc6b4\uc601\uacc4\ud68d \ucd08\uc548\uc73c\ub85c \uc815\ub9ac\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertEqual(route["guide_prompt_template"]["id"], "annual_operation_plan_draft")

    def test_routes_training_course_inventory_prompt_to_plan_tool(self) -> None:
        route = route_ncs_query(
            "\uc870\uc0ac\ub41c \uad50\uc721\uacfc\uc815\uc744 "
            "\ub0b4\ubd80/\uc678\ubd80 \uad6c\ubd84\uacfc "
            "\uad50\uc721\uc720\ud615 \uae30\uc900\uc73c\ub85c \uc815\ub9ac\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertEqual(route["guide_prompt_template"]["id"], "training_course_inventory_table")

    def test_routes_internal_training_questionnaire_prompt_to_plan_tool(self) -> None:
        route = route_ncs_query(
            "\ub0b4\ubd80 \uad50\uc721\uacfc\uc815 \uc218\uc9d1\uc744 \uc704\ud574 "
            "\uad50\uc721\uba85\u00b7\ub300\uc0c1\u00b7\ubaa9\uc801\u00b7\uc6b4\uc601\ubc29\uc2dd "
            "\uc870\uc0ac \uc9c8\ubb38\uc9c0\ub97c \ub9cc\ub4e4\uc5b4\uc918."
        )

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertEqual(route["guide_prompt_template"]["id"], "internal_training_intake_questionnaire")

    def test_routes_transition_without_system_word_to_transition_tool(self) -> None:
        query = "from labor management to HR planning reskilling path"

        route = route_ncs_query(query)

        self.assertEqual(route["scenario"], "training_transition")
        self.assertEqual(route["tool"], "recommend_training_transition")
        self.assertEqual(route["params"]["current_query"], "labor management")
        self.assertEqual(route["params"]["target_query"], "HR planning reskilling path")
        self.assertEqual(route["missing_params"], [])

    def test_routes_natural_korean_similar_task_phrase_to_task_transition(self) -> None:
        route = route_ncs_query(
            "\uc778\ub825\ucc44\uc6a9\uacfc \uc720\uc0ac\ud55c \uacfc\uc5c5 \uc804\ud658 \ucd94\ucc9c"
        )

        self.assertEqual(route["scenario"], "task_transition")
        self.assertEqual(route["tool"], "recommend_task_transitions")
        self.assertEqual(route["required_params"], ["query"])
        self.assertEqual(route["missing_params"], [])
        self.assertEqual(route["params"]["query"], "\uc778\ub825\ucc44\uc6a9")

    def test_strips_compound_training_intent_from_task_query(self) -> None:
        route = route_ncs_query("\uc778\uc0ac\uae30\ud68d \ud6c8\ub828\uacfc\uc815 \ucd94\ucc9c")

        self.assertEqual(route["scenario"], "task_training")
        self.assertEqual(route["params"]["query"], "\uc778\uc0ac\uae30\ud68d")

    def test_strips_evidence_analysis_suffix_from_scope_query(self) -> None:
        route = route_ncs_query(
            "\uc778\uc0ac\uae30\ud68d \uc628\ud1a8\ub85c\uc9c0 \uadfc\uac70 \ubd84\uc11d"
        )

        self.assertEqual(route["scenario"], "evidence_analysis")
        self.assertEqual(route["params"]["mode"], "ontology")
        self.assertEqual(route["params"]["query"], "\uc778\uc0ac\uae30\ud68d")

    def test_routes_evidence_query_to_analysis_mode(self) -> None:
        route = route_ncs_query("\uc790\uaca9 \uadfc\uac70 \ubd84\uc11d")

        self.assertEqual(route["scenario"], "evidence_analysis")
        self.assertEqual(route["tool"], "ncs_analysis")
        self.assertEqual(route["params"]["mode"], "qualification")

    def test_routes_explicit_ncs_search_to_structure_search_despite_planning_word(self) -> None:
        route = route_ncs_query("HR planning NCS search")

        self.assertEqual(route["scenario"], "structure_search")
        self.assertEqual(route["tool"], "ncs_search")
        self.assertEqual(route["params"]["query"], "HR planning")

    def test_structure_search_route_exposes_explicit_classification_filter_path(self) -> None:
        route = route_ncs_query("HR planning NCS search")

        context = route["classification_context"]
        self.assertTrue(context["supported"])
        self.assertEqual(context["parameter"], "classification_filter")
        self.assertEqual(context["mode"], "explicit_hard_filter")
        self.assertIn("major_code", context["fields"])
        self.assertEqual(
            route["route_contract"]["classification_context"],
            context,
        )

    def test_structure_search_route_carries_caller_classification_filter(self) -> None:
        route = route_ncs_query(
            "vehicle dispatch NCS search",
            classification_filter={"major_code": "02", "ignored": "drop"},
        )

        self.assertEqual(route["params"]["classification_filter"], {"major_code": "02"})
        context = route["classification_context"]
        self.assertTrue(context["provided"])
        self.assertEqual(context["filter"], {"major_code": "02"})
        self.assertEqual(
            route["route_contract"]["classification_context"]["filter"],
            {"major_code": "02"},
        )

    def test_routes_korean_unit_lookup_to_structure_search(self) -> None:
        cases = (
            "인사담당자 채용 직무 능력단위 찾기",
            "능력단위 검색",
            "능력단위 조회",
            "능력단위 목록 알려줘",
        )

        for query in cases:
            with self.subTest(query=query):
                route = route_ncs_query(query)
                self.assertEqual(route["scenario"], "structure_search")
                self.assertEqual(route["tool"], "ncs_search")
                self.assertGreater(route["score"], 60)

    def test_explicit_training_intent_overrides_lookup_verb(self) -> None:
        cases = (
            "능력단위 기반 교육 추천",
            "능력단위 찾아서 교육 추천해줘",
            "이 과업의 능력단위에 맞는 훈련과정 추천",
        )

        for query in cases:
            with self.subTest(query=query):
                route = route_ncs_query(query)
                self.assertEqual(route["scenario"], "task_training")
                self.assertEqual(route["tool"], "recommend_training_for_task")

    def test_routes_guide_job_structure_prompt_to_ncs_search(self) -> None:
        route = route_ncs_query(
            "\uc9c1\ubb34\uae30\ub2a5\uacfc \uc8fc\uc694\uc5c5\ubb34\ub97c "
            "\uae30\uc900\uc73c\ub85c NCS \uc9c1\ubb34\ubd84\ub958 "
            "\ud6c4\ubcf4\ub97c \ucc3e\uc544\uc918."
        )

        self.assertEqual(route["scenario"], "structure_search")
        self.assertEqual(route["tool"], "ncs_search")
        self.assertEqual(route["guide_prompt_template"]["id"], "job_structure_mapping")
        self.assertEqual(
            route["route_contract"]["guide_prompt_template"]["expected_tool"],
            "ncs_search",
        )

    def test_routes_guide_mapping_evidence_prompt_to_analysis(self) -> None:
        route = route_ncs_query(
            "\uc9c1\ubb34 \ubaa9\ub85d\uc744 NCS \ubd84\ub958\uc640 "
            "\ub9e4\ud551\ud55c \uadfc\uac70\ub97c \uc815\ub9ac\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "evidence_analysis")
        self.assertEqual(route["tool"], "ncs_analysis")
        self.assertEqual(route["guide_prompt_template"]["id"], "ncs_mapping_evidence_summary")
        self.assertEqual(
            route["route_contract"]["guide_prompt_template"]["expected_tool"],
            "ncs_analysis",
        )

    def test_routes_course_ksa_alignment_prompt_to_task_training(self) -> None:
        route = route_ncs_query(
            "\uc774 \uad50\uc721\uacfc\uc815\uc774 \ucda9\uc871\uc2dc\ud0a4\ub294 "
            "\uc9c0\uc2dd\u00b7\uae30\uc220\u00b7\ud0dc\ub3c4\ub97c \ubd84\uc11d\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "task_training")
        self.assertEqual(route["tool"], "recommend_training_for_task")
        self.assertEqual(route["guide_prompt_template"]["id"], "course_ksa_alignment")

    def test_routes_korean_training_goal_link_quality_review_to_operator_review(self) -> None:
        route = route_ncs_query(
            "\ud6c8\ub828\ubaa9\ud45c KSA \ub9c1\ud06c "
            "\ud488\uc9c8 \uc774\uc288\ub97c \uac80\ud1a0\ud574\uc57c \ud55c\ub2e4",
            available_tool_names={
                "ncs_search",
                "ncs_analysis",
                "recommend_training_for_task",
                "get_quality_issues",
                "review_training_goal_concept_link",
            },
        )

        self.assertEqual(route["scenario"], "operator_review")
        self.assertEqual(route["tool"], "get_quality_issues")
        self.assertTrue(route["available"])
        self.assertEqual(route["params"]["target_type"], "training_goal_concept_link")
        self.assertEqual(route["missing_params"], [])
        guard_codes = {flag["code"] for flag in route["guard_flags"]}
        self.assertIn("operator_review_route", guard_codes)
        self.assertNotIn("missing_required_params", guard_codes)

    def test_routes_ksa_definition_human_review_target_to_operator_review(self) -> None:
        route = route_ncs_query(
            "KSA \uc815\uc758 \uac80\ud1a0\uc640 human review "
            "\ub300\uc0c1\uc744 \uc6b4\uc601\uc790\uac00 "
            "\ud655\uc778\ud558\uace0 \uc2f6\ub2e4",
            available_tool_names={
                "ncs_search",
                "ncs_analysis",
                "recommend_training_for_task",
                "plan_ncs_education_path",
                "get_quality_issues",
            },
        )

        self.assertEqual(route["scenario"], "operator_review")
        self.assertEqual(route["tool"], "get_quality_issues")
        self.assertTrue(route["available"])
        self.assertEqual(route["params"]["target_type"], "ontology_concept")
        self.assertEqual(route["params"]["issue_type"], "human_review_required")
        self.assertEqual(route["missing_params"], [])
        self.assertTrue(
            route["route_contract"]["execution_policy"]["operator_review_requires_operator_surface"]
        )
        self.assertFalse(route["route_contract"]["execution_policy"]["meta_executable"])
        guard_codes = {flag["code"] for flag in route["guard_flags"]}
        self.assertIn("operator_review_route", guard_codes)

    def test_mixed_human_review_and_education_system_intent_prefers_plan_tool_when_plan_signals_dominate(
        self,
    ) -> None:
        route = route_ncs_query(
            "training system roadmap with KSA definition human review criteria",
            available_tool_names={
                "ncs_search",
                "ncs_analysis",
                "recommend_training_for_task",
                "recommend_training_transition",
                "plan_ncs_education_path",
                "get_quality_issues",
            },
        )

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertEqual(route["tool"], "plan_ncs_education_path")
        self.assertIn("current_query", route["missing_params"])
        guard_codes = {flag["code"] for flag in route["guard_flags"]}
        self.assertNotIn("operator_review_route", guard_codes)

    def test_mixed_human_review_and_training_transition_intent_prefers_transition_tool_when_transition_signals_dominate(
        self,
    ) -> None:
        route = route_ncs_query(
            "from labor management to HR planning reskilling path with "
            "KSA definition human review notes",
            available_tool_names={
                "ncs_search",
                "ncs_analysis",
                "recommend_training_for_task",
                "recommend_training_transition",
                "plan_ncs_education_path",
                "get_quality_issues",
            },
        )

        self.assertEqual(route["scenario"], "training_transition")
        self.assertEqual(route["tool"], "recommend_training_transition")
        self.assertEqual(route["params"]["current_query"], "labor management")
        self.assertIn("HR planning", route["params"]["target_query"])
        self.assertEqual(route["missing_params"], [])
        guard_codes = {flag["code"] for flag in route["guard_flags"]}
        self.assertNotIn("operator_review_route", guard_codes)

    def test_routes_job_course_mapping_framework_prompt_to_task_training(self) -> None:
        route = route_ncs_query(
            "\uc544\ub798 \uc9c1\ubb34\u00b7\uacfc\uc5c5\u00b7KSA \ud45c\ub97c "
            "\uae30\ubc18\uc73c\ub85c \uad50\uc721 \ub9e4\ud551 \uae30\uc900 "
            "\ud504\ub808\uc784\uc744 \uc124\uacc4\ud574\uc918."
        )

        self.assertEqual(route["scenario"], "task_training")
        self.assertEqual(route["tool"], "recommend_training_for_task")
        self.assertEqual(route["guide_prompt_template"]["id"], "job_course_mapping_framework")

    def test_marks_unavailable_operator_route(self) -> None:
        route = route_ncs_query(
            "\ud488\uc9c8 \uac80\ud1a0 \uc900\ube44\ub3c4",
            available_tool_names={"ncs_search", "ncs_analysis"},
        )

        self.assertEqual(route["scenario"], "operator_review")
        self.assertEqual(route["tool"], "get_quality_issues")
        self.assertFalse(route["available"])
        guard_codes = {flag["code"] for flag in route["guard_flags"]}
        self.assertIn("route_tool_unavailable", guard_codes)
        self.assertIn("operator_review_route", guard_codes)

    def test_public_claim_risk_is_flagged(self) -> None:
        flags = risk_flags_for_query(
            "\uacf5\uc2dd \uc2b9\uc778 \ubc0f \uc790\uaca9 \uc778\uc815\uc744 "
            "\ubc1b\uc740 AI-HR \uc2dc\uc2a4\ud15c\uc73c\ub85c \ud45c\ud604"
        )

        self.assertEqual(flags[0]["code"], "official_or_legal_claim_risk")
        self.assertEqual(flags[0]["severity"], "high")

    def test_missing_inputs_are_exposed_as_guard_flags(self) -> None:
        route = route_ncs_query("\uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub9cc\ub4e4\uc5b4\uc918")

        self.assertEqual(route["scenario"], "education_system_design")
        self.assertIn("current_query", route["missing_params"])
        guard = next(flag for flag in route["guard_flags"] if flag["code"] == "missing_required_params")
        self.assertIn("current_query", guard["params"])


class PlannerMetaRouteLineageTests(unittest.TestCase):
    def setUp(self) -> None:
        from ncs_mcp import server, tool_registry

        self.server = server
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(
            server, "current_mcp_tool_surface",
            return_value={"all_tools": sorted(tool_registry.NCS_EXECUTABLE_TOOL_NAMES)},
        ))
        self.stack.enter_context(patch.object(
            server, "load_settings", return_value=SimpleNamespace(advanced_tools_enabled=True),
        ))
        self.stack.enter_context(patch.object(server, "open_recommendation_db"))
        self.recommend = self.stack.enter_context(patch.object(
            server, "training_recommend_transition", return_value={"ok": True},
        ))
        self.stack.enter_context(patch.object(
            server, "training_compact_transition_response", side_effect=lambda result, **_: result,
        ))
        self.stack.enter_context(patch.object(
            server, "training_compact_education_plan_response", side_effect=lambda result, **_: result,
        ))
        self.intent = "노무관리에서 인사기획으로 교육훈련체계 수립"

    def test_scoped_discovery_and_meta_plan_share_one_fingerprint(self) -> None:
        scopes = (
            {"major_code": "02", "middle_code": "02", "small_code": "02", "sub_code": "01"},
            {
                "current_major_code": "01", "current_middle_code": "02",
                "current_small_code": "03", "current_sub_code": "04",
                "target_major_code": "02", "target_middle_code": "03",
                "target_small_code": "04", "target_sub_code": "05",
            },
        )
        for scope in scopes:
            for use_general_filter in (False, True):
                with self.subTest(scope=scope, use_general_filter=use_general_filter):
                    discovery = self.server.ncs_discover_tools(self.intent, classification_filter=scope)
                    route = discovery["query_route"]
                    params = {
                        "current_query": route["params"]["current_query"],
                        "target_query": route["params"]["target_query"],
                        "_route_query": self.intent,
                        "_route_fingerprint": route["route_fingerprint"],
                    }
                    if use_general_filter:
                        params["classification_filter"] = scope
                    else:
                        params.update(route["params"])
                    result = self.server.ncs_execute_tool("plan_ncs_education_path", params)
                    self.assertTrue(result["ok"], result)
                    for observed in (
                        result["route_fingerprint"],
                        result["query_route"]["route_fingerprint"],
                        result["query_route"]["route_contract"]["route_fingerprint"],
                        result["meta_execution"]["route_fingerprint"],
                        result["data"]["query_route"]["route_fingerprint"],
                    ):
                        self.assertEqual(observed, route["route_fingerprint"])
                    for field, value in route["params"].items():
                        if field.endswith("_code"):
                            self.assertEqual(self.recommend.call_args.kwargs[field], value)
                    self.assertNotIn("classification_filter", self.recommend.call_args.kwargs)

    def test_unscoped_fingerprint_cannot_authorize_scoped_plan(self) -> None:
        route = self.server.ncs_discover_tools(self.intent)["query_route"]
        fields = ["major_code"] + [
            f"{side}_{level}_code"
            for side in ("current", "target") for level in ("major", "middle", "small", "sub")
        ]
        for field in fields:
            with self.subTest(field=field):
                result = self.server.ncs_execute_tool("plan_ncs_education_path", {
                    **route["params"], field: "02", "_route_query": self.intent,
                    "_route_fingerprint": route["route_fingerprint"],
                })
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "route_fingerprint_mismatch")
        result = self.server.ncs_execute_tool("plan_ncs_education_path", {
            **route["params"], "classification_filter": {"major_code": "02"},
            "_route_query": self.intent, "_route_fingerprint": route["route_fingerprint"],
        })
        self.assertEqual(result["error"]["code"], "route_fingerprint_mismatch")
        self.recommend.assert_not_called()

    def test_empty_explicit_scope_does_not_override_validated_general_filter(self) -> None:
        route = self.server.ncs_discover_tools(
            self.intent, classification_filter={"major_code": "02"},
        )["query_route"]
        result = self.server.ncs_execute_tool("plan_ncs_education_path", {
            "classification_filter": {"major_code": "02"},
            "target_major_code": "", "current_query": None,
            "_route_query": self.intent, "_route_fingerprint": route["route_fingerprint"],
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.recommend.call_args.kwargs["target_major_code"], "02")
        self.assertEqual(self.recommend.call_args.kwargs["current_query"], route["params"]["current_query"])

    def test_meta_plan_without_discovery_uses_the_facade_route(self) -> None:
        result = self.server.ncs_execute_tool("plan_ncs_education_path", {
            "current_query": "노무관리", "target_query": "인사기획", "major_code": "02",
        })
        self.assertTrue(result["ok"], result)
        self.assertEqual(
            result["query_route"]["route_fingerprint"],
            result["meta_execution"]["route_fingerprint"],
        )

    def test_planner_does_not_silently_drop_classification_name_filter(self) -> None:
        result = self.server.ncs_discover_tools(
            self.intent, classification_filter={"major_name": "경영·회계·사무"},
        )
        self.assertFalse(result["ok"])
        self.recommend.assert_not_called()


if __name__ == "__main__":
    unittest.main()
