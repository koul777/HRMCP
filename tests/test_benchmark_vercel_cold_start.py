from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from scripts import benchmark_vercel_cold_start as benchmark
from ncs_mcp import vercel_snapshot


class BenchmarkVercelColdStartTests(unittest.TestCase):
    def _build_fixture(self, root: Path) -> tuple[Path, Path]:
        database = root / vercel_snapshot.COMPACT_SNAPSHOT_NAME
        physical_counts = {
            "competency_units": 1,
            "performance_criteria": 1,
            "ksa_items": 1,
            "ncs_training_courses": 1,
        }
        logical_counts = {"competency_units": 1}
        with closing(sqlite3.connect(database)) as conn:
            for table_name in physical_counts:
                conn.execute(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY)')
                conn.execute(f'INSERT INTO "{table_name}" (id) VALUES (1)')
            conn.execute(
                "CREATE TABLE serving_snapshot_manifest "
                "(manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                [
                    ("schema", vercel_snapshot.COMPACT_DATABASE_SCHEMA),
                    ("codec", vercel_snapshot.COMPACT_POSTING_CODEC),
                ],
            )
            conn.execute(
                "CREATE TABLE serving_snapshot_table_counts "
                "(object_name TEXT NOT NULL, row_count INTEGER NOT NULL, count_kind TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'physical')",
                list(physical_counts.items()),
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'logical')",
                list(logical_counts.items()),
            )
            conn.commit()

        sqlite_bytes = database.stat().st_size
        sqlite_sha256 = hashlib.sha256(database.read_bytes()).hexdigest()
        archive = root / vercel_snapshot.COMPACT_ARCHIVE_NAME
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED
        ) as output:
            output.write(database, arcname=vercel_snapshot.COMPACT_SNAPSHOT_NAME)
        manifest = root / vercel_snapshot.COMPACT_MANIFEST_NAME
        manifest.write_text(
            json.dumps(
                {
                    "schema": vercel_snapshot.COMPACT_MANIFEST_SCHEMA,
                    "archive_member": vercel_snapshot.COMPACT_SNAPSHOT_NAME,
                    "database_schema": vercel_snapshot.COMPACT_DATABASE_SCHEMA,
                    "codec": vercel_snapshot.COMPACT_POSTING_CODEC,
                    "sqlite_bytes": sqlite_bytes,
                    "sqlite_sha256": sqlite_sha256,
                    "physical_counts": physical_counts,
                    "logical_counts": logical_counts,
                }
            ),
            encoding="utf-8",
        )
        return archive, manifest

    def test_latency_summary_reports_nearest_rank_and_variance(self) -> None:
        summary = benchmark.latency_summary([4.0, 1.0, 3.0, 2.0])

        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 4.0)
        self.assertGreater(summary["population_stdev"], 0)
        self.assertGreater(summary["coefficient_of_variation"], 0)

    def test_report_profiles_three_fresh_destinations_and_cleans_them(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            archive, manifest = self._build_fixture(root)
            before = (archive.stat().st_mtime_ns, manifest.stat().st_mtime_ns)

            report = benchmark.build_report(
                archive_path=archive,
                manifest_path=manifest,
                runs=3,
                environment_label="fixture",
                temp_root=root,
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["run_count_successful"], 3)
            self.assertTrue(report["source_artifacts_unchanged"])
            self.assertTrue(report["safety"]["temporary_directories_removed"])
            self.assertEqual(
                before, (archive.stat().st_mtime_ns, manifest.stat().st_mtime_ns)
            )
            for run in report["runs"]:
                self.assertIn(
                    "extract_stream_write_sha256", run["runtime_stages_ms"]
                )
                self.assertIn("sqlite_validation_open", run["runtime_stages_ms"])
                self.assertIn("publish_rename", run["runtime_stages_ms"])
                self.assertIn(
                    "archive_sha256_diagnostic", run["diagnostic_only_stages_ms"]
                )
                self.assertTrue(run["stream_sha256_matches_manifest"])
                self.assertTrue(run["second_pass_sha256_matches_manifest"])
                self.assertTrue(run["temporary_directory_removed"])

    def test_gate_keeps_remote_measurement_unconfirmed(self) -> None:
        runtime = {
            "extract_stream_write_sha256": benchmark.latency_summary(
                [80.0, 90.0, 100.0]
            ),
            "sqlite_validation_open": benchmark.latency_summary([10.0, 10.0, 10.0]),
        }
        gate = benchmark._dominance_and_gate(
            runtime, benchmark.latency_summary([100.0, 110.0, 120.0])
        )

        self.assertEqual(gate["dominant_runtime_stage"], "extract_stream_write_sha256")
        self.assertTrue(gate["dominant_threshold_met"])
        self.assertEqual(gate["remote_measurement_status"], "not_measured")
        self.assertFalse(gate["promotion_allowed"])
        self.assertFalse(gate["diagnostic_hashes_are_optimization_targets"])

    def test_markdown_separates_runtime_and_diagnostic_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            archive, manifest = self._build_fixture(root)
            report = benchmark.build_report(
                archive_path=archive,
                manifest_path=manifest,
                runs=3,
                environment_label="fixture",
                temp_root=root,
            )

            markdown = benchmark.render_markdown(report)

            self.assertIn("Runtime-Critical Stages", markdown)
            self.assertIn("Diagnostic-Only Hash Passes", markdown)
            self.assertIn("Remote measurement status: `not_measured`", markdown)
            self.assertIn("Promotion allowed: `False`", markdown)
            self.assertIn("Source artifacts unchanged: `True`", markdown)


if __name__ == "__main__":
    unittest.main()
