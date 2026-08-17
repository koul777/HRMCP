from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
import os
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import upsert_sqf_items
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.evaluation import run_evaluation
from ncs_mcp.recommendation import recommend_education_for_duty
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model
from ncs_mcp.study_module_api import (
    parse_study_module_xml,
    refresh_learning_module_links,
    upsert_study_modules,
)


def seed_ncs_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
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
                  '5', ?, 'Plan workforce and HR strategy.', 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
        ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', 'Plan workforce', '5')
        """
    )
    element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
    conn.execute(
        """
        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
        VALUES (?, '1', 'Build a workforce plan from business strategy.')
        """,
        (element_id,),
    )
    conn.execute(
        """
        INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
        VALUES (?, '01', 'knowledge', '1', 'workforce planning')
        """,
        (element_id,),
    )
    ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
    conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type, definition,
            definition_status, relation_status, review_status, created_at, updated_at
        ) VALUES ('workforce planning', 'workforceplanning', 'knowledge',
                  'Planning workforce supply and demand.', 'reviewed', 'unlinked',
                  'human_reviewed', ?, ?)
        """,
        (timestamp, timestamp),
    )
    concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()["concept_id"]
    conn.execute(
        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
        (ksa_id, concept_id, timestamp),
    )
    return "0202020101_23v3"


def seed_sqf_target(db_path: Path, conn: sqlite3.Connection) -> str:
    upsert_sqf_items(
        conn,
        [
            {
                "ncsLclasCd": "02",
                "ncsLclasCdnm": "Business",
                "sqfFldCdnm": "Management",
                "sqfSubFldCdnm": "HR",
                "jobCdnm": "HR",
                "dutyNm": "HR planning",
                "dutyLevel": "5",
                "dutyLevelNm": "Practitioner",
                "dutyDef": "Plans workforce strategy.",
                "dutyEduTrain": "HR analytics training",
            }
        ],
    )
    conn.commit()
    conn.close()
    build_sqf_sqlite_model(db_path)
    conn = connect(db_path)
    initialize_database(conn)
    source_key = conn.execute("SELECT source_key FROM sqf_duties").fetchone()["source_key"]
    conn.close()
    return source_key


def add_document_evidence(conn: sqlite3.Connection, source_key: str) -> None:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO sqf_library_posts(lib_seq, title, source_url, collected_at, ontology_role)
        VALUES ('doc-1', 'HR SQF report', 'https://example.test/hr', ?, 'training_design')
        """,
        (timestamp,),
    )
    conn.execute(
        """
        INSERT INTO sqf_library_files(lib_seq, sys_dstin_cd, file_mstky, file_detl_seq, local_path, download_status)
        VALUES ('doc-1', '00', 'file', '1', 'hr.pdf', 'downloaded')
        """
    )
    file_id = conn.execute("SELECT file_id FROM sqf_library_files").fetchone()["file_id"]
    conn.execute(
        """
        INSERT INTO sqf_document_sources(
            lib_seq, file_id, title, ontology_role, text_extraction_status, created_at
        ) VALUES ('doc-1', ?, 'HR SQF report', 'training_design', 'extracted', ?)
        """,
        (file_id, timestamp),
    )
    document_id = conn.execute("SELECT document_id FROM sqf_document_sources").fetchone()["document_id"]
    conn.execute(
        """
        INSERT INTO sqf_document_assets(
            document_id, asset_path, asset_name, asset_type, extraction_status, created_at
        ) VALUES (?, 'hr.pdf', 'hr.pdf', 'pdf', 'extracted', ?)
        """,
        (document_id, timestamp),
    )
    asset_id = conn.execute("SELECT asset_id FROM sqf_document_assets").fetchone()["asset_id"]
    conn.execute(
        """
        INSERT INTO sqf_document_chunks(
            asset_id, chunk_index, page_start, page_end, text,
            char_count, token_estimate, created_at
        ) VALUES (?, 0, 2, 3, ?, 900, 150, ?)
        """,
        (asset_id, "FULL_UNSAFE_TEXT_MARKER " + ("workforce planning " * 40), timestamp),
    )
    chunk_id = conn.execute("SELECT chunk_id FROM sqf_document_chunks").fetchone()["chunk_id"]
    job_level_id = conn.execute(
        "SELECT sqf_job_level_id FROM sqf_job_levels_normalized WHERE sqf_source_key = ?",
        (source_key,),
    ).fetchone()["sqf_job_level_id"]
    conn.execute(
        """
        INSERT INTO sqf_chunk_job_level_matches(
            chunk_id, sqf_job_level_id, sqf_source_key, relation, score, method,
            evidence_text, matched_terms_json, review_status, created_at
        ) VALUES (?, ?, ?, 'strongEvidence', 12.0, 'test', 'HR report mentions workforce planning.', '[]', 'candidate', ?)
        """,
        (chunk_id, job_level_id, source_key, timestamp),
    )


class LearningRecommendationTests(unittest.TestCase):
    def set_db_env(self, db_path: Path) -> None:
        previous = os.environ.get("NCS_DB_PATH")
        os.environ["NCS_DB_PATH"] = str(db_path)

        def restore() -> None:
            if previous is None:
                os.environ.pop("NCS_DB_PATH", None)
            else:
                os.environ["NCS_DB_PATH"] = previous

        self.addCleanup(restore)

    def test_study_module_xml_upserts_without_duplicates(self) -> None:
        xml = """
        <root>
          <dataInfo><totalCount>1</totalCount><totalPage>1</totalPage></dataInfo>
          <row>
            <ncsLclasCd>02</ncsLclasCd>
            <ncsLclasCdnm>Business</ncsLclasCdnm>
            <learnModulSeq>LM-1</learnModulSeq>
            <learnModulName>HR planning</learnModulName>
            <learnModulText>workforce planning basics</learnModulText>
          </row>
        </root>
        """
        payload = parse_study_module_xml(xml)
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            self.assertEqual(upsert_study_modules(conn, payload["items"]), 1)
            self.assertEqual(upsert_study_modules(conn, payload["items"]), 1)
            count = conn.execute("SELECT COUNT(*) FROM ncs_learning_modules").fetchone()[0]
            conn.close()
            self.assertEqual(count, 1)

    def test_refresh_learning_module_links_empty_scope_does_not_refresh_all(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_ncs_unit(conn)
            upsert_study_modules(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsMclasCd": "02",
                        "ncsSclasCd": "02",
                        "ncsSubdCd": "01",
                        "learnModulSeq": "LM-HR-1",
                        "learnModulName": "HR workforce planning",
                        "learnModulText": "Learn workforce planning.",
                    }
                ],
            )

            empty_summary = refresh_learning_module_links(conn, module_seqs=[])
            link_count_after_empty = conn.execute(
                "SELECT COUNT(*) FROM learning_module_unit_links"
            ).fetchone()[0]
            full_summary = refresh_learning_module_links(conn, module_seqs=None)
            link_count_after_full = conn.execute(
                "SELECT COUNT(*) FROM learning_module_unit_links"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(empty_summary["modules_processed"], 0)
            self.assertEqual(link_count_after_empty, 0)
            self.assertEqual(full_summary["modules_processed"], 1)
            self.assertGreater(link_count_after_full, 0)

    def test_recommendation_uses_trusted_mapping_and_saves_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_ncs_unit(conn)
            source_key = seed_sqf_target(db_path, conn)
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, evidence_text, review_status, created_at, updated_at
                ) VALUES (?, 'ncs_competency_unit', ?, 'closeMatch', 9.5, 'reviewed',
                          'test', 'HR planning maps to workforce planning.', 'accepted', ?, ?)
                """,
                (source_key, unit_code, now_utc(), now_utc()),
            )
            upsert_study_modules(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "ncsMclasCd": "02",
                        "ncsMclasCdnm": "HR",
                        "ncsSclasCd": "02",
                        "ncsSclasCdnm": "HRM",
                        "ncsSubdCd": "01",
                        "ncsSubdCdnm": "HR planning",
                        "learnModulSeq": "LM-HR-1",
                        "learnModulName": "HR workforce planning",
                        "learnModulText": "Learn workforce planning and HR strategy.",
                    }
                ],
            )
            refresh_learning_module_links(conn)
            add_document_evidence(conn, source_key)
            conn.commit()

            result = recommend_education_for_duty(conn, query="HR", major_code="02", limit=3)

            self.assertTrue(result["ok"])
            self.assertIsNotNone(result["recommendation_run_id"])
            self.assertFalse(result["audit"]["candidate_mappings_used"])
            self.assertGreaterEqual(result["audit"]["accepted_mappings_count"], 1)
            self.assertGreaterEqual(len(result["recommendations"]), 1)
            first = result["recommendations"][0]
            self.assertEqual(first["learn_module_seq"], "LM-HR-1")
            self.assertGreaterEqual(len(first["evidence"]), 1)
            self.assertNotIn("FULL_UNSAFE_TEXT_MARKER", str(first["sqf_document_evidence"]))
            saved = conn.execute(
                "SELECT COUNT(*) FROM education_recommendation_evidence WHERE run_id = ?",
                (result["recommendation_run_id"],),
            ).fetchone()[0]
            conn.close()
            self.assertGreaterEqual(saved, 1)

    def test_candidate_mapping_does_not_leak_into_recommendation_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_ncs_unit(conn)
            source_key = seed_sqf_target(db_path, conn)
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, evidence_text, review_status, created_at, updated_at
                ) VALUES (?, 'ncs_competency_unit', ?, 'closeMatch', 99, 'lexical',
                          'test', 'Candidate only.', 'candidate', ?, ?)
                """,
                (source_key, unit_code, now_utc(), now_utc()),
            )
            conn.commit()

            result = recommend_education_for_duty(conn, query="HR", major_code="02", limit=1)

            self.assertTrue(result["ok"])
            self.assertFalse(result["audit"]["candidate_mappings_used"])
            self.assertEqual(result["audit"]["accepted_mappings_count"], 0)
            self.assertEqual(result["recommendations"][0]["matched_ncs_units"], [])
            conn.close()

    def test_major_code_only_learning_module_is_not_recommended(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_ncs_unit(conn)
            source_key = seed_sqf_target(db_path, conn)
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, evidence_text, review_status, created_at, updated_at
                ) VALUES (?, 'ncs_competency_unit', ?, 'closeMatch', 9.5, 'reviewed',
                          'test', 'Trusted HR planning mapping.', 'accepted', ?, ?)
                """,
                (source_key, unit_code, now_utc(), now_utc()),
            )
            upsert_study_modules(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "ncsMclasCd": "99",
                        "ncsMclasCdnm": "Unrelated",
                        "ncsSclasCd": "99",
                        "ncsSclasCdnm": "Unrelated",
                        "ncsSubdCd": "99",
                        "ncsSubdCdnm": "Unrelated",
                        "learnModulSeq": "LM-GENERIC-1",
                        "learnModulName": "Generic business administration",
                        "learnModulText": "Office filing and routine paperwork.",
                    }
                ],
            )
            refresh_learning_module_links(conn)
            result = recommend_education_for_duty(
                conn,
                query="HR",
                major_code="02",
                target_source_key=source_key,
                limit=1,
                save=False,
            )
            conn.close()

            self.assertTrue(result["ok"])
            self.assertIsNone(result["recommendations"][0]["learn_module_seq"])
            self.assertIn("ncs_fallback", result["recommendations"][0]["match"]["reasons"])

    def test_learning_path_and_v1_review_tools_use_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_ncs_unit(conn)
            source_key = seed_sqf_target(db_path, conn)
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, evidence_text, review_status, created_at, updated_at
                ) VALUES (?, 'ncs_competency_unit', ?, 'closeMatch', 9.0, 'reviewed',
                          'test', 'Maps HR planning to workforce planning.', 'candidate', ?, ?)
                """,
                (source_key, unit_code, timestamp, timestamp),
            )
            match_id = conn.execute("SELECT match_id FROM sqf_ncs_matches").fetchone()["match_id"]
            concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()["concept_id"]
            upsert_study_modules(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "ncsMclasCd": "02",
                        "ncsMclasCdnm": "HR",
                        "ncsSclasCd": "02",
                        "ncsSclasCdnm": "HRM",
                        "ncsSubdCd": "01",
                        "ncsSubdCdnm": "HR planning",
                        "learnModulSeq": "LM-HR-2",
                        "learnModulName": "Workforce planning module",
                        "learnModulText": "workforce planning HR strategy",
                    }
                ],
            )
            refresh_learning_module_links(conn)
            conn.commit()
            conn.close()

            self.set_db_env(db_path)
            from ncs_mcp import server

            reports_dir = ROOT / "reports" / "_test_review_packets" / Path(tmp).name
            reports_dir.mkdir(parents=True, exist_ok=True)
            self.addCleanup(shutil.rmtree, reports_dir, ignore_errors=True)
            sqf_packet = reports_dir / "sqf_match_review_packet.md"
            sqf_packet_text = (
                f"sqf_match:{match_id}\n"
                "Human confirmed SQF-NCS mapping from reviewed packet.\n"
            )
            sqf_packet.write_text(sqf_packet_text, encoding="utf-8")
            sqf_packet_hash = "sha256:" + hashlib.sha256(
                sqf_packet.read_bytes()
            ).hexdigest()
            concept_packet = reports_dir / "concept_review_packet.md"
            concept_packet_text = (
                f"concept_id:{concept_id}\n"
                "Human confirmed concept definition from reviewed packet.\n"
            )
            concept_packet.write_text(concept_packet_text, encoding="utf-8")
            concept_packet_hash = "sha256:" + hashlib.sha256(
                concept_packet.read_bytes()
            ).hexdigest()

            blocked = server.review_sqf_ncs_match(
                match_id=match_id,
                new_status="accepted",
                reviewer_id="mcp",
            )
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["error"]["code"], "trusted_review_provenance_required")

            reviewed = server.review_sqf_ncs_match(
                match_id=match_id,
                new_status="accepted",
                reviewer_id="tester",
                notes="trusted fixture",
                source_decision_packet=str(sqf_packet),
                source_artifact_hash=sqf_packet_hash,
                rationale="Human confirmed SQF-NCS mapping from reviewed packet.",
                evidence_refs=[f"sqf_match:{match_id}"],
                run_artifact="reports/sqf_match_review_run.json",
            )
            self.assertTrue(reviewed["ok"])
            self.assertIn("data", reviewed)
            self.assertIn("audit", reviewed)
            self.assertTrue(reviewed["recommendation_eligible"])

            concepts = server.search_ontology_concepts(query="workforce", concept_type="knowledge")
            self.assertTrue(concepts["ok"])
            self.assertGreaterEqual(len(concepts["data"]["concepts"]), 1)

            concept_review = server.review_ontology_concept(
                concept_id=concept_id,
                definition="Workforce supply and demand planning.",
                aliases=["workforce plan"],
                reviewer_id="tester",
                source_decision_packet=str(concept_packet),
                source_artifact_hash=concept_packet_hash,
                rationale="Human confirmed concept definition from reviewed packet.",
                evidence_refs=["concept:workforce"],
                run_artifact="reports/concept_review_run.json",
            )
            self.assertTrue(concept_review["ok"])
            self.assertEqual(concept_review["concept"]["definition_status"], "defined")

            evidence = server.get_concept_evidence(concept_id=concept_id)
            self.assertTrue(evidence["ok"])
            self.assertGreaterEqual(len(evidence["source_ksa"]), 1)

            path = server.get_learning_path_for_sqf_job(query="HR", major_code="02", limit=2)
            self.assertTrue(path["ok"])
            self.assertIn("path", path["data"])
            self.assertFalse(path["audit"].get("candidate_mappings_used", False))

    def test_dashboard_recommendation_audit_api_and_evaluation_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_ncs_unit(conn)
            source_key = seed_sqf_target(db_path, conn)
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, evidence_text, review_status, created_at, updated_at
                ) VALUES (?, 'ncs_competency_unit', ?, 'closeMatch', 9.5, 'reviewed',
                          'test', 'Trusted HR planning mapping.', 'accepted', ?, ?)
                """,
                (source_key, unit_code, now_utc(), now_utc()),
            )
            upsert_study_modules(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "ncsMclasCd": "02",
                        "ncsMclasCdnm": "HR",
                        "ncsSclasCd": "02",
                        "ncsSclasCdnm": "HRM",
                        "ncsSubdCd": "01",
                        "ncsSubdCdnm": "HR planning",
                        "learnModulSeq": "LM-HR-3",
                        "learnModulName": "HR workforce planning",
                        "learnModulText": "workforce planning and HR strategy",
                    }
                ],
            )
            refresh_learning_module_links(conn)
            result = recommend_education_for_duty(conn, query="HR", major_code="02", limit=2)
            conn.close()
            self.assertTrue(result["ok"])

            from scripts.ncs_dashboard import HTML, get_recommendation_detail, get_recommendation_runs

            self.assertIn("Recommendation Audit", HTML)
            runs = get_recommendation_runs(db_path, {"limit": ["10"]})
            self.assertEqual(runs["total"], 1)
            detail = get_recommendation_detail(db_path, {"run_id": [str(result["recommendation_run_id"])]})
            self.assertEqual(detail["run"]["run_id"], result["recommendation_run_id"])
            self.assertGreaterEqual(len(detail["evidence"]), 1)

            metrics = run_evaluation(db_path, run_name="recommendation-baseline")
            self.assertEqual(metrics["recommendation_run_count"], 1)
            self.assertGreaterEqual(metrics["recommendation_item_count"], 1)
            self.assertEqual(metrics["candidate_leakage_count"], 0)
            self.assertIsNone(metrics["human_review_precision_at_5"])


if __name__ == "__main__":
    unittest.main()
