from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import institutional_chatbot_readiness_report as readiness


class InstitutionalChatbotReadinessReportTests(unittest.TestCase):
    def test_integration_evidence_template_is_safe_and_incomplete(self) -> None:
        template_path = (
            ROOT
            / "docs"
            / "examples"
            / "institutional_chatbot_integration_evidence.example.json"
        )
        template = json.loads(template_path.read_text(encoding="utf-8"))

        readiness.validate_evidence({"institution_integration": template})
        self.assertFalse(template["status_update_allowed"])
        self.assertFalse(template["db_writes"])
        self.assertFalse(template["approval_claim"])
        self.assertEqual(
            {
                item["id"]
                for item in readiness.INTEGRATION_REQUIREMENTS
                if item["required"]
            }
            - set(template["controls"]),
            set(),
        )
        requirement_by_id = {
            item["id"]: item for item in readiness.INTEGRATION_REQUIREMENTS
        }
        self.assertNotIn("chat_ui", requirement_by_id)
        self.assertFalse(requirement_by_id["llm_gateway"]["required"])
        self.assertTrue(
            all(
                not control["implemented"]
                and not control["tested"]
                and not control["owner"]
                and not control["evidence_refs"]
                for control in template["controls"].values()
            )
        )

    def test_private_pilot_ready_with_current_backend_and_integration_evidence(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            report = self._synthesize(paths, now=now)

        self.assertTrue(report["core_backend_ready"])
        self.assertTrue(report["reference_chat_runtime_ready"])
        self.assertTrue(report["private_pilot_backend_ready"])
        self.assertTrue(report["private_pilot_ready"])
        self.assertFalse(report["stable_release_ready"])
        self.assertTrue(report["evidence_contract"]["contract_ok"])
        self.assertTrue(report["evidence_contract"]["lineage_ok"])
        self.assertEqual(
            report["outsourcing_assessment"]["status"],
            "selective_service_integration",
        )
        self.assertFalse(
            report["outsourcing_assessment"]["core_ncs_engine_replacement_needed"]
        )
        self.assertTrue(
            all(item["ready"] for item in report["institution_integration_requirements"])
        )
        self.assertTrue(report["repository_reference_chat"]["supplied_by_repository"])
        self.assertFalse(
            report["repository_reference_chat"][
                "institution_integration_completion_claimed"
            ]
        )
        self.assertEqual(
            report["reference_chat_smoke_evidence"]["passed_check_count"],
            len(readiness.REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS),
        )
        self.assertIn("release_readiness", report["evidence_paths"])
        self.assertRegex(
            report["evidence_fingerprints"]["release_readiness"]["sha256"],
            r"^sha256:[0-9a-f]{64}$",
        )
        self.assertIn(
            "stable_release_blocker:qualification:collection_coverage",
            {item["code"] for item in report["blockers"]},
        )

    def test_stable_release_requires_closed_release_blockers(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=True)
            report = self._synthesize(paths, now=now)

        self.assertTrue(report["core_backend_ready"])
        self.assertTrue(report["private_pilot_ready"])
        self.assertTrue(report["stable_release_ready"])
        self.assertEqual(report["readiness_basis"]["active_stable_blocker_count"], 0)
        self.assertEqual(report["blockers"], [])

    def test_optional_llm_integration_does_not_block_private_pilot(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            integration = self._read(paths["institution_integration"])
            integration["controls"].pop("llm_gateway")
            self._write(paths["institution_integration"], integration, now)

            report = self._synthesize(paths, now=now)

        llm_requirement = next(
            item
            for item in report["institution_integration_requirements"]
            if item["id"] == "llm_gateway"
        )
        self.assertFalse(llm_requirement["required"])
        self.assertEqual(llm_requirement["status"], "optional_not_configured")
        self.assertTrue(report["private_pilot_backend_ready"])
        self.assertTrue(report["private_pilot_ready"])

    def test_omitted_reference_chat_smoke_blocks_private_pilot_backend(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(
                Path(tmp),
                now=now,
                stable=False,
                include_smoke=False,
            )
            report = self._synthesize(paths, now=now)

        self.assertTrue(report["core_backend_ready"])
        self.assertFalse(report["reference_chat_runtime_ready"])
        self.assertFalse(report["private_pilot_backend_ready"])
        self.assertFalse(report["private_pilot_ready"])
        self.assertFalse(report["reference_chat_smoke_evidence"]["provided"])
        self.assertIn(
            "institutional_chat_smoke_not_provided",
            {item["code"] for item in report["blockers"]},
        )

    def test_stale_reference_chat_smoke_blocks_private_pilot_not_core(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(
                Path(tmp),
                now=now,
                stable=False,
                smoke_time=now - timedelta(days=10),
            )
            report = self._synthesize(paths, now=now)

        self.assertTrue(report["core_backend_ready"])
        self.assertFalse(report["reference_chat_runtime_ready"])
        self.assertFalse(report["private_pilot_backend_ready"])
        blocker_codes = {item["code"] for item in report["blockers"]}
        self.assertIn("stale_or_future_evidence:institutional_chat_smoke", blocker_codes)
        self.assertIn("institutional_chat_smoke_not_ready", blocker_codes)

    def test_each_required_smoke_check_gates_runtime_readiness(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            smoke = self._read(paths["institutional_chat_smoke"])

        for check_id in readiness.REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS:
            with self.subTest(check_id=check_id):
                failed = json.loads(json.dumps(smoke))
                failed["checks"][check_id] = False
                self.assertFalse(readiness._institutional_chat_smoke_ready(failed))

    def test_smoke_validation_rejects_missing_required_check(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            smoke = self._read(paths["institutional_chat_smoke"])
            del smoke["checks"]["gateway_secret_not_in_audit"]

        with self.assertRaisesRegex(
            readiness.EvidenceValidationError,
            "checks missing required ids: gateway_secret_not_in_audit",
        ):
            readiness.validate_evidence({"institutional_chat_smoke": smoke})

    def test_smoke_detail_mismatch_blocks_runtime_even_when_checks_claim_pass(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            smoke = self._read(paths["institutional_chat_smoke"])

        smoke["gateway"]["startup"]["operator_tools_enabled"] = True
        self.assertTrue(all(smoke["checks"].values()))
        self.assertFalse(readiness._institutional_chat_smoke_ready(smoke))

    def test_stale_benchmark_blocks_core_and_private_pilot(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(
                Path(tmp),
                now=now,
                stable=False,
                benchmark_time=now - timedelta(days=10),
            )
            report = self._synthesize(paths, now=now)

        self.assertFalse(report["core_backend_ready"])
        self.assertFalse(report["private_pilot_backend_ready"])
        self.assertFalse(report["private_pilot_ready"])
        self.assertFalse(report["evidence_contract"]["freshness_ok"])
        self.assertFalse(
            report["evidence_fingerprints"]["chatbot_benchmark"]["fresh"]
        )
        self.assertIn(
            "stale_or_future_evidence:chatbot_benchmark",
            {item["code"] for item in report["blockers"]},
        )

    def test_legacy_benchmark_without_sidecar_manifest_is_not_ready(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            benchmark = self._read(paths["chatbot_benchmark"])
            benchmark["database"]["before"].pop("manifest_schema")
            benchmark["database"]["before"].pop("sidecars")
            benchmark["database"]["after"].pop("manifest_schema")
            benchmark["database"]["after"].pop("sidecars")
            benchmark["database"]["immutability"].pop("base_unchanged")
            benchmark["database"]["immutability"].pop("sidecars_unchanged")
            benchmark["database"]["immutability"].pop("changed_sidecars")
            self._write(paths["chatbot_benchmark"], benchmark, now)

            report = self._synthesize(paths, now=now)

        self.assertFalse(report["core_backend_ready"])
        self.assertIn(
            "chatbot_benchmark_not_ready",
            {item["code"] for item in report["blockers"]},
        )

    def test_failed_benchmark_is_valid_negative_evidence(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            benchmark = self._read(paths["chatbot_benchmark"])
            benchmark["ok"] = False
            benchmark["readiness_status"] = "not_ready"
            benchmark["db_writes"] = None
            benchmark["human_status_changes_observed"] = None
            benchmark["database"]["immutability"]["all_unchanged"] = False
            benchmark["database"]["immutability"]["sidecars_unchanged"] = False
            benchmark["database"]["immutability"]["changed_sidecars"] = ["-shm"]
            benchmark["database"]["filesystem_mutation_observed"] = True
            benchmark["summary"]["invalid_measured_runs"] = 1
            benchmark["summary"]["valid_measured_runs"] -= 1
            benchmark["summary"]["result_validity_rate"] = 0.875
            benchmark["summary"]["latency_ms"].update(
                {"sample_count": 0, "p50": None, "p95": None, "max": None}
            )
            self._write(paths["chatbot_benchmark"], benchmark, now)

            report = self._synthesize(paths, now=now)

        self.assertFalse(report["core_backend_ready"])
        blocker_codes = {item["code"] for item in report["blockers"]}
        self.assertIn("unsafe_input_contract:chatbot_benchmark", blocker_codes)
        self.assertIn("chatbot_benchmark_not_ready", blocker_codes)

    def test_release_path_mismatch_blocks_private_pilot_without_hiding_core(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._write_evidence_set(root, now=now, stable=False)
            preview = self._read(paths["source_preview_summary"])
            preview["release"]["path"] = str(root / "different-release.json")
            self._write(paths["source_preview_summary"], preview, now)

            report = self._synthesize(paths, now=now)

        self.assertTrue(report["core_backend_ready"])
        self.assertFalse(report["private_pilot_backend_ready"])
        self.assertFalse(report["private_pilot_ready"])
        self.assertFalse(report["evidence_contract"]["lineage_ok"])
        self.assertIn(
            "preview_release_path_mismatch",
            {item["code"] for item in report["blockers"]},
        )

    def test_strict_validation_rejects_wrong_schema(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            benchmark = self._read(paths["chatbot_benchmark"])
            benchmark["schema"] = "unknown_chatbot_benchmark_v9"
            self._write(paths["chatbot_benchmark"], benchmark, now)

            with self.assertRaisesRegex(readiness.EvidenceValidationError, "unsupported schema"):
                self._synthesize(paths, now=now)

    def test_strict_validation_rejects_missing_required_key(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            preview = self._read(paths["source_preview_summary"])
            del preview["supporting_evidence_freshness_ok"]
            self._write(paths["source_preview_summary"], preview, now)

            with self.assertRaisesRegex(
                readiness.EvidenceValidationError,
                "missing required key: supporting_evidence_freshness_ok",
            ):
                self._synthesize(paths, now=now)

    def test_strict_validation_rejects_missing_input_file(self) -> None:
        now = datetime(2026, 7, 13, 1, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._write_evidence_set(Path(tmp), now=now, stable=False)
            paths["chatbot_benchmark"].unlink()

            with self.assertRaisesRegex(
                readiness.EvidenceValidationError,
                "chatbot_benchmark: input file does not exist",
            ):
                self._synthesize(paths, now=now)

    def test_cli_writes_json_and_markdown_without_claiming_approval(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._write_evidence_set(
                root,
                now=now,
                stable=False,
                include_integration=False,
            )
            json_out = root / "out" / "institution-readiness.json"
            markdown_out = root / "out" / "institution-readiness.md"
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = readiness.main(
                    [
                        "--release-readiness",
                        str(paths["release_readiness"]),
                        "--deployment-decision",
                        str(paths["deployment_decision"]),
                        "--chatbot-benchmark",
                        str(paths["chatbot_benchmark"]),
                        "--source-preview-summary",
                        str(paths["source_preview_summary"]),
                        "--institutional-chat-smoke",
                        str(paths["institutional_chat_smoke"]),
                        "--out",
                        str(json_out),
                        "--markdown-out",
                        str(markdown_out),
                    ]
                )

            stored = json.loads(json_out.read_text(encoding="utf-8"))
            printed = json.loads(output.getvalue())
            markdown = markdown_out.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(stored, printed)
        self.assertTrue(stored["core_backend_ready"])
        self.assertTrue(stored["reference_chat_runtime_ready"])
        self.assertTrue(stored["private_pilot_backend_ready"])
        self.assertFalse(stored["private_pilot_ready"])
        self.assertFalse(stored["status_update_allowed"])
        self.assertFalse(stored["db_writes"])
        self.assertFalse(stored["approval_claim"])
        self.assertFalse(stored["safety_flags"]["human_review_status_writes"])
        self.assertIn("Institutional Chatbot Readiness", markdown)
        self.assertIn("reference_chat_runtime_ready: `true`", markdown)
        self.assertIn("repository supplies the reference chat UI and API", markdown)
        self.assertIn("Gateway auth / origin / rejection audit: `true` / `true` / `true`", markdown)
        self.assertIn("Audit excludes prompt / raw identity / secret", markdown)
        self.assertIn("Institution-approved LLM integration is optional", markdown)
        self.assertIn("private_pilot_ready: `false`", markdown)
        self.assertIn("institution_integration_evidence_not_provided", markdown)
        self.assertIn("Evidence Paths And Fingerprints", markdown)

    def _synthesize(
        self,
        paths: dict[str, Path],
        *,
        now: datetime,
    ) -> dict[str, object]:
        return readiness.synthesize_from_paths(
            release_readiness_path=paths["release_readiness"],
            deployment_decision_path=paths["deployment_decision"],
            chatbot_benchmark_path=paths["chatbot_benchmark"],
            source_preview_summary_path=paths["source_preview_summary"],
            institutional_chat_smoke_path=paths.get("institutional_chat_smoke"),
            institution_integration_path=paths.get("institution_integration"),
            max_evidence_age_hours=72,
            now=now,
        )

    def _write_evidence_set(
        self,
        root: Path,
        *,
        now: datetime,
        stable: bool,
        benchmark_time: datetime | None = None,
        smoke_time: datetime | None = None,
        include_smoke: bool = True,
        include_integration: bool = True,
    ) -> dict[str, Path]:
        release_path = root / "release-readiness.json"
        deployment_path = root / "deployment-decision.json"
        benchmark_path = root / "chatbot-benchmark.json"
        smoke_path = root / "institutional-chat-smoke.json"
        preview_path = root / "source-preview-summary.json"
        integration_path = root / "institution-integration.json"
        export_path = root / "source-preview-export.json"
        output_dir = root / "source-preview-tree"
        output_dir.mkdir(parents=True)
        export_path.write_text("{}\n", encoding="utf-8")

        blocker_names = [] if stable else ["qualification:collection_coverage"]
        blockers = [
            {
                "category": "data_collection",
                "name": name,
                "message": "Qualification coverage remains below the stable threshold.",
            }
            for name in blocker_names
        ]
        release = {
            "schema": readiness.RELEASE_SCHEMA,
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "release_ready": stable,
            "release_decision": {
                "status": "ready" if stable else "blocked_until_requirements_met",
                "release_ready": stable,
                "approval_claim": False,
                "human_decision_required_for_release_claim": True,
                "blocked_by": blocker_names,
            },
            "engineering_hygiene_ok": True,
            "blocker_count": len(blockers),
            "blockers": blockers,
            "artifact_date_contract": {
                "release_outputs": {
                    "ok": True,
                    "expected_date": now.strftime("%Y%m%d"),
                },
                "proof_artifacts": {
                    "ok": True,
                    "expected_date": now.strftime("%Y%m%d"),
                },
            },
            "artifact_lineage_contract": {"ok": True},
            "dashboard_surface_contract": {"ok": True},
            "cycle_safe_content_sha256": "sha256:" + "a" * 64,
        }

        scenarios = []
        scenario_tools = {
            "structure_search": "ncs_search",
            "task_training": "recommend_training_for_task",
            "training_transition": "recommend_training_transition",
            "education_system_design": "plan_ncs_education_path",
        }
        for scenario_id, tool in scenario_tools.items():
            scenarios.append(
                {
                    "id": scenario_id,
                    "tool": tool,
                    "valid": True,
                    "route": {
                        "schema": "ncs_query_route_v1",
                        "scenario": scenario_id,
                        "tool": tool,
                        "available": True,
                        "missing_params": [],
                        "route_fingerprint": f"route-{scenario_id}",
                    },
                }
            )
        measured_runs = len(scenarios) * 2
        absent_sidecars = {
            suffix: {
                "path": str(benchmark_path) + suffix,
                "exists": False,
                "sha256": None,
                "size_bytes": None,
                "mtime_ns": None,
                "stable_during_hash": True,
            }
            for suffix in readiness.SQLITE_SIDECAR_SUFFIXES
        }
        benchmark = {
            "schema": readiness.BENCHMARK_SCHEMA,
            "generated_at": (benchmark_time or now).isoformat(),
            "ok": True,
            "readiness_status": "ready",
            "mutation_policy": "report_only",
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "external_api_calls": False,
            "network_access_required": False,
            "human_status_changes_observed": False,
            "database": {
                "before": {
                    "sha256": "sha256:" + "b" * 64,
                    "stable_during_hash": True,
                    "manifest_schema": readiness.SQLITE_FILE_MANIFEST_SCHEMA,
                    "sidecars": absent_sidecars,
                },
                "after": {
                    "sha256": "sha256:" + "b" * 64,
                    "stable_during_hash": True,
                    "manifest_schema": readiness.SQLITE_FILE_MANIFEST_SCHEMA,
                    "sidecars": absent_sidecars,
                },
                "immutability": {
                    "sha256_unchanged": True,
                    "size_unchanged": True,
                    "mtime_unchanged": True,
                    "base_unchanged": True,
                    "sidecars_unchanged": True,
                    "changed_sidecars": [],
                    "storage_content_unchanged": True,
                    "all_unchanged": True,
                },
                "filesystem_mutation_observed": False,
                "storage_content_unchanged": True,
            },
            "read_only_preflight": {
                "ok": True,
                "configured_read_only_mode": True,
                "sqlite_query_only": True,
                "database_readiness": {"ready": True},
            },
            "summary": {
                "scenario_count": len(scenarios),
                "valid_scenario_count": len(scenarios),
                "total_measured_runs": measured_runs,
                "valid_measured_runs": measured_runs,
                "invalid_measured_runs": 0,
                "result_validity_rate": 1.0,
                "latency_ms": {
                    "sample_count": measured_runs,
                    "p50": 100.0,
                    "p95": 200.0,
                    "max": 220.0,
                },
            },
            "scenarios": scenarios,
        }

        smoke_checks = {
            check_id: True
            for check_id in readiness.REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS
        }
        smoke_database_snapshot = {
            "size_bytes": 1_273_856,
            "mtime_ns": 1_783_910_301_100_661_100,
            "sha256": "c" * 64,
        }
        smoke = {
            "schema": readiness.INSTITUTIONAL_CHAT_SMOKE_SCHEMA,
            "ok": True,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "checks": smoke_checks,
            "startup": {
                "status": "ready",
                "url": "http://127.0.0.1:41001",
                "auth_mode": "local",
                "read_only": True,
                "operator_tools_enabled": False,
                "audit_logging": False,
            },
            "ready": {
                "schema": "ncs_institutional_chat_health_v1",
                "status": "ready",
                "ready": True,
                "release_version": "0.1.0",
                "read_only_mode": True,
                "operator_tools_enabled": False,
                "database_ready": True,
                "gateway_auth_required": False,
                "audit_logging_required": False,
                "public_tool_count": 9,
                "max_http_workers": 32,
                "request_socket_timeout_seconds": 15.0,
            },
            "chat": {
                "status": 200,
                "state": "completed",
                "tool": "recommend_training_for_task",
                "route_fingerprint": "reference-chat-route-fingerprint",
                "course_count": 1,
                "audit": {
                    "request_id": "request-id",
                    "duration_ms": 25.0,
                    "logged": False,
                    "release_version": "0.1.0",
                    "db_writes": False,
                    "operator_tool_execution": False,
                },
            },
            "operator_block": {
                "status": 403,
                "error_code": "operator_route_blocked",
            },
            "gateway": {
                "secret_source": "file",
                "startup": {
                    "status": "ready",
                    "url": "http://127.0.0.1:41002",
                    "auth_mode": "gateway",
                    "read_only": True,
                    "operator_tools_enabled": False,
                    "audit_logging": True,
                },
                "bad_origin_status": 403,
                "bad_origin_error": "origin_not_allowed",
                "missing_secret_status": 401,
                "missing_secret_error": "authentication_required",
                "chat_status": 200,
                "chat_state": "completed",
                "audit_event_count": 3,
                "audit_error_codes": [
                    "origin_not_allowed",
                    "authentication_required",
                    None,
                ],
                "audit_identity_hash_present": True,
            },
            "database_before": dict(smoke_database_snapshot),
            "database_after": dict(smoke_database_snapshot),
        }

        preview = {
            "schema": readiness.PREVIEW_SCHEMA,
            "generated_at": now.isoformat(),
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "ok": True,
            "contract_ok": True,
            "execution_authorized": False,
            "human_signoff_required": True,
            "preview_is_not_approval": True,
            "preview_allowed_by_product_evidence": True,
            "preview_evidence_complete": True,
            "supporting_evidence_freshness_ok": True,
            "supporting_evidence_freshness": {
                "ok": True,
                "missing_artifacts": [],
                "stale_artifacts": [],
            },
            "stable_release_ready": stable,
            "source_preview_export_ok": True,
            "source_package_ok": True,
            "preview_blockers": [],
            "preview_warnings": [],
            "release": {
                "path": str(release_path),
                "release_ready": stable,
                "engineering_hygiene_ok": True,
                "blocker_count": len(blockers),
                "blocked_by": blocker_names,
            },
            "dashboard": {
                "path": str(root / "dashboard.json"),
                "ok": True,
                "static_artifacts_ok": True,
                "artifact_date_contract_ok": True,
                "artifact_lineage_contract_ok": True,
                "review_chain_safety": {
                    "do_not_set_human_reviewed_accepted_reviewed_automatically": True,
                },
            },
            "source_preview_export": {
                "path": str(export_path),
                "ok": True,
                "generated_at": now.isoformat(),
                "output_dir": str(output_dir),
                "output_dir_exists": True,
                "output_dir_is_dir": True,
            },
        }

        deployment = {
            "schema": readiness.DEPLOYMENT_SCHEMA,
            "generated_at": now.isoformat(),
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "deployment_execution_authorized": False,
            "human_signoff_required": True,
            "private_preview_is_not_human_signoff": True,
            "private_preview_deployable_now": True,
            "private_preview_contract_satisfied": True,
            "stable_release_ready": stable,
            "source_preview": {
                "source_package_ok": True,
                "source_preview_export_ok": True,
                "source_preview_export_path": str(export_path),
                "source_preview_export_generated_at": now.isoformat(),
                "output_dir": str(output_dir),
                "output_dir_exists": True,
                "output_dir_is_dir": True,
                "tree_verification_ok": True,
                "tree_hash_consistency_ok": True,
                "required_artifacts_present": True,
                "source_metadata_ok": True,
                "same_tree_ok": True,
                "freshness_ok": True,
                "supporting_evidence_freshness_ok": True,
                "supporting_evidence_freshness": {"ok": True},
                "missing_artifacts": [],
                "output_dir_mismatches": [],
                "freshness_failures": [],
            },
            "product_evidence": {
                "preview_allowed_by_product_evidence": True,
                "preview_evidence_complete": True,
                "preview_is_not_approval": True,
                "dashboard_ok": True,
                "static_artifacts_ok": True,
                "release_engineering_hygiene_ok": True,
            },
            "human_review_guardrail": {
                "do_not_set_human_reviewed_accepted_reviewed_automatically": True,
            },
            "open_stable_blockers": blocker_names,
            "evidence_files": [str(preview_path), str(export_path)],
        }

        integration = {
            "schema": readiness.INTEGRATION_SCHEMA,
            "generated_at": now.isoformat(),
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "controls": {
                item["id"]: {
                    "implemented": True,
                    "tested": True,
                    "owner": f"owner-{item['id']}",
                    "evidence_refs": [f"ticket-{item['id']}"],
                }
                for item in readiness.INTEGRATION_REQUIREMENTS
            },
        }

        self._write(release_path, release, now)
        self._write(deployment_path, deployment, now)
        self._write(benchmark_path, benchmark, now)
        if include_smoke:
            self._write(smoke_path, smoke, smoke_time or now)
        self._write(preview_path, preview, now)
        paths = {
            "release_readiness": release_path,
            "deployment_decision": deployment_path,
            "chatbot_benchmark": benchmark_path,
            "source_preview_summary": preview_path,
        }
        if include_smoke:
            paths["institutional_chat_smoke"] = smoke_path
        if include_integration:
            self._write(integration_path, integration, now)
            paths["institution_integration"] = integration_path
        return paths

    @staticmethod
    def _write(path: Path, payload: dict[str, object], mtime: datetime) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.utime(path, (mtime.timestamp(), mtime.timestamp()))

    @staticmethod
    def _read(path: Path) -> dict[str, object]:
        return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
