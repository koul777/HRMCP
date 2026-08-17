from __future__ import annotations

import asyncio
import hashlib
import os
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from ksa_label_codex_judge import evaluate_candidate

from ncs_mcp.db import (
    build_ksa_label_candidates,
    build_ksa_meaning_candidates,
    compact_ksa_representative_label,
    connect,
    initialize_database,
    ksa_label_quality_flags,
    machine_review_ksa_meaning_candidates,
    now_utc,
    prepare_ontology_human_review_queue,
    preprocess_ksa_atomic_items,
    recommend_task_transitions as recommend_task_transitions_from_db,
    resolve_task_criteria,
    split_ksa_atomic_text,
)
from ncs_mcp.ksa_label_report import (
    build_ksa_label_candidate_report,
    build_ksa_label_review_seedpack,
    write_ksa_label_candidate_report_markdown,
    write_ksa_label_review_seedpack_csv,
    write_ksa_label_review_seedpack_jsonl,
)
from ncs_mcp.career_path import career_path_summary, import_career_paths_csv
from ncs_mcp.query_router import route_ncs_query
from ncs_mcp.smoke_data import create_ready_smoke_db
from ncs_mcp.training_course_api import parse_training_course_xml, upsert_training_courses
from ncs_mcp.training_recommendation import (
    BROAD_GENERIC_KSA_MIN_MAJOR_COUNT,
    BROAD_GENERIC_KSA_PENALTY,
    DIRECT_UNIT_DIVERSITY_BYPASS_SCORE,
    DUPLICATE_KSA_PENALTY,
    SHORT_KSA_PENALTY,
    TRUSTED_TRANSITION_REVIEW_STATUSES,
    _apply_query_alias,
    _attach_query_normalization,
    _candidate_allows_edit_distance,
    _candidate_score,
    _concept_quality_issue_penalty_map,
    _course_candidate_sort_key,
    _course_delivery_relations,
    _diversify_top_k_candidates,
    _generic_job_query_normalization,
    _preference_fit_profile,
    _preference_time_adjustment,
    _recommendation_tier,
    _resolution_classification_filters,
    _significant_tokens,
    _task_concepts,
    _transition_case_course_evidence,
    _training_system_guide_trace,
    _with_korean_direction_particle,
    USABLE_REVIEW_STATUS_WEIGHTS,
    build_training_course_ontology_links,
    compact_ncs_education_plan_response,
    compact_training_task_response,
    compact_training_transition_response,
    evaluate_training_transition_scenarios,
    generate_training_transition_eval_scenarios,
    recommend_training_for_task,
    recommend_training_transition,
    review_training_transition_scenarios,
    resolve_ncs_query_scope,
    search_training_courses,
)


def seed_task_ontology(conn: sqlite3.Connection) -> dict[str, int | str]:
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
            unit_level_raw, classification_id, api_definition,
            api_match_status, created_at, updated_at
        ) VALUES ('0202020101_23v3', '0202020101', '23v3', 'HR planning',
                  '5', ?, 'Plan workforce and HR strategy.', 'matched', ?, ?)
        """,
        (classification_id, timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO competency_elements(
            unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
        ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', 'Plan workforce', '5')
        """
    )
    element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
    conn.execute(
        """
        INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
        VALUES (?, '1', 'Build a workforce plan from business strategy.')
        """,
        (element_id,),
    )
    criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()["criteria_id"]
    conn.execute(
        """
        INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
        VALUES (?, '01', 'knowledge', '1', 'workforce planning')
        """,
        (element_id,),
    )
    ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
    conn.execute(
        """
        INSERT INTO raw_excel_rows(
            source_file, sheet_name, sheet_row_number,
            major_code, major_name, middle_code, middle_name,
            small_code, small_name, sub_code, sub_name,
            unit_code, unit_name, unit_level,
            element_code, element_name, element_level,
            criteria_no, criteria_text,
            ksa_type_code, ksa_type_name, ksa_no, ksa_text,
            loaded_at
        ) VALUES (
            'test.xlsx', 'Sheet1', 1,
            '02', 'Business', '02', 'HR',
            '02', 'HRM', '01', 'HR planning',
            '0202020101_23v3', 'HR planning', '5',
            '0202020101_23v3 1', 'Plan workforce', '5',
            '1', 'Build a workforce plan from business strategy.',
            '01', 'knowledge', '1', 'workforce planning',
            ?
        )
        """,
        (timestamp,),
    )
    raw_row_id = conn.execute("SELECT raw_row_id FROM raw_excel_rows").fetchone()["raw_row_id"]
    conn.execute(
        """
        INSERT INTO element_criteria_ksa_links(raw_row_id, element_id, criteria_id, ksa_id)
        VALUES (?, ?, ?, ?)
        """,
        (raw_row_id, element_id, criteria_id, ksa_id),
    )
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
    concept_id = conn.execute("SELECT concept_id FROM ontology_concepts").fetchone()["concept_id"]
    conn.execute(
        "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
        (ksa_id, concept_id, timestamp),
    )
    conn.execute(
        """
        INSERT INTO ksa_atomic_items(
            ksa_id, element_id, ksa_type_name, atom_index, atom_text,
            normalized_key, split_method, review_status, created_at
        ) VALUES (?, ?, 'knowledge', 0, 'workforce planning', 'workforceplanning',
                  'test', 'raw', ?)
        """,
        (ksa_id, element_id, timestamp),
    )
    atomic_id = conn.execute("SELECT atomic_id FROM ksa_atomic_items").fetchone()["atomic_id"]
    conn.execute(
        "INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
        (atomic_id, concept_id, timestamp),
    )
    conn.execute(
        """
        INSERT INTO ksa_meaning_candidates(
            concept_id, concept_type, meaning_role, meaning_text,
            source_method, evidence_text, unit_code, element_id,
            criteria_id, ksa_id, confidence_score, review_status,
            created_at, updated_at
        ) VALUES (?, 'knowledge', 'task_knowledge_significance',
                  'workforce planning supports HR planning decisions.',
                  'test', 'test evidence', '0202020101_23v3', ?, ?, ?,
                  0.9, 'candidate', ?, ?)
        """,
        (concept_id, element_id, criteria_id, ksa_id, timestamp, timestamp),
    )
    conn.execute(
        """
        UPDATE ontology_concepts
        SET definition = 'workforce planning supports HR planning decisions.',
            definition_source = 'test',
            definition_status = 'candidate',
            review_status = 'model_preprocessed'
        WHERE concept_id = ?
        """,
        (concept_id,),
    )
    conn.execute(
        """
        INSERT INTO ontology_concepts(
            concept_name, normalized_key, concept_type, definition,
            definition_source, definition_status, relation_status,
            review_status, created_at, updated_at
        ) VALUES ('workforce analysis', 'workforceanalysis', 'skill',
                  'workforce analysis executes the plan.',
                  'test', 'candidate', 'linked', 'model_preprocessed', ?, ?)
        """,
        (timestamp, timestamp),
    )
    skill_concept_id = conn.execute(
        "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'workforceanalysis'"
    ).fetchone()["concept_id"]
    conn.execute(
        """
        INSERT INTO ontology_concept_relations(
            source_concept_id, relation_type, target_concept_id,
            relation_label, review_status, created_at
        ) VALUES (?, 'knowledge_enables_skill', ?, 'knowledge enables analysis', 'candidate', ?)
        """,
        (concept_id, skill_concept_id, timestamp),
    )
    return {
        "unit_code": "0202020101_23v3",
        "ksa_id": ksa_id,
        "atomic_id": atomic_id,
        "criteria_id": criteria_id,
        "concept_id": concept_id,
        "skill_concept_id": skill_concept_id,
    }


class TrainingRecommendationTests(unittest.TestCase):
    def test_korean_direction_particle_handles_batchim_and_rieul(self) -> None:
        self.assertEqual(_with_korean_direction_particle("인사기획"), "인사기획으로")
        self.assertEqual(_with_korean_direction_particle("총무"), "총무로")
        self.assertEqual(_with_korean_direction_particle("관리"), "관리로")
        self.assertEqual(_with_korean_direction_particle("HR planning"), "HR planning로")

    def test_llm_reviewed_review_status_weight_sits_between_candidate_and_human(self) -> None:
        self.assertGreater(USABLE_REVIEW_STATUS_WEIGHTS["llm_reviewed"], USABLE_REVIEW_STATUS_WEIGHTS["candidate"])
        self.assertLess(
            USABLE_REVIEW_STATUS_WEIGHTS["llm_reviewed"],
            USABLE_REVIEW_STATUS_WEIGHTS["human_reviewed"],
        )

    def test_training_goal_links_use_korean_tokens_with_particles(self) -> None:
        self.assertIn("운영", _significant_tokens("운영과"))
        self.assertIn("인사", _significant_tokens("인사 운영 관리"))
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES (?, '인사운영관리', 'knowledge',
                          'missing', 'unlinked', 'raw', ?, ?)
                """,
                ("인사 운영 관리", timestamp, timestamp),
            )
            korean_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = '인사운영관리'"
            ).fetchone()["concept_id"]
            element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = ?",
                (fixture["unit_code"],),
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '2', ?)
                """,
                (element_id, "인사 운영 관리"),
            )
            korean_ksa_id = conn.execute(
                "SELECT ksa_id FROM ksa_items WHERE ksa_text_raw = ?",
                ("인사 운영 관리",),
            ).fetchone()["ksa_id"]
            conn.execute(
                "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                (korean_ksa_id, korean_concept_id, timestamp),
            )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "인사 운영과 현장 기준을 학습한다.",
                        "train_time": "8",
                        "fac_name": "HR center",
                        "meth_name": "Classroom",
                    }
                ],
            )

            build_training_course_ontology_links(conn)
            row = conn.execute(
                """
                SELECT tgcl.link_method
                FROM training_goal_concept_links tgcl
                JOIN ontology_concepts oc ON oc.concept_id = tgcl.concept_id
                WHERE oc.concept_name = ?
                """,
                ("인사 운영 관리",),
            ).fetchone()
            conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["link_method"], "training_goal_concept_token")

    def test_short_ksa_quality_issue_penalizes_recommendation_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            before = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO quality_issues(
                    target_type, target_id, issue_type, severity,
                    issue_detail, suggested_action, detected_at
                ) VALUES ('ksa', ?, 'short_ksa', 'info', 'short fixture', 'review', ?)
                """,
                (str(fixture["ksa_id"]), timestamp),
            )
            after = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            conn.close()

        before_item = before["recommendations"][0]
        after_item = after["recommendations"][0]
        self.assertTrue(before["ok"])
        self.assertTrue(after["ok"])
        self.assertNotIn("quality_issue_ksa_penalty", before_item["match"]["reasons"])
        self.assertIn("quality_issue_ksa_penalty", after_item["match"]["reasons"])
        self.assertNotIn("distant_scope_quality_penalty_stack", after_item["match"]["reasons"])
        self.assertEqual(after_item["match"]["quality_issue_penalty"]["issue_types"], ["short_ksa"])
        self.assertEqual(after_item["match"]["quality_issue_penalty"]["multiplier"], SHORT_KSA_PENALTY)
        self.assertAlmostEqual(
            after_item["score_components"]["final_score"],
            round(before_item["score_components"]["final_score"] * SHORT_KSA_PENALTY, 4),
        )
        self.assertLess(after_item["confidence_score"], before_item["confidence_score"])

    def test_same_as_noncanonical_concept_penalizes_recommendation_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            before = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('workforce planning canonical', 'workforceplanningcanonical',
                          'knowledge', 'candidate', 'linked', 'raw', ?, ?)
                """,
                (timestamp, timestamp),
            )
            canonical_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'workforceplanningcanonical'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ontology_concept_relations(
                    source_concept_id, relation_type, target_concept_id,
                    relation_label, review_status, created_at
                ) VALUES (?, 'same_as', ?, 'duplicate_normalized_key', 'candidate', ?)
                """,
                (fixture["concept_id"], canonical_id, timestamp),
            )
            after = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            conn.close()

        before_item = before["recommendations"][0]
        after_item = after["recommendations"][0]
        self.assertTrue(before["ok"])
        self.assertTrue(after["ok"])
        self.assertNotIn("quality_issue_ksa_penalty", before_item["match"]["reasons"])
        self.assertIn("quality_issue_ksa_penalty", after_item["match"]["reasons"])
        self.assertEqual(after_item["match"]["quality_issue_penalty"]["issue_types"], ["duplicate_text"])
        self.assertEqual(
            after_item["match"]["quality_issue_penalty"]["multiplier"],
            DUPLICATE_KSA_PENALTY,
        )
        self.assertAlmostEqual(
            after_item["score_components"]["final_score"],
            round(before_item["score_components"]["final_score"] * DUPLICATE_KSA_PENALTY, 4),
        )

    def test_duplicate_text_quality_issue_only_penalizes_noncanonical_concepts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO quality_issues(
                    target_type, target_id, issue_type, severity,
                    issue_detail, suggested_action, detected_at
                ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'duplicate fixture', 'review', ?)
                """,
                (str(fixture["ksa_id"]), timestamp),
            )
            without_same_as = _concept_quality_issue_penalty_map(
                conn,
                {int(fixture["concept_id"])},
            )
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('workforce planning canonical', 'workforceplanningcanonical',
                          'knowledge', 'candidate', 'linked', 'raw', ?, ?)
                """,
                (timestamp, timestamp),
            )
            canonical_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'workforceplanningcanonical'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ontology_concept_relations(
                    source_concept_id, relation_type, target_concept_id,
                    relation_label, review_status, created_at
                ) VALUES (?, 'same_as', ?, 'duplicate_normalized_key', 'candidate', ?)
                """,
                (fixture["concept_id"], canonical_id, timestamp),
            )
            with_same_as = _concept_quality_issue_penalty_map(
                conn,
                {int(fixture["concept_id"])},
            )
            conn.close()

        self.assertEqual(without_same_as, {})
        self.assertEqual(
            with_same_as[int(fixture["concept_id"])][0]["penalty_multiplier"],
            DUPLICATE_KSA_PENALTY,
        )
        self.assertEqual(
            with_same_as[int(fixture["concept_id"])][0]["issue_type"],
            "duplicate_text",
        )

    def test_broad_duplicate_ksa_penalizes_generic_concept_without_same_as(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            before = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            timestamp = now_utc()
            for index in range(BROAD_GENERIC_KSA_MIN_MAJOR_COUNT):
                major_code = f"{30 + index:02d}"
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES (?, ?, '01', 'M', '01', 'S', '01', 'Sub')
                    """,
                    (major_code, f"Major {major_code}"),
                )
                classification_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                unit_code = f"{major_code}01010101_25v1"
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES (?, ?, '25v1', ?, '3', ?, ?, ?)
                    """,
                    (unit_code, unit_code.split("_")[0], f"Unit {major_code}", classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES (?, '1', ?, ?, '3')
                    """,
                    (unit_code, f"E{index}", f"Element {major_code}"),
                )
                element_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, '01', 'knowledge', '1', 'Generic compliance')
                    """,
                    (element_id,),
                )
                ksa_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, created_at) VALUES (?, ?, ?)",
                    (ksa_id, fixture["concept_id"], timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO quality_issues(
                        target_type, target_id, issue_type, severity,
                        issue_detail, suggested_action, detected_at
                    ) VALUES ('ksa', ?, 'duplicate_text', 'info', 'broad duplicate fixture', 'review', ?)
                    """,
                    (str(ksa_id), timestamp),
                )
            penalty_map = _concept_quality_issue_penalty_map(conn, {int(fixture["concept_id"])})
            after = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            conn.close()

        before_item = before["recommendations"][0]
        after_item = after["recommendations"][0]
        self.assertTrue(before["ok"])
        self.assertTrue(after["ok"])
        self.assertEqual(
            penalty_map[int(fixture["concept_id"])][0]["issue_type"],
            "broad_generic_ksa",
        )
        self.assertEqual(
            penalty_map[int(fixture["concept_id"])][0]["penalty_multiplier"],
            BROAD_GENERIC_KSA_PENALTY,
        )
        self.assertIn("quality_issue_ksa_penalty", after_item["match"]["reasons"])
        self.assertEqual(after_item["match"]["quality_issue_penalty"]["issue_types"], ["broad_generic_ksa"])
        self.assertEqual(after_item["match"]["quality_issue_penalty"]["multiplier"], BROAD_GENERIC_KSA_PENALTY)
        self.assertEqual(
            after_item["match"]["quality_issue_penalty"]["concept_issue_types"],
            {str(fixture["concept_id"]): ["broad_generic_ksa"]},
        )
        self.assertAlmostEqual(
            after_item["score_components"]["final_score"],
            round(before_item["score_components"]["final_score"] * BROAD_GENERIC_KSA_PENALTY, 4),
        )

    def test_task_concepts_do_not_pull_other_elements_from_same_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            source_element_id = conn.execute(
                "SELECT element_id FROM performance_criteria WHERE criteria_id = ?",
                (fixture["criteria_id"],),
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '2', 'Review payroll without linked KSA.')
                """,
                (source_element_id,),
            )
            same_element_unlinked_criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria WHERE criteria_no = '2'"
            ).fetchone()["criteria_id"]
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES (?, '2', '0202020101_23v3 2', 'Run payroll', '5')
                """,
                (fixture["unit_code"],),
            )
            other_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE element_no = '2'"
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', 'Run payroll work.')
                """,
                (other_element_id,),
            )
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'payroll rules')
                """,
                (other_element_id,),
            )
            other_ksa_id = conn.execute(
                "SELECT ksa_id FROM ksa_items WHERE ksa_text_raw = 'payroll rules'"
            ).fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('payroll rules', 'payrollrules', 'knowledge',
                          'missing', 'unlinked', 'raw', ?, ?)
                """,
                (timestamp, timestamp),
            )
            other_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'payrollrules'"
            ).fetchone()["concept_id"]
            conn.execute(
                "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                (other_ksa_id, other_concept_id, timestamp),
            )
            concepts = _task_concepts(conn, int(fixture["criteria_id"]), limit=20)
            same_element_unlinked_concepts = _task_concepts(conn, int(same_element_unlinked_criteria_id), limit=20)
            conn.close()

        concept_ids = {int(item["concept_id"]) for item in concepts}
        unlinked_concept_ids = {int(item["concept_id"]) for item in same_element_unlinked_concepts}
        self.assertIn(int(fixture["concept_id"]), concept_ids)
        self.assertNotIn(int(other_concept_id), concept_ids)
        self.assertNotIn(int(fixture["concept_id"]), unlinked_concept_ids)

    def test_split_ksa_atomic_text_splits_list_patterns_without_parenthetical_commas(self) -> None:
        self.assertEqual(len(split_ksa_atomic_text("\uadfc\ub85c\uae30\uc900\ubc95, \ucd5c\uc800\uc784\uae08\ubc95, \uc0b0\uc5c5\uc548\uc804\ubcf4\uac74\ubc95\uc5d0 \uad00\ud55c \uc9c0\uc2dd")), 3)
        self.assertEqual(len(split_ksa_atomic_text("\ubb38\uc11c \uc791\uc131 \ub2a5\ub825 \ubc0f \uc758\uc0ac\uc18c\ud1b5 \uae30\uc220")), 2)
        self.assertEqual(len(split_ksa_atomic_text("Python, Java \ub610\ub294 C++ \ud504\ub85c\uadf8\ub798\ubc0d \uae30\uc220")), 3)
        self.assertEqual(
            split_ksa_atomic_text(
                "\ucc28\ub2e8 \ubc29\uc2dd(\uac1c\ud3d0\uae30, \ucc28\ub2e8\uae30)\uc5d0 \uad00\ud55c \uc9c0\uc2dd"
            ),
            [
                "\ucc28\ub2e8 \ubc29\uc2dd(\uac1c\ud3d0\uae30, \ucc28\ub2e8\uae30)\uc5d0 \uad00\ud55c \uc9c0\uc2dd"
            ],
        )
        self.assertEqual(
            split_ksa_atomic_text("\ud55c\uad6d\uc758 \ud611\ub825\ub300\uc0c1\uad6d \uc9c0\uc6d0 \uc2e4\uc801 \ubc0f \uc0ac\ub840\ud604\ud669"),
            ["\ud55c\uad6d\uc758 \ud611\ub825\ub300\uc0c1\uad6d \uc9c0\uc6d0 \uc2e4\uc801 \ubc0f \uc0ac\ub840\ud604\ud669"],
        )
        self.assertEqual(
            split_ksa_atomic_text("\ubc30\uad00, \uc6a9\uc811 \uae30\uc220"),
            ["\ubc30\uad00, \uc6a9\uc811 \uae30\uc220"],
        )
        self.assertEqual(
            split_ksa_atomic_text(
                "\uc18c\ubc29\uc2dc\uc124 \uc124\uce58\u00b7\uc720\uc9c0 \ubc0f \uc548\uc804\uad00\ub9ac\uc5d0 \uad00\ud55c \ubc95\ub960"
            ),
            [
                "\uc18c\ubc29\uc2dc\uc124 \uc124\uce58\u00b7\uc720\uc9c0 \ubc0f \uc548\uc804\uad00\ub9ac\uc5d0 \uad00\ud55c \ubc95\ub960"
            ],
        )

    def test_preprocess_ksa_atomic_items_records_list_split_method(self) -> None:
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
                    ) VALUES ('02', 'Business', '02', 'HR', '02', 'People', '01', 'Planning')
                    """
                )
                classification_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0202020101_26v1', '0202020101', '26v1', 'Planning', '4', ?, ?, ?)
                    """,
                    (classification_id, timestamp, timestamp),
                )
                cur = conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw,
                        element_name_raw, element_level_raw
                    ) VALUES ('0202020101_26v1', '1', '01', 'Plan work', '4')
                    """
                )
                element_id = cur.lastrowid
                conn.execute(
                    """
                    INSERT INTO ksa_items(
                        element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw
                    ) VALUES (?, 'S', 'skill', '1', ?)
                    """,
                    (element_id, "Python, Java \ub610\ub294 C++ \ud504\ub85c\uadf8\ub798\ubc0d \uae30\uc220"),
                )
                conn.commit()
                result = preprocess_ksa_atomic_items(conn, reset=True)
                rows = conn.execute(
                    """
                    SELECT atom_index, atom_text, split_method
                    FROM ksa_atomic_items
                    ORDER BY atom_index
                    """
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(result["ksa_processed"], 1)
        self.assertEqual(result["atoms_generated_in_run"], 3)
        self.assertEqual(
            [(row["atom_index"], row["atom_text"], row["split_method"]) for row in rows],
            [
                (1, "Python \uae30\uc220", "rule_based_comma_split"),
                (2, "Java \uae30\uc220", "rule_based_comma_split"),
                (3, "C++ \ud504\ub85c\uadf8\ub798\ubc0d \uae30\uc220", "rule_based_comma_split"),
            ],
        )

    def test_preprocess_ksa_atomic_items_reset_purges_llm_reviewed_label_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                fixture = seed_task_ontology(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                        concept_type, source_text, label_text, normalized_label_key,
                        label_role, source_method, candidate_rank, confidence_score,
                        review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, '02:02:02:01', 'knowledge',
                              'workforce planning', 'workforce planning',
                              'workforceplanning', 'short_representative_label',
                              'already_short_label', 1, 0.88, 'llm_reviewed', ?, ?)
                    """,
                    (
                        fixture["concept_id"],
                        fixture["ksa_id"],
                        fixture["atomic_id"],
                        timestamp,
                        timestamp,
                    ),
                )
                conn.commit()
                result = preprocess_ksa_atomic_items(conn, reset=True)
                label_count = conn.execute(
                    "SELECT COUNT(*) FROM ontology_concept_label_candidates"
                ).fetchone()[0]
                atomic_count = conn.execute("SELECT COUNT(*) FROM ksa_atomic_items").fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(result["ksa_processed"], 1)
        self.assertEqual(atomic_count, 1)
        self.assertEqual(label_count, 0)

    def test_preprocess_ksa_atomic_items_reset_blocks_human_reviewed_task_relations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                fixture = seed_task_ontology(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, confidence_score, review_status, created_at
                    ) VALUES (?, (SELECT element_id FROM ksa_items WHERE ksa_id = ?),
                              ?, 'knowledge_enables_skill', ?, ?, ?,
                              'human checked relation', 0.95, 'human_reviewed', ?)
                    """,
                    (
                        fixture["criteria_id"],
                        fixture["ksa_id"],
                        fixture["concept_id"],
                        fixture["skill_concept_id"],
                        fixture["atomic_id"],
                        fixture["atomic_id"],
                        timestamp,
                    ),
                )
                conn.commit()
                with self.assertRaisesRegex(RuntimeError, "human-reviewed task KSA relations"):
                    preprocess_ksa_atomic_items(conn, reset=True)
                relation_count = conn.execute(
                    "SELECT COUNT(*) FROM task_ksa_concept_relations"
                ).fetchone()[0]
            finally:
                conn.close()

        self.assertEqual(relation_count, 1)

    def test_ksa_label_codex_judge_rejects_missing_provenance(self) -> None:
        ok, evidence = evaluate_candidate(
            {
                "source_text": "workforce planning source",
                "label_text": "workforce planning",
                "concept_type": "knowledge",
                "source_ksa_id": None,
                "source_atomic_id": None,
                "source_scope_key": "02:02:02:01",
            },
            min_ratio=0.35,
            max_ratio=0.98,
        )
        self.assertFalse(ok)
        self.assertIn("missing_source_provenance", evidence["skip_reasons"])

    def test_compact_ksa_representative_label_shortens_long_korean_phrases(self) -> None:
        candidate = compact_ksa_representative_label(
            "국제기구, 양자원조기구, NGO 등의 협력대상국에 대한 개발정책과 지원현황",
            "knowledge",
        )
        self.assertEqual(candidate["label_text"], "협력대상국 개발정책 및 지원현황")
        self.assertEqual(candidate["source_method"], "rule_based_short_label_candidate")
        self.assertTrue(candidate["changed"])

        skill = compact_ksa_representative_label("외국어 의사소통 능력", "skill")
        self.assertEqual(skill["label_text"], "외국어 의사소통")

        meeting_skill = compact_ksa_representative_label("회의 진행 기술", "skill")
        self.assertEqual(meeting_skill["label_text"], "회의 진행")

        consultation_skill = compact_ksa_representative_label("협의 진행 능력", "skill")
        self.assertEqual(consultation_skill["label_text"], "협의 진행")

        possessive_knowledge = compact_ksa_representative_label("데이터의 종류 및 특성", "knowledge")
        self.assertEqual(possessive_knowledge["label_text"], "데이터 종류 및 특성")

        conjunction_protected = compact_ksa_representative_label("통합적 성과 분석 기법", "knowledge")
        self.assertEqual(conjunction_protected["label_text"], "통합적 성과 분석 기법")

        attached_conjunction_protected = compact_ksa_representative_label("평가결과 보고서 작성기술", "skill")
        self.assertEqual(attached_conjunction_protected["label_text"], "평가결과 보고서 작성")

        generic_skill = compact_ksa_representative_label("기술 능력", "skill")
        self.assertEqual(generic_skill["label_text"], "기술 능력")

        generic_collapse = compact_ksa_representative_label(
            "근대 건축물 이전, 해체, 복원 등의 기술",
            "skill",
        )
        self.assertEqual(generic_collapse["label_text"], "근대 건축물 이전, 해체, 복원 등의 기술")
        self.assertEqual(generic_collapse["source_method"], "already_short_label")

        parenthetical = compact_ksa_representative_label(
            "STP(Segmentation, Targeting, Positioning) 전략 수립 절차 이해",
            "knowledge",
        )
        self.assertEqual(parenthetical["label_text"], "STP 전략 수립 절차")
        self.assertNotIn("unbalanced_parentheses", ksa_label_quality_flags(parenthetical["label_text"], parenthetical["label_text"]))
        self.assertNotIn("dangling_enum_suffix", ksa_label_quality_flags(parenthetical["label_text"], parenthetical["label_text"]))

        knowledge_suffix = compact_ksa_representative_label(
            "스마트기술(ICBMA)에 관한 지식",
            "knowledge",
        )
        self.assertEqual(knowledge_suffix["label_text"], "스마트기술")
        self.assertIn("drop_knowledge_suffix", knowledge_suffix["method_details"])

        near_full_knowledge = compact_ksa_representative_label(
            "현금흐름에 영향을 미치는 계정과목",
            "knowledge",
        )
        self.assertEqual(near_full_knowledge["label_text"], "현금흐름 영향 계정과목")

        required_method = compact_ksa_representative_label(
            "모집을 활성화 할 수 있는 방법",
            "knowledge",
        )
        self.assertEqual(required_method["label_text"], "모집 활성화 방법")

        method_skill = compact_ksa_representative_label(
            "직무설계를 하는 방법",
            "skill",
        )
        self.assertEqual(method_skill["label_text"], "직무설계 방법")

        flexible_attitude = compact_ksa_representative_label(
            "다양한 상황에 대처하는 유연한 자세",
            "attitude",
        )
        self.assertEqual(flexible_attitude["label_text"], "다양한 상황 대처 유연한 태도")

        creative_attitude = compact_ksa_representative_label(
            "발상을 전환하는 창의적인 태도",
            "attitude",
        )
        self.assertEqual(creative_attitude["label_text"], "발상 전환 창의적 태도")

        reduction_attitude = compact_ksa_representative_label(
            "원가절감을 하려는 의지",
            "attitude",
        )
        self.assertEqual(reduction_attitude["label_text"], "원가절감 태도")

        bidding_method = compact_ksa_representative_label(
            "개별 입찰공고에서 규정하는 낙찰자 결정방법",
            "knowledge",
        )
        self.assertEqual(bidding_method["label_text"], "개별 입찰공고 규정 낙찰자 결정방법")

        required_document = compact_ksa_representative_label(
            "운용사에서 필요로 하는 법적문서 유형",
            "knowledge",
        )
        self.assertEqual(required_document["label_text"], "운용사 필요 법적문서 유형")

        communication_skill = compact_ksa_representative_label(
            "개선사항을 정리하는데 필요한 문서작성 능력과 의사소통 능력",
            "skill",
        )
        self.assertEqual(communication_skill["label_text"], "개선사항 정리 필요 문서작성 및 의사소통")

        result_attitude = compact_ksa_representative_label(
            "프로젝트 활동에 있어서 적절한 자원, 도구 및 기법을 사용하여 기대하는 결과를 도출하는 태도",
            "attitude",
        )
        self.assertEqual(
            result_attitude["label_text"],
            "프로젝트 활동에 적절한 자원, 도구 및 기법 사용 기대하는 결과 도출 태도",
        )

        detail_attitude = compact_ksa_representative_label(
            "계약서 작성 및 체결의 오류를 배제하는 세밀한 태도",
            "attitude",
        )
        self.assertEqual(detail_attitude["label_text"], "계약서 작성 및 체결 오류 배제 세밀한 태도")

        social_attitude = compact_ksa_representative_label(
            "다양한 사회적 관점을 바라볼 수 있는 태도",
            "attitude",
        )
        self.assertEqual(social_attitude["label_text"], "다양한 사회적 관점 관찰 태도")

        responsibility_attitude = compact_ksa_representative_label(
            "과정과 결과에 책임질 수 있는 책임감",
            "attitude",
        )
        self.assertEqual(responsibility_attitude["label_text"], "과정 및 결과 책임 태도")

        persuasion_skill = compact_ksa_representative_label(
            "정밀 검색자와 보안검색 거부자를 검색에 응하게 하는 설득 능력",
            "skill",
        )
        self.assertEqual(persuasion_skill["label_text"], "정밀 검색자 및 보안검색 거부자 검색 응대 설득")

        leadership_skill = compact_ksa_representative_label(
            "교육생을 이끌어갈 수 있는 리더쉽",
            "skill",
        )
        self.assertEqual(leadership_skill["label_text"], "교육생 지도 리더쉽")

        together_skill = compact_ksa_representative_label(
            "아이와 함께 하는 다양한 놀이능력",
            "skill",
        )
        self.assertEqual(together_skill["label_text"], "아이와 함께하는 다양한 놀이")

        participant_role = compact_ksa_representative_label(
            "투자상품 출시에 참여하는 유관기관의 역할",
            "knowledge",
        )
        self.assertEqual(participant_role["label_text"], "투자상품 출시 참여 유관기관 역할")

        fixed_asset_reason = compact_ksa_representative_label(
            "고정자산 취득명세서를 작성해야 하는 사유",
            "knowledge",
        )
        self.assertEqual(fixed_asset_reason["label_text"], "고정자산 취득명세서 작성 필요 사유")

        hazard_knowledge = compact_ksa_representative_label(
            "취급하는 위험물의 위험성",
            "knowledge",
        )
        self.assertEqual(hazard_knowledge["label_text"], "취급 위험물 위험성")

        fire_facility_knowledge = compact_ksa_representative_label(
            "전기저장시설에 설치하는 소방시설의 구조·원리",
            "knowledge",
        )
        self.assertEqual(fire_facility_knowledge["label_text"], "전기저장시설 설치 소방시설 구조·원리")

        selection_attitude = compact_ksa_representative_label(
            "적합한 평가방법을 선택하는 분석적 태도",
            "attitude",
        )
        self.assertEqual(selection_attitude["label_text"], "적합한 평가방법 선택 분석적 태도")

        compliance_attitude = compact_ksa_representative_label(
            "원칙을 준수하는 공정한 자세",
            "attitude",
        )
        self.assertEqual(compliance_attitude["label_text"], "원칙 준수 공정한 태도")

        coordination_attitude = compact_ksa_representative_label(
            "상반된 의견 차이를 조율하는 적극적인 협상 태도",
            "attitude",
        )
        self.assertEqual(coordination_attitude["label_text"], "상반된 의견 차이 조율 적극적인 협상 태도")

        flexible_acceptance = compact_ksa_representative_label(
            "경쟁사의 장점을 받아들일 수 있는 유연한 태도",
            "attitude",
        )
        self.assertEqual(flexible_acceptance["label_text"], "경쟁사 장점 수용 유연한 태도")

        inclusive_analysis = compact_ksa_representative_label(
            "내담자의 특성 분류와 경력지도 항목을 구체적으로 나눌 수 있는 분석적 사고",
            "attitude",
        )
        self.assertEqual(inclusive_analysis["label_text"], "내담자 특성 분류 및 경력지도 항목 분류 분석적 사고")

        preparation_attitude = compact_ksa_representative_label(
            "예상치 못한 문제발생을 예측하는 준비자세",
            "attitude",
        )
        self.assertEqual(preparation_attitude["label_text"], "예상치 못한 문제발생 예측 준비 태도")

        compliance_no_space = compact_ksa_representative_label(
            "규정을 준수하려는자세",
            "attitude",
        )
        self.assertEqual(compliance_no_space["label_text"], "규정 준수 태도")

        fact_attitude = compact_ksa_representative_label(
            "복잡 다양한 사실을 분석하고 정리해 낼 수 있는 태도",
            "attitude",
        )
        self.assertEqual(fact_attitude["label_text"], "복잡 다양한 사실 분석 및 정리 태도")

        photo_mechanism = compact_ksa_representative_label(
            "조리개, 셔터 속도, 감도 등 노출을 조절하는 사진 메커니즘",
            "knowledge",
        )
        self.assertEqual(photo_mechanism["label_text"], "조리개, 셔터 속도, 감도 등 노출 조절 사진 메커니즘")

        production_skill = compact_ksa_representative_label(
            "촬영 용이성을 바탕으로 하는 제작체계의 기술력",
            "skill",
        )
        self.assertEqual(production_skill["label_text"], "촬영 용이성 기반 제작체계 기술력")

        morale_skill = compact_ksa_representative_label(
            "조직의 목표와 직원의 사기를 높일 수 있는 제도 마련",
            "skill",
        )
        self.assertEqual(morale_skill["label_text"], "조직 목표 및 직원 사기 향상 제도 마련")

        related_law = compact_ksa_representative_label(
            "관련법규(건설기술진흥법, 산업안전보건법 등)",
            "knowledge",
        )
        self.assertEqual(related_law["label_text"], "건설기술진흥법·산업안전보건법 등 관련 법규")
        self.assertIn("compact_related_law_parenthetical", related_law["method_details"])
        self.assertNotIn(
            "generic_or_low_specificity",
            ksa_label_quality_flags(
                "관련법규(건설기술진흥법, 산업안전보건법 등)",
                related_law["label_text"],
                "knowledge",
            ),
        )
        self.assertNotIn(
            "very_low_label_source_ratio",
            ksa_label_quality_flags(
                "관련법규(건설기술진흥법, 산업안전보건법 등)",
                related_law["label_text"],
                "knowledge",
            ),
        )
        self.assertNotIn(
            "changed_near_full_length",
            ksa_label_quality_flags(
                "관련법규(건설기술진흥법, 산업안전보건법 등)",
                related_law["label_text"],
                "knowledge",
            ),
        )

        spaced_related_law = compact_ksa_representative_label(
            "관련 법규(국가계약법, 건설기술진흥법 등)",
            "knowledge",
        )
        self.assertEqual(spaced_related_law["label_text"], "국가계약법·건설기술진흥법 등 관련 법규")

        non_law_parenthetical = compact_ksa_representative_label(
            "관련법규(보고 절차, 안전 수칙)",
            "knowledge",
        )
        self.assertEqual(non_law_parenthetical["label_text"], "법규")
        self.assertNotIn("compact_related_law_parenthetical", non_law_parenthetical["method_details"])

        nested_related_law = compact_ksa_representative_label(
            "관련법규(근로기준법(시행령, 시행규칙), 산업안전보건법)",
            "knowledge",
        )
        self.assertEqual(nested_related_law["label_text"], "근로기준법·산업안전보건법 관련 법규")
        self.assertNotIn(
            "unbalanced_parentheses",
            ksa_label_quality_flags(
                "관련법규(근로기준법(시행령, 시행규칙), 산업안전보건법)",
                nested_related_law["label_text"],
                "knowledge",
            ),
        )

        nested_parenthetical = compact_ksa_representative_label(
            "경영리스크 우선순위 설정 능력(멀티 보팅(Multi-Voting) 기법, 매트릭스 분석 기법)",
            "skill",
        )
        self.assertEqual(nested_parenthetical["label_text"], "경영리스크 우선순위 설정")
        self.assertNotIn(
            "unbalanced_parentheses",
            ksa_label_quality_flags(
                "경영리스크 우선순위 설정 능력(멀티 보팅(Multi-Voting) 기법, 매트릭스 분석 기법)",
                nested_parenthetical["label_text"],
                "skill",
            ),
        )

        dangling_close = compact_ksa_representative_label(
            "자료분석기술(빈도분석, 평균분석 등의 기술통계)",
            "skill",
        )
        self.assertEqual(dangling_close["label_text"], "기술통계")
        self.assertNotIn(
            "unbalanced_parentheses",
            ksa_label_quality_flags(
                "자료분석기술(빈도분석, 평균분석 등의 기술통계)",
                dangling_close["label_text"],
                "skill",
            ),
        )

        dangling_open_enum = compact_ksa_representative_label(
            "(단순조회, JOIN을 이용한 조회, 유니온을 이용한 조회, 서브쿼리를 이용한 조회)",
            "knowledge",
        )
        self.assertEqual(dangling_open_enum["label_text"], "단순조회")
        self.assertNotIn(
            "dangling_enum_suffix",
            ksa_label_quality_flags(
                "(단순조회, JOIN을 이용한 조회, 유니온을 이용한 조회, 서브쿼리를 이용한 조회)",
                dangling_open_enum["label_text"],
                "knowledge",
            ),
        )

        dangling_square = compact_ksa_representative_label(
            "폭발 위험장소 분류(HAC) 관련 국제 표준[IP15, API(American Petroleum Institute) RP 500/505, IEC(International Electrotechnical Commission) 60092-502 등]",
            "knowledge",
        )
        self.assertEqual(dangling_square["label_text"], "폭발 위험장소 분류 국제 표준")
        self.assertNotIn(
            "unbalanced_parentheses",
            ksa_label_quality_flags(
                "폭발 위험장소 분류(HAC) 관련 국제 표준[IP15, API(American Petroleum Institute) RP 500/505, IEC(International Electrotechnical Commission) 60092-502 등]",
                dangling_square["label_text"],
                "knowledge",
            ),
        )

        dangling_enum = compact_ksa_representative_label(
            "ISO 9001, PSM, HACCP, GMP, FDA, ESG 등 인증체계 운영 지식",
            "knowledge",
        )
        self.assertEqual(dangling_enum["label_text"], "ISO 9001")
        self.assertIn(
            "digit_heavy",
            ksa_label_quality_flags(
                "ISO 9001, PSM, HACCP, GMP, FDA, ESG 등 인증체계 운영 지식",
                dangling_enum["label_text"],
            ),
        )

        action_skill = compact_ksa_representative_label(
            "협력업체의 현장 서비스를 관리, 평가하는 능력",
            "skill",
        )
        self.assertEqual(action_skill["label_text"], "협력업체 현장 서비스 관리·평가")

        judgment_skill = compact_ksa_representative_label(
            "프로젝트 계획을 통한 공적개발원조사업의 목표달성 여부를 판단할 수 있는 능력",
            "skill",
        )
        self.assertEqual(
            judgment_skill["label_text"],
            "프로젝트 계획 기반 공적개발원조사업 목표달성 여부 판단",
        )

        identify_skill = compact_ksa_representative_label(
            "사업 추진 계획에서 조달해야 할 제품이나 서비스를 식별할 수 있는 능력",
            "skill",
        )
        self.assertEqual(
            identify_skill["label_text"],
            "사업 추진 계획 조달 제품·서비스 식별",
        )

        attitude = compact_ksa_representative_label(
            "객관적이며 논리적으로 사고하려는 의지",
            "attitude",
        )
        self.assertEqual(attitude["label_text"], "객관적·논리적 사고 태도")

        action_attitude = compact_ksa_representative_label(
            "자사의 사업구조를 명확히 파악하려는 자세",
            "attitude",
        )
        self.assertEqual(action_attitude["label_text"], "자사 사업구조 파악 태도")
        self.assertNotIn(
            "changed_near_full_length",
            ksa_label_quality_flags(
                "자사의 사업구조를 명확히 파악하려는 자세",
                action_attitude["label_text"],
                "attitude",
            ),
        )

        field_attitude = compact_ksa_representative_label(
            "현장의 상황을 고려하려는 자세",
            "attitude",
        )
        self.assertEqual(field_attitude["label_text"], "현장 상황 고려 태도")

        non_hada_attitude = compact_ksa_representative_label(
            "새로운 기술을 배우려는 자세",
            "attitude",
        )
        self.assertEqual(non_hada_attitude["label_text"], "새로운 기술 학습 태도")
        self.assertIn("compact_attitude_action_phrase", non_hada_attitude["method_details"])

        modifier_attitude = compact_ksa_representative_label(
            "데이터를 정확하게 기록하려는 태도",
            "attitude",
        )
        self.assertEqual(modifier_attitude["label_text"], "데이터 기록 태도")
        self.assertIn("normalize_wordlike_phrase", modifier_attitude["method_details"])

        diversity_attitude = compact_ksa_representative_label(
            "문화의 다양성을 이해하고 존경하려는 태도",
            "attitude",
        )
        self.assertEqual(diversity_attitude["label_text"], "문화 다양성 존중 태도")

        completion_attitude = compact_ksa_representative_label(
            "사업을 성공적으로 완료시키려는 의지",
            "attitude",
        )
        self.assertEqual(completion_attitude["label_text"], "사업 완료 태도")

        set_attitude = compact_ksa_representative_label(
            "공정한 평가기준을 설정하려는 의지",
            "attitude",
        )
        self.assertEqual(set_attitude["label_text"], "공정한 평가기준 설정 태도")

        accept_attitude = compact_ksa_representative_label(
            "지방세법 및 조례의 개정사항을 수용하는 자세",
            "attitude",
        )
        self.assertEqual(accept_attitude["label_text"], "지방세법 및 조례 개정사항 수용 태도")

        paired_attitude = compact_ksa_representative_label(
            "각종 기법을 이해하고 활용하려는 태도",
            "attitude",
        )
        self.assertEqual(paired_attitude["label_text"], "기법 이해·활용 태도")

        progress_attitude = compact_ksa_representative_label(
            "계획된 일정에 따라 업무를 진행해 나가려는 태도",
            "attitude",
        )
        self.assertEqual(progress_attitude["label_text"], "계획 일정 기준 업무 진행 태도")

        reflect_attitude = compact_ksa_representative_label(
            "고객의 의견과 사용 경험을 존중하고 반영하려는 자세",
            "attitude",
        )
        self.assertEqual(reflect_attitude["label_text"], "고객 의견 및 사용 경험 존중·반영 태도")

        prevent_delay_attitude = compact_ksa_representative_label(
            "촬영일정에 차질이 없도록 준비하려는 성실성",
            "attitude",
        )
        self.assertEqual(prevent_delay_attitude["label_text"], "촬영일정에 차질 방지 준비 태도")

        listen_attitude = compact_ksa_representative_label(
            "경청하고자 하는 태도",
            "attitude",
        )
        self.assertEqual(listen_attitude["label_text"], "경청 태도")

        evaluate_responsibility = compact_ksa_representative_label(
            "슬리팅 품질 수준을 공정하게 평가하고자 하는 책임감",
            "attitude",
        )
        self.assertEqual(evaluate_responsibility["label_text"], "슬리팅 품질 수준 평가 태도")

        explain_attitude = compact_ksa_representative_label(
            "원인-결과 관계를 논리적으로 설명하려는 태도",
            "attitude",
        )
        self.assertEqual(explain_attitude["label_text"], "원인-결과 관계 설명 태도")

        link_attitude = compact_ksa_representative_label(
            "여러 분야의 요소들을 종합적으로 연계하려는 태도",
            "attitude",
        )
        self.assertEqual(link_attitude["label_text"], "여러 분야 요소 연계 태도")

        response_attitude = compact_ksa_representative_label(
            "통합적인 관점에서 변경 요구에 대응하려는 적극적인 실행 의지",
            "attitude",
        )
        self.assertEqual(response_attitude["label_text"], "통합 관점 변경 요구 대응 태도")

        knowledge_clause = compact_ksa_representative_label(
            "요구사항 문서와 프로젝트 최종 상태를 상호 분석할 수 있는 지식",
            "knowledge",
        )
        self.assertEqual(knowledge_clause["label_text"], "요구사항 문서 및 프로젝트 최종 상태 상호 분석")

        knowledge_method = compact_ksa_representative_label(
            "성과와 성과표준 간 차이의 원인을 해소하는 책임단위를 파악하는 방법",
            "knowledge",
        )
        self.assertEqual(knowledge_method["label_text"], "성과 및 성과표준 간 차이 원인 해소 책임단위 파악 방법")

        applied_skill = compact_ksa_representative_label(
            "공정별 4M 기법을 활용한 위험성 평가",
            "skill",
        )
        self.assertEqual(applied_skill["label_text"], "공정별 4M 기법 활용 위험성 평가")

        hr_attitude_sentence = compact_ksa_representative_label(
            "임금 조정 확정안이 구성원의 동기부여와 회사 경쟁력을 강화할 수 있도록 사전 준비하고 실행하는 분석적인 자세",
            "attitude",
        )
        self.assertEqual(
            hr_attitude_sentence["label_text"],
            "임금 조정 확정안이 구성원 동기부여 및 회사 경쟁력 강화할 수 있도록 준비·실행 분석적인 태도",
        )
        self.assertIn(
            "residual_sentence_like_label",
            ksa_label_quality_flags(
                "임금 조정 확정안이 구성원의 동기부여와 회사 경쟁력을 강화할 수 있도록 사전 준비하고 실행하는 분석적인 자세",
                hr_attitude_sentence["label_text"],
                "attitude",
            ),
        )

        procurement_laws = compact_ksa_representative_label(
            "전자조달 관련 법령 및 규정 - 전자조달의 이용 및 촉진에 관한 법률 - 국가종합전자조달시스템 종합쇼핑몰 운영규정",
            "knowledge",
        )
        self.assertEqual(procurement_laws["label_text"], "전자조달 법령 및 규정")
        self.assertIn("collapse_dash_enumeration", procurement_laws["method_details"])

        long_airwaybill = compact_ksa_representative_label(
            "Master Air Waybill과 House Air Waybill의 차이점 및 사용방법",
            "knowledge",
        )
        self.assertIn(
            "overlong_word_label",
            ksa_label_quality_flags(
                "Master Air Waybill과 House Air Waybill의 차이점 및 사용방법",
                long_airwaybill["label_text"],
                "knowledge",
            ),
        )

    def test_ksa_label_candidates_preserve_source_and_concept_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            original_raw = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]
            original_concept_name = conn.execute(
                "SELECT concept_name FROM ontology_concepts WHERE concept_id = ?",
                (fixture["concept_id"],),
            ).fetchone()["concept_name"]

            result = build_ksa_label_candidates(conn, reset=True)
            label_row = conn.execute(
                """
                SELECT *
                FROM ontology_concept_label_candidates
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()
            raw_after = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]
            concept_after = conn.execute(
                "SELECT concept_name FROM ontology_concepts WHERE concept_id = ?",
                (fixture["concept_id"],),
            ).fetchone()["concept_name"]
            trusted_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concept_label_candidates
                WHERE review_status IN ('human_reviewed', 'accepted', 'reviewed')
                """
            ).fetchone()[0]
            conn.close()

        self.assertGreaterEqual(result["concepts_processed"], 1)
        self.assertIsNotNone(label_row)
        self.assertEqual(label_row["review_status"], "candidate")
        self.assertEqual(label_row["label_role"], "short_representative_label")
        self.assertEqual(original_raw, raw_after)
        self.assertEqual(original_concept_name, concept_after)
        self.assertEqual(trusted_count, 0)

    def test_ksa_label_candidates_machine_review_is_not_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            original_raw = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]

            result = build_ksa_label_candidates(conn, reset=True, machine_review=True)
            label_row = conn.execute(
                """
                SELECT review_status, label_text
                FROM ontology_concept_label_candidates
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()
            raw_after = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]
            trusted_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concept_label_candidates
                WHERE review_status IN ('human_reviewed', 'accepted', 'reviewed')
                """
            ).fetchone()[0]
            conn.close()

        self.assertTrue(result["machine_review"])
        self.assertIsNotNone(label_row)
        self.assertIn(label_row["review_status"], {"llm_reviewed", "needs_review"})
        self.assertNotEqual(label_row["review_status"], "human_reviewed")
        self.assertEqual(label_row["label_text"], "workforce planning")
        self.assertEqual(original_raw, raw_after)
        self.assertEqual(trusted_count, 0)

    def test_ksa_label_candidate_report_surfaces_pipeline_and_safety_counts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            seed_task_ontology(conn)
            timestamp = now_utc()
            concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE concept_name = 'workforce planning'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('99', 'Other', '01', 'Other Middle', '01', 'Other Small', '01', 'Other Sub')
                """
            )
            other_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '99'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('9901010101_23v1', '9901010101', '23v1', 'Other unit',
                          '3', ?, 'Other scope.', 'matched', ?, ?)
                """,
                (other_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('9901010101_23v1', '1', '9901010101_23v1 1', 'Other element', '3')
                """
            )
            other_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '9901010101_23v1'"
            ).fetchone()["element_id"]
            cur = conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'aa')
                """,
                (other_element_id,),
            )
            other_ksa_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'candidate', ?)
                """,
                (other_ksa_id, concept_id, timestamp),
            )
            build_ksa_label_candidates(conn, reset=True)
            build_ksa_label_candidates(conn, major_code="02", reset=True)
            scoped_label = conn.execute(
                """
                SELECT source_text
                FROM ontology_concept_label_candidates
                WHERE concept_id = ?
                  AND source_scope_key LIKE '02:%'
                ORDER BY label_id
                LIMIT 1
                """,
                (concept_id,),
            ).fetchone()["source_text"]

            report = build_ksa_label_candidate_report(conn, sample_limit=5, collision_limit=5)
            scoped_report = build_ksa_label_candidate_report(
                conn,
                major_code="02",
                sample_limit=5,
                collision_limit=5,
            )
            markdown_path = Path(tmp) / "ksa_label_report.md"
            write_ksa_label_candidate_report_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")
            conn.close()

        self.assertTrue(report["ok"])
        self.assertFalse(report["status_update_allowed"])
        self.assertEqual(report["schema"], "ksa_label_candidate_report_v1")
        self.assertGreaterEqual(report["counts"]["label_candidates"], 1)
        self.assertEqual(report["counts"]["missing_source_id_rows"], 0)
        self.assertEqual(report["counts"]["trusted_status_rows"], 0)
        self.assertIn("short_label_candidate", [item["stage"] for item in report["pipeline_contract"]])
        self.assertIn("quality_flag_counts", report["quality"])
        self.assertIn("Short Label Candidate", markdown)
        self.assertIn("Generic Examples", markdown)
        self.assertIn("Quality Flag Counts", markdown)
        self.assertIn("status_update_allowed", markdown)
        self.assertEqual(scoped_report["scope"]["major_code"], "02")
        self.assertEqual(scoped_report["counts"]["label_candidates"], 1)
        self.assertGreater(report["counts"]["label_candidates"], scoped_report["counts"]["label_candidates"])
        self.assertNotEqual(scoped_label, "aa")

    def test_ksa_label_candidate_report_counts_source_violations_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, NULL, NULL, 'knowledge', '', '', 'broken', 'short_representative_label',
                          'broken_test_candidate', 1, 0.1, 'candidate', ?, ?)
                """,
                (fixture["concept_id"], timestamp, timestamp),
            )

            report = build_ksa_label_candidate_report(conn, major_code="02")
            seedpack = build_ksa_label_review_seedpack(conn, major_code="02")
            conn.close()

        self.assertFalse(report["ok"])
        self.assertEqual(report["counts"]["label_candidates"], 0)
        self.assertEqual(report["counts"]["missing_source_id_rows"], 1)
        self.assertEqual(report["counts"]["missing_text_rows"], 1)
        self.assertEqual(report["counts"]["source_preservation_violations"], 1)
        self.assertEqual(report["samples"], [])
        self.assertEqual(seedpack["row_count"], 0)

    def test_ksa_label_candidate_report_scopes_by_label_source_major(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('99', 'Other', '01', 'Other Middle', '01', 'Other Small', '01', 'Other Sub')
                """
            )
            other_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '99'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('9901010101_23v1', '9901010101', '23v1', 'Other unit',
                          '3', ?, 'Other scope.', 'matched', ?, ?)
                """,
                (other_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('9901010101_23v1', '1', '9901010101_23v1 1', 'Other element', '3')
                """
            )
            other_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '9901010101_23v1'"
            ).fetchone()["element_id"]
            cur = conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'cross major source only')
                """,
                (other_element_id,),
            )
            other_ksa_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'candidate', ?)
                """,
                (other_ksa_id, fixture["concept_id"], timestamp),
            )
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, NULL, 'knowledge', 'cross major source only',
                          'cross major label', 'crossmajorlabel',
                          'short_representative_label', 'test_cross_major', 1,
                          0.4, 'candidate', ?, ?)
                """,
                (fixture["concept_id"], other_ksa_id, timestamp, timestamp),
            )

            scoped_report = build_ksa_label_candidate_report(conn, major_code="02")
            other_report = build_ksa_label_candidate_report(conn, major_code="99")
            conn.close()

        self.assertEqual(scoped_report["counts"]["label_candidates"], 0)
        self.assertEqual(scoped_report["samples"], [])
        self.assertEqual(other_report["counts"]["label_candidates"], 1)
        self.assertEqual(other_report["samples"][0]["source_text"], "cross major source only")

    def test_scoped_ksa_label_rebuild_preserves_other_scope_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('99', 'Other', '01', 'Other Middle', '01', 'Other Small', '01', 'Other Sub')
                """
            )
            other_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '99'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('9901010101_23v1', '9901010101', '23v1', 'Other unit',
                          '3', ?, 'Other scope.', 'matched', ?, ?)
                """,
                (other_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('9901010101_23v1', '1', '9901010101_23v1 1', 'Other element', '3')
                """
            )
            other_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '9901010101_23v1'"
            ).fetchone()["element_id"]
            cur = conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'workforce planning')
                """,
                (other_element_id,),
            )
            other_ksa_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'candidate', ?)
                """,
                (other_ksa_id, fixture["concept_id"], timestamp),
            )

            build_ksa_label_candidates(conn, major_code="02", reset=True)
            build_ksa_label_candidates(conn, major_code="99", reset=True)
            rows = conn.execute(
                """
                SELECT source_ksa_id, source_scope_key, source_text, label_text
                FROM ontology_concept_label_candidates
                WHERE concept_id = ?
                ORDER BY source_scope_key
                """,
                (fixture["concept_id"],),
            ).fetchall()
            conn.close()

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source_ksa_id"] for row in rows}, {fixture["ksa_id"], other_ksa_id})
        self.assertEqual({row["source_scope_key"] for row in rows}, {"02:02:02:01", "99:01:01:01"})
        self.assertEqual({row["label_text"] for row in rows}, {"workforce planning"})

    def test_scoped_ksa_label_rebuild_is_not_blocked_by_cross_scope_trusted_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('99', 'Other', '01', 'Other Middle', '01', 'Other Small', '01', 'Other Sub')
                """
            )
            other_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '99'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_definition,
                    api_match_status, created_at, updated_at
                ) VALUES ('9901010101_23v1', '9901010101', '23v1', 'Other unit',
                          '3', ?, 'Other scope.', 'matched', ?, ?)
                """,
                (other_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('9901010101_23v1', '1', '9901010101_23v1 1', 'Other element', '3')
                """
            )
            other_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '9901010101_23v1'"
            ).fetchone()["element_id"]
            cur = conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'other workforce planning')
                """,
                (other_element_id,),
            )
            other_ksa_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'candidate', ?)
                """,
                (other_ksa_id, fixture["concept_id"], timestamp),
            )

            build_ksa_label_candidates(conn, major_code="02", reset=True)
            build_ksa_label_candidates(conn, major_code="99", reset=True)
            conn.execute(
                """
                UPDATE ontology_concept_label_candidates
                SET source_text = 'trusted other source',
                    label_text = 'trusted other label',
                    normalized_label_key = 'trustedotherlabel',
                    review_status = 'human_reviewed'
                WHERE concept_id = ?
                  AND source_scope_key = '99:01:01:01'
                """,
                (fixture["concept_id"],),
            )
            conn.commit()

            build_ksa_label_candidates(conn, major_code="02", reset=True)
            rows = conn.execute(
                """
                SELECT source_scope_key, label_text, review_status
                FROM ontology_concept_label_candidates
                WHERE concept_id = ?
                ORDER BY source_scope_key
                """,
                (fixture["concept_id"],),
            ).fetchall()
            conn.close()

        self.assertEqual(len(rows), 2)
        by_scope = {row["source_scope_key"]: row for row in rows}
        self.assertEqual(by_scope["02:02:02:01"]["review_status"], "candidate")
        self.assertEqual(by_scope["02:02:02:01"]["label_text"], "workforce planning")
        self.assertEqual(by_scope["99:01:01:01"]["review_status"], "human_reviewed")
        self.assertEqual(by_scope["99:01:01:01"]["label_text"], "trusted other label")

    def test_ksa_label_candidate_preprocessing_preserves_trusted_statuses(self) -> None:
        for status in ("accepted", "reviewed", "human_reviewed"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "ncs.db"
                    conn = connect(db_path)
                    initialize_database(conn)
                    fixture = seed_task_ontology(conn)
                    build_ksa_label_candidates(conn, reset=True)
                    conn.execute(
                        """
                        UPDATE ontology_concept_label_candidates
                        SET source_text = 'trusted source',
                            label_text = 'trusted label',
                            normalized_label_key = 'trustedlabel',
                            review_status = ?
                        WHERE concept_id = ?
                        """,
                        (status, fixture["concept_id"]),
                    )
                    conn.commit()

                    build_ksa_label_candidates(conn, reset=True)
                    build_ksa_label_candidates(conn)
                    rows = conn.execute(
                        """
                        SELECT source_text, label_text, normalized_label_key, review_status
                        FROM ontology_concept_label_candidates
                        WHERE concept_id = ?
                        ORDER BY label_id
                        """,
                        (fixture["concept_id"],),
                    ).fetchall()
                    conn.close()

                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["source_text"], "trusted source")
                self.assertEqual(rows[0]["label_text"], "trusted label")
                self.assertEqual(rows[0]["normalized_label_key"], "trustedlabel")
                self.assertEqual(rows[0]["review_status"], status)

    def test_ksa_label_review_seedpack_exports_collision_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            build_ksa_label_candidates(conn, reset=True)
            timestamp = now_utc()
            evidence_element_id = conn.execute(
                "SELECT element_id FROM performance_criteria WHERE criteria_id = ?",
                (fixture["criteria_id"],),
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'term_definition_candidate',
                          'Workforce planning is a compact HR planning term.',
                          'term_definition_template',
                          'term definition fixture evidence',
                          '0202020101_23v3', ?, ?, ?, 0.81,
                          'candidate', ?, ?)
                """,
                (
                    fixture["concept_id"],
                    evidence_element_id,
                    fixture["criteria_id"],
                    fixture["ksa_id"],
                    timestamp,
                    timestamp,
                ),
            )
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('workforce planning duplicate', 'workforceplanningduplicate',
                          'knowledge', 'candidate', 'none', 'model_preprocessed', ?, ?)
                """,
                (timestamp, timestamp),
            )
            duplicate_concept_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, NULL, 'knowledge', 'workforce planning duplicate',
                          'workforce planning', 'workforceplanning',
                          'short_representative_label', 'test_collision', 1, 0.4,
                          'candidate', ?, ?)
                """,
                (duplicate_concept_id, fixture["ksa_id"], timestamp, timestamp),
            )
            cur = conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('broken STP label', 'brokenstplabel',
                          'knowledge', 'candidate', 'none', 'model_preprocessed', ?, ?)
                """,
                (timestamp, timestamp),
            )
            broken_concept_id = cur.lastrowid
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, NULL, 'knowledge',
                          'STP(Segmentation, Targeting, Positioning) 전략 수립 절차 이해',
                          'STP(Segmentation 등', 'stp(segmentation등',
                          'short_representative_label', 'test_broken_label',
                          1, 0.4, 'candidate', ?, ?)
                """,
                (broken_concept_id, fixture["ksa_id"], timestamp, timestamp),
            )

            seedpack = build_ksa_label_review_seedpack(conn, limit=20)
            jsonl_path = Path(tmp) / "seedpack.jsonl"
            csv_path = Path(tmp) / "seedpack.csv"
            write_ksa_label_review_seedpack_jsonl(seedpack, jsonl_path)
            write_ksa_label_review_seedpack_csv(seedpack, csv_path)
            conn.close()

            jsonl_text = jsonl_path.read_text(encoding="utf-8")
            csv_text = csv_path.read_text(encoding="utf-8-sig")

        self.assertFalse(seedpack["status_update_allowed"])
        self.assertFalse(seedpack["db_writes"])
        self.assertFalse(seedpack["approval_claim"])
        self.assertTrue(seedpack["human_decision_required"])
        self.assertGreaterEqual(seedpack["issue_counts"].get("collision", 0), 1)
        self.assertGreaterEqual(seedpack["issue_counts"].get("unbalanced_parentheses", 0), 1)
        self.assertIn('"issue_type": "collision"', jsonl_text)
        self.assertIn('"db_writes": false', jsonl_text)
        self.assertIn('"approval_claim": false', jsonl_text)
        self.assertIn('"human_decision_required": true', jsonl_text)
        self.assertIn('"review_prompt":', jsonl_text)
        self.assertIn('"review_focus":', jsonl_text)
        self.assertIn('"allowed_decisions": ["approve", "needs_revision", "reject"]', jsonl_text)
        self.assertIn('"term_definition_candidate": "Workforce planning is a compact HR planning term."', jsonl_text)
        self.assertIn('"task_evidence_preview":', jsonl_text)
        self.assertIn('"quality_flags": ["dangling_enum_suffix", "unbalanced_parentheses"', jsonl_text)
        self.assertIn("review_prompt", csv_text)
        self.assertIn("review_focus", csv_text)
        self.assertIn("allowed_decisions", csv_text)
        self.assertIn("db_writes", csv_text)
        self.assertIn("approval_claim", csv_text)
        self.assertIn("human_decision_required", csv_text)
        self.assertIn("term_definition_candidate", csv_text)
        self.assertIn("task_evidence_preview", csv_text)
        self.assertIn("human_representative_label", csv_text)
        collision_row = next(row for row in seedpack["rows"] if row["issue_type"] == "collision")
        self.assertEqual(collision_row["allowed_decisions"], ["approve", "needs_revision", "reject"])
        self.assertTrue(collision_row["human_decision_required"])
        self.assertFalse(collision_row["status_update_allowed"])
        self.assertFalse(collision_row["db_writes"])
        self.assertFalse(collision_row["approval_claim"])
        self.assertFalse(collision_row["trusted_status_write_allowed"])
        self.assertIn("raw_ksa_to_atomic_ksa", collision_row["review_focus"])
        self.assertIn("representative_concept_to_short_label", collision_row["review_focus"])
        self.assertIn("normalized_label_collision", collision_row["review_focus"])
        self.assertEqual(collision_row["term_definition_candidate"], "Workforce planning is a compact HR planning term.")
        self.assertGreaterEqual(collision_row["task_evidence_count"], 1)
        self.assertIn(fixture["criteria_id"], collision_row["criteria_ids"])
        self.assertTrue(collision_row["criteria_text_preview"])
        repair_row = next(row for row in seedpack["rows"] if row["issue_type"] == "unbalanced_parentheses")
        self.assertIn("syntax_repair_required", repair_row["review_focus"])
        self.assertIn("do_not_approve_until_fixed", repair_row["review_focus"])

    def test_ksa_label_review_seedpack_excludes_trusted_label_rows(self) -> None:
        for status in ("human_reviewed", "accepted", "reviewed"):
            with self.subTest(status=status):
                with tempfile.TemporaryDirectory() as tmp:
                    db_path = Path(tmp) / "ncs.db"
                    conn = connect(db_path)
                    initialize_database(conn)
                    fixture = seed_task_ontology(conn)
                    timestamp = now_utc()
                    conn.execute(
                        """
                        INSERT INTO ontology_concept_label_candidates(
                            concept_id, source_ksa_id, source_atomic_id, concept_type,
                            source_text, label_text, normalized_label_key, label_role,
                            source_method, candidate_rank, confidence_score,
                            review_status, created_at, updated_at
                        ) VALUES (?, ?, NULL, 'knowledge', 'trusted source',
                                  '법규', '법규',
                                  'short_representative_label', 'test_trusted',
                                  1, 0.4, ?, ?, ?)
                        """,
                        (fixture["concept_id"], fixture["ksa_id"], status, timestamp, timestamp),
                    )

                    seedpack = build_ksa_label_review_seedpack(conn, limit=20)
                    conn.close()

                self.assertEqual(seedpack["row_count"], 0)
                self.assertEqual(seedpack["rows"], [])

    def test_ksa_label_review_seedpack_prefilters_non_ascii_bracket_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('bad bracket concept', 'badbracketconcept',
                          'knowledge', 'candidate', 'none', 'model_preprocessed', ?, ?)
                """,
                (timestamp, timestamp),
            )
            concept_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                INSERT INTO ontology_concept_label_candidates(
                    concept_id, source_ksa_id, source_atomic_id, concept_type,
                    source_text, label_text, normalized_label_key, label_role,
                    source_method, candidate_rank, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, NULL, 'knowledge',
                          'bad bracket source text',
                          'bad bracket【', 'badbracket',
                          'short_representative_label', 'already_short_label',
                          1, 0.9, 'candidate', ?, ?)
                """,
                (concept_id, fixture["ksa_id"], timestamp, timestamp),
            )

            seedpack = build_ksa_label_review_seedpack(conn, limit=20)
            conn.close()

        self.assertEqual(seedpack["row_count"], 1)
        self.assertEqual(seedpack["rows"][0]["label_text"], "bad bracket【")
        self.assertEqual(seedpack["rows"][0]["issue_type"], "unbalanced_parentheses")
        self.assertIn("unbalanced_parentheses", seedpack["rows"][0]["quality_flags"])
        self.assertEqual(seedpack["issue_counts"].get("unbalanced_parentheses"), 1)

    def test_ksa_label_review_seedpack_csv_escapes_formula_cells(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            csv_path = Path(tmp) / "seedpack.csv"
            seedpack = {
                "rows": [
                    {
                        "issue_type": "generic",
                        "quality_flags": ["generic_or_low_specificity"],
                        "review_prompt": "=prompt",
                        "review_focus": ["=focus", "source_label"],
                        "allowed_decisions": ["=approve", "reject"],
                        "label_id": 1,
                        "concept_id": 1,
                        "concept_type": "knowledge",
                        "concept_name": "=cmd",
                        "source_ksa_id": 1,
                        "source_atomic_id": "",
                        "source_text": " +danger",
                        "label_text": "-danger",
                        "normalized_label_key": "@danger",
                        "source_method": "test",
                        "confidence_score": 0.1,
                        "review_status": "candidate",
                        "collision_row_count": 1,
                        "collision_concept_count": 1,
                        "raw_to_label_checked": "=checked",
                        "human_decision": "",
                        "human_representative_label": "",
                        "human_note": "",
                    }
                ]
            }
            write_ksa_label_review_seedpack_csv(seedpack, csv_path)
            csv_text = csv_path.read_text(encoding="utf-8-sig")

        self.assertIn("'=cmd", csv_text)
        self.assertIn("'=prompt", csv_text)
        self.assertIn("'=focus;source_label", csv_text)
        self.assertIn("'=approve;reject", csv_text)
        self.assertIn("'=checked", csv_text)
        self.assertIn("' +danger", csv_text)
        self.assertIn("'-danger", csv_text)
        self.assertIn("'@danger", csv_text)

    def test_diversification_keeps_strong_direct_candidates_before_weak_adjacent(self) -> None:
        def candidate(
            course_name: str,
            sub_code: str,
            score: float,
            *,
            direct: bool,
        ) -> dict[str, object]:
            return {
                "row": {
                    "ncs_subd_cd": sub_code,
                    "compe_unit_name": course_name,
                },
                "score": score,
                "match": {
                    "direct_unit_evidence": direct,
                    "reasons": [],
                    "score_components": {"penalty_score": 0.0},
                },
            }

        selected = _diversify_top_k_candidates(
            [
                candidate("노사관계 계획", "02", 1.0, direct=True),
                candidate("단체교섭준비", "02", 1.0, direct=True),
                candidate("단체교섭", "02", 0.97, direct=True),
                candidate("노사협의회 운영", "02", 0.97, direct=True),
                candidate("인사평가", "01", 0.38, direct=False),
            ],
            max_items=5,
        )

        self.assertEqual(
            [item["row"]["compe_unit_name"] for item in selected[:4]],
            ["노사관계 계획", "단체교섭준비", "단체교섭", "노사협의회 운영"],
        )

    def test_diversification_keeps_quality_penalized_direct_unit_candidates(self) -> None:
        def candidate(
            course_name: str,
            sub_code: str,
            score: float,
            *,
            direct: bool,
        ) -> dict[str, object]:
            return {
                "row": {
                    "ncs_subd_cd": sub_code,
                    "compe_unit_name": course_name,
                },
                "score": score,
                "match": {
                    "direct_unit_evidence": direct,
                    "reasons": [],
                    "score_components": {"penalty_score": 0.0},
                },
            }

        selected = _diversify_top_k_candidates(
            [
                candidate("direct plan", "02", 0.6, direct=True),
                candidate("direct dispute", "02", 0.58, direct=True),
                candidate("direct bargaining prep", "02", 0.48, direct=True),
                candidate("direct bargaining", "02", 0.48, direct=True),
                candidate("adjacent outsourcing", "01", 0.2, direct=False),
            ],
            max_items=5,
        )

        self.assertEqual(
            [item["row"]["compe_unit_name"] for item in selected[:4]],
            ["direct plan", "direct dispute", "direct bargaining prep", "direct bargaining"],
        )

    def test_diversification_keeps_direct_unit_candidate_at_bypass_boundary(self) -> None:
        def candidate(
            course_name: str,
            sub_code: str,
            score: float,
            *,
            direct: bool,
        ) -> dict[str, object]:
            return {
                "row": {
                    "ncs_subd_cd": sub_code,
                    "compe_unit_name": course_name,
                },
                "score": score,
                "match": {
                    "direct_unit_evidence": direct,
                    "reasons": [],
                    "score_components": {"penalty_score": 0.0},
                },
            }

        selected = _diversify_top_k_candidates(
            [
                candidate("direct plan", "02", 0.6, direct=True),
                candidate("direct dispute", "02", 0.58, direct=True),
                candidate(
                    "direct boundary",
                    "02",
                    DIRECT_UNIT_DIVERSITY_BYPASS_SCORE,
                    direct=True,
                ),
                candidate("adjacent reference", "01", 0.3, direct=False),
            ],
            max_items=4,
        )

        self.assertEqual(
            [item["row"]["compe_unit_name"] for item in selected],
            ["direct plan", "direct dispute", "direct boundary", "adjacent reference"],
        )

    def test_diversification_defers_direct_unit_candidate_below_bypass_boundary(self) -> None:
        def candidate(
            course_name: str,
            sub_code: str,
            score: float,
            *,
            direct: bool,
        ) -> dict[str, object]:
            return {
                "row": {
                    "ncs_subd_cd": sub_code,
                    "compe_unit_name": course_name,
                },
                "score": score,
                "match": {
                    "direct_unit_evidence": direct,
                    "reasons": [],
                    "score_components": {"penalty_score": 0.0},
                },
            }

        selected = _diversify_top_k_candidates(
            [
                candidate("direct plan", "02", 0.6, direct=True),
                candidate("direct dispute", "02", 0.58, direct=True),
                candidate(
                    "direct below boundary",
                    "02",
                    DIRECT_UNIT_DIVERSITY_BYPASS_SCORE - 0.001,
                    direct=True,
                ),
                candidate("adjacent reference", "01", 0.3, direct=False),
            ],
            max_items=4,
        )

        self.assertEqual(
            [item["row"]["compe_unit_name"] for item in selected],
            ["direct plan", "direct dispute", "adjacent reference", "direct below boundary"],
        )
        self.assertIn("diversity_penalty", selected[-1]["match"]["reasons"])

    def test_course_candidate_sort_prefers_target_sub_scope_before_adjacent_small_scope(self) -> None:
        def candidate(
            course_name: str,
            relation: str,
            score: float,
            *,
            direct_unit_evidence: bool = False,
            goal_concept_hits: int = 0,
        ) -> dict[str, object]:
            return {
                "row": {"compe_unit_name": course_name},
                "score": score,
                "match": {
                    "course_scope_fit": {"relation": relation},
                    "direct_unit_evidence": direct_unit_evidence,
                    "source_element_covered": False,
                    "goal_concept_hits": goal_concept_hits,
                },
            }

        ranked = sorted(
            [
                candidate("adjacent labor", "same_small_classification", 0.95),
                candidate("same HR support", "same_sub_classification", 0.3),
                candidate("direct unit", "direct_scope_unit", 0.2, direct_unit_evidence=True),
                candidate("generic adjacent", "same_middle_classification", 1.0, goal_concept_hits=1),
            ],
            key=_course_candidate_sort_key,
        )

        self.assertEqual(
            [item["row"]["compe_unit_name"] for item in ranked],
            ["direct unit", "same HR support", "adjacent labor", "generic adjacent"],
        )

    def test_recommendation_tier_keeps_weak_direct_matches_supplemental(self) -> None:
        weak_direct = _recommendation_tier(
            0.46,
            {
                "direct_unit_evidence": True,
                "source_element_covered": False,
                "goal_direct_concept_hits": 0,
                "goal_token_concept_hits": 1,
            },
        )
        strong_direct = _recommendation_tier(
            0.78,
            {
                "direct_unit_evidence": True,
                "source_element_covered": False,
                "goal_direct_concept_hits": 0,
                "goal_token_concept_hits": 0,
            },
        )

        self.assertEqual(weak_direct["tier"], "supplemental")
        self.assertEqual(strong_direct["tier"], "primary")

    def test_preference_fit_uses_method_aliases_and_scaled_time_penalty(self) -> None:
        profile = _preference_fit_profile(
            delivery_methods=["Field practice"],
            requested_methods=["hands-on"],
            hours=36,
            preferred_max_hours=24,
        )
        time_adjustment, time_fit, over_ratio = _preference_time_adjustment(36, 24)

        self.assertTrue(profile["method_fit"])
        self.assertIn("practice", profile["matched_method_groups"])
        self.assertEqual(profile["time_fit"], "over")
        self.assertEqual(profile["time_over_ratio"], 0.5)
        self.assertEqual(time_fit, "over")
        self.assertEqual(over_ratio, 0.5)
        self.assertLess(time_adjustment, -0.04)

    def test_course_delivery_relations_preserve_mixed_method_strings(self) -> None:
        relations = _course_delivery_relations(
            {
                "compe_unit_level": "",
                "train_time": "",
                "fac_name": "",
                "meth_name": "Lecture; Practice",
            }
        )

        methods = [
            relation["relation_value"]
            for relation in relations
            if relation["relation_type"] == "delivered_by"
        ]
        self.assertEqual(methods, ["Lecture", "Practice"])

    def test_task_recommendation_requires_task_locator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                training_result = recommend_training_for_task(conn, query="   ", save=False)
                transition_result = recommend_task_transitions_from_db(conn)
            finally:
                conn.close()

        self.assertFalse(training_result["ok"])
        self.assertEqual(training_result["error"]["code"], "missing_task_locator")
        self.assertFalse(transition_result["ok"])
        self.assertEqual(transition_result["error"]["code"], "missing_task_locator")

    def test_compact_keeps_supported_supplemental_course_visible_as_supplemental(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [],
                    "supplemental": [
                        {
                            "rank": 1,
                            "course_name": "Bridge course",
                            "training_course_id": 10,
                            "confidence_score": 0.55,
                            "confidence_grade": "medium",
                            "evidence_strength": {
                                "grade": "medium",
                                "label": "transition_supporting_evidence",
                            },
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "adjacent": [],
                },
                "transition": {"summary": {}, "current_scope": {}, "target_scope": {}},
                "audit": {},
            },
            recommendation_limit=1,
        )

        self.assertEqual(compact["recommended_courses"][0]["tier"], "supplemental")

    def test_compact_demoted_adjacent_reference_uses_reference_rationale(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [],
                    "supplemental": [
                        {
                            "rank": 1,
                            "course_name": "Weak bridge",
                            "training_course_id": 11,
                            "confidence_score": 0.28,
                            "confidence_grade": "low",
                            "evidence_strength": {"grade": "low", "label": "weak_evidence"},
                            "rationale": "전환 경로의 부족 역량이나 주변 직무 역량을 보완하는 과정",
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "adjacent": [],
                },
                "transition": {"summary": {}, "current_scope": {}, "target_scope": {}},
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["tier"], "adjacent_reference")
        self.assertEqual(course["tier_label"], "참고 과정")
        self.assertIn("참고", course["rationale"])
        self.assertNotIn("부족 역량", course["rationale"])

    def test_compact_input_quality_uses_full_groups_not_display_slice(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 2,
                            "course_name": "Strong course",
                            "training_course_id": 12,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [
                        {
                            "rank": 1,
                            "course_name": "Displayed first",
                            "training_course_id": 11,
                            "confidence_score": 0.5,
                            "confidence_grade": "medium",
                            "evidence_strength": {"grade": "medium"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "General affairs",
                        "requested_target_query": "HR planning",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 1,
                    },
                    "current_scope": {"match_level": "unit", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "unit", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        warning_codes = {warning["code"] for warning in compact["input_quality"]["warnings"]}
        self.assertTrue(compact["input_quality"]["ok"])
        self.assertEqual(compact["recommended_courses"][0]["course_name"], "Displayed first")
        self.assertEqual(compact["source_recommendation_counts"]["primary"], 1)
        self.assertNotIn("zero_primary_recommendations", warning_codes)
        self.assertNotIn("adjacent_reference_only", warning_codes)

    def test_compact_course_has_korean_tier_label_and_fit_summary(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "preference_fit": {
                                "preferred_max_hours": 24,
                                "actual_hours": 30,
                                "time_fit": "over",
                                "requested_methods": ["현장실습"],
                                "delivery_methods": ["집체훈련"],
                                "method_fit": False,
                            },
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["tier_label"], "우선 추천")
        self.assertIn("시간 조건 초과", course["fit_summary"][0])
        self.assertIn("훈련방식 불일치", course["fit_summary"][1])

    def test_low_confidence_primary_course_uses_review_label(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "quality management",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "quality management",
                    "match_level": "unit",
                    "unit_codes": ["1402011106_18v3"],
                },
                "recommendation_summary": {},
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "Quality management",
                            "training_course_id": 200,
                            "confidence_score": 0.3,
                            "confidence_grade": "low",
                            "evidence_strength": {"grade": "weak"},
                            "coverage_counts": {"gap_ksa": 3, "goal_ksa": 2},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["tier"], "primary")
        self.assertEqual(course["confidence_grade"], "low")
        self.assertEqual(course["tier_label"], "우선 검토")

    def test_compact_fit_summary_hides_internal_method_group_labels(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "preference_fit": {
                                "preferred_max_hours": 24,
                                "actual_hours": 24,
                                "time_fit": "fit",
                                "requested_methods": ["현장실습"],
                                "delivery_methods": ["집체훈련", "현장실습"],
                                "matched_method_groups": ["practice", "현장실습"],
                                "method_fit": True,
                            },
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        summary = compact["recommended_courses"][0]["fit_summary"]
        self.assertIn("훈련방식 일치: 현장실습", summary)
        self.assertFalse(any("practice" in item for item in summary))

    def test_compact_task_response_includes_named_evidence_highlights(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendations": [
                    {
                        "rank": 1,
                        "recommendation_tier": {
                            "tier": "primary",
                            "label": "Primary",
                            "rationale": "Direct evidence",
                        },
                        "training_course": {
                            "training_course_id": 100,
                            "compe_unit_name": "HR planning",
                        },
                        "confidence_score": 0.72,
                        "confidence_grade": "medium",
                        "evidence_strength": {
                            "grade": "medium",
                            "label": "usable_supporting_evidence",
                        },
                        "match": {
                            "source_concept_hits": 1,
                            "gap_concept_hits": 1,
                            "goal_concept_hits": 1,
                            "goal_direct_concept_hits": 1,
                            "goal_token_concept_hits": 2,
                            "goal_review_counts": {"reviewed": 1, "human_reviewed": 0},
                            "qualification_hits": ["Q1"],
                            "job_base_hits": ["1:2"],
                            "job_base_signal": {
                                "status": "target_scope_signal",
                                "evidence_role": "auxiliary_tie_breaker",
                                "target_hit_count": 1,
                            },
                            "quality_issue_penalty": {
                                "applied": True,
                                "issue_types": ["broad_generic_ksa"],
                                "multiplier": 0.8,
                                "concept_ids": [1],
                            },
                        },
                        "matched_source_ksa_concepts": [
                            {"concept_id": 1, "concept_name": "workforce planning", "concept_type": "knowledge"}
                        ],
                        "matched_gap_ksa_concepts": [
                            {"concept_id": 2, "concept_name": "labor law", "concept_type": "knowledge"}
                        ],
                        "source_ksa_concepts": [
                            {"concept_id": 1, "concept_name": "workforce planning", "concept_type": "knowledge"}
                        ],
                        "gap_ksa_concepts": [
                            {"concept_id": 2, "concept_name": "labor law", "concept_type": "knowledge"}
                        ],
                        "goal_coverage": [
                            {"concept_id": 3, "concept_name": "HR strategy", "concept_type": "skill"}
                        ],
                        "covered_elements": [{"element_name_raw": "Plan workforce"}],
                        "career_path_evidence": [
                            {"competency_name": "HR planning", "position_name": "Manager"}
                        ],
                        "qualification_evidence": [
                            {"jm_cd": "Q1", "jm_nm": "HR specialist", "ablt_unit_typ_nm": "unit"}
                        ],
                        "job_base_evidence": [
                            {
                                "job_base_competency_id": 1,
                                "job_base_factor_id": 2,
                                "competency_name": "Problem solving",
                                "factor_name": "Analytical thinking",
                            }
                        ],
                    }
                ],
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["evidence_strength_summary"]["label"], "활용 가능한 근거")
        self.assertEqual(course["evidence_highlights"]["source_ksa"], ["workforce planning"])
        self.assertEqual(course["evidence_highlights"]["gap_ksa"], ["labor law"])
        self.assertEqual(course["evidence_highlights"]["goal_ksa"], ["HR strategy"])
        self.assertEqual(course["evidence_highlights"]["covered_elements"], ["Plan workforce"])
        self.assertEqual(course["evidence_highlights"]["career_path"], ["HR planning(Manager)"])
        self.assertEqual(course["evidence_highlights"]["qualifications"], ["HR specialist(unit)"])
        self.assertEqual(
            course["evidence_highlights"]["job_base"],
            ["Problem solving:Analytical thinking"],
        )
        self.assertEqual(course["job_base_signal"]["evidence_role"], "auxiliary_tie_breaker")
        self.assertEqual(course["job_base_signal"]["target_hit_count"], 1)
        self.assertEqual(course["quality_issue_penalty"]["issue_types"], ["broad_generic_ksa"])
        self.assertEqual(course["quality_issue_penalty"]["labels"], ["범용 KSA 과잉 연결 감점"])
        self.assertEqual(course["quality_issue_penalty"]["scoring_role"], "downweight_only")
        self.assertEqual(
            course["quality_issue_penalty"]["affected_concepts"][0],
            {
                "concept_id": 1,
                "concept_name": "workforce planning",
                "concept_type": "knowledge",
                "issue_types": ["broad_generic_ksa"],
            },
        )
        self.assertEqual(course["coverage_breakdown"]["goal_direct"], 1)
        self.assertEqual(course["coverage_breakdown"]["goal_token"], 2)
        self.assertEqual(course["coverage_breakdown"]["reviewed_goal_links"], 1)
        self.assertEqual(course["training_system_fit"]["rubric_role"], "framework_reference_not_scoring_source")
        self.assertEqual(course["training_system_fit"]["need_classification"]["code"], "required")
        self.assertEqual(course["training_system_fit"]["evidence_directness"]["code"], "training_goal_direct")
        self.assertIn("training_goal_ksa", course["training_system_fit"]["task_ksa_basis"]["basis_types"])
        self.assertEqual(course["training_system_fit"]["task_ksa_basis"]["training_goal_ksa"], ["HR strategy"])
        self.assertTrue(any("KSA 품질 감점" in line for line in course["why_recommended"]))
        self.assertTrue(any("훈련목표 KSA: HR strategy" in line for line in course["why_recommended"]))
        self.assertTrue(any("근거 방식" in line for line in course["why_recommended"]))

    def test_training_system_fit_demotes_primary_unit_scope_only(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendations": [
                    {
                        "rank": 1,
                        "recommendation_tier": {"tier": "primary"},
                        "training_course": {
                            "training_course_id": 100,
                            "compe_unit_name": "Unit-name-only course",
                        },
                        "confidence_score": 0.72,
                        "confidence_grade": "medium",
                        "match": {"direct_unit_evidence": True},
                        "coverage_counts": {"source_ksa": 0, "gap_ksa": 0, "goal_ksa": 0},
                    }
                ],
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]

        self.assertEqual(course["tier"], "primary")
        self.assertEqual(course["training_system_fit"]["need_classification"]["code"], "supporting")
        self.assertEqual(course["training_system_fit"]["evidence_directness"]["code"], "unit_scope")
        self.assertIn(
            "primary_demoted_without_direct_task_ksa_or_goal",
            course["training_system_fit"]["review_flags"],
        )
        self.assertIn(
            "unit_scope_without_task_ksa_or_goal",
            course["training_system_fit"]["review_flags"],
        )

    def test_training_system_fit_demotes_primary_unit_scope_with_scope_ksa_only(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "Scope-overlap course",
                            "training_course_id": 102,
                            "confidence_score": 0.82,
                            "confidence_grade": "high",
                            "match": {"direct_unit_evidence": True},
                            "coverage_counts": {"source_ksa": 2, "gap_ksa": 0, "goal_ksa": 0},
                            "evidence_highlights": {"source_ksa": ["workforce planning"]},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "General affairs",
                        "requested_target_query": "HR planning",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 1,
                    },
                    "current_scope": {"match_level": "unit", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "unit", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        fit = compact["recommended_courses"][0]["training_system_fit"]

        self.assertEqual(fit["need_classification"]["code"], "supporting")
        self.assertEqual(fit["evidence_directness"]["code"], "unit_scope")
        self.assertIn("target_scope_ksa", fit["task_ksa_basis"]["basis_types"])
        self.assertIn("primary_demoted_without_direct_task_ksa_or_goal", fit["review_flags"])
        self.assertIn("unit_scope_without_task_ksa_or_goal", fit["review_flags"])

    def test_training_system_fit_flags_adjacent_ksa_overlap_for_review(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {"match_text": "HR planning", "match_level": "unit"},
                "recommendation_summary": {},
                "recommendations": [
                    {
                        "rank": 1,
                        "recommendation_tier": {"tier": "adjacent"},
                        "training_course": {
                            "training_course_id": 101,
                            "compe_unit_name": "Adjacent KSA overlap course",
                        },
                        "confidence_score": 0.32,
                        "confidence_grade": "low",
                        "match": {},
                        "coverage_counts": {"source_ksa": 0, "gap_ksa": 1, "goal_ksa": 0},
                        "evidence_highlights": {"gap_ksa": ["HR policy"]},
                    }
                ],
                "audit": {},
            },
            recommendation_limit=1,
        )

        flags = compact["recommended_courses"][0]["training_system_fit"]["review_flags"]

        self.assertIn("adjacent_reference_only", flags)
        self.assertIn("adjacent_ksa_overlap_requires_review", flags)

    def test_compact_evidence_highlights_do_not_show_unmatched_ksa_candidates(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendations": [
                    {
                        "rank": 1,
                        "recommendation_tier": {"tier": "supplemental"},
                        "training_course": {
                            "training_course_id": 100,
                            "compe_unit_name": "Adjacent course",
                        },
                        "confidence_score": 0.45,
                        "confidence_grade": "low",
                        "evidence_strength": {"grade": "low"},
                        "match": {
                            "source_concept_hits": 0,
                            "gap_concept_hits": 0,
                            "goal_concept_hits": 0,
                        },
                        "source_ksa_concepts": [
                            {"concept_id": 1, "concept_name": "workforce planning", "concept_type": "knowledge"}
                        ],
                        "gap_ksa_concepts": [
                            {"concept_id": 2, "concept_name": "labor law", "concept_type": "knowledge"}
                        ],
                        "covered_elements": [],
                        "goal_coverage": [],
                        "qualification_evidence": [
                            {"jm_cd": "Q1", "jm_nm": "HR specialist", "ablt_unit_typ_nm": "unit"}
                        ],
                        "job_base_evidence": [
                            {
                                "job_base_competency_id": 1,
                                "job_base_factor_id": 2,
                                "competency_name": "Problem solving",
                                "factor_name": "Analytical thinking",
                            }
                        ],
                    }
                ],
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["coverage_counts"]["source_ksa"], 0)
        self.assertNotIn("source_ksa", course["evidence_highlights"])
        self.assertNotIn("gap_ksa", course["evidence_highlights"])
        self.assertNotIn("qualifications", course["evidence_highlights"])
        self.assertNotIn("job_base", course["evidence_highlights"])
        self.assertFalse(any("workforce planning" in line for line in course["why_recommended"]))

    def test_compact_task_response_adds_candidate_query_guidance_for_short_query(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "인",
                "query_resolution": {
                    "ok": True,
                    "query": "인",
                    "normalized_query": "인",
                    "candidates": [
                        {
                            "candidate_type": "unit",
                            "match_level": "competency_unit",
                            "matched_text": "인사기획",
                            "unit_code": "0202020101_23v3",
                            "unit_name": "인사기획",
                            "confidence_score": 0.68,
                        },
                        {
                            "candidate_type": "element",
                            "match_level": "competency_element",
                            "matched_text": "인력운영계획 수립",
                            "unit_code": "0202020101_23v3",
                            "element_id": 10,
                            "confidence_score": 0.58,
                        },
                        {
                            "candidate_type": "unit",
                            "match_level": "competency_unit",
                            "matched_text": "공적개발원조사업 인력관리",
                            "unit_code": "0101010101_23v1",
                            "unit_name": "공적개발원조사업 인력관리",
                            "confidence_score": 0.68,
                        },
                    ],
                },
                "source_task": {},
                "resolved_scope": {
                    "match_text": "인사기획",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        self.assertFalse(compact["input_quality"]["ok"])
        self.assertEqual(
            compact["input_quality"]["candidate_queries"]["query"][0]["query"],
            "인사기획",
        )
        self.assertNotIn(
            "공적개발원조사업 인력관리",
            [item["query"] for item in compact["input_quality"]["candidate_queries"]["query"]],
        )
        self.assertTrue(
            any("인사기획" in suggestion for suggestion in compact["input_quality"]["suggestions"])
        )

    def test_short_query_resolution_prefers_prefix_candidates(self) -> None:
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
                    ) VALUES ('01', 'Domain A', '01', 'Middle A',
                              '01', 'Small A', '01', 'xalpha')
                    """
                )
                xalpha_classification_id = conn.execute(
                    "SELECT classification_id FROM classifications WHERE sub_name = 'xalpha'"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Domain B', '01', 'Middle B',
                              '01', 'Small B', '01', 'alpha')
                    """
                )
                alpha_classification_id = conn.execute(
                    "SELECT classification_id FROM classifications WHERE sub_name = 'alpha'"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0101010101_23v1', '0101010101', '23v1', 'xalpha unit',
                              '3', ?, ?, ?)
                    """,
                    (xalpha_classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, created_at, updated_at
                    ) VALUES ('0201010101_23v1', '0201010101', '23v1', 'alpha unit',
                              '3', ?, ?, ?)
                    """,
                    (alpha_classification_id, timestamp, timestamp),
                )
                conn.commit()

                result = resolve_ncs_query_scope(conn, "a", limit=5)
            finally:
                conn.close()

        self.assertEqual(result["candidates"][0]["matched_text"], "alpha")

    def test_candidate_score_keeps_direct_and_close_typo_matches(self) -> None:
        self.assertGreater(_candidate_score("HR planning", "HR"), 0.0)
        self.assertGreater(_candidate_score("qualification requirement", "qualification requirment"), 0.0)

    def test_candidate_score_skips_unrelated_edit_distance_work(self) -> None:
        self.assertFalse(_candidate_allows_edit_distance("budget control report", "training"))
        self.assertEqual(_candidate_score("budget control report", "training"), 0.0)

    def test_recommend_training_for_task_rejects_one_character_query_before_ranking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                seed_task_ontology(conn)
                result = recommend_training_for_task(conn, query="a", save=False)
            finally:
                conn.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["input_quality"]["warnings"][0]["field"], "query")
        self.assertNotIn("recommendations", result)
        self.assertIn("query_resolution", result)

    def test_compact_training_task_response_has_task_shape(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "disclaimer": "test disclaimer",
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {
                    "criteria_id": 1,
                    "criteria_text": "plan workforce",
                    "unit_code": "0202020101_23v3",
                    "unit_name": "HR planning",
                    "element_id": 2,
                    "element_name": "Planning",
                    "classification": {"major_code": "02"},
                },
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "source_unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {
                    "recommended_training_courses_count": 1,
                    "preferred_max_hours": 16,
                    "preferred_methods": ["Practice"],
                },
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "gaps": {"missing_concepts": [{"concept_name": "workforce analysis", "concept_type": "knowledge"}]},
                "audit": {"sqf_used": False, "learning_modules_used": False, "data_sources": ["ncs_training_courses"]},
            },
            recommendation_limit=1,
        )

        self.assertEqual(compact["view"], "compact_training_task")
        self.assertEqual(compact["requested"]["preferred_max_hours"], 16)
        self.assertEqual(compact["scope_interpretation"]["unit_count"], 1)
        self.assertTrue(compact["input_quality"]["ok"])
        self.assertNotIn("transition", compact)
        self.assertNotIn("recommendations", compact)
        self.assertEqual(compact["recommended_courses"][0]["course_name"], "HR planning")
        self.assertEqual(compact["recommended_courses"][0]["facility_constraint_fit"]["status"], "not_requested")
        self.assertEqual(compact["recommended_courses"][0]["human_review"]["action"], "review_training_course_card")
        self.assertFalse(compact["recommended_courses"][0]["human_review"]["approval_claim"])
        self.assertEqual(
            compact["recommended_courses"][0]["training_system_fit"]["facility_constraint_fit"]["status"],
            "not_requested",
        )
        self.assertEqual(
            compact["recommended_courses"][0]["training_system_fit"]["human_review"]["action"],
            "review_training_course_card",
        )
        self.assertFalse(compact["audit"]["sqf_used"])
        self.assertFalse(compact["audit"]["learning_modules_used"])
        self.assertEqual(compact["audit"]["data_sources"], ["ncs_training_courses"])
        self.assertIn("SQF", compact["audit"]["excluded_legacy_sources"])

    def test_compact_training_task_response_exposes_facility_fit_when_requested(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "source_unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {
                    "preferred_facilities": ["Lab"],
                },
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {"goal_ksa": 1},
                            "evidence_highlights": {"goal_ksa": ["HR strategy"]},
                            "delivery": {"facilities": ["Lab"], "methods": ["Practice"]},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {"sqf_used": False, "learning_modules_used": False},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["facility_constraint_fit"]["status"], "fit")
        self.assertEqual(course["facility_constraint_fit"]["requested"], ["Lab"])
        self.assertEqual(course["facility_constraint_fit"]["matched"], ["Lab"])
        self.assertEqual(course["training_system_fit"]["facility_constraint_fit"]["status"], "fit")

    def test_compact_training_task_response_normalizes_string_facility_evidence(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "source_unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {
                    "preferred_facilities": "Workshop / Simulation Room",
                },
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {"goal_ksa": 1},
                            "evidence_highlights": {"goal_ksa": ["HR strategy"]},
                            "delivery": {
                                "facilities": "Classroom; Workshop / Simulation Room",
                                "methods": "Lecture; Practice",
                            },
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {"sqf_used": False, "learning_modules_used": False},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["delivery"]["facilities"], ["Classroom", "Workshop", "Simulation Room"])
        self.assertEqual(course["delivery"]["methods"], ["Lecture", "Practice"])
        self.assertEqual(course["facility_constraint_fit"]["status"], "fit")
        self.assertEqual(course["facility_constraint_fit"]["requested"], ["Workshop", "Simulation Room"])
        self.assertEqual(course["facility_constraint_fit"]["matched"], ["Workshop", "Simulation Room"])

    def test_compact_training_task_response_flags_facility_mismatch_for_review(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "source_unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {
                    "preferred_facilities": ["Lab"],
                },
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "coverage_counts": {"goal_ksa": 1},
                            "evidence_highlights": {"goal_ksa": ["HR strategy"]},
                            "delivery": {"facilities": ["Classroom"], "methods": ["Lecture"]},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {"sqf_used": False, "learning_modules_used": False},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["facility_constraint_fit"]["status"], "mismatch")
        self.assertEqual(course["human_review"]["severity"], "needs_review")
        self.assertIn("delivery:facility_mismatch", course["human_review"]["flags"])
        self.assertIn("facility_constraint_mismatch", course["human_review"]["flags"])
        self.assertIn("delivery:facility_mismatch", course["training_system_fit"]["review_flags"])
        self.assertIn("facility_constraint_mismatch", course["training_system_fit"]["review_flags"])

    def test_compact_training_task_response_flags_partial_facility_fit_for_review(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "HR planning",
                "query_resolution": {"ok": True},
                "source_task": {},
                "resolved_scope": {
                    "match_text": "HR planning",
                    "match_level": "source_unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {
                    "preferred_facilities": ["Lab", "Workshop"],
                },
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "coverage_counts": {"goal_ksa": 1},
                            "evidence_highlights": {"goal_ksa": ["HR strategy"]},
                            "delivery": {"facilities": ["Lab"], "methods": ["Practice"]},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {"sqf_used": False, "learning_modules_used": False},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["facility_constraint_fit"]["status"], "partial")
        self.assertEqual(course["facility_constraint_fit"]["matched"], ["Lab"])
        self.assertEqual(course["facility_constraint_fit"]["missing"], ["Workshop"])
        self.assertIn("delivery:facility_partial", course["human_review"]["flags"])
        self.assertIn("facility_constraint_partial", course["human_review"]["flags"])
        self.assertIn("delivery:facility_partial", course["training_system_fit"]["review_flags"])
        self.assertIn("facility_constraint_partial", course["training_system_fit"]["review_flags"])

    def test_compact_training_transition_response_uses_summary_facility_preferences(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "current_query": "General affairs",
                "target_query": "HR planning",
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning bridge",
                            "training_course_id": 101,
                            "confidence_score": 0.88,
                            "confidence_grade": "high",
                            "coverage_counts": {"source_ksa": 1, "goal_ksa": 1},
                            "evidence_highlights": {
                                "source_ksa": ["workforce planning"],
                                "goal_ksa": ["HR strategy"],
                            },
                            "delivery": {"facilities": ["Classroom"], "methods": ["Lecture"]},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "General affairs",
                        "requested_target_query": "HR planning",
                        "preferred_facilities": ["Lab"],
                    },
                    "current_scope": {},
                    "target_scope": {},
                },
                "audit": {"sqf_used": False, "learning_modules_used": False},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(compact["requested"]["preferred_facilities"], ["Lab"])
        self.assertEqual(course["facility_constraint_fit"]["status"], "mismatch")
        self.assertEqual(course["facility_constraint_fit"]["requested"], ["Lab"])
        self.assertEqual(course["facility_constraint_fit"]["available"], ["Classroom"])
        self.assertEqual(course["human_review"]["severity"], "needs_review")
        self.assertIn("delivery:facility_mismatch", course["human_review"]["flags"])
        self.assertIn("facility_constraint_mismatch", course["human_review"]["flags"])
        self.assertEqual(
            course["training_system_fit"]["facility_constraint_fit"]["status"],
            "mismatch",
        )

    def test_recommendation_exposes_supplemental_csv_evidence_as_context_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning and workforce analysis practice.",
                        "train_time": "24",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            before_result = recommend_training_for_task(conn, unit_code=str(fixture["unit_code"]), limit=1, save=False)
            conn.execute(
                """
                INSERT INTO ncs_unit_standard_training(
                    source_file, source_row_number, unit_code_raw, unit_name,
                    unit_level, standard_training_hours, matched_unit_code,
                    match_status, created_at, updated_at
                ) VALUES ('unit.csv', 2, ?, 'HR planning', '5', 24, ?, 'matched_unit_exact', ?, ?)
                """,
                (fixture["unit_code"], fixture["unit_code"], timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_external_training_zip_courses(
                    source_file, source_row_number, course_name, business_type,
                    institution_name, ncs_code_raw, ncs_code_normalized, ncs_code_level,
                    ncs_major_code, ncs_middle_code, ncs_small_code, ncs_sub_code,
                    training_method, training_hours, match_status, created_at, updated_at
                ) VALUES (
                    'zip.csv', 2, 'HR bridge catalog course', 'consortium',
                    'Test institution', '020202', '020202', 'small',
                    '02', '02', '02', NULL, 'Practice', 16,
                    'matched_small_scope', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_occupation_code_mappings(
                    source_file, source_row_number, ncs_code_raw, ncs_code_normalized,
                    ncs_code_level, ncs_code_name, national_job_code, national_job_name,
                    keco_code, keco_name, match_status, created_at, updated_at
                ) VALUES (
                    'mapping.csv', 2, '020202', '020202', 'small', 'HRM',
                    '1000001', 'HR specialist', '0261', 'HR clerk',
                    'matched_small_scope', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            result = recommend_training_for_task(conn, unit_code=str(fixture["unit_code"]), limit=1, save=False)
            compact = compact_training_task_response(result, recommendation_limit=1)
            conn.close()

        self.assertTrue(before_result["ok"])
        self.assertTrue(result["ok"])
        self.assertEqual(
            before_result["recommendations"][0]["confidence_score"],
            result["recommendations"][0]["confidence_score"],
        )
        self.assertEqual(
            before_result["recommendations"][0]["recommendation_tier"]["tier"],
            result["recommendations"][0]["recommendation_tier"]["tier"],
        )
        self.assertIn("ncs_unit_standard_training", result["audit"]["data_sources"])
        self.assertIn("ncs_external_training_zip_courses", result["audit"]["data_sources"])
        self.assertIn("ncs_occupation_code_mappings", result["audit"]["data_sources"])
        evidence = compact["recommended_courses"][0]["supplemental_evidence"]
        self.assertEqual(evidence["scoring_role"], "context_only")
        self.assertFalse(evidence["used_for_scoring"])
        self.assertEqual(evidence["standard_training"]["time_alignment"], "matches_standard")
        self.assertFalse(evidence["standard_training"]["used_for_scoring"])
        self.assertEqual(evidence["standard_training"]["hours_delta"], 0.0)
        self.assertEqual(evidence["external_training_catalog"]["matched_course_count"], 1)
        self.assertFalse(evidence["external_training_catalog"]["used_for_scoring"])
        self.assertEqual(evidence["occupation_code_mappings"]["mapping_count"], 1)
        self.assertFalse(evidence["occupation_code_mappings"]["used_for_scoring"])
        self.assertIn("ncs_unit_standard_training", compact["audit"]["data_sources"])
        compact_json = json.dumps(compact["recommended_courses"][0], ensure_ascii=False)
        self.assertNotIn("source_file", compact_json)
        self.assertNotIn("source_payload", compact_json)

    def test_review_training_transition_scenarios_promotes_only_eligible_cases_when_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'eligible_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning"]',
                    'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'low_recall_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning", "Workforce planning"]',
                    'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'top1_miss_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning"]',
                    'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            scenario_ids = {
                row["scenario_name"]: row["scenario_id"]
                for row in conn.execute(
                    "SELECT scenario_id, scenario_name FROM training_transition_gold_scenarios"
                ).fetchall()
            }
            scenario_id = scenario_ids["eligible_transition"]
            low_recall_scenario_id = scenario_ids["low_recall_transition"]
            top1_miss_scenario_id = scenario_ids["top1_miss_transition"]
            evaluation = {
                "ok": True,
                "scenario_count": 3,
                "cases": [
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": "eligible_transition",
                        "review_status": "candidate",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": True,
                        "precision_at_k": 1.0,
                        "expected_recall_at_k": 1.0,
                        "first_expected_rank": 1,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["HR planning"],
                    },
                    {
                        "scenario_id": low_recall_scenario_id,
                        "scenario_name": "low_recall_transition",
                        "review_status": "candidate",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": True,
                        "precision_at_k": 1.0,
                        "expected_recall_at_k": 0.5,
                        "first_expected_rank": 1,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["HR planning"],
                    },
                    {
                        "scenario_id": top1_miss_scenario_id,
                        "scenario_name": "top1_miss_transition",
                        "review_status": "candidate",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": False,
                        "precision_at_k": 0.5,
                        "expected_recall_at_k": 1.0,
                        "first_expected_rank": 2,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["Other course", "HR planning"],
                    }
                ],
            }
            with patch("ncs_mcp.training_recommendation.evaluate_training_transition_scenarios", return_value=evaluation):
                dry_run = review_training_transition_scenarios(conn, apply=False)
                status_after_dry_run = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (scenario_id,),
                ).fetchone()["review_status"]
                audit_count_after_dry_run = conn.execute(
                    "SELECT COUNT(*) AS count FROM training_transition_scenario_reviews"
                ).fetchone()["count"]
                blocked_apply = review_training_transition_scenarios(
                    conn,
                    apply=True,
                    reviewer_id="user_directed_report_review_20260619",
                    source_decision_packet="reports/transition_packet.md",
                    source_artifact_hash="sha256:test",
                    rationale="Report-based transition scenario review.",
                    evidence_refs=["scenario:eligible_transition", "course:HR planning"],
                    run_artifact="reports/transition_review_run.json",
                )
                status_after_blocked_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (scenario_id,),
                ).fetchone()["review_status"]
                audit_count_after_blocked_apply = conn.execute(
                    "SELECT COUNT(*) AS count FROM training_transition_scenario_reviews"
                ).fetchone()["count"]
                off_repo_packet = Path(tmp) / "external" / "reports" / "transition_packet.md"
                off_repo_packet.parent.mkdir(parents=True, exist_ok=True)
                off_repo_packet.write_text("off repo packet\n", encoding="utf-8")
                blocked_nonportable_packet = review_training_transition_scenarios(
                    conn,
                    apply=True,
                    allow_automated_status_write=True,
                    reviewer_id="user_directed_report_review_20260619",
                    source_decision_packet=str(off_repo_packet),
                    source_artifact_hash="sha256:test",
                    rationale="Report-based transition scenario review.",
                    evidence_refs=["scenario:eligible_transition", "course:HR planning"],
                    run_artifact="reports/transition_review_run.json",
                )
                audit_count_after_nonportable_packet = conn.execute(
                    "SELECT COUNT(*) AS count FROM training_transition_scenario_reviews"
                ).fetchone()["count"]
                applied = review_training_transition_scenarios(
                    conn,
                    apply=True,
                    allow_automated_status_write=True,
                    reviewer_id="user_directed_report_review_20260619",
                    source_decision_packet="reports/transition_packet.md",
                    source_artifact_hash="sha256:test",
                    rationale="Report-based transition scenario review.",
                    evidence_refs=["scenario:eligible_transition", "course:HR planning"],
                    run_artifact="reports/transition_review_run.json",
                )
                status_after_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (scenario_id,),
                ).fetchone()["review_status"]
                low_recall_status_after_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (low_recall_scenario_id,),
                ).fetchone()["review_status"]
                top1_miss_status_after_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (top1_miss_scenario_id,),
                ).fetchone()["review_status"]
                trusted_top1_override = review_training_transition_scenarios(
                    conn,
                    target_review_status="reviewed",
                    require_top1_expected_hit=False,
                    apply=False,
                )
                audit_rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT scenario_id, source_review_status, target_review_status,
                               eligible, status_updated, blockers_json, criteria_json, metrics_json
                        FROM training_transition_scenario_reviews
                        ORDER BY review_id
                        """
                    ).fetchall()
                ]
                generic_audit_rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT entity_type, entity_id, action, previous_status, new_status,
                               reviewer_id, notes, source_decision_packet, source_artifact_hash,
                               rationale, evidence_refs_json, created_by_tool, run_artifact
                        FROM review_audit_log
                        WHERE action = 'review_training_transition_scenarios'
                        ORDER BY id
                        """
                    ).fetchall()
                ]
            conn.close()

        self.assertEqual(dry_run["eligible_count"], 1)
        self.assertEqual(dry_run["updated_count"], 0)
        self.assertEqual(status_after_dry_run, "candidate")
        self.assertEqual(audit_count_after_dry_run, 0)
        self.assertFalse(blocked_apply["ok"])
        self.assertIn(
            "automated_status_updates_require_explicit_opt_in",
            blocked_apply["provenance_blockers"],
        )
        self.assertEqual(status_after_blocked_apply, "candidate")
        self.assertEqual(audit_count_after_blocked_apply, 0)
        self.assertFalse(blocked_nonportable_packet["ok"])
        self.assertIn(
            "source_decision_packet_must_be_portable_reports_ref",
            blocked_nonportable_packet["provenance_blockers"],
        )
        self.assertEqual(audit_count_after_nonportable_packet, 0)
        self.assertEqual(applied["updated_count"], 1)
        self.assertTrue(applied["status_update_allowed"])
        self.assertTrue(applied["db_writes"])
        self.assertEqual(status_after_apply, "candidate_auto")
        self.assertEqual(low_recall_status_after_apply, "candidate")
        self.assertEqual(top1_miss_status_after_apply, "candidate")
        cases_by_name = {case["scenario_name"]: case for case in applied["cases"]}
        self.assertIn("expected_recall_below_threshold", cases_by_name["low_recall_transition"]["blockers"])
        self.assertIn("top1_expected_course_miss", cases_by_name["top1_miss_transition"]["blockers"])
        self.assertIn(
            "top1_expected_course_miss",
            {case["scenario_name"]: case for case in trusted_top1_override["cases"]}[
                "top1_miss_transition"
            ]["blockers"],
        )
        self.assertTrue(trusted_top1_override["criteria"]["require_top1_expected_hit"])
        self.assertEqual(len(audit_rows), 3)
        audit_by_id = {row["scenario_id"]: row for row in audit_rows}
        self.assertEqual(audit_by_id[scenario_id]["status_updated"], 1)
        self.assertEqual(audit_by_id[scenario_id]["target_review_status"], "candidate_auto")
        self.assertEqual(audit_by_id[low_recall_scenario_id]["status_updated"], 0)
        self.assertIn(
            "expected_recall_below_threshold",
            json.loads(audit_by_id[low_recall_scenario_id]["blockers_json"]),
        )
        self.assertFalse(json.loads(audit_by_id[scenario_id]["criteria_json"])["trusted_target_status"])
        self.assertEqual(json.loads(audit_by_id[scenario_id]["metrics_json"])["expected_recall_at_k"], 1.0)
        self.assertEqual(applied["review_method"], "automated_eval_gate")
        self.assertEqual(len(generic_audit_rows), 1)
        self.assertEqual(generic_audit_rows[0]["entity_type"], "training_transition_gold_scenario")
        self.assertEqual(generic_audit_rows[0]["entity_id"], str(scenario_id))
        self.assertEqual(generic_audit_rows[0]["previous_status"], "candidate")
        self.assertEqual(generic_audit_rows[0]["new_status"], "candidate_auto")
        self.assertEqual(generic_audit_rows[0]["reviewer_id"], "user_directed_report_review_20260619")
        self.assertEqual(generic_audit_rows[0]["source_decision_packet"], "reports/transition_packet.md")
        self.assertEqual(generic_audit_rows[0]["source_artifact_hash"], "sha256:test")
        self.assertEqual(generic_audit_rows[0]["rationale"], "Report-based transition scenario review.")
        self.assertEqual(
            json.loads(generic_audit_rows[0]["evidence_refs_json"]),
            ["scenario:eligible_transition", "course:HR planning"],
        )
        self.assertEqual(
            generic_audit_rows[0]["created_by_tool"],
            "ncs_harness.review-training-transition-scenarios",
        )
        self.assertEqual(generic_audit_rows[0]["run_artifact"], "reports/transition_review_run.json")

    def test_review_training_transition_scenarios_does_not_audit_same_status_apply(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'already_auto_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning"]',
                    'candidate_auto', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            scenario_id = conn.execute(
                "SELECT scenario_id FROM training_transition_gold_scenarios"
            ).fetchone()["scenario_id"]
            evaluation = {
                "ok": True,
                "scenario_count": 1,
                "cases": [
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": "already_auto_transition",
                        "review_status": "candidate_auto",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": True,
                        "precision_at_k": 1.0,
                        "expected_recall_at_k": 1.0,
                        "first_expected_rank": 1,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["HR planning"],
                    }
                ],
            }
            with patch("ncs_mcp.training_recommendation.evaluate_training_transition_scenarios", return_value=evaluation):
                result = review_training_transition_scenarios(
                    conn,
                    source_review_statuses=["candidate_auto"],
                    target_review_status="candidate_auto",
                    apply=True,
                    allow_automated_status_write=True,
                )
                review_row = conn.execute(
                    """
                    SELECT status_updated
                    FROM training_transition_scenario_reviews
                    WHERE scenario_id = ?
                    """,
                    (scenario_id,),
                ).fetchone()
                audit_count = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM review_audit_log
                    WHERE action = 'review_training_transition_scenarios'
                    """
                ).fetchone()["count"]
            conn.close()

        self.assertTrue(result["ok"])
        self.assertEqual(result["eligible_count"], 1)
        self.assertEqual(result["updated_count"], 0)
        self.assertFalse(result["status_update_allowed"])
        self.assertTrue(result["db_writes"])
        self.assertEqual(review_row["status_updated"], 0)
        self.assertEqual(audit_count, 0)

    def test_review_training_transition_scenarios_blocks_trusted_apply_without_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'eligible_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning"]',
                    'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            scenario_id = conn.execute(
                "SELECT scenario_id FROM training_transition_gold_scenarios"
            ).fetchone()["scenario_id"]
            evaluation = {
                "ok": True,
                "scenario_count": 1,
                "cases": [
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": "eligible_transition",
                        "review_status": "candidate",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": True,
                        "precision_at_k": 1.0,
                        "expected_recall_at_k": 1.0,
                        "first_expected_rank": 1,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["HR planning"],
                    }
                ],
            }
            with patch("ncs_mcp.training_recommendation.evaluate_training_transition_scenarios", return_value=evaluation):
                result = review_training_transition_scenarios(
                    conn,
                    target_review_status="reviewed",
                    apply=True,
                )
                status_after_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (scenario_id,),
                ).fetchone()["review_status"]
                review_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM training_transition_scenario_reviews"
                ).fetchone()["count"]
                audit_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM review_audit_log"
                ).fetchone()["count"]
            conn.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(status_after_apply, "candidate")
        self.assertEqual(review_count, 0)
        self.assertEqual(audit_count, 0)
        self.assertIn(
            "trusted_status_updates_require_human_decision_import",
            result["provenance_blockers"],
        )
        self.assertIn(
            "trusted_status_requires_explicit_human_reviewer_id",
            result["provenance_blockers"],
        )
        self.assertIn(
            "trusted_status_requires_source_decision_packet",
            result["provenance_blockers"],
        )
        self.assertIn(
            "trusted_status_requires_rationale",
            result["provenance_blockers"],
        )

    def test_review_training_transition_scenarios_blocks_trusted_apply_with_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'eligible_transition', 'General affairs', 'HR planning', '02',
                    'General affairs', 'HR planning', '["HR planning"]',
                    'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )
            scenario_id = conn.execute(
                "SELECT scenario_id FROM training_transition_gold_scenarios"
            ).fetchone()["scenario_id"]
            evaluation = {
                "ok": True,
                "scenario_count": 1,
                "cases": [
                    {
                        "scenario_id": scenario_id,
                        "scenario_name": "eligible_transition",
                        "review_status": "candidate",
                        "ok": True,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                        "top1_expected_hit": True,
                        "precision_at_k": 1.0,
                        "expected_recall_at_k": 1.0,
                        "first_expected_rank": 1,
                        "expected_course_hits": ["HR planning"],
                        "recommended_courses": ["HR planning"],
                    }
                ],
            }
            with patch("ncs_mcp.training_recommendation.evaluate_training_transition_scenarios", return_value=evaluation):
                result = review_training_transition_scenarios(
                    conn,
                    target_review_status="reviewed",
                    apply=True,
                    reviewer_id="operator",
                    source_decision_packet="reports/operator-transition-review.md",
                    source_artifact_hash="sha256:test",
                    rationale="Human decision packet exists but automated trusted writes remain blocked.",
                    evidence_refs=["scenario:eligible_transition", "course:HR planning"],
                    run_artifact="reports/operator-transition-review-run.json",
                )
                status_after_apply = conn.execute(
                    "SELECT review_status FROM training_transition_gold_scenarios WHERE scenario_id = ?",
                    (scenario_id,),
                ).fetchone()["review_status"]
                review_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM training_transition_scenario_reviews"
                ).fetchone()["count"]
                audit_count = conn.execute(
                    "SELECT COUNT(*) AS count FROM review_audit_log"
                ).fetchone()["count"]
            conn.close()

        self.assertFalse(result["ok"])
        self.assertEqual(result["updated_count"], 0)
        self.assertEqual(status_after_apply, "candidate")
        self.assertEqual(review_count, 0)
        self.assertEqual(audit_count, 0)
        self.assertEqual(
            result["provenance_blockers"],
            ["trusted_status_updates_require_human_decision_import"],
        )

    def test_compact_task_accepts_unit_or_criteria_locator_without_query(self) -> None:
        base_result = {
            "ok": True,
            "requested_query": None,
            "requested_criteria_id": None,
            "requested_unit_code": "0202020101_23v3",
            "query_resolution": {"ok": True},
            "source_task": {
                "criteria_id": 1,
                "criteria_text": "plan workforce",
                "unit_code": "0202020101_23v3",
                "unit_name": "HR planning",
            },
            "resolved_scope": {
                "match_text": "HR planning",
                "match_level": "source_unit",
                "unit_codes": ["0202020101_23v3"],
            },
            "recommendation_summary": {},
            "recommendation_groups": {
                "primary": [
                    {
                        "rank": 1,
                        "course_name": "HR planning",
                        "training_course_id": 100,
                        "confidence_score": 0.9,
                        "confidence_grade": "high",
                        "evidence_strength": {"grade": "high"},
                        "coverage_counts": {},
                        "delivery": {},
                    }
                ],
                "supplemental": [],
                "adjacent": [],
            },
            "audit": {},
        }
        unit_compact = compact_training_task_response(base_result, recommendation_limit=1)
        criteria_result = dict(base_result)
        criteria_result["requested_unit_code"] = None
        criteria_result["requested_criteria_id"] = 1
        criteria_compact = compact_training_task_response(criteria_result, recommendation_limit=1)

        for compact in [unit_compact, criteria_compact]:
            warning_codes = {warning["code"] for warning in compact["input_quality"]["warnings"]}
            self.assertTrue(compact["input_quality"]["ok"])
            self.assertNotIn("empty_query", warning_codes)
        self.assertEqual(unit_compact["requested"]["unit_code"], "0202020101_23v3")
        self.assertEqual(criteria_compact["requested"]["criteria_id"], 1)

    def test_compact_transition_response_flags_broad_low_evidence_inputs(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [],
                    "supplemental": [
                        {
                            "rank": 1,
                            "course_name": "Weak bridge",
                            "training_course_id": 12,
                            "confidence_score": 0.2,
                            "confidence_grade": "low",
                            "evidence_strength": {"grade": "low", "label": "weak_evidence"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "A",
                        "requested_target_query": "Business",
                        "current_scope_unit_count": 30,
                        "target_scope_unit_count": 40,
                    },
                    "current_scope": {"match_level": "major", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "major", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        warning_codes = {warning["code"] for warning in compact["input_quality"]["warnings"]}
        self.assertFalse(compact["input_quality"]["ok"])
        self.assertIn("short_query", warning_codes)
        self.assertIn("broad_scope", warning_codes)
        self.assertIn("zero_primary_recommendations", warning_codes)
        self.assertIn("adjacent_reference_only", warning_codes)
        self.assertFalse(
            any("능력단위코드" in suggestion for suggestion in compact["input_quality"]["suggestions"])
        )

    def test_compact_transition_broad_scope_warning_fields_match_input_names(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {"primary": [], "supplemental": [], "adjacent": []},
                "transition": {
                    "summary": {
                        "requested_current_query": "A",
                        "requested_target_query": "Business",
                        "current_scope_unit_count": 30,
                        "target_scope_unit_count": 40,
                    },
                    "current_scope": {"match_level": "major", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "major", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        broad_fields = {
            warning["field"]
            for warning in compact["input_quality"]["warnings"]
            if warning["code"] == "broad_scope"
        }

        self.assertEqual(broad_fields, {"current_query", "target_query"})

    def test_compact_transition_card_labels_target_scope_ksa_coverage(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {"source_ksa": 3, "gap_ksa": 1, "goal_ksa": 2},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "General affairs",
                        "requested_target_query": "HR planning",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 1,
                    },
                    "current_scope": {"match_level": "unit", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "unit", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        course = compact["recommended_courses"][0]
        self.assertEqual(course["coverage_counts"]["target_scope_ksa"], 3)
        self.assertIn("목표 범위 KSA 근거 3개", course["coverage_summary"])

    def test_compact_transition_highlights_target_scope_ksa_names(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {"source_ksa": 1},
                            "evidence_highlights": {
                                "source_ksa": ["workforce planning"],
                                "gap_ksa": ["labor law"],
                            },
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "General affairs",
                        "requested_target_query": "HR planning",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 1,
                    },
                    "current_scope": {"match_level": "unit", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "unit", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        highlights = compact["recommended_courses"][0]["evidence_highlights"]
        self.assertEqual(highlights["target_scope_ksa"], ["workforce planning"])
        self.assertEqual(highlights["gap_ksa"], ["labor law"])
        self.assertNotIn("source_ksa", highlights)

    def test_compact_transition_response_adds_candidates_for_each_query(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "current_query_resolution": {
                    "ok": True,
                    "candidates": [
                        {"candidate_type": "unit", "matched_text": "총무", "unit_code": "0202010101_23v3"}
                    ],
                },
                "target_query_resolution": {
                    "ok": True,
                    "candidates": [
                        {"candidate_type": "unit", "matched_text": "인사기획", "unit_code": "0202020101_23v3"}
                    ],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "총",
                        "requested_target_query": "인",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 1,
                    },
                    "current_scope": {"match_level": "unit", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "unit", "unit_codes": ["u2"]},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        candidates = compact["input_quality"]["candidate_queries"]
        self.assertEqual(candidates["current_query"][0]["query"], "총무")
        self.assertEqual(candidates["target_query"][0]["query"], "인사기획")

    def test_compact_transition_response_includes_actual_answer_summary(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "Labor relations planning",
                            "training_course_id": 100,
                            "confidence_score": 0.95,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {"source_ksa": 3, "gap_ksa": 2, "goal_ksa": 1},
                            "evidence_highlights": {
                                "source_ksa": ["labor law"],
                                "gap_ksa": ["collective bargaining", "labor law"],
                                "goal_ksa": ["strategy"],
                            },
                            "delivery": {
                                "relations": [
                                    {"relation_type": "has_level", "numeric_value": 5},
                                    {"relation_type": "requires_time", "numeric_value": 16},
                                ],
                                "profile": {"methods": ["classroom", "practice"]},
                            },
                            "why_recommended": ["gap KSA: labor law"],
                            "source_payload": {"authKey": "SHOULD_NOT_LEAK"},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "service management",
                        "requested_target_query": "labor management",
                        "current_scope_unit_count": 1,
                        "target_scope_unit_count": 10,
                        "target_ksa_concept_count": 20,
                        "transferable_ksa_concept_count": 2,
                        "gap_ksa_concept_count": 18,
                        "current_job_base_count": 1,
                        "target_job_base_count": 2,
                        "transferable_job_base_count": 1,
                        "gap_job_base_count": 1,
                        "transferability_ratio": 0.1,
                    },
                    "current_scope": {"match_text": "Payroll", "match_level": "source_unit", "unit_codes": ["u1"]},
                    "target_scope": {
                        "match_text": "Labor management",
                        "match_level": "sub_classification",
                        "unit_codes": ["u2"],
                    },
                    "current_task": {"element_name": "Attendance management"},
                    "target_task": {"element_name": "Labor relations planning"},
                    "current_query_alias": {
                        "alias_text": "service management",
                        "normalized_query": "attendance management",
                        "unit_code": "u1",
                        "review_status": "candidate",
                    },
                    "current_query_resolution": {
                        "query": "service management",
                        "candidates": [
                            {
                                "candidate_type": "unit",
                                "match_level": "query_alias_unit",
                                "matched_text": "Payroll",
                                "unit_code": "u1",
                                "confidence_score": 0.9,
                            },
                            {
                                "candidate_type": "element",
                                "match_level": "competency_element",
                                "matched_text": "Attendance policy",
                                "unit_code": "u3",
                                "confidence_score": 0.7,
                            },
                            {
                                "candidate_type": "concept",
                                "match_level": "ontology_concept",
                                "matched_text": "Do not show concept",
                                "confidence_score": 1.0,
                            },
                        ],
                    },
                    "target_query_resolution": {
                        "query": "labor management",
                        "candidates": [
                            {
                                "candidate_type": "classification",
                                "match_level": "sub_classification",
                                "matched_text": "Labor management",
                                "confidence_score": 1.0,
                            },
                            {
                                "candidate_type": "unit",
                                "match_level": "competency_unit",
                                "matched_text": "Industrial relations",
                                "unit_code": "u4",
                                "confidence_score": 0.6,
                            },
                        ],
                    },
                },
                "current_job_base_profile": [
                    {
                        "job_base_competency_id": 1,
                        "job_base_factor_id": 10,
                        "competency_name": "Communication",
                        "factor_name": "Listening",
                    }
                ],
                "target_job_base_profile": [
                    {
                        "job_base_competency_id": 1,
                        "job_base_factor_id": 10,
                        "competency_name": "Communication",
                        "factor_name": "Listening",
                    },
                    {
                        "job_base_competency_id": 2,
                        "job_base_factor_id": 20,
                        "competency_name": "Information",
                        "factor_name": "Data processing",
                    },
                ],
                "audit": {},
            },
            recommendation_limit=1,
        )

        answer = compact["answer_summary"]
        answer_json = json.dumps(answer, ensure_ascii=False)
        self.assertIn("Labor relations planning", answer["headline"])
        self.assertEqual(answer["interpretation"]["current"]["resolved_as"], "attendance management (Payroll)")
        self.assertEqual(answer["interpretation"]["current"]["task_element"], "Attendance management")
        self.assertEqual(answer["interpretation"]["current"]["query_alias"]["normalized_query"], "attendance management")
        self.assertEqual(answer["interpretation"]["current"]["alternatives"][0]["query"], "Attendance policy")
        self.assertEqual(answer["interpretation"]["target"]["alternatives"][0]["query"], "Industrial relations")
        self.assertEqual(answer["recommended_path"][0]["course_name"], "Labor relations planning")
        self.assertEqual(answer["recommended_path"][0]["hours"], 16)
        self.assertIn("collective bargaining", answer["key_gap_ksa"])
        self.assertEqual(compact["job_base_transition_profile"]["transferable"], ["Communication:Listening"])
        self.assertEqual(compact["job_base_transition_profile"]["gaps"], ["Information:Data processing"])
        self.assertEqual(compact["job_base_transition_profile"]["scoring_role"], "auxiliary_tie_breaker_not_primary_evidence")
        self.assertTrue(any("attendance management" in caveat for caveat in answer["caveats"]))
        self.assertTrue(any("candidate" in caveat for caveat in answer["caveats"]))
        self.assertNotIn("Do not show concept", answer_json)
        self.assertNotIn("source_payload", answer_json)
        self.assertNotIn("SHOULD_NOT_LEAK", answer_json)
        self.assertNotIn('"review_status"', answer_json)

    def test_compact_transition_job_base_profile_reads_nested_raw_transition(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {"primary": [], "supplemental": [], "adjacent": []},
                "transition": {
                    "summary": {
                        "current_job_base_count": 1,
                        "target_job_base_count": 2,
                        "transferable_job_base_count": 1,
                        "gap_job_base_count": 1,
                    },
                    "current_job_base_profile": [
                        {
                            "job_base_competency_id": 1,
                            "job_base_factor_id": 10,
                            "competency_name": "Communication",
                            "factor_name": "Listening",
                        }
                    ],
                    "target_job_base_profile": [
                        {
                            "job_base_competency_id": 1,
                            "job_base_factor_id": 10,
                            "competency_name": "Communication",
                            "factor_name": "Listening",
                        },
                        {
                            "job_base_competency_id": 2,
                            "job_base_factor_id": 20,
                            "competency_name": "Information",
                            "factor_name": "Data processing",
                        },
                    ],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        profile = compact["job_base_transition_profile"]
        self.assertEqual(profile["schema"], "ncs_job_base_transition_profile_v1")
        self.assertEqual(profile["current_count"], 1)
        self.assertEqual(profile["target_count"], 2)
        self.assertEqual(profile["transferable"], ["Communication:Listening"])
        self.assertEqual(profile["gaps"], ["Information:Data processing"])
        self.assertIs(profile["db_writes"], False)

    def test_compact_transition_job_base_profile_preserves_zero_gap_summary(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {"primary": [], "supplemental": [], "adjacent": []},
                "transition": {
                    "summary": {
                        "current_job_base_count": 1,
                        "target_job_base_count": 2,
                        "transferable_job_base_count": 2,
                        "gap_job_base_count": 0,
                    },
                    "current_job_base_profile": [
                        {
                            "job_base_competency_id": 1,
                            "job_base_factor_id": 10,
                            "competency_name": "Communication",
                            "factor_name": "Listening",
                        }
                    ],
                    "target_job_base_profile": [
                        {
                            "job_base_competency_id": 1,
                            "job_base_factor_id": 10,
                            "competency_name": "Communication",
                            "factor_name": "Listening",
                        },
                        {
                            "job_base_competency_id": 2,
                            "job_base_factor_id": 20,
                            "competency_name": "Information",
                            "factor_name": "Data processing",
                        },
                    ],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        profile = compact["job_base_transition_profile"]
        self.assertEqual(profile["gap_count"], 0)
        self.assertEqual(profile["gaps"], [])
        self.assertEqual(profile["gap_label_status"], "not_applicable")
        self.assertIs(profile["review_required"], False)

    def test_compact_transition_job_base_profile_flags_summary_only_gap_counts(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {"primary": [], "supplemental": [], "adjacent": []},
                "transition": {
                    "summary": {
                        "current_job_base_count": 1,
                        "target_job_base_count": 3,
                        "transferable_job_base_count": 1,
                        "gap_job_base_count": 2,
                    },
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        profile = compact["job_base_transition_profile"]
        self.assertEqual(profile["profile_source"], "summary_only")
        self.assertEqual(profile["gap_count"], 2)
        self.assertEqual(profile["gaps"], [])
        self.assertEqual(profile["gap_label_status"], "summary_only_labels_unavailable")
        self.assertIs(profile["labels_unavailable"], True)
        self.assertIs(profile["review_required"], True)

    def test_compact_transition_group_card_preserves_quality_penalty_affected_concepts(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendations": [
                    {
                        "rank": 1,
                        "recommendation_tier": {"tier": "primary", "label": "Primary", "rationale": "Direct"},
                        "training_course": {"training_course_id": 1, "compe_unit_name": "Planning"},
                        "confidence_score": 0.7,
                        "confidence_grade": "medium",
                        "evidence_strength": {"grade": "medium"},
                        "match": {
                            "quality_issue_penalty": {
                                "applied": True,
                                "issue_types": ["short_ksa"],
                                "concept_ids": [7],
                                "concept_issue_types": {"7": ["short_ksa"]},
                                "multiplier": 0.6,
                            },
                        },
                        "matched_source_ksa_concepts": [
                            {"concept_id": 7, "concept_name": "planning", "concept_type": "knowledge"}
                        ],
                    }
                ],
                "transition": {"summary": {}},
                "audit": {},
            },
            recommendation_limit=1,
        )

        penalty = compact["recommended_courses"][0]["quality_issue_penalty"]
        self.assertEqual(
            penalty["affected_concepts"],
            [
                {
                    "concept_id": 7,
                    "concept_name": "planning",
                    "concept_type": "knowledge",
                    "issue_types": ["short_ksa"],
                }
            ],
        )

    def test_transition_case_course_evidence_preserves_quality_and_job_base_signals(self) -> None:
        evidence = _transition_case_course_evidence(
            [
                {
                    "rank": 1,
                    "training_course": {"training_course_id": 1, "compe_unit_name": "Planning"},
                    "confidence_score": 0.7,
                    "confidence_grade": "medium",
                    "recommendation_tier": {"tier": "primary", "label": "Primary", "rationale": "Direct"},
                    "match": {
                        "quality_issue_penalty": {
                            "applied": True,
                            "issue_types": ["short_ksa"],
                            "concept_ids": [7],
                            "concept_issue_types": {"7": ["short_ksa"]},
                            "multiplier": 0.6,
                        },
                        "job_base_signal": {
                            "status": "gap_bridge",
                            "target_hit_count": 1,
                            "gap_hit_count": 2,
                        },
                    },
                    "matched_source_ksa_concepts": [
                        {"concept_id": 7, "concept_name": "planning", "concept_type": "knowledge"}
                    ],
                }
            ],
            limit=1,
        )

        row = evidence[0]
        self.assertEqual(row["quality_issue_penalty"]["issue_types"], ["short_ksa"])
        self.assertEqual(row["quality_issue_penalty"]["affected_concepts"][0]["concept_id"], 7)
        self.assertEqual(row["job_base_signal"]["status"], "gap_bridge")
        self.assertIn("quality_issue:short_ksa", row["review_flags"])

    def test_compact_transition_response_preserves_relation_only_delivery_methods(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "training_course": {
                                "training_course_id": 10,
                                "compe_unit_name": "HR planning workshop",
                            },
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "delivery_evidence": {
                                "relations": [
                                    {"relation_type": "has_level", "numeric_value": 5},
                                    {"relation_type": "requires_time", "numeric_value": 16},
                                    {"relation_type": "delivered_by", "relation_value": "집체훈련"},
                                ]
                            },
                            "evidence_highlights": {"gap_ksa": ["workforce planning"]},
                            "coverage_counts": {"gap_ksa": 1},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "transition": {
                    "summary": {
                        "requested_current_query": "노무관리",
                        "requested_target_query": "인사기획",
                        "transferability_ratio": 0.2,
                        "gap_ksa_concept_count": 1,
                        "transferable_ksa_concept_count": 1,
                        "target_ksa_concept_count": 3,
                    },
                    "current_scope": {"match_text": "노무관리", "match_level": "source_unit"},
                    "target_scope": {"match_text": "인사기획", "match_level": "target_unit"},
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        self.assertEqual(compact["recommended_courses"][0]["delivery"]["methods"], ["집체훈련"])
        self.assertEqual(compact["answer_summary"]["recommended_path"][0]["methods"], ["집체훈련"])
        compact_json = json.dumps(compact, ensure_ascii=False)
        self.assertNotIn('"relations"', compact_json)
        self.assertNotIn('"relation_id"', compact_json)

    def test_compact_education_plan_response_wraps_transition_for_planners(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "disclaimer": "education guidance only",
            "answer_summary": {
                "interpretation": {
                    "current": {
                        "requested": "노무관리",
                        "resolved_as": "노무관리",
                        "match_level": "source_unit",
                        "unit_count": 1,
                    },
                    "target": {
                        "requested": "인사기획",
                        "resolved_as": "인사기획",
                        "match_level": "source_unit",
                        "unit_count": 1,
                    },
                },
                "transition_assessment": {
                    "summary": "일부 KSA는 전이되지만 목표 직무 KSA 보완이 필요합니다.",
                    "transferability_ratio": 0.2,
                    "exact_ksa_overlap_ratio": 0.1,
                    "ontology_adjusted_transferability_ratio": 0.2,
                    "ncs_scope_relation": "same_small_classification",
                    "adjusted_transferability_components": {
                        "exact_ksa_overlap_ratio": 0.1,
                        "classification_scope_score": 0.05,
                    },
                    "gap_ksa_count": 3,
                },
                "key_gap_ksa": ["workforce planning", "HR strategy"],
                "caveats": ["교육훈련 안내 목적입니다."],
            },
            "requested": {
                "current_query": "노무관리",
                "target_query": "인사기획",
                "preferred_max_hours": 24,
                "preferred_methods": ["집체훈련"],
            },
            "transition_summary": {
                "current_scope_unit_count": 1,
                "target_scope_unit_count": 1,
                "gap_ksa_concept_count": 3,
                "current_trusted_career_path_count": 2,
                "target_trusted_career_path_count": 1,
                "current_career_path_review_status_counts": {"human_reviewed": 2},
                "target_career_path_review_status_counts": {"human_reviewed": 1},
            },
            "source_recommendation_counts": {"primary": 1, "supplemental": 0, "adjacent": 1},
            "recommended_courses": [
                {
                    "rank": 1,
                    "course_goal": "Source training goal: build HR planning capability.",
                    "course_name": "인사기획",
                    "training_course_id": 10,
                    "course_scope_fit": {
                        "relation": "direct_scope_unit",
                        "fields": ["unit_code"],
                        "direct_unit_codes": ["0202020101_23v3"],
                        "target_scope": {
                            "major_code": "02",
                            "middle_code": "02",
                            "small_code": "02",
                            "sub_code": "01",
                        },
                        "course_scope": {
                            "major_code": "02",
                            "middle_code": "02",
                            "small_code": "02",
                            "sub_code": "01",
                        },
                    },
                    "tier": "primary",
                    "tier_label": "우선 추천",
                    "confidence_grade": "high",
                    "confidence_score": 0.91,
                    "coverage_summary": ["보완 KSA 근거 2개"],
                    "evidence_highlights": {"gap_ksa": ["workforce planning"]},
                    "career_path_review_basis": {
                        "schema": "aihr_career_path_review_basis_v1",
                        "status": "trusted_evidence_visible",
                        "trusted_reviewed_count": 1,
                        "trusted_review_state_counts": {"human_reviewed": 1},
                        "display_refs": ["인사기획(차장)"],
                        "basis": [
                            {
                                "career_path_id": 159,
                                "job_name": "인사",
                                "competency_name": "인사기획",
                                "matched_unit_code": "0202020101_23v3",
                                "trusted_review_state": "human_reviewed",
                            }
                        ],
                        "approval_claim": False,
                        "db_writes": False,
                    },
                    "why_recommended": ["보완 KSA: workforce planning"],
                    "quality_issue_penalty": {
                        "applied": True,
                        "issue_types": ["broad_generic_ksa"],
                        "labels": ["범용 KSA 과잉 연결 감점"],
                        "multiplier": 0.8,
                        "concept_ids": [1],
                        "scoring_role": "downweight_only",
                        "review_required": True,
                    },
                    "delivery": {
                        "relations": [
                            {
                                "relation_id": 301,
                                "relation_type": "has_level",
                                "numeric_value": 5,
                                "review_status": "candidate",
                                "created_at": "2026-06-17T00:00:00Z",
                                "updated_at": "2026-06-17T00:00:00Z",
                            },
                            {"relation_id": 302, "relation_type": "requires_time", "numeric_value": 24},
                            {"relation_type": "uses_facility", "relation_value": "HRD 실습실"},
                        ],
                        "profile": {"methods": ["집체훈련"], "facilities": ["전산강의실"]},
                    },
                    "fit_summary": ["시간 조건 적합: 24h"],
                },
                {
                    "rank": 2,
                    "course_name": "직무관리",
                    "training_course_id": 11,
                    "tier": "adjacent_reference",
                    "tier_label": "참고 과정",
                    "confidence_grade": "low",
                    "confidence_score": 0.18,
                    "coverage_summary": [],
                    "evidence_highlights": {},
                    "why_recommended": ["근거 방식: weak_evidence"],
                    "delivery": {},
                    "fit_summary": [],
                },
            ],
            "job_base_transition_profile": {
                "schema": "ncs_job_base_transition_profile_v1",
                "evidence_role": "supporting_gap_context",
                "scoring_role": "auxiliary_tie_breaker_not_primary_evidence",
                "profile_source": "profile_rows",
                "current_count": 1,
                "target_count": 2,
                "transferable_count": 1,
                "gap_count": 1,
                "transferable": ["Communication:Listening"],
                "gaps": ["Information:Data processing"],
                "gap_label_status": "available",
                "labels_unavailable": False,
                "review_required": True,
                "db_writes": False,
            },
            "input_quality": {
                "ok": True,
                "warnings": [],
                "suggestions": [],
                "candidate_queries": {},
            },
            "audit": {
                "sqf_used": False,
                "learning_modules_used": False,
                "data_sources": ["ncs_training_courses", "training_goal_concept_links"],
            },
        }

        plan = compact_ncs_education_plan_response(
            compact_transition,
            plan_objective="HR 전환 교육체계",
            target_population="HR 담당자",
            scenario="자격연계",
        )

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["view"], "ncs_education_plan")
        self.assertEqual(plan["plan_objective"], "HR 전환 교육체계")
        self.assertEqual(plan["target_population"], "HR 담당자")
        self.assertEqual(plan["scenario"]["selected"], "qualification_bridge")
        self.assertIn("task_transition", {item["scenario"] for item in plan["scenario"]["available"]})
        self.assertEqual(plan["scenario_extensions"][0]["scenario"], "qualification_bridge")
        self.assertEqual(plan["current_scope"]["resolved_as"], "노무관리")
        self.assertEqual(plan["target_scope"]["resolved_as"], "인사기획")
        self.assertEqual(plan["scope_baseline"]["schema"], "aihr_scope_baseline_v1")
        self.assertEqual(plan["scope_baseline"]["guide_stage"], "C1-1")
        self.assertEqual(plan["scope_baseline"]["current"]["requested_query"], "노무관리")
        self.assertEqual(plan["scope_baseline"]["target"]["resolved_scope"], "인사기획")
        self.assertEqual(plan["scope_baseline"]["ncs_scope_relation"], "same_small_classification")
        self.assertEqual(plan["scope_baseline"]["exact_ksa_overlap_ratio"], 0.1)
        self.assertEqual(plan["scope_baseline"]["ontology_adjusted_transferability_ratio"], 0.2)
        self.assertIn("classification_scope_score", plan["scope_baseline"]["adjusted_transferability_components"])
        self.assertEqual(plan["priority_gaps"], ["workforce planning", "HR strategy"])
        self.assertEqual(plan["recommended_path"][1]["role"], "core_gap_training")
        self.assertEqual(plan["recommended_path"][0]["guide_stage"], "C1-1")
        self.assertEqual(plan["recommended_path"][1]["guide_stage"], "C1-2")
        self.assertEqual(plan["recommended_path"][2]["guide_stage"], "C2-1")
        self.assertEqual(plan["job_base_transition_profile"]["gaps"], ["Information:Data processing"])
        self.assertEqual(plan["recommended_path"][2]["job_base_gaps"], ["Information:Data processing"])
        self.assertEqual(plan["recommended_path"][2]["job_base_gap_context"]["gap_label_status"], "available")
        self.assertEqual(plan["recommended_path"][1]["courses"][0]["course_name"], "인사기획")
        self.assertEqual(
            plan["recommended_path"][1]["courses"][0]["career_path_review_basis"]["status"],
            "trusted_evidence_visible",
        )
        self.assertEqual(
            plan["recommended_path"][1]["courses"][0]["quality_issue_penalty"]["issue_types"],
            ["broad_generic_ksa"],
        )
        self.assertEqual(
            plan["recommended_path"][0]["transition_review_basis"]["status"],
            "trusted_career_path_visible",
        )
        self.assertEqual(plan["recommended_path"][1]["courses"][0]["hours"], 24)
        self.assertEqual(plan["recommended_path"][1]["courses"][0]["facilities"], ["전산강의실", "HRD 실습실"])
        self.assertEqual(plan["recommended_path"][2]["courses"][0]["course_name"], "직무관리")
        self.assertEqual(plan["recommended_path"][3]["role"], "delivery_fit_review")
        self.assertEqual(plan["recommended_path"][3]["guide_stage"], "C2-2")
        self.assertIn(plan["recommended_path"][3]["guide_stage_status"], {"ready", "needs_review"})
        self.assertEqual(
            plan["course_intake_requirements"]["schema"],
            "aihr_course_intake_requirements_v1",
        )
        self.assertEqual(plan["course_intake_requirements"]["guide_stage"], "C1-1")
        self.assertIs(
            plan["course_intake_requirements"]["mapping_policy"]["title_only_mapping_allowed"],
            False,
        )
        self.assertIs(
            plan["course_intake_requirements"]["review_gate"]["approval_claim"],
            False,
        )
        self.assertIn(
            "course_goal",
            {item["field"] for item in plan["course_intake_requirements"]["required_fields"]},
        )
        self.assertEqual(
            plan["training_course_inventory_template"]["schema"],
            "aihr_training_course_inventory_template_v1",
        )
        self.assertIn(
            "course_goal",
            plan["training_course_inventory_template"]["required_columns"],
        )
        self.assertIs(
            plan["training_course_inventory_template"]["review_gate"]["approval_claim"],
            False,
        )
        self.assertEqual(
            plan["training_course_inventory_template"]["prefill_rows"][0]["source_type"],
            "ncs_training_api",
        )
        self.assertEqual(
            plan["training_course_inventory_template"]["prefill_rows"][0]["course_goal"],
            "Source training goal: build HR planning capability.",
        )
        self.assertNotEqual(
            plan["training_course_inventory_template"]["prefill_rows"][0]["course_goal"],
            plan["training_system_matrix"][0]["why_recommended"][0],
        )
        self.assertIn(
            "performance_criteria_or_task",
            plan["training_course_inventory_template"]["required_columns"],
        )
        self.assertIn(
            "assessment_method",
            plan["training_course_inventory_template"]["required_columns"],
        )
        self.assertEqual(
            plan["training_necessity_review"]["schema"],
            "aihr_training_necessity_review_v1",
        )
        self.assertEqual(plan["training_necessity_review"]["guide_stage"], "C1-2")
        self.assertEqual(plan["training_necessity_review"]["summary"]["row_count"], 2)
        self.assertEqual(
            plan["training_necessity_review"]["summary"]["approval_blocked_rows"],
            2,
        )
        self.assertIs(plan["training_necessity_review"]["review_gate"]["approval_claim"], False)
        self.assertEqual(
            plan["training_necessity_review"]["rows"][0]["required_optional_review"]["code"],
            "required",
        )
        self.assertIs(
            plan["training_necessity_review"]["rows"][0]["required_optional_review"]["approval_claim"],
            False,
        )
        self.assertIn(
            plan["training_necessity_review"]["rows"][0]["job_linkage"]["status"],
            {"evidence_visible", "needs_review"},
        )
        self.assertEqual(
            plan["training_necessity_review"]["rows"][0]["performance_contribution"]["status"],
            "evidence_visible",
        )
        self.assertEqual(plan["annual_operation_plan"]["schema"], "aihr_annual_operation_plan_seed_v1")
        self.assertEqual(plan["annual_operation_plan"]["guide_stage"], "C2-2")
        self.assertEqual(plan["annual_operation_plan"]["summary"]["row_count"], 2)
        self.assertEqual(plan["annual_operation_plan"]["rows"][0]["recommended_window"], "Q1")
        self.assertEqual(plan["annual_operation_plan"]["rows"][0]["decision_status"], "pending_human_decision")
        self.assertIs(plan["annual_operation_plan"]["review_gate"]["approval_claim"], False)
        self.assertEqual(plan["training_system_matrix"][0]["course_name"], "인사기획")
        self.assertEqual(
            plan["training_system_matrix"][0]["quality_issue_penalty"]["issue_types"],
            ["broad_generic_ksa"],
        )
        self.assertEqual(
            plan["transition_review_basis"]["current_trusted_career_path_count"],
            2,
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["career_path_review_basis"]["trusted_reviewed_count"],
            1,
        )
        self.assertIs(
            plan["training_system_matrix"][0]["career_path_review_basis"]["approval_claim"],
            False,
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["transition_review_basis"]["target_trusted_career_path_count"],
            1,
        )
        self.assertEqual(plan["training_system_matrix"][0]["need_classification"]["code"], "required")
        self.assertEqual(plan["training_system_matrix"][0]["job_scope"]["target"], "인사기획")
        self.assertEqual(plan["training_system_matrix"][0]["target_level_band"]["code"], "level_5_6")
        self.assertEqual(plan["training_system_matrix"][0]["education_type"]["code"], "classroom_or_lecture")
        self.assertEqual(plan["training_system_matrix"][0]["required_optional_basis"]["code"], "required")
        self.assertEqual(plan["training_system_matrix"][0]["course_link"]["course_name"], plan["training_system_matrix"][0]["course_name"])
        self.assertEqual(
            plan["training_system_matrix"][0]["course_link"]["training_course_id"],
            plan["training_system_matrix"][0]["training_course_id"],
        )
        self.assertIn("training_course", plan["training_system_matrix"][0]["course_link"]["mapping_chain"])
        self.assertEqual(plan["training_system_matrix"][0]["course_link"]["need_classification"]["code"], "required")
        self.assertEqual(
            plan["training_system_matrix"][0]["course_link"]["evidence_directness"]["code"],
            plan["training_system_matrix"][0]["evidence_directness"]["code"],
        )
        self.assertEqual(plan["recommended_courses"][0]["course_scope_fit"]["relation"], "direct_scope_unit")
        self.assertEqual(plan["training_system_matrix"][0]["course_scope_fit"]["relation"], "direct_scope_unit")
        self.assertEqual(
            plan["training_system_matrix"][0]["course_link"]["course_scope_fit"]["relation"],
            "direct_scope_unit",
        )
        self.assertEqual(plan["training_system_matrix"][0]["planner_grouping"]["required_optional"], "required")
        self.assertEqual(plan["training_system_matrix"][0]["planner_grouping"]["education_type"], "classroom_or_lecture")
        self.assertEqual(
            plan["training_system_matrix"][0]["planner_grouping"]["course_scope_relation"],
            "direct_scope_unit",
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["delivery_operation"]["code"],
            "method_and_facility_specified",
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["course_fit"]["facilities"],
            ["전산강의실", "HRD 실습실"],
        )
        self.assertEqual(plan["training_system_matrix"][0]["specificity_warning"]["status"], "warning")
        self.assertIn(
            "weak_or_missing_direct_evidence",
            plan["training_system_matrix"][0]["specificity_warning"]["codes"],
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["mapping_strength_warning"]["status"],
            "warning",
        )
        self.assertIn(
            "weak_evidence_directness",
            plan["training_system_matrix"][0]["mapping_strength_warning"]["codes"],
        )
        self.assertIn(
            "mapping_strength:weak_evidence_directness",
            plan["training_system_matrix"][0]["review_flags"],
        )
        self.assertIn(
            "quality_issue:broad_generic_ksa",
            plan["training_system_matrix"][0]["review_flags"],
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["decision_state"]["schema"],
            "aihr_training_row_decision_state_v1",
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["decision_state"]["status"],
            "pending_human_decision",
        )
        self.assertIs(plan["training_system_matrix"][0]["decision_state"]["approval_claim"], False)
        self.assertEqual(
            plan["training_system_matrix"][0]["decision_state"]["system_suggestion"],
            "required",
        )
        self.assertEqual(plan["training_system_matrix"][0]["human_review"]["severity"], "needs_review")
        self.assertIn(
            "pending_human_decision",
            plan["training_system_matrix"][0]["human_review"]["flags"],
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["evidence_chain"]["schema"],
            "aihr_course_evidence_chain_v1",
        )
        self.assertEqual(
            plan["training_system_matrix"][0]["evidence_chain"]["chain_order"],
            [
                "job_scope",
                "duty_task",
                "performance_criterion",
                "ksa",
                "training_course",
            ],
        )
        self.assertIn(
            "training_course",
            {link["stage"] for link in plan["training_system_matrix"][0]["evidence_chain"]["links"]},
        )
        self.assertIn(
            plan["training_system_matrix"][0]["duplicate_or_generic_warning"]["status"],
            {"clear", "warning"},
        )
        self.assertEqual(plan["training_system_matrix"][1]["need_classification"]["code"], "adjacent_reference")
        self.assertIn("adjacent_reference_only", plan["training_system_matrix"][1]["review_flags"])
        self.assertIn(
            "adjacent_reference_only",
            plan["training_system_matrix"][1]["specificity_warning"]["codes"],
        )
        self.assertEqual(plan["training_system_summary"]["course_count"], 2)
        self.assertEqual(plan["training_system_summary"]["need_classification_counts"]["required"], 1)
        self.assertEqual(plan["training_system_summary"]["need_classification_counts"]["adjacent_reference"], 1)
        self.assertIn(
            "weak_or_missing_direct_evidence",
            plan["training_system_summary"]["specificity_warning_counts"],
        )
        self.assertIn(
            "weak_evidence_directness",
            plan["training_system_summary"]["mapping_strength_warning_counts"],
        )
        self.assertEqual(plan["training_system_summary"]["quality_issue_penalty_counts"]["broad_generic_ksa"], 1)
        self.assertEqual(plan["training_system_summary"]["quality_issue_penalty_review_required_count"], 1)
        self.assertEqual(plan["training_system_summary"]["quality_issue_penalty_course_names"], ["인사기획"])
        self.assertEqual(plan["training_system_summary"]["review_flag_counts"]["quality_issue:broad_generic_ksa"], 1)
        self.assertEqual(
            plan["training_system_summary"]["decision_state_counts"]["pending_human_decision"],
            2,
        )
        self.assertIn("evidence_chain_status_counts", plan["training_system_summary"])
        self.assertIn(
            "evidence_chain_status_counts",
            plan["training_system_guide_trace"]["evidence_contract"],
        )
        self.assertEqual(plan["training_system_summary"]["planner_group_counts"]["target_level_band"]["level_5_6"], 1)
        self.assertEqual(plan["training_system_summary"]["planner_group_counts"]["education_type"]["classroom_or_lecture"], 1)
        self.assertEqual(plan["training_system_summary"]["planner_group_counts"]["education_type"]["unknown"], 1)
        self.assertEqual(plan["training_system_summary"]["course_scope_relation_counts"]["direct_scope_unit"], 1)
        self.assertEqual(plan["training_system_summary"]["planner_group_counts"]["course_scope_relation"]["direct_scope_unit"], 1)
        self.assertIn("delivery_constraint_fit_counts", plan["training_system_summary"])
        self.assertIn("course_scope_relation", plan["training_system_summary"]["groupable_fields"])
        self.assertIn("education_type", plan["training_system_summary"]["groupable_fields"])
        self.assertEqual(plan["training_system_summary"]["required_course_names"], ["인사기획"])
        self.assertEqual(plan["training_system_summary"]["review_required_course_names"], ["인사기획", "직무관리"])
        self.assertEqual(plan["training_system_guide_trace"]["schema"], "aihr_training_system_guide_trace_v1")
        self.assertEqual(
            set(plan["training_system_guide_trace"]["guide_workflow_stage_codes"]),
            {"C1-1", "C1-2", "C2-1", "C2-2"},
        )
        self.assertEqual(
            {item["code"] for item in plan["training_system_guide_trace"]["guide_workflow_stages"]},
            {"C1-1", "C1-2", "C2-1", "C2-2"},
        )
        self.assertEqual(
            {item["code"] for item in plan["training_system_guide_trace"]["guide_workflow"]["steps"]},
            {"C1-1", "C1-2", "C2-1", "C2-2"},
        )
        c2_2_stage = next(
            item
            for item in plan["training_system_guide_trace"]["guide_workflow_stages"]
            if item["code"] == "C2-2"
        )
        self.assertIn("annual_operation_plan", c2_2_stage["output_fields"])
        c1_1_stage = next(
            item
            for item in plan["training_system_guide_trace"]["guide_workflow_stages"]
            if item["code"] == "C1-1"
        )
        c1_2_stage = next(
            item
            for item in plan["training_system_guide_trace"]["guide_workflow_stages"]
            if item["code"] == "C1-2"
        )
        self.assertIn("course_intake_requirements", c1_1_stage["output_fields"])
        self.assertIn("training_course_inventory_template", c1_1_stage["output_fields"])
        self.assertIn("training_necessity_review", c1_2_stage["output_fields"])
        self.assertEqual(
            set(plan["training_system_guide_trace"]["required_check_codes"]),
            {"job_scope", "task_ksa", "course_link", "required_optional", "level_delivery", "human_review"},
        )
        self.assertEqual(
            {item["check"] for item in plan["training_system_guide_trace"]["checks"]},
            {"job_scope", "task_ksa", "course_link", "required_optional", "level_delivery", "human_review"},
        )
        self.assertEqual(
            {item["check"] for item in plan["training_system_guide_trace"]["checks"]},
            {item["code"] for item in plan["training_system_guide_trace"]["checks"]},
        )
        self.assertEqual(plan["training_system_guide_trace"]["rubric_role"], "framework_reference_not_scoring_source")
        self.assertEqual(plan["training_system_guide_trace"]["guide_reference_schema"], "ncs_hrd_guide_reference_v1")
        self.assertIn(
            "docs/reference/ncs_hrd_guide_codex_readable.md",
            plan["training_system_guide_trace"]["rubric_source_path"],
        )
        self.assertTrue(plan["training_system_guide_trace"]["rubric_source_hash"])
        self.assertIn(
            "pending_decision_rows=",
            next(
                item["evidence"]
                for item in plan["training_system_guide_trace"]["checks"]
                if item["code"] == "human_review"
            ),
        )
        guide_checks = {item["code"]: item for item in plan["training_system_guide_trace"]["checks"]}
        self.assertEqual(guide_checks["required_optional"]["status"], "needs_review")
        self.assertIn("decision_state_rows=2/2", guide_checks["required_optional"]["evidence"])
        self.assertEqual(guide_checks["human_review"]["status"], "needs_review")
        self.assertEqual(c1_2_stage["status"], "needs_review")
        self.assertIn("needs_review", plan["training_system_guide_trace"]["status_counts"])
        self.assertIn("training_system_fit", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("scope_baseline", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("course_scope_fit", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("course_intake_requirements", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("training_course_inventory_template", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("training_necessity_review", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("training_system_summary", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("training_system_guide_trace", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("training_system_matrix", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("quality_issue_penalty", plan["evidence_basis"]["course_evidence_fields"])
        self.assertIn("공식 자격 인정", " ".join(plan["non_goals"]))
        self.assertFalse(plan["audit"]["sqf_used"])
        self.assertFalse(plan["audit"]["learning_modules_used"])
        plan_json = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("source_payload", plan_json)
        self.assertNotIn('"relations"', plan_json)
        self.assertNotIn('"relation_id"', plan_json)
        self.assertNotIn('"created_at"', plan_json)
        self.assertNotIn('"updated_at"', plan_json)
        self.assertNotIn('"review_status"', plan_json)
        self.assertNotIn('"data_sources"', plan_json)

    def test_training_system_guide_trace_marks_pending_decisions_for_human_review(self) -> None:
        trace = _training_system_guide_trace(
            [
                {
                    "course_name": "HR planning",
                    "task_ksa_basis": {"basis_types": ["target_scope_ksa"]},
                    "course_scope_fit": {"relation": "direct_scope_unit"},
                    "need_classification": {"code": "required"},
                    "required_optional_basis": {"code": "required"},
                    "decision_state": {
                        "schema": "aihr_training_row_decision_state_v1",
                        "status": "pending_human_decision",
                    },
                    "course_fit": {
                        "level": 5,
                        "hours": 8,
                        "methods": ["classroom"],
                        "facilities": ["lecture room"],
                    },
                    "delivery_operation": {"code": "method_and_facility_specified"},
                    "facility_constraint_fit": {"status": "not_requested"},
                    "human_review": {"severity": "ready", "flags": []},
                    "review_flags": [],
                }
            ],
            current_label="Current",
            target_label="Target",
            priority_gaps=[],
            summary={},
        )

        checks = {item["code"]: item for item in trace["checks"]}
        self.assertEqual(checks["required_optional"]["status"], "needs_review")
        self.assertIn("decision_state_rows=1/1", checks["required_optional"]["evidence"])
        self.assertEqual(checks["human_review"]["status"], "needs_review")
        self.assertIn("pending_decision_rows=1/1", checks["human_review"]["evidence"])

    def test_compact_education_plan_preserves_summary_only_job_base_gap_context(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"requested": "A", "resolved_as": "A", "match_level": "unit", "unit_count": 1},
                    "target": {"requested": "B", "resolved_as": "B", "match_level": "unit", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.5},
                "key_gap_ksa": [],
                "caveats": [],
            },
            "requested": {"current_query": "A", "target_query": "B"},
            "transition_summary": {
                "current_job_base_count": 1,
                "target_job_base_count": 3,
                "transferable_job_base_count": 1,
                "gap_job_base_count": 2,
            },
            "job_base_transition_profile": {
                "schema": "ncs_job_base_transition_profile_v1",
                "evidence_role": "supporting_gap_context",
                "scoring_role": "auxiliary_tie_breaker_not_primary_evidence",
                "profile_source": "summary_only",
                "current_count": 1,
                "target_count": 3,
                "transferable_count": 1,
                "gap_count": 2,
                "transferable": [],
                "gaps": [],
                "gap_label_status": "summary_only_labels_unavailable",
                "labels_unavailable": True,
                "review_required": True,
                "db_writes": False,
            },
            "recommended_courses": [],
            "input_quality": {"ok": True, "warnings": [], "suggestions": [], "candidate_queries": {}},
            "audit": {},
        }

        plan = compact_ncs_education_plan_response(compact_transition)

        self.assertEqual(plan["job_base_transition_profile"]["gap_count"], 2)
        self.assertEqual(plan["job_base_transition_profile"]["gap_label_status"], "summary_only_labels_unavailable")
        self.assertEqual(plan["recommended_path"][2]["job_base_gaps"], [])
        self.assertEqual(plan["recommended_path"][2]["job_base_gap_context"]["gap_count"], 2)
        self.assertEqual(
            plan["recommended_path"][2]["job_base_gap_context"]["gap_label_status"],
            "summary_only_labels_unavailable",
        )
        self.assertIs(plan["recommended_path"][2]["job_base_gap_context"]["labels_unavailable"], True)
        self.assertIs(plan["recommended_path"][2]["job_base_gap_context"]["review_required"], True)

    def test_compact_education_plan_empty_recommendations_keep_public_contract(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"resolved_as": "labor management", "unit_count": 1},
                    "target": {"resolved_as": "HR planning", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.2},
                "key_gap_ksa": [],
                "caveats": [],
            },
            "requested": {"current_query": "labor management", "target_query": "HR planning"},
            "source_recommendation_counts": {"primary": 0, "supplemental": 0, "adjacent": 0},
            "recommended_courses": [],
            "input_quality": {"ok": True, "warnings": [], "suggestions": []},
            "audit": {
                "generated_at": "2026-06-18T00:00:00+00:00",
                "sqf_used": False,
                "learning_modules_used": False,
                "data_sources": ["ncs_training_courses", "training_goal_concept_links"],
            },
        }

        plan = compact_ncs_education_plan_response(compact_transition)

        self.assertTrue(plan["ok"])
        self.assertEqual(plan["training_system_matrix"], [])
        self.assertEqual(plan["recommended_courses"], [])
        self.assertEqual(
            {stage["guide_stage_status"] for stage in plan["recommended_path"]},
            {"needs_review"},
        )
        self.assertEqual(
            {stage["status"] for stage in plan["training_system_guide_trace"]["guide_workflow_stages"]},
            {"needs_review"},
        )
        self.assertTrue(
            {
                check["status"]
                for check in plan["training_system_guide_trace"]["checks"]
            }.issubset({"ready", "needs_review"})
        )
        self.assertEqual(plan["training_necessity_review"]["schema"], "aihr_training_necessity_review_v1")
        self.assertEqual(plan["training_necessity_review"]["summary"]["row_count"], 0)
        self.assertEqual(plan["training_necessity_review"]["rows"], [])
        self.assertIs(plan["training_necessity_review"]["review_gate"]["approval_claim"], False)
        self.assertEqual(plan["audit"]["sqf_used"], False)
        self.assertEqual(plan["audit"]["learning_modules_used"], False)
        self.assertNotIn("data_sources", plan["audit"])
        self.assertNotIn("data_sources", plan["evidence_basis"])
        plan_json = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn('"data_sources"', plan_json)

    def test_compact_education_plan_fallback_demotes_primary_without_named_task_ksa(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"resolved_as": "총무", "unit_count": 1},
                    "target": {"resolved_as": "인사기획", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.2},
                "key_gap_ksa": [],
                "caveats": [],
            },
            "requested": {"current_query": "총무", "target_query": "인사기획"},
            "recommended_courses": [
                {
                    "rank": 1,
                    "course_name": "명칭 유사 과정",
                    "training_course_id": 20,
                    "tier": "primary",
                    "tier_label": "우선 추천",
                    "confidence_grade": "medium",
                    "confidence_score": 0.7,
                    "coverage_summary": [],
                    "evidence_highlights": {},
                    "why_recommended": ["근거 방식: unit_scope"],
                    "delivery": {},
                    "fit_summary": [],
                }
            ],
            "input_quality": {"ok": True, "warnings": [], "suggestions": []},
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }

        plan = compact_ncs_education_plan_response(compact_transition)
        row = plan["training_system_matrix"][0]

        self.assertEqual(row["need_classification"]["code"], "supporting")
        self.assertEqual(row["education_type"]["code"], "unknown")
        self.assertEqual(row["target_level_band"]["code"], "unknown")
        self.assertEqual(row["required_optional_basis"]["code"], "supporting")
        self.assertEqual(plan["training_system_summary"]["need_classification_counts"]["supporting"], 1)
        self.assertEqual(plan["training_system_summary"]["required_course_names"], [])
        self.assertIn("fallback_from_compact_card", row["review_flags"])
        self.assertIn("primary_demoted_without_direct_task_ksa_or_goal", row["review_flags"])

    def test_compact_education_plan_flags_generic_and_duplicate_course_names_for_review(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"resolved_as": "general affairs", "unit_count": 1},
                    "target": {"resolved_as": "recruiting", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.2},
                "key_gap_ksa": ["candidate screening"],
                "caveats": [],
            },
            "requested": {"current_query": "general affairs", "target_query": "recruiting"},
            "recommended_courses": [
                {
                    "rank": 1,
                    "course_name": "HR",
                    "training_course_id": 301,
                    "tier": "primary",
                    "tier_label": "primary",
                    "confidence_grade": "high",
                    "confidence_score": 0.9,
                    "coverage_summary": [],
                    "evidence_highlights": {
                        "gap_ksa": ["candidate screening"],
                        "training_goal_ksa": ["candidate screening"],
                        "covered_elements": ["screen applicants"],
                    },
                    "why_recommended": ["gap KSA: candidate screening"],
                    "delivery": {},
                    "fit_summary": [],
                },
                {
                    "rank": 2,
                    "course_name": "HR",
                    "training_course_id": 302,
                    "tier": "supplemental",
                    "tier_label": "supplemental",
                    "confidence_grade": "medium",
                    "confidence_score": 0.6,
                    "coverage_summary": [],
                    "evidence_highlights": {
                        "gap_ksa": ["candidate screening"],
                        "training_goal_ksa": ["candidate screening"],
                    },
                    "why_recommended": ["gap KSA: candidate screening"],
                    "delivery": {},
                    "fit_summary": [],
                },
            ],
            "input_quality": {"ok": True, "warnings": [], "suggestions": []},
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }

        plan = compact_ncs_education_plan_response(compact_transition)

        self.assertEqual(len(plan["training_system_matrix"]), 2)
        for row in plan["training_system_matrix"]:
            warning = row["duplicate_or_generic_warning"]
            self.assertEqual(warning["status"], "warning")
            self.assertIn("generic_or_low_specificity_course_name", warning["codes"])
            self.assertIn("duplicate_course_name_in_plan", warning["codes"])
            self.assertIn(
                "duplicate_or_generic:generic_or_low_specificity_course_name",
                row["review_flags"],
            )
            self.assertTrue(row["decision_state"]["evidence_attention_required"])
            self.assertEqual(row["human_review"]["severity"], "needs_review")
            self.assertIs(row["decision_state"]["approval_claim"], False)
        self.assertEqual(
            plan["training_system_summary"]["duplicate_or_generic_warning_counts"][
                "generic_or_low_specificity_course_name"
            ],
            2,
        )
        self.assertEqual(
            plan["training_system_summary"]["duplicate_or_generic_warning_counts"][
                "duplicate_course_name_in_plan"
            ],
            2,
        )
        necessity = plan["training_necessity_review"]
        self.assertEqual(necessity["summary"]["duplicate_or_generic_status_counts"]["warning"], 2)
        self.assertEqual(necessity["summary"]["review_required_rows"], 2)
        self.assertIs(necessity["review_gate"]["approval_claim"], False)

    def test_compact_education_plan_preserves_facility_constraints_and_row_contract(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"resolved_as": "노무관리", "unit_count": 1},
                    "target": {"resolved_as": "인사기획", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.25},
                "key_gap_ksa": ["workforce planning"],
                "caveats": [],
            },
            "requested": {
                "current_query": "노무관리",
                "target_query": "인사기획",
                "preferred_methods": ["Practice"],
                "preferred_max_hours": 16,
                "preferred_facilities": ["HRD 실습실"],
            },
            "recommended_courses": [
                {
                    "rank": 1,
                    "course_name": "시설 적합 과정",
                    "training_course_id": 101,
                    "tier": "primary",
                    "tier_label": "우선 추천",
                    "confidence_grade": "high",
                    "confidence_score": 0.9,
                    "coverage_summary": [],
                    "evidence_highlights": {
                        "gap_ksa": ["workforce planning"],
                        "covered_elements": ["인력계획 수립하기"],
                    },
                    "why_recommended": ["보완 KSA: workforce planning"],
                    "delivery": {
                        "hours": 12,
                        "profile": {
                            "methods": ["Practice", "집체훈련"],
                            "facilities": ["HRD 실습실"],
                        }
                    },
                    "fit_summary": [],
                },
                {
                    "rank": 2,
                    "course_name": "시설 불일치 과정",
                    "training_course_id": 102,
                    "tier": "supplemental",
                    "tier_label": "보조추천",
                    "confidence_grade": "medium",
                    "confidence_score": 0.55,
                    "coverage_summary": [],
                    "evidence_highlights": {"gap_ksa": ["workforce planning"]},
                    "why_recommended": ["보완 KSA: workforce planning"],
                    "delivery": {"profile": {"facilities": ["일반강의실"]}},
                    "fit_summary": [],
                },
                {
                    "rank": 3,
                    "course_name": "시설 미상 과정",
                    "training_course_id": 103,
                    "tier": "adjacent_reference",
                    "tier_label": "참고 과정",
                    "confidence_grade": "low",
                    "confidence_score": 0.2,
                    "coverage_summary": [],
                    "evidence_highlights": {},
                    "why_recommended": ["근거 방식: weak_evidence"],
                    "delivery": {},
                    "fit_summary": [],
                },
            ],
            "input_quality": {"ok": True, "warnings": [], "suggestions": []},
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }

        plan = compact_ncs_education_plan_response(compact_transition)
        rows_by_name = {row["course_name"]: row for row in plan["training_system_matrix"]}

        self.assertEqual(plan["requested"]["preferred_facilities"], ["HRD 실습실"])
        self.assertEqual(rows_by_name["시설 적합 과정"]["facility_constraint_fit"]["status"], "fit")
        self.assertEqual(rows_by_name["시설 불일치 과정"]["facility_constraint_fit"]["status"], "mismatch")
        self.assertEqual(rows_by_name["시설 미상 과정"]["facility_constraint_fit"]["status"], "unknown")
        self.assertEqual(rows_by_name["시설 적합 과정"]["education_type"]["code"], "blended_practice")
        self.assertEqual(rows_by_name["시설 불일치 과정"]["education_type"]["code"], "facility_specified")
        self.assertEqual(
            rows_by_name["시설 적합 과정"]["delivery_operation"]["facility_constraint_fit"]["status"],
            "fit",
        )
        self.assertEqual(
            rows_by_name["시설 적합 과정"]["delivery_operation"]["method_constraint_fit"]["status"],
            "fit",
        )
        self.assertEqual(
            rows_by_name["시설 적합 과정"]["delivery_operation"]["time_constraint_fit"]["status"],
            "fit",
        )
        self.assertEqual(
            rows_by_name["시설 적합 과정"]["delivery_operation"]["constraint_fit"]["status"],
            "fit",
        )
        self.assertEqual(
            rows_by_name["시설 불일치 과정"]["delivery_operation"]["method_constraint_fit"]["status"],
            "unknown",
        )
        self.assertIn(
            "delivery:method_unknown",
            rows_by_name["시설 불일치 과정"]["review_flags"],
        )
        self.assertIn(
            "delivery:facility_mismatch",
            rows_by_name["시설 불일치 과정"]["review_flags"],
        )
        self.assertIn(
            "facility_constraint_mismatch",
            rows_by_name["시설 불일치 과정"]["review_flags"],
        )
        self.assertIn(
            "facility_constraint_mismatch",
            rows_by_name["시설 불일치 과정"]["human_review"]["flags"],
        )
        self.assertIn(
            "facility_constraint_unknown",
            rows_by_name["시설 미상 과정"]["review_flags"],
        )
        self.assertIn("rationale", rows_by_name["시설 적합 과정"]["required_optional_basis"])
        self.assertIn("competency_element", rows_by_name["시설 적합 과정"]["task_ksa_basis"]["basis_types"])
        self.assertEqual(
            rows_by_name["시설 적합 과정"]["task_ksa_basis"]["covered_elements"],
            ["인력계획 수립하기"],
        )
        self.assertEqual(rows_by_name["시설 적합 과정"]["human_review"]["action"], "review_training_system_row")
        self.assertIn(rows_by_name["시설 적합 과정"]["human_review"]["severity"], {"ready", "needs_review"})
        self.assertTrue(rows_by_name["시설 적합 과정"]["human_review"]["prompt"])

    def test_compact_education_plan_flags_partial_facility_aliases(self) -> None:
        compact_transition = {
            "ok": True,
            "view": "compact_training_transition",
            "answer_summary": {
                "interpretation": {
                    "current": {"resolved_as": "labor", "unit_count": 1},
                    "target": {"resolved_as": "planning", "unit_count": 1},
                },
                "transition_assessment": {"transferability_ratio": 0.25},
                "key_gap_ksa": ["workforce planning"],
                "caveats": [],
            },
            "requested": {
                "current_query": "labor",
                "target_query": "planning",
                "preferred_facilities": ["Lab", "Workshop"],
            },
            "recommended_courses": [
                {
                    "rank": 1,
                    "course_name": "Partial facility course",
                    "training_course_id": 201,
                    "tier": "primary",
                    "confidence_grade": "high",
                    "confidence_score": 0.9,
                    "coverage_summary": [],
                    "evidence_highlights": {"gap_ksa": ["workforce planning"]},
                    "why_recommended": ["gap KSA: workforce planning"],
                    "delivery": {"profile": {"facilities": ["Lab"]}},
                    "fit_summary": [],
                }
            ],
            "input_quality": {"ok": True, "warnings": [], "suggestions": []},
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }

        plan = compact_ncs_education_plan_response(compact_transition)
        row = plan["training_system_matrix"][0]

        self.assertEqual(row["facility_constraint_fit"]["status"], "partial")
        self.assertEqual(row["facility_constraint_fit"]["matched"], ["Lab"])
        self.assertEqual(row["facility_constraint_fit"]["missing"], ["Workshop"])
        self.assertIn("delivery:facility_partial", row["review_flags"])
        self.assertIn("facility_constraint_partial", row["review_flags"])
        self.assertIn("delivery:facility_partial", row["human_review"]["flags"])
        self.assertIn("facility_constraint_partial", row["human_review"]["flags"])

    def test_compact_transition_response_reads_nested_resolution_candidates(self) -> None:
        compact = compact_training_transition_response(
            {
                "ok": True,
                "recommendation_groups": {"primary": [], "supplemental": [], "adjacent": []},
                "transition": {
                    "summary": {
                        "requested_current_query": "총무",
                        "requested_target_query": "인사",
                        "current_scope_unit_count": 30,
                        "target_scope_unit_count": 40,
                    },
                    "current_scope": {"match_level": "sub_classification", "unit_codes": ["u1"]},
                    "target_scope": {"match_level": "middle_classification", "unit_codes": ["u2"]},
                    "current_query_resolution": {
                        "ok": True,
                        "query": "총무",
                        "candidates": [{"candidate_type": "unit", "matched_text": "총무", "unit_code": "u1"}],
                    },
                    "target_query_resolution": {
                        "ok": True,
                        "query": "인사",
                        "candidates": [{"candidate_type": "unit", "matched_text": "인사기획", "unit_code": "u2"}],
                    },
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        candidates = compact["input_quality"]["candidate_queries"]
        self.assertEqual(candidates["current_query"][0]["query"], "총무")
        self.assertEqual(candidates["target_query"][0]["query"], "인사기획")

    def test_compact_candidate_guidance_excludes_concept_candidates(self) -> None:
        compact = compact_training_task_response(
            {
                "ok": True,
                "requested_query": "x",
                "query_resolution": {
                    "ok": True,
                    "query": "x",
                    "normalized_query": "x",
                    "candidates": [
                        {
                            "candidate_type": "concept",
                            "match_level": "ontology_concept",
                            "matched_text": "x concept",
                            "confidence_score": 0.99,
                        },
                        {
                            "candidate_type": "unit",
                            "match_level": "competency_unit",
                            "matched_text": "x unit",
                            "unit_code": "0202020101_23v3",
                            "confidence_score": 0.7,
                        },
                    ],
                },
                "source_task": {},
                "resolved_scope": {
                    "match_text": "x unit",
                    "match_level": "unit",
                    "unit_codes": ["0202020101_23v3"],
                },
                "recommendation_summary": {},
                "recommendation_groups": {
                    "primary": [
                        {
                            "rank": 1,
                            "course_name": "HR planning",
                            "training_course_id": 100,
                            "confidence_score": 0.9,
                            "confidence_grade": "high",
                            "evidence_strength": {"grade": "high"},
                            "coverage_counts": {},
                            "delivery": {},
                        }
                    ],
                    "supplemental": [],
                    "adjacent": [],
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        candidate_queries = compact["input_quality"]["candidate_queries"]["query"]
        self.assertEqual([item["query"] for item in candidate_queries], ["x unit"])
        self.assertTrue(all(item["candidate_type"] != "concept" for item in candidate_queries))

    def test_not_found_response_dedupes_suggestions_and_keeps_failed_transition_field(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                seed_task_ontology(conn)
                task_result = recommend_training_for_task(conn, query="없는직무", save=False)
                transition_result = recommend_training_transition(
                    conn,
                    current_query="workforce",
                    target_query="HR planninh",
                    save=False,
                )
            finally:
                conn.close()

        self.assertFalse(task_result["ok"])
        self.assertEqual(task_result["error"]["suggestions"], [])
        self.assertNotIn("없는직무", task_result["content"][0]["text"])
        self.assertFalse(transition_result["ok"])
        self.assertEqual(transition_result["input_quality"]["warnings"][0]["field"], "target_query")
        self.assertIn("HR planning", transition_result["error"]["suggestions"])

    def test_prepare_ontology_review_queue_flags_non_hr_weak_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('03', 'Finance', '01', 'Accounting', '01', 'Accounting', '01', 'Settlement')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0301010101_23v3', '0301010101', '23v3', 'Settlement planning',
                          '4', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0301010101_23v3', '1', '0301010101_23v3 1', 'Prepare settlement', '4')
                """
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', 'Prepare settlement data from transaction records.')
                """,
                (element_id,),
            )
            criteria_id = conn.execute("SELECT criteria_id FROM performance_criteria").fetchone()[
                "criteria_id"
            ]
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'transaction settlement rules')
                """,
                (element_id,),
            )
            knowledge_ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '02', 'skill', '1', 'settlement data analysis')
                """,
                (element_id,),
            )
            skill_ksa_id = conn.execute(
                "SELECT ksa_id FROM ksa_items WHERE ksa_type_name = 'skill'"
            ).fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO raw_excel_rows(
                    source_file, sheet_name, sheet_row_number,
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name,
                    unit_code, unit_name, unit_level,
                    element_code, element_name, element_level,
                    criteria_no, criteria_text,
                    ksa_type_code, ksa_type_name, ksa_no, ksa_text,
                    loaded_at
                ) VALUES (
                    'test.xlsx', 'Sheet1', 1,
                    '03', 'Finance', '01', 'Accounting',
                    '01', 'Accounting', '01', 'Settlement',
                    '0301010101_23v3', 'Settlement planning', '4',
                    '0301010101_23v3 1', 'Prepare settlement', '4',
                    '1', 'Prepare settlement data from transaction records.',
                    '01', 'knowledge', '1', 'transaction settlement rules',
                    ?
                )
                """,
                (timestamp,),
            )
            raw_row_id = conn.execute("SELECT raw_row_id FROM raw_excel_rows").fetchone()["raw_row_id"]
            conn.execute(
                """
                INSERT INTO element_criteria_ksa_links(raw_row_id, element_id, criteria_id, ksa_id)
                VALUES (?, ?, ?, ?)
                """,
                (raw_row_id, element_id, criteria_id, knowledge_ksa_id),
            )
            for concept_name, normalized_key, concept_type in (
                ("transaction settlement rules", "transactionsettlementrules", "knowledge"),
                ("settlement data analysis", "settlementdataanalysis", "skill"),
            ):
                conn.execute(
                    """
                    INSERT INTO ontology_concepts(
                        concept_name, normalized_key, concept_type, definition_status,
                        relation_status, review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'missing', 'unlinked', 'raw', ?, ?)
                    """,
                    (concept_name, normalized_key, concept_type, timestamp, timestamp),
                )
            concept_rows = conn.execute(
                "SELECT concept_id, normalized_key FROM ontology_concepts ORDER BY concept_id"
            ).fetchall()
            concept_by_key = {row["normalized_key"]: row["concept_id"] for row in concept_rows}
            conn.execute(
                """
                INSERT INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, 'knowledge', 0, 'transaction settlement rules',
                          'transactionsettlementrules', 'test', 'raw', ?)
                """,
                (knowledge_ksa_id, element_id, timestamp),
            )
            knowledge_atomic_id = conn.execute(
                "SELECT atomic_id FROM ksa_atomic_items WHERE ksa_id = ?", (knowledge_ksa_id,)
            ).fetchone()["atomic_id"]
            conn.execute(
                """
                INSERT INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, 'skill', 0, 'settlement data analysis',
                          'settlementdataanalysis', 'test', 'raw', ?)
                """,
                (skill_ksa_id, element_id, timestamp),
            )
            skill_atomic_id = conn.execute(
                "SELECT atomic_id FROM ksa_atomic_items WHERE ksa_id = ?", (skill_ksa_id,)
            ).fetchone()["atomic_id"]
            conn.execute(
                """
                INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'raw', ?)
                """,
                (knowledge_atomic_id, concept_by_key["transactionsettlementrules"], timestamp),
            )
            conn.execute(
                """
                INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'raw', ?)
                """,
                (skill_atomic_id, concept_by_key["settlementdataanalysis"], timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_training_courses(
                    ncs_cl_cd, compe_unit_name, compe_unit_level,
                    ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm,
                    ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm,
                    train_goal, train_time, fac_name, meth_name, source_payload, api_fetched_at
                ) VALUES ('0301010101_23v3', 'Settlement planning', '4',
                          '03', 'Finance', '01', 'Accounting',
                          '01', 'Accounting', '01', 'Settlement',
                          'Understand settlement basics.', '8', 'classroom', 'lecture',
                          '{}', ?)
                """,
                (timestamp,),
            )
            course_id = conn.execute("SELECT training_course_id FROM ncs_training_courses").fetchone()[
                "training_course_id"
            ]
            conn.execute(
                """
                INSERT INTO training_goal_concept_links(
                    training_course_id, unit_code, element_id, concept_id,
                    link_method, confidence_score, evidence_text, review_status,
                    created_at, updated_at
                ) VALUES (?, '0301010101_23v3', ?, ?, 'training_goal_unit_core_concept',
                          0.42, 'weak inferred link', 'auto_linked', ?, ?)
                """,
                (course_id, element_id, concept_by_key["settlementdataanalysis"], timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO task_ksa_concept_relations(
                    criteria_id, element_id, source_concept_id, relation_type, target_concept_id,
                    source_atomic_id, target_atomic_id, evidence_text, confidence_score,
                    review_status, created_at
                ) VALUES (?, ?, ?, 'co_required_in_element', ?, ?, ?, 'co-occurrence only',
                          0.5, 'candidate', ?)
                """,
                (
                    criteria_id,
                    element_id,
                    concept_by_key["transactionsettlementrules"],
                    concept_by_key["settlementdataanalysis"],
                    knowledge_atomic_id,
                    skill_atomic_id,
                    timestamp,
                ),
            )

            dry_run = prepare_ontology_human_review_queue(
                conn,
                major_code="03",
                concept_limit=10,
                goal_link_limit=10,
                relation_limit=10,
                dry_run=True,
            )
            issue_count_after_dry_run = conn.execute(
                "SELECT COUNT(*) FROM quality_issues"
            ).fetchone()[0]
            result = prepare_ontology_human_review_queue(
                conn,
                major_code="03",
                concept_limit=10,
                goal_link_limit=10,
                relation_limit=10,
            )
            issue_types = {
                row["issue_type"]
                for row in conn.execute("SELECT issue_type FROM quality_issues").fetchall()
            }
            conn.close()

            self.assertTrue(dry_run["ok"])
            self.assertTrue(dry_run["dry_run"])
            self.assertGreaterEqual(dry_run["concept_review_issues_created"], 1)
            self.assertEqual(dry_run["training_goal_link_review_issues_created"], 1)
            self.assertEqual(dry_run["task_ksa_relation_review_issues_created"], 1)
            self.assertEqual(issue_count_after_dry_run, 0)
            self.assertTrue(result["ok"])
            self.assertFalse(result["dry_run"])
            self.assertGreaterEqual(result["concept_review_issues_created"], 1)
            self.assertEqual(result["training_goal_link_review_issues_created"], 1)
            self.assertEqual(result["task_ksa_relation_review_issues_created"], 1)
            self.assertIn("ontology_core_concept_human_review_required", issue_types)
            self.assertIn("ontology_training_goal_link_human_review_required", issue_types)
            self.assertIn("ontology_task_ksa_relation_human_review_required", issue_types)

    def test_career_path_csv_import_matches_columns_and_partial_unit_names(self) -> None:
        headers = [
            "대분류코드",
            "중분류코드",
            "소분류코드",
            "직무코드",
            "직무명",
            "직무역량코드",
            "직무역량수준(능력단위수준 이면서 세분류의 자식)",
            "직무역량명",
            "수준(직급수준)",
            "직급명",
        ]
        rows = [
            ["2", "2", "1", "1", "총무", "0202010101", "5", "사업계획수립", "5", "과장"],
            ["2", "2", "2", "2", "노무관리", "0202020203", "5", "교섭준비", "5", "과장"],
        ]
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            csv_path = Path(tmp) / "career_paths.csv"
            with csv_path.open("w", encoding="cp949", newline="") as handle:
                handle.write(",".join(headers) + "\n")
                for row in rows:
                    handle.write(",".join(row) + "\n")

            conn = connect(db_path)
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'Administration',
                          '01', 'General affairs', '01', '총무')
                """
            )
            general_affairs_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE sub_name = '총무'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR',
                          '02', 'HRM', '02', '노무관리')
                """
            )
            labor_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE sub_name = '노무관리'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202010101_23v3', '0202010101', '23v3', '사업계획수립',
                          '5', ?, 'matched', ?, ?)
                """,
                (general_affairs_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020203_23v3', '0202020203', '23v3', '단체교섭준비',
                          '5', ?, 'matched', ?, ?)
                """,
                (labor_id, timestamp, timestamp),
            )

            result = import_career_paths_csv(conn, csv_path)
            variant_csv_path = Path(tmp) / "career_paths_variant.csv"
            variant_headers = [
                "대분류 코드",
                "중분류 코드",
                "소분류 코드",
                "직무 코드",
                "직무 명",
                "능력단위코드",
                "능력단위수준",
                "능력단위명칭",
                "직급수준",
                "직급",
            ]
            with variant_csv_path.open("w", encoding="cp949", newline="") as handle:
                handle.write(",".join(variant_headers) + "\n")
                handle.write(",".join(rows[1]) + "\n")
            variant_result = import_career_paths_csv(conn, variant_csv_path)
            summary = career_path_summary(conn)
            partial_match = conn.execute(
                """
                SELECT matched_unit_code, unit_match_method
                FROM ncs_career_paths
                WHERE competency_name = '교섭준비'
                """
            ).fetchone()
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["rows_processed"], 2)
            self.assertEqual(result["matched_classifications"], 2)
            self.assertEqual(result["matched_units"], 2)
            self.assertTrue(variant_result["ok"])
            self.assertEqual(variant_result["rows_processed"], 1)
            self.assertEqual(variant_result["matched_units"], 1)
            self.assertEqual(variant_result["column_map"]["competency_name"], "능력단위명칭")
            self.assertEqual(summary["unit_match_rate"], 1.0)
            self.assertEqual(partial_match["matched_unit_code"], "0202020203_23v3")
            self.assertEqual(partial_match["unit_match_method"], "unit_name_contains")

    def test_training_course_xml_upserts_and_links_to_ksa_concepts(self) -> None:
        xml = """
        <root>
          <dataInfo><code>000</code><totCnt>1</totCnt><totalPage>1</totalPage></dataInfo>
          <data>
            <row>
              <ncsLclasCd>02</ncsLclasCd>
              <ncsLclasCdnm>Business</ncsLclasCdnm>
              <ncsMclasCd>02</ncsMclasCd>
              <ncsMclasCdnm>HR</ncsMclasCdnm>
              <ncsSclasCd>02</ncsSclasCd>
              <ncsSclasCdnm>HRM</ncsSclasCdnm>
              <ncsSubdCd>01</ncsSubdCd>
              <ncsSubdCdnm>HR planning</ncsSubdCdnm>
              <ncsClCd>0202020101_23v3</ncsClCd>
              <compeUnitName>HR planning</compeUnitName>
              <compeUnitLevel>5</compeUnitLevel>
              <trainGoal>Learn workforce planning practice.</trainGoal>
              <trainTime>16</trainTime>
              <facName>HR center</facName>
              <methName>Practice</methName>
            </row>
          </data>
        </root>
        """
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            payload = parse_training_course_xml(xml)
            self.assertEqual(upsert_training_courses(conn, payload["rows"]), 1)

            linked = build_training_course_ontology_links(conn)
            courses = search_training_courses(conn, concept_query="workforce planning", limit=5)
            unit_link_statuses = {
                row["review_status"]
                for row in conn.execute(
                    "SELECT review_status FROM ncs_training_course_unit_links"
                ).fetchall()
            }
            relation_types = {
                row["relation_type"]
                for row in conn.execute("SELECT relation_type FROM training_delivery_relations").fetchall()
            }
            conn.close()

            self.assertGreaterEqual(linked["links_after"], 1)
            self.assertEqual(unit_link_statuses, {"auto_linked"})
            self.assertGreaterEqual(linked["element_links_after"], 1)
            self.assertGreaterEqual(linked["goal_concept_links_after"], 1)
            self.assertGreaterEqual(linked["delivery_relations_after"], 1)
            self.assertIn("has_level", relation_types)
            self.assertIn("requires_time", relation_types)
            self.assertIn("uses_facility", relation_types)
            self.assertIn("delivered_by", relation_types)
            self.assertEqual(courses[0]["training_course"]["ncs_cl_cd"], fixture["unit_code"])
            self.assertGreaterEqual(len(courses[0]["concept_links"]), 1)
            self.assertGreaterEqual(len(courses[0]["element_links"]), 1)
            self.assertGreaterEqual(len(courses[0]["goal_concept_links"]), 1)
            self.assertGreaterEqual(len(courses[0]["delivery_relations"]), 1)

    def test_generate_training_transition_eval_scenarios_uses_non_hr_training_courses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('03', 'Finance', '01', 'Accounting', '01', 'Accounting', '01', 'Accounting')
                """
            )
            classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '03'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES
                  ('0301010101_24v1', '0301010101', '24v1', 'Accounting entry',
                   '3', ?, 'matched', ?, ?),
                  ('0301010102_24v1', '0301010102', '24v1', 'Financial statement',
                   '4', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp, classification_id, timestamp, timestamp),
            )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "03",
                        "ncs_lclas_cdnm": "Finance",
                        "ncs_mclas_cd": "01",
                        "ncs_mclas_cdnm": "Accounting",
                        "ncs_sclas_cd": "01",
                        "ncs_sclas_cdnm": "Accounting",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "Accounting",
                        "ncs_cl_cd": "0301010102_24v1",
                        "compe_unit_name": "Financial statement",
                        "compe_unit_level": "4",
                        "train_goal": "Prepare financial statements.",
                        "train_time": "20",
                        "fac_name": "Classroom",
                        "meth_name": "Practice",
                    }
                ],
            )

            dry_run = generate_training_transition_eval_scenarios(
                conn,
                target_non_hr_count=1,
                per_major_limit=1,
                per_classification_limit=1,
                reset_auto=True,
            )
            row_after_dry_run = conn.execute(
                """
                SELECT *
                FROM training_transition_gold_scenarios
                WHERE scenario_name LIKE 'auto_non_hr_%'
                """
            ).fetchone()
            result = generate_training_transition_eval_scenarios(
                conn,
                target_non_hr_count=1,
                per_major_limit=1,
                per_classification_limit=1,
                reset_auto=True,
                apply=True,
            )
            row = conn.execute(
                """
                SELECT *
                FROM training_transition_gold_scenarios
                WHERE scenario_name LIKE 'auto_non_hr_%'
                """
            ).fetchone()
            conn.close()

            self.assertTrue(dry_run["ok"])
            self.assertTrue(dry_run["report_only"])
            self.assertFalse(dry_run["db_writes"])
            self.assertEqual(dry_run["selected_count"], 1)
            self.assertEqual(dry_run["planned_scenarios"][0]["review_status"], "candidate_auto")
            self.assertIsNone(row_after_dry_run)
            self.assertTrue(result["ok"])
            self.assertFalse(result["report_only"])
            self.assertTrue(result["db_writes"])
            self.assertEqual(result["selected_count"], 1)
            self.assertIsNotNone(row)
            self.assertEqual(row["major_code"], "03")
            self.assertEqual(row["review_status"], "candidate_auto")
            self.assertIn("Financial statement", row["expected_course_names_json"])

    def test_generate_training_transition_eval_scenarios_reports_reset_auto_delete_only_write(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES (
                    'auto_non_hr_old_1', '총무', 'Old target', '03',
                    '[]', 'candidate_auto', ?, ?
                )
                """,
                (timestamp, timestamp),
            )

            result = generate_training_transition_eval_scenarios(
                conn,
                target_non_hr_count=1,
                per_major_limit=0,
                per_classification_limit=0,
                reset_auto=True,
                apply=True,
            )
            remaining_count = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM training_transition_gold_scenarios
                WHERE review_status = 'candidate_auto'
                """
            ).fetchone()["count"]
            audit_row = conn.execute(
                """
                SELECT entity_type, entity_id, action, previous_status, new_status,
                       created_by_tool
                FROM review_audit_log
                WHERE action = 'generate_training_transition_eval_set_reset_auto'
                """
            ).fetchone()
            conn.close()

            self.assertTrue(result["ok"])
            self.assertFalse(result["report_only"])
            self.assertTrue(result["db_writes"])
            self.assertTrue(result["status_update_allowed"])
            self.assertEqual(result["selected_count"], 0)
            self.assertEqual(result["reset_auto_deleted_count"], 1)
            self.assertEqual(result["reset_auto_audit_log_count"], 1)
            self.assertEqual(remaining_count, 0)
            self.assertIsNotNone(audit_row)
            self.assertEqual(audit_row["entity_type"], "training_transition_gold_scenario")
            self.assertEqual(audit_row["previous_status"], "candidate_auto")
            self.assertIsNone(audit_row["new_status"])
            self.assertEqual(
                audit_row["created_by_tool"],
                "ncs_harness.generate-training-transition-eval-set",
            )

    def test_career_path_score_uses_only_trusted_review_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_career_paths(
                    source_file, source_row_number,
                    major_code_raw, middle_code_raw, small_code_raw, job_code_raw,
                    job_name, competency_code_raw, competency_level_raw,
                    competency_name, position_level_raw, position_name,
                    major_code, middle_code, small_code, sub_code,
                    matched_classification_id, matched_unit_code,
                    classification_match_method, unit_match_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES ('test.csv', 2, '2', '2', '2', '1',
                          'HR planning', '0202020101', '5',
                          'HR planning', '5', 'Manager',
                          '02', '02', '02', '01',
                          1, ?, 'code_exact', 'unit_name_exact',
                          1.0, 'review_required', ?, ?)
                """,
                (fixture["unit_code"], timestamp, timestamp),
            )

            pending_result = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            pending_item = pending_result["recommendations"][0]
            pending_components = pending_item["score_components"]
            self.assertTrue(pending_result["ok"])
            self.assertEqual(pending_components["career_path_score"], 0.0)
            self.assertNotIn("career_path_unit_link", pending_item["match"]["reasons"])
            self.assertEqual(
                pending_result["recommendation_summary"]["trusted_career_path_rows_used"],
                0,
            )
            self.assertEqual(
                pending_result["recommendation_summary"]["career_path_review_status_counts"]["review_required"],
                1,
            )
            self.assertEqual(pending_item["career_path_evidence"], [])

            conn.execute(
                """
                UPDATE ncs_career_paths
                SET review_status = 'human_reviewed', updated_at = ?
                WHERE matched_unit_code = ?
                """,
                (now_utc(), fixture["unit_code"]),
            )
            trusted_result = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            trusted_item = trusted_result["recommendations"][0]
            conn.close()

            self.assertTrue(trusted_result["ok"])
            self.assertEqual(trusted_item["score_components"]["career_path_score"], 0.05)
            self.assertIn("career_path_unit_link", trusted_item["match"]["reasons"])
            self.assertEqual(
                trusted_result["recommendation_summary"]["trusted_career_path_rows_used"],
                1,
            )
            self.assertEqual(trusted_item["career_path_evidence"][0]["review_status"], "human_reviewed")

    def test_trusted_career_path_lookup_filters_before_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "8",
                        "fac_name": "Classroom",
                        "meth_name": "Lecture",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            timestamp = now_utc()
            pending_rows = []
            for index in range(305):
                pending_rows.append(
                    (
                        "bulk.csv",
                        1000 + index,
                        "2",
                        "2",
                        "2",
                        "1",
                        f"Pending job {index:03d}",
                        "0202020101",
                        "9",
                        f"Pending competency {index:03d}",
                        "9",
                        "Director",
                        "02",
                        "02",
                        "02",
                        "01",
                        1,
                        fixture["unit_code"],
                        "code_exact",
                        "unit_name_exact",
                        1.0,
                        "review_required",
                        timestamp,
                        timestamp,
                    )
                )
            conn.executemany(
                """
                INSERT INTO ncs_career_paths(
                    source_file, source_row_number,
                    major_code_raw, middle_code_raw, small_code_raw, job_code_raw,
                    job_name, competency_code_raw, competency_level_raw,
                    competency_name, position_level_raw, position_name,
                    major_code, middle_code, small_code, sub_code,
                    matched_classification_id, matched_unit_code,
                    classification_match_method, unit_match_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                pending_rows,
            )
            conn.execute(
                """
                INSERT INTO ncs_career_paths(
                    source_file, source_row_number,
                    major_code_raw, middle_code_raw, small_code_raw, job_code_raw,
                    job_name, competency_code_raw, competency_level_raw,
                    competency_name, position_level_raw, position_name,
                    major_code, middle_code, small_code, sub_code,
                    matched_classification_id, matched_unit_code,
                    classification_match_method, unit_match_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES ('bulk.csv', 2000, '2', '2', '2', '1',
                          'Trusted HR planning', '0202020101', '1',
                          'Trusted HR planning', '1', 'Staff',
                          '02', '02', '02', '01',
                          1, ?, 'code_exact', 'unit_name_exact',
                          1.0, 'human_reviewed', ?, ?)
                """,
                (fixture["unit_code"], timestamp, timestamp),
            )

            result = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                limit=1,
                save=False,
            )
            conn.close()

            self.assertTrue(result["ok"])
            item = result["recommendations"][0]
            self.assertEqual(result["recommendation_summary"]["trusted_career_path_rows_used"], 1)
            self.assertEqual(item["score_components"]["career_path_score"], 0.05)
            self.assertEqual(item["career_path_evidence"][0]["review_status"], "human_reviewed")

    def test_recommend_training_for_task_saves_training_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_career_paths(
                    source_file, source_row_number,
                    major_code_raw, middle_code_raw, small_code_raw, job_code_raw,
                    job_name, competency_code_raw, competency_level_raw,
                    competency_name, position_level_raw, position_name,
                    major_code, middle_code, small_code, sub_code,
                    matched_classification_id, matched_unit_code,
                    classification_match_method, unit_match_method,
                    confidence_score, review_status, created_at, updated_at
                ) VALUES ('test.csv', 2, '2', '2', '2', '1',
                          'HR planning', '0202020101', '5',
                          'HR planning', '6', 'Manager',
                          '02', '02', '02', '01',
                          1, ?, 'code_exact', 'unit_name_exact',
                          1.0, 'human_reviewed', ?, ?)
                """,
                (fixture["unit_code"], timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_qualification_items(
                    jm_cd, jm_nm, exam_insti_nm, source_payload, api_fetched_at
                ) VALUES ('Q-HR-1', 'HR Planning Qualification', 'Test Institute', '{}', ?)
                """,
                (timestamp,),
            )
            conn.execute(
                """
                INSERT INTO ncs_unit_qualification_links(
                    unit_code, jm_cd, organ_std_ver_cd,
                    edu_trng_std_tm_sum, job_basis_ablt_std_tm,
                    mand_ablt_unit_std_tm, sel_ablt_unit_std_tm,
                    compe_unit_name, ablt_unit_typ_cd, ablt_unit_typ_nm,
                    min_edu_trng_tm, link_method, confidence_score,
                    source_payload, api_fetched_at, review_status,
                    created_at, updated_at
                ) VALUES (?, 'Q-HR-1', 'v1', 80, 10, 60, 20,
                          'HR planning', 'MAND', 'Mandatory', 20,
                          'test', 1.0, '{}', ?, 'reviewed', ?, ?)
                """,
                (fixture["unit_code"], timestamp, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO ncs_job_base_competencies(
                    competency_name, normalized_key, created_at, updated_at
                ) VALUES ('Problem solving', 'problemsolving', ?, ?)
                """,
                (timestamp, timestamp),
            )
            job_base_competency_id = conn.execute(
                "SELECT job_base_competency_id FROM ncs_job_base_competencies WHERE normalized_key = 'problemsolving'"
            ).fetchone()["job_base_competency_id"]
            conn.execute(
                """
                INSERT INTO ncs_job_base_factors(
                    job_base_competency_id, factor_name, normalized_key,
                    created_at, updated_at
                ) VALUES (?, 'Analytical thinking', 'analyticalthinking', ?, ?)
                """,
                (job_base_competency_id, timestamp, timestamp),
            )
            job_base_factor_id = conn.execute(
                "SELECT job_base_factor_id FROM ncs_job_base_factors WHERE normalized_key = 'analyticalthinking'"
            ).fetchone()["job_base_factor_id"]
            conn.execute(
                """
                INSERT INTO ncs_unit_job_base_links(
                    unit_code, job_base_competency_id, job_base_factor_id,
                    ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm,
                    ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm,
                    compe_unit_name, link_method, confidence_score,
                    source_payload, api_fetched_at, review_status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, '02', 'Business', '02', 'HR',
                          '02', 'HRM', '01', 'HR planning',
                          'HR planning', 'test', 1.0, '{}', ?,
                          'reviewed', ?, ?)
                """,
                (fixture["unit_code"], job_base_competency_id, job_base_factor_id, timestamp, timestamp, timestamp),
            )

            result = recommend_training_for_task(
                conn,
                criteria_id=int(fixture["criteria_id"]),
                query="workforce",
                preferred_max_hours=20,
                preferred_methods=["Practice"],
                limit=3,
            )
            evidence_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM education_recommendation_evidence
                WHERE run_id = ? AND source_table = 'ncs_training_courses'
                """,
                (result["recommendation_run_id"],),
            ).fetchone()[0]
            qualification_evidence_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM education_recommendation_evidence
                WHERE run_id = ? AND evidence_type = 'related_qualification'
                """,
                (result["recommendation_run_id"],),
            ).fetchone()[0]
            job_base_evidence_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM education_recommendation_evidence
                WHERE run_id = ? AND evidence_type = 'job_base_competency'
                """,
                (result["recommendation_run_id"],),
            ).fetchone()[0]
            conn.close()

            self.assertTrue(result["ok"])
            self.assertGreaterEqual(len(result["recommendations"]), 1)
            self.assertEqual(result["recommendations"][0]["recommendation_source_type"], "training_course")
            self.assertGreaterEqual(len(result["recommendations"][0]["element_links"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["goal_coverage"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["ontology_relations"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["source_task_ksa_concepts"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["scope_ksa_profile"]), 1)
            self.assertTrue(result["query_resolution"]["ok"])
            self.assertEqual(
                result["recommendations"][0]["source_ksa_concepts"][0]["definition_status"],
                "candidate",
            )
            self.assertIn(
                "workforce planning supports",
                result["recommendations"][0]["source_ksa_concepts"][0]["definition"],
            )
            self.assertEqual(
                result["recommendations"][0]["ontology_relations"][0]["relation_type"],
                "knowledge_enables_skill",
            )
            self.assertGreaterEqual(len(result["recommendations"][0]["delivery_evidence"]["relations"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["explanation"]), 2)
            self.assertIn("source_element_coverage", result["recommendations"][0]["match"]["reasons"])
            self.assertIn("training_goal_ksa_coverage", result["recommendations"][0]["match"]["reasons"])
            self.assertIn("training_goal_direct_ksa_coverage", result["recommendations"][0]["match"]["reasons"])
            self.assertIn("career_path_unit_link", result["recommendations"][0]["match"]["reasons"])
            self.assertIn("preferred_time_fit", result["recommendations"][0]["match"]["reasons"])
            self.assertIn("preferred_method_fit", result["recommendations"][0]["match"]["reasons"])
            self.assertGreater(result["recommendations"][0]["match"]["weighted_goal_score"], 0)
            self.assertEqual(result["recommendations"][0]["display_tier"], "primary")
            self.assertEqual(result["recommendations"][0]["evidence_strength"]["grade"], "high")
            self.assertEqual(result["recommendations"][0]["preference_fit"]["time_fit"], "fit")
            self.assertTrue(result["recommendations"][0]["preference_fit"]["method_fit"])
            self.assertIn(
                "practice",
                result["recommendations"][0]["preference_fit"]["matched_method_groups"],
            )
            score_components = result["recommendations"][0]["score_components"]
            self.assertGreater(score_components["training_goal_ksa_score"], 0)
            self.assertGreater(score_components["element_score"], 0)
            self.assertGreater(score_components["preference_score"], 0)
            self.assertGreater(score_components["qualification_score"], 0)
            self.assertGreater(score_components["job_base_score"], 0)
            job_base_signal = result["recommendations"][0]["job_base_signal"]
            self.assertEqual(job_base_signal["evidence_role"], "auxiliary_tie_breaker")
            self.assertEqual(job_base_signal["status"], "target_scope_signal")
            self.assertEqual(job_base_signal["target_hit_count"], 1)
            self.assertEqual(job_base_signal["gap_hit_count"], 0)
            self.assertEqual(job_base_signal["target_hit_ratio"], 1.0)
            self.assertEqual(
                job_base_signal,
                result["recommendations"][0]["match"]["job_base_signal"],
            )
            self.assertEqual(
                score_components,
                result["recommendations"][0]["match"]["score_components"],
            )
            self.assertGreaterEqual(result["recommendations"][0]["match"]["goal_direct_concept_hits"], 1)
            self.assertEqual(result["recommendations"][0]["recommendation_tier"]["tier"], "primary")
            self.assertGreaterEqual(len(result["recommendation_groups"]["primary"]), 1)
            primary_group = result["recommendation_groups"]["primary"][0]
            self.assertEqual(primary_group["evidence_strength"]["grade"], "high")
            self.assertEqual(primary_group["display_tier"], "primary")
            self.assertTrue(primary_group["preference_fit"]["method_fit"])
            self.assertIn("delivery", primary_group)
            self.assertIn("coverage_counts", primary_group)
            self.assertGreaterEqual(
                primary_group["coverage_counts"]["source_ksa"]
                + primary_group["coverage_counts"]["gap_ksa"]
                + primary_group["coverage_counts"]["goal_ksa"],
                1,
            )
            self.assertGreaterEqual(len(primary_group["score_component_highlights"]), 1)
            self.assertEqual(
                result["recommendation_summary"]["primary_recommendation_count"],
                len(result["recommendation_groups"]["primary"]),
            )
            self.assertGreaterEqual(
                result["recommendation_summary"]["display_recommendation_counts"]["primary"],
                1,
            )
            self.assertGreaterEqual(result["recommendation_summary"]["career_path_units_used"], 1)
            self.assertGreaterEqual(result["recommendation_summary"]["qualification_evidence_count"], 1)
            self.assertGreaterEqual(result["recommendation_summary"]["job_base_evidence_count"], 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["career_path_evidence"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["qualification_evidence"]), 1)
            self.assertGreaterEqual(len(result["recommendations"][0]["job_base_evidence"]), 1)
            self.assertFalse(result["audit"]["sqf_used"])
            self.assertFalse(result["audit"]["learning_modules_used"])
            self.assertIn("ncs_career_paths", result["audit"]["data_sources"])
            self.assertIn("training_goal_concept_links", result["audit"]["data_sources"])
            self.assertIn("ontology_concept_relations", result["audit"]["data_sources"])
            self.assertGreaterEqual(evidence_count, 1)
            self.assertGreaterEqual(qualification_evidence_count, 1)
            self.assertGreaterEqual(job_base_evidence_count, 1)

    def test_recommend_training_downweights_rejected_ontology_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "Reviewed HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice with reviewed evidence.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    },
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "Rejected HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice with rejected evidence.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    },
                ],
            )
            build_training_course_ontology_links(conn)
            reviewed_course_id = conn.execute(
                "SELECT training_course_id FROM ncs_training_courses WHERE compe_unit_name = 'Reviewed HR planning'"
            ).fetchone()["training_course_id"]
            rejected_course_id = conn.execute(
                "SELECT training_course_id FROM ncs_training_courses WHERE compe_unit_name = 'Rejected HR planning'"
            ).fetchone()["training_course_id"]
            conn.execute(
                "UPDATE training_goal_concept_links SET review_status = 'human_reviewed' WHERE training_course_id = ?",
                (reviewed_course_id,),
            )
            conn.execute(
                "UPDATE ncs_training_course_concept_links SET review_status = 'human_reviewed' WHERE training_course_id = ?",
                (reviewed_course_id,),
            )
            conn.execute(
                "UPDATE training_goal_concept_links SET review_status = 'rejected' WHERE training_course_id = ?",
                (rejected_course_id,),
            )
            conn.execute(
                "UPDATE ncs_training_course_concept_links SET review_status = 'rejected' WHERE training_course_id = ?",
                (rejected_course_id,),
            )

            result = recommend_training_for_task(
                conn,
                criteria_id=fixture["criteria_id"],
                limit=5,
                save=False,
            )
            conn.close()

            names = [item["training_course"]["compe_unit_name"] for item in result["recommendations"]]
            reviewed = next(
                item
                for item in result["recommendations"]
                if item["training_course"]["compe_unit_name"] == "Reviewed HR planning"
            )
            rejected = next(
                item
                for item in result["recommendations"]
                if item["training_course"]["compe_unit_name"] == "Rejected HR planning"
            )
            self.assertLess(names.index("Reviewed HR planning"), names.index("Rejected HR planning"))
            self.assertGreater(
                reviewed["score_components"]["training_goal_ksa_score"],
                rejected["score_components"]["training_goal_ksa_score"],
            )
            self.assertIn("reviewed_training_goal_ksa_coverage", reviewed["match"]["reasons"])
            self.assertEqual(rejected["match"]["goal_concept_hits"], 0)

    def test_recommend_training_filters_off_scope_inherited_concept_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            try:
                initialize_database(conn)
                fixture = seed_task_ontology(conn)
                timestamp = now_utc()
                conn.execute(
                    """
                    INSERT INTO classifications(
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name
                    ) VALUES ('02', 'Business', '09', 'Operations',
                              '01', 'Facilities', '01', 'Facility safety')
                    """
                )
                off_scope_classification_id = conn.execute(
                    "SELECT classification_id FROM classifications WHERE sub_name = 'Facility safety'"
                ).fetchone()["classification_id"]
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, api_match_status,
                        created_at, updated_at
                    ) VALUES ('0209010101_23v3', '0209010101', '23v3', 'Facility safety',
                              '5', ?, 'matched', ?, ?)
                    """,
                    (off_scope_classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES ('0209010101_23v3', '1', '0209010101_23v3 1', 'Inspect facility', '5')
                    """
                )
                off_scope_element_id = conn.execute(
                    "SELECT element_id FROM competency_elements WHERE unit_code = '0209010101_23v3'"
                ).fetchone()["element_id"]
                conn.execute(
                    """
                    INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                    VALUES (?, '01', 'knowledge', '1', 'workforce planning')
                    """,
                    (off_scope_element_id,),
                )
                off_scope_ksa_id = conn.execute(
                    "SELECT ksa_id FROM ksa_items WHERE element_id = ?", (off_scope_element_id,)
                ).fetchone()["ksa_id"]
                conn.execute(
                    "INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                    (off_scope_ksa_id, fixture["concept_id"], timestamp),
                )
                upsert_training_courses(
                    conn,
                    [
                        {
                            "ncs_lclas_cd": "02",
                            "ncs_lclas_cdnm": "Business",
                            "ncs_mclas_cd": "02",
                            "ncs_mclas_cdnm": "HR",
                            "ncs_sclas_cd": "02",
                            "ncs_sclas_cdnm": "HRM",
                            "ncs_subd_cd": "01",
                            "ncs_subd_cdnm": "HR planning",
                            "ncs_cl_cd": fixture["unit_code"],
                            "compe_unit_name": "HR planning",
                            "compe_unit_level": "5",
                            "train_goal": "Learn workforce planning practice.",
                            "train_time": "16",
                            "fac_name": "HR center",
                            "meth_name": "Practice",
                        },
                        {
                            "ncs_lclas_cd": "02",
                            "ncs_lclas_cdnm": "Business",
                            "ncs_mclas_cd": "09",
                            "ncs_mclas_cdnm": "Operations",
                            "ncs_sclas_cd": "01",
                            "ncs_sclas_cdnm": "Facilities",
                            "ncs_subd_cd": "01",
                            "ncs_subd_cdnm": "Facility safety",
                            "ncs_cl_cd": "0209010101_23v3",
                            "compe_unit_name": "Facility safety",
                            "compe_unit_level": "5",
                            "train_goal": "Learn inspection routines.",
                            "train_time": "16",
                            "fac_name": "Facility lab",
                            "meth_name": "Practice",
                        },
                    ],
                )
                build_training_course_ontology_links(conn)

                result = recommend_training_for_task(
                    conn,
                    criteria_id=int(fixture["criteria_id"]),
                    limit=5,
                    save=False,
                )
                self.assertTrue(result["ok"], result)
                names = [item["training_course"]["compe_unit_name"] for item in result["recommendations"]]
            finally:
                conn.close()

            self.assertTrue(result["ok"])
            self.assertIn("HR planning", names)
            self.assertNotIn("Facility safety", names)

    def test_recommend_training_transition_compares_current_and_target_scope_ksa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            target_element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            target_criteria_id = int(fixture["criteria_id"])
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '02', 'skill', '2', 'workforce analysis')
                """,
                (target_element_id,),
            )
            target_gap_ksa_id = conn.execute(
                "SELECT ksa_id FROM ksa_items WHERE ksa_text_raw = 'workforce analysis'"
            ).fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO raw_excel_rows(
                    source_file, sheet_name, sheet_row_number,
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name,
                    unit_code, unit_name, unit_level,
                    element_code, element_name, element_level,
                    criteria_no, criteria_text,
                    ksa_type_code, ksa_type_name, ksa_no, ksa_text,
                    loaded_at
                ) VALUES (
                    'test.xlsx', 'Sheet1', 2,
                    '02', 'Business', '02', 'HR',
                    '02', 'HRM', '01', 'HR planning',
                    '0202020101_23v3', 'HR planning', '5',
                    '0202020101_23v3 1', 'Plan workforce', '5',
                    '1', 'Build a workforce plan from business strategy.',
                    '02', 'skill', '2', 'workforce analysis',
                    ?
                )
                """,
                (timestamp,),
            )
            target_gap_raw_id = conn.execute(
                "SELECT raw_row_id FROM raw_excel_rows WHERE sheet_row_number = 2"
            ).fetchone()["raw_row_id"]
            conn.execute(
                """
                INSERT INTO element_criteria_ksa_links(raw_row_id, element_id, criteria_id, ksa_id)
                VALUES (?, ?, ?, ?)
                """,
                (target_gap_raw_id, target_element_id, target_criteria_id, target_gap_ksa_id),
            )
            conn.execute(
                """
                INSERT INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, 'skill', 0, 'workforce analysis', 'workforceanalysis',
                          'test', 'raw', ?)
                """,
                (target_gap_ksa_id, target_element_id, timestamp),
            )
            target_gap_atomic_id = conn.execute(
                "SELECT atomic_id FROM ksa_atomic_items WHERE ksa_id = ?", (target_gap_ksa_id,)
            ).fetchone()["atomic_id"]
            conn.execute(
                """
                INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'raw', ?)
                """,
                (target_gap_atomic_id, fixture["skill_concept_id"], timestamp),
            )

            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '01', 'General affairs', '01', 'General affairs')
                """
            )
            current_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE sub_name = 'General affairs'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202010101_25v3', '0202010101', '25v3', 'General affairs planning',
                          '4', ?, 'matched', ?, ?)
                """,
                (current_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202010101_25v3', '1', '0202010101_25v3 1', 'Plan administration', '4')
                """
            )
            current_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '0202010101_25v3'"
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', 'Plan administrative workforce support.')
                """,
                (current_element_id,),
            )
            current_criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria WHERE element_id = ?", (current_element_id,)
            ).fetchone()["criteria_id"]
            conn.execute(
                """
                INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                VALUES (?, '01', 'knowledge', '1', 'workforce planning')
                """,
                (current_element_id,),
            )
            current_ksa_id = conn.execute(
                "SELECT ksa_id FROM ksa_items WHERE element_id = ?", (current_element_id,)
            ).fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO raw_excel_rows(
                    source_file, sheet_name, sheet_row_number,
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name,
                    unit_code, unit_name, unit_level,
                    element_code, element_name, element_level,
                    criteria_no, criteria_text,
                    ksa_type_code, ksa_type_name, ksa_no, ksa_text,
                    loaded_at
                ) VALUES (
                    'test.xlsx', 'Sheet1', 3,
                    '02', 'Business', '02', 'HR',
                    '01', 'General affairs', '01', 'General affairs',
                    '0202010101_25v3', 'General affairs planning', '4',
                    '0202010101_25v3 1', 'Plan administration', '4',
                    '1', 'Plan administrative workforce support.',
                    '01', 'knowledge', '1', 'workforce planning',
                    ?
                )
                """,
                (timestamp,),
            )
            current_raw_id = conn.execute(
                "SELECT raw_row_id FROM raw_excel_rows WHERE sheet_row_number = 3"
            ).fetchone()["raw_row_id"]
            conn.execute(
                """
                INSERT INTO element_criteria_ksa_links(raw_row_id, element_id, criteria_id, ksa_id)
                VALUES (?, ?, ?, ?)
                """,
                (current_raw_id, current_element_id, current_criteria_id, current_ksa_id),
            )
            conn.execute(
                """
                INSERT INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, 'knowledge', 0, 'workforce planning', 'workforceplanning',
                          'test', 'raw', ?)
                """,
                (current_ksa_id, current_element_id, timestamp),
            )
            current_atomic_id = conn.execute(
                "SELECT atomic_id FROM ksa_atomic_items WHERE ksa_id = ?", (current_ksa_id,)
            ).fetchone()["atomic_id"]
            conn.execute(
                """
                INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                VALUES (?, ?, 'raw', ?)
                """,
                (current_atomic_id, fixture["concept_id"], timestamp),
            )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning and workforce analysis practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code,
                    small_code, sub_code, confidence_score, source_method,
                    review_status, created_at, updated_at
                ) VALUES ('HR planning', 'HR planning', '02', '02', '02', '01',
                          0.99, 'test', 'candidate', ?, ?)
                """,
                (timestamp, timestamp),
            )

            result = recommend_training_transition(
                conn,
                current_query="General affairs",
                target_query="HR planning",
                major_code="02",
                preferred_max_hours=20,
                preferred_methods=["Practice"],
                limit=2,
                save=False,
            )
            conn.execute("DELETE FROM training_transition_gold_scenarios")
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES ('fixture_transition', 'General affairs', 'HR planning', '02',
                          'General affairs', 'HR planning', '["HR planning"]',
                          'candidate', ?, ?)
                """,
                (timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO training_transition_gold_scenarios(
                    scenario_name, current_query, target_query, major_code,
                    expected_current_match_text, expected_target_match_text,
                    expected_course_names_json, review_status, created_at, updated_at
                ) VALUES ('fixture_transition_reviewed', 'General affairs', 'HR planning', '02',
                          'General affairs', 'HR planning', '["HR planning"]',
                          'reviewed', ?, ?)
                """,
                (timestamp, timestamp),
            )
            evaluation = evaluate_training_transition_scenarios(conn, limit=2)
            trusted_evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=2,
                review_statuses=list(TRUSTED_TRANSITION_REVIEW_STATUSES),
            )
            candidate_evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=2,
                review_statuses=["candidate"],
            )
            limited_evaluation = evaluate_training_transition_scenarios(
                conn,
                limit=2,
                scenario_limit=1,
            )
            conn.close()

            self.assertTrue(result["ok"])
            self.assertGreaterEqual(len(result["transition"]["explanation"]), 3)
            self.assertEqual(result["transition"]["summary"]["transferable_ksa_concept_count"], 1)
            self.assertEqual(result["transition"]["summary"]["gap_ksa_concept_count"], 1)
            self.assertEqual(result["transition"]["transferable_ksa"][0]["concept_name"], "workforce planning")
            self.assertEqual(result["transition"]["gap_ksa"][0]["concept_name"], "workforce analysis")
            self.assertEqual(result["transition_request"]["current_query"], "General affairs")
            self.assertEqual(result["transition_request"]["target_query"], "HR planning")
            self.assertEqual(result["source_task_role"], "target_task")
            self.assertEqual(result["current_task"]["unit_name"], "General affairs planning")
            self.assertEqual(result["target_task"]["unit_name"], "HR planning")
            self.assertGreaterEqual(len(result["recommendations"]), 1)
            self.assertGreaterEqual(len(result["training_sequence_plan"]), 1)
            self.assertEqual(result["recommendations"][0]["training_sequence"]["stage"], 1)
            self.assertEqual(result["recommendations"][0]["training_sequence"]["role"], "direct_target_coverage")
            self.assertEqual(result["recommendations"][0]["recommendation_tier"]["tier"], "primary")
            self.assertGreaterEqual(len(result["recommendation_groups"]["primary"]), 1)
            self.assertIn("supplemental", result["recommendation_groups"])
            primary_group = result["recommendation_groups"]["primary"][0]
            self.assertIn("evidence_strength", primary_group)
            self.assertIn("delivery", primary_group)
            self.assertIn("coverage_counts", primary_group)
            self.assertGreaterEqual(
                primary_group["coverage_counts"]["source_ksa"]
                + primary_group["coverage_counts"]["gap_ksa"]
                + primary_group["coverage_counts"]["goal_ksa"],
                1,
            )
            self.assertGreaterEqual(len(result["recommendations"][0]["score_component_highlights"]), 1)
            compact = compact_training_transition_response(result, recommendation_limit=2)
            self.assertTrue(compact["ok"])
            self.assertEqual(compact["view"], "compact_training_transition")
            self.assertIn("scope_interpretation", compact)
            self.assertIn("recommended_courses", compact)
            self.assertNotIn("recommendations", compact)
            self.assertIn("audit", compact)
            self.assertFalse(compact["audit"]["sqf_used"])
            self.assertFalse(compact["audit"]["learning_modules_used"])
            self.assertIn("ncs_career_paths", compact["audit"]["data_sources"])
            self.assertIn("training_goal_concept_links", compact["audit"]["data_sources"])
            self.assertNotIn("source_payload", json.dumps(compact["recommended_courses"], ensure_ascii=False))
            summary = result["transition"]["summary"]
            self.assertEqual(summary["exact_ksa_overlap_ratio"], summary["transferability_ratio"])
            self.assertGreater(
                summary["ontology_adjusted_transferability_ratio"],
                summary["exact_ksa_overlap_ratio"],
            )
            self.assertIn("ontology_related_score", summary["adjusted_transferability_components"])
            self.assertEqual(
                compact["answer_summary"]["transition_assessment"]["ontology_adjusted_transferability_ratio"],
                summary["ontology_adjusted_transferability_ratio"],
            )
            self.assertTrue(evaluation["ok"])
            self.assertEqual(evaluation["scenario_count"], 2)
            self.assertEqual(evaluation["review_status_filter"], ["not_rejected"])
            self.assertEqual(trusted_evaluation["scenario_count"], 1)
            self.assertEqual(candidate_evaluation["scenario_count"], 1)
            self.assertEqual(limited_evaluation["scenario_limit"], 1)
            self.assertEqual(limited_evaluation["scenario_count"], 1)
            self.assertEqual(trusted_evaluation["cases"][0]["review_status"], "reviewed")
            self.assertEqual(candidate_evaluation["cases"][0]["review_status"], "candidate")
            self.assertIn("precision_at_k", evaluation)
            self.assertIn("top1_expected_hit_rate", evaluation)
            self.assertIn("mrr_at_k", evaluation)
            self.assertIn("map_at_k", evaluation)
            self.assertIn("ndcg_at_k", evaluation)
            self.assertGreaterEqual(evaluation["top1_expected_hit_rate"], 0.0)
            successful_case = next(item for item in evaluation["cases"] if item.get("ok"))
            self.assertIn("first_expected_rank", successful_case)
            self.assertIn("average_precision_at_k", successful_case)
            self.assertIn("ndcg_at_k", successful_case)
            self.assertIn("recommended_course_evidence", successful_case)
            self.assertGreaterEqual(len(successful_case["recommended_course_evidence"]), 1)
            self.assertEqual(
                successful_case["recommended_course_evidence"][0]["course_scope_fit"]["relation"],
                "direct_scope_unit",
            )
            self.assertEqual(
                successful_case["recommended_course_scope_summary"]["relation_counts"]["direct_scope_unit"],
                1,
            )
            self.assertIn("course_scope_relation_counts", evaluation)
            self.assertGreaterEqual(evaluation["course_scope_relation_counts"]["direct_scope_unit"], 1)

    def test_ksa_meaning_candidates_do_not_apply_candidate_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            conn.execute(
                """
                UPDATE ontology_concepts
                SET definition = NULL,
                    definition_source = NULL,
                    definition_status = 'missing',
                    review_status = 'raw'
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            ksa_id = conn.execute("SELECT ksa_id FROM ksa_items").fetchone()["ksa_id"]
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition_status,
                    relation_status, review_status, created_at, updated_at
                ) VALUES ('headcount forecast', 'headcountforecast', 'skill',
                          'missing', 'unlinked', 'raw', ?, ?)
                """,
                (timestamp, timestamp),
            )
            atomic_only_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'headcountforecast'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ksa_atomic_items(
                    ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                    normalized_key, split_method, review_status, created_at
                ) VALUES (?, ?, 'skill', 1, 'headcount forecast', 'headcountforecast',
                          'test', 'raw', ?)
                """,
                (ksa_id, element_id, timestamp),
            )
            atomic_id = conn.execute(
                "SELECT atomic_id FROM ksa_atomic_items WHERE normalized_key = 'headcountforecast'"
            ).fetchone()["atomic_id"]
            conn.execute(
                "INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at) VALUES (?, ?, 'raw', ?)",
                (atomic_id, atomic_only_concept_id, timestamp),
            )

            result = build_ksa_meaning_candidates(conn)
            task_row = conn.execute(
                """
                SELECT kmc.*, oc.definition, oc.definition_status
                FROM ksa_meaning_candidates kmc
                JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
                WHERE kmc.concept_id = ?
                  AND kmc.source_method = 'task_context_template'
                """,
                (fixture["concept_id"],),
            ).fetchone()
            term_row = conn.execute(
                """
                SELECT kmc.*, oc.definition, oc.definition_source, oc.definition_status
                FROM ksa_meaning_candidates kmc
                JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
                WHERE kmc.concept_id = ?
                  AND kmc.source_method = 'term_definition_template'
                """,
                (fixture["concept_id"],),
            ).fetchone()
            atomic_only = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (atomic_only_concept_id,),
            ).fetchone()
            conn.close()

            self.assertEqual(result["meaning_contexts_processed"], 2)
            self.assertEqual(result["definitions_updated"], 0)
            self.assertFalse(result["definitions_applied"])
            self.assertFalse(result["unlinked_context_included"])
            self.assertIsNotNone(task_row)
            self.assertIsNotNone(term_row)
            self.assertIn("workforce planning", task_row["meaning_text"])
            self.assertIn("Build a workforce plan from business strategy", task_row["meaning_text"])
            self.assertEqual(task_row["source_method"], "task_context_template")
            self.assertEqual(term_row["source_method"], "term_definition_template")
            self.assertIn("workforce planning", term_row["meaning_text"])
            self.assertIsNone(term_row["definition"])
            self.assertEqual(term_row["definition_status"], "missing")
            self.assertIsNone(atomic_only["definition"])
            self.assertIsNone(atomic_only["definition_source"])
            self.assertEqual(atomic_only["definition_status"], "missing")
            self.assertEqual(atomic_only["review_status"], "raw")

            with self.assertRaisesRegex(ValueError, "apply_to_definitions is disabled"):
                build_ksa_meaning_candidates(conn, apply_to_definitions=True)

    def test_ksa_meaning_candidates_reset_automated_review_status_when_text_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = ?",
                (fixture["unit_code"],),
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'term_definition_candidate',
                          'workforce planning: old generated template.',
                          'term_definition_template',
                          'old evidence', '0202020101_23v3', ?, ?, ?,
                          0.40, 'llm_reviewed', ?, ?)
                """,
                (
                    fixture["concept_id"],
                    element_id,
                    fixture["criteria_id"],
                    fixture["ksa_id"],
                    timestamp,
                    timestamp,
                ),
            )
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'task_knowledge_significance',
                          'old context candidate.',
                          'task_context_template',
                          'old evidence', '0202020101_23v3', ?, ?, ?,
                          0.40, 'llm_reviewed', ?, ?)
                """,
                (
                    fixture["concept_id"],
                    element_id,
                    fixture["criteria_id"],
                    fixture["ksa_id"],
                    timestamp,
                    timestamp,
                ),
            )

            build_ksa_meaning_candidates(conn)
            rows = {
                row["source_method"]: row
                for row in conn.execute(
                    """
                    SELECT source_method, meaning_text, review_status
                    FROM ksa_meaning_candidates
                    WHERE concept_id = ?
                      AND source_method IN ('term_definition_template', 'task_context_template')
                    """,
                    (fixture["concept_id"],),
                ).fetchall()
            }
            conn.close()

        self.assertIn("workforce planning", rows["term_definition_template"]["meaning_text"])
        self.assertNotEqual(
            rows["term_definition_template"]["meaning_text"],
            "workforce planning: old generated template.",
        )
        self.assertEqual(rows["term_definition_template"]["review_status"], "candidate")
        self.assertNotEqual(rows["task_context_template"]["meaning_text"], "old context candidate.")
        self.assertEqual(rows["task_context_template"]["review_status"], "candidate")

    def test_ksa_meaning_candidates_machine_review_is_not_human_approval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            original_raw = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]
            concept_before = conn.execute(
                """
                SELECT definition, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()

            build_ksa_meaning_candidates(conn)
            result = machine_review_ksa_meaning_candidates(conn)
            rows = {
                row["source_method"]: row["review_status"]
                for row in conn.execute(
                    """
                    SELECT source_method, review_status
                    FROM ksa_meaning_candidates
                    WHERE concept_id = ?
                    """,
                    (fixture["concept_id"],),
                ).fetchall()
            }
            concept_row = conn.execute(
                """
                SELECT definition, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()
            raw_after = conn.execute(
                "SELECT ksa_text_raw FROM ksa_items WHERE ksa_id = ?",
                (fixture["ksa_id"],),
            ).fetchone()["ksa_text_raw"]
            trusted_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_meaning_candidates
                WHERE review_status IN ('human_reviewed', 'accepted', 'reviewed')
                """
            ).fetchone()[0]
            conn.close()

        self.assertTrue(result["machine_review"])
        self.assertFalse(result["human_review_status_updates"])
        self.assertEqual(result["definitions_updated"], 0)
        self.assertEqual(rows["task_context_template"], "llm_reviewed")
        self.assertEqual(rows["term_definition_template"], "needs_review")
        self.assertEqual(original_raw, raw_after)
        self.assertEqual(concept_row["definition"], concept_before["definition"])
        self.assertEqual(concept_row["definition_status"], concept_before["definition_status"])
        self.assertEqual(concept_row["review_status"], concept_before["review_status"])
        self.assertEqual(trusted_count, 0)

    def test_ksa_meaning_candidates_machine_review_preserves_locked_statuses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)

            build_ksa_meaning_candidates(conn)
            conn.execute(
                """
                UPDATE ksa_meaning_candidates
                SET review_status = 'human_reviewed'
                WHERE concept_id = ?
                  AND source_method = 'task_context_template'
                """,
                (fixture["concept_id"],),
            )
            conn.execute(
                """
                UPDATE ksa_meaning_candidates
                SET review_status = 'rejected'
                WHERE concept_id = ?
                  AND source_method = 'term_definition_template'
                """,
                (fixture["concept_id"],),
            )

            result = machine_review_ksa_meaning_candidates(conn)
            rows = {
                row["source_method"]: row["review_status"]
                for row in conn.execute(
                    """
                    SELECT source_method, review_status
                    FROM ksa_meaning_candidates
                    WHERE concept_id = ?
                    """,
                    (fixture["concept_id"],),
                ).fetchall()
            }
            conn.close()

        self.assertEqual(result["eligible_meanings_screened"], 0)
        self.assertGreaterEqual(result["locked_status_preserved"], 2)
        self.assertEqual(rows["task_context_template"], "human_reviewed")
        self.assertEqual(rows["term_definition_template"], "rejected")

    def test_ksa_meaning_candidates_preserve_non_automated_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)

            conn.execute(
                """
                UPDATE ontology_concepts
                SET definition = 'Human-authored workforce planning definition.',
                    definition_source = 'manual',
                    definition_status = 'defined',
                    review_status = 'raw'
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            )

            result = build_ksa_meaning_candidates(conn)
            concept_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()
            term_candidate_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_meaning_candidates
                WHERE concept_id = ?
                  AND source_method = 'term_definition_template'
                """,
                (fixture["concept_id"],),
            ).fetchone()[0]
            conn.close()

            self.assertEqual(result["definitions_updated"], 0)
            self.assertEqual(term_candidate_count, 1)
            self.assertEqual(
                concept_row["definition"],
                "Human-authored workforce planning definition.",
            )
            self.assertEqual(concept_row["definition_source"], "manual")
            self.assertEqual(concept_row["definition_status"], "defined")
            self.assertEqual(concept_row["review_status"], "raw")

    def test_ksa_meaning_candidates_preserve_rejected_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()

            conn.execute(
                """
                UPDATE ontology_concepts
                SET definition = 'Rejected workforce planning definition.',
                    definition_source = 'manual_rejection_context',
                    definition_status = 'candidate',
                    review_status = 'rejected'
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            )
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type, definition,
                    definition_source, definition_status, relation_status,
                    review_status, created_at, updated_at
                ) VALUES ('rejected unlinked definition', 'rejectedunlinkeddefinition',
                          'knowledge', 'Rejected unlinked template definition.',
                          'ksa_meaning_candidates.term_definition_template',
                          'candidate', 'unlinked', 'rejected', ?, ?)
                """,
                (timestamp, timestamp),
            )
            unlinked_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'rejectedunlinkeddefinition'"
            ).fetchone()["concept_id"]
            conn.execute(
                """
                INSERT INTO ksa_meaning_candidates(
                    concept_id, concept_type, meaning_role, meaning_text,
                    source_method, evidence_text, unit_code, element_id,
                    criteria_id, ksa_id, confidence_score, review_status,
                    created_at, updated_at
                ) VALUES (?, 'knowledge', 'task_knowledge_significance',
                          'Rejected task-context candidate.',
                          'task_context_template', 'old evidence', ?, NULL, NULL, NULL,
                          0.9, 'rejected', ?, ?)
                """,
                (fixture["concept_id"], fixture["unit_code"], timestamp, timestamp),
            )

            result = build_ksa_meaning_candidates(conn)
            concept_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (fixture["concept_id"],),
            ).fetchone()
            unlinked_row = conn.execute(
                """
                SELECT definition, definition_source, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (unlinked_concept_id,),
            ).fetchone()
            meaning_row = conn.execute(
                """
                SELECT meaning_text, evidence_text, review_status
                FROM ksa_meaning_candidates
                WHERE concept_id = ?
                  AND meaning_role = 'task_knowledge_significance'
                  AND source_method = 'task_context_template'
                """,
                (fixture["concept_id"],),
            ).fetchone()
            conn.close()

            self.assertEqual(result["definitions_updated"], 0)
            self.assertEqual(concept_row["definition"], "Rejected workforce planning definition.")
            self.assertEqual(concept_row["definition_source"], "manual_rejection_context")
            self.assertEqual(concept_row["definition_status"], "candidate")
            self.assertEqual(concept_row["review_status"], "rejected")
            self.assertEqual(unlinked_row["definition"], "Rejected unlinked template definition.")
            self.assertEqual(
                unlinked_row["definition_source"],
                "ksa_meaning_candidates.term_definition_template",
            )
            self.assertEqual(unlinked_row["definition_status"], "candidate")
            self.assertEqual(unlinked_row["review_status"], "rejected")
            self.assertEqual(meaning_row["meaning_text"], "Rejected task-context candidate.")
            self.assertEqual(meaning_row["evidence_text"], "old evidence")
            self.assertEqual(meaning_row["review_status"], "rejected")

    def test_ksa_meaning_candidates_include_unlinked_only_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)

            default_result = build_ksa_meaning_candidates(conn)
            default_unlinked = conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_meaning_candidates
                WHERE concept_id = ?
                  AND source_method = 'unlinked_concept_fallback'
                """,
                (fixture["skill_concept_id"],),
            ).fetchone()[0]

            fallback_result = build_ksa_meaning_candidates(conn, include_unlinked=True)
            fallback_unlinked = conn.execute(
                """
                SELECT COUNT(*)
                FROM ksa_meaning_candidates
                WHERE concept_id = ?
                  AND source_method = 'unlinked_concept_fallback'
                """,
                (fixture["skill_concept_id"],),
            ).fetchone()[0]
            conn.close()

            self.assertFalse(default_result["unlinked_context_included"])
            self.assertEqual(default_unlinked, 0)
            self.assertTrue(fallback_result["unlinked_context_included"])
            self.assertEqual(fallback_unlinked, 1)

    def test_mcp_surface_excludes_sqf_and_learning_module_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("NCS_DB_PATH")
            os.environ["NCS_DB_PATH"] = str(Path(tmp) / "ncs.db")
            try:
                from ncs_mcp import server, tool_registry

                tools = getattr(getattr(server.mcp, "_tool_manager", None), "_tools", {})
                surface = server.current_mcp_tool_surface()
                # Advanced ontology / education-integration / transition tools are
                # hidden from the public surface by default.
                public_tools = tool_registry.USER_MCP_TOOLS - tool_registry.ADVANCED_MCP_TOOLS
                self.assertLessEqual(len(tools), 7)
                self.assertEqual(set(surface["all_tools"]), public_tools)
                self.assertEqual(set(surface["user_tools"]), public_tools)
                self.assertEqual(surface["operator_tools"], [])
                self.assertFalse(surface["operator_tools_enabled"])
                self.assertFalse(surface["advanced_tools_enabled"])
                self.assertEqual(set(surface["hidden_operator_tools"]), tool_registry.OPERATOR_MCP_TOOLS)
                self.assertEqual(set(surface["hidden_advanced_tools"]), tool_registry.ADVANCED_MCP_TOOLS)
                self.assertIn("ncs_discover_tools", tools)
                self.assertIn("ncs_execute_tool", tools)
                self.assertIn("ncs_search", tools)
                self.assertIn("ncs_unit_detail", tools)
                self.assertIn("ncs_training", tools)
                self.assertIn("ncs_analysis", tools)
                self.assertIn("recommend_training_for_task", tools)
                self.assertNotIn("recommend_training_transition", tools)
                self.assertNotIn("plan_ncs_education_path", tools)
                self.assertNotIn("recommend_task_transitions", tools)
                self.assertNotIn("get_concept_evidence", tools)
                self.assertNotIn("get_quality_issues", tools)
                self.assertNotIn("review_training_goal_concept_link", tools)
                self.assertNotIn("review_task_ksa_concept_relation", tools)
                self.assertNotIn("review_learning_module_ncs_link", tools)
                self.assertNotIn("review_ontology_concept", tools)
                self.assertEqual(len(tools), 7)
                self.assertIn("ncs_discover_tools", surface["user_tools"])
                self.assertIn("ncs_execute_tool", surface["user_tools"])
                self.assertIn("ncs_search", surface["user_tools"])
                self.assertNotIn("recommend_training_transition", surface["user_tools"])
                self.assertNotIn("plan_ncs_education_path", surface["user_tools"])
                self.assertEqual(surface["legacy_tools_present"], [])
                self.assertEqual(surface["unexpected_tools"], [])
                self.assertNotIn("search_training_courses", tools)
                self.assertNotIn("collect_qualification_items", tools)
                self.assertNotIn("collect_job_base_competencies", tools)
                self.assertNotIn("recommend_education_for_duty", tools)
                self.assertNotIn("search_learning_modules", tools)
                self.assertNotIn("search_sqf_jobs", tools)
            finally:
                if previous is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous

    def test_operator_review_tools_update_active_recommendation_links(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("NCS_DB_PATH")
            db_path = Path(tmp) / "ncs.db"
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                conn = connect(db_path)
                initialize_database(conn)
                fixture = seed_task_ontology(conn)
                timestamp = now_utc()
                element_id = conn.execute(
                    "SELECT element_id FROM competency_elements WHERE unit_code = ?",
                    (fixture["unit_code"],),
                ).fetchone()["element_id"]
                conn.execute(
                    """
                    INSERT INTO ncs_training_courses(
                        ncs_cl_cd, compe_unit_name, train_goal, train_time, api_fetched_at
                    ) VALUES (?, 'HR planning', 'Build workforce planning capability.', '24', ?)
                    """,
                    (fixture["unit_code"], timestamp),
                )
                course_id = conn.execute("SELECT training_course_id FROM ncs_training_courses").fetchone()[
                    "training_course_id"
                ]
                conn.execute(
                    """
                    INSERT INTO training_goal_concept_links(
                        training_course_id, unit_code, element_id, concept_id,
                        link_method, confidence_score, evidence_text, review_status,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'training_goal_concept_token', 0.7,
                              'goal evidence', 'auto_linked', ?, ?)
                    """,
                    (course_id, fixture["unit_code"], element_id, fixture["concept_id"], timestamp, timestamp),
                )
                goal_link_id = conn.execute(
                    "SELECT link_id FROM training_goal_concept_links"
                ).fetchone()["link_id"]
                source_atomic_id = conn.execute(
                    "SELECT atomic_id FROM ksa_atomic_items WHERE normalized_key = 'workforceplanning'"
                ).fetchone()["atomic_id"]
                conn.execute(
                    """
                    INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                    VALUES (?, '02', 'skill', '1', 'workforce analysis')
                    """,
                    (element_id,),
                )
                skill_ksa_id = conn.execute(
                    "SELECT ksa_id FROM ksa_items WHERE ksa_text_raw = 'workforce analysis'"
                ).fetchone()["ksa_id"]
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                        normalized_key, split_method, review_status, created_at
                    ) VALUES (?, ?, 'skill', 0, 'workforce analysis', 'workforceanalysis',
                              'test', 'raw', ?)
                    """,
                    (skill_ksa_id, element_id, timestamp),
                )
                target_atomic_id = conn.execute(
                    "SELECT atomic_id FROM ksa_atomic_items WHERE normalized_key = 'workforceanalysis'"
                ).fetchone()["atomic_id"]
                conn.execute(
                    """
                    INSERT INTO task_ksa_concept_relations(
                        criteria_id, element_id, source_concept_id, relation_type,
                        target_concept_id, source_atomic_id, target_atomic_id,
                        evidence_text, confidence_score, review_status, created_at
                    ) VALUES (?, ?, ?, 'knowledge_enables_skill', ?, ?, ?,
                              'relation evidence', 0.55, 'candidate', ?)
                    """,
                    (
                        fixture["criteria_id"],
                        element_id,
                        fixture["concept_id"],
                        fixture["skill_concept_id"],
                        source_atomic_id,
                        target_atomic_id,
                        timestamp,
                    ),
                )
                relation_id = conn.execute(
                    "SELECT relation_id FROM task_ksa_concept_relations"
                ).fetchone()["relation_id"]
                conn.commit()
                conn.close()

                from ncs_mcp import server

                reports_dir = ROOT / "reports" / "_test_review_packets" / Path(tmp).name
                reports_dir.mkdir(parents=True, exist_ok=True)
                self.addCleanup(shutil.rmtree, reports_dir, ignore_errors=True)
                review_packet = reports_dir / "review_packet.md"
                review_packet_text = (
                    f"training_goal_concept_link:{goal_link_id}\n"
                    "Human confirmed direct training-goal concept coverage.\n"
                )
                review_packet.write_text(review_packet_text, encoding="utf-8")
                review_packet_ref = str(review_packet)
                review_packet_stored_ref = review_packet.relative_to(ROOT).as_posix()
                review_packet_hash = "sha256:" + hashlib.sha256(
                    review_packet.read_bytes()
                ).hexdigest()

                blocked_goal_review = server.review_training_goal_concept_link(
                    goal_link_id,
                    "human_reviewed",
                    reviewer_id="mcp",
                    notes="missing provenance",
                )
                goal_review = server.review_training_goal_concept_link(
                    goal_link_id,
                    "human_reviewed",
                    reviewer_id="tester",
                    notes="valid direct coverage",
                    confidence_score=0.95,
                    source_decision_packet=review_packet_ref,
                    source_artifact_hash=review_packet_hash,
                    rationale="Human confirmed direct training-goal concept coverage.",
                    evidence_refs=["training_goal_concept_link:test"],
                    run_artifact="reports/operator_review_run.json",
                )
                relation_review = server.review_task_ksa_concept_relation(
                    relation_id,
                    "rejected",
                    reviewer_id="tester",
                    notes="co-occurrence noise",
                )
                invalid_status = server.review_training_goal_concept_link(goal_link_id, "approved")

                conn = connect(db_path)
                goal_row = conn.execute(
                    "SELECT review_status, confidence_score FROM training_goal_concept_links WHERE link_id = ?",
                    (goal_link_id,),
                ).fetchone()
                relation_row = conn.execute(
                    "SELECT review_status FROM task_ksa_concept_relations WHERE relation_id = ?",
                    (relation_id,),
                ).fetchone()
                audit_rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT action, source_decision_packet, rationale, evidence_refs_json,
                               created_by_tool, run_artifact
                        FROM review_audit_log
                        WHERE action IN (
                            'review_training_goal_concept_link',
                            'review_task_ksa_concept_relation'
                        )
                        ORDER BY id
                        """
                    ).fetchall()
                ]
                conn.close()
            finally:
                if previous is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous

        self.assertFalse(blocked_goal_review["ok"])
        self.assertEqual(blocked_goal_review["error"]["code"], "trusted_review_provenance_required")
        self.assertTrue(goal_review["ok"])
        self.assertEqual(goal_review["previous_status"], "auto_linked")
        self.assertEqual(goal_review["new_status"], "human_reviewed")
        self.assertTrue(goal_review["trusted_for_recommendation"])
        self.assertTrue(relation_review["ok"])
        self.assertEqual(relation_review["previous_status"], "candidate")
        self.assertEqual(relation_review["new_status"], "rejected")
        self.assertFalse(relation_review["relation_usable_for_transition"])
        self.assertFalse(invalid_status["ok"])
        self.assertEqual(invalid_status["error"]["code"], "unsupported_review_status")
        self.assertEqual(goal_row["review_status"], "human_reviewed")
        self.assertAlmostEqual(goal_row["confidence_score"], 0.95)
        self.assertEqual(relation_row["review_status"], "rejected")
        self.assertEqual(len(audit_rows), 2)
        goal_audit = [row for row in audit_rows if row["action"] == "review_training_goal_concept_link"][0]
        self.assertEqual(goal_audit["source_decision_packet"], review_packet_stored_ref)
        self.assertEqual(goal_audit["rationale"], "Human confirmed direct training-goal concept coverage.")
        self.assertEqual(json.loads(goal_audit["evidence_refs_json"]), ["training_goal_concept_link:test"])
        self.assertEqual(goal_audit["created_by_tool"], "mcp.review_training_goal_concept_link")
        self.assertEqual(goal_audit["run_artifact"], "reports/operator_review_run.json")

    def test_ncs_meta_tools_discover_and_execute_read_only_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("NCS_DB_PATH")
            db_path = Path(tmp) / "ncs.db"
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                conn = connect(db_path)
                initialize_database(conn)
                seed_task_ontology(conn)
                conn.commit()
                conn.close()
                from ncs_mcp import server

                discovery = server.ncs_discover_tools(intent="training transition")
                search_route = route_ncs_query(
                    "HR planning NCS search",
                    available_tool_names=server.tool_registry.NCS_EXECUTABLE_TOOL_NAMES,
                )
                execute_result = server.ncs_execute_tool(
                    "ncs_search",
                    {
                        "query": "HR planning",
                        "scope": "unit",
                        "limit": 2,
                        "_route_query": "HR planning NCS search",
                        "_route_fingerprint": search_route["route_fingerprint"],
                    },
                )
                fingerprint_mismatch = server.ncs_execute_tool(
                    "ncs_search",
                    {
                        "query": "HR planning",
                        "_route_query": "HR planning NCS search",
                        "_route_fingerprint": "stale-route",
                    },
                )
                tool_mismatch = server.ncs_execute_tool(
                    "ncs_search",
                    {
                        "query": "HR planning",
                        "_route_query": "from labor management to HR planning reskilling path",
                    },
                )
                blocked = server.ncs_execute_tool(
                    "review_ontology_concept",
                    {"concept_id": 1},
                )
                recursive = server.ncs_execute_tool("ncs_execute_tool", {})
            finally:
                if previous is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous

        discovered_names = [
            tool["name"]
            for category in discovery["data"]["matched_categories"]
            for tool in category["tools"]
        ]
        # Advanced ontology/transition tools are hidden by default, so discovery
        # surfaces only the stable core training tools.
        self.assertIn("recommend_training_for_task", discovered_names)
        self.assertNotIn("recommend_training_transition", discovered_names)
        self.assertNotIn("plan_ncs_education_path", discovered_names)
        self.assertIn("route_fingerprint", discovery["data"]["query_route"])
        self.assertIn("guard_flags", discovery["data"]["query_route"])
        self.assertTrue(execute_result["ok"])
        self.assertEqual(execute_result["meta_execution"]["tool_name"], "ncs_search")
        self.assertEqual(execute_result["meta_execution"]["query_route"]["tool"], "ncs_search")
        self.assertEqual(execute_result["meta_execution"]["route_fingerprint"], search_route["route_fingerprint"])
        self.assertEqual(execute_result["meta_execution"]["route_contract_schema"], "ncs_query_route_v1")
        self.assertTrue(execute_result["meta_execution"]["route_tool_allowed"])
        self.assertEqual(execute_result["meta_execution"]["route_allowed_tools"], ["ncs_search"])
        self.assertFalse(execute_result["meta_execution"]["route_tool_mismatch"])
        self.assertFalse(fingerprint_mismatch["ok"])
        self.assertEqual(fingerprint_mismatch["error"]["code"], "route_fingerprint_mismatch")
        self.assertFalse(tool_mismatch["ok"])
        self.assertEqual(tool_mismatch["error"]["code"], "route_tool_mismatch")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["error"]["code"], "tool_not_executable_via_meta")
        self.assertFalse(recursive["ok"])
        self.assertEqual(recursive["error"]["code"], "meta_tool_recursion_blocked")

    def test_ncs_error_responses_mask_api_keys(self) -> None:
        from ncs_mcp import server

        secret = "secret-token-123456"
        previous = os.environ.get("NCS_SERVICE_KEY")
        os.environ["NCS_SERVICE_KEY"] = secret
        try:
            result = server.error_response(
                "external_api_error",
                detail=f"https://example.test?authKey={secret}&returnType=XML",
                serviceKey=secret,
                nested={"url": f"https://example.test?serviceKey={secret}&pageNo=1"},
            )
        finally:
            if previous is None:
                os.environ.pop("NCS_SERVICE_KEY", None)
            else:
                os.environ["NCS_SERVICE_KEY"] = previous

        payload_text = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "external_api_error")
        self.assertEqual(result["error"]["serviceKey"], "[REDACTED]")
        self.assertNotIn(secret, payload_text)
        self.assertIn("authKey=[REDACTED]", payload_text)
        self.assertIn("serviceKey=[REDACTED]", payload_text)
        self.assertEqual(result["error"]["category"], "external_dependency")
        self.assertTrue(result["error"]["retryable"])
        self.assertTrue(result["error"]["known"])

    def test_ncs_not_found_error_preserves_specific_code_and_fields(self) -> None:
        from ncs_mcp import server

        result = server.error_response("concept_not_found", concept_id=999, query="missing")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "concept_not_found")
        self.assertEqual(result["error"]["category"], "not_found")
        self.assertFalse(result["error"]["retryable"])
        self.assertTrue(result["error"]["known"])
        self.assertEqual(result["error"]["concept_id"], 999)
        self.assertEqual(result["data"]["concept_id"], 999)
        self.assertIn("[NOT_FOUND]", result["content"][0]["text"])

    def test_ncs_execute_tool_masks_handler_exceptions(self) -> None:
        from ncs_mcp import server

        secret = "runtime-secret-123456"

        def failing_handler(**_params):
            raise RuntimeError(f"upstream failed serviceKey={secret}&pageNo=1")

        previous = os.environ.get("NCS_TRAINING_COURSE_SERVICE_KEY")
        os.environ["NCS_TRAINING_COURSE_SERVICE_KEY"] = secret
        try:
            with patch.dict(server.NCS_EXECUTABLE_TOOL_HANDLERS, {"ncs_search": failing_handler}):
                result = server.ncs_execute_tool("ncs_search", {"query": "HR planning"})
        finally:
            if previous is None:
                os.environ.pop("NCS_TRAINING_COURSE_SERVICE_KEY", None)
            else:
                os.environ["NCS_TRAINING_COURSE_SERVICE_KEY"] = previous

        payload_text = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "tool_execution_failed")
        self.assertEqual(result["error"]["tool_name"], "ncs_search")
        self.assertNotIn(secret, payload_text)
        self.assertIn("serviceKey=[REDACTED]", payload_text)

    def test_ncs_execute_tool_distinguishes_bad_args_from_internal_type_error(self) -> None:
        from ncs_mcp import server

        def failing_handler(**_params):
            raise TypeError("internal bug while formatting result")

        bad_args = server.ncs_execute_tool("ncs_search", {"unknown_param": "x"})
        with patch.dict(server.NCS_EXECUTABLE_TOOL_HANDLERS, {"ncs_search": failing_handler}):
            internal_bug = server.ncs_execute_tool("ncs_search", {"query": "HR planning"})

        self.assertFalse(bad_args["ok"])
        self.assertEqual(bad_args["error"]["code"], "invalid_tool_parameters")
        self.assertEqual(bad_args["error"]["category"], "validation")
        self.assertIn("unexpected keyword argument", bad_args["error"]["message"])
        self.assertFalse(internal_bug["ok"])
        self.assertEqual(internal_bug["error"]["code"], "tool_execution_failed")
        self.assertEqual(internal_bug["error"]["category"], "execution")

    def test_ncs_execute_tool_forces_recommendation_save_false(self) -> None:
        from ncs_mcp import server

        captured: dict[str, object] = {}
        plan_captured: dict[str, object] = {}
        explicit_captured: dict[str, object] = {}
        route_captured: dict[str, object] = {}

        def recommendation_handler(**params):
            captured.update(params)
            return {"ok": True, "data": {"called": True}}

        def explicit_handler(**params):
            explicit_captured.update(params)
            return {"ok": True, "data": {"called": True}}

        def route_handler(**params):
            route_captured.update(params)
            return {"ok": True, "data": {"called": True}}

        def plan_handler(**params):
            plan_captured.update(params)
            return {"ok": True, "view": "ncs_education_plan"}

        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"recommend_training_transition": recommendation_handler},
        ):
            result = server.ncs_execute_tool(
                "recommend_training_transition",
                {"current_query": "General affairs", "target_query": "HR planning", "save": True},
            )

        self.assertTrue(result["ok"])
        self.assertIs(captured["save"], False)
        self.assertIs(captured["compact"], True)
        self.assertTrue(result["meta_execution"]["save_forced_false"])
        self.assertTrue(result["meta_execution"]["compact_defaulted"])

        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"plan_ncs_education_path": plan_handler},
        ):
            plan_result = server.ncs_execute_tool(
                "plan_ncs_education_path",
                {"current_query": "General affairs", "target_query": "HR planning", "save": True},
            )

        self.assertTrue(plan_result["ok"])
        self.assertIs(plan_captured["save"], False)
        self.assertTrue(plan_result["meta_execution"]["save_forced_false"])
        self.assertFalse(plan_result["meta_execution"]["compact_defaulted"])

        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"recommend_training_transition": explicit_handler},
        ):
            explicit_result = server.ncs_execute_tool(
                "recommend_training_transition",
                {
                    "current_query": "General affairs",
                    "target_query": "HR planning",
                    "save": True,
                    "compact": False,
                },
            )

        self.assertTrue(explicit_result["ok"])
        self.assertIs(explicit_captured["save"], False)
        self.assertIs(explicit_captured["compact"], False)
        self.assertFalse(explicit_result["meta_execution"]["compact_defaulted"])

        with patch.dict(
            server.NCS_EXECUTABLE_TOOL_HANDLERS,
            {"recommend_training_transition": route_handler},
        ):
            route_result = server.ncs_execute_tool(
                "recommend_training_transition",
                {"_route_query": "\ucd1d\ubb34\uc5d0\uc11c \uc778\uc0ac\uae30\ud68d\uc73c\ub85c \uc9c1\ubb34\uc804\ud658"},
            )

        self.assertTrue(route_result["ok"])
        self.assertEqual(route_captured["current_query"], "\ucd1d\ubb34")
        self.assertEqual(route_captured["target_query"], "\uc778\uc0ac\uae30\ud68d")
        self.assertIs(route_captured["save"], False)
        self.assertIs(route_captured["compact"], True)
        self.assertEqual(route_result["meta_execution"]["route_contract_schema"], "ncs_query_route_v1")
        self.assertIn("route_fingerprint", route_result["meta_execution"]["query_route"])

    def test_plan_ncs_education_path_facade_returns_query_route_contract(self) -> None:
        from ncs_mcp import server

        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        with (
            patch("ncs_mcp.server.open_db", return_value=DummyDb()) as open_db_mock,
            patch(
                "ncs_mcp.server.training_recommend_transition",
                return_value={"ok": True},
            ) as recommend_mock,
            patch(
                "ncs_mcp.server.training_compact_transition_response",
                return_value={"ok": True, "view": "compact_training_transition"},
            ),
            patch(
                "ncs_mcp.server.training_compact_education_plan_response",
                return_value={"ok": True, "view": "ncs_education_plan"},
            ),
        ):
            result = server.plan_ncs_education_path(
                current_query="\ub178\ubb34\uad00\ub9ac",
                target_query="\uc778\uc0ac\uae30\ud68d",
                save=True,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["query_route"]["schema"], "ncs_query_route_v1")
        self.assertEqual(result["query_route"]["tool"], "plan_ncs_education_path")
        self.assertEqual(result["route_contract_schema"], "ncs_query_route_v1")
        self.assertEqual(
            result["query_route"]["route_contract"]["guide_prompt_template"]["id"],
            "education_system_from_transition",
        )
        self.assertEqual(
            result["query_route"]["guide_reference"]["reference_role"],
            "framework_reference",
        )
        self.assertEqual(result["missing_query_route_fields"], [])

    def test_plan_ncs_education_path_facade_fails_when_route_contract_missing(self) -> None:
        from ncs_mcp import server

        class DummyDb:
            def __enter__(self):
                return object()

            def __exit__(self, exc_type, exc, traceback):
                return False

        with (
            patch("ncs_mcp.server.open_db", return_value=DummyDb()) as open_db_mock,
            patch(
                "ncs_mcp.server.training_recommend_transition",
                return_value={"ok": True},
            ) as recommend_mock,
            patch(
                "ncs_mcp.server.training_compact_transition_response",
                return_value={"ok": True, "view": "compact_training_transition"},
            ),
            patch(
                "ncs_mcp.server.training_compact_education_plan_response",
                return_value={
                    "ok": True,
                    "view": "ncs_education_plan",
                    "training_system_matrix": [{"course": "partial"}],
                    "recommended_path": {"core_gap_training": ["partial"]},
                    "source_payload": {"secret": "hidden"},
                },
            ),
            patch(
                "ncs_mcp.server.aihr_plan_route_evidence",
                return_value={"tool": "plan_ncs_education_path"},
            ),
        ):
            result = server.plan_ncs_education_path(
                current_query="labor management",
                target_query="HR planning",
                save=True,
            )

        open_db_mock.assert_not_called()
        recommend_mock.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["route_contract_schema"])
        self.assertEqual(result["error"]["code"], "missing_query_route_contract")
        self.assertIn("query_route.schema", result["missing_query_route_fields"])
        self.assertIn("query_route.scenario:None", result["missing_query_route_fields"])
        self.assertIn("query_route.available:None", result["missing_query_route_fields"])
        self.assertIn("query_route.route_contract", result["missing_query_route_fields"])
        self.assertNotIn("view", result)
        self.assertNotIn("training_system_matrix", result)
        self.assertNotIn("recommended_path", result)
        self.assertNotIn("source_payload", result)
        self.assertNotIn("view", result["data"])
        self.assertNotIn("training_system_matrix", result["data"])
        self.assertNotIn("recommended_path", result["data"])
        self.assertNotIn("source_payload", result["data"])

    def test_ncs_http_health_route_and_transport_configuration(self) -> None:
        from starlette.testclient import TestClient
        from ncs_mcp import server

        original_host = server.mcp.settings.host
        original_port = server.mcp.settings.port
        original_stateless = server.mcp.settings.stateless_http
        original_json = server.mcp.settings.json_response
        original_transport = server.CURRENT_TRANSPORT
        previous_key = os.environ.get("NCS_SERVICE_KEY")
        previous_db = os.environ.get("NCS_DB_PATH")
        secret = "health-secret-123456"
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            conn = connect(db_path)
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            conn.execute(
                """
                INSERT INTO ncs_training_courses(
                    ncs_cl_cd, compe_unit_name, train_goal, train_time, api_fetched_at
                ) VALUES (?, 'HR planning', 'Build HR planning capability.', '24', ?)
                """,
                (fixture["unit_code"], now_utc()),
            )
            conn.commit()
            conn.close()

            os.environ["NCS_SERVICE_KEY"] = secret
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                server.configure_transport(
                    transport="streamable-http",
                    host="127.0.0.1",
                    port=8766,
                )
                self.assertEqual(server.mcp.settings.host, "127.0.0.1")
                self.assertEqual(server.mcp.settings.port, 8766)
                self.assertTrue(server.mcp.settings.stateless_http)
                self.assertTrue(server.mcp.settings.json_response)

                with TestClient(server.mcp.streamable_http_app()) as client:
                    response = client.get("/health")
                    ready_response = client.get("/ready")
                payload = response.json()
                ready_payload = ready_response.json()
            finally:
                server.mcp.settings.host = original_host
                server.mcp.settings.port = original_port
                server.mcp.settings.stateless_http = original_stateless
                server.mcp.settings.json_response = original_json
                server.CURRENT_TRANSPORT = original_transport
                if previous_key is None:
                    os.environ.pop("NCS_SERVICE_KEY", None)
                else:
                    os.environ["NCS_SERVICE_KEY"] = previous_key
                if previous_db is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous_db

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ready_response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(ready_payload["status"], "ready")
        self.assertEqual(payload["name"], "ncs-mcp")
        self.assertEqual(payload["transport"], "streamable-http")
        self.assertEqual(payload["endpoint"], "/mcp")
        self.assertEqual(payload["tools"]["exposed"], 11)
        self.assertFalse(payload["runtime"]["operator_tools_enabled"])
        self.assertEqual(payload["tools"]["legacy_present"], 0)
        self.assertTrue(payload["runtime"]["database"]["configured"])
        self.assertTrue(payload["runtime"]["database"]["ready"])
        self.assertTrue(payload["runtime"]["database"]["openable"])
        self.assertTrue(payload["runtime"]["api_keys"]["service_key_present"])
        self.assertNotIn(secret, json.dumps(payload, ensure_ascii=False))

    def test_ncs_sse_health_route_reports_sse_endpoint(self) -> None:
        from starlette.testclient import TestClient
        from ncs_mcp import server

        original_host = server.mcp.settings.host
        original_port = server.mcp.settings.port
        original_transport = server.CURRENT_TRANSPORT
        previous_db = os.environ.get("NCS_DB_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                server.configure_transport(transport="sse", host="127.0.0.1", port=8777)
                with TestClient(server.mcp.sse_app()) as client:
                    response = client.get("/health")
                payload = response.json()
            finally:
                server.mcp.settings.host = original_host
                server.mcp.settings.port = original_port
                server.CURRENT_TRANSPORT = original_transport
                if previous_db is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous_db

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["transport"], "sse")
        self.assertEqual(payload["endpoint"], "/sse")

    def test_operator_tools_are_exposed_when_enabled_before_import(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "1"
        script = (
            "import json; "
            "from ncs_mcp import server; "
            "surface=server.current_mcp_tool_surface(); "
            "print(json.dumps({"
            "'operator_enabled': surface['operator_tools_enabled'], "
            "'operator_count': len(surface['operator_tools']), "
            "'exposed_count': len(surface['all_tools']), "
            "'legacy_count': len(surface['legacy_tools_present']), "
            "'unexpected_count': len(surface['unexpected_tools'])"
            "}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)

        self.assertTrue(payload["operator_enabled"])
        self.assertEqual(payload["operator_count"], 5)
        # 7 core user tools + 5 operator tools; advanced tools stay hidden.
        self.assertEqual(payload["exposed_count"], 12)
        self.assertEqual(payload["legacy_count"], 0)
        self.assertEqual(payload["unexpected_count"], 0)

    def test_advanced_tools_are_exposed_when_enabled_before_import(self) -> None:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        env["NCS_MCP_ENABLE_ADVANCED_TOOLS"] = "1"
        env.pop("NCS_MCP_ENABLE_OPERATOR_TOOLS", None)
        script = (
            "import json; "
            "from ncs_mcp import server; "
            "surface=server.current_mcp_tool_surface(); "
            "print(json.dumps({"
            "'advanced_enabled': surface['advanced_tools_enabled'], "
            "'hidden_advanced_count': len(surface['hidden_advanced_tools']), "
            "'exposed_count': len(surface['all_tools']), "
            "'all_tools': surface['all_tools'], "
            "'unexpected_count': len(surface['unexpected_tools'])"
            "}))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        payload = json.loads(completed.stdout)

        self.assertTrue(payload["advanced_enabled"])
        self.assertEqual(payload["hidden_advanced_count"], 0)
        # 7 core user tools + 4 advanced tools; operator tools stay hidden.
        self.assertEqual(payload["exposed_count"], 11)
        self.assertEqual(payload["unexpected_count"], 0)
        for name in (
            "plan_ncs_education_path",
            "recommend_training_transition",
            "recommend_task_transitions",
            "get_concept_evidence",
        ):
            self.assertIn(name, payload["all_tools"])

    def test_ncs_ready_route_fails_when_database_missing(self) -> None:
        from ncs_mcp import server

        previous_db = os.environ.get("NCS_DB_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["NCS_DB_PATH"] = str(Path(tmp) / "missing.db")
            try:
                health_response = asyncio.run(server.health_check(None))
                ready_response = asyncio.run(server.readiness_check(None))
                health_payload = json.loads(health_response.body)
                ready_payload = json.loads(ready_response.body)
            finally:
                if previous_db is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous_db

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_payload["status"], "degraded")
        self.assertFalse(health_payload["runtime"]["database"]["ready"])
        self.assertEqual(ready_response.status_code, 503)
        self.assertEqual(ready_payload["status"], "not_ready")
        self.assertEqual(ready_payload["runtime"]["database"]["error"]["code"], "database_missing")

    def test_ncs_ready_route_fails_when_core_table_is_empty(self) -> None:
        from ncs_mcp import server

        previous_db = os.environ.get("NCS_DB_PATH")
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "ncs.db"
            create_ready_smoke_db(db_path)
            conn = connect(db_path)
            conn.execute("DELETE FROM ncs_training_courses")
            conn.commit()
            conn.close()
            os.environ["NCS_DB_PATH"] = str(db_path)
            try:
                health_response = asyncio.run(server.health_check(None))
                ready_response = asyncio.run(server.readiness_check(None))
                health_payload = json.loads(health_response.body)
                ready_payload = json.loads(ready_response.body)
            finally:
                if previous_db is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous_db

        self.assertEqual(health_response.status_code, 200)
        self.assertEqual(health_payload["status"], "degraded")
        self.assertFalse(health_payload["runtime"]["database"]["ready"])
        self.assertEqual(
            health_payload["runtime"]["database"]["core_tables"]["ncs_training_courses"]["row_count"],
            0,
        )
        self.assertEqual(ready_response.status_code, 503)
        self.assertEqual(ready_payload["status"], "not_ready")
        self.assertEqual(ready_payload["runtime"]["database"]["error"]["code"], "database_not_ready")

    def test_labor_management_query_prefers_hr_classification_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('05', 'Safety', '02', 'Fire', '01', 'Fire', '02', 'Fire construction')
                """
            )
            fire_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '05'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0502010202_14v1', '0502010202', '14v1', '시공관리',
                          '5', ?, 'matched', ?, ?)
                """,
                (fire_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0502010202_14v1', '1', '0502010202_14v1 1', '노무관리하기', '5')
                """
            )
            fire_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '0502010202_14v1'"
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', '작업시기별로 인력을 투입할 수 있다.')
                """,
                (fire_element_id,),
            )
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '02', '노무관리')
                """
            )
            hr_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE major_code = '02'"
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020201_19v2', '0202020201', '19v2', '노사관계 계획',
                          '5', ?, 'matched', ?, ?)
                """,
                (hr_classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020201_19v2', '1', '0202020201_19v2 1', '목표 설정하기', '5')
                """
            )
            hr_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '0202020201_19v2'"
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', '노사관계 관련 내외부 자료를 수집할 수 있다.')
                """,
                (hr_element_id,),
            )

            source = resolve_task_criteria(conn, query="노무관리")
            resolution = resolve_ncs_query_scope(conn, "노무관리", limit=3)
            conn.close()

            self.assertIsNotNone(source)
            self.assertEqual(source["major_code"], "02")
            self.assertEqual(source["sub_name"], "노무관리")
            self.assertEqual(source["unit_name_raw"], "노사관계 계획")
            self.assertTrue(resolution["ok"])
            self.assertEqual(resolution["candidates"][0]["candidate_type"], "classification")
            self.assertEqual(resolution["candidates"][0]["match_level"], "sub_classification")
            self.assertEqual(resolution["candidates"][0]["sub_name"], "노무관리")


    def test_hr_team_lead_alias_resolves_to_hr_subclassification_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', '경영·회계·사무', '02', '총무·인사', '02', '인사·조직', '01', '인사')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020101_23v3', '0202020101', '23v3', '인사기획',
                          '6', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', '인사전략 수립하기', '6')
                """
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', '인사전략 환경을 분석할 수 있다.')
                """,
                (element_id,),
            )

            resolution = resolve_ncs_query_scope(conn, "인사팀장", limit=5)
            conn.close()

        self.assertTrue(resolution["ok"])
        self.assertEqual(resolution["query_alias"]["normalized_query"], "인사")
        self.assertIsNone(resolution["query_alias"]["unit_code"])
        self.assertEqual(resolution["candidates"][0]["candidate_type"], "classification")
        self.assertEqual(resolution["candidates"][0]["match_level"], "sub_classification")
        self.assertEqual(resolution["candidates"][0]["sub_name"], "인사")

    def test_generic_job_suffix_query_resolves_to_subclassification_scope(self) -> None:
        hr = "\uc778\uc0ac"
        hr_work = "\uc778\uc0ac\uc5c5\ubb34"
        hr_planning = "\uc778\uc0ac\uae30\ud68d"
        hr_strategy_element = "\uc778\uc0ac\uc804\ub7b5 \uc218\ub9bd\ud558\uae30"
        short_work = "A\uc5c5\ubb34"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            conn.execute("DELETE FROM ncs_query_aliases")
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', ?)
                """,
                (hr,),
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020101_23v3', '0202020101', '23v3', ?,
                          '6', ?, 'matched', ?, ?)
                """,
                (hr_planning, classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020101_23v3', '1', '0202020101_23v3 1', ?, '6')
                """,
                (hr_strategy_element,),
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', ?)
                """,
                (element_id, "\uc778\uc0ac\uc804\ub7b5 \ud658\uacbd\uc744 \ubd84\uc11d\ud560 \uc218 \uc788\ub2e4."),
            )

            aliased_query, *_rest, alias = _apply_query_alias(conn, hr_work)
            guarded_query, *_guarded_rest, guarded_alias = _apply_query_alias(conn, short_work)
            source = resolve_task_criteria(conn, query=aliased_query)
            resolution = resolve_ncs_query_scope(conn, hr_work, limit=5)
            conn.close()

        self.assertEqual(aliased_query, hr)
        self.assertIsNone(alias)
        self.assertEqual(guarded_query, short_work)
        self.assertIsNone(guarded_alias)
        self.assertIsNotNone(source)
        self.assertEqual(source["sub_name"], hr)
        self.assertTrue(resolution["ok"])
        self.assertEqual(resolution["effective_query"], hr)
        self.assertIsNone(resolution["query_alias"])
        self.assertEqual(resolution["candidates"][0]["candidate_type"], "classification")
        self.assertEqual(resolution["candidates"][0]["match_level"], "sub_classification")
        self.assertEqual(resolution["candidates"][0]["sub_name"], hr)

    def test_generic_job_suffix_alias_lookup_uses_stripped_variant_after_exact_miss(self) -> None:
        accounting = "\ud68c\uacc4"
        accounting_work = "\ud68c\uacc4\uc5c5\ubb34"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            conn.execute("DELETE FROM ncs_query_aliases")
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                    unit_code, confidence_score, source_method, review_status, created_at, updated_at
                ) VALUES (?, ?, '02', '03', '02', '01', NULL, 0.94, 'test', 'human_reviewed', ?, ?)
                """,
                (accounting, accounting, timestamp, timestamp),
            )

            resolved_query, major_code, middle_code, small_code, sub_code, alias = _apply_query_alias(
                conn,
                accounting_work,
            )
            conn.close()

        self.assertEqual(resolved_query, accounting)
        self.assertEqual((major_code, middle_code, small_code, sub_code), ("02", "03", "02", "01"))
        self.assertIsNotNone(alias)
        self.assertEqual(alias["alias_text"], accounting)
        self.assertEqual(alias["query_normalization"]["method"], "generic_suffix_strip")

    def test_exact_classification_resolution_supplies_execution_filters(self) -> None:
        accounting = "\ud68c\uacc4"
        accounting_work = "\ud68c\uacc4\uc5c5\ubb34"
        customer_management = "\uace0\uac1d\uad00\ub9ac"
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            conn.execute("DELETE FROM ncs_query_aliases")
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES
                    ('02', 'Business', '01', 'Management', '01', 'Strategy', '01', 'Strategy planning'),
                    ('02', 'Business', '03', 'Finance/accounting', '02', ?, '01', 'Accounting audit')
                """,
                (accounting,),
            )
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '01', 'Management', '03', ?, '02', ?)
                """,
                (customer_management, customer_management),
            )
            collision_classification_id = conn.execute(
                "SELECT classification_id FROM classifications WHERE small_name = ?",
                (customer_management,),
            ).fetchone()["classification_id"]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0201030406_21v2', '0201030406', '21v2', ?, '4', ?, 'matched', ?, ?)
                """,
                (customer_management, collision_classification_id, now_utc(), now_utc()),
            )

            resolution = resolve_ncs_query_scope(conn, accounting_work, major_code="02", limit=5)
            stripped_resolution = resolve_ncs_query_scope(conn, accounting, major_code="02", limit=5)
            partial_resolution = resolve_ncs_query_scope(conn, "\uad00\ub9ac", major_code="02", limit=5)
            collision_resolution = resolve_ncs_query_scope(conn, customer_management, major_code="02", limit=5)
            conn.close()

        filters = _resolution_classification_filters(resolution, major_code="02")
        stripped_filters = _resolution_classification_filters(stripped_resolution, major_code="02")
        normalized_query, normalization = _generic_job_query_normalization(accounting_work)
        reattached_filters = _resolution_classification_filters(
            _attach_query_normalization(stripped_resolution, normalization, normalized_query),
            major_code="02",
        )
        partial_filters = _resolution_classification_filters(partial_resolution, major_code="02")
        collision_filters = _resolution_classification_filters(collision_resolution, major_code="02")
        self.assertTrue(resolution["ok"])
        self.assertEqual(resolution["effective_query"], accounting)
        self.assertEqual(resolution["query_normalization"]["method"], "generic_suffix_strip")
        self.assertEqual(resolution["candidates"][0]["match_level"], "small_classification")
        self.assertEqual(filters["major_code"], "02")
        self.assertEqual(filters["middle_code"], "03")
        self.assertEqual(filters["small_code"], "02")
        self.assertIsNone(filters["sub_code"])
        self.assertIsNone(stripped_filters["middle_code"])
        self.assertEqual(reattached_filters["middle_code"], "03")
        self.assertEqual(reattached_filters["small_code"], "02")
        self.assertEqual(partial_filters["major_code"], "02")
        self.assertIsNone(partial_filters["middle_code"])
        self.assertIsNone(partial_filters["small_code"])
        self.assertTrue(collision_resolution["ok"])
        self.assertIsNone(collision_resolution["query_normalization"])
        self.assertEqual(collision_resolution["candidates"][0]["match_level"], "competency_unit")
        self.assertEqual(collision_filters["major_code"], "02")
        self.assertIsNone(collision_filters["middle_code"])
        self.assertIsNone(collision_filters["small_code"])

    def test_hr_planning_to_hr_team_lead_uses_scope_overlay_transferability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                UPDATE classifications
                SET major_name = '경영·회계·사무',
                    middle_name = '총무·인사',
                    small_name = '인사·조직',
                    sub_name = '인사'
                WHERE classification_id = ?
                """,
                (classification_id,),
            )
            conn.execute(
                """
                UPDATE competency_units
                SET unit_name_raw = '인사기획', unit_level_raw = '6'
                WHERE unit_code = ?
                """,
                (fixture["unit_code"],),
            )
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020103_23v4', '0202020103', '23v4', '인력채용',
                          '5', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020103_23v4', '1', '0202020103_23v4 1', '채용계획 수립하기', '5')
                """
            )
            recruiting_element_id = conn.execute(
                "SELECT element_id FROM competency_elements WHERE unit_code = '0202020103_23v4'"
            ).fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', '조직의 인력수요에 따라 채용계획을 수립할 수 있다.')
                """,
                (recruiting_element_id,),
            )
            recruiting_criteria_id = conn.execute(
                "SELECT criteria_id FROM performance_criteria WHERE element_id = ?",
                (recruiting_element_id,),
            ).fetchone()["criteria_id"]
            conn.execute(
                """
                INSERT INTO ontology_concepts(
                    concept_name, normalized_key, concept_type,
                    definition_status, relation_status, review_status,
                    created_at, updated_at
                ) VALUES ('recruitment planning', 'recruitmentplanning', 'skill',
                          'missing', 'unlinked', 'raw', ?, ?)
                """,
                (timestamp, timestamp),
            )
            recruiting_concept_id = conn.execute(
                "SELECT concept_id FROM ontology_concepts WHERE normalized_key = 'recruitmentplanning'"
            ).fetchone()["concept_id"]
            for index, (text, concept_id) in enumerate(
                [
                    ("workforce analysis", fixture["skill_concept_id"]),
                    ("recruitment planning", recruiting_concept_id),
                ],
                start=1,
            ):
                conn.execute(
                    """
                    INSERT INTO ksa_items(element_id, ksa_type_code, ksa_type_name, ksa_no, ksa_text_raw)
                    VALUES (?, '02', 'skill', ?, ?)
                    """,
                    (recruiting_element_id, str(index), text),
                )
                ksa_id = conn.execute(
                    """
                    SELECT ksa_id
                    FROM ksa_items
                    WHERE element_id = ? AND ksa_no = ?
                    """,
                    (recruiting_element_id, str(index)),
                ).fetchone()["ksa_id"]
                conn.execute(
                    """
                    INSERT INTO raw_excel_rows(
                        source_file, sheet_name, sheet_row_number,
                        major_code, major_name, middle_code, middle_name,
                        small_code, small_name, sub_code, sub_name,
                        unit_code, unit_name, unit_level,
                        element_code, element_name, element_level,
                        criteria_no, criteria_text,
                        ksa_type_code, ksa_type_name, ksa_no, ksa_text,
                        loaded_at
                    ) VALUES (
                        'test.xlsx', 'Sheet1', ?,
                        '02', 'Business', '02', 'HR',
                        '02', 'HRM', '01', '인사',
                        '0202020103_23v4', '인력채용', '5',
                        '0202020103_23v4 1', '채용계획 수립하기', '5',
                        '1', '조직의 인력수요에 따라 채용계획을 수립할 수 있다.',
                        '02', 'skill', ?, ?,
                        ?
                    )
                    """,
                    (100 + index, str(index), text, timestamp),
                )
                raw_row_id = conn.execute(
                    """
                    SELECT raw_row_id
                    FROM raw_excel_rows
                    WHERE sheet_row_number = ?
                    """,
                    (100 + index,),
                ).fetchone()["raw_row_id"]
                conn.execute(
                    """
                    INSERT INTO element_criteria_ksa_links(raw_row_id, element_id, criteria_id, ksa_id)
                    VALUES (?, ?, ?, ?)
                    """,
                    (raw_row_id, recruiting_element_id, recruiting_criteria_id, ksa_id),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_concept_links(ksa_id, concept_id, link_status, created_at)
                    VALUES (?, ?, 'raw', ?)
                    """,
                    (ksa_id, concept_id, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_items(
                        ksa_id, element_id, ksa_type_name, atom_index, atom_text,
                        normalized_key, split_method, review_status, created_at
                    ) VALUES (?, ?, 'skill', 0, ?, ?, 'test', 'raw', ?)
                    """,
                    (ksa_id, recruiting_element_id, text, text.replace(" ", "").lower(), timestamp),
                )
                atomic_id = conn.execute(
                    "SELECT atomic_id FROM ksa_atomic_items WHERE ksa_id = ?",
                    (ksa_id,),
                ).fetchone()["atomic_id"]
                conn.execute(
                    """
                    INSERT INTO ksa_atomic_concept_links(atomic_id, concept_id, link_status, created_at)
                    VALUES (?, ?, 'raw', ?)
                    """,
                    (atomic_id, concept_id, timestamp),
                )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "인사",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "인사기획",
                        "compe_unit_level": "6",
                        "train_goal": "Learn workforce planning and workforce analysis practice.",
                        "train_time": "24",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    },
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "인사",
                        "ncs_cl_cd": "0202020103_23v4",
                        "compe_unit_name": "인력채용",
                        "compe_unit_level": "5",
                        "train_goal": "Learn recruitment planning.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    },
                ],
            )
            build_training_course_ontology_links(conn)

            result = recommend_training_transition(
                conn,
                current_query="인사기획",
                target_query="인사팀장",
                major_code="02",
                limit=3,
                save=False,
            )
            compact = compact_training_transition_response(result, recommendation_limit=3)
            conn.close()

        self.assertTrue(result["ok"], result)
        summary = result["transition"]["summary"]
        self.assertEqual(result["transition"]["target_scope"]["match_level"], "sub_classification")
        self.assertEqual(summary["target_role_overlay"]["code"], "hr_team_lead")
        self.assertTrue(summary["current_scope_subset_of_target"])
        self.assertEqual(summary["ncs_scope_relation"], "same_sub_classification")
        self.assertGreater(
            summary["ontology_adjusted_transferability_ratio"],
            summary["exact_ksa_overlap_ratio"],
        )
        self.assertGreaterEqual(summary["ontology_adjusted_transferability_ratio"], 0.7)
        self.assertEqual(
            compact["answer_summary"]["transition_assessment"]["transferability_ratio"],
            summary["ontology_adjusted_transferability_ratio"],
        )
        self.assertIn("target_role_overlay", compact["answer_summary"]["transition_assessment"])

    def test_service_management_alias_resolves_to_hr_attendance_unit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020109_23v5', '0202020109', '23v5', 'Payroll',
                          '3', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020109_23v5', '1', '0202020109_23v5 1', ?, '3')
                """,
                ("\uadfc\ud0dc\uad00\ub9ac \ud558\uae30",),
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', ?)
                """,
                (element_id, "\uadfc\ud0dc\uc790\ub8cc\ub97c \uc9d1\uacc4\ud560 \uc218 \uc788\ub2e4."),
            )

            resolution = resolve_ncs_query_scope(conn, "\ubcf5\ubb34\uad00\ub9ac", limit=3)
            source = resolve_task_criteria(
                conn,
                query="\uadfc\ud0dc\uad00\ub9ac",
                unit_code="0202020109_23v5",
            )
            conn.close()

        self.assertTrue(resolution["ok"])
        self.assertEqual(resolution["query_alias"]["unit_code"], "0202020109_23v5")
        self.assertEqual(resolution["candidates"][0]["unit_code"], "0202020109_23v5")
        self.assertEqual(resolution["candidates"][0]["match_level"], "query_alias_unit")
        self.assertIsNotNone(source)
        self.assertEqual(source["unit_code"], "0202020109_23v5")

    def test_transition_ignores_stale_candidate_alias_unit_when_exact_unit_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            units = [
                ("0202020102_23v3", "0202020102", "\uc9c1\ubb34\uad00\ub9ac", "\uc9c1\ubb34\ubd84\uc11d \ud558\uae30", "\uc9c1\ubb34 \uc815\ubcf4\ub97c \uc815\ub9ac\ud560 \uc218 \uc788\ub2e4."),
                ("0202020103_23v4", "0202020103", "\uc778\ub825\ucc44\uc6a9", "\ucc44\uc6a9\uacc4\ud68d \uc218\ub9bd\ud558\uae30", "\uc778\ub825 \uc218\uc694\uc5d0 \ub530\ub77c \ucc44\uc6a9\uacc4\ud68d\uc744 \uc218\ub9bd\ud560 \uc218 \uc788\ub2e4."),
            ]
            for unit_code, base_code, unit_name, element_name, criteria_text in units:
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, api_match_status,
                        created_at, updated_at
                    ) VALUES (?, ?, '23v1', ?, '5', ?, 'matched', ?, ?)
                    """,
                    (unit_code, base_code, unit_name, classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES (?, '1', ?, ?, '5')
                    """,
                    (unit_code, f"{unit_code} 1", element_name),
                )
                element_id = conn.execute(
                    "SELECT element_id FROM competency_elements WHERE unit_code = ?",
                    (unit_code,),
                ).fetchone()["element_id"]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', ?)
                    """,
                    (element_id, criteria_text),
                )
            conn.execute(
                "DELETE FROM ncs_query_aliases WHERE alias_text = ? OR normalized_query = ?",
                ("\uc778\ub825\ucc44\uc6a9", "\uc778\ub825\ucc44\uc6a9"),
            )
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                    unit_code, confidence_score, source_method, review_status, created_at, updated_at
                ) VALUES (?, ?, '02', '02', '02', '01', '0202020102_23v3',
                          1.0, 'test_stale_alias', 'candidate', ?, ?)
                """,
                ("\uc778\ub825\ucc44\uc6a9", "\uc9c1\ubb34\uad00\ub9ac", timestamp, timestamp),
            )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR planning",
                        "ncs_cl_cd": fixture["unit_code"],
                        "compe_unit_name": "HR planning",
                        "compe_unit_level": "5",
                        "train_goal": "Learn workforce planning practice.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)

            result = recommend_training_transition(
                conn,
                current_query="\uc778\ub825\ucc44\uc6a9",
                target_query="HR planning",
                major_code="02",
                limit=1,
                save=False,
            )
            conn.close()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["transition"]["current_task"]["unit_code"], "0202020103_23v4")
        self.assertEqual(result["transition"]["current_scope"]["match_text"], "\uc778\ub825\ucc44\uc6a9")
        alias = result["transition"]["current_query_alias"]
        self.assertEqual(alias["ignored_unit_code"], "0202020102_23v3")
        self.assertEqual(alias["ignore_guard"]["exact_unit_code"], "0202020103_23v4")

    def test_task_recommendation_ignores_stale_candidate_alias_unit_when_exact_unit_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            units = [
                ("0202020102_23v3", "0202020102", "\uc9c1\ubb34\uad00\ub9ac", "\uc9c1\ubb34\ubd84\uc11d \ud558\uae30", "\uc9c1\ubb34 \uc815\ubcf4\ub97c \uc815\ub9ac\ud560 \uc218 \uc788\ub2e4."),
                ("0202020103_23v4", "0202020103", "\uc778\ub825\ucc44\uc6a9", "\ucc44\uc6a9\uacc4\ud68d \uc218\ub9bd\ud558\uae30", "\uc778\ub825 \uc218\uc694\uc5d0 \ub530\ub77c \ucc44\uc6a9\uacc4\ud68d\uc744 \uc218\ub9bd\ud560 \uc218 \uc788\ub2e4."),
            ]
            for unit_code, base_code, unit_name, element_name, criteria_text in units:
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, api_match_status,
                        created_at, updated_at
                    ) VALUES (?, ?, '23v1', ?, '5', ?, 'matched', ?, ?)
                    """,
                    (unit_code, base_code, unit_name, classification_id, timestamp, timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO competency_elements(
                        unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                    ) VALUES (?, '1', ?, ?, '5')
                    """,
                    (unit_code, f"{unit_code} 1", element_name),
                )
                element_id = conn.execute(
                    "SELECT element_id FROM competency_elements WHERE unit_code = ?",
                    (unit_code,),
                ).fetchone()["element_id"]
                conn.execute(
                    """
                    INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                    VALUES (?, '1', ?)
                    """,
                    (element_id, criteria_text),
                )
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                    unit_code, confidence_score, source_method, review_status, created_at, updated_at
                ) VALUES (?, ?, '02', '02', '02', '01', '0202020102_23v3',
                          1.0, 'test_stale_alias', 'candidate', ?, ?)
                """,
                ("\uc778\ub825\ucc44\uc6a9", "\uc9c1\ubb34\uad00\ub9ac", timestamp, timestamp),
            )
            upsert_training_courses(
                conn,
                [
                    {
                        "ncs_lclas_cd": "02",
                        "ncs_lclas_cdnm": "Business",
                        "ncs_mclas_cd": "02",
                        "ncs_mclas_cdnm": "HR",
                        "ncs_sclas_cd": "02",
                        "ncs_sclas_cdnm": "HRM",
                        "ncs_subd_cd": "01",
                        "ncs_subd_cdnm": "HR",
                        "ncs_cl_cd": "0202020103_23v4",
                        "compe_unit_name": "\uc778\ub825\ucc44\uc6a9",
                        "compe_unit_level": "5",
                        "train_goal": "\ucc44\uc6a9\uacc4\ud68d\uacfc \uc778\ub825 \uc218\uc694 \ubd84\uc11d\uc744 \ud6c8\ub828\ud55c\ub2e4.",
                        "train_time": "16",
                        "fac_name": "HR center",
                        "meth_name": "Practice",
                    }
                ],
            )
            build_training_course_ontology_links(conn)

            result = recommend_training_for_task(
                conn,
                query="\uc778\ub825\ucc44\uc6a9",
                major_code="02",
                limit=1,
                save=False,
            )
            conn.close()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["source_task"]["unit_code"], "0202020103_23v4")
        alias = result["query_alias"]
        self.assertEqual(alias["ignored_unit_code"], "0202020102_23v3")
        self.assertEqual(alias["ignore_guard"]["exact_unit_code"], "0202020103_23v4")

    def test_query_alias_status_precedence_ignores_rejected_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            rows = [
                ("service mgmt", "rejected target", "0202020099_23v1", 1.0, "rejected"),
                ("service mgmt", "candidate target", "0202020108_23v1", 0.99, "candidate"),
                ("service mgmt", "reviewed target", "0202020109_23v5", 0.5, "human_reviewed"),
                ("rejected alias", "rejected only", "0202020098_23v1", 1.0, "rejected"),
            ]
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO ncs_query_aliases(
                        alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                        unit_code, confidence_score, source_method, review_status, created_at, updated_at
                    ) VALUES (?, ?, '02', '02', '02', '01', ?, ?, 'test', ?, ?, ?)
                    """,
                    (*row, timestamp, timestamp),
                )

            selected = _apply_query_alias(conn, "service mgmt")
            rejected_by_normalized = _apply_query_alias(conn, "rejected only")
            conn.close()

        self.assertEqual(selected[0], "reviewed target")
        self.assertIsNotNone(selected[5])
        self.assertEqual(selected[5]["review_status"], "human_reviewed")
        self.assertEqual(selected[5]["unit_code"], "0202020109_23v5")
        self.assertIsNone(rejected_by_normalized[5])

    def test_query_alias_exact_text_beats_normalized_synonym_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            rows = [
                ("service management", "attendance management", "0202020198_23v1", 0.99, "human_reviewed"),
                ("attendance management", "attendance management", "0202020199_23v1", 0.8, "candidate"),
            ]
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO ncs_query_aliases(
                        alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                        unit_code, confidence_score, source_method, review_status, created_at, updated_at
                    ) VALUES (?, ?, '02', '02', '02', '01', ?, ?, 'test', ?, ?, ?)
                    """,
                    (*row, timestamp, timestamp),
                )

            selected = _apply_query_alias(conn, "attendance management")
            conn.close()

        self.assertEqual(selected[5]["alias_text"], "attendance management")
        self.assertEqual(selected[5]["unit_code"], "0202020199_23v1")

    def test_alias_unit_scope_does_not_require_query_text_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            conn.execute(
                """
                INSERT INTO competency_units(
                    unit_code, base_unit_code, unit_version, unit_name_raw,
                    unit_level_raw, classification_id, api_match_status,
                    created_at, updated_at
                ) VALUES ('0202020199_23v1', '0202020199', '23v1', 'Payroll operations',
                          '3', ?, 'matched', ?, ?)
                """,
                (classification_id, timestamp, timestamp),
            )
            conn.execute(
                """
                INSERT INTO competency_elements(
                    unit_code, element_no, element_code_raw, element_name_raw, element_level_raw
                ) VALUES ('0202020199_23v1', '1', '0202020199_23v1 1', 'Time record audit', '3')
                """
            )
            element_id = conn.execute("SELECT element_id FROM competency_elements").fetchone()["element_id"]
            conn.execute(
                """
                INSERT INTO performance_criteria(element_id, criteria_no, criteria_text_raw)
                VALUES (?, '1', 'Validate monthly records.')
                """,
                (element_id,),
            )
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                    unit_code, confidence_score, source_method, review_status, created_at, updated_at
                ) VALUES (
                    'service management', 'attendance management',
                    '02', '02', '02', '01', '0202020199_23v1',
                    0.95, 'test', 'human_reviewed', ?, ?
                )
                """,
                (timestamp, timestamp),
            )

            aliased_query, major_code, middle_code, small_code, sub_code, alias = _apply_query_alias(
                conn, "service management"
            )
            source = resolve_task_criteria(
                conn,
                query=None if alias and alias.get("unit_code") else aliased_query,
                unit_code=alias.get("unit_code") if alias else None,
                major_code=major_code,
                middle_code=middle_code,
                small_code=small_code,
                sub_code=sub_code,
            )
            conn.close()

        self.assertEqual(aliased_query, "attendance management")
        self.assertIsNotNone(source)
        self.assertEqual(source["unit_code"], "0202020199_23v1")

    def test_query_alias_unit_does_not_outrank_competing_exact_unit_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            timestamp = now_utc()
            conn.execute(
                """
                INSERT INTO classifications(
                    major_code, major_name, middle_code, middle_name,
                    small_code, small_name, sub_code, sub_name
                ) VALUES ('02', 'Business', '02', 'HR', '02', 'HRM', '01', 'HR')
                """
            )
            classification_id = conn.execute("SELECT classification_id FROM classifications").fetchone()[
                "classification_id"
            ]
            for unit_code, unit_name in [
                ("0202020198_23v1", "attendance management"),
                ("0202020199_23v1", "Payroll operations"),
            ]:
                conn.execute(
                    """
                    INSERT INTO competency_units(
                        unit_code, base_unit_code, unit_version, unit_name_raw,
                        unit_level_raw, classification_id, api_match_status,
                        created_at, updated_at
                    ) VALUES (?, ?, '23v1', ?, '3', ?, 'matched', ?, ?)
                    """,
                    (unit_code, unit_code.split("_", 1)[0], unit_name, classification_id, timestamp, timestamp),
                )
            conn.execute(
                """
                INSERT INTO ncs_query_aliases(
                    alias_text, normalized_query, major_code, middle_code, small_code, sub_code,
                    unit_code, confidence_score, source_method, review_status, created_at, updated_at
                ) VALUES (
                    'service management', 'attendance management',
                    '02', '02', '02', '01', '0202020199_23v1',
                    0.86, 'test', 'candidate', ?, ?
                )
                """,
                (timestamp, timestamp),
            )

            resolution = resolve_ncs_query_scope(conn, "service management", limit=3)
            conn.close()

        self.assertEqual(resolution["candidates"][0]["unit_code"], "0202020198_23v1")
        self.assertEqual(resolution["candidates"][0]["match_level"], "competency_unit")
        self.assertEqual(resolution["candidates"][1]["unit_code"], "0202020199_23v1")
        self.assertEqual(resolution["candidates"][1]["match_level"], "query_alias_unit")


if __name__ == "__main__":
    unittest.main()
