from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.sqf_report_review import (
    build_sqf_report_review_seedpack,
    write_sqf_report_review_seedpack_jsonl,
    write_sqf_report_review_seedpack_markdown,
)


class SqfReportReviewTests(unittest.TestCase):
    def _build_sample_db(self, db_path: Path) -> None:
        conn = connect(db_path)
        initialize_database(conn)
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO classifications(
                major_code, major_name, middle_code, middle_name,
                small_code, small_name, sub_code, sub_name
            ) VALUES ('02', '경영·회계·사무', '03', '재무ㆍ회계', '02', '회계', '01', '회계·감사')
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
            ) VALUES ('0203020101_20v4', '0203020101', '20v4', '전표관리', '3', ?, ?, ?)
            """,
            (classification_id, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_duties(
                source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, job_name, duty_name, duty_level,
                duty_definition, source_payload, api_fetched_at
            ) VALUES (
                'sqf:test:accounting:3', '02', '경영·회계·사무', '경영지원',
                '회계', '회계', '회계(3)', '3',
                '전표와 회계정보를 처리하는 책무', '{}', ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_industry_sectors(
                sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, sector_name, source_count, updated_at
            ) VALUES (
                'sector:02:accounting', '02', '경영·회계·사무', '경영지원',
                '회계', '경영지원/회계', 1, ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_jobs_normalized(
                sqf_job_id, sector_id, job_name, job_definition,
                source_count, updated_at
            ) VALUES (
                'job:accounting', 'sector:02:accounting', '회계',
                '회계 직무', 1, ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_levels(sqf_level, level_name, definition, updated_at)
            VALUES (3, '실무자', '실무 수준', ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_job_levels_normalized(
                sqf_job_level_id, sqf_job_id, sqf_source_key, duty_name,
                sqf_level, level_name, job_level_definition, duty_definition,
                updated_at
            ) VALUES (
                'job-level:accounting:3', 'job:accounting', 'sqf:test:accounting:3',
                '회계(3)', 3, '실무자', '전표 처리와 회계정보 산출을 수행한다.',
                '전표와 회계정보를 처리하는 책무', ?
            )
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_ncs_matches(
                source_type, source_id, target_type, target_id, relation,
                score, confidence, match_method, evidence_text, evidence_source,
                review_status, created_at, updated_at
            ) VALUES (
                'sqf_duty', 'sqf:test:accounting:3', 'ncs_competency_unit',
                '0203020101_20v4', 'closeMatch', 12.5, 'lexical',
                'unit_name_overlap', '회계 책무와 전표관리 능력단위 후보',
                'test', 'candidate', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_library_posts(
                lib_seq, title, source_url, collected_at, ontology_role
            ) VALUES ('lib:1', '2022년 SQF 개발 최종보고서(인사조직, 재무, 회계 분야)', 'local:test', ?, 'reference')
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_library_files(
                lib_seq, sys_dstin_cd, file_mstky, file_detl_seq,
                local_path, download_status
            ) VALUES ('lib:1', 'LOCAL', 'file:1', '1', 'sample.pdf', 'downloaded')
            """
        )
        file_id = conn.execute("SELECT file_id FROM sqf_library_files").fetchone()["file_id"]
        conn.execute(
            """
            INSERT INTO sqf_document_sources(
                lib_seq, file_id, title, ontology_role,
                text_extraction_status, created_at
            ) VALUES ('lib:1', ?, '2022년 SQF 개발 최종보고서(인사조직, 재무, 회계 분야)', 'reference', 'extracted', ?)
            """,
            (file_id, timestamp),
        )
        document_id = conn.execute("SELECT document_id FROM sqf_document_sources").fetchone()[
            "document_id"
        ]
        conn.execute(
            """
            INSERT INTO sqf_document_assets(
                document_id, asset_path, asset_name, asset_type,
                extraction_status, created_at
            ) VALUES (?, 'sample.pdf', 'sample.pdf', 'pdf', 'extracted', ?)
            """,
            (document_id, timestamp),
        )
        asset_id = conn.execute("SELECT asset_id FROM sqf_document_assets").fetchone()["asset_id"]
        conn.execute(
            """
            INSERT INTO sqf_document_chunks(
                asset_id, chunk_index, page_start, page_end, text,
                char_count, token_estimate, created_at
            ) VALUES (?, 0, 12, 12, ?, 120, 40, ?)
            """,
            (
                asset_id,
                "회계(3) 수준은 전표 처리와 회계정보 산출 업무를 수행하는 직무역량을 다룬다.",
                timestamp,
            ),
        )
        chunk_id = conn.execute("SELECT chunk_id FROM sqf_document_chunks").fetchone()["chunk_id"]
        conn.execute(
            """
            INSERT INTO sqf_chunk_job_level_matches(
                chunk_id, sqf_job_level_id, sqf_source_key, relation,
                score, method, evidence_text, matched_terms_json,
                review_status, created_at
            ) VALUES (?, 'job-level:accounting:3', 'sqf:test:accounting:3',
                'strongEvidence', 18.5, 'pdf_chunk_lexical_precision_v1',
                '회계(3) 수준은 전표 처리와 회계정보 산출 업무를 수행한다.',
                '{"exact": ["회계", "회계(3)"]}', 'candidate', ?)
            """,
            (chunk_id, timestamp),
        )
        conn.commit()
        conn.close()

    def test_build_sqf_report_review_seedpack_is_export_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            self._build_sample_db(db_path)

            report = build_sqf_report_review_seedpack(
                db_path,
                major_code="02",
                keywords=["회계"],
                limit=5,
            )

            self.assertTrue(report["ok"])
            self.assertEqual(report["batch"]["item_count"], 1)
            self.assertFalse(report["batch"]["status_update_allowed"])
            self.assertFalse(report["batch"]["used_for_scoring"])
            serialized = json.dumps(report, ensure_ascii=False)
            for forbidden in ["asset_path", "local_path", "db_path", "source_payload", "raw_payload", "raw_response"]:
                self.assertNotIn(forbidden, serialized)
            item = report["items"][0]
            self.assertEqual(item["decision"], "")
            self.assertFalse(item["status_update_allowed"])
            self.assertFalse(item["used_for_scoring"])
            self.assertEqual(item["sqf"]["job_name"], "회계")
            self.assertEqual(item["ncs_candidate"]["unit_name"], "전표관리")
            self.assertEqual(item["sqf_ncs_match"]["relation"], "closeMatch")
            self.assertEqual(len(item["report_evidence"]), 1)
            self.assertIn("회계", item["report_evidence"][0]["document"]["title"])

    def test_write_sqf_report_review_seedpack_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            out_jsonl = Path(tmp) / "seedpack.jsonl"
            out_md = Path(tmp) / "seedpack.md"
            self._build_sample_db(db_path)
            report = build_sqf_report_review_seedpack(
                db_path,
                major_code="02",
                keywords=["회계"],
                limit=5,
            )

            write_sqf_report_review_seedpack_jsonl(report, out_jsonl)
            write_sqf_report_review_seedpack_markdown(report, out_md)

            records = [json.loads(line) for line in out_jsonl.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records[0]["record_type"], "batch")
            self.assertEqual(records[1]["record_type"], "sqf_report_review_item")
            markdown = out_md.read_text(encoding="utf-8")
            self.assertIn("SQF Report-Grounded Human Review Seedpack", markdown)
            self.assertIn("used_for_scoring: false", markdown)
            serialized = json.dumps(records, ensure_ascii=False) + markdown
            for forbidden in ["asset_path", "local_path", "db_path", "source_payload", "raw_payload", "raw_response"]:
                self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
