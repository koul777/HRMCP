from __future__ import annotations

import unittest

from ncs_mcp.contracts import (
    AIHR_TRAINING_SYSTEM_GUIDE_TRACE_REQUIRED_CHECKS,
    AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
    PLAN_NCS_EDUCATION_PATH_TOOL,
    QUERY_ROUTE_SCHEMA,
)
from ncs_mcp.hrd_guide_reference import fallback_hrd_guide_reference_index
from ncs_mcp.query_router import route_ncs_query


class ContractsTests(unittest.TestCase):
    def test_shared_contract_constants_have_expected_values(self) -> None:
        self.assertEqual(QUERY_ROUTE_SCHEMA, "ncs_query_route_v1")
        self.assertEqual(PLAN_NCS_EDUCATION_PATH_TOOL, "plan_ncs_education_path")
        self.assertEqual(AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA, "aihr_training_system_guide_trace_v1")
        self.assertEqual(
            AIHR_TRAINING_SYSTEM_GUIDE_TRACE_REQUIRED_CHECKS,
            ("job_scope", "task_ksa", "course_link", "required_optional", "level_delivery", "human_review"),
        )

    def test_shared_contract_constants_are_used_by_runtime_paths(self) -> None:
        index = fallback_hrd_guide_reference_index()
        self.assertEqual(index["guide_trace_contract"]["schema"], AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA)

        templates = {item["id"]: item for item in index["prompt_scenario_templates"]}
        self.assertEqual(templates["education_system_from_transition"]["expected_tool"], PLAN_NCS_EDUCATION_PATH_TOOL)

        route = route_ncs_query("교육훈련체계 설계")
        self.assertEqual(route["schema"], QUERY_ROUTE_SCHEMA)
        self.assertEqual(route["tool"], PLAN_NCS_EDUCATION_PATH_TOOL)
        self.assertEqual(route["route_contract"]["schema"], QUERY_ROUTE_SCHEMA)

        legacy_route = route_ncs_query("SQF 학습모듈 기반 교육훈련체계 설계")
        execution_policy = legacy_route["route_contract"]["execution_policy"]
        self.assertIs(execution_policy["legacy_sqf_or_learning_module_inactive"], True)
        self.assertIs(execution_policy["legacy_sqf_or_learning_module_mentioned"], True)
        self.assertIs(execution_policy["legacy_sqf_or_learning_module_requested"], True)

        legacy_opt_out_route = route_ncs_query("do not use sqf; training system design for HR planning")
        opt_out_policy = legacy_opt_out_route["route_contract"]["execution_policy"]
        self.assertIs(opt_out_policy["legacy_sqf_or_learning_module_inactive"], True)
        self.assertIs(opt_out_policy["legacy_sqf_or_learning_module_mentioned"], True)
        self.assertIs(opt_out_policy["legacy_sqf_or_learning_module_requested"], False)

        neutral_sqf_route = route_ncs_query("why is sqf inactive?")
        neutral_sqf_policy = neutral_sqf_route["route_contract"]["execution_policy"]
        self.assertIs(neutral_sqf_policy["legacy_sqf_or_learning_module_inactive"], True)
        self.assertIs(neutral_sqf_policy["legacy_sqf_or_learning_module_mentioned"], True)
        self.assertIs(neutral_sqf_policy["legacy_sqf_or_learning_module_requested"], False)

        neutral_learning_module_route = route_ncs_query("why are learning modules inactive?")
        neutral_learning_module_policy = neutral_learning_module_route["route_contract"]["execution_policy"]
        self.assertIs(neutral_learning_module_policy["legacy_sqf_or_learning_module_inactive"], True)
        self.assertIs(neutral_learning_module_policy["legacy_sqf_or_learning_module_mentioned"], True)
        self.assertIs(neutral_learning_module_policy["legacy_sqf_or_learning_module_requested"], False)

        learning_module_request_route = route_ncs_query(
            "use learning module evidence for training system design"
        )
        learning_module_request_policy = learning_module_request_route["route_contract"]["execution_policy"]
        self.assertIs(learning_module_request_policy["legacy_sqf_or_learning_module_inactive"], True)
        self.assertIs(learning_module_request_policy["legacy_sqf_or_learning_module_mentioned"], True)
        self.assertIs(learning_module_request_policy["legacy_sqf_or_learning_module_requested"], True)
