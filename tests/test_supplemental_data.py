from __future__ import annotations

import csv
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.supplemental_data import (
    import_external_training_zip_csv,
    import_occupation_code_mapping_csv,
    import_unit_standard_training_csv,
    supplemental_data_summary,
)


def seed_scope(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
        """
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_match_status,
            created_at, updated_at
        ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                  '5', ?, 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    return "0202020101_23v3"


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]], *, encoding: str) -> None:
    with path.open("w", encoding=encoding, newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


class SupplementalDataTests(unittest.TestCase):
    def test_import_unit_standard_training_is_idempotent_and_matches_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_scope(conn)
            csv_path = Path(tmp) / "unit_standard.csv"
            write_csv(
                csv_path,
                ["분류번호", "명칭", "수준", "훈련시간"],
                [
                    {"분류번호": unit_code, "명칭": "HR planning", "수준": "5", "훈련시간": "40"},
                    {"분류번호": "9999999999_99v9", "명칭": "Missing", "수준": "1", "훈련시간": "20"},
                ],
                encoding="cp949",
            )

            first = import_unit_standard_training_csv(conn, csv_path)
            second = import_unit_standard_training_csv(conn, csv_path)
            summary = supplemental_data_summary(conn)
            conn.close()

            self.assertTrue(first["ok"])
            self.assertEqual(first["rows_processed"], 2)
            self.assertEqual(first["matched_units"], 1)
            self.assertEqual(second["table_total"], 2)
            self.assertEqual(summary["unit_standard_training_count"], 2)

    def test_unit_standard_training_preserves_same_unit_from_different_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_scope(conn)
            first_csv = Path(tmp) / "unit_standard_1.csv"
            second_csv = Path(tmp) / "unit_standard_2.csv"
            row = {"분류번호": unit_code, "명칭": "HR planning", "수준": "5", "훈련시간": "40"}
            write_csv(first_csv, ["분류번호", "명칭", "수준", "훈련시간"], [row], encoding="cp949")
            write_csv(second_csv, ["분류번호", "명칭", "수준", "훈련시간"], [row], encoding="cp949")

            import_unit_standard_training_csv(conn, first_csv)
            import_unit_standard_training_csv(conn, second_csv)
            count = conn.execute("SELECT COUNT(*) FROM ncs_unit_standard_training").fetchone()[0]
            source_count = conn.execute(
                "SELECT COUNT(DISTINCT source_file) FROM ncs_unit_standard_training"
            ).fetchone()[0]
            conn.close()

            self.assertEqual(count, 2)
            self.assertEqual(source_count, 2)

    def test_import_occupation_mapping_matches_small_and_sub_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_scope(conn)
            csv_path = Path(tmp) / "mapping.csv"
            write_csv(
                csv_path,
                ["NCS 코드", "NCS 코드명", "국가기간직종 코드", "국가기간직종 코드명", "KECO 코드", "KECO 코드명"],
                [
                    {
                        "NCS 코드": "20202",
                        "NCS 코드명": "HRM",
                        "국가기간직종 코드": "1000001",
                        "국가기간직종 코드명": "HR",
                        "KECO 코드": "0261",
                        "KECO 코드명": "HR specialist",
                    },
                    {
                        "NCS 코드": "02020201",
                        "NCS 코드명": "HR planning",
                        "국가기간직종 코드": "1000002",
                        "국가기간직종 코드명": "Planning",
                        "KECO 코드": "",
                        "KECO 코드명": "",
                    },
                ],
                encoding="utf-8-sig",
            )

            result = import_occupation_code_mapping_csv(conn, csv_path)
            rows = conn.execute(
                """
                SELECT ncs_code_normalized, ncs_code_level, match_status
                FROM ncs_occupation_code_mappings
                ORDER BY source_row_number
                """
            ).fetchall()
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["rows_processed"], 2)
            self.assertEqual(result["matched_classification_scope_rows"], 2)
            self.assertEqual(rows[0]["ncs_code_normalized"], "020202")
            self.assertEqual(rows[0]["match_status"], "matched_small_scope")
            self.assertEqual(rows[1]["match_status"], "matched_sub_exact")

    def test_import_occupation_mapping_canonicalizes_equivalent_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_scope(conn)
            csv_path = Path(tmp) / "mapping.csv"
            write_csv(
                csv_path,
                ["NCS 코드", "NCS 코드명", "국가기간직종 코드", "국가기간직종 코드명", "KECO 코드", "KECO 코드명"],
                [
                    {
                        "NCS 코드": "02020201",
                        "NCS 코드명": "HR planning",
                        "국가기간직종 코드": "1000002",
                        "국가기간직종 코드명": "Planning",
                        "KECO 코드": "",
                        "KECO 코드명": "",
                    }
                ],
                encoding="utf-8-sig",
            )

            import_occupation_code_mapping_csv(conn, csv_path)
            import_occupation_code_mapping_csv(conn, csv_path.parent / "." / csv_path.name)
            count = conn.execute("SELECT COUNT(*) FROM ncs_occupation_code_mappings").fetchone()[0]
            conn.close()

            self.assertEqual(count, 1)

    def test_import_external_training_zip_keeps_external_catalog_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_scope(conn)
            csv_path = Path(tmp) / "training_zip.csv"
            write_csv(
                csv_path,
                [
                    "연번",
                    "훈련과정명",
                    "사업구분",
                    "훈련기관명",
                    "국가직무능력표준(NCS) 코드",
                    "국가직무능력표준(NCS) 코드1",
                    "국가직무능력표준(NCS) 코드2",
                    "국가직무능력표준(NCS) 코드3",
                    "국가직무능력표준(NCS) 코드명1",
                    "국가직무능력표준(NCS) 코드명2",
                    "국가직무능력표준(NCS) 코드명3",
                    "훈련방법",
                    "훈련시간",
                ],
                [
                    {
                        "연번": "1",
                        "훈련과정명": "HR OJT",
                        "사업구분": "s-ojt",
                        "훈련기관명": "Acme",
                        "국가직무능력표준(NCS) 코드": "020202",
                        "국가직무능력표준(NCS) 코드1": "02",
                        "국가직무능력표준(NCS) 코드2": "02",
                        "국가직무능력표준(NCS) 코드3": "02",
                        "국가직무능력표준(NCS) 코드명1": "Business",
                        "국가직무능력표준(NCS) 코드명2": "HR",
                        "국가직무능력표준(NCS) 코드명3": "HRM",
                        "훈련방법": "OJT",
                        "훈련시간": "20",
                    }
                ],
                encoding="cp949",
            )

            result = import_external_training_zip_csv(conn, csv_path)
            training_course_count = conn.execute("SELECT COUNT(*) FROM ncs_training_courses").fetchone()[0]
            summary = supplemental_data_summary(conn)
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["rows_processed"], 1)
            self.assertEqual(result["matched_classification_scope_rows"], 1)
            self.assertEqual(training_course_count, 0)
            self.assertEqual(summary["external_training_zip_course_count"], 1)

    def test_import_external_training_zip_accepts_split_ncs_code_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_scope(conn)
            csv_path = Path(tmp) / "training_zip_split.csv"
            write_csv(
                csv_path,
                [
                    "연번",
                    "훈련과정명",
                    "사업구분",
                    "훈련기관명",
                    "국가직무능력표준(NCS) 코드1",
                    "국가직무능력표준(NCS) 코드2",
                    "국가직무능력표준(NCS) 코드3",
                    "국가직무능력표준(NCS) 코드명1",
                    "국가직무능력표준(NCS) 코드명2",
                    "국가직무능력표준(NCS) 코드명3",
                    "훈련방법",
                    "훈련시간",
                ],
                [
                    {
                        "연번": "1",
                        "훈련과정명": "HR OJT",
                        "사업구분": "s-ojt",
                        "훈련기관명": "Acme",
                        "국가직무능력표준(NCS) 코드1": "02",
                        "국가직무능력표준(NCS) 코드2": "02",
                        "국가직무능력표준(NCS) 코드3": "02",
                        "국가직무능력표준(NCS) 코드명1": "Business",
                        "국가직무능력표준(NCS) 코드명2": "HR",
                        "국가직무능력표준(NCS) 코드명3": "HRM",
                        "훈련방법": "OJT",
                        "훈련시간": "20",
                    }
                ],
                encoding="cp949",
            )

            result = import_external_training_zip_csv(conn, csv_path)
            row = conn.execute(
                "SELECT ncs_code_normalized, match_status FROM ncs_external_training_zip_courses"
            ).fetchone()
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(row["ncs_code_normalized"], "020202")
            self.assertEqual(row["match_status"], "matched_small_scope")

    def test_supplemental_import_cli_exits_nonzero_for_malformed_csv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            bad_csv = Path(tmp) / "bad.csv"
            write_csv(bad_csv, ["wrong"], [{"wrong": "value"}], encoding="cp949")
            env = {
                **os.environ,
                "PYTHONPATH": str(SRC),
                "NCS_DB_PATH": str(db_path),
                "PYTHONIOENCODING": "utf-8",
            }
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "ncs_harness.py"),
                    "import-supplemental-ncs-data",
                    "--unit-standard-csv",
                    str(bad_csv),
                ],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("supplemental_import_failed", completed.stdout)


if __name__ == "__main__":
    unittest.main()
