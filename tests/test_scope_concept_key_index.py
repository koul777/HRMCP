from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, normalize_concept_key, now_utc
from ncs_mcp.training_recommendation import resolve_ncs_query_scope

FULL_SCAN_SQL = """
    SELECT concept_id, concept_name, concept_type, definition_status, review_status
    FROM ontology_concepts
    WHERE normalized_key LIKE ? OR concept_name LIKE ?
    ORDER BY concept_id
    LIMIT 2000
"""
NAMES = ["인력 채용 계획", "인력채용 절차", "IoT 서비스 운영", "iot 장비", "채용_계획서", "채용X계획서",
         "합격률 50%", "합격률 50점", "다른 개념"]


class ScopeConceptKeyIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "ncs.db")
        initialize_database(self.conn)
        timestamp = now_utc()
        for name in NAMES:
            self.conn.execute(
                "INSERT INTO ontology_concepts(concept_name, normalized_key, concept_type, definition_status, "
                "relation_status, review_status, created_at, updated_at) "
                "VALUES (?, ?, 'knowledge', 'missing', 'unlinked', 'raw', ?, ?)",
                (name, normalize_concept_key(name), timestamp, timestamp),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def concept_query(self, text: str) -> str:
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            resolve_ncs_query_scope(self.conn, text)
        finally:
            self.conn.set_trace_callback(None)
        return next(sql for sql in statements if "FROM ontology_concepts" in sql and "LIMIT 2000" in sql)

    def test_key_index_query_returns_the_full_scan_rows(self) -> None:
        for text in ("인력채용", "인력 채용", "IoT", "iot 서비스", "채용_계획", "50%"):
            with self.subTest(text=text):
                executed = self.conn.execute(self.concept_query(text)).fetchall()
                expected = self.conn.execute(
                    FULL_SCAN_SQL, (f"%{normalize_concept_key(text)}%", f"%{text}%")
                ).fetchall()
                self.assertEqual([tuple(row) for row in executed], [tuple(row) for row in expected])
                self.assertTrue(expected)

    def test_wildcard_text_keeps_the_full_scan(self) -> None:
        self.assertIn("concept_id IN (", self.concept_query("인력채용"))
        for text in ("채용_계획", "50%"):
            with self.subTest(text=text):
                query = self.concept_query(text)
                self.assertIn("concept_name LIKE", query)
                self.assertNotIn("concept_id IN (", query)


if __name__ == "__main__":
    unittest.main()
