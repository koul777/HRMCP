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

from scripts.build_aihr_post_handoff_validation import (  # noqa: E402
    build_validation,
    main as post_handoff_main,
)


class AihrPostHandoffValidationTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _safe_payload(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
        }
        payload.update(extra)
        return payload

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        paths = {
            "handoff": reports / "overnight_10h_operator_handoff_fixture.json",
            "dag": reports / "aihr_release_operator_refresh_dag_fixture.json",
            "dag_audit": reports / "aihr_release_operator_refresh_dag_audit_fixture.json",
            "closure": reports / "aihr_agent_queue_acceptance_closure_fixture.json",
            "powershell": reports / "operator_json_powershell_compatibility_audit_fixture.json",
            "readability": reports / "review_artifact_readability_operator_fixture.json",
            "integrity": reports / "operator_review_packet_integrity_audit_fixture.json",
            "lineage": reports / "operator_report_lineage_sync_audit_fixture.json",
            "crosswalk": reports / "transition_provenance_operator_crosswalk_audit_fixture.json",
        }
        self._write_json(
            paths["handoff"],
            self._safe_payload(
                schema="overnight_10h_operator_handoff_v3",
                forbidden_automatic_statuses=["human_reviewed", "accepted", "reviewed"],
            ),
        )
        self._write_json(
            paths["dag"],
            self._safe_payload(schema="aihr_release_operator_refresh_dag_v1"),
        )
        self._write_json(
            paths["dag_audit"],
            self._safe_payload(schema="aihr_release_operator_refresh_dag_audit_v1", issue_count=0),
        )
        self._write_json(
            paths["closure"],
            self._safe_payload(
                schema="aihr_agent_queue_acceptance_closure_v1",
                acceptance_claim=False,
                closure_summary={
                    "closure_status": "machine_evidence_closed_manual_handoff_review_required",
                    "remaining_manual_handoff_pending_count": 3,
                    "acceptance_verified_by_this_report": False,
                },
            ),
        )
        self._write_json(
            paths["powershell"],
            self._safe_payload(
                schema="operator_json_powershell_compatibility_audit_v1",
                status="pass",
                finding_count=0,
                artifact_count=13,
                python_ok_powershell_failed_count=0,
            ),
        )
        self._write_json(
            paths["readability"],
            self._safe_payload(
                schema="review_artifact_readability_audit_v1",
                status="pass",
                finding_count=0,
                artifact_count=22,
            ),
        )
        self._write_json(
            paths["integrity"],
            self._safe_payload(
                schema="operator_review_packet_integrity_audit_v2",
                issue_count=0,
                warning_count=1,
                warnings=[
                    {
                        "code": "transition_crosswalk_audit_warnings_present",
                        "warning_count": 11,
                    }
                ],
            ),
        )
        self._write_json(
            paths["lineage"],
            self._safe_payload(schema="operator_report_lineage_sync_audit_v1", issue_count=0),
        )
        self._write_json(
            paths["crosswalk"],
            self._safe_payload(
                schema="transition_provenance_operator_crosswalk_audit_v1",
                issue_count=0,
                warning_count=11,
                legacy_gap_recommended_packet_missing_is_non_blocking_when_primary_exists=True,
                warnings=[
                    {
                        "code": "legacy_gap_recommended_packet_artifact_missing",
                        "scenario_id": "3",
                    }
                ],
            ),
        )
        return paths

    def _build(self, paths: dict[str, Path], root: Path) -> dict[str, Any]:
        return build_validation(
            handoff_path=paths["handoff"],
            release_dag_path=paths["dag"],
            release_dag_audit_path=paths["dag_audit"],
            acceptance_closure_path=paths["closure"],
            powershell_compatibility_path=paths["powershell"],
            readability_audit_path=paths["readability"],
            operator_integrity_audit_path=paths["integrity"],
            lineage_audit_path=paths["lineage"],
            crosswalk_audit_path=paths["crosswalk"],
            root=root,
            generated_at="2026-07-12T14:10:00+00:00",
        )

    def test_validation_passes_as_terminal_evidence_with_nonblocking_crosswalk_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = self._build(paths, root)

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["terminal_evidence_only"])
        self.assertFalse(report["include_in_release_refresh_dag"])
        self.assertFalse(report["include_in_operator_handoff"])
        self.assertTrue(report["source_hash_cycle_policy"]["must_not_be_source_for_handoff_or_refresh_dag"])
        self.assertEqual(0, report["summary"]["issue_count"])
        self.assertEqual(2, report["summary"]["warning_count"])
        self.assertEqual(
            ["legacy_gap_recommended_packet_artifact_missing"],
            report["summary"]["crosswalk_warning_codes"],
        )
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["acceptance_claim"])
        self.assertFalse(report["db_writes"])
        self.assertEqual(
            set(report["source_paths"]),
            {
                "operator_handoff",
                "release_operator_refresh_dag",
                "release_operator_refresh_dag_audit",
                "agent_queue_acceptance_closure",
                "operator_json_powershell_compatibility_audit",
                "operator_primary_packet_readability_audit",
                "operator_packet_integrity_audit",
                "operator_report_lineage_sync_audit",
                "transition_provenance_crosswalk_audit",
            },
        )
        self.assertNotIn("out", report["source_paths"])
        self.assertTrue(all(check["hash_matches"] for check in report["source_hash_checks"].values()))
        self.assertTrue(
            all(
                check["lineage_validation"] is False
                for check in report["source_hash_checks"].values()
            )
        )
        self.assertFalse(report["source_hash_check_scope"]["lineage_validation"])
        self.assertTrue(
            report["source_contracts"]["operator_handoff"]["acceptance_claim_is_false_or_absent"]
        )

    def test_disallowed_crosswalk_warning_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            crosswalk = json.loads(paths["crosswalk"].read_text(encoding="utf-8"))
            crosswalk["warnings"].append({"code": "unexpected_warning"})
            self._write_json(paths["crosswalk"], crosswalk)
            report = self._build(paths, root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("crosswalk_audit_has_disallowed_warning_codes", codes)

    def test_powershell_failure_fails_validation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            powershell = json.loads(paths["powershell"].read_text(encoding="utf-8"))
            powershell["ok"] = False
            powershell["finding_count"] = 1
            powershell["python_ok_powershell_failed_count"] = 1
            self._write_json(paths["powershell"], powershell)
            report = self._build(paths, root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("operator_json_powershell_compatibility_not_clean", codes)
        self.assertIn("python_ok_powershell_failed", codes)

    def test_unsafe_acceptance_claim_and_post_handoff_dag_node_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            closure = json.loads(paths["closure"].read_text(encoding="utf-8"))
            closure["acceptance_claim"] = True
            self._write_json(paths["closure"], closure)
            dag = json.loads(paths["dag"].read_text(encoding="utf-8"))
            dag["nodes"] = [{"id": "post_handoff_validation"}]
            self._write_json(paths["dag"], dag)
            report = self._build(paths, root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_source_contract", codes)
        self.assertIn("post_handoff_in_release_dag", codes)

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "post_handoff.json"
            md = root / "reports" / "post_handoff.md"
            argv = [
                "--root",
                str(root),
                "--handoff",
                str(paths["handoff"].relative_to(root)),
                "--release-dag",
                str(paths["dag"].relative_to(root)),
                "--release-dag-audit",
                str(paths["dag_audit"].relative_to(root)),
                "--acceptance-closure",
                str(paths["closure"].relative_to(root)),
                "--powershell-compatibility",
                str(paths["powershell"].relative_to(root)),
                "--readability-audit",
                str(paths["readability"].relative_to(root)),
                "--operator-integrity-audit",
                str(paths["integrity"].relative_to(root)),
                "--lineage-audit",
                str(paths["lineage"].relative_to(root)),
                "--crosswalk-audit",
                str(paths["crosswalk"].relative_to(root)),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = post_handoff_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Post-Handoff Validation", markdown)
        self.assertIn("terminal_evidence_only: `True`", markdown)


if __name__ == "__main__":
    unittest.main()
