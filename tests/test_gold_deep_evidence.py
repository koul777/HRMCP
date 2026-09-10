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

from ncs_mcp.gold_deep_evidence import (
    MAX_DEEP_EVIDENCE_CONCEPT_IDS,
    MAX_DEEP_EVIDENCE_CRITERIA_IDS,
    MAX_DEEP_EVIDENCE_RELATIONS,
    GoldDeepEvidenceValidationError,
    SQLiteGoldDeepEvidenceReader,
)


class GoldDeepEvidenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "deep-evidence.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE ontology_concepts (
                    concept_id INTEGER PRIMARY KEY,
                    concept_name TEXT NOT NULL,
                    concept_type TEXT NOT NULL
                );
                CREATE TABLE ontology_concept_relations (
                    relation_id INTEGER PRIMARY KEY,
                    source_concept_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_concept_id INTEGER NOT NULL,
                    relation_label TEXT,
                    review_status TEXT NOT NULL
                );
                CREATE TABLE task_ksa_concept_relations (
                    relation_id INTEGER PRIMARY KEY,
                    criteria_id INTEGER NOT NULL,
                    element_id INTEGER NOT NULL,
                    source_concept_id INTEGER NOT NULL,
                    relation_type TEXT NOT NULL,
                    target_concept_id INTEGER NOT NULL,
                    source_atomic_id INTEGER NOT NULL,
                    target_atomic_id INTEGER NOT NULL,
                    evidence_text TEXT,
                    confidence_score REAL NOT NULL,
                    review_status TEXT NOT NULL
                );
                CREATE INDEX idx_task_ksa_rel_criteria
                    ON task_ksa_concept_relations(criteria_id);
                INSERT INTO ontology_concepts VALUES
                    (1, 'knowledge one', 'knowledge'),
                    (2, 'skill two', 'skill'),
                    (3, 'attitude three', 'attitude'),
                    (4, 'knowledge four', 'knowledge'),
                    (5, 'skill five', 'skill');
                INSERT INTO ontology_concept_relations VALUES
                    (30, 3, 'attitude_supports_skill', 2, 'candidate relation c', 'candidate'),
                    (10, 1, 'knowledge_enables_skill', 2, 'candidate relation a', 'candidate'),
                    (50, 4, 'knowledge_enables_skill', 5, 'approved-looking row', 'human_reviewed'),
                    (20, 1, 'co_required_in_element', 3, 'candidate relation b', 'candidate'),
                    (40, 2, 'same_as', 5, 'not allowlisted', 'candidate');
                INSERT INTO task_ksa_concept_relations VALUES
                    (100, 10, 1000, 1, 'knowledge_enables_skill', 2,
                     10001, 10002, 'task evidence a', 0.62, 'candidate'),
                    (101, 10, 1000, 3, 'attitude_supports_skill', 2,
                     10003, 10002, 'task evidence b', 0.58, 'review_required'),
                    (102, 10, 1000, 1, 'same_as', 5,
                     10001, 10005, 'not allowlisted', 0.9, 'candidate'),
                    (103, 10, 1000, 4, 'knowledge_enables_skill', 5,
                     10004, 10005, 'rejected evidence', 0.62, 'rejected'),
                    (104, 11, 1100, 1, 'knowledge_informs_attitude', 3,
                     11001, 11003, 'task evidence c', 0.54, 'candidate');
                """
            )
        self.reader = SQLiteGoldDeepEvidenceReader(self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_candidate_state_and_non_approval_are_explicit(self) -> None:
        result = self.reader.read([1, 2, 4], direction="both")

        self.assertTrue(result["read_only"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])
        self.assertEqual(result["audit"]["review_status_filter"], "candidate")
        self.assertTrue(result["relations"])
        self.assertTrue(
            all(row["review_status"] == "candidate" for row in result["relations"])
        )
        self.assertTrue(
            all(row["approval_claim"] is False for row in result["relations"])
        )
        self.assertNotIn("approved-looking row", str(result))
        self.assertNotIn("same_as", str(result))

    def test_incoming_outgoing_and_both_are_bounded_and_directional(self) -> None:
        outgoing = self.reader.read([1], direction="outgoing")
        incoming = self.reader.read([2], direction="incoming")
        both = self.reader.read([2], direction="both")

        self.assertEqual(
            [row["relation_id"] for row in outgoing["relations"]], [20, 10]
        )
        self.assertEqual(
            [row["relation_id"] for row in incoming["relations"]], [30, 10]
        )
        self.assertEqual(
            [row["relation_id"] for row in both["relations"]], [30, 10]
        )
        self.assertTrue(
            all(row["matched_direction"] == "incoming" for row in incoming["relations"])
        )

    def test_output_is_deterministic_and_reports_truncation(self) -> None:
        first = self.reader.read([3, 2, 1], limit=2)
        second = self.reader.read([1, 2, 3], limit=2)

        self.assertEqual(first, second)
        self.assertEqual([row["relation_id"] for row in first["relations"]], [30, 20])
        self.assertTrue(first["audit"]["truncated"])

    def test_input_and_output_bounds_are_rejected_before_query(self) -> None:
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read(
                list(range(1, MAX_DEEP_EVIDENCE_CONCEPT_IDS + 2))
            )
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read([1], limit=MAX_DEEP_EVIDENCE_RELATIONS + 1)
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read([])
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read([True])

    def test_relation_type_and_identifier_inputs_cannot_inject_sql(self) -> None:
        malicious = "knowledge_enables_skill') OR 1=1 --"
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read([1], relation_types=[malicious])
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read(["1) OR 1=1 --"])  # type: ignore[list-item]

        with closing(sqlite3.connect(self.db_path)) as conn:
            relation_count = conn.execute(
                "SELECT COUNT(*) FROM ontology_concept_relations"
            ).fetchone()[0]
        self.assertEqual(relation_count, 5)

    def test_read_leaves_database_bytes_unchanged(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).digest()

        result = self.reader.read([1, 2, 3], direction="both")

        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertEqual(result["audit"]["db_writes"], False)

    def test_task_hydration_requires_criteria_and_preserves_status(self) -> None:
        result = self.reader.read_task_relations([10])

        self.assertEqual(
            [row["relation_id"] for row in result["relations"]], [101, 100]
        )
        self.assertEqual(
            [row["review_status"] for row in result["relations"]],
            ["review_required", "candidate"],
        )
        self.assertTrue(all(not row["approval_claim"] for row in result["relations"]))
        self.assertNotIn("same_as", str(result))
        self.assertNotIn("rejected evidence", str(result))
        self.assertEqual(
            result["audit"]["mandatory_index"], "idx_task_ksa_rel_criteria"
        )
        self.assertFalse(result["audit"]["concept_only_scan_allowed"])

    def test_task_hydration_optional_concepts_narrow_indexed_rows(self) -> None:
        result = self.reader.read_task_relations([11, 10], concept_ids=[3])

        self.assertEqual(
            [row["relation_id"] for row in result["relations"]], [101, 104]
        )
        self.assertEqual(result["criteria_ids"], [10, 11])
        self.assertEqual(result["concept_ids"], [3])

    def test_task_hydration_bounds_and_truncation(self) -> None:
        limited = self.reader.read_task_relations([10], limit=1)
        self.assertEqual(len(limited["relations"]), 1)
        self.assertTrue(limited["audit"]["truncated"])

        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations([])
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations(
                list(range(1, MAX_DEEP_EVIDENCE_CRITERIA_IDS + 2))
            )
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations([10], concept_ids=[])
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations(
                [10],
                concept_ids=list(range(1, MAX_DEEP_EVIDENCE_CONCEPT_IDS + 2)),
            )

    def test_task_hydration_rejects_injection_inputs(self) -> None:
        malicious = "knowledge_enables_skill') OR 1=1 --"
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations(
                [10],
                relation_types=[malicious],
            )
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations(
                ["10) OR 1=1 --"]  # type: ignore[list-item]
            )
        with self.assertRaises(GoldDeepEvidenceValidationError):
            self.reader.read_task_relations(
                [10],
                concept_ids=["1) OR 1=1 --"],  # type: ignore[list-item]
            )

    def test_task_hydration_leaves_database_bytes_unchanged(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).digest()

        result = self.reader.read_task_relations([10, 11], concept_ids=[1, 3])

        after = hashlib.sha256(self.db_path.read_bytes()).digest()
        self.assertEqual(before, after)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["approval_claim"])


if __name__ == "__main__":
    unittest.main()
