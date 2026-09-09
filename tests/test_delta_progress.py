import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.excel_delta_builder import build_excel_delta
from ncs_mcp.ontology_refresh_builder import _run_pipeline, _sqlite_online_snapshot
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer


class DeltaProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.baseline = self.root / 'baseline.db'
        self.values = dict(zip(HEADER_ALIASES, [
            '02', '경영', '01', '기획', '01', '기획', '01', '전략',
            '0201010101_26v1', '전략 능력', '3', '0201010101_26v1.1',
            '분석하기', '3', '1', '자료를 분석한다', 'K', '지식', '1', '분석 지식',
        ]))
        with closing(connect(self.baseline)) as conn:
            initialize_database(conn)
            Normalizer(conn, 'old.xlsx').ingest('02', 2, self.values, now_utc())
            conn.commit()

    def test_backup_counts_real_pages_and_preserves_source(self):
        events = []
        target = self.root / 'copy.db'
        _sqlite_online_snapshot(self.baseline, target, progress=events.append)
        with closing(sqlite3.connect(self.baseline)) as conn:
            pages = conn.execute('PRAGMA page_count').fetchone()[0]
        counts = [e for e in events if e['stage'] == 'DB 복사' and e['total'] is not None]
        self.assertTrue(counts)
        self.assertEqual(counts[-1]['completed'], pages)
        self.assertEqual(counts[-1]['total'], pages)
        with closing(sqlite3.connect(target)) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_pipeline_reports_operations_and_unknown_work_separately(self):
        events = []
        stages, invariants = _run_pipeline(self.baseline, bootstrap=True, progress=events.append)
        completed = [e for e in events if e['total'] == 6]
        self.assertEqual([e['completed'] for e in completed], list(range(1, 7)))
        self.assertEqual(len(stages), 6)
        self.assertTrue(invariants['raw_ksa_preserved'])
        self.assertTrue(any(e['stage'] == '과업 유사도 관계 구축' and e['total'] is None for e in events))

    def test_no_change_reports_exact_rows_and_all_sheets(self):
        excel = self.root / 'upload.xlsx'
        book = Workbook()
        book.active.append([aliases[0] for aliases in HEADER_ALIASES.values()])
        book.active.append([self.values[field] for field in HEADER_ALIASES])
        book.create_sheet('empty')
        book.save(excel)
        book.close()
        events = []
        result = build_excel_delta(excel, self.baseline, self.root / 'candidate.db', self.root,
                                   progress=events.append)
        structured = [event for event in events if isinstance(event, dict)]
        self.assertEqual(result['ontology_processing'], 'skipped_no_change')
        self.assertIn({'stage': '기존 원천 행 비교 준비', 'completed': 1, 'total': 1, 'unit': '행'}, structured)
        self.assertIn({'stage': 'Excel 시트 읽기', 'completed': 2, 'total': 2, 'unit': '시트'}, structured)
        self.assertTrue(any('생략' in event['stage'] for event in structured))


if __name__ == '__main__':
    unittest.main()
