from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.compact_storage import create_atomic_storage  # noqa: E402


class CompactAtomicStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source.db"
        with closing(sqlite3.connect(self.source)) as conn:
            conn.executescript(
                """
                CREATE TABLE ksa_items (
                    ksa_id INTEGER PRIMARY KEY,
                    element_id INTEGER NOT NULL,
                    ksa_type_name TEXT NOT NULL,
                    ksa_text_raw TEXT NOT NULL
                );
                INSERT INTO ksa_items VALUES
                    (1, 10, 'knowledge', 'same raw text'),
                    (2, 10, 'skill', 'long raw skill phrase');

                CREATE TABLE ontology_concepts (
                    concept_id INTEGER PRIMARY KEY,
                    normalized_key TEXT NOT NULL
                );
                INSERT INTO ontology_concepts VALUES
                    (101, 'samerawtext'),
                    (102, 'shortskill');

                CREATE TABLE ksa_atomic_items (
                    atomic_id INTEGER PRIMARY KEY,
                    ksa_id INTEGER NOT NULL,
                    element_id INTEGER NOT NULL,
                    ksa_type_name TEXT NOT NULL,
                    atom_index INTEGER NOT NULL,
                    atom_text TEXT NOT NULL,
                    normalized_key TEXT NOT NULL,
                    split_method TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO ksa_atomic_items VALUES
                    (11, 1, 10, 'knowledge', 1, 'same raw text',
                     'samerawtext', 'rule_based', 'raw', '2026-01-01'),
                    (12, 2, 10, 'skill', 1, 'short skill',
                     'shortskill', 'semicolon', 'raw', '2026-01-01');

                CREATE TABLE ksa_atomic_concept_links (
                    link_id INTEGER PRIMARY KEY,
                    atomic_id INTEGER NOT NULL,
                    concept_id INTEGER NOT NULL,
                    link_status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                INSERT INTO ksa_atomic_concept_links VALUES
                    (21, 11, 101, 'raw', '2026-01-02T03:04:05+00:00'),
                    (22, 12, 102, 'raw', '2026-01-02T03:04:05+00:00');
                """
            )
            conn.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _connections(self):
        src = sqlite3.connect(self.source)
        dst = sqlite3.connect(":memory:")
        dst.execute("ATTACH DATABASE ? AS source", (str(self.source),))
        dst.executescript(
            """
            CREATE TABLE ksa_items AS SELECT * FROM source.ksa_items;
            CREATE TABLE ontology_concepts AS
            SELECT * FROM source.ontology_concepts;
            """
        )
        return src, dst

    def test_atomic_views_are_lossless_including_timestamps(self) -> None:
        src, dst = self._connections()
        with closing(src), closing(dst):
            metrics = create_atomic_storage(src, dst)
            self.assertEqual(
                [(11, None), (12, "short skill")],
                dst.execute(
                    """
                    SELECT atomic_id, atom_text_override
                    FROM ksa_atomic_facts_compact ORDER BY atomic_id
                    """
                ).fetchall(),
            )
            self.assertEqual(
                src.execute(
                    """
                    SELECT atomic_id, ksa_id, element_id, ksa_type_name,
                           atom_index, atom_text, normalized_key, split_method,
                           review_status
                    FROM ksa_atomic_items ORDER BY atomic_id
                    """
                ).fetchall(),
                dst.execute(
                    """
                    SELECT atomic_id, ksa_id, element_id, ksa_type_name,
                           atom_index, atom_text, normalized_key, split_method,
                           review_status
                    FROM ksa_atomic_items ORDER BY atomic_id
                    """
                ).fetchall(),
            )
            self.assertEqual(
                [("2026-01-01",), ("2026-01-01",)],
                dst.execute(
                    "SELECT created_at FROM ksa_atomic_items ORDER BY atomic_id"
                ).fetchall(),
            )
            self.assertEqual(
                src.execute(
                    "SELECT * FROM ksa_atomic_concept_links ORDER BY link_id"
                ).fetchall(),
                dst.execute(
                    "SELECT * FROM ksa_atomic_concept_links ORDER BY link_id"
                ).fetchall(),
            )
            self.assertEqual(2, metrics["servable_counts"]["ksa_atomic_items"])
            self.assertEqual(1, metrics["atom_text_override_count"])

    def test_atomic_guard_rejects_more_than_one_link(self) -> None:
        with closing(sqlite3.connect(self.source)) as conn:
            conn.execute(
                "INSERT INTO ksa_atomic_concept_links VALUES "
                "(23, 11, 102, 'raw', '2026-01-02T03:04:05+00:00')"
            )
            conn.commit()
        src, dst = self._connections()
        with closing(src), closing(dst):
            with self.assertRaisesRegex(RuntimeError, "exactly one concept link"):
                create_atomic_storage(src, dst)


if __name__ == "__main__":
    unittest.main()
