from __future__ import annotations

import contextlib
import csv
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

from scripts.build_aihr_operator_decision_workbench import (  # noqa: E402
    build_workbench,
    main as workbench_main,
)


class AihrOperatorDecisionWorkbenchTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def _safe_top(self, **extra: Any) -> dict[str, Any]:
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
        decision = self._write_csv(
            reports / "decision.csv",
            [
                "order",
                "surface",
                "display",
                "decision",
                "rationale",
                "reviewer_id",
                "reviewed_at",
                "source_decision_packet",
                "evidence_refs_json",
                "status_update_allowed",
                "db_writes",
                "approval_claim",
            ],
            [
                {
                    "order": "22",
                    "surface": "ncs_query_aliases",
                    "display": "alias row",
                    "decision": "",
                    "rationale": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "source_decision_packet": "",
                    "evidence_refs_json": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
                {
                    "order": "1",
                    "surface": "ncs_career_paths",
                    "display": "career row",
                    "decision": "",
                    "rationale": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "source_decision_packet": "",
                    "evidence_refs_json": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
            ],
        )
        seedpack = self._write_csv(
            reports / "seedpack.csv",
            [
                "sequence",
                "issue_type",
                "target_type",
                "target_id",
                "source_context_excerpt",
                "issue_detail",
                "decision",
                "reviewer_id",
                "reviewed_at",
                "rationale",
                "status_update_allowed",
                "db_writes",
                "approval_claim",
            ],
            [
                {
                    "sequence": "1",
                    "issue_type": "ontology_task_ksa_relation_human_review_required",
                    "target_type": "relation",
                    "target_id": "r1",
                    "source_context_excerpt": "task row",
                    "issue_detail": "needs review",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
                {
                    "sequence": "2",
                    "issue_type": "hr_training_goal_link_human_review_required",
                    "target_type": "goal_link",
                    "target_id": "g1",
                    "source_context_excerpt": "goal row",
                    "issue_detail": "needs review",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
            ],
        )
        sprint_queue = self._write_json(
            reports / "sprint_queue.json",
            self._safe_top(
                schema="aihr_blocker_reduction_operator_sprint_queue_v1",
                queue=[
                    {
                        "rank": 1,
                        "sprint_id": "S2-provenance",
                        "blocker": "human_review",
                        "open_first": decision.relative_to(root).as_posix(),
                        "row_selector": "rows 22-23 ncs_query_aliases first, then rows 1-21 ncs_career_paths",
                        "row_count": 2,
                        "first_row_ids": ["22", "1"],
                        "required_human_fields": [
                            "decision",
                            "rationale",
                            "reviewer_id",
                            "reviewed_at",
                            "source_decision_packet",
                            "evidence_refs_json",
                        ],
                        "decision_options": ["reconfirm", "defer"],
                        "forbidden": ["db_writes=true"],
                    },
                    {
                        "rank": 2,
                        "sprint_id": "S4-task",
                        "blocker": "task review",
                        "open_first": seedpack.relative_to(root).as_posix(),
                        "row_selector": "filter issue_type=ontology_task_ksa_relation_human_review_required",
                        "row_count": 1,
                        "first_row_ids": ["1"],
                        "required_human_fields": ["decision", "reviewer_id", "reviewed_at", "rationale"],
                        "decision_options": ["accept_relation", "defer"],
                        "forbidden": ["automatic status promotion"],
                    },
                ],
            ),
        )
        manifest = self._write_json(
            reports / "entrypoint.json",
            self._safe_top(schema="aihr_operator_entrypoint_manifest_v1"),
        )
        return {"sprint_queue": sprint_queue, "manifest": manifest, "seedpack": seedpack}

    def test_workbench_selects_rows_and_preserves_guard_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_workbench(
                sprint_queue_path=paths["sprint_queue"],
                entrypoint_manifest_path=paths["manifest"],
                root=root,
                per_sprint_limit=10,
                generated_at="2026-07-12T15:30:00+00:00",
            )

        self.assertTrue(report["ok"])
        self.assertEqual("pass", report["status"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(2, report["summary"]["sprint_count"])
        self.assertEqual(3, report["summary"]["workbench_row_count"])
        self.assertEqual(3, report["summary"]["selected_row_count"])
        self.assertEqual(3, report["summary"]["source_total_row_count"])
        self.assertEqual(0, report["summary"]["unselected_source_row_count"])
        self.assertEqual(0, report["summary"]["selected_subset_sprint_count"])
        self.assertEqual(
            report["source_hashes"]["sprint_queue"],
            report["source_hash_checks"]["sprint_queue"]["actual_sha256"],
        )
        self.assertTrue(report["source_hash_checks"]["sprint_queue"]["hash_matches"])
        self.assertTrue(report["source_hash_checks"]["entrypoint_manifest"]["hash_matches"])
        first = report["workbench_rows"][0]
        self.assertEqual("22", first["source_row_key"])
        self.assertEqual("2", first["source_row_number"])
        self.assertEqual(1, first["selected_order"])
        self.assertTrue(first["decision_fields_blank_ok"])
        self.assertTrue(first["guard_fields_false_ok"])
        self.assertEqual("True", str(first["scope_match_ok"]))
        task_rows = [row for row in report["workbench_rows"] if row["sprint_id"] == "S4-task"]
        self.assertEqual(1, len(task_rows))
        self.assertEqual("2", task_rows[0]["source_row_number"])
        self.assertIn("task row", task_rows[0]["row_preview"])

    def test_selected_subset_warning_is_visible_without_failing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["sprint_queue"].read_text(encoding="utf-8"))
            payload["queue"][0]["row_count"] = 5
            payload["queue"][0]["next_safe_action"] = "review-provenance-subset"
            self._write_json(paths["sprint_queue"], payload)
            report = build_workbench(
                sprint_queue_path=paths["sprint_queue"],
                entrypoint_manifest_path=paths["manifest"],
                root=root,
                per_sprint_limit=2,
            )
            md = root / "reports" / "workbench.md"
            from scripts.build_aihr_operator_decision_workbench import write_markdown

            write_markdown(md, report)
            markdown = md.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertEqual(1, report["summary"]["warning_count"])
        self.assertEqual(1, report["summary"]["selected_subset_sprint_count"])
        self.assertEqual(3, report["summary"]["unselected_source_row_count"])
        self.assertEqual("selected_workbench_subset", report["warnings"][0]["code"])
        self.assertEqual("review-provenance-subset", report["sprints"][0]["next_safe_action"])
        self.assertIn("## Subset Notice", markdown)
        self.assertIn("unselected_source_row_count", markdown)

    def test_alias_career_selector_respects_declared_row_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            self._write_csv(
                root / "reports" / "decision.csv",
                [
                    "order",
                    "surface",
                    "display",
                    "decision",
                    "rationale",
                    "reviewer_id",
                    "reviewed_at",
                    "source_decision_packet",
                    "evidence_refs_json",
                    "status_update_allowed",
                    "db_writes",
                    "approval_claim",
                ],
                [
                    {
                        "order": "22",
                        "surface": "ncs_query_aliases",
                        "display": "alias row",
                        "decision": "",
                        "rationale": "",
                        "reviewer_id": "",
                        "reviewed_at": "",
                        "source_decision_packet": "",
                        "evidence_refs_json": "",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    },
                    {
                        "order": "24",
                        "surface": "ncs_query_aliases",
                        "display": "out of range alias row",
                        "decision": "",
                        "rationale": "",
                        "reviewer_id": "",
                        "reviewed_at": "",
                        "source_decision_packet": "",
                        "evidence_refs_json": "",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    },
                    {
                        "order": "1",
                        "surface": "ncs_career_paths",
                        "display": "career row",
                        "decision": "",
                        "rationale": "",
                        "reviewer_id": "",
                        "reviewed_at": "",
                        "source_decision_packet": "",
                        "evidence_refs_json": "",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    },
                ],
            )
            report = build_workbench(
                sprint_queue_path=paths["sprint_queue"],
                entrypoint_manifest_path=paths["manifest"],
                root=root,
            )

        rows = [row for row in report["workbench_rows"] if row["sprint_id"] == "S2-provenance"]
        self.assertTrue(report["ok"])
        self.assertEqual(["22", "1"], [row["source_row_key"] for row in rows])
        self.assertFalse(any("out of range" in row["row_preview"] for row in rows))

    def test_missing_declared_first_row_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            payload = json.loads(paths["sprint_queue"].read_text(encoding="utf-8"))
            payload["queue"][1]["first_row_ids"] = ["1", "999"]
            payload["queue"][1]["row_count"] = 2
            self._write_json(paths["sprint_queue"], payload)
            report = build_workbench(
                sprint_queue_path=paths["sprint_queue"],
                entrypoint_manifest_path=paths["manifest"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("declared_first_rows_missing", codes)
        self.assertIn("declared_row_count_not_satisfied", codes)

    def test_unsafe_crosswalk_contract_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            crosswalk = self._write_csv(
                reports / "crosswalk.csv",
                [
                    "scenario_id",
                    "decision_sheet_order",
                    "decision_sheet_row_found",
                    "recommended_source_decision_packet_artifact_exists",
                    "operator_source_decision_packet_ref",
                    "operator_source_artifact_hash",
                    "operator_decision_fields_blank",
                    "operator_guard_fields_false",
                ],
                [
                    {
                        "scenario_id": "3",
                        "decision_sheet_order": "24",
                        "decision_sheet_row_found": "true",
                        "recommended_source_decision_packet_artifact_exists": "true",
                        "operator_source_decision_packet_ref": "",
                        "operator_source_artifact_hash": "",
                        "operator_decision_fields_blank": "false",
                        "operator_guard_fields_false": "false",
                    }
                ],
            )
            sprint_queue = self._write_json(
                reports / "sprint_queue.json",
                self._safe_top(
                    schema="aihr_blocker_reduction_operator_sprint_queue_v1",
                    queue=[
                        {
                            "rank": 1,
                            "sprint_id": "S1-crosswalk",
                            "blocker": "transition",
                            "open_first": crosswalk.relative_to(root).as_posix(),
                            "row_selector": "transition_gap: scenario ids 3",
                            "row_count": 1,
                            "first_row_ids": ["3"],
                            "required_human_fields": [
                                "decision",
                                "rationale",
                                "reviewer_id",
                                "reviewed_at",
                                "source_decision_packet",
                                "evidence_refs_json",
                            ],
                            "decision_options": ["reconfirm", "defer"],
                            "forbidden": ["automatic human_reviewed/accepted/reviewed"],
                        }
                    ],
                ),
            )
            manifest = self._write_json(
                reports / "entrypoint.json",
                self._safe_top(schema="aihr_operator_entrypoint_manifest_v1"),
            )
            report = build_workbench(
                sprint_queue_path=sprint_queue,
                entrypoint_manifest_path=manifest,
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("crosswalk_contract_not_safe", codes)

    def test_prefilled_decision_field_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            self._write_csv(
                paths["seedpack"],
                [
                    "sequence",
                    "issue_type",
                    "target_type",
                    "target_id",
                    "source_context_excerpt",
                    "issue_detail",
                    "decision",
                    "reviewer_id",
                    "reviewed_at",
                    "rationale",
                    "status_update_allowed",
                    "db_writes",
                    "approval_claim",
                ],
                [
                    {
                        "sequence": "1",
                        "issue_type": "ontology_task_ksa_relation_human_review_required",
                        "target_type": "relation",
                        "target_id": "r1",
                        "source_context_excerpt": "task row",
                        "issue_detail": "needs review",
                        "decision": "accept_relation",
                        "reviewer_id": "",
                        "reviewed_at": "",
                        "rationale": "",
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    }
                ],
            )
            report = build_workbench(
                sprint_queue_path=paths["sprint_queue"],
                entrypoint_manifest_path=paths["manifest"],
                root=root,
            )

        codes = {issue["code"] for issue in report["issues"]}
        self.assertFalse(report["ok"])
        self.assertIn("decision_fields_not_blank", codes)

    def test_cli_writes_json_markdown_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            out = root / "reports" / "workbench.json"
            md = root / "reports" / "workbench.md"
            csv_out = root / "reports" / "workbench.csv"
            argv = [
                "--root",
                str(root),
                "--sprint-queue",
                str(paths["sprint_queue"].relative_to(root)),
                "--entrypoint-manifest",
                str(paths["manifest"].relative_to(root)),
                "--out",
                str(out),
                "--markdown-out",
                str(md),
                "--csv-out",
                str(csv_out),
                "--strict",
            ]
            with contextlib.redirect_stdout(io.StringIO()):
                exit_code = workbench_main(argv)

            payload = json.loads(out.read_text(encoding="utf-8"))
            markdown = md.read_text(encoding="utf-8")
            with csv_out.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(0, exit_code)
        self.assertTrue(payload["ok"])
        self.assertIn("AI-HR Operator Decision Workbench", markdown)
        self.assertEqual(3, len(rows))


if __name__ == "__main__":
    unittest.main()
