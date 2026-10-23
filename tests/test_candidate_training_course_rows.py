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
from ncs_mcp.training_recommendation import TRAINING_LINK_TABLES, _candidate_training_course_rows


def seed(conn) -> None:
    conn.execute("PRAGMA foreign_keys = OFF")  # course and link rows only; no unit or concept lineage needed
    timestamp = now_utc()
    courses = [
        (1, "U1_23v1", "Course in scope"),
        (2, "U9_23v1", "Linked by unit link"),
        (3, "U9_23v1", "Linked by element link"),
        (4, "U9_23v1", "Linked by concept link"),
        (5, "U9_23v1", "Linked by goal concept"),
        (6, "U9_23v1", "Support name"),
        (7, "U9_23v1", "Only rejected links"),
        (8, "U9_23v1", "Unrelated"),
    ]
    for course_id, unit_code, name in courses:
        conn.execute(
            "INSERT INTO ncs_training_courses(training_course_id, ncs_cl_cd, compe_unit_name, train_goal, api_fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (course_id, unit_code, name, f"goal {course_id}", timestamp),
        )
    conn.execute("INSERT INTO ncs_training_course_unit_links(training_course_id, unit_code, link_method, confidence_score, review_status, created_at, updated_at) "
                 "VALUES (2, 'U1_23v1', 'm', 1, 'auto_linked', ?, ?), (7, 'U1_23v1', 'm', 1, 'rejected', ?, ?)",
                 (timestamp,) * 4)
    conn.execute("INSERT INTO ncs_training_course_element_links(training_course_id, unit_code, element_id, link_method, confidence_score, review_status, created_at, updated_at) "
                 "VALUES (3, 'U1_23v1', 1, 'm', 1, 'auto_linked', ?, ?)", (timestamp,) * 2)
    conn.execute("INSERT INTO ncs_training_course_concept_links(training_course_id, unit_code, concept_id, link_method, confidence_score, review_status, created_at, updated_at) "
                 "VALUES (4, 'U1_23v1', 10, 'm', 1, 'auto_linked', ?, ?), (7, 'U1_23v1', 11, 'm', 1, 'rejected', ?, ?)",
                 (timestamp,) * 4)
    conn.execute("INSERT INTO training_goal_concept_links(training_course_id, unit_code, concept_id, link_method, confidence_score, review_status, created_at, updated_at) "
                 "VALUES (5, 'U9_23v1', 20, 'training_goal_concept_text', 1, 'auto_linked', ?, ?), "
                 "(7, 'U9_23v1', 20, 'training_goal_concept_text', 1, 'rejected', ?, ?)",
                 (timestamp,) * 4)
    conn.commit()


def as_views(conn) -> None:
    """Re-expose the link tables as views, the shape of compact serving snapshots."""
    for table in TRAINING_LINK_TABLES:
        conn.execute(f"ALTER TABLE {table} RENAME TO {table}_physical")
        conn.execute(f"CREATE VIEW {table} AS SELECT * FROM {table}_physical")
    conn.commit()


class CandidateTrainingCourseRowsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "ncs.db")
        initialize_database(self.conn)
        seed(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def ids(self, **overrides) -> list[int]:
        params = {"scope_unit_codes": {"U1_23v1"}, "target_concept_ids": {20}, "support_course_names": {"Support name"}}
        params.update(overrides)
        return [row["training_course_id"] for row in _candidate_training_course_rows(self.conn, **params)]

    def test_union_matches_every_branch_and_skips_rejected_links(self) -> None:
        self.assertEqual(self.ids(), [1, 2, 3, 4, 5, 6])

    def test_empty_inputs_return_no_rows(self) -> None:
        self.assertEqual(self.ids(scope_unit_codes=set(), target_concept_ids=set(), support_course_names=set()), [])
        self.assertEqual(self.ids(scope_unit_codes={""}, target_concept_ids={0}, support_course_names={""}), [])

    def test_view_backed_links_return_the_same_rows(self) -> None:
        cases = [{}, {"target_concept_ids": set()}, {"scope_unit_codes": set()}, {"support_course_names": set()}]
        physical = [[dict(row) for row in _candidate_training_course_rows(self.conn, **{
            "scope_unit_codes": {"U1_23v1"}, "target_concept_ids": {20}, "support_course_names": {"Support name"}, **case,
        })] for case in cases]
        as_views(self.conn)
        viewed = [[dict(row) for row in _candidate_training_course_rows(self.conn, **{
            "scope_unit_codes": {"U1_23v1"}, "target_concept_ids": {20}, "support_course_names": {"Support name"}, **case,
        })] for case in cases]
        self.assertEqual(physical, viewed)


if __name__ == "__main__":
    unittest.main()
