from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
import os
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_harness import (
    explicit_collection_major_codes,
    has_task_locator,
    lint_repo,
    main,
    run_smoke_check,
    task_locator_error_payload,
    training_transition_eval_set_payload,
    transition_review_status_filter,
    write_training_transition_evaluation_report,
    write_training_transition_review_report,
)


class HarnessTests(unittest.TestCase):
    def test_lint_repo_returns_result_shape(self) -> None:
        result = lint_repo(strict=False)
        self.assertIn("ok", result)
        self.assertIn("issues", result)
        self.assertIsInstance(result["issues"], list)

    def test_collection_scope_requires_explicit_major_or_all_majors(self) -> None:
        with self.assertRaisesRegex(ValueError, "--all-majors or --major-code"):
            explicit_collection_major_codes(
                None,
                major_code=None,
                all_majors=False,
                command_name="collect-training-courses",
            )

        self.assertEqual(
            explicit_collection_major_codes(
                None,
                major_code="03",
                all_majors=False,
                command_name="collect-training-courses",
            ),
            ["03"],
        )

    def test_transition_review_status_filter_supports_trusted_and_csv_values(self) -> None:
        self.assertEqual(
            transition_review_status_filter(trusted_only=False, review_statuses=["candidate,candidate_auto"]),
            ["candidate", "candidate_auto"],
        )
        self.assertIn(
            "accepted",
            transition_review_status_filter(trusted_only=True, review_statuses=["candidate"]) or [],
        )

    def test_transition_review_status_filter_rejects_unknown_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported transition review status"):
            transition_review_status_filter(
                trusted_only=False,
                review_statuses=["candidate,typo_status"],
            )

    def test_task_locator_error_payload_is_localized_and_actionable(self) -> None:
        self.assertFalse(has_task_locator(criteria_id=None, query=" ", unit_code=None))
        self.assertTrue(has_task_locator(criteria_id=1, query=None, unit_code=None))
        self.assertTrue(has_task_locator(criteria_id=None, query=None, unit_code="0202020101_23v3"))

        payload = task_locator_error_payload()

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "missing_task_locator")
        self.assertIn("NCS 과업 선택", payload["error"]["message"])
        self.assertGreaterEqual(len(payload["error"]["examples"]), 2)

    def test_missing_task_locator_cli_exits_nonzero(self) -> None:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        sys.argv = ["ncs_harness.py", "recommend-training-for-task"]
        try:
            with self.assertRaises(SystemExit) as raised:
                with contextlib.redirect_stdout(stdout):
                    main()
        finally:
            sys.argv = previous_argv

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "missing_task_locator")

    def test_review_triage_cli_invalid_input_exits_nonzero(self) -> None:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            priority_path = tmp_path / "review_priority.json"
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            sys.argv = [
                "ncs_harness.py",
                "review-triage",
                "--quality-report",
                str(tmp_path / "missing_quality.json"),
                "--review-priority-report",
                str(priority_path),
            ]
            try:
                with self.assertRaises(SystemExit) as raised:
                    with contextlib.redirect_stdout(stdout):
                        main()
            finally:
                sys.argv = previous_argv

        payload = json.loads(stdout.getvalue())
        self.assertEqual(raised.exception.code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_review_triage_input")

    def test_recommend_training_for_task_cli_uses_compact_transform(self) -> None:
        class DummyConn:
            def close(self) -> None:
                pass

        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        full_result = {"ok": True, "full": True}
        compact_result = {"ok": True, "view": "compact_training_task"}
        sys.argv = [
            "ncs_harness.py",
            "recommend-training-for-task",
            "--query",
            "인사기획",
            "--limit",
            "2",
            "--compact",
            "--no-save",
        ]
        try:
            with (
                patch("ncs_harness.load_settings", return_value=SimpleNamespace(db_path=Path("ncs.db"))),
                patch("ncs_harness.connect", return_value=DummyConn()),
                patch("ncs_harness.initialize_database"),
                patch("ncs_harness.recommend_training_for_task", return_value=full_result) as recommend_mock,
                patch("ncs_harness.compact_training_task_response", return_value=compact_result) as compact_mock,
                contextlib.redirect_stdout(stdout),
            ):
                main()
        finally:
            sys.argv = previous_argv

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["view"], "compact_training_task")
        recommend_mock.assert_called_once()
        compact_mock.assert_called_once_with(full_result, recommendation_limit=2)

    def test_recommend_training_transition_cli_uses_compact_transform(self) -> None:
        class DummyConn:
            def close(self) -> None:
                pass

        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        full_result = {"ok": True, "full": True}
        compact_result = {"ok": True, "view": "compact_training_transition"}
        sys.argv = [
            "ncs_harness.py",
            "recommend-training-transition",
            "--current-query",
            "노무관리",
            "--target-query",
            "인사기획",
            "--limit",
            "3",
            "--compact",
            "--no-save",
        ]
        try:
            with (
                patch("ncs_harness.load_settings", return_value=SimpleNamespace(db_path=Path("ncs.db"))),
                patch("ncs_harness.connect", return_value=DummyConn()),
                patch("ncs_harness.initialize_database"),
                patch("ncs_harness.recommend_training_transition", return_value=full_result) as recommend_mock,
                patch("ncs_harness.compact_training_transition_response", return_value=compact_result) as compact_mock,
                contextlib.redirect_stdout(stdout),
            ):
                main()
        finally:
            sys.argv = previous_argv

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["view"], "compact_training_transition")
        recommend_mock.assert_called_once()
        compact_mock.assert_called_once_with(full_result, recommendation_limit=3)

    def test_transition_evaluation_report_keeps_trusted_and_candidate_cases_separate(self) -> None:
        def evaluation(name: str, review_status: str) -> dict:
            return {
                "ok": True,
                "review_status_filter": [review_status],
                "scenario_count": 1,
                "current_scope_accuracy": 1.0,
                "target_scope_accuracy": 1.0,
                "expected_course_recall_at_k": 0.0,
                "precision_at_k": 0.0,
                "top1_expected_hit_rate": 0.0,
                "mrr_at_k": 0.0,
                "map_at_k": 0.0,
                "ndcg_at_k": 0.0,
                "expected_course_hit_count": 0,
                "expected_course_total": 1,
                "breakdown": {},
                "cases": [
                    {
                        "scenario_name": name,
                        "ok": True,
                        "current_match": "current",
                        "target_match": "target",
                        "expected_recall_at_k": 0.0,
                        "precision_at_k": 0.0,
                        "first_expected_rank": None,
                        "reciprocal_rank": 0.0,
                        "ndcg_at_k": 0.0,
                        "expected_courses": ["expected"],
                        "recommended_courses": ["other"],
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as tmp:
            report_path = Path(tmp) / "transition_eval.md"
            write_training_transition_evaluation_report(
                report_path,
                generation=None,
                evaluations={
                    "trusted_reviewed": evaluation("trusted_case", "reviewed"),
                    "candidate_or_auto": evaluation("candidate_case", "candidate_auto"),
                },
            )
            text = report_path.read_text(encoding="utf-8")

        trusted_low_recall = text.split("## Low Recall Cases: trusted_reviewed", 1)[1].split(
            "## Low Precision Or Ranking Cases: trusted_reviewed",
            1,
        )[0]
        candidate_low_recall = text.split("## Low Recall Cases: candidate_or_auto", 1)[1].split(
            "## Low Precision Or Ranking Cases: candidate_or_auto",
            1,
        )[0]
        self.assertIn("trusted_case", trusted_low_recall)
        self.assertNotIn("candidate_case", trusted_low_recall)
        self.assertIn("candidate_case", candidate_low_recall)
        self.assertNotIn("trusted_case", candidate_low_recall)

    def test_transition_eval_set_payload_keeps_machine_readable_contract(self) -> None:
        def evaluation(review_status: str) -> dict:
            return {
                "ok": True,
                "review_status_filter": [review_status],
                "scenario_count": 1,
                "cases": [{"scenario_name": "case"}],
            }

        payload = training_transition_eval_set_payload(
            generation={"ok": True, "inserted": 1},
            evaluation=evaluation("candidate"),
            trusted_evaluation=evaluation("reviewed"),
            candidate_evaluation=evaluation("candidate_auto"),
            report_path=Path("reports/transition_eval.md"),
        )

        self.assertTrue(payload["ok"])
        self.assertIn("all_non_rejected_evaluation", payload)
        self.assertNotIn("evaluation", payload)
        self.assertEqual(
            set(payload["evaluations"]),
            {"all_non_rejected", "trusted_reviewed", "candidate_or_auto"},
        )
        self.assertNotIn("cases", payload["all_non_rejected_evaluation"])
        self.assertNotIn("cases", payload["evaluations"]["trusted_reviewed"])
        self.assertEqual(payload["report_path"], str(Path("reports/transition_eval.md")))

    def test_write_transition_scenario_review_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scenario_review.md"
            write_training_transition_review_report(
                out,
                {
                    "apply": False,
                    "review_method": "automated_eval_gate",
                    "source_review_statuses": ["candidate"],
                    "target_review_status": "reviewed",
                    "evaluated_count": 1,
                    "eligible_count": 1,
                    "updated_count": 0,
                    "criteria": {"require_top1_expected_hit": True},
                    "cases": [
                        {
                            "scenario_id": 1,
                            "scenario_name": "case_one",
                            "source_review_status": "candidate",
                            "eligible": True,
                            "blockers": [],
                            "current_scope_hit": True,
                            "target_scope_hit": True,
                            "top1_expected_hit": True,
                            "precision_at_k": 1.0,
                            "expected_recall_at_k": 1.0,
                            "first_expected_rank": 1,
                            "expected_course_hits": ["HR planning"],
                            "recommended_courses": ["HR planning"],
                        }
                    ],
                },
            )
            text = out.read_text(encoding="utf-8")

        self.assertIn("automated_eval_gate", text)
        self.assertIn("case_one", text)
        self.assertIn("eligible: True", text)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_smoke_check_uses_hr_classification_codes(self) -> None:
        previous = os.environ.get("NCS_DB_PATH")
        os.environ["NCS_DB_PATH"] = str(ROOT / "data" / "processed" / "ncs.db")
        try:
            result = run_smoke_check("02", "02", "02", "01")
        finally:
            if previous is None:
                os.environ.pop("NCS_DB_PATH", None)
            else:
                os.environ["NCS_DB_PATH"] = previous
        self.assertEqual(result["classification"]["major_code"], "02")
        self.assertGreaterEqual(result["unit_count"], 1)
        self.assertGreaterEqual(result["sample_elements"], 1)
        self.assertGreaterEqual(result["sample_criteria"], 1)
        self.assertGreaterEqual(result["sample_ksa"], 1)


if __name__ == "__main__":
    unittest.main()
