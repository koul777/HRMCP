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
from ncs_mcp.db import connect, initialize_database
from ncs_mcp.preprocess_sqf_documents import chunk_pages, infer_tags, normalize_text
from ncs_mcp.sqf_sqlite import build_sqf_sqlite_model


class SqfSqliteTests(unittest.TestCase):
    def test_build_sqf_sqlite_model_from_api_rows(self) -> None:
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
                        "dutyEduTrain": "HR management training",
                        "dutyQualf": "HR certificate",
                        "dutyCarr": "2 years related career",
                    }
                ],
            )
            conn.commit()
            conn.close()

            result = build_sqf_sqlite_model(db_path)
            self.assertEqual(result["counts"]["sqf_framework_concepts"], 10)
            self.assertEqual(result["counts"]["sqf_industry_sectors"], 1)
            self.assertEqual(result["counts"]["sqf_jobs_normalized"], 1)
            self.assertEqual(result["counts"]["sqf_job_levels_normalized"], 1)
            self.assertGreaterEqual(result["counts"]["sqf_recognition_evidence"], 3)

    def test_text_normalization_chunking_and_tags(self) -> None:
        text = normalize_text(
            "SQF job level\n\n교육훈련과 자격, 현장경력을 연계한다."
        )
        self.assertIn("SQF job level", text)
        tags = infer_tags(text)
        self.assertIn("SQF", tags)
        self.assertIn("EDUCATION_TRAINING", tags)
        chunks = chunk_pages(
            [
                {"page_no": 1, "text": text},
                {"page_no": 2, "text": "NCS 기반 직무역량 인정기준을 제시한다."},
            ],
            chunk_chars=30,
            overlap_chars=0,
        )
        self.assertGreaterEqual(len(chunks), 1)
        self.assertIn("text", chunks[0])


if __name__ == "__main__":
    unittest.main()
