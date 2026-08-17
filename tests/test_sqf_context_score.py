from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import initialize_database, now_utc
from ncs_mcp.sqf_context_score import (
    _context_pair_score,
    build_sqf_context_score_report,
    write_sqf_context_score_json,
    write_sqf_context_score_markdown,
)


def seed_sqf_context_fixture(conn: sqlite3.Connection) -> None:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'HR')
        """
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[0]
    for unit_code, unit_name, level in (
        ("0202020101_26v1", "HR planning", "5"),
        ("0202020201_26v1", "Labor management", "5"),
    ):
        conn.execute(
            """
            INSERT INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, created_at, updated_at
            ) VALUES (?, ?, '26v1', ?, ?, ?, ?, ?)
            """,
            (unit_code, unit_code[:10], unit_name, level, classification_id, timestamp, timestamp),
        )
    conn.execute(
        """
        INSERT INTO sqf_industry_sectors(
            sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
            sqf_sub_field_name, sector_name, source_count, updated_at
        ) VALUES ('sector-hr', '02', 'Business', 'Management Support',
                  'HR', 'Management Support > HR', 2, ?)
        """,
        (timestamp,),
    )
    conn.execute(
        """
        INSERT INTO sqf_jobs_normalized(
            sqf_job_id, sector_id, job_name, source_count, updated_at
        ) VALUES ('sqf-job-hr', 'sector-hr', 'HR', 2, ?)
        """,
        (timestamp,),
    )
    conn.execute(
        "INSERT INTO sqf_levels(sqf_level, level_name, definition, updated_at) VALUES (5, 'L5', 'test', ?)",
        (timestamp,),
    )
    for source_key, duty_name, unit_code in (
        ("sqf-source-labor", "Labor management", "0202020201_26v1"),
        ("sqf-source-planning", "HR planning", "0202020101_26v1"),
    ):
        conn.execute(
            """
            INSERT INTO sqf_duties(
                source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, job_name, duty_name, duty_level,
                source_payload, api_fetched_at
            ) VALUES (?, '02', 'Business', 'Management Support', 'HR',
                      'HR', ?, '5', '{}', ?)
            """,
            (source_key, duty_name, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_job_levels_normalized(
                sqf_job_level_id, sqf_job_id, sqf_source_key, duty_name,
                sqf_level, level_name, updated_at
            ) VALUES (?, 'sqf-job-hr', ?, ?, 5, 'L5', ?)
            """,
            (f"level-{source_key}", source_key, duty_name, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_ncs_matches(
                source_type, source_id, target_type, target_id, relation,
                score, confidence, match_method, evidence_text, review_status,
                filter_status, created_at, updated_at
            ) VALUES ('sqf_duty', ?, 'ncs_competency_unit', ?, 'contextual',
                      10.0, 'lexical', 'test_sqf_context',
                      'SQF level 5 equals NCS unit level 5; legacy lexical evidence',
                      'candidate', 'eligible', ?, ?)
            """,
            (source_key, unit_code, timestamp, timestamp),
        )
    conn.commit()


class SqfContextScoreTests(unittest.TestCase):
    def test_sqf_context_report_is_context_only_and_high_for_same_job_level(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_database(conn)
        seed_sqf_context_fixture(conn)

        report = build_sqf_context_score_report(
            conn,
            current_query="Labor management",
            target_query="HR planning",
        )
        conn.close()

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], "ncs_sqf_context_score_report_v1")
        self.assertTrue(report["context_only"])
        self.assertFalse(report["recommendation_score_mutated"])
        self.assertFalse(report["sqf_used_as_training_score"])
        self.assertFalse(report["approval_ready"])
        self.assertEqual(report["status"], "review_required")
        self.assertFalse(report["modeling_policy"]["ncs_subclassification_is_sqf_job"])
        self.assertEqual(
            report["modeling_policy"]["sqf_context_granularity"],
            "level_based_job_to_ncs_competency_unit",
        )
        self.assertFalse(report["modeling_policy"]["ncs_unit_level_used_as_sqf_level"])
        self.assertFalse(report["modeling_policy"]["required_optional_inferred"])
        self.assertFalse(report["modeling_policy"]["official_recognition_inferred"])
        self.assertEqual(report["summary"]["top_sqf_context_label"], "high")
        self.assertEqual(report["summary"]["top_job_distance_label"], "same_sqf_job")
        self.assertFalse(report["summary"]["classification_scope_only"])
        self.assertEqual(report["scope_guard"]["status"], "unit_scope")
        self.assertEqual(report["summary"]["top_sqf_level_distance"], 0)
        self.assertEqual(report["summary"]["top_level_comparison_status"], "comparable_same_sqf_sector")
        self.assertEqual(report["top_pairs"][0]["sqf_context_score"], 1.0)
        self.assertEqual(report["top_pairs"][0]["mapping_review_statuses"], ["candidate"])
        self.assertNotIn("evidence_text", report["top_pairs"][0]["current_sqf"])
        self.assertTrue(report["top_pairs"][0]["current_sqf"]["legacy_evidence_text_suppressed"])

    def test_classification_scope_suppresses_same_job_high_summary(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_database(conn)
        seed_sqf_context_fixture(conn)

        report = build_sqf_context_score_report(
            conn,
            current_query="HR",
            target_query="HR planning",
        )
        conn.close()

        self.assertTrue(report["scope_guard"]["active"])
        self.assertEqual(report["scope_guard"]["status"], "classification_scope_only")
        self.assertEqual(report["resolved_scopes"]["current"]["match_level"], "sub_classification")
        self.assertEqual(report["resolved_scopes"]["target"]["match_level"], "unit")
        self.assertTrue(report["summary"]["classification_scope_only"])
        self.assertIsNone(report["summary"]["top_sqf_context_score"])
        self.assertEqual(report["summary"]["top_sqf_context_label"], "classification_scope_only")
        self.assertEqual(report["summary"]["top_job_distance_label"], "classification_scope_only")
        self.assertFalse(report["top_pairs"][0]["same_sqf_job"])
        self.assertEqual(report["top_pairs"][0]["scope_guard_status"], "classification_scope_only")
        self.assertEqual(report["top_pairs"][0]["raw_sqf_context_label"], "high")

    def test_sqf_levels_are_not_compared_across_sectors(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_database(conn)
        seed_sqf_context_fixture(conn)
        timestamp = now_utc()
        conn.execute(
            """
            INSERT INTO classifications(
                major_code, major_name, middle_code, middle_name,
                small_code, small_name, sub_code, sub_name
            ) VALUES ('20', 'IT', '01', 'Data', '01', 'Data', '01', 'Analytics')
            """
        )
        classification_id = conn.execute(
            "SELECT classification_id FROM classifications WHERE major_code = '20'"
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, created_at, updated_at
            ) VALUES ('2001010101_26v1', '2001010101', '26v1',
                      'Data analytics', '5', ?, ?, ?)
            """,
            (classification_id, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO sqf_industry_sectors(
                sector_id, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, sector_name, source_count, updated_at
            ) VALUES ('sector-data', '20', 'IT', 'Information Technology',
                      'Data', 'Information Technology > Data', 1, ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_jobs_normalized(
                sqf_job_id, sector_id, job_name, source_count, updated_at
            ) VALUES ('sqf-job-data', 'sector-data', 'Data', 1, ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_duties(
                source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                sqf_sub_field_name, job_name, duty_name, duty_level,
                source_payload, api_fetched_at
            ) VALUES ('sqf-source-data', '20', 'IT', 'Information Technology',
                      'Data', 'Data', 'Data analytics', '5', '{}', ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_job_levels_normalized(
                sqf_job_level_id, sqf_job_id, sqf_source_key, duty_name,
                sqf_level, level_name, updated_at
            ) VALUES ('level-data', 'sqf-job-data', 'sqf-source-data',
                      'Data analytics', 5, 'L5', ?)
            """,
            (timestamp,),
        )
        conn.execute(
            """
            INSERT INTO sqf_ncs_matches(
                source_type, source_id, target_type, target_id, relation,
                score, confidence, match_method, evidence_text, review_status,
                filter_status, created_at, updated_at
            ) VALUES ('sqf_duty', 'sqf-source-data', 'ncs_competency_unit',
                      '2001010101_26v1', 'contextual', 10.0, 'lexical',
                      'test_sqf_context', 'test evidence', 'candidate',
                      'eligible', ?, ?)
            """,
            (timestamp, timestamp),
        )
        conn.commit()

        report = build_sqf_context_score_report(
            conn,
            current_query="Labor management",
            target_query="Data analytics",
        )
        conn.close()

        self.assertEqual(report["summary"]["top_level_comparison_status"], "not_comparable_cross_sector")
        self.assertIsNone(report["summary"]["top_sqf_level_distance"])
        self.assertEqual(report["top_pairs"][0]["raw_sqf_level_distance"], 0)
        self.assertEqual(report["top_pairs"][0]["components"]["level_proximity"], 0.0)
        self.assertLess(report["top_pairs"][0]["sqf_context_score"], 0.75)

    def test_sqf_levels_are_not_compared_when_level_is_missing(self) -> None:
        current = {
            "sqf_job_id": "job-hr",
            "sector_id": "sector-hr",
            "ncs_lclas_cd": "02",
            "sqf_level": 5,
        }
        target = {
            "sqf_job_id": "job-hr",
            "sector_id": "sector-hr",
            "ncs_lclas_cd": "02",
            "sqf_level": 0,
        }

        pair = _context_pair_score(current, target)

        self.assertEqual(pair["level_comparison_status"], "level_missing_same_sqf_sector")
        self.assertIsNone(pair["sqf_level_distance"])
        self.assertIsNone(pair["raw_sqf_level_distance"])
        self.assertEqual(pair["components"]["level_proximity"], 0.0)

    def test_ncs_major_match_is_diagnostic_not_sqf_context_score(self) -> None:
        current = {
            "sqf_job_id": "job-labor",
            "sector_id": "sector-management",
            "ncs_lclas_cd": "02",
            "sqf_level": 5,
        }
        target = {
            "sqf_job_id": "job-finance",
            "sector_id": "sector-finance",
            "ncs_lclas_cd": "02",
            "sqf_level": 5,
        }

        pair = _context_pair_score(current, target)

        self.assertTrue(pair["same_ncs_major"])
        self.assertFalse(pair["same_sqf_sector"])
        self.assertEqual(pair["job_distance_label"], "different_sqf_context")
        self.assertEqual(pair["components"]["job_family_match"], 0.0)
        self.assertEqual(pair["level_comparison_status"], "not_comparable_cross_sector")
        self.assertLess(pair["sqf_context_score"], 0.1)

    def test_sqf_context_report_writers_emit_contract_fields(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        initialize_database(conn)
        seed_sqf_context_fixture(conn)
        report = build_sqf_context_score_report(
            conn,
            current_query="Labor management",
            target_query="HR planning",
        )
        conn.close()

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "sqf_context.json"
            markdown_path = Path(tmp) / "sqf_context.md"
            write_sqf_context_score_json(report, json_path)
            write_sqf_context_score_markdown(report, markdown_path)
            saved = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertFalse(saved["recommendation_score_mutated"])
        self.assertIn("modeling_policy", saved)
        serialized = json.dumps(saved, ensure_ascii=False)
        self.assertNotIn("SQF level 5 equals NCS unit level 5", serialized)
        self.assertNotIn('"evidence_text"', serialized)
        self.assertIn("SQF Context Score Report", markdown)
        self.assertIn("Modeling Policy", markdown)
        self.assertIn("Scope Guard", markdown)
        self.assertIn("ncs_subclassification_is_sqf_job", markdown)
        self.assertIn("required_optional_inferred", markdown)
        self.assertIn("recommendation_score_mutated", markdown)
        self.assertIn("same_sqf_job", markdown)

        report["top_pairs"][0]["target_sqf"]["sqf_level"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "sqf_context_missing_level.md"
            write_sqf_context_score_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("L-", markdown)
        self.assertNotIn(" L0", markdown)


if __name__ == "__main__":
    unittest.main()
