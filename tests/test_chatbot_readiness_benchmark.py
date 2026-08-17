from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for candidate in (SRC, SCRIPTS):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

import benchmark_chatbot_readiness as benchmark
from ncs_mcp.smoke_data import create_ready_smoke_db


class ChatbotReadinessBenchmarkTests(unittest.TestCase):
    def test_benchmark_runs_public_workflows_and_preserves_smoke_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            create_ready_smoke_db(db_path)
            external_before = benchmark.snapshot_database(db_path)

            with patch.dict(
                os.environ,
                {"NCS_DB_PATH": "caller-value", "NCS_MCP_READ_ONLY": "0"},
                clear=False,
            ):
                report = benchmark.run_benchmark(
                    db_path,
                    iterations=2,
                    warmup_iterations=0,
                    limit=2,
                )
                self.assertEqual(os.environ["NCS_DB_PATH"], "caller-value")
                self.assertEqual(os.environ["NCS_MCP_READ_ONLY"], "0")

            external_after = benchmark.snapshot_database(db_path)

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], "ncs_chatbot_readiness_benchmark_v1")
        self.assertEqual(report["readiness_status"], "ready")
        self.assertEqual(report["mutation_policy"], "report_only")
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertFalse(report["external_api_calls"])
        self.assertFalse(report["network_access_required"])
        self.assertFalse(report["human_status_changes_observed"])
        self.assertTrue(report["read_only_preflight"]["ok"])
        self.assertTrue(report["read_only_preflight"]["configured_read_only_mode"])
        self.assertTrue(report["read_only_preflight"]["sqlite_query_only"])
        self.assertTrue(report["database"]["immutability"]["all_unchanged"])
        self.assertTrue(report["database"]["immutability"]["base_unchanged"])
        self.assertTrue(report["database"]["immutability"]["sidecars_unchanged"])
        self.assertEqual(report["database"]["immutability"]["changed_sidecars"], [])
        self.assertEqual(external_before, external_after)
        for snapshot_name in ("before", "after"):
            snapshot = report["database"][snapshot_name]
            self.assertEqual(
                snapshot["manifest_schema"],
                "sqlite_database_file_manifest_v1",
            )
            self.assertTrue(snapshot["base"]["exists"])
            self.assertEqual(
                set(snapshot["sidecars"]),
                set(benchmark.SQLITE_SIDECAR_SUFFIXES),
            )
            for sidecar in snapshot["sidecars"].values():
                self.assertIn("exists", sidecar)
                self.assertIn("size_bytes", sidecar)
                self.assertIn("mtime_ns", sidecar)
                self.assertIn("sha256", sidecar)

        self.assertEqual(report["summary"]["scenario_count"], 4)
        self.assertEqual(report["summary"]["valid_scenario_count"], 4)
        self.assertEqual(report["summary"]["total_measured_runs"], 8)
        self.assertEqual(report["summary"]["valid_measured_runs"], 8)
        self.assertEqual(report["summary"]["invalid_measured_runs"], 0)
        self.assertEqual(report["configuration"]["concurrency"], 1)
        self.assertGreater(report["summary"]["throughput_requests_per_second"], 0)
        self.assertEqual(report["summary"]["result_validity_rate"], 1.0)
        self._assert_latency_summary(report["summary"]["latency_ms"], sample_count=8)

        expected = {
            "structure_search": ("structure_search", "ncs_search"),
            "task_training": ("task_training", "recommend_training_for_task"),
            "training_transition": ("training_transition", "recommend_training_transition"),
            "education_system_design": (
                "education_system_design",
                "plan_ncs_education_path",
            ),
        }
        self.assertEqual({item["id"] for item in report["scenarios"]}, set(expected))
        for scenario in report["scenarios"]:
            expected_route, expected_tool = expected[scenario["id"]]
            self.assertTrue(scenario["valid"])
            self.assertEqual(scenario["route"]["schema"], "ncs_query_route_v1")
            self.assertEqual(scenario["route"]["scenario"], expected_route)
            self.assertEqual(scenario["route"]["tool"], expected_tool)
            self.assertTrue(scenario["route"]["available"])
            self.assertEqual(scenario["route"]["missing_params"], [])
            self.assertTrue(scenario["route"]["route_fingerprint"])
            self._assert_latency_summary(scenario["latency_ms"], sample_count=2)
            for run in scenario["runs"]:
                self.assertTrue(run["result_valid"])
                self.assertEqual(run["validation_errors"], [])
                self.assertGreaterEqual(run["elapsed_ms"], 0)
                self.assertTrue(run["result"]["ok"])
                self.assertGreater(run["result"]["item_count"], 0)
                if expected_tool in benchmark.SAVE_FORCED_TOOLS:
                    self.assertTrue(run["result"]["capacity"]["acquired"])

    def test_cli_writes_only_report_and_keeps_db_fingerprint_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            report_path = Path(tmp) / "reports" / "chatbot-readiness.json"
            markdown_path = Path(tmp) / "reports" / "chatbot-readiness.md"
            create_ready_smoke_db(db_path)
            before = benchmark.snapshot_database(db_path)

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = benchmark.main(
                    [
                        "--db",
                        str(db_path),
                        "--out",
                        str(report_path),
                        "--markdown-out",
                        str(markdown_path),
                        "--iterations",
                        "1",
                        "--warmup-iterations",
                        "0",
                        "--limit",
                        "1",
                    ]
                )

            after = benchmark.snapshot_database(db_path)
            stored = json.loads(report_path.read_text(encoding="utf-8"))
            printed = json.loads(output.getvalue())
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(before, after)
        self.assertEqual(stored, printed)
        self.assertTrue(stored["ok"])
        self.assertTrue(stored["database"]["immutability"]["all_unchanged"])
        self.assertIn("Institutional Chatbot Readiness Benchmark", markdown)
        self.assertIn("database_unchanged: `true`", markdown)
        self.assertIn("database_base_unchanged: `true`", markdown)
        self.assertIn("database_sidecars_unchanged: `true`", markdown)
        self.assertEqual(stored["summary"]["total_measured_runs"], 4)
        self.assertEqual(stored["summary"]["latency_ms"]["sample_count"], 4)
        for scenario in stored["scenarios"]:
            if scenario["tool"] in benchmark.SAVE_FORCED_TOOLS:
                self.assertIs(scenario["params"]["save"], False)

    def test_sidecar_create_delete_and_modify_fail_immutability(self) -> None:
        cases = (
            ("-wal", "created"),
            ("-shm", "deleted"),
            ("-journal", "modified"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            for index, (suffix, expected_change) in enumerate(cases):
                with self.subTest(suffix=suffix, change=expected_change):
                    db_path = Path(tmp) / f"case-{index}" / "ncs-smoke.db"
                    db_path.parent.mkdir()
                    create_ready_smoke_db(db_path)
                    sidecar_path = Path(f"{db_path}{suffix}")
                    if expected_change != "created":
                        sidecar_path.write_bytes(b"sidecar-before")

                    before = benchmark.snapshot_database(db_path)
                    if expected_change == "created":
                        sidecar_path.write_bytes(b"sidecar-created")
                    elif expected_change == "deleted":
                        sidecar_path.unlink()
                    else:
                        sidecar_path.write_bytes(b"sidecar-after!")
                    after = benchmark.snapshot_database(db_path)
                    immutability = benchmark.compare_database_snapshots(before, after)

                    self.assertFalse(immutability["all_unchanged"])
                    self.assertTrue(immutability["base_unchanged"])
                    self.assertFalse(immutability["sidecars_unchanged"])
                    self.assertFalse(immutability["storage_content_unchanged"])
                    self.assertEqual(immutability["changed_sidecars"], [suffix])
                    self.assertEqual(
                        immutability["content_changed_sidecars"],
                        [suffix],
                    )
                    comparison = immutability["sidecar_comparisons"][suffix]
                    self.assertFalse(comparison["unchanged"])
                    self.assertEqual(comparison["change_type"], expected_change)
                    for other_suffix in benchmark.SQLITE_SIDECAR_SUFFIXES:
                        if other_suffix != suffix:
                            self.assertTrue(
                                immutability["sidecar_comparisons"][other_suffix][
                                    "unchanged"
                                ]
                            )

    def test_benchmark_reports_sidecar_creation_without_relaxing_safety_flags(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            sidecar_path = Path(f"{db_path}-wal")
            create_ready_smoke_db(db_path)
            real_snapshot = benchmark.snapshot_database
            snapshot_calls = 0

            def snapshot_with_sidecar_creation(path: Path) -> dict[str, object]:
                nonlocal snapshot_calls
                snapshot_calls += 1
                if snapshot_calls == 2:
                    sidecar_path.write_bytes(b"created-after-benchmark")
                return real_snapshot(path)

            with patch.object(
                benchmark,
                "snapshot_database",
                side_effect=snapshot_with_sidecar_creation,
            ):
                report = benchmark.run_benchmark(
                    db_path,
                    iterations=1,
                    warmup_iterations=0,
                    limit=1,
                )

        self.assertFalse(report["ok"])
        self.assertEqual(report["readiness_status"], "not_ready")
        self.assertEqual(report["mutation_policy"], "report_only")
        self.assertFalse(report["status_update_allowed"])
        self.assertIsNone(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertIsNone(report["human_status_changes_observed"])
        immutability = report["database"]["immutability"]
        self.assertFalse(immutability["all_unchanged"])
        self.assertTrue(immutability["base_unchanged"])
        self.assertFalse(immutability["sidecars_unchanged"])
        self.assertFalse(immutability["storage_content_unchanged"])
        self.assertEqual(immutability["changed_sidecars"], ["-wal"])
        self.assertTrue(report["database"]["filesystem_mutation_observed"])
        self.assertEqual(
            immutability["sidecar_comparisons"]["-wal"]["change_type"],
            "created",
        )

    def test_sidecar_metadata_only_change_is_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            create_ready_smoke_db(db_path)
            shm_path = Path(f"{db_path}-shm")
            shm_path.write_bytes(b"stable-lock-content")
            before = benchmark.snapshot_database(db_path)
            stat = shm_path.stat()
            os.utime(
                shm_path,
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000),
            )
            after = benchmark.snapshot_database(db_path)

        immutability = benchmark.compare_database_snapshots(before, after)
        self.assertFalse(immutability["all_unchanged"])
        self.assertFalse(immutability["sidecars_unchanged"])
        self.assertTrue(immutability["storage_content_unchanged"])
        self.assertEqual(immutability["changed_sidecars"], ["-shm"])
        self.assertEqual(immutability["content_changed_sidecars"], [])

    def test_main_rejects_database_as_report_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            create_ready_smoke_db(db_path)
            before = benchmark.snapshot_database(db_path)

            with self.assertRaisesRegex(ValueError, "must not be the SQLite DB path"):
                benchmark.main(["--db", str(db_path), "--out", str(db_path)])

            after = benchmark.snapshot_database(db_path)

        self.assertEqual(before, after)

    def test_benchmark_supports_bounded_concurrent_requests(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs-smoke.db"
            create_ready_smoke_db(db_path)

            report = benchmark.run_benchmark(
                db_path,
                iterations=2,
                warmup_iterations=0,
                concurrency=2,
                limit=2,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["configuration"]["concurrency"], 2)
        self.assertEqual(report["summary"]["total_measured_runs"], 8)
        self.assertEqual(report["summary"]["valid_measured_runs"], 8)
        self.assertGreater(report["summary"]["throughput_requests_per_second"], 0)
        self.assertTrue(report["database"]["immutability"]["all_unchanged"])

    def _assert_latency_summary(self, payload: dict[str, object], *, sample_count: int) -> None:
        self.assertEqual(payload["sample_count"], sample_count)
        self.assertIsInstance(payload["p50"], float)
        self.assertIsInstance(payload["p95"], float)
        self.assertIsInstance(payload["max"], float)
        self.assertLessEqual(payload["p50"], payload["p95"])
        self.assertLessEqual(payload["p95"], payload["max"])


if __name__ == "__main__":
    unittest.main()
