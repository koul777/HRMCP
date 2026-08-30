from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.benchmark_vercel_bootstrap_retry import (
    SCHEMA,
    build_report,
    compare_candidate_policies,
    probe_current_ensure_bootstrap,
    probe_current_failure_classification,
    probe_mcp_import_readiness_latch,
    write_report,
)


class VercelBootstrapRetryBenchmarkTests(unittest.TestCase):
    def test_current_not_ready_is_cached_and_concurrent_calls_are_single_flight(self) -> None:
        result = probe_current_ensure_bootstrap(concurrency=8, delay_ms=1.0)
        self.assertTrue(result["sequential"]["not_ready_cached_permanently_in_process"])
        self.assertEqual(result["sequential"]["materialization_calls"], 1)
        self.assertTrue(result["concurrent"]["single_flight_observed"])
        self.assertEqual(result["concurrent"]["materialization_calls"], 1)
        self.assertEqual(result["concurrent"]["max_concurrent_materializations"], 1)

    def test_current_metrics_cannot_separate_retryable_oserror_from_enospc(self) -> None:
        result = probe_current_failure_classification(delay_ms=0.1)
        gap = result["classification_gap"]
        self.assertTrue(gap["retryable_oserror_indistinguishable_from_enospc"])
        self.assertTrue(gap["bootstrap_collapses_all_failures_to_no_verified_snapshot"])
        self.assertFalse(gap["safe_automatic_retry_possible_without_product_metric_change"])

    def test_mcp_import_latch_blocks_same_process_recovery(self) -> None:
        result = probe_mcp_import_readiness_latch()
        self.assertFalse(result["import_latched_ready"])
        self.assertTrue(result["state_ready_after_recovery"])
        self.assertEqual(result["post_status_after_state_recovery"], 503)
        self.assertTrue(result["same_process_recovery_blocked_by_import_latch"])

    def test_candidate_C_is_bounded_single_flight_and_never_retries_terminal_errors(self) -> None:
        results = compare_candidate_policies(
            concurrency=8, attempt_ms=0.5, backoff_ms=5.0
        )
        transient = results["lock_timeout_then_ready"]["candidates"]
        self.assertFalse(transient["A_current"]["final"]["ready"])
        self.assertTrue(transient["B_one_bounded_retry"]["final"]["ready"])
        self.assertTrue(transient["C_ttl_backoff"]["final"]["ready"])
        self.assertTrue(transient["D_operator_reset"]["final"]["ready"])
        self.assertTrue(transient["B_one_bounded_retry"]["same_request_retry"])
        self.assertFalse(transient["C_ttl_backoff"]["same_request_retry"])

        for scenario_name in (
            "enospc_then_hypothetical_ready",
            "manifest_mismatch_then_hypothetical_ready",
            "schema_mismatch_then_hypothetical_ready",
            "count_mismatch_then_hypothetical_ready",
            "unknown_oserror_then_hypothetical_ready",
        ):
            for policy, run in results[scenario_name]["candidates"].items():
                with self.subTest(scenario=scenario_name, policy=policy):
                    self.assertFalse(run["final"]["ready"])
                    self.assertEqual(run["materialization_attempts"], 1)
                    self.assertEqual(run["max_concurrent_materializations"], 1)
                    self.assertEqual(run["duplicate_extractions"], 0)
                    self.assertFalse(run["stampede_detected"])

    def test_report_contract_and_artifacts(self) -> None:
        report = build_report(concurrency=4, attempt_ms=0.5, backoff_ms=5.0)
        self.assertEqual(report["schema"], SCHEMA)
        self.assertEqual(report["conclusion"]["safe_candidate"], "C_ttl_backoff")
        self.assertFalse(report["scope"]["product_code_mutated"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            markdown = Path(tmp) / "report.md"
            write_report(report, out, markdown)
            self.assertGreater(out.stat().st_size, 0)
            self.assertIn("Vercel Bootstrap Retry Experiment", markdown.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
