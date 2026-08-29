from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import build_vercel_snapshot as builder


class BuildVercelSnapshotTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "canonical-ncs.db"
        source.write_bytes(builder.SQLITE_HEADER + b"canonical")
        return source

    def _paths(self, root: Path) -> tuple[Path, Path, Path, Path]:
        return (
            root / "snapshot.db",
            root / "snapshot.zip",
            root / "snapshot.manifest.json",
            root / "build-report.json",
        )

    def test_success_executes_fixed_stages_and_records_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output_db, archive, manifest, report_path = self._paths(root)
            calls: list[list[str]] = []

            def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
                calls.append(argv)
                script_name = Path(argv[1]).name
                if script_name == "export_interview_serving_db.py":
                    output_db.write_bytes(builder.SQLITE_HEADER + b"snapshot")
                elif script_name == "package_vercel_compact_snapshot.py":
                    archive.write_bytes(b"archive")
                    manifest.write_bytes(b"{}")
                return subprocess.CompletedProcess(argv, 0)

            with patch.object(builder.subprocess, "run", side_effect=fake_run):
                result = builder.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0][0], builder.sys.executable)
            self.assertIn("vercel-ontology-compact", calls[0])
            self.assertIn("--skip-function-bundle-check", calls[2])
            self.assertEqual(result["artifacts"]["database"]["bytes"], output_db.stat().st_size)
            self.assertRegex(result["artifacts"]["archive"]["sha256"], r"^sha256:[0-9a-f]{64}$")
            self.assertTrue(result["source"]["sqlite_header_valid"])

    def test_failure_stops_before_later_stages(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output_db, archive, manifest, report_path = self._paths(root)
            with patch.object(
                builder.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 23),
            ) as run:
                result = builder.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["stage"], "export_compact_snapshot")
            self.assertEqual(run.call_count, 1)
            self.assertFalse(output_db.exists())
            self.assertFalse(archive.exists())

    def test_dry_run_does_not_execute_or_write(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output_db, archive, manifest, report_path = self._paths(root)
            with patch.object(builder.subprocess, "run") as run:
                result = builder.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                    dry_run=True,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["dry_run"])
            self.assertEqual(len(result["stages"]), 3)
            self.assertEqual(result["stages"][0]["argv"][0], builder.sys.executable)
            run.assert_not_called()
            self.assertFalse(output_db.exists())
            self.assertFalse(report_path.exists())

    def test_rejects_existing_or_colliding_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output_db, archive, manifest, report_path = self._paths(root)
            archive.write_bytes(b"existing")
            with self.assertRaisesRegex(builder.SnapshotBuildError, "refusing to replace"):
                builder.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                    dry_run=True,
                )
            with self.assertRaisesRegex(builder.SnapshotBuildError, "distinct"):
                builder.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=manifest,
                    manifest=manifest,
                    report_path=report_path,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
