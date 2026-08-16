from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, normalize_concept_key, now_utc
from ncs_mcp.config import load_settings, read_env_values
from ncs_mcp.ncs_reference import (
    build_learning_module_links,
    build_ncs_derived_learning_plans,
    build_report_training_courses,
    extract_ncs_reference_entities,
    import_ncs_reference_html,
    link_reference_entities_to_ncs,
    recommend_education_by_concepts,
    recommend_learning_modules_by_ncs,
    resolve_ncs_unit_target,
    review_exact_learning_module_name_links,
    review_learning_module_ncs_link,
    search_ncs_reference_chunks,
)


def seed_hiring_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('02', '경영·회계·사무', '02', '총무·인사', '02', '인사·조직', '01', '인사')
        """
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()["classification_id"]
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_definition,
            api_match_status, created_at, updated_at
        ) VALUES ('0202020202_23v1', '0202020202', '23v1', '인력채용',
                  '4', ?, '조직에 필요한 인력을 채용하는 능력이다.', 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
        ) VALUES ('0202020202_23v1', '1', '0202020202_23v1 1', '채용계획 수립하기', '4')
        """
    )
    element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
    conn.execute(
        """
        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
        VALUES (?, '1', '조직의 인력수요를 분석하여 채용계획을 수립할 수 있다.')
        """,
        (element_id,),
    )
    conn.execute(
        """
        INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
        VALUES (?, '01', '지식', '1', '채용절차')
        """,
        (element_id,),
    )
    ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
    conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type, definition,
            definition_status, relation_status, review_status, created_at, updated_at
        ) VALUES ('채용절차', '채용절차', 'knowledge', '채용 단계와 기준을 운영하는 지식.',
                  'defined', 'unlinked', 'human_reviewed', ?, ?)
        """,
        (timestamp, timestamp),
    )
    concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()["concept_id"]
    conn.execute(
        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
        (ksa_id, concept_id, timestamp),
    )
    conn.execute(
        """
        INSERT INTO ncs_learning_modules(
            learn_module_seq, learn_module_name, learn_module_text,
            ncs_lclas_cd, ncs_lclas_name, ncs_mclas_cd, ncs_mclas_name,
            ncs_sclas_cd, ncs_sclas_name, ncs_subd_cd, ncs_subd_name,
            source_payload, api_fetched_at
        ) VALUES ('LM-HIRE-1', '인력채용', '채용계획과 채용절차 학습',
                  '02', '경영·회계·사무', '02', '총무·인사', '02', '인사·조직', '01', '인사',
                  '{}', ?)
        """,
        (timestamp,),
    )
    return "0202020202_23v1"


def seed_out_of_scope_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('03', '금융·보험', '01', '금융', '01', '금융영업', '01', '창구사무')
        """
    )
    classification_id = conn.execute(
        "SELECT classification_id FROM classifications WHERE major_code = '03'"
    ).fetchone()["classification_id"]
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_definition,
            api_match_status, created_at, updated_at
        ) VALUES ('0301010101_23v1', '0301010101', '23v1', '금융고객관리',
                  '3', ?, '금융 고객을 관리하는 능력이다.', 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    return "0301010101_23v1"


class NcsReferenceTests(unittest.TestCase):
    def test_env_reader_uses_last_non_empty_study_module_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "NCS_STUDY_MODULE_SERVICE_KEY=\n"
                "NCS_LEARNING_MODULE_SERVICE_KEY=\n"
                "NCS_STUDY_MODULE_SERVICE_KEY=real-key\n",
                encoding="utf-8",
            )
            values = read_env_values(env_path)
            self.assertEqual(values["NCS_STUDY_MODULE_SERVICE_KEY"], "real-key")

    def test_load_settings_reads_qualification_service_key(self) -> None:
        keys = [
            "NCS_QUALIFICATION_SERVICE_KEY",
            "NCS_NCS_CL_CD_JM_SERVICE_KEY",
            "NCS_SERVICE_KEY",
        ]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ["NCS_QUALIFICATION_SERVICE_KEY"] = "qualification-key"
            self.assertEqual(load_settings().qualification_service_key, "qualification-key")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_load_settings_reads_job_base_service_key(self) -> None:
        keys = [
            "NCS_JOB_BASE_SERVICE_KEY",
            "NCS_JOB_BASE_COMPETENCY_SERVICE_KEY",
            "NCS_SERVICE_KEY",
        ]
        previous = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ.pop(key, None)
            os.environ["NCS_JOB_BASE_SERVICE_KEY"] = "job-base-key"
            self.assertEqual(load_settings().job_base_service_key, "job-base-key")
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_svg_text_coordinates_import_and_linking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            html_path = Path(tmp) / "report.html"
            html_path.write_text(
                """
                <html><body>
                <svg width="600" height="800">
                  <text x="120" y="10">뒤</text>
                  <text x="10" y="10">앞</text>
                  <text x="10" y="30">인력채용 0202020202_23v1 채용계획 수립하기 채용절차</text>
                </svg>
                </body></html>
                """,
                encoding="utf-8",
            )
            conn = connect(db_path)
            initialize_database(conn)
            seed_hiring_unit(conn)

            imported = import_ncs_reference_html(conn, html_path, title="NCS report")
            page = conn.execute("SELECT text FROM ncs_reference_pages").fetchone()["text"]
            self.assertTrue(page.startswith("앞뒤\n인력채용"))

            extracted = extract_ncs_reference_entities(conn, document_id=imported["document_id"])
            linked = link_reference_entities_to_ncs(conn, document_id=imported["document_id"])
            conn.close()

            self.assertGreaterEqual(extracted["entities_inserted"], 3)
            self.assertGreaterEqual(linked["links_by_target"]["ncs_competency_unit"], 1)

    def test_reference_chunk_search_returns_summary_and_location_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            html_path = Path(tmp) / "report.html"
            html_path.write_text(
                "<svg><text x='1' y='1'>인력채용 핵심 요약 "
                + ("채용절차 " * 80)
                + " FULL_UNSAFE_TEXT_MARKER</text></svg>",
                encoding="utf-8",
            )
            import_ncs_reference_html(conn, html_path, title="NCS report")
            results = search_ncs_reference_chunks(conn, query="인력채용", limit=5)
            conn.close()

            self.assertEqual(len(results), 1)
            self.assertIn("location", results[0])
            self.assertNotIn("text", results[0])
            self.assertNotIn("FULL_UNSAFE_TEXT_MARKER", str(results[0]))

    def test_hiring_query_resolves_unit_and_trusted_links_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_hiring_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO learning_module_unit_links(
                    learn_module_seq, unit_code, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES ('LM-HIRE-1', ?, 'test_candidate', 0.99,
                          'candidate link must not be trusted', 'auto_candidate', ?, ?)
                """,
                (unit_code, timestamp, timestamp),
            )

            target = resolve_ncs_unit_target(conn, query="인력채용", major_code="02")
            candidate_result = recommend_learning_modules_by_ncs(
                conn,
                query="인력채용",
                major_code="02",
                trust_mode="trusted",
                save=False,
            )
            link_id = conn.execute("SELECT link_id FROM learning_module_unit_links").fetchone()["link_id"]
            reviewed = review_learning_module_ncs_link(
                conn,
                link_id=link_id,
                review_status="accepted",
                reviewer_id="tester",
            )
            trusted_result = recommend_learning_modules_by_ncs(
                conn,
                query="인력채용",
                major_code="02",
                trust_mode="trusted",
                save=True,
            )
            saved_evidence = conn.execute(
                "SELECT COUNT(*) FROM education_recommendation_evidence WHERE run_id = ?",
                (trusted_result["recommendation_run_id"],),
            ).fetchone()[0]
            ksa_raw = conn.execute("SELECT ksa_text_raw FROM ksa_items").fetchone()["ksa_text_raw"]
            conn.close()

            self.assertEqual(target["unit_code"], unit_code)
            self.assertIsNone(candidate_result["recommendations"][0]["learn_module_seq"])
            self.assertTrue(reviewed["recommendation_eligible"])
            self.assertEqual(trusted_result["recommendations"][0]["learn_module_seq"], "LM-HIRE-1")
            self.assertGreaterEqual(saved_evidence, 1)
            self.assertEqual(ksa_raw, "채용절차")

    def test_derived_learning_plan_is_trusted_fallback_item(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_hiring_unit(conn)

            built = build_ncs_derived_learning_plans(
                conn,
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_code="01",
            )
            result = recommend_learning_modules_by_ncs(
                conn,
                query="인력채용",
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_code="01",
                trust_mode="trusted",
                save=False,
            )
            concept_name = conn.execute("SELECT concept_name FROM ontology_concepts LIMIT 1").fetchone()[0]
            concept_result = recommend_education_by_concepts(
                conn,
                concepts=[concept_name],
                trust_mode="trusted",
                save=False,
            )
            concept_link_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM learning_module_concept_links
                WHERE learn_module_seq = ?
                  AND review_status = 'reviewed'
                """,
                (f"NCS-DERIVED-{unit_code}",),
            ).fetchone()[0]
            conn.close()

            self.assertEqual(built["derived_plans_upserted"], 1)
            self.assertEqual(
                result["recommendations"][0]["learn_module_seq"],
                f"NCS-DERIVED-{unit_code}",
            )
            self.assertEqual(result["recommendations"][0]["recommendation_type"], "ncs_derived")
            self.assertEqual(result["recommendation_summary"]["recommended_official_modules_count"], 0)
            self.assertEqual(result["recommendation_summary"]["recommended_derived_plans_count"], 1)
            self.assertGreaterEqual(concept_link_count, 1)
            self.assertEqual(
                concept_result["recommendations"][0]["learn_module_seq"],
                f"NCS-DERIVED-{unit_code}",
            )

    def test_build_learning_module_links_skips_ncs_derived_plans(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_hiring_unit(conn)
            build_ncs_derived_learning_plans(
                conn,
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_code="01",
            )

            summary = build_learning_module_links(
                conn,
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_code="01",
            )
            derived_generated_links = conn.execute(
                """
                SELECT COUNT(*)
                FROM learning_module_unit_links
                WHERE learn_module_seq LIKE 'NCS-DERIVED-%'
                  AND link_method = 'classification_code'
                """
            ).fetchone()[0]
            conn.close()

            self.assertEqual(summary["module_count"], 1)
            self.assertEqual(derived_generated_links, 0)

    def test_report_training_courses_link_to_ontology_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_hiring_unit(conn)
            timestamp = now_utc()
            concept_name = "\ucc44\uc6a9\uc804\ub7b5\ubd84\uc11d"
            conn.execute(
                """
                UPDATE ontology_concepts
                SET concept_name = ?,
                    normalized_key = ?,
                    updated_at = ?
                WHERE concept_id = (SELECT concept_id FROM ontology_concepts LIMIT 1)
                """,
                (concept_name, normalize_concept_key(concept_name), timestamp),
            )
            html_path = Path(tmp) / "report.html"
            table_title = "\uad50\uc721\ud6c8\ub828\uacfc\uc815"
            course_name = "\ucc44\uc6a9\uacfc\uc815"
            department = "\uacbd\uc601\ud559\uacfc"
            level = "\uc804\ubb38\ub300"
            learns = "\ub0b4\uc6a9\uc744 \ud559\uc2b5\ud55c\ub2e4."
            hiring_practice = "\uc778\uc0ac \ucc44\uc6a9 \uc2e4\ubb34\ub97c \ud559\uc2b5\ud55c\ub2e4."
            html_path.write_text(
                f"""
                <html><body>
                <svg width="1800" height="1400">
                  <text x="1117" y="48">{table_title}</text>
                  <text x="1079" y="220">{course_name}</text>
                  <text x="679" y="220">{department}</text>
                  <text x="870" y="220">{level}</text>
                  <text x="1272" y="187">{concept_name} {learns}</text>
                  <text x="1272" y="244">{hiring_practice}</text>
                </svg>
                </body></html>
                """,
                encoding="utf-8",
            )
            imported = import_ncs_reference_html(conn, html_path, title="Training report")

            built = build_report_training_courses(
                conn,
                document_id=imported["document_id"],
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_code="01",
            )
            report_modules = conn.execute(
                "SELECT COUNT(*) FROM ncs_learning_modules WHERE learn_module_seq LIKE 'REPORT-TRAINING-%'"
            ).fetchone()[0]
            concept_links = conn.execute(
                "SELECT COUNT(*) FROM learning_module_concept_links WHERE learn_module_seq LIKE 'REPORT-TRAINING-%'"
            ).fetchone()[0]
            concept_result = recommend_education_by_concepts(
                conn,
                concepts=[concept_name],
                trust_mode="trusted",
                save=False,
            )
            conn.close()

            self.assertEqual(built["report_training_courses_upserted"], 1)
            self.assertEqual(report_modules, 1)
            self.assertGreaterEqual(concept_links, 1)
            self.assertTrue(
                concept_result["recommendations"][0]["learn_module_seq"].startswith("REPORT-TRAINING-")
            )
            self.assertEqual(concept_result["recommendations"][0]["recommendation_type"], "report_training_course")

    def test_scope_filters_reference_entities_and_auto_reviews_exact_names_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_hiring_unit(conn)
            out_of_scope = seed_out_of_scope_unit(conn)
            html_path = Path(tmp) / "report.html"
            html_path.write_text(
                """
                <svg>
                  <text x='1' y='1'>인력채용 0202020202_23v1 채용절차</text>
                  <text x='1' y='20'>금융고객관리 0301010101_23v1</text>
                </svg>
                """,
                encoding="utf-8",
            )
            imported = import_ncs_reference_html(conn, html_path, title="Scoped report")
            extract_ncs_reference_entities(
                conn,
                document_id=imported["document_id"],
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_codes=["01", "02"],
            )
            link_reference_entities_to_ncs(
                conn,
                document_id=imported["document_id"],
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_codes=["01", "02"],
            )
            out_links = conn.execute(
                """
                SELECT COUNT(*)
                FROM ncs_reference_entity_links
                WHERE target_type = 'ncs_competency_unit'
                  AND target_id = ?
                """,
                (out_of_scope,),
            ).fetchone()[0]
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO learning_module_unit_links(
                    learn_module_seq, unit_code, link_method, confidence_score,
                    evidence_text, review_status, created_at, updated_at
                ) VALUES ('LM-HIRE-1', ?, 'module_text_unit_name', 0.85,
                          'Module text mentions unit name: 인력채용', 'auto_linked', ?, ?)
                """,
                (unit_code, timestamp, timestamp),
            )
            reviewed = review_exact_learning_module_name_links(
                conn,
                major_code="02",
                middle_code="02",
                small_code="02",
                sub_codes=["01", "02"],
                reviewer_id="tester",
            )
            review_status = conn.execute(
                "SELECT review_status FROM learning_module_unit_links WHERE unit_code = ?",
                (unit_code,),
            ).fetchone()["review_status"]
            conn.close()

            self.assertEqual(out_links, 0)
            self.assertEqual(reviewed["reviewed_count"], 1)
            self.assertEqual(review_status, "reviewed")


if __name__ == "__main__":
    unittest.main()
