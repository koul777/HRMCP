from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.ontology import analyze_sqf_gap, build_sqf_mapping_candidates
from ncs_mcp.collect_api import extract_sqf_items, upsert_sqf_items
from ncs_mcp.preprocess_excel import preprocess_excel


HEADERS = [
    "대분류코드",
    "대분류코드명",
    "중분류코드",
    "중분류코드명",
    "소분류코드",
    "소분류코드명",
    "세분류코드",
    "세분류코드명",
    "능력단위분류번호",
    "능력단위명칭",
    "수준",
    "능력단위요소번호",
    "능력단위요소명",
    "능력단위요소수준",
    "수행준거번호",
    "수행준거",
    "지식기술태도코드",
    "지식기술태도코드명",
    "지식기술태도번호",
    "지식기술태도의의",
]


def make_sample_excel(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "02"
    ws.append(HEADERS)
    base = [
        "02",
        "경영·회계·사무",
        "02",
        "총무·인사",
        "02",
        "인사·조직",
        "01",
        "인사",
        "0202020101_23v3",
        "인사기획",
        6,
        "0202020101_23v3 1",
        "인사전략 수립하기",
        6,
    ]
    ws.append(
        base
        + [
            1,
            "조직의 전략방향을 고려하여 인사전략을 수립할 수 있다.",
            "01",
            "지식",
            1,
            "인사전략",
        ]
    )
    ws.append(
        base
        + [
            1,
            "조직의 전략방향을 고려하여 인사전략을 수립할 수 있다.",
            "02",
            "기술",
            1,
            "자료분석 능력",
        ]
    )
    wb.save(path)


class NcsMcpTests(unittest.TestCase):
    def test_schema_initializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("competency_units", tables)
            self.assertIn("quality_issues", tables)
            self.assertIn("ontology_concepts", tables)
            self.assertIn("ksa_concept_links", tables)
            self.assertIn("sqf_duties", tables)
            self.assertIn("sqf_ncs_matches", tables)
            self.assertIn("sqf_library_posts", tables)
            self.assertIn("sqf_library_files", tables)
            self.assertIn("sqf_document_sources", tables)
            self.assertIn("sqf_framework_concepts", tables)
            self.assertIn("sqf_industry_sectors", tables)
            self.assertIn("sqf_jobs_normalized", tables)
            self.assertIn("sqf_job_levels_normalized", tables)
            self.assertIn("sqf_recognition_evidence", tables)
            self.assertIn("sqf_document_assets", tables)
            self.assertIn("sqf_document_pages", tables)
            self.assertIn("sqf_document_chunks", tables)
            self.assertIn("sqf_chunk_job_level_matches", tables)
            self.assertIn("sqf_document_evidence_links", tables)
            self.assertIn("ncs_reference_documents", tables)
            self.assertIn("ncs_reference_pages", tables)
            self.assertIn("ncs_reference_chunks", tables)
            self.assertIn("ncs_reference_entities", tables)
            self.assertIn("ncs_reference_entity_links", tables)
            self.assertIn("review_audit_log", tables)
            self.assertIn("evaluation_runs", tables)
            self.assertIn("ncs_query_aliases", tables)
            self.assertIn("training_transition_gold_scenarios", tables)
            self.assertIn("training_transition_scenario_reviews", tables)
            self.assertIn("ncs_career_paths", tables)
            self.assertIn("ncs_qualification_items", tables)
            self.assertIn("ncs_unit_qualification_links", tables)
            self.assertIn("ncs_job_base_competencies", tables)
            self.assertIn("ncs_job_base_factors", tables)
            self.assertIn("ncs_unit_job_base_links", tables)
            review_defaults = {}
            for table in (
                "ncs_training_course_unit_links",
                "ncs_unit_qualification_links",
                "ncs_unit_job_base_links",
            ):
                columns = {
                    row["name"]: row["dflt_value"]
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                review_defaults[table] = columns["review_status"]
            self.assertEqual(
                review_defaults,
                {
                    "ncs_training_course_unit_links": "'auto_linked'",
                    "ncs_unit_qualification_links": "'auto_linked'",
                    "ncs_unit_job_base_links": "'auto_linked'",
                },
            )
            audit_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(review_audit_log)").fetchall()
            }
            self.assertTrue(
                {
                    "source_decision_packet",
                    "source_artifact_hash",
                    "rationale",
                    "evidence_refs_json",
                    "created_by_tool",
                    "run_artifact",
                }.issubset(audit_columns)
            )
            alias_count = conn.execute("SELECT COUNT(*) FROM ncs_query_aliases").fetchone()[0]
            recruiting_aliases = conn.execute(
                """
                SELECT alias_text, normalized_query, unit_code
                FROM ncs_query_aliases
                WHERE alias_text IN ('채용', '인력채용')
                   OR normalized_query = '인력채용'
                ORDER BY alias_text
                """
            ).fetchall()
            scenario_count = conn.execute(
                "SELECT COUNT(*) FROM training_transition_gold_scenarios"
            ).fetchone()[0]
            self.assertGreaterEqual(alias_count, 1)
            self.assertEqual(
                {row["unit_code"] for row in recruiting_aliases},
                {"0202020103_23v4"},
            )
            self.assertGreaterEqual(scenario_count, 1)
            conn.close()

    def test_sqf_payload_upserts_duties(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            payload = {
                "data": {
                    "row": {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "management",
                        "sqfFldCdnm": "HR",
                        "jobCdnm": "HR specialist",
                        "dutyNm": "HR planning",
                        "dutyLevel": "5",
                        "dutyLevelNm": "manager",
                        "dutyDef": "Plan workforce strategy.",
                        "autoResp": "Works with autonomy.",
                        "dutyEduTrain": "HR analytics training",
                        "dutyQualf": "HR certificate",
                        "dutyCarr": "3 years",
                    }
                },
                "dataInfo": {
                    "totCnt": "1",
                    "code": "00",
                    "message": "OK",
                    "totalPage": "1",
                    "pageNo": "1",
                    "numOfRows": "10",
                },
            }

            items = extract_sqf_items(payload)
            self.assertEqual(len(items), 1)
            self.assertEqual(upsert_sqf_items(conn, items), 1)
            row = conn.execute("SELECT * FROM sqf_duties").fetchone()
            self.assertEqual(row["ncs_lclas_cd"], "02")
            self.assertEqual(row["duty_name"], "HR planning")
            self.assertEqual(row["duty_education_training"], "HR analytics training")
            conn.close()

    def test_sqf_ncs_mapping_candidates_support_gap_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "02",
                    "경영·회계·사무",
                    "02",
                    "총무·인사",
                    "03",
                    "일반사무",
                    "02",
                    "사무행정",
                ),
            )
            classification_id = conn.execute(
                "SELECT classification_id FROM classifications"
            ).fetchone()["classification_id"]
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "0202030201_22v3",
                    "0202030201",
                    "22v3",
                    "문서 작성",
                    "2",
                    classification_id,
                    "문서 작성은 자료를 조사·정리하여 목적에 맞는 문서를 완성하는 능력이다.",
                    "matched",
                    timestamp,
                    timestamp,
                ),
            )
            payload = {
                "data": [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "경영·회계·사무",
                        "sqfFldCdnm": "경영관리",
                        "jobCdnm": "경영지원",
                        "dutyNm": "사무행정(2)",
                        "dutyLevel": "2",
                        "dutyLevelNm": "초급 사무행정",
                        "dutyDef": "구성원들의 업무 보조를 위한 기안 문서를 작성하고 관리하는 일",
                        "dutyLevelDef": "기안 문서 작성과 사무환경 관리를 수행하는 수준",
                    }
                ],
                "dataInfo": {"code": "000", "message": "OK"},
            }
            upsert_sqf_items(conn, extract_sqf_items(payload))
            source_key = conn.execute("SELECT source_key FROM sqf_duties").fetchone()[
                "source_key"
            ]

            summary = build_sqf_mapping_candidates(conn, limit_per_duty=5)
            self.assertGreaterEqual(summary["candidates_upserted"], 1)
            match = conn.execute("SELECT * FROM sqf_ncs_matches").fetchone()
            self.assertEqual(match["source_id"], source_key)
            self.assertEqual(match["target_id"], "0202030201_22v3")
            self.assertEqual(match["review_status"], "candidate")

            gap = analyze_sqf_gap(
                conn,
                current_ncs_unit_codes=[],
                target_source_key=source_key,
            )
            self.assertEqual(gap["missing_unit_count"], 1)
            self.assertEqual(gap["coverage_ratio"], 0.0)
            covered = analyze_sqf_gap(
                conn,
                current_ncs_unit_codes=["0202030201_22v3"],
                target_source_key=source_key,
            )
            self.assertEqual(covered["covered_unit_count"], 1)
            self.assertEqual(covered["coverage_ratio"], 1.0)
            conn.close()

    def test_preprocess_deduplicates_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            excel_path = tmp_path / "sample.xlsx"
            db_path = tmp_path / "ncs.db"
            reports_dir = tmp_path / "reports"
            make_sample_excel(excel_path)

            summary = preprocess_excel(
                excel_path=excel_path,
                db_path=db_path,
                reports_dir=reports_dir,
                reset=True,
            )

            counts = summary["counts"]
            self.assertEqual(counts["raw_excel_rows"], 2)
            self.assertEqual(counts["competency_units"], 1)
            self.assertEqual(counts["competency_elements"], 1)
            self.assertEqual(counts["performance_criteria"], 1)
            self.assertEqual(counts["ksa_items"], 2)
            self.assertEqual(counts["element_criteria_ksa_links"], 2)

    def test_server_get_unit_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            excel_path = tmp_path / "sample.xlsx"
            db_path = tmp_path / "ncs.db"
            make_sample_excel(excel_path)
            preprocess_excel(
                excel_path=excel_path,
                db_path=db_path,
                reports_dir=tmp_path / "reports",
                reset=True,
            )
            os.environ["NCS_DB_PATH"] = str(db_path)
            from ncs_mcp.server import get_unit_structure

            result = get_unit_structure("0202020101_23v3")
            self.assertEqual(result["unit"]["unit_name"], "인사기획")
            self.assertEqual(len(result["elements"]), 1)
            self.assertEqual(len(result["elements"][0]["performance_criteria"]), 1)
            self.assertEqual(len(result["elements"][0]["ksa"]), 2)


if __name__ == "__main__":
    unittest.main()
