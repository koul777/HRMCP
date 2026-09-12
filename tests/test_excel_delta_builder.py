import hashlib
import copy
import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.excel_delta_builder import build_excel_delta
from ncs_mcp.ontology_refresh_builder import _run_pipeline
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer
from ncs_mcp.data_builder import DataBuilder, BuilderError
from ncs_mcp.builder_authorization import BuilderAuthorizationError
from ncs_mcp.api_refresh_builder import file_sha256, raw_ksa_sha256, trusted_review_status_identity_digest


def row(unit, text='기초 지식'):
    return dict(zip(HEADER_ALIASES, ['02', '경영', '01', '기획', '01', '계획', '01', '전략',
                                   unit, '전략 수립', '3', unit + '.1', '분석하기', '3',
                                   '1', '자료를 분석한다', 'K', '지식', '1', text]))


class ExcelDeltaTests(unittest.TestCase):
    def test_candidate_evidence_binds_file_raw_and_trusted_identity(self):
        result = self.build(self.initial)
        db = Path(result['candidate_db'])
        self.assertEqual(result['candidate_integrity'], {
            'sha256': file_sha256(db), 'raw_ksa_sha256': raw_ksa_sha256(db),
            'trusted_review_status_identity_digest': trusted_review_status_identity_digest(db),
        })

    def test_active_excel_rechecks_after_progress_before_retirement(self):
        builder = DataBuilder(self.root)
        replacement = {'operation_id': 'replacement'}
        def revoke(message):
            if isinstance(message, dict) and message.get('stage') == '변경 원천 의존 관계 정리':
                (builder.state / 'operation.lock').write_text(json.dumps(replacement))
        builder.progress = revoke
        upload = self.workbook([dict(self.initial[0], ksa_text='changed'), self.initial[1]])
        with patch('ncs_mcp.excel_delta_builder._retire') as retire:
            with self.assertRaises(BuilderError):
                builder.build_delta(upload, self.baseline)
            retire.assert_not_called()
        self.assertEqual(json.loads((builder.state / 'operation.lock').read_text()), replacement)
        self.assertFalse(list((builder.state / 'versions').glob('*/ncs.db')))

    def test_active_excel_passes_live_authorizer_to_ontology_pipeline(self):
        builder = DataBuilder(self.root)
        replacement = {'operation_id': 'replacement'}
        def pipeline(*args, **kwargs):
            kwargs['authorize']()
            (builder.state / 'operation.lock').write_text(json.dumps(replacement))
            kwargs['authorize']()
            self.fail('revoked pipeline must stop')
        upload = self.workbook([dict(self.initial[0], ksa_text='changed'), self.initial[1]])
        with patch('ncs_mcp.excel_delta_builder._run_pipeline', side_effect=pipeline) as run:
            with self.assertRaises(BuilderError):
                builder.build_delta(upload, self.baseline)
            run.assert_called_once()
        self.assertFalse(list((builder.state / 'versions').glob('*/ncs.db')))

    def test_active_excel_rechecks_before_publishing_candidate(self):
        builder = DataBuilder(self.root)
        def revoke_after_digest(path):
            digest = trusted_review_status_identity_digest(path)
            (builder.state / 'operation.lock').write_text('{"operation_id":"replacement"}')
            return digest
        upload = self.workbook(self.initial)
        with patch('ncs_mcp.excel_delta_builder.trusted_review_status_identity_digest',
                   side_effect=revoke_after_digest):
            with self.assertRaises(BuilderError):
                builder.build_delta(upload, self.baseline)
        self.assertFalse(list((builder.state / 'versions').glob('*/ncs.db')))

    def test_optional_dbstat_table_does_not_block_changed_unit_build(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute('PRAGMA writable_schema=ON')
            conn.execute("INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) VALUES ('table','page_statistics','page_statistics',0,'CREATE VIRTUAL TABLE page_statistics USING dbstat')")
            conn.commit()
        result = self.build([dict(self.initial[0], ksa_text='변경된 지식'), self.initial[1]])
        self.assertTrue(result['ok'])
        self.assertEqual(result['source_delta']['counts']['updated'], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.builder = DataBuilder(self.root)
        self.version = '20260912_abcd'
        self.folder = self.builder.state / 'versions' / self.version
        self.baseline = self.root / 'baseline.db'
        self.initial = [row('0201010101_26v1'), row('0201010102_26v1')]
        conn = connect(self.baseline)
        initialize_database(conn)
        normalizer = Normalizer(conn, 'old.xlsx')
        for number, values in enumerate(self.initial, 2):
            normalizer.ingest('02', number, values, now_utc())
        conn.commit()
        conn.close()
        _run_pipeline(self.baseline, bootstrap=True)

    def tearDown(self):
        self.temp.cleanup()

    def workbook(self, rows):
        path = self.root / 'upload.xlsx'
        book = Workbook()
        sheet = book.active
        sheet.append([aliases[0] for aliases in HEADER_ALIASES.values()])
        for values in rows:
            sheet.append([values[field] for field in HEADER_ALIASES])
        book.save(path)
        book.close()
        return path

    def build(self, rows):
        with self.builder.exclusive('build_delta', self.version) as context:
            return build_excel_delta(self.workbook(rows), self.baseline, self.folder / 'ncs.db',
                                     self.folder / 'delta', builder_context=context)

    def test_direct_call_without_live_context_has_no_filesystem_effects(self):
        upload = self.workbook(self.initial)
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob('*') if path.is_file()}
        args = (upload, self.baseline, self.folder / 'ncs.db', self.folder / 'delta')
        with self.assertRaises(TypeError):
            build_excel_delta(*args)
        for context in (None, {}, object()):
            with self.subTest(context=type(context).__name__):
                with self.assertRaises(BuilderAuthorizationError):
                    build_excel_delta(*args, builder_context=context)
        with self.builder.exclusive('build_delta', self.version) as expired:
            with self.assertRaises(BuilderAuthorizationError):
                build_excel_delta(*args, builder_context=copy.copy(expired))
        with self.assertRaises(BuilderAuthorizationError):
            build_excel_delta(*args, builder_context=expired)
        self.assertEqual(before, {path.relative_to(self.root): path.read_bytes()
                                for path in self.root.rglob('*') if path.is_file()})
        self.assertFalse(self.folder.exists())

    def test_output_created_during_build_is_preserved(self):
        output = self.folder / 'ncs.db'

        def create_output(path):
            digest = trusted_review_status_identity_digest(path)
            output.write_bytes(b'concurrent output')
            return digest

        with patch('ncs_mcp.excel_delta_builder.trusted_review_status_identity_digest',
                   side_effect=create_output):
            with self.assertRaisesRegex(ValueError, 'new path'):
                self.build(self.initial)
        self.assertEqual(output.read_bytes(), b'concurrent output')

    def test_live_context_rejects_wrong_action_version_and_external_output(self):
        upload = self.workbook(self.initial)
        for action, output in (
            ('refresh_api', self.folder / 'ncs.db'),
            ('build_delta', self.folder.with_name('20260912_dcba') / 'ncs.db'),
            ('build_delta', self.root / 'outside' / 'ncs.db'),
        ):
            with self.subTest(action=action, output=output):
                with self.builder.exclusive(action, self.version) as context:
                    with self.assertRaises(BuilderAuthorizationError):
                        build_excel_delta(upload, self.baseline, output, self.folder / 'delta',
                                          builder_context=context)
                self.assertFalse(output.parent.exists())

    def test_work_directory_rejects_external_and_traversal_without_writes(self):
        upload = self.workbook(self.initial)
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_bytes(b'unchanged')
        before = self.baseline.read_bytes()
        for work_dir in (outside, outside / 'missing', self.folder / 'other',
                         self.folder / 'child' / '..' / 'delta',
                         outside / '..' / self.folder.relative_to(self.root) / 'delta'):
            with self.subTest(work_dir=work_dir):
                with self.builder.exclusive('build_delta', self.version) as context:
                    with patch('ncs_mcp.excel_delta_builder._sqlite_online_snapshot') as snapshot:
                        with self.assertRaises(BuilderAuthorizationError):
                            build_excel_delta(upload, self.baseline, self.folder / 'ncs.db',
                                              work_dir, builder_context=context)
                        snapshot.assert_not_called()
                self.assertFalse(self.folder.exists())
                self.assertEqual(list(outside.iterdir()), [sentinel])
                self.assertEqual(sentinel.read_bytes(), b'unchanged')
                self.assertEqual(self.baseline.read_bytes(), before)

    def test_expired_context_cannot_create_external_work_directory(self):
        upload = self.workbook(self.initial)
        outside = self.root / 'outside' / 'missing'
        with self.builder.exclusive('build_delta', self.version) as context:
            pass
        with self.assertRaises(BuilderAuthorizationError):
            build_excel_delta(upload, self.baseline, self.folder / 'ncs.db', outside,
                              builder_context=context)
        self.assertFalse(outside.parent.exists())
        self.assertFalse(self.folder.exists())

    def test_hardlinked_file_cannot_be_used_as_work_directory(self):
        upload = self.workbook(self.initial)
        self.folder.mkdir(parents=True)
        external = self.root / 'external-file'
        external.write_bytes(b'unchanged external file')
        work_dir = self.folder / 'delta'
        os.link(external, work_dir)
        with self.builder.exclusive('build_delta', self.version) as context:
            with self.assertRaises(BuilderAuthorizationError):
                build_excel_delta(upload, self.baseline, self.folder / 'ncs.db', work_dir,
                                  builder_context=context)
        self.assertEqual(external.read_bytes(), b'unchanged external file')
        self.assertEqual(work_dir.read_bytes(), b'unchanged external file')
        self.assertEqual(list(self.folder.iterdir()), [work_dir])

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_junction_work_directory_cannot_write_to_external_target(self):
        upload = self.workbook(self.initial)
        self.folder.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_bytes(b'unchanged')
        work_dir = self.folder / 'delta'
        command = ("New-Item -ItemType Junction -Path '" + str(work_dir).replace("'", "''")
                   + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null")
        subprocess.run(['powershell', '-NoProfile', '-Command', command],
                       check=True, capture_output=True)
        try:
            with self.builder.exclusive('build_delta', self.version) as context:
                with self.assertRaises(BuilderAuthorizationError):
                    build_excel_delta(upload, self.baseline, self.folder / 'ncs.db', work_dir,
                                      builder_context=context)
            self.assertEqual(list(outside.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_bytes(), b'unchanged')
            self.assertEqual(list(self.folder.iterdir()), [work_dir])
        finally:
            # Remove only this test junction; preserve the outside target.
            os.rmdir(work_dir)

    def test_symlink_work_directory_cannot_write_to_external_target(self):
        upload = self.workbook(self.initial)
        self.folder.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        sentinel = outside / 'keep.txt'
        sentinel.write_bytes(b'unchanged')
        work_dir = self.folder / 'delta'
        try:
            work_dir.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f'Directory symlink creation unavailable: {exc}')
        with self.builder.exclusive('build_delta', self.version) as context:
            with self.assertRaises(BuilderAuthorizationError):
                build_excel_delta(upload, self.baseline, self.folder / 'ncs.db', work_dir,
                                  builder_context=context)
        self.assertEqual(list(outside.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'unchanged')
        self.assertFalse((self.folder / 'ncs.db').exists())

    def test_update_preserves_baseline_and_unchanged_ids_and_archives_old_raw(self):
        before = hashlib.sha256(self.baseline.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.baseline)) as conn:
            old = conn.execute('SELECT ksa_id,element_id,ksa_text_raw FROM ksa_items ORDER BY ksa_id').fetchall()
        changed = dict(self.initial[0], ksa_text='새로운 분석 지식')
        result = self.build([changed, self.initial[1]])
        self.assertEqual(result['source_delta']['counts'], {'inserted': 0, 'updated': 1, 'deleted': 0, 'unchanged': 1})
        self.assertEqual(hashlib.sha256(self.baseline.read_bytes()).hexdigest(), before)
        with closing(sqlite3.connect(result['candidate_db'])) as conn:
            self.assertEqual(conn.execute('SELECT ksa_id,element_id,ksa_text_raw FROM ksa_items WHERE ksa_id=?', (old[1][0],)).fetchone(), old[1])
            archive = [json.loads(item[0]) for item in conn.execute("SELECT row_json FROM builder_retired_rows WHERE table_name='ksa_items'")]
            self.assertEqual(archive[0]['ksa_text_raw'], old[0][2])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM ksa_items WHERE ksa_text_raw='새로운 분석 지식'").fetchone()[0], 1)
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_add_delete(self):
        result = self.build([self.initial[1], row('0201010103_26v1')])
        self.assertEqual(result['source_delta']['counts'], {'inserted': 1, 'updated': 0, 'deleted': 1, 'unchanged': 1})
        with closing(sqlite3.connect(result['candidate_db'])) as conn:
            self.assertEqual([item[0] for item in conn.execute('SELECT unit_code FROM competency_units ORDER BY unit_code')], ['0201010102_26v1', '0201010103_26v1'])

    def test_api_source_survives_removed_unit(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            columns = conn.execute('PRAGMA table_info(ncs_training_courses)').fetchall()
            values = {item[1]: 'fixture' for item in columns if item[3] and item[4] is None and not item[5]}
            values.update(ncs_cl_cd=self.initial[0]['unit_code'])
            conn.execute('INSERT INTO ncs_training_courses(' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            conn.commit()
        result = self.build([self.initial[1]])
        with closing(sqlite3.connect(result['candidate_db'])) as conn:
            course = conn.execute('SELECT ncs_cl_cd FROM ncs_training_courses').fetchone()
            self.assertEqual(course, (self.initial[0]['unit_code'],))

    def test_reordering_and_provenance_are_noop(self):
        result = self.build(list(reversed(self.initial)))
        self.assertEqual(result['affected_units'], [])
        self.assertEqual(result['stages'], [])

    def test_trusted_dependency_blocks_without_output(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute("UPDATE ksa_items SET review_status='human_reviewed' WHERE ksa_id=1")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'human decisions'):
            self.build([dict(self.initial[0], ksa_text='변경'), self.initial[1]])
        self.assertFalse((self.folder / 'ncs.db').exists())

    def test_trusted_link_status_blocks_without_output(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute("UPDATE ksa_concept_links SET link_status='human_reviewed' WHERE ksa_id=1")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'human decisions'):
            self.build([dict(self.initial[0], ksa_text='변경'), self.initial[1]])
        self.assertFalse((self.folder / 'ncs.db').exists())

    def test_classification_name_update_preserves_id(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            class_id = conn.execute('SELECT classification_id FROM classifications').fetchone()[0]
        result = self.build([dict(values, sub_name='새 전략') for values in self.initial])
        with closing(sqlite3.connect(result['candidate_db'])) as conn:
            self.assertEqual(conn.execute('SELECT classification_id,sub_name FROM classifications').fetchall(), [(class_id, '새 전략')])

    def test_trusted_classification_name_update_blocks(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute("UPDATE classifications SET review_status='human_reviewed'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'human decisions'):
            self.build([dict(values, sub_name='새 전략') for values in self.initial])

    def test_empty_upload_rejected(self):
        with self.assertRaisesRegex(ValueError, 'no NCS data'):
            self.build([])
        self.assertFalse((self.folder / 'ncs.db').exists())

    def test_existing_output_rejected(self):
        self.folder.mkdir(parents=True)
        output = self.folder / 'ncs.db'
        output.write_bytes(b'existing candidate')
        with self.builder.exclusive('build_delta', self.version) as context:
            with self.assertRaisesRegex(ValueError, 'new path'):
                build_excel_delta(self.workbook(self.initial), self.baseline, output, self.folder / 'delta',
                                  builder_context=context)
        self.assertEqual(output.read_bytes(), b'existing candidate')


if __name__ == '__main__':
    unittest.main()
