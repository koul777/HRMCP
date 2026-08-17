from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import prepare_serving_database as serving_snapshot


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_serving_database.py"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


class PrepareServingDatabaseTests(unittest.TestCase):
    def _create_active_wal_database(self, path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        self.assertEqual("wal", conn.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        conn.execute("PRAGMA wal_autocheckpoint=0")
        for table_name in serving_snapshot.READINESS_CORE_TABLES:
            conn.execute(
                f'CREATE TABLE "{table_name}" '
                "(id INTEGER PRIMARY KEY, label TEXT NOT NULL)"
            )
            conn.execute(
                f'INSERT INTO "{table_name}" (label) VALUES (?)',
                (f"checkpointed-{table_name}",),
            )
        conn.commit()
        self.assertEqual((0, 0, 0), conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())

        conn.execute(
            "INSERT INTO competency_units (label) VALUES (?)",
            ("committed-only-in-wal",),
        )
        conn.commit()
        self.assertTrue(Path(f"{path}-wal").exists())
        self.assertGreater(Path(f"{path}-wal").stat().st_size, 0)
        return conn

    def _run(
        self,
        *,
        source: Path,
        output: Path,
        json_out: Path,
        markdown_out: Path,
        quick_check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(SCRIPT),
            "--source-db",
            str(source),
            "--output-db",
            str(output),
            "--out",
            str(json_out),
            "--markdown-out",
            str(markdown_out),
        ]
        if quick_check:
            command.append("--quick-check")
        return subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )

    def test_snapshot_includes_uncheckpointed_commit_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            output = root / "serving.db"
            json_out = root / "snapshot.json"
            markdown_out = root / "snapshot.md"
            writer = self._create_active_wal_database(source)
            try:
                immutable = sqlite3.connect(
                    source.resolve().as_uri() + "?mode=ro&immutable=1",
                    uri=True,
                )
                try:
                    self.assertEqual(
                        1,
                        immutable.execute(
                            "SELECT COUNT(*) FROM competency_units"
                        ).fetchone()[0],
                    )
                finally:
                    immutable.close()

                source_before = {
                    "main": (source.stat().st_size, _sha256(source)),
                    "wal": (
                        Path(f"{source}-wal").stat().st_size,
                        _sha256(Path(f"{source}-wal")),
                    ),
                }
                completed = self._run(
                    source=source,
                    output=output,
                    json_out=json_out,
                    markdown_out=markdown_out,
                    quick_check=True,
                )
                self.assertEqual(0, completed.returncode, completed.stderr)

                source_after = {
                    "main": (source.stat().st_size, _sha256(source)),
                    "wal": (
                        Path(f"{source}-wal").stat().st_size,
                        _sha256(Path(f"{source}-wal")),
                    ),
                }
                self.assertEqual(source_before, source_after)
                self.assertEqual(
                    2,
                    writer.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0],
                )

                destination = sqlite3.connect(
                    output.resolve().as_uri() + "?mode=ro",
                    uri=True,
                )
                try:
                    self.assertEqual(
                        2,
                        destination.execute(
                            "SELECT COUNT(*) FROM competency_units"
                        ).fetchone()[0],
                    )
                    self.assertEqual(
                        "delete",
                        destination.execute("PRAGMA journal_mode").fetchone()[0],
                    )
                finally:
                    destination.close()

                for suffix in serving_snapshot.SQLITE_SIDECAR_SUFFIXES:
                    self.assertFalse(Path(f"{output}{suffix}").exists())

                report = json.loads(json_out.read_text(encoding="utf-8"))
                self.assertTrue(report["ok"])
                self.assertFalse(report["source_logical_writes"])
                self.assertTrue(report["destination_db_created"])
                self.assertFalse(report["report_only"])
                self.assertTrue(report["db_writes"])
                self.assertEqual(
                    report["db_write_scope"],
                    "new_serving_snapshot_only",
                )
                self.assertFalse(report["source_db_writes"])
                self.assertTrue(report["destination_db_writes"])
                self.assertTrue(report["storage_preflight"]["ok"])
                self.assertGreater(
                    report["storage_preflight"]["available_free_bytes_before"],
                    report["storage_preflight"]["required_free_bytes"],
                )
                self.assertFalse(report["status_update_allowed"])
                self.assertFalse(report["approval_claim"])
                self.assertFalse(report["external_api_calls"])
                self.assertFalse(report["human_review_status_writes"])
                self.assertTrue(report["source"]["main_and_wal_content_unchanged"])
                self.assertTrue(report["source"]["shm_observation"]["observation_only"])
                self.assertFalse(
                    report["source"]["shm_observation"][
                        "used_as_logical_write_evidence"
                    ]
                )
                self.assertEqual(
                    2,
                    report["validation"]["core_tables"]["tables"]
                    ["competency_units"]["destination_row_count"],
                )
                self.assertTrue(report["validation"]["core_tables"]["ready"])
                self.assertTrue(report["validation"]["quick_check"]["ok"])
                self.assertEqual(_sha256(output), report["destination"]["file"]["sha256"])
                self.assertEqual(
                    output.stat().st_size,
                    report["destination"]["file"]["size_bytes"],
                )
                self.assertEqual(
                    output.stat().st_mtime_ns,
                    report["destination"]["file"]["mtime_ns"],
                )
                markdown = markdown_out.read_text(encoding="utf-8")
                self.assertIn("source_logical_writes: `false`", markdown)
                self.assertIn("journal_mode_after_close: `delete`", markdown)
                self.assertIn("`competency_units` | 2 | 2 | `true`", markdown)
            finally:
                writer.close()

    def test_default_validation_uses_schema_counts_without_quick_check(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            output = root / "serving.db"
            json_out = root / "snapshot.json"
            writer = self._create_active_wal_database(source)
            try:
                completed = self._run(
                    source=source,
                    output=output,
                    json_out=json_out,
                    markdown_out=root / "snapshot.md",
                )
                self.assertEqual(0, completed.returncode, completed.stderr)
                report = json.loads(json_out.read_text(encoding="utf-8"))
                self.assertEqual(
                    "schema_and_counts",
                    report["validation"]["default_validation"],
                )
                self.assertTrue(
                    report["validation"]["schema_count_check_performed"]
                )
                self.assertFalse(report["validation"]["quick_check"]["requested"])
                self.assertIsNone(report["validation"]["quick_check"]["ok"])
                self.assertIsNone(report["validation"]["quick_check"]["rows"])
            finally:
                writer.close()

    def test_rejects_overwrite_without_changing_existing_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            output = root / "existing.db"
            writer = self._create_active_wal_database(source)
            try:
                output.write_bytes(b"do-not-overwrite")
                completed = self._run(
                    source=source,
                    output=output,
                    json_out=root / "snapshot.json",
                    markdown_out=root / "snapshot.md",
                )
                self.assertNotEqual(0, completed.returncode)
                self.assertIn("overwrite is not allowed", completed.stderr)
                self.assertEqual(b"do-not-overwrite", output.read_bytes())
            finally:
                writer.close()

    def test_rejects_source_sidecar_and_report_path_overlaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            writer = self._create_active_wal_database(source)
            try:
                cases = (
                    (
                        "source",
                        source,
                        root / "source-overlap.json",
                        root / "source-overlap.md",
                        "source database family",
                    ),
                    (
                        "source-sidecar",
                        Path(f"{source}-journal"),
                        root / "sidecar-overlap.json",
                        root / "sidecar-overlap.md",
                        "source database family",
                    ),
                    (
                        "json-report",
                        root / "same-path.db",
                        root / "same-path.db",
                        root / "same-path.md",
                        "report path",
                    ),
                )
                for label, output, json_out, markdown_out, expected in cases:
                    with self.subTest(label=label):
                        completed = self._run(
                            source=source,
                            output=output,
                            json_out=json_out,
                            markdown_out=markdown_out,
                        )
                        self.assertNotEqual(0, completed.returncode)
                        self.assertIn(expected, completed.stderr)
                        if output not in {source, Path(f"{source}-journal")}:
                            self.assertFalse(output.exists())
            finally:
                writer.close()

    def test_failed_validation_removes_only_new_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "incomplete.db"
            conn = sqlite3.connect(source)
            try:
                conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
                conn.commit()
            finally:
                conn.close()
            output = root / "partial.db"
            sentinel = root / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")

            completed = self._run(
                source=source,
                output=output,
                json_out=root / "failure.json",
                markdown_out=root / "failure.md",
            )

            self.assertNotEqual(0, completed.returncode)
            self.assertIn("core_table_schema_or_count_check_failed", completed.stderr)
            self.assertFalse(output.exists())
            for suffix in serving_snapshot.SQLITE_SIDECAR_SUFFIXES:
                self.assertFalse(Path(f"{output}{suffix}").exists())
            self.assertEqual("keep", sentinel.read_text(encoding="utf-8"))

    def test_source_content_change_fails_and_removes_destination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            writer = self._create_active_wal_database(source)
            output = root / "serving.db"
            unchanged = {"content_unchanged": True, "metadata_unchanged": True}
            changed = {"content_unchanged": False, "metadata_unchanged": False}
            try:
                with patch.object(
                    serving_snapshot,
                    "compare_file_content",
                    side_effect=[unchanged, changed, unchanged],
                ):
                    with self.assertRaisesRegex(
                        serving_snapshot.SnapshotPreparationError,
                        "source database or WAL content changed",
                    ):
                        serving_snapshot.prepare_serving_database(
                            source_db=source,
                            output_db=output,
                            json_out=root / "snapshot.json",
                            markdown_out=root / "snapshot.md",
                        )
            finally:
                writer.close()

            self.assertFalse(output.exists())
            for suffix in serving_snapshot.SQLITE_SIDECAR_SUFFIXES:
                self.assertFalse(Path(f"{output}{suffix}").exists())

    def test_insufficient_space_fails_before_destination_creation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "active.db"
            writer = self._create_active_wal_database(source)
            output = root / "serving.db"
            usage = type("DiskUsage", (), {"free": 0})()
            try:
                with patch.object(
                    serving_snapshot.shutil,
                    "disk_usage",
                    return_value=usage,
                ):
                    with self.assertRaisesRegex(
                        serving_snapshot.SnapshotPreparationError,
                        "insufficient free space",
                    ):
                        serving_snapshot.prepare_serving_database(
                            source_db=source,
                            output_db=output,
                            json_out=root / "snapshot.json",
                            markdown_out=root / "snapshot.md",
                        )
            finally:
                writer.close()

            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
