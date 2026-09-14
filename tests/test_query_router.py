from __future__ import annotations

import unittest
import sys
import json
import os
from contextlib import ExitStack, nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_explicit_job_competency_request_extracts_bounded_scope_and_query(self) -> None:
        query = "\uC778\uC0AC \uC9C1\uBB34\uC5D0 \uD544\uC694\uD55C \uC5ED\uB7C9"

        route = route_ncs_query(query)
        repeated = route_ncs_query(query)

        self.assertEqual(route["scenario"], "structure_search")
        self.assertEqual(route["tool"], "ncs_search")
        self.assertEqual(route["params"]["query"], "인사")
        self.assertEqual(route["params"]["job_scope"], "인사")
        self.assertNotIn("classification_filter", route["params"])
        self.assertEqual(
            route["classification_context"]["source"],
            "explicit_query_job_scope",
        )
        self.assertEqual(
            route["classification_context"]["extraction"]["pattern"],
            "job_need_competency",
        )
        self.assertEqual(route["route_fingerprint"], repeated["route_fingerprint"])

    def test_optional_mcp_prefix_is_removed_from_job_scope(self) -> None:
        cases = (
            ("인사 직무에 필요한 역량을 알려줘.", "인사"),
            ("NCSMCP로 인사 직무에 필요한 역량을 알려줘.", "인사"),
            ("NCS MCP로 사회복지 업무에 필요한 역량을 알려줘.", "사회복지"),
        )

        for query, expected_scope in cases:
            with self.subTest(query=query):
                route = route_ncs_query(query)
                self.assertEqual(route["params"]["query"], expected_scope)
                self.assertEqual(route["params"]["job_scope"], expected_scope)

    def test_job_and_work_marker_particles_preserve_explicit_scope(self) -> None:
        cases = (
            "인사 직무의 필요역량",
            "인사 직무에서 필요한 역량",
            "인사 업무의 요구역량",
            "인사 업무에서 요구되는 능력",
            "인사 직무에 필요한 역량",
            "인사 직무 필요역량",
        )

        for query in cases:
            with self.subTest(query=query):
                route = route_ncs_query(query)
                self.assertEqual(route["params"]["query"], "인사")
                self.assertEqual(route["params"]["job_scope"], "인사")
                self.assertEqual(
                    route["classification_context"]["source"],
                    "explicit_query_job_scope",
                )

    def test_explicit_job_request_scopes_quoted_leaf(self) -> None:
        route = route_ncs_query(
            "NCSMCP 인사직무 필요역량의 '인사하기'에 해당하는 "
            "NCS 원문 근거를 찾아줘."
        )

        self.assertEqual(route["scenario"], "structure_search")
        self.assertEqual(route["tool"], "ncs_search")
        self.assertEqual(route["params"]["query"], "인사하기")
        self.assertEqual(route["params"]["job_scope"], "인사")
        self.assertTrue(
            route["classification_context"]["extraction"]["quoted_target"]
        )

    def test_bare_greeting_and_customer_greeting_do_not_infer_hr_scope(self) -> None:
        for query in ("인사하기", "고객 접객 인사하기"):
            with self.subTest(query=query):
                route = route_ncs_query(query)
                self.assertNotIn("job_scope", route["params"])
                self.assertNotIn("classification_filter", route["params"])
                self.assertNotEqual(
                    route["classification_context"].get("source"),
                    "explicit_query_job_scope",
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


class ExplicitJobScopeServerRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        from ncs_mcp import server, tool_registry

        self.server = server
        self.tool_registry = tool_registry
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(
            patch.object(
                server,
                "current_mcp_tool_surface",
                return_value={
                    "all_tools": sorted(tool_registry.NCS_EXECUTABLE_TOOL_NAMES)
                },
            )
        )
        self.stack.enter_context(
            patch.object(server, "open_db", side_effect=lambda: nullcontext(object()))
        )

    @staticmethod
    def _context(
        *,
        status: str = "resolved",
        classification_filter: dict[str, str] | None = None,
    ) -> dict[str, object]:
        selected = (
            {
                "major_code": "02",
                "middle_code": "02",
                "small_code": "02",
                "sub_code": "01",
                "path_label": "경영·회계·사무 > 총무·인사 > 인사·조직 > 인사",
                "confidence": 1.0,
                "match_basis": ["job_scope_exact_sub_name"],
            }
            if status in {"resolved", "conflict"}
            else None
        )
        return {
            "schema": "ncs_search_context_v1",
            "resolver_version": "ncs-classification-context-resolver-v2",
            "requested": {
                "context_text_present": False,
                "context_text_length": 0,
                "context_text_digest": None,
                "job_scope": "인사",
                "classification_filter": classification_filter,
            },
            "policy": {
                "query_inference_allowed": False,
                "soft_prior_source": "caller_supplied_context",
                "hard_filter_source": (
                    "caller_supplied" if classification_filter else None
                ),
                "lexical_tier_preserved": True,
                "rollout_phase": "shadow",
            },
            "selected_candidate": selected,
            "alternative_candidates": (
                [
                    {
                        "major_code": "91",
                        "middle_code": "01",
                        "small_code": "01",
                        "sub_code": "01",
                        "confidence": 1.0,
                    },
                    {
                        "major_code": "92",
                        "middle_code": "01",
                        "small_code": "01",
                        "sub_code": "01",
                        "confidence": 1.0,
                    },
                ]
                if status == "ambiguous"
                else []
            ),
            "alternative_count": 2 if status == "ambiguous" else 0,
            "resolution_margin": 0.0 if status == "ambiguous" else 1.0,
            "prior_applied": False,
            "hard_filter_applied": bool(classification_filter),
            "needs_context": status in {"ambiguous", "unresolved", "conflict"},
            "status": status,
            "warnings": [],
        }

    def test_exact_source_scope_is_promoted_to_hard_filter_deterministically(self) -> None:
        def resolve(*_args, classification_filter=None, **_kwargs):
            return self._context(classification_filter=classification_filter)

        with patch.object(
            self.server, "resolve_ncs_search_context", side_effect=resolve
        ):
            first = self.server.ncs_discover_tools(
                "NCSMCP로 인사 직무에 필요한 역량을 알려줘."
            )["query_route"]
            second = self.server.ncs_discover_tools(
                "NCSMCP로 인사 직무에 필요한 역량을 알려줘."
            )["query_route"]

        expected = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        self.assertEqual(first["params"]["query"], "인사")
        self.assertEqual(first["params"]["classification_filter"], expected)
        self.assertEqual(first["classification_context"]["filter"], expected)
        self.assertEqual(
            first["classification_context"]["mode"],
            "source_backed_hard_filter",
        )
        self.assertEqual(first["route_fingerprint"], second["route_fingerprint"])
        self.assertFalse(first["search_context"]["needs_context"])

    def test_caller_filter_wins_but_conflict_requires_context(self) -> None:
        caller_filter = {"major_code": "13"}

        def resolve(*_args, classification_filter=None, **_kwargs):
            return self._context(
                status="conflict",
                classification_filter=classification_filter,
            )

        with patch.object(
            self.server, "resolve_ncs_search_context", side_effect=resolve
        ):
            route = self.server.ncs_discover_tools(
                "NCSMCP로 인사 직무에 필요한 역량을 알려줘.",
                classification_filter=caller_filter,
            )["query_route"]

        self.assertEqual(route["params"]["classification_filter"], caller_filter)
        self.assertEqual(route["classification_context"]["filter"], caller_filter)
        self.assertTrue(route["search_context"]["needs_context"])
        self.assertFalse(
            route["route_contract"]["execution_policy"]["meta_executable"]
        )
        self.assertIn(
            "classification_context_needs_context",
            {flag["code"] for flag in route["guard_flags"]},
        )

    def test_ambiguous_explicit_job_scope_is_not_promoted(self) -> None:
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(status="ambiguous"),
        ):
            route = self.server.ncs_discover_tools(
                "NCSMCP로 중복기능 직무에 필요한 역량을 알려줘."
            )["query_route"]

        self.assertNotIn("classification_filter", route["params"])
        self.assertEqual(route["search_context"]["status"], "ambiguous")
        self.assertTrue(route["search_context"]["needs_context"])

    def test_direct_ambiguous_job_scope_fails_closed_before_search(self) -> None:
        search = Mock(return_value={"results": []})
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(status="ambiguous"),
        ), patch.object(self.server, "search_ncs", search):
            result = self.server.ncs_search(
                query="ambiguous task",
                job_scope="ambiguous",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_context_required")
        search.assert_not_called()

    def test_direct_job_scope_with_context_still_binds_a_hard_filter(self) -> None:
        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        search = Mock(
            return_value={
                "ok": True,
                "results": [
                    {
                        "id": "0202020101_23v3",
                        "path": dict(expected_filter),
                    }
                ],
                "classification_filter_applied": True,
            }
        )
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(),
        ), patch.object(self.server, "search_ncs", search):
            result = self.server.ncs_search(
                query="task",
                context_text="caller context",
                job_scope="job scope",
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(search.call_args.kwargs["classification_filter"], expected_filter)

    def test_direct_job_scope_conflicting_filter_fails_closed(self) -> None:
        search = Mock(return_value={"results": []})
        conflicting_filter = {"major_code": "13"}
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(
                status="conflict", classification_filter=conflicting_filter
            ),
        ), patch.object(self.server, "search_ncs", search):
            result = self.server.ncs_search(
                query="task",
                context_text="caller context",
                job_scope="job scope",
                classification_filter=conflicting_filter,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_context_required")
        search.assert_not_called()

    def test_direct_job_scope_compatible_parent_filter_is_allowed(self) -> None:
        compatible_filter = {"major_code": "02"}
        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        search = Mock(
            return_value={
                "ok": True,
                "results": [
                    {
                        "id": "0202020101_23v3",
                        "path": dict(expected_filter),
                    }
                ],
                "classification_filter_applied": True,
            }
        )
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(
                classification_filter=compatible_filter
            ),
        ), patch.object(self.server, "search_ncs", search):
            result = self.server.ncs_search(
                query="task",
                job_scope="job scope",
                classification_filter=compatible_filter,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(
            search.call_args.kwargs["classification_filter"], expected_filter
        )

    def test_direct_parent_filter_cannot_weaken_exact_job_scope(self) -> None:
        compatible_filter = {"major_code": "02"}
        search = Mock(
            return_value={
                "ok": True,
                "results": [
                    {
                        "id": "0202029901_23v3",
                        "path": {
                            "major_code": "02",
                            "middle_code": "02",
                            "small_code": "99",
                            "sub_code": "01",
                        },
                    }
                ],
                "classification_filter_applied": True,
            }
        )
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(classification_filter=compatible_filter),
        ), patch.object(self.server, "search_ncs", search):
            result = self.server.ncs_search(
                query="task",
                job_scope="job scope",
                classification_filter=compatible_filter,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["code"], "search_scope_containment_violation"
        )
        search.assert_called_once()

    def test_scope_validator_accepts_public_name_path_fields(self) -> None:
        payload = {
            "results": [
                {
                    "id": "unit-1",
                    "path": {
                        "major": "Major name",
                        "middle": "Middle name",
                        "small": "Small name",
                        "sub": "Sub name",
                    },
                }
            ]
        }

        self.assertIsNone(
            self.server._validate_ncs_search_scope_payload(
                payload,
                {"major_name": "Major name"},
            )
        )

    def test_scope_validator_uses_name_boundary_and_nfkc_matching(self) -> None:
        def validate(actual: str, expected: str) -> dict[str, object] | None:
            return self.server._validate_ncs_search_scope_payload(
                {"results": [{"path": {"major": actual}}]},
                {"major_name": expected},
            )

        self.assertIsNone(validate("Major name", "Ｍａｊｏｒ"))
        self.assertIsNone(validate("Major·name", "Major"))
        self.assertIsNotNone(validate("수출입", "출입"))

    def test_name_filter_accepts_classification_listing_payload(self) -> None:
        classification_filter = {"major_name": "Major name"}
        listing = {
            "classifications": [
                {
                    "classification_id": "major-1",
                    "major_name": "Major name",
                    "middle_name": "Middle name",
                    "small_name": "Small name",
                    "sub_name": "Sub name",
                }
            ]
        }
        with patch.object(
            self.server,
            "list_classifications",
            return_value=listing,
        ), patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(classification_filter=classification_filter),
        ):
            result = self.server.ncs_search(
                query="",
                classification_filter=classification_filter,
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["classifications"], listing["classifications"])

    def test_resolved_nonexact_scope_fails_closed_before_meta_handler(self) -> None:
        context = self._context()
        context["selected_candidate"]["confidence"] = 0.9
        context["selected_candidate"]["match_basis"] = [
            "job_scope_boundary_sub_name"
        ]
        handler = Mock(return_value={"ok": True})
        query = "NCSMCP로 접객 직무에 필요한 역량을 알려줘."

        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=context,
        ), patch.dict(
            self.server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": handler},
        ):
            route = self.server.ncs_discover_tools(query)["query_route"]
            result = self.server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "_route_query": query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertNotIn("classification_filter", route["params"])
        self.assertEqual(route["search_context"]["status"], "resolved")
        self.assertTrue(route["search_context"]["needs_context"])
        self.assertEqual(
            route["search_context"]["promotion_status"],
            "rejected_not_exact_unique_high_confidence",
        )
        self.assertFalse(
            route["route_contract"]["execution_policy"]["meta_executable"]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_context_required")
        handler.assert_not_called()

    def test_meta_execution_uses_promoted_filter_and_bound_fingerprint(self) -> None:
        calls: list[dict[str, object]] = []

        def resolve(*_args, classification_filter=None, **_kwargs):
            return self._context(classification_filter=classification_filter)

        def fake_search(
            query="",
            scope="all",
            limit=20,
            offset=0,
            classification_filter=None,
            context_text=None,
            job_scope=None,
        ):
            calls.append(
                {
                    "query": query,
                    "classification_filter": classification_filter,
                    "job_scope": job_scope,
                }
            )
            return {
                "ok": True,
                "results": [
                    {
                        "id": "0202020101_23v3",
                        "path": {
                            "major_code": "02",
                            "middle_code": "02",
                            "small_code": "02",
                            "sub_code": "01",
                        },
                    }
                ],
                "search_context": resolve(
                    classification_filter=classification_filter,
                    job_scope=job_scope,
                ),
            }

        query = "NCSMCP로 인사 직무에 필요한 역량을 알려줘."
        with patch.object(
            self.server, "resolve_ncs_search_context", side_effect=resolve
        ), patch.dict(
            self.server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": fake_search},
        ):
            route = self.server.ncs_discover_tools(query)["query_route"]
            result = self.server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "_route_query": query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertTrue(result["ok"], result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["query"], "인사")
        self.assertEqual(calls[0]["job_scope"], "인사")
        self.assertEqual(
            calls[0]["classification_filter"],
            {
                "major_code": "02",
                "middle_code": "02",
                "small_code": "02",
                "sub_code": "01",
            },
        )
        self.assertTrue(
            result["meta_execution"]["search_context_binding_verified"]
        )

    def test_caller_job_scope_route_matches_direct_binding_and_context_hash(self) -> None:
        route_test_query = "shared planning NCS search"
        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }

        def resolve(*_args, classification_filter=None, **_kwargs):
            return self._context(classification_filter=classification_filter)

        def fake_search(
            query="",
            scope="all",
            limit=20,
            offset=0,
            classification_filter=None,
            context_text=None,
            job_scope=None,
        ):
            return {
                "ok": True,
                "results": [
                    {
                        "id": "0202020101_23v3",
                        "path": dict(expected_filter),
                    }
                ],
                "classification_filter": classification_filter,
                "classification_filter_applied": True,
                "search_context": resolve(
                    classification_filter=classification_filter,
                    job_scope=job_scope,
                ),
            }

        query = "NCSMCP로 인사 직무에 필요한 역량을 알려줘."
        with patch.object(
            self.server, "resolve_ncs_search_context", side_effect=resolve
        ), patch.dict(
            self.server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": fake_search},
        ):
            route = self.server.ncs_discover_tools(
                route_test_query,
                context_text="caller context",
                job_scope="인사",
                classification_filter={"major_code": "02"},
            )["query_route"]
            result = self.server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "context_text": "caller context",
                    "_route_query": route_test_query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertEqual(route["params"]["classification_filter"], expected_filter)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["meta_execution"]["search_context_binding_verified"])
        self.assertEqual(
            result["meta_execution"]["search_context_hash"],
            route["route_contract"]["search_context_hash"],
        )

    def test_meta_execution_stops_ambiguous_query_derived_scope(self) -> None:
        query = "NCSMCP로 중복기능 직무에 필요한 역량을 알려줘."
        handler = Mock(return_value={"ok": True})
        with patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(status="ambiguous"),
        ), patch.dict(
            self.server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": handler},
        ):
            route = self.server.ncs_discover_tools(query)["query_route"]
            result = self.server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "_route_query": query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_context_required")
        handler.assert_not_called()

    def test_meta_execution_rejects_injected_off_scope_handler_payload(self) -> None:
        query = "NCSMCP로 인사 직무에 필요한 역량을 알려줘."
        handler = Mock(
            return_value={
                "ok": True,
                "results": [
                    {
                        "id": "1301020103_22v4",
                        "path": {
                            "major_code": "13",
                            "middle_code": "01",
                            "small_code": "02",
                            "sub_code": "01",
                        },
                    }
                ],
            }
        )

        def resolve(*_args, classification_filter=None, **_kwargs):
            return self._context(classification_filter=classification_filter)

        with patch.object(
            self.server, "resolve_ncs_search_context", side_effect=resolve
        ), patch.dict(
            self.server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": handler},
        ):
            route = self.server.ncs_discover_tools(query)["query_route"]
            result = self.server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "_route_query": query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"]["code"], "search_scope_containment_violation"
        )
        self.assertFalse(result["meta_execution"]["scope_containment_verified"])
        self.assertEqual(
            result["error"]["scope_containment"]["classification_filter"][
                "major_code"
            ],
            "02",
        )
        handler.assert_called_once()

    def test_filtered_not_found_forbids_unsupported_downstream_claims(self) -> None:
        classification_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        with patch.object(
            self.server,
            "search_ncs",
            return_value={"results": [], "search_context": {"status": "resolved"}},
        ), patch.object(
            self.server,
            "resolve_ncs_search_context",
            return_value=self._context(classification_filter=classification_filter),
        ):
            result = self.server.ncs_search(
                query="인사하기",
                classification_filter=classification_filter,
                job_scope="인사",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        self.assertIn("do not use it as evidence", result["ncs_evidence_guidance"])
        self.assertEqual(result["evidence_status"]["status"], "filtered_no_match")
        self.assertFalse(result["evidence_status"]["usable_as_evidence"])
        description = self.tool_registry.NCS_TOOL_PROFILES["ncs_search"]["description"]
        self.assertIn("classification_filter returned by ncs_discover_tools", description)
        self.assertIn("filtered NOT_FOUND", description)


class ExplicitJobScopeRealDbRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        canonical_db = ROOT / "data" / "processed" / "ncs.db"
        self.env_patch = patch.dict(
            os.environ,
            {"NCS_DB_PATH": str(canonical_db)},
            clear=False,
        )
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)

    def test_job_marker_particle_variants_bind_the_same_exact_hr_filter(self) -> None:
        from ncs_mcp import server

        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        for query in (
            "인사 직무의 필요역량",
            "인사 직무에서 필요한 역량",
        ):
            with self.subTest(query=query):
                route = server.ncs_discover_tools(query)["query_route"]
                result = server.ncs_execute_tool(
                    "ncs_search",
                    {
                        **route["params"],
                        "_route_query": query,
                        "_route_fingerprint": route["route_fingerprint"],
                    },
                )

                self.assertEqual(route["params"]["query"], "인사")
                self.assertEqual(route["params"]["job_scope"], "인사")
                self.assertEqual(
                    route["params"]["classification_filter"], expected_filter
                )
                self.assertTrue(result["ok"], result)
                self.assertTrue(result.get("results"), result)
                for row in result["results"]:
                    path = row.get("path") or {}
                    self.assertTrue(
                        all(path.get(key) == value for key, value in expected_filter.items()),
                        row,
                    )

    def test_direct_hr_scope_cannot_leak_hospitality_or_social_welfare_rows(self) -> None:
        from ncs_mcp import server

        result = server.ncs_search(
            query="\uC778\uC0AC",
            job_scope="\uC778\uC0AC",
            scope="all",
            limit=30,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"], result)
        self.assertTrue(result["classification_filter_applied"], result)
        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        for row in result["results"]:
            path = row.get("path") or {}
            self.assertTrue(
                all(path.get(key) == value for key, value in expected_filter.items()),
                row,
            )
            self.assertNotEqual(path.get("major_code"), "12")
            self.assertNotEqual(path.get("major_code"), "13")
            self.assertNotEqual(path.get("major_code"), "07")

    def test_direct_explicit_job_request_infers_and_binds_its_scope(self) -> None:
        from ncs_mcp import server

        result = server.ncs_search(
            query="\uC778\uC0AC \uC9C1\uBB34\uC5D0 \uD544\uC694\uD55C \uC5ED\uB7C9",
            scope="all",
            limit=30,
        )

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["query"], "\uC778\uC0AC")
        expected_filter = {
            "major_code": "02",
            "middle_code": "02",
            "small_code": "02",
            "sub_code": "01",
        }
        self.assertEqual(result["classification_filter"], expected_filter)
        self.assertEqual(
            result["search_context"]["requested"]["job_scope"], "인사"
        )
        self.assertEqual(result["search_context"]["status"], "resolved")
        self.assertEqual(
            result["search_context"]["policy"]["hard_filter_source"],
            "source_backed_exact_job_scope",
        )
        self.assertEqual(
            result["search_context"]["policy"]["soft_prior_source"],
            "explicit_query_job_scope",
        )
        self.assertTrue(
            all(
                all((row.get("path") or {}).get(key) == value for key, value in expected_filter.items())
                for row in result["results"]
            ),
            result,
        )

    def test_direct_bare_task_keeps_generic_unscoped_search(self) -> None:
        from ncs_mcp import server

        result = server.ncs_search(
            query="\uC778\uC0AC\uD558\uAE30",
            scope="all",
            limit=30,
        )

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["classification_filter_applied"], result)
        self.assertTrue(
            any((row.get("path") or {}).get("major_code") == "13" for row in result["results"]),
            result,
        )

    def test_direct_unknown_explicit_job_request_fails_closed(self) -> None:
        from ncs_mcp import server

        result = server.ncs_search(
            query="Unknown Synthetic Function \uC9C1\uBB34\uC5D0 \uD544\uC694\uD55C \uC5ED\uB7C9",
            scope="all",
            limit=30,
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "route_context_required")

    def test_hospitality_scope_still_returns_its_target_element(self) -> None:
        from ncs_mcp import server

        result = server.ncs_search(
            query="\uC778\uC0AC\uD558\uAE30",
            scope="element",
            classification_filter={"major_code": "13"},
            limit=10,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["results"], result)
        self.assertTrue(
            any("\uC778\uC0AC\uD558\uAE30" in str(row.get("text")) for row in result["results"]),
            result,
        )
        self.assertTrue(
            all((row.get("path") or {}).get("major_code") == "13" for row in result["results"]),
            result,
        )

    def test_nonexact_hospitality_scope_cannot_run_unfiltered_mixed_search(self) -> None:
        from ncs_mcp import server

        query = "접객 직무 필요역량"
        route = server.ncs_discover_tools(query)["query_route"]
        handler = Mock(return_value={"ok": True, "results": []})
        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"ncs_search": handler},
        ):
            result = server.ncs_execute_tool(
                "ncs_search",
                {
                    **route["params"],
                    "_route_query": query,
                    "_route_fingerprint": route["route_fingerprint"],
                },
            )

        self.assertEqual(route["params"]["query"], "접객")
        self.assertEqual(route["params"]["job_scope"], "접객")
        self.assertNotIn("classification_filter", route["params"])
        self.assertEqual(route["search_context"]["status"], "unresolved")
        self.assertTrue(route["search_context"]["needs_context"])
        self.assertEqual(
            route["search_context"]["promotion_status"],
            "rejected_not_exact_unique_high_confidence",
        )
        self.assertFalse(
            route["route_contract"]["execution_policy"]["meta_executable"]
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "route_context_required")
        handler.assert_not_called()


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
