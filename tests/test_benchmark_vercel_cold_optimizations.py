from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path

from scripts import benchmark_vercel_cold_optimizations as benchmark


class VercelColdOptimizationBenchmarkTests(unittest.TestCase):
    def test_latency_summary_has_nearest_rank_percentiles_and_cv(self) -> None:
        summary = benchmark.latency_summary([1.0, 2.0, 3.0])

        self.assertEqual(summary["sample_count"], 3)
        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 3.0)
        self.assertGreater(summary["coefficient_of_variation"], 0)

    def test_experiment_matrix_separates_safe_and_diagnostic_candidates(self) -> None:
        variants = benchmark.experiment_variants()
        by_id = {variant.variant_id: variant for variant in variants}

        self.assertEqual(len(variants), 5)
        safe = by_id["safe_readinto_4m_stream_hash_fsync"]
        self.assertTrue(safe.compute_stream_sha256)
        self.assertTrue(safe.fsync_extracted_file)
        self.assertFalse(safe.default_promotion_prohibited)
        self.assertTrue(
            by_id["diagnostic_readinto_4m_no_hash_fsync"].default_promotion_prohibited
        )
        self.assertTrue(
            by_id["diagnostic_readinto_4m_stream_hash_no_fsync"].default_promotion_prohibited
        )

    def test_build_report_uses_read_only_sources_and_records_all_gates(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            database = root / "ncs_ontology_compact.db"
            with closing(sqlite3.connect(database)) as conn:
                conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO sample(value) VALUES ('evidence')")
                conn.commit()
            database_bytes = database.read_bytes()
            expected_sha = hashlib.sha256(database_bytes).hexdigest()
            archive = root / "ncs_ontology_compact.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
                output.write(database, arcname=database.name)
            manifest = root / "ncs_ontology_compact.manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "sqlite_bytes": len(database_bytes),
                        "sqlite_sha256": expected_sha,
                    }
                ),
                encoding="utf-8",
            )
            archive_before = archive.read_bytes()
            manifest_before = manifest.read_bytes()

            report = benchmark.build_report(
                archive_path=archive,
                manifest_path=manifest,
                runs=3,
                environment_label="unit-test",
                temp_root=root,
                warmup=False,
            )

            self.assertTrue(report["ok"])
            self.assertTrue(report["source_artifacts_unchanged"])
            self.assertEqual(archive.read_bytes(), archive_before)
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual(len(report["variants"]), 5)
            self.assertTrue(all(item["run_count"] == 3 for item in report["variants"]))
            self.assertTrue(report["rss_measurement_methods"])
            self.assertTrue(
                all(item["rss_measurement_methods"] for item in report["variants"])
            )
            self.assertEqual(
                report["promotion_policy"]["remote_measurement_status"],
                "not_measured",
            )
            self.assertFalse(report["promotion_policy"]["promotion_allowed"])
            unsafe = next(
                item
                for item in report["variants"]
                if item["variant"]["variant_id"]
                == "diagnostic_readinto_4m_no_hash_no_fsync"
            )
            self.assertTrue(unsafe["promotion_gate"]["default_promotion_prohibited"])
            self.assertFalse(unsafe["promotion_gate"]["cryptographic_integrity_gate_pass"])
            self.assertFalse(unsafe["promotion_gate"]["explicit_fsync_gate_pass"])

    def test_missing_source_report_is_a_safe_failure(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            report = benchmark.build_report(
                archive_path=root / "missing.zip",
                manifest_path=root / "missing.json",
                runs=3,
                environment_label="unit-test",
                warmup=False,
            )

        self.assertFalse(report["ok"])
        self.assertEqual(report["error"], "archive_or_manifest_missing")
        self.assertEqual(report["remote_measurement_status"], "not_measured")


if __name__ == "__main__":
    unittest.main()
