from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.import_ontology_sources import register_local_ontology_source
from ncs_mcp.preprocess_sqf_documents import has_unprocessed_assets


class ImportOntologySourcesTests(unittest.TestCase):
    def test_register_local_source_creates_library_file_and_document_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            source_path = tmp_path / "source.pdf"
            source_path.write_bytes(b"%PDF-1.4\n% local ontology source\n")

            result = register_local_ontology_source(
                db_path,
                source_path,
                raw_dir=tmp_path / "raw",
                title="KQF SQF framework source",
                ontology_role="framework_reference",
            )

            self.assertTrue(Path(result["stored_path"]).exists())
            conn = connect(db_path)
            initialize_database(conn)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_library_posts").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_library_files").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_document_sources").fetchone()[0], 1)
            row = conn.execute(
                "SELECT title, ontology_role, text_extraction_status FROM sqf_document_sources"
            ).fetchone()
            self.assertEqual(row["title"], "KQF SQF framework source")
            self.assertEqual(row["ontology_role"], "framework_reference")
            self.assertEqual(row["text_extraction_status"], "pending")
            conn.close()

    def test_new_document_without_assets_is_unprocessed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            source_path = Path(tmp) / "source.pdf"
            source_path.write_bytes(b"%PDF-1.4\n% local ontology source\n")
            result = register_local_ontology_source(db_path, source_path, raw_dir=Path(tmp) / "raw")

            conn = connect(db_path)
            initialize_database(conn)
            self.assertTrue(has_unprocessed_assets(conn, int(result["document_id"])))
            conn.close()


if __name__ == "__main__":
    unittest.main()
