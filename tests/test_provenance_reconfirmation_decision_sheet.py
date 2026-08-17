from __future__ import annotations

import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


decision_sheet = load_script_module(
    "export_provenance_reconfirmation_decision_sheet",
    "scripts/export_provenance_reconfirmation_decision_sheet.py",
)
decision_audit = load_script_module(
    "audit_provenance_reconfirmation_decisions",
    "scripts/audit_provenance_reconfirmation_decisions.py",
)


class ProvenanceReconfirmationDecisionSheetTests(unittest.TestCase):
    def test_latest_report_path_accepts_aihr_and_short_human_review_families(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            readonly_root = root / "overnight_sessions" / "readonly_refresh"
            readonly_root.mkdir(parents=True)
            older = root / "aihr_human_review_provenance_reconfirmation_packet_20260620.json"
            newer = readonly_root / "human_review_provenance_reconfirmation_packet_20260621_followup.json"
            fallback = root / "fallback.json"
            older.write_text("{}", encoding="utf-8")
            newer.write_text("{}", encoding="utf-8")

            original_sheet_reports = decision_sheet.REPORTS
            original_audit_reports = decision_audit.REPORTS
            original_sheet_readonly = decision_sheet.READONLY_REFRESH_REPORTS
            original_audit_readonly = decision_audit.READONLY_REFRESH_REPORTS
            try:
                decision_sheet.REPORTS = root
                decision_audit.REPORTS = root
                decision_sheet.READONLY_REFRESH_REPORTS = readonly_root
                decision_audit.READONLY_REFRESH_REPORTS = readonly_root

                self.assertEqual(
                    decision_sheet._latest_report_path(
                        "aihr_human_review_provenance_reconfirmation_packet_20*.json",
                        "human_review_provenance_reconfirmation_packet_20*.json",
                        fallback=fallback,
                    ),
                    newer,
                )
                self.assertEqual(
                    decision_audit._latest_report_path(
                        "aihr_human_review_provenance_reconfirmation_packet_20*.json",
                        "human_review_provenance_reconfirmation_packet_20*.json",
                        fallback=fallback,
                    ),
                    newer,
                )
            finally:
                decision_sheet.REPORTS = original_sheet_reports
                decision_audit.REPORTS = original_audit_reports
                decision_sheet.READONLY_REFRESH_REPORTS = original_sheet_readonly
                decision_audit.READONLY_REFRESH_REPORTS = original_audit_readonly

    def test_decision_sheet_csv_neutralizes_formula_like_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            packet_path = tmp_path / "packet.json"
            csv_out = tmp_path / "decision_sheet.csv"
            packet = {
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "ok": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_query_aliases",
                        "target_table": "ncs_query_aliases",
                        "target_id": "2",
                        "entity_type": "ncs_query_alias",
                        "review_status_display": "legacy_status_needs_reconfirmation",
                        "status_trust": " @cmd",
                        "provenance_state": "audit_log_without_packet",
                        "display": "=2+2",
                        "scope": {"unit_name": " @cmd"},
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")

            report = decision_sheet.build_decision_sheet(packet_path)
            decision_sheet.write_csv(csv_out, report)

            with csv_out.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["display"], "'=2+2")
        self.assertEqual(rows[0]["status_trust"], "'@cmd")
        self.assertIn("unit_name=@cmd", rows[0]["scope_summary"])
        self.assertEqual(rows[0]["status_update_allowed"], "false")
        self.assertEqual(rows[0]["db_writes"], "false")
        self.assertEqual(rows[0]["approval_claim"], "false")

    def test_default_outputs_are_derived_from_selected_artifact_suffix(self) -> None:
        packet_path = Path(
            "reports/overnight_sessions/readonly_refresh/"
            "human_review_provenance_reconfirmation_packet_20260630_7h_extension.json"
        )
        csv_path = Path(
            "reports/overnight_sessions/readonly_refresh/"
            "human_review_provenance_reconfirmation_decision_sheet_20260630_7h_extension.csv"
        )

        self.assertEqual(
            decision_sheet.default_output_path(packet_path, ".json"),
            decision_sheet.REPORTS
            / "human_review_provenance_reconfirmation_decision_sheet_20260630_7h_extension.json",
        )
        self.assertEqual(
            decision_audit.default_output_path(csv_path, ".md"),
            decision_audit.REPORTS
            / "human_review_provenance_reconfirmation_decision_audit_20260630_7h_extension.md",
        )

    def test_decision_sheet_exports_blank_guarded_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            out = root / "decision_sheet.json"
            csv_out = root / "decision_sheet.csv"
            html_out = root / "decision_sheet.html"
            md_out = root / "decision_sheet.md"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "db_path": "C:/workspace/NCS_MCP/data/processed/ncs.db",
                "selected_surface_counts": {"ncs_career_paths": 1},
                "review_status_display_counts": {
                    "legacy_status_needs_reconfirmation:human_reviewed": 1
                },
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "entity_type": "ncs_career_path",
                        "target_id": "146",
                        "raw_review_status": "human_reviewed",
                        "review_status_display": "legacy_status_needs_reconfirmation:human_reviewed",
                        "status_trust": "not_trusted_until_packet_backed_reconfirmation",
                        "provenance_state": "audit_log_without_packet",
                        "display": "인사 / 직무관리 -> 직무관리",
                        "scope": {
                            "major_name": "경영·회계·사무",
                            "matched_unit_code": "0202020102_23v3",
                            "matched_unit_name": "직무관리",
                        },
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")

            report = decision_sheet.build_decision_sheet(packet_path)
            out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            decision_sheet.write_csv(csv_out, report)
            decision_sheet.write_html(html_out, report)
            decision_sheet.write_markdown(md_out, report)

            self.assertTrue(report["ok"])
            self.assertEqual(report["source_packet"], packet_path.name)
            self.assertEqual(report["row_count"], 1)
            self.assertEqual(report["blank_decision_count"], 1)
            self.assertFalse(report["db_writes"])
            self.assertFalse(report["api_calls"])
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["approval_claim"])
            self.assertTrue(report["human_decision_required"])
            self.assertTrue(report["policy"]["reconfirm_is_evidence_review_only"])
            self.assertTrue(report["policy"]["reconfirm_does_not_apply_or_preserve_status"])
            self.assertEqual(report["generated_at"], report["created_at"])
            self.assertTrue(report["content_sha256_excluding_self_hash"].startswith("sha256:"))
            self.assertEqual(
                report["content_hash_algorithm"],
                "sha256(stable_json(report_without_content_sha256_excluding_self_hash))",
            )
            self.assertTrue(report["source_packet_sha256"].startswith("sha256:"))
            self.assertIn("reviewed_at", report["minimum_packet_fields"])
            self.assertEqual(report["source_packet_row_identity_issue_count"], 0)
            self.assertEqual(report["source_packet_row_identity_issues"], [])
            self.assertEqual(report["rows"][0]["decision"], "")
            self.assertNotIn("raw_review_status", report["rows"][0])
            self.assertEqual(
                report["rows"][0]["review_status_display"],
                "legacy_status_needs_reconfirmation",
            )
            self.assertEqual(report["rows"][0]["source_decision_packet"], "")
            self.assertEqual(
                report["rows"][0]["source_packet_sha256"],
                report["source_packet_sha256"],
            )
            self.assertTrue(report["rows"][0]["source_packet_row_sha256"].startswith("sha256:"))
            self.assertEqual(report["rows"][0]["evidence_refs_json"], "")
            self.assertIs(report["rows"][0]["status_update_allowed"], False)
            self.assertIs(report["rows"][0]["db_writes"], False)
            self.assertIs(report["rows"][0]["approval_claim"], False)

            with csv_out.open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual(len(csv_rows), 1)
            self.assertEqual(csv_rows[0]["decision"], "")
            self.assertEqual(csv_rows[0]["status_update_allowed"], "false")
            self.assertEqual(csv_rows[0]["db_writes"], "false")
            self.assertEqual(csv_rows[0]["approval_claim"], "false")
            self.assertNotIn("raw_review_status", csv_rows[0])
            self.assertEqual(
                csv_rows[0]["review_status_display"],
                "legacy_status_needs_reconfirmation",
            )
            self.assertTrue(csv_rows[0]["source_packet_sha256"].startswith("sha256:"))
            self.assertTrue(csv_rows[0]["source_packet_row_sha256"].startswith("sha256:"))

            output_text = out.read_text(encoding="utf-8")
            csv_text = csv_out.read_text(encoding="utf-8-sig")
            html_text = html_out.read_text(encoding="utf-8")
            markdown_text = md_out.read_text(encoding="utf-8")
            self.assertNotIn(str(root), output_text)
            self.assertNotIn("db_path", output_text)
            self.assertNotIn("data/processed/ncs.db", output_text)
            for rendered in (output_text, csv_text, html_text, markdown_text):
                self.assertNotIn("raw_review_status", rendered)
                self.assertNotIn("legacy_status_needs_reconfirmation:human_reviewed", rendered)
                self.assertNotIn("human_reviewed", rendered)
            self.assertNotIn(str(root), html_text)
            self.assertNotIn(str(root), markdown_text)
            self.assertIn("not_trusted_until_packet_backed_reconfirmation", html_text)
            self.assertIn("API calls", markdown_text)
            self.assertIn("`reviewed_at`", markdown_text)
            self.assertIn("evidence-review input", html_text)
            self.assertIn("evidence-review input only", markdown_text)
            self.assertIn("not a DB write, approval claim, or status-preservation action", markdown_text)

    def test_decision_audit_keeps_blank_rows_pending_and_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            markdown_out = root / "decision_audit.md"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)
            decision_audit.write_markdown(markdown_out, audit)
            markdown_text = markdown_out.read_text(encoding="utf-8")

            self.assertTrue(audit["ok"])
            self.assertEqual(audit["csv"], csv_out.name)
            self.assertEqual(audit["source_packet"], packet_path.name)
            self.assertEqual(audit["row_count"], 1)
            self.assertEqual(audit["pending_decision_count"], 1)
            self.assertEqual(audit["completed_decision_count"], 0)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertTrue(audit["report_only"])
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["api_calls"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["approval_claim"])
            self.assertFalse(audit["acceptance_claim"])
            self.assertTrue(audit["human_decision_required"])
            self.assertFalse(audit["guarded_apply_ready"])
            self.assertTrue(audit["policy"]["reconfirm_is_evidence_review_only"])
            self.assertTrue(audit["policy"]["reconfirm_does_not_apply_or_preserve_status"])
            self.assertTrue(audit["source_packet_sha256"].startswith("sha256:"))
            self.assertEqual(audit["source_packet_row_identity_issue_count"], 0)
            self.assertEqual(audit["source_decision_packet_unsupported_type_count"], 0)
            self.assertEqual(audit["source_decision_packet_unrecognized_count"], 0)
            self.assertIn("evidence-review input only", markdown_text)
            self.assertIn("does not approve, downgrade, preserve, or write any review status", markdown_text)

    def test_source_packet_rows_require_stable_target_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "entity_type": "ncs_career_path",
                        "display": "Row without a stable target identity",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")

            report = decision_sheet.build_decision_sheet(packet_path)
            decision_sheet.write_csv(csv_out, report)
            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(report["ok"])
            self.assertEqual(report["source_packet_row_identity_issue_count"], 3)
            self.assertIn("row_1_missing_surface", report["source_packet_row_identity_issues"])
            self.assertFalse(audit["ok"])
            self.assertEqual(audit["source_packet_row_identity_issue_count"], 3)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertIn(
                "source_packet_row_identity_incomplete",
                audit["issue_type_counts"],
            )

    def test_decision_audit_accepts_filled_reconfirm_as_action_eligible_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            evidence_packet = root / "evidence_packet.json"
            evidence_packet.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["rationale"] = "Reviewer confirmed the packet evidence."
            row["reviewer_id"] = "hr-reviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00+09:00"
            row["source_decision_packet"] = evidence_packet.name
            row["evidence_refs_json"] = json.dumps(["packet#row:1"])
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertTrue(audit["ok"])
            self.assertEqual(audit["pending_decision_count"], 0)
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["invalid_decision_count"], 0)
            self.assertEqual(audit["missing_required_field_row_count"], 0)
            self.assertEqual(audit["action_eligible_count"], 1)
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["approval_claim"])
            self.assertFalse(audit["guarded_apply_ready"])

    def test_decision_audit_rejects_absolute_source_decision_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            evidence_packet = root / "evidence_packet.json"
            evidence_packet.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["rationale"] = "Reviewer confirmed the packet evidence."
            row["reviewer_id"] = "hr-reviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00+09:00"
            row["source_decision_packet"] = str(evidence_packet)
            row["evidence_refs_json"] = json.dumps(["packet#row:1"])
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertEqual(audit["source_decision_packet_not_portable_count"], 1)
            self.assertIn(
                "source_decision_packet_not_portable",
                audit["issue_type_counts"],
            )

    def test_decision_audit_rejects_unrelated_packet_file_and_empty_evidence_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            unrelated = root / "totally_unrelated.json"
            unrelated.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["rationale"] = "Reviewer confirmed the packet evidence."
            row["reviewer_id"] = "hr-reviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00+09:00"
            row["source_decision_packet"] = unrelated.name
            row["evidence_refs_json"] = json.dumps([])
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertEqual(audit["source_decision_packet_unrecognized_count"], 1)
            self.assertEqual(audit["invalid_evidence_refs_json_count"], 1)
            self.assertIn("source_decision_packet_unrecognized", audit["issue_type_counts"])
            self.assertIn("invalid_evidence_refs_json", audit["issue_type_counts"])

    def test_decision_audit_rejects_invalid_completed_reviewer_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            evidence_packet = root / "evidence_packet.json"
            evidence_packet.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["rationale"] = "Reviewer confirmed the packet evidence."
            row["reviewer_id"] = "hr\nreviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00"
            row["source_decision_packet"] = evidence_packet.name
            row["evidence_refs_json"] = json.dumps("packet#row:1")
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertEqual(audit["invalid_reviewer_id_count"], 1)
            self.assertEqual(audit["invalid_reviewed_at_count"], 1)
            self.assertEqual(audit["invalid_evidence_refs_json_count"], 1)
            self.assertIn("invalid_reviewer_id", audit["issue_type_counts"])
            self.assertIn("invalid_reviewed_at", audit["issue_type_counts"])
            self.assertIn("invalid_evidence_refs_json", audit["issue_type_counts"])
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["approval_claim"])

    def test_decision_audit_rejects_stale_sheet_when_source_packet_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            evidence_packet = root / "evidence_packet.json"
            evidence_packet.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                        "status_trust": "not_trusted_until_packet_backed_reconfirmation",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["rationale"] = "Reviewer confirmed the packet evidence."
            row["reviewer_id"] = "hr-reviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00+09:00"
            row["source_decision_packet"] = evidence_packet.name
            row["evidence_refs_json"] = json.dumps(["packet#row:1"])
            decision_sheet.write_csv(csv_out, report)

            stale_packet = dict(packet)
            stale_packet["rows"] = [
                {
                    **packet["rows"][0],
                    "display": "HR career path changed after sheet export",
                    "status_trust": "changed_trust_marker",
                }
            ]
            packet_path.write_text(json.dumps(stale_packet, ensure_ascii=False), encoding="utf-8")

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertEqual(audit["source_mismatch_count"], 0)
            self.assertEqual(audit["source_identity_mismatch_count"], 1)
            self.assertIn("source_packet_sha256_mismatch", audit["issue_type_counts"])
            self.assertIn("source_packet_row_sha256_mismatch", audit["issue_type_counts"])

    def test_decision_audit_rejects_truncated_or_duplicate_csv_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            truncated_csv = root / "truncated.csv"
            duplicate_csv = root / "duplicate.csv"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    },
                    {
                        "order": 2,
                        "surface": "training_transition_scenario_reviews",
                        "target_table": "training_transition_scenario_reviews",
                        "target_id": "scenario-1",
                        "entity_type": "transition_review",
                        "display": "Scenario review",
                    },
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)

            truncated_report = {**report, "rows": report["rows"][:1]}
            decision_sheet.write_csv(truncated_csv, truncated_report)
            truncated_audit = decision_audit.build_decision_audit(truncated_csv, packet_path)

            self.assertFalse(truncated_audit["ok"])
            self.assertEqual(truncated_audit["missing_packet_row_count"], 1)
            self.assertEqual(truncated_audit["unexpected_csv_row_count"], 1)
            self.assertIn("missing_packet_rows", truncated_audit["issue_type_counts"])

            duplicate_report = {**report, "rows": [report["rows"][0], report["rows"][0]]}
            decision_sheet.write_csv(duplicate_csv, duplicate_report)
            duplicate_audit = decision_audit.build_decision_audit(duplicate_csv, packet_path)

            self.assertFalse(duplicate_audit["ok"])
            self.assertEqual(duplicate_audit["duplicate_csv_key_count"], 1)
            self.assertEqual(duplicate_audit["missing_packet_row_count"], 1)
            self.assertIn("duplicate_csv_rows", duplicate_audit["issue_type_counts"])

    def test_decision_audit_rejects_missing_or_blank_guard_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            missing_guard_csv = root / "missing_guard.csv"
            blank_guard_csv = root / "blank_guard.csv"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)

            fieldnames = [
                field
                for field in decision_sheet.DECISION_FIELDS
                if field != "approval_claim"
            ]
            with missing_guard_csv.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerow(report["rows"][0])
            missing_guard_audit = decision_audit.build_decision_audit(
                missing_guard_csv,
                packet_path,
            )

            self.assertFalse(missing_guard_audit["ok"])
            self.assertIn("approval_claim", missing_guard_audit["missing_csv_columns"])
            self.assertIn("missing_csv_columns", missing_guard_audit["issue_type_counts"])

            report["rows"][0]["status_update_allowed"] = ""
            decision_sheet.write_csv(blank_guard_csv, report)
            blank_guard_audit = decision_audit.build_decision_audit(
                blank_guard_csv,
                packet_path,
            )

            self.assertFalse(blank_guard_audit["ok"])
            self.assertEqual(blank_guard_audit["unsafe_flag_count"], 1)
            self.assertIn("unsafe_true_fields", blank_guard_audit["issue_type_counts"])

    def test_decision_audit_rejects_decision_missing_required_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            evidence_packet = root / "evidence_packet.json"
            evidence_packet.write_text("{}", encoding="utf-8")
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "reconfirm"
            row["reviewer_id"] = "hr-reviewer"
            row["reviewed_at"] = "2026-06-29T05:00:00+09:00"
            row["source_decision_packet"] = evidence_packet.name
            row["evidence_refs_json"] = json.dumps(["packet#row:1"])
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["completed_decision_count"], 1)
            self.assertEqual(audit["missing_required_field_row_count"], 1)
            self.assertEqual(audit["action_eligible_count"], 0)
            self.assertIn("missing_required_fields", audit["issue_type_counts"])
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["approval_claim"])

    def test_decision_audit_flags_invalid_or_unsafe_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            report = decision_sheet.build_decision_sheet(packet_path)
            row = report["rows"][0]
            row["decision"] = "approve"
            row["status_update_allowed"] = "true"
            row["db_writes"] = "true"
            decision_sheet.write_csv(csv_out, report)

            audit = decision_audit.build_decision_audit(csv_out, packet_path)

            self.assertFalse(audit["ok"])
            self.assertEqual(audit["invalid_decision_count"], 1)
            self.assertEqual(audit["unsafe_flag_count"], 1)
            self.assertIn("invalid_decision", audit["issue_type_counts"])
            self.assertIn("unsafe_true_fields", audit["issue_type_counts"])
            self.assertEqual(audit["action_eligible_count"], 0)

    def test_unsafe_source_packet_blocks_sheet_and_audit_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet_path = root / "packet.json"
            csv_out = root / "decision_sheet.csv"
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "status_update_allowed": True,
                "db_writes": True,
                "approval_claim": True,
                "human_decision_required": False,
                "rows": [
                    {
                        "order": 1,
                        "surface": "ncs_career_paths",
                        "target_table": "ncs_career_paths",
                        "target_id": "146",
                        "entity_type": "ncs_career_path",
                        "display": "HR career path",
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")

            report = decision_sheet.build_decision_sheet(packet_path)
            decision_sheet.write_csv(csv_out, report)
            audit = decision_audit.build_decision_audit(csv_out, packet_path)

        self.assertFalse(report["ok"])
        self.assertFalse(report["source_packet_contract_ok"])
        self.assertEqual(report["row_count"], 1)
        self.assertIn("status_update_allowed_not_false", report["source_packet_contract_issues"])
        self.assertIn("db_writes_not_false", report["source_packet_contract_issues"])
        self.assertIn("approval_claim_not_false", report["source_packet_contract_issues"])
        self.assertIn(
            "human_decision_required_not_true",
            report["source_packet_contract_issues"],
        )
        self.assertFalse(audit["ok"])
        self.assertFalse(audit["source_packet_contract_ok"])
        self.assertEqual(audit["pending_decision_count"], 1)
        self.assertEqual(audit["action_eligible_count"], 0)
        self.assertIn(
            "source_packet_status_update_allowed_not_false",
            audit["issue_type_counts"],
        )
        self.assertIn("source_packet_db_writes_not_false", audit["issue_type_counts"])
        self.assertIn("source_packet_approval_claim_not_false", audit["issue_type_counts"])
        self.assertIn(
            "source_packet_human_decision_required_not_true",
            audit["issue_type_counts"],
        )


if __name__ == "__main__":
    unittest.main()
