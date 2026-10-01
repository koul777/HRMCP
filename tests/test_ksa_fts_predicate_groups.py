from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.search import core  # noqa: E402


class KsaFtsPredicateGroupTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        core._register_ncs_search_udfs(self.conn)
        self.conn.executescript("""
            CREATE TABLE ksa_items (
                ksa_id INTEGER PRIMARY KEY, ksa_text_raw TEXT, ksa_text_refined TEXT,
                ksa_text_raw_search_override TEXT, ksa_text_refined_search_override TEXT
            );
            CREATE VIRTUAL TABLE ksa_search_fts USING fts5(
                search_text, content='', detail='none', columnsize=0, tokenize='trigram'
            );
        """)
        self.conn.executemany("INSERT INTO ksa_items VALUES (?, ?, ?, NULL, NULL)", [
            (1, "xy alpha", None), (2, "longterm alpha", None),
            (3, "longterm elsewhere", "alpha remnant"), (4, "xy elsewhere", None),
            (5, "xxalphabeta", None), (6, "hr alpha", None),
            (7, "orphan", "xy alpha"), (8, "longterm beta", None),
        ])
        self.conn.execute("""
            INSERT INTO ksa_search_fts(rowid, search_text)
            SELECT ksa_id, ksa_text_raw || ' ' || COALESCE(ksa_text_refined, '') FROM ksa_items
        """)

    def tiers(self, phrase, tokens, expansions=None):
        return core._ncs_search_tier_predicates(
            ("ki.ksa_text_raw", "ki.ksa_text_refined"), phrase, tokens, expansions,
            normalized="v2",
        )

    def ids(self, tier):
        _, where, params, _, meaningful = tier
        suffix = f" AND ({meaningful})" if meaningful else ""
        return [row[0] for row in self.conn.execute(
            f"SELECT ki.ksa_id FROM ksa_items ki WHERE ({where}){suffix} ORDER BY ki.ksa_id", params)]

    def assert_all_tiers_preserved(self, tiers):
        filtered = core._compact_ksa_search_fts_tiers(tiers)
        self.assertEqual([row[0] for row in tiers], [row[0] for row in filtered])
        for original, candidate in zip(tiers, filtered):
            with self.subTest(tier=original[0]):
                self.assertEqual(self.ids(original), self.ids(candidate))
        return {row[0]: row for row in filtered}

    def test_short_mandatory_token_does_not_disable_other_mandatory_anchor(self):
        filtered = self.assert_all_tiers_preserved(self.tiers("alpha hr", ["alpha", "hr"]))
        self.assertEqual(self.ids(filtered[1]), [6])
        self.assertIn("_ksa_fts_match", filtered[1][2])
        self.assertNotIn("_ksa_fts_match", filtered[3][2])

    def test_short_or_alternative_stays_reachable_through_other_required_group(self):
        filtered = self.assert_all_tiers_preserved(
            self.tiers("longterm alpha", ["longterm", "alpha"], {"longterm": ["xy"]}))
        self.assertEqual(self.ids(filtered[2]), [1, 2, 3, 7])
        self.assertIn("_ksa_fts_match", filtered[2][2])
        self.assertIn(4, self.ids(filtered[3]))
        self.assertNotIn("_ksa_fts_match", filtered[3][2])

    def test_no_safe_required_group_uses_unfiltered_fallback(self):
        filtered = self.assert_all_tiers_preserved(self.tiers("longterm", ["longterm"], {"longterm": ["xy"]}))
        self.assertIn(4, self.ids(filtered[2]))
        self.assertNotIn("_ksa_fts_match", filtered[2][2])
        self.assertNotIn("_ksa_fts_match", filtered[3][2])

    def test_short_query_keeps_plain_scan(self):
        filtered = self.assert_all_tiers_preserved(self.tiers("xy", ["xy"]))
        self.assertEqual(self.ids(filtered[0]), [1, 4, 7])
        self.assertTrue(all("_ksa_fts_match" not in tier[2] for tier in filtered.values()))


if __name__ == "__main__":
    unittest.main()
