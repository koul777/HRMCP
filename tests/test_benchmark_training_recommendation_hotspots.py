from __future__ import annotations

import unittest

from scripts.benchmark_training_recommendation_hotspots import (
    RequestNormalizeMemo,
    candidate_gate,
    extract_major_code,
    metric_summary,
    recommendation_fingerprint,
)


class TrainingRecommendationHotspotBenchmarkTests(unittest.TestCase):
    def test_metric_summary_reports_interpolated_percentiles(self) -> None:
        result = metric_summary([10.0, 20.0, 30.0, 40.0])
        self.assertEqual(result["p50_ms"], 25.0)
        self.assertEqual(result["p95_ms"], 38.5)

    def test_request_normalize_memo_caches_equal_values_within_request(self) -> None:
        calls: list[str] = []

        def normalize(value: str) -> str:
            calls.append(value)
            return value.replace(" ", "").lower()

        memo = RequestNormalizeMemo(normalize)
        self.assertEqual(memo("Data Analysis"), "dataanalysis")
        self.assertEqual(memo("Data Analysis"), "dataanalysis")
        self.assertEqual(memo("Other"), "other")
        self.assertEqual(calls, ["Data Analysis", "Other"])
        self.assertEqual(memo.as_dict()["hits"], 1)
        self.assertEqual(memo.as_dict()["misses"], 2)

    def test_recommendation_fingerprint_detects_order_score_and_evidence_changes(self) -> None:
        base = {
            "ok": True,
            "recommended_courses": [
                {"course_id": "C1", "score": 0.9, "evidence": [{"concept_id": 1}]},
                {"course_id": "C2", "score": 0.8, "evidence": [{"concept_id": 2}]},
            ],
        }
        reordered = {"recommended_courses": list(reversed(base["recommended_courses"]))}
        rescored = {
            "recommended_courses": [
                {**base["recommended_courses"][0], "score": 0.7},
                base["recommended_courses"][1],
            ]
        }
        reevidenced = {
            "recommended_courses": [
                {**base["recommended_courses"][0], "evidence": [{"concept_id": 3}]},
                base["recommended_courses"][1],
            ]
        }
        baseline = recommendation_fingerprint(base)
        self.assertNotEqual(baseline["ids_order_fingerprint"], recommendation_fingerprint(reordered)["ids_order_fingerprint"])
        self.assertNotEqual(baseline["score_fingerprint"], recommendation_fingerprint(rescored)["score_fingerprint"])
        self.assertNotEqual(baseline["evidence_fingerprint"], recommendation_fingerprint(reevidenced)["evidence_fingerprint"])

    def test_recommendation_fingerprint_ignores_dynamic_audit_values(self) -> None:
        first = {
            "recommended_courses": [{"course_id": "C1", "score": 1.0, "evidence": []}],
            "audit": {"generated_at": "first"},
        }
        second = {
            "recommended_courses": [{"course_id": "C1", "score": 1.0, "evidence": []}],
            "audit": {"generated_at": "second"},
        }
        self.assertEqual(
            recommendation_fingerprint(first)["exact_recommendation_fingerprint"],
            recommendation_fingerprint(second)["exact_recommendation_fingerprint"],
        )

    def test_extract_major_code_prefers_source_scope(self) -> None:
        result = {
            "source": {"major_code": "02"},
            "query_resolution": {"candidates": [{"major_code": "20"}]},
        }
        self.assertEqual(extract_major_code(result), "02")

    def test_candidate_gate_requires_speed_parity_and_rss(self) -> None:
        baseline = {"latency": {"p50_ms": 100.0}}
        accepted = candidate_gate(
            baseline,
            {"latency": {"p50_ms": 70.0}, "max_rss_delta_mb": 10.0},
            parity=True,
        )
        rejected = candidate_gate(
            baseline,
            {"latency": {"p50_ms": 60.0}, "max_rss_delta_mb": 10.0},
            parity=False,
        )
        self.assertTrue(accepted["promotion_candidate"])
        self.assertFalse(rejected["promotion_candidate"])


if __name__ == "__main__":
    unittest.main()
