from __future__ import annotations

import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_lpg import build_gold_lpg_projection  # noqa: E402
from ncs_mcp.gold_readiness import SERVING_CORE_PROFILE  # noqa: E402
from ncs_mcp.gold_stream import (  # noqa: E402
    GOLD_LPG_NDJSON_SCHEMA,
    MAX_STREAM_BATCH_SIZE,
    _columns,
    export_gold_lpg_ndjson,
)
from ncs_mcp.internal_job_roles import deterministic_gold_id  # noqa: E402


SCHEMA = """
CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
  middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
  sub_code TEXT, sub_name TEXT, review_status TEXT
);
CREATE TABLE competency_units (unit_code TEXT PRIMARY KEY, classification_id INTEGER, unit_name_raw TEXT, unit_name_refined TEXT, unit_level_raw TEXT, review_status TEXT);
CREATE TABLE competency_elements (element_id INTEGER PRIMARY KEY, unit_code TEXT, element_no TEXT, element_code_raw TEXT, element_name_raw TEXT, element_level_raw TEXT, review_status TEXT);
CREATE TABLE performance_criteria (criteria_id INTEGER PRIMARY KEY, element_id INTEGER, criteria_no TEXT, criteria_text_raw TEXT, criteria_text_refined TEXT, review_status TEXT);
CREATE TABLE ontology_concepts (concept_id INTEGER PRIMARY KEY, concept_name TEXT, concept_type TEXT, definition TEXT, definition_source TEXT, definition_status TEXT, review_status TEXT);
CREATE TABLE criteria_concept_links (link_id INTEGER PRIMARY KEY, criteria_id INTEGER, concept_id INTEGER, relation_type TEXT, link_method TEXT, link_status TEXT, confidence_score REAL);
CREATE TABLE task_ksa_concept_relations (relation_id INTEGER PRIMARY KEY, criteria_id INTEGER, source_concept_id INTEGER, target_concept_id INTEGER);
CREATE TABLE ncs_training_courses (training_course_id INTEGER PRIMARY KEY, ncs_cl_cd TEXT, compe_unit_name TEXT, compe_unit_level TEXT, train_goal TEXT, train_time TEXT, fac_name TEXT, meth_name TEXT);
CREATE TABLE ncs_training_course_unit_links (link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT, link_method TEXT, confidence_score REAL, review_status TEXT);
CREATE TABLE ncs_training_course_concept_links (link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT, concept_id INTEGER, link_method TEXT, confidence_score REAL, evidence_text TEXT, review_status TEXT);
CREATE TABLE ncs_training_course_element_links (link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT, element_id INTEGER, link_method TEXT, confidence_score REAL, evidence_text TEXT, review_status TEXT);
CREATE TABLE training_goal_concept_links (link_id INTEGER PRIMARY KEY, training_course_id INTEGER, unit_code TEXT, element_id INTEGER, concept_id INTEGER, link_method TEXT, confidence_score REAL, evidence_text TEXT, review_status TEXT);
CREATE TABLE training_delivery_relations (relation_id INTEGER PRIMARY KEY, training_course_id INTEGER, relation_type TEXT, relation_value TEXT, normalized_value TEXT, numeric_value REAL, evidence_text TEXT, confidence_score REAL, review_status TEXT);
CREATE TABLE ontology_concept_relations (relation_id INTEGER PRIMARY KEY);
CREATE TABLE task_similarity_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_career_paths (career_path_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_qualification_items (qualification_item_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_unit_qualification_links (link_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_job_base_competencies (job_base_competency_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_job_base_factors (job_base_factor_id INTEGER PRIMARY KEY);
CREATE TABLE ncs_unit_job_base_links (link_id INTEGER PRIMARY KEY);
"""


class GoldStreamTests(unittest.TestCase):
    def test_schema_introspection_ignores_uncontracted_virtual_tables(self) -> None:
        class VirtualTableSensitiveConnection:
            def execute(self, sql: str):
                if sql == "SELECT name FROM sqlite_master WHERE type = 'table'":
                    return [("classifications",), ("dbstat",), ("application_cache",)]
                if '"dbstat"' in sql:
                    raise sqlite3.OperationalError("no such module: dbstat")
                if '"classifications"' in sql:
                    return [(0, "classification_id")]
                raise AssertionError(f"Unexpected introspection query: {sql}")

        columns = _columns(VirtualTableSensitiveConnection())  # type: ignore[arg-type]

        self.assertEqual(columns, {"classifications": {"classification_id"}})

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "fixture.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO classifications VALUES (1, '02', 'Management', '01', 'HR', '01', 'Plan', '01', 'Planning', 'raw')")
            conn.execute("INSERT INTO competency_units VALUES ('U1', 1, 'HR Planning', NULL, '5', 'raw')")
            conn.execute("INSERT INTO competency_elements VALUES (10, 'U1', '1', 'E1', 'Plan work', '5', 'raw')")
            conn.executemany("INSERT INTO performance_criteria VALUES (?, 10, ?, ?, NULL, 'raw')", [(100, '1', 'Criterion A'), (101, '2', 'Criterion B')])
            conn.executemany("INSERT INTO ontology_concepts VALUES (?, ?, ?, NULL, NULL, 'missing', 'candidate')", [(200, 'Knowledge', 'knowledge'), (201, 'Skill', 'skill')])
            conn.executemany("INSERT INTO criteria_concept_links VALUES (?, ?, ?, 'requires', 'exact', 'auto_linked', .9)", [(300, 100, 200), (301, 101, 200), (302, 101, 201)])
            conn.execute("INSERT INTO task_ksa_concept_relations VALUES (900, 100, 200, 201)")
            conn.execute("INSERT INTO ncs_training_courses VALUES (400, 'U1', 'HR course', '5', 'Goal', '24', 'room', 'practice')")
            conn.execute("INSERT INTO ncs_training_course_unit_links VALUES (401, 400, 'U1', 'exact', 1, 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_concept_links VALUES (402, 400, 'U1', 201, 'goal_text', .9, 'goal', 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_concept_links VALUES (403, 400, 'U1', 200, 'unit_ksa_concept_inherited', .4, 'weak', 'auto_linked')")
            conn.execute("INSERT INTO ncs_training_course_element_links VALUES (404, 400, 'U1', 10, 'element', .8, 'element', 'auto_linked')")
            conn.execute("INSERT INTO training_goal_concept_links VALUES (405, 400, 'U1', 10, 201, 'goal_text', .95, 'direct', 'auto_linked')")
            conn.execute("INSERT INTO training_delivery_relations VALUES (406, 400, 'method', 'practice', 'practice', NULL, 'delivery', 1, 'auto_linked')")
            conn.execute("INSERT INTO ontology_concept_relations VALUES (500)")
            conn.execute("INSERT INTO task_similarity_links VALUES (501)")
            conn.execute("INSERT INTO ncs_career_paths VALUES (502)")
            conn.execute("INSERT INTO ncs_qualification_items VALUES (503)")
            conn.execute("INSERT INTO ncs_unit_qualification_links VALUES (504)")
            conn.execute("INSERT INTO ncs_job_base_competencies VALUES (505)")
            conn.execute("INSERT INTO ncs_job_base_factors VALUES (506)")
            conn.execute("INSERT INTO ncs_unit_job_base_links VALUES (507)")
            conn.commit()

    @staticmethod
    def _read_records(path: Path) -> list[dict[str, object]]:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @staticmethod
    def _role(role_id: str = "HR-PLANNER") -> dict[str, object]:
        return {
            "organization_namespace": "tenant-a",
            "role_id": role_id,
            "display_name": "Internal HR Planner",
            "aliases": ["People Planner"],
            "description": "Plans the internal workforce.",
            "duties": ["Workforce planning"],
            "provenance": {"source_system": "enterprise_role_catalog"},
        }

    @staticmethod
    def _alignment(role_id: str = "HR-PLANNER", **overrides: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "role_gold_id": deterministic_gold_id("tenant-a", role_id),
            "ncs_target_type": "ncs_job",
            "ncs_target_key": "02010101",
            "score": 0.91,
            "method": "semantic_candidate",
            "model": "fixture-model",
            "status": "candidate",
            "evidence": [{"kind": "role_text_similarity"}],
            "provenance": {"source_system": "enterprise_role_catalog"},
        }
        payload.update(overrides)
        return payload

    def test_stream_is_deterministic_and_matches_core_record_counts(self) -> None:
        one, two = self.root / "one.ndjson", self.root / "two.ndjson"
        first = export_gold_lpg_ndjson(self.db_path, one, batch_size=2)
        second = export_gold_lpg_ndjson(self.db_path, two, batch_size=2)
        in_memory = build_gold_lpg_projection(self.db_path)

        self.assertEqual(one.read_bytes(), two.read_bytes())
        self.assertEqual(first, second)
        self.assertEqual(first["schema"], GOLD_LPG_NDJSON_SCHEMA)
        self.assertEqual(first["snapshot"]["transaction_snapshot"], True)
        self.assertIsInstance(first["snapshot"]["schema_version"], int)
        self.assertIsInstance(first["snapshot"]["data_version"], int)
        self.assertEqual(first["node_count"], in_memory["manifest"]["node_count"])
        self.assertEqual(first["edge_count"], in_memory["manifest"]["edge_count"])
        records = self._read_records(one)
        self.assertEqual(records[-1]["record_type"], "manifest")
        self.assertEqual(records[-1]["manifest"], first)
        edges = [record["relationship"] for record in records if record["record_type"] == "relationship"]
        nodes = [record["node"] for record in records if record["record_type"] == "node"]
        self.assertEqual({node["id"] for node in nodes}, {node["id"] for node in in_memory["nodes"]})
        self.assertEqual({edge["id"] for edge in edges}, {edge["id"] for edge in in_memory["edges"]})
        job_summaries = [edge for edge in edges if edge["properties"].get("summary_scope") == "ncs_job_to_ksa"]
        self.assertEqual(len(job_summaries), 2)
        self.assertEqual(job_summaries[0]["properties"]["source_link_key_samples"], ["300", "301"])
        self.assertEqual(json.loads(job_summaries[0]["properties"]["method_distribution_json"]), {"exact": 2})
        self.assertFalse(any(edge["type"] == "TASK_KSA_CONCEPT_RELATION" for edge in edges))
        self.assertFalse(any(edge["properties"].get("link_method") == "unit_ksa_concept_inherited" for edge in edges))

        scope = first["scope_contract"]
        self.assertTrue(scope["sqlite_authoritative"])
        self.assertFalse(scope["complete_sqlite_ontology_replica"])
        fallback = {item["table"]: item for item in scope["fallback_evidence"]}
        self.assertEqual(fallback["task_similarity_links"]["row_count"], 1)
        self.assertEqual(fallback["ncs_unit_qualification_links"]["row_count"], 1)
        self.assertEqual(fallback["ncs_unit_job_base_links"]["row_count"], 1)
        self.assertEqual(
            scope["intentional_omissions"][0]["code"],
            "task_ksa_concept_relations_omitted",
        )
        self.assertEqual(
            scope["intentional_omissions"][1]["row_count"],
            1,
        )
        diagnostics = [record["diagnostic"] for record in records if record["record_type"] == "diagnostic"]
        self.assertEqual(
            {item["table"] for item in diagnostics if item["code"] == "sqlite_authoritative_fallback"},
            set(fallback),
        )

    def test_fetches_are_bounded_and_source_db_is_unchanged(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        observed: list[int] = []
        manifest = export_gold_lpg_ndjson(self.db_path, self.root / "bounded.ndjson", batch_size=1, _batch_observer=observed.append)

        self.assertTrue(observed)
        self.assertTrue(all(size <= 1 for size in observed))
        self.assertLessEqual(manifest["max_observed_batch_rows"], 1)
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertTrue(manifest["read_only"])
        self.assertFalse(manifest["db_writes"])

    def test_internal_role_nodes_precede_relationships_and_alignment_is_linked(self) -> None:
        output = self.root / "roles.ndjson"
        role = self._role()
        alignment = self._alignment()
        manifest = export_gold_lpg_ndjson(
            self.db_path,
            output,
            batch_size=2,
            internal_roles=[role],
            role_alignments=[alignment],
        )
        records = self._read_records(output)
        first_relationship = next(
            index for index, record in enumerate(records)
            if record["record_type"] == "relationship"
        )
        role_indexes = [
            index for index, record in enumerate(records)
            if record["record_type"] == "node"
            and "InternalJobRole" in record["node"]["labels"]
        ]
        self.assertEqual(len(role_indexes), 1)
        self.assertLess(role_indexes[0], first_relationship)

        alignments = [
            record["relationship"] for record in records
            if record["record_type"] == "relationship"
            and record["relationship"]["type"] == "ALIGNED_TO"
        ]
        self.assertEqual(len(alignments), 1)
        self.assertEqual(
            alignments[0]["source"],
            f"ncs:internal_job_role:{deterministic_gold_id('tenant-a', 'HR-PLANNER')}",
        )
        self.assertEqual(alignments[0]["target"], "ncs:ncs_job:02010101")
        self.assertEqual(alignments[0]["properties"]["status"], "candidate")
        self.assertEqual(manifest["node_counts"]["internal_job_role"], 1)
        self.assertEqual(manifest["edge_counts"]["ALIGNED_TO"], 1)

        in_memory = build_gold_lpg_projection(
            self.db_path,
            internal_roles=[role],
            role_alignments=[alignment],
        )
        streamed_nodes = {
            record["node"]["id"] for record in records
            if record["record_type"] == "node"
        }
        streamed_edges = {
            record["relationship"]["id"] for record in records
            if record["record_type"] == "relationship"
        }
        self.assertEqual(streamed_nodes, {node["id"] for node in in_memory["nodes"]})
        self.assertEqual(streamed_edges, {edge["id"] for edge in in_memory["edges"]})

    def test_role_overlay_order_is_canonical_and_deterministic(self) -> None:
        roles = [self._role("B"), self._role("A")]
        alignments = [
            self._alignment("B", score=0.8, status="review_required"),
            self._alignment("A", score=0.9),
        ]
        one = self.root / "role-order-one.ndjson"
        two = self.root / "role-order-two.ndjson"
        export_gold_lpg_ndjson(
            self.db_path,
            one,
            batch_size=2,
            internal_roles=roles,
            role_alignments=alignments,
        )
        export_gold_lpg_ndjson(
            self.db_path,
            two,
            batch_size=2,
            internal_roles=reversed(roles),
            role_alignments=reversed(alignments),
        )
        self.assertEqual(one.read_bytes(), two.read_bytes())

    def test_role_pii_and_trusted_status_are_rejected_before_output(self) -> None:
        pii_output = self.root / "pii.ndjson"
        pii_role = self._role()
        pii_role["provenance"] = {"employee_id": "E-123"}
        with self.assertRaisesRegex(ValueError, "forbidden personal or employee field"):
            export_gold_lpg_ndjson(
                self.db_path,
                pii_output,
                internal_roles=[pii_role],
            )
        self.assertFalse(pii_output.exists())

        status_output = self.root / "trusted-status.ndjson"
        with self.assertRaisesRegex(ValueError, "status must be one of"):
            export_gold_lpg_ndjson(
                self.db_path,
                status_output,
                internal_roles=[self._role()],
                role_alignments=[self._alignment(status="human_reviewed")],
            )
        self.assertFalse(status_output.exists())

    def test_unresolved_role_target_is_diagnostic_only_and_never_dangling(self) -> None:
        output = self.root / "unresolved-role.ndjson"
        export_gold_lpg_ndjson(
            self.db_path,
            output,
            internal_roles=[self._role()],
            role_alignments=[self._alignment(ncs_target_key="99999999")],
        )
        records = self._read_records(output)
        self.assertFalse(any(
            record["record_type"] == "relationship"
            and record["relationship"]["type"] == "ALIGNED_TO"
            for record in records
        ))
        diagnostics = [
            record["diagnostic"] for record in records
            if record["record_type"] == "diagnostic"
        ]
        self.assertTrue(any(
            item["code"] == "role_alignment_target_unresolved"
            and item["ncs_target_key"] == "99999999"
            for item in diagnostics
        ))

    def test_explicit_empty_role_overlay_preserves_default_output(self) -> None:
        default = self.root / "default.ndjson"
        explicit = self.root / "explicit-empty.ndjson"
        export_gold_lpg_ndjson(self.db_path, default, batch_size=2)
        export_gold_lpg_ndjson(
            self.db_path,
            explicit,
            batch_size=2,
            internal_roles=[],
            role_alignments=[],
        )
        self.assertEqual(default.read_bytes(), explicit.read_bytes())

    def test_failure_removes_temporary_output_and_keeps_existing_final(self) -> None:
        output = self.root / "failed.ndjson"
        output.write_text("prior export\n", encoding="utf-8")
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        with patch("ncs_mcp.gold_stream._write_json_line", side_effect=RuntimeError("injected write failure")):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                export_gold_lpg_ndjson(self.db_path, output, batch_size=2)

        self.assertEqual(output.read_text(encoding="utf-8"), "prior export\n")
        self.assertEqual(list(self.root.glob(".failed.ndjson.*.tmp")), [])
        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())

    def test_invalid_profile_and_batch_size_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            export_gold_lpg_ndjson(self.db_path, self.root / "bad.ndjson", batch_size=0)
        with self.assertRaises(ValueError):
            export_gold_lpg_ndjson(self.db_path, self.root / "bad.ndjson", batch_size=MAX_STREAM_BATCH_SIZE + 1)
        with self.assertRaises(ValueError):
            export_gold_lpg_ndjson(
                self.db_path,
                self.root / "bad.ndjson",
                profile=replace(SERVING_CORE_PROFILE, include_training_courses=False),
            )

    def test_source_and_sqlite_sidecars_can_never_be_export_destinations(self) -> None:
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        for blocked in (
            self.db_path,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
            Path(str(self.db_path) + "-journal"),
        ):
            with self.assertRaisesRegex(ValueError, "must not overwrite"):
                export_gold_lpg_ndjson(self.db_path, blocked)

        self.assertEqual(before, hashlib.sha256(self.db_path.read_bytes()).hexdigest())
        self.assertFalse(Path(str(self.db_path) + "-wal").exists())
        self.assertFalse(Path(str(self.db_path) + "-shm").exists())
        self.assertFalse(Path(str(self.db_path) + "-journal").exists())

    def test_duplicate_job_code_is_valid_and_not_reported_as_unresolved(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("INSERT INTO classifications VALUES (2, '02', 'Management', '01', 'HR', '01', 'Plan', '01', 'Planning duplicate', 'raw')")
            conn.commit()

        output = self.root / "duplicate-job.ndjson"
        export_gold_lpg_ndjson(self.db_path, output, batch_size=2)
        diagnostics = [record["diagnostic"] for record in self._read_records(output) if record["record_type"] == "diagnostic"]
        self.assertFalse(any(item["code"] == "ncs_job_code_unresolved" for item in diagnostics))

    def test_deleted_element_never_creates_dangling_summary_edge(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM competency_elements WHERE element_id = 10")
            conn.commit()

        output = self.root / "orphan-element.ndjson"
        export_gold_lpg_ndjson(self.db_path, output, batch_size=2)
        streamed = self._read_records(output)
        in_memory = build_gold_lpg_projection(self.db_path)
        streamed_edges = [record["relationship"] for record in streamed if record["record_type"] == "relationship"]
        self.assertEqual({edge["id"] for edge in streamed_edges}, {edge["id"] for edge in in_memory["edges"]})
        self.assertFalse(any(edge["properties"].get("derived_from") == "criteria_concept_links" for edge in streamed_edges if edge["source"].startswith("ncs:competency_element:")))

    def test_incomplete_classification_emits_no_job_or_job_edges(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("UPDATE classifications SET middle_code = NULL WHERE classification_id = 1")
            conn.commit()

        output = self.root / "incomplete-classification.ndjson"
        export_gold_lpg_ndjson(self.db_path, output, batch_size=2)
        streamed = self._read_records(output)
        in_memory = build_gold_lpg_projection(self.db_path)
        streamed_nodes = [record["node"] for record in streamed if record["record_type"] == "node"]
        streamed_edges = [record["relationship"] for record in streamed if record["record_type"] == "relationship"]
        self.assertEqual({node["id"] for node in streamed_nodes}, {node["id"] for node in in_memory["nodes"]})
        self.assertEqual({edge["id"] for edge in streamed_edges}, {edge["id"] for edge in in_memory["edges"]})
        self.assertFalse(any("NCSJob" in node["labels"] for node in streamed_nodes))
        self.assertFalse(any(edge["type"] == "REQUIRES_UNIT" for edge in streamed_edges))
        self.assertFalse(any(edge["properties"].get("summary_scope") == "ncs_job_to_ksa" for edge in streamed_edges))

    def test_blank_and_padded_classification_codes_match_in_memory_ids(self) -> None:
        for index, middle_code in enumerate(("", "   ", " 01 ")):
            with self.subTest(middle_code=repr(middle_code)):
                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute(
                        "UPDATE classifications SET middle_code = ? WHERE classification_id = 1",
                        (middle_code,),
                    )
                    conn.commit()

                output = self.root / f"normalized-classification-{index}.ndjson"
                export_gold_lpg_ndjson(self.db_path, output, batch_size=2)
                streamed = self._read_records(output)
                in_memory = build_gold_lpg_projection(self.db_path)
                streamed_nodes = [
                    record["node"] for record in streamed if record["record_type"] == "node"
                ]
                streamed_edges = [
                    record["relationship"]
                    for record in streamed
                    if record["record_type"] == "relationship"
                ]
                self.assertEqual(
                    {node["id"] for node in streamed_nodes},
                    {node["id"] for node in in_memory["nodes"]},
                )
                self.assertEqual(
                    {edge["id"] for edge in streamed_edges},
                    {edge["id"] for edge in in_memory["edges"]},
                )

                with closing(sqlite3.connect(self.db_path)) as conn:
                    conn.execute(
                        "UPDATE classifications SET middle_code = '01' WHERE classification_id = 1"
                    )
                    conn.commit()

    def test_stream_cli_requires_output_and_prints_manifest(self) -> None:
        script = ROOT / "scripts" / "export_gold_lpg_stream.py"
        output = self.root / "cli.ndjson"
        completed = subprocess.run(
            [sys.executable, str(script), "--db", str(self.db_path), "--out", str(output), "--batch-size", "2"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(output.is_file())
        self.assertEqual(json.loads(completed.stdout)["schema"], GOLD_LPG_NDJSON_SCHEMA)

        missing_out = subprocess.run(
            [sys.executable, str(script), "--db", str(self.db_path)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertNotEqual(missing_out.returncode, 0)
        self.assertIn("--out", missing_out.stderr)

    def test_stream_cli_accepts_role_json_and_alignment_jsonl(self) -> None:
        script = ROOT / "scripts" / "export_gold_lpg_stream.py"
        roles = self.root / "roles.json"
        alignments = self.root / "alignments.jsonl"
        roles.write_text(
            json.dumps({"internal_roles": [self._role()]}, ensure_ascii=False),
            encoding="utf-8",
        )
        alignments.write_text(
            json.dumps(self._alignment(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        output = self.root / "cli-roles.ndjson"
        completed = subprocess.run(
            [
                sys.executable,
                str(script),
                "--db", str(self.db_path),
                "--out", str(output),
                "--batch-size", "2",
                "--internal-roles", str(roles),
                "--role-alignments", str(alignments),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        manifest = json.loads(completed.stdout)
        self.assertEqual(manifest["node_counts"]["internal_job_role"], 1)
        self.assertEqual(manifest["edge_counts"]["ALIGNED_TO"], 1)


if __name__ == "__main__":
    unittest.main()
