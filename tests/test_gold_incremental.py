from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_incremental import (  # noqa: E402
    GOLD_INCREMENTAL_SCHEMA,
    GoldIncrementalValidationError,
    MAX_RECORD_BYTES,
    plan_gold_lpg_incremental,
)
from ncs_mcp.gold_stream import GOLD_LPG_NDJSON_SCHEMA  # noqa: E402


PROFILE = {
    "name": "serving_core",
    "include_training_courses": True,
    "include_task_ksa_relations": False,
}


def _canonical(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _node(identifier: str, value: str = "same") -> dict[str, object]:
    return {
        "id": identifier,
        "labels": ["LpgNode", "KSAConcept"],
        "properties": {"id": identifier, "name": value, "node_type": "ontology_concept"},
        "provenance": {"source_table": "fixture", "source_key": identifier, "review_status": "candidate"},
    }


def _edge(identifier: str, source: str, target: str, value: str = "same") -> dict[str, object]:
    return {
        "id": identifier,
        "type": "REQUIRES_SKILL",
        "source": source,
        "target": target,
        "properties": {"id": identifier, "edge_type": "REQUIRES_SKILL", "value": value},
        "provenance": {"source_table": "fixture", "source_key": identifier, "review_status": "candidate"},
    }


def _write_export(
    path: Path,
    *,
    nodes: list[dict[str, object]],
    edges: list[dict[str, object]],
    diagnostics: list[dict[str, object]] | None = None,
    profile: dict[str, object] | None = None,
) -> None:
    records: list[dict[str, object]] = []
    records.extend({"record_type": "node", "node": node} for node in nodes)
    records.extend({"record_type": "relationship", "relationship": edge} for edge in edges)
    records.extend({"record_type": "diagnostic", "diagnostic": diagnostic} for diagnostic in diagnostics or [])
    raw = b"".join(_canonical(record) for record in records)
    manifest = {
        "schema": GOLD_LPG_NDJSON_SCHEMA,
        "projection_schema": "ncs_gold_lpg_v2",
        "profile": profile or PROFILE,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "diagnostic_count": len(diagnostics or []),
        "records_before_manifest": len(records),
        "records_sha256": hashlib.sha256(raw).hexdigest(),
        "source_tables": ["fixture"],
        "snapshot": {"transaction_snapshot": True, "schema_version": 1, "data_version": 1},
        "read_only": True,
        "db_writes": False,
        "approval_claim": False,
    }
    path.write_bytes(raw + _canonical({"record_type": "manifest", "manifest": manifest}))


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class GoldIncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _exports(self) -> tuple[Path, Path]:
        previous, current = self.root / "previous.ndjson", self.root / "current.ndjson"
        _write_export(
            previous,
            nodes=[_node("ncs:a"), _node("ncs:b"), _node("ncs:unchanged")],
            edges=[_edge("edge:old", "ncs:a", "ncs:b")],
        )
        _write_export(
            current,
            nodes=[_node("ncs:c", "created"), _node("ncs:a", "changed"), _node("ncs:unchanged")],
            edges=[_edge("edge:new", "ncs:a", "ncs:c", "created")],
        )
        return previous, current

    def test_added_changed_unchanged_and_deleted_records_become_safe_operations(self) -> None:
        previous, current = self._exports()
        output = self.root / "plan.ndjson"
        manifest = plan_gold_lpg_incremental(previous, current, output)
        records = _read(output)

        self.assertEqual(manifest["schema"], GOLD_INCREMENTAL_SCHEMA)
        self.assertEqual(manifest["mode"], "plan_written")
        self.assertEqual(
            manifest["operation_counts"],
            {"tombstone_node": 1, "tombstone_relationship": 1, "upsert_node": 2, "upsert_relationship": 1},
        )
        self.assertEqual(
            [record["record_type"] for record in records[:-1]],
            ["upsert_node", "upsert_node", "upsert_relationship", "tombstone_relationship", "tombstone_node"],
        )
        self.assertEqual([record[record["record_type"]]["id"] for record in records[:-1]], ["ncs:a", "ncs:c", "edge:new", "edge:old", "ncs:b"])
        tombstones = [record for record in records if record["record_type"].startswith("tombstone_")]
        self.assertTrue(all(record[record["record_type"]]["requires_operator_approval"] for record in tombstones))
        self.assertTrue(all(record[record["record_type"]]["action"] == "plan_only_no_apply" for record in tombstones))
        self.assertEqual(records[-1]["manifest"], manifest)
        self.assertTrue(manifest["tombstones_are_plans_only"])
        self.assertFalse(manifest["db_writes"])
        self.assertFalse(manifest["approval_claim"])

    def test_same_content_with_different_input_order_has_identical_plan(self) -> None:
        previous, current = self._exports()
        reordered = self.root / "reordered.ndjson"
        _write_export(
            reordered,
            nodes=[_node("ncs:unchanged"), _node("ncs:a", "changed"), _node("ncs:c", "created")],
            edges=[_edge("edge:new", "ncs:a", "ncs:c", "created")],
        )
        first, second = self.root / "first.ndjson", self.root / "second.ndjson"
        plan_gold_lpg_incremental(previous, current, first)
        plan_gold_lpg_incremental(previous, reordered, second)
        first_records, second_records = _read(first), _read(second)
        self.assertEqual(first_records[:-1], second_records[:-1])
        self.assertEqual(
            first_records[-1]["manifest"]["operations_sha256"],
            second_records[-1]["manifest"]["operations_sha256"],
        )

    def test_default_is_report_only_and_does_not_modify_inputs(self) -> None:
        previous, current = self._exports()
        before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (previous, current)}
        manifest = plan_gold_lpg_incremental(previous, current)

        self.assertEqual(manifest["mode"], "dry_run")
        self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (previous, current)})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_rejects_bad_manifest_digest_duplicate_ids_and_profile_drift(self) -> None:
        previous, current = self._exports()
        records = _read(current)
        records[-1]["manifest"]["records_sha256"] = "0" * 64
        current.write_bytes(b"".join(_canonical(record) for record in records))
        with self.assertRaisesRegex(GoldIncrementalValidationError, "records_sha256"):
            plan_gold_lpg_incremental(previous, current)

        _write_export(current, nodes=[_node("duplicate"), _node("duplicate")], edges=[])
        with self.assertRaisesRegex(GoldIncrementalValidationError, "duplicate node"):
            plan_gold_lpg_incremental(previous, current)

        _write_export(current, nodes=[_node("ncs:a")], edges=[], profile={**PROFILE, "include_training_courses": False})
        with self.assertRaisesRegex(GoldIncrementalValidationError, "profile changed"):
            plan_gold_lpg_incremental(previous, current)

    def test_bounded_record_reading_and_source_path_protection(self) -> None:
        previous, current = self._exports()
        observed: list[int] = []
        manifest = plan_gold_lpg_incremental(previous, current, _record_observer=observed.append)
        self.assertTrue(observed)
        self.assertTrue(all(0 < size <= MAX_RECORD_BYTES for size in observed))
        self.assertEqual(manifest["max_record_bytes"], MAX_RECORD_BYTES)
        with self.assertRaisesRegex(ValueError, "separate"):
            plan_gold_lpg_incremental(previous, current, previous)
        with self.assertRaisesRegex(ValueError, "different paths"):
            plan_gold_lpg_incremental(previous, previous)

    def test_atomic_failure_preserves_existing_output_and_removes_temp(self) -> None:
        previous, current = self._exports()
        output = self.root / "plan.ndjson"
        output.write_text("previous plan\n", encoding="utf-8")
        with patch("ncs_mcp.gold_incremental._write_line", side_effect=RuntimeError("write failed")):
            with self.assertRaisesRegex(RuntimeError, "write failed"):
                plan_gold_lpg_incremental(previous, current, output)
        self.assertEqual(output.read_text(encoding="utf-8"), "previous plan\n")
        self.assertEqual(list(self.root.glob(".plan.ndjson.*.tmp")), [])

    def test_cli_runs_as_dry_run_by_default_and_can_write_plan(self) -> None:
        previous, current = self._exports()
        script = ROOT / "scripts" / "diff_gold_lpg.py"
        dry = subprocess.run(
            [sys.executable, str(script), "--previous", str(previous), "--current", str(current)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(json.loads(dry.stdout)["mode"], "dry_run")
        output = self.root / "cli-plan.ndjson"
        write = subprocess.run(
            [sys.executable, str(script), "--previous", str(previous), "--current", str(current), "--out", str(output)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(write.returncode, 0, write.stderr)
        self.assertTrue(output.is_file())
        invalid = subprocess.run(
            [sys.executable, str(script), "--previous", str(previous), "--current", str(current), "--out", str(output), "--dry-run"],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("cannot be combined", invalid.stderr)
