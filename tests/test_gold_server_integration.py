from __future__ import annotations

import inspect
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.query_router import route_ncs_query  # noqa: E402


class _Facade:
    def status(self):
        return {"status": "disabled", "runtime": {"password_present": False}}

    def internal_role_context(self, role_id, *, summary, limit):
        return {
            "schema": "ncs_gold_mcp_context_v1",
            "operation": "internal_role_context",
            "status": "ok",
            "role_id": role_id,
            "summary": summary,
            "limit": limit,
            "context": {"rows": []},
        }

    def semantic_context(self, query, *, entity_kind, top_k, limit):
        return {
            "schema": "ncs_gold_mcp_context_v1",
            "operation": "semantic_context",
            "status": "ok",
            "query": query,
            "entity_kind": entity_kind,
            "top_k": top_k,
            "limit": limit,
            "context": {"rows": []},
        }


class GoldServerIntegrationTests(unittest.TestCase):
    def test_existing_analysis_tool_routes_internal_role_context(self):
        with patch("ncs_mcp.server._get_gold_mcp_facade", return_value=_Facade()):
            result = server.ncs_analysis(
                mode="internal_role",
                internal_role_id="ijr-1",
                summary=True,
                limit=7,
            )
        self.assertEqual(result["operation"], "internal_role_context")
        self.assertEqual(result["role_id"], "ijr-1")
        self.assertEqual(result["limit"], 7)
        self.assertEqual(
            result["training_recommendation_chain"]["status"],
            "not_requested",
        )

    def test_existing_analysis_tool_routes_semantic_context(self):
        with patch("ncs_mcp.server._get_gold_mcp_facade", return_value=_Facade()):
            result = server.ncs_analysis(
                mode="semantic",
                query="\uc778\ub825\uc6b4\uc601\uacc4\ud68d \uc218\ub9bd",
                entity_kind="performance_element",
                top_k=3,
                limit=8,
            )
        self.assertEqual(result["operation"], "semantic_context")
        self.assertEqual(result["top_k"], 3)
        self.assertEqual(result["entity_kind"], "performance_element")

    def test_semantic_context_renderer_is_compact_and_keeps_graph_evidence(self):
        payload = {
            "operation": "semantic_context",
            "context": {
                "rows": [
                    {
                        "score": 0.91234,
                        "ncs_job_name": "인사",
                        "competency_unit_code": "0202020101_23v1",
                        "competency_unit_name": "인사기획",
                        "performance_criterion_id": "100",
                        "performance_criterion_text": "인력운영계획을 수립할 수 있다.",
                        "required_knowledge": ["정원산정 기법"],
                        "required_skills": ["인력예측 기술"],
                        "required_attitudes": ["전략적 분석 태도"],
                    }
                ],
                "audit": {"retrieval_method": "vector_search", "row_count": 1},
            },
        }

        rendered = server._render_ncs_analysis_markdown(payload)

        self.assertIsInstance(rendered, str)
        self.assertLessEqual(len(rendered or ""), 1_300)
        self.assertIn("인사기획", rendered or "")
        self.assertIn("100", rendered or "")
        self.assertIn("정원산정", rendered or "")
        self.assertIn("0.9123", rendered or "")

    def test_internal_role_renderer_is_compact_and_keeps_alignment_evidence(self):
        payload = {
            "operation": "internal_role_context",
            "context": {
                "rows": [
                    {
                        "internal_job_role_id": "ijr-1",
                        "internal_job_role_name": "People Partner",
                        "ncs_job_id": "ncs:ncs_job:02020201",
                        "ncs_job_name": "인사",
                        "ksa_concept_id": "ncs:ontology_concept:1",
                        "ksa_concept_name": "인력운영계획",
                        "ksa_concept_type": "skill",
                        "source_link_count": 4,
                    }
                ],
                "audit": {"retrieval_method": "role_alignment", "row_count": 1},
            },
        }

        rendered = server._render_ncs_analysis_markdown(payload)

        self.assertIsInstance(rendered, str)
        self.assertLessEqual(len(rendered or ""), 1_300)
        self.assertIn("People Partner", rendered or "")
        self.assertIn("인력운영계획", rendered or "")
        self.assertIn("4", rendered or "")

    def test_gold_resources_are_secret_free_and_active_ncs_only(self):
        schema = json.loads(server.gold_ontology_schema())
        self.assertEqual(schema["default_profile"], "serving_core")
        self.assertNotIn("SQF", repr(schema))
        with patch("ncs_mcp.server._get_gold_mcp_facade", return_value=_Facade()):
            status = json.loads(server.gold_runtime_status())
        self.assertEqual(status["status"], "disabled")

    def test_router_and_signature_expose_additive_modes_without_new_tool(self):
        role_route = route_ncs_query("\uc0ac\ub0b4 \uc9c1\ubb34 \ub9e4\ud551 \uadfc\uac70 \ubd84\uc11d")
        semantic_route = route_ncs_query("\uc2dc\ub9e8\ud2f1 \ubca1\ud130 \uadfc\uac70 \ubd84\uc11d")
        self.assertEqual(role_route["tool"], "ncs_analysis")
        self.assertEqual(role_route["params"]["mode"], "internal_role")
        self.assertIn("internal_role_id", role_route["missing_params"])
        self.assertEqual(semantic_route["params"]["mode"], "semantic")
        parameters = inspect.signature(server.ncs_analysis).parameters
        self.assertIn("internal_role_id", parameters)
        self.assertIn("top_k", parameters)
        self.assertIn("include_training", parameters)

    def test_internal_role_route_extracts_id_and_meta_execution_uses_it(self):
        route = route_ncs_query("internal role ijr-1 evidence analysis")
        self.assertEqual(route["params"]["internal_role_id"], "ijr-1")
        self.assertNotIn("internal_role_id", route["missing_params"])
        with patch("ncs_mcp.server._get_gold_mcp_facade", return_value=_Facade()):
            result = server.ncs_execute_tool(
                "ncs_analysis",
                {"_route_query": "internal role ijr-1 evidence analysis"},
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["role_id"], "ijr-1")

    def test_internal_role_meta_execution_fails_before_facade_without_id(self):
        result = server.ncs_execute_tool(
            "ncs_analysis",
            {"_route_query": "internal role mapping evidence analysis"},
        )
        self.assertEqual(result["error"]["code"], "route_required_params_missing")
        self.assertIn("internal_role_id", result["error"]["missing_params"])

    def test_internal_role_training_query_routes_and_executes_bounded_chain(self):
        class TrainingFacade(_Facade):
            def internal_role_context(self, role_id, *, summary, limit):
                result = super().internal_role_context(
                    role_id,
                    summary=summary,
                    limit=limit,
                )
                result["context"]["rows"] = [
                    {
                        "ncs_job_id": "ncs:job:02010101",
                        "ncs_job_name": "\uc778\uc0ac",
                        "ksa_concept_id": "ncs:ksa:1",
                    },
                    {
                        "ncs_job_id": "ncs:job:02010101",
                        "ncs_job_name": "\uc778\uc0ac",
                        "ksa_concept_id": "ncs:ksa:2",
                    },
                ]
                return result

        route_query = "\uc0ac\ub0b4 \uc9c1\ubb34 ijr_hr_manager NCS KSA \uad50\uc721 \ucd94\ucc9c"
        route = route_ncs_query(route_query)
        self.assertEqual(route["tool"], "ncs_analysis")
        self.assertTrue(route["params"]["include_training"])
        self.assertIn("recommend_training_for_task", route["expected_tool_chain"])
        recommendation = {"ok": True, "recommendations": [{"course": "fixture"}]}
        with patch(
            "ncs_mcp.server._get_gold_mcp_facade",
            return_value=TrainingFacade(),
        ), patch(
            "ncs_mcp.server.recommend_training_for_task",
            return_value=recommendation,
        ) as recommend:
            result = server.ncs_execute_tool(
                "ncs_analysis",
                {"_route_query": route_query},
            )
        chain = result["training_recommendation_chain"]
        self.assertEqual(chain["status"], "executed")
        self.assertEqual(chain["target_count"], 1)
        self.assertEqual(chain["results"][0]["result"], recommendation)
        recommend.assert_called_once_with(
            query="\uc778\uc0ac",
            limit=3,
            save=False,
            compact=True,
        )


if __name__ == "__main__":
    unittest.main()
