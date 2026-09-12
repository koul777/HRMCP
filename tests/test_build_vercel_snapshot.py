from __future__ import annotations

import subprocess
import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import build_vercel_snapshot as builder
from ncs_mcp.data_builder import DataBuilder
from ncs_mcp.builder_authorization import BuilderAuthorizationError


class BuildVercelSnapshotTests(unittest.TestCase):
    def test_direct_mutating_snapshot_export_and_package_are_denied_before_io(self):
        from scripts.export_interview_serving_db import export_serving_db
        from scripts.package_vercel_compact_snapshot import package_compact_snapshot
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / 'source.db'
            source.write_bytes(builder.SQLITE_HEADER + b'preserved')
            before = source.read_bytes()
            with patch.object(builder.subprocess, 'run') as run:
                with self.assertRaises(BuilderAuthorizationError):
                    builder.build_snapshot(source=source, output_db=root / 'compact.db',
                        archive=root / 'compact.zip', manifest=root / 'manifest.json',
                        report_path=root / 'report.json')
                with self.assertRaises(BuilderAuthorizationError):
                    export_serving_db(source, root / 'compact.db', profile='vercel-ontology-compact')
                with self.assertRaises(BuilderAuthorizationError):
                    package_compact_snapshot(source, root / 'compact.zip', root / 'manifest.json')
                run.assert_not_called()
            self.assertEqual(list(root.iterdir()), [source])
            self.assertEqual(source.read_bytes(), before)

    def test_lower_mutators_reject_serialized_copied_and_expired_contexts(self):
        from scripts.export_interview_serving_db import export_serving_db
        from scripts.package_vercel_compact_snapshot import package_compact_snapshot
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output, archive, manifest, report = self._paths(root)
            def check(context):
                calls = (
                    lambda: builder.build_snapshot(source=source, output_db=output, archive=archive,
                        manifest=manifest, report_path=report, builder_context=context),
                    lambda: export_serving_db(source, output, profile='vercel-ontology-compact',
                                              builder_context=context),
                    lambda: package_compact_snapshot(source, archive, manifest, builder_context=context),
                )
                for call in calls:
                    with self.assertRaises(BuilderAuthorizationError):
                        call()
            with DataBuilder(root).exclusive('package', 'a1') as context:
                check(context.lineage())
                check(copy.copy(context))
            check(context)
            self.assertFalse(output.exists())
            self.assertFalse(archive.exists())
            self.assertFalse(manifest.exists())
            self.assertFalse(report.exists())

    def _source(self, root: Path) -> Path:
        source = root / '.state/ncs-data-builder/versions/a1/ncs.db'
        source.parent.mkdir(parents=True)
        source.write_bytes(builder.SQLITE_HEADER + b"canonical")
        return source

    def _paths(self, root: Path) -> tuple[Path, Path, Path, Path]:
        root = root / '.state/ncs-data-builder/versions/a1/release'
        root.mkdir(parents=True)
        return (
            root / "snapshot.db",
            root / "snapshot.zip",
            root / "snapshot.manifest.json",
            root / "build-report.json",
        )

    def build_snapshot(self, **kwargs):
        if kwargs.get('dry_run'):
            return builder.build_snapshot(**kwargs)
        root = kwargs['source'].parent.parent.parent.parent.parent
        with DataBuilder(root).exclusive('package', 'a1') as context:
            return builder.build_snapshot(**kwargs, builder_context=context)

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

            def export(*args, **kwargs):
                argv = builder.build_plan(source, output_db, archive, manifest)[0]['argv']
                fake_run(argv)
                return {'ok': True}

            def package(*args, **kwargs):
                argv = builder.build_plan(source, output_db, archive, manifest)[1]['argv']
                fake_run(argv)
                return {'ok': True}

            with patch.object(builder.subprocess, "run", side_effect=fake_run), \
                    patch('scripts.export_interview_serving_db.export_serving_db', side_effect=export), \
                    patch('scripts.package_vercel_compact_snapshot.package_compact_snapshot', side_effect=package):
                result = self.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                    clock=lambda: datetime(2026, 9, 12, 1, 2, 3, tzinfo=timezone.utc),
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["generated_at"], "2026-09-12T01:02:03+00:00")
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
            with patch('scripts.export_interview_serving_db.export_serving_db',
                       side_effect=ValueError('bad source')) as run:
                result = self.build_snapshot(
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
                result = self.build_snapshot(
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

    def test_rejects_active_wal_before_stages_or_any_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            source.with_name(f"{source.name}-wal").write_bytes(b"uncheckpointed")
            output_db, archive, manifest, report_path = self._paths(root)
            progress = report_path.parent / "progress.json"

            with (
                patch.object(builder.subprocess, "run") as run,
                self.assertRaisesRegex(builder.SnapshotBuildError, "active transaction sidecar"),
            ):
                self.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                    progress_file=progress,
                )

            run.assert_not_called()
            for path in (output_db, archive, manifest, report_path, progress):
                self.assertFalse(path.exists(), path)

    def test_rejects_active_journal_before_hashing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            source.with_name(f"{source.name}-journal").write_bytes(b"active")

            with self.assertRaisesRegex(builder.SnapshotBuildError, "-journal"):
                builder._source_artifact(source)

    def test_rejects_existing_or_colliding_output_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            output_db, archive, manifest, report_path = self._paths(root)
            archive.write_bytes(b"existing")
            with self.assertRaisesRegex(builder.SnapshotBuildError, "refusing to replace"):
                self.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=archive,
                    manifest=manifest,
                    report_path=report_path,
                    dry_run=True,
                )
            with self.assertRaisesRegex(builder.SnapshotBuildError, "distinct"):
                self.build_snapshot(
                    source=source,
                    output_db=output_db,
                    archive=manifest,
                    manifest=manifest,
                    report_path=report_path,
                    dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()
