from __future__ import annotations

import random
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ncs_mcp.db import initialize_database
from ncs_mcp.search.concept_index import (
    CONCEPT_MASK_BITS, CONCEPT_MASK_REQUIRED_MANIFEST, CONCEPT_MASK_TABLE,
    compatible_concept_masks, concept_like_mask, concept_text_mask,
    create_concept_mask_index,
)
from ncs_mcp.training_recommendation import resolve_ncs_query_scope


class ConceptCandidateMaskTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript("""
            CREATE TABLE ontology_concepts(concept_id INTEGER PRIMARY KEY,
                concept_name TEXT, normalized_key TEXT);
            CREATE TABLE serving_snapshot_manifest(manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL);
        """)

    def build(self, rows):
        self.conn.executemany("INSERT INTO ontology_concepts VALUES (?, ?, ?)", rows)
        count = create_concept_mask_index(self.conn)
        self.conn.executemany("INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                              [*CONCEPT_MASK_REQUIRED_MANIFEST.items(), ("concept_mask_rows", str(count))])
        return count

    def test_masks_are_portable_bounded_and_preserve_ascii_case_only(self):
        self.assertEqual(concept_text_mask("AbC"), concept_text_mask("aBc"))
        self.assertEqual(concept_text_mask("a", None), 0)
        self.assertLess(concept_text_mask("".join(chr(i) for i in range(10000))), 1 << CONCEPT_MASK_BITS)
        with self.assertRaises(TypeError):
            concept_text_mask(b"invalid blob")

    def test_every_native_like_match_is_a_mask_candidate(self):
        rng = random.Random(20261004)
        alphabet = "ABab가나_ %\\İÄäßΣσ가\u0307\0"
        documents = ["".join(rng.choices(alphabet, k=rng.randrange(2, 30))) for _ in range(140)]
        documents += ["FOO_bar", "Straße", "ＡＢ", "ab\0cd", "인사 관리", "a%b"]
        patterns = ["%", "_", "%A%", "%ab%", "%ab\0cd%", "%FOO_bar%", "%인사%", "%ß%", "%_AB_%"]
        for document in documents:
            patterns.append("%" + document[:rng.randrange(1, len(document) + 1)] + "%")
        for pattern in patterns:
            required = concept_like_mask(pattern)
            if required is None:
                continue
            for document in documents:
                if self.conn.execute("SELECT ? LIKE ?", (document, pattern)).fetchone()[0]:
                    self.assertEqual(concept_text_mask(document) & required, required, (document, pattern))

    def test_complete_sparse_ids_and_both_fields_are_indexed_without_source_edits(self):
        source = [(99, "Unrelated label", "target-key"), (1, "TARGET label", "different"), (400, None, None)]
        count = self.build(source)
        self.assertEqual(count, 3)
        self.assertEqual([tuple(row) for row in self.conn.execute("SELECT * FROM ontology_concepts ORDER BY concept_id")], sorted(source))
        self.assertIsNotNone(compatible_concept_masks(self.conn, ("%target%", "%TARGET%")))
        masks = dict(self.conn.execute(f"SELECT concept_id, mask FROM {CONCEPT_MASK_TABLE}"))
        need = concept_like_mask("%target%")
        self.assertEqual(masks[1] & need, need)
        self.assertEqual(masks[99] & need, need)
        self.assertEqual(masks[400], 0)
        with self.assertRaises(sqlite3.OperationalError):
            create_concept_mask_index(self.conn)

    def test_unindexable_or_branch_never_discards_matches(self):
        self.build([(1, "alpha", "alpha")])
        for patterns in (("%alpha%", "%a%"), ("%%", "%alpha%"), ("%a_b%", "%alpha%")):
            self.assertIsNone(compatible_concept_masks(self.conn, patterns))

    def test_missing_mismatched_or_malformed_attestation_falls_back(self):
        self.build([(1, "alpha", "alpha")])
        for key, value in [*CONCEPT_MASK_REQUIRED_MANIFEST.items(), ("concept_mask_rows", "1")]:
            with self.subTest(key=key):
                self.conn.execute("DELETE FROM serving_snapshot_manifest WHERE manifest_key=?", (key,))
                self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))
                self.conn.execute("INSERT INTO serving_snapshot_manifest VALUES (?, ?)", (key, "invalid"))
                self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))
                self.conn.execute("UPDATE serving_snapshot_manifest SET manifest_value=? WHERE manifest_key=?", (value, key))
        self.conn.execute("UPDATE serving_snapshot_manifest SET manifest_value=? WHERE manifest_key='concept_mask_rows'", ("9" * 5000,))
        self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))

    def test_missing_or_wrong_table_schema_falls_back(self):
        self.build([(1, "alpha", "alpha")])
        self.conn.execute(f"DROP TABLE {CONCEPT_MASK_TABLE}")
        self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))
        self.conn.execute(f"CREATE TABLE {CONCEPT_MASK_TABLE}(concept_id INTEGER, mask TEXT)")
        self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))

    def test_incompatible_manifest_columns_fall_back_without_failing_lookup(self):
        self.build([(1, "alpha", "alpha")])
        self.conn.execute("DROP TABLE serving_snapshot_manifest")
        for schema in ("key TEXT, value TEXT", "manifest_key TEXT, value TEXT"):
            with self.subTest(schema=schema):
                self.conn.execute(f"CREATE TABLE serving_snapshot_manifest({schema})")
                self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))
                self.conn.execute("DROP TABLE serving_snapshot_manifest")

    def test_overridden_like_is_not_assumed_to_use_ascii_matching(self):
        self.build([(1, "alpha", "alpha")])
        self.conn.create_function("like", 2, lambda pattern, value: 1)
        self.assertIsNone(compatible_concept_masks(self.conn, ("%alpha%", "%alpha%")))

    def test_scope_resolver_retains_complete_response_and_2000_row_cutoff(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        initialize_database(conn)
        rows = [(i, f"Target source {i}", f"targetsource{i}") for i in range(2300, 0, -1)]
        rows.extend((3000 + i, text, text) for i, text in enumerate(
            ["인사 관리", "Ｃ＋＋", "C++", "a_b%cd", "Straße", "가 능력", "ab\0cd"]))
        conn.executemany("""INSERT INTO ontology_concepts
            (concept_id,concept_name,normalized_key,concept_type,created_at,updated_at)
            VALUES (?,?,?,'knowledge','test','test')""", rows)
        count = create_concept_mask_index(conn)
        conn.execute("CREATE TABLE serving_snapshot_manifest(manifest_key TEXT PRIMARY KEY, manifest_value TEXT NOT NULL)")
        conn.executemany("INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
                         [*CONCEPT_MASK_REQUIRED_MANIFEST.items(), ("concept_mask_rows", str(count))])
        for query in ("Target", "인사", "C++", "a_b%cd", "Straße", "가", "ab\0cd", "a", "不存在的领域"):
            with self.subTest(query=query):
                with patch("ncs_mcp.training_recommendation.compatible_concept_masks", return_value=None):
                    original = resolve_ncs_query_scope(conn, query, limit=50)
                accelerated = resolve_ncs_query_scope(conn, query, limit=50)
                self.assertEqual(accelerated, original)


if __name__ == "__main__":
    unittest.main()
