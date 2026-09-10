from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.builder_gold import (  # noqa: E402
    GOLD_EXPORT_NAME,
    GOLD_EMBEDDING_EXPORT_NAME,
    GOLD_EMBEDDING_LOAD_REPORT_NAME,
    GOLD_EMBEDDING_REPORT_NAME,
    GOLD_REPORT_NAME,
    load_internal_role_mapping_packet,
    prepare_builder_gold_embeddings,
    sync_builder_gold_embeddings,
)
from ncs_mcp.data_builder import BuilderError, DataBuilder  # noqa: E402


class BuilderGoldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = DataBuilder(self.root)
        self.version = "20260910_010101_deadbeef"
        self.folder = self.engine._version_dir(self.version)
        self.folder.mkdir(parents=True)
        source = ROOT / "data" / "processed" / "ncs.db"
        if not source.is_file():
            self.skipTest("prepared project database is unavailable")
        # Candidate verification is isolated from the multi-GB real DB.  The
        # bridge functions are mocked below; only Builder binding/gating is in scope.
        (self.folder / "ncs.db").write_bytes(b"fixture")
        (self.folder / "build.json").write_text(
            json.dumps(
                {
                    "schema": "ncs_data_builder_version_v1",
                    "version": self.version,
                    "status": "ready",
                    "sha256": "fixture-sha",
                }
            ),
            encoding="utf-8",
        )

    def _candidate(self, _version):
        return self.folder / "ncs.db"

    def test_prepare_is_explicit_and_binds_builder_version(self):
        expected = {"status": "ready", "builder_version": self.version}
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.prepare_builder_gold", return_value=expected
            ) as prepare,
        ):
            result = self.engine.prepare_gold(self.version, batch_size=17)
        self.assertEqual(result, expected)
        kwargs = prepare.call_args.kwargs
        self.assertEqual(kwargs["source_db_sha256"], "fixture-sha")
        self.assertEqual(kwargs["batch_size"], 17)
        self.assertFalse((self.folder / "gold" / GOLD_EXPORT_NAME).exists())

    def test_prepare_accepts_mapper_packet_as_builder_overlay(self):
        from ncs_mcp.internal_job_roles import InternalJobRole, RoleAlignmentCandidate

        role = InternalJobRole(
            organization_namespace="example",
            role_id="hr-planner",
            display_name="인사기획",
            duties=("인력운영계획 수립",),
        )
        candidate = RoleAlignmentCandidate(
            role_gold_id=role.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key="02020201",
            score=0.6,
            method="fixture",
            status="candidate",
        )
        packet = self.root / "roles.json"
        packet.write_text(
            json.dumps(
                {
                    "schema": "ncs_internal_role_mapping_packet_v1",
                    "role_results": [
                        {
                            "role": role.to_public_dict(),
                            "alignment_candidates": [candidate.to_public_dict()],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        roles, candidates = load_internal_role_mapping_packet(packet)
        self.assertEqual(roles[0].gold_id, role.gold_id)
        self.assertEqual(candidates[0].ncs_target_key, "02020201")

        expected = {"status": "ready", "builder_version": self.version}
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.prepare_builder_gold", return_value=expected
            ) as prepare,
        ):
            result = self.engine.prepare_gold(self.version, role_mapping_packet=packet)
        self.assertEqual(result, expected)
        self.assertEqual(
            prepare.call_args.kwargs["internal_roles"][0].gold_id, role.gold_id
        )
        self.assertEqual(
            prepare.call_args.kwargs["role_alignments"][0].ncs_target_key,
            "02020201",
        )

    def test_sync_defaults_to_dry_run(self):
        expected = {"status": "validated_dry_run"}
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.sync_builder_gold", return_value=expected
            ) as sync,
        ):
            result = self.engine.sync_gold(self.version)
        self.assertEqual(result, expected)
        self.assertFalse(sync.call_args.kwargs["apply"])
        self.assertFalse(sync.call_args.kwargs["reconcile"])

    def test_gold_failure_does_not_mark_sqlite_candidate_failed(self):
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.prepare_builder_gold",
                side_effect=RuntimeError("backend secret"),
            ),
        ):
            with self.assertRaisesRegex(BuilderError, "RuntimeError") as ctx:
                self.engine.prepare_gold(self.version)
        self.assertNotIn("backend secret", str(ctx.exception))
        build = json.loads((self.folder / "build.json").read_text(encoding="utf-8"))
        self.assertEqual(build["status"], "ready")

    def test_prepare_embeddings_is_source_bound_and_smoke_bounded_by_default(self):
        expected = {"status": "ready_smoke", "builder_version": self.version}
        provider = object()
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.prepare_builder_gold_embeddings",
                return_value=expected,
            ) as prepare,
        ):
            result = self.engine.prepare_gold_embeddings(
                self.version,
                provider=provider,
            )
        self.assertEqual(result, expected)
        kwargs = prepare.call_args.kwargs
        self.assertIs(kwargs["provider"], provider)
        self.assertEqual(kwargs["source_db_sha256"], "fixture-sha")
        self.assertEqual(kwargs["max_records"], 20)
        self.assertFalse((self.folder / "gold" / GOLD_EMBEDDING_EXPORT_NAME).exists())

    def test_prepare_full_embedding_shards_is_a_distinct_uncapped_entrypoint(self):
        expected = {"status": "ready_full_resumable", "builder_version": self.version}
        provider = object()
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.prepare_builder_gold_embedding_shards",
                return_value=expected,
            ) as prepare,
        ):
            result = self.engine.prepare_gold_embedding_shards(
                self.version, provider=provider, shard_size=7
            )
        self.assertEqual(result, expected)
        kwargs = prepare.call_args.kwargs
        self.assertIs(kwargs["provider"], provider)
        self.assertEqual(kwargs["source_db_sha256"], "fixture-sha")
        self.assertEqual(kwargs["shard_size"], 7)
        self.assertNotIn("max_records", kwargs)

    def test_prepare_full_embedding_shards_builds_local_provider_for_builder_ui(self):
        expected = {"status": "ready_full_resumable", "builder_version": self.version}
        provider = object()
        with patch.object(self.engine, "candidate", side_effect=self._candidate), patch(
            "ncs_mcp.local_embeddings.SentenceTransformerEmbeddingProvider",
            return_value=provider,
        ) as provider_factory, patch(
            "ncs_mcp.builder_gold.prepare_builder_gold_embedding_shards",
            return_value=expected,
        ) as prepare:
            result = self.engine.prepare_gold_embedding_shards(
                self.version,
                model="cached/model",
                dimensions=1024,
                device="cuda",
                shard_size=10_000,
            )

        self.assertEqual(result, expected)
        provider_factory.assert_called_once_with(
            "cached/model",
            dimensions=1024,
            device="cuda",
            local_files_only=True,
        )
        self.assertIs(prepare.call_args.kwargs["provider"], provider)
        self.assertNotIn("max_records", prepare.call_args.kwargs)

    def test_sync_full_embedding_shards_defaults_to_dry_run(self):
        expected = {"status": "validated_dry_run"}
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.sync_builder_gold_embedding_shards",
                return_value=expected,
            ) as sync,
        ):
            result = self.engine.sync_gold_embedding_shards(self.version)
        self.assertEqual(result, expected)
        kwargs = sync.call_args.kwargs
        self.assertFalse(kwargs["apply"])
        self.assertTrue(kwargs["create_indexes"])
        self.assertEqual(kwargs["source_db_sha256"], "fixture-sha")

    def test_embedding_sync_defaults_to_dry_run_with_indexes(self):
        expected = {"status": "validated_dry_run"}
        with (
            patch.object(self.engine, "candidate", side_effect=self._candidate),
            patch(
                "ncs_mcp.builder_gold.sync_builder_gold_embeddings",
                return_value=expected,
            ) as sync,
        ):
            result = self.engine.sync_gold_embeddings(self.version)
        self.assertEqual(result, expected)
        self.assertFalse(sync.call_args.kwargs["apply"])
        self.assertTrue(sync.call_args.kwargs["create_indexes"])

    def _embedding_manifest(self):
        return {
            "schema": "ncs_gold_embedding_patch_ndjson_v1",
            "records_before_manifest": 3,
            "records_sha256": "records-sha",
            "patch_count": 2,
            "patch_counts": {"PerformanceCriterion": 2},
            "provider": "fixture-provider",
            "model": "fixture-model",
            "dimensions": 3,
            "max_observed_batch_size": 2,
            "limited_by_max_records": True,
            "semantic_text_included": False,
            "read_only": True,
            "db_writes": False,
            "neo4j_writes": False,
            "approval_claim": False,
            "status_update_allowed": False,
        }

    def test_embedding_prepare_writes_source_bound_report_without_text(self):
        gold = self.folder / "gold"
        gold.mkdir()
        (gold / GOLD_REPORT_NAME).write_text(
            json.dumps(
                {
                    "status": "ready",
                    "builder_version": self.version,
                    "source_db_sha256": "fixture-sha",
                    "records_sha256": "gold-records-sha",
                }
            ),
            encoding="utf-8",
        )
        manifest = self._embedding_manifest()

        def fake_export(_db, destination, _provider, **_kwargs):
            Path(destination).write_text("fixture\n", encoding="utf-8")
            return manifest

        with (
            patch(
                "ncs_mcp.builder_gold.export_gold_embedding_patches",
                side_effect=fake_export,
            ),
            patch(
                "ncs_mcp.builder_gold.inspect_gold_embedding_patches",
                return_value={"manifest": manifest},
            ),
            patch("ncs_mcp.builder_gold.file_sha256", return_value="artifact-sha"),
        ):
            result = prepare_builder_gold_embeddings(
                builder_version=self.version,
                version_dir=self.folder,
                db_path=self.folder / "ncs.db",
                source_db_sha256="fixture-sha",
                provider=object(),
                max_records=20,
            )
        self.assertEqual(result["status"], "ready_smoke")
        self.assertFalse(result["semantic_text_included"])
        self.assertFalse(result["source_db_writes"])
        self.assertTrue((gold / GOLD_EMBEDDING_REPORT_NAME).is_file())

    def test_embedding_apply_requires_reconciled_graph(self):
        gold = self.folder / "gold"
        gold.mkdir()
        artifact = gold / GOLD_EMBEDDING_EXPORT_NAME
        artifact.write_text("fixture\n", encoding="utf-8")
        manifest = self._embedding_manifest()
        (gold / GOLD_EMBEDDING_REPORT_NAME).write_text(
            json.dumps(
                {
                    "status": "ready_smoke",
                    "builder_version": self.version,
                    "source_db_sha256": "fixture-sha",
                    "embedding_export_sha256": "artifact-sha",
                    "patch_count": 2,
                    "provider": "fixture-provider",
                    "model": "fixture-model",
                    "dimensions": 3,
                }
            ),
            encoding="utf-8",
        )
        with (
            patch(
                "ncs_mcp.builder_gold.inspect_gold_embedding_patches",
                return_value={"manifest": manifest},
            ),
            patch("ncs_mcp.builder_gold.file_sha256", return_value="artifact-sha"),
        ):
            with self.assertRaisesRegex(ValueError, "reconciled Gold graph"):
                sync_builder_gold_embeddings(
                    builder_version=self.version,
                    version_dir=self.folder,
                    source_db_sha256="fixture-sha",
                    apply=True,
                )
        failure = json.loads(
            (gold / GOLD_EMBEDDING_LOAD_REPORT_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(failure["status"], "failed")
        self.assertFalse(failure["source_db_writes"])


if __name__ == "__main__":
    unittest.main()
