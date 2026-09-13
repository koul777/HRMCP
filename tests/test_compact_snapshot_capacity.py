from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing

from scripts import export_interview_serving_db as exporter


class CompactSnapshotIntegerKeyTests(unittest.TestCase):
    def test_integer_key_preserves_sparse_ids_types_nulls_and_payloads(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE input(id INTEGER, raw TEXT, nullable TEXT, payload BLOB, score REAL)')
            rows = [(900000, '보존할 원문', None, b'\x00\xff', 1.25),
                    (-9, 'first', '', b'', None),
                    (2**63-1, 'last', 'reviewed', b'a', -0.25)]
            conn.executemany('INSERT INTO input VALUES (?,?,?,?,?)', rows)
            select = 'SELECT id, raw, nullable, payload, score, NULL AS projected FROM input'
            conn.execute(f'CREATE TABLE reference AS {select}')
            exporter._create_projected_table(conn, 'compact', select, integer_primary_key='id')
            self.assertEqual(conn.execute('SELECT * FROM reference ORDER BY id').fetchall(),
                             conn.execute('SELECT * FROM compact ORDER BY id').fetchall())
            before = conn.execute('PRAGMA table_info(reference)').fetchall()
            after = conn.execute('PRAGMA table_info(compact)').fetchall()
            self.assertEqual(before[1:], after[1:])
            self.assertEqual(('INTEGER', 1), (after[0][2], after[0][5]))
            self.assertEqual([], conn.execute('PRAGMA index_list(compact)').fetchall())
            plan = conn.execute('EXPLAIN QUERY PLAN SELECT * FROM compact WHERE id=?', (900000,)).fetchall()
            self.assertTrue(any('INTEGER PRIMARY KEY' in row[3] for row in plan), plan)

    def test_invalid_ids_fail_without_silent_conversion_or_row_loss(self):
        for ids in [(None,), ('7',), (1.5,), (b'1',), (1, 1)]:
            with self.subTest(ids=ids), closing(sqlite3.connect(':memory:')) as conn:
                conn.execute('CREATE TABLE input(id, raw TEXT)')
                conn.executemany('INSERT INTO input VALUES (?, ?)', [(value, 'unchanged') for value in ids])
                before = conn.execute('SELECT * FROM input').fetchall()
                with self.assertRaisesRegex(RuntimeError, 'unique non-null integers'):
                    exporter._create_projected_table(conn, 'compact', 'SELECT * FROM input', integer_primary_key='id')
                self.assertEqual(before, conn.execute('SELECT * FROM input').fetchall())
                self.assertEqual(0, conn.execute('SELECT count(*) FROM compact').fetchone()[0])

    def test_empty_input_retains_integer_key_and_column_shape(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute('CREATE TABLE input(id INTEGER, raw TEXT)')
            exporter._create_projected_table(conn, 'compact', 'SELECT * FROM input', integer_primary_key='id')
            self.assertEqual(0, conn.execute('SELECT count(*) FROM compact').fetchone()[0])
            self.assertEqual(1, conn.execute('PRAGMA table_info(compact)').fetchone()[5])


if __name__ == '__main__':
    unittest.main()
