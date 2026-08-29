from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from ncs_mcp.source_change_plan import ProjectionColumn, TableSpec, build_source_change_plan


ITEM_SPEC = TableSpec(
    name="items",
    from_sql='"items" AS t',
    key_columns=(ProjectionColumn("item_code", 't."item_code"'),),
    content_columns=(ProjectionColumn("label", 't."label"'), ProjectionColumn("level", 't."level"')),
    scope_columns=(ProjectionColumn("unit_code", 't."unit_code"'),),
)


class SourceChangePlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_db(
        self,
        name: str,
        rows: list[tuple[str, str, int, str]],
        *,
        extra_column: bool = False,
    ) -> Path:
        path = self.root / name
        with closing(sqlite3.connect(path)) as connection:
            suffix = ", note TEXT" if extra_column else ""
            connection.execute(
                f"""
                CREATE TABLE items (
                    item_code TEXT NOT NULL,
                    label TEXT NOT NULL,
                    level INTEGER NOT NULL,
                    unit_code TEXT NOT NULL
                    {suffix}
                )
                """
            )
            connection.executemany(
                "INSERT INTO items(item_code, label, level, unit_code) VALUES (?, ?, ?, ?)",
                rows,
            )
            connection.commit()
        return path

    def _plan(self, baseline: Path, candidate: Path, **kwargs: object) -> dict[str, object]:
        return build_source_change_plan(
            baseline,
            candidate,
            table_specs=(ITEM_SPEC,),
            minimum_table_changes_for_fallback=100,
            **kwargs,
        )

    def test_unchanged_database_needs_no_rebuild(self) -> None:
        rows = [("A", "alpha", 1, "U1"), ("B", "beta", 2, "U2")]
        baseline = self._create_db("baseline.db", rows)
        candidate = self._create_db("candidate.db", list(reversed(rows)))

        plan = self._plan(baseline, candidate)

        self.assertFalse(plan["full_rebuild_required"])
        self.assertFalse(plan["full_rebuild_recommended"])
        self.assertEqual(plan["suggested_strategy"], "no_rebuild")
        self.assertEqual(plan["totals"]["changed"], 0)
        self.assertFalse(plan["safety"]["database_writes"])

    def test_insert_update_delete_and_affected_unit_scopes(self) -> None:
        baseline = self._create_db(
            "baseline.db",
            [("A", "old", 1, "U1"), ("B", "remove", 1, "U2"), ("C", "same", 1, "U3")],
        )
        candidate = self._create_db(
            "candidate.db",
            [("A", "new", 1, "U1"), ("C", "same", 1, "U3"), ("D", "insert", 2, "U4")],
        )

        plan = self._plan(
            baseline,
            candidate,
            full_rebuild_change_ratio_threshold=1.0,
            per_table_change_ratio_threshold=1.0,
        )

        table = plan["tables"][0]
        self.assertEqual(table["counts"]["inserted"], 1)
        self.assertEqual(table["counts"]["updated"], 1)
        self.assertEqual(table["counts"]["deleted"], 1)
        self.assertEqual(table["affected_scopes"]["unit_code"]["values"], ["U1", "U2", "U4"])
        self.assertEqual(plan["suggested_strategy"], "incremental_rebuild")

    def test_schema_mismatch_requires_full_rebuild(self) -> None:
        rows = [("A", "alpha", 1, "U1")]
        baseline = self._create_db("baseline.db", rows)
        candidate = self._create_db("candidate.db", rows, extra_column=True)

        plan = self._plan(baseline, candidate)

        self.assertTrue(plan["full_rebuild_required"])
        self.assertEqual(plan["suggested_strategy"], "full_rebuild")
        self.assertEqual(plan["structural_reasons"][0]["code"], "schema_mismatch")

    def test_duplicate_stable_key_requires_full_rebuild(self) -> None:
        baseline = self._create_db("baseline.db", [("A", "alpha", 1, "U1")])
        candidate = self._create_db(
            "candidate.db",
            [("A", "alpha", 1, "U1"), ("A", "different", 2, "U2")],
        )

        plan = self._plan(baseline, candidate)

        self.assertTrue(plan["full_rebuild_required"])
        reason = next(reason for reason in plan["structural_reasons"] if reason["code"] == "duplicate_stable_key")
        self.assertEqual(reason["candidate_duplicate"]["key"], {"item_code": "A"})

    def test_change_ratio_recommends_full_fallback(self) -> None:
        baseline = self._create_db(
            "baseline.db",
            [("A", "alpha", 1, "U1"), ("B", "beta", 1, "U2")],
        )
        candidate = self._create_db(
            "candidate.db",
            [("A", "changed", 1, "U1"), ("B", "beta", 1, "U2")],
        )

        plan = build_source_change_plan(
            baseline,
            candidate,
            table_specs=(ITEM_SPEC,),
            full_rebuild_change_ratio_threshold=0.40,
            per_table_change_ratio_threshold=0.40,
            minimum_table_changes_for_fallback=1,
        )

        self.assertFalse(plan["full_rebuild_required"])
        self.assertTrue(plan["full_rebuild_recommended"])
        self.assertEqual(plan["suggested_strategy"], "full_rebuild")
        self.assertTrue(
            any(reason["code"] == "overall_change_ratio_exceeded" for reason in plan["recommendation_reasons"])
        )

    def test_missing_table_requires_full_rebuild(self) -> None:
        baseline = self._create_db("baseline.db", [("A", "alpha", 1, "U1")])
        candidate = self.root / "candidate.db"
        sqlite3.connect(candidate).close()

        plan = self._plan(baseline, candidate)

        self.assertTrue(plan["full_rebuild_required"])
        self.assertEqual(plan["structural_reasons"][0]["code"], "missing_table")


if __name__ == "__main__":
    unittest.main()
