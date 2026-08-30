from __future__ import annotations

import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from api import bootstrap_runtime
from ncs_mcp import runtime_readiness


class VercelReadinessFastPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved_env = {
            name: os.environ.get(name)
            for name in (
                runtime_readiness.READINESS_EXTRA_TABLES_ENV,
                runtime_readiness.READINESS_MIN_ROWS_ENV,
                "NCS_DB_PATH",
            )
        }
        os.environ.pop(runtime_readiness.READINESS_EXTRA_TABLES_ENV, None)
        os.environ.pop(runtime_readiness.READINESS_MIN_ROWS_ENV, None)
        runtime_readiness.clear_verified_readiness_counts()
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        runtime_readiness.clear_verified_readiness_counts()
        for name, value in self._saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._temp.cleanup()

    @property
    def all_tables(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                runtime_readiness.READINESS_CORE_TABLES
                + runtime_readiness.READINESS_PUBLIC_TOOL_TABLES
            )
        )

    def make_db(self, name: str = "snapshot.db") -> Path:
        db_path = self.root / name
        conn = sqlite3.connect(db_path)
        try:
            for table_name in self.all_tables:
                conn.execute(f'CREATE TABLE "{table_name}" (value INTEGER NOT NULL)')
                conn.execute(f'INSERT INTO "{table_name}" VALUES (1)')
            conn.commit()
        finally:
            conn.close()
        return db_path

    def configure(
        self,
        db_path: Path,
        *,
        counts: dict[str, int] | None = None,
        minima: dict[str, int] | None = None,
    ) -> bool:
        return runtime_readiness.configure_verified_readiness_counts(
            db_path,
            sqlite_sha256="a" * 64,
            sqlite_bytes=db_path.stat().st_size,
            table_counts=counts or {table_name: 1 for table_name in self.all_tables},
            required_tables=runtime_readiness.READINESS_CORE_TABLES,
            minimum_rows=minima or {},
        )

    @staticmethod
    def contract_signature(payload: dict[str, object]) -> dict[str, object]:
        return {
            key: payload.get(key)
            for key in (
                "configured",
                "exists",
                "openable",
                "ready",
                "core_ready",
                "public_tools_ready",
                "required_tables",
                "core_tables",
                "public_tool_tables",
                "capabilities",
                "degraded_capabilities",
            )
        }

    def test_valid_verified_counts_skip_sql_and_preserve_contract(self) -> None:
        db_path = self.make_db()
        baseline = runtime_readiness.database_readiness_metadata(db_path)
        self.assertTrue(self.configure(db_path))
        original_connect = sqlite3.connect
        with mock.patch.object(
            runtime_readiness.sqlite3, "connect", wraps=original_connect
        ) as connect_spy:
            promoted = runtime_readiness.database_readiness_metadata(db_path)
        connect_spy.assert_not_called()
        self.assertEqual(
            "verified_snapshot_metadata", promoted["readiness_count_source"]
        )
        self.assertEqual(self.contract_signature(baseline), self.contract_signature(promoted))

    def test_missing_counts_and_minimum_violation_reject_fast_path(self) -> None:
        db_path = self.make_db()
        counts = {table_name: 1 for table_name in self.all_tables}
        counts.pop(runtime_readiness.READINESS_CORE_TABLES[0])
        self.assertFalse(self.configure(db_path, counts=counts))
        self.assertEqual(
            "sql_count",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )

        os.environ[runtime_readiness.READINESS_MIN_ROWS_ENV] = '{"ksa_items":2}'
        counts = {table_name: 1 for table_name in self.all_tables}
        self.assertFalse(self.configure(db_path, counts=counts, minima={"ksa_items": 2}))
        fallback = runtime_readiness.database_readiness_metadata(db_path)
        self.assertEqual("sql_count", fallback["readiness_count_source"])
        self.assertFalse(fallback["ready"])
        self.assertEqual("database_not_ready", fallback["error"]["code"])
        self.assertEqual(1, fallback["core_tables"]["ksa_items"]["row_count"])

    def test_override_path_and_required_contract_mismatch_fall_back(self) -> None:
        bundled = self.make_db("bundled.db")
        override = self.make_db("override.db")
        self.assertTrue(self.configure(bundled))
        self.assertEqual(
            "sql_count",
            runtime_readiness.database_readiness_metadata(override)["readiness_count_source"],
        )

        self.assertTrue(self.configure(bundled))
        os.environ[runtime_readiness.READINESS_EXTRA_TABLES_ENV] = "extra_required"
        payload = runtime_readiness.database_readiness_metadata(bundled)
        self.assertEqual("sql_count", payload["readiness_count_source"])
        self.assertFalse(payload["ready"])

    def test_file_replacement_invalidates_fingerprint_and_uses_sql(self) -> None:
        db_path = self.make_db()
        self.assertTrue(self.configure(db_path))
        db_path.unlink()
        self.make_db()
        payload = runtime_readiness.database_readiness_metadata(db_path)
        self.assertEqual("sql_count", payload["readiness_count_source"])
        self.assertTrue(payload["ready"])

    def test_runtime_minimum_contract_change_invalidates_state(self) -> None:
        db_path = self.make_db()
        self.assertTrue(self.configure(db_path))
        os.environ[runtime_readiness.READINESS_MIN_ROWS_ENV] = '{"ksa_items":1}'
        payload = runtime_readiness.database_readiness_metadata(db_path)
        self.assertEqual("sql_count", payload["readiness_count_source"])

    def test_clear_and_explicit_invalidate_prevent_state_leaks(self) -> None:
        db_path = self.make_db()
        self.assertTrue(self.configure(db_path))
        runtime_readiness.clear_verified_readiness_counts()
        self.assertEqual(
            "sql_count",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )

        self.assertTrue(self.configure(db_path))
        self.assertFalse(
            runtime_readiness.invalidate_verified_readiness_counts(self.root / "other.db")
        )
        self.assertEqual(
            "verified_snapshot_metadata",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )
        self.assertTrue(runtime_readiness.invalidate_verified_readiness_counts(db_path))
        self.assertEqual(
            "sql_count",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )

    def test_local_bootstrap_configures_and_failure_clears_fast_path(self) -> None:
        db_path = self.make_db()
        manifest = {
            "sqlite_sha256": "b" * 64,
            "sqlite_bytes": db_path.stat().st_size,
            "logical_counts": {},
            "physical_counts": {table_name: 1 for table_name in self.all_tables},
            "servable_counts": {},
        }
        with mock.patch.object(bootstrap_runtime, "COMPACT_SNAPSHOT_NAME", str(db_path)), mock.patch.object(
            bootstrap_runtime, "materialize_compact_snapshot", return_value=True
        ), mock.patch.object(bootstrap_runtime, "load_compact_manifest", return_value=manifest):
            ready, metrics = bootstrap_runtime._bootstrap_db_from_local_snapshot(
                required_tables=runtime_readiness.READINESS_CORE_TABLES,
                minimum_rows={},
            )
        self.assertTrue(ready)
        self.assertTrue(metrics["readiness_fast_path_configured"])
        self.assertEqual(
            "verified_snapshot_metadata",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )

        with mock.patch.object(bootstrap_runtime, "COMPACT_SNAPSHOT_NAME", str(db_path)), mock.patch.object(
            bootstrap_runtime, "materialize_compact_snapshot", return_value=False
        ):
            ready, metrics = bootstrap_runtime._bootstrap_db_from_local_snapshot(
                required_tables=runtime_readiness.READINESS_CORE_TABLES,
                minimum_rows={},
            )
        self.assertFalse(ready)
        self.assertFalse(metrics["readiness_fast_path_configured"])
        self.assertEqual(
            "sql_count",
            runtime_readiness.database_readiness_metadata(db_path)["readiness_count_source"],
        )

    def test_logical_only_required_count_is_rejected_for_sql_fallback(self) -> None:
        logical_only = "logical_only_required"
        os.environ[runtime_readiness.READINESS_EXTRA_TABLES_ENV] = logical_only
        db_path = self.make_db()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f'CREATE TABLE "{logical_only}" (value INTEGER NOT NULL)')
            conn.execute(f'INSERT INTO "{logical_only}" VALUES (1)')
            conn.commit()
        finally:
            conn.close()
        required = runtime_readiness.READINESS_CORE_TABLES + (logical_only,)
        manifest = {
            "sqlite_sha256": "c" * 64,
            "sqlite_bytes": db_path.stat().st_size,
            "logical_counts": {logical_only: 1},
            "physical_counts": {table_name: 1 for table_name in self.all_tables},
            "servable_counts": {},
        }
        with mock.patch.object(
            bootstrap_runtime, "COMPACT_SNAPSHOT_NAME", str(db_path)
        ), mock.patch.object(
            bootstrap_runtime, "materialize_compact_snapshot", return_value=True
        ), mock.patch.object(
            bootstrap_runtime, "load_compact_manifest", return_value=manifest
        ):
            ready, metrics = bootstrap_runtime._bootstrap_db_from_local_snapshot(
                required_tables=required,
                minimum_rows={},
            )
        self.assertTrue(ready)
        self.assertFalse(metrics["readiness_fast_path_configured"])
        fallback = runtime_readiness.database_readiness_metadata(db_path)
        self.assertEqual("sql_count", fallback["readiness_count_source"])
        self.assertTrue(fallback["ready"])
        self.assertEqual(1, fallback["core_tables"][logical_only]["row_count"])

    def test_checked_in_required_25_have_physical_or_servable_provenance(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        deploy_root = project_root / "deploy" / "vercel_mcp_app"
        config = json.loads((deploy_root / "vercel.json").read_text(encoding="utf-8"))
        environment = {
            str(key): str(value) for key, value in dict(config.get("env") or {}).items()
        }
        required = bootstrap_runtime.readiness_required_tables(environment)
        manifest = bootstrap_runtime.load_compact_manifest(
            deploy_root / "api" / "ncs_ontology_compact.manifest.json"
        )
        trusted_names = set(manifest["physical_counts"]) | set(
            manifest["servable_counts"]
        )
        self.assertEqual(25, len(required))
        self.assertEqual([], [name for name in required if name not in trusted_names])

    def test_root_and_deploy_mirrors_are_identical(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        pairs = (
            (
                project_root / "src" / "ncs_mcp" / "runtime_readiness.py",
                project_root / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / "runtime_readiness.py",
            ),
            (
                project_root / "api" / "bootstrap_runtime.py",
                project_root / "deploy" / "vercel_mcp_app" / "api" / "bootstrap_runtime.py",
            ),
            (
                project_root / "api" / "mcp.py",
                project_root / "deploy" / "vercel_mcp_app" / "api" / "mcp.py",
            ),
        )
        for root_path, deploy_path in pairs:
            self.assertEqual(root_path.read_bytes(), deploy_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
