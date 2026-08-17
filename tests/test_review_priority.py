from __future__ import annotations

from collections import Counter
import csv
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.db import connect, initialize_database, now_utc
from ncs_mcp.review_priority import (
    KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES,
    audit_ksa_term_minimal_review_decision_csv,
    build_ksa_definition_candidate_family_report,
    build_ksa_review_minimization_audit,
    build_ksa_term_review_readiness_report,
    build_ksa_term_minimal_review_decision_action_plan,
    build_ksa_term_minimal_review_slice,
    build_ksa_term_ontology_impact_report,
    build_ksa_term_preprocessing_review_pack,
    review_priority_summary,
    write_ksa_term_review_readiness_markdown,
    write_ksa_definition_candidate_family_report_csv,
    write_ksa_definition_candidate_family_report_markdown,
    write_ksa_review_minimization_audit_markdown,
    write_ksa_term_minimal_review_decision_action_plan_markdown,
    write_ksa_term_minimal_review_decision_audit_markdown,
    write_ksa_term_minimal_review_slice_jsonl,
    write_ksa_term_minimal_review_slice_csv,
    write_ksa_term_minimal_review_slice_markdown,
    write_ksa_term_ontology_impact_report_markdown,
    write_ksa_term_preprocessing_review_pack_markdown,
    write_review_priority_markdown,
)


class ReviewPriorityTests(unittest.TestCase):
    def test_ksa_definition_candidate_family_report_groups_llm_term_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', '경영·회계·사무', '02', '총무·인사', '02', '인사', '01', '인사기획')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', '인사전략 수립',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw,
                        element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', '인사환경 분석', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                candidates = [
                    (
                        "knowledge",
                        "전략적 인적자원관리",
                        "전략적 인적자원관리: 전략적 인적자원관리의 목적, 범위, 실행 조건을 이해하여 과업 방향을 정하는 지식.",
                        "candidate",
                    ),
                    (
                        "knowledge",
                        "인사정책",
                        "인사정책: 인사정책의 목적, 범위, 실행 조건을 이해하여 과업 방향을 정하는 지식.",
                        "candidate",
                    ),
                    (
                        "knowledge",
                        "인사전략 환경분석법",
                        "인사전략 환경분석법: 인사전략 환경분석법에 필요한 자료와 판단 기준을 해석하여 과업 의사결정에 활용하는 지식.",
                        "candidate",
                    ),
                    (
                        "attitude",
                        "문장형 태도",
                        "문장형 태도.: 문장형 태도.를 기준으로 업무 품질, 협업, 책임 있는 실행을 유지하려는 태도.",
                        "candidate",
                    ),
                ]
                for index, (concept_type, concept_name, meaning_text, review_status) in enumerate(
                    candidates,
                    start=1,
                ):
                    conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, relation_status, review_status,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'missing', 'unlinked', 'model_preprocessed', ?, ?)
                        """,
                        (concept_name, f"concept{index}", concept_type, timestamp, timestamp),
                    )
                    concept_id = conn.execute(
                        "SELECT concept_id FROM ontology_concepts WHERE normalized_key = ?",
                        (f"concept{index}",),
                    ).fetchone()["concept_id"]
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            element_id,
                            concept_type[:1].upper(),
                            concept_type,
                            str(index),
                            concept_name,
                        ),
                    )
                    ksa_id = conn.execute(
                        "SELECT ksa_id FROM ksa_items WHERE ksa_no = ?",
                        (str(index),),
                    ).fetchone()["ksa_id"]
                    conn.execute(
                        """
                        INSERT INTO ksa_meaning_candidates(
                            concept_id, concept_type, meaning_role, meaning_text,
                            source_method, evidence_text, unit_code, element_id,
                            ksa_id, confidence_score, review_status, created_at, updated_at
                        ) VALUES (?, ?, 'term_definition_candidate', ?,
                                  'term_definition_template', ?, '0202020101_23v3',
                                  ?, ?, 0.74, ?, ?, ?)
                        """,
                        (
                            concept_id,
                            concept_type,
                            meaning_text,
                            f"unit: 인사전략 수립 | KSA: {concept_name}",
                            element_id,
                            ksa_id,
                            review_status,
                            timestamp,
                            timestamp,
                        ),
                    )
                conn.commit()

                report = build_ksa_definition_candidate_family_report(
                    conn,
                    limit=10,
                    sample_limit=2,
                )
                report["top_families"][0]["family_label"] = "=definition family"
                report["top_families"][0]["samples"] = [
                    {"meaning_text": " @definition sample"}
                ]
                markdown_path = Path(tmp) / "definition_family.md"
                csv_path = Path(tmp) / "definition_family.csv"
                write_ksa_definition_candidate_family_report_markdown(report, markdown_path)
                csv_summary = write_ksa_definition_candidate_family_report_csv(
                    report,
                    csv_path,
                )
            finally:
                conn.close()

            self.assertTrue(report["ok"])
            self.assertEqual(report["schema"], "ncs_ksa_definition_candidate_family_report_v1")
            self.assertEqual(report["candidate_count"], 4)
            self.assertEqual(report["review_status_counts"], {"candidate": 4})
            self.assertEqual(report["concept_type_counts"]["knowledge"], 3)
            family_by_key = {family["family_key"]: family for family in report["top_families"]}
            self.assertEqual(family_by_key["knowledge_purpose_scope"]["candidate_count"], 2)
            self.assertEqual(
                family_by_key["knowledge_analysis_context"]["recommended_review_level"],
                "low_volume_family_spotcheck",
            )
            self.assertIn("sentence_punctuation_before_particle", report["risk_flag_counts"])
            self.assertFalse(report["safety"]["status_update_allowed"])
            self.assertFalse(report["safety"]["db_writes"])
            self.assertFalse(report["safety"]["approval_claim"])
            self.assertFalse(report["status_update_allowed"])
            self.assertFalse(report["db_writes"])
            self.assertFalse(report["approval_claim"])
            self.assertIn("Do not click every term-definition candidate row", markdown_path.read_text(encoding="utf-8"))
            self.assertEqual(
                csv_summary["schema"],
                "ncs_ksa_definition_candidate_family_decision_sheet_v1",
            )
            self.assertFalse(csv_summary["status_update_allowed"])
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), len(report["top_families"]))
            self.assertEqual(rows[0]["decision"], "")
            self.assertEqual(rows[0]["reviewer_id"], "")
            self.assertEqual(rows[0]["operator_review_scope"], "family_sample_plus_risk_samples")
            self.assertEqual(rows[0]["family_label"], "'=definition family")
            self.assertEqual(rows[0]["sample_meaning_text"], "' @definition sample")
            self.assertEqual(rows[0]["status_update_allowed"], "False")
            self.assertEqual(rows[0]["db_writes"], "False")
            self.assertEqual(rows[0]["approval_claim"], "False")

    def test_review_priority_orders_high_impact_issues_and_adds_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', '.')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                    "criteria_id"
                ]
                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition_status,
                        relation_status, review_status, created_at, updated_at
                    ) VALUES ('workforce planning', 'workforceplanning', 'knowledge',
                              'missing', 'unlinked', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()[
                    "concept_id"
                ]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('criteria', ?, 'criteria_format_issue', 'warning',
                              'Malformed criteria text', 'Review refined criteria', ?)
                    """,
                    (str(criteria_id), timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ontology_concept', ?, 'hr_core_concept_human_review_required',
                              'high', 'Core concept needs review', 'Define concept', ?)
                    """,
                    (str(concept_id), timestamp),
                )
                conn.commit()

                result = review_priority_summary(conn, limit=5, per_issue_type_limit=2)
            finally:
                conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["schema"], "ncs_review_priority_v1")
            self.assertEqual(
                result["top_items"][0]["issue"]["issue_type"],
                "hr_core_concept_human_review_required",
            )
            self.assertEqual(result["top_items"][0]["context"]["concept_name"], "workforce planning")
            self.assertIn("criteria_format_issue", result["groups"])
            self.assertEqual(
                result["groups"]["criteria_format_issue"][0]["context"]["criteria_text_raw"],
                ".",
            )
            focus = {item["code"]: item for item in result["focus_overlays"]}
            self.assertIn("aihr_demo_major_02", focus)
            self.assertGreaterEqual(focus["aihr_demo_major_02"]["item_count"], 1)
            self.assertEqual(
                focus["aihr_demo_major_02"]["top_items"][0]["issue"]["issue_type"],
                "hr_core_concept_human_review_required",
            )

    def test_review_priority_reclassifies_api_element_failures_for_display(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                cur = conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('14', 'Construction', '14', 'Construction', '14', 'Construction', '01', 'Construction planning')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('1401010101_25v1', '1401010101', '25v1', 'Construction planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw, api_match_status
                    ) VALUES ('1401010101_25v1', '1', 'E1', 'Plan construction work', '5', 'api_failed')
                    """
                )
                element_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('element', ?, 'api_element_unmatched', 'warning',
                              'NCS006 request failed after retries.', 'Retry later', ?)
                    """,
                    (str(element_id), timestamp),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["api_element_unmatched", "api_element_collection_failure"],
                    limit=5,
                    per_issue_type_limit=5,
                )
            finally:
                conn.close()

        self.assertEqual(result["top_items"][0]["issue"]["issue_type"], "api_element_collection_failure")
        self.assertEqual(result["top_items"][0]["issue"]["source_issue_type"], "api_element_unmatched")
        self.assertIn("api_element_collection_failure", result["groups"])
        self.assertEqual(result["open_issue_counts"][0]["issue_type"], "api_element_collection_failure")

    def test_review_priority_truncates_long_operator_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute(
                    "SELECT classification_id FROM classifications"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()[
                    "element_id"
                ]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', ?)
                    """,
                    (element_id, "x" * 1500),
                )
                criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                    "criteria_id"
                ]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('criteria', ?, 'criteria_format_issue', 'warning',
                              ?, 'Review refined criteria', ?)
                    """,
                    (str(criteria_id), "y" * 1500, timestamp),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["criteria_format_issue"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        item = result["top_items"][0]
        self.assertLess(len(item["issue"]["issue_detail"]), 950)
        self.assertIn("issue_detail", item["issue"]["_truncated_fields"])
        self.assertLess(len(item["context"]["criteria_text_raw"]), 950)
        self.assertIn("criteria_text_raw", item["context"]["_truncated_fields"])

    def test_review_priority_task_ksa_issues_keep_fallback_context_when_relation_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('task_ksa_concept_relation', '987',
                              'ontology_task_ksa_relation_human_review_required', 'high',
                              'Task relation needs a structured context snapshot',
                              'Review the task/KSA relationship', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["ontology_task_ksa_relation_human_review_required"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        item = result["top_items"][0]
        self.assertEqual(
            item["issue"]["issue_type"],
            "ontology_task_ksa_relation_human_review_required",
        )
        self.assertEqual(item["context"]["issue_type"], "ontology_task_ksa_relation_human_review_required")
        self.assertEqual(item["context"]["target_type"], "task_ksa_concept_relation")
        self.assertEqual(item["context"]["target_id"], "987")
        self.assertEqual(
            item["context"]["issue_detail"],
            "Task relation needs a structured context snapshot",
        )
        self.assertIn(
            "Human reviewer should inspect whether the task-KSA relation is supported by evidence",
            item["issue"]["suggested_action"],
        )
        self.assertEqual(item["context"]["suggested_action"], item["issue"]["suggested_action"])
        self.assertEqual(item["context"]["context_source"], "quality_issue_fallback")
        self.assertIn("Task relation needs a structured context snapshot", item["context"]["issue_detail"])

    def test_review_priority_neutralizes_trusted_status_suggested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ontology_concept', '42',
                              'ontology_core_concept_human_review_required', 'high',
                              'Core concept needs evidence review',
                              'Mark human_reviewed if valid, or rejected if noisy.', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["ontology_core_concept_human_review_required"],
                    limit=1,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        action = result["top_items"][0]["issue"]["suggested_action"]
        self.assertIn("controlled review workflow", action)
        self.assertNotIn("Mark human_reviewed", action)
        self.assertNotIn("rejected if noisy", action)

    def test_review_priority_top_items_respect_per_issue_type_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for issue_type in ("criteria_format_issue", "suspected_typo"):
                    for index in range(3):
                        conn.execute(
                            """
                            INSERT INTO quality_issues(
                                target_type, target_id, issue_type, severity,
                                issue_detail, suggested_action, detected_at
                            ) VALUES ('unit', ?, ?, 'warning', 'Needs review', 'Review', ?)
                            """,
                            (f"U-{issue_type}-{index}", issue_type, timestamp),
                        )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["criteria_format_issue", "suspected_typo"],
                    limit=10,
                    per_issue_type_limit=1,
                )
            finally:
                conn.close()

        counts = Counter(item["issue"]["issue_type"] for item in result["top_items"])
        self.assertEqual(counts["criteria_format_issue"], 1)
        self.assertEqual(counts["suspected_typo"], 1)
        self.assertEqual(len(result["top_items"]), 2)

    def test_review_priority_top_items_deduplicate_same_issue_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                for detail in ("Level mismatch", "Name mismatch"):
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('unit', 'U-1', 'api_value_mismatch', 'warning', ?, 'Review', ?)
                        """,
                        (detail, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('unit', 'U-2', 'api_value_mismatch', 'warning', 'Other', 'Review', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                result = review_priority_summary(
                    conn,
                    issue_types=["api_value_mismatch"],
                    limit=10,
                    per_issue_type_limit=10,
                )
            finally:
                conn.close()

        targets = [item["issue"]["target_id"] for item in result["top_items"]]
        self.assertEqual(targets, ["U-1", "U-2"])
        self.assertEqual(result["duplicate_target_items_skipped"], 1)

    def test_ksa_term_preprocessing_preset_is_explicit_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ksa', '1', 'short_ksa', 'info', 'Short KSA', 'Review KSA', ?)
                    """,
                    (timestamp,),
                )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ksa', '2', 'duplicate_text', 'info', 'Duplicate KSA', 'Review KSA', ?)
                    """,
                    (timestamp,),
                )
                conn.commit()

                default_result = review_priority_summary(conn, limit=10, per_issue_type_limit=5)
                preset_result = review_priority_summary(
                    conn,
                    issue_types=KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES,
                    limit=10,
                    per_issue_type_limit=5,
                )
            finally:
                conn.close()

        self.assertEqual(default_result["top_items"], [])
        self.assertEqual(
            preset_result["issue_types"],
            ["short_ksa", "duplicate_text"],
        )
        issue_types = {item["issue"]["issue_type"] for item in preset_result["top_items"]}
        self.assertEqual(issue_types, {"short_ksa", "duplicate_text"})
        reasons = {item["issue"]["issue_type"]: item["priority_reason"] for item in preset_result["top_items"]}
        self.assertIn("Short KSA text", reasons["short_ksa"])
        self.assertIn("Repeated KSA text", reasons["duplicate_text"])

    def test_grouped_ksa_term_preprocessing_review_pack_collapses_repeated_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                ksa_ids = []
                for index, (element_name, ksa_text, issue_type) in enumerate(
                    [
                        ("Plan workforce", "Common analysis", "duplicate_text"),
                        ("Review workforce", "Common  analysis", "duplicate_text"),
                        ("Define metric", "Common analysis", "short_ksa"),
                    ],
                    start=1,
                ):
                    conn.execute(
                        """
                        INSERT INTO competency_elements(
                            unit_code, element_no, element_code_raw,
                            element_name_raw, element_level_raw
                        ) VALUES ('0202020101_23v3', ?, ?, ?, '5')
                        """,
                        (str(index), f"E{index}", element_name),
                    )
                    element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, ?)
                        """,
                        (element_id, str(index), ksa_text),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    ksa_ids.append(ksa_id)
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, ?, 'info', 'fixture issue',
                                  'Mark human_reviewed if valid', ?)
                        """,
                        (str(ksa_id), issue_type, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at, resolved_at
                    ) VALUES ('ksa', ?, 'short_ksa', 'info', 'resolved fixture issue',
                              'Mark accepted if valid', ?, ?)
                    """,
                    (str(ksa_ids[0]), timestamp, timestamp),
                )
                conn.commit()

                report = build_ksa_term_preprocessing_review_pack(
                    conn,
                    limit=10,
                    sample_limit=2,
                )
                with tempfile.TemporaryDirectory() as report_tmp:
                    markdown_path = Path(report_tmp) / "ksa-term-pack.md"
                    write_ksa_term_preprocessing_review_pack_markdown(report, markdown_path)
                    markdown = markdown_path.read_text(encoding="utf-8")
            finally:
                conn.close()

        self.assertEqual(report["schema"], "ncs_ksa_term_preprocessing_review_pack_v1")
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertTrue(report["human_decision_required"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["total_open_issue_count"], 3)
        self.assertEqual(report["represented_issue_count"], 3)
        self.assertEqual(report["represented_ksa_count"], 3)
        self.assertEqual(report["represented_issue_reduction"], 2)
        self.assertEqual(report["review_bucket_counts"], {"term_quality_and_genericity_review": 1})
        group = report["groups"][0]
        self.assertEqual(group["normalized_ksa_term"], "commonanalysis")
        self.assertEqual(group["issue_count"], 3)
        self.assertEqual(group["ksa_count"], 3)
        self.assertEqual(group["issue_type_counts"], {"duplicate_text": 2, "short_ksa": 1})
        self.assertEqual(group["ksa_type_counts"], {"knowledge": 3})
        self.assertEqual(group["raw_ksa_text_variant_count"], 2)
        self.assertEqual(group["review_bucket"], "term_quality_and_genericity_review")
        self.assertIn("has_duplicate_text_issue", group["review_flags"])
        self.assertIn("has_short_ksa_issue", group["review_flags"])
        self.assertIn("inspect_term_meaning_with_samples", group["operator_decision_options"])
        self.assertFalse(group["auto_apply_allowed"])
        self.assertEqual(group["recommended_review_action"], "inspect_term_quality_and_genericity")
        self.assertEqual(len(group["samples"]), 2)
        self.assertFalse(group["status_update_allowed"])
        self.assertFalse(group["db_writes"])
        self.assertFalse(group["approval_claim"])
        self.assertEqual(group["decision_fields"]["decision"], "")
        self.assertEqual(group["decision_fields"]["reviewer_id"], "")
        self.assertNotIn("human_reviewed", group["samples"][0]["suggested_action"])
        self.assertNotIn("accepted", group["samples"][0]["suggested_action"])
        self.assertIn("KSA Term Preprocessing Review Pack", markdown)
        self.assertIn("issue_type_counts", markdown)
        self.assertIn("review_bucket", markdown)

    def test_grouped_ksa_term_preprocessing_review_pack_rejects_unknown_issue_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                with self.assertRaisesRegex(
                    ValueError,
                    "Unsupported KSA term preprocessing issue type",
                ):
                    build_ksa_term_preprocessing_review_pack(
                        conn,
                        issue_types=["not_a_real_issue"],
                    )
            finally:
                conn.close()

    def test_grouped_ksa_term_preprocessing_review_pack_refreshes_temp_snapshot_per_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                def insert_flagged_ksa(ksa_no: str) -> None:
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, 'Refresh term')
                        """,
                        (element_id, ksa_no),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                                  'Review KSA', ?)
                        """,
                        (str(ksa_id), timestamp),
                    )

                insert_flagged_ksa("1")
                conn.commit()
                first = build_ksa_term_preprocessing_review_pack(
                    conn,
                    limit=10,
                    issue_types=["duplicate_text"],
                )
                insert_flagged_ksa("2")
                conn.commit()
                second = build_ksa_term_preprocessing_review_pack(
                    conn,
                    limit=10,
                    issue_types=["duplicate_text"],
                )
            finally:
                conn.close()

        self.assertEqual(first["represented_issue_count"], 1)
        self.assertEqual(second["represented_issue_count"], 2)
        self.assertEqual(second["groups"][0]["issue_count"], 2)

    def test_ksa_term_ontology_impact_report_summarizes_linked_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', 'Analyze workforce data')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                ksa_ids = []
                for index, issue_type in enumerate(["duplicate_text", "short_ksa"], start=1):
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, 'Common analysis')
                        """,
                        (element_id, str(index)),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    ksa_ids.append(ksa_id)
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, ?, 'info', 'fixture issue',
                                  'Mark human_reviewed if valid', ?)
                        """,
                        (str(ksa_id), issue_type, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, review_status, created_at, updated_at
                    ) VALUES ('Common analysis', 'commonanalysis', 'knowledge',
                              'missing', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, review_status, created_at, updated_at
                    ) VALUES ('Unflagged common analysis', 'unflaggedcommonanalysis', 'knowledge',
                              'missing', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                unflagged_concept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, '01', 'knowledge', '99', 'Common analysis')
                    """,
                    (element_id,),
                )
                unflagged_ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                    (unflagged_ksa_id, unflagged_concept_id, timestamp),
                )
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                    (ksa_ids[0], concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index,
                        atom_text, normalized_key, created_at
                    ) VALUES (?, ?, 'knowledge', 1, 'Common analysis', 'commonanalysis', ?)
                    """,
                    (ksa_ids[1], element_id, timestamp),
                )
                atomic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, created_at) VALUES (?, ?, ?)",
                    (atomic_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO criteria_concept_links(criteria_id, concept_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (criteria_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, created_at
                    ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                              'fixture relation', ?)
                    """,
                    (criteria_id, element_id, concept_id, concept_id, atomic_id, atomic_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ncs_training_courses(
                        ncs_cl_cd, compe_unit_name, train_goal, train_time,
                        fac_name, meth_name, api_fetched_at
                    ) VALUES ('0202020101', 'HR planning', 'Analyze workforce data',
                              '8', 'classroom', 'lecture', ?)
                    """,
                    (timestamp,),
                )
                training_course_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_training_course_concept_links(
                        training_course_id, unit_code, concept_id, link_method,
                        confidence_score, created_at, updated_at
                    ) VALUES (?, '0202020101_23v3', ?, 'fixture', 0.9, ?, ?)
                    """,
                    (training_course_id, concept_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO training_goal_concept_links(
                        training_course_id, unit_code, element_id, concept_id,
                        link_method, confidence_score, created_at, updated_at
                    ) VALUES (?, '0202020101_23v3', ?, ?, 'fixture', 0.9, ?, ?)
                    """,
                    (training_course_id, element_id, concept_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ncs_job_base_competencies(
                        competency_name, normalized_key, created_at, updated_at
                    ) VALUES ('Information', 'information', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                job_base_competency_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_job_base_factors(
                        job_base_competency_id, factor_name, normalized_key,
                        created_at, updated_at
                    ) VALUES (?, 'Data processing', 'dataprocessing', ?, ?)
                    """,
                    (job_base_competency_id, timestamp, timestamp),
                )
                job_base_factor_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_unit_job_base_links(
                        unit_code, job_base_competency_id, job_base_factor_id,
                        compe_unit_name, source_payload, api_fetched_at,
                        created_at, updated_at
                    ) VALUES ('0202020101_23v3', ?, ?, 'HR planning', '{}', ?, ?, ?)
                    """,
                    (job_base_competency_id, job_base_factor_id, timestamp, timestamp, timestamp),
                )
                conn.commit()

                transition_quality_report = {
                    "schema": "ncs_quality_gates_v1",
                    "evidence": {
                        "transition_evaluation": {
                            "scenario_count": 1,
                            "cases": [
                                {
                                    "ok": True,
                                    "recommended_course_evidence": [
                                        {
                                            "course_name": "HR planning",
                                            "quality_issue_penalty": {
                                                "applied": True,
                                                "issue_types": ["short_ksa"],
                                                "affected_concepts": [
                                                    {
                                                        "concept_id": concept_id,
                                                        "concept_name": "Common analysis",
                                                        "concept_type": "knowledge",
                                                        "issue_types": ["short_ksa"],
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    },
                }
                report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=5,
                    concept_limit_per_group=3,
                    sample_limit=1,
                    transition_quality_report=transition_quality_report,
                    transition_quality_report_path="reports/quality_gates.json",
                )
                with tempfile.TemporaryDirectory() as report_tmp:
                    markdown_path = Path(report_tmp) / "impact.md"
                    write_ksa_term_ontology_impact_report_markdown(report, markdown_path)
                    markdown = markdown_path.read_text(encoding="utf-8")
            finally:
                conn.close()

        self.assertEqual(report["schema"], "ncs_ksa_term_ontology_impact_report_v1")
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertTrue(report["transition_quality_report_available"])
        self.assertEqual(report["transition_quality_report_path"], "reports/quality_gates.json")
        self.assertEqual(report["recommendation_penalty_group_count"], 1)
        self.assertEqual(report["recommendation_penalty_concept_count"], 1)
        self.assertEqual(report["source_transition_penalty_concept_count"], 1)
        self.assertEqual(report["represented_recommendation_penalty_concept_count"], 1)
        self.assertEqual(report["unrepresented_recommendation_penalty_concept_count"], 0)
        self.assertEqual(report["recommendation_penalty_course_count"], 1)
        self.assertEqual(report["source_transition_penalized_recommendation_row_count"], 1)
        self.assertEqual(report["source_transition_distinct_penalized_course_count"], 1)
        self.assertEqual(report["recommendation_penalty_issue_counts"], {"short_ksa": 1})
        self.assertEqual(report["source_transition_penalty_issue_counts"], {"short_ksa": 1})
        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["impacted_group_count"], 1)
        self.assertEqual(report["total_unique_impacted_concept_count"], 1)
        self.assertEqual(report["job_base_auxiliary_group_count"], 1)
        self.assertEqual(report["job_base_auxiliary_concept_count"], 1)
        self.assertEqual(report["job_base_auxiliary_signal_role"], "supporting_gap_context_not_primary_evidence")
        group = report["groups"][0]
        self.assertEqual(group["normalized_ksa_term"], "commonanalysis")
        self.assertEqual(group["linked_concept_count"], 1)
        group_job_base = group["job_base_auxiliary_signal"]
        self.assertEqual(group_job_base["evidence_role"], "supporting_gap_context_not_primary_evidence")
        self.assertEqual(group_job_base["scoring_role"], "review_priority_context_only")
        self.assertEqual(group_job_base["competency_names"], ["Information"])
        self.assertEqual(group_job_base["factor_labels"], ["Information:Data processing"])
        self.assertFalse(group_job_base["status_update_allowed"])
        self.assertFalse(group_job_base["db_writes"])
        self.assertNotIn(
            unflagged_concept_id,
            [concept["concept_id"] for concept in group["top_concepts"]],
        )
        self.assertEqual(group["recommendation_penalty"]["concept_count"], 1)
        self.assertEqual(group["recommendation_penalty"]["course_count"], 1)
        self.assertEqual(group["recommendation_penalty"]["course_names"], ["HR planning"])
        self.assertEqual(group["linked_penalized_concepts"]["concept_count"], 1)
        self.assertEqual(group["linked_penalized_concepts"]["distinct_course_count"], 1)
        self.assertIn("not proof", group["linked_penalized_concepts"]["scope_note"])
        self.assertGreaterEqual(group["minimal_review_priority_score"], 80)
        self.assertEqual(group["minimal_review_priority_level"], "critical_minimal_review")
        self.assertIn("linked_transition_penalty_concepts", group["minimal_review_priority_reasons"])
        self.assertEqual(
            group["minimal_review_operator_action"],
            "inspect_linked_penalized_concepts_before_any_scoring_decision",
        )
        self.assertIn("not approval evidence", group["minimal_review_scope_note"])
        self.assertEqual(group["group_task_relation_count"], 1)
        self.assertEqual(group["group_training_course_link_count"], 1)
        self.assertEqual(group["group_training_goal_link_count"], 1)
        self.assertEqual(group["group_training_link_count"], 1)
        self.assertEqual(group["operator_impact_action"], "inspect_linked_concepts_before_ontology_cleanup")
        self.assertFalse(group["auto_apply_allowed"])
        concept = group["top_concepts"][0]
        self.assertEqual(concept["concept_id"], concept_id)
        self.assertEqual(concept["recommendation_penalty_course_count"], 1)
        self.assertEqual(concept["recommendation_penalty_issue_counts"], {"short_ksa": 1})
        self.assertEqual(concept["recommendation_penalty_course_names"], ["HR planning"])
        self.assertEqual(concept["linked_ksa_count"], 2)
        self.assertEqual(concept["direct_linked_ksa_count"], 1)
        self.assertEqual(concept["atomic_linked_ksa_count"], 1)
        self.assertEqual(concept["task_relation_count"], 1)
        self.assertEqual(concept["criteria_link_count"], 1)
        self.assertEqual(concept["training_course_link_count"], 1)
        self.assertEqual(concept["training_goal_link_count"], 1)
        self.assertEqual(
            concept["job_base_auxiliary_signal"]["factor_labels"],
            ["Information:Data processing"],
        )
        self.assertEqual(
            concept["job_base_auxiliary_signal"]["operator_action"],
            "use_only_as_supporting_transition_gap_context_not_primary_ksa_evidence",
        )
        self.assertEqual(report["total_task_relation_count"], 1)
        self.assertEqual(report["total_criteria_link_count"], 1)
        self.assertEqual(report["total_training_goal_link_count"], 1)
        minimal_slice = build_ksa_term_minimal_review_slice(report, limit=5)
        self.assertEqual(
            minimal_slice["concept_review_groups"][0]["job_base_auxiliary_signal"]["factor_labels"],
            ["Information:Data processing"],
        )
        self.assertEqual(
            minimal_slice["items"][0]["job_base_auxiliary_signal"]["factor_labels"],
            ["Information:Data processing"],
        )
        self.assertIn("KSA Term Ontology Impact Report", markdown)
        self.assertIn("Top concepts", markdown)
        self.assertIn("linked_penalty_group_count", markdown)
        self.assertIn("job_base_auxiliary_group_count", markdown)
        self.assertIn("Information:Data processing", markdown)
        self.assertIn("minimal_review_priority_level_counts", markdown)
        self.assertIn("minimal_review_priority_score", markdown)
        self.assertIn("linked_penalty_rows=1", markdown)

    def test_ksa_term_ontology_impact_report_deduplicates_shared_concept_totals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', 'Analyze workforce data')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                ksa_ids = []
                atomic_ids = []
                for index, text in enumerate(("Alpha term", "Beta term"), start=1):
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, ?)
                        """,
                        (element_id, str(index), text),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    ksa_ids.append(ksa_id)
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                                  'Review KSA', ?)
                        """,
                        (str(ksa_id), timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO ksa_atomic_items(
                            ksa_id, element_id, ksa_type_name, atom_index,
                            atom_text, normalized_key, created_at
                        ) VALUES (?, ?, 'knowledge', 1, ?, ?, ?)
                        """,
                        (ksa_id, element_id, text, text.replace(" ", "").lower(), timestamp),
                    )
                    atomic_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type,
                        definition_status, review_status, created_at, updated_at
                    ) VALUES ('Shared concept', 'sharedconcept', 'knowledge',
                              'missing', 'raw', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                concept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for ksa_id in ksa_ids:
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                conn.execute(
                    """
                    INSERT INTO criteria_concept_links(criteria_id, concept_id, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (criteria_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, created_at
                    ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                              'fixture relation', ?)
                    """,
                    (criteria_id, element_id, concept_id, concept_id, atomic_ids[0], atomic_ids[1], timestamp),
                )
                conn.commit()

                transition_quality_report = {
                    "evidence": {
                        "transition_evaluation": {
                            "scenario_count": 1,
                            "cases": [
                                {
                                    "ok": True,
                                    "recommended_course_evidence": [
                                        {
                                            "course_name": "Shared course",
                                            "quality_issue_penalty": {
                                                "applied": True,
                                                "issue_types": ["duplicate_text"],
                                                "affected_concepts": [
                                                    {
                                                        "concept_id": concept_id,
                                                        "issue_types": ["duplicate_text"],
                                                    }
                                                ],
                                            },
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                }
                report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=10,
                    issue_types=["duplicate_text"],
                    transition_quality_report=transition_quality_report,
                )
            finally:
                conn.close()

        self.assertEqual(report["group_count"], 2)
        self.assertEqual(report["total_unique_impacted_concept_count"], 1)
        self.assertEqual(report["recommendation_penalty_group_count"], 2)
        self.assertEqual(report["represented_recommendation_penalty_concept_count"], 1)
        self.assertEqual(report["source_transition_penalty_concept_count"], 1)
        self.assertEqual(
            [group["linked_penalized_concepts"]["concept_count"] for group in report["groups"]],
            [1, 1],
        )
        self.assertEqual(
            report["minimal_review_priority_level_counts"],
            {"critical_minimal_review": 2},
        )
        self.assertTrue(
            all(group["minimal_review_priority_score"] >= 80 for group in report["groups"])
        )
        self.assertEqual(report["total_task_relation_count"], 1)
        self.assertEqual(report["total_criteria_link_count"], 1)

    def test_ksa_term_ontology_impact_report_supplements_transition_penalty_groups_beyond_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                concept_ids: dict[str, int] = {}
                for concept_name in ("Frequent concept", "Penalty concept"):
                    conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, review_status, created_at, updated_at
                        ) VALUES (?, ?, 'knowledge', 'missing', 'raw', ?, ?)
                        """,
                        (concept_name, concept_name.replace(" ", "").lower(), timestamp, timestamp),
                    )
                    concept_ids[concept_name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                fixtures = [
                    ("Frequent term", concept_ids["Frequent concept"]),
                    ("Frequent term", concept_ids["Frequent concept"]),
                    ("Penalty term", concept_ids["Penalty concept"]),
                ]
                for index, (ksa_text, concept_id) in enumerate(fixtures, start=1):
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, ?)
                        """,
                        (element_id, str(index), ksa_text),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                                  'Review KSA', ?)
                        """,
                        (str(ksa_id), timestamp),
                    )
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                conn.commit()

                transition_quality_report = {
                    "evidence": {
                        "transition_evaluation": {
                            "scenario_count": 1,
                            "cases": [
                                {
                                    "recommended_course_evidence": [
                                        {
                                            "course_name": "Penalty course",
                                            "quality_issue_penalty": {
                                                "applied": True,
                                                "issue_types": ["duplicate_text"],
                                                "affected_concepts": [
                                                    {
                                                        "concept_id": concept_ids["Penalty concept"],
                                                        "issue_types": ["duplicate_text"],
                                                    }
                                                ],
                                            },
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                }
                report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=1,
                    issue_types=["duplicate_text"],
                    transition_quality_report=transition_quality_report,
                )
            finally:
                conn.close()

        self.assertEqual(report["group_count"], 1)
        self.assertEqual(report["candidate_group_count"], 2)
        self.assertEqual(report["dropped_group_count"], 1)
        self.assertEqual(report["transition_penalty_candidate_group_count"], 1)
        self.assertEqual(report["transition_penalty_supplemental_candidate_group_count"], 1)
        self.assertEqual(report["transition_penalty_supplemental_group_count"], 1)
        self.assertEqual(report["represented_recommendation_penalty_concept_count"], 1)
        self.assertEqual(report["represented_issue_count"], 1)
        self.assertEqual(report["represented_ksa_count"], 1)
        self.assertEqual(
            report["represented_issue_count"],
            sum(group["issue_count"] for group in report["groups"]),
        )
        groups_by_key = {group["normalized_ksa_term"]: group for group in report["groups"]}
        self.assertNotIn("frequentterm", groups_by_key)
        self.assertIn("penaltyterm", groups_by_key)
        penalty_group = groups_by_key["penaltyterm"]
        self.assertTrue(penalty_group["included_by_transition_penalty"])
        self.assertEqual(penalty_group["review_pack_source"], "transition_quality_penalty_concept")
        self.assertEqual(penalty_group["source_penalty_concept_ids"], [concept_ids["Penalty concept"]])
        self.assertEqual(penalty_group["linked_penalized_concepts"]["concept_count"], 1)
        self.assertEqual(penalty_group["minimal_review_priority_level"], "high_minimal_review")
        self.assertIn("linked_transition_penalty_concepts", penalty_group["minimal_review_priority_reasons"])

    def test_ksa_term_ontology_impact_group_counts_deduplicate_shared_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
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
                    ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                              '5', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_23v3', '1', 'E1', 'Plan workforce', '5')
                    """
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', 'Analyze workforce data')
                    """,
                    (element_id,),
                )
                criteria_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                concept_ids = []
                for concept_name in ("Shared concept A", "Shared concept B"):
                    conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, review_status, created_at, updated_at
                        ) VALUES (?, ?, 'knowledge', 'missing', 'raw', ?, ?)
                        """,
                        (concept_name, concept_name.replace(" ", "").lower(), timestamp, timestamp),
                    )
                    concept_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

                atomic_ids = []
                for index, concept_id in enumerate(concept_ids, start=1):
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, 'Shared term')
                        """,
                        (element_id, str(index)),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                                  'Review KSA', ?)
                        """,
                        (str(ksa_id), timestamp),
                    )
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                        (ksa_id, concept_id, timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO ksa_atomic_items(
                            ksa_id, element_id, ksa_type_name, atom_index,
                            atom_text, normalized_key, created_at
                        ) VALUES (?, ?, 'knowledge', 1, 'Shared term', 'sharedterm', ?)
                        """,
                        (ksa_id, element_id, timestamp),
                    )
                    atomic_ids.append(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
                    conn.execute(
                        """
                        INSERT INTO criteria_concept_links(criteria_id, concept_id, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (criteria_id, concept_id, timestamp),
                    )

                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, created_at
                    ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                              'fixture relation', ?)
                    """,
                    (
                        criteria_id,
                        element_id,
                        concept_ids[0],
                        concept_ids[1],
                        atomic_ids[0],
                        atomic_ids[1],
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ncs_training_courses(
                        ncs_cl_cd, compe_unit_name, train_goal, train_time,
                        fac_name, meth_name, api_fetched_at
                    ) VALUES ('0202020101', 'HR planning', 'Analyze workforce data',
                              '8', 'classroom', 'lecture', ?)
                    """,
                    (timestamp,),
                )
                training_course_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                for concept_id in concept_ids:
                    conn.execute(
                        """
                        INSERT INTO ncs_training_course_concept_links(
                            training_course_id, unit_code, concept_id, link_method,
                            confidence_score, created_at, updated_at
                        ) VALUES (?, '0202020101_23v3', ?, 'fixture', 0.9, ?, ?)
                        """,
                        (training_course_id, concept_id, timestamp, timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO training_goal_concept_links(
                            training_course_id, unit_code, element_id, concept_id,
                            link_method, confidence_score, created_at, updated_at
                        ) VALUES (?, '0202020101_23v3', ?, ?, 'fixture', 0.9, ?, ?)
                        """,
                        (training_course_id, element_id, concept_id, timestamp, timestamp),
                    )
                conn.commit()

                report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=5,
                    concept_limit_per_group=5,
                    issue_types=["duplicate_text"],
                )
            finally:
                conn.close()

        self.assertEqual(report["group_count"], 1)
        group = report["groups"][0]
        self.assertEqual(group["linked_concept_count"], 2)
        self.assertEqual(group["group_task_relation_count"], 1)
        self.assertEqual(group["group_training_course_link_count"], 1)
        self.assertEqual(group["group_training_goal_link_count"], 1)
        self.assertEqual(group["group_training_link_count"], 1)
        self.assertEqual(report["total_task_relation_count"], 1)
        self.assertEqual(report["total_training_course_link_count"], 1)
        self.assertEqual(report["total_training_goal_link_count"], 1)

    def test_ksa_term_ontology_impact_job_base_auxiliary_signal_is_unit_scoped_and_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR planning')
                    """
                )
                classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                    "classification_id"
                ]
                for unit_code, base_unit_code, unit_name in (
                    ("0202020101_23v3", "0202020101", "HR planning"),
                    ("0202020102_23v3", "0202020102", "HR data support"),
                ):
                    conn.execute(
                        """
                        INSERT INTO competency_units(
                            unit_code, base_unit_code, unit_version, unit_name_raw,
                            unit_level_raw, classification_id, created_at, updated_at
                        ) VALUES (?, ?, '23v3', ?, '5', ?, ?, ?)
                        """,
                        (unit_code, base_unit_code, unit_name, classification_id, timestamp, timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO competency_elements(
                            unit_code, element_no, element_code_raw,
                            element_name_raw, element_level_raw
                        ) VALUES (?, '1', 'E1', ?, '5')
                        """,
                        (unit_code, unit_name),
                    )
                    element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                        VALUES (?, '1', ?)
                        """,
                        (element_id, f"{unit_name} criteria"),
                    )
                element_rows = {
                    row["unit_code"]: row["element_id"]
                    for row in conn.execute("SELECT unit_code, element_id FROM competency_elements").fetchall()
                }
                criteria_rows = {
                    row["element_id"]: row["criteria_id"]
                    for row in conn.execute("SELECT element_id, criteria_id FROM performance_criteria").fetchall()
                }
                concepts: dict[str, int] = {}
                for concept_name in ("Data work", "Information"):
                    conn.execute(
                        """
                        INSERT INTO ontology_concepts(
                            concept_name, normalized_key, concept_type,
                            definition_status, review_status, created_at, updated_at
                        ) VALUES (?, ?, 'knowledge', 'missing', 'raw', ?, ?)
                        """,
                        (concept_name, concept_name.replace(" ", "").lower(), timestamp, timestamp),
                    )
                    concepts[concept_name] = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

                data_element_id = element_rows["0202020101_23v3"]
                data_criteria_id = criteria_rows[data_element_id]
                data_atomic_ids = []
                for index in range(2):
                    conn.execute(
                        """
                        INSERT INTO ksa_items(
                            element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                        ) VALUES (?, '01', 'knowledge', ?, 'Data term')
                        """,
                        (data_element_id, str(index + 1)),
                    )
                    ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    conn.execute(
                        """
                        INSERT INTO quality_issues(
                            target_type, target_id, issue_type, severity,
                            issue_detail, suggested_action, detected_at
                        ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                                  'Review KSA', ?)
                        """,
                        (str(ksa_id), timestamp),
                    )
                    conn.execute(
                        "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                        (ksa_id, concepts["Data work"], timestamp),
                    )
                    conn.execute(
                        """
                        INSERT INTO ksa_atomic_items(
                            ksa_id, element_id, ksa_type_name, atom_index,
                            atom_text, normalized_key, created_at
                        ) VALUES (?, ?, 'knowledge', 1, 'Data term', ?, ?)
                        """,
                        (ksa_id, data_element_id, f"dataterm{index}", timestamp),
                    )
                    atomic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    data_atomic_ids.append(atomic_id)
                    conn.execute(
                        """
                        INSERT INTO task_ksa_concept_relations(
                            criteria_id, element_id, source_concept_id, relation_type,
                            target_concept_id, source_atomic_id, target_atomic_id,
                            evidence_text, created_at
                        ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                                  'fixture relation', ?)
                        """,
                        (
                            data_criteria_id,
                            data_element_id,
                            concepts["Data work"],
                            concepts["Data work"],
                            atomic_id,
                            atomic_id,
                            timestamp,
                        ),
                    )

                similar_element_id = element_rows["0202020102_23v3"]
                similar_criteria_id = criteria_rows[similar_element_id]
                conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, '01', 'knowledge', '1', 'Information')
                    """,
                    (similar_element_id,),
                )
                similar_ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'fixture issue',
                              'Review KSA', ?)
                    """,
                    (str(similar_ksa_id), timestamp),
                )
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                    (similar_ksa_id, concepts["Information"], timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index,
                        atom_text, normalized_key, created_at
                    ) VALUES (?, ?, 'knowledge', 1, 'Information', 'information', ?)
                    """,
                    (similar_ksa_id, similar_element_id, timestamp),
                )
                similar_atomic_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, created_at
                    ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?,
                              'fixture relation', ?)
                    """,
                    (
                        similar_criteria_id,
                        similar_element_id,
                        concepts["Information"],
                        concepts["Information"],
                        similar_atomic_id,
                        similar_atomic_id,
                        timestamp,
                    ),
                )
                before_job_base_report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=10,
                    concept_limit_per_group=5,
                    issue_types=["duplicate_text"],
                )

                conn.execute(
                    """
                    INSERT INTO ncs_job_base_competencies(
                        competency_name, normalized_key, created_at, updated_at
                    ) VALUES ('Information', 'information', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                information_competency_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_job_base_factors(
                        job_base_competency_id, factor_name, normalized_key,
                        created_at, updated_at
                    ) VALUES (?, 'Data processing', 'dataprocessing', ?, ?)
                    """,
                    (information_competency_id, timestamp, timestamp),
                )
                data_processing_factor_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_job_base_competencies(
                        competency_name, normalized_key, created_at, updated_at
                    ) VALUES ('Communication', 'communication', ?, ?)
                    """,
                    (timestamp, timestamp),
                )
                communication_competency_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ncs_unit_job_base_links(
                        unit_code, job_base_competency_id, job_base_factor_id,
                        compe_unit_name, source_payload, api_fetched_at,
                        created_at, updated_at
                    ) VALUES ('0202020101_23v3', ?, ?, 'HR planning',
                              '{"secret":"source_payload_should_not_surface"}', ?, ?, ?)
                    """,
                    (
                        information_competency_id,
                        data_processing_factor_id,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO ncs_unit_job_base_links(
                        unit_code, job_base_competency_id, job_base_factor_id,
                        compe_unit_name, source_payload, api_fetched_at,
                        created_at, updated_at
                    ) VALUES ('0202020101_23v3', ?, NULL, 'HR planning',
                              '{"secret":"factorless_payload_should_not_surface"}', ?, ?, ?)
                    """,
                    (communication_competency_id, timestamp, timestamp, timestamp),
                )
                after_job_base_report = build_ksa_term_ontology_impact_report(
                    conn,
                    limit=10,
                    concept_limit_per_group=5,
                    issue_types=["duplicate_text"],
                )
                with tempfile.TemporaryDirectory() as report_tmp:
                    markdown_path = Path(report_tmp) / "impact.md"
                    write_ksa_term_ontology_impact_report_markdown(after_job_base_report, markdown_path)
                    markdown = markdown_path.read_text(encoding="utf-8")
            finally:
                conn.close()

        before_by_key = {
            group["normalized_ksa_term"]: group for group in before_job_base_report["groups"]
        }
        after_by_key = {
            group["normalized_ksa_term"]: group for group in after_job_base_report["groups"]
        }
        self.assertEqual(
            after_by_key["dataterm"]["minimal_review_priority_score"],
            before_by_key["dataterm"]["minimal_review_priority_score"],
        )
        self.assertEqual(
            after_by_key["dataterm"]["minimal_review_priority_level"],
            before_by_key["dataterm"]["minimal_review_priority_level"],
        )
        self.assertEqual(after_job_base_report["job_base_auxiliary_group_count"], 1)
        self.assertEqual(after_job_base_report["job_base_auxiliary_concept_count"], 1)

        data_signal = after_by_key["dataterm"]["job_base_auxiliary_signal"]
        self.assertEqual(data_signal["unit_count"], 1)
        self.assertEqual(data_signal["competency_count"], 2)
        self.assertEqual(data_signal["factor_count"], 2)
        self.assertCountEqual(data_signal["competency_names"], ["Information", "Communication"])
        self.assertCountEqual(
            data_signal["factor_labels"],
            ["Information:Data processing", "Communication"],
        )
        self.assertTrue(all("source_payload" not in link for link in data_signal["top_links"]))
        self.assertEqual(
            next(link for link in data_signal["top_links"] if link["label"] == "Information:Data processing")[
                "unit_count"
            ],
            1,
        )
        self.assertIsNone(
            next(link for link in data_signal["top_links"] if link["label"] == "Communication")[
                "job_base_factor_id"
            ]
        )

        similar_signal = after_by_key["information"]["job_base_auxiliary_signal"]
        self.assertEqual(similar_signal["competency_count"], 0)
        self.assertEqual(similar_signal["factor_labels"], [])
        self.assertEqual(similar_signal["operator_action"], "no_job_base_auxiliary_signal")

        serialized = json.dumps(after_job_base_report, ensure_ascii=False)
        self.assertNotIn("source_payload_should_not_surface", serialized)
        self.assertNotIn("factorless_payload_should_not_surface", serialized)
        self.assertNotIn("source_payload_should_not_surface", markdown)
        self.assertNotIn("factorless_payload_should_not_surface", markdown)
        self.assertFalse(after_job_base_report["db_writes"])
        self.assertFalse(after_job_base_report["status_update_allowed"])

    def test_ksa_term_minimal_review_slice_filters_priority_levels_and_writes_outputs(self) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "group_count": 3,
            "candidate_group_count": 3,
            "dropped_group_count": 0,
            "source_transition_penalty_concept_count": 2,
            "represented_recommendation_penalty_concept_count": 2,
            "groups": [
                {
                    "normalized_ksa_term": "criticalterm",
                    "representative_ksa_text": "Critical term",
                    "review_bucket": "broad_duplicate_downweight_review",
                    "review_pack_source": "transition_quality_penalty_concept",
                    "included_by_transition_penalty": True,
                    "source_penalty_concept_ids": [101],
                    "minimal_review_priority_score": 100,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "minimal_review_operator_action": "inspect_linked_penalized_concepts_before_any_scoring_decision",
                    "minimal_review_scope_note": "triage only",
                    "operator_impact_action": "inspect",
                    "issue_count": 4,
                    "ksa_count": 3,
                    "unit_count": 2,
                    "major_count": 1,
                    "linked_concept_count": 1,
                    "group_task_relation_count": 10,
                    "group_training_course_link_count": 2,
                    "group_training_goal_link_count": 1,
                    "linked_penalized_concepts": {
                        "concept_count": 1,
                        "issue_counts": {"short_ksa": 1},
                        "course_names": ["Course A"],
                    },
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 101,
                            "concept_name": "Critical concept",
                            "concept_type": "knowledge",
                            "review_status": "model_preprocessed",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {"short_ksa": 1},
                            "linked_penalty_courses": ["Course A"],
                            "task_relation_count": 10,
                            "training_course_link_count": 2,
                            "training_goal_link_count": 1,
                        }
                    ],
                    "top_concepts": [
                        {
                            "concept_id": 101,
                            "concept_name": "Critical concept",
                            "concept_type": "knowledge",
                            "review_status": "model_preprocessed",
                            "recommendation_penalty_course_count": 1,
                            "recommendation_penalty_issue_counts": {"short_ksa": 1},
                            "recommendation_penalty_course_names": ["Course A"],
                            "task_relation_count": 10,
                            "training_course_link_count": 2,
                            "training_goal_link_count": 1,
                        }
                    ],
                    "samples": [{"ksa_id": 1, "ksa_text_raw": "Critical term"}],
                },
                {
                    "normalized_ksa_term": "mediumterm",
                    "representative_ksa_text": "Medium term",
                    "minimal_review_priority_score": 50,
                    "minimal_review_priority_level": "medium_minimal_review",
                    "issue_count": 2,
                    "ksa_count": 2,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 0},
                    "top_concepts": [],
                    "samples": [],
                },
                {
                    "normalized_ksa_term": "criticalvariant",
                    "representative_ksa_text": "Critical term variant",
                    "review_bucket": "broad_duplicate_downweight_review",
                    "review_pack_source": "transition_quality_penalty_concept",
                    "included_by_transition_penalty": True,
                    "source_penalty_concept_ids": [101],
                    "minimal_review_priority_score": 95,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "minimal_review_operator_action": "inspect_linked_penalized_concepts_before_any_scoring_decision",
                    "issue_count": 2,
                    "ksa_count": 2,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_concept_count": 1,
                    "group_task_relation_count": 8,
                    "group_training_course_link_count": 1,
                    "group_training_goal_link_count": 1,
                    "linked_penalized_concepts": {
                        "concept_count": 1,
                        "issue_counts": {"duplicate_text": 1},
                        "course_names": ["Course A"],
                    },
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 101,
                            "concept_name": "Critical concept",
                            "concept_type": "knowledge",
                            "review_status": "model_preprocessed",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {"duplicate_text": 1},
                            "linked_penalty_courses": ["Course A"],
                            "task_relation_count": 8,
                            "training_course_link_count": 1,
                            "training_goal_link_count": 1,
                        }
                    ],
                    "top_concepts": [
                        {
                            "concept_id": 101,
                            "concept_name": "Critical concept",
                            "concept_type": "knowledge",
                            "review_status": "model_preprocessed",
                            "recommendation_penalty_course_count": 1,
                            "recommendation_penalty_issue_counts": {"duplicate_text": 1},
                            "recommendation_penalty_course_names": ["Course A"],
                            "task_relation_count": 8,
                            "training_course_link_count": 1,
                            "training_goal_link_count": 1,
                        }
                    ],
                    "samples": [],
                },
                {
                    "normalized_ksa_term": "highterm",
                    "representative_ksa_text": "High term",
                    "minimal_review_priority_score": 75,
                    "minimal_review_priority_level": "high_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "source_penalty_concept_ids": [102],
                    "issue_count": 1,
                    "ksa_count": 1,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1, "issue_counts": {}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 102,
                            "concept_name": "High concept",
                            "concept_type": "skill",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {},
                            "linked_penalty_courses": [],
                        }
                    ],
                    "top_concepts": [
                        {
                            "concept_id": 102,
                            "concept_name": "High concept",
                            "concept_type": "skill",
                            "recommendation_penalty_course_count": 1,
                        }
                    ],
                    "samples": [],
                },
            ],
        }

        report = build_ksa_term_minimal_review_slice(
            impact_report,
            source_path="reports/impact.json",
            limit=10,
        )
        report["concept_review_groups"][0]["concept_name"] = "=cmd|'/C calc'!A0"
        report["concept_review_groups"][0]["operator_action"] = " @operator-action"
        with tempfile.TemporaryDirectory() as tmp:
            jsonl_path = Path(tmp) / "slice.jsonl"
            csv_path = Path(tmp) / "slice.csv"
            markdown_path = Path(tmp) / "slice.md"
            write_ksa_term_minimal_review_slice_jsonl(report, jsonl_path)
            csv_summary = write_ksa_term_minimal_review_slice_csv(report, csv_path)
            write_ksa_term_minimal_review_slice_markdown(report, markdown_path)
            jsonl_lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            csv_text = csv_path.read_text(encoding="utf-8-sig")
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(report["schema"], "ncs_ksa_term_minimal_review_slice_v1")
        self.assertEqual(report["item_count"], 3)
        self.assertEqual(report["candidate_item_count"], 3)
        self.assertEqual(report["represented_recommendation_penalty_concept_count"], 2)
        self.assertEqual(report["concept_review_group_count"], 2)
        critical_concept_group = next(
            group for group in report["concept_review_groups"] if group["concept_id"] == 101
        )
        self.assertEqual(critical_concept_group["item_count"], 2)
        self.assertEqual(
            critical_concept_group["term_variants"],
            ["Critical term", "Critical term variant"],
        )
        self.assertEqual(
            critical_concept_group["suggested_decision"]["suggested_decision"],
            "split_or_scope_term",
        )
        self.assertEqual(
            critical_concept_group["suggested_decision"]["suggested_decision_policy"],
            "review_assist_only_not_a_human_decision",
        )
        self.assertEqual(
            critical_concept_group["genericity_signal"]["schema"],
            "ncs_ksa_term_genericity_signal_v1",
        )
        self.assertEqual(
            critical_concept_group["genericity_signal"]["scoring_role"],
            "review_assist_only_not_a_human_decision",
        )
        self.assertFalse(critical_concept_group["genericity_signal"]["status_update_allowed"])
        self.assertFalse(critical_concept_group["genericity_signal"]["db_writes"])
        self.assertFalse(critical_concept_group["genericity_signal"]["approval_claim"])
        self.assertEqual(
            [item["representative_ksa_text"] for item in report["items"]],
            ["Critical term", "Critical term variant", "High term"],
        )
        self.assertTrue(all(item["status_update_allowed"] is False for item in report["items"]))
        self.assertTrue(all(item["db_writes"] is False for item in report["items"]))
        self.assertTrue(all(item["approval_claim"] is False for item in report["items"]))
        self.assertTrue(all(not value for value in report["items"][0]["decision_fields"].values()))
        self.assertNotIn("Medium term", [item["representative_ksa_text"] for item in report["items"]])
        self.assertEqual(len(jsonl_lines), 2)
        self.assertIn("ncs_ksa_term_minimal_review_slice_concept_group_v1", jsonl_lines[0])
        self.assertEqual(csv_summary["record_count"], 2)
        self.assertEqual(csv_summary["schema"], "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1")
        self.assertEqual(len(csv_rows), 2)
        self.assertEqual(csv_rows[0]["schema"], "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1")
        self.assertEqual(csv_rows[0]["concept_id"], "101")
        self.assertEqual(csv_rows[0]["concept_name"], "'=cmd|'/C calc'!A0")
        self.assertEqual(csv_rows[0]["operator_action"], "' @operator-action")
        self.assertEqual(csv_rows[0]["suggested_decision"], "split_or_scope_term")
        self.assertEqual(csv_rows[0]["suggested_decision_policy"], "review_assist_only_not_a_human_decision")
        self.assertEqual(csv_rows[0]["decision"], "")
        self.assertEqual(csv_rows[0]["reviewer_id"], "")
        self.assertEqual(csv_rows[0]["reviewed_at"], "")
        self.assertEqual(csv_rows[0]["rationale"], "")
        self.assertEqual(csv_rows[0]["status_update_allowed"], "False")
        self.assertEqual(csv_rows[0]["db_writes"], "False")
        self.assertEqual(csv_rows[0]["approval_claim"], "False")
        self.assertNotIn("source_payload", csv_text)
        self.assertIn("KSA Term Minimal Review Slice", markdown)
        self.assertIn("Concept Review Groups", markdown)
        self.assertIn("Critical term", markdown)
        self.assertIn("High term", markdown)

    def test_ksa_review_minimization_audit_summarizes_review_reduction_safely(self) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "ok": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "group_count": 2,
            "candidate_group_count": 2,
            "dropped_group_count": 0,
            "total_open_issue_count": 1000,
            "represented_issue_count": 80,
            "represented_ksa_count": 40,
            "source_transition_penalty_concept_count": 2,
            "represented_recommendation_penalty_concept_count": 2,
            "source_transition_penalized_recommendation_row_count": 3,
            "groups": [
                {
                    "normalized_ksa_term": "privacy law",
                    "representative_ksa_text": "Privacy law",
                    "review_bucket": "broad_generic_downweight_review",
                    "review_pack_source": "transition_quality_penalty_concept",
                    "included_by_transition_penalty": True,
                    "source_penalty_concept_ids": [10],
                    "minimal_review_priority_score": 100,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "minimal_review_operator_action": "inspect_linked_concepts_for_generic_downweight_or_scope_split",
                    "minimal_review_scope_note": "fixture",
                    "operator_impact_action": "review",
                    "issue_count": 4,
                    "ksa_count": 4,
                    "unit_count": 2,
                    "major_count": 1,
                    "linked_concept_count": 1,
                    "group_task_relation_count": 2500,
                    "group_training_course_link_count": 130,
                    "group_training_goal_link_count": 50,
                    "linked_penalized_concepts": {"concept_count": 1},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 10,
                            "concept_name": "Privacy law",
                            "concept_type": "knowledge",
                            "review_status": "model_preprocessed",
                            "linked_penalty_rows": 2,
                            "linked_penalty_issues": {"broad_generic_ksa": 2},
                            "linked_penalty_courses": ["Payroll"],
                            "task_relation_count": 2500,
                            "training_course_link_count": 130,
                            "training_goal_link_count": 50,
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                    "status_update_allowed": False,
                },
                {
                    "normalized_ksa_term": "specific method",
                    "representative_ksa_text": "Specific method",
                    "review_bucket": "term_quality_review",
                    "review_pack_source": "quality_issue",
                    "included_by_transition_penalty": True,
                    "source_penalty_concept_ids": [20],
                    "minimal_review_priority_score": 80,
                    "minimal_review_priority_level": "high_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "minimal_review_operator_action": "inspect_linked_concepts",
                    "minimal_review_scope_note": "fixture",
                    "operator_impact_action": "review",
                    "issue_count": 1,
                    "ksa_count": 1,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_concept_count": 1,
                    "group_task_relation_count": 10,
                    "group_training_course_link_count": 1,
                    "group_training_goal_link_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 20,
                            "concept_name": "Specific method",
                            "concept_type": "skill",
                            "review_status": "model_preprocessed",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {"short_ksa": 1},
                            "linked_penalty_courses": ["HR plan"],
                            "task_relation_count": 10,
                            "training_course_link_count": 1,
                            "training_goal_link_count": 1,
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                    "status_update_allowed": False,
                },
            ],
        }
        minimal_slice = build_ksa_term_minimal_review_slice(impact_report, limit=2)
        readiness = {
            "schema": "ncs_ksa_term_review_readiness_v1",
            "ok": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "summary": {
                "concept_review_group_count": 2,
                "pending_decision_count": 2,
                "completed_decision_count": 0,
                "action_count": 0,
            },
        }

        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "audit.md"
            audit = build_ksa_review_minimization_audit(
                impact_report,
                minimal_slice,
                readiness_report=readiness,
            )
            write_ksa_review_minimization_audit_markdown(audit, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(audit["ok"])
        self.assertEqual(audit["schema"], "ncs_ksa_review_minimization_audit_v1")
        self.assertEqual(audit["review_reduction"]["source_open_issue_count"], 1000)
        self.assertEqual(audit["review_reduction"]["concept_review_group_count"], 2)
        self.assertEqual(
            audit["review_reduction"]["open_issue_to_concept_review_group_ratio"],
            500.0,
        )
        self.assertEqual(
            audit["review_reduction"]["transition_penalty_concept_coverage_percent"],
            100.0,
        )
        self.assertEqual(audit["genericity_signal_summary"]["level_counts"]["high"], 1)
        self.assertEqual(
            audit["genericity_signal_summary"]["scoring_role"],
            "review_assist_only_not_a_human_decision",
        )
        self.assertFalse(audit["safety_contract"]["status_update_allowed"])
        self.assertFalse(audit["safety_contract"]["db_writes"])
        self.assertFalse(audit["safety_contract"]["approval_claim"])
        self.assertFalse(audit["safety_contract"]["trusted_status_write_allowed"])
        self.assertTrue(audit["safety_contract"]["decision_fields_blank_until_human_action"])
        self.assertIn("KSA Review Minimization Audit", markdown)
        self.assertIn("Privacy law", markdown)

    def test_ksa_term_minimal_review_slice_uses_full_penalized_rows_when_top_concepts_truncated(self) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "group_count": 1,
            "represented_recommendation_penalty_concept_count": 3,
            "source_transition_penalty_concept_count": 3,
            "groups": [
                {
                    "normalized_ksa_term": "multiconceptterm",
                    "representative_ksa_text": "Multi concept term",
                    "minimal_review_priority_score": 100,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "issue_count": 3,
                    "ksa_count": 3,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 3, "issue_counts": {}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {"concept_id": 1, "concept_name": "A", "concept_type": "knowledge", "linked_penalty_rows": 1},
                        {"concept_id": 2, "concept_name": "B", "concept_type": "skill", "linked_penalty_rows": 1},
                        {"concept_id": 3, "concept_name": "C", "concept_type": "attitude", "linked_penalty_rows": 1},
                    ],
                    "top_concepts": [
                        {"concept_id": 1, "concept_name": "A", "recommendation_penalty_course_count": 1}
                    ],
                    "samples": [],
                }
            ],
        }

        report = build_ksa_term_minimal_review_slice(impact_report)

        self.assertEqual(report["represented_recommendation_penalty_concept_count"], 3)
        self.assertEqual(report["concept_review_group_count"], 3)
        self.assertEqual(
            [group["concept_id"] for group in report["concept_review_groups"]],
            [1, 2, 3],
        )

    def test_ksa_term_minimal_review_slice_rejects_wrong_schema_and_invalid_level(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires ncs_ksa_term_ontology_impact_report_v1"):
            build_ksa_term_minimal_review_slice(
                {
                    "schema": "ncs_ksa_term_preprocessing_review_pack_v1",
                    "groups": [],
                }
            )
        with self.assertRaisesRegex(ValueError, "Unsupported minimal review priority level"):
            build_ksa_term_minimal_review_slice(
                {
                    "schema": "ncs_ksa_term_ontology_impact_report_v1",
                    "groups": [],
                },
                levels=["critical"],
            )

    def test_ksa_term_minimal_review_decision_csv_audit_is_readonly_and_validates_decisions(self) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "group_count": 1,
            "source_transition_penalty_concept_count": 1,
            "groups": [
                {
                    "normalized_ksa_term": "criticalterm",
                    "representative_ksa_text": "Critical term",
                    "minimal_review_priority_score": 100,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "issue_count": 1,
                    "ksa_count": 1,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1, "issue_counts": {}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 101,
                            "concept_name": "=cmd|'/C calc'!A0",
                            "concept_type": "knowledge",
                            "review_status": "candidate",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {},
                            "linked_penalty_courses": [],
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                }
            ],
        }
        report = build_ksa_term_minimal_review_slice(impact_report)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision.csv"
            audit_path = tmp_path / "audit.md"
            write_ksa_term_minimal_review_slice_csv(report, csv_path)
            source_slice_path = tmp_path / "slice.json"
            source_jsonl_path = tmp_path / "slice.jsonl"
            source_manifest_path = tmp_path / "manifest.json"
            source_slice_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            write_ksa_term_minimal_review_slice_jsonl(report, source_jsonl_path)
            source_manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_term_review_workflow_manifest_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "human_decision_required": True,
                        "approval_claim": False,
                        "summary": {
                            "concept_review_group_count": 1,
                            "concept_review_csv_record_count": 1,
                        },
                        "artifacts": {
                            "minimal_review_slice": str(source_slice_path),
                            "minimal_review_jsonl": str(source_jsonl_path),
                        },
                        "safety_contract": {
                            "raw_source_mutation_allowed": False,
                            "trusted_status_write_allowed": False,
                            "status_update_allowed": False,
                            "db_writes": False,
                            "approval_claim": False,
                            "source_payload_exposed": False,
                            "preferred_decision_unit": "concept_review_group",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            blank_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            no_source_audit = audit_ksa_term_minimal_review_decision_csv(csv_path)
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fieldnames = list(rows[0])
            self.assertEqual(rows[0]["concept_name"], "'=cmd|'/C calc'!A0")
            stripped_csv_path = tmp_path / "stripped_decision.csv"
            stripped_fieldnames = [
                field
                for field in fieldnames
                if field
                not in {
                    "item_count",
                    "priority_levels",
                    "suggested_decision",
                    "proposed_concept_action",
                }
            ]
            with stripped_csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=stripped_fieldnames)
                writer.writeheader()
                writer.writerows(
                    {field: row.get(field, "") for field in stripped_fieldnames} for row in rows
                )
            stripped_context_audit = audit_ksa_term_minimal_review_decision_csv(
                stripped_csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["reviewer_id"] = "reviewer-without-decision"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            pending_metadata_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["reviewer_id"] = ""
            rows[0]["item_count"] = "999"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            stale_context_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["item_count"] = "1"
            source_jsonl_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_term_minimal_review_slice_concept_group_v1",
                        "concept_id": 999,
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            jsonl_mismatch_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            write_ksa_term_minimal_review_slice_jsonl(report, source_jsonl_path)
            same_id_tampered_jsonl_row = {
                "schema": "ncs_ksa_term_minimal_review_slice_concept_group_v1",
                **report["concept_review_groups"][0],
                "concept_name": "Tampered review context",
            }
            source_jsonl_path.write_text(
                json.dumps(same_id_tampered_jsonl_row, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            jsonl_record_mismatch_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            write_ksa_term_minimal_review_slice_jsonl(report, source_jsonl_path)
            rows[0]["concept_name"] = "=cmd|'/C calc'!A0"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            unescaped_formula_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["concept_name"] = "'=cmd|'/C calc'!A0"
            rows[0]["suggested_decision"] = "split_or_scope_term"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            spoofed_suggestion_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["suggested_decision"] = "needs_more_evidence"
            rows[0]["review_status"] = "human_reviewed"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            spoofed_review_status_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            rows[0]["review_status"] = "candidate"
            rows[0]["decision"] = "downweight_generic_term"
            rows[0]["reviewer_id"] = "reviewer-1"
            rows[0]["reviewed_at"] = "2026-06-26T00:00:00Z"
            rows[0]["rationale"] = "Generic term affects many task relations."
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            completed_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            write_ksa_term_minimal_review_decision_audit_markdown(completed_audit, audit_path)

            rows[0]["decision"] = "human_reviewed"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            invalid_audit = audit_ksa_term_minimal_review_decision_csv(csv_path)
            rows[0]["decision"] = ""
            rows[0]["status_update_allowed"] = "True"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            unsafe_flag_audit = audit_ksa_term_minimal_review_decision_csv(csv_path)
            rows[0]["status_update_allowed"] = "False"
            rows[0]["schema"] = "wrong_schema"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            schema_audit = audit_ksa_term_minimal_review_decision_csv(csv_path)
            rows[0]["schema"] = "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1"
            rows[0]["concept_id"] = "999"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            source_mismatch_audit = audit_ksa_term_minimal_review_decision_csv(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            markdown = audit_path.read_text(encoding="utf-8")

        self.assertTrue(blank_audit["ok"])
        self.assertEqual(blank_audit["pending_decision_count"], 1)
        self.assertEqual(blank_audit["completed_decision_count"], 0)
        self.assertEqual(blank_audit["next_action"], "await_human_decisions")
        self.assertTrue(blank_audit["source_manifest_match"])
        self.assertTrue(blank_audit["source_slice_match"])
        self.assertTrue(blank_audit["source_jsonl_match"])
        self.assertTrue(blank_audit["source_validation_performed"])
        self.assertTrue(blank_audit["source_validation_required_for_operator_evidence"])
        self.assertFalse(blank_audit["status_update_allowed"])
        self.assertFalse(blank_audit["db_writes"])
        self.assertFalse(blank_audit["approval_claim"])
        self.assertFalse(no_source_audit["ok"])
        self.assertFalse(no_source_audit["source_validation_performed"])
        self.assertEqual(
            {item["type"] for item in no_source_audit["source_validation_errors"]},
            {"source_manifest_path_required", "source_slice_path_required"},
        )
        self.assertFalse(stripped_context_audit["ok"])
        self.assertIn("item_count", stripped_context_audit["missing_required_fields"])
        self.assertIn("suggested_decision", stripped_context_audit["missing_required_fields"])
        self.assertIn("proposed_concept_action", stripped_context_audit["missing_required_fields"])
        self.assertFalse(pending_metadata_audit["ok"])
        self.assertIn(
            "pending_row_has_reviewer_or_action_metadata",
            pending_metadata_audit["invalid_rows"][0]["errors"],
        )
        self.assertFalse(stale_context_audit["ok"])
        self.assertIn(
            "source_slice_concept_metadata_mismatch",
            {item["type"] for item in stale_context_audit["source_slice_errors"]},
        )
        self.assertFalse(jsonl_mismatch_audit["ok"])
        self.assertFalse(jsonl_mismatch_audit["source_jsonl_match"])
        self.assertIn(
            "source_jsonl_concept_id_mismatch",
            {item["type"] for item in jsonl_mismatch_audit["source_jsonl_errors"]},
        )
        self.assertFalse(jsonl_record_mismatch_audit["ok"])
        self.assertFalse(jsonl_record_mismatch_audit["source_jsonl_match"])
        self.assertIn(
            "source_jsonl_record_mismatch",
            {item["type"] for item in jsonl_record_mismatch_audit["source_jsonl_errors"]},
        )
        self.assertFalse(unescaped_formula_audit["ok"])
        self.assertFalse(unescaped_formula_audit["source_slice_match"])
        self.assertIn(
            "source_slice_concept_metadata_mismatch",
            {item["type"] for item in unescaped_formula_audit["source_slice_errors"]},
        )
        self.assertFalse(spoofed_suggestion_audit["ok"])
        self.assertFalse(spoofed_suggestion_audit["source_slice_match"])
        self.assertIn(
            "source_slice_concept_metadata_mismatch",
            {item["type"] for item in spoofed_suggestion_audit["source_slice_errors"]},
        )
        self.assertFalse(spoofed_review_status_audit["ok"])
        self.assertFalse(spoofed_review_status_audit["source_slice_match"])
        self.assertIn(
            "source_slice_concept_metadata_mismatch",
            {item["type"] for item in spoofed_review_status_audit["source_slice_errors"]},
        )
        self.assertEqual(
            spoofed_review_status_audit["forbidden_status_rows"][0]["matched_terms"],
            ["human_reviewed"],
        )
        self.assertTrue(completed_audit["ok"])
        self.assertEqual(completed_audit["completed_decision_count"], 1)
        self.assertEqual(completed_audit["decision_counts"], {"downweight_generic_term": 1})
        self.assertEqual(completed_audit["next_action"], "ready_for_guarded_operator_review")
        self.assertFalse(invalid_audit["ok"])
        self.assertEqual(invalid_audit["invalid_decision_count"], 1)
        self.assertEqual(invalid_audit["forbidden_status_rows"][0]["matched_terms"], ["human_reviewed"])
        self.assertFalse(unsafe_flag_audit["ok"])
        self.assertIn("status_update_allowed_not_false", unsafe_flag_audit["invalid_rows"][0]["errors"])
        self.assertFalse(schema_audit["ok"])
        self.assertIn("unexpected_schema", schema_audit["invalid_rows"][0]["errors"])
        self.assertFalse(source_mismatch_audit["ok"])
        self.assertFalse(source_mismatch_audit["source_slice_match"])
        self.assertIn(
            "csv_concepts_not_in_source_slice",
            {item["type"] for item in source_mismatch_audit["source_slice_errors"]},
        )
        self.assertIn("KSA Term Minimal Review Decision Audit", markdown)
        self.assertIn("downweight_generic_term", markdown)

    def test_ksa_term_minimal_review_decision_audit_resolves_manifest_relative_sidecars(
        self,
    ) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "group_count": 1,
            "source_transition_penalty_concept_count": 1,
            "groups": [
                {
                    "normalized_ksa_term": "relative",
                    "representative_ksa_text": "Relative",
                    "minimal_review_priority_score": 80,
                    "minimal_review_priority_level": "high_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "issue_count": 1,
                    "ksa_count": 1,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1, "issue_counts": {}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 501,
                            "concept_name": "Relative concept",
                            "concept_type": "knowledge",
                            "review_status": "candidate",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {},
                            "linked_penalty_courses": [],
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                }
            ],
        }
        report = build_ksa_term_minimal_review_slice(impact_report)
        original_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision.csv"
            manifest_path = tmp_path / "manifest.json"
            source_slice_path = tmp_path / "slice.json"
            source_jsonl_path = tmp_path / "slice.jsonl"
            write_ksa_term_minimal_review_slice_csv(report, csv_path)
            source_slice_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            write_ksa_term_minimal_review_slice_jsonl(report, source_jsonl_path)
            manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_term_review_workflow_manifest_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "human_decision_required": True,
                        "approval_claim": False,
                        "summary": {
                            "concept_review_group_count": 1,
                            "concept_review_csv_record_count": 1,
                        },
                        "artifacts": {
                            "minimal_review_slice": "slice.json",
                            "minimal_review_jsonl": "slice.jsonl",
                        },
                        "safety_contract": {
                            "raw_source_mutation_allowed": False,
                            "trusted_status_write_allowed": False,
                            "status_update_allowed": False,
                            "db_writes": False,
                            "approval_claim": False,
                            "source_payload_exposed": False,
                            "preferred_decision_unit": "concept_review_group",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            try:
                os.chdir(other)
                audit = audit_ksa_term_minimal_review_decision_csv(
                    csv_path,
                    source_manifest_path=manifest_path,
                    source_slice_path=source_slice_path,
                )
                source_jsonl_path.unlink()
                write_ksa_term_minimal_review_slice_jsonl(report, Path(other) / "slice.jsonl")
                cwd_fallback_audit = audit_ksa_term_minimal_review_decision_csv(
                    csv_path,
                    source_manifest_path=manifest_path,
                    source_slice_path=source_slice_path,
                )
            finally:
                os.chdir(original_cwd)

        self.assertTrue(audit["ok"])
        self.assertTrue(audit["source_manifest_match"])
        self.assertTrue(audit["source_slice_match"])
        self.assertTrue(audit["source_jsonl_match"])
        self.assertFalse(cwd_fallback_audit["ok"])
        self.assertFalse(cwd_fallback_audit["source_jsonl_match"])
        self.assertIn(
            "source_jsonl_read_error",
            {item["type"] for item in cwd_fallback_audit["source_jsonl_errors"]},
        )

    def test_ksa_term_minimal_review_decision_action_plan_is_readonly(self) -> None:
        impact_report = {
            "schema": "ncs_ksa_term_ontology_impact_report_v1",
            "group_count": 2,
            "source_transition_penalty_concept_count": 2,
            "groups": [
                {
                    "normalized_ksa_term": "genericterm",
                    "representative_ksa_text": "Generic term",
                    "minimal_review_priority_score": 100,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "issue_count": 2,
                    "ksa_count": 2,
                    "unit_count": 2,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1, "issue_counts": {"short_ksa": 2}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 101,
                            "concept_name": "Generic concept",
                            "concept_type": "knowledge",
                            "review_status": "candidate",
                            "linked_penalty_rows": 2,
                            "linked_penalty_issues": {"short_ksa": 2},
                            "linked_penalty_courses": [],
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                },
                {
                    "normalized_ksa_term": "evidenceterm",
                    "representative_ksa_text": "Evidence term",
                    "minimal_review_priority_score": 95,
                    "minimal_review_priority_level": "critical_minimal_review",
                    "minimal_review_priority_reasons": ["linked_transition_penalty_concepts"],
                    "issue_count": 1,
                    "ksa_count": 1,
                    "unit_count": 1,
                    "major_count": 1,
                    "linked_penalized_concepts": {"concept_count": 1, "issue_counts": {"duplicate_text": 1}, "course_names": []},
                    "linked_penalized_concept_rows": [
                        {
                            "concept_id": 202,
                            "concept_name": "Evidence concept",
                            "concept_type": "skill",
                            "review_status": "candidate",
                            "linked_penalty_rows": 1,
                            "linked_penalty_issues": {"duplicate_text": 1},
                            "linked_penalty_courses": [],
                        }
                    ],
                    "top_concepts": [],
                    "samples": [],
                },
            ],
        }
        report = build_ksa_term_minimal_review_slice(impact_report)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            csv_path = tmp_path / "decision.csv"
            source_slice_path = tmp_path / "slice.json"
            source_jsonl_path = tmp_path / "slice.jsonl"
            source_manifest_path = tmp_path / "manifest.json"
            markdown_path = tmp_path / "plan.md"
            write_ksa_term_minimal_review_slice_csv(report, csv_path)
            source_slice_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            write_ksa_term_minimal_review_slice_jsonl(report, source_jsonl_path)
            source_manifest_path.write_text(
                json.dumps(
                    {
                        "schema": "ncs_ksa_term_review_workflow_manifest_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "human_decision_required": True,
                        "approval_claim": False,
                        "summary": {
                            "concept_review_group_count": 2,
                            "concept_review_csv_record_count": 2,
                        },
                        "artifacts": {
                            "minimal_review_slice": str(source_slice_path),
                            "minimal_review_jsonl": str(source_jsonl_path),
                        },
                        "safety_contract": {
                            "raw_source_mutation_allowed": False,
                            "trusted_status_write_allowed": False,
                            "status_update_allowed": False,
                            "db_writes": False,
                            "approval_claim": False,
                            "source_payload_exposed": False,
                            "preferred_decision_unit": "concept_review_group",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
                fieldnames = list(rows[0])
            rows[0]["decision"] = "downweight_generic_term"
            rows[0]["reviewer_id"] = "reviewer-1"
            rows[0]["reviewed_at"] = "2026-06-26T00:00:00Z"
            rows[0]["rationale"] = "Generic term dominates unrelated task evidence."
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            plan = build_ksa_term_minimal_review_decision_action_plan(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            write_ksa_term_minimal_review_decision_action_plan_markdown(plan, markdown_path)
            rows[0]["db_writes"] = "True"
            with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            unsafe_plan = build_ksa_term_minimal_review_decision_action_plan(
                csv_path,
                source_manifest_path=source_manifest_path,
                source_slice_path=source_slice_path,
            )
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["action_count"], 1)
        self.assertEqual(plan["pending_decision_count"], 1)
        self.assertEqual(plan["actions"][0]["decision"], "downweight_generic_term")
        self.assertEqual(plan["actions"][0]["suggested_decision"], "needs_more_evidence")
        self.assertEqual(plan["actions"][0]["suggested_decision_policy"], "review_assist_only_not_a_human_decision")
        self.assertEqual(plan["actions"][0]["operator_action"], "prepare_candidate_downweight_rule_for_generic_term")
        self.assertEqual(plan["actions"][0]["source_context"]["preferred_decision_unit"], "concept_review_group")
        self.assertFalse(plan["status_update_allowed"])
        self.assertFalse(plan["db_writes"])
        self.assertFalse(plan["approval_claim"])
        self.assertFalse(plan["trusted_status_write_allowed"])
        self.assertFalse(unsafe_plan["ok"])
        self.assertEqual(unsafe_plan["action_count"], 0)
        self.assertIn("db_writes_not_false", unsafe_plan["audit_errors"]["invalid_rows"][0]["errors"])
        self.assertIn("KSA Term Minimal Review Action Plan", markdown)
        self.assertIn("downweight_generic_term", markdown)

    def test_ksa_term_review_readiness_report_gates_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest_path = tmp_path / "workflow.json"
            audit_path = tmp_path / "audit.json"
            action_path = tmp_path / "action.json"
            markdown_path = tmp_path / "readiness.md"
            artifact_paths = {
                "manifest": manifest_path,
                "impact_report": tmp_path / "impact.json",
                "impact_markdown": tmp_path / "impact.md",
                "minimal_review_slice": tmp_path / "slice.json",
                "minimal_review_jsonl": tmp_path / "slice.jsonl",
                "minimal_review_csv": tmp_path / "slice.csv",
                "minimal_review_markdown": tmp_path / "slice.md",
            }
            for key, path in artifact_paths.items():
                path.write_text(f"{key}\n", encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ncs_ksa_term_review_workflow_manifest_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "summary": {
                            "concept_review_group_count": 1,
                            "decision_blank_count": 1,
                            "suggested_decision_counts": {"needs_more_evidence": 1},
                            "suggested_decision_confidence_counts": {"medium": 1},
                            "first_review_queue": [
                                {
                                    "concept_id": 10,
                                    "concept_name": "Planning",
                                    "suggested_decision": "needs_more_evidence",
                                    "suggested_decision_confidence": "medium",
                                    "max_priority_score": 100,
                                    "item_count": 1,
                                }
                            ],
                        },
                        "artifacts": {key: str(path) for key, path in artifact_paths.items()},
                        "safety_contract": {
                            "source_payload_exposed": False,
                            "trusted_status_write_allowed": False,
                            "preferred_decision_unit": "concept_review_group",
                            "suggested_decision_policy": "review_assist_only_not_a_human_decision",
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            audit_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ncs_ksa_term_minimal_review_decision_audit_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "source_manifest_match": True,
                        "source_slice_match": True,
                        "source_jsonl_match": True,
                        "source_payload_exposed": False,
                        "pending_decision_count": 1,
                        "completed_decision_count": 0,
                        "invalid_decision_count": 0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            action_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "ncs_ksa_term_minimal_review_decision_action_plan_v1",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "trusted_status_write_allowed": False,
                        "preferred_decision_unit": "concept_review_group",
                        "action_count": 0,
                        "pending_decision_count": 1,
                        "audit_summary": {
                            "source_payload_exposed": False,
                            "pending_decision_count": 1,
                            "completed_decision_count": 0,
                            "invalid_decision_count": 0,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_ksa_term_review_readiness_report(
                workflow_manifest_path=manifest_path,
                decision_audit_path=audit_path,
                action_plan_path=action_path,
            )
            write_ksa_term_review_readiness_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertTrue(report["ready_for_minimal_human_review"])
        self.assertFalse(report["ready_for_guarded_action_plan_review"])
        self.assertEqual(report["summary"]["pending_decision_count"], 1)
        self.assertEqual(report["summary"]["first_review_queue_count"], 1)
        self.assertEqual(report["next_step"], "fill_minimal_review_csv_decisions_for_first_review_queue")
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertEqual(report["failed_gates"], [])
        self.assertIn("KSA Term Review Readiness", markdown)
        self.assertIn("ready_for_minimal_human_review", markdown)

    def test_write_review_priority_markdown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "review_priority.md"
            write_review_priority_markdown(
                {
                    "ok": True,
                    "schema": "ncs_review_priority_v1",
                    "issue_types": ["criteria_format_issue"],
                    "open_issue_counts": [
                        {"issue_type": "criteria_format_issue", "severity": "warning", "count": 1}
                    ],
                    "top_items": [
                        {
                            "priority_score": 80,
                            "priority_reason": "Criteria text quality affects task evidence shown to users.",
                            "issue": {
                                "issue_id": 1,
                                "issue_type": "criteria_format_issue",
                                "target_type": "criteria",
                                "target_id": "10",
                                "issue_detail": "Malformed criteria text",
                                "suggested_action": "Review refined criteria",
                            },
                            "context": {"criteria_text_raw": "."},
                        }
                    ],
                    "next_actions": ["Review"],
                },
                out_path,
            )

            text = out_path.read_text(encoding="utf-8")

        self.assertIn("# NCS Review Priority", text)
        self.assertIn("schema: ncs_review_priority_v1", text)
        self.assertIn("criteria_format_issue #1", text)
        self.assertIn("Malformed criteria text", text)


if __name__ == "__main__":
    unittest.main()
