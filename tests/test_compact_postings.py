from __future__ import annotations

import sqlite3
import unittest

from ncs_mcp.compact_postings import (
    compact_ontology_relation_rows,
    concept_criteria_ids,
    criteria_concept_ids,
    decode_posting_ids,
    encode_posting_ids,
)


class CompactPostingCodecTests(unittest.TestCase):
    def test_delta_uvarint_round_trip_is_sorted_and_unique(self) -> None:
        values = [70000, 0, 128, 127, 128, 4, 900000]
        payload = encode_posting_ids(values)
        self.assertEqual(sorted(set(values)), decode_posting_ids(payload))
        self.assertLess(len(payload), len(set(values)) * 8)

    def test_decode_rejects_truncated_or_oversized_payloads(self) -> None:
        with self.assertRaisesRegex(ValueError, "truncated"):
            decode_posting_ids(b"\x80")
        with self.assertRaisesRegex(ValueError, "oversized"):
            decode_posting_ids(b"\x81" * 10 + b"\x00")

    def test_runtime_reads_forward_inverse_and_relation_postings(self) -> None:
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(
                """
                CREATE TABLE criteria_concept_forward (
                    criteria_id INTEGER PRIMARY KEY,
                    concept_count INTEGER NOT NULL,
                    concept_ids BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE criteria_concept_inverse (
                    concept_id INTEGER PRIMARY KEY,
                    criteria_count INTEGER NOT NULL,
                    criteria_ids BLOB NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE ontology_relation_types (
                    relation_type_code INTEGER PRIMARY KEY,
                    relation_type TEXT NOT NULL UNIQUE
                );
                INSERT INTO ontology_relation_types VALUES
                    (1, 'knowledge_enables_skill'),
                    (2, 'co_required_in_element');
                CREATE TABLE ontology_relation_outgoing (
                    source_concept_id INTEGER NOT NULL,
                    relation_type_code INTEGER NOT NULL,
                    target_count INTEGER NOT NULL,
                    target_ids BLOB NOT NULL,
                    PRIMARY KEY(source_concept_id, relation_type_code)
                ) WITHOUT ROWID;
                CREATE TABLE ontology_relation_incoming (
                    target_concept_id INTEGER NOT NULL,
                    relation_type_code INTEGER NOT NULL,
                    source_count INTEGER NOT NULL,
                    source_ids BLOB NOT NULL,
                    PRIMARY KEY(target_concept_id, relation_type_code)
                ) WITHOUT ROWID;
                """
            )
            conn.execute(
                "INSERT INTO criteria_concept_forward VALUES (?, ?, ?)",
                (10, 3, encode_posting_ids([2, 5, 9])),
            )
            for concept_id in (2, 5, 9):
                conn.execute(
                    "INSERT INTO criteria_concept_inverse VALUES (?, ?, ?)",
                    (concept_id, 1, encode_posting_ids([10])),
                )
            conn.execute(
                "INSERT INTO ontology_relation_outgoing VALUES (?, ?, ?, ?)",
                (2, 1, 2, encode_posting_ids([5, 9])),
            )
            for target_id in (5, 9):
                conn.execute(
                    "INSERT INTO ontology_relation_incoming VALUES (?, ?, ?, ?)",
                    (target_id, 1, 1, encode_posting_ids([2])),
                )

            self.assertEqual({10: [2, 5, 9]}, criteria_concept_ids(conn, [10]))
            self.assertEqual({5: [10]}, concept_criteria_ids(conn, [5]))
            rows = compact_ontology_relation_rows(
                conn,
                source_ids=[2],
                target_ids=[5, 9],
            )
            self.assertEqual(
                [(2, 5), (2, 9)],
                [
                    (row["source_concept_id"], row["target_concept_id"])
                    for row in rows
                ],
            )
            self.assertTrue(all(row["review_status"] == "candidate" for row in rows))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
