from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.build_aihr_agent_queue_acceptance_closure import (  # noqa: E402
    build_acceptance_closure,
    main as closure_main,
    sha256_file,
)


class AihrAgentQueueAcceptanceClosureTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _safe_report(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
        }
        payload.update(extra)
        return payload

    def _queue_run(self, *, machine_gap: bool = False, artifact_gap: bool = False) -> dict[str, Any]:
        return self._safe_report(
            schema="aihr_agent_queue_run_v1",
            summary={
                "selected_count": 1,
                "succeeded_count": 1,
                "failed_count": 0,
                "selected_item_ids": ["aihr-01"],
                "acceptance_unverified_count": 1,
                "acceptance_unverified_declared_check_count": 1,
                "acceptance_manual_unverified_declared_check_count": 1,
                "acceptance_machine_unverified_declared_check_count": 0,
                "acceptance_machine_contract_manual_handoff_pending_count": 1,
            },
            runs=[
                {
                    "id": "aihr-01",
                    "owner": "operator-support",
                    "status": "succeeded",
                    "exit_code": 0,
                    "command": "python scripts\\ncs_harness.py fixture --out reports/aihr-01.json",
                    "expected_artifacts": ["reports/aihr-01.json"],
                    "expected_artifact_checks": [
                        {
                            "path": "reports/aihr-01.json",
                            "exists": not artifact_gap,
                            "non_empty": not artifact_gap,
                        }
                    ],
                    "acceptance_check_results": [
                        {"check": "command completed", "ok": True},
                        {
                            "check": "machine contract verified",
                            "ok": True,
                            "machine_contract": True,
                            "machine_contract_id": "fixture_contract_v1",
                        },
                    ],
                    "manual_unverified_declared_acceptance_checks": [
                        "Record commands run and touched files in the handoff."
                    ],
                    "machine_unverified_declared_acceptance_checks": (
                        ["Regenerate source hash audit."] if machine_gap else []
                    ),
                    "acceptance_verified": False,
                    "acceptance_verification_status": (
                        "machine_contract_verified_manual_handoff_pending"
                    ),
                }
            ],
        )

    def _fixture(self, root: Path, *, machine_gap: bool = False) -> dict[str, Path]:
        reports = root / "reports"
        paths = {
            "queue_run": reports / "aihr_agent_queue_run_fixture.json",
            "operator_handoff": reports / "overnight_10h_operator_handoff_fixture.json",
            "integrity": reports / "operator_review_packet_integrity_audit_fixture.json",
            "lineage": reports / "operator_report_lineage_sync_audit_fixture.json",
            "next_actions": reports / "aihr_operator_next_actions_fixture.json",
        }
        self._write_json(paths["queue_run"], self._queue_run(machine_gap=machine_gap))
        self._write_json(
            paths["operator_handoff"],
            self._safe_report(
                schema="overnight_10h_operator_handoff_v3",
                queue_state={
                    "queue_run_summary": {
                        "selected_item_ids": ["aihr-01"],
                    }
                },
                canonical_artifacts=[
                    {
                        "path": str(paths["queue_run"].relative_to(root)),
                        "sha256": sha256_file(paths["queue_run"]),
                    }
                ],
            ),
        )
        self._write_json(
            paths["integrity"],
            self._safe_report(
                schema="operator_review_packet_integrity_audit_v1",
                ok=True,
                issue_count=0,
            ),
        )
        self._write_json(
            paths["lineage"],
            self._safe_report(
                schema="operator_report_lineage_sync_audit_v1",
                ok=True,
                issue_count=0,
            ),
        )
        self._write_json(
            paths["next_actions"],
            self._safe_report(
                schema="aihr_operator_next_actions_v1",
                operator_packet_integrity_ok=True,
                operator_packet_integrity_issue_count=0,
                source_paths={"queue_run": str(paths["queue_run"].relative_to(root))},
                source_hashes={"queue_run": sha256_file(paths["queue_run"])},
            ),
        )
        return paths

    def test_happy_path_closes_machine_evidence_without_claiming_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_acceptance_closure(
                queue_run_path=paths["queue_run"],
                operator_handoff_path=paths["operator_handoff"],
                operator_integrity_audit_path=paths["integrity"],
                lineage_audit_path=paths["lineage"],
                operator_next_actions_path=paths["next_actions"],
                root=root,
            )

        summary = report["closure_summary"]
        self.assertTrue(report["ok"])
        self.assertTrue(summary["machine_closure_ok"])
        self.assertEqual(1, summary["remaining_manual_handoff_pending_count"])
        self.assertEqual(1, summary["manual_handoff_evidence_recorded_count"])
        self.assertEqual(0, summary["manual_handoff_evidence_missing_count"])
        self.assertTrue(summary["operator_handoff_queue_run_hash_ok"])
        self.assertTrue(summary["operator_next_actions_queue_run_hash_ok"])
        self.assertEqual(
            "machine_evidence_closed_manual_handoff_review_required",
            summary["closure_status"],
        )
        evidence = report["runs"][0]["manual_handoff_evidence"]
        self.assertTrue(evidence["command_recorded"])
        self.assertTrue(evidence["evidence_recorded"])
        self.assertEqual(["reports/aihr-01.json"], evidence["generated_artifacts"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["acceptance_claim"])
        self.assertFalse(summary["acceptance_verified_by_this_report"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertTrue(report["human_decision_required"])

    def test_machine_unverified_acceptance_check_keeps_closure_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root, machine_gap=True)
            report = build_acceptance_closure(
                queue_run_path=paths["queue_run"],
                operator_handoff_path=paths["operator_handoff"],
                operator_integrity_audit_path=paths["integrity"],
                lineage_audit_path=paths["lineage"],
                operator_next_actions_path=paths["next_actions"],
                root=root,
            )

        summary = report["closure_summary"]
        self.assertFalse(report["ok"])
        self.assertFalse(summary["machine_closure_ok"])
        self.assertEqual(1, summary["remaining_machine_unverified_declared_check_count"])
        self.assertEqual("machine_evidence_incomplete", summary["closure_status"])

    def test_handoff_queue_run_hash_mismatch_keeps_closure_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            handoff = json.loads(paths["operator_handoff"].read_text(encoding="utf-8"))
            handoff["canonical_artifacts"][0]["sha256"] = "sha256:" + "0" * 64
            self._write_json(paths["operator_handoff"], handoff)
            report = build_acceptance_closure(
                queue_run_path=paths["queue_run"],
                operator_handoff_path=paths["operator_handoff"],
                operator_integrity_audit_path=paths["integrity"],
                lineage_audit_path=paths["lineage"],
                operator_next_actions_path=paths["next_actions"],
                root=root,
            )

        summary = report["closure_summary"]
        source_check = report["supporting_source_hash_checks"]["operator_handoff_queue_run"]
        self.assertFalse(report["ok"])
        self.assertTrue(summary["machine_closure_ok"])
        self.assertFalse(summary["operator_handoff_queue_run_hash_ok"])
        self.assertFalse(source_check["hash_matches"])
        self.assertEqual("machine_evidence_incomplete", summary["closure_status"])

    def test_next_actions_queue_run_hash_mismatch_keeps_closure_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            next_actions = json.loads(paths["next_actions"].read_text(encoding="utf-8"))
            next_actions["source_hashes"]["queue_run"] = "sha256:" + "0" * 64
            self._write_json(paths["next_actions"], next_actions)
            report = build_acceptance_closure(
                queue_run_path=paths["queue_run"],
                operator_handoff_path=paths["operator_handoff"],
                operator_integrity_audit_path=paths["integrity"],
                lineage_audit_path=paths["lineage"],
                operator_next_actions_path=paths["next_actions"],
                root=root,
            )

        summary = report["closure_summary"]
        source_check = report["supporting_source_hash_checks"]["operator_next_actions_queue_run"]
        self.assertFalse(report["ok"])
        self.assertTrue(summary["machine_closure_ok"])
        self.assertFalse(summary["operator_next_actions_queue_run_hash_ok"])
        self.assertFalse(source_check["hash_matches"])
        self.assertEqual("machine_evidence_incomplete", summary["closure_status"])

    def test_cli_writes_json_and_markdown_in_strict_happy_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "acceptance_closure.json"
            md = root / "reports" / "acceptance_closure.md"
            argv = [
                "--root",
                str(root),
                "--queue-run",
                str(paths["queue_run"].relative_to(root)),
                "--operator-handoff",
                str(paths["operator_handoff"].relative_to(root)),
                "--operator-integrity-audit",
                str(paths["integrity"].relative_to(root)),
                "--lineage-audit",
                str(paths["lineage"].relative_to(root)),
                "--operator-next-actions",
                str(paths["next_actions"].relative_to(root)),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = closure_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Agent Queue Acceptance Closure", markdown)
        self.assertIn("acceptance_claim: `False`", markdown)


if __name__ == "__main__":
    unittest.main()
