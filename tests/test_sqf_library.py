from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_sqf_library import (
    filename_from_content_disposition,
    infer_ontology_role,
    parse_library_posts,
    upsert_library_posts,
)
from ncs_mcp.db import connect, initialize_database


SAMPLE_HTML = """
<tr>
  <td><a href="javascript:fn_view('20260120165126709');"
      title="산업별역량체계(SQF) 직무역량체계도(가상융합 등26개 분야)">
      산업별역량체계(SQF) 직무역량체계도(가상융합 등26개 분야)</a></td>
  <td><a href="#" onclick="gfn_file_downloadFile('01','20260120165112973','20260120165113869', {'downlDstinCd':'09'});">첨부</a></td>
  <td>2026.01.20</td>
  <td>2026.01.21</td>
  <td>415</td>
</tr>
<tr>
  <td><a href="javascript:fn_view('20250717132036694');"
      title="2025년 산업별역량체계(SQF) 개발 매뉴얼">
      2025년 산업별역량체계(SQF) 개발 매뉴얼</a></td>
  <td><a href="#" onclick="gfn_file_downloadFile('01','20250717131957543','20250717131957975', {'downlDstinCd':'09'});">첨부</a></td>
  <td>2025.07.17</td>
  <td>2025.07.17</td>
  <td>992</td>
</tr>
"""


class SqfLibraryTests(unittest.TestCase):
    def test_parse_library_posts_extracts_download_params(self) -> None:
        posts = parse_library_posts(
            SAMPLE_HTML,
            page_index=0,
            source_url="https://www.ncs.go.kr/sqf/sqf01/bbs_lib_list.do?pageIndex=0",
        )

        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].lib_seq, "20260120165126709")
        self.assertEqual(posts[0].title, "산업별역량체계(SQF) 직무역량체계도(가상융합 등26개 분야)")
        self.assertEqual(posts[0].published_at, "2026-01-20")
        self.assertEqual(posts[0].updated_at, "2026-01-21")
        self.assertEqual(posts[0].view_count, 415)
        self.assertEqual(posts[0].ontology_role, "competency_framework")
        self.assertEqual(len(posts[0].files), 1)
        self.assertEqual(posts[0].files[0].file_mstky, "20260120165112973")
        self.assertEqual(posts[0].files[0].file_detl_seq, "20260120165113869")
        self.assertEqual(posts[0].files[0].downl_dstin_cd, "09")

    def test_infer_ontology_role(self) -> None:
        self.assertEqual(infer_ontology_role("SQF 기반 훈련과정 설계가이드"), "training_design")
        self.assertEqual(infer_ontology_role("SQF 기반 대학교육과정 인정"), "university_curriculum_recognition")
        self.assertEqual(infer_ontology_role("2017년 SQF 구축방안 연구보고서"), "legacy_research")

    def test_upsert_library_posts_creates_sources(self) -> None:
        posts = parse_library_posts(
            SAMPLE_HTML,
            page_index=0,
            source_url="https://www.ncs.go.kr/sqf/sqf01/bbs_lib_list.do?pageIndex=0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            result = upsert_library_posts(conn, posts)
            self.assertEqual(result["posts_upserted"], 2)
            self.assertEqual(result["files_upserted"], 2)
            self.assertEqual(result["document_sources_upserted"], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_library_posts").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_library_files").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sqf_document_sources").fetchone()[0], 2)
            conn.close()

    def test_filename_from_content_disposition_supports_encoded_filename(self) -> None:
        header = "attachment; filename*=UTF-8''SQF%20report.pdf"
        self.assertEqual(filename_from_content_disposition(header), "SQF report.pdf")


if __name__ == "__main__":
    unittest.main()
