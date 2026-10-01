from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.search import core  # noqa: E402
from ncs_mcp.search.prefix_index import (  # noqa: E402
    _known_word_character, prefix_fts_document, prefix_fts_term,
)


class LexicalPrefixIndexTests(unittest.TestCase):
    def test_fixed_word_set_cannot_hide_a_runtime_boundary(self):
        self.assertTrue(all(
            not _known_word_character(chr(point)) or core._ncs_search_word_character(chr(point))
            for point in range(sys.maxunicode + 1)
        ))

    def test_prefix_filter_preserves_boundary_matches_and_rejects_false_positives(self):
        texts = ("alpha beta", "xxalpha beta", "a\u0301alpha", "éalpha", "c++ r&d",
                 "가나 출입", "품질관리", "𠀀alpha", "x_alpha", "\u200balpha", "ab", "alpha%beta")
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        core._register_ncs_search_udfs(conn)
        conn.executescript("""
            CREATE TABLE samples(id INTEGER PRIMARY KEY, value TEXT);
            CREATE VIRTUAL TABLE prefixes USING fts5(search_prefixes, content='', detail='none', columnsize=0, tokenize='ascii');
        """)
        conn.executemany("INSERT INTO samples VALUES (?,?)", enumerate(texts, 1))
        conn.executemany("INSERT INTO prefixes(rowid,search_prefixes) VALUES (?,?)",
                         ((index, prefix_fts_document(text)) for index, text in enumerate(texts, 1)))
        needles = {text[start:start + width] for text in texts for start in range(len(text)) for width in (1, 2, 3, 5)}
        for needle in needles:
            with self.subTest(needle=needle):
                sql = "SELECT id FROM samples WHERE ncs_search_match_normalized(value, :needle)=1"
                expected = conn.execute(sql + " ORDER BY id", {"needle": needle}).fetchall()
                term = prefix_fts_term(needle)
                if term:
                    sql += " AND id IN (SELECT rowid FROM prefixes WHERE prefixes MATCH :term)"
                actual = conn.execute(sql + " ORDER BY id", {"needle": needle, "term": term}).fetchall()
                self.assertEqual(expected, actual)

    def test_one_character_or_alternative_keeps_required_candidates(self):
        tiers = core._ncs_search_tier_predicates(
            ("ki.ksa_text_raw", "ki.ksa_text_refined"), "alpha beta", ["alpha", "beta"],
            {"alpha": ["a"]}, normalized="v2",
        )
        indexed = {tier[0]: tier for tier in core._compact_lexical_prefix_tiers(tiers, "ksa")}
        self.assertEqual(indexed[2][2]["_lexical_prefix_match"], '("' + prefix_fts_term("beta") + '")')
        self.assertNotIn("_lexical_prefix_match", indexed[3][2])


if __name__ == "__main__":
    unittest.main()
