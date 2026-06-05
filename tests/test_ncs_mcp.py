from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database
from ncs_mcp.preprocess_excel import preprocess_excel


HEADERS = [
    "대분류코드",
    "대분류코드명",
    "중분류코드",
    "중분류코드명",
    "소분류코드",
    "소분류코드명",
    "세분류코드",
    "세분류코드명",
    "능력단위분류번호",
    "능력단위명칭",
    "수준",
    "능력단위요소번호",
    "능력단위요소명",
    "능력단위요소수준",
    "수행준거번호",
    "수행준거",
    "지식기술태도코드",
    "지식기술태도코드명",
    "지식기술태도번호",
    "지식기술태도의의",
]


def make_sample_excel(path: Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "02"
    ws.append(HEADERS)
    base = [
        "02",
        "경영·회계·사무",
        "02",
        "총무·인사",
        "02",
        "인사·조직",
        "01",
        "인사",
        "0202020101_23v3",
        "인사기획",
        6,
        "0202020101_23v3 1",
        "인사전략 수립하기",
        6,
    ]
    ws.append(
        base
        + [
            1,
            "조직의 전략방향을 고려하여 인사전략을 수립할 수 있다.",
            "01",
            "지식",
            1,
            "인사전략",
        ]
    )
    ws.append(
        base
        + [
            1,
            "조직의 전략방향을 고려하여 인사전략을 수립할 수 있다.",
            "02",
            "기술",
            1,
            "자료분석 능력",
        ]
    )
    wb.save(path)


class NcsMcpTests(unittest.TestCase):
    def test_schema_initializes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            self.assertIn("competency_units", tables)
            self.assertIn("quality_issues", tables)
            conn.close()

    def test_preprocess_deduplicates_hierarchy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            excel_path = tmp_path / "sample.xlsx"
            db_path = tmp_path / "ncs.db"
            reports_dir = tmp_path / "reports"
            make_sample_excel(excel_path)

            summary = preprocess_excel(
                excel_path=excel_path,
                db_path=db_path,
                reports_dir=reports_dir,
                reset=True,
            )

            counts = summary["counts"]
            self.assertEqual(counts["raw_excel_rows"], 2)
            self.assertEqual(counts["competency_units"], 1)
            self.assertEqual(counts["competency_elements"], 1)
            self.assertEqual(counts["performance_criteria"], 1)
            self.assertEqual(counts["ksa_items"], 2)
            self.assertEqual(counts["element_criteria_ksa_links"], 2)

    def test_server_get_unit_structure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            excel_path = tmp_path / "sample.xlsx"
            db_path = tmp_path / "ncs.db"
            make_sample_excel(excel_path)
            preprocess_excel(
                excel_path=excel_path,
                db_path=db_path,
                reports_dir=tmp_path / "reports",
                reset=True,
            )
            os.environ["NCS_DB_PATH"] = str(db_path)
            from ncs_mcp.server import get_unit_structure

            result = get_unit_structure("0202020101_23v3")
            self.assertEqual(result["unit"]["unit_name"], "인사기획")
            self.assertEqual(len(result["elements"]), 1)
            self.assertEqual(len(result["elements"][0]["performance_criteria"]), 1)
            self.assertEqual(len(result["elements"][0]["ksa"]), 2)


if __name__ == "__main__":
    unittest.main()

