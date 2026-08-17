from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


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
    def test_validate_and_export_jsonld(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_path = Path(tmp) / "ontology.jsonld"
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

            validation = validate_ontology_readiness(db_path)
            self.assertIn("counts", validation)
            self.assertIn("metrics", validation)
            self.assertIn("ontology_concept_label_candidates", validation["counts"])
            self.assertIn("label_candidates_missing_provenance", validation["metrics"])

            export = export_ontology_jsonld(db_path, out_path, include_chunk_evidence=False)
            self.assertTrue(out_path.exists())
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
