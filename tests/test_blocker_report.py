from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.blocker_report import (
    DEFAULT_HUMAN_REVIEW_BACKLOG_PATH,
    DEFAULT_REMAINING_BLOCKERS_PATH,
    _audit_review_seedpack,
    _ksa_immutability_audit_summary,
    _latest_report_path,
    _queue_snapshot_from_report,
    _release_family_artifact_path,
    _release_readiness_cycle_safe_sha256,
    build_goal_completion_audit_report_from_files,
    build_human_review_backlog_report_from_files,
    build_remaining_blockers_report_from_files,
    latest_ksa_definition_operator_packet_path,
    write_goal_completion_audit_markdown,
    write_human_review_backlog_markdown,
    write_remaining_blockers_markdown,
)


def _write_ksa_definition_packet_fixture(
    path: Path,
    *,
    source_payload_exposed: bool = False,
    action_plan_action_count: int = 0,
    include_first_review_queue: bool = False,
) -> dict[str, Path]:
    decision_audit_path = path.with_name(f"{path.stem}_decision_audit.json")
    action_plan_path = path.with_name(f"{path.stem}_action_plan.json")
    priority_pack_reference = "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.json"
    priority_csv_reference = "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.csv"
    decision_audit_path.write_text(
        json.dumps(
            {
                "schema": "ncs_ksa_definition_review_decision_audit_v1",
                "ok": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "trusted_status_write_allowed": False,
                "completed_decision_count": 0,
                "pending_decision_count": 25,
                "invalid_decision_count": 0,
                "unsafe_flag_count": 0,
                "source_mismatch_count": 0,
                "action_eligible_count": 0,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    action_plan_path.write_text(
        json.dumps(
            {
                "schema": "ncs_ksa_definition_review_action_plan_v1",
                "ok": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "trusted_status_write_allowed": False,
                "blocked_by_invalid_audit": False,
                "action_count": action_plan_action_count,
                "actions": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    first_review_queue = []
    if include_first_review_queue:
        first_review_queue = [
            {
                "concept_id": 1,
                "concept_name": "?덉쟾?섏튃 以??",
                "concept_type": "attitude",
                "recommended_review_action": "draft_for_human_review_only",
            }
        ]
    path.write_text(
        json.dumps(
            {
                "schema": "ncs_ksa_definition_review_operator_packet_v1",
                "ok": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "source_payload_exposed": source_payload_exposed,
                "trusted_status_write_allowed": False,
                "raw_source_mutation_allowed": False,
                "summary": {
                    "review_pack_row_count": 25,
                    "review_csv_record_count": 25,
                    "decision_blank_count": 25,
                    "pending_decision_count": 25,
                    "completed_decision_count": 0,
                    "invalid_decision_count": 0,
                    "action_plan_action_count": action_plan_action_count,
                    "draft_definition_candidate_count": 25,
                    "priority_report_row_count": 25,
                    "first_review_queue": first_review_queue,
                },
                "artifacts": {
                    "priority_review_csv": priority_csv_reference,
                    "priority_review_pack": priority_pack_reference,
                    "decision_audit": str(decision_audit_path),
                    "action_plan": str(action_plan_path),
                },
                "safety_contract": {
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "trusted_status_write_allowed": False,
                    "raw_source_mutation_allowed": False,
                    "source_payload_exposed": source_payload_exposed,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "packet": path,
        "decision_audit": decision_audit_path,
        "action_plan": action_plan_path,
    }


def _ksa_immutability_audit_fixture() -> dict:
    return {
        "schema": "ncs_ksa_immutability_audit_v1",
        "ok": True,
        "report_only": True,
        "human_decision_required_for_status_update": True,
        "forbidden_automatic_statuses": [
            "human_reviewed",
            "accepted",
            "reviewed",
        ],
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "raw_source_mutation_allowed": False,
        "trusted_status_write_allowed": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "raw_source_mutation_allowed": False,
            "trusted_status_write_allowed": False,
            "human_decision_required_for_status_update": True,
            "forbidden_automatic_statuses": [
                "human_reviewed",
                "accepted",
                "reviewed",
            ],
        },
        "ksa_items": {
            "row_count": 574279,
            "sha256": "sha256:fixture",
            "raw_text_multiset_sha256": "sha256:raw-text-fixture",
        },
        "baseline": {
            "provided": True,
            "matches_current": True,
            "raw_text_multiset_matches_current": True,
            "source_text_matches_current": True,
        },
        "ontology_definitions": {
            "total_concepts": 533909,
            "concepts_with_definition": 413143,
            "boilerplate_definition_count": 413143,
            "boilerplate_trusted_status_count": 0,
            "draft_or_template_trusted_status_count": 0,
        },
    }


class BlockerReportTests(unittest.TestCase):
    def test_queue_snapshot_does_not_infer_human_gated_legacy_items_as_auto_startable(self) -> None:
        snapshot = _queue_snapshot_from_report(
            {
                "summary": {
                    "item_count": 2,
                    "blocked_count": 0,
                    "manual_ready_count": 1,
                    "auto_startable_count": 2,
                },
                "items": [
                    {
                        "id": "human-gated",
                        "auto_runnable": True,
                        "requires_human_decision": True,
                        "mutation_policy": "regenerate_reports_only",
                    },
                    {
                        "id": "inspect-only",
                        "auto_runnable": True,
                        "requires_human_decision": False,
                        "mutation_policy": "inspect_only",
                    },
                ],
            }
        )

        self.assertEqual(snapshot["item_count"], 2)
        self.assertEqual(snapshot["auto_startable_count"], 0)
        self.assertEqual(snapshot["state_counts"], {"manual_ready": 2})

    def test_queue_snapshot_preserves_zero_summary_counts_with_details(self) -> None:
        snapshot = _queue_snapshot_from_report(
            {
                "summary": {
                    "item_count": 5,
                    "blocked_count": 0,
                    "manual_ready_count": 0,
                    "auto_startable_count": 0,
                    "state_counts": {"ready_to_start": 5},
                },
                "items": [
                    {
                        "id": "auto-1",
                        "state": "ready_to_start",
                        "can_start_automated": True,
                        "mutation_policy": "regenerate_reports_only",
                    }
                ],
            }
        )

        self.assertEqual(snapshot["item_count"], 5)
        self.assertEqual(snapshot["blocked_count"], 0)
        self.assertEqual(snapshot["manual_ready_count"], 0)
        self.assertEqual(snapshot["auto_startable_count"], 1)

    def test_goal_completion_default_patterns_include_unprefixed_followup_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_remaining = tmp_path / "aihr_remaining_blockers_20260624_latest.json"
            new_remaining = tmp_path / "remaining_blockers_20260626_safe_next.json"
            old_backlog = tmp_path / "aihr_human_review_backlog_20260624_latest.json"
            new_backlog = tmp_path / "human_review_backlog_20260626_safe_next.json"
            for path in (old_remaining, new_remaining, old_backlog, new_backlog):
                path.write_text("{}", encoding="utf-8")

            remaining = _latest_report_path(
                "aihr_remaining_blockers_*.json",
                "remaining_blockers_*.json",
                fallback=tmp_path / DEFAULT_REMAINING_BLOCKERS_PATH.name,
            )
            backlog = _latest_report_path(
                "aihr_human_review_backlog_*.json",
                "human_review_backlog_*.json",
                fallback=tmp_path / DEFAULT_HUMAN_REVIEW_BACKLOG_PATH.name,
            )

        self.assertEqual(remaining, new_remaining)
        self.assertEqual(backlog, new_backlog)

    def test_latest_ksa_definition_operator_packet_accepts_session_labels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            fallback = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            older = tmp_path / "ksa_definition_review_operator_packet_20260629.json"
            latest = tmp_path / "ksa_definition_review_operator_packet_llm_preprocessing_20260630_9h.json"
            sidecar = (
                tmp_path
                / "ksa_definition_review_operator_packet_llm_preprocessing_20260630_9h_decision_audit.json"
            )
            for path in (fallback, older, latest, sidecar):
                path.write_text("{}", encoding="utf-8")

            selected = latest_ksa_definition_operator_packet_path(fallback=fallback)

        self.assertEqual(selected, latest)

    def test_remaining_blockers_report_joins_current_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            queue_path = tmp_path / "queue.json"
            hygiene_path = tmp_path / "hygiene.json"
            api_linkage_path = tmp_path / "api.json"
            markdown_path = tmp_path / "blockers.md"

            release_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_release_readiness_v1",
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blocker_count": 4,
                        "warning_count": 2,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260626.json",
                        "blockers": [
                            {"name": "review_debt:human_reviewed_concepts", "value": 0, "threshold": "> 0"},
                            {"name": "qualification:collection_coverage", "value": 0.2205, "threshold": ">= 0.9"},
                            {"name": "human_review:provenance_reconfirmation_required"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            review_priority_path = tmp_path / "review_priority_20260625_after_ksa.json"
            transition_seedpack_path = tmp_path / "transition_seedpack_20260625_after_ksa.jsonl"
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "quality_warning_count": 6,
                            "source_paths": {
                                "review_priority_report": str(review_priority_path),
                                "transition_seedpack": str(transition_seedpack_path),
                            },
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": "reports\\aihr_agent_queue_20260626.json",
                        "summary": {"item_count": 4, "blocked_count": 1, "manual_ready_count": 2, "auto_startable_count": 1},
                        "items": [
                            {
                                "id": "aihr-01-review",
                                "owner": "ontology-review-agent",
                                "state": "ready_to_start",
                                "can_start_automated": True,
                                "mutation_policy": "regenerate_reports_only",
                                "command": (
                                    "python scripts\\ncs_harness.py export-ontology-definition-seedpack "
                                    "--source-report-path reports\\review_priority_20260626_overnight.md "
                                    "--out reports\\aihr_ontology_definition_review_seedpack_20260626.jsonl"
                                ),
                            },
                            {
                                "id": "aihr-02-human",
                                "owner": "training-goal-review-agent",
                                "state": "manual_ready",
                                "can_start_automated": False,
                                "mutation_policy": "requires_existing_artifacts",
                                "command": (
                                    "python scripts\\ncs_harness.py review-triage "
                                    "--quality-report reports\\quality_gates_20260626_safe_next.json "
                                    "--review-priority-report reports\\review_priority_20260626_overnight.json "
                                    "--transition-seedpack reports\\aihr_transition_scenario_seedpack_20260626.jsonl "
                                    "--out reports\\aihr_review_triage_20260626.json"
                                ),
                            },
                            {
                                "id": "aihr-05-provenance",
                                "owner": "ontology-review-agent",
                                "state": "ready_to_start",
                                "can_start_automated": True,
                                "mutation_policy": "regenerate_reports_only",
                                "command": (
                                    "python scripts\\ncs_harness.py "
                                    "export-human-review-provenance-reconfirmation-proofset "
                                    "--out reports\\human_review_provenance_reconfirmation_packet_20260626.json"
                                ),
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 0.2205, "attempted_unit_count": 2963, "total_unit_count": 13435},
                        "status_counts": {"collected": 1599, "empty": 1363, "error": 1},
                        "api_execution_guard": {
                            "status": "blocked",
                            "api_call_allowed_now": False,
                            "element_api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                            "qualification_retry_guard_reason": "next_safe_action:start_guarded_watchdog_if_no_active_process",
                            "next_safe_action_resolution_status": "refresh_qualification_retry_hygiene_before_retry",
                        },
                        "qualification_retry_allowed_now": True,
                        "next_safe_action_status": "start_guarded_watchdog_if_no_active_process",
                        "blocked_by_checkpoint": True,
                        "checkpoint_path": str(
                            ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260623.json"
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            api_linkage_path.write_text(
                json.dumps({"summary": {"major_count": 4}, "safe_next_actions": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )
            write_remaining_blockers_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(report["queue_snapshot"]["item_count"], 4)
        self.assertTrue(report["queue_source_path_consistency"]["ok"])
        self.assertEqual(report["queue_source_path_consistency"]["status"], "matched")
        self.assertEqual(report["release_readiness"]["blocker_count"], 4)
        self.assertEqual(report["qualification_snapshot"]["coverage"], 0.2205)
        self.assertEqual(report["remaining_blockers"][0]["name"], "review_debt:human_reviewed_concepts")
        self.assertEqual(report["remaining_blockers"][0]["evidence"]["current_count"], 0)
        self.assertEqual(report["remaining_blockers"][0]["evidence"]["required_threshold"], "> 0")
        self.assertNotIn("count", report["remaining_blockers"][0]["evidence"])
        self.assertNotIn("threshold", report["remaining_blockers"][0]["evidence"])
        self.assertEqual(report["remaining_blockers"][1]["status"], "guarded_blocked")
        self.assertEqual(
            report["remaining_blockers"][1]["next_safe_action"],
            "refresh_qualification_retry_hygiene_before_retry",
        )
        self.assertTrue(
            report["remaining_blockers"][1]["evidence"]["qualification_retry_allowed_now"]
        )
        self.assertFalse(
            report["remaining_blockers"][1]["evidence"]["automatic_collection_allowed_now"]
        )
        self.assertTrue(report["remaining_blockers"][1]["evidence"]["operator_timing_required"])
        self.assertTrue(report["remaining_blockers"][1]["evidence"]["guarded_collection_required"])
        self.assertEqual(
            report["remaining_blockers"][1]["evidence"]["qualification_retry_guard_reason"],
            "next_safe_action:start_guarded_watchdog_if_no_active_process",
        )
        self.assertEqual(
            report["remaining_blockers"][1]["evidence"][
                "retry_hygiene_next_safe_action_resolution_status"
            ],
            "refresh_qualification_retry_hygiene_before_retry",
        )
        self.assertEqual(
            report["remaining_blockers"][1]["evidence"]["retry_hygiene_status_scope"],
            "retry_preflight_only_not_collection_coverage",
        )
        self.assertTrue(report["qualification_snapshot"]["qualification_retry_allowed_now"])
        self.assertEqual(
            report["latest_supporting_reports"]["review_priority"],
            review_priority_path.name,
        )
        self.assertEqual(
            report["latest_supporting_reports"]["transition_seedpack"],
            transition_seedpack_path.name,
        )
        self.assertEqual(
            report["remaining_blockers"][1]["evidence"]["checkpoint_path"],
            "reports/checkpoint_ncs006_element_api_status_20260623.json",
        )
        self.assertIn("source_artifact_hashes", report)
        self.assertTrue(
            report["source_artifact_hashes"]["release_readiness"]["sha256"].startswith(
                "sha256:"
            )
        )
        self.assertTrue(
            report["source_artifact_hashes"]["release_readiness"][
                "cycle_safe_content_sha256"
            ].startswith("sha256:")
        )
        self.assertEqual(
            report["source_artifact_hashes"]["release_readiness"]["sha256_scope"],
            "cycle_safe_release_readiness",
        )
        self.assertTrue(
            report["source_artifact_hashes"]["queue_status"]["sha256"].startswith(
                "sha256:"
            )
        )
        self.assertIn(
            "queue_supporting_report_inputs.quality_report[0]",
            report["source_artifact_hashes"],
        )
        self.assertIn(
            "queue_supporting_report_inputs.source_report_path[0]",
            report["source_artifact_hashes"],
        )
        self.assertEqual(
            report["remaining_blockers"][2]["next_safe_action"],
            "export-human-review-provenance-reconfirmation-proofset",
        )
        self.assertEqual(
            report["remaining_blockers"][2]["evidence"]["queue_action_id"],
            "aihr-05-provenance",
        )
        queue_inputs = report["queue_supporting_report_inputs"]["inputs"]
        self.assertEqual(
            queue_inputs["source_report_path"],
            ["reports\\review_priority_20260626_overnight.md"],
        )
        self.assertEqual(
            queue_inputs["quality_report"],
            ["reports\\quality_gates_20260626_safe_next.json"],
        )
        self.assertEqual(
            queue_inputs["review_priority_report"],
            ["reports\\review_priority_20260626_overnight.json"],
        )
        self.assertEqual(
            queue_inputs["transition_seedpack"],
            ["reports\\aihr_transition_scenario_seedpack_20260626.jsonl"],
        )
        alignment = report["review_artifact_date_alignment"]
        self.assertEqual(alignment["status"], "stale_against_active_date")
        self.assertEqual(alignment["active_date"], "20260626")
        self.assertIn("latest_supporting_reports.review_priority", alignment["stale_keys"])
        self.assertIn("latest_supporting_reports.transition_seedpack", alignment["stale_keys"])
        self.assertEqual(
            alignment["path_dates"]["queue_supporting_report_inputs.review_priority_report"],
            ["20260626"],
        )
        self.assertEqual(
            [item["id"] for item in report["fallback_actions"]],
            ["aihr-01-review", "aihr-05-provenance"],
        )
        self.assertIn("Remaining Blockers", markdown)
        self.assertIn("Current state is evidence-backed, but unresolved blockers remain.", markdown)
        self.assertIn("Safety Contract", markdown)
        self.assertIn("status_update_allowed: `False`", markdown)
        self.assertIn("forbidden_automatic_statuses", markdown)
        self.assertIn("Fallback Actions", markdown)
        self.assertIn("Queue Report Inputs", markdown)
        self.assertIn("Queue Source Path Consistency", markdown)
        self.assertIn("Review Artifact Date Alignment", markdown)
        self.assertIn("stale_against_active_date", markdown)
        self.assertIn("Source Artifact Hashes", markdown)
        self.assertIn("sha256=`sha256:", markdown)
        self.assertIn("cycle_safe_content_sha256=`sha256:", markdown)
        self.assertIn("queue_supporting_report_inputs.quality_report[0]", markdown)
        self.assertIn("reports\\quality_gates_20260626_safe_next.json", markdown)
        self.assertIn("qualification:collection_coverage", markdown)
        self.assertIn("automatic_collection_allowed_now", markdown)
        self.assertNotIn(str(ROOT), json.dumps(report, ensure_ascii=False))
        self.assertNotIn(str(ROOT), markdown)

    def test_release_readiness_cycle_safe_hash_tracks_dashboard_contract_ok(
        self,
    ) -> None:
        payload = {
            "schema": "aihr_release_readiness_v1",
            "release_ready": False,
            "blockers": [],
            "dashboard_surface_contract": {
                "ok": True,
                "artifact": {
                    "path": "reports/dashboard.json",
                    "mtime_utc": "2026-07-12T00:00:00+00:00",
                    "content_sha256": "sha256:" + ("1" * 64),
                },
            },
            "artifact_lineage_contract": {"release_readiness_self_hash": "sha256:" + ("2" * 64)},
        }
        changed_ok = json.loads(json.dumps(payload))
        changed_ok["dashboard_surface_contract"]["ok"] = False
        changed_mtime = json.loads(json.dumps(payload))
        changed_mtime["dashboard_surface_contract"]["artifact"]["mtime_utc"] = (
            "2026-07-12T01:00:00+00:00"
        )

        self.assertNotEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed_ok),
        )
        self.assertEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed_mtime),
        )

    def test_release_readiness_cycle_safe_hash_ignores_backlog_self_revalidation(
        self,
    ) -> None:
        payload = {
            "schema": "aihr_release_readiness_v1",
            "release_ready": False,
            "blockers": [],
            "dashboard_surface_contract": {
                "ok": True,
                "artifact": {
                    "static_artifacts": [
                        {
                            "name": "human_review_backlog_json",
                            "human_review_backlog": {
                                "schema": "aihr_human_review_backlog_v1",
                                "contract_ok": True,
                                "source_hash_contract_ok": True,
                                "source_hash_revalidation_ok": True,
                                "source_hash_revalidation_checked_count": 8,
                                "source_hash_revalidation_mismatch_count": 0,
                                "source_hash_revalidation_issues": [],
                                "source_release_hash_scope": "cycle_safe_release_readiness",
                                "source_release_cycle_safe_hash_present": True,
                            },
                        }
                    ]
                },
            },
        }
        changed = json.loads(json.dumps(payload))
        backlog = changed["dashboard_surface_contract"]["artifact"]["static_artifacts"][0][
            "human_review_backlog"
        ]
        backlog["contract_ok"] = False
        backlog["source_hash_contract_ok"] = False
        backlog["source_hash_revalidation_ok"] = False
        backlog["source_hash_revalidation_mismatch_count"] = 1
        backlog["source_hash_revalidation_issues"] = [
            {"code": "release_readiness_hash_mismatch"}
        ]

        self.assertEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed),
        )

    def test_remaining_blockers_report_separates_retry_complete_from_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            queue_path = tmp_path / "queue.json"
            hygiene_path = tmp_path / "hygiene.json"
            api_linkage_path = tmp_path / "api.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blocker_count": 1,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260626.json",
                        "blockers": [
                            {
                                "name": "qualification:collection_coverage",
                                "value": 0.3221,
                                "threshold": ">= 0.9",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": "reports/aihr_agent_queue_20260626.json",
                        "summary": {
                            "item_count": 1,
                            "blocked_count": 0,
                            "manual_ready_count": 1,
                            "auto_startable_count": 0,
                        },
                        "items": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {
                            "collection_coverage": 0.3221,
                            "attempted_unit_count": 4327,
                            "total_unit_count": 13435,
                            "unattempted_unit_count": 9108,
                            "additional_attempted_units_needed": 7765,
                        },
                        "retry_candidate_unit_count": 0,
                        "api_execution_guard": {
                            "status": "allowed",
                            "api_call_allowed_now": False,
                            "element_api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                            "qualification_retry_guard_reason": "next_safe_action:complete_no_collection_needed",
                            "next_safe_action_resolution_status": "complete_no_collection_needed",
                        },
                        "qualification_retry_allowed_now": True,
                        "next_safe_action_status": "complete_no_collection_needed",
                        "blocked_by_checkpoint": False,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            api_linkage_path.write_text(
                json.dumps({"summary": {}, "safe_next_actions": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )

        blocker = report["remaining_blockers"][0]
        self.assertEqual(blocker["name"], "qualification:collection_coverage")
        self.assertEqual(blocker["status"], "guarded_manual_ready")
        self.assertEqual(
            blocker["next_safe_action"],
            "plan_guarded_qualification_collection_for_unattempted_units",
        )
        self.assertEqual(blocker["evidence"]["retry_candidate_unit_count"], 0)
        self.assertEqual(blocker["evidence"]["additional_attempted_units_needed"], 7765)
        self.assertEqual(blocker["evidence"]["unattempted_unit_count"], 9108)
        self.assertEqual(
            blocker["evidence"]["retry_hygiene_next_safe_action_resolution_status"],
            "complete_no_collection_needed",
        )
        self.assertEqual(
            blocker["evidence"]["coverage_gap_normalized_next_safe_action"],
            "plan_guarded_qualification_collection_for_unattempted_units",
        )
        self.assertEqual(
            blocker["evidence"]["retry_hygiene_status_scope"],
            "retry_preflight_only_not_collection_coverage",
        )
        self.assertFalse(blocker["evidence"]["automatic_collection_allowed_now"])
        self.assertTrue(blocker["evidence"]["operator_timing_required"])
        self.assertTrue(blocker["evidence"]["guarded_collection_required"])
        self.assertTrue(blocker["evidence"]["retry_gate_complete_but_coverage_gap_open"])

    def test_remaining_blockers_prefers_queue_review_priority_when_triage_date_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260707.json"
            triage_path = tmp_path / "aihr_review_triage_20260707.json"
            queue_path = tmp_path / "aihr_agent_queue_status_20260707.json"
            hygiene_path = tmp_path / "qualification_retry_hygiene_20260707.json"
            api_linkage_path = tmp_path / "api_linkage_summary_20260707.json"
            review_seedpack_path = (
                tmp_path / "aihr_review_seedpack_blocker_ranked_20260707.jsonl"
            )
            session_review_priority = (
                "reports\\overnight_sessions\\aihr_review_priority_20260707.json"
            )
            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "agent_work_queue_path": "reports\\overnight_sessions\\aihr_agent_queue_20260707.json",
                        "blockers": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "source_paths": {
                                "review_priority_report": "reports\\aihr_review_priority_20260707.json",
                                "transition_seedpack": "reports\\overnight_sessions\\aihr_transition_scenario_seedpack_20260707.jsonl",
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": "reports\\overnight_sessions\\aihr_agent_queue_20260707.json",
                        "summary": {"item_count": 1},
                        "items": [
                            {
                                "id": "aihr-02",
                                "state": "manual_ready",
                                "mutation_policy": "requires_existing_artifacts",
                                "command": (
                                    "python scripts\\ncs_harness.py review-triage "
                                    "--quality-report reports\\overnight_sessions\\aihr_quality_gates_with_transition_20260707.json "
                                    f"--review-priority-report {session_review_priority} "
                                    "--transition-seedpack reports\\overnight_sessions\\aihr_transition_scenario_seedpack_20260707.jsonl "
                                    "--out reports\\overnight_sessions\\aihr_review_triage_20260707.json"
                                ),
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps({"coverage_gap": {"collection_coverage": 0.4}}, ensure_ascii=False),
                encoding="utf-8",
            )
            api_linkage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            review_seedpack_path.write_text(
                json.dumps(
                    {
                        "record_type": "batch",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )

        self.assertEqual(
            report["latest_supporting_reports"]["review_priority"],
            session_review_priority,
        )
        self.assertEqual(
            report["review_triage"]["summary"]["source_paths"]["review_priority_report"],
            session_review_priority,
        )
        self.assertEqual(report["review_artifact_date_alignment"]["status"], "aligned")

    def test_remaining_blockers_flags_queue_status_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            queue_path = tmp_path / "queue.json"
            hygiene_path = tmp_path / "hygiene.json"
            api_linkage_path = tmp_path / "api.json"
            markdown_path = tmp_path / "blockers.md"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blocker_count": 1,
                        "warning_count": 0,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260627.json",
                        "blockers": [
                            {
                                "name": "review_debt:human_reviewed_concepts",
                                "value": 0,
                                "threshold": "> 0",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": "reports/aihr_agent_queue_20260626.json",
                        "summary": {
                            "item_count": 1,
                            "blocked_count": 0,
                            "manual_ready_count": 0,
                            "auto_startable_count": 1,
                        },
                        "items": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps({"coverage_gap": {"collection_coverage": 1.0}}, ensure_ascii=False),
                encoding="utf-8",
            )
            api_linkage_path.write_text(
                json.dumps({"summary": {}, "safe_next_actions": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )
            write_remaining_blockers_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        consistency = report["queue_source_path_consistency"]
        self.assertFalse(consistency["ok"])
        self.assertEqual(consistency["status"], "mismatch")
        self.assertEqual(
            consistency["expected_agent_work_queue_path"],
            "reports/aihr_agent_queue_20260627.json",
        )
        self.assertEqual(
            consistency["queue_status_source_queue_path"],
            "reports/aihr_agent_queue_20260626.json",
        )
        self.assertIn("Queue Source Path Consistency", markdown)
        self.assertIn("mismatch", markdown)

    def test_remaining_blockers_queue_status_matches_absolute_workspace_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            queue_path = tmp_path / "queue.json"
            hygiene_path = tmp_path / "hygiene.json"
            api_linkage_path = tmp_path / "api.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blocker_count": 1,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260627.json",
                        "blockers": [
                            {
                                "name": "review_debt:human_reviewed_concepts",
                                "value": 0,
                                "threshold": "> 0",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": str(
                            ROOT / "reports" / "aihr_agent_queue_20260627.json"
                        ),
                        "summary": {
                            "item_count": 1,
                            "blocked_count": 0,
                            "manual_ready_count": 0,
                            "auto_startable_count": 1,
                        },
                        "items": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps({"coverage_gap": {"collection_coverage": 1.0}}, ensure_ascii=False),
                encoding="utf-8",
            )
            api_linkage_path.write_text(
                json.dumps({"summary": {}, "safe_next_actions": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )

        consistency = report["queue_source_path_consistency"]
        self.assertTrue(consistency["ok"])
        self.assertEqual(consistency["status"], "matched")

    def test_remaining_blockers_queue_status_rejects_external_workspace_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            triage_path = tmp_path / "triage.json"
            queue_path = tmp_path / "queue.json"
            hygiene_path = tmp_path / "hygiene.json"
            api_linkage_path = tmp_path / "api.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "engineering_hygiene_ok": True,
                        "blocker_count": 1,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260627.json",
                        "blockers": [
                            {
                                "name": "review_debt:human_reviewed_concepts",
                                "value": 0,
                                "threshold": "> 0",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            queue_path.write_text(
                json.dumps(
                    {
                        "source_queue_path": (
                            "C:/other-workspace/reports/aihr_agent_queue_20260627.json"
                        ),
                        "summary": {
                            "item_count": 1,
                            "blocked_count": 0,
                            "manual_ready_count": 0,
                            "auto_startable_count": 1,
                        },
                        "items": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps({"coverage_gap": {"collection_coverage": 1.0}}, ensure_ascii=False),
                encoding="utf-8",
            )
            api_linkage_path.write_text(
                json.dumps({"summary": {}, "safe_next_actions": []}, ensure_ascii=False),
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )

        consistency = report["queue_source_path_consistency"]
        self.assertFalse(consistency["ok"])
        self.assertEqual(consistency["status"], "mismatch")

    def test_human_review_backlog_flags_mixed_review_artifact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260626.json"
            triage_path = tmp_path / "aihr_review_triage_20260625_after_ksa.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260626.jsonl"
            review_seedpack_path = tmp_path / "aihr_review_seedpack_blocker_ranked_20260625.jsonl"
            transition_seedpack_path = tmp_path / "aihr_transition_scenario_seedpack_20260626.jsonl"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {
                        "blockers": [
                            {"name": "review_debt:human_reviewed_concepts", "value": 0, "threshold": "> 0"},
                            {"name": "review_debt:human_reviewed_goal_links", "value": 0, "threshold": "> 0"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "source_paths": {
                                "review_priority_report": "reports/review_priority_20260625_after_ksa.json",
                                "transition_seedpack": "reports/aihr_transition_scenario_seedpack_20260625.jsonl",
                            }
                        },
                        "review_priority_items": [],
                        "focus_review_priority_overlays": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "test",
                "item_count": 1,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            review_item = {
                "record_type": "review_item",
                "seedpack_id": "test",
                "sequence": 1,
                "issue_type": "hr_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "42",
                "target_snapshot_hash": "hash-42",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            for path in (ontology_seedpack_path, review_seedpack_path, transition_seedpack_path):
                path.write_text(
                    json.dumps(batch, ensure_ascii=False) + "\n"
                    + json.dumps(review_item, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        alignment = report["review_artifact_date_alignment"]
        self.assertEqual(alignment["status"], "stale_against_active_date")
        self.assertEqual(alignment["active_date"], "20260626")
        self.assertIn("source_paths.review_triage", alignment["stale_keys"])
        self.assertIn("source_paths.blocker_ranked_seedpack", alignment["stale_keys"])
        self.assertIn("triage_source_paths.review_priority_report", alignment["stale_keys"])
        self.assertTrue(alignment["mixed_dates"])
        self.assertIn("Review Artifact Date Alignment", markdown)
        self.assertIn("stale_against_active_date", markdown)

    def test_human_review_backlog_prefers_release_queue_review_priority_when_triage_date_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260707.json"
            triage_path = tmp_path / "aihr_review_triage_20260707.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260707.jsonl"
            review_seedpack_path = tmp_path / "aihr_review_seedpack_blocker_ranked_20260707.jsonl"
            transition_seedpack_path = tmp_path / "aihr_transition_scenario_seedpack_20260707.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260707.json"
            session_review_priority = (
                "reports\\overnight_sessions\\aihr_review_priority_20260707.json"
            )

            release_path.write_text(
                json.dumps(
                    {
                        "blockers": [
                            {
                                "name": "review_debt:human_reviewed_goal_links",
                                "value": 0,
                                "threshold": "> 0",
                            }
                        ],
                        "agent_work_queue": {
                            "items": [
                                {
                                    "id": "aihr-02",
                                    "state": "manual_ready",
                                    "mutation_policy": "requires_existing_artifacts",
                                    "command": (
                                        "python scripts\\ncs_harness.py review-triage "
                                        "--quality-report reports\\overnight_sessions\\aihr_quality_gates_with_transition_20260707.json "
                                        f"--review-priority-report {session_review_priority} "
                                        "--transition-seedpack reports\\overnight_sessions\\aihr_transition_scenario_seedpack_20260707.jsonl "
                                        "--out reports\\overnight_sessions\\aihr_review_triage_20260707.json"
                                    ),
                                }
                            ]
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "source_paths": {
                                "review_priority_report": "reports\\aihr_review_priority_20260707.json",
                                "transition_seedpack": "reports\\overnight_sessions\\aihr_transition_scenario_seedpack_20260707.jsonl",
                            }
                        },
                        "review_priority_items": [],
                        "focus_review_priority_overlays": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "test",
                "item_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            for path in (ontology_seedpack_path, review_seedpack_path, transition_seedpack_path):
                path.write_text(json.dumps(batch, ensure_ascii=False) + "\n", encoding="utf-8")
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        self.assertEqual(
            report["triage_summary"]["source_paths"]["review_priority_report"],
            session_review_priority,
        )
        self.assertEqual(
            report["queue_supporting_report_inputs"]["inputs"]["review_priority_report"],
            [session_review_priority],
        )
        self.assertIn(
            "queue_supporting_report_inputs.review_priority_report[0]",
            report["source_artifact_hashes"],
        )
        self.assertEqual(
            report["source_artifact_hashes"][
                "queue_supporting_report_inputs.review_priority_report[0]"
            ]["path"],
            session_review_priority,
        )
        self.assertEqual(report["review_artifact_date_alignment"]["status"], "aligned")

    def test_human_review_backlog_includes_ksa_definition_operator_packet_as_safe_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627.json"
            triage_path = tmp_path / "aihr_review_triage_20260627.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260627.jsonl"
            review_seedpack_path = tmp_path / "aihr_review_seedpack_blocker_ranked_20260627.jsonl"
            transition_seedpack_path = tmp_path / "aihr_transition_scenario_seedpack_20260627.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            markdown_path = tmp_path / "backlog.md"

            release_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_release_readiness_v1",
                        "blockers": [
                            {"name": "review_debt:human_reviewed_concepts", "value": 0, "threshold": "> 0"}
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {"source_paths": {}}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "test",
                "item_count": 1,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            review_item = {
                "record_type": "review_item",
                "seedpack_id": "test",
                "sequence": 1,
                "issue_type": "hr_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "42",
                "target_snapshot_hash": "hash-42",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            for path in (ontology_seedpack_path, review_seedpack_path, transition_seedpack_path):
                path.write_text(
                    json.dumps(batch, ensure_ascii=False)
                    + "\n"
                    + json.dumps(review_item, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
            definition_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_definition_review_operator_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "trusted_status_write_allowed": False,
                        "raw_source_mutation_allowed": False,
                        "summary": {
                            "review_pack_row_count": 25,
                            "review_csv_record_count": 25,
                            "decision_blank_count": 25,
                            "pending_decision_count": 25,
                            "completed_decision_count": 0,
                            "invalid_decision_count": 0,
                            "action_plan_action_count": 0,
                            "draft_definition_candidate_count": 25,
                            "priority_report_row_count": 25,
                            "first_review_queue": [
                                {
                                    "concept_id": 1,
                                    "concept_name": "안전수칙 준수",
                                    "concept_type": "attitude",
                                    "recommended_review_action": "draft_for_human_review_only",
                                }
                            ],
                        },
                        "artifacts": {
                            "priority_review_csv": "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.csv",
                            "action_plan": "reports/ksa_definition_review_operator_packet_20260627_action_plan.json",
                        },
                        "safety_contract": {
                            "status_update_allowed": False,
                            "db_writes": False,
                            "approval_claim": False,
                            "trusted_status_write_allowed": False,
                            "raw_source_mutation_allowed": False,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(
                definition_packet_path,
                include_first_review_queue=True,
            )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(
            report["source_paths"]["ksa_definition_review_operator_packet"],
            definition_packet_path.name,
        )
        self.assertIn(
            "ksa_definition_review_operator_packet",
            report["blockers"][0]["review_artifacts"],
        )
        definition_packet = report["seedpack_safety"]["ksa_definition_review_operator_packet"]
        self.assertTrue(definition_packet["safety_ok"])
        self.assertEqual(definition_packet["review_pack_row_count"], 25)
        self.assertEqual(definition_packet["decision_blank_count"], 25)
        self.assertFalse(definition_packet["db_writes"])
        self.assertFalse(definition_packet["approval_claim"])
        self.assertFalse(definition_packet["source_payload_exposed"])
        sidecar_safety = definition_packet["sidecar_safety"]
        self.assertEqual(
            sidecar_safety["decision_audit"]["path"],
            definition_packet_path.with_name(f"{definition_packet_path.stem}_decision_audit.json").name,
        )
        self.assertEqual(
            sidecar_safety["action_plan"]["path"],
            definition_packet_path.with_name(f"{definition_packet_path.stem}_action_plan.json").name,
        )
        self.assertTrue(report["seedpack_safety"]["all_seedpacks_safe"])
        policy = report["review_status_policy"]
        self.assertTrue(policy["human_decision_required_for_status_update"])
        self.assertFalse(policy["status_update_allowed"])
        self.assertEqual(
            policy["forbidden_automatic_statuses"],
            ["human_reviewed", "accepted", "reviewed"],
        )
        self.assertIn("Review Status Policy", markdown)
        self.assertIn("Source Artifact Hashes", markdown)
        self.assertIn("sha256=`sha256:", markdown)
        self.assertTrue(
            report["source_artifact_hashes"]["release_readiness"]["sha256"].startswith(
                "sha256:"
            )
        )
        self.assertTrue(
            report["source_artifact_hashes"]["release_readiness"][
                "cycle_safe_content_sha256"
            ].startswith("sha256:")
        )
        self.assertEqual(
            report["source_artifact_hashes"]["release_readiness"]["sha256_scope"],
            "cycle_safe_release_readiness",
        )
        self.assertTrue(
            report["source_artifact_hashes"][
                "ksa_definition_review_operator_packet"
            ]["sha256"].startswith("sha256:")
        )
        self.assertIn("human_decision_required_for_status_update", markdown)
        self.assertIn("KSA Definition Operator Packet", markdown)
        self.assertIn("action_plan_action_count", markdown)

    def test_human_review_backlog_flags_seedpack_missing_db_writes_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627.json"
            triage_path = tmp_path / "aihr_review_triage_20260627.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260627.jsonl"
            review_seedpack_path = tmp_path / "aihr_review_seedpack_blocker_ranked_20260627.jsonl"
            transition_seedpack_path = tmp_path / "aihr_transition_scenario_seedpack_20260627.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            markdown_path = tmp_path / "human_review_backlog.md"

            release_path.write_text(
                json.dumps(
                    {
                        "blockers": [
                            {"name": "review_debt:human_reviewed_goal_links", "value": 1}
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {"source_paths": {}}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            safe_batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "safe",
                "item_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            for path in (ontology_seedpack_path, review_seedpack_path):
                path.write_text(json.dumps(safe_batch, ensure_ascii=False) + "\n", encoding="utf-8")
            transition_batch = {
                "record_type": "batch",
                "format_version": "ncs-transition-scenario-seedpack-v1",
                "seedpack_id": "transition",
                "item_count": 1,
                "status_update_allowed": False,
                "approval_claim": False,
            }
            transition_item = {
                "record_type": "transition_scenario_review_item",
                "seedpack_id": "transition",
                "sequence": 1,
                "scenario_id": 32,
                "status_update_allowed": False,
                "approval_claim": False,
                "target_snapshot_hash": "hash-32",
            }
            transition_seedpack_path.write_text(
                json.dumps(transition_batch, ensure_ascii=False)
                + "\n"
                + json.dumps(transition_item, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )
            write_human_review_backlog_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        safety = report["seedpack_safety"]
        transition_audit = safety["audits"]["transition_scenario_seedpack"]
        self.assertFalse(safety["all_seedpacks_safe"])
        self.assertEqual(transition_audit["missing_db_writes_count"], 2)
        self.assertEqual(safety["total_missing_db_writes"], 2)
        self.assertIn("total_missing_db_writes", markdown)
        self.assertIn("missing_db_writes=`2`", markdown)

    def test_seedpack_audit_rejects_batch_and_malformed_safety_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "transition_seedpack.jsonl"
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-seedpack-v1",
                    "seedpack_id": "transition",
                    "item_count": 1,
                    "status_update_allowed": True,
                    "db_writes": False,
                    "approval_claim": "false",
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "seedpack_id": "transition",
                    "sequence": 1,
                    "scenario_id": 32,
                    "target_snapshot_hash": "hash-32",
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": "false",
                    "source_payload": {"raw_review_status": "reviewed"},
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            audit = _audit_review_seedpack(path)

        self.assertFalse(audit["safety_ok"])
        self.assertEqual(audit["status_update_allowed_violations"], 1)
        self.assertEqual(audit["missing_status_update_allowed_count"], 0)
        self.assertEqual(audit["approval_claim_violations"], 2)
        self.assertEqual(audit["missing_approval_claim_count"], 0)
        self.assertEqual(audit["forbidden_true_field_counts"]["approval_claim"], 2)
        self.assertEqual(audit["forbidden_true_field_counts"]["internal_payload_marker"], 1)

    def test_seedpack_audit_requires_approval_claim_on_all_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review_seedpack.jsonl"
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-review-seedpack-v1",
                    "seedpack_id": "review",
                    "item_count": 1,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                },
                {
                    "record_type": "review_item",
                    "seedpack_id": "review",
                    "sequence": 1,
                    "target_snapshot_hash": "hash-1",
                    "status_update_allowed": False,
                    "db_writes": False,
                },
            ]
            path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            audit = _audit_review_seedpack(path)

        self.assertFalse(audit["safety_ok"])
        self.assertEqual(audit["approval_claim_violations"], 0)
        self.assertEqual(audit["missing_approval_claim_count"], 1)

    def test_human_review_backlog_marks_unsafe_ksa_definition_operator_packet_not_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627.json"
            triage_path = tmp_path / "aihr_review_triage_20260627.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260627.jsonl"
            review_seedpack_path = tmp_path / "aihr_review_seedpack_blocker_ranked_20260627.jsonl"
            transition_seedpack_path = tmp_path / "aihr_transition_scenario_seedpack_20260627.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"

            release_path.write_text(
                json.dumps(
                    {"blockers": [{"name": "review_debt:human_reviewed_concepts", "value": 0}]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps({"summary": {"source_paths": {}}, "review_priority_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            batch = {
                "record_type": "batch",
                "format_version": "ncs-review-seedpack-v1",
                "seedpack_id": "test",
                "item_count": 1,
            }
            review_item = {
                "record_type": "review_item",
                "seedpack_id": "test",
                "sequence": 1,
                "issue_type": "hr_core_concept_human_review_required",
                "target_type": "ontology_concept",
                "target_id": "42",
                "target_snapshot_hash": "hash-42",
                "status_update_allowed": False,
            }
            for path in (ontology_seedpack_path, review_seedpack_path, transition_seedpack_path):
                path.write_text(
                    json.dumps(batch, ensure_ascii=False)
                    + "\n"
                    + json.dumps(review_item, ensure_ascii=False)
                    + "\n",
                    encoding="utf-8",
                )
            definition_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_definition_review_operator_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "source_payload_exposed": True,
                        "trusted_status_write_allowed": False,
                        "raw_source_mutation_allowed": False,
                        "summary": {
                            "review_pack_row_count": 25,
                            "review_csv_record_count": 25,
                            "decision_blank_count": 25,
                            "completed_decision_count": 0,
                            "invalid_decision_count": 0,
                            "action_plan_action_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(
                definition_packet_path,
                source_payload_exposed=True,
            )

            report = build_human_review_backlog_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                ontology_seedpack_path=ontology_seedpack_path,
                review_seedpack_path=review_seedpack_path,
                transition_seedpack_path=transition_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        definition_packet = report["seedpack_safety"]["ksa_definition_review_operator_packet"]
        self.assertTrue(definition_packet["source_payload_exposed"])
        self.assertFalse(definition_packet["safety_ok"])
        self.assertFalse(report["seedpack_safety"]["all_seedpacks_safe"])

    def test_goal_completion_audit_report_tracks_verified_and_open_requirements(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            qualification_coverage_plan_path = tmp_path / "qualification_coverage_plan.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            ksa_immutability_path = tmp_path / "ksa_immutability_audit_20260630.json"
            operator_addendum_path = tmp_path / "aihr_release_blocker_operator_addendum_20260630.json"
            operator_entrypoint_path = tmp_path / "aihr_operator_entrypoint_manifest_20260630.json"
            markdown_path = tmp_path / "audit.md"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {
                            "ok": True,
                            "json_artifacts": [
                                {"path": "reports/session/aihr_plan_demo_20260630.json"},
                                {"path": "reports/session/aihr_plan_demo_20260630_alias.json"},
                            ],
                            "html_artifact": {"path": "reports/session/aihr_plan_demo_20260630.html"},
                        },
                        "dashboard_surface_contract": {
                            "ok": True,
                            "artifact": {
                                "path": "reports/session/aihr_dashboard_surface_verification_20260630.json"
                            },
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps(
                    {
                        "latest_supporting_reports": {
                            "review_priority": "reports/review_priority_after_ksa.json",
                            "review_triage": "reports/review_triage_after_ksa.json",
                            "transition_seedpack": "reports/transition_seedpack_after_ksa.jsonl",
                            "review_seedpack": "reports/review_seedpack_after_ksa.jsonl",
                        },
                        "remaining_blockers": [
                            {"name": "review_debt:human_reviewed_concepts"},
                            {"name": "review_debt:human_reviewed_goal_links"},
                            {"name": "review_debt:human_reviewed_task_relations"},
                            {"name": "qualification:collection_coverage"},
                            {"name": "human_review:provenance_reconfirmation_required"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "blockers": [
                            {"name": "review_debt:human_reviewed_concepts", "value": 0},
                            {"name": "review_debt:human_reviewed_goal_links", "value": 0},
                            {"name": "review_debt:human_reviewed_task_relations", "value": 0},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 0.2205},
                        "api_execution_guard": {"status": "blocked"},
                        "checkpoint_path": str(
                            ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260623.json"
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            qualification_coverage_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_qualification_collection_coverage_plan_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "human_review_status_updates": False,
                        "approval_claim": False,
                        "automatic_collection_allowed_now": False,
                        "operator_timed_guarded_api_commands_only": True,
                        "batch_count": 1,
                        "unsafe_batch_count": 0,
                        "unsafe_batches": [],
                        "current_state": {
                            "attempted_unit_count": 5349,
                            "total_unit_count": 13435,
                            "collection_coverage": 0.398139,
                        },
                        "target_state": {
                            "additional_attempted_units_needed": 6743,
                            "estimated_batch_count": 68,
                        },
                        "batches": [
                            {
                                "command": (
                                    "python scripts\\ncs_harness.py collect-qualification-items "
                                    "--all-units --limit-units 100 --num-of-rows 50 "
                                    "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_current.json"
                                ),
                                "command_role": "operator_timed_guarded_api_collection",
                                "auto_runnable": False,
                                "automatic_queue_execution_allowed": False,
                                "execution_authorized": False,
                                "do_not_execute_from_report": True,
                                "not_queue_item": True,
                                "requires_operator_ticket": True,
                                "requires_explicit_operator_start": True,
                                "requires_operator_timing": True,
                                "guard_required": True,
                                "mutation_policy": "guarded_api_collection",
                            }
                        ],
                        "guard_policy": {
                            "must_run_qualification_retry_hygiene_first": True,
                            "must_use_ncs006_checkpoint_path": True,
                            "must_not_write_human_review_statuses": True,
                            "operator_timing_required": True,
                            "automatic_queue_execution_allowed": False,
                            "forbidden_status_updates": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"issue_type": "hr_core_concept_human_review_required"}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            ksa_immutability_path.write_text(
                json.dumps(_ksa_immutability_audit_fixture(), ensure_ascii=False),
                encoding="utf-8",
            )
            definition_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_definition_review_operator_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "trusted_status_write_allowed": False,
                        "raw_source_mutation_allowed": False,
                        "summary": {
                            "review_pack_row_count": 25,
                            "review_csv_record_count": 25,
                            "decision_blank_count": 25,
                            "completed_decision_count": 0,
                            "invalid_decision_count": 0,
                            "action_plan_action_count": 0,
                        },
                        "artifacts": {
                            "priority_review_csv": "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.csv",
                            "priority_review_pack": "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.json",
                            "decision_audit": "reports/ksa_definition_review_operator_packet_20260627_decision_audit.json",
                            "action_plan": "reports/ksa_definition_review_operator_packet_20260627_action_plan.json",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)
            operator_addendum_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_release_blocker_operator_addendum_v1",
                        "ok": True,
                        "status": "pass",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "summary": {
                            "remaining_blocker_count": 6,
                            "covered_remaining_blocker_count": 6,
                            "issue_count": 0,
                            "warning_count": 2,
                            "workbench_summary_row_count_matches_selected": True,
                            "workbench_selected_row_count": 53,
                            "workbench_source_total_row_count": 67,
                            "workbench_unselected_source_row_count": 14,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            operator_entrypoint_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_operator_entrypoint_manifest_v1",
                        "ok": True,
                        "status": "pass",
                        "terminal_evidence_only": True,
                        "include_in_release_refresh_dag": False,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "summary": {
                            "entry_count": 12,
                            "entry_ok_count": 12,
                            "issue_count": 0,
                            "warning_count": 0,
                            "csv_decision_surface_count": 5,
                            "guarded_api_timing_surface_count": 2,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                qualification_coverage_plan_path=qualification_coverage_plan_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
                ksa_immutability_audit_path=ksa_immutability_path,
                operator_addendum_path=operator_addendum_path,
                operator_entrypoint_manifest_path=operator_entrypoint_path,
                objective="8-hour overnight release audit",
            )
            write_goal_completion_audit_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(report["objective"], "8-hour overnight release audit")
        policy = report["review_status_policy"]
        self.assertTrue(policy["human_decision_required_for_status_update"])
        self.assertFalse(policy["status_update_allowed"])
        self.assertEqual(
            policy["forbidden_automatic_statuses"],
            ["human_reviewed", "accepted", "reviewed"],
        )
        self.assertEqual(report["verified_requirement_count"], 3)
        self.assertEqual(report["open_requirement_count"], 5)
        self.assertEqual(
            report["source_paths"]["operator_addendum"],
            operator_addendum_path.name,
        )
        self.assertEqual(
            report["source_paths"]["operator_entrypoint_manifest"],
            operator_entrypoint_path.name,
        )
        operator_support = report["supporting_snapshots"]["operator_support"]
        self.assertTrue(operator_support["support_only"])
        self.assertTrue(operator_support["support_ok"])
        self.assertEqual([], operator_support["issues"])
        self.assertFalse(operator_support["approval_or_review_status_claim"])
        self.assertEqual(
            operator_support["operator_addendum"]["covered_remaining_blocker_count"],
            6,
        )
        self.assertEqual(
            operator_support["operator_addendum"]["workbench_unselected_source_row_count"],
            14,
        )
        self.assertEqual(
            operator_support["operator_entrypoint_manifest"]["entry_ok_count"],
            12,
        )
        self.assertEqual(
            operator_support["operator_entrypoint_manifest"]["guarded_api_timing_surface_count"],
            2,
        )
        self.assertIn("Operator Support Evidence", markdown)
        self.assertIn("support evidence only", markdown)
        self.assertEqual(report["requirements"][0]["status"], "verified")
        self.assertIn("reports/session/aihr_plan_demo_20260630.json", report["requirements"][0]["evidence"])
        self.assertIn(
            "reports/session/aihr_dashboard_surface_verification_20260630.json",
            report["requirements"][0]["evidence"],
        )
        self.assertEqual(report["requirements"][3]["status"], "open")
        self.assertEqual(report["requirements"][6]["name"], "provenance reconfirmation review")
        self.assertEqual(report["requirements"][6]["status"], "open")
        self.assertIn("human_review_provenance_reconfirmation_packet", " ".join(report["requirements"][6]["evidence"]))
        self.assertEqual(report["requirements"][7]["status"], "guarded_blocked")
        self.assertEqual(
            report["requirements"][7]["evidence"],
            [
                "hygiene.json",
                "qualification_coverage_plan.json",
                "reports/checkpoint_ncs006_element_api_status_20260623.json",
            ],
        )
        coverage_plan = report["supporting_snapshots"]["qualification_coverage_plan"]
        self.assertTrue(coverage_plan["guard_summary_ok"])
        self.assertFalse(coverage_plan["human_review_status_updates"])
        self.assertFalse(coverage_plan["approval_claim"])
        self.assertTrue(coverage_plan["must_not_write_human_review_statuses"])
        self.assertIn("reports/review_priority_after_ksa.json", report["requirements"][3]["evidence"])
        self.assertIn(ksa_immutability_path.name, report["requirements"][3]["evidence"])
        self.assertIn(definition_packet_path.name, report["requirements"][3]["evidence"])
        self.assertIn(
            "reports/ksa_definition_review_operator_packet_20260627_priority_review_pack.csv",
            report["requirements"][3]["evidence"],
        )
        self.assertTrue(
            report["supporting_snapshots"]["ksa_definition_review_operator_packet"]["safety_ok"]
        )
        ksa_immutability = report["supporting_snapshots"]["ksa_immutability_audit"]
        self.assertTrue(ksa_immutability["contract_ok"])
        self.assertEqual(ksa_immutability["ksa_items_row_count"], 574279)
        self.assertEqual(
            ksa_immutability["ksa_items_raw_text_multiset_sha256"],
            "sha256:raw-text-fixture",
        )
        self.assertEqual(ksa_immutability["baseline_matches_current"], True)
        self.assertTrue(ksa_immutability["human_decision_required_for_status_update"])
        self.assertEqual(
            ksa_immutability["forbidden_automatic_statuses"],
            ["human_reviewed", "accepted", "reviewed"],
        )
        self.assertTrue(ksa_immutability["safety_contract_ok"])
        self.assertEqual(ksa_immutability["boilerplate_trusted_status_count"], 0)
        self.assertIn("reports/review_triage_after_ksa.json", report["requirements"][4]["evidence"])
        self.assertIn("reports/transition_seedpack_after_ksa.jsonl", report["requirements"][4]["evidence"])
        self.assertIn("reports/review_seedpack_after_ksa.jsonl", report["requirements"][5]["evidence"])

    def test_goal_completion_audit_rejects_cyclic_operator_addendum_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            remaining_path = root / "remaining.json"
            backlog_path = root / "backlog.json"
            hygiene_path = root / "hygiene.json"
            ontology_seedpack_path = root / "ontology.jsonl"
            definition_packet_path = root / "ksa_definition_review_operator_packet.json"
            operator_addendum_path = root / "operator_addendum.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)
            operator_addendum_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_release_blocker_operator_addendum_v1",
                        "ok": True,
                        "status": "pass",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "source_artifacts": {
                            "goal_completion_audit": {"path": "reports/goal_completion_audit.json"},
                            "terminal_evidence_index": {
                                "path": "reports/aihr_terminal_evidence_index.json"
                            },
                        },
                        "summary": {"issue_count": 0, "warning_count": 0},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
                operator_addendum_path=operator_addendum_path,
            )

        support = report["supporting_snapshots"]["operator_support"]
        self.assertFalse(support["support_ok"])
        self.assertNotIn("operator_addendum", report["source_paths"])
        self.assertIn("operator_addendum_cycle_source", {item["code"] for item in support["issues"]})
        self.assertFalse(support["operator_addendum"]["support_ok"])

    def test_goal_completion_audit_rejects_unsafe_operator_entrypoint_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            remaining_path = root / "remaining.json"
            backlog_path = root / "backlog.json"
            hygiene_path = root / "hygiene.json"
            ontology_seedpack_path = root / "ontology.jsonl"
            definition_packet_path = root / "ksa_definition_review_operator_packet.json"
            entrypoint_path = root / "entrypoint.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)
            entrypoint_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_operator_entrypoint_manifest_v1",
                        "ok": True,
                        "status": "pass",
                        "terminal_evidence_only": True,
                        "include_in_release_refresh_dag": False,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": True,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "summary": {"entry_count": 1, "entry_ok_count": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
                operator_entrypoint_manifest_path=entrypoint_path,
            )

        support = report["supporting_snapshots"]["operator_support"]
        self.assertFalse(support["support_ok"])
        self.assertTrue(support["approval_or_review_status_claim"])
        self.assertNotIn("operator_entrypoint_manifest", report["source_paths"])
        self.assertIn(
            "operator_entrypoint_manifest_unsafe_contract",
            {item["code"] for item in support["issues"]},
        )

    def test_goal_completion_audit_rejects_nested_operator_addendum_unsafe_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            remaining_path = root / "remaining.json"
            backlog_path = root / "backlog.json"
            hygiene_path = root / "hygiene.json"
            ontology_seedpack_path = root / "ontology.jsonl"
            definition_packet_path = root / "ksa_definition_review_operator_packet.json"
            operator_addendum_path = root / "operator_addendum.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)
            operator_addendum_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_release_blocker_operator_addendum_v1",
                        "ok": True,
                        "status": "pass",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "entries": [
                            {
                                "id": "nested-unsafe",
                                "approval_claim": True,
                                "status_update_allowed": True,
                                "db_writes": True,
                                "source_payload": {"secret": "hidden"},
                            }
                        ],
                        "summary": {"issue_count": 0, "warning_count": 0},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
                operator_addendum_path=operator_addendum_path,
            )

        support = report["supporting_snapshots"]["operator_support"]
        self.assertFalse(support["support_ok"])
        self.assertTrue(support["approval_or_review_status_claim"])
        self.assertNotIn("operator_addendum", report["source_paths"])
        self.assertIn(
            "operator_addendum_unsafe_contract",
            {item["code"] for item in support["issues"]},
        )
        violations = support["operator_addendum"]["unsafe_flag_violations"]
        self.assertIn("$.entries[0].approval_claim", {item["path"] for item in violations})
        self.assertIn("$.entries[0].source_payload", {item["path"] for item in violations})
        self.assertIn(
            "forbidden_field_present",
            {item["reason"] for item in violations},
        )
        self.assertFalse(
            support["operator_addendum"]["safety_contract"]["nested_unsafe_flags_absent"]
        )

    def test_goal_completion_audit_rejects_nested_entrypoint_cycle_support(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            remaining_path = root / "remaining.json"
            backlog_path = root / "backlog.json"
            hygiene_path = root / "hygiene.json"
            ontology_seedpack_path = root / "ontology.jsonl"
            definition_packet_path = root / "ksa_definition_review_operator_packet.json"
            entrypoint_path = root / "entrypoint.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)
            entrypoint_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_operator_entrypoint_manifest_v1",
                        "ok": True,
                        "status": "pass",
                        "terminal_evidence_only": True,
                        "include_in_release_refresh_dag": False,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "approval_claim": False,
                        "acceptance_claim": False,
                        "human_decision_required": True,
                        "entries": [
                            {
                                "id": "nested-cycle",
                                "source_artifacts": {
                                    "goal_completion_audit": {
                                        "path": "reports/goal_completion_audit.json"
                                    }
                                },
                            }
                        ],
                        "summary": {"entry_count": 1, "entry_ok_count": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
                operator_entrypoint_manifest_path=entrypoint_path,
            )

        support = report["supporting_snapshots"]["operator_support"]
        self.assertFalse(support["support_ok"])
        self.assertNotIn("operator_entrypoint_manifest", report["source_paths"])
        self.assertIn(
            "operator_entrypoint_manifest_cycle_source",
            {item["code"] for item in support["issues"]},
        )
        self.assertFalse(support["operator_entrypoint_manifest"]["support_ok"])
        self.assertIn(
            "$.entries[0].source_artifacts",
            {
                item["container"]
                for item in support["operator_entrypoint_manifest"]["cycle_refs"]
            },
        )

    def test_ksa_immutability_summary_rejects_missing_ontology_table_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ksa_immutability_audit.json"
            path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_immutability_audit_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "raw_source_mutation_allowed": False,
                        "trusted_status_write_allowed": False,
                        "issues": ["missing_table:ontology_concepts"],
                        "ksa_items": {
                            "row_count": 1,
                            "sha256": "sha256:row",
                            "raw_text_multiset_sha256": "sha256:raw",
                        },
                        "baseline": {
                            "provided": True,
                            "matches_current": True,
                            "raw_text_multiset_matches_current": None,
                            "source_text_matches_current": True,
                        },
                        "ontology_definitions": {
                            "total_concepts": 0,
                            "boilerplate_trusted_status_count": 0,
                            "draft_or_template_trusted_status_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            summary = _ksa_immutability_audit_summary(path)

        self.assertFalse(summary["contract_ok"])
        self.assertTrue(summary["exists"])

    def test_ksa_immutability_summary_rejects_missing_safety_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ksa_immutability_audit.json"
            payload = _ksa_immutability_audit_fixture()
            payload.pop("human_decision_required_for_status_update")
            payload.pop("forbidden_automatic_statuses")
            payload.pop("safety")
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            summary = _ksa_immutability_audit_summary(path)

        self.assertTrue(summary["exists"])
        self.assertFalse(summary["contract_ok"])
        self.assertFalse(summary["safety_contract_ok"])
        self.assertIsNone(summary["human_decision_required_for_status_update"])

    def test_release_family_artifact_path_does_not_pick_newer_mismatched_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260624.json"
            fallback = tmp_path / "ksa_immutability_audit_default.json"
            newer_audit = tmp_path / "ksa_immutability_audit_20260703.json"
            release_path.write_text("{}", encoding="utf-8")
            newer_audit.write_text("{}", encoding="utf-8")

            selected = _release_family_artifact_path(
                release_path,
                prefix="ksa_immutability_audit_",
                suffix=".json",
                fallback=fallback,
            )

        self.assertEqual(selected.name, "ksa_immutability_audit_20260624.json")
        self.assertNotEqual(selected.name, newer_audit.name)
        self.assertFalse(selected.exists())

    def test_goal_completion_audit_keeps_query_route_open_without_dashboard_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": False},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "aligned",
                            "active_date": "20260627",
                            "all_dates": ["20260627"],
                            "stale_keys": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        query_route_requirement = report["requirements"][0]
        self.assertEqual(query_route_requirement["name"], "query_route demo contract")
        self.assertEqual(query_route_requirement["status"], "open")
        self.assertIn("dashboard_surface_contract.ok", query_route_requirement["notes"])
        self.assertEqual(report["requirements"][6]["name"], "provenance reconfirmation review")
        self.assertEqual(report["requirements"][6]["status"], "verified")
        self.assertNotIn("human_review_provenance_reconfirmation_packet_json", json.dumps(report))
        self.assertNotIn("human_review_provenance_reconfirmation_decision_sheet_csv", json.dumps(report))
        self.assertEqual(report["requirements"][7]["status"], "guarded_manual_ready")
        ontology_requirement = next(
            requirement
            for requirement in report["requirements"]
            if requirement["name"] == "ontology definition review"
        )
        self.assertEqual(ontology_requirement["status"], "open")
        self.assertIn("KSA immutability audit is missing", ontology_requirement["notes"])
        self.assertEqual(report["open_requirement_count"], 3)
        self.assertEqual(report["verified_requirement_count"], len(report["requirements"]) - 3)

    def test_goal_completion_audit_keeps_provenance_open_from_release_blockers_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627_12h.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            qualification_coverage_plan_path = tmp_path / "qualification_coverage_plan.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            provenance_packet_path = (
                tmp_path / "human_review_provenance_reconfirmation_packet_20260627_12h.json"
            )
            provenance_sheet_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_sheet_20260627_12h.csv"
            )
            provenance_audit_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_audit_20260627_12h.json"
            )
            stale_packet_path = (
                tmp_path / "human_review_provenance_reconfirmation_packet_20260628.json"
            )

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "blockers": [
                            {"name": "human_review:provenance_reconfirmation_required"}
                        ],
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for path in (
                provenance_packet_path,
                provenance_sheet_path,
                provenance_audit_path,
                stale_packet_path,
            ):
                path.write_text("{}", encoding="utf-8")
            remaining_path.write_text(
                json.dumps(
                    {"latest_supporting_reports": {}, "remaining_blockers": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "aligned",
                            "active_date": "20260627",
                            "all_dates": ["20260627"],
                            "stale_keys": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            qualification_coverage_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_qualification_collection_coverage_plan_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "human_review_status_updates": False,
                        "approval_claim": False,
                        "automatic_collection_allowed_now": False,
                        "operator_timed_guarded_api_commands_only": True,
                        "batch_count": 0,
                        "unsafe_batch_count": 0,
                        "unsafe_batches": [],
                        "current_state": {
                            "attempted_unit_count": 1,
                            "total_unit_count": 1,
                            "collection_coverage": 1.0,
                        },
                        "target_state": {
                            "additional_attempted_units_needed": 0,
                            "estimated_batch_count": 0,
                        },
                        "batches": [],
                        "guard_policy": {
                            "must_run_qualification_retry_hygiene_first": True,
                            "must_use_ncs006_checkpoint_path": True,
                            "must_not_write_human_review_statuses": True,
                            "operator_timing_required": True,
                            "automatic_queue_execution_allowed": False,
                            "forbidden_status_updates": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                qualification_coverage_plan_path=qualification_coverage_plan_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        provenance_requirement = next(
            requirement
            for requirement in report["requirements"]
            if requirement["name"] == "provenance reconfirmation review"
        )
        self.assertEqual(provenance_requirement["status"], "open")
        self.assertIn(
            provenance_packet_path.name,
            provenance_requirement["evidence"],
        )
        self.assertIn(
            provenance_sheet_path.name,
            provenance_requirement["evidence"],
        )
        self.assertIn(
            provenance_audit_path.name,
            provenance_requirement["evidence"],
        )
        self.assertNotIn(stale_packet_path.name, provenance_requirement["evidence"])

    def test_goal_completion_audit_prefers_prefixed_same_stamp_provenance_over_newer_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627_12h.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            qualification_coverage_plan_path = tmp_path / "qualification_coverage_plan.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            provenance_packet_path = (
                tmp_path / "aihr_human_review_provenance_reconfirmation_packet_20260627_12h.json"
            )
            provenance_sheet_path = (
                tmp_path / "aihr_human_review_provenance_reconfirmation_decision_sheet_20260627_12h.csv"
            )
            provenance_audit_path = (
                tmp_path / "aihr_human_review_provenance_reconfirmation_decision_audit_20260627_12h.json"
            )
            stale_packet_path = (
                tmp_path / "human_review_provenance_reconfirmation_packet_20260628.json"
            )
            stale_sheet_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_sheet_20260628.csv"
            )
            stale_audit_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_audit_20260628.json"
            )

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "blockers": [
                            {"name": "human_review:provenance_reconfirmation_required"}
                        ],
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for path in (
                provenance_packet_path,
                provenance_sheet_path,
                provenance_audit_path,
                stale_packet_path,
                stale_sheet_path,
                stale_audit_path,
            ):
                path.write_text("{}", encoding="utf-8")
            remaining_path.write_text(
                json.dumps(
                    {"latest_supporting_reports": {}, "remaining_blockers": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "aligned",
                            "active_date": "20260627",
                            "all_dates": ["20260627"],
                            "stale_keys": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            qualification_coverage_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_qualification_collection_coverage_plan_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "human_review_status_updates": False,
                        "approval_claim": False,
                        "automatic_collection_allowed_now": False,
                        "operator_timed_guarded_api_commands_only": True,
                        "batch_count": 0,
                        "unsafe_batch_count": 0,
                        "unsafe_batches": [],
                        "current_state": {
                            "attempted_unit_count": 1,
                            "total_unit_count": 1,
                            "collection_coverage": 1.0,
                        },
                        "target_state": {
                            "additional_attempted_units_needed": 0,
                            "estimated_batch_count": 0,
                        },
                        "batches": [],
                        "guard_policy": {
                            "must_run_qualification_retry_hygiene_first": True,
                            "must_use_ncs006_checkpoint_path": True,
                            "must_not_write_human_review_statuses": True,
                            "operator_timing_required": True,
                            "automatic_queue_execution_allowed": False,
                            "forbidden_status_updates": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                qualification_coverage_plan_path=qualification_coverage_plan_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        provenance_requirement = next(
            requirement
            for requirement in report["requirements"]
            if requirement["name"] == "provenance reconfirmation review"
        )
        self.assertEqual(provenance_requirement["status"], "open")
        self.assertIn(provenance_packet_path.name, provenance_requirement["evidence"])
        self.assertIn(provenance_sheet_path.name, provenance_requirement["evidence"])
        self.assertIn(provenance_audit_path.name, provenance_requirement["evidence"])
        self.assertNotIn(stale_packet_path.name, provenance_requirement["evidence"])
        self.assertNotIn(stale_sheet_path.name, provenance_requirement["evidence"])
        self.assertNotIn(stale_audit_path.name, provenance_requirement["evidence"])

    def test_goal_completion_audit_uses_date_stamp_provenance_for_labeled_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260627_after_runbook_v2.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            qualification_coverage_plan_path = tmp_path / "qualification_coverage_plan.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"
            provenance_packet_path = (
                tmp_path / "human_review_provenance_reconfirmation_packet_20260627.json"
            )
            provenance_sheet_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_sheet_20260627.csv"
            )
            provenance_audit_path = (
                tmp_path / "human_review_provenance_reconfirmation_decision_audit_20260627.json"
            )
            missing_labeled_packet_path = (
                tmp_path
                / "human_review_provenance_reconfirmation_packet_20260627_after_runbook_v2.json"
            )

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "blockers": [
                            {"name": "human_review:provenance_reconfirmation_required"}
                        ],
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for path in (
                provenance_packet_path,
                provenance_sheet_path,
                provenance_audit_path,
            ):
                path.write_text("{}", encoding="utf-8")
            remaining_path.write_text(
                json.dumps(
                    {"latest_supporting_reports": {}, "remaining_blockers": []},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "aligned",
                            "active_date": "20260627",
                            "all_dates": ["20260627"],
                            "stale_keys": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            qualification_coverage_plan_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_qualification_collection_coverage_plan_v1",
                        "ok": True,
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "api_calls": False,
                        "human_review_status_updates": False,
                        "approval_claim": False,
                        "automatic_collection_allowed_now": False,
                        "operator_timed_guarded_api_commands_only": True,
                        "batch_count": 0,
                        "unsafe_batch_count": 0,
                        "unsafe_batches": [],
                        "current_state": {
                            "attempted_unit_count": 1,
                            "total_unit_count": 1,
                            "collection_coverage": 1.0,
                        },
                        "target_state": {
                            "additional_attempted_units_needed": 0,
                            "estimated_batch_count": 0,
                        },
                        "batches": [],
                        "guard_policy": {
                            "must_run_qualification_retry_hygiene_first": True,
                            "must_use_ncs006_checkpoint_path": True,
                            "must_not_write_human_review_statuses": True,
                            "operator_timing_required": True,
                            "automatic_queue_execution_allowed": False,
                            "forbidden_status_updates": [
                                "human_reviewed",
                                "accepted",
                                "reviewed",
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(definition_packet_path)

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                qualification_coverage_plan_path=qualification_coverage_plan_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        provenance_requirement = next(
            requirement
            for requirement in report["requirements"]
            if requirement["name"] == "provenance reconfirmation review"
        )
        self.assertEqual(provenance_requirement["status"], "open")
        self.assertIn(provenance_packet_path.name, provenance_requirement["evidence"])
        self.assertIn(provenance_sheet_path.name, provenance_requirement["evidence"])
        self.assertIn(provenance_audit_path.name, provenance_requirement["evidence"])
        self.assertNotIn(missing_labeled_packet_path.name, provenance_requirement["evidence"])

    def test_goal_completion_audit_keeps_ontology_definition_open_when_definition_packet_unsafe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "release.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            ontology_seedpack_path = tmp_path / "ontology.jsonl"
            definition_packet_path = tmp_path / "ksa_definition_review_operator_packet_20260627.json"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps({"latest_supporting_reports": {}, "remaining_blockers": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "aligned",
                            "active_date": "20260627",
                            "all_dates": ["20260627"],
                            "stale_keys": [],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 1.0},
                        "api_execution_guard": {"qualification_retry_allowed_now": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 0}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            definition_packet_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_definition_review_operator_packet_v1",
                        "ok": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "source_payload_exposed": True,
                        "trusted_status_write_allowed": False,
                        "raw_source_mutation_allowed": False,
                        "summary": {
                            "review_csv_record_count": 1,
                            "decision_blank_count": 1,
                            "completed_decision_count": 0,
                            "invalid_decision_count": 0,
                            "action_plan_action_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            _write_ksa_definition_packet_fixture(
                definition_packet_path,
                source_payload_exposed=True,
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
                ksa_definition_operator_packet_path=definition_packet_path,
            )

        ontology_requirement = next(
            requirement
            for requirement in report["requirements"]
            if requirement["name"] == "ontology definition review"
        )
        self.assertEqual(ontology_requirement["status"], "open")
        self.assertIn("Definition operator packet safety check failed", ontology_requirement["notes"])
        self.assertFalse(
            report["supporting_snapshots"]["ksa_definition_review_operator_packet"]["safety_ok"]
        )

    def test_remaining_blockers_prefers_session_local_blocker_ranked_seedpack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260630_7h_extension.json"
            triage_path = tmp_path / "review_triage_20260630_7h_extension.json"
            queue_path = tmp_path / "aihr_agent_queue_release_status_20260630_7h_extension.json"
            hygiene_path = tmp_path / "qualification_retry_hygiene_20260630_7h_extension.json"
            api_linkage_path = tmp_path / "api_linkage_summary_20260630_7h_extension.json"
            review_seedpack_path = (
                tmp_path / "aihr_review_seedpack_blocker_ranked_20260630_7h_extension.jsonl"
            )
            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "blockers": [],
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            triage_path.write_text(
                json.dumps(
                    {
                        "summary": {
                            "source_paths": {
                                "review_priority_report": str(
                                    tmp_path / "review_priority_20260630_7h_extension.json"
                                ),
                                "transition_seedpack": str(
                                    tmp_path
                                    / "aihr_transition_scenario_seedpack_20260630_7h_extension.jsonl"
                                ),
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path.write_text(
                json.dumps({"summary": {"item_count": 0}, "items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps({"coverage_gap": {"collection_coverage": 0.5}}, ensure_ascii=False),
                encoding="utf-8",
            )
            api_linkage_path.write_text(json.dumps({"summary": {}}, ensure_ascii=False), encoding="utf-8")
            review_seedpack_path.write_text(
                json.dumps(
                    {
                        "record_type": "batch",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            report = build_remaining_blockers_report_from_files(
                release_readiness_path=release_path,
                review_triage_path=triage_path,
                queue_status_path=queue_path,
                qualification_hygiene_path=hygiene_path,
                api_linkage_path=api_linkage_path,
            )

        self.assertEqual(
            report["latest_supporting_reports"]["review_seedpack"],
            review_seedpack_path.name,
        )
        self.assertEqual(report["review_artifact_date_alignment"]["status"], "aligned")
        self.assertNotIn(
            "latest_supporting_reports.review_seedpack",
            report["review_artifact_date_alignment"]["stale_keys"],
        )

    def test_goal_completion_audit_surfaces_stale_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness_20260626.json"
            remaining_path = tmp_path / "remaining.json"
            backlog_path = tmp_path / "backlog.json"
            hygiene_path = tmp_path / "hygiene.json"
            ontology_seedpack_path = tmp_path / "aihr_ontology_definition_review_seedpack_20260626.jsonl"
            markdown_path = tmp_path / "audit.md"

            release_path.write_text(
                json.dumps(
                    {
                        "release_ready": False,
                        "checks": {
                            "deployment_runbook": {"ok": True},
                            "productization_strategy": {"ok": True},
                        },
                        "demo_contract": {"ok": True},
                        "dashboard_surface_contract": {"ok": True},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            remaining_path.write_text(
                json.dumps(
                    {
                        "latest_supporting_reports": {
                            "review_priority": "reports/review_priority_20260625.json",
                            "review_triage": "reports/review_triage_20260625.json",
                            "transition_seedpack": "reports/transition_seedpack_20260625.jsonl",
                            "review_seedpack": "reports/review_seedpack_20260625.jsonl",
                        },
                        "review_artifact_date_alignment": {
                            "status": "stale_against_active_date",
                            "active_date": "20260626",
                            "all_dates": ["20260625", "20260626"],
                            "stale_keys": [
                                "latest_supporting_reports.review_priority",
                                "latest_supporting_reports.review_triage",
                                "latest_supporting_reports.transition_seedpack",
                                "latest_supporting_reports.review_seedpack",
                            ],
                        },
                        "remaining_blockers": [
                            {"name": "review_debt:human_reviewed_concepts"},
                            {"name": "review_debt:human_reviewed_goal_links"},
                            {"name": "review_debt:human_reviewed_task_relations"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            backlog_path.write_text(
                json.dumps(
                    {
                        "review_artifact_date_alignment": {
                            "status": "stale_against_active_date",
                            "active_date": "20260626",
                            "all_dates": ["20260625", "20260626"],
                            "stale_keys": [
                                "source_paths.review_triage",
                                "source_paths.blocker_ranked_seedpack",
                            ],
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            hygiene_path.write_text(
                json.dumps(
                    {
                        "coverage_gap": {"collection_coverage": 0.22},
                        "api_execution_guard": {"status": "blocked", "qualification_retry_allowed_now": False},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            ontology_seedpack_path.write_text(
                json.dumps({"record_type": "batch", "item_count": 1}, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            report = build_goal_completion_audit_report_from_files(
                release_readiness_path=release_path,
                remaining_blockers_path=remaining_path,
                human_review_backlog_path=backlog_path,
                qualification_hygiene_path=hygiene_path,
                ontology_seedpack_path=ontology_seedpack_path,
            )
            write_goal_completion_audit_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        freshness = report["review_artifact_freshness"]
        self.assertEqual(freshness["status"], "stale_against_active_date")
        self.assertIn(
            "remaining_blockers.latest_supporting_reports.review_triage",
            freshness["stale_keys"],
        )
        self.assertIn("Freshness warning", report["requirements"][3]["notes"])
        self.assertIn("review_priority", report["requirements"][3]["notes"])
        self.assertIn("Freshness warning", report["requirements"][4]["notes"])
        self.assertIn("review_triage", report["requirements"][4]["notes"])
        self.assertIn("Freshness warning", report["requirements"][5]["notes"])
        self.assertIn("review_seedpack", report["requirements"][5]["notes"])
        self.assertIn("Review Artifact Freshness", markdown)
        self.assertIn("stale_against_active_date", markdown)


if __name__ == "__main__":
    unittest.main()
