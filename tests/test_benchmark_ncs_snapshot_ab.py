from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/benchmark_ncs_snapshot_ab.py"
spec = importlib.util.spec_from_file_location("ncs_snapshot_ab_benchmark", SCRIPT)
assert spec is not None and spec.loader is not None
benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark)


class SnapshotBenchmarkTests(unittest.TestCase):
    def test_metadata_excludes_both_index_implementations_without_trusting_partial_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "snapshot.db"
            conn = sqlite3.connect(database)
            conn.executescript("""
                CREATE TABLE serving_snapshot_manifest(manifest_key TEXT, manifest_value TEXT);
                INSERT INTO serving_snapshot_manifest VALUES ('schema', 'compact'), ('raw_ksa_sha256', 'source'), ('lexical_prefix_fts_schema', 'ncs_lexical_prefix_fts_v1');
                CREATE TABLE serving_snapshot_table_counts(object_name TEXT, row_count INTEGER, count_kind TEXT);
                INSERT INTO serving_snapshot_table_counts VALUES
                    ('ksa_items', 2, 'physical'), ('ksa_search_fts_data', 3, 'physical'),
                    ('ksa_prefix_fts_data', 4, 'physical'), ('criteria_prefix_fts_idx', 5, 'physical');
            """)
            conn.close()
            record = benchmark._database_record(database)
            self.assertEqual(record['snapshot_table_counts'], {'ksa_items:physical': 2})
            self.assertEqual(record['available_candidate_indexes'], [])
            with self.assertRaisesRegex(ValueError, 'available, attested'):
                benchmark.benchmark(database, database, ['alpha'], runs=3, scope='all', limit=5, disable_fts_baseline=True)

    def _toggle_case(self, fail):
        database = Path('snapshot.db')
        record = {'path': str(database), 'sha256_before': 'same', 'embedded_manifest': {'schema': 'compact', 'raw_ksa_sha256': 'source'},
                  'snapshot_table_counts': {'ksa_items:physical': 2}, 'available_candidate_indexes': ['lexical_prefix']}
        calls = []

        def search(**kwargs):
            calls.append((benchmark.search_core._compact_ksa_search_fts_available(None, 'v2'),
                          benchmark.search_core._compact_lexical_prefix_available(None, 'v2')))
            if fail and len(calls) == 2:
                raise RuntimeError('sentinel')
            return {'ok': True, 'returned': 1, 'results': [{'id': 1}]}

        with patch.object(benchmark, '_database_record', return_value=record), \
             patch.object(benchmark, '_sha256', return_value='same'), \
             patch.object(benchmark.server, 'search_ncs', side_effect=search), \
             patch.object(benchmark.search_core, '_compact_ksa_search_fts_available', return_value=True) as legacy, \
             patch.object(benchmark.search_core, '_compact_lexical_prefix_available', return_value=True) as prefix:
            if fail:
                with self.assertRaisesRegex(RuntimeError, 'sentinel'):
                    benchmark.benchmark(database, database, ['alpha'], runs=3, scope='all', limit=5, disable_fts_baseline=True)
            else:
                report = benchmark.benchmark(database, database, ['alpha'], runs=3, scope='all', limit=5, disable_fts_baseline=True)
                self.assertTrue(report['all_responses_equal'])
            self.assertEqual(calls[:2], [(False, False), (True, True)])
            self.assertIs(benchmark.search_core._compact_ksa_search_fts_available, legacy)
            self.assertIs(benchmark.search_core._compact_lexical_prefix_available, prefix)

    def test_toggle_disables_both_indexes_only_for_baseline(self):
        self._toggle_case(False)

    def test_toggle_restores_both_indexes_after_exception(self):
        self._toggle_case(True)


if __name__ == '__main__':
    unittest.main()
