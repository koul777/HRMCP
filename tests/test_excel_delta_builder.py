import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.excel_delta_builder import build_excel_delta
from ncs_mcp.ontology_refresh_builder import _run_pipeline
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer


def row(unit, text='기초 지식'):
    return dict(zip(HEADER_ALIASES, ['02', '경영', '01', '기획', '01', '계획', '01', '전략',
                                   unit, '전략 수립', '3', unit + '.1', '분석하기', '3',
                                   '1', '자료를 분석한다', 'K', '지식', '1', text]))


class ExcelDeltaTests(unittest.TestCase):
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
        return build_excel_delta(self.workbook(rows), self.baseline, self.root / 'candidate.db', self.root)

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
        self.assertFalse((self.root / 'candidate.db').exists())

    def test_trusted_link_status_blocks_without_output(self):
        with closing(sqlite3.connect(self.baseline)) as conn:
            conn.execute("UPDATE ksa_concept_links SET link_status='human_reviewed' WHERE ksa_id=1")
            conn.commit()
        with self.assertRaisesRegex(ValueError, 'human decisions'):
            self.build([dict(self.initial[0], ksa_text='변경'), self.initial[1]])
        self.assertFalse((self.root / 'candidate.db').exists())

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
        self.assertFalse((self.root / 'candidate.db').exists())

    def test_existing_output_rejected(self):
        with self.assertRaisesRegex(ValueError, 'new path'):
            build_excel_delta(self.workbook(self.initial), self.baseline, self.baseline, self.root)


if __name__ == '__main__':
    unittest.main()
