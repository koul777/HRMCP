"""Spelling aids must recover source words without inventing query aliases."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ncs_mcp.search.typo import UnitNameTypoIndex  # noqa: E402


class UnitNameTypoIndexTests(unittest.TestCase):
    def test_recovers_a_misspelled_official_compound(self) -> None:
        index = UnitNameTypoIndex(["인력채용", "자금관리", "자료분석"])
        self.assertEqual(index.suggest("인력채옹"), ["인력채용"])

    def test_supports_one_extra_or_missing_character(self) -> None:
        index = UnitNameTypoIndex(["database"])
        self.assertEqual(index.suggest("databasse"), ["database"])
        # A source word's existing prefix is already valid and needs no change.
        self.assertEqual(index.suggest("databas"), [])
        self.assertEqual(index.suggest("datbase"), ["database"])

    def test_supports_adjacent_transposition(self) -> None:
        index = UnitNameTypoIndex(["인력채용"])
        self.assertEqual(index.suggest("인력용채"), ["인력채용"])

    def test_recovers_a_compound_prefix_without_rewriting_to_the_full_name(self) -> None:
        index = UnitNameTypoIndex(["재무제표작성"])
        self.assertEqual(index.suggest("재무재표"), ["재무제표"])

    def test_nfkc_and_case_variants_use_the_same_source_vocabulary(self) -> None:
        index = UnitNameTypoIndex(["DATABASE"])
        self.assertEqual(index.suggest("ＤＡＴＢＡＳＥ"), ["database"])
        index = UnitNameTypoIndex(["인력채용"])
        import unicodedata
        self.assertEqual(index.suggest(unicodedata.normalize("NFD", "인력채옹")), ["인력채용"])

    def test_existing_words_and_prefixes_are_not_corrected(self) -> None:
        index = UnitNameTypoIndex(["문서관리", "문서관람"])
        for query in ("문서관리", "문서관", "문서"):
            with self.subTest(query=query):
                self.assertEqual(index.suggest(query), [])

    def test_ambiguous_corpus_alternatives_are_rejected(self) -> None:
        for words in (["문서관리", "문서관람"], ["문서관람", "문서관리"]):
            self.assertEqual(UnitNameTypoIndex(words).suggest("문서관라"), [])

    def test_short_words_codes_and_query_syntax_are_not_guessed(self) -> None:
        index = UnitNameTypoIndex(["채용", "자료관리", "database"])
        for query in ("채옹", "0202020103_23v4", "자료%관리", "자료 관리", "x" * 33):
            with self.subTest(query=query):
                self.assertEqual(index.suggest(query), [])

    def test_multiple_edits_and_non_adjacent_swaps_are_rejected(self) -> None:
        index = UnitNameTypoIndex(["인력채용", "database"])
        self.assertEqual(index.suggest("인녁채옹"), [])
        self.assertEqual(index.suggest("batadase"), [])

    def test_deletion_must_not_turn_an_unknown_word_into_a_shorter_prefix(self) -> None:
        index = UnitNameTypoIndex(["document"])
        self.assertEqual(index.suggest("docxum"), [])


if __name__ == "__main__":
    unittest.main()
