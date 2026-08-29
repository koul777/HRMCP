from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.job_base_api import (
    count_job_base_links,
    job_base_profile_for_units,
    job_base_summary,
    parse_job_base_xml,
    search_job_base_links,
    upsert_job_base_rows,
)


def seed_unit(conn: sqlite3.Connection) -> str:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('01', 'Business management', '01', 'Business management',
                  '01', 'Project management', '01', 'ODA')
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
        ) VALUES ('0101010101_17v2', '0101010101', '17v2', '공적개발원조사업 개발전략수립',
                  '5', ?, 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    return "0101010101_17v2"


class JobBaseApiTests(unittest.TestCase):
    def test_parse_job_base_xml_splits_factors(self) -> None:
        xml = """
        <root>
          <data>
            <row>
              <ncsLclasCd>01</ncsLclasCd>
              <ncsLclasCdnm>사업관리</ncsLclasCdnm>
              <ncsMclasCd>01</ncsMclasCd>
              <ncsMclasCdnm>사업관리</ncsMclasCdnm>
              <ncsSclasCd>01</ncsSclasCd>
              <ncsSclasCdnm>프로젝트관리</ncsSclasCdnm>
              <ncsSubdCd>01</ncsSubdCd>
              <ncsSubdCdnm>공적개발원조사업관리</ncsSubdCdnm>
              <ncsClCd>0101010101_17v2</ncsClCd>
              <compeUnitName>공적개발원조사업 개발전략수립</compeUnitName>
              <jobBasCompeName>조직이해능력</jobBasCompeName>
              <jobBasCompeFactrNm>경영이해 능력,국제감각,업무이해 능력</jobBasCompeFactrNm>
            </row>
          </data>
          <dataInfo>
            <code>000</code>
            <message>정상</message>
            <totalPage>1</totalPage>
            <pageNo>1</pageNo>
            <numOfRows>10</numOfRows>
            <totCnt>1</totCnt>
          </dataInfo>
        </root>
        """
        parsed = parse_job_base_xml(xml)

        self.assertEqual(parsed["code"], "000")
        self.assertEqual(parsed["total_count"], 1)
        self.assertEqual(parsed["rows"][0]["job_base_competency_name"], "조직이해능력")
        self.assertEqual(
            parsed["rows"][0]["job_base_factors"],
            ["경영이해 능력", "국제감각", "업무이해 능력"],
        )

    def test_upsert_job_base_rows_links_units_and_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            result = upsert_job_base_rows(
                conn,
                [
                    {
                        "ncs_lclas_cd": "01",
                        "ncs_lclas_cdnm": "사업관리",
                        "ncs_mclas_cd": "01",
                        "ncs_mclas_cdnm": "사업관리",
                        "ncs_sclas_cd": "01",
                        "ncs_sclas_cdnm": "프로젝트관리",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "공적개발원조사업관리",
                        "ncs_cl_cd": unit_code,
                        "compe_unit_name": "공적개발원조사업 개발전략수립",
                        "job_base_competency_name": "조직이해능력",
                        "job_base_factor_text": "경영이해 능력,국제감각",
                        "job_base_factors": ["경영이해 능력", "국제감각"],
                    }
                ],
            )
            summary = job_base_summary(conn)
            links = search_job_base_links(conn, unit_code=unit_code)
            link_count = count_job_base_links(conn, unit_code=unit_code)
            profile = job_base_profile_for_units(conn, {unit_code})
            conn.close()

            self.assertEqual(result["rows_processed"], 1)
            self.assertEqual(result["links_upserted"], 2)
            self.assertEqual(summary["job_base_competency_count"], 1)
            self.assertEqual(summary["job_base_factor_count"], 2)
            self.assertEqual(summary["unit_job_base_link_count"], 2)
            self.assertEqual(summary["linked_unit_count"], 1)
            self.assertEqual(summary["unit_count"], 1)
            self.assertEqual(summary["unit_job_base_coverage"], 1.0)
            self.assertEqual(summary["factorless_link_count"], 0)
            self.assertEqual(summary["links_with_factor_count"], 2)
            self.assertEqual(summary["avg_factors_per_linked_unit"], 2.0)
            self.assertEqual(summary["review_status_counts"], {"auto_linked": 2})
            self.assertEqual(len(summary["top_factors"]), 2)
            self.assertEqual(len(links), 2)
            self.assertEqual(link_count, 2)
            self.assertNotIn("source_payload", links[0])
            self.assertNotIn("api_fetched_at", links[0])
            self.assertEqual({link["review_status"] for link in links}, {"auto_linked"})
            self.assertEqual(len(profile), 2)
            self.assertEqual(profile[0]["competency_name"], "조직이해능력")

    def test_job_base_summary_reports_factorless_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            unit_code = seed_unit(conn)
            result = upsert_job_base_rows(
                conn,
                [
                    {
                        "ncs_lclas_cd": "01",
                        "ncs_lclas_cdnm": "사업관리",
                        "ncs_mclas_cd": "01",
                        "ncs_mclas_cdnm": "사업관리",
                        "ncs_sclas_cd": "01",
                        "ncs_sclas_cdnm": "프로젝트관리",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "공적개발원조사업관리",
                        "ncs_cl_cd": unit_code,
                        "compe_unit_name": "공적개발원조사업 개발전략수립",
                        "job_base_competency_name": "조직이해능력",
                        "job_base_factor_text": "",
                        "job_base_factors": [],
                    }
                ],
            )
            summary = job_base_summary(conn)
            conn.close()

        self.assertEqual(result["links_upserted"], 1)
        self.assertEqual(summary["job_base_competency_count"], 1)
        self.assertEqual(summary["job_base_factor_count"], 0)
        self.assertEqual(summary["unit_job_base_link_count"], 1)
        self.assertEqual(summary["factorless_link_count"], 1)
        self.assertEqual(summary["links_with_factor_count"], 0)
        self.assertEqual(summary["top_factors"], [])


if __name__ == "__main__":
    unittest.main()
