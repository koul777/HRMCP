from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.mvp_workflow import (
    SCOPE_MANAGEMENT_SUPPORT_HR_MVP,
    build_mvp_mapping_candidates,
    ensure_mvp_ksa_concepts,
    review_mvp_mapping_candidates,
    run_mvp_bootstrap,
)


def seed_mvp_fixture(conn) -> None:
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("02", "경영·회계·사무", "02", "총무·인사", "03", "일반사무", "02", "사무행정"),
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
    timestamp = now_utc()
    units = [
        ("0202030201_22v3", "0202030201", "22v3", "사무행정", "2"),
        ("0202030202_22v3", "0202030202", "22v3", "사무행정지원", "5"),
    ]
    for unit in units:
        conn.execute(
            """
            INSERT INTO competency_units(
                unit_code, base_unit_code, unit_version, unit_name_raw,
                unit_level_raw, classification_id, api_definition,
                api_match_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'matched', ?, ?)
            """,
            (
                unit[0],
                unit[1],
                unit[2],
                unit[3],
                unit[4],
                classification_id,
                "사무행정 문서를 작성하고 관리하는 능력이다.",
                timestamp,
                timestamp,
            ),
        )
        conn.execute(
            """
            INSERT INTO competency_elements(
                unit_code, element_no, element_code_raw,
                element_name_raw, element_level_raw
            ) VALUES (?, '1', ?, '문서 작성하기', ?)
            """,
            (unit[0], f"{unit[0]} 1", unit[4]),
        )
    element_id = conn.execute(
        "SELECT element_id FROM competency_elements WHERE unit_code = ?",
        ("0202030201_22v3",),
    ).fetchone()["element_id"]
    conn.execute(
        """
        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
        VALUES (?, '1', '목적에 맞게 문서를 작성할 수 있다.')
        """,
        (element_id,),
    )
    conn.execute(
        """
        INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
        VALUES (?, '01', '지식', '1', '문서작성 원칙')
        """,
        (element_id,),
    )
    conn.execute(
        """
        INSERT INTO sqf_duties(
            source_key, ncs_lclas_cd, ncs_lclas_name, sqf_field_name,
            sqf_sub_field_name, job_name, duty_name, duty_level,
            duty_level_name, duty_level_definition, duty_definition,
            autonomy_responsibility, duty_acarr, duty_education_training,
            duty_qualification, duty_career, duty_license, duty_remark,
            source_payload, api_fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "sqf:test:management-support:2",
            "02",
            "경영·회계·사무",
            "경영관리",
            "",
            "경영지원",
            "사무행정(2)",
            "2",
            "초급 사무행정",
            "기안 문서 작성과 사무환경 관리를 수행하는 수준",
            "구성원 업무 보조를 위한 문서를 작성하고 관리하는 일",
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            "{}",
            timestamp,
        ),
    )
    conn.commit()


class MvpWorkflowTests(unittest.TestCase):
    def test_mvp_review_accepts_top_candidate_and_rejects_rest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            seed_mvp_fixture(conn)

            build_summary = build_mvp_mapping_candidates(conn, limit_per_duty=5)
            review_summary = review_mvp_mapping_candidates(
                conn,
                accept_top_n=1,
                min_accept_score=7.0,
            )

            statuses = {
                row["review_status"]: row["count"]
                for row in conn.execute(
                    "SELECT review_status, COUNT(*) AS count FROM sqf_ncs_matches GROUP BY review_status"
                )
            }
            audit_count = conn.execute("SELECT COUNT(*) FROM review_audit_log").fetchone()[0]
            conn.close()

            self.assertEqual(build_summary["scope_tag"], SCOPE_MANAGEMENT_SUPPORT_HR_MVP)
            self.assertEqual(review_summary["accepted"], 1)
            self.assertGreaterEqual(review_summary["rejected"], 1)
            self.assertEqual(statuses["accepted"], 1)
            self.assertGreaterEqual(statuses["rejected"], 1)
            self.assertEqual(audit_count, review_summary["accepted"] + review_summary["rejected"])

    def test_mvp_bootstrap_links_ksa_concepts_for_accepted_units(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            seed_mvp_fixture(conn)
            conn.close()

            summary = run_mvp_bootstrap(
                db_path,
                limit_per_duty=5,
                accept_top_n=1,
                min_accept_score=7.0,
            )
            conn = connect(db_path)
            initialize_database(conn)
            linked = conn.execute("SELECT COUNT(*) FROM ksa_concept_links").fetchone()[0]
            conn.close()

            self.assertEqual(summary["scope"]["scope_tag"], SCOPE_MANAGEMENT_SUPPORT_HR_MVP)
            self.assertGreaterEqual(summary["review_mappings"]["accepted"], 1)
            self.assertGreaterEqual(summary["ksa_concepts"]["accepted_unit_ksa_linked"], 1)
            self.assertGreaterEqual(linked, 1)


if __name__ == "__main__":
    unittest.main()
