from __future__ import annotations

import contextlib
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.audit_aihr_review_seedpack_csv_decisions import (  # noqa: E402
    build_audit,
    main as audit_main,
)


class AihrReviewSeedpackCsvDecisionAuditTests(unittest.TestCase):
    fields = [
        "sequence",
        "issue_type",
        "target_type",
        "target_id",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
        "human_decision_required",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
        "acceptance_claim",
        "proposed_target_review_status",
        "target_snapshot_hash",
    ]

    def _write_csv(self, path: Path, rows: list[dict[str, str]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=self.fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _row(self, **extra: str) -> dict[str, str]:
        row = {
            "sequence": "1",
            "issue_type": "hr_training_goal_link_human_review_required",
            "target_type": "training_goal_concept_link",
            "target_id": "g1",
            "decision": "",
            "reviewer_id": "",
            "reviewed_at": "",
            "rationale": "",
            "human_decision_required": "true",
            "status_update_allowed": "false",
            "db_writes": "false",
            "approval_claim": "false",
            "acceptance_claim": "false",
            "proposed_target_review_status": "",
            "target_snapshot_hash": "sha256:abc",
        }
        row.update(extra)
        return row

    def test_pending_blank_decisions_pass_without_status_write_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(root / "review.csv", [self._row()])
            report = build_audit(csv_path, root=root)

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(1, report["pending_decision_count"])
        self.assertEqual(0, report["completed_decision_count"])
        self.assertEqual(0, report["invalid_decision_count"])

    def test_completed_decision_requires_reviewer_timestamp_and_rationale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(
                        decision="accept_link",
                        reviewer_id="hr_lead",
                        reviewed_at="2026-07-12T15:30:00+09:00",
                        rationale="Evidence matches the goal link.",
                    )
                ],
            )
            report = build_audit(csv_path, root=root)

        self.assertTrue(report["ok"])
        self.assertEqual(1, report["completed_decision_count"])
        self.assertEqual(0, report["invalid_decision_count"])

    def test_missing_reviewer_fields_fail_for_nonblank_decision(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(root / "review.csv", [self._row(decision="accept_link")])
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["invalid_decision_count"])
        self.assertEqual(
            ["rationale", "reviewed_at", "reviewer_id"],
            report["invalid_rows"][0]["missing_required_fields"],
        )

    def test_guard_flags_and_trusted_status_proposals_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(
                        decision="accept_link",
                        reviewer_id="hr_lead",
                        reviewed_at="2026-07-12",
                        rationale="ok",
                        status_update_allowed="true",
                        acceptance_claim="true",
                        proposed_target_review_status="human_reviewed",
                    )
                ],
            )
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["guard_issue_row_count"])
        self.assertEqual(1, report["trusted_status_proposal_row_count"])
        self.assertIn("status_update_allowed_not_false", report["invalid_rows"][0]["issues"])
        self.assertIn("acceptance_claim_not_false", report["invalid_rows"][0]["issues"])
        self.assertIn(
            "trusted_status_proposal_requires_separate_guarded_apply",
            report["invalid_rows"][0]["issues"],
        )

    def test_issue_type_specific_decision_vocabulary_is_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(
                        decision="accept_relation",
                        reviewer_id="hr_lead",
                        reviewed_at="2026-07-12T15:30:00+09:00",
                        rationale="Wrong vocabulary for goal link.",
                    )
                ],
            )
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertIn("decision_not_allowed_for_issue_type", report["invalid_rows"][0]["issues"])

    def test_concept_issue_type_uses_concept_decision_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(
                        issue_type="hr_core_concept_human_review_required",
                        decision="accept_link",
                        reviewer_id="hr_lead",
                        reviewed_at="2026-07-12T15:30:00+09:00",
                        rationale="Wrong vocabulary for concept review.",
                    )
                ],
            )
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertIn("decision_not_allowed_for_issue_type", report["invalid_rows"][0]["issues"])

    def test_unknown_issue_type_with_decision_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(
                        issue_type="new_unmapped_review_required",
                        decision="accept_link",
                        reviewer_id="hr_lead",
                        reviewed_at="2026-07-12T15:30:00+09:00",
                        rationale="Unknown issue type should not use global vocabulary alone.",
                    )
                ],
            )
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertIn("issue_type_decision_vocabulary_missing", report["invalid_rows"][0]["issues"])

    def test_require_completed_decisions_fails_when_any_decision_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(root / "review.csv", [self._row()])
            report = build_audit(csv_path, root=root, require_completed_decisions=True)

        self.assertFalse(report["ok"])
        self.assertTrue(report["completion_issue"])
        self.assertEqual(1, report["pending_decision_count"])

    def test_invalid_row_number_matches_csv_data_row_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [self._row(sequence="7", status_update_allowed="true")],
            )
            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertEqual(1, report["invalid_rows"][0]["row_number"])
        self.assertEqual("7", report["invalid_rows"][0]["row_key"])

    def test_issue_type_filter_limits_audit_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(
                root / "review.csv",
                [
                    self._row(sequence="1", issue_type="hr_training_goal_link_human_review_required"),
                    self._row(
                        sequence="2",
                        issue_type="ontology_task_ksa_relation_human_review_required",
                    ),
                ],
            )
            report = build_audit(
                csv_path,
                root=root,
                issue_types={"ontology_task_ksa_relation_human_review_required"},
            )

        self.assertTrue(report["ok"])
        self.assertEqual(2, report["source_row_count"])
        self.assertEqual(1, report["row_count"])
        self.assertEqual(
            ["ontology_task_ksa_relation_human_review_required"],
            report["row_filter"]["issue_types"],
        )
        self.assertEqual(1, report["row_filter"]["filtered_out_row_count"])
        self.assertEqual(
            {"ontology_task_ksa_relation_human_review_required": 1},
            report["issue_type_counts"],
        )

    def test_missing_human_decision_required_column_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = root / "review.csv"
            fields = [field for field in self.fields if field != "human_decision_required"]
            row = self._row()
            row.pop("human_decision_required")
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(row)

            report = build_audit(csv_path, root=root)

        self.assertFalse(report["ok"])
        self.assertIn("human_decision_required", report["missing_required_columns"])
        self.assertIn("missing_required_columns", report["invalid_rows"][0]["issues"])

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            csv_path = self._write_csv(root / "review.csv", [self._row()])
            out = root / "audit.json"
            md = root / "audit.md"
            argv = [
                "--csv",
                str(csv_path),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = audit_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Review Seedpack CSV Decision Audit", markdown)


if __name__ == "__main__":
    unittest.main()
