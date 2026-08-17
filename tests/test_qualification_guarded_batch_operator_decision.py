from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_qualification_guarded_batch_operator_decision import (
    audit_decision_packet,
    build_decision_packet,
    input_or_latest,
    write_csv,
    write_markdown,
)
from scripts.audit_qualification_guarded_batch_operator_decision import (
    build_existing_packet_audit,
)


def sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class QualificationGuardedBatchOperatorDecisionTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _fixtures(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        coverage_plan = self._write_json(
            reports / "qualification_collection_coverage_plan_20260712_10h.json",
            {
                "schema": "ncs_qualification_collection_coverage_plan_v1",
                "ok": True,
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "automatic_collection_allowed_now": False,
                "automatic_queue_execution_allowed": False,
                "operator_timed_guarded_api_commands_only": True,
                "execution_authorized": False,
                "checkpoint_path": "reports\\checkpoint_ncs006_element_api_status_20260712_10h.json",
                "batch_commands_have_ncs006_checkpoint_values": True,
                "batch_commands_checkpoint_values_match_plan": True,
                "batch_commands_unique_checkpoint_paths": [
                    "reports\\checkpoint_ncs006_element_api_status_20260712_10h.json"
                ],
                "batch_count": 4,
                "batch_size": 100,
                "unsafe_batch_count": 0,
                "current_state": {
                    "total_unit_count": 1000,
                    "attempted_unit_count": 400,
                    "unattempted_unit_count": 600,
                    "collected_unit_count": 200,
                    "empty_unit_count": 200,
                    "error_unit_count": 0,
                    "collection_coverage": 0.4,
                },
                "target_state": {
                    "target_attempted_unit_count": 900,
                    "additional_attempted_units_needed": 350,
                    "estimated_batch_count": 4,
                },
                "major_gaps": [
                    {
                        "major_code": "19",
                        "major_name": "전기전자",
                        "coverage": 0.2,
                        "unattempted_unit_count": 200,
                        "attempted_unit_count": 50,
                        "total_unit_count": 250,
                    }
                ],
                "batches": [
                    {
                        "batch_index": 1,
                        "limit_units": 100,
                        "command": (
                            "python scripts\\ncs_harness.py collect-qualification-items "
                            "--all-units --limit-units 100 --num-of-rows 50 --max-pages 1 "
                            "--request-delay 2 --max-retries 1 --retry-backoff-seconds 30 "
                            "--stop-after-rate-limit-errors 3 --ncs006-checkpoint-path "
                            "reports\\checkpoint_ncs006_element_api_status_20260712_10h.json"
                        ),
                        "execution_authorized": False,
                        "automatic_queue_execution_allowed": False,
                        "requires_operator_ticket": True,
                        "requires_explicit_operator_start": True,
                        "requires_operator_timing": True,
                    }
                ],
            },
        )
        retry_hygiene = self._write_json(
            reports / "qualification_retry_hygiene_20260712_10h.json",
            {
                "schema": "ncs_qualification_retry_hygiene_v1",
                "ok": True,
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "execution_authorized": False,
                "automatic_queue_execution_allowed": False,
                "approval_claim": False,
                "error_unit_count": 0,
                "retry_candidate_unit_count": 0,
                "retry_ready_unit_count": 0,
                "broad_retry_risk": "low",
                "api_call_allowed_now": False,
                "retry_collection_authorized": False,
                "checkpoint_path": "reports/checkpoint_ncs006_element_api_status_20260712_10h.json",
                "api_execution_guard": {"status": "allowed", "safety_violations": []},
            },
        )
        release_readiness = self._write_json(
            reports / "aihr_release_readiness_20260712_10h.json",
            {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "blockers": [
                    {
                        "category": "data_collection",
                        "name": "qualification:collection_coverage",
                        "message": "below target",
                    }
                ],
            },
        )
        queue_status = self._write_json(
            reports / "aihr_agent_queue_status_20260712_10h.json",
            {
                "schema": "aihr_agent_queue_status_v1",
                "queue_ready": True,
                "blocked_count": 0,
                "manual_ready_count": 3,
                "auto_startable_count": 3,
            },
        )
        queue_run = self._write_json(
            reports / "aihr_agent_queue_run_20260712_10h.json",
            {
                "schema": "aihr_agent_queue_run_v1",
                "actual_run": True,
                "selected_count": 3,
                "failed_count": 0,
            },
        )
        return {
            "coverage_plan": coverage_plan,
            "retry_hygiene": retry_hygiene,
            "release_readiness": release_readiness,
            "queue_status": queue_status,
            "queue_run": queue_run,
        }

    def test_build_decision_packet_preserves_guard_contract_and_source_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._fixtures(Path(tmp))
            report = build_decision_packet(
                coverage_plan_path=paths["coverage_plan"],
                retry_hygiene_path=paths["retry_hygiene"],
                release_readiness_path=paths["release_readiness"],
                queue_status_path=paths["queue_status"],
                queue_run_path=paths["queue_run"],
                generated_at="2026-07-12T00:00:00+00:00",
            )

        self.assertEqual(report["schema"], "qualification_guarded_batch_operator_decision_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["execution_authorized"])
        self.assertFalse(report["automatic_queue_execution_allowed"])
        self.assertFalse(report["approval_claim"])
        self.assertTrue(report["human_decision_required"])
        self.assertEqual(report["forbidden_automatic_statuses"], ["human_reviewed", "accepted", "reviewed"])
        self.assertEqual(report["batch_summary"]["batch_count"], 4)
        self.assertEqual([row["wave"] for row in report["wave_plan"]], ["pilot", "first_wave", "target_completion"])
        self.assertIn("collect-qualification-items", report["command_template"])
        self.assertIn("--all-units", report["command_template"])
        self.assertIn("--limit-units 100", report["command_template"])
        self.assertEqual(set(report["source_paths"]), set(report["source_hashes"]))
        self.assertTrue(all(value and value.startswith("sha256:") for value in report["source_hashes"].values()))

    def test_audit_flags_hash_drift_and_unsafe_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixtures(root)
            report = build_decision_packet(
                coverage_plan_path=paths["coverage_plan"],
                retry_hygiene_path=paths["retry_hygiene"],
                release_readiness_path=paths["release_readiness"],
                queue_status_path=paths["queue_status"],
                queue_run_path=paths["queue_run"],
            )
            audit = audit_decision_packet(report, base_dir=root)
            self.assertTrue(audit["ok"], audit["issues"])
            self.assertTrue(audit["report_only"])
            self.assertFalse(audit["status_update_allowed"])
            self.assertFalse(audit["db_writes"])
            self.assertFalse(audit["api_calls"])
            self.assertFalse(audit["approval_claim"])
            self.assertFalse(audit["acceptance_claim"])
            self.assertTrue(audit["human_decision_required"])
            report["source_hashes"]["release_readiness"] = "sha256:" + ("0" * 64)
            report["execution_authorized"] = True
            audit = audit_decision_packet(report, base_dir=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertIn("source_hash_stale", codes)
        self.assertIn("required_false_field_not_false", codes)

    def test_writers_emit_csv_and_markdown_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixtures(root)
            report = build_decision_packet(
                coverage_plan_path=paths["coverage_plan"],
                retry_hygiene_path=paths["retry_hygiene"],
                release_readiness_path=paths["release_readiness"],
                queue_status_path=paths["queue_status"],
                queue_run_path=paths["queue_run"],
            )
            csv_path = root / "decision.csv"
            md_path = root / "decision.md"
            write_csv(csv_path, report)
            write_markdown(md_path, report)
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            markdown = md_path.read_text(encoding="utf-8")

        self.assertEqual([row["wave"] for row in rows], ["pilot", "first_wave", "target_completion"])
        self.assertEqual(rows[0]["requires_operator_start"], "True")
        self.assertIn("Execution authorized: `False`", markdown)
        self.assertIn("This artifact does not authorize API execution.", markdown)

    def test_existing_packet_audit_does_not_require_or_rewrite_operator_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixtures(root)
            report = build_decision_packet(
                coverage_plan_path=paths["coverage_plan"],
                retry_hygiene_path=paths["retry_hygiene"],
                release_readiness_path=paths["release_readiness"],
                queue_status_path=paths["queue_status"],
                queue_run_path=paths["queue_run"],
            )
            packet_path = self._write_json(root / "reports" / "decision.json", report)
            csv_path = root / "reports" / "decision.csv"
            csv_path.write_text("operator_decision,reviewer_id\nrun_pilot_window,hr_lead\n", encoding="utf-8")
            before_csv = csv_path.read_text(encoding="utf-8")

            audit = build_existing_packet_audit(packet_path, root=root)
            after_csv = csv_path.read_text(encoding="utf-8")

        self.assertTrue(audit["ok"], audit["issues"])
        self.assertEqual(before_csv, after_csv)
        self.assertTrue(audit["source_packet_exists_nonempty"])
        self.assertFalse(audit["acceptance_claim"])
        self.assertTrue(audit["human_decision_required"])

    def test_input_or_latest_filters_stamp_and_resolves_relative_paths_from_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            current = self._write_json(
                reports / "qualification_retry_hygiene_20260712_10h.json",
                {"ok": True},
            )
            later_mtime_wrong_family = self._write_json(
                reports / "qualification_retry_hygiene_20260712_9h.json",
                {"ok": True},
            )
            later_mtime_wrong_family.touch()

            discovered = input_or_latest(
                None,
                "qualification_retry_hygiene_*.json",
                reports_dir=reports,
                root=root,
                stamp="20260712_10h",
            )
            explicit = input_or_latest(
                Path("reports/qualification_retry_hygiene_20260712_10h.json"),
                "qualification_retry_hygiene_*.json",
                reports_dir=reports,
                root=root,
                stamp="20260712_10h",
            )

        self.assertEqual(discovered, current)
        self.assertEqual(explicit, current)


if __name__ == "__main__":
    unittest.main()
