from __future__ import annotations

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.query_router import route_ncs_query, risk_flags_for_query


class NcsQueryRouterTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
