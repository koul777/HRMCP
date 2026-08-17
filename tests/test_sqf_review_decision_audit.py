from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.sqf_review_decision_audit import (
    build_sqf_review_decision_audit,
    record_sqf_review_decision,
    render_sqf_review_decision_audit_markdown,
    write_sqf_review_decision_audit_json,
    write_sqf_review_decision_audit_markdown,
)


FIELDS = [
    "order",
    "claim_id",
    "claim_type",
    "recommended_priority",
    "job_name",
    "duty_name",
    "ncs_unit_code",
    "ncs_unit_name",
    "mapping_relation",
    "evidence_strength",
    "scope_alignment",
    "decision",
    "reason",
    "rationale",
    "reject_reason_code",
    "defer_reason_code",
    "reviewer_id",
    "reviewed_at",
    "source_packet",
    "top_evidence_refs",
    "status_update_allowed",
    "used_for_scoring",
    "approval_claim",
    "db_writes",
]


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str] | None = None) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or FIELDS)
        writer.writeheader()
        for row in rows:
            base = {field: "" for field in (fieldnames or FIELDS)}
            base.update(row)
            writer.writerow(base)


def guardrails() -> dict[str, str]:
    return {
        "status_update_allowed": "false",
        "used_for_scoring": "false",
        "approval_claim": "false",
        "db_writes": "false",
    }


class SqfReviewDecisionAuditTests(unittest.TestCase):
    def test_build_audit_validates_blank_valid_and_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            json_path = tmp_path / "audit.json"
            markdown_path = tmp_path / "audit.md"
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "",
                        **guardrails(),
                    },
                    {
                        "order": "2",
                        "claim_id": "claim-2",
                        "ncs_unit_code": "0202010101_23v3",
                        "decision": "approve",
                        "reason": "evidence refs support the supplementary mapping",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:00:00+09:00",
                        "source_packet": "reports/sqf_claims.json",
                        "top_evidence_refs": "claim-2:e1;claim-2:e2",
                        **guardrails(),
                    },
                    {
                        "order": "3",
                        "claim_id": "claim-3",
                        "decision": "reject",
                        "reject_reason_code": "scope_mismatch",
                        **guardrails(),
                    },
                    {
                        "order": "4",
                        "claim_id": "claim-4",
                        "decision": "defer",
                        "reason": "needs another report packet",
                        **guardrails(),
                    },
                    {
                        "order": "5",
                        "claim_id": "claim-5",
                        "decision": "approve",
                        "source_packet": "reports/sqf_claims.json",
                        "reviewed_at": "2026-06-20T10:00:00+09:00",
                        **guardrails(),
                    },
                ],
            )

            report = build_sqf_review_decision_audit(csv_path)
            write_sqf_review_decision_audit_json(report, json_path)
            write_sqf_review_decision_audit_markdown(report, markdown_path)
            rendered = render_sqf_review_decision_audit_markdown(report)
            loaded = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        summary = report["summary"]
        self.assertFalse(report["ok"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["used_for_scoring"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["execution_allowed"])
        self.assertTrue(report["pre_import_annotation_only"])
        self.assertEqual(report["allowed_decisions"], ["blank", "approve", "reject", "defer"])
        self.assertEqual(summary["row_count"], 5)
        self.assertEqual(summary["blank_count"], 1)
        self.assertEqual(summary["approve_count"], 2)
        self.assertEqual(summary["reject_count"], 1)
        self.assertEqual(summary["defer_count"], 1)
        self.assertEqual(summary["invalid_count"], 1)
        self.assertEqual(summary["pending_review_count"], 1)
        self.assertEqual(summary["completed_decision_count"], 4)
        self.assertEqual(summary["valid_completed_decision_count"], 3)
        self.assertEqual(summary["status_update_candidate_count"], 2)
        self.assertEqual(summary["guarded_import_candidate_count"], 2)
        self.assertEqual(summary["pre_import_annotation_count"], 3)
        self.assertEqual(summary["import_ready_count"], 2)
        self.assertEqual(summary["defer_ready_count"], 1)
        self.assertEqual(summary["missing_required_counts"]["reviewer_id"], 1)
        self.assertEqual(summary["missing_required_counts"]["reason_or_rationale"], 1)
        self.assertEqual(summary["missing_required_counts"]["top_evidence_refs"], 1)
        self.assertTrue(report["rows"][0]["valid"])
        self.assertFalse(report["rows"][0]["status_update_candidate"])
        self.assertTrue(report["rows"][1]["valid"])
        self.assertTrue(report["rows"][1]["status_update_candidate"])
        self.assertTrue(report["rows"][1]["guarded_import_candidate"])
        self.assertTrue(report["rows"][1]["pre_import_annotation"])
        self.assertFalse(report["rows"][1]["execution_allowed"])
        self.assertEqual(report["rows"][1]["top_evidence_ref_count"], 2)
        self.assertFalse(report["rows"][3]["status_update_candidate"])
        self.assertFalse(report["rows"][4]["valid"])
        self.assertFalse(report["rows"][4]["status_update_candidate"])
        self.assertEqual(loaded["schema"], "ncs_sqf_review_decision_audit_v1")
        self.assertIn("SQF Review Decision Audit", markdown)
        self.assertIn("invalid_count: 1", markdown)
        self.assertEqual(markdown, rendered)

    def test_sensitive_columns_are_invalid_and_not_echoed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            fieldnames = [*FIELDS, "asset_path"]
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "source_payload",
                        "asset_path": "internal.pdf",
                        **guardrails(),
                    }
                ],
                fieldnames=fieldnames,
            )

            report = build_sqf_review_decision_audit(csv_path)
            markdown = render_sqf_review_decision_audit_markdown(report)
            serialized = json.dumps(report, ensure_ascii=False)

        self.assertEqual(report["summary"]["invalid_count"], 1)
        self.assertEqual(report["rows"][0]["decision"], "invalid")
        self.assertIn("decision_not_allowed", report["rows"][0]["issue_codes"])
        self.assertIn("sensitive_reference", report["rows"][0]["issue_codes"])
        for forbidden in [
            "asset_path",
            "local_path",
            "db_path",
            "source_payload",
            "raw_payload",
            "raw_response",
        ]:
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, markdown)

    def test_record_sqf_review_decision_updates_csv_without_db_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            out_csv = tmp_path / "decision_sheet.reviewed.csv"
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "",
                        "source_packet": "reports/sqf_claims.json",
                        "top_evidence_refs": "claim-1:e1",
                        **guardrails(),
                    }
                ],
            )

            result = record_sqf_review_decision(
                decision_sheet_path=csv_path,
                out_csv_path=out_csv,
                order="1",
                decision="approve",
                reason="report evidence supports supplementary review context",
                reviewer_id="reviewer-a",
                reviewed_at="2026-06-20T09:00:00+09:00",
            )
            audit = build_sqf_review_decision_audit(out_csv)
            out_csv_exists = out_csv.exists()

        self.assertTrue(result["ok"])
        self.assertFalse(result["db_writes"])
        self.assertFalse(result["status_update_allowed"])
        self.assertEqual(result["status"], "pre_import_annotation_recorded")
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(result["pre_import_annotation_only"])
        self.assertEqual(result["updated_count"], 1)
        self.assertEqual(result["updated_row"]["decision"], "approve")
        self.assertTrue(out_csv_exists)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["summary"]["import_ready_count"], 1)
        self.assertEqual(audit["summary"]["pending_review_count"], 0)

    def test_record_sqf_review_decision_escapes_formula_like_csv_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            out_csv = tmp_path / "decision_sheet.reviewed.csv"
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "",
                        "ncs_unit_name": "=unit",
                        "mapping_relation": " @relation",
                        "source_packet": "reports/sqf_claims.json",
                        "top_evidence_refs": "claim-1:e1",
                        **guardrails(),
                    }
                ],
            )

            result = record_sqf_review_decision(
                decision_sheet_path=csv_path,
                out_csv_path=out_csv,
                order="1",
                decision="approve",
                reason="=HYPERLINK(\"http://example.com\")",
                notes="\t=cmd",
                reviewer_id="+reviewer",
                reviewed_at="2026-06-20T09:00:00+09:00",
            )
            with out_csv.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertTrue(result["ok"])
        self.assertEqual(rows[0]["ncs_unit_name"], "'=unit")
        self.assertEqual(rows[0]["mapping_relation"], "' @relation")
        self.assertEqual(rows[0]["reason"], "'=HYPERLINK(\"http://example.com\")")
        self.assertEqual(rows[0]["notes"], "'=cmd")
        self.assertEqual(rows[0]["reviewer_id"], "'+reviewer")

    def test_record_sqf_review_decision_blocks_invalid_record_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            out_csv = tmp_path / "decision_sheet.reviewed.csv"
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "",
                        "source_packet": "reports/sqf_claims.json",
                        **guardrails(),
                    }
                ],
            )

            result = record_sqf_review_decision(
                decision_sheet_path=csv_path,
                out_csv_path=out_csv,
                claim_id="claim-1",
                decision="approve",
                reason="missing evidence refs should block",
                reviewer_id="reviewer-a",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["updated_count"], 0)
        self.assertFalse(out_csv.exists())
        self.assertEqual(result["findings"][0]["code"], "recorded_decision_row_invalid")

    def test_record_sqf_review_decision_blocks_sensitive_source_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision_sheet.csv"
            out_csv = tmp_path / "decision_sheet.reviewed.csv"
            write_csv(
                csv_path,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "decision": "",
                        "source_packet": "reports/sqf_claims.json",
                        "top_evidence_refs": "claim-1:e1",
                        **guardrails(),
                    }
                ],
            )

            result = record_sqf_review_decision(
                decision_sheet_path=csv_path,
                out_csv_path=out_csv,
                order="1",
                decision="approve",
                reason="path hygiene check",
                reviewer_id="reviewer-a",
                source_packet=str((tmp_path / "claims.json").resolve()),
            )

        self.assertFalse(result["ok"])
        self.assertFalse(out_csv.exists())
        self.assertEqual(result["findings"][0]["code"], "source_packet_sensitive_reference")


if __name__ == "__main__":
    unittest.main()
