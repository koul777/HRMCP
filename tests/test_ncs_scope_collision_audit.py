from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ncs_mcp.search.collision_audit import (
    SCHEMA,
    build_collision_inventory,
    normalize_label,
    render_markdown,
    write_report,
    _stratified_collision_select,
)


class NcsScopeCollisionAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "ncs.db"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT NOT NULL, major_name TEXT NOT NULL,
                middle_code TEXT NOT NULL, middle_name TEXT NOT NULL,
                small_code TEXT NOT NULL, small_name TEXT NOT NULL,
                sub_code TEXT NOT NULL, sub_name TEXT NOT NULL
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY, base_unit_code TEXT NOT NULL,
                unit_version TEXT NOT NULL, unit_name_raw TEXT NOT NULL,
                unit_level_raw TEXT NOT NULL, classification_id INTEGER NOT NULL
            );
            CREATE TABLE competency_elements (
                element_id INTEGER PRIMARY KEY, unit_code TEXT NOT NULL,
                element_no TEXT NOT NULL, element_code_raw TEXT NOT NULL,
                element_name_raw TEXT NOT NULL, element_level_raw TEXT NOT NULL
            );
            INSERT INTO classifications VALUES
                (1, '01', '\uacbd\uc601', '0101', '\uc778\uc0ac', '010101', '\uc778\uc0ac\uad00\ub9ac', '01010101', '\uc778\uc0ac'),
                (2, '02', '\uae08\uc735', '0201', '\uae08\uc735', '020101', '\uae08\uc735\uad00\ub9ac', '02010101', '\ucd9c\uc785'),
                (3, '03', '\uacbd\uc601', '0301', '\uc778\uc0ac', '030101', '\uc778\uc0ac\uad00\ub9ac', '03010101', '\uad50\uc721');
            INSERT INTO competency_units VALUES
                ('U1', 'U1', '1', '\uc778\uc0ac\ud558\uae30', '3', 2),
                ('U2', 'U2', '1', '\uc218\ucd9c\uc785', '3', 3),
                ('U3', 'U3', '1', '\uc778\uc0ac', '3', 1);
            INSERT INTO competency_elements VALUES
                (11, 'U1', '1', 'U1-01', '\uc778\uc0ac\ud558\uae30', '3'),
                (12, 'U2', '1', 'U2-01', '\uc218\ucd9c\uc785', '3'),
                (13, 'U3', '1', 'U3-01', '\uad50\uc721 \uc124\uacc4', '3');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_normalization_is_conservative_and_punctuation_insensitive(self) -> None:
        self.assertEqual(normalize_label(" \uc778\uc0ac-\ud558\uae30 "), "\uc778\uc0ac \ud558\uae30")
        self.assertEqual(normalize_label("ＡＢＣ_12"), "abc 12")
        self.assertEqual(normalize_label(""), "")

    def test_all_classification_levels_are_level_qualified(self) -> None:
        report = build_collision_inventory(self.db_path, collision_cap=10)
        self.assertEqual(report["per_type"]["classification"]["record_count"], 12)
        levels = {item["scope_level"] for item in report["classification_scope_labels"]}
        self.assertEqual(levels, {"major", "middle", "small", "sub"})
        ids = [item["record_id"] for item in report["classification_scope_labels"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_prefix_internal_and_exact_off_path_hazards_are_candidates(self) -> None:
        report = build_collision_inventory(self.db_path, hazard_cap=100, scenario_limit=100)
        kinds = {item["hazard_kind"] for item in report["scope_label_hazards"]}
        self.assertIn("prefix", kinds)
        self.assertIn("internal_compound", kinds)
        self.assertIn("exact_off_path", kinds)
        self.assertEqual(
            report["metrics"]["off_path_hazard_total_count"],
            report["metrics"]["off_path_prefix_hazard_count"]
            + report["metrics"]["off_path_internal_compound_hazard_count"]
            + report["metrics"]["off_path_exact_hazard_count"],
        )
        self.assertTrue(all(item["candidate_only"] for item in report["scope_label_hazards"]))
        self.assertTrue(all(item["expected_behavior"]["off_path_leaf_excluded_when_scope_is_applied"] for item in report["scope_hazard_scenarios"]))
        self.assertTrue(all(item["expected_behavior"]["bare_unscoped_search_may_return_source_row"] for item in report["scope_hazard_scenarios"]))

    def test_stratified_cap_keeps_prefix_and_internal_and_in_sa_candidate(self) -> None:
        report = build_collision_inventory(self.db_path, hazard_cap=2, scenario_limit=2)
        kinds = {item["hazard_kind"] for item in report["scope_label_hazards"]}
        self.assertEqual(kinds, {"prefix", "internal_compound"})
        self.assertTrue(any(item["hazard_kind"] == "prefix" and item["classification_scope_label"]["normalized_label"] == "\uc778\uc0ac" and item["off_path_leaf"]["normalized_label"] == "\uc778\uc0ac\ud558\uae30" for item in report["scope_label_hazards"]))

    def test_classification_collision_is_level_agnostic_but_branch_aware(self) -> None:
        report = build_collision_inventory(self.db_path, collision_cap=100)
        candidates = [item for item in report["collisions"] if item["collision_group"] == "classification:\uc778\uc0ac"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["distinct_path_count"], 2)

    def test_collision_cap_stratifies_classification_cross_path_cross_type_and_leaf(self) -> None:
        def row(stratum: str, label: str, major: str) -> dict:
            return {
                "collision_stratum": stratum,
                "normalized_label": label,
                "collision_id": f"id-{stratum}-{major}-{label}",
                "distinct_path_count": 2,
                "cross_path": stratum != "leaf",
                "cross_type": stratum == "cross_type",
                "paths": [{"path": {"major_code": major}}],
            }

        rows = [
            row("classification_cross_path", "발전설비설계", "15"),
            row("cross_type", "공통", "02"),
            row("leaf", "일반", "08"),
            row("leaf", "다른", "09"),
        ]
        selected = _stratified_collision_select(rows, 3)
        self.assertEqual(
            {item["collision_stratum"] for item in selected},
            {"classification_cross_path", "cross_type", "leaf"},
        )
        self.assertIn("발전설비설계", {item["normalized_label"] for item in selected})

    def test_scope_filter_stops_at_source_scope_level(self) -> None:
        report = build_collision_inventory(self.db_path, hazard_cap=100)
        middle_hazards = [item for item in report["scope_label_hazards"] if item["classification_scope_label"]["level"] == "middle"]
        self.assertTrue(middle_hazards)
        self.assertTrue(all(set(item["classification_scope_label"]["scope_filter"]) == {"major_code", "middle_code"} for item in middle_hazards))

    def test_empty_scope_is_unresolved(self) -> None:
        empty_dir = tempfile.TemporaryDirectory()
        try:
            empty_db = Path(empty_dir.name) / "empty.db"
            conn = sqlite3.connect(empty_db)
            conn.executescript(
                "CREATE TABLE classifications (classification_id INTEGER, major_code TEXT, major_name TEXT, middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT, sub_code TEXT, sub_name TEXT);"
                "CREATE TABLE competency_units (unit_code TEXT, unit_name_raw TEXT, classification_id INTEGER);"
                "CREATE TABLE competency_elements (element_id INTEGER, unit_code TEXT, element_name_raw TEXT);"
            )
            conn.close()
            report = build_collision_inventory(empty_db)
            self.assertEqual(report["scope"]["major_codes"], [])
            self.assertEqual(report["scope"]["major_count"], 0)
            self.assertFalse(report["scope"]["all_major_scope_covered"])
            self.assertEqual(report["scope"]["resolution"], "unresolved")
        finally:
            empty_dir.cleanup()

    def test_caps_bound_serialized_rows_but_totals_remain_exact_and_db_is_preserved(self) -> None:
        before = self.db_path.read_bytes()
        report = build_collision_inventory(self.db_path, top_n=1, collision_cap=1, paths_per_collision=1, hazard_cap=1, scenario_limit=1)
        self.assertLessEqual(len(report["collisions"]), 1)
        self.assertLessEqual(len(report["top_collisions"]), 1)
        self.assertLessEqual(len(report["scope_label_hazards"]), 1)
        self.assertLessEqual(len(report["scoped_containment_scenarios"]), 1)
        self.assertLessEqual(len(report["scope_hazard_scenarios"]), 1)
        self.assertTrue(report["metrics"]["collision_serialization_truncated"])
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_render_and_write_report(self) -> None:
        report = build_collision_inventory(self.db_path)
        self.assertIn("Off-path prefix/internal-compound hazards", render_markdown(report))
        out = Path(self.temp_dir.name) / "nested" / "report.json"
        md_out = Path(self.temp_dir.name) / "nested" / "report.md"
        written = write_report(self.db_path, out, md_out, top_n=3, collision_cap=3, scenario_limit=3)
        self.assertEqual(written["schema"], SCHEMA)
        self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["schema"], SCHEMA)


if __name__ == "__main__":
    unittest.main()
