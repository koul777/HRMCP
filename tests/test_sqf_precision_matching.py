from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.collect_api import upsert_sqf_items
from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.sqf_precision_matching import (
    build_sqf_chunk_job_level_matches,
    fetch_chunks,
    score_chunk_for_job_level,
)
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model


class SqfPrecisionMatchingTests(unittest.TestCase):
    def test_score_chunk_for_job_level_uses_duty_job_sector_and_level(self) -> None:
        row = {
            "duty_name": "HR planning",
            "job_name": "HR support",
            "sqf_field_name": "Management",
            "sqf_sub_field_name": "HR",
            "sector_name": "Management/HR",
            "ncs_lclas_name": "Business",
            "sqf_level": 5,
            "job_definition": "Plans workforce strategy.",
            "job_level_definition": "Performs complex planning tasks.",
            "duty_definition": "Performs HR planning work.",
            "autonomy_responsibility": "Works with autonomy.",
        }
        result = score_chunk_for_job_level(
            "Management HR support HR planning L5 performs workforce planning.",
            row,
        )
        self.assertGreaterEqual(result["score"], 15)
        self.assertEqual(result["relation"], "strongEvidence")

    def test_build_sqf_chunk_job_level_matches_from_sample_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            upsert_sqf_items(
                conn,
                [
                    {
                        "ncsLclasCd": "02",
                        "ncsLclasCdnm": "Business",
                        "sqfFldCdnm": "Management",
                        "sqfSubFldCdnm": "HR",
                        "jobCdnm": "HR support",
                        "dutyNm": "HR planning",
                        "dutyLevel": "5",
                        "dutyLevelNm": "Practitioner",
                        "dutyLevelDef": "Performs complex HR planning tasks.",
                        "dutyDef": "Performs HR planning work.",
                        "autoResp": "Works with partial autonomy and responsibility.",
                    }
                ],
            )
            conn.commit()
            conn.close()
            build_sqf_sqlite_model(db_path)

            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_library_posts(
                    lib_seq, title, source_url, collected_at, ontology_role
                ) VALUES ('1', 'HR SQF report', 'https://example.test', ?, 'case_report')
                """,
                (ts,),
            )
            conn.execute(
                """
                INSERT INTO sqf_library_files(
                    lib_seq, sys_dstin_cd, file_mstky, file_detl_seq,
                    local_path, download_status
                ) VALUES ('1', '00', 'f', '1', 'sample.pdf', 'downloaded')
                """
            )
            file_id = conn.execute("SELECT file_id FROM sqf_library_files").fetchone()["file_id"]
            conn.execute(
                """
                INSERT INTO sqf_document_sources(
                    lib_seq, file_id, title, ontology_role, text_extraction_status, created_at
                ) VALUES ('1', ?, 'HR SQF report', 'case_report', 'extracted', ?)
                """,
                (file_id, ts),
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
                (document_id, ts),
            )
            asset_id = conn.execute("SELECT asset_id FROM sqf_document_assets").fetchone()["asset_id"]
            conn.execute(
                """
                INSERT INTO sqf_document_chunks(
                    asset_id, chunk_index, page_start, page_end, text,
                    char_count, token_estimate, created_at
                ) VALUES (?, 0, 1, 1, ?, 120, 40, ?)
                """,
                (
                    asset_id,
                    "Management HR support HR planning L5 performs complex HR planning tasks.",
                    ts,
                ),
            )
            conn.commit()
            conn.close()

            result = build_sqf_chunk_job_level_matches(db_path, min_score=9)
            self.assertGreaterEqual(result["matches_inserted"], 1)

            conn = connect(db_path)
            count = conn.execute("SELECT COUNT(*) FROM sqf_chunk_job_level_matches").fetchone()[0]
            conn.close()
            self.assertGreaterEqual(count, 1)

    def test_framework_reference_chunks_are_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            ts = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_library_posts(
                    lib_seq, title, source_url, collected_at, ontology_role
                ) VALUES ('1', 'KQF SQF purpose', 'local:test.pdf', ?, 'framework_reference')
                """,
                (ts,),
            )
            conn.execute(
                """
                INSERT INTO sqf_library_files(
                    lib_seq, sys_dstin_cd, file_mstky, file_detl_seq,
                    local_path, download_status
                ) VALUES ('1', 'LOCAL', 'f', '1', 'test.pdf', 'downloaded')
                """
            )
            file_id = conn.execute("SELECT file_id FROM sqf_library_files").fetchone()["file_id"]
            conn.execute(
                """
                INSERT INTO sqf_document_sources(
                    lib_seq, file_id, title, ontology_role, text_extraction_status, created_at
                ) VALUES ('1', ?, 'KQF SQF purpose', 'framework_reference', 'extracted', ?)
                """,
                (file_id, ts),
            )
            document_id = conn.execute("SELECT document_id FROM sqf_document_sources").fetchone()[
                "document_id"
            ]
            conn.execute(
                """
                INSERT INTO sqf_document_assets(
                    document_id, asset_path, asset_name, asset_type,
                    extraction_status, created_at
                ) VALUES (?, 'test.pdf', 'test.pdf', 'pdf', 'extracted', ?)
                """,
                (document_id, ts),
            )
            asset_id = conn.execute("SELECT asset_id FROM sqf_document_assets").fetchone()["asset_id"]
            conn.execute(
                """
                INSERT INTO sqf_document_chunks(
                    asset_id, chunk_index, page_start, page_end, text,
                    char_count, token_estimate, created_at
                ) VALUES (?, 0, 1, 1, 'SQF and KQF connect training degree qualification career.', 80, 20, ?)
                """,
                (asset_id, ts),
            )
            conn.commit()

            self.assertEqual(len(fetch_chunks(conn)), 0)
            self.assertEqual(len(fetch_chunks(conn, include_framework_references=True)), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
