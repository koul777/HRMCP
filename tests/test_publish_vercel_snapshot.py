from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_success_replaces_verified_pair_and_records_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")

            with patch.object(
                publisher.builder, "build_snapshot", side_effect=self._successful_build
            ):
                result = publisher.publish_snapshot(
                    source=source,
                    deploy_root=deploy_root,
                )

            self.assertTrue(result["ok"])
            self.assertTrue(result["old_artifacts_replaced"])
            self.assertEqual(archive.read_bytes(), b"verified-new-archive")
            self.assertEqual(manifest.read_bytes(), b'{"verified": true}\n')
            self.assertEqual(
                result["source"]["sha256"], builder._source_artifact(source)["sha256"]
            )
            self.assertEqual(
                result["published_artifacts"]["archive"]["bytes"], archive.stat().st_size
            )
            self.assertTrue(result["policy"]["stage_verified_before_publish"])
            self.assertTrue(result["policy"]["source_hash_rechecked_after_build"])
            self.assertFalse(result["policy"]["source_database_mutated"])
            self.assertFalse(result["policy"]["vercel_deployment_performed"])

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

    def test_build_failure_leaves_old_artifacts_intact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            failed_build = {
                "ok": False,
                "dry_run": False,
                "source": builder._source_artifact(source),
                "stages": [
                    {"name": "export_compact_snapshot", "returncode": 19}
                ],
                "artifacts": {},
                "error": {"stage": "export_compact_snapshot"},
            }

            with patch.object(
                publisher.builder, "build_snapshot", return_value=failed_build
            ):
                result = publisher.publish_snapshot(
                    source=source,
                    deploy_root=deploy_root,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["stage"], "build")
            self.assertFalse(result["publication"]["attempted"])
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")

    def test_publish_failure_rolls_back_complete_old_pair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = self._source(root)
            deploy_root, archive, manifest = self._deploy_pair(root)
            archive.write_bytes(b"old-archive")
            manifest.write_bytes(b"old-manifest")
            real_replace = os.replace

            def fail_manifest_commit(source_path: object, target_path: object) -> None:
                source_candidate = Path(source_path)
                target_candidate = Path(target_path)
                if (
                    target_candidate == manifest
                    and ".incoming." in source_candidate.name
                ):
                    raise OSError("injected manifest publication failure")
                real_replace(source_candidate, target_candidate)

            with (
                patch.object(
                    publisher.builder, "build_snapshot", side_effect=self._successful_build
                ),
                patch.object(publisher.os, "replace", side_effect=fail_manifest_commit),
            ):
                result = publisher.publish_snapshot(
                    source=source,
                    deploy_root=deploy_root,
                )

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["stage"], "publication")
            self.assertTrue(result["publication"]["rollback_performed"])
            self.assertTrue(result["publication"]["rollback_ok"])
            self.assertEqual(archive.read_bytes(), b"old-archive")
            self.assertEqual(manifest.read_bytes(), b"old-manifest")
            self.assertEqual(
                sorted(path.name for path in archive.parent.iterdir()),
                sorted([publisher.ARCHIVE_NAME, publisher.MANIFEST_NAME]),
            )

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


if __name__ == "__main__":
    unittest.main()
