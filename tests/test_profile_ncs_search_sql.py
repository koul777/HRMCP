from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "profile_ncs_search_sql.py"
SPEC = importlib.util.spec_from_file_location("profile_ncs_search_sql", MODULE_PATH)
assert SPEC and SPEC.loader
profile = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(profile)


class ProfileNcsSearchSqlTests(unittest.TestCase):
    @contextmanager
    def _synthetic_harness(self, *, normalized: bool = False):
        from tests.test_ncs_search_recall import NcsSearchRecallTests

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "search.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            NcsSearchRecallTests._create_schema(conn)
            NcsSearchRecallTests._seed(conn)
            conn.execute(
                "INSERT INTO competency_units VALUES ('UNICODE', ?, '', '4', 1)",
                ("ＡＬＰＨＡ Straße",),
            )
            if normalized:
                NcsSearchRecallTests()._add_normalized_columns(conn)
            conn.commit()
            conn.close()
            recorder = profile.StatementRecorder()
            with profile.SearchHarness(path, recorder) as harness:
                yield harness, recorder

    def test_fast_reject_uses_production_intent_and_expansion_tiers(self) -> None:
        with self._synthetic_harness() as (harness, recorder):
            for query in ("연봉 협상", "인사 채용관리"):
                with self.subTest(query=query):
                    expected = harness.normal_search(query, "unit", 5)
                    actual, rejected = harness.fast_reject_search(
                        query, "unit", 5, ["unit"]
                    )
                    self.assertGreater(expected["returned"], 0)
                    self.assertEqual(actual, expected)
                    self.assertFalse(rejected)
            self.assertTrue(any(
                row["match_tier"] == -1 and row["statement_kind"] == "fast_reject_probe"
                for row in recorder.records
            ))

    def test_fast_reject_uses_normalized_production_schema(self) -> None:
        with self._synthetic_harness(normalized=True) as (harness, _):
            expected = harness.normal_search("alpha strasse", "unit", 5)
            actual, rejected = harness.fast_reject_search(
                "alpha strasse", "unit", 5, ["unit"]
            )
            self.assertEqual(expected["results"][0]["id"], "UNICODE")
            self.assertEqual(actual, expected)
            self.assertFalse(rejected)

    def test_fast_reject_preserves_explicit_classification_and_empty_contract(self) -> None:
        with self._synthetic_harness() as (harness, _):
            for classification_filter, is_empty in (({"major_code": "02"}, False), ({"major_code": "15"}, True)):
                with self.subTest(classification_filter=classification_filter):
                    expected = harness.normal_search(
                        "데이터분석", "all", 8,
                        classification_filter=classification_filter,
                    )
                    actual, rejected = harness.fast_reject_search(
                        "데이터분석", "all", 8, ["ksa", "unit"],
                        classification_filter=classification_filter,
                    )
                    self.assertEqual(actual, expected)
                    self.assertEqual(rejected, is_empty)
                    self.assertEqual(actual["classification_filter"], classification_filter)

    def test_fast_reject_incomplete_type_order_does_not_hide_results(self) -> None:
        with self._synthetic_harness() as (harness, _):
            expected = harness.normal_search("신입사원 면접 절차", "criteria", 5)
            actual, rejected = harness.fast_reject_search(
                "신입사원 면접 절차", "criteria", 5, ["unit"]
            )
            self.assertGreater(expected["returned"], 0)
            self.assertEqual(actual, expected)
            self.assertFalse(rejected)

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

    def test_read_only_profile_connection_registers_search_boundary_function(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "profile.db"
            raw = sqlite3.connect(db_path)
            raw.execute("CREATE TABLE marker (value TEXT)")
            raw.commit()
            raw.close()

            recorder = profile.StatementRecorder()
            harness = profile.SearchHarness(db_path, recorder)
            with harness.open_db() as conn:
                row = conn.execute(
                    "SELECT ncs_search_match(?, ?), "
                    "ncs_search_match_normalized('출입 통제', '출입'), "
                    "ncs_search_match_normalized('수출입계약', '출입'), "
                    "ncs_search_match_normalized('strasse alpha', 'strasse')",
                    ("출입 통제와 보안 점검", "출입"),
                ).fetchone()

            self.assertEqual(tuple(row), (1, 1, 0, 1))


if __name__ == "__main__":
    unittest.main()
