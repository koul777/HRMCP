from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.api_linkage_summary import (
    build_api_linkage_summary,
    write_api_linkage_summary_json,
    write_api_linkage_summary_markdown,
)


def _seed_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE classifications (
            classification_id INTEGER PRIMARY KEY,
            major_code TEXT NOT NULL,
            major_name TEXT NOT NULL
        );
        CREATE TABLE competency_units (
            unit_code TEXT PRIMARY KEY,
            classification_id INTEGER NOT NULL,
            api_match_status TEXT NOT NULL
        );
        CREATE TABLE competency_elements (
            element_id INTEGER PRIMARY KEY,
            unit_code TEXT NOT NULL,
            api_match_status TEXT NOT NULL
        );
        CREATE TABLE ncs_training_course_unit_links (
            link_id INTEGER PRIMARY KEY,
            training_course_id INTEGER NOT NULL,
            unit_code TEXT NOT NULL
        );
        CREATE TABLE ncs_unit_job_base_links (
            link_id INTEGER PRIMARY KEY,
            unit_code TEXT NOT NULL
        );
        CREATE TABLE ncs_unit_qualification_links (
            link_id INTEGER PRIMARY KEY,
            unit_code TEXT NOT NULL
        );
        CREATE TABLE ncs_qualification_collection_status (
            unit_code TEXT PRIMARY KEY,
            collection_status TEXT NOT NULL
        );
        """
    )
    conn.executemany(
        "INSERT INTO classifications VALUES (?, ?, ?)",
        [(1, "01", "Business"), (2, "02", "HR")],
    )
    conn.executemany(
        "INSERT INTO competency_units VALUES (?, ?, ?)",
        [
            ("0101", 1, "matched"),
            ("0102", 1, "matched"),
            ("0201", 2, "matched"),
            ("0202", 2, "not_collected"),
        ],
    )
    conn.executemany(
        "INSERT INTO competency_elements VALUES (?, ?, ?)",
        [
            (1, "0101", "matched"),
            (2, "0102", "matched"),
            (3, "0201", "matched"),
            (4, "0202", "api_failed"),
            (5, "0202", "not_collected"),
        ],
    )
    conn.executemany(
        "INSERT INTO ncs_training_course_unit_links VALUES (?, ?, ?)",
        [(1, 10, "0101"), (2, 11, "0201")],
    )
    conn.executemany(
        "INSERT INTO ncs_unit_job_base_links VALUES (?, ?)",
        [(1, "0101"), (2, "0201"), (3, "0202")],
    )
    conn.executemany(
        "INSERT INTO ncs_unit_qualification_links VALUES (?, ?)",
        [(1, "0201")],
    )
    conn.executemany(
        "INSERT INTO ncs_qualification_collection_status VALUES (?, ?)",
        [("0101", "empty"), ("0201", "collected"), ("0202", "error")],
    )
    conn.commit()


def _seed_core_only_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE classifications (
            classification_id INTEGER PRIMARY KEY,
            major_code TEXT NOT NULL,
            major_name TEXT NOT NULL
        );
        CREATE TABLE competency_units (
            unit_code TEXT PRIMARY KEY,
            classification_id INTEGER NOT NULL,
            api_match_status TEXT NOT NULL
        );
        CREATE TABLE competency_elements (
            element_id INTEGER PRIMARY KEY,
            unit_code TEXT NOT NULL,
            api_match_status TEXT NOT NULL
        );
        """
    )
    conn.execute("INSERT INTO classifications VALUES (?, ?, ?)", (1, "02", "HR"))
    conn.executemany(
        "INSERT INTO competency_units VALUES (?, ?, ?)",
        [("0201", 1, "matched"), ("0202", 1, "not_collected")],
    )
    conn.executemany(
        "INSERT INTO competency_elements VALUES (?, ?, ?)",
        [(1, "0201", "matched"), (2, "0202", "not_collected")],
    )
    conn.commit()


class ApiLinkageSummaryTests(unittest.TestCase):
    def test_build_api_linkage_summary_groups_by_major(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_db(conn)
            report = build_api_linkage_summary(conn)
        finally:
            conn.close()

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], "ncs_api_linkage_summary_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["human_review_status_updates"])
        self.assertFalse(report["sqf_active_scoring_source"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["human_decision_required"])
        self.assertEqual(report["safe_next_action_count"], 1)
        self.assertEqual(report["guarded_collection_candidate_count"], len(report["guarded_collection_candidates"]))
        self.assertEqual(report["unguarded_collection_candidate_count"], 0)
        self.assertEqual(report["unsafe_safe_next_action_count"], 0)
        self.assertEqual(report["summary"]["major_count"], 2)
        self.assertEqual(report["summary"]["element_api_remaining_targets"], 2)
        self.assertEqual(report["summary"]["training_unit_coverage"], 0.5)
        self.assertEqual(report["summary"]["job_base_unit_coverage"], 0.75)
        coverage_hint = report["qualification_coverage_plan_hint"]
        self.assertEqual(coverage_hint["scope"], "all_majors")
        self.assertEqual(coverage_hint["target_ratio"], 0.9)
        self.assertEqual(coverage_hint["batch_size"], 100)
        self.assertEqual(coverage_hint["total_unit_count"], 4)
        self.assertEqual(coverage_hint["attempted_unit_count"], 3)
        self.assertEqual(coverage_hint["collection_coverage"], 0.75)
        self.assertEqual(coverage_hint["target_attempted_unit_count"], 4)
        self.assertEqual(coverage_hint["additional_attempted_units_needed"], 1)
        self.assertEqual(coverage_hint["estimated_batch_count"], 1)
        self.assertEqual(coverage_hint["coverage_plan_command_scope"], "all_units")
        self.assertTrue(coverage_hint["coverage_plan_matches_summary_scope"])
        self.assertTrue(coverage_hint["must_run_qualification_retry_hygiene_first"])
        self.assertFalse(coverage_hint["api_calls"])
        self.assertFalse(coverage_hint["db_writes"])
        self.assertIn("qualification-coverage-plan", coverage_hint["coverage_plan_command"])
        self.assertIn("qualification-coverage-plan", coverage_hint["global_coverage_plan_command"])
        self.assertIn("qualification-retry-hygiene", coverage_hint["qualification_retry_hygiene_command"])
        self.assertEqual(report["diagnostic_targets"]["training_courses"]["major_codes"], ["01", "02"])
        self.assertEqual(report["diagnostic_targets"]["job_base"]["major_codes"], ["01"])
        self.assertEqual(report["diagnostic_targets"]["qualification_collection"]["major_codes"], ["01"])
        self.assertEqual([item["area"] for item in report["safe_next_actions"]], ["report_only_recheck"])
        guarded_candidates = report["guarded_collection_candidates"]
        training_actions = [item for item in guarded_candidates if item["area"] == "training_course_links"]
        job_base_actions = [item for item in guarded_candidates if item["area"] == "job_base_links"]
        qualification_actions = [
            item for item in guarded_candidates if item["area"] == "qualification_collection_major"
        ]
        self.assertEqual({item["major_code"] for item in training_actions}, {"01", "02"})
        self.assertEqual({item["major_code"] for item in job_base_actions}, {"01"})
        self.assertEqual({item["major_code"] for item in qualification_actions}, {"01"})
        self.assertIn(
            "python scripts\\ncs_harness.py collect-training-courses --major-code 01 --num-of-rows 500",
            {item["command"] for item in training_actions},
        )
        self.assertIn(
            "python scripts\\ncs_harness.py collect-job-base --major-code 01 --num-of-rows 500",
            {item["command"] for item in job_base_actions},
        )
        self.assertIn(
            (
                "python scripts\\ncs_harness.py collect-qualification-items --major-code 01 "
                "--num-of-rows 50 --max-pages 1 --request-delay 2 --max-retries 1 "
                "--retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 "
                "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_<DATE>_current.json"
            ),
            {item["command"] for item in qualification_actions},
        )
        self.assertFalse(report["policy"]["api_calls"])
        self.assertEqual(report["safe_next_actions"][0]["area"], "report_only_recheck")
        self.assertFalse(report["safe_next_actions"][0]["guard_required"])
        self.assertIn(
            "qualification_collection",
            {item["area"] for item in guarded_candidates},
        )
        major_02 = {item["major_code"]: item for item in report["by_major"]}["02"]
        self.assertEqual(major_02["major_name_label"], "HR")
        self.assertEqual(major_02["element_api"]["remaining_targets"], 2)
        self.assertEqual(major_02["qualifications"]["collection_status_counts"]["error"], 1)
        self.assertEqual(major_02["qualifications"]["collection_coverage"], 1.0)

    def test_build_api_linkage_summary_degrades_when_optional_tables_are_missing(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_core_only_db(conn)
            report = build_api_linkage_summary(conn)
        finally:
            conn.close()

        self.assertTrue(report["ok"])
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertEqual(report["summary"]["major_count"], 1)
        self.assertEqual(report["summary"]["unit_count"], 2)
        self.assertEqual(report["summary"]["training_linked_unit_count"], 0)
        self.assertEqual(report["summary"]["job_base_linked_unit_count"], 0)
        self.assertEqual(report["summary"]["qualification_attempted_unit_count"], 0)
        self.assertEqual(
            set(report["missing_optional_tables"]),
            {
                "ncs_training_course_unit_links",
                "ncs_unit_job_base_links",
                "ncs_unit_qualification_links",
                "ncs_qualification_collection_status",
            },
        )
        self.assertEqual(len(report["source_issues"]), 4)
        self.assertTrue(
            all(issue["code"] == "missing_optional_table" for issue in report["source_issues"])
        )

    def test_build_api_linkage_summary_filters_major_codes(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_db(conn)
            report = build_api_linkage_summary(conn, major_codes=["2", "14"])
        finally:
            conn.close()

        self.assertEqual(report["filter"]["major_codes"], ["02", "14"])
        self.assertEqual(report["filter"]["missing_major_codes"], ["14"])
        self.assertEqual([item["major_code"] for item in report["by_major"]], ["02"])
        self.assertEqual(report["summary"]["major_count"], 1)
        self.assertEqual(report["summary"]["unit_count"], 2)
        self.assertEqual(report["summary"]["element_api_remaining_targets"], 2)
        self.assertEqual(report["summary"]["training_unit_coverage"], 0.5)
        self.assertEqual(report["summary"]["job_base_unit_coverage"], 1.0)
        self.assertEqual(
            report["qualification_coverage_plan_hint"]["scope"],
            "selected_majors_report_only",
        )
        self.assertEqual(report["qualification_coverage_plan_hint"]["scope_major_codes"], ["02", "14"])
        self.assertEqual(report["qualification_coverage_plan_hint"]["additional_attempted_units_needed"], 0)
        self.assertFalse(report["qualification_coverage_plan_hint"]["coverage_plan_matches_summary_scope"])
        self.assertIsNone(report["qualification_coverage_plan_hint"]["coverage_plan_command"])
        self.assertIn(
            "qualification-coverage-plan",
            report["qualification_coverage_plan_hint"]["global_coverage_plan_command"],
        )
        self.assertEqual(report["diagnostic_targets"]["training_courses"]["major_codes"], ["02"])
        self.assertEqual(report["diagnostic_targets"]["job_base"]["major_codes"], [])
        self.assertEqual(report["diagnostic_targets"]["qualification_collection"]["major_codes"], [])
        training_actions = [
            item for item in report["guarded_collection_candidates"] if item["area"] == "training_course_links"
        ]
        self.assertEqual([item["major_code"] for item in training_actions], ["02"])
        self.assertEqual(
            [item["command"] for item in training_actions],
            ["python scripts\\ncs_harness.py collect-training-courses --major-code 02 --num-of-rows 500"],
        )

    def test_write_api_linkage_summary_markdown(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_db(conn)
            report = build_api_linkage_summary(conn, major_codes=["02"])
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "api_linkage.md"
            write_api_linkage_summary_markdown(report, out)
            markdown = out.read_text(encoding="utf-8")

        self.assertIn("# NCS API Linkage Summary", markdown)
        self.assertIn("major_code_filter: 02", markdown)
        self.assertIn("## Diagnostic Targets", markdown)
        self.assertIn("## Qualification Coverage Plan Hint", markdown)
        self.assertIn("coverage_plan_command: `not_available_for_filtered_scope`", markdown)
        self.assertIn("global_coverage_plan_command: `python scripts\\ncs_harness.py qualification-coverage-plan --target-ratio 0.9 --batch-size 100", markdown)
        self.assertIn("coverage_plan_matches_summary_scope: false", markdown)
        self.assertIn("qualification-retry-hygiene", markdown)
        self.assertIn("must_run_qualification_retry_hygiene_first: true", markdown)
        self.assertIn("### Training Courses", markdown)
        self.assertIn("### Qualification Collection", markdown)
        self.assertIn("Guarded recovery commands are listed under Guarded Collection Candidates.", markdown)
        self.assertIn("collect-training-courses --major-code 02 --num-of-rows 500", markdown)
        self.assertIn("| 02 | HR |", markdown)
        self.assertIn("No API calls", markdown)
        self.assertIn("## Safe Next Actions", markdown)
        self.assertIn("## Guarded Collection Candidates", markdown)
        self.assertIn("report_only_recheck", markdown)

    def test_write_api_linkage_summary_json_is_ascii_safe(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_db(conn)
            conn.execute(
                "UPDATE classifications SET major_name = ? WHERE major_code = ?",
                ("\uacbd\uc601\u00b7\ud68c\uacc4\u00b7\uc0ac\ubb34", "02"),
            )
            conn.commit()
            report = build_api_linkage_summary(conn, major_codes=["02"])
        finally:
            conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "api_linkage.json"
            write_api_linkage_summary_json(report, out)
            self.assertTrue(out.read_bytes().isascii())

    def test_major_display_name_uses_canonical_label_for_mojibake_source_name(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            _seed_db(conn)
            conn.execute(
                "INSERT INTO classifications VALUES (?, ?, ?)",
                (14, "14", "\u6e6f\u6c72"),
            )
            conn.execute(
                "INSERT INTO competency_units VALUES (?, ?, ?)",
                ("1401", 14, "matched"),
            )
            conn.execute(
                "INSERT INTO competency_elements VALUES (?, ?, ?)",
                (14, "1401", "matched"),
            )
            conn.commit()
            report = build_api_linkage_summary(conn, major_codes=["14"])
        finally:
            conn.close()

        major_14 = report["by_major"][0]
        self.assertEqual(major_14["major_name"], "\u6e6f\u6c72")
        self.assertEqual(major_14["major_name_label"], "\uac74\uc124")
        training_target_14 = report["diagnostic_targets"]["training_courses"]["majors"][0]
        self.assertEqual(training_target_14["major_name"], "\u6e6f\u6c72")
        self.assertEqual(training_target_14["major_name_label"], "\uac74\uc124")
        qualification_target_14 = report["diagnostic_targets"]["qualification_collection"]["majors"][0]
        self.assertEqual(qualification_target_14["major_name"], "\u6e6f\u6c72")
        self.assertEqual(qualification_target_14["major_name_label"], "\uac74\uc124")

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "api_linkage.md"
            write_api_linkage_summary_markdown(report, out)
            markdown = out.read_text(encoding="utf-8")

        self.assertIn("| 14 | \uac74\uc124 |", markdown)
        self.assertNotIn("\u6e6f\u6c72", markdown)


if __name__ == "__main__":
    unittest.main()
