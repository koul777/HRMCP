from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import review_safety
from ncs_mcp.review_safety import (
    is_automated_reviewer,
    latest_audit_provenance,
    packet_backed_entity_ids,
    review_packet_sha256,
)

AUDIT_TABLE = """
    CREATE TABLE review_audit_log(
        id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT, action TEXT, new_status TEXT,
        reviewer_id TEXT, source_decision_packet TEXT, source_artifact_hash TEXT, rationale TEXT,
        evidence_refs_json TEXT
    )
"""


class PacketBackedProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.packet = Path(self.tmp.name) / "decision_audit.json"
        self.packet.write_text("{}", encoding="utf-8")
        self.digest = "sha256:" + review_packet_sha256(self.packet)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(AUDIT_TABLE)
        self.resolver = patch(
            "ncs_mcp.review_safety.resolve_repo_reports_artifact",
            side_effect=lambda ref, **_: self.packet if ref == "reports/decision_audit.json" else None,
        )
        self.resolver.start()

    def tearDown(self) -> None:
        self.resolver.stop()
        self.conn.close()
        self.tmp.cleanup()

    def audit(self, entity_id: str, reviewer: str, *, packet: bool = True, status: str = "human_reviewed") -> None:
        self.conn.execute(
            "INSERT INTO review_audit_log(entity_type, entity_id, action, new_status, reviewer_id, "
            "source_decision_packet, source_artifact_hash, rationale, evidence_refs_json) "
            "VALUES ('ncs_career_path', ?, 'review', ?, ?, ?, ?, ?, ?)",
            (entity_id, status, reviewer, "reports/decision_audit.json" if packet else None,
             self.digest if packet else None, "checked" if packet else None,
             json.dumps([f"career_path_id:{entity_id}"]) if packet else None),
        )

    def test_only_the_latest_audit_row_counts(self) -> None:
        self.audit("1", "reviewer")
        self.audit("2", "reviewer")
        self.audit("2", "reviewer", packet=False, status="candidate")
        self.audit("2", "codex_report_review_20260619", packet=False)
        statuses = {"1": "human_reviewed", "2": "human_reviewed", "3": "human_reviewed"}
        self.assertEqual(packet_backed_entity_ids(self.conn, "ncs_career_path", statuses), {"1"})
        provenance = latest_audit_provenance(self.conn, statuses, {key: [("ncs_career_path", key)] for key in statuses})
        self.assertIn("human_reviewer_id", provenance["2"][0])
        self.assertEqual(provenance["3"], (["review_audit_log"], None))

    def test_job_ids_are_not_human_reviewers(self) -> None:
        for reviewer in ("", "dashboard", "dashboard_click", "codex_report_review_20260619", "claude-session", "mcp"):
            with self.subTest(reviewer=reviewer):
                self.assertTrue(is_automated_reviewer(reviewer))
        for reviewer in ("dd", "reviewer", "systems_team_lead", "user_directed_report_review_20260619"):
            with self.subTest(reviewer=reviewer):
                self.assertFalse(is_automated_reviewer(reviewer))

    def test_missing_audit_table_or_columns_fail_closed(self) -> None:
        statuses = {"1": "human_reviewed"}
        keys = {"1": [("ncs_career_path", "1")]}
        bare = sqlite3.connect(":memory:")
        bare.row_factory = sqlite3.Row
        self.assertEqual(latest_audit_provenance(bare, statuses, keys), {"1": (["review_audit_log"], None)})
        bare.execute("CREATE TABLE review_audit_log(id INTEGER PRIMARY KEY, entity_type TEXT, entity_id TEXT)")
        self.assertEqual(latest_audit_provenance(bare, statuses, keys), {"1": (["review_audit_log_columns"], None)})
        self.assertEqual(packet_backed_entity_ids(bare, "ncs_career_path", statuses), set())
        bare.close()

    def test_packet_hash_is_computed_once_per_file_version(self) -> None:
        for entity_id in ("1", "2", "3"):
            self.audit(entity_id, "reviewer")
        review_safety._PACKET_SHA256_CACHE.clear()
        with patch("ncs_mcp.review_safety.review_packet_sha256", wraps=review_packet_sha256) as digest:
            backed = packet_backed_entity_ids(self.conn, "ncs_career_path",
                                              {key: "human_reviewed" for key in ("1", "2", "3")})
        self.assertEqual(backed, {"1", "2", "3"})
        self.assertEqual(digest.call_count, 1)


class TrustedCareerPathLimitTests(unittest.TestCase):
    def test_provenance_is_checked_before_the_limit(self) -> None:
        from ncs_mcp.db import connect, initialize_database, now_utc
        from ncs_mcp.training_recommendation import _packet_backed_career_paths

        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            conn.execute("PRAGMA foreign_keys = OFF")
            timestamp = now_utc()
            columns = ("source_file, source_row_number, major_code_raw, middle_code_raw, small_code_raw, job_code_raw, "
                       "job_name, competency_code_raw, competency_level_raw, competency_name, position_level_raw, "
                       "position_name, major_code, middle_code, small_code, sub_code, matched_classification_id, "
                       "matched_unit_code, classification_match_method, unit_match_method, confidence_score, "
                       "review_status, created_at, updated_at")
            rows = [("bulk.csv", index, "2", "2", "2", "1", f"Status only {index:03d}", "0202020101", "9",
                     f"Status only {index:03d}", "9", "Director", "02", "02", "02", "01", 1, "0202020101_23v1",
                     "code_exact", "unit_name_exact", 1.0, "human_reviewed", timestamp, timestamp)
                    for index in range(301)]
            rows.append(("bulk.csv", 999, "2", "2", "2", "1", "Backed", "0202020101", "1", "Backed", "1", "Staff",
                         "02", "02", "02", "01", 1, "0202020101_23v1", "code_exact", "unit_name_exact", 1.0,
                         "human_reviewed", timestamp, timestamp))
            conn.executemany(f"INSERT INTO ncs_career_paths({columns}) VALUES ({','.join('?' * 24)})", rows)
            backed_id = conn.execute("SELECT career_path_id FROM ncs_career_paths WHERE job_name = 'Backed'").fetchone()[0]
            packet = Path(tmp) / "decision_audit.json"
            packet.write_text("{}", encoding="utf-8")
            conn.execute(
                "INSERT INTO review_audit_log(entity_type, entity_id, action, new_status, reviewer_id, "
                "source_decision_packet, source_artifact_hash, rationale, evidence_refs_json, created_at) "
                "VALUES ('ncs_career_path', ?, 'review', 'human_reviewed', 'reviewer', 'reports/decision_audit.json', "
                "?, 'checked', ?, ?)",
                (str(backed_id), "sha256:" + review_packet_sha256(packet), json.dumps([f"career_path_id:{backed_id}"]),
                 timestamp),
            )
            with patch("ncs_mcp.review_safety.resolve_repo_reports_artifact",
                       side_effect=lambda ref, **_: packet if ref == "reports/decision_audit.json" else None):
                paths = _packet_backed_career_paths(conn, {"0202020101_23v1"}, limit=300)
            conn.close()
        self.assertEqual([path["job_name"] for path in paths], ["Backed"])


if __name__ == "__main__":
    unittest.main()
