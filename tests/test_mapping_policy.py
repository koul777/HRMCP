from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import extract_sqf_items, upsert_sqf_items
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.evaluation import run_evaluation
from ncs_mcp.mapping_policy import apply_mapping_filter
from ncs_mcp.ontology import analyze_sqf_gap, build_sqf_mapping_candidates


def seed_sqf_ncs_fixture(conn) -> str:
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("02", "경영·회계·사무", "02", "총무·인사", "03", "일반사무", "02", "사무행정"),
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
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
            "사무행정 문서를 작성하고 자료를 정리하는 능력이다.",
            "matched",
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(unit_code, element_no, element_code_raw, element_name_raw, element_level_raw)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("0202030201_22v3", "1", "0202030201_22v3 1", "문서 작성하기", "2"),
    )
    element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
    conn.execute(
        "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, ?, ?)",
        (element_id, "1", "목적에 맞게 문서를 작성할 수 있다."),
    )
    conn.execute(
        """
        INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
        VALUES (?, ?, ?, ?, ?)
        """,
        (element_id, "01", "지식", "1", "문서작성 원칙"),
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
                "dutyDef": "기안 문서를 작성하고 관리하는 일",
                "dutyLevelDef": "기안 문서 작성과 사무환경 관리를 수행하는 수준",
            }
        ],
        "dataInfo": {"code": "000", "message": "OK"},
    }
    upsert_sqf_items(conn, extract_sqf_items(payload))
    return conn.execute("SELECT source_key FROM sqf_duties").fetchone()["source_key"]


class MappingPolicyTests(unittest.TestCase):
    def test_mapping_filter_excludes_low_related_and_rejected(self) -> None:
        matches = [
            {"mapping": {"target_id": "A", "score": 8, "relation": "partiallyCovers", "review_status": "candidate"}},
            {"mapping": {"target_id": "B", "score": 6, "relation": "partiallyCovers", "review_status": "candidate"}},
            {"mapping": {"target_id": "C", "score": 9, "relation": "related", "review_status": "candidate"}},
            {"mapping": {"target_id": "D", "score": 99, "relation": "closeMatch", "review_status": "rejected"}},
            {"mapping": {"target_id": "E", "score": 3, "relation": "related", "review_status": "accepted"}},
        ]

        result = apply_mapping_filter(matches)

        self.assertEqual([item["mapping"]["target_id"] for item in result["matches"]], ["E", "A"])
        self.assertEqual(result["metadata"]["used_mapping_count"], 2)
        self.assertEqual(result["metadata"]["excluded_mapping_count"], 3)
        self.assertEqual(result["metadata"]["exclusion_reasons"]["score_below_threshold"], 1)
        self.assertEqual(result["metadata"]["exclusion_reasons"]["relation:related"], 1)
        self.assertEqual(result["metadata"]["exclusion_reasons"]["rejected"], 1)

    def test_gap_analysis_uses_mapping_quality_gate_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            source_key = seed_sqf_ncs_fixture(conn)
            build_sqf_mapping_candidates(conn, limit_per_duty=5)
            conn.execute(
                """
                UPDATE sqf_ncs_matches
                SET score = 5, relation = 'related'
                WHERE source_id = ?
                """,
                (source_key,),
            )
            conn.commit()

            gap = analyze_sqf_gap(conn, current_ncs_unit_codes=[], target_source_key=source_key)

            self.assertEqual(gap["required_unit_count"], 0)
            self.assertEqual(gap["metadata"]["used_mapping_count"], 0)
            self.assertGreaterEqual(gap["metadata"]["excluded_mapping_count"], 1)
            self.assertIn("relation:related", gap["metadata"]["exclusion_reasons"])
            conn.close()

    def test_recommend_education_falls_back_to_ncs_learning_objectives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            source_key = seed_sqf_ncs_fixture(conn)
            build_sqf_mapping_candidates(conn, limit_per_duty=5)
            conn.close()
            os.environ["NCS_DB_PATH"] = str(db_path)
            from ncs_mcp.server import recommend_education_for_duty

            result = recommend_education_for_duty("사무행정", major_code="02", limit=1)

            recommendation = result["recommendations"][0]
            self.assertIn(recommendation["recommendation_type"], {"ncs_derived", "mixed"})
            self.assertEqual(recommendation["source_sqf_fields"], {})
            self.assertGreaterEqual(len(recommendation["learning_objectives"]), 1)
            self.assertEqual(recommendation["metadata"]["used_refined_policy"], "refined_if_approved")
            self.assertEqual(recommendation["metadata"]["used_mapping_count"], 1)
            self.assertIsNotNone(source_key)

    def test_evaluation_records_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            seed_sqf_ncs_fixture(conn)
            build_sqf_mapping_candidates(conn, limit_per_duty=5)
            conn.close()

            metrics = run_evaluation(db_path, scope_tag="management_support", run_name="test")

            conn = connect(db_path)
            initialize_database(conn)
            saved = conn.execute("SELECT COUNT(*) AS count FROM evaluation_runs").fetchone()["count"]
            conn.close()
            self.assertEqual(saved, 1)
            self.assertEqual(metrics["scope_tag"], "management_support")
            self.assertGreaterEqual(metrics["mapping_count"], 1)


if __name__ == "__main__":
    unittest.main()
