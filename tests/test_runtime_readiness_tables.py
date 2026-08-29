from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ncs_mcp import server


class RuntimeReadinessTablesTests(unittest.TestCase):
    def _create_database(
        self,
        db_path: Path,
        *,
        extra_tables: tuple[str, ...] = (),
        empty_tables: tuple[str, ...] = (),
    ) -> None:
        conn = sqlite3.connect(db_path)
        try:
            for table_name in (*server.READINESS_CORE_TABLES, *extra_tables):
                conn.execute(f'CREATE TABLE "{table_name}" (id INTEGER)')
                if table_name not in empty_tables:
                    conn.execute(f'INSERT INTO "{table_name}" (id) VALUES (1)')
            conn.commit()
        finally:
            conn.close()

    def _metadata(self, db_path: Path, extra_tables: str | None) -> dict:
        with patch.dict(os.environ, {}, clear=False):
            if extra_tables is None:
                os.environ.pop(server.READINESS_EXTRA_TABLES_ENV, None)
            else:
                os.environ[server.READINESS_EXTRA_TABLES_ENV] = extra_tables
            return server.database_readiness_metadata(db_path)

    def test_default_core_table_readiness_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ready.db"
            self._create_database(db_path)

            metadata = self._metadata(db_path, None)

        self.assertTrue(metadata["ready"])
        self.assertEqual(list(server.READINESS_CORE_TABLES), metadata["required_tables"])
        self.assertEqual(
            list(server.READINESS_CORE_TABLES),
            list(metadata["core_tables"]),
        )
        self.assertTrue(
            all(
                item == {"exists": True, "row_count": 1}
                for item in metadata["core_tables"].values()
            )
        )
        self.assertNotIn("invalid_extra_tables", metadata)
        self.assertTrue(metadata["core_ready"])
        self.assertFalse(metadata["public_tools_ready"])
        self.assertIn("career_path", metadata["degraded_capabilities"])

    def test_public_capability_status_is_separate_from_core_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "public-ready.db"
            self._create_database(
                db_path,
                extra_tables=server.READINESS_PUBLIC_TOOL_TABLES,
            )

            metadata = self._metadata(db_path, None)

        self.assertTrue(metadata["ready"])
        self.assertTrue(metadata["core_ready"])
        self.assertTrue(metadata["public_tools_ready"])
        self.assertEqual([], metadata["degraded_capabilities"])
        self.assertTrue(
            all(item["available"] for item in metadata["capabilities"].values())
        )

    def test_populated_extra_tables_are_required_once_and_keep_order(self) -> None:
        extra_tables = ("ontology_concepts", "task_ksa_concept_relations")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ready.db"
            self._create_database(db_path, extra_tables=extra_tables)

            metadata = self._metadata(
                db_path,
                " ontology_concepts,task_ksa_concept_relations,ONTOLOGY_CONCEPTS,competency_units ",
            )

        self.assertTrue(metadata["ready"])
        self.assertEqual(
            [*server.READINESS_CORE_TABLES, *extra_tables],
            metadata["required_tables"],
        )
        self.assertEqual(
            {"exists": True, "row_count": 1},
            metadata["core_tables"]["ontology_concepts"],
        )
        self.assertNotIn("ONTOLOGY_CONCEPTS", metadata["core_tables"])

    def test_empty_extra_table_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "not-ready.db"
            self._create_database(
                db_path,
                extra_tables=("ontology_concepts",),
                empty_tables=("ontology_concepts",),
            )

            metadata = self._metadata(db_path, "ontology_concepts")

        self.assertFalse(metadata["ready"])
        self.assertEqual(
            {"exists": True, "row_count": 0},
            metadata["core_tables"]["ontology_concepts"],
        )
        self.assertEqual("database_not_ready", metadata["error"]["code"])

    def test_missing_extra_table_blocks_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "not-ready.db"
            self._create_database(db_path)

            metadata = self._metadata(db_path, "ontology_concepts")

        self.assertFalse(metadata["ready"])
        self.assertEqual(
            {"exists": False, "row_count": None},
            metadata["core_tables"]["ontology_concepts"],
        )
        self.assertEqual("database_not_ready", metadata["error"]["code"])

    def test_view_extra_table_is_counted_as_a_servable_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "view-ready.db"
            self._create_database(db_path)
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("CREATE TABLE ontology_concepts_source (id INTEGER)")
                conn.execute("INSERT INTO ontology_concepts_source VALUES (1)")
                conn.execute(
                    "CREATE VIEW ontology_concepts AS "
                    "SELECT id FROM ontology_concepts_source"
                )
                conn.commit()

            metadata = self._metadata(db_path, "ontology_concepts")

        self.assertTrue(metadata["ready"])
        self.assertEqual(
            {"exists": True, "row_count": 1},
            metadata["core_tables"]["ontology_concepts"],
        )

    def test_malicious_identifiers_are_reported_and_never_queried(self) -> None:
        valid_extra = "ontology_concepts"
        invalid_names = (
            "competency_units; DROP TABLE ncs_training_courses;--",
            "foo.bar",
            '"quoted"',
            "name with spaces",
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ready.db"
            self._create_database(db_path, extra_tables=(valid_extra,))
            configured = ",".join((valid_extra, *invalid_names))

            metadata = self._metadata(db_path, configured)

            conn = sqlite3.connect(db_path)
            try:
                training_rows = conn.execute(
                    "SELECT COUNT(*) FROM ncs_training_courses"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertTrue(metadata["ready"])
        self.assertEqual(list(invalid_names), metadata["invalid_extra_tables"])
        self.assertEqual(
            [*server.READINESS_CORE_TABLES, valid_extra],
            metadata["required_tables"],
        )
        self.assertTrue(all(name not in metadata["core_tables"] for name in invalid_names))
        self.assertEqual(1, training_rows)


if __name__ == "__main__":
    unittest.main()
