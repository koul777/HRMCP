from __future__ import annotations

import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.sqf_guarded_import_plan import (
    build_sqf_guarded_import_plan,
    render_sqf_guarded_import_plan_markdown,
    write_sqf_guarded_import_plan_json,
    write_sqf_guarded_import_plan_markdown,
)


FIELDS = [
    "order",
    "claim_id",
    "claim_type",
    "recommended_priority",
    "ncs_unit_code",
    "ncs_unit_name",
    "mapping_relation",
    "top_evidence_refs",
    "decision",
    "reason",
    "reject_reason_code",
    "defer_reason_code",
    "notes",
    "reviewer_id",
    "reviewed_at",
    "source_packet",
    "status_update_allowed",
    "used_for_scoring",
    "approval_claim",
]
FORBIDDEN_MARKERS = [
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
]


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            base = {field: "" for field in FIELDS}
            base.update(
                {
                    "status_update_allowed": "false",
                    "used_for_scoring": "false",
                    "approval_claim": "false",
                    "source_packet": "reports/sqf_claims.json",
                }
            )
            base.update(row)
            writer.writerow(base)


def write_claims(path: Path) -> None:
    payload = {
        "ok": True,
        "claims": [
            {
                "claim_id": "claim-1",
                "status_update_allowed": False,
                "used_for_scoring": False,
                "approval_claim": False,
                "sqf_ncs_match": {"match_id": 101, "relation": "closeMatch"},
            },
            {
                "claim_id": "claim-2",
                "status_update_allowed": False,
                "used_for_scoring": False,
                "approval_claim": False,
                "sqf_ncs_match": {"match_id": 102, "relation": "partiallyCovers"},
            },
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def write_match_db(path: Path, *, status_101: str = "candidate", status_102: str = "candidate") -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sqf_ncs_matches(
            match_id INTEGER PRIMARY KEY,
            review_status TEXT,
            relation TEXT,
            target_id TEXT
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO sqf_ncs_matches(match_id, review_status, relation, target_id)
        VALUES (?, ?, ?, ?)
        """,
        [
            (101, status_101, "closeMatch", "0202020101_23v3"),
            (102, status_102, "partiallyCovers", "0203020101_20v4"),
        ],
    )
    conn.commit()
    conn.close()


class SqfGuardedImportPlanTests(unittest.TestCase):
    def test_pending_sheet_has_no_plan_items_and_is_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sheet = tmp_path / "decision.csv"
            claims = tmp_path / "claims.json"
            write_csv(sheet, [{"order": "1", "claim_id": "claim-1"}])
            write_claims(claims)

            report = build_sqf_guarded_import_plan(decision_sheet_path=sheet, claim_report_path=claims)
            markdown = render_sqf_guarded_import_plan_markdown(report)
            serialized = json.dumps(report, ensure_ascii=False)

        self.assertTrue(report["ok"])
        self.assertFalse(report["execution_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["used_for_scoring"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["summary"]["pending_count"], 1)
        self.assertEqual(report["summary"]["plan_item_count"], 0)
        self.assertIn("dry_run_only", serialized)
        self.assertIn("# SQF Guarded Import Plan", markdown)
        for forbidden in FORBIDDEN_MARKERS:
            self.assertNotIn(forbidden, serialized)
            self.assertNotIn(forbidden, markdown)

    def test_valid_approve_and_reject_rows_create_non_executable_templates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sheet = tmp_path / "decision.csv"
            claims = tmp_path / "claims.json"
            db_path = tmp_path / "ncs.db"
            out_json = tmp_path / "plan.json"
            out_md = tmp_path / "plan.md"
            write_csv(
                sheet,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "ncs_unit_code": "0202020101_23v3",
                        "ncs_unit_name": "HR planning",
                        "mapping_relation": "closeMatch",
                        "top_evidence_refs": "claim-1:e1;claim-1:e2",
                        "decision": "approve",
                        "reason": "human checked supplementary evidence",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:00:00+09:00",
                    },
                    {
                        "order": "2",
                        "claim_id": "claim-2",
                        "ncs_unit_code": "0203020101_20v4",
                        "ncs_unit_name": "Accounting",
                        "mapping_relation": "partiallyCovers",
                        "decision": "reject",
                        "reject_reason_code": "scope_mismatch",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:05:00+09:00",
                    },
                ],
            )
            write_claims(claims)
            write_match_db(db_path)

            report = build_sqf_guarded_import_plan(
                decision_sheet_path=sheet,
                claim_report_path=claims,
                db_path=db_path,
                run_artifact_name="reports/sqf_guarded_import_plan.json",
            )
            write_sqf_guarded_import_plan_json(report, out_json)
            write_sqf_guarded_import_plan_markdown(report, out_md)
            loaded = json.loads(out_json.read_text(encoding="utf-8"))
            markdown = out_md.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertFalse(report["execution_allowed"])
        self.assertEqual(report["planned_db_writes"], 0)
        self.assertEqual(report["summary"]["plan_item_count"], 2)
        self.assertEqual(report["summary"]["approve_plan_count"], 1)
        self.assertEqual(report["summary"]["reject_plan_count"], 1)
        self.assertTrue(report["summary"]["db_status_check_performed"])
        self.assertEqual(report["summary"]["db_status_stale_or_missing_count"], 0)
        approve = report["plan_items"][0]["server_call_template"]
        reject = report["plan_items"][1]["server_call_template"]
        self.assertIsNone(approve["new_status"])
        self.assertEqual(approve["new_status_policy"], "operator_status_mapping_required")
        self.assertEqual(approve["suggested_status_options"], ["reviewed"])
        self.assertEqual(approve["match_id"], 101)
        self.assertEqual(approve["source_decision_packet"], sheet.name)
        self.assertEqual(approve["evidence_refs"], ["claim-1:e1", "claim-1:e2"])
        self.assertIsNone(reject["new_status"])
        self.assertEqual(reject["new_status_policy"], "operator_status_mapping_required")
        self.assertEqual(reject["suggested_status_options"], ["rejected"])
        self.assertEqual(reject["match_id"], 102)
        self.assertFalse(report["plan_items"][0]["execution_allowed"])
        self.assertEqual(loaded["schema"], "ncs_sqf_guarded_import_plan_v1")
        self.assertIn("SQF Guarded Import Plan", markdown)

    def test_stale_db_match_status_blocks_plan_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sheet = tmp_path / "decision.csv"
            claims = tmp_path / "claims.json"
            db_path = tmp_path / "ncs.db"
            write_csv(
                sheet,
                [
                    {
                        "order": "1",
                        "claim_id": "claim-1",
                        "ncs_unit_code": "0202020101_23v3",
                        "mapping_relation": "closeMatch",
                        "top_evidence_refs": "claim-1:e1",
                        "decision": "approve",
                        "reason": "human checked supplementary evidence",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:00:00+09:00",
                    }
                ],
            )
            write_claims(claims)
            write_match_db(db_path, status_101="reviewed")

            report = build_sqf_guarded_import_plan(
                decision_sheet_path=sheet,
                claim_report_path=claims,
                db_path=db_path,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["plan_item_count"], 0)
        self.assertEqual(report["summary"]["db_status_stale_or_missing_count"], 1)
        self.assertIn("db_match_status_not_candidate", {finding["code"] for finding in report["findings"]})

    def test_invalid_decision_or_missing_claim_blocks_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            sheet = tmp_path / "decision.csv"
            claims = tmp_path / "claims.json"
            write_csv(
                sheet,
                [
                    {
                        "order": "1",
                        "claim_id": "missing-claim",
                        "decision": "approve",
                        "reason": "missing claim should block",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:00:00+09:00",
                        "top_evidence_refs": "missing:e1",
                    },
                    {
                        "order": "2",
                        "claim_id": "claim-1",
                        "decision": "approve",
                        "reason": "missing evidence refs should fail audit",
                        "reviewer_id": "reviewer-a",
                        "reviewed_at": "2026-06-20T09:05:00+09:00",
                    },
                ],
            )
            write_claims(claims)

            report = build_sqf_guarded_import_plan(decision_sheet_path=sheet, claim_report_path=claims)

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["plan_item_count"], 0)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("claim_not_found", codes)
        self.assertIn("decision_audit_not_ok", codes)
        self.assertIn("decision_row_not_audit_valid", codes)


if __name__ == "__main__":
    unittest.main()
