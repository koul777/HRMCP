from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.embedding_export import (  # noqa: E402
    EmbeddingPatchArtifactError,
    build_gold_embedding_shard_plan,
    export_gold_embedding_shards,
    export_gold_embedding_patches,
    inspect_gold_embedding_patches,
    inspect_gold_embedding_shard_manifest,
    iter_gold_embedding_patches,
)


class _FakeProvider:
    provider_name = "fixture_provider"
    model = "fixture-model-v1"
    dimensions = 3
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def embed_texts(self, texts):
        self.calls.append(tuple(texts))
        return tuple((float(index + 1), 0.0, -1.0) for index, _ in enumerate(texts))


class _BadProvider(_FakeProvider):
    def embed_texts(self, texts):
        return ((1.0, 2.0),) * len(texts)


class EmbeddingPatchExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "source.db"
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY,
                    element_name_raw TEXT NOT NULL,
                    element_name_refined TEXT
                );
                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    element_id INTEGER NOT NULL,
                    criteria_text_raw TEXT NOT NULL,
                    criteria_text_refined TEXT
                );
                CREATE TABLE ontology_concepts (
                    concept_id INTEGER PRIMARY KEY,
                    concept_name TEXT NOT NULL,
                    definition TEXT,
                    definition_source TEXT,
                    definition_status TEXT,
                    review_status TEXT
                );
                INSERT INTO competency_elements VALUES (1, 'element raw', 'Element refined');
                INSERT INTO performance_criteria VALUES (10, 1, 'criterion raw', 'Criterion refined');
                INSERT INTO ontology_concepts VALUES
                  (2, 'Concept', 'reviewed definition', 'operator_manual', 'defined', 'human_reviewed');
                """
            )
            conn.commit()
        finally:
            conn.close()

    def test_export_is_deterministic_atomic_and_text_free(self) -> None:
        first = self.root / "first.ndjson"
        second = self.root / "second.ndjson"
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        provider = _FakeProvider()
        manifest = export_gold_embedding_patches(
            self.db_path, first, provider, batch_size=2, fetch_size=1
        )
        export_gold_embedding_patches(
            self.db_path, second, _FakeProvider(), batch_size=2, fetch_size=1
        )
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(manifest["patch_count"], 3)
        self.assertFalse(manifest["semantic_text_included"])
        self.assertFalse(manifest["db_writes"])
        self.assertFalse(manifest["neo4j_writes"])
        self.assertLessEqual(manifest["max_observed_batch_size"], 2)
        inspected = inspect_gold_embedding_patches(first)
        self.assertEqual(inspected["manifest"], manifest)
        patches = list(iter_gold_embedding_patches(first))
        self.assertEqual(len(patches), 3)
        serialized = first.read_text(encoding="utf-8")
        self.assertNotIn("Criterion refined", serialized)
        self.assertNotIn("reviewed definition", serialized)
        self.assertNotIn('"text"', serialized)
        self.assertTrue(provider.calls)

    def test_max_records_is_bounded_and_marked(self) -> None:
        output = self.root / "limited.ndjson"
        manifest = export_gold_embedding_patches(
            self.db_path, output, _FakeProvider(), max_records=2, batch_size=2
        )
        self.assertEqual(manifest["patch_count"], 2)
        self.assertTrue(manifest["limited_by_max_records"])
        self.assertEqual(len(list(iter_gold_embedding_patches(output))), 2)

    def test_provider_failure_keeps_existing_destination_untouched(self) -> None:
        output = self.root / "existing.ndjson"
        output.write_bytes(b"existing-safe-artifact")
        with self.assertRaises(Exception):
            export_gold_embedding_patches(self.db_path, output, _BadProvider())
        self.assertEqual(output.read_bytes(), b"existing-safe-artifact")
        self.assertFalse(list(self.root.glob(".existing.ndjson.*.tmp")))

    def test_corrupt_or_incomplete_artifact_fails_closed(self) -> None:
        output = self.root / "artifact.ndjson"
        export_gold_embedding_patches(
            self.db_path, output, _FakeProvider(), max_records=1
        )
        tampered = self.root / "tampered.ndjson"
        tampered.write_bytes(
            output.read_bytes().replace(b'"fixture-model-v1"', b'"tampered-model-v"', 1)
        )
        with self.assertRaises(EmbeddingPatchArtifactError):
            inspect_gold_embedding_patches(tampered)
        incomplete = self.root / "incomplete.ndjson"
        incomplete.write_bytes(output.read_bytes().rsplit(b"\n", 2)[0])
        with self.assertRaises(EmbeddingPatchArtifactError):
            inspect_gold_embedding_patches(incomplete)

    def test_patch_reader_returns_raw_loader_compatible_objects(self) -> None:
        output = self.root / "loader.ndjson"
        export_gold_embedding_patches(
            self.db_path, output, _FakeProvider(), max_records=1
        )
        patch = next(iter_gold_embedding_patches(output))
        self.assertEqual(patch["schema"], "ncs_embedding_node_patch_v1")
        self.assertEqual(patch["dimensions"], 3)
        self.assertNotIn("text", patch)
        self.assertIsInstance(json.loads(json.dumps(patch)), dict)

    def test_full_shards_have_deterministic_keyset_plan_and_resume_safely(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        provider = _FakeProvider()
        plan = build_gold_embedding_shard_plan(
            self.db_path, provider=provider, gold_records_sha256="a" * 64, shard_size=1
        )
        second_plan = build_gold_embedding_shard_plan(
            self.db_path,
            provider=_FakeProvider(),
            gold_records_sha256="a" * 64,
            shard_size=1,
        )
        self.assertEqual(plan, second_plan)
        self.assertEqual(
            [item["entity_type"] for item in plan["shards"]],
            ["PerformanceCriterion", "PerformanceElement", "KSAConcept"],
        )
        root = self.root / "shards"
        manifest = export_gold_embedding_shards(
            self.db_path, root, provider, plan=plan, batch_size=1
        )
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertEqual(manifest["patch_count"], 3)
        self.assertEqual([item["index"] for item in manifest["shards"]], [1, 2, 3])
        calls = len(provider.calls)
        resumed = export_gold_embedding_shards(
            self.db_path, root, provider, plan=plan, batch_size=1
        )
        self.assertEqual(resumed, manifest)
        self.assertEqual(
            len(provider.calls), calls, "validated completed shards must be skipped"
        )
        # An interrupted/missing unit is the only one regenerated.
        (root / manifest["shards"][1]["path"]).unlink()
        export_gold_embedding_shards(
            self.db_path, root, provider, plan=plan, batch_size=1
        )
        self.assertEqual(len(provider.calls), calls + 1)

    def test_shard_manifest_fails_closed_for_source_config_and_tampering(self) -> None:
        plan = build_gold_embedding_shard_plan(
            self.db_path,
            provider=_FakeProvider(),
            gold_records_sha256="b" * 64,
            shard_size=2,
        )
        root = self.root / "shards"
        manifest = export_gold_embedding_shards(
            self.db_path, root, _FakeProvider(), plan=plan
        )
        manifest_path = root / "ncs_gold_embeddings.manifest.json"
        self.assertEqual(inspect_gold_embedding_shard_manifest(manifest_path), manifest)
        bad_provider = _FakeProvider()
        bad_provider.model = "different"
        with self.assertRaisesRegex(EmbeddingPatchArtifactError, "configuration"):
            export_gold_embedding_shards(self.db_path, root, bad_provider, plan=plan)
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE competency_elements SET element_name_raw='changed' WHERE element_id=1"
            )
            conn.commit()
        finally:
            conn.close()
        with self.assertRaisesRegex(
            EmbeddingPatchArtifactError, "source database changed"
        ):
            export_gold_embedding_shards(self.db_path, root, _FakeProvider(), plan=plan)
        # A completed-looking artifact with mismatched final digest is never accepted.
        shard = root / manifest["shards"][0]["path"]
        shard.write_bytes(
            shard.read_bytes().replace(b'"fixture-model-v1"', b'"tampered-model-v"', 1)
        )
        with self.assertRaises(EmbeddingPatchArtifactError):
            inspect_gold_embedding_shard_manifest(manifest_path)

    def test_plan_rejects_non_hex_digest_and_invalid_source_row_count(self) -> None:
        plan = build_gold_embedding_shard_plan(
            self.db_path,
            provider=_FakeProvider(),
            gold_records_sha256="d" * 64,
            shard_size=2,
        )

        def fingerprint(value):
            material = dict(value)
            material.pop("plan_fingerprint", None)
            return hashlib.sha256(
                json.dumps(
                    material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()

        bad_digest = dict(plan)
        bad_digest["source_db_sha256"] = "not-a-digest"
        bad_digest["plan_fingerprint"] = fingerprint(bad_digest)
        with self.assertRaisesRegex(EmbeddingPatchArtifactError, "digest"):
            export_gold_embedding_shards(
                self.db_path, self.root / "bad-digest", _FakeProvider(), plan=bad_digest
            )
        bad_count = dict(plan)
        bad_count["shards"] = [dict(item) for item in plan["shards"]]
        bad_count["shards"][0]["source_row_count"] = 0
        bad_count["plan_fingerprint"] = fingerprint(bad_count)
        with self.assertRaisesRegex(EmbeddingPatchArtifactError, "range"):
            export_gold_embedding_shards(
                self.db_path, self.root / "bad-count", _FakeProvider(), plan=bad_count
            )

    def test_final_manifest_must_cover_plan_and_bind_each_shard_provider(self) -> None:
        root = self.root / "strict-shards"
        plan = build_gold_embedding_shard_plan(
            self.db_path,
            provider=_FakeProvider(),
            gold_records_sha256="e" * 64,
            shard_size=1,
        )
        export_gold_embedding_shards(self.db_path, root, _FakeProvider(), plan=plan)
        manifest_path = root / "ncs_gold_embeddings.manifest.json"
        original = json.loads(manifest_path.read_text(encoding="utf-8"))
        truncated = dict(original)
        truncated["shards"] = truncated["shards"][:1]
        truncated["patch_count"] = truncated["shards"][0]["patch_count"]
        truncated["patch_counts"] = truncated["shards"][0]["patch_counts"]
        manifest_path.write_text(json.dumps(truncated), encoding="utf-8")
        with self.assertRaisesRegex(EmbeddingPatchArtifactError, "shards"):
            inspect_gold_embedding_shard_manifest(manifest_path)

        manifest_path.write_text(json.dumps(original), encoding="utf-8")
        first = original["shards"][0]
        shard_path = root / first["path"]
        binding = inspect_gold_embedding_patches(shard_path)["header"]["shard_binding"]
        other = _FakeProvider()
        other.model = "other-model"
        export_gold_embedding_patches(
            self.db_path,
            shard_path,
            other,
            entity_types=(first["entity_type"],),
            source_key_min=first["source_key_min"],
            source_key_max=first["source_key_max"],
            shard_binding=binding,
        )
        replacement = inspect_gold_embedding_patches(shard_path)["manifest"]
        updated = json.loads(manifest_path.read_text(encoding="utf-8"))
        updated["shards"][0]["sha256"] = hashlib.sha256(
            shard_path.read_bytes()
        ).hexdigest()
        updated["shards"][0]["records_sha256"] = replacement["records_sha256"]
        updated["shards"][0]["patch_count"] = replacement["patch_count"]
        updated["shards"][0]["patch_counts"] = replacement["patch_counts"]
        manifest_path.write_text(json.dumps(updated), encoding="utf-8")
        with self.assertRaisesRegex(EmbeddingPatchArtifactError, "provider"):
            inspect_gold_embedding_shard_manifest(manifest_path)


if __name__ == "__main__":
    unittest.main()
