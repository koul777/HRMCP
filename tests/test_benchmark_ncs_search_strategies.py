from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from scripts.benchmark_ncs_search_strategies import (
    _aggregate,
    concept_first_search,
    lazy_tier_search,
    render_markdown,
)


class SearchStrategyExperimentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "search.db"
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE classifications (
                    classification_id INTEGER PRIMARY KEY,
                    major_name TEXT, middle_name TEXT, small_name TEXT, sub_name TEXT
                );
                CREATE TABLE competency_units (
                    unit_code TEXT PRIMARY KEY, unit_name_raw TEXT, api_definition TEXT,
                    unit_level_raw TEXT, classification_id INTEGER
                );
                CREATE TABLE competency_elements (
                    element_id INTEGER PRIMARY KEY, element_name_raw TEXT, unit_code TEXT
                );
                CREATE TABLE performance_criteria (
                    criteria_id INTEGER PRIMARY KEY, criteria_text_raw TEXT,
                    criteria_text_refined TEXT, element_id INTEGER
                );
                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY, ksa_text_raw TEXT,
                    ksa_text_refined TEXT, ksa_type_name TEXT, element_id INTEGER
                );
                CREATE TABLE ncs_query_aliases (
                    alias_text TEXT, normalized_query TEXT, unit_code TEXT
                );
                CREATE TABLE ontology_concepts (
                    concept_id INTEGER PRIMARY KEY, concept_name TEXT,
                    normalized_key TEXT, concept_type TEXT
                );
                CREATE INDEX idx_concepts_key ON ontology_concepts(normalized_key);
                CREATE TABLE ontology_concept_aliases (
                    alias_id INTEGER PRIMARY KEY, concept_id INTEGER,
                    alias_text TEXT, normalized_alias_key TEXT
                );
                CREATE INDEX idx_aliases_key ON ontology_concept_aliases(normalized_alias_key);
                CREATE TABLE ksa_concept_links (ksa_id INTEGER, concept_id INTEGER);
                CREATE INDEX idx_ksa_concepts_concept ON ksa_concept_links(concept_id);
                CREATE TABLE criteria_concept_links (criteria_id INTEGER, concept_id INTEGER);
                CREATE INDEX idx_criteria_concepts_concept ON criteria_concept_links(concept_id);
                INSERT INTO classifications VALUES (1, 'business', 'hr', 'staffing', 'recruitment');
                INSERT INTO competency_units VALUES ('U1', 'recruitment planning', 'hire people', '4', 1);
                INSERT INTO competency_units VALUES ('U2', 'interview operation', 'screen candidates', '3', 1);
                INSERT INTO competency_elements VALUES (1, 'candidate interview', 'U1');
                INSERT INTO competency_elements VALUES (2, 'schedule control', 'U2');
                INSERT INTO performance_criteria VALUES (1, 'conduct candidate interview', NULL, 1);
                INSERT INTO performance_criteria VALUES (2, 'control interview schedule', NULL, 2);
                INSERT INTO ksa_items VALUES (1, 'interview skill', NULL, 'skill', 1);
                INSERT INTO ksa_items VALUES (2, 'schedule skill', NULL, 'skill', 2);
                INSERT INTO ontology_concepts VALUES (1, 'interview', 'interview', 'skill');
                INSERT INTO ontology_concept_aliases VALUES (1, 1, 'interview', 'interview');
                INSERT INTO ksa_concept_links VALUES (1, 1);
                INSERT INTO criteria_concept_links VALUES (1, 1);
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_lazy_tier_stops_after_phrase_hit(self) -> None:
        payload = lazy_tier_search(self.db_path, "candidate interview", limit=10)
        self.assertTrue(payload["results"])
        for stats in payload["instrumentation"]["raw_by_type"].values():
            if stats["selected_tier"] == "phrase":
                self.assertEqual(["phrase"], stats["tiers_executed"])

    def test_lazy_tier_uses_and_then_or_and_is_deterministic(self) -> None:
        first = lazy_tier_search(self.db_path, "recruitment schedule", limit=10)
        second = lazy_tier_search(self.db_path, "recruitment schedule", limit=10)
        self.assertEqual(
            [(row["type"], row["id"]) for row in first["results"]],
            [(row["type"], row["id"]) for row in second["results"]],
        )
        self.assertTrue(
            any(
                stats["selected_tier"] == "token_or"
                for stats in first["instrumentation"]["raw_by_type"].values()
            )
        )

    def test_lazy_tier_offset_pages_without_reordering(self) -> None:
        full = lazy_tier_search(self.db_path, "interview", limit=10)
        page = lazy_tier_search(self.db_path, "interview", limit=2, offset=1)
        self.assertEqual(
            [(row["type"], row["id"]) for row in full["results"][1:3]],
            [(row["type"], row["id"]) for row in page["results"]],
        )

    def test_concept_first_uses_linked_evidence(self) -> None:
        payload = concept_first_search(self.db_path, "interview", limit=4)
        modes = {row["match_mode"] for row in payload["results"]}
        self.assertIn("concept_link", modes)
        self.assertGreater(payload["instrumentation"]["resolver"]["accepted_count"], 0)

    def test_aggregate_refuses_false_promotion(self) -> None:
        base_record = {
            "query": "q", "strategy": "p1_baseline", "zero_hit": False,
            "elapsed_ms": {"p50": 100.0, "p95": 120.0}, "rss_after_bytes": 100,
            "quality_proxy": {"mean_query_token_coverage": 1.0, "distinct_type_count": 4},
            "stable_ids": True,
        }
        lazy = {**base_record, "strategy": "lazy_tier", "elapsed_ms": {"p50": 70.0, "p95": 100.0}}
        base_record["quality_proxy"]["baseline_overlap_at_10"] = 1.0
        concept = {**base_record, "strategy": "concept_first", "elapsed_ms": {"p50": 60.0, "p95": 90.0}, "quality_proxy": {"mean_query_token_coverage": 0.5, "distinct_type_count": 4, "baseline_overlap_at_10": 0.2}}
        aggregate = _aggregate([base_record, lazy, concept], 425_758_720)
        self.assertEqual("hold_for_labeled_recall_and_product_parity", aggregate["lazy_tier"]["promotion_recommendation"])
        self.assertEqual("do_not_promote", aggregate["concept_first"]["promotion_recommendation"])

    def test_markdown_states_labeled_recall_limit(self) -> None:
        report = {
            "generated_at": "now", "database": {"open_mode": "read_only"},
            "experiment": {
                "query_count": 0, "runs_per_query": 1, "records": [],
                "aggregate": {
                    name: {
                        "aggregate_p50_ms": 1, "aggregate_p95_ms": 1,
                        "zero_hit_count": 0, "mean_query_token_coverage": 1,
                        "mean_baseline_overlap_at_10": 1,
                    }
                    for name in ("p1_baseline", "lazy_tier", "concept_first")
                },
            },
            "decision": {"winner": "none_promoted", "latency_winner": "lazy_tier", "observed_gate_candidate": "lazy_tier", "warning": "warning"},
        }
        rendered = render_markdown(report)
        self.assertIn("Labeled recall delta", rendered)
        self.assertIn("cannot replace labeled recall", rendered)


if __name__ == "__main__":
    unittest.main()
