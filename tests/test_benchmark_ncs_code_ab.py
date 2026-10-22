from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/benchmark_ncs_code_ab.py"
spec = importlib.util.spec_from_file_location("ncs_code_ab_benchmark", SCRIPT)
assert spec is not None and spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class CodeBenchmarkTests(unittest.TestCase):
    def test_response_comparison_excludes_only_top_level_audit_timestamp(self):
        original = {"audit": {"generated_at": "time", "review_status": "candidate"},
                    "rows": [{"generated_at": "source-time", "id": 7}], "elapsed_ms": 12}
        stable = benchmark.stable_response(original)
        self.assertEqual(stable, {"audit": {"review_status": "candidate"},
                                  "rows": [{"generated_at": "source-time", "id": 7}], "elapsed_ms": 12})
        self.assertIn("generated_at", original["audit"])

    def test_summary_separates_workloads_and_flags_each_regression(self):
        records = []
        for key, group, base, candidate in (("a", "search", 100, 50), ("b", "search", 200, 220), ("c", "scope", 400, 100)):
            records.append({"id": key, "workload": group, "p50_ms": {"baseline": base, "candidate": candidate},
                            "samples": {"baseline": [{"elapsed_ms": base}], "candidate": [{"elapsed_ms": candidate}]},
                            "response_equal": key != "c"})
        result = benchmark.summarize(records)
        self.assertEqual(result["search"]["query_p50_median_ms"], {"baseline": 150, "candidate": 135})
        self.assertEqual(result["search"]["regression_over_5pct"], ["b"])
        self.assertEqual(result["search"]["p50_reduction_percent"], 10)
        self.assertFalse(result["scope"]["all_responses_equal"])
        self.assertEqual(result["scope"]["p50_reduction_percent"], 75)

    def test_workload_mapping_keeps_transition_read_only_and_connection_scoped(self):
        server = types.SimpleNamespace(search_ncs=object(), resolve_ncs_query_scope=object())
        events = []

        @contextmanager
        def open_db():
            events.append("open")
            yield "conn"
            events.append("close")

        def recommend_training_transition(conn, **params):
            events.append(("call", conn, params))
            return {"ok": True}

        fake = types.ModuleType("ncs_mcp.training_recommendation")
        fake.recommend_training_transition = recommend_training_transition
        self.assertIs(benchmark.workload_function(server, "search", open_db), server.search_ncs)
        self.assertIs(benchmark.workload_function(server, "scope_edge", open_db), server.resolve_ncs_query_scope)
        with mock.patch.dict(sys.modules, {"ncs_mcp.training_recommendation": fake}):
            fn = benchmark.workload_function(server, "transition", open_db)
            self.assertEqual(fn(current_query="a", target_query="b", save=True), {"ok": True})
        self.assertEqual(events, ["open", ("call", "conn", {"current_query": "a", "target_query": "b", "save": False}),
                                  "close"])

    def test_source_fingerprint_changes_with_content_and_ignores_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.py"
            source.write_text("x = 1\n", encoding="utf-8")
            first = benchmark.source_record(root)
            (root / "sample.pyc").write_bytes(b"cache")
            self.assertEqual(first, benchmark.source_record(root))
            source.write_text("x = 2\n", encoding="utf-8")
            self.assertNotEqual(first["sha256"], benchmark.source_record(root)["sha256"])


if __name__ == "__main__":
    unittest.main()
