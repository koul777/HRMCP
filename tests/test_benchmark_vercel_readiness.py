from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import tempfile
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "benchmark_vercel_readiness",
    ROOT / "scripts" / "benchmark_vercel_readiness.py",
)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class VercelReadinessBenchmarkTests(unittest.TestCase):
    REQUIRED = (
        "competency_units",
        "performance_criteria",
        "ksa_items",
        "ncs_training_courses",
    )

    def setUp(self) -> None:
        benchmark._PROCESS_CACHE.clear()
        benchmark._runtime_readiness().clear_verified_readiness_counts()

    def tearDown(self) -> None:
        benchmark._runtime_readiness().clear_verified_readiness_counts()

    def _fixture(self, root: Path, counts: dict[str, int] | None = None) -> tuple[Path, Path, Path, dict[str, str]]:
        counts = counts or {name: index + 1 for index, name in enumerate(self.REQUIRED)}
        db_path = root / "ncs_ontology_compact.db"
        with closing(sqlite3.connect(db_path)) as conn:
            for table_name, count in counts.items():
                conn.execute(f'CREATE TABLE "{table_name}" (id INTEGER PRIMARY KEY)')
                conn.executemany(f'INSERT INTO "{table_name}" (id) VALUES (?)', [(index + 1,) for index in range(count)])
            conn.execute("CREATE TABLE serving_snapshot_table_counts (object_name TEXT, row_count INTEGER, count_kind TEXT)")
            conn.executemany(
                "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'physical')",
                list(counts.items()),
            )
            conn.execute("CREATE TABLE serving_snapshot_manifest (manifest_key TEXT, manifest_value TEXT)")
            conn.commit()
        digest = hashlib.sha256(db_path.read_bytes()).hexdigest()
        manifest = root / "ncs_ontology_compact.manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "archive_member": db_path.name,
                    "sqlite_bytes": db_path.stat().st_size,
                    "sqlite_sha256": digest,
                    "physical_counts": counts,
                    "logical_counts": {},
                    "servable_counts": {},
                }
            ),
            encoding="utf-8",
        )
        archive = root / "ncs_ontology_compact.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
            output.write(db_path, arcname=db_path.name)
        env = {"NCS_MCP_READINESS_EXTRA_TABLES": "", "NCS_MCP_READ_ONLY": "1"}
        return db_path, manifest, archive, env

    def test_manifest_candidate_matches_actual_runtime_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            db_path, manifest, _archive, env = self._fixture(Path(raw_dir))
            actual = benchmark.run_candidate("A_count", db_path, manifest, env)
            metadata = benchmark.run_candidate("B_manifest", db_path, manifest, env)

            self.assertEqual(benchmark.contract_signature(actual), benchmark.contract_signature(metadata))
            self.assertFalse(metadata["used_scan_fallback"])

    def test_count_baseline_stays_sql_scan_after_product_fast_path_is_configured(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            db_path, manifest, _archive, env = self._fixture(Path(raw_dir))
            runtime_readiness = benchmark._runtime_readiness()
            counts = {
                table_name: 1
                for table_name in (
                    runtime_readiness.READINESS_CORE_TABLES
                    + runtime_readiness.READINESS_PUBLIC_TOOL_TABLES
                )
            }
            with benchmark.applied_environment(env):
                configured = runtime_readiness.configure_verified_readiness_counts(
                    db_path,
                    sqlite_sha256=hashlib.sha256(db_path.read_bytes()).hexdigest(),
                    sqlite_bytes=db_path.stat().st_size,
                    table_counts=counts,
                    required_tables=runtime_readiness.READINESS_CORE_TABLES,
                    minimum_rows={},
                )
                self.assertTrue(configured)
                self.assertEqual(
                    runtime_readiness.database_readiness_metadata(db_path)[
                        "readiness_count_source"
                    ],
                    "verified_snapshot_metadata",
                )

            baseline = benchmark.run_candidate("A_count", db_path, manifest, env)

            self.assertEqual(baseline["readiness_count_source"], "sql_count")

    def test_count_mismatch_is_detected_and_manifest_mismatch_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            db_path, manifest_path, _archive, env = self._fixture(root)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["physical_counts"]["competency_units"] += 7
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            audit = benchmark.audit_metadata_against_actual(db_path, manifest, list(self.REQUIRED))
            result = benchmark.run_candidate("B_manifest", db_path, manifest_path, env)

            self.assertFalse(audit["ok"])
            self.assertEqual(result["fallback_reason"], "metadata_manifest_mismatch")
            self.assertTrue(result["used_scan_fallback"])
            self.assertEqual(result["core_tables"]["competency_units"]["row_count"], 1)

    def test_process_cache_invalidates_after_file_change(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            db_path, manifest_path, _archive, env = self._fixture(root)
            first = benchmark.run_candidate("C_process_cache", db_path, manifest_path, env)
            second = benchmark.run_candidate("C_process_cache", db_path, manifest_path, env)
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])

            time.sleep(0.01)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("INSERT INTO competency_units VALUES (99)")
                conn.commit()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["sqlite_bytes"] = db_path.stat().st_size
            manifest["sqlite_sha256"] = hashlib.sha256(db_path.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            os.utime(db_path, None)

            third = benchmark.run_candidate("C_process_cache", db_path, manifest_path, env)
            self.assertFalse(third["cache_hit"])
            self.assertEqual(third["core_tables"]["competency_units"]["row_count"], 2)

    def test_external_override_and_full_database_force_scan_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            db_path, manifest, _archive, env = self._fixture(Path(raw_dir))
            candidate_b = benchmark.run_candidate(
                "B_manifest", db_path, manifest, env, source_kind="explicit_override", trusted_compact=False
            )
            candidate_c = benchmark.run_candidate(
                "C_process_cache", db_path, manifest, env, source_kind="full_database", trusted_compact=False
            )

            self.assertTrue(candidate_b["used_scan_fallback"])
            self.assertTrue(candidate_c["used_scan_fallback"])
            self.assertEqual(candidate_b["fallback_reason"], "untrusted_or_nonbundled_source")
            self.assertEqual(candidate_c["fallback_reason"], "mutable_or_external_source")

    def test_stat_only_is_not_an_exact_count_contract(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            db_path, manifest, _archive, env = self._fixture(Path(raw_dir))
            result = benchmark.run_candidate("D_stat_only", db_path, manifest, env)

            self.assertFalse(result["semantic_exact"])
            self.assertTrue(result["core_tables"]["competency_units"]["has_rows"])
            self.assertIsNone(result["core_tables"]["competency_units"]["row_count"])

    def test_verified_snapshot_context_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            _db, manifest, archive, _env = self._fixture(root)
            extracted_workspace = None
            with benchmark.prepare_verified_snapshot(archive, manifest, root) as (db_path, _payload, workspace):
                extracted_workspace = workspace
                self.assertTrue(db_path.exists())
                self.assertTrue(workspace.exists())
            self.assertIsNotNone(extracted_workspace)
            self.assertFalse(extracted_workspace.exists())

    def test_latency_summary_has_p50_p95_cv(self) -> None:
        summary = benchmark.latency_summary([1, 2, 3, 4, 5])
        self.assertEqual(summary["p50"], 3.0)
        self.assertEqual(summary["p95"], 5.0)
        self.assertGreater(summary["coefficient_of_variation"], 0)


if __name__ == "__main__":
    unittest.main()
