from __future__ import annotations

import sqlite3
import unittest

from scripts.benchmark_ncs_single_pass_search import (
    _gate,
    candidate_search,
    escape_like,
    normalize_query,
    stable_ids,
)


class SinglePassSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT, major_name TEXT,
                middle_code TEXT, middle_name TEXT,
                small_code TEXT, small_name TEXT,
                sub_code TEXT, sub_name TEXT,
                duty_order INTEGER
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                api_definition TEXT,
                unit_level_raw TEXT,
                classification_id INTEGER
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY,
                element_name_raw TEXT,
                unit_code TEXT
            );
            CREATE TABLE performance_criteria (
                criteria_id INTEGER PRIMARY KEY,
                criteria_text_raw TEXT,
                criteria_text_refined TEXT,
                element_id INTEGER
            );
            CREATE TABLE ksa_items (
                ksa_id INTEGER PRIMARY KEY,
                ksa_type_name TEXT,
                ksa_text_raw TEXT,
                ksa_text_refined TEXT,
                element_id INTEGER
            );
            CREATE TABLE ncs_query_aliases (
                alias_text TEXT,
                normalized_query TEXT,
                unit_code TEXT
            );
            INSERT INTO classifications VALUES
                (1, '02', '경영 회계 사무', '0202', '총무 인사', '020202', '인사 조직', '02020201', '인사', 1);
            INSERT INTO competency_units VALUES
                ('U01', '채용 계획 수립', '신입사원 채용과 면접 계획을 수립한다', '4', 1),
                ('U02', '채용 면접 운영', '면접 평가를 운영한다', '4', 1),
                ('U03', '교육훈련 운영', '교육과정을 운영한다', '3', 1);
            INSERT INTO competency_elements VALUES
                (11, '신입사원 채용 면접 준비', 'U01'),
                (12, '채용 면접 실시', 'U02'),
                (13, '교육과정 운영', 'U03');
            INSERT INTO performance_criteria VALUES
                (21, '채용 기준에 따라 면접 질문을 작성할 수 있다', '채용 면접 질문 작성', 11),
                (22, '신입사원 면접 결과를 평가할 수 있다', '신입사원 면접 평가', 12),
                (23, '교육 결과를 분석할 수 있다', '교육 분석', 13);
            INSERT INTO ksa_items VALUES
                (31, '지식', '채용 절차에 대한 지식', '채용 절차 지식', 11),
                (32, '기술', '면접 질문 작성 기술', '면접 질문 작성', 12),
                (33, '태도', '교육 운영 담당자의 책임감', '교육 운영 책임', 13);
            INSERT INTO ncs_query_aliases VALUES ('HR 채용', '채용', 'U01');
            """
        )

    def tearDown(self) -> None:
        self.conn.close()

    def test_normalization_and_like_escaping(self) -> None:
        phrase, tokens = normalize_query("  채용/면접_A-B  ")
        self.assertEqual(phrase, "채용 면접_A B")
        self.assertEqual(tokens, ["채용", "면접_A", "B"])
        self.assertEqual(escape_like(r"100%_\x"), r"100\%\_\\x")

    def test_one_statement_per_type_and_two_syllable_query(self) -> None:
        payload = candidate_search(self.conn, "채용", scope="all", limit=8)
        self.assertEqual(payload["_sql_statement_count"], 4)
        self.assertEqual(set(payload["counts_by_type"]), set(("unit", "element", "criteria", "ksa")))
        self.assertGreaterEqual(payload["returned"], 4)
        self.assertTrue(all("채용" in item["matched_tokens"] for item in payload["results"]))

    def test_punctuation_multi_token_and_determinism(self) -> None:
        first = candidate_search(self.conn, "신입사원/채용-면접", scope="all", limit=8)
        second = candidate_search(self.conn, "신입사원/채용-면접", scope="all", limit=8)
        self.assertEqual(stable_ids(first), stable_ids(second))
        self.assertEqual(first["match_mode"], "mixed")
        self.assertTrue(any(item["matched_token_count"] >= 2 for item in first["results"]))

    def test_scope_offset_and_injection_text_are_bounded(self) -> None:
        first = candidate_search(self.conn, "채용", scope="unit", limit=1, offset=0)
        second = candidate_search(self.conn, "채용", scope="unit", limit=1, offset=1)
        self.assertEqual(first["_sql_statement_count"], 1)
        self.assertEqual(second["_sql_statement_count"], 1)
        self.assertNotEqual(stable_ids(first), stable_ids(second))
        hostile = candidate_search(self.conn, "채용%' OR 1=1 --", scope="unit", limit=100)
        self.assertLessEqual(hostile["returned"], 3)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0], 3)

    def test_gate_rejects_slow_or_low_overlap_candidate(self) -> None:
        aggregate = {
            "latency": {
                "baseline": {"p50_ms": 100.0, "p95_ms": 1500.0},
                "candidate": {"p50_ms": 90.0, "p95_ms": 1400.0},
            },
            "rss": {
                "baseline": {"max_bytes": 100},
                "candidate": {"max_bytes": 100},
            },
        }
        record = {
            "top10_overlap": 0.7,
            "baseline": {"zero_hit": False, "deterministic": True, "proxies": {"aggregate_risk_proxy": 0.1}},
            "candidate": {"zero_hit": False, "deterministic": True, "proxies": {"aggregate_risk_proxy": 0.1}},
        }
        gate = _gate([record], aggregate)
        self.assertFalse(gate["performance_gate_pass"])
        self.assertEqual(gate["promotion_verdict"], "do_not_promote_gate_failed")


if __name__ == "__main__":
    unittest.main()
