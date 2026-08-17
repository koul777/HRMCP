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
from scripts.build_aihr_operator_handoff_bundle import (  # noqa: E402
    NEXT_ACTION_SOURCE_KEYS,
    build_bundle,
    sha256_file,
)


STAMP = "20260712_10h"
GENERATED_AT = "2026-07-12T04:20:00+00:00"


class AihrOperatorHandoffBundleTests(unittest.TestCase):
    def _write_json(self, path: Path, payload: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    def _write_json_with_md(
        self,
        path: Path,
        schema: str,
        *,
        extra: dict | None = None,
    ) -> Path:
        payload = {
            "schema": schema,
            "generated_at": GENERATED_AT,
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
        }
        if extra:
            payload.update(extra)
        self._write_json(path, payload)
        path.with_suffix(".md").write_text(
            f"# {schema}\n\n- generated_at: `{payload.get('generated_at')}`\n",
            encoding="utf-8",
        )
        return path

    def _write_decision_csv(self, path: Path, *, row_count: int = 1) -> Path:
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "id",
                    "decision",
                    "reviewer_id",
                    "reviewed_at",
                    "rationale",
                    "source_decision_packet",
                    "evidence_refs_json",
                    "status_update_allowed",
                    "db_writes",
                    "approval_claim",
                ],
            )
            writer.writeheader()
            for index in range(row_count):
                writer.writerow(
                    {
                        "id": str(index + 1),
                        "status_update_allowed": "false",
                        "db_writes": "false",
                        "approval_claim": "false",
                    }
                )
        return path

    def _fixture(self, root: Path) -> dict[str, Path]:
        reports = root / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        packet = reports / "human_review_provenance_reconfirmation_packet_20260712_10h.json"
        packet.write_text("{}\n", encoding="utf-8")
        packet_hash = sha256_file(packet)

        concept_csv = reports / f"aihr_ontology_definition_review_seedpack_{STAMP}.csv"
        blocker_csv = reports / f"aihr_review_seedpack_blocker_ranked_{STAMP}.csv"
        provenance_csv = reports / f"human_review_provenance_reconfirmation_decision_sheet_{STAMP}.csv"
        self._write_decision_csv(concept_csv, row_count=2)
        self._write_decision_csv(blocker_csv, row_count=2)
        self._write_decision_csv(provenance_csv, row_count=2)
        for path in (
            concept_csv.with_suffix(".md"),
            concept_csv.with_suffix(".jsonl"),
            blocker_csv.with_suffix(".md"),
            blocker_csv.with_suffix(".jsonl"),
            provenance_csv.with_suffix(".md"),
            reports / f"aihr_review_triage_{STAMP}.md",
            reports / f"aihr_review_triage_{STAMP}.json",
            reports / f"aihr_transition_scenario_seedpack_{STAMP}.md",
            reports / f"aihr_transition_scenario_seedpack_{STAMP}.jsonl",
            reports / f"qualification_retry_hygiene_{STAMP}.md",
            reports / f"qualification_collection_coverage_plan_{STAMP}.md",
            reports / f"qualification_collection_coverage_plan_{STAMP}.csv",
            reports / f"human_review_provenance_reconfirmation_packet_{STAMP}.md",
            reports / f"human_review_provenance_reconfirmation_decision_audit_{STAMP}.md",
        ):
            path.write_text("sidecar\n", encoding="utf-8")

        transition_gap_csv = reports / f"transition_trusted_scenario_provenance_gap_{STAMP}.csv"
        with transition_gap_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["scenario_id", "audit_id", "gap_fields"])
            writer.writeheader()
            writer.writerow({"scenario_id": "3", "audit_id": "64", "gap_fields": "source"})

        crosswalk_csv = reports / f"transition_provenance_operator_crosswalk_{STAMP}.csv"
        with crosswalk_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "scenario_id",
                    "decision_sheet_order",
                    "operator_source_decision_packet_ref",
                    "operator_source_artifact_hash",
                    "operator_decision_fields_blank",
                    "operator_guard_fields_false",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "scenario_id": "3",
                    "decision_sheet_order": "1",
                    "operator_source_decision_packet_ref": (
                        f"reports/human_review_provenance_reconfirmation_packet_{STAMP}.json#order:1"
                    ),
                    "operator_source_artifact_hash": packet_hash,
                    "operator_decision_fields_blank": "true",
                    "operator_guard_fields_false": "true",
                }
            )
        crosswalk_csv.with_suffix(".md").write_text("crosswalk\n", encoding="utf-8")

        qualification_csv = reports / f"qualification_guarded_batch_operator_decision_{STAMP}.csv"
        with qualification_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["wave", "batch_count"])
            writer.writeheader()
            writer.writerow({"wave": "pilot", "batch_count": "3"})

        release = self._write_json(
            reports / f"aihr_release_readiness_{STAMP}.json",
            {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "sha256_scope": "cycle_safe_release_readiness",
                "cycle_safe_content_sha256": "sha256:" + "f" * 64,
                "release_ready": False,
                "blocker_count": 6,
                "warning_count": 0,
                "inputs": {
                    "quality_status": "warn",
                    "quality_summary": {"pass_count": 1, "warn_count": 1, "fail_count": 0},
                },
                "blockers": [
                    {
                        "name": blocker,
                        "category": "human_review" if "qualification" not in blocker else "data_collection",
                        "message": f"{blocker} blocked",
                        "value": 0,
                        "threshold": "> 0",
                    }
                    for blocker in [
                        "review_debt:human_reviewed_concepts",
                        "review_debt:human_reviewed_goal_links",
                        "review_debt:human_reviewed_task_relations",
                        "qualification:collection_coverage",
                        "transition_eval:trusted_scenarios",
                        "human_review:provenance_reconfirmation_required",
                    ]
                ],
                "next_actions": [
                    {
                        "blocker": blocker,
                        "owner": "operator",
                        "action": f"act on {blocker}",
                        "command": f"python scripts\\ncs_harness.py noop --blocker {blocker}",
                    }
                    for blocker in [
                        "review_debt:human_reviewed_concepts",
                        "review_debt:human_reviewed_goal_links",
                        "review_debt:human_reviewed_task_relations",
                        "qualification:collection_coverage",
                        "transition_eval:trusted_scenarios",
                        "human_review:provenance_reconfirmation_required",
                    ]
                ],
            },
        )
        quality = self._write_json(reports / f"aihr_quality_gates_with_transition_{STAMP}.json", {"status": "warn"})
        dashboard = self._write_json(reports / f"aihr_dashboard_surface_verification_{STAMP}.json", {"ok": True})
        queue_run = self._write_json(
            reports / f"aihr_agent_queue_run_{STAMP}.json",
            {"schema": "aihr_agent_queue_run_v1", "summary": {"acceptance_unverified_count": 3}},
        )
        proof_log = reports / f"command_provenance_reconfirmation_proofset_{STAMP}.log"
        proof_log.write_text("proofset ok\n", encoding="utf-8")

        transition_gap_json = self._write_json_with_md(
            reports / f"transition_trusted_scenario_provenance_gap_{STAMP}.json",
            "transition_trusted_scenario_provenance_gap_v1",
            extra={
                "scenario_count": 1,
                "scenario_gap_count": 1,
                "ready_packet_backed_scenario_count": 0,
            },
        )
        qualification_json = self._write_json_with_md(
            reports / f"qualification_guarded_batch_operator_decision_{STAMP}.json",
            "qualification_guarded_batch_operator_decision_v1",
            extra={
                "execution_authorized": False,
                "automatic_queue_execution_allowed": False,
                "coverage_state": {"collection_coverage": 0.5},
                "batch_summary": {
                    "batch_count": 3,
                    "additional_attempted_units_needed": 120,
                },
            },
        )
        qualification_audit = self._write_json_with_md(
            reports / f"qualification_guarded_batch_operator_decision_audit_{STAMP}.json",
            "qualification_guarded_batch_operator_decision_audit_v1",
        )
        crosswalk_json = self._write_json_with_md(
            reports / f"transition_provenance_operator_crosswalk_{STAMP}.json",
            "transition_provenance_operator_crosswalk_v1",
        )
        crosswalk_audit = self._write_json_with_md(
            reports / f"transition_provenance_operator_crosswalk_audit_{STAMP}.json",
            "transition_provenance_operator_crosswalk_audit_v1",
            extra={"warning_count": 0},
        )
        queue_json = self._write_json_with_md(
            reports / f"aihr_blocker_reduction_operator_sprint_queue_{STAMP}.json",
            "aihr_blocker_reduction_operator_sprint_queue_v1",
            extra={
                "queue": [
                    {
                        "sprint_id": "S1-transition-provenance-crosswalk",
                        "open_first": f"reports/transition_provenance_operator_crosswalk_{STAMP}.csv",
                    }
                ]
            },
        )
        queue_audit = self._write_json_with_md(
            reports / f"aihr_blocker_reduction_operator_sprint_queue_audit_{STAMP}.json",
            "aihr_blocker_reduction_operator_sprint_queue_audit_v1",
        )
        decision_json = self._write_json_with_md(
            reports / f"human_review_provenance_reconfirmation_decision_sheet_{STAMP}.json",
            "aihr_provenance_reconfirmation_decision_sheet_v1",
            extra={
                "created_at": GENERATED_AT,
                "generated_at": GENERATED_AT,
                "content_sha256_excluding_self_hash": "sha256:" + "a" * 64,
            },
        )
        decision_audit = self._write_json_with_md(
            reports / f"human_review_provenance_reconfirmation_decision_audit_{STAMP}.json",
            "aihr_provenance_reconfirmation_decision_audit_v1",
        )

        return {
            "release": release,
            "quality": quality,
            "dashboard": dashboard,
            "queue_run": queue_run,
            "proof_log": proof_log,
            "transition_gap_json": transition_gap_json,
            "qualification_json": qualification_json,
            "qualification_audit": qualification_audit,
            "qualification_csv": qualification_csv,
            "crosswalk_json": crosswalk_json,
            "crosswalk_csv": crosswalk_csv,
            "crosswalk_audit": crosswalk_audit,
            "queue_json": queue_json,
            "queue_audit": queue_audit,
            "decision_json": decision_json,
            "decision_csv": provenance_csv,
            "decision_audit": decision_audit,
            "concept_csv": concept_csv,
            "blocker_csv": blocker_csv,
        }

    def test_build_bundle_keeps_next_actions_acyclic_and_lineage_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            reports = root / "reports"
            next_json = reports / f"aihr_operator_next_actions_{STAMP}.json"
            next_md = reports / f"aihr_operator_next_actions_{STAMP}.md"
            integrity_json = reports / f"operator_review_packet_integrity_audit_{STAMP}.json"
            integrity_md = reports / f"operator_review_packet_integrity_audit_{STAMP}.md"
            handoff_json = reports / f"overnight_10h_operator_handoff_{STAMP}.json"
            handoff_md = reports / f"overnight_10h_operator_handoff_{STAMP}.md"
            lineage_json = reports / f"operator_report_lineage_sync_audit_{STAMP}.json"
            lineage_md = reports / f"operator_report_lineage_sync_audit_{STAMP}.md"

            result = build_bundle(
                release_readiness_path=paths["release"],
                queue_run_path=paths["queue_run"],
                transition_gap_json=paths["transition_gap_json"],
                qualification_decision_json=paths["qualification_json"],
                provenance_proofset_log=paths["proof_log"],
                blocker_sprint_queue_json=paths["queue_json"],
                blocker_sprint_queue_audit_json=paths["queue_audit"],
                transition_crosswalk_json=paths["crosswalk_json"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit_json=paths["crosswalk_audit"],
                provenance_decision_sheet_json=paths["decision_json"],
                concept_seedpack_csv=paths["concept_csv"],
                blocker_ranked_seedpack_csv=paths["blocker_csv"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit_json=paths["decision_audit"],
                qualification_decision_csv=paths["qualification_csv"],
                qualification_decision_audit_json=paths["qualification_audit"],
                quality_report_path=paths["quality"],
                dashboard_verification_path=paths["dashboard"],
                next_actions_out=next_json,
                next_actions_markdown_out=next_md,
                operator_audit_out=integrity_json,
                operator_audit_markdown_out=integrity_md,
                handoff_out=handoff_json,
                handoff_markdown_out=handoff_md,
                lineage_audit_out=lineage_json,
                lineage_audit_markdown_out=lineage_md,
                stamp=STAMP,
                root=root,
                generated_at=GENERATED_AT,
            )

            next_actions = json.loads(next_json.read_text(encoding="utf-8"))
            integrity = json.loads(integrity_json.read_text(encoding="utf-8"))
            handoff = json.loads(handoff_json.read_text(encoding="utf-8"))
            lineage = json.loads(lineage_json.read_text(encoding="utf-8"))

            self.assertTrue(result["ok"])
            self.assertTrue(integrity["ok"])
            self.assertTrue(lineage["ok"])
            self.assertEqual(set(NEXT_ACTION_SOURCE_KEYS), set(next_actions["source_paths"]))
            self.assertEqual(
                "sha256:" + "f" * 64,
                next_actions["source_hashes"]["release_readiness"],
            )
            self.assertEqual(
                "cycle_safe_release_readiness",
                next_actions["source_hash_scopes"]["release_readiness"],
            )
            self.assertTrue(next_actions["operator_packet_integrity_ok"])
            self.assertEqual(
                f"reports/operator_review_packet_integrity_audit_{STAMP}.json",
                next_actions["operator_packet_integrity_path"],
            )
            for action in next_actions["actions"]:
                self.assertTrue(action["report_only"])
                self.assertFalse(action["status_update_allowed"])
                self.assertFalse(action["db_writes"])
                self.assertFalse(action["api_calls"])
                self.assertFalse(action["approval_claim"])
                self.assertTrue(action["human_decision_required"])
                self.assertEqual(
                    ["human_reviewed", "accepted", "reviewed"],
                    action["forbidden_automatic_statuses"],
                )
            by_id = {action["id"]: action for action in next_actions["actions"]}
            self.assertEqual(
                f"reports/aihr_review_seedpack_blocker_ranked_{STAMP}.csv",
                by_id["review_debt:human_reviewed_goal_links"]["open_first"],
            )
            self.assertIn(
                f"reports/aihr_review_triage_{STAMP}.md",
                by_id["review_debt:human_reviewed_goal_links"]["artifacts_to_open"],
            )
            self.assertEqual(
                f"reports/qualification_guarded_batch_operator_decision_{STAMP}.csv",
                by_id["qualification:collection_coverage"]["open_first"],
            )
            self.assertIn(
                f"reports/qualification_guarded_batch_operator_decision_{STAMP}.md",
                by_id["qualification:collection_coverage"]["artifacts_to_open"],
            )
            forbidden_source_tokens = (
                "operator_review_packet_integrity",
                "operator_handoff",
                "lineage",
            )
            self.assertFalse(
                any(
                    token in str(path)
                    for path in next_actions["source_paths"].values()
                    for token in forbidden_source_tokens
                )
            )
            self.assertEqual(handoff["operator_next_actions"]["sha256"], sha256_file(next_json))
            self.assertEqual(
                handoff["operator_packet_integrity_audit"]["sha256"],
                sha256_file(integrity_json),
            )
            canonical = {
                item["path"]: item
                for item in handoff["canonical_artifacts"]
                if isinstance(item, dict) and item.get("path")
            }
            self.assertEqual(
                canonical[f"reports/aihr_operator_next_actions_{STAMP}.json"]["sha256"],
                sha256_file(next_json),
            )
            self.assertEqual(
                handoff["qualification_guarded_batch_decision"]["audit_sha256"],
                sha256_file(paths["qualification_audit"]),
            )
            self.assertNotIn("operator_report_lineage_sync_audit", json.dumps(handoff))
            self.assertIn(
                f"- generated_at: `{GENERATED_AT}`",
                next_md.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "- human_decision_required: `True`",
                next_md.read_text(encoding="utf-8"),
            )
            self.assertIn("sha256=`sha256:", next_md.read_text(encoding="utf-8"))
            handoff_text = handoff_md.read_text(encoding="utf-8")
            self.assertIn("## Canonical Artifacts", handoff_text)
            self.assertIn("## Verification Logs", handoff_text)
            self.assertIn("- human_decision_required: `True`", handoff_text)

    def test_harness_command_writes_bundle_outputs(self) -> None:
        previous_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            reports = root / "reports"
            next_json = reports / f"aihr_operator_next_actions_{STAMP}.json"
            next_md = reports / f"aihr_operator_next_actions_{STAMP}.md"
            integrity_json = reports / f"operator_review_packet_integrity_audit_{STAMP}.json"
            integrity_md = reports / f"operator_review_packet_integrity_audit_{STAMP}.md"
            handoff_json = reports / f"overnight_10h_operator_handoff_{STAMP}.json"
            handoff_md = reports / f"overnight_10h_operator_handoff_{STAMP}.md"
            lineage_json = reports / f"operator_report_lineage_sync_audit_{STAMP}.json"
            lineage_md = reports / f"operator_report_lineage_sync_audit_{STAMP}.md"
            sys.argv = [
                "ncs_harness.py",
                "build-aihr-operator-handoff-bundle",
                "--root",
                str(root),
                "--stamp",
                STAMP,
                "--release-readiness",
                str(paths["release"]),
                "--queue-run",
                str(paths["queue_run"]),
                "--transition-gap-json",
                str(paths["transition_gap_json"]),
                "--qualification-decision-json",
                str(paths["qualification_json"]),
                "--provenance-proofset-log",
                str(paths["proof_log"]),
                "--blocker-sprint-queue-json",
                str(paths["queue_json"]),
                "--blocker-sprint-queue-audit-json",
                str(paths["queue_audit"]),
                "--transition-crosswalk-json",
                str(paths["crosswalk_json"]),
                "--transition-crosswalk-csv",
                str(paths["crosswalk_csv"]),
                "--transition-crosswalk-audit-json",
                str(paths["crosswalk_audit"]),
                "--provenance-decision-sheet-json",
                str(paths["decision_json"]),
                "--provenance-decision-sheet-csv",
                str(paths["decision_csv"]),
                "--provenance-decision-audit-json",
                str(paths["decision_audit"]),
                "--concept-seedpack-csv",
                str(paths["concept_csv"]),
                "--blocker-ranked-seedpack-csv",
                str(paths["blocker_csv"]),
                "--qualification-decision-csv",
                str(paths["qualification_csv"]),
                "--qualification-decision-audit-json",
                str(paths["qualification_audit"]),
                "--quality-report",
                str(paths["quality"]),
                "--dashboard-verification",
                str(paths["dashboard"]),
                "--next-actions-out",
                str(next_json),
                "--next-actions-markdown-out",
                str(next_md),
                "--operator-audit-out",
                str(integrity_json),
                "--operator-audit-markdown-out",
                str(integrity_md),
                "--handoff-out",
                str(handoff_json),
                "--handoff-markdown-out",
                str(handoff_md),
                "--lineage-audit-out",
                str(lineage_json),
                "--lineage-audit-markdown-out",
                str(lineage_md),
                "--strict",
            ]
            try:
                with contextlib.redirect_stdout(stdout):
                    harness_main()
            finally:
                sys.argv = previous_argv
            payload = json.loads(stdout.getvalue())
            handoff = json.loads(handoff_json.read_text(encoding="utf-8"))
            lineage = json.loads(lineage_json.read_text(encoding="utf-8"))

            self.assertTrue(payload["ok"])
            self.assertTrue(lineage["ok"])
            self.assertEqual(handoff["schema"], "overnight_10h_operator_handoff_v3")
            self.assertEqual(payload["next_actions_sha256"], sha256_file(next_json))

    def test_build_bundle_uses_explicit_nonstandard_qualification_audit_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._fixture(root)
            reports = root / "reports"
            nonstandard_audit = reports / "custom_qualification_packet_audit.json"
            nonstandard_audit.write_text(
                json.dumps(
                    {
                        "schema": "qualification_guarded_batch_operator_decision_audit_v1",
                        "ok": True,
                        "issue_count": 0,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            nonstandard_audit.with_suffix(".md").write_text("custom audit\n", encoding="utf-8")
            nonstandard_audit_sha256 = sha256_file(nonstandard_audit)
            next_json = reports / f"aihr_operator_next_actions_{STAMP}.json"
            next_md = reports / f"aihr_operator_next_actions_{STAMP}.md"
            integrity_json = reports / f"operator_review_packet_integrity_audit_{STAMP}.json"
            integrity_md = reports / f"operator_review_packet_integrity_audit_{STAMP}.md"
            handoff_json = reports / f"overnight_10h_operator_handoff_{STAMP}.json"
            handoff_md = reports / f"overnight_10h_operator_handoff_{STAMP}.md"
            lineage_json = reports / f"operator_report_lineage_sync_audit_{STAMP}.json"
            lineage_md = reports / f"operator_report_lineage_sync_audit_{STAMP}.md"

            build_bundle(
                release_readiness_path=paths["release"],
                queue_run_path=paths["queue_run"],
                transition_gap_json=paths["transition_gap_json"],
                qualification_decision_json=paths["qualification_json"],
                provenance_proofset_log=paths["proof_log"],
                blocker_sprint_queue_json=paths["queue_json"],
                blocker_sprint_queue_audit_json=paths["queue_audit"],
                transition_crosswalk_json=paths["crosswalk_json"],
                transition_crosswalk_csv=paths["crosswalk_csv"],
                transition_crosswalk_audit_json=paths["crosswalk_audit"],
                provenance_decision_sheet_json=paths["decision_json"],
                concept_seedpack_csv=paths["concept_csv"],
                blocker_ranked_seedpack_csv=paths["blocker_csv"],
                provenance_decision_sheet_csv=paths["decision_csv"],
                provenance_decision_audit_json=paths["decision_audit"],
                qualification_decision_csv=paths["qualification_csv"],
                qualification_decision_audit_json=nonstandard_audit,
                quality_report_path=paths["quality"],
                dashboard_verification_path=paths["dashboard"],
                next_actions_out=next_json,
                next_actions_markdown_out=next_md,
                operator_audit_out=integrity_json,
                operator_audit_markdown_out=integrity_md,
                handoff_out=handoff_json,
                handoff_markdown_out=handoff_md,
                lineage_audit_out=lineage_json,
                lineage_audit_markdown_out=lineage_md,
                stamp=STAMP,
                root=root,
                generated_at=GENERATED_AT,
            )

            handoff = json.loads(handoff_json.read_text(encoding="utf-8"))

        self.assertEqual(
            handoff["qualification_guarded_batch_decision"]["audit_path"],
            "reports/custom_qualification_packet_audit.json",
        )
        self.assertEqual(
            handoff["qualification_guarded_batch_decision"]["audit_sha256"],
            nonstandard_audit_sha256,
        )


if __name__ == "__main__":
    unittest.main()
