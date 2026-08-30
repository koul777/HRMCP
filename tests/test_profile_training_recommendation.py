from __future__ import annotations

import sqlite3
import unittest

from scripts.profile_training_recommendation import (
    ProfiledConnection,
    StageRecorder,
    _metric_summary,
    percentile,
    semantic_fingerprint,
)


class TrainingRecommendationProfilerTest(unittest.TestCase):
    def test_percentile_uses_linear_interpolation(self) -> None:
        self.assertEqual(percentile([1, 2, 3, 4], 0.5), 2.5)
        self.assertAlmostEqual(percentile([1, 2, 3, 4], 0.95), 3.85)
        self.assertIsNone(percentile([], 0.5))

    def test_metric_summary_reports_p50_p95_and_max(self) -> None:
        summary = _metric_summary([10.0, 20.0, 30.0])
        self.assertEqual(summary["count"], 3)
        self.assertEqual(summary["p50_ms"], 20.0)
        self.assertEqual(summary["p95_ms"], 29.0)
        self.assertEqual(summary["max_ms"], 30.0)

    def test_semantic_fingerprint_ignores_dynamic_audit_and_capacity_values(self) -> None:
        first = {
            "ok": True,
            "source": {"criteria_id": 10, "unit_code": "0201"},
            "recommendations": [
                {"course_id": "C1", "evidence": [{"concept_id": 3, "source_id": "S1"}]}
            ],
            "audit": {"generated_at": "2026-01-01T00:00:00Z"},
            "capacity": {"queue_wait_ms": 0.1},
        }
        second = {
            **first,
            "audit": {"generated_at": "2026-08-30T00:00:00Z"},
            "capacity": {"queue_wait_ms": 99.9},
        }
        self.assertEqual(
            semantic_fingerprint(first)["fingerprint"],
            semantic_fingerprint(second)["fingerprint"],
        )

    def test_semantic_fingerprint_changes_when_ids_change(self) -> None:
        first = {"ok": True, "recommendations": [{"course_id": "C1", "evidence": []}]}
        second = {"ok": True, "recommendations": [{"course_id": "C2", "evidence": []}]}
        self.assertNotEqual(
            semantic_fingerprint(first)["identity_fingerprint"],
            semantic_fingerprint(second)["identity_fingerprint"],
        )

    def test_semantic_fingerprint_counts_public_recommended_courses(self) -> None:
        result = {
            "ok": True,
            "recommended_courses": [
                {"training_course_id": 1, "evidence": []},
                {"training_course_id": 2, "evidence": []},
            ],
        }
        self.assertEqual(semantic_fingerprint(result)["recommendation_count"], 2)

    def test_profiled_connection_counts_statements_and_fetch_time(self) -> None:
        raw = sqlite3.connect(":memory:")
        recorder = StageRecorder()
        conn = ProfiledConnection(raw, recorder)
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.executemany("INSERT INTO sample(value) VALUES (?)", [("a",), ("b",)])
        rows = conn.execute("SELECT id, value FROM sample ORDER BY id").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(recorder.sql_statement_count, 3)
        self.assertEqual(recorder.sql_by_kind["SELECT"], 1)
        self.assertGreaterEqual(recorder.sql_api_ms, 0.0)
        raw.close()


if __name__ == "__main__":
    unittest.main()
