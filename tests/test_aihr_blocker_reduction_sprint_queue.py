from __future__ import annotations

import csv
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_harness import main as harness_main  # noqa: E402
from scripts.build_aihr_blocker_reduction_operator_sprint_queue import (  # noqa: E402
    audit_queue,
    build_queue,
    write_audit_markdown,
    write_queue_csv,
    write_queue_markdown,
)


class AihrBlockerReductionSprintQueueTests(unittest.TestCase):
    def _write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        stamp = "20260712_10h"
        concept = reports / f"aihr_ontology_definition_review_seedpack_{stamp}.csv"
        blocker = reports / f"aihr_review_seedpack_blocker_ranked_{stamp}.csv"
        provenance = reports / f"human_review_provenance_reconfirmation_decision_sheet_{stamp}.csv"
        transition = reports / f"transition_trusted_scenario_provenance_gap_{stamp}.csv"
        transition_crosswalk = reports / f"transition_provenance_operator_crosswalk_{stamp}.csv"
        transition_crosswalk_audit = reports / f"transition_provenance_operator_crosswalk_audit_{stamp}.json"
        qualification = reports / f"qualification_guarded_batch_operator_decision_{stamp}.csv"
        next_actions = reports / f"aihr_operator_next_actions_{stamp}.json"
        lineage = reports / f"operator_report_lineage_sync_audit_{stamp}.json"
        integrity = reports / f"operator_review_packet_integrity_audit_{stamp}.json"
        for sidecar in (
            concept.with_suffix(".md"),
            concept.with_suffix(".jsonl"),
            blocker.with_suffix(".md"),
            provenance.with_suffix(".md"),
            transition.with_suffix(".md"),
            transition_crosswalk.with_suffix(".md"),
            transition_crosswalk_audit.with_suffix(".md"),
            qualification.with_suffix(".md"),
        ):
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text("sidecar\n", encoding="utf-8")
        self._write_csv(
            concept,
            ["sequence", "issue_type", "target_type", "target_id", "priority_score"],
            [
                {
                    "sequence": str(index),
                    "issue_type": "hr_core_concept_human_review_required",
                    "target_type": "ontology_concept",
                    "target_id": str(100 + index),
                    "priority_score": "100",
                }
                for index in range(1, 13)
            ],
        )
        blocker_rows = []
        for index in range(1, 4):
            blocker_rows.append(
                {
                    "sequence": str(index),
                    "issue_type": "ontology_training_goal_link_human_review_required",
                    "target_type": "training_goal_concept_link",
                    "target_id": str(index),
                }
            )
        for index in range(11, 14):
            blocker_rows.append(
                {
                    "sequence": str(index),
                    "issue_type": "ontology_task_ksa_relation_human_review_required",
                    "target_type": "task_ksa_concept_relation",
                    "target_id": str(index),
                }
            )
        for index in range(31, 34):
            blocker_rows.append(
                {
                    "sequence": str(index),
                    "issue_type": "hr_training_goal_link_human_review_required",
                    "target_type": "training_goal_concept_link",
                    "target_id": str(index),
                }
            )
        self._write_csv(blocker, ["sequence", "issue_type", "target_type", "target_id"], blocker_rows)
        provenance_rows = []
        for order in range(1, 3):
            provenance_rows.append(
                {
                    "order": str(order),
                    "surface": "ncs_career_paths",
                    "target_table": "ncs_career_paths",
                    "target_id": str(140 + order),
                }
            )
        provenance_rows.append(
            {
                "order": "3",
                "surface": "ncs_query_aliases",
                "target_table": "ncs_query_aliases",
                "target_id": "2",
            }
        )
        for order, scenario in enumerate(("3", "31"), start=4):
            provenance_rows.append(
                {
                    "order": str(order),
                    "surface": "training_transition_gold_scenarios",
                    "target_table": "training_transition_gold_scenarios",
                    "target_id": scenario,
                }
            )
        self._write_csv(
            provenance,
            ["order", "surface", "target_table", "target_id"],
            provenance_rows,
        )
        self._write_csv(
            transition,
            ["scenario_id", "audit_id", "gap_fields"],
            [
                {"scenario_id": "31", "audit_id": "", "gap_fields": "source_decision_packet"},
                {"scenario_id": "3", "audit_id": "64", "gap_fields": "source_decision_packet"},
            ],
        )
        self._write_csv(
            transition_crosswalk,
            ["scenario_id", "operator_source_decision_packet_ref"],
            [{"scenario_id": "3", "operator_source_decision_packet_ref": "reports/packet.json#order:4"}],
        )
        transition_crosswalk_audit.write_text(
            json.dumps({"schema": "transition_provenance_operator_crosswalk_audit_v1", "ok": True}),
            encoding="utf-8",
        )
        self._write_csv(
            qualification,
            ["wave", "batch_count", "requires_operator_start"],
            [{"wave": "pilot", "batch_count": "3", "requires_operator_start": "True"}],
        )
        for path in (next_actions, lineage, integrity):
            path.write_text("{}", encoding="utf-8")
        return {
            "concept": concept,
            "blocker": blocker,
            "provenance": provenance,
            "transition": transition,
            "transition_crosswalk": transition_crosswalk,
            "transition_crosswalk_audit": transition_crosswalk_audit,
            "qualification": qualification,
            "next_actions": next_actions,
            "lineage": lineage,
            "integrity": integrity,
        }

    def test_build_queue_prioritizes_transition_crosswalk_and_keeps_safety_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                operator_next_actions_json=paths["next_actions"],
                lineage_sync_audit_json=paths["lineage"],
                operator_packet_integrity_audit_json=paths["integrity"],
                transition_crosswalk_csv=paths["transition_crosswalk"],
                transition_crosswalk_audit_json=paths["transition_crosswalk_audit"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )

        self.assertTrue(report["ok"])
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["queue"][0]["sprint_id"], "S1-transition-provenance-crosswalk")
        self.assertEqual(
            report["queue"][0]["open_first"],
            "reports/transition_provenance_operator_crosswalk_20260712_10h.csv",
        )
        self.assertEqual(
            report["queue"][0]["next_safe_action"],
            "review-transition-provenance-crosswalk-human-decisions",
        )
        self.assertTrue(
            report["source_paths"]["transition_provenance_crosswalk_csv"].endswith(
                "transition_provenance_operator_crosswalk_20260712_10h.csv"
            )
        )
        self.assertFalse(Path(report["source_paths"]["concept_seedpack_csv"]).is_absolute())
        self.assertNotIn("operator_next_actions", report["source_paths"])
        self.assertNotIn("lineage_sync_audit", report["source_paths"])
        self.assertNotIn("operator_packet_integrity_audit", report["source_paths"])
        self.assertNotIn("operator_next_actions", report["source_hashes"])
        self.assertNotIn("lineage_sync_audit", report["source_hashes"])
        self.assertNotIn("operator_packet_integrity_audit", report["source_hashes"])
        self.assertEqual(
            report["context_artifacts"]["operator_next_actions"]["role"],
            "context_only_not_queue_source",
        )
        self.assertFalse(report["context_artifacts"]["operator_next_actions"]["hash_checked"])
        self.assertTrue(
            report["cycle_avoidance_contract"]["source_hashes_exclude_context_only_keys"]
        )
        self.assertEqual(report["queue"][0]["first_row_ids"], ["3", "31"])
        self.assertEqual(report["queue"][0]["row_count"], 2)
        self.assertEqual(report["queue"][3]["first_row_ids"], ["11", "12", "13"])
        self.assertEqual(report["queue"][4]["first_row_ids"], ["31", "32", "33"])
        self.assertTrue(report["acceptance_contract"]["all_open_first_exist"])
        self.assertTrue(report["acceptance_contract"]["all_artifacts_exist"])
        self.assertTrue(
            report["acceptance_contract"]["transition_scenarios_have_decision_sheet_rows"]
        )
        self.assertTrue(report["acceptance_contract"]["transition_scenario_decision_rows_unique"])

    def test_audit_queue_does_not_hash_check_context_only_operator_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                operator_next_actions_json=paths["next_actions"],
                lineage_sync_audit_json=paths["lineage"],
                operator_packet_integrity_audit_json=paths["integrity"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )
            paths["next_actions"].write_text('{"changed": true}\n', encoding="utf-8")
            paths["lineage"].write_text('{"changed": true}\n', encoding="utf-8")
            paths["integrity"].write_text('{"changed": true}\n', encoding="utf-8")

            audit = audit_queue(report, root=root)

        self.assertTrue(audit["ok"])
        self.assertNotIn("operator_next_actions", audit["source_hash_checks"])
        self.assertNotIn("lineage_sync_audit", audit["source_hash_checks"])
        self.assertNotIn("operator_packet_integrity_audit", audit["source_hash_checks"])

    def test_audit_queue_flags_cycle_prone_keys_in_source_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )
            report["source_paths"]["operator_next_actions"] = "reports/next_actions.json"

            audit = audit_queue(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("cycle_prone_context_keys_in_source_paths", codes)

    def test_audit_queue_flags_missing_open_first_and_guard_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )
            report["db_writes"] = True
            report["queue"][0]["open_first_exists_nonempty"] = False
            report["acceptance_contract"]["all_open_first_exist"] = False

            audit = audit_queue(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("db_writes_not_false", codes)
        self.assertIn("open_first_missing", codes)
        self.assertIn("acceptance_all_open_first_exist_not_true", codes)

    def test_audit_queue_flags_source_hash_drift_and_nonblank_decision_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )
            with paths["concept"].open("a", encoding="utf-8") as handle:
                handle.write("13,hr_core_concept_human_review_required,13\n")
            with paths["blocker"].open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["sequence", "issue_type", "target_id", "decision"],
                )
                writer.writeheader()
                writer.writerow(
                    {
                        "sequence": "31",
                        "issue_type": "hr_training_goal_link_human_review_required",
                        "target_id": "31",
                        "decision": "accept_link",
                    }
                )

            audit = audit_queue(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("source_artifact_hash_mismatch", codes)
        self.assertIn("input_decision_fields_not_blank", codes)
        self.assertFalse(
            audit["input_decision_field_checks"]["blocker_ranked_seedpack_csv"]["blank_ok"]
        )

    def test_writers_emit_queue_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )
            queue_csv = root / "queue.csv"
            queue_md = root / "queue.md"
            audit_md = root / "audit.md"

            write_queue_csv(queue_csv, report)
            write_queue_markdown(queue_md, report)
            write_audit_markdown(audit_md, audit_queue(report, root=root))

            csv_text = queue_csv.read_text(encoding="utf-8-sig")
            md_text = queue_md.read_text(encoding="utf-8")
            audit_text = audit_md.read_text(encoding="utf-8")

        self.assertIn("S1-transition-provenance-crosswalk", csv_text)
        self.assertIn("status_update_allowed: `False`", md_text)
        self.assertIn("No human_reviewed, accepted, or reviewed status is authorized", md_text)
        self.assertIn("No sprint queue integrity issues found.", audit_text)

    def test_audit_queue_flags_transition_gap_without_decision_sheet_row(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            self._write_csv(
                paths["provenance"],
                ["order", "surface", "target_table", "target_id"],
                [
                    {
                        "order": "1",
                        "surface": "training_transition_gold_scenarios",
                        "target_table": "training_transition_gold_scenarios",
                        "target_id": "3",
                    }
                ],
            )
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )

            audit = audit_queue(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(report["ok"])
        self.assertFalse(audit["ok"])
        self.assertIn("transition_scenarios_missing_decision_sheet_rows", codes)
        self.assertIn(
            "31",
            audit["transition_provenance_decision_mapping"][
                "missing_decision_sheet_scenario_ids"
            ],
        )

    def test_audit_queue_flags_duplicate_transition_decision_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            with paths["provenance"].open("a", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["order", "surface", "target_table", "target_id"],
                )
                writer.writerow(
                    {
                        "order": "99",
                        "surface": "training_transition_gold_scenarios",
                        "target_table": "training_transition_gold_scenarios",
                        "target_id": "3",
                    }
                )
            report = build_queue(
                concept_seedpack_csv=paths["concept"],
                blocker_ranked_seedpack_csv=paths["blocker"],
                provenance_decision_sheet_csv=paths["provenance"],
                transition_gap_csv=paths["transition"],
                qualification_decision_csv=paths["qualification"],
                generated_at="2026-07-12T02:00:00+00:00",
                root=root,
            )

            audit = audit_queue(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(report["ok"])
        self.assertFalse(audit["ok"])
        self.assertIn("transition_scenario_duplicate_decision_sheet_rows", codes)
        self.assertIn(
            "3",
            audit["transition_provenance_decision_mapping"][
                "duplicate_decision_sheet_scenario_ids"
            ],
        )

    def test_harness_command_auto_discovers_from_temp_root_and_writes_relative_paths(self) -> None:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            reports = root / "reports"
            out = reports / "aihr_blocker_reduction_operator_sprint_queue_20260712_10h.json"
            audit_out = reports / "aihr_blocker_reduction_operator_sprint_queue_audit_20260712_10h.json"
            sys.argv = [
                "ncs_harness.py",
                "build-aihr-blocker-reduction-sprint-queue",
                "--root",
                str(root),
                "--operator-next-actions-json",
                str(paths["next_actions"]),
                "--lineage-sync-audit-json",
                str(paths["lineage"]),
                "--operator-packet-integrity-audit-json",
                str(paths["integrity"]),
                "--transition-crosswalk-csv",
                str(paths["transition_crosswalk"]),
                "--transition-crosswalk-audit-json",
                str(paths["transition_crosswalk_audit"]),
                "--out",
                str(out),
                "--audit-out",
                str(audit_out),
                "--strict",
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    harness_main()
            finally:
                sys.argv = previous_argv
            payload = json.loads(stdout.getvalue())
            report = json.loads(out.read_text(encoding="utf-8"))

        self.assertTrue(payload["ok"])
        self.assertTrue(
            payload["audit_path"].endswith(
                "aihr_blocker_reduction_operator_sprint_queue_audit_20260712_10h.json"
            )
        )
        self.assertEqual(
            report["source_paths"]["concept_seedpack_csv"],
            "reports/aihr_ontology_definition_review_seedpack_20260712_10h.csv",
        )


if __name__ == "__main__":
    unittest.main()
