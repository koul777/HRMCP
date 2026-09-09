import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from ncs_mcp.builder_validation import TABLES, validate_candidate


class BuilderValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'candidate.db'
        self.checkpoint = Path(self.temp.name) / 'validation.json'
        with closing(sqlite3.connect(self.db)) as conn:
            for table in TABLES:
                conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
                conn.execute(f'INSERT INTO "{table}" VALUES (1)')
            conn.commit()

    def test_interrupt_preserves_completed_validation_and_resume_skips_it(self):
        def interrupt(event):
            if event['stage'] == 'DB 참조 무결성 검증':
                raise KeyboardInterrupt()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}) as check:
            with self.assertRaises(KeyboardInterrupt):
                validate_candidate(self.db, self.checkpoint, interrupt)
            self.assertTrue(json.loads(self.checkpoint.read_text(encoding='utf-8'))['validation']['ok'])
            result = validate_candidate(self.db, self.checkpoint)
            self.assertEqual(check.call_count, 1)
        self.assertEqual(result['counts'], dict.fromkeys(TABLES, 1))
        self.assertEqual(len(result['sha256']), 64)

    def test_changed_bytes_invalidate_completed_checks(self):
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}) as check:
            original = validate_candidate(self.db, self.checkpoint)
            with closing(sqlite3.connect(self.db)) as conn:
                conn.execute('INSERT INTO competency_units VALUES (2)')
                conn.commit()
            result = validate_candidate(self.db, self.checkpoint)
            self.assertEqual(check.call_count, 2)
        self.assertNotEqual(original['sha256'], result['sha256'])
        self.assertEqual(result['counts']['competency_units'], 2)

    def test_each_count_is_saved_before_interruption(self):
        def interrupt(event):
            if event['stage'] == '테이블 건수 확인 완료 · competency_units':
                raise KeyboardInterrupt()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaises(KeyboardInterrupt):
                validate_candidate(self.db, self.checkpoint, interrupt)
            from ncs_mcp.builder_validation import _count
            with patch('ncs_mcp.builder_validation._count', wraps=_count) as count:
                validate_candidate(self.db, self.checkpoint)
                self.assertNotIn('competency_units', [call.args[1] for call in count.call_args_list])

    def test_empty_required_table_blocks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DELETE FROM ksa_items')
            conn.commit()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaisesRegex(ValueError, '필수 원천'):
                validate_candidate(self.db, self.checkpoint)

    def test_db_change_during_validation_blocks(self):
        def mutate(path):
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('INSERT INTO competency_units VALUES (2)')
                conn.commit()
            return {'ok': True}
        with patch('ncs_mcp.builder_validation.validate_ontology_database', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'DB가 변경'):
                validate_candidate(self.db, self.checkpoint)

    def test_foreign_key_violation_blocks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE orphan (parent INTEGER REFERENCES competency_units(id))')
            conn.execute('INSERT INTO orphan VALUES (999)')
            conn.commit()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaisesRegex(ValueError, '관계 무결성'):
                validate_candidate(self.db, self.checkpoint)

    def test_wal_mode_read_checks_do_not_invalidate_identity(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            result = validate_candidate(self.db, self.checkpoint)
        self.assertEqual(result['counts']['competency_units'], 1)


if __name__ == '__main__':
    unittest.main()
