import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from ncs_mcp.builder_desktop import format_result
from ncs_mcp.data_builder import BuilderError, DataBuilder, inspect_workbook, api_failure_message
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.ontology_refresh_builder import _run_pipeline
from ncs_mcp.api_refresh_builder import (
    file_sha256,
    raw_ksa_sha256,
    trusted_review_status_identity_digest,
    trusted_review_status_counts,
)
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer
from ncs_mcp.builder_authorization import BuilderAuthorizationError, require_builder_context


class DataBuilderTests(unittest.TestCase):
    def replace_operation_lock(self):
        replacement = b'{"operation_id":"replacement-owner"}'
        (self.engine.state / 'operation.lock').write_bytes(replacement)
        return replacement

    def test_refresh_progress_revocation_blocks_report_update_and_api_call(self):
        recorded = {}

        def revoke(message):
            folder = next((self.engine.state / 'versions').iterdir())
            recorded.update(folder=folder, build=(folder / 'build.json').read_bytes(),
                            files=set(folder.iterdir()), lock=self.replace_operation_lock())

        self.engine.progress = revoke
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as refresh:
            with self.assertRaises(BuilderError):
                self.engine.refresh_api(self.baseline, ['job-base'])
            refresh.assert_not_called()
        self.assertEqual((recorded['folder'] / 'build.json').read_bytes(), recorded['build'])
        self.assertEqual(set(recorded['folder'].iterdir()), recorded['files'])
        self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])

    def test_refresh_revoked_api_result_preserves_existing_reports_and_lock(self):
        recorded = {}

        def revoke(*args, **kwargs):
            folder = kwargs['output_path'].parent
            (folder / 'api-refresh.json').write_bytes(b'previous API evidence')
            recorded.update(folder=folder, build=(folder / 'build.json').read_bytes(),
                            api=(folder / 'api-refresh.json').read_bytes(), files=set(folder.iterdir()),
                            lock=self.replace_operation_lock())
            return {'outcome': 'failed_no_reconcile', 'failure_type': 'BuilderAuthorizationError'}

        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=revoke):
            with self.assertRaises(BuilderError):
                self.engine.refresh_api(self.baseline, ['job-base'])
        self.assertEqual((recorded['folder'] / 'build.json').read_bytes(), recorded['build'])
        self.assertEqual((recorded['folder'] / 'api-refresh.json').read_bytes(), recorded['api'])
        self.assertEqual(set(recorded['folder'].iterdir()), recorded['files'])
        self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])

    def test_resume_revoked_api_result_preserves_existing_reports_and_lock(self):
        report, folder = self.interrupted_api_version()
        report.update(parent_database=str(self.baseline), sources=['job-base'])
        (folder / 'build.json').write_text(json.dumps(report), encoding='utf-8')
        recorded = {}

        def revoke(*args, **kwargs):
            recorded.update(build=(folder / 'build.json').read_bytes(),
                            api=(folder / 'api-refresh.json').read_bytes(), files=set(folder.iterdir()),
                            lock=self.replace_operation_lock())
            return {'outcome': 'failed_no_reconcile', 'failure_type': 'BuilderAuthorizationError'}

        with patch.object(self.engine, 'resume_kind', return_value='api-collection'), \
                patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=revoke):
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
        self.assertEqual((folder / 'build.json').read_bytes(), recorded['build'])
        self.assertEqual((folder / 'api-refresh.json').read_bytes(), recorded['api'])
        self.assertEqual(set(folder.iterdir()), recorded['files'])
        self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])

    def test_resume_revocation_after_invariants_blocks_merged_report(self):
        report, folder = self.interrupted_api_version()
        recorded = {}

        def revoke(path):
            identity = trusted_review_status_identity_digest(path)
            recorded.update(build=(folder / 'build.json').read_bytes(),
                            api=(folder / 'api-refresh.json').read_bytes(), files=set(folder.iterdir()),
                            lock=self.replace_operation_lock())
            return identity

        with patch('ncs_mcp.data_builder.trusted_review_status_identity_digest', side_effect=revoke), \
                patch.object(self.engine, '_finish') as finish:
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            finish.assert_not_called()
        self.assertEqual((folder / 'build.json').read_bytes(), recorded['build'])
        self.assertEqual((folder / 'api-refresh.json').read_bytes(), recorded['api'])
        self.assertEqual(set(folder.iterdir()), recorded['files'])
        self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])

    def test_version_report_rechecks_before_temp_creation_and_replace(self):
        original_dumps = json.dumps
        original_fsync = os.fsync
        for phase in ('before_temp', 'before_replace'):
            with self.subTest(phase=phase):
                # The prior replacement lock belongs to this fixture only.
                (self.engine.state / 'operation.lock').unlink(missing_ok=True)
                version = 'abcd' if phase == 'before_temp' else 'dcba'
                folder = self.engine.state / 'versions' / version
                folder.mkdir(parents=True)
                target = folder / 'build.json'
                target.write_bytes(b'original report')
                recorded = {}

                def revoke_dumps(*args, **kwargs):
                    result = original_dumps(*args, **kwargs)
                    recorded['lock'] = self.replace_operation_lock()
                    return result

                def revoke_fsync(fd):
                    original_fsync(fd)
                    recorded['lock'] = self.replace_operation_lock()

                patch_target = ('ncs_mcp.data_builder.json.dumps' if phase == 'before_temp'
                                else 'ncs_mcp.data_builder.os.fsync')
                side_effect = revoke_dumps if phase == 'before_temp' else revoke_fsync
                with self.engine.exclusive('resume', version) as context:
                    with patch(patch_target, side_effect=side_effect):
                        with self.assertRaises(BuilderAuthorizationError):
                            self.engine._write_version_json(folder, 'build.json', {'updated': True}, context,
                                                            action='resume', version=version)
                self.assertEqual(target.read_bytes(), b'original report')
                self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])
                self.assertEqual(len(list(folder.glob('*.tmp'))), int(phase == 'before_replace'))

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_revoked_refresh_cannot_write_through_replaced_version_junction(self):
        outside = self.root / 'outside'
        outside.mkdir()
        for name in ('build.json', 'api-refresh.json', 'sentinel.txt'):
            (outside / name).write_bytes(b'external sentinel')
        recorded = {}
        retained = self.root / 'retained-version'

        def revoke(*args, **kwargs):
            folder = kwargs['output_path'].parent
            recorded.update(folder=folder, build=(folder / 'build.json').read_bytes())
            folder.rename(retained)
            command = ("New-Item -ItemType Junction -Path '" + str(folder).replace("'", "''")
                       + "' -Target '" + str(outside).replace("'", "''") + "' | Out-Null")
            subprocess.run(['powershell', '-NoProfile', '-Command', command],
                           check=True, capture_output=True)
            recorded['lock'] = self.replace_operation_lock()
            return {'outcome': 'failed_no_reconcile', 'failure_type': 'BuilderAuthorizationError'}

        try:
            with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=revoke):
                with self.assertRaises(BuilderError):
                    self.engine.refresh_api(self.baseline, ['job-base'])
            self.assertEqual((retained / 'build.json').read_bytes(), recorded['build'])
            self.assertEqual({path.name: path.read_bytes() for path in outside.iterdir()},
                             dict.fromkeys(('build.json', 'api-refresh.json', 'sentinel.txt'), b'external sentinel'))
            self.assertEqual((self.engine.state / 'operation.lock').read_bytes(), recorded['lock'])
        finally:
            if recorded.get('folder') is not None:
                os.rmdir(recorded['folder'])
                retained.rename(recorded['folder'])

    def test_successful_refresh_writes_ready_reports_with_live_context(self):
        def refresh(*args, **kwargs):
            context = kwargs['builder_context']
            require_builder_context(context, action='refresh_api', root=self.engine.root,
                                    state_dir=self.engine.state, version=context.version,
                                    version_dir=kwargs['output_path'].parent)
            shutil.copyfile(self.baseline, kwargs['output_path'])
            return {'outcome': 'completed_with_warnings', 'sources': ['job-base']}

        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=refresh):
            report = self.engine.refresh_api(self.baseline, ['job-base'])
        folder = self.engine._version_dir(report['version'])
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(json.loads((folder / 'build.json').read_text(encoding='utf-8'))['status'], 'ready')
        self.assertEqual(json.loads((folder / 'api-refresh.json').read_text(encoding='utf-8'))['outcome'],
                         'completed_with_warnings')
        self.assertEqual(list(folder.glob('*.tmp')), [])
        self.assertFalse((self.engine.state / 'operation.lock').exists())

    def interrupted_excel_version(self):
        report = self.engine.build_delta(self.source, self.baseline)
        folder = self.engine._version_dir(report['version'])
        report['status'] = 'interrupted'
        (folder / 'build.json').write_text(json.dumps(report), encoding='utf-8')
        return report, folder

    def test_excel_resume_checks_all_three_digests_and_rejects_tampering(self):
        report, folder = self.interrupted_excel_version()
        self.assertEqual(self.engine.resume_kind(report['version']), 'excel-validation')
        path = folder / 'delta.json'
        evidence = json.loads(path.read_text(encoding='utf-8'))
        for field in ('sha256', 'raw_ksa_sha256', 'trusted_review_status_identity_digest'):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(evidence))
                changed['candidate_integrity'].pop(field)
                path.write_text(json.dumps(changed), encoding='utf-8')
                self.assertIsNone(self.engine.resume_kind(report['version']))
                with patch('ncs_mcp.builder_validation.validate_candidate') as validate:
                    with self.assertRaises(BuilderError):
                        self.engine.resume(report['version'])
                    validate.assert_not_called()
        path.write_text(json.dumps(evidence), encoding='utf-8')
        self.assertEqual(self.engine.resume(report['version'])['status'], 'ready')

    def test_excel_resume_rejects_raw_change_even_with_updated_file_hash(self):
        report, folder = self.interrupted_excel_version()
        with closing(sqlite3.connect(folder / 'ncs.db')) as conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='fixture tamper'")
            conn.commit()
        path = folder / 'delta.json'
        evidence = json.loads(path.read_text(encoding='utf-8'))
        evidence['candidate_integrity']['sha256'] = file_sha256(folder / 'ncs.db')
        path.write_text(json.dumps(evidence), encoding='utf-8')
        self.assertIsNone(self.engine.resume_kind(report['version']))
        with self.assertRaises(BuilderError):
            self.engine.resume(report['version'])

    def test_excel_resume_rejects_same_count_trusted_row_swap(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute("UPDATE ksa_items SET review_status='human_reviewed'")
            conn.commit()
        report, folder = self.interrupted_excel_version()
        db = folder / 'ncs.db'
        before = trusted_review_status_counts(db)
        with closing(sqlite3.connect(db)) as conn:
            conn.execute('PRAGMA foreign_keys=OFF')
            conn.execute('UPDATE ksa_items SET ksa_id=ksa_id+10000')
            conn.commit()
        self.assertEqual(trusted_review_status_counts(db), before)
        path = folder / 'delta.json'
        evidence = json.loads(path.read_text(encoding='utf-8'))
        # Isolate the trusted identity guard from the file/raw guards.
        evidence['candidate_integrity']['sha256'] = file_sha256(db)
        evidence['candidate_integrity']['raw_ksa_sha256'] = raw_ksa_sha256(db)
        path.write_text(json.dumps(evidence), encoding='utf-8')
        self.assertIsNone(self.engine.resume_kind(report['version']))
        with self.assertRaises(BuilderError):
            self.engine.resume(report['version'])

    def test_excel_resume_rejects_replaced_file_before_validation(self):
        report, folder = self.interrupted_excel_version()
        (folder / 'ncs.db').write_bytes(b'replaced fixture')
        with patch('ncs_mcp.builder_validation.validate_candidate') as validate:
            self.assertIsNone(self.engine.resume_kind(report['version']))
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            validate.assert_not_called()

    def test_finish_rechecks_live_context_after_validation(self):
        report, folder = self.interrupted_excel_version()
        replacement = {'operation_id': 'replacement'}
        def revoked(*args, **kwargs):
            (self.engine.state / 'operation.lock').write_text(json.dumps(replacement))
            return {'sha256': 'fixture'}
        with patch('ncs_mcp.builder_validation.validate_candidate', side_effect=revoked):
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
        self.assertNotEqual(json.loads((folder / 'build.json').read_text(encoding='utf-8'))['status'], 'ready')
        self.assertEqual(json.loads((self.engine.state / 'operation.lock').read_text()), replacement)

    def interrupted_api_version(self):
        report = self.engine.build_delta(self.source, self.baseline)
        folder = self.engine._version_dir(report['version'])
        report.update(kind='api', status='interrupted')
        (folder / 'build.json').write_text(json.dumps(report), encoding='utf-8')
        db = folder / 'ncs.db'
        evidence = {'outcome':'completed_with_warnings', 'sources':['training-courses'],
                    'prepared_output':str(db), 'working_copy_invariants_unchanged':True,
                    'source_invariants_after':{'unchanged':True},
                    'working_copy_invariants_after':{'raw_ksa_sha256':raw_ksa_sha256(db),
                        'trusted_review_status_counts':trusted_review_status_counts(db),
                        'trusted_review_status_identity_digest':trusted_review_status_identity_digest(db)}}
        (folder / 'api-refresh.json').write_text(json.dumps(evidence), encoding='utf-8')
        return report, folder

    def test_resume_completed_api_candidate_never_recollects(self):
        report, folder = self.interrupted_api_version()
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as collect:
            resumed = self.engine.resume(report['version'])
            collect.assert_not_called()
        self.assertEqual(resumed['status'], 'ready')
        self.assertEqual(resumed['version'], report['version'])
        self.assertEqual(resumed['operation_lineage']['action'], 'resume')
        self.assertEqual(resumed['operation_history'][-1], report['operation_lineage'])
        self.assertNotEqual(
            resumed['operation_lineage']['operation_id'],
            report['operation_lineage']['operation_id'],
        )
        self.assertFalse(resumed['human_approval_claim'])

    def test_resume_collection_passes_live_version_scoped_context(self):
        report, folder = self.interrupted_api_version()
        report.update(parent_database=str(self.baseline), sources=['job-base'])
        (folder / 'build.json').write_text(json.dumps(report), encoding='utf-8')

        def blocked(*args, **kwargs):
            require_builder_context(
                kwargs['builder_context'], action='resume', root=self.engine.root,
                version=report['version'], version_dir=kwargs['output_path'].parent,
            )
            self.assertTrue(kwargs['resume'])
            return {'outcome': 'blocked_preflight', 'preflight_errors': ['fixture_stop']}

        with patch.object(self.engine, 'resume_kind', return_value='api-collection'), patch(
            'ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=blocked
        ) as refresh:
            with self.assertRaisesRegex(BuilderError, 'fixture_stop'):
                self.engine.resume(report['version'])
            refresh.assert_called_once()
        self.assertFalse((self.engine.state / 'operation.lock').exists())
        self.assertFalse((self.engine.state / 'deployed.json').exists())

    def test_resume_rejects_changed_raw_candidate(self):
        report, folder = self.interrupted_api_version()
        with closing(sqlite3.connect(folder / 'ncs.db')) as conn:
            conn.execute("UPDATE ksa_items SET ksa_text_raw='fixture tamper'")
            conn.commit()
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as collect:
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            collect.assert_not_called()

    def test_resume_rejects_trusted_status_row_swap_with_same_counts(self):
        report, folder = self.interrupted_api_version()
        with closing(sqlite3.connect(folder / 'ncs.db')) as conn:
            conn.executemany(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name,current_query,target_query,review_status,created_at,updated_at
                ) VALUES (?,?,?,?,?,?)
                """,
                [
                    ('first', 'current', 'target', 'human_reviewed', 'now', 'now'),
                    ('second', 'current', 'target', 'candidate', 'now', 'now'),
                ],
            )
            conn.commit()
        evidence_path = folder / 'api-refresh.json'
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        expected = evidence['working_copy_invariants_after']
        expected['trusted_review_status_counts'] = trusted_review_status_counts(
            folder / 'ncs.db'
        )
        expected['trusted_review_status_identity_digest'] = (
            trusted_review_status_identity_digest(folder / 'ncs.db')
        )
        evidence_path.write_text(json.dumps(evidence), encoding='utf-8')
        with closing(sqlite3.connect(folder / 'ncs.db')) as conn:
            conn.execute(
                "UPDATE training_transition_gold_scenarios SET review_status='candidate' "
                "WHERE scenario_name='first'"
            )
            conn.execute(
                "UPDATE training_transition_gold_scenarios SET review_status='human_reviewed' "
                "WHERE scenario_name='second'"
            )
            conn.commit()

        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as collect:
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            collect.assert_not_called()

    def test_resume_rejects_legacy_api_evidence_without_identity_digest(self):
        report, folder = self.interrupted_api_version()
        evidence_path = folder / 'api-refresh.json'
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        evidence['working_copy_invariants_after'].pop(
            'trusted_review_status_identity_digest'
        )
        evidence_path.write_text(json.dumps(evidence), encoding='utf-8')

        self.assertIsNone(self.engine.resume_kind(report['version']))
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as collect:
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            collect.assert_not_called()

    def test_resume_rejects_legacy_api_checkpoint_without_identity_digest(self):
        report, folder = self.interrupted_api_version()
        report.update(sources=['training-courses'])
        (folder / 'build.json').write_text(json.dumps(report), encoding='utf-8')
        evidence_path = folder / 'api-refresh.json'
        evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
        evidence['outcome'] = 'interrupted'
        evidence_path.write_text(json.dumps(evidence), encoding='utf-8')
        checkpoint_path = folder / 'api-checkpoint' / 'api_checkpoint.json'
        checkpoint_path.parent.mkdir()
        checkpoint_path.write_text(
            json.dumps(
                {
                    'identity': {'source_invariants': {}},
                    'baseline': {},
                    'completed': {},
                }
            ),
            encoding='utf-8',
        )

        self.assertIsNone(self.engine.resume_kind(report['version']))
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence') as collect:
            with self.assertRaises(BuilderError):
                self.engine.resume(report['version'])
            collect.assert_not_called()

    def test_failure_message_explains_db_stage_and_source_failures(self):
        message = api_failure_message({'outcome': 'failed_no_reconcile',
                                      'failed_phase': 'source_invariant_check',
                                      'failure_reason': 'sqlite_dbstat_module_unavailable',
                                      'failure_type': 'OperationalError'})
        self.assertIn('원본 DB 검사', message)
        self.assertIn('dbstat', message)
        message = api_failure_message({'source_results': {'training-courses': [
            {'major_code': '01', 'completion_proven': False, 'error_type': 'Timeout'}]}})
        self.assertIn('01:Timeout', message)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = DataBuilder(self.root)
        self.source = self.root / 'upload.xlsx'
        self.baseline = self.root / 'data/processed/ncs.db'
        self.values = dict(zip(HEADER_ALIASES, [
            '20', '정보통신', '01', '정보기술', '02', '개발', '02', 'SW',
            '2001020201_26v1', '개발', '3', '2001020201_26v1.1', '분석', '3',
            '1', '요구사항을 분석한다', 'K', '지식', '1', '요구사항 분석 지식']))
        with closing(connect(self.baseline)) as conn:
            initialize_database(conn)
            Normalizer(conn, 'old.xlsx').ingest('20', 2, self.values, now_utc())
            conn.commit()
        _run_pipeline(self.baseline, bootstrap=True)
        self.book(self.values)

    def book(self, values):
        book = Workbook()
        book.active.append([v[0] for v in HEADER_ALIASES.values()])
        book.active.append([values[k] for k in HEADER_ALIASES])
        book.save(self.source)
        book.close()

    def test_upload_delta_ready_and_tamper_blocked(self):
        self.book(dict(self.values, ksa_text='요구사항 분석 기준 지식'))
        result = self.engine.build_delta(self.source, self.baseline)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['operation_lineage']['action'], 'build_delta')
        self.assertEqual(result['operation_lineage']['version'], result['version'])
        self.assertEqual(result['source_delta']['counts']['updated'], 1)
        self.assertIn('변경: 1개', format_result(result))
        candidate = self.engine.candidate(result['version'])
        self.assertNotEqual(candidate, self.baseline)
        self.assertEqual(self.engine.current_db(), self.baseline)
        with closing(sqlite3.connect(candidate)) as conn:
            conn.execute("UPDATE competency_units SET unit_name_raw='changed'")
            conn.commit()
        with self.assertRaises(BuilderError):
            self.engine.candidate(result['version'])

    def test_api_failure_not_publishable_and_lock_released(self):
        def blocked(*args, **kwargs):
            require_builder_context(
                kwargs['builder_context'], action='refresh_api', root=self.engine.root,
                version_dir=kwargs['output_path'].parent,
            )
            return {'outcome': 'blocked_preflight', 'preflight_errors': ['missing_credentials:training-courses']}
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', side_effect=blocked):
            with self.assertRaises(BuilderError):
                self.engine.refresh_api(self.baseline, ['training-courses'])
        versions = self.engine.versions()
        self.assertEqual(versions[0]['status'], 'failed')
        self.assertFalse((self.engine.state / 'deployed.json').exists())
        self.assertFalse((self.engine.state / 'operation.lock').exists())

    def test_second_operation_blocked_and_path_escape_rejected(self):
        with self.engine.exclusive():
            with self.assertRaises(BuilderError):
                with self.engine.exclusive():
                    pass
        with self.assertRaises(BuilderError):
            self.engine.candidate('../../ncs.db')

    def test_preview_and_malformed_workbook(self):
        self.assertEqual(len(inspect_workbook(self.source)['sheets']), 1)
        book = Workbook()
        book.active.append(['wrong'])
        book.save(self.source)
        book.close()
        with self.assertRaises(BuilderError):
            inspect_workbook(self.source)


if __name__ == '__main__':
    unittest.main()
