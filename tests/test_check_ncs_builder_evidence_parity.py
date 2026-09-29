"""The source reconciliation gate must catch evidence loss in a candidate."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.check_ncs_builder_evidence_parity import (
    TRAINING_PROJECTIONS,
    check,
)


class BuilderEvidenceParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.current = root / "current.db"
        self.older = root / "older.db"
        candidate_dir = root / "candidate"
        candidate_dir.mkdir()
        self.candidate = candidate_dir / "ncs.db"
        for path in (self.current, self.older, self.candidate):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("CREATE TABLE ncs_qualification_items (jm_cd TEXT PRIMARY KEY)")
                conn.execute(
                    "CREATE TABLE ncs_qualification_collection_status "
                    "(unit_code TEXT PRIMARY KEY, status TEXT)"
                )
                conn.execute(
                    "CREATE TABLE ncs_unit_qualification_links "
                    "(link_id INTEGER PRIMARY KEY, unit_code TEXT, jm_cd TEXT)"
                )
                for table, projection in TRAINING_PROJECTIONS.items():
                    columns = [name.strip() for name in projection.split(",")]
                    conn.execute(
                        f"CREATE TABLE {table} ("
                        + ", ".join(f"{name} TEXT" for name in columns)
                        + ")"
                    )
        (candidate_dir / "build.json").write_text(
            json.dumps({"status": "ready", "version": "fixture", "human_approval_claim": False}),
            encoding="utf-8",
        )

    def test_rejects_missing_qualification_and_training_rows(self) -> None:
        with closing(sqlite3.connect(self.current)) as conn, conn:
            conn.execute("INSERT INTO ncs_qualification_items VALUES ('Q1')")
            conn.execute("INSERT INTO ncs_qualification_collection_status VALUES ('U1', 'collected')")
            conn.execute("INSERT INTO ncs_unit_qualification_links VALUES (1, 'U1', 'Q1')")
        with closing(sqlite3.connect(self.older)) as conn, conn:
            conn.execute(
                "INSERT INTO ncs_training_course_unit_links "
                "VALUES ('C1', 'U1', 'exact', '1', 'auto_linked')"
            )
        report = check(self.current, self.older, self.candidate)
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["qualification"]["ncs_unit_qualification_links"]["current_rows_missing_or_changed"],
            1,
        )
        self.assertEqual(
            report["training"]["ncs_training_course_unit_links"]["older_builder_rows_missing_or_changed"],
            1,
        )

    def test_accepts_candidate_with_both_sources_rows(self) -> None:
        for path in (self.current, self.candidate):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute("INSERT INTO ncs_qualification_items VALUES ('Q1')")
                conn.execute("INSERT INTO ncs_qualification_collection_status VALUES ('U1', 'collected')")
                conn.execute("INSERT INTO ncs_unit_qualification_links VALUES (1, 'U1', 'Q1')")
        for path in (self.older, self.candidate):
            with closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    "INSERT INTO ncs_training_course_unit_links "
                    "VALUES ('C1', 'U1', 'exact', '1', 'auto_linked')"
                )
        report = check(self.current, self.older, self.candidate)
        self.assertTrue(report["ok"])
        self.assertTrue(report["read_only"])
        self.assertFalse(report["human_approval_claim"])

    def test_rejects_loss_of_duplicate_training_projection(self) -> None:
        table = "training_goal_concept_links"
        columns = [name.strip() for name in TRAINING_PROJECTIONS[table].split(",")]
        placeholders = ", ".join("?" for _ in columns)
        old_row = ["same"] * len(columns)
        changed_row = ["different", *old_row[1:]]
        with closing(sqlite3.connect(self.older)) as conn, conn:
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                (old_row, old_row),
            )
        with closing(sqlite3.connect(self.candidate)) as conn, conn:
            conn.executemany(
                f"INSERT INTO {table} VALUES ({placeholders})",
                (old_row, changed_row),
            )
        report = check(self.current, self.older, self.candidate)
        relation = report["training"][table]
        self.assertEqual(relation["older_builder_rows_missing_or_changed"], 0)
        self.assertEqual(relation["older_builder_rows"], relation["candidate_rows"])
        self.assertEqual(
            relation["older_builder_projection_groups_with_changed_multiplicity"],
            1,
        )
        self.assertFalse(report["training_ok"])
        self.assertFalse(report["ok"])


if __name__ == "__main__":
    unittest.main()
