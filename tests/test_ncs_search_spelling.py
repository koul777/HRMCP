from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_mcp.search.spelling import UnitNameSpellingIndex, one_edit_apart


class UnitNameSpellingTests(unittest.TestCase):
    def test_supported_edits(self):
        for query in ("인샤기획", "인사기", "인사기획획", "인기사획"):
            with self.subTest(query=query):
                self.assertTrue(one_edit_apart(query, "인사기획"))
                self.assertEqual(UnitNameSpellingIndex(["인사기획"]).correction(query), "인사기획")

    def test_rejects_exact_distant_short_and_code_queries(self):
        index = UnitNameSpellingIndex(["인사기획", "인사", "0202020101_23v3"])
        for query in ("인사기획", "인 사 기 획", "인사", "인기", "연봉설계", "0202020101_23v4"):
            with self.subTest(query=query):
                self.assertIsNone(index.correction(query))

    def test_ambiguous_names_are_not_guessed(self):
        index = UnitNameSpellingIndex(["인사기획", "인력기획"])
        self.assertIsNone(index.correction("인소기획"))

    def test_spacing_unicode_and_duplicate_names(self):
        index = UnitNameSpellingIndex(["교육과정 개발", "교육과정 개발"])
        self.assertEqual(index.correction("교육과정 개뱔"), "교육과정 개발")
        self.assertIsNone(index.correction("교육과정 개발"))

    def test_long_input_is_bounded(self):
        self.assertIsNone(UnitNameSpellingIndex(["인사기획"]).correction("가" * 10000))


if __name__ == "__main__":
    unittest.main()
