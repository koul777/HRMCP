from __future__ import annotations

import asyncio
from contextlib import contextmanager
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402


class NcsSearchRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "search.db"
        self.sql_statements: list[str] = []
        conn = self._connect()
        try:
            self._create_schema(conn)
            self._seed(conn)
            conn.commit()
        finally:
            conn.close()
        self.open_db_patch = patch.object(server, "open_db", new=self._open_db)
        self.open_db_patch.start()

    def tearDown(self) -> None:
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _open_db(self):
        conn = self._connect()
        conn.set_trace_callback(self.sql_statements.append)
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT, major_name TEXT,
                middle_code TEXT, middle_name TEXT,
                small_code TEXT, small_name TEXT,
                sub_code TEXT, sub_name TEXT,
                duty_order TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                api_definition TEXT,
                unit_level_raw TEXT,
                classification_id INTEGER
            );
            CREATE TABLE ncs_query_aliases (
                unit_code TEXT,
                alias_text TEXT,
                normalized_query TEXT
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
            """
        )

    @staticmethod
    def _seed(conn: sqlite3.Connection) -> None:
        conn.executemany(
            """
            INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (1, "02", "경영", "02", "인사", "02", "인사관리", "01", "인사조직", "1"),
                (2, "99", "기타", "99", "기타", "99", "기타", "99", "데이터분석 직무", "2"),
                (3, "12", "이용·숙박·여행·오락·스포츠", "04", "스포츠", "04", "스포츠산업", "03", "스포츠구단", "3"),
                (4, "02", "경영·회계·사무", "02", "총무·인사", "01", "총무", "02", "시설총무", "4"),
                (5, "07", "사회복지·종교", "01", "사회복지", "02", "사회복지서비스", "05", "자원봉사관리", "5"),
                (6, "15", "기계", "01", "금속가공", "01", "표면가공", "01", "금속표면가공", "6"),
            ),
        )
        units = (
            ("U_EXACT", "데이터분석", "직무 정의", "5", 1),
            ("U_PREFIX", "데이터분석 실무", "직무 정의", "5", 1),
            ("U_PARTIAL", "인사 데이터분석", "직무 정의", "5", 1),
            ("U_CLASS", "직무분류 검색", "직무 정의", "5", 2),
            ("U_DEFINITION", "정의 검색", "데이터분석 업무를 수행한다", "5", 1),
            ("U_HIRE_1", "채용 운영", "신입사원 선발", "4", 1),
            ("U_HIRE_2", "급여 운영", "급여 업무", "4", 1),
            ("U_ASSET", "자산관리", "자산 취득과 처분을 관리한다", "4", 1),
            ("U_QUALITY", "품질관리", "품질 기준을 관리한다", "4", 1),
            ("U_PERFORMANCE", "성과관리", "성과 목표를 관리한다", "4", 1),
            ("U_HR_MANAGEMENT", "인사관리", "인사 운영을 관리한다", "4", 1),
            ("U_LABOR", "노무관리", "노무 업무를 관리한다", "4", 1),
            ("U_ASCII", "data workflow analysis", "data operations", "4", 1),
            ("U_WAGE", "임금관리", "임금 정책과 보상 기준을 관리한다", "5", 1),
            ("U_SUPPLIES", "비품관리", "사무 비품을 구매하고 관리한다", "4", 1),
            ("U_SECURITY", "총무보안관리", "사옥 출입과 보안을 관리한다", "4", 1),
            ("U_VAT", "부가가치세 신고", "부가가치세 신고 업무를 수행한다", "4", 1),
            ("U_PLAYER", "선수연봉계약", "프로야구 선수의 연봉 협상을 수행한다", "4", 3),
            # Same base code 02020102 20 shared by two 세분류: NCS lets a 세분류
            # borrow a unit developed elsewhere. The borrowed copy keeps the
            # older version tag, so a plain unit_code sort puts it first.
            ("0202010220_19v2", "공용시설관리", "공용 시설을 관리한다", "4", 5),
            ("0202010220_25v3", "공용시설관리", "공용 시설을 관리한다", "4", 4),
            # 처리 spreads across the corpus while 퇴직정산 names one unit, so a
            # lone 퇴직정산 hit has to outrank a lone 처리 hit even though the
            # heat-treatment names are shorter.
            ("U_HEAT_1", "심냉처리", "금속을 냉각한다", "3", 6),
            ("U_HEAT_2", "퀜칭열처리", "금속을 열처리한다", "3", 6),
            ("U_HEAT_3", "진공열처리", "진공에서 열처리한다", "3", 6),
            ("U_SEVERANCE", "퇴직정산지원", "퇴직 정산 업무를 지원한다", "4", 1),
        )
        conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)", units)
        conn.execute(
            "INSERT INTO ncs_query_aliases VALUES ('U_HIRE_1', 'recruiting', 'recruiting')"
        )
        conn.execute(
            "INSERT INTO ncs_query_aliases VALUES ('U_HIRE_1', '채용', '인력채용')"
        )
        elements = (
            (1, "채용 운영", "U_HIRE_1"),
            (2, "급여 운영", "U_HIRE_2"),
            (3, "분석 방법", "U_EXACT"),
            (4, "data workflow analysis", "U_ASCII"),
        )
        conn.executemany("INSERT INTO competency_elements VALUES (?, ?, ?)", elements)
        criteria = (
            (1, "신입사원 면접 절차와 채용 기준을 적용한다", None, 1),
            (2, "채용 운영 계획을 검토한다", None, 1),
            (3, "급여 운영 계획을 검토한다", None, 2),
            (4, "data workflow analysis", None, 4),
        )
        conn.executemany("INSERT INTO performance_criteria VALUES (?, ?, ?, ?)", criteria)
        ksa = (
            (1, "knowledge", "채용 운영 절차 지식", None, 1),
            (2, "skill", "급여 운영 도구 활용 기술", None, 2),
            (3, "skill", "데이터 품질 점검 기술", None, 3),
            (4, "skill", "data workflow analysis", None, 4),
        )
        conn.executemany("INSERT INTO ksa_items VALUES (?, ?, ?, ?, ?)", ksa)

    def test_exact_prefix_phrase_unit_ranking_is_preserved(self) -> None:
        result = server.search_ncs("데이터분석", scope="unit", limit=5)

        self.assertEqual(
            [row["id"] for row in result["results"]],
            ["U_EXACT", "U_PREFIX", "U_PARTIAL", "U_CLASS", "U_DEFINITION"],
        )
        self.assertEqual(result["match_mode"], "phrase")

    def test_ksa_scope_is_public_and_returns_only_ksa(self) -> None:
        result = server.ncs_search("채용", scope="ksa", limit=5)

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["scope"], "ksa")
        self.assertTrue(result["results"])
        self.assertEqual({row["type"] for row in result["results"]}, {"ksa"})

    def test_multiword_query_uses_and_then_or_fallback(self) -> None:
        token_and = server.search_ncs("신입사원 채용 면접", scope="all", limit=10)
        token_or = server.search_ncs("데이터 분석가", scope="all", limit=10)

        self.assertEqual(token_and["match_mode_by_type"]["criteria"], "token_and")
        self.assertIn(1, [row["id"] for row in token_and["results"] if row["type"] == "criteria"])
        self.assertIn(token_or["match_mode"], {"token_or", "mixed"})
        self.assertGreater(token_or["returned"], 0)

    def test_alias_validated_compound_expansion_preserves_and_semantics(self) -> None:
        result = server.search_ncs("인사 채용관리", scope="unit", limit=5)

        self.assertEqual(result["match_mode"], "expanded_token_and")
        self.assertEqual(result["results"][0]["id"], "U_HIRE_1")
        self.assertEqual(
            result["query_expansions"]["채용관리"],
            ["채용", "인력채용"],
        )
        self.assertEqual(result["results"][0]["matched_tokens"], ["인사", "채용관리"])
        self.assertEqual(
            result["results"][0]["matched_expansions"][0]["matched_as"],
            "채용",
        )

    def test_exact_management_terms_outrank_compound_expansion(self) -> None:
        expectations = {
            "자산관리": "U_ASSET",
            "품질관리": "U_QUALITY",
            "성과관리": "U_PERFORMANCE",
            "인사관리": "U_HR_MANAGEMENT",
            "노무관리": "U_LABOR",
        }

        for query, expected_id in expectations.items():
            with self.subTest(query=query):
                result = server.search_ncs(query, scope="unit", limit=5)
                self.assertEqual(result["match_mode"], "phrase")
                self.assertEqual(result["results"][0]["id"], expected_id)
                self.assertEqual(result["query_expansions"], {})

    def test_high_specificity_practitioner_aliases_retrieve_official_units(self) -> None:
        expectations = {
            "연봉 협상 기준": ("U_WAGE", "임금관리"),
            "사무용품 구매 요청": ("U_SUPPLIES", "비품관리"),
            "사옥 보안 점검": ("U_SECURITY", "총무보안관리"),
            "부가세 신고 준비": ("U_VAT", "부가가치세 신고"),
        }

        for query, (expected_id, expected_expansion) in expectations.items():
            with self.subTest(query=query):
                result = server.search_ncs(query, scope="unit", limit=3)
                self.assertEqual(result["match_mode"], "intent_alias")
                self.assertEqual(result["results"][0]["id"], expected_id)
                self.assertIn(expected_expansion, result["query_intent_expansions"])
                self.assertEqual(
                    result["results"][0]["matched_expansions"][0]["matched_as"],
                    expected_expansion,
                )

    def test_intent_aliases_do_not_override_explicit_cross_domain_qualifiers(self) -> None:
        from ncs_mcp.search import core as search_core

        sports = server.search_ncs("프로야구 선수 연봉 협상", scope="unit", limit=3)

        self.assertEqual(sports["results"][0]["id"], "U_PLAYER")
        self.assertEqual(sports["query_intent_expansions"], [])
        self.assertEqual(
            search_core._ncs_search_intent_expansions("자동차 제조 원가 계산"),
            [],
        )
        self.assertEqual(
            search_core._ncs_search_intent_expansions("설비보수 외주 용역"),
            [],
        )

    def test_rare_token_outranks_common_token_in_fallback(self) -> None:
        result = server.search_ncs("퇴직정산 처리", scope="unit", limit=5)

        self.assertEqual(result["results"][0]["id"], "U_SEVERANCE")

    def test_token_idf_weights_scan_the_corpus_once(self) -> None:
        from ncs_mcp.search import core as search_core

        with self._open_db() as conn:
            self.sql_statements.clear()
            search_core._ncs_search_token_idf_weights(
                conn, ["퇴직정산", "처리", "관리", "퇴직정산"]
            )

        # The compact serving profile has no unit_name_raw index, so every LIKE
        # reads the whole table. One statement keeps that at one scan.
        self.assertEqual(len(self.sql_statements), 1)

    def test_token_idf_weights_fall_with_document_frequency(self) -> None:
        from ncs_mcp.search import core as search_core

        with self._open_db() as conn:
            weights = search_core._ncs_search_token_idf_weights(
                conn, ["퇴직정산", "처리"]
            )

        self.assertLess(weights["처리"], weights["퇴직정산"])
        self.assertGreaterEqual(weights["처리"], search_core._NCS_SEARCH_IDF_FLOOR)
        self.assertLessEqual(weights["퇴직정산"], 1.0)

    def test_shared_unit_ranks_home_classification_above_borrowed_copy(self) -> None:
        result = server.search_ncs("공용시설관리", scope="unit", limit=5)

        ids = [row["id"] for row in result["results"]]
        self.assertEqual(ids, ["0202010220_25v3", "0202010220_19v2"])

    def test_borrowing_classification_context_still_surfaces_borrowed_copy(self) -> None:
        result = server.search_ncs("자원봉사관리 공용시설관리", scope="unit", limit=5)

        ids = [row["id"] for row in result["results"]]
        self.assertEqual(ids[0], "0202010220_19v2")

    def test_phrase_hit_skips_lower_tier_sql_for_each_type(self) -> None:
        result = server.search_ncs("data workflow", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "phrase")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [sql for sql in self.sql_statements if f"FROM {table}" in sql]
            self.assertEqual(len(statements), 1, table)

    def test_and_fallback_runs_only_after_empty_phrase_tier(self) -> None:
        result = server.search_ncs("analysis data", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "token_and")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [sql for sql in self.sql_statements if f"FROM {table}" in sql]
            self.assertEqual(len(statements), 2, table)

    def test_or_fallback_runs_only_after_empty_phrase_and_and_tiers(self) -> None:
        result = server.search_ncs("missing data", scope="all", limit=4)

        self.assertEqual(result["match_mode"], "token_or")
        for table in (
            "competency_units cu",
            "competency_elements ce",
            "performance_criteria pc",
            "ksa_items ki",
        ):
            statements = [sql for sql in self.sql_statements if f"FROM {table}" in sql]
            self.assertEqual(len(statements), 3, table)

    def test_all_scope_keeps_each_types_best_available_match_tier(self) -> None:
        result = server.search_ncs("신입사원 면접", scope="all", limit=10)

        modes_by_type = {
            row["type"]: row["match_mode"] for row in result["results"]
        }
        self.assertEqual(result["match_mode"], "mixed")
        self.assertEqual(modes_by_type["criteria"], "phrase")
        self.assertEqual(modes_by_type["unit"], "token_or")
        self.assertEqual(result["match_mode_by_type"]["criteria"], "phrase")
        self.assertEqual(result["match_mode_by_type"]["unit"], "token_or")

    def test_all_scope_balances_types_and_pages_without_overlap(self) -> None:
        first = server.search_ncs("운영", scope="all", limit=4, offset=0)
        second = server.search_ncs("운영", scope="all", limit=4, offset=4)

        self.assertEqual(
            [row["type"] for row in first["results"]],
            ["unit", "element", "criteria", "ksa"],
        )
        self.assertEqual(first["counts_by_type"], {"unit": 1, "element": 1, "criteria": 1, "ksa": 1})
        self.assertEqual(first["next_offset"], 4)
        first_ids = {(row["type"], row["id"]) for row in first["results"]}
        second_ids = {(row["type"], row["id"]) for row in second["results"]}
        self.assertTrue(second_ids)
        self.assertFalse(first_ids.intersection(second_ids))

    def test_match_metadata_and_next_offset_are_rendered(self) -> None:
        result = server.search_ncs("운영", scope="all", limit=4)

        for row in result["results"]:
            self.assertEqual(row["match_mode"], "phrase")
            self.assertEqual(row["matched_tokens"], ["운영"])
            self.assertTrue(row["match_fields"])
        self.assertIn("최대 5건 미리보기", result["markdown_summary"])
        self.assertIn("offset=4", result["markdown_summary"])
        rendered = server._render_ncs_search_markdown(result)
        self.assertIsInstance(rendered, str)
        self.assertIn("offset=4", rendered)

    def test_like_wildcards_do_not_expand_and_offset_is_optional_in_mcp_schema(self) -> None:
        wildcard = server.search_ncs("%", scope="all", limit=20)
        self.assertEqual(wildcard["returned"], 0)

        tools = asyncio.run(server.mcp.list_tools())
        search_tool = next(tool for tool in tools if tool.name == "ncs_search")
        properties = search_tool.inputSchema.get("properties", {})
        self.assertIn("offset", properties)
        self.assertNotIn("offset", search_tool.inputSchema.get("required", []))

    def test_hr_natural_language_queries_rank_expected_units(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                DELETE FROM ksa_items;
                DELETE FROM performance_criteria;
                DELETE FROM competency_elements;
                DELETE FROM ncs_query_aliases;
                DELETE FROM competency_units;
                DELETE FROM classifications;
                """
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        10,
                        "02",
                        "경영·회계·사무",
                        "0202",
                        "총무·인사",
                        "020202",
                        "인사·조직",
                        "02020201",
                        "인사",
                        "1",
                    ),
                    (
                        20,
                        "08",
                        "문화·예술·디자인·방송",
                        "0803",
                        "문화콘텐츠",
                        "080304",
                        "영상제작",
                        "08030402",
                        "촬영",
                        "2",
                    ),
                    (
                        30,
                        "03",
                        "금융·보험",
                        "0301",
                        "금융",
                        "030104",
                        "자산운용",
                        "03010404",
                        "투자운용",
                        "3",
                    ),
                ),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                (
                    (
                        "0202020105_23v3",
                        "인사평가",
                        "인사평가 계획을 수립하고 조직 성과 향상을 지원한다",
                        "5",
                        10,
                    ),
                    (
                        "0202020107_23v4",
                        "교육훈련운영",
                        "직원의 교육훈련 계획을 수립하고 운영한다",
                        "4",
                        10,
                    ),
                    (
                        "0202020103_23v4",
                        "인력채용",
                        "인사 부문의 채용 계획을 수립한다",
                        "4",
                        10,
                    ),
                    (
                        "0202020109_23v5",
                        "급여지급",
                        "직원의 급여를 계산하여 지급한다",
                        "4",
                        10,
                    ),
                    (
                        "0301040409_14v1",
                        "투자 성과평가",
                        "투자 성과평가를 수행한다",
                        "5",
                        30,
                    ),
                    (
                        "0803040207_13v1",
                        "촬영",
                        "직원 업무의 제도 설계와 계획 수립을 지원한다",
                        "3",
                        20,
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO ncs_query_aliases VALUES (?, ?, ?)",
                ("0202020103_23v4", "채용", "인력채용"),
            )
            conn.commit()
        finally:
            conn.close()

        performance = server.search_ncs("성과평가 제도 설계", scope="unit", limit=5)
        training = server.search_ncs("직원 교육훈련 계획 수립", scope="unit", limit=5)
        hiring = server.search_ncs("인사 채용관리", scope="unit", limit=5)
        exact_evaluation = server.search_ncs("인사평가", scope="unit", limit=5)
        payroll = server.search_ncs("급여 계산", scope="unit", limit=5)

        self.assertIn(
            "0202020105_23v3",
            [row["id"] for row in performance["results"][:3]],
        )
        self.assertIn(
            "0202020107_23v4",
            [row["id"] for row in training["results"][:3]],
        )
        self.assertEqual(hiring["results"][0]["id"], "0202020103_23v4")
        self.assertEqual(exact_evaluation["results"][0]["id"], "0202020105_23v3")
        self.assertEqual(payroll["results"][0]["id"], "0202020109_23v5")

    def test_local_and_vercel_server_mirrors_are_identical(self) -> None:
        local_server = ROOT / "src" / "ncs_mcp" / "server.py"
        vercel_server = ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / "server.py"

        self.assertEqual(local_server.read_bytes(), vercel_server.read_bytes())

        for relative in ("search/__init__.py", "search/core.py"):
            with self.subTest(relative=relative):
                local_search = ROOT / "src" / "ncs_mcp" / relative
                vercel_search = (
                    ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / relative
                )
                self.assertEqual(local_search.read_bytes(), vercel_search.read_bytes())

    def test_server_reexports_search_package_entrypoint(self) -> None:
        from ncs_mcp.search import search_ncs as package_search_ncs

        self.assertIs(server.search_ncs, package_search_ncs)


if __name__ == "__main__":
    unittest.main()
