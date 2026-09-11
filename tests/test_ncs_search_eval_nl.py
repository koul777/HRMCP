from __future__ import annotations

from collections import Counter
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_ncs_search_precision as audit


FIXTURE = ROOT / "tests" / "fixtures" / "ncs_search_eval_nl.json"


class NaturalLanguageSearchEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cases = audit.load_nl_evaluation_cases(FIXTURE)
        cls.expected_lookup = {
            code: {
                "unit_code": code,
                "unit_name": f"unit-{code}",
                "classification": ["major", "middle", "small", "sub"],
            }
            for case in cls.cases
            for code in case["expected_unit_codes"]
        }
        cls.case_index = {
            case["query"]: index for index, case in enumerate(cls.cases)
        }

    def ranked_fake_search(self, query: str, scope: str, limit: int) -> dict:
        self.assertEqual(scope, "unit")
        self.assertGreaterEqual(limit, 3)
        index = self.case_index[query]
        expected = self.cases[index]["expected_unit_codes"][0]
        expected_rank = (index % 4) + 1
        result_codes = [f"DISTRACTOR-{index}-{rank}" for rank in range(1, limit + 1)]
        if expected_rank <= 3:
            result_codes[expected_rank - 1] = expected
        return {
            "results": [
                {"type": "unit", "id": code, "text": f"result-{code}"}
                for code in result_codes
            ]
        }

    @staticmethod
    def zero_hit_search(query: str, scope: str, limit: int) -> dict:
        return {
            "results": [
                {"type": "unit", "id": f"NONE-{index}", "text": query}
                for index in range(limit)
            ]
        }

    def test_checked_in_fixture_has_balanced_required_coverage(self) -> None:
        self.assertEqual(len(self.cases), 40)
        self.assertEqual(
            Counter(case["category"] for case in self.cases),
            Counter({category: 8 for category in audit.NL_CATEGORIES}),
        )
        self.assertEqual(len({case["query"] for case in self.cases}), 40)
        self.assertTrue(
            all(case["expected_unit_codes"] for case in self.cases)
        )

    def test_hit_at_1_hit_at_3_and_mrr_are_calculated_by_category(self) -> None:
        result = audit.evaluate_nl_cases(
            self.cases,
            self.ranked_fake_search,
            limit=5,
        )

        self.assertEqual(result["overall"]["hit_at_1"], 0.25)
        self.assertEqual(result["overall"]["hit_at_3"], 0.75)
        self.assertEqual(result["overall"]["mrr"], 0.4583)
        for category in audit.NL_CATEGORIES:
            self.assertEqual(result["by_category"][category]["case_count"], 8)
            self.assertEqual(result["by_category"][category]["hit_at_3"], 0.75)

    def test_report_compares_stage1_baseline_and_applies_warning_gate(self) -> None:
        report = audit.build_nl_evaluation_report(
            input_path=FIXTURE,
            db_path=Path("unused.db"),
            limit=5,
            hit3_threshold=0.7,
            enforce_hit3=False,
            compare_stage1_baseline=True,
            search_fn=self.ranked_fake_search,
            baseline_search_fn=self.zero_hit_search,
            expected_unit_lookup=self.expected_lookup,
        )

        self.assertEqual(report["schema"], audit.NL_SCHEMA)
        self.assertEqual(report["stage1_baseline"]["overall"]["hit_at_3"], 0.0)
        self.assertEqual(report["current"]["overall"]["hit_at_3"], 0.75)
        self.assertEqual(report["delta"]["overall"]["hit_at_3"], 0.75)
        self.assertEqual(report["gate"]["status"], "pass")
        self.assertFalse(
            report["interpretation_contract"]["human_relevance_labels_used"]
        )
        self.assertFalse(report["safety"]["database_writes"])

    def test_hit3_gate_can_warn_or_fail_without_changing_expectations(self) -> None:
        common = {
            "input_path": FIXTURE,
            "db_path": Path("unused.db"),
            "limit": 5,
            "hit3_threshold": 0.7,
            "compare_stage1_baseline": False,
            "search_fn": self.zero_hit_search,
            "expected_unit_lookup": self.expected_lookup,
        }
        warning = audit.build_nl_evaluation_report(
            **common,
            enforce_hit3=False,
        )
        enforced = audit.build_nl_evaluation_report(
            **common,
            enforce_hit3=True,
        )

        self.assertEqual(warning["gate"]["status"], "warn")
        self.assertEqual(enforced["gate"]["status"], "fail")

    def test_expected_unit_validation_reads_sqlite_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            db_path = Path(raw_dir) / "evaluation.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.executescript(
                    """
                    CREATE TABLE classifications (
                        classification_id INTEGER PRIMARY KEY,
                        major_name TEXT,
                        middle_name TEXT,
                        small_name TEXT,
                        sub_name TEXT
                    );
                    CREATE TABLE competency_units (
                        unit_code TEXT PRIMARY KEY,
                        unit_name_raw TEXT,
                        classification_id INTEGER
                    );
                    INSERT INTO classifications VALUES (1, 'major', 'middle', 'small', 'sub');
                    INSERT INTO competency_units VALUES ('UNIT-1', 'Unit one', 1);
                    """
                )
                conn.commit()
            finally:
                conn.close()
            before = db_path.read_bytes()
            lookup, count = audit.validate_nl_expected_units(
                db_path,
                [
                    {
                        "query": "query",
                        "expected_unit_codes": ["UNIT-1"],
                        "category": "인사",
                    }
                ],
            )

            self.assertEqual(count, 1)
            self.assertEqual(lookup["UNIT-1"]["unit_name"], "Unit one")
            self.assertEqual(db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
