"""Unit-search term resolution for long practitioner queries and compounds."""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.search import core as search_core  # noqa: E402
from ncs_mcp.search.normalization import (  # noqa: E402
    SEARCH_NORMALIZATION_FIELDS,
    SEARCH_NORMALIZATION_REQUIRED_MANIFEST,
    normalize_search_text,
)


HR = (1, "02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사", "1")
SAFETY = (2, "05", "법률·경찰·소방", "02", "소방방재", "01", "방재", "01", "방재안전", "1")
DESIGN = (3, "14", "건설", "03", "건축", "01", "건축설계", "01", "설계도면", "1")


class NcsSearchUnitTermTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "terms.db"
        with self._open_db() as conn:
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
                    unit_code TEXT, alias_text TEXT, normalized_query TEXT
                );
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY,
                    element_name_raw TEXT, unit_code TEXT
                );
                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    criteria_text_raw TEXT, criteria_text_refined TEXT,
                    element_id INTEGER
                );
                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY,
                    ksa_type_name TEXT, ksa_text_raw TEXT, ksa_text_refined TEXT,
                    element_id INTEGER
                );
                """
            )
            conn.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (HR, SAFETY, DESIGN),
            )
            conn.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, '5', ?)",
                (
                    ("HR_PLAN", "인사기획",
                     "인사기획이란 정원과 인력 운영 계획을 수립하는 능력이다.", 1),
                    ("HR_RETIRE", "퇴직업무지원",
                     "퇴직 절차를 안내하고 퇴직 업무를 지원하는 능력이다.", 1),
                    ("HR_EDU", "교육체계 수립",
                     "교육체계를 직급과 직무에 맞게 수립하는 능력이다.", 1),
                    ("SAFE_READY", "비상상황 대비 대응",
                     "비상상황에 대비하여 대응하는 능력이다.", 2),
                    ("DESIGN_DRAW", "설계도 작성",
                     "건축 설계도를 작성하는 능력이다.", 3),
                ),
            )
            conn.commit()
        self.open_db_patch = patch.object(server, "open_db", new=self._open_db)
        self.open_db_patch.start()

    def tearDown(self) -> None:
        self.open_db_patch.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _open_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def test_stems_strip_predicates_and_particles_but_keep_nouns(self) -> None:
        cases = {
            "분석해서": ["분석"],
            "직원들에게": ["직원", "직원들"],
            "시설의": ["시설"],
            "보정하고": ["보정"],
            "DBMS를": ["DBMS"],
            # A noun whose last syllable only looks like a particle stays.
            "인사평가": [],
            "제조원가": [],
            "명세서": [],
            "평가": [],
        }
        for word, expected in cases.items():
            with self.subTest(word=word):
                self.assertEqual(search_core._ncs_search_term_stems(word), expected)

    def test_long_query_ranks_subject_words_beyond_the_fourth_word(self) -> None:
        query = "올해 정원 대비 현원을 분석해서 내년도 인력 운영 계획을 세우려고 합니다"
        with patch.object(
            search_core, "_select_ncs_search_unit_terms", return_value=([], {}, [])
        ):
            legacy = server.search_ncs(query, scope="unit", limit=3)
        # The first four raw words reach ranking unresolved, so 대비 wins.
        self.assertEqual(legacy["results"][0]["id"], "SAFE_READY")

        result = server.search_ncs(query, scope="unit", limit=3)

        self.assertEqual(result["results"][0]["id"], "HR_PLAN")
        terms = result["unit_query_terms"]["terms"]
        # Framing words go; 현원 and 분석 name nothing in this corpus; the
        # workflow words 운영 and 계획 yield to the two specific terms.
        self.assertEqual(terms, ["정원", "인력"])
        self.assertEqual(
            result["unit_query_terms"]["resolved_from"], {"정원": "정원", "인력": "인력"}
        )
        # The public token list and the raw query are unchanged.
        self.assertEqual(result["query_tokens"], query.split()[:4])
        self.assertEqual(result["normalized_query"], query)

    def test_absent_closed_compound_falls_back_to_its_head(self) -> None:
        with patch.object(
            search_core, "_select_ncs_search_unit_terms", return_value=([], {}, [])
        ):
            legacy = server.search_ncs("명예퇴직", scope="unit", limit=5)
        self.assertEqual(legacy["returned"], 0)

        result = server.search_ncs("명예퇴직", scope="unit", limit=5)

        self.assertEqual([row["id"] for row in result["results"]], ["HR_RETIRE"])
        self.assertEqual(result["unit_query_terms"]["terms"], ["퇴직"])

    def test_request_framing_preserves_long_query_unit_term_selection(self) -> None:
        subject = "올해 정원 대비 현원을 분석해서 내년도 인력 운영 계획을 세우려고 합니다"
        prompt = f"NCS 기준으로 다음 직무를 찾아줘: {subject}"
        bare = server.ncs_search(subject, scope="unit", limit=3)
        framed = server.ncs_search(prompt, scope="unit", limit=3)
        self.assertEqual(framed["query"], prompt)
        self.assertEqual(framed["normalized_query"], bare["normalized_query"])
        self.assertEqual(framed["unit_query_terms"], bare["unit_query_terms"])
        self.assertEqual([row["id"] for row in framed["results"]],
                         [row["id"] for row in bare["results"]])
        self.assertEqual(framed["results"][0]["id"], "HR_PLAN")

    def test_short_query_with_present_tokens_is_unchanged(self) -> None:
        result = server.search_ncs("인사기획", scope="unit", limit=5)

        self.assertEqual(result["results"][0]["id"], "HR_PLAN")
        self.assertNotIn("unit_query_terms", result)

    def test_compound_pieces_use_lexical_boundaries(self) -> None:
        # 계도 occurs inside 설계도 but never starts a word, so it is not a
        # usable head; the long query falls back to the leading piece.
        query = "부서별 교육체계도를 새로 만드는 일과 관련 자료 정리"
        result = server.search_ncs(query, scope="unit", limit=3)

        terms = result["unit_query_terms"]["terms"]
        self.assertIn("교육체계", terms)
        self.assertNotIn("계도", terms)
        self.assertEqual(result["results"][0]["id"], "HR_EDU")

    def test_scoped_long_query_resolves_terms_inside_the_filter(self) -> None:
        query = "올해 정원 대비 현원을 분석해서 내년도 인력 운영 계획을 세우려고 합니다"
        result = server.search_ncs(
            query,
            scope="unit",
            limit=3,
            classification_filter={"sub_name": "인사"},
        )

        self.assertEqual(result["results"][0]["id"], "HR_PLAN")
        self.assertTrue(
            all(row["id"].startswith("HR_") for row in result["results"])
        )

    def test_scoped_long_query_on_normalized_storage(self) -> None:
        with self._open_db() as conn:
            for table, fields in SEARCH_NORMALIZATION_FIELDS.items():
                for raw, derived in fields.items():
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {derived} TEXT")
                    expression = (
                        "COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, '')"
                        if table == "ncs_query_aliases" else raw
                    )
                    rows = conn.execute(f"SELECT rowid, {expression} FROM {table}").fetchall()
                    conn.executemany(
                        f"UPDATE {table} SET {derived} = ? WHERE rowid = ?",
                        ((normalize_search_text(row[1]), row[0]) for row in rows),
                    )
            conn.execute(
                "CREATE TABLE serving_snapshot_manifest ("
                "manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL)"
            )
            conn.executemany(
                "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                SEARCH_NORMALIZATION_REQUIRED_MANIFEST.items(),
            )
            conn.commit()
            self.assertEqual(search_core._normalized_search_storage(conn), "v1")
        query = "올해 정원 대비 현원을 분석해서 내년도 인력 운영 계획을 세우려고 합니다"
        for classification_filter in (None, {"sub_name": "인사"}):
            with self.subTest(classification_filter=classification_filter):
                result = server.search_ncs(
                    query,
                    scope="unit",
                    limit=3,
                    classification_filter=classification_filter,
                )
                self.assertEqual(result["results"][0]["id"], "HR_PLAN")
                self.assertEqual(result["unit_query_terms"]["terms"], ["정원", "인력"])

    def test_unit_lexicon_refreshes_when_units_are_added(self) -> None:
        with self._open_db() as conn:
            before = search_core._ncs_search_unit_lexicon(conn).counts("근태")
        self.assertEqual(before, (0, 0))
        with self._open_db() as conn:
            conn.execute(
                "INSERT INTO competency_units VALUES "
                "('HR_TIME', '근태관리', '근태 기록을 관리하는 능력이다.', '4', 1)"
            )
            conn.commit()
        with self._open_db() as conn:
            after = search_core._ncs_search_unit_lexicon(conn).counts("근태")
        # One unit name now starts a word with 근태.  The name-or-definition
        # figure sums matching words (근태관리, 근태) and only ranks terms.
        self.assertEqual(after[0], 1)
        self.assertGreaterEqual(after[1], 1)

    def test_leaf_scopes_keep_the_original_bounded_tokens(self) -> None:
        query = "올해 정원 대비 현원을 분석해서 내년도 인력 운영 계획을 세우려고 합니다"
        calls: list[list[str]] = []
        original = search_core._ncs_search_tier_predicates

        def record(columns, phrase, fallback_tokens, *args, **kwargs):
            calls.append((columns[0], list(fallback_tokens)))
            return original(columns, phrase, fallback_tokens, *args, **kwargs)

        with patch.object(search_core, "_TIER_PREDICATES", record):
            server.search_ncs(query, scope="all", limit=5)

        by_column = dict(calls)
        self.assertEqual(by_column["ki.ksa_text_raw"], query.split()[:4])
        self.assertEqual(by_column["pc.criteria_text_raw"], query.split()[:4])
        self.assertNotEqual(by_column["cu.unit_code"], query.split()[:4])


if __name__ == "__main__":
    unittest.main()
