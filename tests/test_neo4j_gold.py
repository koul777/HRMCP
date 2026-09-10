from __future__ import annotations

from pathlib import Path
import sys
import traceback
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.neo4j_gold import (  # noqa: E402
    CURRENT_VECTOR_SEARCH_CYPHER,
    INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER,
    INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER,
    LEGACY_VECTOR_SEARCH_CYPHER,
    Neo4jGoldReadClient,
    Neo4jGoldUnavailableError,
    Neo4jGoldValidationError,
    VECTOR_INDEX_ALLOWLIST,
    VECTOR_SEARCH_LEGACY,
)


class FakeDriver:
    def __init__(self, result=None, error: Exception | None = None):
        self.result = result if result is not None else ([], None, [])
        self.error = error
        self.calls = []

    def execute_query(self, query, **kwargs):
        required = {"parameters_", "database_", "routing_"}
        if set(kwargs) != required:
            raise AssertionError(f"expected driver kwargs {required}, got {set(kwargs)}")
        if kwargs["routing_"] != "r":
            raise AssertionError("adapter must use Neo4j read routing value 'r'")
        self.calls.append((query, kwargs))
        if self.error:
            raise self.error
        return self.result


class Neo4jGoldReadClientTests(unittest.TestCase):
    def client(self, fake: FakeDriver, **kwargs):
        return Neo4jGoldReadClient(
            fake.execute_query, database="gold", embedding_dimensions=3, **kwargs
        )

    def test_role_subgraph_is_parameterized_read_only_and_public_safe(self):
        fake = FakeDriver(([
            {
                "internal_job_role_id": "role:1",
                "ncs_job_id": "job:1",
                "competency_unit_code": "10010101",
                "performance_element_id": "17",
                "ksa_concept_name": "process control",
                "projection_version": "v1",
                "internal_neo4j_id": 99,
                "password": "never-returned",
            }
        ], None, []))
        result = self.client(fake).internal_role_subgraph("role:1", limit=4)

        query, kwargs = fake.calls[0]
        self.assertEqual(query, INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER)
        self.assertIn("role.display_name AS internal_job_role_name", query)
        self.assertNotIn("role.name AS internal_job_role_name", query)
        self.assertEqual(kwargs["parameters_"], {"internal_role_id": "role:1", "limit": 4})
        self.assertEqual(kwargs["database_"], "gold")
        self.assertEqual(kwargs["routing_"], "r")
        self.assertNotIn("role:1", query)
        self.assertNotIn("internal_neo4j_id", result["rows"][0])
        self.assertNotIn("password", result["rows"][0])
        self.assertEqual(result["audit"]["retrieval_method"], "role_alignment_subgraph")
        self.assertFalse(result["audit"]["db_writes"])

    def test_role_job_ksa_summary_uses_fixed_two_hop_query_and_bound(self):
        fake = FakeDriver(([{
            "internal_job_role_id": "role-1",
            "ncs_job_id": "job-1",
            "ksa_concept_id": "200",
            "source_link_count": 4,
            "distinct_unit_count": 2,
            "distinct_element_count": 3,
            "distinct_criteria_count": 4,
            "status_distribution_json": '{"candidate":2,"raw":2}',
        }], None, []))
        result = self.client(fake).internal_role_job_ksa_summary("role-1", limit=25)

        query, kwargs = fake.calls[0]
        self.assertEqual(query, INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER)
        self.assertIn("-[:ALIGNED_TO]->(job:LpgNode:NCSJob)", query)
        self.assertIn("[summary:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]", query)
        self.assertIn("ORDER BY source_link_count DESC, distinct_criteria_count DESC", query)
        self.assertNotIn("CompetencyUnit", query)
        self.assertEqual(kwargs["parameters_"], {"internal_role_id": "role-1", "limit": 25})
        self.assertEqual(result["audit"]["graph_depth"], 2)
        self.assertEqual(result["audit"]["retrieval_method"], "role_alignment_job_ksa_summary")
        self.assertEqual(result["rows"][0]["source_link_count"], 4)
        with self.assertRaises(Neo4jGoldValidationError):
            self.client(FakeDriver()).internal_role_job_ksa_summary("role-1", limit=251)
        with self.assertRaises(Neo4jGoldValidationError):
            self.client(FakeDriver()).internal_role_job_ksa_summary("role-1", max_hops=4)

    def test_role_job_ksa_summary_orders_by_evidence_then_stable_ids(self):
        fake = FakeDriver(([
            {"internal_job_role_id": "role", "ncs_job_id": "job-z", "ksa_concept_id": "z", "source_link_count": 2, "distinct_criteria_count": 9},
            {"internal_job_role_id": "role", "ncs_job_id": "job-b", "ksa_concept_id": "b", "source_link_count": 5, "distinct_criteria_count": 1},
            {"internal_job_role_id": "role", "ncs_job_id": "job-c", "ksa_concept_id": "b", "source_link_count": 5, "distinct_criteria_count": 3},
            {"internal_job_role_id": "role", "ncs_job_id": "job-a", "ksa_concept_id": "a", "source_link_count": 5, "distinct_criteria_count": 3},
        ], None, []))

        result = self.client(fake).internal_role_job_ksa_summary("role")

        self.assertEqual(
            [(row["ncs_job_id"], row["ksa_concept_id"]) for row in result["rows"]],
            [("job-a", "a"), ("job-c", "b"), ("job-b", "b"), ("job-z", "z")],
        )

    def test_current_vector_search_uses_fixed_allowlisted_search_query(self):
        fake = FakeDriver(([
            {"score": 0.2, "performance_criterion_id": "1"},
            {"score": 0.9, "performance_criterion_id": "9", "projection_freshness": "2026-09-10"},
        ], None, []))
        result = self.client(fake).search_performance_criteria([0.1, 0.2, 0.3], top_k=2, limit=2)

        query, kwargs = fake.calls[0]
        self.assertEqual(query, CURRENT_VECTOR_SEARCH_CYPHER["performance_criterion"])
        self.assertIn("SEARCH seed IN", query)
        self.assertNotIn(VECTOR_INDEX_ALLOWLIST["performance_element"], query)
        self.assertEqual(kwargs["parameters_"]["embedding"], [0.1, 0.2, 0.3])
        self.assertNotIn("index_name", kwargs["parameters_"])
        self.assertEqual([row["score"] for row in result["rows"]], [0.9, 0.2])
        self.assertEqual(result["audit"]["projection_freshness"], "2026-09-10")

    def test_driver_record_data_is_materialized_before_public_field_filtering(self):
        class DriverRecord(dict):
            # neo4j.Record reports Mapping but uses tuple/value membership.
            def __contains__(self, value):
                return value in self.values()

            def data(self):
                return dict(self.items())

        fake = FakeDriver(([
            DriverRecord({
                "performance_criterion_id": "criterion-1",
                "performance_criterion_text": "Plan workforce demand.",
                "ncs_job_name": "HR planning",
                "competency_unit_name": "Workforce planning",
                "ksa_concept_name": "workforce forecasting",
                "score": 0.91,
            })
        ], None, []))

        result = self.client(fake).search_performance_criteria([0.1, 0.2, 0.3])

        row = result["rows"][0]
        self.assertEqual(row["performance_criterion_id"], "criterion-1")
        self.assertEqual(row["ncs_job_name"], "HR planning")
        self.assertEqual(row["competency_unit_name"], "Workforce planning")
        self.assertEqual(row["ksa_concept_name"], "workforce forecasting")
        self.assertEqual(row["score"], 0.91)

    def test_legacy_vector_search_receives_only_allowlisted_index_parameter(self):
        fake = FakeDriver(([
            {
                "score": 0.1,
                "performance_element_id": "1",
                "ncs_job_name": "Lower score",
                "ksa_concept_name": "analysis",
            },
            {
                "score": 0.9,
                "performance_element_id": "9",
                "ncs_job_name": "HR planning",
                "ksa_concept_name": "analysis",
            },
        ], None, []))
        client = self.client(fake, vector_search_capability=VECTOR_SEARCH_LEGACY)
        result = client.search_performance_elements([1, 2, 3])

        query, kwargs = fake.calls[0]
        self.assertEqual(query, LEGACY_VECTOR_SEARCH_CYPHER["performance_element"])
        self.assertEqual(
            kwargs["parameters_"]["index_name"],
            VECTOR_INDEX_ALLOWLIST["performance_element"],
        )
        self.assertIn("db.index.vector.queryNodes", query)
        self.assertIn("WITH seed, seed AS element, score", query)
        self.assertEqual(result["rows"][0]["ncs_job_name"], "HR planning")
        self.assertEqual([row["score"] for row in result["rows"]], [0.9, 0.1])

    def test_element_templates_preserve_seed_for_projection_metadata(self):
        for templates in (CURRENT_VECTOR_SEARCH_CYPHER, LEGACY_VECTOR_SEARCH_CYPHER):
            template = templates["performance_element"]
            self.assertIn("WITH seed, seed AS element, score", template)
            self.assertIn("collect(DISTINCT CASE WHEN ksa.concept_type = 'skill'", template)
            self.assertIn("AS required_skills", template)
            self.assertIn("coalesce(seed.projection_fingerprint", template)
        self.assertIn("OPTIONAL MATCH (seed)-[:REQUIRES_KNOWLEDGE", CURRENT_VECTOR_SEARCH_CYPHER["performance_criterion"])
        self.assertIn("OPTIONAL MATCH (element)-[:REQUIRES_KNOWLEDGE", CURRENT_VECTOR_SEARCH_CYPHER["performance_element"])

    def test_task_vector_rows_keep_bounded_grouped_ksa_context(self):
        fake = FakeDriver(([{
            "score": 0.8,
            "performance_criterion_id": "criterion-1",
            "required_knowledge": ["workforce model"],
            "required_skills": ["forecasting"],
            "required_attitudes": ["strategic thinking"],
            "knowledge_count": 1,
            "skill_count": 1,
            "attitude_count": 1,
            "ksa_concept_count": 3,
            "ksa_lists_truncated": False,
        }], None, []))

        result = self.client(fake).search_performance_criteria([0.1, 0.2, 0.3])

        row = result["rows"][0]
        self.assertEqual(row["required_knowledge"], ["workforce model"])
        self.assertEqual(row["required_skills"], ["forecasting"])
        self.assertEqual(row["required_attitudes"], ["strategic thinking"])
        self.assertEqual(row["knowledge_count"], 1)
        self.assertEqual(row["skill_count"], 1)
        self.assertEqual(row["attitude_count"], 1)
        self.assertEqual(row["ksa_concept_count"], 3)
        self.assertFalse(row["ksa_lists_truncated"])

    def test_ksa_vector_search_uses_common_concept_index_and_task_context(self):
        fake = FakeDriver(([{
            "score": 0.8,
            "ksa_concept_id": "ksa-1",
            "ksa_concept_name": "인력예측 기술",
            "performance_element_id": "element-1",
            "ncs_job_id": "job-1",
        }], None, []))
        result = self.client(fake).search_ksa_concepts([0.1, 0.2, 0.3], top_k=3)

        query, kwargs = fake.calls[0]
        self.assertEqual(query, CURRENT_VECTOR_SEARCH_CYPHER["ksa_concept"])
        self.assertIn("VECTOR INDEX ncs_lpg_ksa_concept_embedding", query)
        self.assertIn("->(seed)", query)
        self.assertNotIn("index_name", kwargs["parameters_"])
        self.assertEqual(result["rows"][0]["ksa_concept_name"], "인력예측 기술")

    def test_validation_blocks_bad_dimensions_bounds_and_index_kind_before_io(self):
        fake = FakeDriver()
        client = self.client(fake)
        with self.assertRaises(Neo4jGoldValidationError):
            client.search_performance_criteria([0.1, 0.2])
        with self.assertRaises(Neo4jGoldValidationError):
            client.vector_graph_expansion([0.1, 0.2, 0.3], entity_kind="arbitrary")
        with self.assertRaises(Neo4jGoldValidationError):
            client.search_performance_criteria([0.1, 0.2, 0.3], top_k=101)
        with self.assertRaises(Neo4jGoldValidationError):
            client.internal_role_subgraph("role", max_hops=1)
        with self.assertRaises(Neo4jGoldValidationError):
            Neo4jGoldReadClient(
                fake.execute_query, database="gold", embedding_dimensions=4_097
            )
        self.assertEqual(fake.calls, [])

    def test_fixed_depth_is_reported_without_a_misleading_caller_hop_value(self):
        fake = FakeDriver()
        result = self.client(fake).internal_role_subgraph("role")
        self.assertEqual(result["audit"]["graph_depth"], 4)
        self.assertNotIn("max_hops", result["audit"])

    def test_backend_errors_are_typed_for_sqlite_fallback(self):
        client = self.client(FakeDriver(error=RuntimeError("backend credential details")))
        with self.assertRaises(Neo4jGoldUnavailableError) as raised:
            client.search_performance_criteria([0.1, 0.2, 0.3])
        self.assertNotIn("credential", str(raised.exception).lower())

    def test_backend_and_record_shape_tracebacks_do_not_expose_driver_details(self):
        secret = "neo4j-password=secret"

        class MalformedRecord:
            def __iter__(self):
                raise RuntimeError(secret)

        cases = (
            self.client(FakeDriver(error=RuntimeError(secret))),
            self.client(FakeDriver(([MalformedRecord()], None, []))),
        )
        for client in cases:
            with self.assertRaises(Neo4jGoldUnavailableError) as raised:
                client.search_performance_criteria([0.1, 0.2, 0.3])
            formatted = "".join(traceback.format_exception(raised.exception))
            self.assertNotIn(secret, formatted)

    def test_templates_contain_no_write_keywords_or_internal_id_function(self):
        templates = [
            INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER,
            INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER,
        ]
        templates.extend(CURRENT_VECTOR_SEARCH_CYPHER.values())
        templates.extend(LEGACY_VECTOR_SEARCH_CYPHER.values())
        forbidden = ("CREATE", "MERGE", "SET ", "DELETE", "REMOVE", "DROP", "id(")
        for template in templates:
            upper = template.upper()
            self.assertTrue(all(token.upper() not in upper for token in forbidden), template)


if __name__ == "__main__":
    unittest.main()
