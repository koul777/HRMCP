from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import review_ncs_performance_kpi as reviewer


class PerformanceKpiReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases_path = self.root / "cases.json"
        self.plan_path = self.root / "plan.json"
        self.benchmark_path = self.root / "benchmark.json"
        self.cases = [{"id": "search-a", "workload": "search", "params": {"query": "a", "limit": 3}},
                      {"id": "scope-a", "workload": "scope", "params": {"query": "a"}}]
        self.write(self.cases_path, self.cases)
        self.plan = {"schema": "ncs_performance_trial_plan_v1", "trial": "synthetic",
                     "frozen_at": "2026-10-18T08:00:00+00:00", "primary_cases": "cases.json",
                     "primary_metric": {"workload": "search", "statistic": "pooled_p95_ms"},
                     "initial_runs_per_case": 14, "retest_runs_per_case": 30,
                     "maximum_retests": 1, "db_mutation": False, "scope": "synthetic function tests"}
        self.write(self.plan_path, self.plan)
        self.benchmark = self.make_benchmark()

    @staticmethod
    def write(path: Path, value: object) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")

    def make_benchmark(self, runs: int = 14, factor: float = 0.7) -> dict:
        files = {"ncs_mcp/server.py": "a" * 64}
        sources = {label: {"root": str(self.root / label / "src"), "files": files,
                           "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()}
                   for label in reviewer.LABELS}
        db = {"path": str(self.root / "fixed.db"), "bytes": 100, "sha256_before": "b" * 64,
              "sha256_after": "b" * 64, "unchanged": True}
        body = {"returned": 1, "results": [{"id": "source-1"}], "audit": {"source": "NCS"}}
        data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        record_template = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "returned": 1, "ok": None}
        records = []
        for case in self.cases:
            record = {**case, "response_equal": True, "samples": {}, "warmup": {}}
            for label in reviewer.LABELS:
                latency = 100.0 if label == "baseline" or case["workload"] == "scope" else 100.0 * factor
                record["samples"][label] = [{**record_template, "elapsed_ms": latency} for _ in range(runs)]
                record["warmup"][label] = {**record_template, "elapsed_ms": latency, "body": body}
            records.append(record)
        return {
            "schema": "ncs_code_ab_v1", "mode": "separate_process_interleaved_read_only_function_calls",
            "started_at": "2026-10-18T09:00:00+00:00", "finished_at": "2026-10-18T10:00:00+00:00",
            "deployment_performed": False, "runs_per_case": runs, "warmups_per_case": 1,
            "excluded_response_paths": ["$.audit.generated_at"], "same_database": True,
            "database": db, "candidate_database": copy.deepcopy(db),
            "source_before": sources, "source_after": copy.deepcopy(sources), "source_unchanged_during_run": True,
            "cases_sha256": hashlib.sha256(self.cases_path.read_bytes()).hexdigest(),
            "all_responses_equal": True, "records": records,
            "workers": {label: {"ready": True, "source": str(self.root / label / "src" / "ncs_mcp" / "server.py"),
                                "python": "same-python", "sqlite": "same-sqlite",
                                "pragmas": {"query_only": 1, "mmap_size": 1, "cache_size": -1,
                                            "foreign_keys": 1, "busy_timeout": 30000}}
                        for label in reviewer.LABELS},
        }

    def review(self, *, resamples: int = 10000) -> dict:
        self.write(self.benchmark_path, self.benchmark)
        return reviewer.review_performance(self.benchmark_path, self.plan_path, resamples=resamples)

    def replace_response_body(self, case_index: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, sort_keys=True).encode()
        record = self.benchmark["records"][case_index]
        for label in reviewer.LABELS:
            for sample in [record["warmup"][label], *record["samples"][label]]:
                sample.update({"ok": body.get("ok"), "sha256": hashlib.sha256(data).hexdigest(),
                               "bytes": len(data), "returned": body.get("returned", len(body.get("candidates", [])))})
            record["warmup"][label]["body"] = copy.deepcopy(body)

    def test_clear_improvement_is_technical_only_and_keeps_unimproved_scope(self) -> None:
        report = self.review()
        self.assertTrue(report["technical_performance_eligible"], report["validation_issues"])
        self.assertFalse(report["commit_eligible"])
        self.assertTrue(report["primary"]["improvement_proven"])
        self.assertEqual(report["workload_metrics"]["scope"]["relative_change"], 0)
        self.assertTrue(all(item["status"] == "unknown" for item in report["companion_checklist"]))

    def test_no_improvement_is_not_eligible(self) -> None:
        self.benchmark = self.make_benchmark(factor=1)
        report = self.review()
        self.assertFalse(report["technical_performance_eligible"])
        self.assertTrue(report["all_regression_metrics_pass"])
        self.assertFalse(report["primary"]["improvement_proven"])

    def test_scope_empty_and_no_match_are_technical_completion_with_full_latency(self) -> None:
        for query, expected_kind in (("", "empty_query"), ("unmatched", "no_match")):
            with self.subTest(query=query):
                self.benchmark = self.make_benchmark()
                self.replace_response_body(1, {"query": query, "normalized_query": query,
                                               "candidates": [], "ok": False})
                report = self.review()
                self.assertTrue(report["technical_performance_eligible"], report["validation_issues"])
                scope = next(metric for metric in report["query_metrics"] if metric["id"] == "scope-a")
                self.assertEqual(scope["response_classification"], "normal_no_match_contract")
                self.assertEqual(scope["no_match_kind"], expected_kind)
                self.assertTrue(scope["zero_result"])
                self.assertEqual(scope["sample_count_per_arm"], 14)
                self.assertEqual(report["workload_metrics"]["scope"]["sample_count_per_arm"], 14)
                self.assertEqual(report["normal_no_match_contract_counts"][expected_kind], 1)

    def test_scope_no_match_exception_does_not_allow_error_or_unknown_failure_shapes(self) -> None:
        ordinary = {"query": "unmatched", "normalized_query": "unmatched", "candidates": [], "ok": False}
        invalid_bodies = [
            {**ordinary, "error": "failed"},
            {**ordinary, "error": None},
            {**ordinary, "isError": True},
            {**ordinary, "timeout": True},
            {**ordinary, "timed_out": True},
            {**ordinary, "candidates": [{"id": "unexpected"}]},
            {key: value for key, value in ordinary.items() if key != "normalized_query"},
        ]
        for body in invalid_bodies:
            with self.subTest(body=body):
                self.benchmark = self.make_benchmark()
                self.replace_response_body(1, body)
                report = self.review()
                self.assertFalse(report["technical_performance_eligible"])
                self.assertTrue(any("error_response" in issue for issue in report["validation_issues"]))
        self.benchmark = self.make_benchmark()
        self.replace_response_body(0, ordinary)
        self.assertFalse(self.review()["technical_performance_eligible"])

    def test_regression_initial_warns_and_retest_fails(self) -> None:
        for runs, expected in ((14, "warn"), (30, "fail")):
            with self.subTest(runs=runs):
                self.benchmark = self.make_benchmark(runs=runs, factor=1.2)
                report = self.review()
                self.assertFalse(report["technical_performance_eligible"])
                self.assertEqual(report["primary"]["regression_status"], expected)
                self.assertEqual(report["primary"]["planned_retest_required"], runs == 14)

    def test_small_samples_empty_records_and_small_bootstrap_cannot_pass(self) -> None:
        self.benchmark = self.make_benchmark(runs=5)
        self.assertFalse(self.review()["technical_performance_eligible"])
        self.benchmark = self.make_benchmark()
        self.benchmark["records"] = []
        self.assertIn("empty_or_invalid_records", self.review()["validation_issues"])
        self.benchmark = self.make_benchmark()
        self.assertIn("bootstrap_resamples_below_10000", self.review(resamples=20)["validation_issues"])

    def test_reported_booleans_cannot_hide_tampering(self) -> None:
        mutations = {
            "response": lambda d: d["records"][0]["samples"]["candidate"][0].update(sha256="c" * 64),
            "database": lambda d: d["database"].update(sha256_after="d" * 64),
            "source": lambda d: d["source_after"]["candidate"]["files"].update({"extra.py": "e" * 64}),
            "source_aggregate": lambda d: d["source_before"]["candidate"].update(sha256="f" * 64),
            "error": lambda d: d["records"][0]["samples"]["candidate"][0].update(ok=False, error="failed"),
            "timeout": lambda d: d["records"][0]["samples"]["candidate"][0].update(timed_out=True),
            "pragmas": lambda d: d["workers"]["candidate"]["pragmas"].update(query_only=0),
            "runtime": lambda d: d["workers"]["candidate"].update(python="different"),
            "empty_samples": lambda d: d["records"][0]["samples"].update(candidate=[]),
            "zero_baseline": lambda d: d["records"][0]["samples"]["baseline"][0].update(elapsed_ms=0),
            "missing_finish": lambda d: d.pop("finished_at"),
            "case_params": lambda d: d["records"][0].update(params={"query": "changed"}),
            "fixture_hash": lambda d: d.update(cases_sha256="f" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                self.benchmark = self.make_benchmark()
                mutate(self.benchmark)
                report = self.review()
                self.assertTrue(report["validation_issues"])
                self.assertFalse(report["technical_performance_eligible"])

    def test_nonfinite_and_malformed_json_are_inconclusive(self) -> None:
        for value in (float("nan"), float("inf")):
            self.benchmark = self.make_benchmark()
            self.benchmark["records"][0]["samples"]["candidate"][0]["elapsed_ms"] = value
            report = self.review()
            self.assertEqual(report["status"], "inconclusive")
            self.assertFalse(report["technical_performance_eligible"])
        self.benchmark_path.write_text("{", encoding="utf-8")
        report = reviewer.review_performance(self.benchmark_path, self.plan_path)
        self.assertTrue(report["validation_issues"])

    def test_nearest_rank_and_pairing_preserve_scaled_ratio(self) -> None:
        self.assertEqual(reviewer.nearest_rank(list(range(1, 21)), 0.95), 19)
        interval = reviewer.paired_interval([([1, 10, 100, 1000], [0.5, 5, 50, 500]),
                                             ([2, 20, 200], [1, 10, 100])], "p95", 100)
        self.assertEqual(interval, (-0.5, -0.5))

    def test_even_sample_p50_uses_the_contract_nearest_rank(self) -> None:
        # The frozen 14-pair cohort selects its seventh value, not 7.5.
        metric = reviewer._metric([(list(range(1, 15)), list(range(2, 29, 2)))],
                                  "p50", 10000, "initial")
        self.assertEqual(metric["baseline_ms"], 7)
        self.assertEqual(metric["candidate_ms"], 14)
        self.assertEqual(metric["relative_change"], 1)
        self.assertEqual(metric["paired95_ci"], {"lower": 1, "upper": 1})

    def test_quantile_metadata_records_nearest_rank_for_both_percentiles(self) -> None:
        report = self.review()
        self.assertEqual(report["statistics"]["p50"], "nearest_rank")
        self.assertEqual(report["statistics"]["p95"], "nearest_rank")

    def test_crossing_regression_interval_is_inconclusive(self) -> None:
        with patch.object(reviewer, "paired_interval", return_value=(-0.1, 0.2)):
            report = self.review()
        self.assertEqual(report["status"], "inconclusive")
        self.assertFalse(report["technical_performance_eligible"])

    def test_initial_point_regression_requires_retest_even_if_ci_is_below_limit(self) -> None:
        with patch.object(reviewer, "paired_interval", return_value=(-0.1, 0.04)):
            metric = reviewer._metric([([100] * 14, [110] * 14)], "p50", 10000, "initial")
        self.assertEqual(metric["regression_status"], "warn")
        self.assertTrue(metric["planned_retest_required"])

    def test_exact_five_percent_is_inclusive_despite_float_rounding(self) -> None:
        with patch.object(reviewer, "paired_interval", return_value=(1.05 - 1, 1.05 - 1)):
            metric = reviewer._metric([([100] * 14, [105] * 14)], "p50", 10000, "initial")
        self.assertEqual(metric["regression_status"], "pass")
        self.assertFalse(metric["planned_retest_required"])

    def test_summary_cannot_fabricate_improvement(self) -> None:
        self.benchmark = self.make_benchmark(factor=1)
        self.benchmark["summary"] = {"search": {"p50_reduction_percent": 99, "all_responses_equal": True}}
        report = self.review()
        self.assertEqual(report["primary"]["relative_change"], 0)
        self.assertFalse(report["primary"]["improvement_proven"])

    def test_cli_require_improvement_and_input_overwrite_protection(self) -> None:
        self.benchmark = self.make_benchmark(factor=1)
        self.write(self.benchmark_path, self.benchmark)
        out = self.root / "review.json"
        with patch("builtins.print"):
            result = reviewer.main(["--benchmark", str(self.benchmark_path), "--plan", str(self.plan_path),
                                    "--out", str(out), "--require-improvement"])
        self.assertEqual(result, 1)
        self.assertFalse(json.loads(out.read_text(encoding="utf-8"))["commit_eligible"])
        with self.assertRaises(SystemExit):
            reviewer.main(["--benchmark", str(self.benchmark_path), "--plan", str(self.plan_path),
                           "--out", str(self.cases_path)])


if __name__ == "__main__":
    unittest.main()
