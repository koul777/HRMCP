from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts import benchmark_ncs_search as benchmark


class BenchmarkNcsSearchTests(unittest.TestCase):
    def test_percentile_and_latency_summary_use_stable_nearest_rank(self) -> None:
        summary = benchmark.latency_summary([4.0, 1.0, 3.0, 2.0])

        self.assertEqual(summary["samples"], [4.0, 1.0, 3.0, 2.0])
        self.assertEqual(summary["p50"], 2.0)
        self.assertEqual(summary["p95"], 4.0)
        self.assertEqual(summary["sample_count"], 4)

    def test_benchmark_records_counts_zero_hit_and_stable_ids(self) -> None:
        def fake_search(query: str, scope: str, limit: int) -> dict[str, object]:
            self.assertEqual(query, "채용")
            self.assertEqual(limit, 20)
            if scope == "all":
                return {
                    "results": [
                        {"type": "unit", "id": "u1"},
                        {"type": "ksa", "id": 7},
                    ]
                }
            return {"results": []}

        records = benchmark.benchmark_searches(
            fake_search,
            [{"query": "채용", "case": "short_exact_alias"}],
            scopes=("all", "criteria"),
            runs=2,
            limit=20,
        )

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["result_count"], 2)
        self.assertEqual(records[0]["counts_by_type"], {"ksa": 1, "unit": 1})
        self.assertEqual(records[0]["preview_stable_ids"], ["unit:u1", "ksa:7"])
        self.assertFalse(records[0]["zero_hit"])
        self.assertTrue(records[1]["zero_hit"])
        self.assertTrue(records[0]["result_count_stable"])

    def test_missing_snapshot_artifacts_are_reported_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            root = Path(raw_dir)
            report = benchmark.measure_cold_start(
                root / "missing.zip", root / "missing.manifest.json"
            )

            self.assertFalse(report["supported"])
            self.assertEqual(report["reason"], "archive_or_manifest_missing")
            self.assertEqual(report["writes"], "temporary_directory_only")

    def test_markdown_exposes_scope_counts_and_caveat(self) -> None:
        record = {
            "query": "채용",
            "case": "short_exact_alias",
            "scope": "all",
            "limit": 20,
            "elapsed_ms": {"p50": 1.0, "p95": 2.0},
            "result_count": 3,
            "zero_hit": False,
        }
        report = {
            "generated_at": "2026-08-30T00:00:00+00:00",
            "schema": benchmark.SCHEMA,
            "database": {"path": "fixture.db"},
            "benchmark": {
                "runs_per_case": 2,
                "limit": 20,
                "records": [record],
            },
            "acceptance_metrics": {
                "all_scope_zero_hit_rate": 0.0,
                "all_scope_zero_hit_queries": [],
                "result_counts_stable": True,
                "interpretation": "baseline only",
            },
            "deployment_artifacts": {
                "size_budget": {
                    "snapshot_bytes": 10,
                    "max_snapshot_bytes": benchmark.MAX_SNAPSHOT_BYTES,
                    "headroom_bytes": benchmark.MAX_SNAPSHOT_BYTES - 10,
                    "within_budget": True,
                }
            },
            "cold_start": {"supported": False, "stages_ms": {}},
            "environment": {"caveat": "local warm measurement"},
            "commands": {"reproduce": "python benchmark.py"},
        }

        markdown = benchmark.render_markdown(report)

        self.assertIn("| 채용 | short_exact_alias | 3 |", markdown)
        self.assertIn("local warm measurement", markdown)
        self.assertIn("Database writes and human-review status updates: `false`", markdown)


if __name__ == "__main__":
    unittest.main()
