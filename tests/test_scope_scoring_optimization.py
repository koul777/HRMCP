from __future__ import annotations

import itertools
import sqlite3
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.training_recommendation import (  # noqa: E402
    _bounded_levenshtein,
    _candidate_score,
    _exact_unit_name_match,
    _levenshtein,
    _scope_lookup_key,
)


class ScopeScoringOptimizationTests(unittest.TestCase):
    def test_scope_normalization_keeps_compatibility_case_and_symbol_contract(self):
        cases = [
            (None, ""), ("\tＨＲ_Ｐｌａｎｎｉｎｇ\u3000", "hrplanning"),
            ("가·나_다", "가나다"), ("Straße", "strasse"),
            ("İ", "i\u0307"), ("C++ #1", "c1"), ("① Ⅱ", "1ii"),
            ("\u200b채용\ufeff", "채용"), (123, "123"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_scope_lookup_key(value), expected)

    def test_bounded_distance_matches_exact_distance_around_cutoff(self):
        words = ["".join(chars) for length in range(4) for chars in itertools.product("ab가", repeat=length)]
        for left, right in itertools.product(words, repeat=2):
            self.assertEqual(_bounded_levenshtein(left, right), min(3, _levenshtein(left, right)), (left, right))
        for left, right in [
            ("qualificationrequirement", "qualificationrequirment"),
            ("qualificationrequirement", "quxlificationrequirment"),
            ("qualificationrequirement", "quxlificxtionrequirment"),
            ("abcdefghijk", "bcdefghijklm"), ("가나다라마바사", "가나다라바마사"),
        ]:
            with self.subTest(left=left, right=right):
                self.assertEqual(_bounded_levenshtein(left, right), min(3, _levenshtein(left, right)))

    def test_scoring_retains_typo_threshold_and_direct_match_priority(self):
        self.assertEqual(_candidate_score("abcde", "abcde", exact_bonus=.08), 1.08)
        self.assertEqual(_candidate_score("abcdef", "abcde"), .82)
        self.assertEqual(_candidate_score("zabcdef", "abcde"), .64)
        self.assertAlmostEqual(_candidate_score("abcde", "abxde"), .7)
        self.assertAlmostEqual(_candidate_score("abcde", "abxdy"), .6)
        self.assertEqual(_candidate_score("abcde", "axxdy"), 0.0)
        self.assertEqual(_candidate_score("abcd", "abxd"), 0.0)

    def test_exact_unit_lookup_preserves_order_filters_and_complete_row(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
                middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
                sub_code TEXT, sub_name TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY, unit_name_raw TEXT, classification_id INTEGER,
                api_definition TEXT, review_status TEXT, extra_source_column TEXT
            );
            INSERT INTO classifications VALUES (1, '01', 'First', '01', 'Middle', '01', 'Small', '01', 'Sub');
            INSERT INTO classifications VALUES (2, '02', 'Second', '01', 'Middle', '01', 'Small', '01', 'Sub');
            INSERT INTO competency_units VALUES ('01_A', 'Target-Unit', 1, 'Original first definition', 'raw', 'first');
            INSERT INTO competency_units VALUES ('02_B', 'Target Unit', 2, 'Original second definition', 'candidate', 'second');
            INSERT INTO competency_units VALUES ('02_C', 'Ｔａｒｇｅｔ　Ｕｎｉｔ', 2, 'Another definition', 'raw', 'third');
        """)
        row = _exact_unit_name_match(conn, "target_unit")
        self.assertEqual(row["unit_code"], "02_B")
        self.assertEqual(row["api_definition"], "Original second definition")
        self.assertEqual(row["review_status"], "candidate")
        self.assertEqual(row["extra_source_column"], "second")
        self.assertEqual(row["major_name"], "Second")
        self.assertEqual(_exact_unit_name_match(conn, "target_unit", major_code="01")["unit_code"], "01_A")
        self.assertIsNone(_exact_unit_name_match(conn, "target_unit", major_code="20"))
        self.assertIsNone(_exact_unit_name_match(conn, "missing"))


if __name__ == "__main__":
    unittest.main()
