from __future__ import annotations

import csv
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
SRC = ROOT / "src"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_harness import (
    _qualification_checkpoint_values_match_plan,
    qualification_collection_coverage_plan,
    write_qualification_collection_coverage_plan_csv,
    write_qualification_collection_coverage_plan_markdown,
)


def _create_sample_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE classifications (
            classification_id INTEGER PRIMARY KEY,
            major_code TEXT NOT NULL,
            major_name TEXT NOT NULL
        );
        CREATE TABLE competency_units (
            unit_code TEXT PRIMARY KEY,
            classification_id INTEGER NOT NULL
        );
        CREATE TABLE ncs_qualification_collection_status (
            unit_code TEXT PRIMARY KEY,
            collection_status TEXT NOT NULL
        );
        INSERT INTO classifications VALUES
          (1, '01', '사업관리'),
          (2, '02', '경영·회계·사무');
        INSERT INTO competency_units VALUES
          ('0101', 1),
          ('0102', 1),
          ('0201', 2),
          ('0202', 2),
          ('0203', 2);
        INSERT INTO ncs_qualification_collection_status VALUES
          ('0101', 'collected'),
          ('0201', 'empty'),
          ('0202', 'error');
        """
    )


def _sample_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    _create_sample_schema(conn)
    return conn


class QualificationCoveragePlanTests(unittest.TestCase):
    def test_checkpoint_value_match_accepts_equivalent_absolute_and_relative_paths(self) -> None:
        relative_checkpoint = "reports\\checkpoint_ncs006_element_api_status_20260630_current.json"
        absolute_checkpoint = ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260630_current.json"

        self.assertTrue(
            _qualification_checkpoint_values_match_plan(
                [relative_checkpoint, str(absolute_checkpoint)],
                relative_checkpoint,
            )
        )
        self.assertFalse(
            _qualification_checkpoint_values_match_plan(
                [relative_checkpoint, "reports\\stale_checkpoint.json"],
                str(absolute_checkpoint),
            )
        )

    def test_qualification_collection_coverage_plan_is_report_only(self) -> None:
        conn = _sample_conn()
        try:
            report = qualification_collection_coverage_plan(
                conn,
                target_ratio=0.8,
                batch_size=2,
                checkpoint_path=Path("reports/checkpoint_ncs006.json"),
            )
        finally:
            conn.close()

        self.assertEqual(report["schema"], "ncs_qualification_collection_coverage_plan_v1")
        self.assertTrue(report["ok"])
        self.assertTrue(report["report_only"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["human_review_status_updates"])
        self.assertEqual(report["current_state"]["total_unit_count"], 5)
        self.assertEqual(report["current_state"]["attempted_unit_count"], 3)
        self.assertEqual(report["current_state"]["unattempted_unit_count"], 2)
        self.assertEqual(report["target_state"]["target_attempted_unit_count"], 4)
        self.assertEqual(report["target_state"]["additional_attempted_units_needed"], 1)
        self.assertEqual(report["target_state"]["estimated_batch_count"], 1)
        self.assertEqual(report["batches"][0]["limit_units"], 1)
        self.assertIn("--ncs006-checkpoint-path reports\\checkpoint_ncs006.json", report["batches"][0]["command"])
        self.assertFalse(report["batches"][0]["execution_authorized"])
        self.assertTrue(report["batches"][0]["do_not_execute_from_report"])
        self.assertTrue(report["batches"][0]["not_queue_item"])
        self.assertTrue(report["batches"][0]["requires_operator_ticket"])
        self.assertTrue(report["guard_policy"]["must_not_write_human_review_statuses"])

    def test_qualification_collection_coverage_plan_outputs_markdown_and_csv(self) -> None:
        conn = _sample_conn()
        try:
            report = qualification_collection_coverage_plan(conn, target_ratio=0.8, batch_size=2)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            markdown_path = temp / "plan.md"
            csv_path = temp / "plan.csv"
            report["major_gaps"][0]["major_name"] = '=HYPERLINK("http://example.com")'
            write_qualification_collection_coverage_plan_markdown(report, markdown_path)
            write_qualification_collection_coverage_plan_csv(report, csv_path)

            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("Qualification Collection Coverage Plan", markdown)
            self.assertIn("Do not set `human_reviewed`, `accepted`, or `reviewed`", markdown)

            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            by_major = {row["major_code"]: row for row in rows}
            self.assertEqual(
                rows[0]["major_name"],
                '\'=HYPERLINK("http://example.com")',
            )
            self.assertEqual(by_major["02"]["unattempted_unit_count"], "1")
            self.assertEqual(by_major["02"]["attempted_unit_count"], "2")

    def test_qualification_coverage_plan_cli_uses_configured_db_and_writes_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            db_path = temp / "sample.db"
            conn = sqlite3.connect(db_path)
            try:
                _create_sample_schema(conn)
            finally:
                conn.close()
            out_path = temp / "plan.json"
            markdown_path = temp / "plan.md"
            csv_path = temp / "plan.csv"
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SRC)
            env["NCS_DB_PATH"] = str(db_path)

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "ncs_harness.py"),
                    "qualification-coverage-plan",
                    "--target-ratio",
                    "0.8",
                    "--batch-size",
                    "2",
                    "--out",
                    str(out_path),
                    "--markdown-out",
                    str(markdown_path),
                    "--csv-out",
                    str(csv_path),
                ],
                cwd=ROOT,
                env=env,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=30,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "ncs_qualification_collection_coverage_plan_v1")
            self.assertFalse(payload["db_writes"])
            self.assertFalse(payload["api_calls"])
            self.assertEqual(payload["current_state"]["total_unit_count"], 5)
            self.assertTrue(markdown_path.exists())
            self.assertTrue(csv_path.exists())
            self.assertFalse((temp / "sample.db-journal").exists())
            self.assertFalse((temp / "sample.db-wal").exists())


if __name__ == "__main__":
    unittest.main()
