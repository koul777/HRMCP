from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.search import core as search_core  # noqa: E402


class NcsScopeResolverPerformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE classifications (
                classification_id INTEGER PRIMARY KEY,
                major_code TEXT, major_name TEXT,
                middle_code TEXT, middle_name TEXT,
                small_code TEXT, small_name TEXT,
                sub_code TEXT, sub_name TEXT,
                duty_order TEXT
            );
            CREATE TABLE competency_units (
                unit_code TEXT PRIMARY KEY,
                unit_name_raw TEXT,
                api_definition TEXT,
                unit_level_raw TEXT,
                classification_id INTEGER
            );
            CREATE TABLE ncs_query_aliases (
                unit_code TEXT, alias_text TEXT, normalized_query TEXT
            );
            """
        )
        self.conn.executemany(
            "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                (1, "01", "Engineering", "01", "Design", "01", "Power", "01", "Target Job", "1"),
                (2, "02", "Operations", "01", "Design", "01", "Power", "01", "Other Target Job", "1"),
                (3, "03", "Operations", "01", "Design", "01", "Repeated", "01", "Repeated", "1"),
                (4, "04", "Only Other", "01", "Other", "01", "Other", "01", "Different", "1"),
                (5, "05", "Cross One", "01", "Design", "01", "Power", "01", "Cross Branch", "1"),
                (6, "06", "Cross Two", "01", "Design", "01", "Cross Branch", "01", "Different", "1"),
                # A separate canonical row repeats the label at the major
                # level on the same branch as row 3's deepest exact node.
                (7, "03", "Repeated", "99", "Other", "99", "Other", "99", "Different", "1"),
            ),
        )
        self.conn.execute(
            "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
            ("UNIT_ONLY", "Unit Only", "", "4", 4),
        )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()

    @staticmethod
    def _resolve(conn: sqlite3.Connection, **kwargs):
        return search_core.resolve_ncs_search_context(conn, **kwargs)

    def test_unique_exact_classification_avoids_unit_group_concat(self) -> None:
        sql: list[str] = []
        self.conn.set_trace_callback(sql.append)

        result = self._resolve(self.conn, job_scope=" target-job ")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolver_version"], "ncs-classification-context-resolver-v2")
        self.assertEqual(result["selected_candidate"]["sub_code"], "01")
        self.assertEqual(
            result["selected_candidate"]["match_basis"],
            ["job_scope_exact_sub_name"],
        )
        self.assertFalse(any("GROUP_CONCAT" in statement for statement in sql))

    def test_same_branch_ancestor_descendant_keeps_deepest_node(self) -> None:
        result = self._resolve(self.conn, job_scope="Repeated")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(
            result["selected_candidate"]["path_label"],
            "Operations > Design > Repeated > Repeated",
        )
        self.assertEqual(result["selected_candidate"]["match_basis"], ["job_scope_exact_sub_name"])
        self.assertEqual(result["alternative_candidates"], [])
        self.assertEqual(result["alternative_count"], 0)
        self.assertEqual(result["resolution_margin"], 1.0)

    def test_exact_fast_path_matches_legacy_binding_payload(self) -> None:
        fast = self._resolve(self.conn, job_scope="Target Job")
        with patch.object(search_core, "_ncs_resolve_exact_classification_scope", return_value=None):
            legacy = self._resolve(self.conn, job_scope="Target Job")

        for field in (
            "selected_candidate",
            "alternative_candidates",
            "alternative_count",
            "resolution_margin",
            "status",
            "needs_context",
        ):
            self.assertEqual(fast.get(field), legacy.get(field), field)

    def test_cross_branch_exact_duplicates_fail_closed_as_ambiguous(self) -> None:
        result = self._resolve(self.conn, job_scope="Cross Branch")

        self.assertEqual(result["status"], "ambiguous")
        self.assertIsNone(result["selected_candidate"])
        self.assertGreaterEqual(result["alternative_count"], 2)

    def test_classification_filter_conflict_is_preserved(self) -> None:
        result = self._resolve(
            self.conn,
            job_scope="Target Job",
            classification_filter={"major_code": "99"},
        )

        self.assertEqual(result["status"], "conflict")
        self.assertTrue(result["needs_context"])
        self.assertIn("context_conflicts_with_hard_filter", result["warnings"])
        self.assertEqual(result["alternative_count"], 0)
        self.assertEqual(result["alternative_candidates"], [])
        self.assertEqual(result["resolution_margin"], 0.0)

    def test_classification_filter_can_select_one_cross_branch_exact_path(self) -> None:
        result = self._resolve(
            self.conn,
            job_scope="Cross Branch",
            classification_filter={"major_code": "05"},
        )

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["selected_candidate"]["major_code"], "05")
        self.assertEqual(result["alternative_count"], 0)
        self.assertEqual(result["alternative_candidates"], [])
        self.assertEqual(result["resolution_margin"], 1.0)

    def test_non_classification_exact_scope_resolves_official_unit_without_group_concat(self) -> None:
        sql: list[str] = []
        self.conn.set_trace_callback(sql.append)

        result = self._resolve(self.conn, job_scope="Unit Only")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["selected_candidate"]["path_label"], "Only Other > Other > Other > Different")
        self.assertEqual(result["selected_candidate"]["match_basis"], ["job_scope_exact_unit_name"])
        self.assertEqual(result["alternative_candidates"], [])
        self.assertEqual(result["alternative_count"], 0)
        self.assertEqual(result["resolution_margin"], 1.0)
        self.assertFalse(any("GROUP_CONCAT" in statement for statement in sql))

    def test_classification_prefix_does_not_promote_element_like_scope(self) -> None:
        self.conn.execute(
            "UPDATE classifications SET sub_name = '인사' WHERE classification_id = 1"
        )
        self.conn.commit()
        result = self._resolve(self.conn, job_scope="인사하기")

        self.assertEqual(result["status"], "unresolved")
        self.assertTrue(result["needs_context"])
        self.assertIsNone(result["selected_candidate"])

    def test_exact_unit_scope_wins_over_broad_classification_prefix(self) -> None:
        self.conn.execute(
            "UPDATE classifications SET middle_name = '사회복지', sub_name = '사회복지조직운영' WHERE classification_id = 4"
        )
        self.conn.execute(
            "UPDATE competency_units SET unit_name_raw = '사회복지조직 인사관리' WHERE unit_code = 'UNIT_ONLY'"
        )
        self.conn.commit()
        result = self._resolve(self.conn, job_scope="사회복지조직 인사관리")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["selected_candidate"]["match_basis"], ["job_scope_exact_unit_name"])
        self.assertIn("사회복지 >", result["selected_candidate"]["path_label"])
        self.assertTrue(result["selected_candidate"]["path_label"].endswith("사회복지조직운영"))


if __name__ == "__main__":
    unittest.main()
