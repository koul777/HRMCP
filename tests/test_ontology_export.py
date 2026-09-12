from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import upsert_sqf_items
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.ontology import build_sqf_mapping_candidates
from ncs_mcp.ontology_export import export_ontology_jsonld, validate_ontology_readiness
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model


class OntologyExportTests(unittest.TestCase):
    @staticmethod
    def _create_empty_database(db_path: Path) -> None:
        conn = connect(db_path)
        initialize_database(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _source_snapshot(root: Path, db_path: Path) -> dict[str, object]:
        source_bytes = db_path.read_bytes()
        return {
            "bytes": source_bytes,
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
            "mtime_ns": db_path.stat().st_mtime_ns,
            "files": sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if not path.is_dir()
            ),
        }

    def _assert_collision_rejected_without_mutation(
        self,
        root: Path,
        db_path: Path,
        out_path: Path,
    ) -> None:
        before = self._source_snapshot(root, db_path)
        with self.assertRaisesRegex(ValueError, "destination must differ"):
            export_ontology_jsonld(db_path, out_path)
        self.assertEqual(self._source_snapshot(root, db_path), before)

    def test_export_rejects_source_database_as_destination_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            self._create_empty_database(db_path)

            self._assert_collision_rejected_without_mutation(root, db_path, db_path)

    def test_export_rejects_normalized_alias_before_creating_parent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            missing_parent = root / "must-not-be-created"
            alias_path = missing_parent / ".." / db_path.name
            self._create_empty_database(db_path)

            self._assert_collision_rejected_without_mutation(root, db_path, alias_path)
            self.assertFalse(missing_parent.exists())

    def test_export_rejects_relative_absolute_alias_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            self._create_empty_database(db_path)
            relative_alias = Path(os.path.relpath(db_path, start=Path.cwd()))

            self._assert_collision_rejected_without_mutation(root, db_path, relative_alias)

    @unittest.skipUnless(os.name == "nt", "case aliases require a case-insensitive path platform")
    def test_export_rejects_case_normalized_alias_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            self._create_empty_database(db_path)
            case_alias = Path(str(db_path).swapcase())

            self._assert_collision_rejected_without_mutation(root, db_path, case_alias)

    def test_export_rejects_hardlink_destination_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            hardlink_path = root / "ontology.jsonld"
            self._create_empty_database(db_path)
            try:
                os.link(db_path, hardlink_path)
            except OSError as exc:
                self.skipTest(f"hardlinks unavailable: {exc}")

            self._assert_collision_rejected_without_mutation(root, db_path, hardlink_path)

    def test_export_rejects_symlink_destination_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            symlink_path = root / "ontology.jsonld"
            self._create_empty_database(db_path)
            try:
                os.symlink(db_path, symlink_path)
            except OSError as exc:
                self.skipTest(f"symlinks unavailable: {exc}")

            self._assert_collision_rejected_without_mutation(root, db_path, symlink_path)

    def test_export_fails_closed_when_file_identity_cannot_be_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "ncs.db"
            missing_parent = root / "must-not-be-created"
            out_path = missing_parent / "ontology.jsonld"
            self._create_empty_database(db_path)
            before = self._source_snapshot(root, db_path)

            with mock.patch(
                "ncs_mcp.ontology_export.os.path.samefile",
                side_effect=PermissionError("identity unavailable"),
            ):
                with self.assertRaisesRegex(ValueError, "safely verify"):
                    export_ontology_jsonld(db_path, out_path)

            self.assertEqual(self._source_snapshot(root, db_path), before)
            self.assertFalse(missing_parent.exists())

    def test_validate_is_report_only_and_does_not_initialize_source(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.commit()
            conn.close()
            before = (
                db_path.stat().st_size,
                db_path.stat().st_mtime_ns,
                db_path.read_bytes(),
            )

            validation = validate_ontology_readiness(db_path)

            after = (
                db_path.stat().st_size,
                db_path.stat().st_mtime_ns,
                db_path.read_bytes(),
            )

        self.assertIn("counts", validation)
        self.assertEqual(after, before)

    def test_validate_and_export_jsonld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "new-export-parent" / "ontology.jsonld"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                          '6', ?, 'Plan HR strategy.', 'matched', ?, ?)
                """,
                (classification_id, ts, ts),
            )
            upsert_sqf_items(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "sqfFldCdnm": "Management",
                        "jobCdnm": "HR",
                        "dutyNm": "HR(6)",
                        "dutyLevel": "6",
                        "dutyDef": "Plan HR strategy.",
                    }
                ],
            )
            conn.commit()
            build_sqf_sqlite_model(db_path)
            build_sqf_mapping_candidates(conn, mvp_only=False, major_code="02")
            conn.close()

            source_before = (
                db_path.stat().st_size,
                db_path.stat().st_mtime_ns,
                db_path.read_bytes(),
            )
            validation = validate_ontology_readiness(db_path)
            self.assertIn("counts", validation)
            self.assertIn("metrics", validation)
            self.assertIn("ontology_concept_label_candidates", validation["counts"])
            self.assertIn("label_candidates_missing_provenance", validation["metrics"])

            export = export_ontology_jsonld(db_path, out_path, include_chunk_evidence=False)
            self.assertTrue(out_path.exists())
            source_after = (
                db_path.stat().st_size,
                db_path.stat().st_mtime_ns,
                db_path.read_bytes(),
            )
            self.assertEqual(source_after, source_before)
            payload = json.loads(out_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["@type"], "schema:Dataset")
            self.assertGreater(export["nodes_and_edges"], 0)

    def test_validate_flags_label_candidates_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
                """,
                ("long concept", "longconcept", "knowledge", ts, ts),
            )
            concept_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, concept_type, source_text, label_text,
                    normalized_label_key, label_role, source_method,
                    candidate_rank, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', '', 'label', 'label',
                          'short_representative_label', 'test', 1, 0.5,
                          'candidate', ?, ?)
                """,
                (concept_id, ts, ts),
            )
            conn.commit()
            conn.close()

            validation = validate_ontology_readiness(db_path)

        self.assertFalse(validation["ok"])
        self.assertEqual(validation["metrics"]["label_candidates_missing_provenance"], 1)
        self.assertIn(
            "ontology_concept_label_candidates.provenance",
            {issue["check"] for issue in validation["issues"]},
        )

    def test_validate_allows_audited_human_reviewed_label_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                """
            )
            classification_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                          'HR planning', '4', ?, ?, ?)
                """,
                (classification_id, ts, ts),
            )
            cur = conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw,
                    element_name_raw, element_level_raw
                ) VALUES ('0202020101_26v1', '1', '01', 'Workforce plan', '4')
                """
            )
            element_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?, 'K', 'knowledge', '1', 'workforce planning source')
                """,
                (element_id,),
            )
            ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES ('workforce planning source', 'workforceplanningsource',
                          'knowledge', 'candidate', 'linked', 'model_preprocessed', ?, ?)
                """,
                (ts, ts),
            )
            concept_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_scope_key, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                          'workforce planning source', 'workforce planning',
                          'workforceplanning', 'short_representative_label',
                          'rule_based_short_label_candidate', 1, 0.8,
                          'human_reviewed', ?, ?)
                """,
                (concept_id, ksa_id, ts, ts),
            )
            label_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO review_audit_log(
                    entity_type, entity_id, action, previous_status,
                    new_status, reviewer_id, notes, created_at
                ) VALUES ('ontology_concept_label_candidate', ?, 'ksa_label_approve',
                          'candidate', 'human_reviewed', 'tester',
                          'human checked source and label', ?)
                """,
                (str(label_id), ts),
            )
            conn.commit()
            conn.close()

            validation = validate_ontology_readiness(db_path)

        checks = {issue["check"] for issue in validation["issues"]}
        self.assertNotIn("ontology_concept_label_candidates.review_status", checks)
        self.assertEqual(validation["metrics"]["trusted_label_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["audited_trusted_label_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["unaudited_trusted_label_candidate_statuses"], 0)

    def test_validate_allows_llm_reviewed_label_candidate_without_human_audit_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            cur = conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                """
            )
            classification_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES ('0202020101_26v1', '0202020101', '26v1',
                          'HR planning', '4', ?, ?, ?)
                """,
                (classification_id, ts, ts),
            )
            cur = conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw,
                    element_name_raw, element_level_raw
                ) VALUES ('0202020101_26v1', '1', '01', 'Workforce plan', '4')
                """
            )
            element_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                ) VALUES (?, 'K', 'knowledge', '1', 'workforce planning source')
                """,
                (element_id,),
            )
            ksa_id = cur.lastrowid
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES ('workforce planning source', 'workforceplanningsource',
                          'knowledge', 'candidate', 'linked', 'model_preprocessed', ?, ?)
                """,
                (ts, ts),
            )
            concept_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_scope_key, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, '02:02:02:01', 'knowledge',
                          'workforce planning source', 'workforce planning',
                          'workforceplanning', 'short_representative_label',
                          'rule_based_short_label_candidate', 1, 0.8,
                          'llm_reviewed', ?, ?)
                """,
                (concept_id, ksa_id, ts, ts),
            )
            for meaning_role, source_method, review_status in (
                ("term_definition_candidate", "term_definition_template", "llm_reviewed"),
                ("task_knowledge_significance", "task_context_template", "needs_review"),
                ("task_knowledge_significance", "unlinked_concept_fallback", "candidate"),
            ):
                conn.execute(
                    """
                    INSERT INTO ksa_meaning_candidates(
                        concept_id, concept_type, meaning_role, meaning_text,
                        source_method, evidence_text, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, 'knowledge', ?, 'workforce planning meaning',
                              ?, 'unit: HR planning', 0.72, ?, ?, ?)
                    """,
                    (concept_id, meaning_role, source_method, review_status, ts, ts),
                )
            conn.commit()
            conn.close()

            validation = validate_ontology_readiness(db_path)

        checks = {issue["check"] for issue in validation["issues"]}
        self.assertNotIn("ontology_concept_label_candidates.review_status", checks)
        self.assertEqual(validation["metrics"]["trusted_label_candidate_statuses"], 0)
        self.assertEqual(validation["metrics"]["llm_reviewed_label_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["llm_reviewed_meaning_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["needs_review_meaning_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["candidate_meaning_candidate_statuses"], 1)
        self.assertEqual(validation["metrics"]["audited_trusted_label_candidate_statuses"], 0)
        self.assertEqual(validation["metrics"]["unaudited_trusted_label_candidate_statuses"], 0)


if __name__ == "__main__":
    unittest.main()
