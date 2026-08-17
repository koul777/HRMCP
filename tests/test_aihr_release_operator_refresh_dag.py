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

from ncs_harness import main as harness_main  # noqa: E402
from ncs_mcp.agent_queue import (  # noqa: E402
    _canonical_json_sha256,
    build_agent_queue_status_from_file,
)
from scripts.build_aihr_release_operator_refresh_dag import (  # noqa: E402
    audit_refresh_dag,
    build_refresh_dag,
    powershell_quote,
    sha256_file,
)


STAMP = "20260712_10h"
GENERATED_AT = "2026-07-12T05:05:00+00:00"


class AihrReleaseOperatorRefreshDagTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict[str, Any] | None = None) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload or {"ok": True}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    def _write_text(self, path: Path, text: str = "artifact\n") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def _write_source_hash_payload(
        self,
        path: Path,
        *,
        schema: str,
        source_paths: dict[str, Path],
        extra: dict[str, Any] | None = None,
    ) -> Path:
        payload = {
            "schema": schema,
            "ok": True,
            "source_paths": {
                key: value.relative_to(path.parents[1]).as_posix()
                for key, value in source_paths.items()
            },
            "source_hashes": {
                key: sha256_file(value)
                for key, value in source_paths.items()
            },
        }
        if extra:
            payload.update(extra)
        return self._write_json(path, payload)

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        quality = self._write_json(
            reports / f"aihr_quality_gates_with_transition_{STAMP}.json",
            {"schema": "quality_gates_v1", "ok": True},
        )
        contract = self._write_json(
            reports / f"mcp_tool_contract_{STAMP}.json",
            {"schema": "mcp_tool_contract_v1", "ok": True},
        )
        demo_json = self._write_json(
            reports / f"aihr_plan_demo_{STAMP}.json",
            {"schema": "aihr_plan_demo_v1", "ok": True},
        )
        demo_html = self._write_text(reports / f"aihr_plan_demo_{STAMP}.html", "<html></html>\n")
        dashboard = self._write_json(
            reports / f"aihr_dashboard_surface_verification_{STAMP}.json",
            {"schema": "aihr_dashboard_surface_verification_v1", "ok": True},
        )
        queue = self._write_json(
            reports / f"aihr_agent_queue_{STAMP}.json",
            {"schema": "aihr_agent_work_queue_v1", "ok": True, "items": []},
        )
        release = self._write_json(
            reports / f"aihr_release_readiness_{STAMP}.json",
            {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "agent_work_queue_path": queue.relative_to(root).as_posix(),
            },
        )
        queue_sha = sha256_file(queue)
        queue_status_payload = build_agent_queue_status_from_file(queue, workspace=root)
        queue_status_snapshot_sha = _canonical_json_sha256(queue_status_payload)
        queue_status_artifact_payload = dict(queue_status_payload)
        queue_status_artifact_payload["out_path"] = str(
            reports / f"aihr_agent_queue_status_{STAMP}.json"
        )
        queue_status_artifact_payload["markdown_path"] = str(
            reports / f"aihr_agent_queue_status_{STAMP}.md"
        )
        queue_status = self._write_json(
            reports / f"aihr_agent_queue_status_{STAMP}.json",
            queue_status_artifact_payload,
        )
        queue_dryrun = self._write_json(
            reports / f"aihr_agent_queue_run_dryrun_{STAMP}.json",
            {
                "schema": "aihr_agent_queue_run_v1",
                "ok": True,
                "dry_run": True,
                "source_queue_path": queue.relative_to(root).as_posix(),
                "source_queue_sha256": queue_sha,
                "queue_status_snapshot_sha256": queue_status_snapshot_sha,
            },
        )
        queue_run = self._write_json(
            reports / f"aihr_agent_queue_run_{STAMP}.json",
            {
                "schema": "aihr_agent_queue_run_v1",
                "ok": True,
                "actual_run": True,
                "source_queue_path": queue.relative_to(root).as_posix(),
                "source_queue_sha256": queue_sha,
                "queue_status_snapshot_sha256": queue_status_snapshot_sha,
                "summary": {"failed_count": 0, "skipped_unsafe_count": 0},
            },
        )
        coverage_plan = self._write_json(reports / f"qualification_collection_coverage_plan_{STAMP}.json")
        retry_hygiene = self._write_json(reports / f"qualification_retry_hygiene_{STAMP}.json")
        qualification = self._write_source_hash_payload(
            reports / f"qualification_guarded_batch_operator_decision_{STAMP}.json",
            schema="qualification_guarded_batch_operator_decision_v1",
            source_paths={
                "coverage_plan": coverage_plan,
                "retry_hygiene": retry_hygiene,
                "release_readiness": release,
                "queue_status": queue_status,
                "queue_run": queue_run,
            },
            extra={"execution_authorized": False},
        )
        qualification_audit = self._write_json(
            reports / f"qualification_guarded_batch_operator_decision_audit_{STAMP}.json",
            {"schema": "qualification_guarded_batch_operator_decision_audit_v1", "ok": True},
        )
        qualification_csv = self._write_text(qualification.with_suffix(".csv"), "wave,batch_count\npilot,3\n")
        transition_gap_json = self._write_json(
            reports / f"transition_trusted_scenario_provenance_gap_{STAMP}.json",
            {"schema": "transition_trusted_scenario_provenance_gap_v1", "ok": True},
        )
        transition_gap_csv = self._write_text(
            reports / f"transition_trusted_scenario_provenance_gap_{STAMP}.csv",
            "scenario_id,audit_id\n3,64\n",
        )
        proofset_log = self._write_text(
            reports / f"command_provenance_reconfirmation_proofset_{STAMP}_after_sheet_timestamp.log",
            "proof ok\n",
        )
        decision_json = self._write_json(
            reports / f"human_review_provenance_reconfirmation_decision_sheet_{STAMP}.json",
            {"schema": "aihr_provenance_reconfirmation_decision_sheet_v1", "ok": True},
        )
        decision_csv = self._write_text(
            reports / f"human_review_provenance_reconfirmation_decision_sheet_{STAMP}.csv",
            "order,surface,target_id,decision\n1,training_transition_gold_scenarios,3,\n",
        )
        decision_audit = self._write_json(
            reports / f"human_review_provenance_reconfirmation_decision_audit_{STAMP}.json",
            {"schema": "aihr_provenance_reconfirmation_decision_audit_v1", "ok": True},
        )
        crosswalk = self._write_source_hash_payload(
            reports / f"transition_provenance_operator_crosswalk_{STAMP}.json",
            schema="transition_provenance_operator_crosswalk_v1",
            source_paths={
                "transition_gap_csv": transition_gap_csv,
                "provenance_decision_sheet_csv": decision_csv,
                "provenance_decision_sheet_json": decision_json,
            },
        )
        crosswalk_csv = self._write_text(crosswalk.with_suffix(".csv"), "scenario_id\n3\n")
        crosswalk_audit = self._write_json(
            reports / f"transition_provenance_operator_crosswalk_audit_{STAMP}.json",
            {"schema": "transition_provenance_operator_crosswalk_audit_v1", "ok": True},
        )
        concept_csv = self._write_text(
            reports / f"aihr_ontology_definition_review_seedpack_{STAMP}.csv",
            "sequence,decision\n1,\n",
        )
        blocker_csv = self._write_text(
            reports / f"aihr_review_seedpack_blocker_ranked_{STAMP}.csv",
            "sequence,decision\n1,\n",
        )
        sprint = self._write_source_hash_payload(
            reports / f"aihr_blocker_reduction_operator_sprint_queue_{STAMP}.json",
            schema="aihr_blocker_reduction_operator_sprint_queue_v1",
            source_paths={
                "concept_seedpack_csv": concept_csv,
                "blocker_ranked_seedpack_csv": blocker_csv,
                "provenance_decision_sheet_csv": decision_csv,
                "transition_trusted_scenario_provenance_gap_csv": transition_gap_csv,
                "qualification_guarded_batch_decision_csv": qualification_csv,
                "transition_provenance_crosswalk_csv": crosswalk_csv,
                "transition_provenance_crosswalk_audit": crosswalk_audit,
            },
            extra={
                "acceptance_contract": {
                    "transition_scenarios_have_decision_sheet_rows": True,
                    "transition_scenario_decision_rows_unique": True,
                }
            },
        )
        sprint_audit = self._write_json(
            reports / f"aihr_blocker_reduction_operator_sprint_queue_audit_{STAMP}.json",
            {"schema": "aihr_blocker_reduction_operator_sprint_queue_audit_v1", "ok": True},
        )
        next_actions = self._write_source_hash_payload(
            reports / f"aihr_operator_next_actions_{STAMP}.json",
            schema="aihr_operator_next_actions_v3",
            source_paths={
                "release_readiness": release,
                "queue_run": queue_run,
                "transition_trusted_scenario_provenance_gap": transition_gap_json,
                "qualification_guarded_batch_operator_decision": qualification,
                "provenance_reconfirmation_proofset_log": proofset_log,
                "blocker_reduction_sprint_queue": sprint,
                "blocker_reduction_sprint_queue_audit": sprint_audit,
                "transition_provenance_crosswalk": crosswalk,
                "transition_provenance_crosswalk_csv": crosswalk_csv,
                "transition_provenance_crosswalk_audit": crosswalk_audit,
            },
        )
        operator_audit = self._write_json(
            reports / f"operator_review_packet_integrity_audit_{STAMP}.json",
            {"schema": "operator_review_packet_integrity_audit_v2", "ok": True, "issue_count": 0},
        )
        handoff = self._write_json(
            reports / f"overnight_10h_operator_handoff_{STAMP}.json",
            {"schema": "overnight_10h_operator_handoff_v3", "ok": True},
        )
        lineage = self._write_json(
            reports / f"operator_report_lineage_sync_audit_{STAMP}.json",
            {"schema": "operator_report_lineage_sync_audit_v1", "ok": True, "issue_count": 0},
        )
        for path in [
            dashboard,
            release,
            queue,
            queue_status,
            queue_dryrun,
            queue_run,
            qualification,
            qualification_audit,
            decision_json,
            decision_audit,
            crosswalk,
            crosswalk_audit,
            sprint,
            sprint_audit,
        ]:
            self._write_text(path.with_suffix(".md"))
        self._write_text(sprint.with_suffix(".csv"), "rank\n1\n")
        return {
            "quality": quality,
            "contract": contract,
            "demo_json": demo_json,
            "demo_html": demo_html,
            "dashboard": dashboard,
            "release": release,
            "queue": queue,
            "queue_status": queue_status,
            "queue_dryrun": queue_dryrun,
            "queue_run": queue_run,
            "coverage_plan": coverage_plan,
            "retry_hygiene": retry_hygiene,
            "qualification": qualification,
            "qualification_audit": qualification_audit,
            "proofset_log": proofset_log,
            "transition_gap_json": transition_gap_json,
            "transition_gap_csv": transition_gap_csv,
            "decision_json": decision_json,
            "decision_csv": decision_csv,
            "decision_audit": decision_audit,
            "crosswalk": crosswalk,
            "crosswalk_csv": crosswalk_csv,
            "crosswalk_audit": crosswalk_audit,
            "sprint": sprint,
            "sprint_audit": sprint_audit,
            "next_actions": next_actions,
            "operator_audit": operator_audit,
            "handoff": handoff,
            "lineage": lineage,
        }

    def test_powershell_quote_handles_spaces_and_single_quotes(self) -> None:
        self.assertEqual(
            powershell_quote(Path("C:/tmp/workspace with space/reports/out.json")),
            "'C:\\tmp\\workspace with space\\reports\\out.json'",
        )
        self.assertEqual(
            powershell_quote(Path("C:/tmp/O'Brien/reports/out.json")),
            "'C:\\tmp\\O''Brien\\reports\\out.json'",
        )

    def test_build_refresh_dag_records_order_and_hash_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_refresh_dag(
                quality_report=paths["quality"],
                contract=paths["contract"],
                demo_json=paths["demo_json"],
                demo_html=paths["demo_html"],
                dashboard_verification=paths["dashboard"],
                release_readiness=paths["release"],
                agent_queue=paths["queue"],
                queue_status=paths["queue_status"],
                queue_run_dryrun=paths["queue_dryrun"],
                queue_run=paths["queue_run"],
                qualification_coverage_plan=paths["coverage_plan"],
                qualification_retry_hygiene=paths["retry_hygiene"],
                qualification_decision=paths["qualification"],
                qualification_decision_audit=paths["qualification_audit"],
                provenance_proofset_log=paths["proofset_log"],
                transition_gap_json=paths["transition_gap_json"],
                transition_gap_csv=paths["transition_gap_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit=paths["decision_audit"],
                transition_crosswalk_json=paths["crosswalk"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit=paths["crosswalk_audit"],
                sprint_queue=paths["sprint"],
                sprint_queue_audit=paths["sprint_audit"],
                next_actions=paths["next_actions"],
                operator_packet_integrity_audit=paths["operator_audit"],
                handoff=paths["handoff"],
                lineage_audit=paths["lineage"],
                generated_at=GENERATED_AT,
                root=root,
                stamp=STAMP,
            )
            audit = audit_refresh_dag(report, root=root)

        self.assertEqual(report["schema"], "aihr_release_operator_refresh_dag_v1")
        self.assertEqual([node["id"] for node in report["nodes"]][-1], "operator-handoff-bundle")
        for node_id in ("queue-preflight", "queue-dryrun", "queue-run"):
            queue_node = next(node for node in report["nodes"] if node["id"] == node_id)
            self.assertIn("--root", queue_node["command"])
        handoff_node = next(node for node in report["nodes"] if node["id"] == "operator-handoff-bundle")
        for required_arg in (
            "--next-actions-markdown-out",
            "--operator-audit-markdown-out",
            "--handoff-markdown-out",
            "--lineage-audit-markdown-out",
        ):
            self.assertIn(required_arg, handoff_node["command"])
        qualification_node = next(
            node for node in report["nodes"] if node["id"] == "qualification-operator-decision"
        )
        for required_arg in (
            "--coverage-plan",
            "--retry-hygiene",
            "--root",
            "--stamp",
        ):
            self.assertIn(required_arg, qualification_node["command"])
        self.assertIn("qualification_coverage_plan", report["source_paths"])
        self.assertIn("qualification_retry_hygiene", report["source_paths"])
        self.assertTrue(report["dag_contract"]["same_stamp_family_ok"])
        self.assertTrue(
            report["embedded_source_hash_checks"]["next_actions"][
                "provenance_reconfirmation_proofset_log"
            ]["hash_matches"]
        )
        self.assertTrue(report["queue_hash_checks"]["queue_run"]["hash_matches"])
        self.assertTrue(
            report["queue_hash_checks"]["queue_run"]["source_queue_path_matches_expected"]
        )
        self.assertTrue(
            report["queue_status_snapshot_checks"]["queue_run"]["artifact_matches_current"]
        )
        self.assertTrue(audit["ok"])

    def test_build_refresh_dag_quotes_operator_commands_for_spaced_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace with space"
            paths = self._fixture(root)
            report = build_refresh_dag(
                quality_report=paths["quality"],
                contract=paths["contract"],
                demo_json=paths["demo_json"],
                demo_html=paths["demo_html"],
                dashboard_verification=paths["dashboard"],
                release_readiness=paths["release"],
                agent_queue=paths["queue"],
                queue_status=paths["queue_status"],
                queue_run_dryrun=paths["queue_dryrun"],
                queue_run=paths["queue_run"],
                qualification_coverage_plan=paths["coverage_plan"],
                qualification_retry_hygiene=paths["retry_hygiene"],
                qualification_decision=paths["qualification"],
                qualification_decision_audit=paths["qualification_audit"],
                provenance_proofset_log=paths["proofset_log"],
                transition_gap_json=paths["transition_gap_json"],
                transition_gap_csv=paths["transition_gap_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit=paths["decision_audit"],
                transition_crosswalk_json=paths["crosswalk"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit=paths["crosswalk_audit"],
                sprint_queue=paths["sprint"],
                sprint_queue_audit=paths["sprint_audit"],
                next_actions=paths["next_actions"],
                operator_packet_integrity_audit=paths["operator_audit"],
                handoff=paths["handoff"],
                lineage_audit=paths["lineage"],
                generated_at=GENERATED_AT,
                root=root,
                stamp=STAMP,
            )

        quoted_root = powershell_quote(root)
        for node_id in ("queue-preflight", "queue-dryrun", "queue-run", "qualification-operator-decision"):
            command = next(node["command"] for node in report["nodes"] if node["id"] == node_id)
            self.assertIn(f"--root {quoted_root}", command)
        release_seed = next(node["command"] for node in report["nodes"] if node["id"] == "release-seed")
        self.assertIn(powershell_quote(paths["release"]), release_seed)

    def test_audit_refresh_dag_flags_stale_current_artifact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_refresh_dag(
                quality_report=paths["quality"],
                contract=paths["contract"],
                demo_json=paths["demo_json"],
                demo_html=paths["demo_html"],
                dashboard_verification=paths["dashboard"],
                release_readiness=paths["release"],
                agent_queue=paths["queue"],
                queue_status=paths["queue_status"],
                queue_run_dryrun=paths["queue_dryrun"],
                queue_run=paths["queue_run"],
                qualification_coverage_plan=paths["coverage_plan"],
                qualification_retry_hygiene=paths["retry_hygiene"],
                qualification_decision=paths["qualification"],
                qualification_decision_audit=paths["qualification_audit"],
                provenance_proofset_log=paths["proofset_log"],
                transition_gap_json=paths["transition_gap_json"],
                transition_gap_csv=paths["transition_gap_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit=paths["decision_audit"],
                transition_crosswalk_json=paths["crosswalk"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit=paths["crosswalk_audit"],
                sprint_queue=paths["sprint"],
                sprint_queue_audit=paths["sprint_audit"],
                next_actions=paths["next_actions"],
                operator_packet_integrity_audit=paths["operator_audit"],
                handoff=paths["handoff"],
                lineage_audit=paths["lineage"],
                generated_at=GENERATED_AT,
                root=root,
                stamp=STAMP,
            )
            paths["queue"].write_text('{"changed": true}\n', encoding="utf-8")
            audit = audit_refresh_dag(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("current_artifact_hash_mismatch", codes)

    def test_audit_refresh_dag_flags_foreign_queue_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_refresh_dag(
                quality_report=paths["quality"],
                contract=paths["contract"],
                demo_json=paths["demo_json"],
                demo_html=paths["demo_html"],
                dashboard_verification=paths["dashboard"],
                release_readiness=paths["release"],
                agent_queue=paths["queue"],
                queue_status=paths["queue_status"],
                queue_run_dryrun=paths["queue_dryrun"],
                queue_run=paths["queue_run"],
                qualification_coverage_plan=paths["coverage_plan"],
                qualification_retry_hygiene=paths["retry_hygiene"],
                qualification_decision=paths["qualification"],
                qualification_decision_audit=paths["qualification_audit"],
                provenance_proofset_log=paths["proofset_log"],
                transition_gap_json=paths["transition_gap_json"],
                transition_gap_csv=paths["transition_gap_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit=paths["decision_audit"],
                transition_crosswalk_json=paths["crosswalk"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit=paths["crosswalk_audit"],
                sprint_queue=paths["sprint"],
                sprint_queue_audit=paths["sprint_audit"],
                next_actions=paths["next_actions"],
                operator_packet_integrity_audit=paths["operator_audit"],
                handoff=paths["handoff"],
                lineage_audit=paths["lineage"],
                generated_at=GENERATED_AT,
                root=root,
                stamp=STAMP,
            )
            report["queue_hash_checks"]["queue_run"]["source_queue_path_matches_expected"] = False
            audit = audit_refresh_dag(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("queue_run_source_queue_path_mismatch", codes)

    def test_audit_refresh_dag_flags_stale_queue_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            report = build_refresh_dag(
                quality_report=paths["quality"],
                contract=paths["contract"],
                demo_json=paths["demo_json"],
                demo_html=paths["demo_html"],
                dashboard_verification=paths["dashboard"],
                release_readiness=paths["release"],
                agent_queue=paths["queue"],
                queue_status=paths["queue_status"],
                queue_run_dryrun=paths["queue_dryrun"],
                queue_run=paths["queue_run"],
                qualification_coverage_plan=paths["coverage_plan"],
                qualification_retry_hygiene=paths["retry_hygiene"],
                qualification_decision=paths["qualification"],
                qualification_decision_audit=paths["qualification_audit"],
                provenance_proofset_log=paths["proofset_log"],
                transition_gap_json=paths["transition_gap_json"],
                transition_gap_csv=paths["transition_gap_csv"],
                provenance_decision_sheet_json=paths["decision_json"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit=paths["decision_audit"],
                transition_crosswalk_json=paths["crosswalk"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit=paths["crosswalk_audit"],
                sprint_queue=paths["sprint"],
                sprint_queue_audit=paths["sprint_audit"],
                next_actions=paths["next_actions"],
                operator_packet_integrity_audit=paths["operator_audit"],
                handoff=paths["handoff"],
                lineage_audit=paths["lineage"],
                generated_at=GENERATED_AT,
                root=root,
                stamp=STAMP,
            )
            report["queue_status_snapshot_checks"]["queue_run"]["artifact_matches_declared"] = False
            audit = audit_refresh_dag(report, root=root)

        codes = {issue["code"] for issue in audit["issues"]}
        self.assertFalse(audit["ok"])
        self.assertIn("queue_status_snapshot_artifact_matches_declared_not_true", codes)

    def test_harness_command_auto_discovers_temp_root_artifacts(self) -> None:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._fixture(root)
            reports = root / "reports"
            out = reports / f"aihr_release_operator_refresh_dag_{STAMP}.json"
            audit_out = reports / f"aihr_release_operator_refresh_dag_audit_{STAMP}.json"
            sys.argv = [
                "ncs_harness.py",
                "build-aihr-release-operator-refresh-dag",
                "--root",
                str(root),
                "--stamp",
                STAMP,
                "--out",
                str(out),
                "--markdown-out",
                str(out.with_suffix(".md")),
                "--audit-out",
                str(audit_out),
                "--audit-markdown-out",
                str(audit_out.with_suffix(".md")),
                "--strict",
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    harness_main()
            finally:
                sys.argv = previous_argv
            payload = json.loads(stdout.getvalue())
            report = json.loads(out.read_text(encoding="utf-8"))
            markdown = out.with_suffix(".md").read_text(encoding="utf-8")

        self.assertTrue(payload["ok"])
        self.assertTrue(payload["audit_ok"])
        self.assertEqual(report["source_paths"]["agent_queue"], f"reports/aihr_agent_queue_{STAMP}.json")
        self.assertIn("## Current Artifacts", markdown)
        self.assertIn("## Source Hashes", markdown)
        self.assertIn("## Embedded Source Hash Checks", markdown)
        self.assertIn("## Queue Hash Checks", markdown)
        self.assertIn("## Queue Status Snapshot Checks", markdown)
        self.assertIn("## Operator Audit Status", markdown)
        self.assertIn("sha256:", markdown)
        self.assertIn("operator_packet_integrity_ok", markdown)


if __name__ == "__main__":
    unittest.main()
