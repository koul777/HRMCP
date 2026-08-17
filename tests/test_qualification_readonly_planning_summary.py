from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import summarize_qualification_readonly_planning as summary


def _coverage_plan(additional_needed: int = 6743) -> dict:
    return {
        "schema": "ncs_qualification_collection_coverage_plan_v1",
        "ok": True,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "human_review_status_updates": False,
        "automatic_collection_allowed_now": False,
        "automatic_queue_execution_allowed": False,
        "approval_claim": False,
        "execution_authorized": False,
        "operator_timed_guarded_api_commands_only": True,
        "target_ratio": 0.9,
        "batch_count": 2,
        "unsafe_batch_count": 0,
        "current_state": {
            "total_unit_count": 13435,
            "attempted_unit_count": 5349,
            "unattempted_unit_count": 8086,
            "collection_coverage": 0.398139 if additional_needed else 0.9,
        },
        "target_state": {
            "additional_attempted_units_needed": additional_needed,
            "estimated_batch_count": 2 if additional_needed else 0,
        },
        "batches": [
            {
                "batch_index": 1,
                "limit_units": 100,
                "command": (
                    "python scripts\\ncs_harness.py collect-qualification-items "
                    "--all-units --limit-units 100 --num-of-rows 50 "
                    "--ncs006-checkpoint-path "
                    "reports\\checkpoint_ncs006_element_api_status_20260702_9h_public.json"
                ),
                "auto_runnable": False,
                "automatic_queue_execution_allowed": False,
                "requires_explicit_operator_start": True,
                "requires_operator_timing": True,
                "guard_required": True,
                "mutation_policy": "guarded_api_collection",
                "command_role": "operator_timed_guarded_api_collection",
                "execution_authorized": False,
                "do_not_execute_from_report": True,
                "not_queue_item": True,
                "requires_operator_ticket": True,
            },
            {
                "batch_index": 2,
                "limit_units": 43,
                "command": (
                    "python scripts\\ncs_harness.py collect-qualification-items "
                    "--all-units --limit-units 43 --num-of-rows 50 "
                    "--ncs006-checkpoint-path "
                    "reports\\checkpoint_ncs006_element_api_status_20260702_9h_public.json"
                ),
                "auto_runnable": False,
                "automatic_queue_execution_allowed": False,
                "requires_explicit_operator_start": True,
                "requires_operator_timing": True,
                "guard_required": True,
                "mutation_policy": "guarded_api_collection",
                "command_role": "operator_timed_guarded_api_collection",
                "execution_authorized": False,
                "do_not_execute_from_report": True,
                "not_queue_item": True,
                "requires_operator_ticket": True,
            },
        ]
        if additional_needed
        else [],
        "guard_policy": {
            "must_run_qualification_retry_hygiene_first": True,
            "must_use_ncs006_checkpoint_path": True,
            "must_not_write_human_review_statuses": True,
            "operator_must_confirm_api_timing": True,
            "batch_commands_are_operator_timed": True,
            "batch_commands_are_not_queue_items": True,
            "operator_timing_required": True,
            "automatic_queue_execution_allowed": False,
            "forbidden_status_updates": ["human_reviewed", "accepted", "reviewed"],
        },
    }


class QualificationReadonlyPlanningSummaryTests(unittest.TestCase):
    def test_summary_separates_retry_hygiene_from_coverage_collection_status(self) -> None:
        report = summary.build_summary(
            coverage_plan=_coverage_plan(),
            retry_hygiene={
                "next_safe_action_status": "complete_no_collection_needed",
                "retry_candidate_unit_count": 0,
                "qualification_retry_allowed_now": True,
                "blocked_by_checkpoint": False,
            },
        )

        self.assertTrue(report["coverage_gap_open"])
        self.assertTrue(report["artifact_ok"])
        self.assertTrue(report["contract_ok"])
        self.assertTrue(report["read_only_contract_ok"])
        self.assertFalse(report["execution_ready"])
        self.assertEqual(report["authorization_status"], "not_authorized_read_only_report")
        self.assertEqual(report["next_safe_action_status"], "operator_timed_collection_required")
        self.assertEqual(
            report["retry_error_next_safe_action_status"],
            "complete_no_collection_needed",
        )
        self.assertFalse(report["retry_needed_now"])
        self.assertTrue(report["qualification_retry_allowed_now"])
        self.assertIn("not coverage collection authorization", report["qualification_retry_allowed_meaning"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["human_review_status_updates"])
        self.assertFalse(report["execution_authorized"])
        self.assertTrue(report["human_start_required"])
        self.assertTrue(report["operator_timed_guarded_api_commands_only"])
        self.assertTrue(report["retry_preflight_clear_only"])
        self.assertFalse(report["retry_collection_authorized"])
        self.assertIn(
            "qualification_retry_allowed_now_is_retry_preflight_only_not_collection_authorization",
            report["execution_readiness_warnings"],
        )
        self.assertIn(
            "operator_commands_present_but_execution_authorized_false",
            report["execution_readiness_warnings"],
        )
        self.assertIn("reviewed", report["forbidden_status_updates"])
        self.assertEqual(report["input_contract_issues"], [])

    def test_summary_rejects_unsafe_coverage_plan_contract(self) -> None:
        plan = _coverage_plan()
        plan["db_writes"] = True
        plan["guard_policy"]["forbidden_status_updates"] = ["human_reviewed"]
        plan["batches"][0]["not_queue_item"] = False

        report = summary.build_summary(coverage_plan=plan)

        self.assertFalse(report["contract_ok"])
        self.assertIn("db_writes", report["input_contract_issues"])
        self.assertIn(
            "guard_policy.forbidden_status_updates",
            report["input_contract_issues"],
        )
        self.assertIn("batch[1].not_queue_item", report["input_contract_issues"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["status_update_allowed"])

    def test_summary_rejects_missing_readonly_safety_flags(self) -> None:
        plan = _coverage_plan()
        del plan["db_writes"]
        del plan["approval_claim"]
        del plan["execution_authorized"]
        del plan["human_review_status_updates"]
        del plan["operator_timed_guarded_api_commands_only"]

        report = summary.build_summary(coverage_plan=plan)

        self.assertFalse(report["contract_ok"])
        self.assertIn("missing:db_writes", report["input_contract_issues"])
        self.assertIn("missing:approval_claim", report["input_contract_issues"])
        self.assertIn("missing:execution_authorized", report["input_contract_issues"])
        self.assertIn("missing:human_review_status_updates", report["input_contract_issues"])
        self.assertIn(
            "missing:operator_timed_guarded_api_commands_only",
            report["input_contract_issues"],
        )

    def test_summary_rejects_missing_operator_guard_fields(self) -> None:
        plan = _coverage_plan()
        del plan["guard_policy"]["must_use_ncs006_checkpoint_path"]
        del plan["guard_policy"]["operator_must_confirm_api_timing"]
        plan["batches"][0].pop("requires_operator_timing")
        plan["batches"][1]["mutation_policy"] = "regenerate_reports_only"

        report = summary.build_summary(coverage_plan=plan)

        self.assertFalse(report["contract_ok"])
        self.assertIn(
            "guard_policy.must_use_ncs006_checkpoint_path",
            report["input_contract_issues"],
        )
        self.assertIn(
            "guard_policy.operator_must_confirm_api_timing",
            report["input_contract_issues"],
        )
        self.assertIn("batch[1].requires_operator_timing", report["input_contract_issues"])
        self.assertIn("batch[2].mutation_policy", report["input_contract_issues"])

    def test_summary_allows_no_collection_status_only_when_target_is_met(self) -> None:
        report = summary.build_summary(coverage_plan=_coverage_plan(additional_needed=0))

        self.assertFalse(report["coverage_gap_open"])
        self.assertEqual(
            report["next_safe_action_status"],
            "target_coverage_met_no_collection_needed",
        )

    def test_timing_schedule_uses_current_checkpoint_and_stays_operator_only(self) -> None:
        report = summary.build_timing_schedule(coverage_plan=_coverage_plan(), wave_size=2)

        self.assertEqual(report["wave_count"], 1)
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["execution_authorized"])
        self.assertFalse(report["execution_ready"])
        self.assertEqual(report["authorization_status"], "not_authorized_read_only_report")
        self.assertFalse(report["automatic_queue_execution_allowed"])
        self.assertTrue(report["do_not_execute_from_report"])
        self.assertTrue(report["not_queue_item"])
        self.assertTrue(report["requires_operator_ticket"])
        self.assertTrue(report["human_start_required"])
        self.assertEqual(
            report["checkpoint_path"],
            "reports\\checkpoint_ncs006_element_api_status_20260702_9h_public.json",
        )
        self.assertNotIn("20260630", "\n".join(report["waves"][0]["operator_commands"]))
        self.assertFalse(report["waves"][0]["auto_runnable"])
        self.assertFalse(report["waves"][0]["execution_authorized"])
        self.assertFalse(report["waves"][0]["operator_command_authorized"])
        self.assertTrue(report["waves"][0]["do_not_execute_from_report"])
        self.assertTrue(report["waves"][0]["not_queue_item"])
        self.assertTrue(report["waves"][0]["requires_operator_ticket"])
        self.assertTrue(report["waves"][0]["human_start_required"])
        self.assertEqual(
            report["waves"][0]["operator_command_template"],
            report["waves"][0]["operator_command"],
        )

    def test_cli_writes_summary_but_not_timing_when_input_contract_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plan = _coverage_plan()
            del plan["operator_timed_guarded_api_commands_only"]
            coverage_path = tmp_path / "coverage.json"
            summary_path = tmp_path / "summary.json"
            summary_md_path = tmp_path / "summary.md"
            timing_path = tmp_path / "timing.json"
            timing_md_path = tmp_path / "timing.md"
            coverage_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

            with mock.patch(
                "sys.argv",
                [
                    "summarize_qualification_readonly_planning.py",
                    "--coverage-plan",
                    str(coverage_path),
                    "--summary-out",
                    str(summary_path),
                    "--summary-markdown-out",
                    str(summary_md_path),
                    "--timing-out",
                    str(timing_path),
                    "--timing-markdown-out",
                    str(timing_md_path),
                ],
            ):
                exit_code = summary.main()

            self.assertEqual(exit_code, 2)
            self.assertTrue(summary_path.is_file())
            self.assertTrue(summary_md_path.is_file())
            self.assertFalse(timing_path.exists())
            self.assertFalse(timing_md_path.exists())


if __name__ == "__main__":
    unittest.main()
