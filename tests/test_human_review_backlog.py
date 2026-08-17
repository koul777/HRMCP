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

from ncs_mcp.blocker_report import build_human_review_backlog_report_from_files, write_human_review_backlog_markdown


class HumanReviewBacklogTests(unittest.TestCase):
    def test_backlog_report_focuses_human_review_blockers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            ontology_seedpack_path = tmp_path / "ontology_seedpack.jsonl"
            review_seedpack_path = tmp_path / "review_seedpack.jsonl"
            transition_seedpack_path = tmp_path / "transition_seedpack.jsonl"
            operator_audit_path = tmp_path / "operator_review_packet_integrity_audit.json"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {
                        "blockers": [
                            {"name": "review_debt:human_reviewed_concepts", "value": 0, "threshold": "> 0"},
                            {"name": "review_debt:human_reviewed_goal_links", "value": 0, "threshold": "> 0"},
                            {"name": "review_debt:human_reviewed_task_relations", "value": 0, "threshold": "> 0"},
                            {"name": "human_review:provenance_reconfirmation_required"},
                            {"name": "qualification:collection_coverage", "value": 0.2205, "threshold": ">= 0.9"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {"quality_warning_count": 6},
                        "review_priority_items": [
                            {
                                "priority_score": 125,
                                "issue_type": "ontology_training_goal_link_human_review_required",
                                "target_type": "training_goal_concept_link",
                                "target_id": "1",
                                "priority_reason": "training goal review",
                                "context_excerpt": "PR 전략 수립 | PR 목표",
                                "suggested_action": "Mark the link human_reviewed if valid.",
                            }
                        ],
                        "focus_review_priority_overlays": [
                            {
                                "code": "aihr_demo_major_02",
                                "label": "AI-HR demo focus",
                                "major_code": "02",
                                "item_count": 1,
                                "reason": "demo",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            seedpack_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 1,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "seedpack-test",
                    "sequence": 1,
                    "issue_type": "ontology_core_concept_human_review_required",
                    "target_type": "ontology_concept",
                    "target_id": "42",
                    "target_snapshot_hash": "hash-42",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "proposed_target_review_status": "",
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
            ]
            for path in [ontology_seedpack_path, review_seedpack_path, transition_seedpack_path]:
                path.write_text(
                    "\n".join(json.dumps(record, ensure_ascii=False) for record in seedpack_records) + "\n",
                    encoding="utf-8",
                )
            operator_audit_path.write_text(
                json.dumps({"schema": "operator_review_packet_integrity_audit_v1", "ok": True}),
                encoding="utf-8",
            )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
            )
            explicit_operator_report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
                operator_packet_integrity_audit_path=operator_audit_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(len(report["blockers"]), 4)
        self.assertEqual(report["blockers"][0]["name"], "review_debt:human_reviewed_concepts")
        self.assertEqual(
            report["blockers"][0]["review_artifacts"],
            ["ontology_definition_seedpack", "ksa_definition_review_operator_packet"],
        )
        provenance_blocker = report["blockers"][3]
        self.assertEqual(
            provenance_blocker["safe_next_action"],
            "export-human-review-provenance-reconfirmation-proofset",
        )
        self.assertIn(
            "provenance_reconfirmation_decision_sheet_markdown",
            provenance_blocker["review_artifacts"],
        )
        self.assertEqual(
            provenance_blocker["open_first"],
            "transition_provenance_operator_crosswalk_csv",
        )
        self.assertIn(
            "transition_provenance_operator_crosswalk_csv",
            provenance_blocker["review_artifacts"],
        )
        self.assertIn(
            "operator_packet_integrity_audit",
            provenance_blocker["review_artifacts"],
        )
        self.assertIn(
            "provenance_reconfirmation_decision_sheet_csv",
            report["source_paths"],
        )
        self.assertIn(
            "transition_provenance_operator_crosswalk_csv",
            report["source_paths"],
        )
        self.assertNotIn("operator_packet_integrity_audit", report["source_paths"])
        self.assertIn(
            "operator_packet_integrity_audit",
            explicit_operator_report["source_paths"],
        )
        self.assertTrue(report["seedpack_safety"]["all_seedpacks_safe"])
        self.assertEqual(report["seedpack_safety"]["total_review_items"], 3)
        self.assertEqual(report["seedpack_safety"]["total_nonblank_decision_items"], 0)
        self.assertEqual(report["seedpack_safety"]["total_trusted_status_proposals"], 0)
        self.assertEqual(report["seedpack_safety"]["total_status_update_allowed_violations"], 0)
        self.assertEqual(report["seedpack_safety"]["total_missing_status_update_allowed"], 0)
        self.assertEqual(report["seedpack_safety"]["total_forbidden_true_field_violations"], 0)
        self.assertEqual(report["top_items"][0]["issue_type"], "ontology_training_goal_link_human_review_required")
        self.assertIn(
            "Human reviewer should inspect whether the training goal directly supports the KSA link",
            report["top_items"][0]["suggested_action"],
        )
        self.assertNotIn("human_reviewed if valid", report["top_items"][0]["suggested_action"])
        self.assertIn("Human Review Backlog", markdown)
        self.assertIn("Seedpack Safety Audit", markdown)
        self.assertIn("transition_provenance_operator_crosswalk_csv", markdown)
        self.assertIn("provenance_reconfirmation_decision_sheet_markdown", markdown)
        self.assertIn("human_review_provenance_reconfirmation_decision_sheet", markdown)
        self.assertIn("all_seedpacks_safe: `True`", markdown)
        self.assertIn("AI-HR demo focus", markdown)
        self.assertNotIn("Mark the link human_reviewed", markdown)

    def test_missing_status_update_allowed_is_not_seedpack_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            ontology_seedpack_path = tmp_path / "ontology_seedpack.jsonl"
            review_seedpack_path = tmp_path / "review_seedpack.jsonl"
            transition_seedpack_path = tmp_path / "transition_seedpack.jsonl"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {"blockers": [{"name": "review_debt:human_reviewed_concepts", "value": 0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            seedpack_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 1,
                    "db_writes": False,
                    "approval_claim": False,
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "seedpack-test",
                    "sequence": 1,
                    "issue_type": "ontology_core_concept_human_review_required",
                    "target_type": "ontology_concept",
                    "target_id": "42",
                    "target_snapshot_hash": "hash-42",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "proposed_target_review_status": "",
                    "db_writes": False,
                    "approval_claim": False,
                },
            ]
            for path in [ontology_seedpack_path, review_seedpack_path, transition_seedpack_path]:
                path.write_text(
                    "\n".join(json.dumps(record, ensure_ascii=False) for record in seedpack_records) + "\n",
                    encoding="utf-8",
                )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertFalse(report["seedpack_safety"]["all_seedpacks_safe"])
        self.assertEqual(report["seedpack_safety"]["total_missing_status_update_allowed"], 6)
        self.assertFalse(
            report["seedpack_safety"]["audits"]["ontology_definition_seedpack"]["safety_ok"]
        )
        self.assertIn("total_missing_status_update_allowed: `6`", markdown)

    def test_forbidden_seedpack_safety_fields_are_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            ontology_seedpack_path = tmp_path / "ontology_seedpack.jsonl"
            review_seedpack_path = tmp_path / "review_seedpack.jsonl"
            transition_seedpack_path = tmp_path / "transition_seedpack.jsonl"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {"blockers": [{"name": "review_debt:human_reviewed_concepts", "value": 0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            safe_review_item = {
                "record_type": "review_item",
                "seedpack_id": "seedpack-test",
                "sequence": 1,
                "issue_type": "ontology_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "42",
                "target_snapshot_hash": "hash-42",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "proposed_target_review_status": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            safe_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 1,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
                safe_review_item,
            ]
            unsafe_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 1,
                    "status_update_allowed": False,
                    "db_writes": True,
                    "approval_claim": False,
                },
                {**safe_review_item, "source_payload_exposed": True},
            ]
            ontology_seedpack_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in unsafe_records) + "\n",
                encoding="utf-8",
            )
            for path in [review_seedpack_path, transition_seedpack_path]:
                path.write_text(
                    "\n".join(json.dumps(record, ensure_ascii=False) for record in safe_records) + "\n",
                    encoding="utf-8",
                )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        audit = report["seedpack_safety"]["audits"]["ontology_definition_seedpack"]
        self.assertFalse(report["seedpack_safety"]["all_seedpacks_safe"])
        self.assertFalse(audit["safety_ok"])
        self.assertEqual(audit["forbidden_true_field_counts"]["db_writes"], 1)
        self.assertEqual(audit["forbidden_true_field_counts"]["source_payload_exposed"], 1)
        self.assertNotIn('"source_payload":', json.dumps(report, ensure_ascii=False))
        self.assertEqual(audit["forbidden_true_field_violation_count"], 2)
        self.assertEqual(report["seedpack_safety"]["total_forbidden_true_field_violations"], 2)
        self.assertIn("total_forbidden_true_field_violations: `2`", markdown)

    def test_seedpack_structure_issues_are_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            ontology_seedpack_path = tmp_path / "ontology_seedpack.jsonl"
            review_seedpack_path = tmp_path / "review_seedpack.jsonl"
            transition_seedpack_path = tmp_path / "transition_seedpack.jsonl"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {"blockers": [{"name": "review_debt:human_reviewed_concepts", "value": 0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            safe_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 1,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "seedpack-test",
                    "sequence": 1,
                    "issue_type": "ontology_core_concept_human_review_required",
                    "target_type": "ontology_concept",
                    "target_id": "42",
                    "target_snapshot_hash": "hash-42",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "proposed_target_review_status": "",
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
            ]
            unsafe_records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "seedpack-test",
                    "item_count": 2,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
                {
                    **safe_records[1],
                    "seedpack_id": "other-seedpack",
                    "sequence": "",
                    "target_snapshot_hash": "",
                },
            ]
            ontology_seedpack_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in unsafe_records) + "\n",
                encoding="utf-8",
            )
            for path in [review_seedpack_path, transition_seedpack_path]:
                path.write_text(
                    "\n".join(json.dumps(record, ensure_ascii=False) for record in safe_records) + "\n",
                    encoding="utf-8",
                )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        audit = report["seedpack_safety"]["audits"]["ontology_definition_seedpack"]
        self.assertFalse(report["seedpack_safety"]["all_seedpacks_safe"])
        self.assertFalse(audit["safety_ok"])
        self.assertFalse(audit["batch_item_count_matches"])
        self.assertEqual(audit["item_seedpack_id_mismatch_count"], 1)
        self.assertEqual(audit["missing_sequence_count"], 1)
        self.assertEqual(audit["missing_target_snapshot_hash_count"], 1)
        self.assertEqual(audit["structure_issue_count"], 4)
        self.assertEqual(report["seedpack_safety"]["total_seedpack_structure_issues"], 4)
        self.assertIn("total_seedpack_structure_issues: `4`", markdown)


if __name__ == "__main__":
    unittest.main()
