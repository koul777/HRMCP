from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from scripts import benchmark_settings_cache as benchmark


class SettingsCacheBenchmarkTests(unittest.TestCase):
    def test_environment_fingerprint_changes_for_env_but_not_cwd(self) -> None:
        previous = os.environ.get("NCS_DB_PATH")
        original_cwd = Path.cwd()
        try:
            os.environ["NCS_DB_PATH"] = "first.db"
            first = benchmark.environment_fingerprint()
            os.environ["NCS_DB_PATH"] = "second.db"
            second = benchmark.environment_fingerprint()
            self.assertNotEqual(first, second)
            with tempfile.TemporaryDirectory() as tmp:
                try:
                    os.chdir(tmp)
                    self.assertEqual(second, benchmark.environment_fingerprint())
                finally:
                    os.chdir(original_cwd)
        finally:
            os.chdir(original_cwd)
            if previous is None:
                os.environ.pop("NCS_DB_PATH", None)
            else:
                os.environ["NCS_DB_PATH"] = previous

    def test_lru_is_stale_and_explicit_clear_refreshes(self) -> None:
        state = {"value": "first"}
        lru = benchmark.LruCandidate(lambda: state["value"])
        explicit = benchmark.ExplicitClearCandidate(lambda: state["value"])
        self.assertEqual("first", lru())
        self.assertEqual("first", explicit())
        state["value"] = "second"
        self.assertEqual("first", lru())
        self.assertEqual("first", explicit())
        explicit.clear_settings_cache()
        self.assertEqual("second", explicit())

    def test_fingerprint_candidate_refreshes_and_is_single_flight(self) -> None:
        state = {"fingerprint": "first", "loads": 0}

        def loader() -> str:
            state["loads"] += 1
            return state["fingerprint"]

        candidate = benchmark.FingerprintCandidate(
            loader, fingerprint=lambda: state["fingerprint"]
        )
        self.assertEqual("first", candidate())
        self.assertEqual("first", candidate())
        self.assertEqual(1, state["loads"])
        state["fingerprint"] = "second"
        self.assertEqual("second", candidate())
        self.assertEqual(2, state["loads"])

        thread_result = benchmark._thread_scenario(workers=4)
        self.assertGreater(thread_result["candidate_a_loader_calls_on_concurrent_miss"], 1)
        self.assertEqual(1, thread_result["candidate_b_loader_calls_on_concurrent_miss"])
        self.assertTrue(thread_result["candidate_b_single_identity"])

    def test_env_file_change_reproduces_current_stale_behavior(self) -> None:
        result = benchmark._env_file_change_scenario()
        self.assertTrue(result["current_stale_due_to_env_promotion"])
        self.assertTrue(result["candidate_b_key_changed_but_loader_stale"])

    def test_small_benchmark_has_contract_and_no_secret_values(self) -> None:
        with patch.dict(os.environ, {"NCS_SERVICE_KEY": "sentinel-secret"}, clear=False):
            report = benchmark.run_benchmark(repeats=3)
        self.assertEqual("ncs_settings_cache_experiment_v1", report["schema"])
        self.assertEqual(
            "do_not_promote_settings_object_cache_yet",
            report["recommendation"]["verdict"],
        )
        self.assertEqual([1, 10, 24, 100], report["scope"]["batch_sizes"])
        self.assertNotIn("sentinel-secret", str(report))
        for strategy in report["timings"].values():
            self.assertEqual({"1", "10", "24", "100"}, set(strategy))


if __name__ == "__main__":
    unittest.main()
