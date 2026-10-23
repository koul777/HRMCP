from __future__ import annotations

import itertools
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.training_recommendation import _classification_code_filter, resolve_ncs_query_scope

OPTIONAL_FILTER_SQL = """
    SELECT cu.unit_code
    FROM competency_units cu
    JOIN classifications c ON c.classification_id = cu.classification_id
    WHERE (? IS NULL OR c.major_code = ?)
      AND (? IS NULL OR c.middle_code = ?)
      AND (? IS NULL OR c.small_code = ?)
      AND (? IS NULL OR c.sub_code = ?)
    ORDER BY cu.unit_code
"""
CLASSIFICATIONS = [("01", "01", "01", "01"), ("01", "01", "02", "01"), ("02", "01", "01", "01"), ("02", "02", "01", "02")]


class ClassificationCodeFilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.tmp.name) / "ncs.db")
        initialize_database(self.conn)
        timestamp = now_utc()
        for index, (major, middle, small, sub) in enumerate(CLASSIFICATIONS):
            self.conn.execute(
                "INSERT INTO classifications(major_code, major_name, middle_code, middle_name, small_code, small_name, "
                "sub_code, sub_name) VALUES (?, 'M', ?, 'M', ?, 'S', ?, 'Sub')",
                (major, middle, small, sub),
            )
            classification_id = self.conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            unit_code = f"{major}{middle}{small}{sub}01_25v1"
            self.conn.execute(
                "INSERT INTO competency_units(unit_code, base_unit_code, unit_version, unit_name_raw, unit_level_raw, "
                "classification_id, created_at, updated_at) VALUES (?, ?, '25v1', ?, '3', ?, ?, ?)",
                (unit_code, unit_code.split("_")[0], f"채용 관리 {index}", classification_id, timestamp, timestamp),
            )
            self.conn.execute(
                "INSERT INTO competency_elements(unit_code, element_no, element_code_raw, element_name_raw, "
                "element_level_raw) VALUES (?, '1', ?, ?, '3')",
                (unit_code, f"E{index}", f"채용 계획 수립 {index}"),
            )
        self.conn.commit()

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp.cleanup()

    def test_filter_matches_the_optional_filter_query(self) -> None:
        values = (None, "", "01", "02", "99")
        for codes in itertools.product(values, repeat=4):
            with self.subTest(codes=codes):
                where, params = _classification_code_filter(*codes)
                actual = self.conn.execute(
                    "SELECT cu.unit_code FROM competency_units cu "
                    "JOIN classifications c ON c.classification_id = cu.classification_id "
                    f"WHERE {where} ORDER BY cu.unit_code",
                    params,
                ).fetchall()
                expected = self.conn.execute(
                    OPTIONAL_FILTER_SQL, tuple(value for code in codes for value in (code, code))
                ).fetchall()
                self.assertEqual([tuple(row) for row in actual], [tuple(row) for row in expected])

    def test_scope_scans_use_the_classification_code_index(self) -> None:
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        try:
            resolve_ncs_query_scope(self.conn, "채용 계획", major_code="02")
        finally:
            self.conn.set_trace_callback(None)
        scans = [
            sql
            for sql in statements
            if "JOIN classifications c ON" in sql and "c.major_code = '02'" in sql and "IN (" not in sql
        ]
        self.assertTrue(any("FROM competency_elements ce" in sql for sql in scans))
        self.assertTrue(any("FROM competency_units cu" in sql for sql in scans))
        for sql in scans:
            with self.subTest(sql=sql.split("FROM", 1)[1][:40]):
                self.assertNotIn("IS NULL", sql)
                plan = " ".join(row[3] for row in self.conn.execute(f"EXPLAIN QUERY PLAN {sql}"))
                self.assertIn("idx_classifications_codes", plan)


if __name__ == "__main__":
    unittest.main()
