from __future__ import annotations

import os
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.builder_authorization import BuilderAuthorizationError
from ncs_mcp.data_builder import DataBuilder
from scripts import build_vercel_snapshot as builder
from scripts import publish_vercel_snapshot as publisher


class PublishVercelSnapshotTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "canonical-ncs.db"
        source.write_bytes(builder.SQLITE_HEADER + b"canonical-source")
        return source

    def _deploy_pair(self, root: Path) -> tuple[Path, Path, Path]:
        deploy_root = root / "deploy" / "vercel_mcp_app"
        api_dir = deploy_root / "api"
        api_dir.mkdir(parents=True)
        return (
            deploy_root,
            api_dir / publisher.ARCHIVE_NAME,
            api_dir / publisher.MANIFEST_NAME,
        )

    def _successful_build(self, **kwargs: object) -> dict[str, object]:
        source = Path(kwargs["source"])
        dry_run = bool(kwargs.get("dry_run"))
        source_record = builder._source_artifact(source)
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "generated_at": "2026-09-12T01:02:03+00:00",
                "source": source_record,
                "stages": [{"name": "verify_archive_only"}],
                "artifacts": {},
                "policy": {},
            }

        output_db = Path(kwargs["output_db"])
        archive = Path(kwargs["archive"])
        manifest = Path(kwargs["manifest"])
        output_db.write_bytes(builder.SQLITE_HEADER + b"compact")
        archive.write_bytes(b"verified-new-archive")
        manifest.write_bytes(b'{"verified": true}\n')
        return {
            "ok": True,
            "dry_run": False,
            "generated_at": "2026-09-12T01:02:03+00:00",
            "source": source_record,
            "stages": [
                {"name": "export_compact_snapshot", "returncode": 0},
                {"name": "package_compact_snapshot", "returncode": 0},
                {"name": "verify_archive_only", "returncode": 0},
            ],
            "artifacts": {
                "source": source_record,
                "database": publisher._artifact(output_db),
                "archive": publisher._artifact(archive),
                "manifest": publisher._artifact(manifest),
            },
            "policy": {
                "source_database_mutated": False,
                "api_collection_called": False,
                "human_review_statuses_changed": False,
                "deployment_performed": False,
            },
        }

    def test_non_dry_publication_is_retired_even_with_live_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            guard = DataBuilder(root)

            with (
                guard.exclusive("publish_snapshot") as builder_context,
                patch.object(publisher.builder, "build_snapshot") as build,
            ):
                with self.assertRaisesRegex(
                    publisher.LegacySnapshotPublisherRetired,
                    "version-bound DataBuilder",
                ):
                    publisher.publish_snapshot(
                        source=source,
                        deploy_root=deploy_root,
                        builder_context=builder_context,
                    )

            build.assert_not_called()
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")

    def test_dry_run_never_replaces_existing_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")

            with patch.object(
                publisher.builder, "build_snapshot", side_effect=self._successful_build
            ) as build:
                result = publisher.publish_snapshot(
                    source=source,
                    deploy_root=deploy_root,
                    dry_run=True,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["dry_run"])
            self.assertTrue(result["would_replace_old_artifacts"])
            self.assertFalse(result["old_artifacts_replaced"])
            self.assertFalse(result["publication"]["attempted"])
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")
            self.assertTrue(build.call_args.kwargs["dry_run"])

    def test_path_containment_rejects_escape_and_report_in_deploy_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, _, _ = self._deploy_pair(root)

            with self.assertRaisesRegex(
                publisher.SnapshotPublishError, "escapes deploy root"
            ):
                publisher._require_contained(
                    deploy_root,
                    deploy_root.parent / "outside" / publisher.ARCHIVE_NAME,
                    label="test target",
                )

            with self.assertRaisesRegex(
                publisher.SnapshotPublishError, "outside deploy root"
            ):
                publisher.publish_snapshot(
                    source=source,
                    deploy_root=deploy_root,
                    report_path=deploy_root / "publication-report.json",
                    dry_run=True,
                )

    def test_retired_mutation_rejects_every_context_before_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")

            for forged in (None, False, True, {}, {"action": "publish_snapshot"}):
                with self.subTest(forged=forged):
                    with patch.object(publisher.builder, "build_snapshot") as build:
                        with self.assertRaises(
                            publisher.LegacySnapshotPublisherRetired
                        ):
                            publisher.publish_snapshot(
                                source=source,
                                deploy_root=deploy_root,
                                builder_context=forged,  # type: ignore[arg-type]
                            )
                    build.assert_not_called()
                    self.assertEqual(archive.read_bytes(), b"old-archive")
                    self.assertEqual(manifest.read_bytes(), b"old-manifest")

    def test_retired_mutation_does_not_inspect_unrelated_builder_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, _, _ = self._deploy_pair(root)
            unrelated_builder = DataBuilder(root / "unrelated")

            with (
                unrelated_builder.exclusive("publish_snapshot") as builder_context,
                patch.object(publisher.builder, "build_snapshot") as build,
            ):
                with self.assertRaises(publisher.LegacySnapshotPublisherRetired):
                    publisher.publish_snapshot(
                        source=source,
                        deploy_root=deploy_root,
                        builder_context=builder_context,
                    )
            build.assert_not_called()

    def test_retired_mutation_never_reaches_pair_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            guard = DataBuilder(root)

            def build_then_expire(**kwargs: object) -> dict[str, object]:
                result = self._successful_build(**kwargs)
                lock = guard.state / "operation.lock"
                payload = json.loads(lock.read_text(encoding="utf-8"))
                payload["operation_id"] = "expired-before-publication"
                lock.write_text(json.dumps(payload), encoding="utf-8")
                return result

            with (
                guard.exclusive("publish_snapshot") as builder_context,
                patch.object(
                    publisher.builder,
                    "build_snapshot",
                    side_effect=build_then_expire,
                ),
            ):
                with self.assertRaises(publisher.LegacySnapshotPublisherRetired):
                    publisher.publish_snapshot(
                        source=source,
                        deploy_root=deploy_root,
                        builder_context=builder_context,
                    )

            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")

    def test_legacy_cli_mutation_is_blocked_with_explicit_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            stdout = io.StringIO()

            with (
                patch.object(publisher.builder, "build_snapshot") as build,
                redirect_stdout(stdout),
            ):
                return_code = publisher.main(
                    ["--source", str(source), "--deploy-root", str(deploy_root)]
                )

            self.assertEqual(return_code, 1)
            payload = json.loads(stdout.getvalue())
            self.assertEqual(
                payload["error"]["code"], "legacy_publisher_retired"
            )
            build.assert_not_called()
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")

    def test_report_destination_rejects_source_sidecar_and_source_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, _, _ = self._deploy_pair(root)
            sidecar_report = source.with_name(source.name + "-wal")

            with patch.object(publisher.builder, "build_snapshot") as build:
                with self.assertRaisesRegex(
                    publisher.SnapshotPublishError, "protected artifact"
                ):
                    publisher.publish_snapshot(
                        source=source,
                        deploy_root=deploy_root,
                        report_path=sidecar_report,
                        dry_run=True,
                    )
            build.assert_not_called()
            self.assertFalse(sidecar_report.exists())

            hardlink_report = root / "source-hardlink-report.json"
            os.link(source, hardlink_report)
            original = source.read_bytes()
            with self.assertRaisesRegex(
                publisher.SnapshotPublishError, "protected artifact"
            ):
                publisher._write_report(
                    hardlink_report,
                    {"source": publisher._artifact(source)},
                )
            self.assertEqual(source.read_bytes(), original)

    def test_second_staging_copy_failure_cleans_first_incoming_temp(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            staged_archive = root / "staged.zip"
            staged_manifest = root / "staged.json"
            staged_archive.write_bytes(b"new-archive")
            staged_manifest.write_bytes(b"new-manifest")
            _, expected_old = publisher._pair_state(archive, manifest)
            real_copyfile = publisher.shutil.copyfile
            copy_count = 0

            def fail_second_copy(source_path: object, target_path: object) -> object:
                nonlocal copy_count
                copy_count += 1
                if copy_count == 2:
                    raise OSError("injected second copy failure")
                return real_copyfile(source_path, target_path)

            guard = DataBuilder(root)
            with (
                guard.exclusive("publish_snapshot") as builder_context,
                patch.object(
                    publisher.shutil,
                    "copyfile",
                    side_effect=fail_second_copy,
                ),
            ):
                with self.assertRaisesRegex(
                    publisher.SnapshotPublishError, "second copy failure"
                ):
                    publisher._publish_pair(
                        staged_archive=staged_archive,
                        staged_manifest=staged_manifest,
                        target_archive=archive,
                        target_manifest=manifest,
                        expected_old=expected_old,
                        source=source,
                        deploy_root=deploy_root,
                        builder_context=builder_context,
                    )

            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")
            self.assertFalse(
                any(".incoming." in path.name for path in archive.parent.iterdir())
            )

    def test_expired_lease_after_copy_causes_zero_replace_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            staged_archive = root / "staged.zip"
            staged_manifest = root / "staged.json"
            staged_archive.write_bytes(b"new-archive")
            staged_manifest.write_bytes(b"new-manifest")
            _, expected_old = publisher._pair_state(archive, manifest)
            guard = DataBuilder(root)
            real_copy = publisher._copy_verified
            copy_count = 0

            def copy_then_expire(*args: object, **kwargs: object) -> Path:
                nonlocal copy_count
                result = real_copy(*args, **kwargs)
                copy_count += 1
                if copy_count == 2:
                    lock = guard.state / "operation.lock"
                    payload = json.loads(lock.read_text(encoding="utf-8"))
                    payload["operation_id"] = "expired-after-copy"
                    lock.write_text(json.dumps(payload), encoding="utf-8")
                return result

            with (
                guard.exclusive("publish_snapshot") as builder_context,
                patch.object(
                    publisher,
                    "_copy_verified",
                    side_effect=copy_then_expire,
                ),
                patch.object(publisher.os, "replace") as replace,
            ):
                with self.assertRaises(BuilderAuthorizationError):
                    publisher._publish_pair(
                        staged_archive=staged_archive,
                        staged_manifest=staged_manifest,
                        target_archive=archive,
                        target_manifest=manifest,
                        expected_old=expected_old,
                        source=source,
                        deploy_root=deploy_root,
                        builder_context=builder_context,
                    )

            replace.assert_not_called()
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")


if __name__ == "__main__":
    unittest.main()
