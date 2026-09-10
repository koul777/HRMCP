from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.neo4j_loader import (  # noqa: E402
    GoldLoadExecutionError,
    GoldLoadValidationError,
    Neo4jLoaderSettings,
    apply_embedding_patches,
    apply_embedding_shards,
    apply_vector_indexes,
    inspect_gold_lpg_ndjson,
    load_gold_lpg_ndjson,
    reconcile_gold_lpg,
)
from ncs_mcp.embedding_export import (  # noqa: E402
    build_gold_embedding_shard_plan,
    export_gold_embedding_shards,
)


class _EmbeddingProvider:
    provider_name = "fixture_provider"
    model = "fixture-model-v1"
    dimensions = 2
    enabled = True

    def embed_texts(self, texts):
        return tuple((0.1, 0.2) for _ in texts)


def _node(
    identifier: str,
    node_type: str = "ncs_job",
    labels: list[str] | None = None,
) -> dict[str, object]:
    labels = labels or ["LpgNode", "NCSJob"]
    return {
        "id": identifier,
        "labels": labels,
        "properties": {"id": identifier, "node_type": node_type, "name": identifier},
        "provenance": {"source_table": "fixture", "source_key": identifier},
    }


def _edge(identifier: str, source: str, target: str) -> dict[str, object]:
    return {
        "id": identifier,
        "type": "REQUIRES_KSA",
        "source": source,
        "target": target,
        "properties": {"id": identifier, "edge_type": "REQUIRES_KSA"},
        "provenance": {"source_table": "fixture_link", "source_key": identifier},
    }


def _write_artifact(path: Path, records: list[dict[str, object]]) -> None:
    raw_lines = [
        json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        + b"\n"
        for record in records
    ]
    nodes = [
        record["node"] for record in records if record.get("record_type") == "node"
    ]
    edges = [
        record["relationship"]
        for record in records
        if record.get("record_type") == "relationship"
    ]
    diagnostics = [
        record for record in records if record.get("record_type") == "diagnostic"
    ]
    node_counts: dict[str, int] = {}
    for node in nodes:
        key = str(node["properties"]["node_type"])
        node_counts[key] = node_counts.get(key, 0) + 1
    edge_counts: dict[str, int] = {}
    for edge in edges:
        key = str(edge["type"])
        edge_counts[key] = edge_counts.get(key, 0) + 1
    manifest = {
        "schema": "ncs_gold_lpg_ndjson_v1",
        "records_before_manifest": len(records),
        "records_sha256": hashlib.sha256(b"".join(raw_lines)).hexdigest(),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "diagnostic_count": len(diagnostics),
        "node_counts": node_counts,
        "edge_counts": edge_counts,
    }
    path.write_bytes(
        b"".join(raw_lines)
        + json.dumps(
            {"record_type": "manifest", "manifest": manifest},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


class FakeQuery:
    def __init__(self, text: str, timeout: float) -> None:
        self.text = text
        self.timeout = timeout


class FakeDriver:
    def __init__(
        self, *, fail_writes: int = 0, count_results: list[int] | None = None
    ) -> None:
        self.fail_writes = fail_writes
        self.count_results = list(count_results or [])
        self.calls: list[tuple[FakeQuery, dict[str, object]]] = []
        self.closed = False

    def execute_query(self, query: FakeQuery, **kwargs: object) -> object:
        self.calls.append((query, kwargs))
        if "matched_count" in query.text:
            parameters = kwargs.get("parameters_", {})
            return ([{"matched_count": len(parameters["patches"])}], None, None)
        if "RETURN count" in query.text:
            return ([{"count": self.count_results.pop(0)}], None, None)
        if self.fail_writes:
            self.fail_writes -= 1
            raise RuntimeError("temporary backend failure")
        return ([], None, None)

    def close(self) -> None:
        self.closed = True


class Neo4jLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.ndjson = self.root / "gold.ndjson"
        first, second = "ncs:ncs_job:1", "ncs:ncs_job:2"
        _write_artifact(
            self.ndjson,
            [
                {"record_type": "node", "node": _node(first)},
                {"record_type": "node", "node": _node(second)},
                {
                    "record_type": "relationship",
                    "relationship": _edge("ncs:edge:1", first, second),
                },
                {"record_type": "diagnostic", "diagnostic": {"code": "fixture"}},
            ],
        )
        self.settings = Neo4jLoaderSettings(
            enabled=True,
            uri="bolt://example",
            username="user",
            password="TOP-SECRET",
            database="gold",
        )

    def test_vector_indexes_are_dry_run_by_default_and_apply_fixed_ddl(self) -> None:
        dry = apply_vector_indexes(1024)
        self.assertEqual(dry["mode"], "dry_run")
        self.assertFalse(dry["neo4j_writes"])
        self.assertEqual(dry["vector_indexes"], 3)

        driver = FakeDriver()
        applied = apply_vector_indexes(
            1024,
            apply=True,
            settings=self.settings,
            driver_factory=lambda _: (driver, FakeQuery),
        )
        self.assertEqual(applied["mode"], "applied")
        self.assertEqual(applied["vector_indexes"], 3)
        self.assertTrue(driver.closed)
        self.assertEqual(len(driver.calls), 3)
        for query, kwargs in driver.calls:
            self.assertIn("CREATE VECTOR INDEX", query.text)
            self.assertIn("1024", query.text)
            self.assertEqual(kwargs["routing_"], "w")

    def test_dry_run_validates_without_driver_or_sqlite_write(self) -> None:
        source_db = self.root / "source.db"
        with closing(sqlite3.connect(source_db)) as conn:
            conn.execute("CREATE TABLE x (id INTEGER)")
            conn.commit()
        before = hashlib.sha256(source_db.read_bytes()).hexdigest()
        called = False

        def unexpected(_: object) -> object:
            nonlocal called
            called = True
            raise AssertionError("dry run must not create a driver")

        result = load_gold_lpg_ndjson(self.ndjson, driver_factory=unexpected)
        self.assertEqual(result["mode"], "dry_run")
        self.assertFalse(result["driver_used"])
        self.assertFalse(called)
        self.assertEqual(before, hashlib.sha256(source_db.read_bytes()).hexdigest())
        self.assertEqual(result["node_group_counts"], {"NCSJob": 2})

    def test_apply_batches_retry_and_resume_checkpoint(self) -> None:
        checkpoint = self.root / "resume.json"
        driver = FakeDriver(fail_writes=1)
        result = load_gold_lpg_ndjson(
            self.ndjson,
            apply=True,
            settings=self.settings,
            checkpoint_path=checkpoint,
            batch_size=1,
            apply_schema=False,
            max_retries=1,
            sleep=lambda _: None,
            driver_factory=lambda _: (driver, FakeQuery),
        )
        self.assertEqual(result["batches_written"], 3)
        self.assertEqual(result["completed_import_records"], 3)
        self.assertTrue(driver.closed)
        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        self.assertEqual(checkpoint_data["completed_import_records"], 3)
        resumed = FakeDriver()
        second = load_gold_lpg_ndjson(
            self.ndjson,
            apply=True,
            settings=self.settings,
            checkpoint_path=checkpoint,
            apply_schema=False,
            driver_factory=lambda _: (resumed, FakeQuery),
        )
        self.assertEqual(second["resumed_from_record"], 3)
        self.assertEqual(second["batches_written"], 0)

    def test_interleaved_groups_are_coalesced_within_one_bounded_window(self) -> None:
        artifact = self.root / "interleaved.ndjson"
        _write_artifact(
            artifact,
            [
                {"record_type": "node", "node": _node("ncs:ncs_job:1")},
                {
                    "record_type": "node",
                    "node": _node(
                        "ncs:competency_unit:1",
                        "competency_unit",
                        ["LpgNode", "CompetencyUnit"],
                    ),
                },
                {"record_type": "node", "node": _node("ncs:ncs_job:2")},
            ],
        )
        driver = FakeDriver()
        result = load_gold_lpg_ndjson(
            artifact,
            apply=True,
            settings=self.settings,
            batch_size=3,
            apply_schema=False,
            driver_factory=lambda _: (driver, FakeQuery),
        )
        self.assertEqual(result["batches_written"], 2)
        self.assertEqual(
            sorted(len(call[1]["parameters_"]["nodes"]) for call in driver.calls),
            [1, 2],
        )

    def test_rejects_order_dangling_duplicate_and_manifest_mismatch(self) -> None:
        first = "ncs:ncs_job:1"
        bad_order = self.root / "bad-order.ndjson"
        _write_artifact(
            bad_order,
            [
                {
                    "record_type": "relationship",
                    "relationship": _edge("ncs:edge:1", first, first),
                },
                {"record_type": "node", "node": _node(first)},
            ],
        )
        with self.assertRaises(GoldLoadValidationError):
            inspect_gold_lpg_ndjson(bad_order)

        dangling = self.root / "dangling.ndjson"
        _write_artifact(
            dangling,
            [
                {"record_type": "node", "node": _node(first)},
                {
                    "record_type": "relationship",
                    "relationship": _edge("ncs:edge:1", first, "ncs:ncs_job:missing"),
                },
            ],
        )
        with self.assertRaisesRegex(GoldLoadValidationError, "absent"):
            inspect_gold_lpg_ndjson(dangling)

        duplicate = self.root / "duplicate.ndjson"
        _write_artifact(
            duplicate,
            [
                {"record_type": "node", "node": _node(first)},
                {"record_type": "node", "node": _node(first)},
            ],
        )
        with self.assertRaisesRegex(GoldLoadValidationError, "duplicate"):
            inspect_gold_lpg_ndjson(duplicate)

        damaged = self.root / "damaged.ndjson"
        damaged.write_bytes(
            self.ndjson.read_bytes().replace(
                b'"name":"ncs:ncs_job:2"', b'"name":"tampered"', 1
            )
        )
        with self.assertRaisesRegex(GoldLoadValidationError, "records_sha256"):
            inspect_gold_lpg_ndjson(damaged)

    def test_paths_and_secrets_are_protected(self) -> None:
        with self.assertRaisesRegex(GoldLoadValidationError, "checkpoint"):
            load_gold_lpg_ndjson(self.ndjson, checkpoint_path=self.ndjson)
        report = self.settings.readiness()
        self.assertNotIn("TOP-SECRET", json.dumps(report))
        with self.assertRaises(GoldLoadExecutionError):
            load_gold_lpg_ndjson(
                self.ndjson,
                apply=True,
                settings=self.settings,
                apply_schema=False,
                max_retries=0,
                driver_factory=lambda _: (FakeDriver(fail_writes=1), FakeQuery),
            )

    def test_read_only_reconciliation_compares_allowlisted_groups(self) -> None:
        inspection = inspect_gold_lpg_ndjson(self.ndjson)
        driver = FakeDriver(count_results=[2, 1])
        result = reconcile_gold_lpg(
            inspection,
            settings=self.settings,
            driver_factory=lambda _: (driver, FakeQuery),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(all(call[1]["routing_"] == "r" for call in driver.calls))
        self.assertTrue(driver.closed)

    def test_embedding_patches_are_fixed_property_and_label_writes(self) -> None:
        patch = {
            "schema": "ncs_embedding_node_patch_v1",
            "entity_type": "PerformanceCriterion",
            "entity_id": "ncs:performance_criterion:1",
            "embedding": [0.1, 0.2],
            "content_hash": "hash",
            "cache_key": "cache",
            "provider": "provider",
            "model": "model@revision",
            "dimensions": 2,
            "metadata": {"untrusted_extra": "not_written"},
        }
        dry = apply_embedding_patches(iter([patch]))
        self.assertEqual(dry["mode"], "dry_run")
        driver = FakeDriver()
        applied = apply_embedding_patches(
            [patch],
            apply=True,
            settings=self.settings,
            driver_factory=lambda _: (driver, FakeQuery),
        )
        self.assertEqual(applied["embedding_patch_counts"], {"PerformanceCriterion": 1})
        query, kwargs = driver.calls[0]
        self.assertIn("node.embedding = row.embedding", query.text)
        self.assertNotIn("untrusted_extra", json.dumps(kwargs))
        self.assertEqual(
            kwargs["parameters_"]["patches"][0]["entity_id"], patch["entity_id"]
        )
        with self.assertRaises(GoldLoadValidationError):
            apply_embedding_patches([dict(patch, entity_type="Anything", dimensions=2)])

    def test_embedding_shards_replay_unmarked_units_and_defer_indexes(self) -> None:
        source = self.root / "embedding.db"
        conn = sqlite3.connect(source)
        try:
            conn.executescript("""
                CREATE TABLE competency_elements (element_id INTEGER PRIMARY KEY, element_name_raw TEXT);
                CREATE TABLE performance_criteria (criteria_id INTEGER PRIMARY KEY, element_id INTEGER, criteria_text_raw TEXT);
                CREATE TABLE ontology_concepts (concept_id INTEGER PRIMARY KEY, concept_name TEXT);
                INSERT INTO competency_elements VALUES (1, 'element');
                INSERT INTO performance_criteria VALUES (2, 1, 'criterion');
                INSERT INTO ontology_concepts VALUES (3, 'concept');
            """)
            conn.commit()
        finally:
            conn.close()
        provider = _EmbeddingProvider()
        plan = build_gold_embedding_shard_plan(
            source, provider=provider, gold_records_sha256="c" * 64, shard_size=1
        )
        shard_root = self.root / "embedding-shards"
        export_gold_embedding_shards(
            source, shard_root, provider, plan=plan, batch_size=1
        )
        manifest_path = shard_root / "ncs_gold_embeddings.manifest.json"
        ledger = self.root / "ledger.json"
        failed_drivers: list[FakeDriver] = []

        class ShortMatchDriver(FakeDriver):
            def execute_query(self, query, **kwargs):
                if "matched_count" in query.text:
                    self.calls.append((query, kwargs))
                    return ([{"matched_count": 0}], None, None)
                return super().execute_query(query, **kwargs)

        def failing(_):
            driver = ShortMatchDriver()
            failed_drivers.append(driver)
            return driver, FakeQuery

        with self.assertRaises(GoldLoadExecutionError):
            apply_embedding_shards(
                manifest_path,
                apply=True,
                reconciled=True,
                ledger_path=ledger,
                settings=self.settings,
                max_retries=0,
                driver_factory=failing,
                sleep=lambda _: None,
            )
        self.assertFalse(ledger.exists(), "failed shard must not be marked")
        self.assertFalse(
            any(
                "CREATE VECTOR INDEX" in call[0].text
                for driver in failed_drivers
                for call in driver.calls
            )
        )
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        ledger.write_text(
            json.dumps(
                {
                    "schema": "ncs_gold_embedding_shard_ledger_v1",
                    "manifest_fingerprint": manifest_data["plan_fingerprint"],
                    "manifest_sha256": hashlib.sha256(
                        manifest_path.read_bytes()
                    ).hexdigest(),
                    "shards": {
                        str(item["index"]): item["sha256"]
                        for item in manifest_data["shards"]
                    },
                    "completed": [1, 2, 3],
                    "approval_claim": False,
                }
            ),
            encoding="utf-8",
        )
        replay_drivers: list[FakeDriver] = []
        receipts: dict[tuple[str, int], str] = {}

        class ReceiptDriver(FakeDriver):
            def execute_query(self, query, **kwargs):
                parameters = kwargs.get("parameters_", {})
                if "NcsEmbeddingShardReceipt" in query.text:
                    if "MATCH" in query.text:
                        digest = parameters["manifest_sha256"]
                        return (
                            [
                                {"shard_index": index, "shard_sha256": shard_digest}
                                for (
                                    manifest_digest,
                                    index,
                                ), shard_digest in receipts.items()
                                if manifest_digest == digest
                            ],
                            None,
                            None,
                        )
                    receipts[
                        (parameters["manifest_sha256"], parameters["shard_index"])
                    ] = parameters["shard_sha256"]
                    return ([], None, None)
                return super().execute_query(query, **kwargs)

        def replay(_):
            driver = ReceiptDriver()
            replay_drivers.append(driver)
            return driver, FakeQuery

        result = apply_embedding_shards(
            manifest_path,
            apply=True,
            reconciled=True,
            ledger_path=ledger,
            settings=self.settings,
            driver_factory=replay,
            sleep=lambda _: None,
        )
        self.assertEqual(result["completed_shards"], [1, 2, 3])
        self.assertGreaterEqual(
            sum(
                "node.embedding = row.embedding" in call[0].text
                for driver in replay_drivers
                for call in driver.calls
            ),
            3,
            "a forged local ledger must not skip unreceipted shard writes",
        )
        self.assertTrue(
            any(
                "CREATE VECTOR INDEX" in call[0].text
                for driver in replay_drivers
                for call in driver.calls
            )
        )
        ledger_data = json.loads(ledger.read_text(encoding="utf-8"))
        ledger_data["manifest_sha256"] = "tampered"
        ledger.write_text(json.dumps(ledger_data), encoding="utf-8")
        with self.assertRaisesRegex(GoldLoadValidationError, "ledger"):
            apply_embedding_shards(
                manifest_path,
                apply=True,
                reconciled=True,
                ledger_path=ledger,
                settings=self.settings,
                driver_factory=replay,
            )

        crash_plan = build_gold_embedding_shard_plan(
            source,
            provider=provider,
            gold_records_sha256="f" * 64,
            shard_size=1,
        )
        crash_root = self.root / "crash-shards"
        export_gold_embedding_shards(source, crash_root, provider, plan=crash_plan)
        crash_manifest = crash_root / "ncs_gold_embeddings.manifest.json"
        crash_ledger = self.root / "crash-ledger.json"
        crash_receipts: dict[tuple[str, int], str] = {}
        crash_drivers: list[FakeDriver] = []

        class CrashBeforeReceiptDriver(FakeDriver):
            def execute_query(self, query, **kwargs):
                parameters = kwargs.get("parameters_", {})
                if "NcsEmbeddingShardReceipt" in query.text:
                    if "MATCH" in query.text:
                        return (
                            [
                                {"shard_index": index, "shard_sha256": digest}
                                for (
                                    manifest_digest,
                                    index,
                                ), digest in crash_receipts.items()
                                if manifest_digest == parameters["manifest_sha256"]
                            ],
                            None,
                            None,
                        )
                    raise RuntimeError("crash after embedding data write")
                return super().execute_query(query, **kwargs)

        def crash_before_receipt(_):
            driver = CrashBeforeReceiptDriver()
            crash_drivers.append(driver)
            return driver, FakeQuery

        with self.assertRaises(GoldLoadExecutionError):
            apply_embedding_shards(
                crash_manifest,
                apply=True,
                reconciled=True,
                ledger_path=crash_ledger,
                settings=self.settings,
                max_retries=0,
                driver_factory=crash_before_receipt,
                sleep=lambda _: None,
            )
        self.assertFalse(crash_ledger.exists())
        self.assertFalse(
            any(
                "CREATE VECTOR INDEX" in call[0].text
                for driver in crash_drivers
                for call in driver.calls
            )
        )
        replayed = apply_embedding_shards(
            crash_manifest,
            apply=True,
            reconciled=True,
            ledger_path=crash_ledger,
            settings=self.settings,
            driver_factory=replay,
            sleep=lambda _: None,
        )
        self.assertEqual(replayed["completed_shards"], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
