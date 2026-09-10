from __future__ import annotations

import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_readiness import (  # noqa: E402
    GoldProjectionProfile,
    GoldReadinessThresholds,
    SERVING_CORE_PROFILE,
    _table_columns,
    preflight_gold_projection,
)
from ncs_mcp.gold_lpg import build_gold_lpg_projection  # noqa: E402


SCHEMA = """
CREATE TABLE classifications (classification_id INTEGER PRIMARY KEY);
CREATE TABLE competency_units (unit_code TEXT PRIMARY KEY);
CREATE TABLE competency_elements (element_id INTEGER PRIMARY KEY, unit_code TEXT);
CREATE TABLE performance_criteria (criteria_id INTEGER PRIMARY KEY, element_id INTEGER);
CREATE TABLE ontology_concepts (concept_id INTEGER PRIMARY KEY);
CREATE TABLE criteria_concept_links (link_id INTEGER PRIMARY KEY, criteria_id INTEGER, concept_id INTEGER);
CREATE TABLE task_ksa_concept_relations (relation_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_training_courses (training_course_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_training_course_unit_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_training_course_concept_links (link_id INTEGER PRIMARY KEY, link_method TEXT);
CREATE TABLE ncs_training_course_element_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE training_goal_concept_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE training_delivery_relations (relation_id INTEGER PRIMARY KEY);
CREATE TABLE ontology_concept_relations (relation_id INTEGER PRIMARY KEY);
CREATE TABLE task_similarity_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_career_paths (career_path_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_qualification_items (qualification_item_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_unit_qualification_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_job_base_competencies (job_base_competency_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_job_base_factors (job_base_factor_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_unit_job_base_links (link_id INTEGER PRIMARY KEY);
"""


class GoldReadinessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "fixture.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(SCHEMA)
            conn.executemany("INSERT INTO classifications VALUES (?)", [(1,), (2,)])
            conn.executemany("INSERT INTO competency_units VALUES (?)", [("U1",), ("U2",)])
            conn.executemany("INSERT INTO competency_elements VALUES (?, ?)", [(10, "U1"), (11, "U2")])
            conn.executemany("INSERT INTO performance_criteria VALUES (?, ?)", [(100, 10), (101, 10), (102, 11)])
            conn.executemany("INSERT INTO ontology_concepts VALUES (?)", [(200,), (201,)])
            conn.executemany(
                "INSERT INTO criteria_concept_links VALUES (?, ?, ?)",
                [(300, 100, 200), (301, 101, 200), (302, 102, 201)],
            )
            conn.executemany("INSERT INTO task_ksa_concept_relations VALUES (?)", [(400,), (401,)])
            conn.execute("INSERT INTO ncs_training_courses VALUES (500)")
            conn.execute("INSERT INTO ncs_training_course_unit_links VALUES (501)")
            conn.execute("INSERT INTO ncs_training_course_concept_links (link_id) VALUES (502)")
            conn.execute("INSERT INTO ncs_training_course_element_links VALUES (503)")
            conn.execute("INSERT INTO training_goal_concept_links VALUES (504)")
            conn.execute("INSERT INTO training_delivery_relations VALUES (505)")
            conn.execute("INSERT INTO ontology_concept_relations VALUES (600)")
            conn.execute("INSERT INTO task_similarity_links VALUES (601)")
            conn.execute("INSERT INTO ncs_career_paths VALUES (602)")
            conn.execute("INSERT INTO ncs_qualification_items VALUES (603)")
            conn.execute("INSERT INTO ncs_unit_qualification_links VALUES (604)")
            conn.execute("INSERT INTO ncs_job_base_competencies VALUES (605)")
            conn.execute("INSERT INTO ncs_job_base_factors VALUES (606)")
            conn.execute("INSERT INTO ncs_unit_job_base_links VALUES (607)")
            conn.commit()

    def test_table_introspection_ignores_uncontracted_virtual_tables(self) -> None:
        class VirtualTableSensitiveConnection:
            def execute(self, sql: str):
                if sql == "SELECT name FROM sqlite_master WHERE type = 'table'":
                    return [("classifications",), ("dbstat",), ("application_cache",)]
                if '"dbstat"' in sql:
                    raise sqlite3.OperationalError("no such module: dbstat")
                if '"classifications"' in sql:
                    return [(0, "classification_id")]
                raise AssertionError(f"Unexpected introspection query: {sql}")

        columns = _table_columns(VirtualTableSensitiveConnection())  # type: ignore[arg-type]

        self.assertEqual(columns, {"classifications": {"classification_id"}})

    def test_serving_core_excludes_detailed_relations_and_keeps_summary(self) -> None:
        report = preflight_gold_projection(self.db_path)

        self.assertEqual(report["profile"]["name"], "serving_core")
        self.assertFalse(report["profile"]["include_task_ksa_detailed_relations"])
        self.assertEqual(report["table_counts"]["task_ksa_concept_relations"]["row_count"], 2)
        self.assertFalse(report["table_counts"]["task_ksa_concept_relations"]["profile_enabled"])
        self.assertEqual(report["estimates"]["distinct_element_concept_summary_edges"], 2)
        self.assertGreater(report["table_counts"]["training_goal_concept_links"]["row_count"], 0)
        self.assertIn("detailed task_ksa_concept_relations", " ".join(report["risk_reasons"]))

    def test_scope_contract_marks_sqlite_fallbacks_and_intentional_omissions(self) -> None:
        report = preflight_gold_projection(self.db_path)

        contract = report["scope_contract"]
        self.assertEqual(contract["projection_kind"], "hybrid_serving_core")
        self.assertTrue(contract["sqlite_authoritative"])
        self.assertFalse(contract["neo4j_projection_authoritative"])
        self.assertFalse(contract["complete_sqlite_ontology_replica"])
        fallback = {item["table"]: item for item in contract["fallback_evidence"]}
        self.assertEqual(fallback["ontology_concept_relations"]["row_count"], 1)
        self.assertEqual(fallback["ncs_unit_job_base_links"]["row_count"], 1)
        self.assertTrue(all(item["present"] for item in fallback.values()))
        omissions = {item["code"]: item for item in contract["intentional_omissions"]}
        self.assertEqual(omissions["task_ksa_concept_relations_omitted"]["row_count"], 2)
        self.assertEqual(omissions["inherited_course_concept_links_omitted"]["row_count"], 0)
        self.assertFalse(contract["human_approval_claim"])

    def test_serving_core_estimates_match_tiny_projection_v2(self) -> None:
        parity_path = Path(self.tmp.name) / "projection-parity.db"
        parity_schema = """
        CREATE TABLE classifications (
            classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
            middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
            sub_code TEXT, sub_name TEXT
        );
        CREATE TABLE competency_units (
            unit_code TEXT PRIMARY KEY, classification_id INTEGER, unit_name_raw TEXT
        );
        CREATE TABLE competency_elements (element_id INTEGER PRIMARY KEY, unit_code TEXT);
        CREATE TABLE performance_criteria (criteria_id INTEGER PRIMARY KEY, element_id INTEGER);
        CREATE TABLE ontology_concepts (concept_id INTEGER PRIMARY KEY, concept_name TEXT, concept_type TEXT);
        CREATE TABLE criteria_concept_links (
            link_id INTEGER PRIMARY KEY, criteria_id INTEGER, concept_id INTEGER, link_method TEXT
        );
        CREATE TABLE task_ksa_concept_relations (relation_id INTEGER PRIMARY KEY);
        CREATE TABLE ncs_training_courses (training_course_id INTEGER PRIMARY KEY, course_name TEXT);
        CREATE TABLE ncs_training_course_unit_links (
            link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT
        );
        CREATE TABLE ncs_training_course_concept_links (
            link_id INTEGER PRIMARY KEY, training_course_id INTEGER, concept_id INTEGER, link_method TEXT
        );
        CREATE TABLE ncs_training_course_element_links (
            link_id INTEGER PRIMARY KEY, training_course_id INTEGER, element_id INTEGER
        );
        CREATE TABLE training_goal_concept_links (
            link_id INTEGER PRIMARY KEY, training_course_id INTEGER, concept_id INTEGER
        );
        CREATE TABLE training_delivery_relations (
            relation_id INTEGER PRIMARY KEY, training_course_id INTEGER
        );
        """
        with closing(sqlite3.connect(parity_path)) as conn:
            conn.executescript(parity_schema)
            conn.execute("INSERT INTO classifications VALUES (1, '02', 'M', '01', 'Mi', '01', 'S', '01', 'Sub')")
            conn.execute("INSERT INTO competency_units VALUES ('U1', 1, 'Unit')")
            conn.execute("INSERT INTO competency_elements VALUES (10, 'U1')")
            conn.execute("INSERT INTO performance_criteria VALUES (100, 10)")
            conn.execute("INSERT INTO ontology_concepts VALUES (200, 'Knowledge', 'knowledge')")
            conn.execute("INSERT INTO criteria_concept_links VALUES (300, 100, 200, 'direct')")
            conn.execute("INSERT INTO task_ksa_concept_relations VALUES (301)")
            conn.execute("INSERT INTO ncs_training_courses VALUES (400, 'Course')")
            conn.execute("INSERT INTO ncs_training_course_unit_links VALUES (401, 400, 'U1')")
            conn.executemany(
                "INSERT INTO ncs_training_course_concept_links VALUES (?, 400, 200, ?)",
                [(402, 'goal_text'), (403, 'unit_ksa_concept_inherited')],
            )
            conn.execute("INSERT INTO ncs_training_course_element_links VALUES (404, 400, 10)")
            conn.execute("INSERT INTO training_goal_concept_links VALUES (405, 400, 200)")
            conn.execute("INSERT INTO training_delivery_relations VALUES (406, 400)")
            conn.commit()

        readiness = preflight_gold_projection(parity_path)
        projection = build_gold_lpg_projection(parity_path, profile=SERVING_CORE_PROFILE)

        self.assertEqual(readiness["estimates"]["estimated_nodes"], len(projection["nodes"]))
        self.assertEqual(readiness["estimates"]["estimated_edges"], len(projection["edges"]))
        self.assertEqual(readiness["estimates"]["distinct_element_concept_summary_edges"], 1)
        self.assertEqual(readiness["estimates"]["distinct_job_concept_summary_edges"], 1)
        self.assertEqual(readiness["estimates"]["valid_classification_job_rows"], 1)
        self.assertEqual(readiness["estimates"]["omitted_inherited_training_concept_link_rows"], 1)

        with closing(sqlite3.connect(parity_path)) as conn:
            conn.execute("UPDATE classifications SET middle_code = NULL WHERE classification_id = 1")
            conn.commit()
        missing_middle = preflight_gold_projection(parity_path)
        missing_middle_projection = build_gold_lpg_projection(parity_path, profile=SERVING_CORE_PROFILE)
        self.assertEqual(missing_middle["estimates"]["estimated_nodes"], len(missing_middle_projection["nodes"]))
        self.assertEqual(missing_middle["estimates"]["estimated_edges"], len(missing_middle_projection["edges"]))
        self.assertEqual(missing_middle["estimates"]["valid_classification_job_rows"], 0)
        self.assertEqual(missing_middle["estimates"]["distinct_job_concept_summary_edges"], 0)

        for blank_middle in ("", "   "):
            with closing(sqlite3.connect(parity_path)) as conn:
                conn.execute("UPDATE classifications SET middle_code = ? WHERE classification_id = 1", (blank_middle,))
                conn.commit()
            blank_report = preflight_gold_projection(parity_path)
            blank_projection = build_gold_lpg_projection(parity_path, profile=SERVING_CORE_PROFILE)
            self.assertEqual(blank_report["estimates"]["estimated_nodes"], len(blank_projection["nodes"]))
            self.assertEqual(blank_report["estimates"]["estimated_edges"], len(blank_projection["edges"]))
            self.assertEqual(blank_report["estimates"]["valid_classification_job_rows"], 0)
            self.assertEqual(blank_report["estimates"]["distinct_job_concept_summary_edges"], 0)

        with closing(sqlite3.connect(parity_path)) as conn:
            conn.execute("UPDATE classifications SET middle_code = '01' WHERE classification_id = 1")
            conn.execute("DELETE FROM competency_elements WHERE element_id = 10")
            conn.commit()
        orphan_element = preflight_gold_projection(parity_path)
        orphan_element_projection = build_gold_lpg_projection(parity_path, profile=SERVING_CORE_PROFILE)
        self.assertEqual(orphan_element["estimates"]["estimated_nodes"], len(orphan_element_projection["nodes"]))
        self.assertEqual(orphan_element["estimates"]["estimated_edges"], len(orphan_element_projection["edges"]))
        self.assertEqual(orphan_element["estimates"]["distinct_element_concept_summary_edges"], 0)

    def test_missing_optional_tables_are_safe_and_reported(self) -> None:
        sparse_path = Path(self.tmp.name) / "sparse.db"
        with closing(sqlite3.connect(sparse_path)) as conn:
            conn.execute("CREATE TABLE performance_criteria (criteria_id INTEGER PRIMARY KEY, element_id INTEGER)")
            conn.commit()

        report = preflight_gold_projection(sparse_path)

        self.assertEqual(report["table_counts"]["ncs_training_courses"]["row_count"], 0)
        self.assertFalse(report["table_counts"]["ncs_training_courses"]["present"])
        self.assertIn("optional tables unavailable", " ".join(report["risk_reasons"]))
        self.assertTrue(report["read_only"])
        self.assertFalse(report["db_writes"])

    def test_high_volume_refuses_in_memory_export_with_configured_limit(self) -> None:
        thresholds = GoldReadinessThresholds(
            max_in_memory_records=10,
            warning_in_memory_records=5,
            max_in_memory_bytes=100_000,
            warning_in_memory_bytes=50_000,
            streaming_batch_size=7,
        )
        report = preflight_gold_projection(self.db_path, thresholds=thresholds)

        self.assertEqual(report["risk_level"], "critical")
        self.assertFalse(report["in_memory_export_allowed"])
        self.assertTrue(report["recommended_execution"]["streaming_required"])
        self.assertEqual(report["recommended_execution"]["recommended_batch_size"], 7)

    def test_thresholds_and_output_are_deterministic(self) -> None:
        thresholds = {
            "max_in_memory_records": 100_000,
            "warning_in_memory_records": 50_000,
            "max_in_memory_bytes": 100_000_000,
            "warning_in_memory_bytes": 50_000_000,
        }
        first = preflight_gold_projection(self.db_path, thresholds=thresholds)
        second = preflight_gold_projection(self.db_path, thresholds=thresholds)

        self.assertEqual(first, second)
        self.assertEqual(first["thresholds"]["max_in_memory_records"], 100_000)
        expected_sampled_bytes = (
            self.db_path.stat().st_size
            if self.db_path.stat().st_size <= 65_536
            else 131_072
        )
        self.assertEqual(first["source_fingerprint"]["sampled_bytes"], expected_sampled_bytes)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            preflight_gold_projection(self.db_path, thresholds={"not_a_threshold": 1})

    def test_database_bytes_are_unchanged(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        profile = GoldProjectionProfile(
            name="with_detailed_relations", include_task_ksa_detailed_relations=True
        )

        report = preflight_gold_projection(self.db_path, profile=profile)

        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertTrue(report["read_only"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["status_mutation"])
        self.assertTrue(report["profile"]["include_task_ksa_detailed_relations"])
        self.assertGreater(report["estimates"]["estimated_edges"], 0)

    def test_default_profile_is_named_singleton_contract(self) -> None:
        self.assertEqual(SERVING_CORE_PROFILE, GoldProjectionProfile())


if __name__ == "__main__":
    unittest.main()
