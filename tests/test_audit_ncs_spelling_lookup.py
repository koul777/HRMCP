from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "src"))

from audit_ncs_spelling_lookup import build_typo_cases
from ncs_mcp.search.typo import _one_edit


class SpellingLookupAuditTests(unittest.TestCase):
    def test_covers_each_major_without_treating_real_names_as_typos(self):
        rows = [
            ("U1", "인사기획", "02"), ("U2", "인사기획", "02"),
            ("U3", "설계도 작성", "14"), ("U4", "인사", "02"),
        ]
        cases = build_typo_cases(rows, per_major_limit=1)
        self.assertEqual({major for c in cases for major in c["majors"]}, {"02", "14"})
        self.assertEqual(cases, build_typo_cases(list(reversed(rows)), per_major_limit=1))
        for case in cases:
            self.assertTrue(_one_edit(
                "".join(case["query"].split()), "".join(case["source_query"].split())
            ))
        hr = next(case for case in cases if case["majors"] == ["02"])
        self.assertEqual(hr["expected"], ["U1", "U2"])


if __name__ == "__main__":
    unittest.main()
