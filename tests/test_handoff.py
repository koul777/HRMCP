from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.handoff import export_handoff_package


class HandoffTests(unittest.TestCase):
    def test_export_handoff_package_without_db_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.close()

            output_dir = tmp_path / "handoff"
            result = export_handoff_package(db_path, output_dir)

            self.assertEqual(result["db"]["mode"], "none")
            self.assertTrue((output_dir / "README.md").exists())
            self.assertTrue((output_dir / "manifest.json").exists())
            self.assertTrue((output_dir / "sql" / "schema.sql").exists())
            self.assertTrue((output_dir / "sql" / "indexes.sql").exists())
            self.assertTrue((output_dir / "sql" / "sample_queries.sql").exists())
            self.assertTrue((output_dir / "docs" / "schema.md").exists())
            self.assertTrue((output_dir / "docs" / "data_dictionary.md").exists())
            self.assertFalse((output_dir / "data" / "db" / "ncs_sqf.sqlite").exists())

            manifest = json.loads((output_dir / "manifest.json").read_text(encoding="utf-8"))
            self.assertIn("competency_units", manifest["counts"])
            self.assertIn("sqf_ncs_matches", manifest["tables"])

    def test_export_handoff_package_with_hardlink_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.close()

            output_dir = tmp_path / "handoff"
            result = export_handoff_package(db_path, output_dir, db_mode="hardlink")
            linked_db = output_dir / "data" / "db" / "ncs_sqf.sqlite"

            self.assertEqual(result["db"]["mode"], "hardlink")
            self.assertTrue(linked_db.exists())
            self.assertEqual(linked_db.stat().st_size, db_path.stat().st_size)
            if os.name == "nt":
                self.assertEqual(os.stat(linked_db).st_ino, os.stat(db_path).st_ino)


if __name__ == "__main__":
    unittest.main()

