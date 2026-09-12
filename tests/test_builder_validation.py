import json
import copy
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ncs_mcp.builder_validation import TABLES, validate_candidate
from ncs_mcp.builder_authorization import BuilderAuthorizationError
from ncs_mcp.data_builder import DataBuilder


class BuilderValidationTests(unittest.TestCase):
    def test_revoked_context_blocks_before_writable_connection(self):
        with self.builder.exclusive('resume', self.version) as context:
            self.revoke()
            with patch('ncs_mcp.builder_validation.sqlite3.connect') as connection:
                with self.assertRaises(BuilderAuthorizationError):
                    validate_candidate(self.db, self.checkpoint, builder_context=context)
                connection.assert_not_called()
        self.assertFalse(self.checkpoint.exists())

    def test_revocation_after_validation_blocks_checkpoint_write(self):
        def revoke(path):
            self.revoke()
            return {'ok': True}
        with self.builder.exclusive('resume', self.version) as context:
            with patch('ncs_mcp.builder_validation.validate_ontology_database', side_effect=revoke):
                with self.assertRaises(BuilderAuthorizationError):
                    validate_candidate(self.db, self.checkpoint, builder_context=context)
        self.assertFalse(self.checkpoint.exists())

    def test_revocation_before_atomic_replace_preserves_existing_checkpoint(self):
        from ncs_mcp.builder_validation import _atomic_checkpoint
        self.checkpoint.write_text('original', encoding='utf-8')
        checks = 0
        def authorize():
            nonlocal checks
            checks += 1
            if checks == 3:
                raise BuilderAuthorizationError('revoked')
        with self.assertRaises(BuilderAuthorizationError):
            _atomic_checkpoint(self.checkpoint, {'complete': True}, authorize)
        self.assertEqual(self.checkpoint.read_text(), 'original')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.builder = DataBuilder(self.root)
        self.version = '20260912_abcd'
        self.folder = self.builder.state / 'versions' / self.version
        self.folder.mkdir(parents=True)
        self.db = self.folder / 'ncs.db'
        self.checkpoint = self.folder / 'validation-checkpoint.json'
        with closing(sqlite3.connect(self.db)) as conn:
            for table in TABLES:
                conn.execute(f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY)')
                conn.execute(f'INSERT INTO "{table}" VALUES (1)')
            conn.commit()

    def revoke(self):
        (self.builder.state / 'operation.lock').write_text('{"operation_id":"replacement"}')

    def validate(self, progress=None):
        with self.builder.exclusive('resume', self.version) as context:
            return validate_candidate(self.db, self.checkpoint, progress, builder_context=context)

    def snapshot(self):
        return {path.relative_to(self.root): path.read_bytes() if path.is_file() else None
                for path in self.root.rglob('*')}

    def test_missing_or_forged_context_does_not_touch_db_wal_or_checkpoint(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('INSERT INTO competency_units VALUES (2)')
            conn.commit()
            self.checkpoint.write_text('existing checkpoint')
            before = self.snapshot()
            with self.assertRaises(TypeError):
                validate_candidate(self.db, self.checkpoint)
            for context in (None, {}, object()):
                with self.subTest(context=type(context).__name__):
                    with self.assertRaises(BuilderAuthorizationError):
                        validate_candidate(self.db, self.checkpoint, builder_context=context)
            with self.assertRaises(TypeError):
                validate_candidate(self.db, self.checkpoint, authorize=lambda: None)
            self.assertEqual(before, self.snapshot())

    def test_copied_and_expired_context_does_not_touch_files(self):
        with self.builder.exclusive('resume', self.version) as context:
            before = self.snapshot()
            with self.assertRaises(BuilderAuthorizationError):
                validate_candidate(self.db, self.checkpoint, builder_context=copy.copy(context))
            self.assertEqual(before, self.snapshot())
        before = self.snapshot()
        with self.assertRaises(BuilderAuthorizationError):
            validate_candidate(self.db, self.checkpoint, builder_context=context)
        self.assertEqual(before, self.snapshot())

    def test_missing_candidate_does_not_create_db_checkpoint_or_parent(self):
        self.db.unlink()
        self.folder.rmdir()
        with self.builder.exclusive('resume', self.version) as context:
            with self.assertRaises(FileNotFoundError):
                validate_candidate(self.db, self.checkpoint, builder_context=context)
        self.assertFalse(self.folder.exists())

    def test_context_cannot_validate_another_version_or_external_path(self):
        for action, db in (
            ('publish', self.db),
            ('resume', self.folder.with_name('20260912_dcba') / 'ncs.db'),
            ('resume', self.root / 'outside' / 'ncs.db'),
            ('resume', self.folder / 'other.db'),
        ):
            with self.subTest(action=action, db=db):
                with self.builder.exclusive(action, self.version) as context:
                    before = self.snapshot()
                    with self.assertRaises(BuilderAuthorizationError):
                        validate_candidate(db, db.parent / 'validation-checkpoint.json',
                                           builder_context=context)
                    self.assertEqual(before, self.snapshot())

    def test_each_supported_live_builder_action_can_validate_its_candidate(self):
        for action in ('build_delta', 'refresh_api', 'copy_current', 'resume'):
            with self.subTest(action=action):
                with self.builder.exclusive(action, self.version) as context:
                    with patch('ncs_mcp.builder_validation.validate_ontology_database',
                               return_value={'ok': True}):
                        result = validate_candidate(self.db, self.checkpoint, builder_context=context)
                self.assertEqual(result['counts'], dict.fromkeys(TABLES, 1))

    def test_checkpoint_cannot_overwrite_candidate_source_or_external_file(self):
        for checkpoint in (self.db, self.folder / 'source.xlsx', self.folder / 'build.json',
                           self.root / 'outside' / 'validation-checkpoint.json'):
            with self.subTest(checkpoint=checkpoint):
                with self.builder.exclusive('resume', self.version) as context:
                    before = self.snapshot()
                    with self.assertRaises(BuilderAuthorizationError):
                        validate_candidate(self.db, checkpoint, builder_context=context)
                    self.assertEqual(before, self.snapshot())

    def test_hardlinked_candidate_or_sidecar_is_rejected_before_connection(self):
        external = self.root / 'external.db'
        for target in (self.db, Path(str(self.db) + '-wal'), self.checkpoint):
            with self.subTest(target=target):
                if target == self.db:
                    os.link(self.db, external)
                else:
                    external.write_bytes(b'external data')
                    os.link(external, target)
                try:
                    with self.builder.exclusive('resume', self.version) as context:
                        before = self.snapshot()
                        with patch('ncs_mcp.builder_validation.sqlite3.connect') as connection:
                            with self.assertRaises(BuilderAuthorizationError):
                                validate_candidate(self.db, self.checkpoint, builder_context=context)
                            connection.assert_not_called()
                        self.assertEqual(before, self.snapshot())
                finally:
                    if target != self.db:
                        target.unlink()
                    external.unlink()

    def test_file_reparse_attribute_blocks_before_writable_connection(self):
        original_lstat = Path.lstat

        def reparse(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == self.db:
                return SimpleNamespace(st_mode=info.st_mode, st_nlink=info.st_nlink,
                                       st_file_attributes=0x400)
            return info

        with self.builder.exclusive('resume', self.version) as context:
            before = self.snapshot()
            with patch.object(Path, 'lstat', reparse), \
                    patch('ncs_mcp.builder_validation.sqlite3.connect') as connection:
                with self.assertRaises(BuilderAuthorizationError):
                    validate_candidate(self.db, self.checkpoint, builder_context=context)
                connection.assert_not_called()
            self.assertEqual(before, self.snapshot())

    def test_symlink_candidate_blocks_before_writable_connection(self):
        external = self.root / 'external.db'
        external.write_bytes(self.db.read_bytes())
        self.db.unlink()
        try:
            self.db.symlink_to(external)
        except OSError as exc:
            self.db.write_bytes(external.read_bytes())
            self.skipTest(f'File symlink creation unavailable: {exc}')
        with self.builder.exclusive('resume', self.version) as context:
            before = self.snapshot()
            with patch('ncs_mcp.builder_validation.sqlite3.connect') as connection:
                with self.assertRaises(BuilderAuthorizationError):
                    validate_candidate(self.db, self.checkpoint, builder_context=context)
                connection.assert_not_called()
            self.assertEqual(before, self.snapshot())

    def test_progress_revocation_before_wal_checkpoint_blocks_connection(self):
        def revoke(event):
            self.revoke()
        with self.builder.exclusive('resume', self.version) as context:
            with patch('ncs_mcp.builder_validation.sqlite3.connect') as connection:
                with self.assertRaises(BuilderAuthorizationError):
                    validate_candidate(self.db, self.checkpoint, revoke, builder_context=context)
                connection.assert_not_called()

    def test_interrupt_preserves_completed_validation_and_resume_skips_it(self):
        def interrupt(event):
            if event['stage'] == 'DB 참조 무결성 검증':
                raise KeyboardInterrupt()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}) as check:
            with self.assertRaises(KeyboardInterrupt):
                self.validate(interrupt)
            self.assertTrue(json.loads(self.checkpoint.read_text(encoding='utf-8'))['validation']['ok'])
            result = self.validate()
            self.assertEqual(check.call_count, 1)
        self.assertEqual(result['counts'], dict.fromkeys(TABLES, 1))
        self.assertEqual(len(result['sha256']), 64)

    def test_changed_bytes_invalidate_completed_checks(self):
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}) as check:
            original = self.validate()
            with closing(sqlite3.connect(self.db)) as conn:
                conn.execute('INSERT INTO competency_units VALUES (2)')
                conn.commit()
            result = self.validate()
            self.assertEqual(check.call_count, 2)
        self.assertNotEqual(original['sha256'], result['sha256'])
        self.assertEqual(result['counts']['competency_units'], 2)

    def test_each_count_is_saved_before_interruption(self):
        def interrupt(event):
            if event['stage'] == '테이블 건수 확인 완료 · competency_units':
                raise KeyboardInterrupt()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaises(KeyboardInterrupt):
                self.validate(interrupt)
            from ncs_mcp.builder_validation import _count
            with patch('ncs_mcp.builder_validation._count', wraps=_count) as count:
                self.validate()
                self.assertNotIn('competency_units', [call.args[1] for call in count.call_args_list])

    def test_empty_required_table_blocks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('DELETE FROM ksa_items')
            conn.commit()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaisesRegex(ValueError, '필수 원천'):
                self.validate()

    def test_db_change_during_validation_blocks(self):
        def mutate(path):
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('INSERT INTO competency_units VALUES (2)')
                conn.commit()
            return {'ok': True}
        with patch('ncs_mcp.builder_validation.validate_ontology_database', side_effect=mutate):
            with self.assertRaisesRegex(ValueError, 'DB가 변경'):
                self.validate()

    def test_foreign_key_violation_blocks(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('CREATE TABLE orphan (parent INTEGER REFERENCES competency_units(id))')
            conn.execute('INSERT INTO orphan VALUES (999)')
            conn.commit()
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            with self.assertRaisesRegex(ValueError, '관계 무결성'):
                self.validate()

    def test_wal_mode_read_checks_do_not_invalidate_identity(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        with patch('ncs_mcp.builder_validation.validate_ontology_database', return_value={'ok': True}):
            result = self.validate()
        self.assertEqual(result['counts']['competency_units'], 1)


if __name__ == '__main__':
    unittest.main()
