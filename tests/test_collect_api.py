from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import (
    api_quality_hygiene_report,
    api_unmatched_cleanup_proposal_report,
    api_unmatched_diagnosis_report,
    apply_api_quality_hygiene,
    collect_elements_api,
    fetch_standard_page,
    normalize_api_compare_text,
    upsert_standard_element_items,
)
from ncs_mcp.db import connect, initialize_database, insert_quality_issue, now_utc


def seed_element(conn: sqlite3.Connection) -> int:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('02', '경영', '02', '총무ㆍ인사', '02', '인사ㆍ조직', '01', '인사')
        """
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, created_at, updated_at
        ) VALUES ('0202020101_23v3', '0202020101', '23v3', '인사기획',
                  '5', ?, ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
        ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1',
                  '지식재산 기반 인수합병(M&;A) 전략 세우기', '0')
        """
    )
    return int(conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"])


def add_element(
    conn: sqlite3.Connection,
    *,
    element_no: str,
    element_name: str,
    level: str,
    status: str,
) -> int:
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw,
            element_level_raw, api_match_status
        ) VALUES ('0202020101_23v3', ?, ?, ?, ?, ?)
        """,
        (
            element_no,
            f"0202020101_23v3 {element_no}",
            element_name,
            level,
            status,
        ),
    )
    return int(
        conn.execute(
            "SELECT element_id FROM competency_elements WHERE element_no = ?",
            (element_no,),
        ).fetchone()["element_id"]
    )


class CollectApiQualityTests(unittest.TestCase):
    def test_fetch_standard_page_reports_non_json_response_without_service_key(self) -> None:
        class FakeResponse:
            status_code = 200
            headers = {"Content-Type": "text/html"}

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                raise ValueError("not json")

        with patch("ncs_mcp.collect_api.requests.get", return_value=FakeResponse()):
            with self.assertRaisesRegex(RuntimeError, "API response was not JSON") as raised:
                fetch_standard_page(
                    "https://example.test/api",
                    "openapi6",
                    "SECRET_SERVICE_KEY",
                    1,
                    10,
                    3,
                    {"ncsLclasCd": "14"},
                )

        message = str(raised.exception)
        self.assertIn("openapi6", message)
        self.assertIn("content_type=text/html", message)
        self.assertNotIn("SECRET_SERVICE_KEY", message)

    def test_fetch_standard_page_reports_connection_error_detail_without_service_key(self) -> None:
        with patch("ncs_mcp.collect_api.requests.get", side_effect=requests.ConnectionError("proxy failed")):
            with self.assertRaisesRegex(RuntimeError, "API request failed") as raised:
                fetch_standard_page(
                    "https://example.test/api",
                    "openapi6",
                    "SECRET_SERVICE_KEY",
                    1,
                    10,
                    3,
                    {"ncsLclasCd": "14"},
                )

        message = str(raised.exception)
        self.assertIn("error=ConnectionError", message)
        self.assertIn("proxy failed", message)
        self.assertNotIn("SECRET_SERVICE_KEY", message)

    def test_normalize_api_compare_text_handles_html_entities(self) -> None:
        self.assertEqual(normalize_api_compare_text("M&;A"), "M&A")
        self.assertEqual(normalize_api_compare_text("M&amp;A"), "M&A")

    def test_standard_element_upsert_deduplicates_api_quality_issues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_element(conn)
            item = {
                "USG_YN": "Y",
                "NCS_CL_CD": "0202020101_23v3",
                "COMPE_UNIT_FACTR_NO": "1",
                "COMPE_UNIT_FACTR_NAME": "지식재산 기반 인수합병(M&amp;A) 전략 세우기",
                "COMPE_UNIT_FACTR_LEVEL": "5",
            }

            first = upsert_standard_element_items(conn, [item])
            second = upsert_standard_element_items(conn, [item])
            issue_rows = conn.execute(
                """
                SELECT issue_detail
                FROM quality_issues
                WHERE issue_type='api_element_value_mismatch'
                ORDER BY issue_id
                """
            ).fetchall()
            conn.close()

        self.assertEqual(first["mismatches"], 1)
        self.assertEqual(second["mismatches"], 0)
        self.assertEqual(len(issue_rows), 1)
        self.assertIn("element_level mismatch", issue_rows[0]["issue_detail"])

    def test_standard_element_upsert_resolves_prior_unmatched_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            element_id = seed_element(conn)
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=element_id,
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry.",
            )

            upsert_standard_element_items(
                conn,
                [
                    {
                        "USG_YN": "Y",
                        "NCS_CL_CD": "0202020101_23v3",
                        "COMPE_UNIT_FACTR_NO": "1",
                        "COMPE_UNIT_FACTR_NAME": "지식재산 기반 인수합병(M&amp;A) 전략 세우기",
                        "COMPE_UNIT_FACTR_LEVEL": "0",
                    }
                ],
            )
            resolved_at = conn.execute(
                "SELECT resolved_at FROM quality_issues WHERE issue_type='api_element_unmatched'"
            ).fetchone()["resolved_at"]
            conn.close()

        self.assertIsNotNone(resolved_at)

    def test_collect_elements_api_only_failed_excludes_not_collected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            failed_element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status='api_failed' WHERE element_id=?",
                (failed_element_id,),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=failed_element_id,
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry.",
            )
            add_element(
                conn,
                element_no="3",
                element_name="열린 이슈 없는 실패 요소",
                level="5",
                status="api_failed",
            )
            not_collected_id = add_element(
                conn,
                element_no="2",
                element_name="다른 요소",
                level="5",
                status="not_collected",
            )
            conn.commit()
            conn.close()

            def fake_fetch(**kwargs):
                return {
                    "response": {
                        "header": {"resultCode": "00", "resultMsg": "OK"},
                        "body": {
                            "totalCount": 1,
                            "items": {
                                "item": {
                                    "USG_YN": "Y",
                                    "NCS_CL_CD": "0202020101_23v3",
                                    "COMPE_UNIT_FACTR_NO": "1",
                                    "COMPE_UNIT_FACTR_NAME": "지식재산 기반 인수합병(M&amp;A) 전략 세우기",
                                    "COMPE_UNIT_FACTR_LEVEL": "0",
                                }
                            },
                        },
                    }
                }

            with patch("ncs_mcp.collect_api.fetch_standard_page", side_effect=fake_fetch):
                summary = collect_elements_api(
                    db_path,
                    reports_dir,
                    service_key="test-key",
                    only_failed=True,
                    only_open_unmatched=True,
                    element_limit=10,
                )

            conn = connect(db_path)
            try:
                not_collected_status = conn.execute(
                    "SELECT api_match_status FROM competency_elements WHERE element_id=?",
                    (not_collected_id,),
                ).fetchone()["api_match_status"]
            finally:
                conn.close()

        self.assertEqual(summary["elements_requested"], 1)
        self.assertEqual(summary["elements_successful"], 1)
        self.assertEqual(not_collected_status, "not_collected")

    def test_collect_elements_api_no_data_resolves_prior_unmatched_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status='api_failed' WHERE element_id=?",
                (element_id,),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=element_id,
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry.",
            )
            conn.commit()
            conn.close()

            def fake_fetch(**kwargs):
                return {
                    "response": {
                        "header": {"resultCode": "03", "resultMsg": "NO_DATA"},
                        "body": {"totalCount": 0, "items": {}},
                    }
                }

            with patch("ncs_mcp.collect_api.fetch_standard_page", side_effect=fake_fetch):
                summary = collect_elements_api(
                    db_path,
                    reports_dir,
                    service_key="test-key",
                    only_failed=True,
                    only_open_unmatched=True,
                    element_limit=10,
                )

            conn = connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT ce.api_match_status, qi.resolved_at
                    FROM competency_elements ce
                    JOIN quality_issues qi ON qi.target_id = CAST(ce.element_id AS TEXT)
                    WHERE ce.element_id = ?
                      AND qi.issue_type = 'api_element_unmatched'
                    """,
                    (element_id,),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(summary["elements_failed"], 0)
        self.assertEqual(summary["elements_no_data"], 1)
        self.assertEqual(row["api_match_status"], "no_data")
        self.assertIsNotNone(row["resolved_at"])

    def test_collect_elements_api_skips_cached_no_data_response(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            element_id = seed_element(conn)
            source_url = (
                "https://apis.data.go.kr/B490007/hrdkapi/NCS006?"
                "COMPE_UNIT_FACTR_NO=1&NCS_CL_CD=0202020101_23v3&USG_YN=Y"
            )
            conn.execute(
                """
                INSERT INTO api_raw_responses(
                    source_url, page_no, num_of_rows, total_count,
                    result_code, result_msg, response_json, fetched_at
                ) VALUES (?, 1, 100, 0, '03', 'NO_DATA', '{}', ?)
                """,
                (source_url, now_utc()),
            )
            conn.execute(
                "UPDATE competency_elements SET api_match_status='api_failed' WHERE element_id=?",
                (element_id,),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=element_id,
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry.",
            )
            conn.commit()
            conn.close()

            with patch("ncs_mcp.collect_api.fetch_standard_page") as fake_fetch:
                summary = collect_elements_api(
                    db_path,
                    reports_dir,
                    service_key="test-key",
                    element_limit=10,
                )

            conn = connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT ce.api_match_status, qi.resolved_at
                    FROM competency_elements ce
                    JOIN quality_issues qi ON qi.target_id = CAST(ce.element_id AS TEXT)
                    WHERE ce.element_id = ?
                      AND qi.issue_type = 'api_element_unmatched'
                    """,
                    (element_id,),
                ).fetchone()
            finally:
                conn.close()

        fake_fetch.assert_not_called()
        self.assertEqual(summary["elements_skipped"], 1)
        self.assertEqual(summary["elements_successful"], 0)
        self.assertEqual(summary["elements_failed"], 0)
        self.assertEqual(summary["elements_no_data"], 1)
        self.assertEqual(row["api_match_status"], "no_data")
        self.assertIsNotNone(row["resolved_at"])

    def test_collect_elements_api_non_no_data_error_stays_api_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status='api_failed' WHERE element_id=?",
                (element_id,),
            )
            conn.commit()
            conn.close()

            def fake_fetch(**kwargs):
                return {
                    "response": {
                        "header": {"resultCode": "99", "resultMsg": "ERROR"},
                        "body": {"totalCount": 0, "items": {}},
                    }
                }

            with patch("ncs_mcp.collect_api.fetch_standard_page", side_effect=fake_fetch):
                summary = collect_elements_api(
                    db_path,
                    reports_dir,
                    service_key="test-key",
                    only_failed=True,
                    element_limit=10,
                )

            conn = connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT ce.api_match_status, qi.resolved_at, qi.issue_detail
                    FROM competency_elements ce
                    JOIN quality_issues qi ON qi.target_id = CAST(ce.element_id AS TEXT)
                    WHERE ce.element_id = ?
                      AND qi.issue_type IN ('api_element_unmatched', 'api_element_collection_failure')
                    """,
                    (element_id,),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(summary["elements_failed"], 1)
        self.assertEqual(summary["elements_no_data"], 0)
        self.assertEqual(row["api_match_status"], "api_failed")
        self.assertIsNone(row["resolved_at"])
        self.assertIn("resultCode=99", row["issue_detail"])

    def test_collect_elements_api_request_error_stores_connection_detail(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            reports_dir = Path(tmp) / "reports"
            conn = connect(db_path)
            initialize_database(conn)
            element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status='api_failed' WHERE element_id=?",
                (element_id,),
            )
            conn.commit()
            conn.close()

            def fake_fetch(**kwargs):
                raise RuntimeError(
                    "API request failed: url=https://example.test/api, "
                    "params={'pageNo': 1}, error=ConnectionError, "
                    "detail=HTTPSConnectionPool(host='apis.data.go.kr', port=443): "
                    "Max retries exceeded with url: /B490007/hrdkapi/NCS006 "
                    "(Caused by NewConnectionError(\"HTTPSConnection(host='apis.data.go.kr', "
                    "port=443): Failed to establish a new connection: [WinError 10013] "
                    "액세스 권한에 의해 숨겨진 소켓에 액세스를 시도했습니다\"))"
                )

            with patch("ncs_mcp.collect_api.fetch_standard_page", side_effect=fake_fetch):
                summary = collect_elements_api(
                    db_path,
                    reports_dir,
                    service_key="test-key",
                    only_failed=True,
                    element_limit=10,
                )

            conn = connect(db_path)
            try:
                row = conn.execute(
                    """
                    SELECT qi.issue_type, qi.issue_detail
                    FROM quality_issues qi
                    WHERE qi.target_id = ? AND qi.issue_type = 'api_element_collection_failure'
                    ORDER BY qi.issue_id DESC
                    LIMIT 1
                    """,
                    (str(element_id),),
                ).fetchone()
            finally:
                conn.close()

        self.assertEqual(summary["elements_failed"], 1)
        self.assertEqual(row["issue_type"], "api_element_collection_failure")
        self.assertIn("WinError 10013", row["issue_detail"])

    def test_api_quality_hygiene_resolves_duplicates_and_normalized_equal_mismatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            for _ in range(2):
                insert_quality_issue(
                    conn,
                    target_type="element",
                    target_id=1,
                    issue_type="api_element_value_mismatch",
                    severity="warning",
                    issue_detail="element_level mismatch: excel='0', api='5'",
                    suggested_action="Review.",
                )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=2,
                issue_type="api_element_value_mismatch",
                severity="warning",
                issue_detail="element_name mismatch: excel='지식재산 기반 인수합병(M&;A) 전략 세우기', api='지식재산 기반 인수합병(M&amp;A) 전략 세우기'",
                suggested_action="Review.",
            )
            conn.execute("UPDATE quality_issues SET detected_at = ?", (timestamp,))
            conn.commit()

            report = api_quality_hygiene_report(conn, limit=10)
            applied = apply_api_quality_hygiene(conn, limit=10)
            open_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE issue_type='api_element_value_mismatch' AND resolved_at IS NULL
                """
            ).fetchone()[0]
            conn.close()

        self.assertEqual(report["candidate_count"], 2)
        self.assertEqual(applied["resolved_count"], 2)
        self.assertEqual(open_count, 1)

    def test_api_quality_hygiene_resolves_terminal_no_data_unmatched_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status='no_data' WHERE element_id=?",
                (element_id,),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=element_id,
                issue_type="api_element_unmatched",
                severity="warning",
                issue_detail="NCS006 request failed after retries.",
                suggested_action="Retry.",
            )
            conn.commit()

            report = api_quality_hygiene_report(conn, limit=10)
            applied = apply_api_quality_hygiene(conn, limit=10)
            open_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE issue_type='api_element_unmatched'
                  AND resolved_at IS NULL
                """
            ).fetchone()[0]
            conn.close()

        self.assertEqual(report["candidate_action_counts"]["resolve_terminal_no_data"], 1)
        self.assertEqual(applied["resolved_count"], 1)
        self.assertEqual(open_count, 0)

    def test_api_quality_hygiene_resolves_stale_matched_element_api_issue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            element_id = seed_element(conn)
            conn.execute(
                "UPDATE competency_elements SET api_match_status = 'matched' WHERE element_id = ?",
                (element_id,),
            )
            insert_quality_issue(
                conn,
                target_type="element",
                target_id=str(element_id),
                issue_type="api_element_collection_failure",
                severity="warning",
                issue_detail="NCS006 returned resultCode=15: HRDK_UNKNOWN_ERROR",
                suggested_action="Retry.",
            )
            conn.commit()

            report = api_quality_hygiene_report(conn, limit=10)
            applied = apply_api_quality_hygiene(conn, limit=10)
            open_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM quality_issues
                WHERE issue_type='api_element_collection_failure'
                  AND resolved_at IS NULL
                """
            ).fetchone()[0]
            conn.close()

        self.assertEqual(
            report["candidate_action_counts"]["resolve_stale_matched_element_api_issue"],
            1,
        )
        self.assertEqual(applied["resolved_count"], 1)
        self.assertEqual(open_count, 0)

    def test_api_unmatched_diagnosis_and_cleanup_proposal_reports_open_and_resolved_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            for issue_type, target_id, detail, resolved in (
                ("api_element_unmatched", "101", "NCS006 request failed after retries.", False),
                ("api_element_unmatched", "102", "NCS006 request failed after retries.", True),
                ("api_element_collection_failure", "103", "API collection failed after retries.", False),
                ("api_element_collection_failure", "104", "API collection failed after retries.", True),
                ("api_value_mismatch", "105", "unit_name mismatch.", False),
                ("api_element_value_mismatch", "106", "element_level mismatch.", False),
            ):
                insert_quality_issue(
                    conn,
                    target_type="element",
                    target_id=target_id,
                    issue_type=issue_type,
                    severity="warning",
                    issue_detail=detail,
                    suggested_action="Review.",
                )
                if resolved:
                    issue_id = int(
                        conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    )
                    conn.execute(
                        "UPDATE quality_issues SET resolved_at = ? WHERE issue_id = ?",
                        (timestamp, issue_id),
                    )
            conn.commit()

            diagnosis = api_unmatched_diagnosis_report(conn, limit=1)
            proposal = api_unmatched_cleanup_proposal_report(diagnosis)
            conn.close()

        self.assertEqual(diagnosis["summary"]["api_element_unmatched"]["total_rows"], 2)
        self.assertEqual(diagnosis["summary"]["api_element_unmatched"]["open_rows"], 1)
        self.assertEqual(diagnosis["summary"]["api_element_unmatched"]["resolved_rows"], 1)
        self.assertEqual(
            diagnosis["summary"]["api_element_collection_failure"]["resolved_rows"],
            1,
        )
        self.assertEqual(
            diagnosis["dominant_details"]["api_element_unmatched"][0]["issue_detail"],
            "NCS006 request failed after retries.",
        )
        self.assertIn("retry-noise bookkeeping", diagnosis["root_cause"])
        self.assertEqual(
            proposal["diagnosis_snapshot"]["api_element_collection_failure_resolved_rows"],
            1,
        )
        self.assertEqual(proposal["dry_run_impact"]["remaining_open_api_value_mismatch_rows"], 1)
        self.assertEqual(proposal["dry_run_impact"]["resolved_api_element_unmatched_rows_removed"], 1)
        self.assertIn("report-only cleanup proposal", proposal["recommendation"])


if __name__ == "__main__":
    unittest.main()
