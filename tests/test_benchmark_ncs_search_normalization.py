from __future__ import annotations

import unittest

from scripts import benchmark_ncs_search_normalization as benchmark


class BenchmarkNcsSearchNormalizationTests(unittest.TestCase):
    def test_latency_summary_uses_nearest_rank(self) -> None:
        summary = benchmark.latency_summary([4.0, 1.0, 3.0, 2.0])

        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 4.0)
        self.assertEqual(summary["sample_count"], 4)

    def test_compare_pairs_checks_ids_order_and_tier_distribution(self) -> None:
        pair = {
            "pair_id": "p",
            "feature": "slash",
        }
        records = []
        for strategy in benchmark.STRATEGIES:
            for variant in ("a", "b"):
                records.append(
                    {
                        "strategy": strategy,
                        "pair_id": "p",
                        "variant": variant,
                        "exact_ids_in_order": ["unit:u1", "unit:u2"],
                        "zero_hit": False,
                        "match_tier_proxy": {
                            "result_match_mode_counts": {"token_and": 2}
                        },
                    }
                )

        comparisons = benchmark.compare_pair_records(records, [pair])

        self.assertEqual(len(comparisons), 3)
        self.assertTrue(all(row["same_exact_ids"] for row in comparisons))
        self.assertTrue(all(row["same_exact_order"] for row in comparisons))
        self.assertTrue(
            all(row["same_match_tier_distribution"] for row in comparisons)
        )

    def test_decision_keeps_control_without_zero_hit_gain(self) -> None:
        metrics = {
            "candidate_summary": {
                "query_pattern_expansion": {
                    "zero_hit_change_count": 0,
                    "result_set_change_count": 2,
                    "max_sql_call_proxy_delta": 0,
                    "median_p50_latency_delta_pct": 2.0,
                },
                "db_expression_normalization": {
                    "zero_hit_change_count": 0,
                    "result_set_change_count": 0,
                    "max_sql_call_proxy_delta": 0,
                    "median_p50_latency_delta_pct": 250.0,
                },
            }
        }

        decision = benchmark.decide(metrics)

        self.assertEqual(decision["recommendation"], "keep_current_token_fallback")
        self.assertFalse(decision["promotion_approved"])

    def test_markdown_exposes_caveat_and_safety(self) -> None:
        report = {
            "generated_at": "2026-08-30T00:00:00+00:00",
            "schema": benchmark.SCHEMA,
            "environment": {"database_path": "fixture.db"},
            "experiment": {
                "runs_per_variant": 2,
                "pair_comparisons": [],
            },
            "metrics": {
                "by_strategy": {
                    strategy: {
                        "zero_hit_count": 0,
                        "pair_exact_order_parity_count": 1,
                        "pair_count": 1,
                        "median_variant_p50_ms": 1.0,
                        "max_sql_calls_per_search": 1,
                    }
                    for strategy in benchmark.STRATEGIES
                },
                "candidate_summary": {
                    strategy: {
                        "result_set_change_count": 0,
                        "order_change_count": 0,
                        "match_tier_change_count": 0,
                        "median_p50_latency_delta_pct": 0.0,
                        "max_sql_call_proxy_delta": 0,
                    }
                    for strategy in benchmark.STRATEGIES[1:]
                },
            },
            "decision": {
                "recommendation": "keep_current_token_fallback",
                "promotion_approved": False,
                "rationale": "No measured accuracy gain.",
            },
            "commands": {"reproduce": "python benchmark.py"},
        }

        markdown = benchmark.render_markdown(report)

        self.assertIn("does not claim Recall@K", markdown)
        self.assertIn("Database writes: `false`", markdown)
        self.assertIn("Human-review/status claims: `false`", markdown)


if __name__ == "__main__":
    unittest.main()
