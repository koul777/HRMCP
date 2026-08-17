from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.agent_queue import (
    AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT,
    _machine_covers_declared_acceptance_check,
    _verify_review_triage_machine_contract,
    _redact_sensitive_output,
    _split_agent_queue_command,
    build_agent_queue_status,
    build_agent_queue_status_from_file,
    build_ncs006_guarded_api_gate,
    run_agent_queue_ready_from_file,
    write_agent_queue_run_markdown,
    write_agent_queue_status_markdown,
)


def queue_item(**overrides):
    item = {
        "id": "aihr-01-review",
        "priority": 3,
        "owner": "ontology-review-agent",
        "agent_file": ".agents/ontology-review-agent.md",
        "blocker": "review_debt:human_reviewed_concepts",
        "blocker_category": "human_review",
        "command": (
            "python scripts\\ncs_harness.py review-priority "
            "--out reports\\aihr_review_priority_20260617.json"
        ),
        "prerequisite_artifacts": [],
        "expected_artifacts": ["reports/aihr_review_priority_20260617.json"],
        "acceptance_checks": ["Record commands run."],
        "auto_runnable": True,
        "mutation_policy": "regenerate_reports_only",
        "requires_human_decision": False,
    }
    item.update(overrides)
    return item


class AgentQueueTests(unittest.TestCase):
    def test_build_agent_queue_status_classifies_ready_manual_and_blocked_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            (workspace / "reports").mkdir()
            (workspace / "reports" / "aihr_review_priority_20260617.json").write_text(
                "{}",
                encoding="utf-8",
            )
            queue = {
                "schema": "aihr_agent_work_queue_v1",
                "global_guardrails": ["Do not print service keys."],
                "items": [
                    queue_item(),
                    queue_item(
                        id="aihr-02-data",
                        priority=4,
                        owner="data-collection-agent",
                        agent_file=".agents/data-collection-agent.md",
                        blocker="qualification:collection_coverage",
                        blocker_category="data_collection",
                        command=(
                            "python scripts\\ncs_harness.py retry-qualification-errors "
                            "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                            "--request-delay 3 --max-retries 1 "
                            "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                        ),
                        expected_artifacts=["data/processed/ncs.db"],
                        auto_runnable=False,
                        mutation_policy="guarded_api_collection",
                    ),
                    queue_item(
                        id="aihr-03-human",
                        priority=3,
                        command="python scripts\\ncs_harness.py export-review-seedpack --out reports\\seedpack.jsonl",
                        expected_artifacts=["reports/seedpack.jsonl"],
                        auto_runnable=True,
                        mutation_policy="regenerate_reports_only",
                        requires_human_decision=True,
                    ),
                    queue_item(
                        id="aihr-03-triage",
                        priority=3,
                        command="python scripts\\ncs_harness.py review-triage --out reports\\triage.json",
                        prerequisite_artifacts=["reports/missing_quality.json"],
                        mutation_policy="requires_existing_artifacts",
                    ),
                ],
            }

            report = build_agent_queue_status(queue, workspace=workspace)
            markdown_path = workspace / "queue_status.md"
            write_agent_queue_status_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["item_count"], 4)
        self.assertEqual(report["summary"]["auto_startable_count"], 1)
        self.assertEqual(report["summary"]["manual_ready_count"], 2)
        self.assertEqual(report["summary"]["manual_human_decision_count"], 1)
        self.assertEqual(report["summary"]["guarded_manual_count"], 2)
        self.assertEqual(
            report["summary"]["manual_classification_counts"],
            {
                "human_decision_required": 1,
                "operator_timed_guarded_api_collection": 1,
            },
        )
        self.assertEqual(report["summary"]["blocked_count"], 1)
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["execution_order"][0]["id"], "aihr-01-review")
        self.assertEqual(
            report["execution_order"][0]["blocker_display_label"],
            "needs explicit human review: ontology concept definitions",
        )
        self.assertEqual([item["id"] for item in report["manual_queue"]], ["aihr-03-human", "aihr-02-data"])
        self.assertFalse(report["manual_queue"][0]["can_start_automated"])
        self.assertEqual(report["manual_queue"][0]["automation_block_reason"], "requires_human_decision")
        self.assertEqual(
            report["manual_queue"][0]["manual_classification"],
            "human_decision_required",
        )
        self.assertEqual(
            report["manual_queue"][0]["operator_action_recommended"],
            "collect_explicit_human_decision",
        )
        self.assertEqual(report["manual_queue"][0]["pending_human_decision_ids"], ["aihr-03-human"])
        self.assertEqual(report["blocked_queue"][0]["state"], "blocked_missing_prerequisites")
        self.assertEqual(
            report["blocked_queue"][0]["automation_block_reason"],
            "missing_prerequisite_artifacts",
        )
        self.assertEqual([item["id"] for item in report["fallback_actions"]], ["aihr-01-review"])
        self.assertEqual(report["next_fallback_action"]["id"], "aihr-01-review")
        data_item = next(item for item in report["manual_queue"] if item["id"] == "aihr-02-data")
        self.assertEqual(data_item["automation_block_reason"], "guarded_api_collection")
        self.assertEqual(
            data_item["manual_classification"],
            "operator_timed_guarded_api_collection",
        )
        self.assertEqual(
            data_item["operator_action_recommended"],
            "operator_timed_guarded_api_collection",
        )
        self.assertEqual(data_item["pending_human_decision_ids"], [])
        self.assertEqual(data_item["expected_artifacts"], ["configured_ncs_database"])
        self.assertEqual(data_item["missing_expected_artifacts"], ["configured_ncs_database"])
        self.assertEqual(report["items"][0]["blocker"], "review_debt:human_reviewed_concepts")
        self.assertIn("needs explicit human review: ontology concept definitions", markdown)
        self.assertIn("no human decision required for this report-generation step", markdown)
        self.assertIn("aihr-03-human", markdown)
        self.assertIn("manual_human_decision_count: 1", markdown)
        self.assertIn("guarded_manual_count: 2", markdown)
        self.assertIn("manual_classification_counts:", markdown)
        self.assertIn("requires_human_decision=true", markdown)
        self.assertIn("guarded_manual=true", markdown)
        self.assertIn("manual_classification=human_decision_required", markdown)
        self.assertIn("block_reason=requires_human_decision", markdown)
        self.assertIn("operator_action=operator_timed_guarded_api_collection", markdown)
        serialized = json.dumps(report, ensure_ascii=False).replace("\\\\", "/")
        self.assertNotIn("data/processed/ncs.db", serialized)

    def test_build_agent_queue_status_suppresses_database_expected_artifact_paths(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-db-target",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    blocker="qualification:collection_coverage",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    expected_artifacts=[
                        "data/processed/ncs.db",
                        "C:/workspace/NCS_MCP/data/processed/ncs.db",
                    ],
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")

            report = build_agent_queue_status(queue, workspace=workspace)
            markdown_path = workspace / "reports" / "queue_status.md"
            write_agent_queue_status_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        serialized = json.dumps(report, ensure_ascii=False).replace("\\\\", "/")
        self.assertIn("configured_ncs_database", serialized)
        self.assertNotIn("data/processed/ncs.db", serialized)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized)
        self.assertNotIn("data/processed/ncs.db", markdown)
        self.assertNotIn("C:/workspace/NCS_MCP", markdown)

    def test_build_agent_queue_status_sanitizes_command_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue = {
                "schema": "aihr_agent_work_queue_v1",
                "items": [
                    queue_item(
                        command=(
                            "python scripts\\ncs_harness.py review-priority "
                            f"--out {workspace}\\reports\\aihr_review_priority_20260617.json "
                            "--db data/processed/ncs.db"
                        ),
                        prerequisite_commands=[
                            (
                                "python scripts\\ncs_harness.py export-review-seedpack "
                                f"--source {workspace}\\reports\\source.json "
                                "--db C:/workspace/NCS_MCP/data/processed/ncs.db"
                            )
                        ],
                    )
                ],
            }

            report = build_agent_queue_status(queue, workspace=workspace)

        serialized = json.dumps(report, ensure_ascii=False).replace("\\\\", "/")
        self.assertIn("reports/aihr_review_priority_20260617.json", serialized)
        self.assertIn("reports/source.json", serialized)
        self.assertIn("configured_ncs_database", serialized)
        self.assertNotIn(str(workspace).replace("\\", "/"), serialized)
        self.assertNotIn("C:/workspace/NCS_MCP", serialized)
        self.assertNotIn("data/processed/ncs.db", serialized)
        self.assertEqual(report["execution_order"][0]["command"], report["items"][0]["command"])

    def test_guarded_api_collection_requires_rate_limit_guards(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command="python scripts\\ncs_harness.py retry-qualification-errors --limit-units 10",
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")

            report = build_agent_queue_status(queue, workspace=workspace)

        self.assertEqual(report["blocked_queue"][0]["state"], "blocked_safety")
        self.assertIn(
            "missing_guard_flag:--stop-after-rate-limit-errors",
            report["blocked_queue"][0]["safety_violations"],
        )

    def test_guarded_api_collection_blocks_during_ncs006_cooldown(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260620.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-20T15:25:48+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_active",
                            "cooldown_until": "2026-06-20T16:23:01+00:00",
                        },
                        "next_safe_action": {
                            "status": "wait_for_rate_limit_cooldown",
                            "api_call_allowed_now": False,
                            "blocked_automation": [
                                "retry_qualification_api_during_ncs006_cooldown"
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["blocked_count"], 1)
        blocked_item = report["blocked_queue"][0]
        self.assertEqual(blocked_item["state"], "blocked_safety")
        self.assertFalse(blocked_item["preflight_ok"])
        self.assertIn(
            "ncs006_checkpoint_api_call_not_allowed",
            blocked_item["safety_violations"],
        )
        self.assertIn(
            "ncs006_blocks_qualification_retry_during_cooldown",
            blocked_item["safety_violations"],
        )
        self.assertIn(
            "ncs006_qualification_retry_not_allowed",
            blocked_item["safety_violations"],
        )
        self.assertEqual(blocked_item["operational_guard"]["status"], "blocked")
        self.assertFalse(blocked_item["operational_guard"]["qualification_retry_allowed_now"])

    def test_guarded_api_collection_allows_qualification_retry_without_cooldown(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260623.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-23T10:01:39+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_consumed_by_later_activity",
                            "cooldown_until": None,
                        },
                        "next_safe_action": {
                            "status": "start_guarded_watchdog_if_no_active_process",
                            "api_call_allowed_now": False,
                            "blocked_automation": [
                                "retry_qualification_api_during_ncs006_cooldown"
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )
            markdown_path = workspace / "queue_status.md"
            write_agent_queue_status_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["blocked_count"], 0)
        self.assertEqual(report["summary"]["manual_ready_count"], 1)
        manual_item = report["manual_queue"][0]
        self.assertFalse(manual_item["safety_violations"])
        self.assertEqual(
            manual_item["operational_guard"]["next_safe_action_status"],
            "start_guarded_watchdog_if_no_active_process",
        )
        self.assertFalse(manual_item["operational_guard"]["api_call_allowed_now"])
        self.assertTrue(manual_item["operational_guard"]["qualification_retry_allowed_now"])
        self.assertEqual(
            manual_item["operational_guard"]["checkpoint_path"],
            "reports/checkpoint_ncs006_element_api_status_20260623.json",
        )
        self.assertNotIn(str(workspace), json.dumps(report, ensure_ascii=False))
        self.assertIn("api_call_allowed_now=false", markdown)
        self.assertIn("qualification_retry_allowed_now=true", markdown)
        self.assertIn("next_safe_action_status=start_guarded_watchdog_if_no_active_process", markdown)
        self.assertNotIn("command: `disabled_until_guard_allows_api_call`", markdown)
        self.assertIn("retry-qualification-errors", markdown)

    def test_guarded_api_collection_allows_qualification_collect_without_cooldown(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py collect-qualification-items --all-units "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 2 --max-retries 1 "
                        "--retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 "
                        "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_20260623_current.json"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260623_current.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-23T10:01:39+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_consumed_by_later_activity",
                            "cooldown_until": None,
                        },
                        "next_safe_action": {
                            "status": "complete_no_collection_needed",
                            "api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )
            markdown_path = workspace / "queue_status.md"
            write_agent_queue_status_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["blocked_count"], 0)
        self.assertEqual(report["summary"]["manual_ready_count"], 1)
        manual_item = report["manual_queue"][0]
        self.assertFalse(manual_item["safety_violations"])
        self.assertTrue(manual_item["operational_guard"]["qualification_retry_allowed_now"])
        self.assertIn("collect-qualification-items", markdown)
        self.assertNotIn("disabled_until_guard_allows_api_call", markdown)

    def test_guarded_api_collection_respects_explicit_qualification_retry_allow(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260623.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-23T10:01:39+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_consumed_by_later_activity",
                            "cooldown_until": None,
                        },
                        "next_safe_action": {
                            "status": "future_status_name",
                            "api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )

        self.assertTrue(report["ok"])
        manual_item = report["manual_queue"][0]
        self.assertFalse(manual_item["safety_violations"])
        self.assertFalse(manual_item["operational_guard"]["api_call_allowed_now"])
        self.assertTrue(manual_item["operational_guard"]["qualification_retry_allowed_now"])
        self.assertEqual(
            manual_item["operational_guard"]["qualification_retry_guard_reason"],
            "checkpoint_explicit",
        )

    def test_guarded_api_gate_prefers_newer_generated_at_over_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            reports_dir = workspace / "reports"
            reports_dir.mkdir()
            old_checkpoint = reports_dir / "checkpoint_ncs006_element_api_status_20260620.json"
            new_checkpoint = reports_dir / "checkpoint_ncs006_element_api_status_20260625.json"
            old_checkpoint.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-20T00:00:00+00:00",
                        "next_safe_action": {
                            "status": "wait_for_rate_limit_cooldown",
                            "api_call_allowed_now": False,
                        },
                        "rate_limit_cooldown": {"status": "cooldown_active"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            new_checkpoint.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-25T00:00:00+00:00",
                        "next_safe_action": {
                            "status": "start_guarded_watchdog_if_no_active_process",
                            "api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.utime(old_checkpoint, (2_000_000_000, 2_000_000_000))

            guard = build_ncs006_guarded_api_gate(workspace=workspace)

        self.assertEqual(Path(guard["checkpoint_path"]).name, new_checkpoint.name)
        self.assertEqual(guard["status"], "allowed")
        self.assertTrue(guard["qualification_retry_allowed_now"])

    def test_guarded_api_queue_status_prefers_queue_local_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            reports_dir = workspace / "reports"
            session_dir = reports_dir / "overnight_sessions" / "readonly_refresh"
            session_dir.mkdir(parents=True)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            root_checkpoint = reports_dir / "checkpoint_ncs006_element_api_status_20260629_current.json"
            local_checkpoint = session_dir / "checkpoint_ncs006_element_api_status_20260630_7h_extension.json"
            root_checkpoint.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-30T23:00:00+00:00",
                        "next_safe_action": {
                            "status": "wait_for_rate_limit_cooldown",
                            "api_call_allowed_now": False,
                        },
                        "rate_limit_cooldown": {"status": "cooldown_active"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            local_checkpoint.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-30T00:00:00+00:00",
                        "next_safe_action": {
                            "status": "start_guarded_watchdog_if_no_active_process",
                            "api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            queue_path = session_dir / "aihr_agent_queue_release_20260630_7h_extension.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                owner="data-collection-agent",
                                agent_file=".agents/data-collection-agent.md",
                                command=(
                                    "python scripts\\ncs_harness.py retry-qualification-errors "
                                    "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                                    "--request-delay 3 --max-retries 1 "
                                    "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                                ),
                                auto_runnable=False,
                                mutation_policy="guarded_api_collection",
                                expected_artifacts=[],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status_from_file(queue_path, workspace=workspace)

        item = report["items"][0]
        self.assertEqual(
            Path(item["operational_guard"]["checkpoint_path"]).name,
            local_checkpoint.name,
        )
        self.assertFalse(item["safety_violations"])

    def test_guarded_api_collection_blocks_unknown_checkpoint_status(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260623.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-23T10:01:39+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_consumed_by_later_activity",
                            "cooldown_until": None,
                        },
                        "next_safe_action": {
                            "status": "future_status_name",
                            "api_call_allowed_now": False,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )

        self.assertFalse(report["ok"])
        blocked_item = report["blocked_queue"][0]
        self.assertIsNone(blocked_item["operational_guard"]["qualification_retry_allowed_now"])
        self.assertIn(
            "ncs006_qualification_retry_permission_unknown",
            blocked_item["safety_violations"],
        )
        self.assertIn(
            "ncs006_checkpoint_api_call_not_allowed",
            blocked_item["safety_violations"],
        )

    def test_guarded_api_collection_expired_cooldown_switches_to_refresh_resolution(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "items": [
                queue_item(
                    id="aihr-qualification",
                    owner="data-collection-agent",
                    agent_file=".agents/data-collection-agent.md",
                    command=(
                        "python scripts\\ncs_harness.py retry-qualification-errors "
                        "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                        "--request-delay 3 --max-retries 1 "
                        "--retry-backoff-seconds 120 --stop-after-rate-limit-errors 2"
                    ),
                    auto_runnable=False,
                    mutation_policy="guarded_api_collection",
                )
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "data-collection-agent.md").write_text("role", encoding="utf-8")
            checkpoint_path = workspace / "reports" / "checkpoint_ncs006_element_api_status_20260619.json"
            checkpoint_path.parent.mkdir()
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-19T10:01:39+00:00",
                        "rate_limit_cooldown": {
                            "status": "cooldown_active",
                            "cooldown_until": "2026-06-19T10:02:39+00:00",
                        },
                        "next_safe_action": {
                            "status": "wait_for_rate_limit_cooldown",
                            "api_call_allowed_now": False,
                            "blocked_automation": [
                                "retry_qualification_api_during_ncs006_cooldown"
                            ],
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status(
                queue,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )

        blocked_item = report["blocked_queue"][0]
        self.assertIn(
            "ncs006_stale_checkpoint_after_cooldown",
            blocked_item["safety_violations"],
        )
        self.assertFalse(blocked_item["operational_guard"]["qualification_retry_allowed_now"])
        self.assertEqual(
            blocked_item["operational_guard"]["next_safe_action_resolution_status"],
            "refresh_qualification_retry_hygiene_before_retry",
        )

    def test_build_agent_queue_status_from_file_and_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            markdown_path = workspace / "queue_status.md"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "global_guardrails": ["Do not auto-approve review statuses."],
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            write_agent_queue_status_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report.get("workspace_ref"), "configured_workspace")
        self.assertNotIn("workspace", report)
        self.assertEqual(report["source_queue_path"], "queue.json")
        self.assertIn("# AI-HR Agent Queue Status", markdown)
        self.assertIn("status_update_allowed: False", markdown)
        self.assertIn("db_writes: False", markdown)
        self.assertIn("approval_claim: False", markdown)
        self.assertIn("Automated Start Order", markdown)
        self.assertIn("can_start_automated=true", markdown)
        self.assertIn("auto_startable_policy", markdown)
        self.assertIn("Do not auto-approve review statuses.", markdown)
        self.assertNotIn(str(workspace), json.dumps(report, ensure_ascii=False))
        self.assertNotIn(str(workspace), markdown)

    def test_build_agent_queue_status_from_file_resolves_relative_path_against_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            (workspace / "queue.json").write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_agent_queue_status_from_file(Path("queue.json"), workspace=workspace)

        self.assertTrue(report["ok"])
        self.assertEqual(report["source_queue_path"], "queue.json")

    def test_run_agent_queue_ready_dry_run_reports_safe_command_args(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)
            expected_queue_hash = "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()

        self.assertTrue(report["ok"])
        self.assertEqual(report.get("workspace_ref"), "configured_workspace")
        self.assertNotIn("workspace", report)
        self.assertEqual(report["source_queue_path"], "queue.json")
        self.assertNotIn(str(workspace), json.dumps(report, ensure_ascii=False))
        self.assertEqual(report["summary"]["candidate_count"], 1)
        self.assertEqual(report["summary"]["selected_item_ids"], ["aihr-01-review"])
        self.assertEqual(report["source_queue_sha256"], expected_queue_hash)
        self.assertRegex(report["queue_status_snapshot_sha256"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(report["runs"][0]["status"], "dry_run")
        self.assertEqual(report["runs"][0]["args"][:3], ["python", "scripts\\ncs_harness.py", "review-priority"])
        self.assertEqual(report["runs"][0]["acceptance_check_results"][0]["check"], "dry_run_only")
        self.assertEqual(report["runs"][0]["declared_acceptance_checks"], ["Record commands run."])

    def test_run_agent_queue_ready_resolves_relative_path_against_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(
                Path("queue.json"),
                workspace=workspace,
                dry_run=True,
            )
            expected_queue_hash = "sha256:" + hashlib.sha256(queue_path.read_bytes()).hexdigest()

        self.assertTrue(report["ok"])
        self.assertEqual(report["source_queue_path"], "queue.json")
        self.assertEqual(report["source_queue_sha256"], expected_queue_hash)

    def test_run_aihr_plan_demo_is_allowed_as_report_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "aihr-demo-runner-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="aihr-demo",
                                owner="aihr-demo-runner-agent",
                                agent_file=".agents/aihr-demo-runner-agent.md",
                                blocker="aihr_demo_contract",
                                blocker_category="demo_contract",
                                command=(
                                    "python scripts\\ncs_harness.py run-aihr-plan-demo "
                                    "--out-dir reports --base-name aihr_plan_demo_20260617"
                                ),
                                expected_artifacts=[
                                    "reports/aihr_plan_demo_20260617.json",
                                    "reports/aihr_plan_demo_20260617.html",
                                ],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["blocked_count"], 0)
        self.assertEqual(status["execution_order"][0]["id"], "aihr-demo")
        self.assertEqual(report["runs"][0]["args"][:3], ["python", "scripts\\ncs_harness.py", "run-aihr-plan-demo"])

    def test_ontology_definition_seedpack_is_allowed_as_report_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="ontology-definition-seedpack",
                                command=(
                                    "python scripts\\ncs_harness.py export-ontology-definition-seedpack "
                                    "--limit 100 --per-issue-type-limit 50 "
                                    "--out reports\\aihr_ontology_definition_review_seedpack_20260617.jsonl "
                                    "--markdown-out reports\\aihr_ontology_definition_review_seedpack_20260617.md "
                                    "--csv-out reports\\aihr_ontology_definition_review_seedpack_20260617.csv "
                                    "--source-report-path reports\\aihr_review_priority_20260617.md"
                                ),
                                expected_artifacts=[
                                    "reports/aihr_ontology_definition_review_seedpack_20260617.jsonl",
                                    "reports/aihr_ontology_definition_review_seedpack_20260617.md",
                                    "reports/aihr_ontology_definition_review_seedpack_20260617.csv",
                                ],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 1)
        self.assertEqual(status["summary"]["blocked_count"], 0)
        self.assertEqual(status["execution_order"][0]["id"], "ontology-definition-seedpack")
        self.assertEqual(
            report["runs"][0]["args"][:3],
            ["python", "scripts\\ncs_harness.py", "export-ontology-definition-seedpack"],
        )

    def test_human_review_provenance_reconfirmation_proofset_is_allowed_as_report_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="human-review-provenance-reconfirmation",
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "export-human-review-provenance-reconfirmation-proofset "
                                    "--out reports\\human_review_provenance_reconfirmation_packet_20260629.json "
                                    "--markdown-out reports\\human_review_provenance_reconfirmation_packet_20260629.md "
                                    "--html-out reports\\human_review_provenance_reconfirmation_packet_20260629.html "
                                    "--decision-sheet-out reports\\human_review_provenance_reconfirmation_decision_sheet_20260629.json "
                                    "--decision-sheet-csv-out reports\\human_review_provenance_reconfirmation_decision_sheet_20260629.csv "
                                    "--decision-sheet-html-out reports\\human_review_provenance_reconfirmation_decision_sheet_20260629.html "
                                    "--decision-sheet-markdown-out reports\\human_review_provenance_reconfirmation_decision_sheet_20260629.md "
                                    "--decision-audit-out reports\\human_review_provenance_reconfirmation_decision_audit_20260629.json "
                                    "--decision-audit-markdown-out reports\\human_review_provenance_reconfirmation_decision_audit_20260629.md"
                                ),
                                expected_artifacts=[
                                    "reports/human_review_provenance_reconfirmation_packet_20260629.json",
                                    "reports/human_review_provenance_reconfirmation_packet_20260629.md",
                                    "reports/human_review_provenance_reconfirmation_packet_20260629.html",
                                    "reports/human_review_provenance_reconfirmation_decision_sheet_20260629.json",
                                    "reports/human_review_provenance_reconfirmation_decision_sheet_20260629.csv",
                                    "reports/human_review_provenance_reconfirmation_decision_audit_20260629.json",
                                ],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 1)
        self.assertEqual(status["summary"]["blocked_count"], 0)
        self.assertEqual(status["execution_order"][0]["id"], "human-review-provenance-reconfirmation")
        self.assertEqual(
            report["runs"][0]["args"][:3],
            ["python", "scripts\\ncs_harness.py", "export-human-review-provenance-reconfirmation-proofset"],
        )

    def test_human_review_provenance_reconfirmation_packet_only_is_not_auto_runnable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="human-review-provenance-packet-only",
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "export-human-review-provenance-reconfirmation-packet "
                                    "--out reports\\human_review_provenance_reconfirmation_packet_20260629.json "
                                    "--markdown-out reports\\human_review_provenance_reconfirmation_packet_20260629.md "
                                    "--html-out reports\\human_review_provenance_reconfirmation_packet_20260629.html"
                                ),
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertFalse(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 0)
        self.assertEqual(status["summary"]["blocked_count"], 1)
        self.assertIn("regenerate_reports_only_command_not_recognized_as_read_only", status["blocked_queue"][0]["safety_violations"])
        self.assertEqual(report["runs"], [])

    def test_review_artifact_readability_audit_is_allowed_as_report_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "evaluation-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="review-artifact-readability-audit",
                                owner="evaluation-agent",
                                agent_file=".agents/evaluation-agent.md",
                                blocker="review_artifact:readability_audit",
                                blocker_category="review_artifact_quality",
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "audit-review-artifact-readability "
                                    "--reports-dir reports "
                                    "--out reports\\review_artifact_readability_audit_20260629.json "
                                    "--markdown-out reports\\review_artifact_readability_audit_20260629.md"
                                ),
                                expected_artifacts=[
                                    "reports/review_artifact_readability_audit_20260629.json",
                                    "reports/review_artifact_readability_audit_20260629.md",
                                ],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 1)
        self.assertEqual(status["summary"]["blocked_count"], 0)
        self.assertEqual(status["execution_order"][0]["id"], "review-artifact-readability-audit")
        self.assertEqual(
            report["runs"][0]["args"][:3],
            ["python", "scripts\\ncs_harness.py", "audit-review-artifact-readability"],
        )

    def test_release_report_regeneration_commands_are_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "evaluation-agent.md").write_text("role", encoding="utf-8")
            (workspace / ".agents" / "aihr-demo-runner-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="quality-gates",
                                owner="evaluation-agent",
                                agent_file=".agents/evaluation-agent.md",
                                blocker="missing_quality_gate:transition_eval",
                                blocker_category="quality",
                                command=(
                                    "python scripts\\ncs_harness.py quality-gates "
                                    "--include-transition-eval --transition-limit 5 "
                                    "--out reports\\aihr_quality_gates_with_transition_20260617.json "
                                    "--markdown-out reports\\aihr_quality_gates_with_transition_20260617.md"
                                ),
                                expected_artifacts=[
                                    "reports/aihr_quality_gates_with_transition_20260617.json",
                                    "reports/aihr_quality_gates_with_transition_20260617.md",
                                ],
                            ),
                            queue_item(
                                id="dashboard-verify",
                                owner="aihr-demo-runner-agent",
                                agent_file=".agents/aihr-demo-runner-agent.md",
                                blocker="aihr_dashboard_surface",
                                blocker_category="demo_contract",
                                command=(
                                    "python scripts\\ncs_harness.py verify-aihr-dashboard "
                                    "--base-url http://127.0.0.1:8765 "
                                    "--out reports\\aihr_dashboard_surface_verification_20260617.json "
                                    "--markdown-out reports\\aihr_dashboard_surface_verification_20260617.md"
                                ),
                                expected_artifacts=[
                                    "reports/aihr_dashboard_surface_verification_20260617.json",
                                    "reports/aihr_dashboard_surface_verification_20260617.md",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 2)
        self.assertEqual(status["summary"]["blocked_count"], 0)
        self.assertEqual(
            {item["id"] for item in status["execution_order"]},
            {"quality-gates", "dashboard-verify"},
        )
        self.assertTrue(all(item["state"] == "ready_to_start" for item in status["execution_order"]))
        self.assertTrue(all(item["can_start_automated"] for item in status["execution_order"]))
        self.assertEqual(
            {run["args"][2] for run in report["runs"]},
            {"quality-gates", "verify-aihr-dashboard"},
        )

    def test_inspect_only_items_are_not_auto_startable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "project-maintainer.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="inspect-only",
                                owner="project-maintainer",
                                agent_file=".agents/project-maintainer.md",
                                command="python scripts\\ncs_harness.py inspect",
                                expected_artifacts=[],
                                auto_runnable=True,
                                mutation_policy="inspect_only",
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            markdown_path = workspace / "queue_status.md"
            write_agent_queue_status_markdown(status, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 0)
        self.assertEqual(status["items"][0]["state"], "manual_ready")
        self.assertFalse(status["items"][0]["can_start_automated"])
        self.assertIn("state=manual_ready", markdown)
        self.assertIn("can_start_automated=false", markdown)
        self.assertEqual(status["execution_order"], [])
        self.assertEqual(status["fallback_actions"], [])
        self.assertEqual(report["summary"]["candidate_count"], 0)
        self.assertEqual(report["runs"], [])

    def test_verify_aihr_dashboard_queue_command_requires_loopback_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "aihr-demo-runner-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="dashboard-verify",
                                owner="aihr-demo-runner-agent",
                                agent_file=".agents/aihr-demo-runner-agent.md",
                                blocker="aihr_dashboard_surface",
                                blocker_category="demo_contract",
                                command=(
                                    "python scripts\\ncs_harness.py verify-aihr-dashboard "
                                    "--base-url https://example.com "
                                    "--out reports\\aihr_dashboard_surface_verification_20260617.json "
                                    "--markdown-out reports\\aihr_dashboard_surface_verification_20260617.md"
                                ),
                                expected_artifacts=[
                                    "reports/aihr_dashboard_surface_verification_20260617.json",
                                    "reports/aihr_dashboard_surface_verification_20260617.md",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(queue_path, workspace=workspace)
            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, dry_run=True)

        blocked = status["blocked_queue"][0]
        self.assertEqual(blocked["state"], "blocked_safety")
        self.assertIn(
            "verify_aihr_dashboard_base_url_not_loopback",
            blocked["safety_violations"],
        )
        self.assertEqual(status["summary"]["auto_startable_count"], 0)
        self.assertEqual(report["runs"], [])
        with self.assertRaisesRegex(ValueError, "verify_aihr_dashboard_base_url_not_loopback"):
            _split_agent_queue_command(blocked["command"])

    def test_run_agent_queue_ready_executes_only_auto_startable_reports_only_items(self) -> None:
        calls = []

        def fake_runner(args, **kwargs):
            calls.append((args, kwargs))
            output = Path(kwargs["cwd"]) / "reports" / "aihr_review_priority_20260617.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"ok": true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(),
                            queue_item(
                                id="manual-review-seedpack",
                                command="python scripts\\ncs_harness.py export-review-seedpack --out reports\\seedpack.jsonl",
                                expected_artifacts=["reports/seedpack.jsonl"],
                                requires_human_decision=True,
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown_path = workspace / "run.md"

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], sys.executable)
        self.assertEqual(calls[0][0][2], "review-priority")
        self.assertEqual(report["summary"]["succeeded_count"], 1)
        self.assertEqual(report["summary"]["selected_count"], 1)
        self.assertTrue(all(item["ok"] for item in report["runs"][0]["acceptance_check_results"]))
        self.assertFalse(report["runs"][0]["acceptance_verified"])
        self.assertEqual(
            report["runs"][0]["acceptance_verification_status"],
            "declared_checks_recorded_not_auto_verified",
        )
        self.assertEqual(report["summary"]["acceptance_failed_count"], 0)
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 1)
        self.assertTrue(report["runs"][0]["expected_artifact_checks"][0]["non_empty"])
        self.assertEqual(
            report["runs"][0]["blocker_display_label"],
            "needs explicit human review: ontology concept definitions",
        )
        self.assertIn("needs explicit human review: ontology concept definitions", markdown)
        self.assertIn("acceptance_unverified_count: 1", markdown)
        self.assertIn(
            "acceptance: execution_artifacts_ok; declared_checks=1 recorded_not_auto_verified",
            markdown,
        )
        self.assertIn("AI-HR Agent Queue Automated Run", markdown)
        self.assertIn("status_update_allowed: False", markdown)
        self.assertIn("db_writes: False", markdown)
        self.assertIn("approval_claim: False", markdown)
        self.assertIn("source_queue_sha256: sha256:", markdown)
        self.assertIn("queue_status_snapshot_sha256: sha256:", markdown)
        self.assertIn("output: stdout_chars=12 tail_chars=12 truncated=False redacted=False redactions=0", markdown)

    def test_run_agent_queue_ready_machine_verifies_review_seedpack_contract(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and remains review-pending.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown_path = workspace / "run.md"

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertTrue(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "machine_contract_verified_non_decisional",
        )
        self.assertEqual(run["machine_contract_check_count"], 2)
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 0)
        self.assertEqual(report["summary"]["acceptance_machine_verified_count"], 1)
        self.assertIn("acceptance_machine_verified_count: 1", markdown)
        self.assertIn("machine_contract_verified_non_decisional", markdown)

    def test_run_agent_queue_ready_allows_no_declared_acceptance_checks(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "scripts").mkdir()
            (workspace / "scripts" / "ncs_harness.py").write_text("# harness", encoding="utf-8")
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                expected_artifacts=[
                                    "reports/aihr_review_priority_20260617.json"
                                ],
                                acceptance_checks=[],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(
                queue_path,
                workspace=workspace,
                runner=fake_runner,
            )

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertEqual(report["summary"]["acceptance_failed_count"], 0)
        self.assertEqual(run["acceptance_failed_check_count"], 0)
        self.assertTrue(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "no_declared_acceptance_checks",
        )
        self.assertNotIn(
            "declared_acceptance_checks_recorded",
            {check["check"] for check in run["acceptance_check_results"]},
        )

    def test_run_agent_queue_ready_machine_contract_keeps_manual_checks_unverified(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Record commands run and touched files in the handoff.",
                                    "Seedpack is JSONL and remains review-pending.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown_path = workspace / "run.md"

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "machine_contract_verified_manual_handoff_pending",
        )
        self.assertEqual(run["machine_verified_declared_acceptance_check_count"], 1)
        self.assertEqual(run["unverified_declared_acceptance_check_count"], 1)
        self.assertEqual(run["manual_unverified_declared_acceptance_check_count"], 1)
        self.assertEqual(run["machine_unverified_declared_acceptance_check_count"], 0)
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 1)
        self.assertEqual(report["summary"]["acceptance_unverified_declared_check_count"], 1)
        self.assertEqual(
            report["summary"]["acceptance_manual_unverified_declared_check_count"],
            1,
        )
        self.assertEqual(
            report["summary"]["acceptance_machine_unverified_declared_check_count"],
            0,
        )
        self.assertEqual(report["summary"]["acceptance_machine_verified_count"], 0)
        self.assertEqual(report["summary"]["acceptance_machine_partially_verified_count"], 0)
        self.assertEqual(
            report["summary"][
                "acceptance_machine_contract_manual_handoff_pending_count"
            ],
            1,
        )
        self.assertIn("machine_contract_verified_manual_handoff_pending", markdown)
        self.assertIn("manual_handoff_checks_pending=1", markdown)

    def _write_review_triage_contract_fixture(
        self,
        workspace: Path,
        *,
        payload_extra: dict[str, Any] | None = None,
        source_paths: dict[str, str] | None = None,
        quality_arg: str = "reports\\quality.json",
        review_priority_arg: str = "reports\\review_priority.json",
        transition_seedpack_arg: str = "reports\\transition_seedpack.jsonl",
    ) -> list[str]:
        reports = workspace / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        for path_text, content in (
            (quality_arg, "{}"),
            (review_priority_arg, "{}"),
            (transition_seedpack_arg, "{}\n"),
        ):
            path = Path(path_text)
            if not path.is_absolute():
                path = workspace / path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        out = reports / "triage.json"
        default_source_paths = {
            "quality_report": quality_arg,
            "review_priority_report": review_priority_arg,
            "transition_seedpack": transition_seedpack_arg,
        }
        payload = {
            "schema": "ncs_review_triage_v1",
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
            "summary": {
                "source_paths": source_paths or default_source_paths,
            },
        }
        if payload_extra:
            payload.update(payload_extra)
        out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return [
            "python",
            "scripts\\ncs_harness.py",
            "review-triage",
            "--quality-report",
            quality_arg,
            "--review-priority-report",
            review_priority_arg,
            "--transition-seedpack",
            transition_seedpack_arg,
            "--out",
            "reports\\triage.json",
        ]

    def test_review_triage_machine_contract_rejects_nested_status_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            args = self._write_review_triage_contract_fixture(
                workspace,
                payload_extra={
                    "rows": [
                        {
                            "review_status": "human_reviewed",
                            "accepted": True,
                        }
                    ]
                },
            )

            results = _verify_review_triage_machine_contract(
                args=args,
                workspace=workspace,
            )

        self.assertEqual(1, len(results))
        self.assertFalse(results[0]["ok"])
        self.assertIn("payload_safety_ok=False", results[0]["detail"])

    def test_review_triage_machine_contract_rejects_boolean_status_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            args = self._write_review_triage_contract_fixture(
                workspace,
                payload_extra={
                    "rows": [
                        {
                            "human_reviewed": True,
                            "reviewed": True,
                        }
                    ]
                },
            )

            results = _verify_review_triage_machine_contract(
                args=args,
                workspace=workspace,
            )

        self.assertEqual(1, len(results))
        self.assertFalse(results[0]["ok"])
        self.assertIn("payload_safety_ok=False", results[0]["detail"])

    def test_review_triage_machine_contract_rejects_absolute_source_path_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            expected = root / "expected"
            other = root / "other"
            workspace.mkdir()
            quality_arg = str(expected / "quality.json")
            review_priority_arg = str(expected / "review_priority.json")
            transition_seedpack_arg = str(expected / "transition_seedpack.jsonl")
            args = self._write_review_triage_contract_fixture(
                workspace,
                quality_arg=quality_arg,
                review_priority_arg=review_priority_arg,
                transition_seedpack_arg=transition_seedpack_arg,
                source_paths={
                    "quality_report": str(other / "quality.json"),
                    "review_priority_report": review_priority_arg,
                    "transition_seedpack": transition_seedpack_arg,
                },
            )

            results = _verify_review_triage_machine_contract(
                args=args,
                workspace=workspace,
            )

        self.assertEqual(1, len(results))
        self.assertFalse(results[0]["ok"])
        self.assertIn("source_paths_match_args=False", results[0]["detail"])

    def test_run_agent_queue_ready_machine_verifies_review_triage_readonly_contract(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            markdown_out = workspace / arg_value(args, "--markdown-out")
            quality = workspace / arg_value(args, "--quality-report")
            review_priority = workspace / arg_value(args, "--review-priority-report")
            transition_seedpack = workspace / arg_value(args, "--transition-seedpack")
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema": "ncs_review_triage_v1",
                "ok": True,
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "human_decision_required": True,
                "summary": {
                    "source_paths": {
                        "quality_report": str(quality.relative_to(workspace)),
                        "review_priority_report": str(review_priority.relative_to(workspace)),
                        "transition_seedpack": str(transition_seedpack.relative_to(workspace)),
                    }
                },
            }
            out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            markdown_out.write_text("# Review triage\n", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "scripts").mkdir()
            (workspace / "scripts" / "ncs_harness.py").write_text("# harness", encoding="utf-8")
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "training-goal-review-agent.md").write_text(
                "role",
                encoding="utf-8",
            )
            (workspace / "reports").mkdir()
            (workspace / "reports" / "quality.json").write_text("{}", encoding="utf-8")
            (workspace / "reports" / "review_priority.json").write_text("{}", encoding="utf-8")
            (workspace / "reports" / "transition_seedpack.jsonl").write_text(
                "{}\n",
                encoding="utf-8",
            )
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                id="aihr-02-training-goal",
                                owner="training-goal-review-agent",
                                agent_file=".agents/training-goal-review-agent.md",
                                command=(
                                    "python scripts\\ncs_harness.py review-triage "
                                    "--quality-report reports\\quality.json "
                                    "--review-priority-report reports\\review_priority.json "
                                    "--transition-seedpack reports\\transition_seedpack.jsonl "
                                    "--out reports\\triage.json "
                                    "--markdown-out reports\\triage.md"
                                ),
                                expected_artifacts=[
                                    "reports/triage.json",
                                    "reports/triage.md",
                                ],
                                acceptance_checks=[
                                    (
                                        "Confirm prerequisite review-priority, quality-gates, "
                                        "and transition seedpack artifacts exist before running."
                                    ),
                                    (
                                        "Review-triage report reads existing artifacts only and "
                                        "does not mutate review statuses."
                                    ),
                                    "Record commands run and touched files in the handoff.",
                                ],
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(
                queue_path,
                workspace=workspace,
                runner=fake_runner,
            )

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            "machine_contract_verified_manual_handoff_pending",
            run["acceptance_verification_status"],
        )
        self.assertEqual(1, run["machine_contract_check_count"])
        self.assertEqual(2, run["machine_verified_declared_acceptance_check_count"])
        self.assertEqual(1, run["manual_unverified_declared_acceptance_check_count"])
        self.assertEqual(0, run["machine_unverified_declared_acceptance_check_count"])

    def test_run_agent_queue_ready_machine_contract_reports_true_machine_gap(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and remains review-pending.",
                                    "Review summary reconciles with the operator packet.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(
                queue_path,
                workspace=workspace,
                runner=fake_runner,
            )

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "machine_contract_partially_verified_non_decisional",
        )
        self.assertEqual(run["machine_verified_declared_acceptance_check_count"], 1)
        self.assertEqual(run["manual_unverified_declared_acceptance_check_count"], 0)
        self.assertEqual(run["machine_unverified_declared_acceptance_check_count"], 1)
        self.assertEqual(
            report["summary"]["acceptance_machine_unverified_declared_check_count"],
            1,
        )
        self.assertEqual(report["summary"]["acceptance_machine_partially_verified_count"], 1)

    def test_run_agent_queue_ready_machine_verifies_ontology_definition_seedpack_contract(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_core_concept_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_core_concept_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "approved_definition": "",
                "proposed_target_review_status": "",
                "proposed_issue_resolution": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "approved_definition,proposed_target_review_status,"
                    "proposed_issue_resolution,status_update_allowed,db_writes,approval_claim,"
                    "human_decision_required\n"
                    "1,ontology_core_concept_human_review_required,,,,,,,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "export-ontology-definition-seedpack "
                                    "--out reports\\ontology_seedpack.jsonl "
                                    "--csv-out reports\\ontology_seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/ontology_seedpack.jsonl",
                                    "reports/ontology_seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and every item remains candidate/review-pending unless a human decides otherwise.",
                                    "Seedpack issue_types are limited to ontology definition blockers and status_update_allowed remains false.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertTrue(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "machine_contract_verified_non_decisional",
        )
        self.assertEqual(run["machine_verified_declared_acceptance_check_count"], 2)
        self.assertEqual(run["unverified_declared_acceptance_check_count"], 0)
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 0)
        self.assertEqual(report["summary"]["acceptance_machine_verified_count"], 1)

    def test_run_agent_queue_ready_machine_contract_fails_on_prefilled_decision(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "approve",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,approve,,,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and remains review-pending.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertFalse(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "execution_or_artifact_checks_failed",
        )
        self.assertGreaterEqual(report["summary"]["acceptance_failed_count"], 1)
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 0)

    def test_run_agent_queue_ready_machine_contract_fails_on_prefilled_status_proposal(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": True,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "proposed_target_review_status": "human_reviewed",
                "proposed_issue_resolution": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "proposed_target_review_status,proposed_issue_resolution,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,,,,,"
                    "human_reviewed,,False,False,False,True\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and remains review-pending.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertFalse(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "execution_or_artifact_checks_failed",
        )
        self.assertGreaterEqual(report["summary"]["acceptance_failed_count"], 1)

    def test_run_agent_queue_ready_machine_contract_fails_without_human_decision_required(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            out = workspace / arg_value(args, "--out")
            csv_out = workspace / arg_value(args, "--csv-out")
            out.parent.mkdir(parents=True, exist_ok=True)
            metadata = {
                "record_type": "metadata",
                "format_version": "ncs-review-seedpack-v1",
                "item_count": 1,
                "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "raw_source_mutation_allowed": False,
                "trusted_status_write_allowed": False,
                "human_decision_required": False,
            }
            row = {
                "record_type": "review_item",
                "format_version": "ncs-review-seedpack-v1",
                "issue_type": "ontology_task_ksa_relation_human_review_required",
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": False,
            }
            out.write_text(
                json.dumps(metadata, ensure_ascii=False) + "\n"
                + json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            csv_out.write_text(
                (
                    "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                    "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                    "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False,False\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py export-review-seedpack "
                                    "--issue-types ontology_task_ksa_relation_human_review_required "
                                    "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                ),
                                expected_artifacts=[
                                    "reports/seedpack.jsonl",
                                    "reports/seedpack.csv",
                                ],
                                acceptance_checks=[
                                    "Seedpack is JSONL and remains review-pending.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertFalse(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "execution_or_artifact_checks_failed",
        )
        self.assertGreaterEqual(report["summary"]["acceptance_failed_count"], 1)

    def test_run_agent_queue_ready_machine_contract_fails_when_human_decision_required_missing(self) -> None:
        def run_case(*, omit_metadata=False, omit_row=False, omit_csv_column=False):
            def arg_value(args, flag):
                return args[args.index(flag) + 1]

            def fake_runner(args, **kwargs):
                workspace = Path(kwargs["cwd"])
                out = workspace / arg_value(args, "--out")
                csv_out = workspace / arg_value(args, "--csv-out")
                out.parent.mkdir(parents=True, exist_ok=True)
                metadata = {
                    "record_type": "metadata",
                    "format_version": "ncs-review-seedpack-v1",
                    "item_count": 1,
                    "issue_types": ["ontology_task_ksa_relation_human_review_required"],
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "raw_source_mutation_allowed": False,
                    "trusted_status_write_allowed": False,
                    "human_decision_required": True,
                }
                row = {
                    "record_type": "review_item",
                    "format_version": "ncs-review-seedpack-v1",
                    "issue_type": "ontology_task_ksa_relation_human_review_required",
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "human_decision_required": True,
                }
                if omit_metadata:
                    metadata.pop("human_decision_required")
                if omit_row:
                    row.pop("human_decision_required")
                out.write_text(
                    json.dumps(metadata, ensure_ascii=False) + "\n"
                    + json.dumps(row, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )
                if omit_csv_column:
                    csv_out.write_text(
                        (
                            "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                            "status_update_allowed,db_writes,approval_claim\n"
                            "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False\n"
                        ),
                        encoding="utf-8",
                    )
                else:
                    csv_out.write_text(
                        (
                            "sequence,issue_type,decision,reviewer_id,reviewed_at,rationale,"
                            "status_update_allowed,db_writes,approval_claim,human_decision_required\n"
                            "1,ontology_task_ksa_relation_human_review_required,,,,,False,False,False,True\n"
                        ),
                        encoding="utf-8",
                    )
                return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

            with tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp)
                (workspace / ".agents").mkdir()
                (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
                queue_path = workspace / "queue.json"
                queue_path.write_text(
                    json.dumps(
                        {
                            "schema": "aihr_agent_work_queue_v1",
                            "items": [
                                queue_item(
                                    command=(
                                        "python scripts\\ncs_harness.py export-review-seedpack "
                                        "--issue-types ontology_task_ksa_relation_human_review_required "
                                        "--out reports\\seedpack.jsonl --csv-out reports\\seedpack.csv"
                                    ),
                                    expected_artifacts=[
                                        "reports/seedpack.jsonl",
                                        "reports/seedpack.csv",
                                    ],
                                    acceptance_checks=[
                                        "Seedpack is JSONL and remains review-pending.",
                                    ],
                                ),
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                return run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        cases = [
            {"omit_metadata": True},
            {"omit_row": True},
            {"omit_csv_column": True},
        ]
        for case in cases:
            with self.subTest(case=case):
                report = run_case(**case)
                run = report["runs"][0]
                self.assertFalse(report["ok"])
                self.assertFalse(run["acceptance_verified"])
                self.assertEqual(
                    run["acceptance_verification_status"],
                    "execution_or_artifact_checks_failed",
                )
                self.assertGreaterEqual(report["summary"]["acceptance_failed_count"], 1)

    def test_machine_declared_acceptance_coverage_rejects_mixed_manual_clauses(self) -> None:
        machine_contract_ids = {"export-review-seedpack_review_seedpack_contract_v1"}

        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                "Seedpack is JSONL and remains review-pending. "
                "Record commands run and touched files in the handoff.",
                machine_contract_ids,
            )
        )
        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                "Seedpack is JSONL and remains review-pending after a human signs off in the handoff.",
                machine_contract_ids,
            )
        )
        self.assertTrue(
            _machine_covers_declared_acceptance_check(
                "Seedpack is JSONL and remains review-pending.",
                machine_contract_ids,
            )
        )

    def test_machine_declared_acceptance_coverage_requires_precise_review_triage_clauses(self) -> None:
        machine_contract_ids = {"review_triage_readonly_contract_v1"}

        self.assertTrue(
            _machine_covers_declared_acceptance_check(
                (
                    "Confirm prerequisite review-priority, quality-gates, and "
                    "transition seedpack artifacts exist before running."
                ),
                machine_contract_ids,
            )
        )
        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                "Confirm prerequisite review-priority and quality-gates artifacts exist.",
                machine_contract_ids,
            )
        )
        self.assertTrue(
            _machine_covers_declared_acceptance_check(
                (
                    "Review-triage report reads existing artifacts only and does not "
                    "mutate review statuses."
                ),
                machine_contract_ids,
            )
        )
        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                (
                    "Review-triage reads existing artifacts only and status fields are "
                    "included for operator context."
                ),
                machine_contract_ids,
            )
        )
        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                (
                    "Review-triage report reads existing artifacts only and does not "
                    "mutate review statuses after a human reviews the output."
                ),
                machine_contract_ids,
            )
        )
        self.assertFalse(
            _machine_covers_declared_acceptance_check(
                (
                    "Confirm prerequisite review-priority, quality-gates, and "
                    "transition seedpack artifacts exist before running after human review."
                ),
                machine_contract_ids,
            )
        )

    def test_run_agent_queue_ready_machine_verifies_provenance_reconfirmation_contract(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            packet_path = workspace / arg_value(args, "--out")
            sheet_path = workspace / arg_value(args, "--decision-sheet-out")
            sheet_csv_path = workspace / arg_value(args, "--decision-sheet-csv-out")
            audit_path = workspace / arg_value(args, "--decision-audit-out")
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "row_count": 1,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "review_policy": {
                    "does_not_change_existing_statuses": True,
                    "allowed_decisions": [
                        "reconfirm",
                        "downgrade_to_review_required",
                        "defer",
                    ],
                    "reconfirm_is_evidence_review_only": True,
                    "reconfirm_does_not_apply_or_preserve_status": True,
                },
                "rows": [
                    {
                        "requested_decision": "reconfirm | downgrade_to_review_required | defer",
                        "decision_semantics": "review_input_only_not_status_update",
                        "db_writes": False,
                        "approval_claim": False,
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            packet_sha = "sha256:" + hashlib.sha256(packet_path.read_bytes()).hexdigest()
            sheet = {
                "ok": True,
                "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                "source_packet_sha256": packet_sha,
                "row_count": 1,
                "blank_decision_count": 1,
                "completed_decision_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
            }
            audit = {
                "ok": True,
                "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
                "source_packet_sha256": packet_sha,
                "row_count": 1,
                "source_packet_row_count": 1,
                "pending_decision_count": 1,
                "completed_decision_count": 0,
                "action_eligible_count": 0,
                "guarded_apply_ready": False,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            sheet_path.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
            audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
            sheet_csv_path.write_text(
                (
                    "order,decision,rationale,reviewer_id,reviewed_at,source_decision_packet,"
                    "evidence_refs_json,status_update_allowed,db_writes,approval_claim\n"
                    "1,,,,,,,false,false,false\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "export-human-review-provenance-reconfirmation-proofset "
                                    "--out reports\\packet.json "
                                    "--decision-sheet-out reports\\sheet.json "
                                    "--decision-sheet-csv-out reports\\sheet.csv "
                                    "--decision-audit-out reports\\audit.json"
                                ),
                                expected_artifacts=[
                                    "reports/packet.json",
                                    "reports/sheet.json",
                                    "reports/sheet.csv",
                                    "reports/audit.json",
                                ],
                                acceptance_checks=[
                                    "Proofset remains report-only and does not update statuses.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertTrue(report["ok"])
        self.assertTrue(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "machine_contract_verified_non_decisional",
        )
        self.assertEqual(report["summary"]["acceptance_unverified_count"], 0)
        self.assertEqual(report["summary"]["acceptance_machine_verified_count"], 1)

    def test_run_agent_queue_ready_machine_contract_fails_on_provenance_policy_drift(self) -> None:
        def arg_value(args, flag):
            return args[args.index(flag) + 1]

        def fake_runner(args, **kwargs):
            workspace = Path(kwargs["cwd"])
            packet_path = workspace / arg_value(args, "--out")
            sheet_path = workspace / arg_value(args, "--decision-sheet-out")
            sheet_csv_path = workspace / arg_value(args, "--decision-sheet-csv-out")
            audit_path = workspace / arg_value(args, "--decision-audit-out")
            packet_path.parent.mkdir(parents=True, exist_ok=True)
            packet = {
                "ok": True,
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "row_count": 1,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "review_policy": {
                    "does_not_change_existing_statuses": True,
                    "allowed_decisions": [
                        "reconfirm",
                        "downgrade_to_review_required",
                        "defer",
                    ],
                    "reconfirm_is_evidence_review_only": True,
                    "reconfirm_does_not_apply_or_preserve_status": False,
                },
                "rows": [
                    {
                        "requested_decision": "reconfirm | downgrade_to_review_required | defer",
                        "decision_semantics": "review_status_preservation",
                        "db_writes": False,
                        "approval_claim": False,
                    }
                ],
            }
            packet_path.write_text(json.dumps(packet, ensure_ascii=False), encoding="utf-8")
            packet_sha = "sha256:" + hashlib.sha256(packet_path.read_bytes()).hexdigest()
            sheet = {
                "ok": True,
                "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                "source_packet_sha256": packet_sha,
                "row_count": 1,
                "blank_decision_count": 1,
                "completed_decision_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
            }
            audit = {
                "ok": True,
                "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
                "source_packet_sha256": packet_sha,
                "row_count": 1,
                "source_packet_row_count": 1,
                "pending_decision_count": 1,
                "completed_decision_count": 0,
                "action_eligible_count": 0,
                "guarded_apply_ready": False,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
            sheet_path.write_text(json.dumps(sheet, ensure_ascii=False), encoding="utf-8")
            audit_path.write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
            sheet_csv_path.write_text(
                (
                    "order,decision,rationale,reviewer_id,reviewed_at,source_decision_packet,"
                    "evidence_refs_json,status_update_allowed,db_writes,approval_claim\n"
                    "1,,,,,,,false,false,false\n"
                ),
                encoding="utf-8",
            )
            return SimpleNamespace(returncode=0, stdout='{"ok": true}', stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py "
                                    "export-human-review-provenance-reconfirmation-proofset "
                                    "--out reports\\packet.json "
                                    "--decision-sheet-out reports\\sheet.json "
                                    "--decision-sheet-csv-out reports\\sheet.csv "
                                    "--decision-audit-out reports\\audit.json"
                                ),
                                expected_artifacts=[
                                    "reports/packet.json",
                                    "reports/sheet.json",
                                    "reports/sheet.csv",
                                    "reports/audit.json",
                                ],
                                acceptance_checks=[
                                    "Proofset remains report-only and does not update statuses.",
                                ],
                            ),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertFalse(report["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "execution_or_artifact_checks_failed",
        )
        self.assertGreaterEqual(report["summary"]["acceptance_failed_count"], 1)

    def test_run_agent_queue_ready_fails_when_expected_artifact_is_missing(self) -> None:
        def fake_runner(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout="ok", stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            markdown_path = workspace / "run.md"

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        run = report["runs"][0]
        results = {item["check"]: item for item in run["acceptance_check_results"]}
        self.assertFalse(report["ok"])
        self.assertEqual(report["summary"]["succeeded_count"], 1)
        self.assertEqual(report["summary"]["acceptance_failed_count"], 2)
        self.assertFalse(run["expected_artifact_checks"][0]["exists"])
        self.assertFalse(results["expected_artifacts_exist"]["ok"])
        self.assertFalse(results["expected_artifacts_non_empty"]["ok"])
        self.assertFalse(run["acceptance_verified"])
        self.assertEqual(
            run["acceptance_verification_status"],
            "execution_or_artifact_checks_failed",
        )
        self.assertIn(
            "acceptance: 2 check(s) need attention "
            "(expected_artifacts_exist, expected_artifacts_non_empty)",
            markdown,
        )

    def test_run_agent_queue_ready_truncates_captured_output_with_metadata(self) -> None:
        long_stdout = "start-" + ("x" * (AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT + 30)) + "-end"

        def fake_runner(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=long_stdout, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)

        run = report["runs"][0]
        self.assertTrue(run["stdout_truncated"])
        self.assertEqual(run["stdout_original_chars"], len(long_stdout))
        self.assertEqual(run["stdout_tail_chars"], AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT)
        self.assertEqual(run["stdout_tail"], long_stdout[-AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT:])
        self.assertFalse(run["stderr_truncated"])
        self.assertEqual(run["stderr_original_chars"], 0)

    def test_run_agent_queue_ready_redacts_sensitive_captured_output(self) -> None:
        stdout = (
            "NCS_SERVICE_KEY=abc123 "
            "url=https://example.test/path?serviceKey=url-secret "
            "source_payload={'debug': true} "
            '{"authKey":"json-secret","raw_payload":{"token":"nested-secret"}}'
        )
        stderr = "Authorization: Bearer token-secret"

        def fake_runner(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=stdout, stderr=stderr)

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            markdown_path = workspace / "run.md"
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        run = report["runs"][0]
        self.assertTrue(run["stdout_redacted"])
        self.assertTrue(run["stderr_redacted"])
        self.assertGreaterEqual(run["stdout_redaction_count"], 3)
        self.assertGreaterEqual(run["stderr_redaction_count"], 1)
        self.assertEqual(run["stdout_original_chars"], len(stdout))
        self.assertEqual(run["stdout_tail_chars"], len(run["stdout_tail"]))
        self.assertEqual(run["stderr_original_chars"], len(stderr))
        self.assertEqual(run["stderr_tail_chars"], len(run["stderr_tail"]))
        redacted_text = run["stdout_tail"] + run["stderr_tail"]
        for marker in (
            "NCS_SERVICE_KEY",
            "abc123",
            "serviceKey",
            "url-secret",
            "source_payload",
            "authKey",
            "json-secret",
            "raw_payload",
            "nested-secret",
            "Authorization",
            "Bearer",
            "token-secret",
        ):
            self.assertNotIn(marker, redacted_text)
            self.assertNotIn(marker, markdown)
        self.assertIn("redacted=True", markdown)

    def test_run_agent_queue_ready_redacts_human_decision_vocab_from_captured_output(self) -> None:
        stdout = (
            '{"allowed_decisions":["approve","reject","defer"],'
            '"requested_decision":"reconfirm | downgrade_to_review_required | defer",'
            '"status":"trusted/reviewed","review_status":"human_reviewed"}'
        )

        def fake_runner(args, **kwargs):
            output = Path(kwargs["cwd"]) / "reports" / "aihr_review_priority_20260617.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text('{"ok": true}', encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [queue_item()],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace, runner=fake_runner)
            markdown_path = workspace / "run.md"
            write_agent_queue_run_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        run = report["runs"][0]
        self.assertTrue(run["stdout_redacted"])
        self.assertTrue(run["stdout_human_decision_vocab_redacted"])
        self.assertGreaterEqual(run["stdout_human_decision_vocab_redaction_count"], 7)
        self.assertGreaterEqual(run["stdout_redaction_count"], 1)
        for marker in (
            "allowed_decisions",
            "approve",
            "reject",
            "defer",
            "requested_decision",
            "reconfirm",
            "downgrade_to_review_required",
            "trusted/reviewed",
            "human_reviewed",
        ):
            self.assertNotIn(marker, run["stdout_tail"])
            self.assertNotIn(marker, markdown)
        self.assertIn("human_decision_vocab_redacted=True", markdown)

    def test_redact_sensitive_structured_output_preserves_unrelated_context(self) -> None:
        redacted, count = _redact_sensitive_output(
            'prefix source_payload={"debug":true} suffix keepme '
            '{"source_payload":{"debug":true},"ok":1,"status":"done"}'
        )

        self.assertGreaterEqual(count, 2)
        for marker in ("source_payload", '{"debug":true}'):
            self.assertNotIn(marker, redacted)
        for context in ("prefix", "suffix", "keepme", '"ok":1', '"status":"done"'):
            self.assertIn(context, redacted)

    def test_run_agent_queue_ready_skips_shell_metacharacter_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".agents").mkdir()
            (workspace / ".agents" / "ontology-review-agent.md").write_text("role", encoding="utf-8")
            queue_path = workspace / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [
                            queue_item(
                                command=(
                                    "python scripts\\ncs_harness.py review-priority "
                                    "--out reports\\priority.json & del data\\processed\\ncs.db"
                                )
                            )
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = run_agent_queue_ready_from_file(queue_path, workspace=workspace)

        self.assertFalse(report["ok"])
        self.assertEqual(report["runs"][0]["status"], "skipped_unsafe")
        self.assertIn("command_contains_shell_metacharacters", report["runs"][0]["validation_errors"])

    def test_invalid_schema_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported agent queue schema"):
            build_agent_queue_status({"schema": "wrong", "items": []})


if __name__ == "__main__":
    unittest.main()
