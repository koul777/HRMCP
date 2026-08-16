from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.quality import effective_text, is_standard_criteria_expression, is_typo_false_positive, run_quality_checks


class QualityRuleTests(unittest.TestCase):
    def test_typo_false_positive_whitelist_keeps_domain_terms(self) -> None:
        self.assertTrue(is_typo_false_positive("자가가구 조사표에 준하여 조사하는 능력"))
        self.assertTrue(is_typo_false_positive("진행자와 보조진행자의 의견을 취합한다"))
        self.assertTrue(is_typo_false_positive("열교환기 구조진동해석에 관한 지식"))

    def test_typo_false_positive_whitelist_does_not_hide_known_candidates(self) -> None:
        self.assertFalse(is_typo_false_positive("조진문화 진단하기"))
        self.assertFalse(is_typo_false_positive("전직지원자가가 추구하는 요구내용"))
        self.assertFalse(is_typo_false_positive("자격요견을 확인한다"))

    def test_standard_criteria_expression_accepts_common_ncs_forms(self) -> None:
        self.assertTrue(is_standard_criteria_expression("광고 호감도를 높이는 결과물을 만들어 낼 수 있다."))
        self.assertTrue(is_standard_criteria_expression("기기의 조정법을 알고 있다."))
        self.assertTrue(is_standard_criteria_expression("작품 연출 의도나 방향에 관해 연출가와 논의한다."))
        self.assertTrue(is_standard_criteria_expression("PLC 하드웨어 작동 상황을 모니터링 할 수 있어야 한다."))
        self.assertTrue(is_standard_criteria_expression("전체 프로세스를 수립할 수있다."))

    def test_standard_criteria_expression_keeps_malformed_candidates(self) -> None:
        self.assertFalse(is_standard_criteria_expression("."))
        self.assertFalse(is_standard_criteria_expression("IT기술교육 운영계획수립이란 교육 운영을 준비하는 능력이다."))
        self.assertFalse(is_standard_criteria_expression("운영방식을 결정할 수 있"))
        self.assertFalse(is_standard_criteria_expression("소프트웨어의 유형을 정의할 수 있다,"))

    def test_effective_text_prefers_refined_value(self) -> None:
        self.assertEqual(effective_text("조진문화 진단하기", "조직문화 진단하기"), "조직문화 진단하기")
        self.assertEqual(effective_text("원문", ""), "원문")

    def test_quality_checks_do_not_reopen_fixed_refined_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', '경영', '02', '인사', '02', '인사조직', '01', '인사기획')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, created_at, updated_at
                ) VALUES ('0202020101_23v3', '0202020101', '23v3', '인사기획',
                          '5', ?, ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw,
                    element_name_refined, element_level_raw
                ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1',
                          '조진문화 진단하기', '조직문화 진단하기', '5')
                """
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(
                    element_id, criteria_no, criteria_text_raw, criteria_text_refined
                ) VALUES (?, '1',
                          '전직지원자가가 요구내용을 확인할 수 있다.',
                          '전직지원자가 요구내용을 확인할 수 있다.')
                """,
                (element_id,),
            )
            conn.execute(
                """
                INSERT INTO performance_criteria(
                    element_id, criteria_no, criteria_text_raw, criteria_text_refined
                ) VALUES (?, '2', '업무를 수행할 수 있다', '업무를 수행할 수 있다.')
                """,
                (element_id,),
            )
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '3', '운영방식을 결정할 수 있')
                """,
                (element_id,),
            )
            conn.execute(
                """
                INSERT INTO ksa_items(
                    element_id, ksa_type_code, ksa_type_name, ksa_no,
                    ksa_text_raw, ksa_text_refined
                ) VALUES (?, 'A', '태도', '1',
                          '피자문자가가 통제할 수 있다고 보는 태도',
                          '피자문자가 통제할 수 있다고 보는 태도')
                """,
                (element_id,),
            )
            conn.commit()
            conn.close()

            counts = run_quality_checks(db_path, reports_dir)
            conn = connect(db_path)
            try:
                suspected_typo = conn.execute(
                    "SELECT COUNT(*) FROM quality_issues WHERE issue_type='suspected_typo'"
                ).fetchone()[0]
                criteria_issues = conn.execute(
                    "SELECT issue_detail FROM quality_issues WHERE issue_type='criteria_format_issue'"
                ).fetchall()
            finally:
                conn.close()

        self.assertEqual(counts["suspected_typo"], 0)
        self.assertEqual(suspected_typo, 0)
        self.assertEqual(len(criteria_issues), 2)
        self.assertTrue(any("운영방식을 결정할 수 있" in row[0] for row in criteria_issues))


if __name__ == "__main__":
    unittest.main()
