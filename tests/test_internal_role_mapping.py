from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.internal_job_roles import ContractValidationError  # noqa: E402
from ncs_mcp.internal_role_mapping import (  # noqa: E402
    INTERNAL_ROLE_MAPPING_PACKET_SCHEMA,
    MAPPING_METHOD,
    load_ncs_job_contexts,
    map_internal_job_role,
    map_internal_job_roles,
)


SCHEMA = """
CREATE TABLE classifications (
  classification_id INTEGER PRIMARY KEY, major_code TEXT, major_name TEXT,
  middle_code TEXT, middle_name TEXT, small_code TEXT, small_name TEXT,
  sub_code TEXT, sub_name TEXT
);
CREATE TABLE competency_units (
  unit_code TEXT PRIMARY KEY, classification_id INTEGER,
  unit_name_refined TEXT, unit_name_raw TEXT, api_unit_name TEXT
);
CREATE TABLE competency_elements (
  element_id INTEGER PRIMARY KEY, unit_code TEXT,
  element_name_refined TEXT, element_name_raw TEXT, api_element_name TEXT
);
"""


class InternalRoleMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "ncs.db"
        connection = sqlite3.connect(self.db_path)
        try:
            connection.executescript(SCHEMA)
            connection.executemany(
                "INSERT INTO classifications VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (1, "02", "경영", "01", "기획", "01", "인사", "01", "인사기획"),
                    (2, "03", "재무", "01", "회계", "01", "세무", "01", "세무회계"),
                    (3, "99", "테스트", "01", "직무", "01", "국제", "01", "글로벌조달"),
                ],
            )
            connection.executemany(
                "INSERT INTO competency_units VALUES (?, ?, ?, ?, ?)",
                [
                    ("0201010101", 1, None, "인력운영계획", None),
                    ("0301010101", 2, None, "세무 신고", None),
                    ("9901010101", 3, None, "글로벌 조달 계약", None),
                ],
            )
            connection.executemany(
                "INSERT INTO competency_elements VALUES (?, ?, ?, ?, ?)",
                [
                    (11, "0201010101", None, "인력운영계획 수립", None),
                    (12, "0301010101", None, "세무 신고서 작성", None),
                    (13, "9901010101", None, "해외 조달 계약 관리", None),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def role(**overrides):
        payload = {
            "organization_namespace": "Acme",
            "role_id": "hr-plan",
            "display_name": "인사기획",
            "description": "인력운영계획 수립",
            "duties": ["인력 계획"],
        }
        payload.update(overrides)
        return payload

    def test_deterministic_ranking_and_target_code(self) -> None:
        first = map_internal_job_role(self.role(), self.db_path, limit=3)
        second = map_internal_job_role(self.role(), self.db_path, limit=3)
        self.assertEqual(first["status"], "candidate")
        self.assertEqual(first["alignment_candidates"], second["alignment_candidates"])
        candidate = first["alignment_candidates"][0]
        self.assertEqual(candidate["ncs_target_type"], "ncs_job")
        self.assertEqual(candidate["ncs_target_key"], "02010101")
        self.assertEqual(candidate["method"], MAPPING_METHOD)
        self.assertEqual(candidate["status"], "candidate")
        self.assertTrue(candidate["evidence"])

    def test_uses_all_ncs_scope_not_major_02_default(self) -> None:
        result = map_internal_job_role(
            self.role(role_id="global", display_name="글로벌조달", description="해외 조달 계약"),
            self.db_path,
        )
        self.assertEqual(result["alignment_candidates"][0]["ncs_target_key"], "99010101")
        self.assertEqual(result["provenance"]["ncs_scope"], "all_classifications")
        self.assertIsNone(result["provenance"]["major_code_filter"])

    def test_provenance_is_read_only_and_candidate_only(self) -> None:
        result = map_internal_job_role(self.role(), self.db_path)
        provenance = result["provenance"]
        self.assertFalse(provenance["db_writes"])
        self.assertFalse(provenance["neo4j_writes"])
        self.assertFalse(provenance["human_approval_claim"])
        self.assertNotIn("human_reviewed", str(result))
        self.assertNotIn("accepted", str(result))

    def test_pii_role_payload_is_rejected_before_query(self) -> None:
        with self.assertRaises(ContractValidationError):
            map_internal_job_role(self.role(employee_id="E-1"), self.db_path)

    def test_semantic_recall_is_not_score_or_evidence(self) -> None:
        result = map_internal_job_role(
            self.role(display_name="무관한표현", description=""), self.db_path,
            semantic_candidates=[{"ncs_target_key": "99010101", "score": 0.99, "model": "external"}],
        )
        self.assertEqual(result["status"], "unresolved")
        candidate = result["alignment_candidates"][0]
        self.assertEqual(candidate["ncs_target_key"], "99010101")
        self.assertEqual(candidate["score"], 0.0)
        self.assertEqual(candidate["status"], "unresolved")
        self.assertEqual(candidate["evidence"], [])
        self.assertFalse(result["provenance"]["semantic_scores_used_as_evidence"])

    def test_batch_packet_reuses_all_scope_catalog(self) -> None:
        contexts = load_ncs_job_contexts(self.db_path)
        self.assertEqual({context.code for context in contexts}, {"02010101", "03010101", "99010101"})
        packet = map_internal_job_roles([self.role()], self.db_path)
        self.assertEqual(packet["schema"], INTERNAL_ROLE_MAPPING_PACKET_SCHEMA)
        self.assertEqual(len(packet["role_results"]), 1)
        self.assertFalse(packet["provenance"]["db_writes"])


if __name__ == "__main__":
    unittest.main()
