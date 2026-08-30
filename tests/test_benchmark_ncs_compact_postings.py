from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "benchmark_ncs_compact_postings.py"
SPEC = importlib.util.spec_from_file_location("benchmark_ncs_compact_postings", SCRIPT_PATH)
assert SPEC and SPEC.loader
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class CompactSearchPostingsExperimentTests(unittest.TestCase):
    def _source_db(self, path: Path) -> None:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_name TEXT, middle_name TEXT, small_name TEXT, sub_name TEXT
                );
                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY,
                    classification_id INTEGER NOT NULL,
                    unit_name_raw TEXT,
                    api_definition TEXT
                );
                CREATE TABLE ncs_query_aliases (
                    unit_code TEXT, alias_text TEXT, normalized_query TEXT
                );
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY,
                    unit_code TEXT,
                    element_name_raw TEXT
                );
                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY,
                    element_id INTEGER,
                    criteria_text_raw TEXT,
                    criteria_text_refined TEXT
                );
                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY,
                    element_id INTEGER,
                    ksa_text_raw TEXT,
                    ksa_text_refined TEXT
                );
                INSERT INTO classifications VALUES (1, '경영', '인사', '인사관리', '채용');
                INSERT INTO competency_units VALUES ('U1', 1, '인력채용', '신입 인력을 선발한다');
                INSERT INTO ncs_query_aliases VALUES ('U1', 'HR recruiting', '채용');
                INSERT INTO competency_elements VALUES (10, 'U1', '채용·면접');
                INSERT INTO performance_criteria VALUES
                    (100, 10, '신입사원 채용 면접 계획 수립', NULL);
                INSERT INTO ksa_items VALUES
                    (1000, 10, '채용-면접 절차', NULL),
                    (1001, 10, '인사 운영 태도', NULL);
                """
            )

    def test_normalization_keeps_short_korean_and_folds_variants(self) -> None:
        self.assertEqual("채용 면접", benchmark.normalize_text(" 채용/면접 "))
        self.assertEqual("채용 면접", benchmark.normalize_text("채용-면접"))
        self.assertEqual({"채용", "면접"}, benchmark.index_terms("채용·면접"))

    def test_full_temporary_index_finds_two_syllable_and_punctuation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source_path = temp / "source.db"
            index_path = temp / "index.db"
            self._source_db(source_path)
            build = benchmark.build_index(source_path, index_path)
            self.assertEqual("full_build", build["build_mode"])
            self.assertGreater(build["posting_id_count"], 0)
            with benchmark._readonly_connection(source_path) as source, benchmark._readonly_connection(index_path) as index:
                short = benchmark.search_postings(source, index, "채용", limit=10)
                punctuated = benchmark.search_postings(
                    source, index, "채용/면접", limit=10
                )
            self.assertGreater(len(short["results"]), 0)
            self.assertGreater(len(punctuated["results"]), 0)
            self.assertTrue(
                any(row["type"] == "ksa" and row["id"] == 1000 for row in punctuated["results"])
            )

    def test_promotion_is_blocked_when_overlap_is_not_preserved(self) -> None:
        build = {"build_mode": "full_build", "index_bytes": 1_000_000}
        query_metrics = {
            "latency_ms_across_calls": {"p50": 10.0, "p95": 20.0},
            "mean_top10_overlap_with_current": 0.5,
            "minimum_top10_overlap_with_current": 0.5,
            "zero_hit_count": 0,
            "two_syllable_checks": {"zero_hit_count": 0},
            "punctuation_spacing_checks": {"zero_hit_count": 0},
        }
        decision = benchmark.promotion_decision(build, query_metrics, 425_000_000)
        self.assertEqual("do_not_promote", decision["decision"])
        self.assertIn("mean_top10_overlap_at_least_0_99", decision["failed_checks"])

    def test_windows_memory_probe_returns_working_set(self) -> None:
        memory = benchmark._process_memory_bytes()
        if os.name == "nt":
            self.assertGreater(int(memory["rss"] or 0), 0)
            self.assertGreaterEqual(int(memory["peak_rss"] or 0), int(memory["rss"] or 0))

    def test_sample_build_is_explicitly_not_full_build(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp = Path(temp_name)
            source_path = temp / "source.db"
            index_path = temp / "index.db"
            self._source_db(source_path)
            build = benchmark.build_index(
                source_path, index_path, sample_rows_per_type=1
            )
        self.assertEqual("not_full_build", build["build_mode"])
        self.assertEqual("sample_linear_range_not_full_build", build["projection"]["method"])


if __name__ == "__main__":
    unittest.main()
