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
                (1, "02", "경영", "02", "인사", "02", "인사관리", "01", "채용", "1"),
                (2, "99", "기타", "99", "기타", "99", "기타", "99", "데이터분석 직무", "2"),
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
            ("U_ASCII", "data workflow analysis", "data operations", "4", 1),
        )
        conn.executemany("INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)", units)
        conn.execute(
            "INSERT INTO ncs_query_aliases VALUES ('U_HIRE_1', 'recruiting', 'recruiting')"
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

    def test_local_and_vercel_server_mirrors_are_identical(self) -> None:
        local_server = ROOT / "src" / "ncs_mcp" / "server.py"
        vercel_server = ROOT / "deploy" / "vercel_mcp_app" / "src" / "ncs_mcp" / "server.py"

        self.assertEqual(local_server.read_bytes(), vercel_server.read_bytes())


if __name__ == "__main__":
    unittest.main()
