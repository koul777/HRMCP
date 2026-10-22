from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.training_recommendation import _task_similarity_transition_profile


def seed_units(conn, unit_codes: list[str]) -> dict[str, tuple[int, int]]:
    """Create one element and one criterion per unit; return code -> (element_id, criteria_id)."""
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
        """
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[0]
    lineage = {}
    for index, code in enumerate(unit_codes, start=1):
        conn.execute(
            """
            INSERT INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, api_definition,
                api_match_status, created_at, updated_at
            ) VALUES (?, ?, '23v1', ?, '3', ?, '', 'matched', ?, ?)
            """,
            (code, code.split("_")[0], f"unit {index}", classification_id, timestamp, timestamp),
        )
        element_id = conn.execute(
            """
            INSERT INTO competency_elements(
                unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
            ) VALUES (?, '1', ?, ?, '3')
            """,
            (code, f"{code} 1", f"element {index}"),
        ).lastrowid
        criteria_id = conn.execute(
            "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, '1', ?)",
            (element_id, f"criterion {index}"),
        ).lastrowid
        lineage[code] = (element_id, criteria_id)
    return lineage


def add_link(conn, lineage, source: str, target: str, score: float, shared: int, status: str = "candidate") -> None:
    source_element, source_criteria = lineage[source]
    target_element, target_criteria = lineage[target]
    conn.execute(
        """
        INSERT INTO task_similarity_links(
            source_criteria_id, target_criteria_id, source_element_id, target_element_id,
            source_unit_code, target_unit_code, relation_type, similarity_score,
            shared_concept_count, source_concept_count, target_concept_count,
            source_only_count, target_only_count, review_status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 4, 4, 1, 1, ?, ?)
        """,
        (
            source_criteria, target_criteria, source_element, target_element,
            source, target, f"rel_{source}_{target}_{status}", score, shared, status, now_utc(),
        ),
    )


class TransitionTaskSimilarityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "ncs.db")
        initialize_database(self.conn)
        self.lineage = seed_units(self.conn, ["A_23v1", "B_23v1", "C_23v1"])
        add_link(self.conn, self.lineage, "A_23v1", "B_23v1", 0.5, 2)
        add_link(self.conn, self.lineage, "B_23v1", "A_23v1", 0.7, 3)
        add_link(self.conn, self.lineage, "A_23v1", "C_23v1", 0.9, 4)
        add_link(self.conn, self.lineage, "C_23v1", "B_23v1", 0.95, 4)
        add_link(self.conn, self.lineage, "A_23v1", "B_23v1", 0.99, 5, status="rejected")
        add_link(self.conn, self.lineage, "A_23v1", "A_23v1", 0.8, 3)
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_returns_both_directions_between_scopes_only(self) -> None:
        profile = _task_similarity_transition_profile(
            self.conn, current_unit_codes={"A_23v1"}, target_unit_codes={"B_23v1"}
        )
        pairs = [(row["source_unit_code"], row["target_unit_code"], row["similarity_score"]) for row in profile["evidence"]]
        self.assertEqual(pairs, [("B_23v1", "A_23v1", 0.7), ("A_23v1", "B_23v1", 0.5)])
        self.assertEqual(profile["max_score"], 0.7)
        self.assertEqual(profile["link_count"], 2)

    def test_overlapping_scopes_do_not_duplicate_links(self) -> None:
        profile = _task_similarity_transition_profile(
            self.conn, current_unit_codes={"A_23v1"}, target_unit_codes={"A_23v1", "B_23v1"}
        )
        pairs = [(row["source_unit_code"], row["target_unit_code"]) for row in profile["evidence"]]
        self.assertEqual(pairs, [("A_23v1", "A_23v1"), ("B_23v1", "A_23v1"), ("A_23v1", "B_23v1")])

    def test_ties_keep_newest_link_first(self) -> None:
        # Same score and shared count as the B->A link; inserted later, so it ranks first.
        add_link(self.conn, self.lineage, "A_23v1", "B_23v1", 0.7, 3, status="llm_reviewed")
        profile = _task_similarity_transition_profile(
            self.conn, current_unit_codes={"A_23v1"}, target_unit_codes={"B_23v1"}
        )
        pairs = [(row["source_unit_code"], row["target_unit_code"], row["similarity_score"]) for row in profile["evidence"]]
        self.assertEqual(pairs, [("A_23v1", "B_23v1", 0.7), ("B_23v1", "A_23v1", 0.7), ("A_23v1", "B_23v1", 0.5)])

    def test_query_uses_source_criteria_index_instead_of_score_scan(self) -> None:
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        _task_similarity_transition_profile(self.conn, current_unit_codes={"A_23v1"}, target_unit_codes={"B_23v1"})
        self.conn.set_trace_callback(None)
        query = next(sql for sql in statements if "FROM task_similarity_links" in sql)
        plan = " ".join(str(row[-1]) for row in self.conn.execute("EXPLAIN QUERY PLAN " + query))
        self.assertIn("idx_task_similarity_source", plan)
        self.assertNotIn("idx_task_similarity_score", plan)


if __name__ == "__main__":
    unittest.main()
