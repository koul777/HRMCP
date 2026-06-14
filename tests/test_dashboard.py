from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ncs_dashboard import (
    HTML,
    get_api_orphans,
    get_classifications,
    get_item_detail,
    get_items,
    get_issues,
    get_progress,
    get_status,
    get_unit_detail,
    get_units,
    get_workbench,
    review_mapping_candidate,
    review_refinement_job,
)

SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, insert_quality_issue, now_utc


class DashboardTests(unittest.TestCase):
    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_status_shape(self) -> None:
        status = get_status(ROOT / "data" / "processed" / "ncs.db")
        self.assertIn("counts", status)
        self.assertIn("element_progress", status)
        self.assertIn("sqf", status)
        self.assertIn("ontology", status)
        self.assertGreaterEqual(status["counts"]["competency_units"], 1)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_issues_shape(self) -> None:
        result = get_issues(ROOT / "data" / "processed" / "ncs.db", {"limit": ["5"]})
        self.assertIn("issues", result)
        self.assertIsInstance(result["issues"], list)

    def test_dashboard_html_has_lookup_and_large_editor(self) -> None:
        self.assertIn("NCS-SQF 온톨로지 워크벤치", HTML)
        self.assertIn("온톨로지 준비 전처리 단계", HTML)
        self.assertIn("경영지원 MVP", HTML)
        self.assertIn("setManagementSupportMvp()", HTML)
        self.assertIn("/api/preprocess", HTML)
        self.assertIn("min-height:210px", HTML)
        self.assertIn("min-width:420px", HTML)
        self.assertIn('id="majorCode" class="code" value=""', HTML)
        self.assertIn("setHrScope()", HTML)
        self.assertIn("scheduleAutoRefresh", HTML)
        self.assertIn("자동갱신 30초", HTML)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_lookup_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        classifications = get_classifications(db_path, params)
        self.assertIn("classifications", classifications)
        self.assertGreaterEqual(len(classifications["classifications"]), 1)

        units = get_units(db_path, params)
        self.assertIn("units", units)
        self.assertGreaterEqual(len(units["units"]), 1)
        self.assertLessEqual(units["units"][0]["element_matched"], units["units"][0]["element_count"])

        detail = get_unit_detail(db_path, {"unit_code": [units["units"][0]["unit_code"]]})
        self.assertIn("unit", detail)
        self.assertIn("elements", detail)

        api_orphans = get_api_orphans(db_path, {"limit": ["5"]})
        self.assertIn("api_orphans", api_orphans)
        self.assertIsInstance(api_orphans["api_orphans"], list)

    @unittest.skipUnless(
        (ROOT / "data" / "processed" / "ncs.db").exists(),
        "local generated DB is not available",
    )
    def test_dashboard_workbench_shapes(self) -> None:
        db_path = ROOT / "data" / "processed" / "ncs.db"
        params = {
            "major_code": ["02"],
            "middle_code": ["02"],
            "small_code": ["02"],
            "sub_code": ["01"],
            "limit": ["5"],
        }
        progress = get_progress(db_path, params)
        self.assertIn("phases", progress)
        self.assertGreaterEqual(len(progress["phases"]), 1)

        workbench = get_workbench(db_path, params)
        self.assertIn("cards", workbench)
        self.assertGreaterEqual(len(workbench["cards"]), 1)

        items = get_items(db_path, {**params, "kind": ["element"], "state": ["api_matched"]})
        self.assertIn("items", items)
        self.assertGreaterEqual(len(items["items"]), 1)

        detail = get_item_detail(
            db_path,
            {"kind": ["element"], "id": [str(items["items"][0]["id"])]},
        )
        self.assertIn("item", detail)
        self.assertEqual(detail["item"]["kind"], "element")

        sqf_items = get_items(
            db_path,
            {"kind": ["sqf"], "state": ["mvp"], "limit": ["10"]},
        )
        self.assertIn("items", sqf_items)
        self.assertGreaterEqual(sqf_items["total"], 0)
        if sqf_items["items"]:
            sqf_detail = get_item_detail(
                db_path,
                {"kind": ["sqf"], "id": [str(sqf_items["items"][0]["id"])]},
            )
            self.assertIn("item", sqf_detail)
            self.assertEqual(sqf_detail["item"]["kind"], "sqf")

    def test_dashboard_review_actions_create_audit_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO sqf_duties(
                    source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
                    job_name, duty_name, duty_level, source_payload, api_fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "02", "경영·회계·사무", "경영관리", "경영지원", "사무행정(2)", "2", "{}", timestamp),
            )
            conn.execute(
                """
                INSERT INTO sqf_ncs_matches(
                    source_id, target_type, target_id, relation, score, confidence,
                    match_method, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("sqf:test", "ncs_competency_unit", "0202030201_22v3", "partiallyCovers", 8, "lexical", "test", "candidate", timestamp, timestamp),
            )
            match_id = conn.execute("SELECT match_id FROM sqf_ncs_matches").fetchone()["match_id"]
            conn.commit()
            conn.close()

            result = review_mapping_candidate(
                db_path,
                {"match_id": match_id, "action": "accept", "reviewer_id": "tester", "notes": "ok"},
            )
            self.assertEqual(result["new_status"], "accepted")

            conn = connect(db_path)
            initialize_database(conn)
            audit_count = conn.execute("SELECT COUNT(*) AS count FROM review_audit_log").fetchone()["count"]
            self.assertEqual(audit_count, 1)
            conn.close()

    def test_dashboard_refinement_review_applies_to_refined_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사"),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()["classification_id"]
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                ("0202020101_23v3", "0202020101", "23v3", "인사기획", "6", classification_id, "matched", timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(unit_code, element_no, element_code_raw, element_name_raw, element_level_raw)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("0202020101_23v3", "1", "0202020101_23v3 1", "인사전략 수립하기", "6"),
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                "INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw) VALUES (?, ?, ?)",
                (element_id, "1", "인사전략  환경을 분석할 수 있다"),
            )
            criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()["criteria_id"]
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=criteria_id,
                issue_type="double_space",
                severity="info",
                issue_detail="공백",
            )
            issue_id = conn.execute("SELECT issue_id FROM quality_issues").fetchone()["issue_id"]
            conn.execute(
                """
                INSERT INTO refinement_jobs(
                    target_type, target_id, source_issue_id, model_name, prompt_version,
                    input_hash, raw_text, refined_text, rationale, confidence,
                    output_text, review_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "criteria",
                    str(criteria_id),
                    issue_id,
                    "jsonl-import",
                    "test",
                    "hash",
                    "인사전략  환경을 분석할 수 있다",
                    "인사전략 환경을 분석할 수 있다.",
                    "공백과 문장부호 보정",
                    0.8,
                    "{}",
                    "review_required",
                    timestamp,
                ),
            )
            job_id = conn.execute("SELECT job_id FROM refinement_jobs").fetchone()["job_id"]
            conn.commit()
            conn.close()

            result = review_refinement_job(
                db_path,
                {"job_id": job_id, "action": "approve_refined", "reviewer_id": "tester"},
            )
            self.assertEqual(result["new_status"], "applied")

            conn = connect(db_path)
            initialize_database(conn)
            row = conn.execute("SELECT criteria_text_refined, review_status FROM performance_criteria").fetchone()
            self.assertEqual(row["criteria_text_refined"], "인사전략 환경을 분석할 수 있다.")
            self.assertEqual(row["review_status"], "human_reviewed")
            conn.close()


if __name__ == "__main__":
    unittest.main()
