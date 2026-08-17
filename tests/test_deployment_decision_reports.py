from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import build_deployment_decision as decision
from scripts import scan_source_preview_artifacts as scan
from scripts import summarize_preview_release_evidence as preview_summary


class DeploymentDecisionReportTests(unittest.TestCase):
    def test_source_preview_scan_allows_templates_but_blocks_real_env_file(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.example").write_text("NCS_API_KEY=\n", encoding="utf-8")
            (root / ".env").write_text("NCS_API_KEY=real-value\n", encoding="utf-8")
            (root / "tests").mkdir()
            (root / "tests" / "test_smoke.py").write_text(
                'secret = "placeholder"\n',
                encoding="utf-8",
            )

            report = scan.scan_tree(root)

        self.assertFalse(report["ok"])
        self.assertEqual(report["allowed_template_file_count"], 1)
        self.assertEqual(report["allowed_secret_example_count"], 1)
        self.assertEqual(report["blocked_name_finding_count"], 1)
        self.assertGreaterEqual(report["high_confidence_secret_finding_count"], 1)

    def test_source_preview_scan_passes_clean_source_tree(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

            report = scan.scan_tree(root)

        self.assertTrue(report["ok"])
        self.assertEqual(report["blocked_name_finding_count"], 0)
        self.assertEqual(report["high_confidence_secret_finding_count"], 0)

    def test_deployment_decision_allows_private_preview_but_not_dirty_branch_push(self) -> None:
        with TemporaryDirectory() as tmp:
            preview_dir = Path(tmp) / "tmp" / "preview"
            preview_dir.mkdir(parents=True)
            output_dir = str(preview_dir)

            report = decision.build_decision(
                preview_summary={
                    "ok": True,
                    "preview_allowed_by_product_evidence": True,
                    "stable_release_ready": False,
                    "source_boundary_ok": False,
                    "source_package_ok": True,
                    "supporting_evidence_freshness_ok": True,
                    "source_preview_export": {
                        "ok": True,
                        "generated_at": "2026-07-02T01:00:00+00:00",
                        "output_dir": output_dir,
                        "copied_file_count": 10,
                        "included_untracked_path_count": 2,
                    },
                    "release": {
                        "engineering_hygiene_ok": True,
                        "blocked_by": ["review_debt:human_reviewed_concepts"],
                    },
                    "dashboard": {
                        "ok": True,
                        "static_artifacts_ok": True,
                        "review_chain_safety": {
                            "do_not_set_human_reviewed_accepted_reviewed_automatically": True
                        },
                    },
                    "queue": {"run_failed_count": 0},
                    "remaining_blockers": {
                        "qualification_coverage_plan_snapshot": {
                            "automatic_collection_allowed_now": False,
                            "operator_timing_required": True,
                        }
                    },
                },
                tree_verification={
                    "ok": True,
                    "generated_at": "2026-07-02T01:01:00+00:00",
                    "output_dir": output_dir,
                    "file_count": 10,
                    "expected_file_count": 10,
                    "hash_mismatch_count": 0,
                    "missing_required_count": 0,
                    "summary": {"blocked_path_count": 0},
                },
                runtime_smoke={
                    "ok": True,
                    "generated_at": "2026-07-02T01:02:00+00:00",
                    "output_dir": output_dir,
                    "commands": [{"returncode": 0}],
                },
                secret_scan={
                    "ok": True,
                    "generated_at": "2026-07-02T01:03:00+00:00",
                    "output_dir": output_dir,
                    "blocked_name_finding_count": 0,
                    "high_confidence_secret_finding_count": 0,
                    "large_file_count": 0,
                },
            )

        self.assertTrue(report["private_preview_deployable_now"])
        self.assertTrue(report["private_preview_contract_satisfied"])
        self.assertTrue(report["private_preview_is_not_human_signoff"])
        self.assertEqual(
            report["recommended_timing"],
            "after source-preview export review; stable release after human review and "
            "qualification coverage blockers close",
        )
        self.assertTrue(report["human_signoff_required"])
        self.assertFalse(report["deployment_execution_authorized"])
        self.assertFalse(report["stable_release_ready"])
        self.assertFalse(report["github_push_current_branch"])
        self.assertEqual(report["recommended_publication_level"], "private/draft developer preview")
        self.assertTrue(report["source_preview"]["same_tree_ok"])
        self.assertTrue(report["source_preview"]["freshness_ok"])
        self.assertTrue(report["source_preview"]["output_dir_is_dir"])
        self.assertTrue(report["source_preview"]["tree_hash_consistency_ok"])
        self.assertTrue(report["source_preview"]["supporting_evidence_freshness_ok"])
        self.assertTrue(report["product_evidence"]["preview_evidence_complete"])
        self.assertTrue(report["product_evidence"]["preview_is_not_approval"])
        self.assertEqual(
            report["cli_exit_semantics"]["default_exit_code"],
            decision.EXIT_PRIVATE_PREVIEW_ONLY,
        )
        self.assertEqual(
            decision.deployment_decision_exit_code(report),
            decision.EXIT_PRIVATE_PREVIEW_ONLY,
        )
        self.assertIn(
            "LFS history was not evaluated in this run",
            report["required_next_steps"][1],
        )

    def test_deployment_decision_blocks_deleted_preview_tree(self) -> None:
        preview = {
            "ok": True,
            "preview_allowed_by_product_evidence": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "supporting_evidence_freshness_ok": True,
            "source_preview_export": {
                "ok": True,
                "generated_at": "2026-07-02T01:00:00+00:00",
                "output_dir": "tmp/preview",
                "copied_file_count": 10,
                "included_untracked_path_count": 2,
            },
            "release": {
                "engineering_hygiene_ok": True,
                "blocked_by": ["review_debt:human_reviewed_concepts"],
            },
            "dashboard": {
                "ok": True,
                "static_artifacts_ok": True,
                "review_chain_safety": {
                    "do_not_set_human_reviewed_accepted_reviewed_automatically": True
                },
            },
            "queue": {"run_failed_count": 0},
            "remaining_blockers": {
                "qualification_coverage_plan_snapshot": {
                    "automatic_collection_allowed_now": False,
                    "operator_timing_required": True,
                }
            },
        }

        report = decision.build_decision(
            preview_summary=preview,
            tree_verification={
                "ok": True,
                "generated_at": "2026-07-02T01:01:00+00:00",
                "output_dir": "tmp/preview",
                "summary": {"blocked_path_count": 0},
            },
            runtime_smoke={
                "ok": True,
                "generated_at": "2026-07-02T01:02:00+00:00",
                "output_dir": "tmp/preview",
                "commands": [{"returncode": 0}],
            },
            secret_scan={
                "ok": True,
                "generated_at": "2026-07-02T01:03:00+00:00",
                "output_dir": "tmp/preview",
                "blocked_name_finding_count": 0,
                "high_confidence_secret_finding_count": 0,
                "large_file_count": 0,
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["private_preview_contract_satisfied"])
        self.assertFalse(report["deployment_execution_authorized"])
        self.assertEqual(report["recommended_publication_level"], "do not deploy")
        self.assertTrue(report["source_preview"]["same_tree_ok"])
        self.assertTrue(report["source_preview"]["freshness_ok"])
        self.assertTrue(report["source_preview"]["supporting_evidence_freshness_ok"])
        self.assertFalse(report["source_preview"]["output_dir_is_dir"])
        self.assertEqual(report["source_preview"]["output_dir_issue"], "output_dir_not_found")
        self.assertEqual(
            decision.deployment_decision_exit_code(report),
            decision.EXIT_NOT_DEPLOYABLE,
        )

    def test_deployment_decision_blocks_stale_supporting_evidence(self) -> None:
        preview = {
            "ok": True,
            "preview_allowed_by_product_evidence": True,
            "preview_evidence_complete": False,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "supporting_evidence_freshness_ok": False,
            "source_preview_export": {
                "ok": True,
                "generated_at": "2026-07-02T01:00:00+00:00",
                "output_dir": "tmp/preview",
            },
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(
            preview_summary=preview,
            tree_verification={
                "ok": True,
                "generated_at": "2026-07-02T01:01:00+00:00",
                "output_dir": "tmp/preview",
            },
            runtime_smoke={
                "ok": True,
                "generated_at": "2026-07-02T01:02:00+00:00",
                "output_dir": "tmp/preview",
            },
            secret_scan={
                "ok": True,
                "generated_at": "2026-07-02T01:03:00+00:00",
                "output_dir": "tmp/preview",
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["source_preview"]["supporting_evidence_freshness_ok"])

    def test_deployment_decision_blocks_preview_when_secret_scan_fails(self) -> None:
        preview = {
            "ok": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "source_preview_export": {"ok": True},
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(
            preview_summary=preview,
            secret_scan={
                "ok": False,
                "blocked_name_finding_count": 0,
                "high_confidence_secret_finding_count": 1,
                "large_file_count": 0,
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertEqual(report["recommended_publication_level"], "do not deploy")

    def test_deployment_decision_requires_all_preview_proof_artifacts(self) -> None:
        preview = {
            "ok": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "source_preview_export": {
                "ok": True,
                "generated_at": "2026-07-02T01:00:00+00:00",
                "output_dir": "tmp/preview",
            },
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(preview_summary=preview)

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["source_preview"]["required_artifacts_present"])
        self.assertEqual(
            report["source_preview"]["missing_artifacts"],
            ["tree_verification", "runtime_smoke", "secret_scan"],
        )

    def test_deployment_decision_requires_source_export_metadata(self) -> None:
        preview = {
            "ok": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "source_preview_export": {"ok": True},
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(
            preview_summary=preview,
            tree_verification={
                "ok": True,
                "generated_at": "2026-07-02T01:01:00+00:00",
                "output_dir": "tmp/preview",
            },
            runtime_smoke={
                "ok": True,
                "generated_at": "2026-07-02T01:02:00+00:00",
                "output_dir": "tmp/preview",
            },
            secret_scan={
                "ok": True,
                "generated_at": "2026-07-02T01:03:00+00:00",
                "output_dir": "tmp/preview",
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["source_preview"]["source_metadata_ok"])
        self.assertEqual(
            report["source_preview"]["missing_source_fields"],
            ["output_dir", "generated_at"],
        )

    def test_deployment_decision_blocks_mismatched_preview_tree(self) -> None:
        preview = {
            "ok": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "source_preview_export": {
                "ok": True,
                "generated_at": "2026-07-02T01:00:00+00:00",
                "output_dir": "tmp/preview-new",
            },
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(
            preview_summary=preview,
            tree_verification={
                "ok": True,
                "generated_at": "2026-07-02T01:01:00+00:00",
                "output_dir": "tmp/preview-old",
            },
            runtime_smoke={
                "ok": True,
                "generated_at": "2026-07-02T01:02:00+00:00",
                "output_dir": "tmp/preview-new",
                "commands": [],
            },
            secret_scan={
                "ok": True,
                "generated_at": "2026-07-02T01:03:00+00:00",
                "output_dir": "tmp/preview-new",
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["source_preview"]["same_tree_ok"])
        self.assertEqual(
            report["source_preview"]["output_dir_mismatches"][0]["artifact"],
            "tree_verification",
        )

    def test_deployment_decision_blocks_stale_preview_artifact(self) -> None:
        preview = {
            "ok": True,
            "stable_release_ready": False,
            "source_boundary_ok": False,
            "source_package_ok": True,
            "source_preview_export": {
                "ok": True,
                "generated_at": "2026-07-02T01:00:00+00:00",
                "output_dir": "tmp/preview",
            },
            "release": {"engineering_hygiene_ok": True, "blocked_by": []},
            "dashboard": {"ok": True, "static_artifacts_ok": True},
        }

        report = decision.build_decision(
            preview_summary=preview,
            tree_verification={
                "ok": True,
                "generated_at": "2026-07-02T01:01:00+00:00",
                "output_dir": "tmp/preview",
            },
            runtime_smoke={
                "ok": True,
                "generated_at": "2026-07-02T00:59:59+00:00",
                "output_dir": "tmp/preview",
                "commands": [],
            },
            secret_scan={
                "ok": True,
                "generated_at": "2026-07-02T01:03:00+00:00",
                "output_dir": "tmp/preview",
            },
        )

        self.assertFalse(report["private_preview_deployable_now"])
        self.assertFalse(report["source_preview"]["freshness_ok"])
        self.assertEqual(
            report["source_preview"]["freshness_failures"][0]["artifact"],
            "runtime_smoke",
        )

    def test_deployment_decision_includes_source_boundary_and_export_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            preview_dir = Path(tmp) / "tmp" / "preview"
            preview_dir.mkdir(parents=True)
            output_dir = str(preview_dir)
            preview = {
                "ok": True,
                "preview_allowed_by_product_evidence": True,
                "stable_release_ready": False,
                "source_boundary_ok": False,
                "source_package_ok": True,
                "supporting_evidence_freshness_ok": True,
                "source_boundary": {
                    "path": "reports/deployment_source_boundary_20260702_9h.json",
                    "ok": False,
                    "tracked_blocker_count": 334,
                    "lfs_history_evaluated": False,
                    "lfs_history_blocker_count": 0,
                },
                "source_preview_export": {
                    "path": "reports/deployment_source_preview_export_reviewed_20260702_9h.json",
                    "ok": True,
                    "generated_at": "2026-07-02T01:00:00+00:00",
                    "output_dir": output_dir,
                },
                "release": {
                    "engineering_hygiene_ok": True,
                    "blocked_by": ["review_debt:human_reviewed_concepts"],
                },
                "dashboard": {
                    "ok": True,
                    "static_artifacts_ok": True,
                    "review_chain_safety": {
                        "do_not_set_human_reviewed_accepted_reviewed_automatically": True
                    },
                },
                "remaining_blockers": {
                    "qualification_coverage_plan_snapshot": {
                        "automatic_collection_allowed_now": False,
                        "operator_timing_required": True,
                    }
                },
            }

            report = decision.build_decision(
                preview_summary=preview,
                tree_verification={
                    "ok": True,
                    "generated_at": "2026-07-02T01:01:00+00:00",
                    "output_dir": output_dir,
                    "file_count": 10,
                    "expected_file_count": 10,
                    "hash_mismatch_count": 0,
                },
                runtime_smoke={
                    "ok": True,
                    "generated_at": "2026-07-02T01:02:00+00:00",
                    "output_dir": output_dir,
                },
                secret_scan={
                    "ok": True,
                    "generated_at": "2026-07-02T01:03:00+00:00",
                    "output_dir": output_dir,
                },
                preview_summary_path="reports/preview_release_evidence_summary_20260702_9h.json",
                tree_verification_path="reports/deployment_source_preview_tree_verification_20260702_9h.json",
                runtime_smoke_path="reports/deployment_source_preview_runtime_smoke_20260702_9h.json",
                secret_scan_path="reports/source_preview_secret_artifact_scan_20260702_9h.json",
            )

        self.assertIn(
            "reports/deployment_source_boundary_20260702_9h.json",
            report["evidence_files"],
        )
        self.assertIn(
            "reports/deployment_source_preview_export_reviewed_20260702_9h.json",
            report["evidence_files"],
        )
        self.assertEqual(
            report["source_preview"]["source_boundary_tracked_blocker_count"],
            334,
        )
        self.assertIn("LFS history was not evaluated", report["required_next_steps"][1])

    def test_preview_summary_preserves_review_backlog_counts(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            source_boundary_path = root / "boundary.json"
            source_export_path = root / "export.json"

            release_path.write_text(
                '{"release_decision":{"release_ready":false,"blocked_by":[]},"engineering_hygiene_ok":true}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                """
                {
                  "ok": true,
                  "checks": [{"name":"static_artifacts","ok":true,"artifact_count":1}],
                  "review_chain_safety_summary": {
                    "do_not_set_human_reviewed_accepted_reviewed_automatically": true,
                    "legacy_status_needs_reconfirmation_count": 34,
                    "rows_without_packet_backed_provenance": 34,
                    "provenance_date_matches_plan_review_family": true
                  },
                  "static_artifacts": [
                    {"blank_decision_count": 34, "pending_decision_count": 34}
                  ]
                }
                """,
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            source_boundary_path.write_text('{"ok":false}', encoding="utf-8")
            source_export_path.write_text(
                '{"ok":true,"generated_at":"2026-07-02T01:00:00+00:00","output_dir":"tmp/preview","summary":{"copied_file_count":10}}',
                encoding="utf-8",
            )

            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=source_boundary_path,
                source_preview_export_path=source_export_path,
            )

        safety = report["dashboard"]["review_chain_safety"]
        self.assertEqual(safety["legacy_status_needs_reconfirmation_count"], 34)
        self.assertEqual(safety["pending_decision_count"], 34)
        self.assertEqual(safety["blank_decision_count"], 34)
        self.assertEqual(
            report["source_preview_export"]["generated_at"],
            "2026-07-02T01:00:00+00:00",
        )
        self.assertEqual(report["source_boundary"]["path"], str(source_boundary_path))
        self.assertFalse(report["source_boundary"]["ok"])
        self.assertIn(
            "source_boundary",
            report["supporting_evidence_freshness"]["artifact_mtimes"],
        )
        self.assertIn(
            "source_preview_export",
            report["supporting_evidence_freshness"]["artifact_mtimes"],
        )

    def test_preview_summary_preserves_zero_review_counts_without_global_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            source_boundary_path = root / "boundary.json"
            source_export_path = root / "export.json"

            release_path.write_text(
                '{"release_decision":{"release_ready":false,"blocked_by":[]},"engineering_hygiene_ok":true}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                """
                {
                  "ok": true,
                  "checks": [{"name":"static_artifacts","ok":true,"artifact_count":1}],
                  "review_chain_safety_summary": {
                    "do_not_set_human_reviewed_accepted_reviewed_automatically": true,
                    "legacy_status_needs_reconfirmation_count": 0,
                    "pending_decision_count": 0,
                    "blank_decision_count": 0,
                    "rows_without_packet_backed_provenance": 0
                  },
                  "static_artifacts": [
                    {"pending_decision_count": 80, "blank_decision_count": 80}
                  ]
                }
                """,
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            source_boundary_path.write_text('{"ok":false}', encoding="utf-8")
            source_export_path.write_text(
                '{"ok":true,"generated_at":"2026-07-02T01:00:00+00:00","output_dir":"tmp/preview","summary":{"copied_file_count":10}}',
                encoding="utf-8",
            )

            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=source_boundary_path,
                source_preview_export_path=source_export_path,
            )

        safety = report["dashboard"]["review_chain_safety"]
        self.assertEqual(safety["legacy_status_needs_reconfirmation_count"], 0)
        self.assertEqual(safety["pending_decision_count"], 0)
        self.assertEqual(safety["blank_decision_count"], 0)

    def test_preview_summary_blocks_supporting_proof_older_than_source_export(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            source_boundary_path = root / "boundary.json"
            source_export_path = root / "export.json"

            release_path.write_text(
                '{"release_decision":{"release_ready":false,"blocked_by":[]},"engineering_hygiene_ok":true}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                '{"ok":true,"checks":[{"name":"static_artifacts","ok":true}],'
                '"review_chain_safety_summary":'
                '{"do_not_set_human_reviewed_accepted_reviewed_automatically":true}}',
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            source_boundary_path.write_text('{"ok":false}', encoding="utf-8")
            preview_dir = root / "tmp" / "preview"
            preview_dir.mkdir(parents=True)
            source_export_path.write_text(
                '{"ok":true,"generated_at":"2999-07-02T01:00:00+00:00",'
                f'"output_dir":"{preview_dir.as_posix()}",'
                '"summary":{"copied_file_count":10}}',
                encoding="utf-8",
            )

            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=source_boundary_path,
                source_preview_export_path=source_export_path,
            )

        self.assertFalse(report["ok"])
        self.assertFalse(report["contract_ok"])
        self.assertFalse(report["supporting_evidence_freshness_ok"])
        self.assertFalse(report["execution_authorized"])
        self.assertTrue(report["human_signoff_required"])
        self.assertTrue(report["preview_is_not_approval"])
        self.assertFalse(report["preview_evidence_complete"])
        self.assertTrue(report["source_preview_export_ok"])
        self.assertTrue(report["source_package_ok"])
        self.assertEqual(
            report["preview_blockers"],
            ["supporting_evidence_older_than_source_preview_export"],
        )
        self.assertEqual(
            report["supporting_evidence_freshness"]["stale_artifacts"][0]["artifact"],
            "release_readiness",
        )

    def test_preview_summary_rejects_source_export_without_required_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            release_path = root / "release.json"
            dashboard_path = root / "dashboard.json"
            blockers_path = root / "blockers.json"
            queue_status_path = root / "queue_status.json"
            queue_run_path = root / "queue_run.json"
            source_boundary_path = root / "boundary.json"
            source_export_path = root / "export.json"

            release_path.write_text(
                '{"release_decision":{"release_ready":false,"blocked_by":[]},"engineering_hygiene_ok":true}',
                encoding="utf-8",
            )
            dashboard_path.write_text(
                '{"ok":true,"checks":[{"name":"static_artifacts","ok":true}],'
                '"review_chain_safety_summary":'
                '{"do_not_set_human_reviewed_accepted_reviewed_automatically":true}}',
                encoding="utf-8",
            )
            blockers_path.write_text(
                '{"remaining_blockers":[],"qualification_coverage_plan_snapshot":{"automatic_collection_allowed_now":false}}',
                encoding="utf-8",
            )
            queue_status_path.write_text('{"summary":{}}', encoding="utf-8")
            queue_run_path.write_text('{"summary":{}}', encoding="utf-8")
            source_boundary_path.write_text('{"ok":false}', encoding="utf-8")
            source_export_path.write_text(
                '{"ok":true,"summary":{"copied_file_count":10}}',
                encoding="utf-8",
            )

            report = preview_summary.build_summary(
                release_path=release_path,
                dashboard_path=dashboard_path,
                blockers_path=blockers_path,
                queue_status_path=queue_status_path,
                queue_run_path=queue_run_path,
                source_boundary_path=source_boundary_path,
                source_preview_export_path=source_export_path,
            )

        self.assertFalse(report["ok"])
        self.assertFalse(report["source_preview_export_ok"])
        self.assertFalse(report["source_package_ok"])
        self.assertEqual(
            report["source_preview_export"]["missing_required_fields"],
            ["output_dir", "generated_at"],
        )


if __name__ == "__main__":
    unittest.main()
