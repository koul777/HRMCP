from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import audit_ncs_search_precision as audit


EDUCATION = "\uad50\uc721"
ANALYSIS = "\ubd84\uc11d"
DESIGN = "\uc124\uacc4"


def _row(result_type: str, result_id: object, text: str, mode: str, tokens: list[str], unit: str) -> dict:
    return {
        "type": result_type,
        "id": result_id,
        "text": text,
        "match_mode": mode,
        "matched_tokens": tokens,
        "path": {"unit_code": unit},
    }


class PrecisionRiskAuditTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "schema": "fixture_eval_pack_v1",
            "candidate_eval": [
                {
                    "case_id": "C-001",
                    "query": f"{EDUCATION} {ANALYSIS}",
                    "measurement_scope": "all",
                    "risk_tags": ["off_scope_candidate"],
                },
                {
                    "case_id": "C-002",
                    "query": f"{EDUCATION} {DESIGN}",
                    "measurement_scope": "all",
                    "risk_tags": ["multiword"],
                },
                {
                    "case_id": "C-003",
                    "query": EDUCATION,
                    "measurement_scope": "ksa",
                    "risk_tags": ["short_query"],
                },
            ],
            "measurement": {
                "cases": [
                    {"case_id": "C-001", "query": f"{EDUCATION} {ANALYSIS}", "result_count": 4, "preview": []},
                    {"case_id": "C-002", "query": f"{EDUCATION} {DESIGN}", "result_count": 4, "preview": []},
                    {"case_id": "C-003", "query": EDUCATION, "result_count": 1, "preview": []},
                ]
            },
        }

    def fake_search(self, query: str, scope: str, limit: int) -> dict:
        self.assertEqual(limit, 20)
        if query.endswith(ANALYSIS):
            results = [
                _row("unit", "0201", f"{EDUCATION} A", "or", [EDUCATION], "0201"),
                _row("unit", "0202", f"{EDUCATION} B", "or", [EDUCATION], "0202"),
                _row("unit", "0203", f"{EDUCATION} C", "or", [EDUCATION], "0203"),
                _row("unit", "0204", f"{EDUCATION} D", "or", [EDUCATION], "0204"),
            ]
            return {"query_tokens": [EDUCATION, ANALYSIS], "match_mode": "or", "results": results}
        if query.endswith(DESIGN):
            duplicate = f"{EDUCATION}{DESIGN} \uacfc\uc815"
            results = [
                _row("unit", "0205", duplicate, "phrase", [EDUCATION, DESIGN], "0205"),
                _row("element", 2, duplicate, "phrase", [EDUCATION, DESIGN], "0301"),
                _row("criteria", 3, f"{EDUCATION}{DESIGN} \uae30\uc900", "phrase", [EDUCATION, DESIGN], "0401"),
                _row("ksa", 4, f"{EDUCATION}{DESIGN} \uc9c0\uc2dd", "phrase", [EDUCATION, DESIGN], "0501"),
            ]
            return {"query_tokens": [EDUCATION, DESIGN], "match_mode": "phrase", "results": results}
        results = [_row("ksa", 5, f"{EDUCATION} \uc6b4\uc601", "phrase", [EDUCATION], "0601")]
        return {"query_tokens": [EDUCATION], "match_mode": "phrase", "results": results}

    def test_extracts_and_merges_candidate_cases(self) -> None:
        cases = audit.extract_candidate_cases(self.payload)
        self.assertEqual([case["case_id"] for case in cases], ["C-001", "C-002", "C-003"])
        self.assertTrue(cases[0]["off_scope_candidate"])
        self.assertEqual(cases[2]["measurement_scope"], "ksa")

    def test_audits_required_precision_risk_proxies(self) -> None:
        result = audit.audit_cases(
            audit.extract_candidate_cases(self.payload),
            self.fake_search,
            limit=20,
        )
        aggregate = result["aggregate"]
        self.assertEqual(aggregate["off_scope_candidate"]["hit_rate"], 1.0)
        self.assertEqual(aggregate["or_tier"]["or_only_case_count"], 1)
        self.assertEqual(aggregate["match_tier_distribution"], {"or": 4, "phrase": 5})
        self.assertEqual(
            aggregate["single_token_common_word"]["common_tokens"],
            [{"token": EDUCATION, "case_count": 3}],
        )
        self.assertEqual(
            aggregate["single_token_common_word"]["common_single_token_result_count"],
            5,
        )
        self.assertEqual(aggregate["type_imbalance"]["severe_case_ids"], ["C-001"])
        self.assertEqual(aggregate["preview_duplicates"]["exact_pair_count"], 1)
        self.assertTrue(aggregate["match_metadata"]["complete"])

    def test_report_contract_disclaims_relevance_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            input_path = Path(raw_dir) / "eval.json"
            input_path.write_text(json.dumps(self.payload), encoding="utf-8")
            report = audit.build_report(
                input_path=input_path,
                db_path=Path(raw_dir) / "unused.db",
                limit=20,
                search_fn=self.fake_search,
            )
        contract = report["interpretation_contract"]
        self.assertTrue(contract["proxy_only"])
        self.assertFalse(contract["human_relevance_labels_used"])
        self.assertFalse(contract["precision_measured"])
        self.assertFalse(contract["recall_measured"])
        self.assertFalse(contract["mrr_measured"])
        self.assertFalse(contract["ndcg_measured"])
        self.assertTrue(report["strategy_promotion_veto_policy"]["mandatory_human_labeled_gate"]["required"])
        self.assertFalse(report["safety"]["database_writes"])
        self.assertFalse(report["safety"]["human_reviewed_written"])

    def test_markdown_repeats_proxy_limit(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            input_path = Path(raw_dir) / "eval.json"
            input_path.write_text(json.dumps(self.payload), encoding="utf-8")
            report = audit.build_report(
                input_path=input_path,
                db_path=Path(raw_dir) / "unused.db",
                limit=20,
                search_fn=self.fake_search,
            )
        markdown = audit.render_markdown(report)
        self.assertIn("does not measure or replace Precision@K", markdown)
        self.assertIn("Proposed Strategy-Promotion Vetoes", markdown)
        self.assertIn("No automated status or approval field was written", markdown)


class RecordedBaselineComparisonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.recorded = {
            "overall": {"hit_at_1": 0.5, "hit_at_3": 0.75, "mrr": 0.6},
            "by_category": {"A": {"hit_at_1": 0.4, "hit_at_3": 0.8, "mrr": 0.5}},
        }

    def test_identical_metrics_pass(self) -> None:
        result = audit.compare_recorded_baseline(self.recorded, self.recorded, tolerance=0.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["regressions"], [])

    def test_drop_in_overall_or_category_is_reported(self) -> None:
        current = {
            "overall": {"hit_at_1": 0.5, "hit_at_3": 0.70, "mrr": 0.6},
            "by_category": {"A": {"hit_at_1": 0.3, "hit_at_3": 0.8, "mrr": 0.5}},
        }
        result = audit.compare_recorded_baseline(current, self.recorded, tolerance=0.0)
        self.assertFalse(result["ok"])
        self.assertEqual(
            {(item["scope"], item["metric"]) for item in result["regressions"]},
            {("overall", "hit_at_3"), ("A", "hit_at_1")},
        )

    def test_tolerance_absorbs_small_drops_and_gains_never_fail(self) -> None:
        current = {
            "overall": {"hit_at_1": 0.49, "hit_at_3": 0.95, "mrr": 0.6},
            "by_category": {"A": {"hit_at_1": 0.4, "hit_at_3": 0.8, "mrr": 0.5}},
        }
        self.assertTrue(
            audit.compare_recorded_baseline(current, self.recorded, tolerance=0.02)["ok"]
        )
        self.assertFalse(
            audit.compare_recorded_baseline(current, self.recorded, tolerance=0.0)["ok"]
        )

    def test_new_category_is_not_a_regression(self) -> None:
        current = {
            "overall": self.recorded["overall"],
            "by_category": {
                "A": self.recorded["by_category"]["A"],
                "B": {"hit_at_1": 0.1, "hit_at_3": 0.1, "mrr": 0.1},
            },
        }
        result = audit.compare_recorded_baseline(current, self.recorded, tolerance=0.0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["new_categories"], ["B"])

    def test_loads_a_full_report_or_a_trimmed_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            full = Path(tmp) / "full.json"
            full.write_text(json.dumps({"current": self.recorded}), encoding="utf-8")
            trimmed = Path(tmp) / "trimmed.json"
            trimmed.write_text(json.dumps(self.recorded), encoding="utf-8")
            self.assertEqual(audit.load_recorded_baseline(full), self.recorded)
            self.assertEqual(audit.load_recorded_baseline(trimmed), self.recorded)
            invalid = Path(tmp) / "invalid.json"
            invalid.write_text(json.dumps({"by_category": {}}), encoding="utf-8")
            with self.assertRaises(ValueError):
                audit.load_recorded_baseline(invalid)

    def test_committed_baselines_match_their_fixtures(self) -> None:
        for name, fixture in (
            ("dev.json", "ncs_search_eval_nl_dev.json"),
            ("regression_40.json", "ncs_search_eval_nl.json"),
        ):
            with self.subTest(baseline=name):
                recorded = json.loads(
                    (ROOT / "tests" / "fixtures" / "search_baselines" / name).read_text(
                        encoding="utf-8"
                    )
                )
                cases = json.loads(
                    (ROOT / "tests" / "fixtures" / fixture).read_text(encoding="utf-8")
                )
                self.assertEqual(recorded["case_count"], len(cases))
                self.assertEqual(recorded["fixture"], f"tests/fixtures/{fixture}")
                self.assertEqual(
                    set(recorded["by_category"]),
                    {case["category"] for case in cases},
                )


if __name__ == "__main__":
    unittest.main()
