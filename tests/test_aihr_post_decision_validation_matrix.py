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

from scripts.build_aihr_post_decision_validation_matrix import (  # noqa: E402
    build_matrix,
    default_paths,
    main as matrix_main,
)


class AihrPostDecisionValidationMatrixTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_text(self, path: Path, text: str = "x\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _safe(self, **extra: Any) -> dict[str, Any]:
        payload = {
            "schema": "fixture_v1",
            "ok": True,
            "status": "pass",
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

    def _fixture(
        self,
        root: Path,
        stamp: str = "demo",
        *,
        pending_decision_count: int = 0,
    ) -> dict[str, Path]:
        reports = root / "reports"
        paths = default_paths(stamp, root=root)
        for key, path in paths.items():
            schema = {
                "release_blocker_addendum": "aihr_release_blocker_operator_addendum_v1",
                "operator_workbench": "aihr_operator_decision_workbench_v1",
                "ontology_seedpack_audit": "aihr_review_seedpack_csv_decision_audit_v1",
                "goal_link_seedpack_audit": "aihr_review_seedpack_csv_decision_audit_v1",
                "task_relation_seedpack_audit": "aihr_review_seedpack_csv_decision_audit_v1",
                "provenance_decision_audit": "aihr_provenance_reconfirmation_decision_audit_v1",
                "qualification_decision": "qualification_guarded_batch_operator_decision_v1",
                "qualification_decision_audit": "qualification_guarded_batch_operator_decision_audit_v1",
                "ksa_definition_decision_audit": "ncs_ksa_definition_review_decision_audit_v1",
                "ksa_definition_action_plan": "ncs_ksa_definition_review_action_plan_v1",
            }[key]
            pending = (
                pending_decision_count
                if key
                in {
                    "ontology_seedpack_audit",
                    "goal_link_seedpack_audit",
                    "task_relation_seedpack_audit",
                    "provenance_decision_audit",
                    "ksa_definition_decision_audit",
                }
                else None
            )
            requires_completion = key in {
                "ontology_seedpack_audit",
                "goal_link_seedpack_audit",
                "task_relation_seedpack_audit",
            }
            completion_issue = bool(requires_completion and pending)
            ok = not completion_issue
            self._write_json(
                path,
                self._safe(
                    schema=schema,
                    ok=ok,
                    status="pass" if ok else "fail",
                    row_count=10 if pending is not None else None,
                    pending_decision_count=pending,
                    completed_decision_count=0 if pending is not None else None,
                    invalid_decision_count=0,
                    guard_issue_row_count=0,
                    require_completed_decisions=requires_completion,
                    completion_issue=completion_issue,
                    missing_required_columns=[],
                    issue_count=0,
                ),
            )
        for name in [
            f"aihr_ontology_definition_review_seedpack_{stamp}.csv",
            f"aihr_review_seedpack_blocker_ranked_{stamp}.csv",
            f"transition_provenance_operator_crosswalk_{stamp}.csv",
            f"human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv",
            f"qualification_guarded_batch_operator_decision_{stamp}.csv",
        ]:
            self._write_text(reports / name, "header\nvalue\n")
        return paths

    def test_matrix_maps_six_blockers_to_safe_post_decision_audits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            report = build_matrix(stamp="demo", root=root)

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertTrue(report["terminal_evidence_only"])
        self.assertFalse(report["include_in_release_refresh_dag"])
        self.assertFalse(report["include_in_operator_handoff"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(0, report["issue_count"])
        self.assertEqual(1, report["warning_count"])
        self.assertEqual(6, report["summary"]["validation_row_count"])
        self.assertEqual(6, report["summary"]["route_ok_count"])
        self.assertEqual(6, report["summary"]["row_ok_count"])
        self.assertEqual(0, report["summary"]["pending_post_decision_row_count"])
        self.assertEqual(0, report["summary"]["audit_scope_exceeds_workbench_selection_count"])
        self.assertEqual(0, report["summary"]["issue_count"])
        self.assertEqual(1, report["summary"]["warning_count"])
        self.assertIn("shared_post_decision_command", {w["code"] for w in report["warnings"]})
        qualification_row = [
            row for row in report["validation_rows"] if row["blocker"] == "qualification:collection_coverage"
        ][0]
        self.assertNotIn("--csv-out", qualification_row["post_decision_command"])
        self.assertIn(
            "audit_qualification_guarded_batch_operator_decision.py",
            qualification_row["post_decision_command"],
        )

    def test_pending_decision_audits_leave_post_decision_rows_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root, pending_decision_count=3)
            report = build_matrix(stamp="demo", root=root)

        self.assertFalse(report["ok"])
        self.assertEqual("pending_human_decisions", report["status"])
        self.assertEqual(6, report["summary"]["route_ok_count"])
        self.assertEqual(1, report["summary"]["row_ok_count"])
        self.assertEqual(5, report["summary"]["pending_post_decision_row_count"])
        self.assertEqual(1, report["summary"]["pending_issue_count"])
        self.assertIn("post_decision_rows_pending", {issue["code"] for issue in report["issues"]})

    def test_workbench_scope_gap_is_explicit_warning_not_hard_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            workbench = json.loads(paths["operator_workbench"].read_text(encoding="utf-8"))
            workbench["sprints"] = [
                {
                    "sprint_id": "S5-core-concept-definition-review",
                    "blocker": "review_debt:human_reviewed_concepts",
                    "selected_row_count": 10,
                    "source_path": "reports/aihr_ontology_definition_review_seedpack_demo.csv",
                    "row_selector": "start with rows 1-10",
                }
            ]
            self._write_json(paths["operator_workbench"], workbench)
            audit = json.loads(paths["ontology_seedpack_audit"].read_text(encoding="utf-8"))
            audit["row_count"] = 100
            self._write_json(paths["ontology_seedpack_audit"], audit)
            report = build_matrix(stamp="demo", root=root)

        concept_row = [
            row
            for row in report["validation_rows"]
            if row["blocker"] == "review_debt:human_reviewed_concepts"
        ][0]
        self.assertFalse(report["ok"])
        self.assertEqual("pass_with_workbench_scope_warnings", report["status"])
        self.assertEqual(1, report["summary"]["audit_scope_exceeds_workbench_selection_count"])
        self.assertTrue(concept_row["audit_scope_exceeds_workbench_selection"])
        self.assertEqual(10, concept_row["workbench_selected_row_count"])
        self.assertEqual(100, concept_row["audit_row_count"])
        self.assertIn(
            "post_decision_audit_scope_exceeds_workbench_selection",
            {warning["code"] for warning in report["warnings"]},
        )

    def test_pending_seedpack_audit_without_completion_guard_is_hard_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root, pending_decision_count=3)
            payload = json.loads(paths["ontology_seedpack_audit"].read_text(encoding="utf-8"))
            payload["ok"] = True
            payload["status"] = "pass"
            payload["require_completed_decisions"] = False
            payload["completion_issue"] = False
            self._write_json(paths["ontology_seedpack_audit"], payload)
            report = build_matrix(stamp="demo", root=root)

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertEqual("fail", report["status"])
        self.assertIn("post_decision_audit_missing_completion_guard", codes)

    def test_cli_strict_fails_for_pending_human_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root, pending_decision_count=3)
            out = root / "reports" / "matrix.json"
            argv = [
                "--root",
                str(root),
                "--stamp",
                "demo",
                "--out",
                str(out),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = matrix_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(1, exit_code)
        self.assertEqual("pending_human_decisions", payload["status"])

    def test_missing_decision_surface_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            (root / "reports" / "qualification_guarded_batch_operator_decision_demo.csv").unlink()
            report = build_matrix(stamp="demo", root=root)

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("missing_decision_surface", codes)

    def test_missing_actual_decision_sheet_fails_transition_crosswalk_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            (root / "reports" / "human_review_provenance_reconfirmation_decision_sheet_demo.csv").unlink()
            report = build_matrix(stamp="demo", root=root)

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("missing_actual_decision_sheet", codes)

    def test_unsafe_audit_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["ontology_seedpack_audit"].read_text(encoding="utf-8"))
            payload["db_writes"] = True
            self._write_json(paths["ontology_seedpack_audit"], payload)
            report = build_matrix(stamp="demo", root=root)

        codes = {item["code"] for item in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("unsafe_source_contract", codes)

    def test_missing_report_only_is_legacy_warning_when_false_flags_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["provenance_decision_audit"].read_text(encoding="utf-8"))
            payload.pop("report_only")
            self._write_json(paths["provenance_decision_audit"], payload)
            report = build_matrix(stamp="demo", root=root)

        self.assertTrue(report["ok"])
        self.assertIn("legacy_report_only_field_absent", {w["code"] for w in report["warnings"]})

    def test_cli_writes_json_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            out = root / "reports" / "matrix.json"
            md = root / "reports" / "matrix.md"
            argv = [
                "--root",
                str(root),
                "--stamp",
                "demo",
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = matrix_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Post-Decision Validation Matrix", markdown)
        self.assertIn("qualification:collection_coverage", markdown)


if __name__ == "__main__":
    unittest.main()
