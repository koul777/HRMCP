from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.review_triage import (
    _transition_trust_review_candidate_item,
    build_review_triage_from_files,
    write_review_triage_markdown,
)


class ReviewTriageTests(unittest.TestCase):
    def test_build_review_triage_accepts_utf8_sig_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            transition_path = tmp_path / "transition_seedpack.jsonl"

            quality_path.write_text(
                "\ufeff"
                + json.dumps(
                    {
                        "status": "warn",
                        "summary": {"fail_count": 0, "warn_count": 1},
                        "gates": [
                            {
                                "name": "review_debt:human_reviewed_goal_links",
                                "status": "warn",
                                "message": "human_reviewed_goal_links is still zero.",
                                "value": 0,
                                "threshold": "> 0",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            priority_path.write_text(
                "\ufeff" + json.dumps({"top_items": []}, ensure_ascii=False),
                encoding="utf-8",
            )
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "transition-scenario-seedpack-bom",
                    "item_count": 1,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "transition-scenario-seedpack-bom",
                    "scenario_id": 1,
                    "scenario_name": "bom_case",
                    "current_review_status": "candidate",
                    "current_query": "current",
                    "target_query": "target",
                    "expected_courses": [],
                    "recommended_courses": [],
                    "recommended_course_evidence": [],
                    "recommended_course_scope_summary": {},
                    "expected_recall_at_k": 1.0,
                    "precision_at_k": 1.0,
                    "top1_expected_hit": True,
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "evaluation_case": {
                        "transferability_ratio": 1.0,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                    },
                },
            ]
            transition_path.write_text(
                "\ufeff"
                + "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
                + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=transition_path,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], "ncs_review_triage_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertEqual(report["summary"]["transition_seedpack_id"], "transition-scenario-seedpack-bom")

    def test_build_review_triage_from_files_keeps_artifacts_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            transition_path = tmp_path / "transition_seedpack.jsonl"
            markdown_path = tmp_path / "triage.md"

            quality_path.write_text(
                json.dumps(
                    {
                        "status": "warn",
                        "summary": {"fail_count": 0, "warn_count": 2},
                        "gates": [
                            {
                                "name": "review_debt:human_reviewed_goal_links",
                                "status": "warn",
                                "message": "human_reviewed_goal_links is still zero.",
                                "value": 0,
                                "threshold": "> 0",
                            },
                            {
                                "name": "qualification:error_share",
                                "status": "warn",
                                "message": "Qualification collection error share needs attention.",
                                "value": 0.49,
                                "threshold": "warn > 0.35",
                            },
                            {
                                "name": "recommendation_evidence:training_goal_link_references",
                                "status": "warn",
                                "message": "Saved recommendation evidence references missing training-goal concept links.",
                                "value": 1,
                                "threshold": "== 0",
                            },
                            {
                                "name": "quality_issues:api_element_collection_failure",
                                "status": "warn",
                                "message": "API collection failures exceed the warning threshold.",
                                "value": 1,
                                "threshold": "<= 200",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            priority_path.write_text(
                json.dumps(
                    {
                        "top_items": [
                            {
                                "priority_score": 50,
                                "priority_reason": "training goal link review",
                                "issue": {
                                    "issue_type": "ontology_training_goal_link_human_review_required",
                                    "target_type": "training_goal_concept_link",
                                    "target_id": "10",
                                    "severity": "high",
                                    "suggested_action": "Review link",
                                },
                                "context": {
                                    "compe_unit_name": "HR planning",
                                    "train_goal": "Plan workforce",
                                },
                            },
                            {
                                "priority_score": 80,
                                "priority_reason": "typo",
                                "issue": {
                                    "issue_type": "suspected_typo",
                                    "target_type": "criteria",
                                    "target_id": "20",
                                    "severity": "warning",
                                    "suggested_action": "Review typo",
                                },
                                "context": {"criteria_text_raw": "Typo candidate"},
                            },
                            {
                                "priority_score": 60,
                                "priority_reason": "collection failure",
                                "issue": {
                                    "issue_type": "api_element_collection_failure",
                                    "source_issue_type": "api_element_unmatched",
                                    "target_type": "element",
                                    "target_id": "30",
                                    "severity": "warning",
                                    "suggested_action": "Retry later",
                                },
                                "context": {"element_name_raw": "Plan workforce"},
                            },
                        ],
                        "focus_overlays": [
                            {
                                "code": "aihr_demo_major_02",
                                "label": "AI-HR demo focus",
                                "major_code": "02",
                                "reason": "current demo",
                                "item_count": 1,
                                "top_items": [
                                    {
                                        "priority_score": 50,
                                        "priority_reason": "training goal link review",
                                        "issue": {
                                            "issue_type": "ontology_training_goal_link_human_review_required",
                                            "target_type": "training_goal_concept_link",
                                            "target_id": "10",
                                            "severity": "high",
                                            "suggested_action": "Review link",
                                        },
                                        "context": {
                                            "compe_unit_name": "HR planning",
                                            "train_goal": "Plan workforce",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "transition-scenario-seedpack-test",
                    "item_count": 1,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "transition-scenario-seedpack-test",
                    "scenario_id": 1,
                    "scenario_name": "general_affairs_to_hr",
                    "current_review_status": "candidate",
                    "current_query": "General affairs",
                    "target_query": "HR planning",
                    "expected_courses": ["HR planning"],
                    "recommended_courses": ["Other"],
                    "recommended_course_evidence": [
                        {
                            "rank": 1,
                            "course_name": "Other",
                            "course_scope_fit": {
                                "relation": "same_middle_classification",
                                "alignment": "adjacent",
                                "requires_scope_review": True,
                            },
                            "review_flags": ["course_scope_adjacent_requires_review"],
                        }
                    ],
                    "recommended_course_scope_summary": {
                        "course_count": 1,
                        "relation_counts": {"same_middle_classification": 1},
                        "alignment_counts": {"adjacent": 1},
                        "direct_or_near_count": 0,
                        "requires_scope_review_count": 1,
                        "review_flag_counts": {"course_scope_adjacent_requires_review": 1},
                    },
                    "expected_recall_at_k": 0.0,
                    "precision_at_k": 0.4,
                    "top1_expected_hit": False,
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "evaluation_case": {
                        "transferability_ratio": 0.0,
                        "current_scope_hit": True,
                        "target_scope_hit": True,
                    },
                },
            ]
            transition_path.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=transition_path,
            )
            write_review_triage_markdown(report, markdown_path)
            markdown_text = markdown_path.read_text(encoding="utf-8")

        self.assertTrue(report["ok"])
        self.assertEqual(report["schema"], "ncs_review_triage_v1")
        self.assertTrue(report["report_only"])
        self.assertFalse(report["status_update_allowed"])
        self.assertFalse(report["db_writes"])
        self.assertFalse(report["approval_claim"])
        self.assertTrue(report["human_decision_required"])
        self.assertEqual(report["summary"]["quality_warning_count"], 4)
        self.assertEqual(report["summary"]["transition_seedpack_item_count"], 1)
        self.assertEqual(report["summary"]["transition_attention_count"], 1)
        self.assertEqual(report["summary"]["transition_trust_review_candidate_count"], 0)
        self.assertEqual(report["summary"]["transition_course_scope_review_required_count"], 1)
        self.assertEqual(report["summary"]["transition_course_scope_review_required_item_count"], 1)
        self.assertEqual(
            report["summary"]["transition_course_scope_relation_counts"],
            {"same_middle_classification": 1},
        )
        self.assertEqual(
            report["summary"]["transition_course_scope_alignment_counts"],
            {"adjacent": 1},
        )
        self.assertEqual(report["summary"]["review_focus_overlay_count"], 1)
        self.assertEqual(report["focus_review_priority_overlays"][0]["code"], "aihr_demo_major_02")
        self.assertEqual(report["transition_review_priorities"][0]["priority_band"], "high")
        self.assertIn("low_expected_recall", report["transition_review_priorities"][0]["flags"])
        self.assertIn("course_scope_review_required", report["transition_review_priorities"][0]["flags"])
        self.assertEqual(report["transition_trust_review_candidates"], [])
        self.assertEqual(
            report["transition_review_priorities"][0]["course_scope_fit_relation_counts"],
            {"same_middle_classification": 1},
        )
        self.assertEqual(report["summary"]["quality_warning_categories"]["collection_stability"], 2)
        self.assertEqual(report["summary"]["review_issue_type_counts"]["api_element_collection_failure"], 1)
        self.assertEqual(
            report["review_priority_items"][0]["issue_type"],
            "ontology_training_goal_link_human_review_required",
        )
        self.assertIn("human_review", report["summary"]["quality_warning_categories"])
        self.assertEqual(report["summary"]["quality_warning_categories"]["data_quality"], 1)
        self.assertIn("# NCS Review Triage", markdown_text)
        self.assertIn("scope_relations", markdown_text)
        self.assertIn("Transition Trust Review Candidates", markdown_text)
        self.assertIn("Do not mark candidate scenarios as trusted without human review.", markdown_text)
        self.assertIn("transition_course_scope_review_required_count", markdown_text)
        self.assertIn("Focus Review Priority Overlays", markdown_text)
        self.assertIn("AI-HR demo focus", markdown_text)
        self.assertIn("Do not mark candidate scenarios as trusted", markdown_text)

    def test_review_triage_neutralizes_status_write_suggested_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(
                json.dumps(
                    {
                        "top_items": [
                            {
                                "priority_score": 100,
                                "priority_reason": "core concept",
                                "issue": {
                                    "issue_type": "hr_core_concept_human_review_required",
                                    "target_type": "ontology_concept",
                                    "target_id": "42",
                                    "severity": "high",
                                    "suggested_action": (
                                        "review_ontology_concept로 정의를 확정하고 "
                                        "definition_status='defined', review_status='human_reviewed'로 승인한다."
                                    ),
                                },
                                "context": {"concept_name": "근로기준법"},
                            }
                        ],
                        "focus_overlays": [
                            {
                                "code": "focus",
                                "top_items": [
                                    {
                                        "priority_score": 100,
                                        "priority_reason": "core concept",
                                        "issue": {
                                            "issue_type": "hr_core_concept_human_review_required",
                                            "target_type": "ontology_concept",
                                            "target_id": "42",
                                            "severity": "high",
                                            "suggested_action": "Mark human_reviewed if valid",
                                        },
                                        "context": {"concept_name": "근로기준법"},
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
            )

        action = report["review_priority_items"][0]["suggested_action"]
        overlay_action = report["focus_review_priority_overlays"][0]["items"][0]["suggested_action"]
        self.assertIn("Human reviewer should inspect", action)
        self.assertIn("separate explicit human decision", action)
        self.assertNotIn("human_reviewed", action)
        self.assertIn("separate explicit human decision", overlay_action)

    def test_transition_trust_candidate_requires_careful_review_for_adjacent_scope(self) -> None:
        item = {
            "scenario_id": 9,
            "scenario_name": "adjacent_scope",
            "current_review_status": "candidate",
            "expected_courses": ["Target course"],
            "recommended_courses": ["Adjacent course"],
            "expected_recall_at_k": 1.0,
            "precision_at_k": 1.0,
            "top1_expected_hit": True,
            "recommended_course_scope_summary": {
                "course_count": 1,
                "direct_or_near_count": 0,
                "requires_scope_review_count": 1,
            },
            "evaluation_case": {
                "transferability_ratio": 1.0,
                "current_scope_hit": True,
                "target_scope_hit": True,
            },
        }

        candidate = _transition_trust_review_candidate_item(item)

        self.assertIsNotNone(candidate)
        self.assertIn("course_scope_review_required", candidate["flags"])
        self.assertEqual(candidate["review_readiness"], "needs_careful_review")

    def test_review_triage_sorts_before_applying_review_item_limit(self) -> None:
        report = {
            "top_items": [
                {
                    "priority_score": 80,
                    "priority_reason": "typo",
                    "issue": {
                        "issue_type": "suspected_typo",
                        "target_type": "criteria",
                        "target_id": "20",
                    },
                    "context": {"criteria_text_raw": "Typo candidate"},
                },
                {
                    "priority_score": 50,
                    "priority_reason": "training goal",
                    "issue": {
                        "issue_type": "ontology_training_goal_link_human_review_required",
                        "target_type": "training_goal_concept_link",
                        "target_id": "10",
                    },
                    "context": {"compe_unit_name": "HR planning"},
                },
            ]
        }

        triage = build_review_triage_from_files
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
            result = triage(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                review_item_limit=1,
            )

        self.assertEqual(
            result["review_priority_items"][0]["issue_type"],
            "ontology_training_goal_link_human_review_required",
        )

    def test_review_triage_rejects_non_transition_seedpack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "wrong_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            seedpack_path.write_text(
                json.dumps(
                    {
                        "record_type": "batch",
                        "format_version": "ncs-review-seedpack-v1",
                        "seedpack_id": "review-seedpack-test",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Unsupported transition seedpack format_version"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                    transition_seedpack_path=seedpack_path,
                )

    def test_review_triage_reports_missing_json_input_as_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            priority_path = tmp_path / "review_priority.json"
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "does not exist"):
                build_review_triage_from_files(
                    quality_report_path=tmp_path / "missing_quality.json",
                    review_priority_path=priority_path,
                )

    def test_review_triage_reports_malformed_json_input_as_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text("{not-json", encoding="utf-8")
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                )

    def test_review_triage_reports_malformed_transition_jsonl_as_value_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            seedpack_path.write_text("{not-jsonl\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "not valid JSONL"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                    transition_seedpack_path=seedpack_path,
                )

    def test_review_triage_rejects_quality_report_with_missing_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text(json.dumps({"status": "pass", "gates": []}), encoding="utf-8")
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "summary"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                )

    def test_review_triage_rejects_review_priority_without_top_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"items": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "top_items"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                )

    def test_review_triage_rejects_transition_seedpack_id_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 1,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "different-id",
                    "scenario_id": 10,
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "mismatched seedpack_id"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                    transition_seedpack_path=seedpack_path,
                )

    def test_review_triage_rejects_transition_seedpack_item_count_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 2,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "scenario_id": 10,
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "item_count mismatch"):
                build_review_triage_from_files(
                    quality_report_path=quality_path,
                    review_priority_path=priority_path,
                    transition_seedpack_path=seedpack_path,
                )

    def test_review_triage_warns_when_transition_evaluation_cases_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 1,
                    "missing_evaluation_scenario_ids": [32],
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "scenario_id": 32,
                    "scenario_name": "missing_eval",
                    "current_review_status": "candidate_auto",
                    "current_query": "current",
                    "target_query": "target",
                    "expected_courses": ["Target course"],
                    "recommended_courses": [],
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "evaluation_case_missing": True,
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=seedpack_path,
            )

        checks = {item["name"]: item for item in report["cross_checks"]}
        self.assertEqual(
            report["summary"]["transition_evaluation_snapshot"]["missing_evaluation_scenario_ids"],
            [32],
        )
        self.assertEqual(checks["transition_seedpack_evaluation_complete"]["status"], "warn")
        self.assertEqual(checks["transition_seedpack_evaluation_complete"]["value"], [32])
        self.assertTrue(report["transition_review_priorities"][0]["evaluation_case_missing"])
        self.assertIn("missing_evaluation_case", report["transition_review_priorities"][0]["flags"])

    def test_review_triage_warns_on_trusted_or_partial_transition_review_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 1,
                    "review_statuses": ["candidate", "candidate_auto"],
                    "actual_review_status_counts": {"accepted": 1},
                    "missing_requested_review_statuses": ["candidate", "candidate_auto"],
                    "trusted_review_status_count": 1,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "scenario_id": 10,
                    "scenario_name": "accepted_case",
                    "current_review_status": "accepted",
                    "current_query": "current",
                    "target_query": "target",
                    "expected_courses": ["Target course"],
                    "recommended_courses": ["Other course"],
                    "expected_recall_at_k": 0.0,
                    "precision_at_k": 0.2,
                    "top1_expected_hit": False,
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "proposed_review_status": "accepted",
                    "evaluation_case": {"transferability_ratio": 0.0},
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=seedpack_path,
            )

        cross_checks = {item["name"]: item for item in report["cross_checks"]}
        flags = set(report["transition_review_priorities"][0]["flags"])
        self.assertEqual(cross_checks["transition_seedpack_decisions_empty"]["status"], "warn")
        self.assertEqual(cross_checks["transition_seedpack_no_trusted_items"]["status"], "warn")
        self.assertEqual(cross_checks["transition_seedpack_requested_status_coverage"]["status"], "warn")
        self.assertIn("contains_review_decision", flags)
        self.assertIn("trusted_review_status_in_blank_seedpack", flags)

    def test_review_triage_recomputes_transition_status_coverage_from_items(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 1,
                    "review_statuses": ["candidate", "candidate_auto"],
                    "actual_review_status_counts": {"candidate": 1, "candidate_auto": 1},
                    "missing_requested_review_statuses": [],
                    "trusted_review_status_count": 0,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "scenario_id": 10,
                    "scenario_name": "candidate_case",
                    "current_review_status": "candidate",
                    "expected_courses": ["Target course"],
                    "recommended_courses": ["Target course"],
                    "expected_recall_at_k": 1.0,
                    "precision_at_k": 1.0,
                    "top1_expected_hit": True,
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "proposed_review_status": "",
                    "evaluation_case": {"transferability_ratio": 0.2},
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=seedpack_path,
            )

        cross_checks = {item["name"]: item for item in report["cross_checks"]}
        snapshot = report["summary"]["transition_status_snapshot"]
        self.assertEqual(snapshot["actual_review_status_counts"], {"candidate": 1})
        self.assertEqual(snapshot["missing_requested_review_statuses"], ["candidate_auto"])
        self.assertEqual(cross_checks["transition_seedpack_requested_status_coverage"]["status"], "warn")
        self.assertEqual(cross_checks["transition_seedpack_status_snapshot_consistent"]["status"], "warn")
        self.assertIn(
            "actual_review_status_counts",
            cross_checks["transition_seedpack_status_snapshot_consistent"]["value"],
        )
        self.assertIn(
            "missing_requested_review_statuses",
            cross_checks["transition_seedpack_status_snapshot_consistent"]["value"],
        )

    def test_review_triage_warns_when_trusted_transition_count_is_below_release_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            seedpack_path = tmp_path / "transition_seedpack.jsonl"
            quality_path.write_text(
                json.dumps({"status": "pass", "summary": {"fail_count": 0}, "gates": []}),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")
            records = [
                {
                    "record_type": "batch",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "item_count": 1,
                    "review_statuses": ["candidate"],
                    "actual_review_status_counts": {"candidate": 1},
                    "trusted_review_status_count": 0,
                },
                {
                    "record_type": "transition_scenario_review_item",
                    "format_version": "ncs-transition-scenario-review-v1",
                    "seedpack_id": "batch-id",
                    "scenario_id": 10,
                    "scenario_name": "candidate_case",
                    "current_review_status": "candidate",
                    "expected_courses": ["Target course"],
                    "recommended_courses": ["Target course"],
                    "expected_recall_at_k": 1.0,
                    "precision_at_k": 1.0,
                    "top1_expected_hit": True,
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "proposed_review_status": "",
                    "evaluation_case": {"transferability_ratio": 0.2},
                },
            ]
            seedpack_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
                transition_seedpack_path=seedpack_path,
            )

        cross_checks = {item["name"]: item for item in report["cross_checks"]}
        self.assertEqual(cross_checks["transition_seedpack_no_trusted_items"]["status"], "pass")
        self.assertEqual(cross_checks["seedpack_trusted_transition_scenarios"]["status"], "pass")
        self.assertEqual(cross_checks["seedpack_trusted_transition_scenarios"]["value"], 0)
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["status"], "warn")
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["value"], 0)
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["threshold"], ">= 10")

    def test_review_triage_uses_release_threshold_even_when_quality_gate_is_looser(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            priority_path = tmp_path / "review_priority.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0},
                        "gates": [
                            {
                                "name": "transition_eval:trusted_scenarios",
                                "status": "pass",
                                "message": "At least one trusted scenario exists.",
                                "value": 1,
                                "threshold": "> 0",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            priority_path.write_text(json.dumps({"top_items": []}), encoding="utf-8")

            report = build_review_triage_from_files(
                quality_report_path=quality_path,
                review_priority_path=priority_path,
            )

        cross_checks = {item["name"]: item for item in report["cross_checks"]}
        self.assertEqual(cross_checks["seedpack_trusted_transition_scenarios"]["status"], "pass")
        self.assertEqual(cross_checks["seedpack_trusted_transition_scenarios"]["value"], 0)
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["status"], "warn")
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["value"], 1)
        self.assertEqual(cross_checks["global_trusted_transition_scenarios"]["threshold"], ">= 10")


if __name__ == "__main__":
    unittest.main()
