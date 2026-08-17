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

from scripts.build_aihr_release_blocker_operator_addendum import (  # noqa: E402
    build_addendum,
    main as addendum_main,
)


class AihrReleaseBlockerOperatorAddendumTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _safe(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "acceptance_claim": False,
            "human_decision_required": True,
        }
        payload.update(extra)
        return payload

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        blockers = [
            {
                "name": "review_debt:human_reviewed_concepts",
                "category": "human_review",
                "status": "open",
                "evidence": {"current_count": 0, "required_threshold": "> 0"},
                "next_safe_action": "export-ontology-definition-seedpack",
            },
            {
                "name": "human_review:provenance_reconfirmation_required",
                "category": "human_review",
                "status": "open",
                "evidence": {"current_count": 34, "required_threshold": "0 unresolved"},
                "next_safe_action": "export-human-review-provenance-reconfirmation-proofset",
            },
            {
                "name": "transition_eval:trusted_scenarios",
                "category": "evaluation",
                "status": "open",
                "evidence": {"current_count": 0, "required_threshold": ">= 10"},
            },
            {
                "name": "qualification:collection_coverage",
                "category": "data_collection",
                "status": "guarded_manual_ready",
                "evidence": {
                    "coverage": 0.39,
                    "target": 0.9,
                    "operator_timing_required": True,
                    "guarded_collection_required": True,
                    "automatic_collection_allowed_now": False,
                },
                "next_safe_action": "plan_guarded_qualification_collection_for_unattempted_units",
            },
        ]
        release = self._write_json(
            reports / "aihr_release_readiness_demo.json",
            self._safe(
                schema="aihr_release_readiness_v1",
                ok=True,
                release_ready=False,
                blocker_count=4,
                blockers=blockers,
            ),
        )
        remaining = self._write_json(
            reports / "remaining_blockers_demo.json",
            self._safe(
                schema="aihr_remaining_blockers_v1",
                remaining_blockers=blockers,
            ),
        )
        goal = self._write_json(
            reports / "goal_completion_audit_demo.json",
            self._safe(
                schema="aihr_goal_completion_audit_v1",
                release_ready=False,
                objective="demo",
                open_requirement_count=2,
                verified_requirement_count=3,
            ),
        )
        workbench = self._write_json(
            reports / "aihr_operator_decision_workbench_demo.json",
            self._safe(
                schema="aihr_operator_decision_workbench_v1",
                ok=True,
                status="pass",
                summary={"sprint_count": 3, "workbench_row_count": 23},
                sprints=[
                    {
                        "rank": 1,
                        "sprint_id": "S1-transition-provenance",
                        "blocker": "transition_eval:trusted_scenarios + human_review:provenance_reconfirmation_required",
                        "open_first": "reports/crosswalk.csv",
                        "row_selector": "transition gap",
                        "declared_row_count": 10,
                        "selected_row_count": 10,
                        "scope_match_ok": True,
                        "missing_expected_first_row_ids": [],
                        "required_human_fields": [
                            "decision",
                            "rationale",
                            "reviewer_id",
                            "reviewed_at",
                            "source_decision_packet",
                            "evidence_refs_json",
                        ],
                        "decision_options": ["reconfirm", "downgrade_to_review_required", "defer"],
                        "rows": [{"source_row_key": "3"}],
                    },
                    {
                        "rank": 2,
                        "sprint_id": "S5-concepts",
                        "blocker": "review_debt:human_reviewed_concepts",
                        "open_first": "reports/concepts.csv",
                        "row_selector": "rows 1-10",
                        "declared_row_count": 10,
                        "selected_row_count": 10,
                        "scope_match_ok": True,
                        "missing_expected_first_row_ids": [],
                        "required_human_fields": [
                            "decision",
                            "reviewer_id",
                            "reviewed_at",
                            "rationale",
                        ],
                        "decision_options": [
                            "accept_concept",
                            "revise_definition",
                            "reject_concept",
                            "defer",
                        ],
                        "rows": [{"source_row_key": "1"}],
                    },
                    {
                        "rank": 3,
                        "sprint_id": "S6-qualification",
                        "blocker": "qualification:collection_coverage",
                        "open_first": "reports/qualification.csv",
                        "row_selector": "pilot wave",
                        "declared_row_count": 3,
                        "selected_row_count": 3,
                        "scope_match_ok": True,
                        "missing_expected_first_row_ids": [],
                        "required_human_fields": [
                            "operator timing approval",
                            "batch count",
                            "stop conditions",
                            "post-run verification owner",
                        ],
                        "decision_options": ["run_pilot_window", "defer", "reduce_batch_count"],
                        "rows": [{"source_row_key": "1"}],
                    },
                ],
            ),
        )
        terminal = self._write_json(
            reports / "aihr_terminal_evidence_index_demo.json",
            self._safe(
                schema="aihr_terminal_evidence_index_v1",
                ok=True,
                status="pass",
                terminal_evidence_only=True,
                include_in_release_refresh_dag=False,
                include_in_operator_handoff=False,
                summary={"artifact_count": 21, "issue_count": 0, "warning_count": 0},
            ),
        )
        return {
            "release": release,
            "remaining": remaining,
            "goal": goal,
            "workbench": workbench,
            "terminal": terminal,
        }

    def test_addendum_maps_remaining_blockers_to_operator_sprints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
                generated_at="2026-07-12T16:00:00+00:00",
            )

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["release_ready"] if "release_ready" in report else False)
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(4, report["summary"]["remaining_blocker_count"])
        self.assertEqual(4, report["summary"]["covered_remaining_blocker_count"])
        by_name = {item["name"]: item for item in report["blocker_operator_status"]}
        self.assertEqual(
            "operator_prepared_human_decision_pending",
            by_name["review_debt:human_reviewed_concepts"]["operator_readiness"],
        )
        self.assertEqual(
            "guarded_manual_ready_operator_timing_required",
            by_name["qualification:collection_coverage"]["operator_readiness"],
        )
        self.assertEqual(
            1,
            by_name["transition_eval:trusted_scenarios"]["operator_sprint_count"],
        )
        self.assertEqual(
            "selected_workbench_rows",
            by_name["transition_eval:trusted_scenarios"]["operator_row_count_meaning"],
        )
        self.assertEqual(
            10,
            by_name["transition_eval:trusted_scenarios"]["selected_operator_row_count"],
        )
        self.assertEqual(
            10,
            by_name["transition_eval:trusted_scenarios"]["operator_source_total_row_count"],
        )
        self.assertEqual(
            0,
            by_name["transition_eval:trusted_scenarios"][
                "operator_unselected_source_row_count"
            ],
        )
        self.assertEqual(
            "review-transition-provenance-crosswalk-human-decisions",
            by_name["transition_eval:trusted_scenarios"]["next_safe_action"],
        )
        self.assertEqual(0, report["summary"]["issue_count"])
        self.assertEqual(0, report["summary"]["warning_count"])
        self.assertTrue(report["summary"]["workbench_summary_row_count_matches_selected"])
        self.assertEqual(0, report["summary"]["operator_row_issue_count"])
        self.assertEqual(0, report["summary"]["human_decision_contract_issue_count"])
        self.assertEqual(0, report["summary"]["qualification_guard_contract_issue_count"])

    def test_selected_workbench_subset_is_warned_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            workbench["sprints"][0]["declared_row_count"] = 11
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        by_name = {item["name"]: item for item in report["blocker_operator_status"]}
        transition = by_name["transition_eval:trusted_scenarios"]
        provenance = by_name["human_review:provenance_reconfirmation_required"]
        self.assertTrue(report["ok"])
        self.assertEqual(1, report["summary"]["warning_count"])
        self.assertEqual(1, report["summary"]["workbench_selected_subset_sprint_count"])
        self.assertEqual(23, report["summary"]["workbench_selected_row_count"])
        self.assertEqual(24, report["summary"]["workbench_source_total_row_count"])
        self.assertEqual(1, report["summary"]["workbench_unselected_source_row_count"])
        self.assertEqual(10, transition["selected_operator_row_count"])
        self.assertEqual(11, transition["operator_source_total_row_count"])
        self.assertEqual(1, transition["operator_unselected_source_row_count"])
        self.assertEqual(10, provenance["selected_operator_row_count"])
        self.assertEqual(11, provenance["operator_source_total_row_count"])
        self.assertEqual(
            "operator_workbench_selected_subset",
            report["warnings"][0]["code"],
        )

    def test_workbench_summary_row_count_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            workbench["summary"]["workbench_row_count"] = 999
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("operator_workbench_summary_row_count_mismatch", codes)
        self.assertFalse(report["summary"]["workbench_summary_row_count_matches_selected"])
        self.assertEqual(23, report["summary"]["workbench_selected_row_count"])

    def test_embedded_workbench_source_hash_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            source = self._write_json(
                root / "reports" / "entrypoint_source.json",
                self._safe(schema="aihr_operator_entrypoint_manifest_v1"),
            )
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            workbench["source_hash_checks"] = {
                "entrypoint_manifest": {
                    "path": source.relative_to(root).as_posix(),
                    "expected_sha256": "sha256:" + ("0" * 64),
                    "hash_matches": True,
                }
            }
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("embedded_source_hash_mismatch", codes)
        self.assertEqual(1, report["summary"]["embedded_source_hash_mismatch_count"])
        mismatch = report["embedded_source_hash_mismatches"]["operator_decision_workbench"][0]
        self.assertEqual("current_source_hash_mismatch", mismatch["reason"])

    def test_terminal_warning_codes_are_exposed_without_failing_addendum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            terminal = json.loads(paths["terminal"].read_text(encoding="utf-8"))
            terminal["summary"]["warning_count"] = 2
            terminal["warnings"] = [
                {
                    "code": "json_reports_warnings",
                    "message": "Nested warning count.",
                    "label": "post_handoff_validation",
                    "source_warning_code_counts": [
                        {"code": "operator_packet_integrity_warnings_present", "count": 1},
                    ],
                },
                {
                    "code": "post_decision_gate_not_in_default",
                    "message": "Post-decision gate is opt-in.",
                },
            ]
            self._write_json(paths["terminal"], terminal)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        expected_codes = [
            "json_reports_warnings",
            "post_decision_gate_not_in_default",
        ]
        self.assertTrue(report["ok"])
        self.assertEqual(1, report["summary"]["warning_count"])
        self.assertEqual(expected_codes, report["summary"]["terminal_warning_codes"])
        self.assertEqual(2, report["summary"]["terminal_warning_source_count"])
        self.assertEqual(expected_codes, report["terminal_evidence"]["warning_codes"])
        self.assertEqual(
            [
                {"code": "operator_packet_integrity_warnings_present", "count": 1},
            ],
            report["terminal_evidence"]["warning_sources"][0]["source_warning_code_counts"],
        )
        warning = report["warnings"][0]
        self.assertEqual("terminal_evidence_index_warnings", warning["code"])
        self.assertEqual(expected_codes, warning["warning_codes"])

    def test_missing_operator_sprint_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            workbench["sprints"] = [
                sprint
                for sprint in workbench["sprints"]
                if sprint["blocker"] != "qualification:collection_coverage"
            ]
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("remaining_blocker_without_operator_sprint", codes)

    def test_zero_row_operator_sprint_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            for sprint in workbench["sprints"]:
                if sprint["blocker"] == "qualification:collection_coverage":
                    sprint["selected_row_count"] = 0
                    sprint["rows"] = []
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        by_name = {item["name"]: item for item in report["blocker_operator_status"]}
        self.assertFalse(report["ok"])
        self.assertIn("remaining_blocker_without_operator_rows", codes)
        self.assertEqual(
            "operator_row_issue",
            by_name["qualification:collection_coverage"]["operator_readiness"],
        )

    def test_qualification_requires_explicit_guarded_manual_ready_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            remaining = json.loads(paths["remaining"].read_text(encoding="utf-8"))
            for blocker in remaining["remaining_blockers"]:
                if blocker["name"] == "qualification:collection_coverage":
                    blocker["status"] = "open"
            self._write_json(paths["remaining"], remaining)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        by_name = {item["name"]: item for item in report["blocker_operator_status"]}
        self.assertFalse(report["ok"])
        self.assertIn("qualification_guard_contract_issue", codes)
        self.assertEqual(
            "qualification_guard_contract_issue",
            by_name["qualification:collection_coverage"]["operator_readiness"],
        )

    def test_human_review_sprint_requires_decision_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["workbench"].read_text(encoding="utf-8"))
            for sprint in workbench["sprints"]:
                if sprint["blocker"] == "review_debt:human_reviewed_concepts":
                    sprint["required_human_fields"] = []
                    sprint["decision_options"] = []
            self._write_json(paths["workbench"], workbench)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        by_name = {item["name"]: item for item in report["blocker_operator_status"]}
        self.assertFalse(report["ok"])
        self.assertIn("remaining_blocker_human_decision_contract_issue", codes)
        self.assertEqual(
            "human_decision_contract_issue",
            by_name["review_debt:human_reviewed_concepts"]["operator_readiness"],
        )

    def test_unsafe_source_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            release = json.loads(paths["release"].read_text(encoding="utf-8"))
            release["approval_claim"] = True
            self._write_json(paths["release"], release)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_source_contract", codes)

    def test_terminal_cycle_contract_failure_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            terminal = json.loads(paths["terminal"].read_text(encoding="utf-8"))
            terminal["include_in_release_refresh_dag"] = True
            self._write_json(paths["terminal"], terminal)
            report = build_addendum(
                release_readiness_path=paths["release"],
                remaining_blockers_path=paths["remaining"],
                goal_completion_audit_path=paths["goal"],
                operator_workbench_path=paths["workbench"],
                terminal_evidence_index_path=paths["terminal"],
                root=root,
            )

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_terminal_cycle_contract", codes)

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "addendum.json"
            md = root / "reports" / "addendum.md"
            argv = [
                "--root",
                str(root),
                "--release-readiness",
                str(paths["release"]),
                "--remaining-blockers",
                str(paths["remaining"]),
                "--goal-completion-audit",
                str(paths["goal"]),
                "--operator-workbench",
                str(paths["workbench"]),
                "--terminal-evidence-index",
                str(paths["terminal"]),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = addendum_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Release Blocker Operator Addendum", markdown)
        self.assertIn("qualification:collection_coverage", markdown)
        self.assertIn("selected_rows=", markdown)
        self.assertIn("source_total_rows=", markdown)

    def test_cli_stamp_defaults_are_resolved_under_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            stamp = "demo"
            expected_names = {
                paths["release"]: f"aihr_release_readiness_{stamp}.json",
                paths["remaining"]: f"remaining_blockers_{stamp}.json",
                paths["goal"]: f"goal_completion_audit_{stamp}.json",
                paths["workbench"]: f"aihr_operator_decision_workbench_{stamp}.json",
                paths["terminal"]: f"aihr_terminal_evidence_index_{stamp}.json",
            }
            for original, name in expected_names.items():
                original.replace(root / "reports" / name)
            out = root / "reports" / "addendum.json"
            argv = [
                "--root",
                str(root),
                "--stamp",
                stamp,
                "--out",
                str(out),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = addendum_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertEqual(
            "reports/aihr_release_readiness_demo.json",
            payload["source_artifacts"]["release_readiness"]["path"],
        )


if __name__ == "__main__":
    unittest.main()
