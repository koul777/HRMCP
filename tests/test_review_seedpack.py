from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.review_seedpack import (
    ALLOWED_DECISIONS,
    SEEDPACK_FORMAT_VERSION,
    TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION,
    _seedpack_item,
    _seedpack_id_from_timestamp,
    export_review_seedpack,
    export_review_seedpack_from_db,
    export_transition_scenario_seedpack,
    export_transition_scenario_seedpack_from_db,
    write_review_seedpack_csv,
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
        self.assertTrue(lines[0]["human_decision_required"])
        self.assertFalse(lines[0]["status_update_allowed"])
        self.assertFalse(lines[0]["db_writes"])
        self.assertFalse(lines[0]["approval_claim"])
        self.assertFalse(lines[0]["trusted_status_write_allowed"])
        self.assertFalse(lines[0]["raw_source_mutation_allowed"])
        self.assertEqual(lines[1]["record_type"], "review_item")
        self.assertEqual(lines[1]["decision"], "")
        self.assertEqual(lines[1]["reviewer_id"], "")
        self.assertEqual(lines[1]["rationale"], "")
        self.assertEqual(lines[1]["proposed_target_review_status"], "")
        self.assertEqual(lines[1]["proposed_issue_resolution"], "")
        self.assertTrue(lines[1]["target_snapshot_hash"])
        self.assertEqual(lines[1]["current_review_status"], "raw")
        self.assertIn("Malformed criteria text", lines[1]["issue_detail"])

    def test_export_review_seedpack_neutralizes_status_write_suggested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ontology_concept', '42',
                              'hr_core_concept_human_review_required', 'high',
                              'Core concept needs review',
                              'review_ontology_concept로 정의를 확정하고 definition_status=''defined'', review_status=''human_reviewed''로 승인한다.',
                              ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                export_review_seedpack(
                    conn,
                    out_path=out_path,
                    issue_types=["hr_core_concept_human_review_required"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        action = records[1]["suggested_action"]
        nested_action = records[1]["issue"]["suggested_action"]
        self.assertEqual(action, nested_action)
        self.assertIn("Human reviewer should inspect", action)
        self.assertIn("separate explicit human decision", action)
        self.assertNotIn("human_reviewed", action)
        self.assertFalse(records[1]["status_update_allowed"])
        self.assertTrue(records[1]["human_decision_required"])

    def test_export_review_seedpack_neutralizes_accept_suggested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('training_goal_concept_link', '77',
                              'hr_training_goal_link_human_review_required', 'high',
                              'Training goal link needs review',
                              'Accept this mapping if direct evidence is sufficient.',
                              ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                export_review_seedpack(
                    conn,
                    out_path=out_path,
                    issue_types=["hr_training_goal_link_human_review_required"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

            records = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        action = records[1]["suggested_action"]
        self.assertIn("Human reviewer should inspect", action)
        self.assertNotIn("Accept this mapping", action)

    def test_seedpack_item_neutralizes_context_suggested_action(self) -> None:
        record = _seedpack_item(
            "review-seedpack-test",
            1,
            {
                "priority_score": 9,
                "priority_reason": "high impact",
                "issue": {
                    "issue_id": 10,
                    "issue_type": "ontology_task_ksa_relation_human_review_required",
                    "target_type": "task_ksa_concept_relation",
                    "target_id": "55",
                    "severity": "high",
                    "issue_detail": "Task relation needs review",
                    "suggested_action": "Review the relation.",
                },
                "context": {
                    "review_status": "candidate",
                    "criteria_text_raw": "Can perform the task.",
                    "suggested_action": (
                        "Confirm whether this KSA relation is task-essential. "
                        "Mark human_reviewed if valid, or rejected if it is only co-occurrence noise."
                    ),
                },
            },
        )

        context_action = record["context"]["suggested_action"]
        self.assertIn("Human reviewer should inspect", context_action)
        self.assertIn("separate explicit human decision", context_action)
        self.assertNotIn("human_reviewed", context_action)
        self.assertNotIn("Mark", context_action)

    def test_export_review_seedpack_keeps_task_ksa_context_excerpt_when_relation_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "review_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('task_ksa_concept_relation', '987',
                              'ontology_task_ksa_relation_human_review_required', 'high',
                              'Task relation needs a structured context snapshot',
                              'Review the task/KSA relationship', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                summary = export_review_seedpack(
                    conn,
                    out_path=out_path,
                    issue_types=["ontology_task_ksa_relation_human_review_required"],
                    limit=1,
                    per_issue_type_limit=1,
                    source_report_path="reports/review_priority.md",
                    selection_command="test command",
                )
            finally:
                conn.close()

            lines = [
                json.loads(line)
                for line in out_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["item_count"], 1)
        self.assertEqual(lines[1]["issue_type"], "ontology_task_ksa_relation_human_review_required")
        self.assertEqual(lines[1]["context"]["target_type"], "task_ksa_concept_relation")
        self.assertEqual(lines[1]["context"]["target_id"], "987")
        self.assertEqual(
            lines[1]["context"]["issue_detail"],
            "Task relation needs a structured context snapshot",
        )
        self.assertIn(
            "Task relation needs a structured context snapshot",
            lines[1]["source_context_excerpt"],
        )
        self.assertTrue(lines[1]["source_context_excerpt"])

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
        self.assertIn("approve` is reviewer input only", text)
        self.assertIn("does not update DB status", text)
        self.assertIn("later guarded apply workflow", text)
        self.assertNotIn("approved derived fields", text)

    def test_write_review_seedpack_markdown_includes_item_preview_when_jsonl_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seedpack_path = tmp_path / "review_seedpack.jsonl"
            out_path = tmp_path / "review_seedpack.md"
            records = [
                {
                    "record_type": "batch",
                    "format_version": SEEDPACK_FORMAT_VERSION,
                    "seedpack_id": "review-seedpack-test",
                    "item_count": 2,
                    "open_issue_counts": [
                        {"issue_type": "concept_review", "severity": "high", "count": 7}
                    ],
                },
                {
                    "record_type": "review_item",
                    "sequence": 1,
                    "issue_type": "concept_review",
                    "target_type": "ontology_concept",
                    "target_id": "42",
                    "current_review_status": "model_preprocessed",
                    "priority_score": 115,
                    "source_context_excerpt": "workforce\rplanning | evidence",
                    "suggested_action": "Confirm the definition.",
                },
                {
                    "record_type": "review_item",
                    "sequence": 2,
                    "issue_type": "goal_link_review",
                    "target_type": "training_goal_concept_link",
                    "target_id": "99",
                    "current_review_status": "candidate",
                    "priority_score": 125,
                    "source_context_excerpt": "training goal | KSA",
                    "suggested_action": "Approve only direct evidence.",
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            write_review_seedpack_markdown(
                {
                    "seedpack_id": "review-seedpack-test",
                    "format_version": SEEDPACK_FORMAT_VERSION,
                    "item_count": 2,
                    "db_fingerprint": "abc123",
                    "allowed_decisions": ALLOWED_DECISIONS,
                },
                seedpack_path,
                out_path,
            )

            text = out_path.read_text(encoding="utf-8")

        self.assertIn("## Selection Snapshot", text)
        self.assertIn('"concept_review": 1', text)
        self.assertIn('"goal_link_review": 1', text)
        self.assertIn("## Review Item Preview", text)
        self.assertIn("ontology_concept:42", text)
        self.assertIn("workforce planning \\| evidence", text)
        self.assertNotIn("workforce\rplanning", text)
        self.assertIn("Confirm the definition.", text)

    def test_write_review_seedpack_csv_exports_blank_decision_sheet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            seedpack_path = tmp_path / "review_seedpack.jsonl"
            csv_path = tmp_path / "review_seedpack.csv"
            records = [
                {
                    "record_type": "batch",
                    "format_version": SEEDPACK_FORMAT_VERSION,
                    "seedpack_id": "review-seedpack-test",
                    "item_count": 1,
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "review-seedpack-test",
                    "sequence": 1,
                    "issue_type": "concept_review",
                    "target_type": "ontology_concept",
                    "target_id": "42",
                    "current_review_status": "model_preprocessed",
                    "priority_score": 115,
                    "priority_reason": "Core concept",
                    "source_context_excerpt": "=formula-like context",
                    "suggested_action": "Confirm the definition.",
                    "issue_detail": "Needs review.",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "human_decision_required": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "proposed_target_review_status": "",
                    "proposed_issue_resolution": "",
                    "target_snapshot_hash": "hash",
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "review-seedpack-test",
                    "sequence": 2,
                    "issue_type": "concept_review",
                    "target_type": "ontology_concept",
                    "target_id": "43",
                    "current_review_status": "model_preprocessed",
                    "priority_score": 114,
                    "priority_reason": "Core concept",
                    "source_context_excerpt": " =space-prefixed formula",
                    "suggested_action": "\t=tab-prefixed formula",
                    "issue_detail": "Needs review.",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "human_decision_required": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "proposed_target_review_status": "",
                    "proposed_issue_resolution": "",
                    "target_snapshot_hash": "hash2",
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            summary = write_review_seedpack_csv(seedpack_path, csv_path)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(summary["item_count"], 2)
        self.assertTrue(summary["human_decision_required"])
        self.assertFalse(summary["status_update_allowed"])
        self.assertFalse(summary["db_writes"])
        self.assertFalse(summary["approval_claim"])
        self.assertEqual(rows[0]["decision"], "")
        self.assertEqual(rows[0]["reviewer_id"], "")
        self.assertEqual(rows[0]["rationale"], "")
        self.assertEqual(rows[0]["human_decision_required"], "True")
        self.assertEqual(rows[0]["status_update_allowed"], "False")
        self.assertEqual(rows[0]["db_writes"], "False")
        self.assertEqual(rows[0]["approval_claim"], "False")
        self.assertEqual(rows[0]["source_context_excerpt"], "'=formula-like context")
        self.assertEqual(rows[0]["target_snapshot_hash"], "hash")
        self.assertEqual(rows[1]["source_context_excerpt"], "' =space-prefixed formula")
        self.assertEqual(rows[1]["suggested_action"], "'\t=tab-prefixed formula")

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
            ["", "candidate", "candidate_auto", "rejected"],
        )
        self.assertFalse(records[0]["status_update_allowed"])
        self.assertFalse(records[0]["db_writes"])
        self.assertTrue(records[0]["human_decision_required"])
        self.assertFalse(records[0]["approval_claim"])
        self.assertIn(
            "accepted",
            records[0]["trusted_review_statuses_hidden_until_guarded_apply"],
        )
        self.assertEqual(summary["actual_review_status_counts"], {"accepted": 1})
        self.assertEqual(summary["trusted_review_status_count"], 1)
        self.assertEqual(
            summary["allowed_proposed_review_statuses"],
            ["", "candidate", "candidate_auto", "rejected"],
        )
        self.assertFalse(summary["status_update_allowed"])
        self.assertFalse(summary["db_writes"])
        self.assertTrue(summary["human_decision_required"])
        self.assertFalse(summary["approval_claim"])
        self.assertEqual(records[1]["record_type"], "transition_scenario_review_item")
        self.assertEqual(records[1]["seedpack_id"], records[0]["seedpack_id"])
        self.assertEqual(records[1]["decision"], "")
        self.assertFalse(records[1]["status_update_allowed"])
        self.assertFalse(records[1]["db_writes"])
        self.assertTrue(records[1]["human_decision_required"])
        self.assertFalse(records[1]["approval_claim"])
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

    def test_export_transition_scenario_seedpack_evaluates_selected_scenario_ids(self) -> None:
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
                        scenario_id, scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES (1, 'candidate_case', 'current', 'target', '02',
                              'current', 'target', '["Target course"]',
                              'candidate', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO training_transition_gold_scenarios(
                        scenario_id, scenario_name, current_query, target_query, major_code,
                        expected_current_match_text, expected_target_match_text,
                        expected_course_names_json, review_status, created_at, updated_at
                    ) VALUES (32, 'candidate_auto_case', 'auto current', 'auto target', '01',
                              'auto current', 'auto target', '["Auto target course"]',
                              'candidate_auto', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                conn.commit()

                with patch("ncs_mcp.review_seedpack.evaluate_training_transition_scenarios") as evaluate_mock:
                    evaluate_mock.return_value = {
                        "ok": True,
                        "scenario_limit": 2,
                        "scenario_id_filter": [1, 32],
                        "review_status_filter": ["candidate", "candidate_auto"],
                        "scenario_count": 2,
                        "cases": [
                            {
                                "scenario_id": 1,
                                "scenario_name": "candidate_case",
                                "current_match": "current",
                                "target_match": "target",
                                "expected_course_hits": ["Target course"],
                                "recommended_courses": ["Target course"],
                                "recommended_course_evidence": [
                                    {
                                        "rank": 1,
                                        "course_name": "Target course",
                                        "course_scope_fit": {
                                            "relation": "direct_scope_unit",
                                            "alignment": "direct",
                                            "direct_unit_codes": ["0202020101_23v3"],
                                            "requires_scope_review": False,
                                        },
                                        "review_flags": [],
                                    }
                                ],
                                "recommended_course_scope_summary": {
                                    "course_count": 1,
                                    "relation_counts": {"direct_scope_unit": 1},
                                    "alignment_counts": {"direct": 1},
                                    "direct_or_near_count": 1,
                                    "requires_scope_review_count": 0,
                                    "review_flag_counts": {},
                                },
                                "expected_recall_at_k": 1.0,
                                "precision_at_k": 1.0,
                                "top1_expected_hit": True,
                            },
                            {
                                "scenario_id": 32,
                                "scenario_name": "candidate_auto_case",
                                "current_match": "auto current",
                                "target_match": "auto target",
                                "expected_course_hits": ["Auto target course"],
                                "recommended_courses": ["Auto target course"],
                                "expected_recall_at_k": 1.0,
                                "precision_at_k": 1.0,
                                "top1_expected_hit": True,
                            },
                        ],
                    }
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
            items = [
                record
                for record in records
                if record["record_type"] == "transition_scenario_review_item"
            ]

        self.assertEqual(evaluate_mock.call_args.kwargs["scenario_ids"], [1, 32])
        self.assertEqual(summary["item_count"], 2)
        self.assertEqual(items[1]["scenario_id"], 32)
        self.assertEqual(items[1]["recommended_courses"], ["Auto target course"])
        self.assertEqual(items[1]["expected_recall_at_k"], 1.0)
        self.assertFalse(items[1]["evaluation_case_missing"])
        self.assertEqual(summary["missing_evaluation_scenario_ids"], [])
        self.assertEqual(
            items[0]["recommended_course_scope_summary"]["relation_counts"],
            {"direct_scope_unit": 1},
        )
        self.assertEqual(
            items[0]["recommended_course_evidence"][0]["course_scope_fit"]["alignment"],
            "direct",
        )

    def test_export_transition_scenario_seedpack_records_missing_evaluation_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "transition_seedpack.jsonl"
            conn = connect(tmp_path / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute("DELETE FROM training_transition_gold_scenarios")
                for scenario_id, scenario_name, review_status in (
                    (1, "candidate_case", "candidate"),
                    (32, "candidate_auto_case", "candidate_auto"),
                ):
                    conn.execute(
                        """
                        INSERT INTO training_transition_gold_scenarios(
                            scenario_id, scenario_name, current_query, target_query, major_code,
                            expected_current_match_text, expected_target_match_text,
                            expected_course_names_json, review_status, created_at, updated_at
                        ) VALUES (?, ?, 'current', 'target', '02',
                                  'current', 'target', '["Target course"]',
                                  ?, ?, ?)
                        """,
                        (scenario_id, scenario_name, review_status, timestamp, timestamp),
                    )
                conn.commit()

                with patch("ncs_mcp.review_seedpack.evaluate_training_transition_scenarios") as evaluate_mock:
                    evaluate_mock.return_value = {
                        "ok": True,
                        "scenario_limit": 2,
                        "scenario_id_filter": [1, 32],
                        "review_status_filter": ["candidate", "candidate_auto"],
                        "scenario_count": 2,
                        "cases": [
                            {
                                "scenario_id": 1,
                                "scenario_name": "candidate_case",
                                "current_match": "current",
                                "target_match": "target",
                                "expected_course_hits": ["Target course"],
                                "recommended_courses": ["Target course"],
                                "expected_recall_at_k": 1.0,
                                "precision_at_k": 1.0,
                                "top1_expected_hit": True,
                            }
                        ],
                    }
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
            batch = records[0]
            items = [
                record
                for record in records
                if record["record_type"] == "transition_scenario_review_item"
            ]

        self.assertEqual(summary["missing_evaluation_scenario_ids"], [32])
        self.assertEqual(batch["missing_evaluation_scenario_ids"], [32])
        self.assertFalse(items[0]["evaluation_case_missing"])
        self.assertTrue(items[1]["evaluation_case_missing"])
        self.assertEqual(items[1]["recommended_courses"], [])

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
                    "allowed_proposed_review_statuses": ["", "candidate", "candidate_auto", "rejected"],
                    "trusted_review_statuses_hidden_until_guarded_apply": [
                        "accepted",
                        "human_reviewed",
                        "reviewed",
                    ],
                    "status_update_allowed": False,
                    "db_writes": False,
                    "human_decision_required": True,
                    "approval_claim": False,
                    "source_report_path": "reports/training_transition_evaluation.md",
                    "selection_command": "export-transition-scenario-seedpack",
                    "review_statuses": ["candidate", "candidate_auto"],
                    "actual_review_status_counts": {"candidate": 1},
                    "missing_requested_review_statuses": ["candidate_auto"],
                    "trusted_review_status_count": 0,
                    "missing_evaluation_scenario_ids": [32],
                    "evaluation_summary": {
                        "scenario_count": 1,
                        "expected_course_recall_at_k": 1.0,
                        "precision_at_k": 0.5,
                        "top1_expected_hit_rate": 1.0,
                        "course_scope_relation_counts": {"direct_scope_unit": 1},
                        "course_scope_alignment_counts": {"direct": 1},
                        "course_scope_review_required_count": 0,
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
        self.assertIn("status_update_allowed: False", text)
        self.assertIn("db_writes: False", text)
        self.assertIn("human_decision_required: True", text)
        self.assertIn("approval_claim: False", text)
        self.assertIn("trusted_review_statuses_hidden_until_guarded_apply", text)
        self.assertIn("not an approval claim", text)
        self.assertIn("reviewer_id", text)
        self.assertIn("reviewed_at", text)
        self.assertIn("rationale", text)
        self.assertIn("leave them blank when no human decision is supplied", text)
        self.assertIn("candidate_auto", text)
        self.assertIn("missing_evaluation_scenario_ids: 32", text)
        self.assertIn("expected_course_recall_at_k", text)
        self.assertIn("course_scope_relation_counts", text)
        self.assertIn("direct_scope_unit", text)

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
