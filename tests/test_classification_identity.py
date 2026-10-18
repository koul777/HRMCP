from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import server  # noqa: E402
from ncs_mcp.classification_identity import (  # noqa: E402
    reconcile_unit_classification_ids,
    resolve_import_classification_id,
    unit_classification_join,
)
from ncs_mcp.db import connect, initialize_database  # noqa: E402
from ncs_mcp.preprocess_excel import HEADER_ALIASES, Normalizer, preprocess_excel  # noqa: E402


NOW = "2026-10-19T00:00:00+00:00"


def _classification(
    conn: sqlite3.Connection,
    path: str,
    name: str,
    *,
    active: str | None = "Y",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name, api_usg_yn
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            path[:2],
            name + " 대분류",
            path[2:4],
            name + " 중분류",
            path[4:6],
            name + " 소분류",
            path[6:8],
            name,
            active,
        ),
    )
    return int(cursor.lastrowid)


def _unit(
    conn: sqlite3.Connection,
    code: str,
    name: str,
    classification_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, created_at, updated_at
        ) VALUES (?, ?, 'v1', ?, '5', ?, ?, ?)
        """,
        (code, code.split("_", 1)[0], name, classification_id, NOW, NOW),
    )


def _values(unit_code: str, unit_name: str, path: str, sub_name: str) -> dict[str, str]:
    return {
        "major_code": path[:2],
        "major_name": "대분류",
        "middle_code": path[2:4],
        "middle_name": "중분류",
        "small_code": path[4:6],
        "small_name": "소분류",
        "sub_code": path[6:8],
        "sub_name": sub_name,
        "unit_code": unit_code,
        "unit_name": unit_name,
        "unit_level": "5",
        "element_code": unit_code + " 1",
        "element_name": "요소",
        "element_level": "5",
        "criteria_no": "1",
        "criteria_text": "수행한다.",
        "ksa_type_code": "01",
        "ksa_type_name": "지식",
        "ksa_no": "1",
        "ksa_text": "필요 지식",
    }


def test_importer_uses_unit_prefix_instead_of_last_source_row() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_database(conn)
    normalizer = Normalizer(conn, "fixture.xlsx")
    code = "0201010106_13v1"
    normalizer.ingest("02", 2, _values(code, "예산 관리", "02010101", "경영기획"), NOW)
    normalizer.ingest("07", 2, _values(code, "예산 관리", "07010205", "자원봉사관리"), NOW)

    row = conn.execute(
        """
        SELECT c.major_code || c.middle_code || c.small_code || c.sub_code AS path
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE cu.unit_code = ?
        """,
        (code,),
    ).fetchone()
    assert row["path"] == "02010101"
    assert conn.execute("SELECT COUNT(*) FROM raw_excel_rows").fetchone()[0] == 2


class ExcelClassificationPrepassTests(unittest.TestCase):
    @staticmethod
    def write_workbook(path: Path, sheet_rows: list[tuple[str, dict[str, str]]]) -> None:
        workbook = Workbook()
        workbook.remove(workbook.active)
        for sheet_name, values in sheet_rows:
            sheet = workbook.create_sheet(sheet_name)
            sheet.append([aliases[0] for aliases in HEADER_ALIASES.values()])
            sheet.append([values[field] for field in HEADER_ALIASES])
        workbook.save(path)
        workbook.close()

    def test_workbook_preserves_all_evidence_when_canonical_path_appears_later(self) -> None:
        code = "0201010106_13v1"
        canonical = _values(code, "예산 관리", "02010101", "경영기획")
        canonical.update(ksa_text="정규 경로 지식", criteria_text="정규 경로 수행준거")
        borrowed = _values(code, "예산 관리", "07010205", "자원봉사관리")
        borrowed.update(ksa_text="차용 경로 고유 지식", criteria_text="차용 경로 고유 수행준거")
        for canonical_first in (False, True):
            with self.subTest(canonical_first=canonical_first), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [("borrowed", borrowed), ("canonical", canonical)]
                if canonical_first:
                    rows.reverse()
                self.write_workbook(root / "source.xlsx", rows)
                summary = preprocess_excel(root / "source.xlsx", root / "ncs.db", root / "reports")
                self.assertEqual(summary["counts"]["raw_excel_rows"], 2)
                self.assertEqual(summary["counts"]["ksa_items"], 2)
                self.assertEqual(summary["counts"]["performance_criteria"], 2)
                self.assertEqual(summary["counts"]["element_criteria_ksa_links"], 2)
                conn = connect(root / "ncs.db")
                try:
                    evidence = conn.execute(
                        """
                        SELECT r.sheet_name, r.ksa_text, ki.ksa_text_raw, pc.criteria_text_raw,
                               c.major_code || c.middle_code || c.small_code || c.sub_code AS path
                        FROM element_criteria_ksa_links link
                        JOIN raw_excel_rows r ON r.raw_row_id = link.raw_row_id
                        JOIN ksa_items ki ON ki.ksa_id = link.ksa_id
                        JOIN performance_criteria pc ON pc.criteria_id = link.criteria_id
                        JOIN competency_elements ce ON ce.element_id = link.element_id
                        JOIN competency_units cu ON cu.unit_code = ce.unit_code
                        JOIN classifications c ON c.classification_id = cu.classification_id
                        ORDER BY r.sheet_name
                        """
                    ).fetchall()
                    self.assertEqual([tuple(row) for row in evidence], [
                        ("borrowed", borrowed["ksa_text"], borrowed["ksa_text"],
                         borrowed["criteria_text"], "02010101"),
                        ("canonical", canonical["ksa_text"], canonical["ksa_text"],
                         canonical["criteria_text"], "02010101"),
                    ])
                finally:
                    conn.close()

    def test_prepass_respects_sheet_and_row_limits_and_preserves_unresolved_raw(self) -> None:
        code = "0201010106_13v1"
        rows = [
            ("borrowed", _values(code, "예산 관리", "07010205", "자원봉사관리")),
            ("canonical", _values(code, "예산 관리", "02010101", "경영기획")),
        ]
        for limits in ({"max_rows": 1}, {"sheets": {"borrowed"}}):
            with self.subTest(limits=limits), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self.write_workbook(root / "source.xlsx", rows)
                summary = preprocess_excel(
                    root / "source.xlsx", root / "ncs.db", root / "reports", **limits
                )
                self.assertEqual(summary["counts"]["raw_excel_rows"], 1)
                self.assertEqual(summary["counts"]["classifications"], 1)
                self.assertEqual(summary["counts"]["competency_units"], 0)
                self.assertEqual(summary["counts"]["ksa_items"], 0)
                self.assertEqual(summary["counts"]["element_criteria_ksa_links"], 0)

    def test_fresh_workbook_search_and_detail_use_unique_structural_path(self) -> None:
        code = "0201010106_13v1"
        values = _values(code, "예산 관리", "02010101", "경영기획")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.write_workbook(root / "source.xlsx", [("canonical", values)])
            preprocess_excel(root / "source.xlsx", root / "ncs.db", root / "reports")

            @contextmanager
            def fixture_db():
                conn = connect(root / "ncs.db")
                try:
                    yield conn
                finally:
                    conn.close()

            with patch.object(server, "open_db", fixture_db):
                detail = server.get_unit_structure(code)
                self.assertEqual(detail["unit"]["classification"]["sub"], "경영기획")
                results = server.search_ncs("예산 관리", scope="unit", limit=5)["results"]
                self.assertEqual([row["id"] for row in results], [code])
                self.assertEqual(results[0]["path"]["sub"], "경영기획")

                # Once any API activity is known, an unknown/inactive path
                # must not inherit the fresh-import structural fallback.
                with fixture_db() as conn:
                    _classification(conn, "07010205", "자원봉사관리", active="Y")
                    conn.commit()
                for active in (None, "N"):
                    with self.subTest(active=active):
                        with fixture_db() as conn:
                            conn.execute(
                                "UPDATE classifications SET api_usg_yn = ? WHERE major_code = '02'",
                                (active,),
                            )
                            conn.commit()
                        self.assertEqual(server.get_unit_structure(code)["error"], "not_found")
                        self.assertEqual(server.search_ncs("예산 관리", scope="unit")["results"], [])


def test_reconciliation_repairs_only_unique_active_prefixes() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_database(conn)
    correct = _classification(conn, "02020302", "사무행정")
    wrong = _classification(conn, "07010205", "자원봉사관리")
    code = "0202030207_22v4"
    _unit(conn, code, "사무행정 업무 관리", wrong)

    dry_run = reconcile_unit_classification_ids(conn)
    assert dry_run["repair_count"] == 1
    assert dry_run["repairs"][0]["resolved_classification_id"] == correct
    assert conn.execute(
        "SELECT classification_id FROM competency_units WHERE unit_code = ?", (code,)
    ).fetchone()[0] == wrong

    applied = reconcile_unit_classification_ids(conn, apply=True)
    assert applied["repair_count"] == 1
    assert conn.execute(
        "SELECT classification_id FROM competency_units WHERE unit_code = ?", (code,)
    ).fetchone()[0] == correct


def test_serving_join_materializes_active_paths_without_correlated_subquery() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    initialize_database(conn)
    correct = _classification(conn, "02010101", "경영기획")
    wrong = _classification(conn, "07010205", "자원봉사관리")
    _unit(conn, "0201010106_13v1", "예산 관리", wrong)
    _unit(conn, "SYNTHETIC_UNIT", "synthetic fixture", wrong)
    join_sql = unit_classification_join(conn)

    assert "GROUP BY major_code, middle_code, small_code, sub_code" in join_sql
    assert "HAVING COUNT(*) = 1" in join_sql
    assert "ncs_identity_candidate" not in join_sql
    assert " || " not in join_sql
    plan = conn.execute(
        "EXPLAIN QUERY PLAN SELECT cu.unit_code, c.sub_name "
        "FROM competency_units cu "
        + join_sql
        + " WHERE cu.unit_code = ?",
        ("0201010106_13v1",),
    ).fetchall()
    assert all("CORRELATED" not in str(row[3]).upper() for row in plan)
    rows = conn.execute(
        "SELECT cu.unit_code, c.classification_id FROM competency_units cu "
        + join_sql
        + " ORDER BY cu.unit_code"
    ).fetchall()
    assert {row["unit_code"]: row["classification_id"] for row in rows} == {
        "0201010106_13v1": correct,
        "SYNTHETIC_UNIT": wrong,
    }


def test_serving_projects_unique_active_prefix_and_fails_closed_without_it() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "ncs.db"
        conn = connect(db_path)
        initialize_database(conn)
        correct = _classification(conn, "02010101", "경영기획")
        wrong = _classification(conn, "07010205", "자원봉사관리")
        code = "0201010106_13v1"
        _unit(conn, code, "예산 관리", wrong)
        conn.commit()
        conn.close()

        @contextmanager
        def fixture_db():
            fixture = connect(db_path)
            try:
                yield fixture
            finally:
                fixture.close()

        with patch.object(server, "open_db", fixture_db):
            detail = server.get_unit_structure(code)
            assert detail["unit"]["classification"]["sub"] == "경영기획"
            assert detail["unit"]["classification"]["sub_code"] == "01"
            results = server.search_ncs("예산 관리", scope="unit", limit=5)["results"]
            assert results[0]["path"]["sub"] == "경영기획"

            with fixture_db() as mutable:
                mutable.execute(
                    "UPDATE classifications SET api_usg_yn = 'N' WHERE classification_id = ?",
                    (correct,),
                )
                mutable.commit()
            assert server.get_unit_structure(code)["error"] == "not_found"
            assert server.search_ncs("예산 관리", scope="unit", limit=5)["results"] == []


def test_ambiguous_active_prefix_is_never_repaired() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE classifications(
            classification_id INTEGER PRIMARY KEY,
            major_code TEXT, middle_code TEXT, small_code TEXT, sub_code TEXT,
            api_usg_yn TEXT
        );
        CREATE TABLE competency_units(
            unit_code TEXT PRIMARY KEY, base_unit_code TEXT,
            classification_id INTEGER
        );
        INSERT INTO classifications VALUES(1, '02', '01', '01', '01', 'Y');
        INSERT INTO classifications VALUES(2, '02', '01', '01', '01', 'Y');
        INSERT INTO classifications VALUES(3, '07', '01', '02', '05', 'Y');
        INSERT INTO competency_units VALUES('0201010106_13v1', '0201010106', 3);
        """
    )
    report = reconcile_unit_classification_ids(conn, apply=True)
    assert report["repair_count"] == 0
    assert report["ambiguous_active_path_count"] == 1
    assert conn.execute("SELECT classification_id FROM competency_units").fetchone()[0] == 3
    assert resolve_import_classification_id(conn, "0201010106") is None
    for clear_metadata in (False, True):
        if clear_metadata:
            conn.execute("UPDATE classifications SET api_usg_yn = NULL")
        assert conn.execute(
            "SELECT cu.unit_code FROM competency_units cu " + unit_classification_join(conn)
        ).fetchall() == []
        assert resolve_import_classification_id(conn, "0201010106") is None
