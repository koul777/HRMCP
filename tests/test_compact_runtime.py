from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.knowledge_graph import build_ncs_knowledge_graph
from ncs_mcp.training_recommendation import recommend_training_for_task
from scripts.export_interview_serving_db import (
    PROFILE_VERCEL_ONTOLOGY_COMPACT,
    export_serving_db,
)
from tests.test_training_recommendation import seed_task_ontology


class CompactRuntimeParityTests(unittest.TestCase):
    def _create_source(self, path: Path) -> dict[str, int | str]:
        conn = connect(path)
        try:
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            criteria_id = int(fixture["criteria_id"])
            concept_id = int(fixture["concept_id"])
            unit_code = str(fixture["unit_code"])
            element_id = int(
                conn.execute(
                    "SELECT element_id FROM performance_criteria WHERE criteria_id = ?",
                    (criteria_id,),
                ).fetchone()["element_id"]
            )
            conn.execute(
                """
                INSERT INTO criteria_concept_links(
                    criteria_id, concept_id, relation_type, link_status, created_at
                ) VALUES (?, ?, 'related', 'raw', ?)
                """,
                (criteria_id, concept_id, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_training_courses(
                    ncs_cl_cd, compe_unit_name, compe_unit_level,
                    ncs_lclas_cd, ncs_mclas_cd, ncs_sclas_cd, ncs_subd_cd,
                    train_goal, train_time, fac_name, meth_name, api_fetched_at
                ) VALUES (?, 'HR planning practice', '5', '02', '02', '02', '01',
                          'Build workforce planning capability.', '24',
                          'training room', 'practice', ?)
                """,
                (unit_code, timestamp),
            )
            course_id = int(
                conn.execute(
                    "SELECT MAX(training_course_id) FROM ncs_training_courses"
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO ncs_training_course_unit_links(
                    training_course_id, unit_code, link_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES (?, ?, 'ncs_cl_cd_exact', 1.0, 'raw', ?, ?)
                """,
                (course_id, unit_code, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_training_course_concept_links(
                    training_course_id, unit_code, concept_id, link_method,
                    confidence_score, evidence_text, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'training_goal_concept_text', 1.0,
                          'workforce planning', 'raw', ?, ?)
                """,
                (course_id, unit_code, concept_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_training_course_element_links(
                    training_course_id, unit_code, element_id, link_method,
                    confidence_score, evidence_text, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'ncs_unit_element', 1.0,
                          'Plan workforce', 'raw', ?, ?)
                """,
                (course_id, unit_code, element_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO training_goal_concept_links(
                    training_course_id, unit_code, element_id, concept_id,
                    link_method, confidence_score, evidence_text, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'training_goal_concept_text', 1.0,
                          'workforce planning', 'raw', ?, ?)
                """,
                (
                    course_id,
                    unit_code,
                    element_id,
                    concept_id,
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
            return {**fixture, "course_id": course_id}
        finally:
            conn.close()

    def test_public_graph_and_task_recommendation_keep_result_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source.db"
            compact = root / "compact.db"
            fixture = self._create_source(source)

            source_conn = connect(source)
            try:
                source_result = recommend_training_for_task(
                    source_conn,
                    criteria_id=int(fixture["criteria_id"]),
                    limit=3,
                    save=False,
                )
            finally:
                source_conn.close()
            source_graph = build_ncs_knowledge_graph(
                source,
                unit_code=str(fixture["unit_code"]),
            )

            export_serving_db(
                source,
                compact,
                profile=PROFILE_VERCEL_ONTOLOGY_COMPACT,
            )
            compact_conn = sqlite3.connect(compact)
            compact_conn.row_factory = sqlite3.Row
            try:
                compact_result = recommend_training_for_task(
                    compact_conn,
                    criteria_id=int(fixture["criteria_id"]),
                    limit=3,
                    save=False,
                )
            finally:
                compact_conn.close()
            compact_graph = build_ncs_knowledge_graph(
                compact,
                unit_code=str(fixture["unit_code"]),
            )

            self.assertTrue(source_result["ok"])
            self.assertTrue(compact_result["ok"])
            self.assertEqual(set(source_result), set(compact_result))
            self.assertEqual(
                source_result["source_task"]["criteria_id"],
                compact_result["source_task"]["criteria_id"],
            )
            self.assertEqual(
                {
                    concept["concept_id"]
                    for item in source_result["recommendations"]
                    for concept in item["source_task_ksa_concepts"]
                },
                {
                    concept["concept_id"]
                    for item in compact_result["recommendations"]
                    for concept in item["source_task_ksa_concepts"]
                },
            )
            self.assertEqual(
                [
                    item["training_course"]["training_course_id"]
                    for item in source_result["recommendations"]
                ],
                [
                    item["training_course"]["training_course_id"]
                    for item in compact_result["recommendations"]
                ],
            )

            self.assertTrue(source_graph["ok"])
            self.assertTrue(compact_graph["ok"])
            self.assertEqual(source_graph["schema"], compact_graph["schema"])
            self.assertEqual(
                {node["id"] for node in source_graph["nodes"]},
                {node["id"] for node in compact_graph["nodes"]},
            )
            self.assertTrue(
                any(
                    edge["type"] == "task_requires_concept"
                    for edge in compact_graph["edges"]
                )
            )


if __name__ == "__main__":
    unittest.main()
