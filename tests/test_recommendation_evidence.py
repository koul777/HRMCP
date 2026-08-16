from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.recommendation_evidence import (
    apply_recommendation_evidence_hygiene,
    recommendation_evidence_hygiene_report,
    write_recommendation_evidence_hygiene_markdown,
)


def seed_course_and_concept(conn: sqlite3.Connection, concept_name: str = "Concept A") -> tuple[int, int]:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO ncs_training_courses(
            ncs_cl_cd, compe_unit_name, train_goal, train_time,
            fac_name, meth_name, api_fetched_at
        ) VALUES ('U1', 'Course A', 'Goal A', '10', 'Room', 'Practice', ?)
        """,
        (timestamp,),
    )
    course_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type,
            definition_status, relation_status, review_status,
            created_at, updated_at
        ) VALUES (?, ?, 'knowledge', 'candidate', 'linked', 'model_preprocessed', ?, ?)
        """,
        (concept_name, concept_name.lower(), timestamp, timestamp),
    )
    concept_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    conn.commit()
    return course_id, concept_id


def seed_recommendation_evidence(
    conn: sqlite3.Connection,
    *,
    course_id: int,
    concept_name: str,
    source_ids: list[str],
) -> list[int]:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO education_recommendation_runs(
            query, target_source_key, request_payload, target_payload,
            summary_payload, audit_payload, created_at
        ) VALUES ('query', 'unit:U1', '{}', '{}', '{}', '{}', ?)
        """,
        (timestamp,),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    payload = {"training_course": {"training_course_id": course_id, "compe_unit_name": "Course A"}}
    conn.execute(
        """
        INSERT INTO education_recommendation_items(
            run_id, rank, learn_module_name, recommendation_payload,
            confidence_score, confidence_grade, created_at
        ) VALUES (?, 1, 'Course A', ?, 0.9, 'high', ?)
        """,
        (run_id, json.dumps(payload), timestamp),
    )
    item_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    evidence_ids: list[int] = []
    for source_id in source_ids:
        conn.execute(
            """
            INSERT INTO education_recommendation_evidence(
                run_id, item_id, evidence_type, source_table, source_id,
                evidence_text, evidence_summary, confidence_score, created_at
            ) VALUES (?, ?, 'training_goal_ksa_concept',
                      'training_goal_concept_links', ?, 'Goal A', ?, 0.8, ?)
            """,
            (run_id, item_id, source_id, concept_name, timestamp),
        )
        evidence_ids.append(int(conn.execute("SELECT last_insert_rowid()").fetchone()[0]))
    conn.commit()
    return evidence_ids


class RecommendationEvidenceHygieneTests(unittest.TestCase):
    def test_recommendation_evidence_hygiene_remaps_orphan_goal_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = connect(tmp_path / "ncs.db")
            initialize_database(conn)
            course_id, concept_id = seed_course_and_concept(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_goal_concept_links(
                    training_course_id, concept_id, link_method,
                    confidence_score, evidence_text, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, 'training_goal_concept_token', 0.7,
                          'Goal A', 'auto_linked', ?, ?)
                """,
                (course_id, concept_id, timestamp, timestamp),
            )
            link_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            evidence_id = seed_recommendation_evidence(
                conn,
                course_id=course_id,
                concept_name="Concept A",
                source_ids=["999999"],
            )[0]

            report = recommendation_evidence_hygiene_report(conn, limit=5)
            result = apply_recommendation_evidence_hygiene(conn, limit=5)
            row = conn.execute(
                "SELECT source_id FROM education_recommendation_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
            markdown_path = tmp_path / "recommendation_evidence_hygiene.md"
            write_recommendation_evidence_hygiene_markdown(result, markdown_path)
            markdown_text = markdown_path.read_text(encoding="utf-8")
            conn.close()

        self.assertEqual(report["orphan_training_goal_link_evidence_count"], 1)
        self.assertEqual(report["resolvable_count"], 1)
        self.assertEqual(report["remap_updates"][0]["new_source_id"], str(link_id))
        self.assertEqual(result["updated_evidence_count"], 1)
        self.assertEqual(result["after"]["orphan_training_goal_link_evidence_count"], 0)
        self.assertEqual(row["source_id"], str(link_id))
        self.assertIn("mode: applied", markdown_text)

    def test_recommendation_evidence_hygiene_assigns_duplicate_candidates_distinctly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            course_id, concept_id = seed_course_and_concept(conn)
            timestamp = now_utc()
            for method, score in [
                ("training_goal_concept_token", 0.7),
                ("training_goal_element_implied_concept", 0.68),
            ]:
                conn.execute(
                    """
                    INSERT INTO training_goal_concept_links(
                        training_course_id, concept_id, link_method,
                        confidence_score, evidence_text, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'Goal A', 'auto_linked', ?, ?)
                    """,
                    (course_id, concept_id, method, score, timestamp, timestamp),
                )
            seed_recommendation_evidence(
                conn,
                course_id=course_id,
                concept_name="Concept A",
                source_ids=["111", "222"],
            )

            report = recommendation_evidence_hygiene_report(conn, limit=5)
            conn.close()

        new_ids = [item["new_source_id"] for item in report["remap_updates"]]
        self.assertEqual(report["orphan_training_goal_link_evidence_count"], 2)
        self.assertEqual(report["resolvable_count"], 2)
        self.assertEqual(len(set(new_ids)), 2)


if __name__ == "__main__":
    unittest.main()
