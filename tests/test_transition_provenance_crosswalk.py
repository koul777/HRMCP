from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_script_module(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {relative_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


crosswalk = load_script_module(
    "build_transition_provenance_crosswalk",
    "scripts/build_transition_provenance_crosswalk.py",
)


class TransitionProvenanceCrosswalkTests(unittest.TestCase):
    def _write_csv(self, path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _fixture(self, root: Path, *, suffix: str = "20260712_10h") -> dict[str, Path]:
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        transition = reports / f"transition_trusted_scenario_provenance_gap_{suffix}.csv"
        decision_csv = reports / f"human_review_provenance_reconfirmation_decision_sheet_{suffix}.csv"
        decision_json = decision_csv.with_suffix(".json")
        packet = reports / f"human_review_provenance_reconfirmation_packet_{suffix}.json"
        packet.write_text('{"schema":"packet"}\n', encoding="utf-8")
        packet_sha = "sha256:" + hashlib.sha256(packet.read_bytes()).hexdigest()

        self._write_csv(
            transition,
            [
                "scenario_id",
                "scenario_name",
                "review_status",
                "audit_id",
                "gap_fields",
                "recommended_source_decision_packet",
                "required_action",
                "required_evidence_refs_json",
                "current_query",
                "target_query",
                "expected_course_names_json",
            ],
            [
                {
                    "scenario_id": "3",
                    "scenario_name": "case_3",
                    "review_status": "reviewed",
                    "audit_id": "64",
                    "gap_fields": "source_decision_packet,source_artifact_hash",
                    "recommended_source_decision_packet": f"reports/transition_trusted_scenario_packet_{suffix}.md#scenario:3",
                    "required_action": "packet_backed_human_review",
                    "required_evidence_refs_json": '["scenario:3"]',
                    "current_query": "current",
                    "target_query": "target",
                    "expected_course_names_json": '["course"]',
                },
                {
                    "scenario_id": "3",
                    "scenario_name": "case_3",
                    "review_status": "reviewed",
                    "audit_id": "74",
                    "gap_fields": "rationale,evidence_refs_json",
                    "recommended_source_decision_packet": f"reports/transition_trusted_scenario_packet_{suffix}.md#scenario:3",
                    "required_action": "packet_backed_human_review",
                    "required_evidence_refs_json": '["scenario:3"]',
                    "current_query": "current",
                    "target_query": "target",
                    "expected_course_names_json": '["course"]',
                },
                {
                    "scenario_id": "31",
                    "scenario_name": "case_31",
                    "review_status": "reviewed",
                    "audit_id": "",
                    "gap_fields": "review_audit_log",
                    "recommended_source_decision_packet": f"reports/transition_trusted_scenario_packet_{suffix}.md#scenario:31",
                    "required_action": "packet_backed_human_review",
                    "required_evidence_refs_json": '["scenario:31"]',
                    "current_query": "current31",
                    "target_query": "target31",
                    "expected_course_names_json": '["course31"]',
                },
            ],
        )
        # Reversed order proves matching is semantic by target_id, not row position.
        self._write_csv(
            decision_csv,
            [
                "order",
                "surface",
                "target_table",
                "target_id",
                "display",
                "provenance_state",
                "decision",
                "rationale",
                "reviewer_id",
                "reviewed_at",
                "source_decision_packet",
                "source_packet_sha256",
                "source_packet_row_sha256",
                "evidence_refs_json",
                "status_update_allowed",
                "db_writes",
                "approval_claim",
            ],
            [
                {
                    "order": "34",
                    "surface": "training_transition_gold_scenarios",
                    "target_table": "training_transition_gold_scenarios",
                    "target_id": "31",
                    "display": "case_31",
                    "provenance_state": "no_audit_log",
                    "source_packet_sha256": packet_sha,
                    "source_packet_row_sha256": "sha256:" + ("3" * 64),
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
                {
                    "order": "24",
                    "surface": "training_transition_gold_scenarios",
                    "target_table": "training_transition_gold_scenarios",
                    "target_id": "3",
                    "display": "case_3",
                    "provenance_state": "audit_log_without_packet",
                    "source_packet_sha256": packet_sha,
                    "source_packet_row_sha256": "sha256:" + ("4" * 64),
                    "status_update_allowed": "false",
                    "db_writes": "false",
                    "approval_claim": "false",
                },
            ],
        )
        decision_json.write_text(
            json.dumps(
                {
                    "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                    "generated_at": "2026-07-12T02:10:39+00:00",
                    "source_packet": f"reports/human_review_provenance_reconfirmation_packet_{suffix}.json",
                    "source_packet_sha256": packet_sha,
                    "content_sha256_excluding_self_hash": "sha256:" + ("5" * 64),
                    "status_update_allowed": False,
                    "db_writes": False,
                    "api_calls": False,
                    "approval_claim": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "transition": transition,
            "decision_csv": decision_csv,
            "decision_json": decision_json,
            "packet": packet,
        }

    def test_build_crosswalk_dedupes_scenarios_and_keeps_report_only_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = crosswalk.build_crosswalk(
                transition_gap_csv=paths["transition"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                generated_at="2026-07-12T03:00:00+00:00",
                root=root,
            )
            audit = crosswalk.audit_crosswalk(report, root=root)

        self.assertEqual(report["schema"], "transition_provenance_operator_crosswalk_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["api_calls"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["scenario_count"], 2)
        self.assertEqual([row["scenario_id"] for row in report["rows"]], ["3", "31"])
        first = report["rows"][0]
        self.assertEqual(first["transition_gap_row_count"], 2)
        self.assertEqual(first["audit_ids"], ["64", "74"])
        self.assertEqual(first["decision_sheet_order"], "24")
        self.assertNotIn("review_status", first)
        self.assertTrue(first["operator_decision_fields_blank"])
        self.assertTrue(first["operator_guard_fields_false"])
        self.assertTrue(first["operator_source_decision_packet_ref"].endswith("#order:24"))
        self.assertTrue(first["recommended_source_decision_packet_ref"].endswith("#order:24"))
        self.assertTrue(first["recommended_source_decision_packet_artifact_exists"])
        self.assertEqual(
            first["recommended_source_decision_packet_source"],
            "operator_reconfirmation_packet",
        )
        self.assertEqual(report["operator_ready_row_count"], 2)
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["issue_count"], 0)
        self.assertEqual(audit["warning_count"], 0)
        self.assertEqual(audit["diagnostic_count"], 2)
        self.assertEqual(
            {diagnostic["code"] for diagnostic in audit["diagnostics"]},
            {"legacy_gap_recommended_packet_artifact_missing"},
        )
        self.assertEqual(2, audit["legacy_gap_recommended_packet_diagnostic_count"])
        self.assertTrue(
            audit["legacy_gap_recommended_packet_missing_is_non_blocking_when_primary_exists"]
        )

    def test_build_crosswalk_prefers_operator_packet_when_legacy_recommended_packet_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            legacy_packet = root / "reports" / "transition_trusted_scenario_packet_20260712_10h.md"
            legacy_packet.write_text("# legacy packet\n", encoding="utf-8")
            report = crosswalk.build_crosswalk(
                transition_gap_csv=paths["transition"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                generated_at="2026-07-12T03:00:00+00:00",
                root=root,
            )
            audit = crosswalk.audit_crosswalk(report, root=root)

        first = report["rows"][0]
        self.assertTrue(first["gap_recommended_packet_artifact_exists"])
        self.assertTrue(first["recommended_source_decision_packet_ref"].endswith("#order:24"))
        self.assertEqual(
            first["recommended_source_decision_packet_source"],
            "operator_reconfirmation_packet",
        )
        self.assertTrue(audit["ok"])
        self.assertEqual(audit["warning_count"], 0)
        self.assertEqual(audit["diagnostic_count"], 0)

    def test_audit_flags_hash_drift_and_decision_field_contamination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            with paths["decision_csv"].open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            rows[0]["decision"] = "reconfirm"
            self._write_csv(paths["decision_csv"], list(rows[0].keys()), rows)
            report = crosswalk.build_crosswalk(
                transition_gap_csv=paths["transition"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                generated_at="2026-07-12T03:00:00+00:00",
                root=root,
            )
            paths["transition"].write_text(
                paths["transition"].read_text(encoding="utf-8-sig") + "\n",
                encoding="utf-8",
            )
            audit = crosswalk.audit_crosswalk(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("source_artifact_hash_mismatch", codes)
        self.assertIn("operator_decision_fields_not_blank", codes)

    def test_audit_flags_source_suffix_family_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root, suffix="20260712_10h")
            old_paths = self._fixture(root, suffix="20260711_8h")
            report = crosswalk.build_crosswalk(
                transition_gap_csv=paths["transition"],
                provenance_decision_sheet_csv=old_paths["decision_csv"],
                provenance_decision_sheet_json=old_paths["decision_json"],
                generated_at="2026-07-12T03:00:00+00:00",
                root=root,
            )
            audit = crosswalk.audit_crosswalk(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("source_artifact_suffix_family_mismatch", codes)

    def test_writers_emit_csv_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = crosswalk.build_crosswalk(
                transition_gap_csv=paths["transition"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                root=root,
            )
            csv_path = root / "crosswalk.csv"
            md_path = root / "crosswalk.md"
            audit_md_path = root / "audit.md"
            crosswalk.write_csv(csv_path, report)
            crosswalk.write_markdown(md_path, report)
            crosswalk.write_audit_markdown(
                audit_md_path,
                crosswalk.audit_crosswalk(report, root=root),
            )
            csv_text = csv_path.read_text(encoding="utf-8-sig")
            audit_md_text = audit_md_path.read_text(encoding="utf-8")
            md_text = md_path.read_text(encoding="utf-8")

        self.assertIn("recommended_source_decision_packet_ref", csv_text)
        self.assertIn("operator_source_decision_packet_ref", csv_text)
        self.assertIn("No crosswalk integrity issues found.", audit_md_text)
        self.assertIn("does not set `human_reviewed`", md_text)
        self.assertIn("primary recommended packet ref", md_text)
        self.assertIn("recommended_source_decision_packet_ref", md_text)
        self.assertIn("legacy gap packet exists (non-blocking if primary exists)", md_text)


if __name__ == "__main__":
    unittest.main()
