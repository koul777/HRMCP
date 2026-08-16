from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
import ncs_mcp.qualification_api as qualification_api
from ncs_mcp.qualification_api import (
    apply_qualification_retry_hygiene,
    collect_qualification_links,
    parse_qualification_xml,
    qualification_error_report,
    qualification_retry_hygiene_report,
    qualification_summary,
    search_qualification_links,
    upsert_qualification_rows,
    write_qualification_retry_hygiene_markdown,
)


def seed_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('15', 'Machinery', '01', 'Machine design',
                  '02', 'Machine design', '02', 'Machine design')
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
        ) VALUES ('1501020207_14v2', '1501020207', '14v2', '요소부품제작성검토',
                  '4', ?, 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    return "1501020207_14v2"


def seed_second_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_match_status,
            created_at, updated_at
        ) VALUES ('1501020208_14v2', '1501020208', '14v2', 'secondary unit',
                  '4', ?, 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    return "1501020208_14v2"


class QualificationApiTests(unittest.TestCase):
    def test_parse_qualification_xml(self) -> None:
        xml = """
        <response>
          <header>
            <resultCode>00</resultCode>
            <resultMsg>NORMAL SERVICE</resultMsg>
          </header>
          <body>
            <items>
              <item>
                <jmCd>B080</jmCd>
                <jmNm>기계설계기사</jmNm>
                <organStdVerCd>v2.0</organStdVerCd>
                <eduTrngStdTmSum>825</eduTrngStdTmSum>
                <jobBasisAbltStdTm>40</jobBasisAbltStdTm>
                <mandAbltUnitStdTm>480</mandAbltUnitStdTm>
                <selAbltUnitStdTm>305</selAbltUnitStdTm>
                <examInstiNm>한국산업인력공단</examInstiNm>
                <ncsClCd>1501020207_14v2</ncsClCd>
                <compeUnitName>요소부품제작성검토</compeUnitName>
                <abltUnitTypCd>SEL</abltUnitTypCd>
                <abltUnitTypNm>선택</abltUnitTypNm>
                <minEduTrngTm>30</minEduTrngTm>
              </item>
            </items>
            <numOfRows>10</numOfRows>
            <pageNo>1</pageNo>
            <totalCount>1</totalCount>
          </body>
        </response>
        """
        parsed = parse_qualification_xml(xml)

        self.assertEqual(parsed["result_code"], "00")
        self.assertEqual(parsed["total_count"], 1)
        self.assertEqual(parsed["rows"][0]["jm_cd"], "B080")
        self.assertEqual(parsed["rows"][0]["jm_nm"], "기계설계기사")
        self.assertEqual(parsed["rows"][0]["min_edu_trng_tm"], 30)

    def test_upsert_qualification_rows_links_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            rows = [
                {
                    "jm_cd": "B080",
                    "jm_nm": "기계설계기사",
                    "organ_std_ver_cd": "v2.0",
                    "edu_trng_std_tm_sum": 825,
                    "job_basis_ablt_std_tm": 40,
                    "mand_ablt_unit_std_tm": 480,
                    "sel_ablt_unit_std_tm": 305,
                    "exam_insti_nm": "한국산업인력공단",
                    "ncs_cl_cd": unit_code,
                    "compe_unit_name": "요소부품제작성검토",
                    "ablt_unit_typ_cd": "SEL",
                    "ablt_unit_typ_nm": "선택",
                    "min_edu_trng_tm": 30,
                },
                {
                    "jm_cd": "C031",
                    "jm_nm": "기계설계산업기사",
                    "organ_std_ver_cd": "v1.0",
                    "edu_trng_std_tm_sum": 600,
                    "job_basis_ablt_std_tm": 30,
                    "mand_ablt_unit_std_tm": 360,
                    "sel_ablt_unit_std_tm": 210,
                    "exam_insti_nm": "한국산업인력공단",
                    "ncs_cl_cd": unit_code,
                    "compe_unit_name": "요소부품제작성검토",
                    "ablt_unit_typ_cd": "MAND",
                    "ablt_unit_typ_nm": "필수",
                    "min_edu_trng_tm": 15,
                },
            ]

            count = upsert_qualification_rows(conn, rows)
            count_again = upsert_qualification_rows(conn, rows)
            summary = qualification_summary(conn)
            links = search_qualification_links(conn, unit_code=unit_code)
            mandatory = search_qualification_links(conn, unit_type="MAND")
            conn.close()

            self.assertEqual(count, 2)
            self.assertEqual(count_again, 2)
            self.assertEqual(summary["qualification_item_count"], 2)
            self.assertEqual(summary["unit_qualification_link_count"], 2)
            self.assertEqual(summary["total_unit_count"], 1)
            self.assertEqual(summary["attempted_unit_count"], 0)
            self.assertEqual(summary["unattempted_unit_count"], 1)
            self.assertEqual(summary["collection_coverage"], 0.0)
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0]["ablt_unit_typ_cd"], "MAND")
            self.assertEqual(mandatory[0]["jm_nm"], "기계설계산업기사")

    def test_qualification_error_report_classifies_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, updated_at
                ) VALUES (?, 'error', 0, 0, ?, ?)
                """,
                (
                    unit_code,
                    "NCS qualification API request failed: status=429, unit_code=1501020207_14v2",
                    timestamp,
                ),
            )
            conn.commit()

            report = qualification_error_report(conn)
            summary = qualification_summary(conn)
            conn.close()

            self.assertEqual(report["error_unit_count"], 1)
            self.assertEqual(report["sample_errors"][0]["error_type"], "rate_limited")
            self.assertEqual(report["sample_error_type_counts"]["rate_limited"], 1)
            self.assertEqual(summary["attempted_unit_count"], 1)
            self.assertEqual(summary["collection_coverage"], 1.0)
            self.assertEqual(summary["errors_by_major"][0]["major_code"], "15")
            self.assertEqual(summary["errors_by_major"][0]["error_unit_count"], 1)

    def test_qualification_retry_hygiene_reports_metadata_gaps_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = connect(tmp_path / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, updated_at
                ) VALUES (?, 'error', 0, 0, ?, ?)
                """,
                (
                    unit_code,
                    "NCS qualification API request failed: status=429, unit_code=1501020207_14v2",
                    timestamp,
                ),
            )
            conn.commit()

            report = qualification_retry_hygiene_report(conn, retry_backoff_seconds=5, limit=5)
            unchanged = conn.execute(
                """
                SELECT last_error_type, attempt_count, next_retry_at
                FROM ncs_qualification_collection_status
                WHERE unit_code = ?
                """,
                (unit_code,),
            ).fetchone()
            markdown_path = tmp_path / "qualification_retry_hygiene.md"
            write_qualification_retry_hygiene_markdown(report, markdown_path)
            conn.close()

            text = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["error_type_counts"]["rate_limited"], 1)
        self.assertEqual(report["metadata_gaps"]["missing_error_type_count"], 1)
        self.assertEqual(report["metadata_gaps"]["zero_attempt_count"], 1)
        self.assertEqual(report["metadata_gaps"]["missing_next_retry_at_count"], 1)
        self.assertEqual(report["dry_run_updates"][0]["inferred_error_type"], "rate_limited")
        self.assertIsNone(unchanged["last_error_type"])
        self.assertEqual(unchanged["attempt_count"], 0)
        self.assertIsNone(unchanged["next_retry_at"])
        self.assertIn("# Qualification Retry Hygiene", text)

    def test_apply_qualification_retry_hygiene_backfills_metadata_without_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            conn = connect(tmp_path / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, updated_at
                ) VALUES (?, 'error', 0, 0, ?, ?)
                """,
                (
                    unit_code,
                    "NCS qualification API request failed: status=429, unit_code=1501020207_14v2",
                    timestamp,
                ),
            )
            conn.commit()

            result = apply_qualification_retry_hygiene(conn, retry_backoff_seconds=60, limit=5)
            row = conn.execute(
                """
                SELECT last_error_type, attempt_count, next_retry_at, last_error
                FROM ncs_qualification_collection_status
                WHERE unit_code = ?
                """,
                (unit_code,),
            ).fetchone()
            markdown_path = tmp_path / "qualification_retry_hygiene_applied.md"
            write_qualification_retry_hygiene_markdown(result, markdown_path)
            conn.close()

            text = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertEqual(result["mode"], "applied")
        self.assertEqual(result["updated_unit_count"], 1)
        self.assertEqual(result["before"]["metadata_gaps"]["missing_error_type_count"], 1)
        self.assertEqual(result["after"]["metadata_gaps"]["missing_error_type_count"], 0)
        self.assertEqual(result["after"]["metadata_gaps"]["zero_attempt_count"], 0)
        self.assertEqual(result["after"]["metadata_gaps"]["missing_next_retry_at_count"], 0)
        self.assertEqual(result["after"]["retry_ready_unit_count"], 0)
        self.assertEqual(row["last_error_type"], "rate_limited")
        self.assertEqual(row["attempt_count"], 1)
        self.assertTrue(row["next_retry_at"])
        self.assertIn("status=429", row["last_error"])
        self.assertIn("mode: applied", text)

    def test_apply_qualification_retry_hygiene_respects_max_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            first_unit = seed_unit(conn)
            second_unit = seed_second_unit(conn)
            timestamp = now_utc()
            conn.executemany(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, updated_at
                ) VALUES (?, 'error', 0, 0, 'status=429', ?)
                """,
                [(first_unit, timestamp), (second_unit, timestamp)],
            )
            conn.commit()

            result = apply_qualification_retry_hygiene(
                conn,
                retry_backoff_seconds=60,
                limit=5,
                max_updates=1,
            )
            rows = conn.execute(
                """
                SELECT COUNT(*) AS updated_rows
                FROM ncs_qualification_collection_status
                WHERE last_error_type = 'rate_limited'
                  AND attempt_count = 1
                  AND next_retry_at IS NOT NULL
                """
            ).fetchone()
            conn.close()

        self.assertEqual(result["updated_unit_count"], 1)
        self.assertEqual(rows["updated_rows"], 1)

    def test_qualification_retry_hygiene_respects_waiting_backoff_and_existing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            future_retry_at = "2999-01-01T00:00:00+00:00"
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, last_error_type, attempt_count, next_retry_at, updated_at
                ) VALUES (?, 'error', 0, 0, ?, 'rate_limited', 2, ?, ?)
                """,
                (
                    unit_code,
                    "NCS qualification API request failed: status=429",
                    future_retry_at,
                    timestamp,
                ),
            )
            conn.commit()

            report = qualification_retry_hygiene_report(conn, retry_backoff_seconds=5, limit=5)
            conn.close()

        self.assertEqual(report["error_unit_count"], 1)
        self.assertEqual(report["retry_ready_unit_count"], 0)
        self.assertEqual(report["retry_waiting_unit_count"], 1)
        self.assertEqual(report["metadata_gaps"]["missing_error_type_count"], 0)
        self.assertEqual(report["metadata_gaps"]["zero_attempt_count"], 0)
        self.assertEqual(report["metadata_gaps"]["missing_next_retry_at_count"], 0)
        self.assertEqual(report["dry_run_updates"], [])

    def test_collect_qualification_links_can_retry_error_status_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            error_unit = seed_unit(conn)
            skipped_unit = seed_second_unit(conn)
            timestamp = now_utc()
            conn.executemany(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, updated_at
                ) VALUES (?, ?, 0, 0, ?, ?)
                """,
                [
                    (error_unit, "error", "status=503", timestamp),
                    (skipped_unit, "empty", None, timestamp),
                ],
            )
            conn.commit()
            conn.close()

            requested: list[str] = []

            def fake_fetch(*args, **kwargs):
                requested.append(kwargs["unit_code"])
                return {
                    "result_code": "00",
                    "result_msg": "NORMAL SERVICE",
                    "num_of_rows": 50,
                    "page_no": 1,
                    "total_count": 1,
                    "request": {
                        "unit_code": kwargs["unit_code"],
                        "page_no": kwargs["page_no"],
                        "num_of_rows": kwargs["num_of_rows"],
                    },
                    "rows": [
                        {
                            "jm_cd": "T001",
                            "jm_nm": "Test Qualification",
                            "organ_std_ver_cd": "v1.0",
                            "edu_trng_std_tm_sum": 10,
                            "job_basis_ablt_std_tm": 0,
                            "mand_ablt_unit_std_tm": 10,
                            "sel_ablt_unit_std_tm": 0,
                            "exam_insti_nm": "Test",
                            "ncs_cl_cd": kwargs["unit_code"],
                            "compe_unit_name": "Test Unit",
                            "ablt_unit_typ_cd": "MAND",
                            "ablt_unit_typ_nm": "Mandatory",
                            "min_edu_trng_tm": 10,
                        }
                    ],
                }

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    all_units=True,
                    resume=False,
                    collection_statuses=["error"],
                    request_delay=0,
                    max_retries=0,
                    retry_backoff_seconds=0,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

            conn = connect(db_path)
            status_rows = {
                row["unit_code"]: row["collection_status"]
                for row in conn.execute(
                    "SELECT unit_code, collection_status FROM ncs_qualification_collection_status"
                ).fetchall()
            }
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["unit_codes_requested"], 1)
            self.assertEqual(requested, [error_unit])
            self.assertEqual(status_rows[error_unit], "collected")
            self.assertEqual(status_rows[skipped_unit], "empty")

    def test_collect_qualification_links_uses_clamped_page_size_for_pagination(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_unit(conn)
            conn.commit()
            conn.close()

            requested_pages: list[int] = []

            def fake_fetch(*args, **kwargs):
                requested_pages.append(kwargs["page_no"])
                safe_num_of_rows = min(int(kwargs["num_of_rows"]), 50)
                row = {
                    "jm_cd": f"T{kwargs['page_no']:03d}",
                    "jm_nm": f"Test Qualification {kwargs['page_no']}",
                    "organ_std_ver_cd": "v1.0",
                    "edu_trng_std_tm_sum": 10,
                    "job_basis_ablt_std_tm": 0,
                    "mand_ablt_unit_std_tm": 10,
                    "sel_ablt_unit_std_tm": 0,
                    "exam_insti_nm": "Test",
                    "ncs_cl_cd": kwargs["unit_code"],
                    "compe_unit_name": "Test Unit",
                    "ablt_unit_typ_cd": "MAND",
                    "ablt_unit_typ_nm": "Mandatory",
                    "min_edu_trng_tm": 10,
                }
                return {
                    "result_code": "00",
                    "result_msg": "NORMAL SERVICE",
                    "num_of_rows": safe_num_of_rows,
                    "page_no": kwargs["page_no"],
                    "total_count": 51,
                    "request": {
                        "unit_code": kwargs["unit_code"],
                        "page_no": kwargs["page_no"],
                        "num_of_rows": safe_num_of_rows,
                    },
                    "rows": [row],
                }

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    unit_codes=[unit_code],
                    num_of_rows=500,
                    request_delay=0,
                    max_retries=0,
                    retry_backoff_seconds=0,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

            self.assertTrue(result["ok"])
            self.assertEqual(requested_pages, [1, 2])

    def test_collect_qualification_links_records_retry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_unit(conn)
            conn.commit()
            conn.close()

            def fake_fetch(*args, **kwargs):
                raise qualification_api.QualificationApiError(
                    "NCS qualification API request failed: status=503, unit_code=1501020207_14v2"
                )

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    unit_codes=[unit_code],
                    request_delay=0,
                    retry_backoff_seconds=1,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

            conn = connect(db_path)
            row = conn.execute(
                """
                SELECT collection_status, last_error_type, attempt_count, next_retry_at
                FROM ncs_qualification_collection_status
                WHERE unit_code = ?
                """,
                (unit_code,),
            ).fetchone()
            report = qualification_error_report(conn)
            conn.close()

            self.assertFalse(result["ok"])
            self.assertEqual(row["collection_status"], "error")
            self.assertEqual(row["last_error_type"], "server_error")
            self.assertEqual(row["attempt_count"], 1)
            self.assertTrue(row["next_retry_at"])
            self.assertEqual(report["retry_waiting_unit_count"], 1)

    def test_collect_qualification_links_can_stop_after_rate_limit_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            first_unit = seed_unit(conn)
            second_unit = seed_second_unit(conn)
            conn.commit()
            conn.close()

            requested: list[str] = []

            def fake_fetch(*args, **kwargs):
                requested.append(kwargs["unit_code"])
                raise qualification_api.QualificationApiError(
                    f"NCS qualification API request failed: status=429, unit_code={kwargs['unit_code']}"
                )

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    unit_codes=[first_unit, second_unit],
                    request_delay=0,
                    max_retries=0,
                    retry_backoff_seconds=1,
                    stop_after_rate_limit_errors=1,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

            conn = connect(db_path)
            status_rows = {
                row["unit_code"]: row["collection_status"]
                for row in conn.execute(
                    "SELECT unit_code, collection_status FROM ncs_qualification_collection_status"
                ).fetchall()
            }
            conn.close()

        self.assertFalse(result["ok"])
        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertEqual(result["rate_limit_error_count"], 1)
        self.assertEqual(result["units_processed"], 1)
        self.assertEqual(requested, [first_unit])
        self.assertEqual(status_rows[first_unit], "error")
        self.assertNotIn(second_unit, status_rows)

    def test_fetch_qualification_page_reports_repeated_429_attempts(self) -> None:
        class Response:
            status_code = 429
            headers: dict[str, str] = {}
            text = ""

        with (
            mock.patch("requests.get", side_effect=[Response() for _ in range(6)]),
            mock.patch.object(qualification_api.time, "sleep"),
        ):
            with self.assertRaises(qualification_api.QualificationApiError) as raised:
                qualification_api.fetch_qualification_page(
                    "service-key",
                    unit_code="1501020207_14v2",
                    max_retries=5,
                    retry_backoff_seconds=0,
                )

        self.assertEqual(raised.exception.rate_limit_attempts, 6)
        self.assertEqual(raised.exception.status_code, 429)

    def test_collect_qualification_links_counts_rate_limit_attempts_for_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            first_unit = seed_unit(conn)
            second_unit = seed_second_unit(conn)
            conn.commit()
            conn.close()

            requested: list[str] = []

            def fake_fetch(*args, **kwargs):
                requested.append(kwargs["unit_code"])
                raise qualification_api.QualificationApiError(
                    f"NCS qualification API request failed: status=429, unit_code={kwargs['unit_code']}",
                    rate_limit_attempts=6,
                    status_code=429,
                )

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    unit_codes=[first_unit, second_unit],
                    request_delay=0,
                    max_retries=5,
                    retry_backoff_seconds=1,
                    stop_after_rate_limit_errors=1,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

        self.assertFalse(result["ok"])
        self.assertTrue(result["stopped_early"])
        self.assertEqual(result["stop_reason"], "rate_limited")
        self.assertEqual(result["rate_limit_error_count"], 6)
        self.assertEqual(result["units_processed"], 1)
        self.assertEqual(requested, [first_unit])

    def test_retry_ready_only_skips_future_retry_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, last_error_type, attempt_count, next_retry_at, updated_at
                ) VALUES (?, 'error', 0, 0, 'status=429', 'rate_limited', 3, '9999-01-01T00:00:00+00:00', ?)
                """,
                (unit_code, timestamp),
            )
            conn.commit()
            conn.close()

            requested: list[str] = []

            def fake_fetch(*args, **kwargs):
                requested.append(kwargs["unit_code"])
                return {"result_code": "00", "result_msg": "", "rows": [], "request": {}, "total_count": 0}

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    all_units=True,
                    resume=False,
                    collection_statuses=["error"],
                    retry_ready_only=True,
                    request_delay=0,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

            self.assertTrue(result["ok"])
            self.assertEqual(result["unit_codes_requested"], 0)
            self.assertEqual(requested, [])

    def test_qualification_retry_hygiene_flags_invalid_next_retry_at_as_ready(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, last_error_type, attempt_count, next_retry_at, updated_at
                ) VALUES (?, 'error', 0, 0, 'status=429', 'rate_limited', 2, 'not-a-date', ?)
                """,
                (unit_code, timestamp),
            )
            conn.commit()

            report = qualification_retry_hygiene_report(conn, retry_backoff_seconds=5, limit=5)
            conn.close()

        self.assertEqual(report["error_unit_count"], 1)
        self.assertEqual(report["retry_ready_unit_count"], 1)
        self.assertEqual(report["retry_waiting_unit_count"], 0)
        self.assertEqual(report["metadata_gaps"]["invalid_next_retry_at_count"], 1)
        self.assertEqual(report["dry_run_updates"][0]["current_next_retry_at"], "not-a-date")
        self.assertTrue(report["dry_run_updates"][0]["invalid_next_retry_at"])

    def test_retry_ready_only_includes_invalid_next_retry_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            unit_code = seed_unit(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_qualification_collection_status(
                    unit_code, collection_status, rows_collected, pages_processed,
                    last_error, last_error_type, attempt_count, next_retry_at, updated_at
                ) VALUES (?, 'error', 0, 0, 'status=429', 'rate_limited', 3, 'not-a-date', ?)
                """,
                (unit_code, timestamp),
            )
            conn.commit()
            conn.close()

            requested: list[str] = []

            def fake_fetch(*args, **kwargs):
                requested.append(kwargs["unit_code"])
                return {"result_code": "00", "result_msg": "", "rows": [], "request": {}, "total_count": 0}

            original_fetch = qualification_api.fetch_qualification_page
            qualification_api.fetch_qualification_page = fake_fetch
            try:
                result = collect_qualification_links(
                    db_path,
                    "service-key",
                    all_units=True,
                    resume=False,
                    collection_statuses=["error"],
                    retry_ready_only=True,
                    request_delay=0,
                )
            finally:
                qualification_api.fetch_qualification_page = original_fetch

        self.assertTrue(result["ok"])
        self.assertEqual(result["unit_codes_requested"], 1)
        self.assertEqual(requested, [unit_code])


if __name__ == "__main__":
    unittest.main()
