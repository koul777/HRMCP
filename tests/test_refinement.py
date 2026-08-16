from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, insert_quality_issue, now_utc
from ncs_mcp.refinement import (
    apply_refinement_jobs,
    create_refinement_jobs,
    export_refinement_jsonl,
    import_refinement_jsonl,
    refinement_stats,
    safe_typo_refinement,
    unsafe_refinement_reason,
)


def seed_unit_with_criteria(conn) -> int:
    conn.execute(
        """
        INSERT INTO classifications(
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("02", "경영·회계·사무", "02", "총무·인사", "02", "인사·조직", "01", "인사"),
    )
    classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
        "classification_id"
    ]
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO competency_units(
            unit_code, base_unit_code, unit_version, unit_name_raw,
            unit_level_raw, classification_id, api_definition,
            api_match_status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "0202020101_23v3",
            "0202020101",
            "23v3",
            "인사기획",
            "6",
            classification_id,
            "인사전략과 운영 계획을 수립하는 능력이다.",
            "matched",
            timestamp,
            timestamp,
        ),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
        ) VALUES (?, ?, ?, ?, ?)
        """,
        ("0202020101_23v3", "1", "0202020101_23v3 1", "인사전략 수립하기", "6"),
    )
    element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
        "element_id"
    ]
    conn.execute(
        """
        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
        VALUES (?, ?, ?)
        """,
        (element_id, "1", "인사전략  환경을 분석할 수 있다"),
    )
    criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
        "criteria_id"
    ]
    conn.commit()
    return criteria_id


class RefinementTests(unittest.TestCase):
    def test_safe_typo_refinement_handles_narrow_patterns(self) -> None:
        self.assertEqual(
            safe_typo_refinement("전직지원자가가 추구하는 요구내용"),
            "전직지원자가 추구하는 요구내용",
        )
        self.assertEqual(safe_typo_refinement("조진문화 진단하기"), "조직문화 진단하기")
        self.assertEqual(
            safe_typo_refinement("자격요견을 설정할 수 있다."),
            "자격요건을 설정할 수 있다.",
        )
        self.assertIsNone(safe_typo_refinement("자가가구 조사표에 준하여 조사하는 능력"))

    def test_generate_and_apply_refinement_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            criteria_id = seed_unit_with_criteria(conn)
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=criteria_id,
                issue_type="double_space",
                severity="info",
                issue_detail="연속 공백 포함",
                suggested_action="공백 정규화",
            )
            conn.commit()

            generated = create_refinement_jobs(
                conn,
                issue_types=["double_space"],
                target_types=["criteria"],
                limit=10,
            )
            self.assertEqual(generated["jobs_created"], 1)
            second = create_refinement_jobs(
                conn,
                issue_types=["double_space"],
                target_types=["criteria"],
                limit=10,
            )
            self.assertEqual(second["jobs_created"], 0)
            self.assertEqual(second["issues_seen"], 0)
            self.assertEqual(second["jobs_skipped_existing"], 0)

            applied = apply_refinement_jobs(conn, limit=10, min_confidence=0.95)
            self.assertEqual(applied["jobs_applied"], 1)
            row = conn.execute(
                "SELECT criteria_text_refined, review_status FROM performance_criteria WHERE criteria_id = ?",
                (criteria_id,),
            ).fetchone()
            self.assertEqual(row["criteria_text_refined"], "인사전략 환경을 분석할 수 있다")
            self.assertEqual(row["review_status"], "model_refined")
            issue = conn.execute("SELECT resolved_at FROM quality_issues").fetchone()
            self.assertIsNotNone(issue["resolved_at"])
            stats = refinement_stats(conn)
            self.assertEqual(stats["refinement_jobs"]["applied"], 1)
            self.assertEqual(stats["refined_targets"]["criteria_refined"], 1)
            conn.close()

    def test_apply_refinement_jobs_filters_by_issue_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            first_criteria_id = seed_unit_with_criteria(conn)
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                "element_id"
            ]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, ?, ?)
                """,
                (element_id, "2", "other  text"),
            )
            second_criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria WHERE criteria_no = '2'"
            ).fetchone()["criteria_id"]
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=first_criteria_id,
                issue_type="wanted_issue",
                severity="warning",
                issue_detail="wanted",
            )
            wanted_issue_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=second_criteria_id,
                issue_type="other_issue",
                severity="warning",
                issue_detail="other",
            )
            other_issue_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            for target_id, issue_id, raw_text, refined_text in (
                (first_criteria_id, wanted_issue_id, "wanted  text", "wanted text"),
                (second_criteria_id, other_issue_id, "other  text", "other text"),
            ):
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
                        str(target_id),
                        issue_id,
                        "test",
                        "test",
                        f"hash-{issue_id}",
                        raw_text,
                        refined_text,
                        "test",
                        0.99,
                        json.dumps({"result": {"action": "refine"}}),
                        "review_required",
                        now_utc(),
                    ),
                )
            conn.commit()

            applied = apply_refinement_jobs(
                conn,
                issue_types=["wanted_issue"],
                target_types=["criteria"],
                limit=10,
                min_confidence=0.95,
            )

            self.assertEqual(applied["jobs_seen"], 1)
            self.assertEqual(applied["jobs_applied"], 1)
            first_row = conn.execute(
                "SELECT criteria_text_refined FROM performance_criteria WHERE criteria_id = ?",
                (first_criteria_id,),
            ).fetchone()
            second_row = conn.execute(
                "SELECT criteria_text_refined FROM performance_criteria WHERE criteria_id = ?",
                (second_criteria_id,),
            ).fetchone()
            self.assertEqual(first_row["criteria_text_refined"], "wanted text")
            self.assertIsNone(second_row["criteria_text_refined"])
            issue_rows = {
                row["issue_type"]: row["resolved_at"]
                for row in conn.execute("SELECT issue_type, resolved_at FROM quality_issues")
            }
            self.assertIsNotNone(issue_rows["wanted_issue"])
            self.assertIsNone(issue_rows["other_issue"])
            conn.close()

    def test_short_ksa_is_not_auto_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            criteria_id = seed_unit_with_criteria(conn)
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                "element_id"
            ]
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, ?, ?, ?, ?)
                """,
                (element_id, "01", "지식", "1", "인사"),
            )
            ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
            insert_quality_issue(
                conn,
                target_type="ksa",
                target_id=ksa_id,
                issue_type="short_ksa",
                severity="info",
                issue_detail="짧은 KSA",
                suggested_action="사람 검토",
            )
            conn.commit()

            generated = create_refinement_jobs(
                conn,
                issue_types=["short_ksa"],
                target_types=["ksa"],
                limit=10,
            )
            self.assertEqual(generated["jobs_created"], 1)
            applied = apply_refinement_jobs(conn, limit=10, min_confidence=0.0)
            self.assertEqual(applied["jobs_applied"], 0)
            row = conn.execute("SELECT ksa_text_refined FROM ksa_items WHERE ksa_id = ?", (ksa_id,)).fetchone()
            self.assertIsNone(row["ksa_text_refined"])
            self.assertIsNotNone(criteria_id)
            conn.close()

    def test_unsafe_criteria_refinement_is_skipped(self) -> None:
        self.assertEqual(
            unsafe_refinement_reason(
                target_type="criteria",
                raw_text="IT프로젝트 운영방식을 결정할 수 있",
                refined_text="IT프로젝트 운영방식을 결정할 수 있.",
            ),
            "criteria_sentence_ends_with_incomplete_can_do_phrase",
        )
        self.assertEqual(
            unsafe_refinement_reason(
                target_type="criteria",
                raw_text="가식장의 면적을 산출할 수  다.",
                refined_text="가식장의 면적을 산출할 수 다.",
            ),
            "criteria_sentence_ends_with_incomplete_can_do_phrase",
        )
        self.assertIsNone(
            unsafe_refinement_reason(
                target_type="criteria",
                raw_text="DAW 소프트웨어의 세부 메뉴와 기능을 이해하고 설명할 수  있다.",
                refined_text="DAW 소프트웨어의 세부 메뉴와 기능을 이해하고 설명할 수 있다.",
            )
        )

    def test_generation_batches_skip_active_jobs_and_continue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            first_criteria_id = seed_unit_with_criteria(conn)
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                "element_id"
            ]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, ?, ?)
                """,
                (element_id, "2", "인사운영  계획을 수립할 수 있다"),
            )
            second_criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria WHERE criteria_no = ?",
                ("2",),
            ).fetchone()["criteria_id"]
            for criteria_id in (first_criteria_id, second_criteria_id):
                insert_quality_issue(
                    conn,
                    target_type="criteria",
                    target_id=criteria_id,
                    issue_type="double_space",
                    severity="info",
                    issue_detail="연속 공백 포함",
                    suggested_action="공백 정규화",
                )
            conn.commit()

            first_batch = create_refinement_jobs(
                conn,
                issue_types=["double_space"],
                target_types=["criteria"],
                limit=1,
            )
            second_batch = create_refinement_jobs(
                conn,
                issue_types=["double_space"],
                target_types=["criteria"],
                limit=1,
            )
            third_batch = create_refinement_jobs(
                conn,
                issue_types=["double_space"],
                target_types=["criteria"],
                limit=1,
            )

            self.assertEqual(first_batch["jobs_created"], 1)
            self.assertEqual(second_batch["jobs_created"], 1)
            self.assertEqual(third_batch["issues_seen"], 0)
            job_count = conn.execute("SELECT COUNT(*) AS count FROM refinement_jobs").fetchone()[
                "count"
            ]
            self.assertEqual(job_count, 2)
            conn.close()

    def test_jsonl_export_import_creates_review_job_without_applying(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            criteria_id = seed_unit_with_criteria(conn)
            insert_quality_issue(
                conn,
                target_type="criteria",
                target_id=criteria_id,
                issue_type="criteria_format_issue",
                severity="info",
                issue_detail="문장부호 확인",
                suggested_action="LLM 검토",
            )
            conn.commit()

            export_path = tmp_path / "export.jsonl"
            exported = export_refinement_jsonl(
                conn,
                out_path=export_path,
                issue_types=["criteria_format_issue"],
                target_types=["criteria"],
                limit=10,
            )
            self.assertEqual(exported["records_written"], 1)
            exported_payload = json.loads(export_path.read_text(encoding="utf-8").splitlines()[0])

            import_path = tmp_path / "results.jsonl"
            import_payload = {
                **exported_payload,
                "action": "refine",
                "refined_text": "인사전략 환경을 분석할 수 있다.",
                "rationale": "문장부호를 보완했다.",
                "confidence": 0.8,
            }
            import_path.write_text(json.dumps(import_payload, ensure_ascii=False) + "\n", encoding="utf-8")

            imported = import_refinement_jsonl(conn, input_path=import_path)
            self.assertEqual(imported["jobs_imported"], 1)
            job = conn.execute("SELECT review_status, refined_text FROM refinement_jobs").fetchone()
            self.assertEqual(job["review_status"], "review_required")
            self.assertEqual(job["refined_text"], "인사전략 환경을 분석할 수 있다.")
            criteria = conn.execute(
                "SELECT criteria_text_refined FROM performance_criteria WHERE criteria_id = ?",
                (criteria_id,),
            ).fetchone()
            self.assertIsNone(criteria["criteria_text_refined"])
            conn.close()


if __name__ == "__main__":
    unittest.main()
