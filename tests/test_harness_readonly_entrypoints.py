import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import ncs_harness as harness
from ncs_mcp.db import connect, initialize_database


class HarnessReadonlyEntrypointTests(unittest.TestCase):
    def calls(self, db):
        return (
            lambda: harness.build_aihr_plan_review_seedpack(demo_json_paths=[], db_path=db),
            lambda: harness.build_aihr_official_learning_module_gap_audit(db_path=db),
        )

    def test_missing_database_and_parent_are_never_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for db in (root / 'missing.db', root / 'missing-parent' / 'missing.db'):
                for call in self.calls(db):
                    with self.subTest(db=db):
                        with patch.object(harness, 'connect') as writable, \
                                patch.object(harness, 'initialize_database') as initialize, \
                                patch.object(harness.sqlite3, 'connect') as sqlite_connect:
                            with self.assertRaisesRegex(FileNotFoundError, 'SQLite DB does not exist'):
                                call()
                            writable.assert_not_called()
                            initialize.assert_not_called()
                            sqlite_connect.assert_not_called()
                        self.assertFalse(db.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_database_uses_uri_readonly_without_schema_or_source_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / 'source.db'
            with contextlib.closing(connect(db)) as conn:
                initialize_database(conn)
                conn.commit()
            before = db.read_bytes()
            for call in self.calls(db):
                with patch.object(harness, 'connect') as writable, \
                        patch.object(harness, 'initialize_database') as initialize, \
                        patch.object(harness.sqlite3, 'connect', wraps=sqlite3.connect) as sqlite_connect:
                    report = call()
                    writable.assert_not_called()
                    initialize.assert_not_called()
                    self.assertEqual(sqlite_connect.call_count, 1)
                    args, kwargs = sqlite_connect.call_args
                    self.assertEqual(args[0], db.resolve().as_uri() + '?mode=ro')
                    self.assertTrue(kwargs['uri'])
                    self.assertFalse(report['db_writes'])
                self.assertEqual(db.read_bytes(), before)
                self.assertEqual(list(db.parent.iterdir()), [db])


if __name__ == '__main__':
    unittest.main()
