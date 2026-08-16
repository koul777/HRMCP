from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.review_seedpack import (
    ALLOWED_DECISIONS,
    ALLOWED_PROPOSED_TRANSITION_SCENARIO_REVIEW_STATUSES,
    SEEDPACK_FORMAT_VERSION,
    TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
    _seedpack_id_from_timestamp,
    export_review_seedpack,
    export_review_seedpack_from_db,
    export_transition_scenario_seedpack,
    export_transition_scenario_seedpack_from_db,
    write_review_seedpack_markdown,
    write_transition_scenario_seedpack_markdown,
)


class ReviewSeedpackTests(unittest.TestCase):
    def test_seedpack_id_uses_selection_discriminator(self) -> None:
        exported_at = "2026-06-16T16:14:37+00:00"

        first = _seedpack_id_from_timestamp(exported_at, {"out_path": "a.jsonl"})
        second = _seedpack_id_from_timestamp(exported_at, {"out_path": "b.jsonl"})

        self.assertTrue(first.startswith("review-seedpack-2026-06-16T161437Z-"))
        self.assertNotEqual(first, second)

    def test_export_review_seedpack_writes_jsonl_without_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', '.')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                    "criteria_id"
                ]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('criteria', ?, 'criteria_format_issue', 'warning',
                              'Malformed criteria text', 'Review refined criteria', ?)
                    """,
                    (str(criteria_id), timestamp),
                )
                conn.commit()

                summary = export_review_seedpack(
                    conn,
                    out_path=out_path,
                    issue_types=["criteria_format_issue"],
                    limit=1,
                    per_issue_type_limit=1,
                    source_report_path="reports/review_priority.md",
                    selection_command="test command",
                )
                unresolved_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM quality_issues WHERE resolved_at IS NULL"
                ).fetchone()["count"]
            finally:
                conn.close()

            lines = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["source_report_path"], "reports/review_priority.md")
        self.assertEqual(summary["selection_command"], "test command")
        self.assertEqual(unresolved_count, 1)
        self.assertEqual(lines[0]["record_type"], "batch")
        self.assertEqual(lines[0]["format_version"], SEEDPACK_FORMAT_VERSION)
        self.assertEqual(lines[0]["allowed_decisions"], ALLOWED_DECISIONS)
        self.assertEqual(lines[1]["record_type"], "review_item")
        self.assertEqual(lines[1]["decision"], "")
        self.assertEqual(lines[1]["reviewer_id"], "")
        self.assertEqual(lines[1]["rationale"], "")
        self.assertEqual(lines[1]["proposed_target_review_status"], "")
        self.assertEqual(lines[1]["proposed_issue_resolution"], "")
        self.assertTrue(lines[1]["target_snapshot_hash"])
        self.assertEqual(lines[1]["current_review_status"], "raw")
        self.assertIn("Malformed criteria text", lines[1]["issue_detail"])

    def test_export_review_seedpack_metadata_uses_effective_priority_caps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for index in range(80):
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('unit', ?, 'criteria_format_issue', 'warning',
                                  'Needs review', 'Review', ?)
                        """,
                        (f"U-{index}", timestamp),
                    )
                conn.commit()

                summary = export_review_seedpack(
                    conn,
                    out_path=out_path,
                    issue_types=["criteria_format_issue"],
                    limit=500,
                    per_issue_type_limit=80,
                )
            finally:
                conn.close()

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        batch = records[0]
        item_records = records[1:]
        self.assertEqual(batch["limit"], 200)
        self.assertEqual(batch["per_issue_type_limit"], 50)
        self.assertEqual(batch["item_count"], len(item_records))
        self.assertEqual(summary["item_count"], 50)
        self.assertEqual(len(item_records), 50)

    def test_export_review_seedpack_from_db_uses_read_only_database_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            out_path = tmp_path / "review_seedpack_from_db.jsonl"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('unit', 'U-1', 'criteria_format_issue', 'warning',
                              'Needs review', 'Review', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()
            finally:
                conn.close()

            summary = export_review_seedpack_from_db(
                db_path,
                out_path=out_path,
                issue_types=["criteria_format_issue"],
                limit=1,
                per_issue_type_limit=1,
            )
            records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(records[0]["record_type"], "batch")
        self.assertEqual(records[1]["record_type"], "review_item")

    def test_export_review_seedpack_from_db_rejects_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(FileNotFoundError, "SQLite DB does not exist"):
                export_review_seedpack_from_db(
                    Path(tmp) / "missing.db",
                    out_path=Path(tmp) / "seedpack.jsonl",
                    limit=1,
                )

    def test_write_review_seedpack_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.md"
            write_review_seedpack_markdown(
                {
                    "seedpack_id": "review-seedpack-test",
                    "format_version": SEEDPACK_FORMAT_VERSION,
                    "item_count": 1,
                    "db_fingerprint": "abc123",
                    "allowed_decisions": ALLOWED_DECISIONS,
                    "source_report_path": "reports/review_priority.md",
                    "selection_command": "export-review-seedpack --limit 1",
                },
                tmp_path / "review_seedpack.jsonl",
                out_path,
            )

            text = out_path.read_text(encoding="utf-8")

        self.assertIn("# NCS Review Seedpack", text)
        self.assertIn("review-seedpack-test", text)
        self.assertIn("approve, reject, defer", text)
        self.assertIn("reports/review_priority.md", text)
        self.assertIn("export-review-seedpack --limit 1", text)

    def test_export_transition_scenario_seedpack_writes_review_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('accepted_case', 'current', 'target', '02',
                              'current', 'target', '["Target course"]',
                              'accepted', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()

                summary = export_transition_scenario_seedpack(
                    conn,
                    out_path=out_path,
                    review_statuses=["accepted"],
                    scenario_limit=5,
                    recommendation_limit=3,
                    source_report_path="reports/training_transition_evaluation.md",
                    selection_command="transition command",
                )
                status = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_name = 'accepted_case'"
                ).fetchone()["review_status"]
            finally:
                conn.close()

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary["ok"])
        self.assertTrue(summary["seedpack_id"].startswith("transition-scenario-seedpack-"))
        self.assertEqual(status, "accepted")
        self.assertEqual(records[0]["record_type"], "batch")
        self.assertEqual(records[0]["format_version"], TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION)
        self.assertTrue(records[0]["seedpack_id"].startswith("transition-scenario-seedpack-"))
        self.assertEqual(records[0]["item_count"], 1)
        self.assertEqual(records[0]["actual_review_status_counts"], {"accepted": 1})
        self.assertEqual(records[0]["missing_requested_review_statuses"], [])
        self.assertEqual(records[0]["trusted_review_status_count"], 1)
        self.assertEqual(
            records[0]["allowed_proposed_review_statuses"],
            ALLOWED_PROPOSED_TRANSITION_SCENARIO_REVIEW_STATUSES,
        )
        self.assertEqual(summary["actual_review_status_counts"], {"accepted": 1})
        self.assertEqual(summary["trusted_review_status_count"], 1)
        self.assertEqual(
            summary["allowed_proposed_review_statuses"],
            ALLOWED_PROPOSED_TRANSITION_SCENARIO_REVIEW_STATUSES,
        )
        self.assertEqual(records[1]["record_type"], "transition_scenario_review_item")
        self.assertEqual(records[1]["seedpack_id"], records[0]["seedpack_id"])
        self.assertEqual(records[1]["decision"], "")
        self.assertEqual(records[1]["scenario_name"], "accepted_case")
        self.assertEqual(records[1]["expected_courses"], ["Target course"])
        self.assertTrue(records[1]["target_snapshot_hash"])

    def test_export_transition_scenario_seedpack_reports_missing_requested_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('candidate_case', 'current', 'target', '02',
                              'current', 'target', '["Target course"]',
                              'candidate', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()

                summary = export_transition_scenario_seedpack(
                    conn,
                    out_path=out_path,
                    review_statuses=["candidate", "candidate_auto"],
                    scenario_limit=5,
                    recommendation_limit=1,
                )
            finally:
                conn.close()

            records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(summary["actual_review_status_counts"], {"candidate": 1})
        self.assertEqual(summary["missing_requested_review_statuses"], ["candidate_auto"])
        self.assertEqual(records[0]["missing_requested_review_statuses"], ["candidate_auto"])

    def test_export_transition_scenario_seedpack_samples_each_requested_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                for index in range(3):
                    conn.execute(
                        """
                        INSERT INTO training_transition_gold_scenarios(
                            scenario_name, current_query, target_query, major_code,
                            expected_current_match_text, expected_target_match_text,
                            expected_course_names_json, review_status, created_at, updated_at
                        ) VALUES (?, 'current', 'target', '02',
                                  'current', 'target', '["Target course"]',
                                  'candidate', ?, ?)
                        """,
                        (f"candidate_case_{index}", timestamp, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('candidate_auto_case', 'current', 'target', '02',
                              'current', 'target', '["Target course"]',
                              'candidate_auto', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()

                summary = export_transition_scenario_seedpack(
                    conn,
                    out_path=out_path,
                    review_statuses=["candidate", "candidate_auto"],
                    scenario_limit=2,
                    recommendation_limit=1,
                )
            finally:
                conn.close()

            records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]
            item_statuses = [
                record["current_review_status"]
                for record in records
                if record["record_type"] == "transition_scenario_review_item"
            ]

        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(item_statuses, ["candidate", "candidate_auto"])
        self.assertEqual(summary["missing_requested_review_statuses"], [])

    def test_export_transition_scenario_seedpack_from_db_uses_read_only_database_uri(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            out_path = tmp_path / "transition_seedpack_from_db.jsonl"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('candidate_case', 'current', 'target', '02',
                              'current', 'target', '["Target course"]',
                              'candidate', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()
            finally:
                conn.close()

            summary = export_transition_scenario_seedpack_from_db(
                db_path,
                out_path=out_path,
                review_statuses=["candidate"],
                scenario_limit=1,
                recommendation_limit=1,
            )
            records = [json.loads(line) for line in out_path.read_text(encoding="utf-8").splitlines()]

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(records[0]["record_type"], "batch")
        self.assertEqual(records[1]["record_type"], "transition_scenario_review_item")

    def test_write_transition_scenario_seedpack_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.md"
            write_transition_scenario_seedpack_markdown(
                {
                    "seedpack_id": "transition-seedpack-test",
                    "format_version": TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
                    "item_count": 1,
                    "db_fingerprint": "abc123",
                    "allowed_decisions": ALLOWED_DECISIONS,
                    "allowed_proposed_review_statuses": ALLOWED_PROPOSED_TRANSITION_SCENARIO_REVIEW_STATUSES,
                    "source_report_path": "reports/training_transition_evaluation.md",
                    "selection_command": "export-transition-scenario-seedpack",
                    "review_statuses": ["candidate", "candidate_auto"],
                    "actual_review_status_counts": {"candidate": 1},
                    "missing_requested_review_statuses": ["candidate_auto"],
                    "trusted_review_status_count": 0,
                    "evaluation_summary": {
                        "scenario_count": 1,
                        "expected_course_recall_at_k": 1.0,
                        "precision_at_k": 0.5,
                        "top1_expected_hit_rate": 1.0,
                    },
                },
                tmp_path / "transition_seedpack.jsonl",
                out_path,
            )

            text = out_path.read_text(encoding="utf-8")

        self.assertIn("# NCS Transition Scenario Review Seedpack", text)
        self.assertIn("transition-seedpack-test", text)
        self.assertIn("actual_review_status_counts", text)
        self.assertIn("allowed_proposed_review_statuses", text)
        self.assertIn("candidate_auto", text)
        self.assertIn("expected_course_recall_at_k", text)

    def test_export_transition_scenario_seedpack_rejects_malformed_expected_course_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES ('bad_case', 'current', 'target', '02',
                              'current', 'target', '[not-json',
                              'candidate', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()

                with self.assertRaisesRegex(ValueError, "expected_course_names_json.*bad_case"):
                    export_transition_scenario_seedpack(
                        conn,
                        out_path=out_path,
                        review_statuses=["candidate"],
                        scenario_limit=1,
                        recommendation_limit=1,
                    )
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
