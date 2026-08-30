from __future__ import annotations

import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "benchmark_sqlite_pragmas.py"
SPEC = importlib.util.spec_from_file_location("benchmark_sqlite_pragmas", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class SqlitePragmaBenchmarkTests(unittest.TestCase):
    def test_variant_matrix_covers_requested_candidates(self) -> None:
        variants = MODULE.build_variants()
        self.assertEqual(13, len(variants))
        by_name = {item.name: item for item in variants}
        self.assertFalse(by_name["baseline"].query_only)
        self.assertTrue(by_name["query_only"].query_only)
        self.assertEqual(0, by_name["mmap_0mb"].mmap_bytes)
        self.assertEqual(256 * 1024 * 1024, by_name["mmap_256mb"].mmap_bytes)
        self.assertEqual(8 * 1024, by_name["cache_8mb"].cache_kib)
        self.assertEqual(64 * 1024, by_name["cache_64mb"].cache_kib)
        self.assertEqual(
            "safe_combination", by_name["combo_128mmap_32cache"].family
        )

    def test_mode_ro_and_query_only_are_distinct_read_only_guards(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "probe.db"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE sample(value INTEGER)")
            conn.execute("INSERT INTO sample VALUES (1)")
            conn.commit()
            conn.close()

            baseline = MODULE.Variant("baseline")
            with MODULE.open_read_only_connection(path, baseline) as read_only:
                self.assertEqual(0, MODULE.effective_pragmas(read_only)["query_only"])
                with self.assertRaises(sqlite3.OperationalError):
                    read_only.execute("INSERT INTO sample VALUES (2)")

            defended = MODULE.Variant("query_only", query_only=True)
            with MODULE.open_read_only_connection(path, defended) as query_only:
                self.assertEqual(1, MODULE.effective_pragmas(query_only)["query_only"])
                with self.assertRaises(sqlite3.OperationalError):
                    query_only.execute("INSERT INTO sample VALUES (3)")

            verify = sqlite3.connect(path)
            self.assertEqual(1, verify.execute("SELECT COUNT(*) FROM sample").fetchone()[0])
            verify.close()

    def test_timing_summary_is_interpolated_and_complete(self) -> None:
        result = MODULE.timing_summary([1.0, 2.0, 3.0, 4.0, 100.0])
        self.assertEqual(5, result["samples"])
        self.assertEqual(3.0, result["p50_ms"])
        self.assertEqual(80.8, result["p95_ms"])
        self.assertEqual(100.0, result["max_ms"])
        self.assertEqual(110.0, result["total_ms"])

    def test_comparison_vetoes_quality_rss_and_unstable_p95(self) -> None:
        def record(peak: int, p95: float, signature: str) -> dict:
            workload = {
                "p50_ms": 1.0,
                "p95_ms": p95,
                "max_ms": p95,
                "samples": 1,
                "total_ms": p95,
            }
            return {
                "memory_final": {"peak_rss_bytes": peak},
                "write_probe": {"blocked": True},
                "passes": {
                    name: {
                        "search_50_candidate_eval": dict(workload),
                        "readiness_25_count": dict(workload),
                        "random_detail_lookup": dict(workload),
                    }
                    for name in ("cold", "warm")
                },
                "search_signatures": {
                    name: {"case": {"rows_sha256": signature}}
                    for name in ("cold", "warm")
                },
            }

        baseline = record(100 * 1024 * 1024, 10.0, "same")
        candidate = record(151 * 1024 * 1024, 20.0, "different")
        comparison = MODULE.compare_variant(baseline, candidate)
        self.assertTrue(comparison["veto"])
        self.assertIn("search_result_or_metadata_difference", comparison["veto_reasons"])
        self.assertIn("peak_rss_delta_over_50mib", comparison["veto_reasons"])
        self.assertIn("unstable_warm_p95", comparison["veto_reasons"])


if __name__ == "__main__":
    unittest.main()
