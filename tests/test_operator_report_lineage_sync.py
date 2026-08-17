from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.audit_operator_report_lineage_sync import (  # noqa: E402
    build_lineage_sync_audit,
    sha256_file,
    write_markdown,
)


class OperatorReportLineageSyncAuditTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        reports.mkdir()
        open_first = reports / "open_first.csv"
        open_first.write_text("decision,reviewer_id\n,\n", encoding="utf-8")
        support = reports / "support.json"
        self._write_json(support, {"ok": True})

        generated_at = "2026-07-12T02:00:00+00:00"
        next_json = reports / "aihr_operator_next_actions_20260712_10h.json"
        next_md = reports / "aihr_operator_next_actions_20260712_10h.md"
        next_payload = {
            "schema": "aihr_operator_next_actions_v3",
            "generated_at": generated_at,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "source_paths": {"support": "reports/support.json"},
            "source_hashes": {"support": sha256_file(support)},
            "actions": [
                {
                    "id": "review_debt:human_reviewed_concepts",
                    "open_first": "reports/open_first.csv",
                    "artifacts_to_open": ["reports/open_first.csv"],
                }
            ],
        }
        self._write_json(next_json, next_payload)
        next_md.write_text(
            "# AI-HR Operator Next Actions\n\n"
            f"- generated_at: `{generated_at}`\n",
            encoding="utf-8",
        )

        operator_json = reports / "operator_review_packet_integrity_audit_20260712_10h.json"
        operator_md = reports / "operator_review_packet_integrity_audit_20260712_10h.md"
        self._write_json(
            operator_json,
            {
                "schema": "operator_review_packet_integrity_audit_v2",
                "ok": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            },
        )
        operator_md.write_text("# Operator Review Packet Integrity Audit\n", encoding="utf-8")
        queue_json = reports / "aihr_blocker_reduction_operator_sprint_queue_20260712_10h.json"
        queue_md = reports / "aihr_blocker_reduction_operator_sprint_queue_20260712_10h.md"
        queue_audit = reports / "aihr_blocker_reduction_operator_sprint_queue_audit_20260712_10h.json"
        self._write_json(queue_json, {"schema": "aihr_blocker_reduction_operator_sprint_queue_v1"})
        queue_md.write_text("# Queue\n", encoding="utf-8")
        self._write_json(queue_audit, {"schema": "aihr_blocker_reduction_operator_sprint_queue_audit_v1"})

        decision_json = (
            reports / "human_review_provenance_reconfirmation_decision_sheet_20260712_10h.json"
        )
        self._write_json(
            decision_json,
            {
                "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                "created_at": generated_at,
                "generated_at": generated_at,
                "content_sha256_excluding_self_hash": "sha256:" + "a" * 64,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            },
        )

        handoff_json = reports / "overnight_10h_operator_handoff_20260712_10h.json"
        self._write_json(
            handoff_json,
            {
                "schema": "overnight_10h_operator_handoff_v3",
                "operator_next_actions": {
                    "sha256": sha256_file(next_json),
                    "markdown_sha256": sha256_file(next_md),
                },
                "operator_packet_integrity_audit": {
                    "sha256": sha256_file(operator_json),
                    "markdown_sha256": sha256_file(operator_md),
                },
                "blocker_reduction_sprint_queue": {
                    "path": "reports/aihr_blocker_reduction_operator_sprint_queue_20260712_10h.json",
                    "markdown_path": "reports/aihr_blocker_reduction_operator_sprint_queue_20260712_10h.md",
                    "audit_path": "reports/aihr_blocker_reduction_operator_sprint_queue_audit_20260712_10h.json",
                    "sha256": sha256_file(queue_json),
                    "markdown_sha256": sha256_file(queue_md),
                    "audit_sha256": sha256_file(queue_audit),
                },
            },
        )

        return {
            "next_json": next_json,
            "next_md": next_md,
            "support": support,
            "handoff_json": handoff_json,
            "operator_json": operator_json,
            "operator_md": operator_md,
            "queue_json": queue_json,
            "decision_json": decision_json,
        }

    def test_lineage_sync_audit_passes_for_current_hashes_and_open_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)

            report = build_lineage_sync_audit(
                next_actions_json=paths["next_json"],
                next_actions_markdown=paths["next_md"],
                handoff_json=paths["handoff_json"],
                operator_audit_json=paths["operator_json"],
                operator_audit_markdown=paths["operator_md"],
                decision_sheet_json=paths["decision_json"],
                base_dir=root,
                generated_at="2026-07-12T02:01:00+00:00",
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["issue_count"], 0)
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["checks"]["next_actions_open_first_count"], 1)
        self.assertTrue(
            report["checks"]["next_actions_source_hash_checks"]["support"]["hash_matches"]
        )
        self.assertTrue(
            report["checks"]["handoff_blocker_reduction_sprint_queue_hash_checks"]["json"][
                "hash_matches"
            ]
        )

    def test_lineage_sync_audit_flags_mismatches_and_missing_open_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            paths["next_md"].write_text(
                "# AI-HR Operator Next Actions\n\n"
                "- generated_at: `2026-07-12T03:00:00+00:00`\n",
                encoding="utf-8",
            )
            next_payload = json.loads(paths["next_json"].read_text(encoding="utf-8"))
            next_payload["actions"][0]["open_first"] = "reports/missing.csv"
            next_payload["source_hashes"]["support"] = "sha256:" + "0" * 64
            self._write_json(paths["next_json"], next_payload)
            paths["queue_json"].write_text('{"changed": true}\n', encoding="utf-8")
            self._write_json(
                paths["decision_json"],
                {
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "created_at": "2026-07-12T02:00:00+00:00",
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
            )

            report = build_lineage_sync_audit(
                next_actions_json=paths["next_json"],
                next_actions_markdown=paths["next_md"],
                handoff_json=paths["handoff_json"],
                operator_audit_json=paths["operator_json"],
                operator_audit_markdown=paths["operator_md"],
                decision_sheet_json=paths["decision_json"],
                base_dir=root,
                generated_at="2026-07-12T02:01:00+00:00",
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("next_actions_json_md_generated_at_mismatch", codes)
        self.assertIn("handoff_next_actions_json_hash_stale", codes)
        self.assertIn("handoff_next_actions_markdown_hash_stale", codes)
        self.assertIn("decision_sheet_generated_at_missing_or_mismatched", codes)
        self.assertIn("decision_sheet_content_hash_missing", codes)
        self.assertIn("next_actions_open_first_missing_or_empty", codes)
        self.assertIn("next_actions_source_hash_stale", codes)
        self.assertIn("handoff_blocker_reduction_sprint_queue_json_hash_stale", codes)

    def test_markdown_writer_preserves_report_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            markdown = root / "lineage.md"
            report = {
                "schema": "operator_report_lineage_sync_audit_v1",
                "generated_at": "2026-07-12T02:01:00+00:00",
                "ok": True,
                "issue_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "issues": [],
                "checks": {
                    "next_actions_json_md_generated_at": {
                        "json": "2026-07-12T02:00:00+00:00",
                        "markdown": "2026-07-12T02:00:00+00:00",
                    },
                    "next_actions_hashes": {
                        "json": "sha256:" + "a" * 64,
                        "markdown": "sha256:" + "b" * 64,
                    },
                    "decision_sheet_generated_at": "2026-07-12T02:00:00+00:00",
                    "decision_sheet_content_sha256_excluding_self_hash": "sha256:" + "c" * 64,
                },
            }

            write_markdown(markdown, report)
            text = markdown.read_text(encoding="utf-8")

        self.assertIn("operator_report_lineage_sync_audit_v1", text)
        self.assertIn("status_update_allowed: `False`", text)
        self.assertIn("db_writes: `False`", text)
        self.assertIn("approval_claim: `False`", text)
        self.assertIn("No lineage sync issues found.", text)


if __name__ == "__main__":
    unittest.main()
