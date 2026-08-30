from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "profile_ncs_search_sql.py"
SPEC = importlib.util.spec_from_file_location("profile_ncs_search_sql", MODULE_PATH)
assert SPEC and SPEC.loader
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


class ProfileNcsSearchSqlTests(unittest.TestCase):
    def test_percentile_interpolates(self) -> None:
        self.assertEqual(profile.percentile([1, 2, 3], 0.5), 2.0)
        self.assertEqual(profile.percentile([1, 3], 0.5), 2.0)
        self.assertIsNone(profile.percentile([], 0.95))

    def test_result_fingerprints_are_order_sensitive(self) -> None:
        first = {"results": [{"type": "unit", "id": "1"}, {"type": "ksa", "id": "2"}]}
        second = {"results": [{"type": "ksa", "id": "2"}, {"type": "unit", "id": "1"}]}
        self.assertNotEqual(
            profile.result_order_fingerprint(first),
            profile.result_order_fingerprint(second),
        )
        self.assertEqual(
            profile.result_order_fingerprint(first),
            profile.result_order_fingerprint(dict(first)),
        )

    def test_identify_search_type_prefers_specific_table(self) -> None:
        self.assertEqual(
            profile.identify_search_type(
                "SELECT * FROM performance_criteria pc JOIN competency_units cu ON 1=1"
            ),
            "criteria",
        )
        self.assertEqual(
            profile.identify_search_type("SELECT * FROM ksa_items ki"), "ksa"
        )

    def test_query_plan_classification(self) -> None:
        plan = profile.classify_query_plan(
            [
                "SCAN pc",
                "SEARCH ce USING INTEGER PRIMARY KEY (rowid=?)",
                "USE TEMP B-TREE FOR ORDER BY",
            ]
        )
        self.assertTrue(plan["full_scan"])
        self.assertTrue(plan["index_access"])
        self.assertTrue(plan["temp_btree"])

    def test_round_robin_expected_counts(self) -> None:
        self.assertEqual(
            profile.expected_round_robin_counts(10, profile.SEARCH_TYPES),
            {"unit": 3, "element": 3, "criteria": 2, "ksa": 2},
        )

    def test_promotion_requires_parity_and_threshold(self) -> None:
        accepted = profile.promotion_gate(
            baseline_p50_ms=100.0,
            candidate_p50_ms=70.0,
            exact_contract_parity=True,
        )
        parity_failure = profile.promotion_gate(
            baseline_p50_ms=100.0,
            candidate_p50_ms=60.0,
            exact_contract_parity=False,
        )
        speed_failure = profile.promotion_gate(
            baseline_p50_ms=100.0,
            candidate_p50_ms=80.0,
            exact_contract_parity=True,
        )
        self.assertTrue(accepted["promotion_candidate"])
        self.assertFalse(parity_failure["promotion_candidate"])
        self.assertFalse(speed_failure["promotion_candidate"])

    def test_candidate_groups_keep_requested_risk_groups(self) -> None:
        self.assertEqual(
            profile.candidate_groups(
                {"tags": ["punctuation", "two_syllable", "negative_control"]}
            ),
            ["punctuation", "two_syllable", "off_scope"],
        )


if __name__ == "__main__":
    unittest.main()
