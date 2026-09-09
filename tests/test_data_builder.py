import json
import sqlite3
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
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer


class DataBuilderTests(unittest.TestCase):
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
        with patch('ncs_mcp.data_builder.refresh_ncs_api_evidence', return_value={
            'outcome': 'blocked_preflight', 'preflight_errors': ['missing_credentials:training-courses']}):
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
