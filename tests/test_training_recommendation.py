from __future__ import annotations

import asyncio
import os
import json
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

from ncs_mcp.db import (
    build_ksa_meaning_candidates,
    connect,
    initialize_database,
    now_utc,
    prepare_ontology_human_review_queue,
    recommend_task_transitions as recommend_task_transitions_from_db,
    resolve_task_criteria,
)
from ncs_mcp.career_path import career_path_summary, import_career_paths_csv
from ncs_mcp.smoke_data import create_ready_smoke_db
from ncs_mcp.training_course_api import parse_training_course_xml, upsert_training_courses
from ncs_mcp.training_recommendation import (
    TRUSTED_TRANSITION_REVIEW_STATUSES,
    _apply_query_alias,
    _candidate_allows_edit_distance,
    _candidate_score,
    _diversify_top_k_candidates,
    _preference_fit_profile,
    _preference_time_adjustment,
    _recommendation_tier,
    build_training_course_ontology_links,
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
        "criteria_id": criteria_id,
        "concept_id": concept_id,
        "skill_concept_id": skill_concept_id,
    }


class TrainingRecommendationTests(unittest.TestCase):
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
        self.assertEqual(course["coverage_breakdown"]["goal_direct"], 1)
        self.assertEqual(course["coverage_breakdown"]["goal_token"], 2)
        self.assertEqual(course["coverage_breakdown"]["reviewed_goal_links"], 1)
        self.assertTrue(any("훈련목표 KSA: HR strategy" in line for line in course["why_recommended"]))
        self.assertTrue(any("근거 방식" in line for line in course["why_recommended"]))

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
        self.assertFalse(compact["audit"]["sqf_used"])
        self.assertFalse(compact["audit"]["learning_modules_used"])
        self.assertEqual(compact["audit"]["data_sources"], ["ncs_training_courses"])
        self.assertIn("SQF", compact["audit"]["excluded_legacy_sources"])

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
                applied = review_training_transition_scenarios(conn, apply=True)
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
            conn.close()

        self.assertEqual(dry_run["eligible_count"], 1)
        self.assertEqual(dry_run["updated_count"], 0)
        self.assertEqual(status_after_dry_run, "candidate")
        self.assertEqual(audit_count_after_dry_run, 0)
        self.assertEqual(applied["updated_count"], 1)
        self.assertEqual(status_after_apply, "reviewed")
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
        self.assertEqual(audit_by_id[scenario_id]["target_review_status"], "reviewed")
        self.assertEqual(audit_by_id[low_recall_scenario_id]["status_updated"], 0)
        self.assertIn(
            "expected_recall_below_threshold",
            json.loads(audit_by_id[low_recall_scenario_id]["blockers_json"]),
        )
        self.assertTrue(json.loads(audit_by_id[scenario_id]["criteria_json"])["trusted_target_status"])
        self.assertEqual(json.loads(audit_by_id[scenario_id]["metrics_json"])["expected_recall_at_k"], 1.0)
        self.assertEqual(applied["review_method"], "automated_eval_gate")

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
                    },
                },
                "audit": {},
            },
            recommendation_limit=1,
        )

        answer = compact["answer_summary"]
        self.assertIn("Labor relations planning", answer["headline"])
        self.assertEqual(answer["interpretation"]["current"]["resolved_as"], "attendance management (Payroll)")
        self.assertEqual(answer["interpretation"]["current"]["task_element"], "Attendance management")
        self.assertEqual(answer["interpretation"]["current"]["query_alias"]["normalized_query"], "attendance management")
        self.assertEqual(answer["recommended_path"][0]["course_name"], "Labor relations planning")
        self.assertEqual(answer["recommended_path"][0]["hours"], 16)
        self.assertIn("collective bargaining", answer["key_gap_ksa"])
        self.assertTrue(any("attendance management" in caveat for caveat in answer["caveats"]))

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
            relation_types = {
                row["relation_type"]
                for row in conn.execute("SELECT relation_type FROM training_delivery_relations").fetchall()
            }
            conn.close()

            self.assertGreaterEqual(linked["links_after"], 1)
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

            result = generate_training_transition_eval_scenarios(
                conn,
                target_non_hr_count=1,
                per_major_limit=1,
                per_classification_limit=1,
                reset_auto=True,
            )
            row = conn.execute(
                """
                SELECT *
                FROM training_transition_gold_scenarios
                WHERE scenario_name LIKE 'auto_non_hr_%'
                """
            ).fetchone()
            conn.close()

            self.assertTrue(result["ok"])
            self.assertEqual(result["selected_count"], 1)
            self.assertIsNotNone(row)
            self.assertEqual(row["major_code"], "03")
            self.assertEqual(row["review_status"], "candidate_auto")
            self.assertIn("Financial statement", row["expected_course_names_json"])

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
                          1.0, 'candidate', ?, ?)
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

    def test_ksa_meaning_candidates_can_apply_candidate_definitions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(Path(tmp) / "ncs.db")
            initialize_database(conn)
            fixture = seed_task_ontology(conn)
            timestamp = now_utc()
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

            result = build_ksa_meaning_candidates(conn, apply_to_definitions=True)
            row = conn.execute(
                """
                SELECT kmc.*, oc.definition, oc.definition_status
                FROM ksa_meaning_candidates kmc
                JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
                WHERE kmc.concept_id = ?
                  AND kmc.source_method = 'task_context_template'
                """,
                (fixture["concept_id"],),
            ).fetchone()
            atomic_only = conn.execute(
                """
                SELECT definition, definition_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (atomic_only_concept_id,),
            ).fetchone()
            conn.close()

            self.assertEqual(result["meaning_contexts_processed"], 3)
            self.assertEqual(result["definitions_updated"], 3)
            self.assertIsNotNone(row)
            self.assertIn("workforce planning", row["meaning_text"])
            self.assertIn("Build a workforce plan from business strategy", row["meaning_text"])
            self.assertEqual(row["source_method"], "task_context_template")
            self.assertIn("workforce planning supports", row["definition"])
            self.assertEqual(row["definition_status"], "candidate")
            self.assertIn("headcount forecast", atomic_only["definition"])
            self.assertEqual(atomic_only["definition_status"], "candidate")
            self.assertEqual(atomic_only["review_status"], "model_preprocessed")

    def test_mcp_surface_excludes_sqf_and_learning_module_tools(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            previous = os.environ.get("NCS_DB_PATH")
            os.environ["NCS_DB_PATH"] = str(Path(tmp) / "ncs.db")
            try:
                from ncs_mcp import server, tool_registry

                tools = getattr(getattr(server.mcp, "_tool_manager", None), "_tools", {})
                surface = server.current_mcp_tool_surface()
                self.assertLessEqual(len(tools), 10)
                self.assertEqual(set(surface["all_tools"]), tool_registry.ACTIVE_MCP_TOOLS)
                self.assertEqual(set(surface["user_tools"]), tool_registry.USER_MCP_TOOLS)
                self.assertEqual(surface["operator_tools"], [])
                self.assertFalse(surface["operator_tools_enabled"])
                self.assertEqual(set(surface["hidden_operator_tools"]), tool_registry.OPERATOR_MCP_TOOLS)
                self.assertIn("ncs_discover_tools", tools)
                self.assertIn("ncs_execute_tool", tools)
                self.assertIn("ncs_search", tools)
                self.assertIn("ncs_unit_detail", tools)
                self.assertIn("ncs_training", tools)
                self.assertIn("ncs_analysis", tools)
                self.assertIn("recommend_training_for_task", tools)
                self.assertIn("recommend_training_transition", tools)
                self.assertIn("recommend_task_transitions", tools)
                self.assertIn("get_concept_evidence", tools)
                self.assertNotIn("get_quality_issues", tools)
                self.assertNotIn("review_training_goal_concept_link", tools)
                self.assertNotIn("review_task_ksa_concept_relation", tools)
                self.assertNotIn("review_learning_module_ncs_link", tools)
                self.assertNotIn("review_ontology_concept", tools)
                self.assertEqual(len(tools), 10)
                self.assertIn("ncs_discover_tools", surface["user_tools"])
                self.assertIn("ncs_execute_tool", surface["user_tools"])
                self.assertIn("ncs_search", surface["user_tools"])
                self.assertIn("recommend_training_transition", surface["user_tools"])
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

                goal_review = server.review_training_goal_concept_link(
                    goal_link_id,
                    "human_reviewed",
                    reviewer_id="tester",
                    notes="valid direct coverage",
                    confidence_score=0.95,
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
                audit_count = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM review_audit_log
                    WHERE action IN (
                        'review_training_goal_concept_link',
                        'review_task_ksa_concept_relation'
                    )
                    """
                ).fetchone()[0]
                conn.close()
            finally:
                if previous is None:
                    os.environ.pop("NCS_DB_PATH", None)
                else:
                    os.environ["NCS_DB_PATH"] = previous

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
        self.assertEqual(audit_count, 2)

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
                execute_result = server.ncs_execute_tool(
                    "ncs_search",
                    {"query": "HR planning", "scope": "unit", "limit": 2},
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
        self.assertIn("recommend_training_transition", discovered_names)
        self.assertTrue(execute_result["ok"])
        self.assertEqual(execute_result["meta_execution"]["tool_name"], "ncs_search")
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
        explicit_captured: dict[str, object] = {}

        def recommendation_handler(**params):
            captured.update(params)
            return {"ok": True, "data": {"called": True}}

        def explicit_handler(**params):
            explicit_captured.update(params)
            return {"ok": True, "data": {"called": True}}

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
        self.assertEqual(payload["tools"]["exposed"], 10)
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
        self.assertEqual(payload["exposed_count"], 15)
        self.assertEqual(payload["legacy_count"], 0)
        self.assertEqual(payload["unexpected_count"], 0)

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
     