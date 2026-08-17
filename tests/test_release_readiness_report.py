from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.agent_queue import build_agent_queue_status_from_file
from scripts.export_mcp_tool_contract import build_contract
from scripts.release_readiness_report import (
    _command_option_value,
    _dashboard_verification_lineage_contract,
    _guarded_preflight_from_status_item,
    _preflight_for_action,
    _prerequisite_artifacts_for_command,
    _queue_source_path_matches,
    _release_readiness_cycle_safe_sha256,
    build_agent_work_queue,
    build_aihr_demo_contract,
    build_dashboard_surface_contract,
    build_release_readiness,
    build_review_artifact_readability_contract,
    main,
    write_agent_queue_markdown,
    write_markdown,
)


def valid_mcp_contract() -> dict:
    tools = [
        {"name": name, "role": "user"}
        for name in [
            "get_concept_evidence",
            "ncs_analysis",
            "ncs_discover_tools",
            "ncs_execute_tool",
            "ncs_search",
            "ncs_training",
            "ncs_unit_detail",
            "plan_ncs_education_path",
            "recommend_task_transitions",
            "recommend_training_for_task",
            "recommend_training_transition",
        ]
    ]
    return {
        "surface": {
            "active_tool_count": len(tools),
            "operator_tool_count": 0,
            "user_tool_count": len(tools),
        },
        "tools": tools,
        "operator_tools_available": [
            "get_quality_issues",
            "review_learning_module_ncs_link",
            "review_ontology_concept",
            "review_task_ksa_concept_relation",
            "review_training_goal_concept_link",
        ],
        "query_router": {
            "schema": "ncs_query_route_v1",
            "fingerprint_version": "route-fingerprint-v1",
            "scenario_count": 7,
            "scenarios": [
                {
                    "scenario": "education_system_design",
                    "tool": "plan_ncs_education_path",
                    "required_params": ["current_query", "target_query"],
                    "pipeline": ["recommend_training_transition", "get_concept_evidence", "ncs_analysis"],
                    "expected_tool_chain": [
                        "plan_ncs_education_path",
                        "recommend_training_transition",
                        "get_concept_evidence",
                        "ncs_analysis",
                    ],
                },
                {
                    "scenario": "training_transition",
                    "tool": "recommend_training_transition",
                    "required_params": ["current_query", "target_query"],
                    "pipeline": ["recommend_task_transitions", "recommend_training_for_task"],
                    "expected_tool_chain": [
                        "recommend_training_transition",
                        "recommend_task_transitions",
                        "recommend_training_for_task",
                    ],
                },
                {
                    "scenario": "task_training",
                    "tool": "recommend_training_for_task",
                    "required_params": ["query"],
                    "pipeline": ["get_concept_evidence", "ncs_training"],
                    "expected_tool_chain": [
                        "recommend_training_for_task",
                        "get_concept_evidence",
                        "ncs_training",
                    ],
                },
                {
                    "scenario": "task_transition",
                    "tool": "recommend_task_transitions",
                    "required_params": ["query"],
                    "pipeline": ["recommend_training_for_task"],
                    "expected_tool_chain": [
                        "recommend_task_transitions",
                        "recommend_training_for_task",
                    ],
                },
                {
                    "scenario": "evidence_analysis",
                    "tool": "ncs_analysis",
                    "required_params": ["mode"],
                    "pipeline": [],
                    "expected_tool_chain": ["ncs_analysis"],
                },
                {
                    "scenario": "operator_review",
                    "tool": "get_quality_issues",
                    "required_params": [],
                    "pipeline": [],
                    "requires_operator_surface": True,
                    "public_executable": False,
                    "expected_tool_chain": ["get_quality_issues"],
                },
                {
                    "scenario": "structure_search",
                    "tool": "ncs_search",
                    "required_params": ["query"],
                    "pipeline": [],
                    "expected_tool_chain": ["ncs_search"],
                },
            ],
        },
    }


def valid_guide_trace() -> dict:
    guide_workflow_stages = [
        {
            "code": "C1-1",
            "title": "course investigation and job/KSA mapping",
            "status": "ready",
            "evidence": "mapped_rows=1/1",
            "output_fields": [
                "course_intake_requirements",
                "training_course_inventory_template",
                "training_system_matrix",
                "task_ksa_basis",
            ],
        },
        {
            "code": "C1-2",
            "title": "necessity review and confirmed course list",
            "status": "ready",
            "evidence": "classified_rows=1/1",
            "output_fields": ["training_necessity_review", "need_classification", "human_review"],
        },
        {
            "code": "C2-1",
            "title": "education-system matrix",
            "status": "ready",
            "evidence": "matrix_rows=1",
            "output_fields": ["training_system_matrix", "planner_grouping"],
        },
        {
            "code": "C2-2",
            "title": "annual operation and management fields",
            "status": "ready",
            "evidence": "delivery_rows=1/1",
            "output_fields": ["annual_operation_plan", "delivery_operation", "facility_constraint_fit"],
        },
    ]
    return {
        "schema": "aihr_training_system_guide_trace_v1",
        "rubric_source": "2026_hr_ncs_training_system_guide",
        "rubric_role": "framework_reference_not_scoring_source",
        "non_source_data_policy": "guide_used_as_rubric_only",
        "matrix_reconstruction_fields": [
            "job_scope",
            "target_level_band",
            "education_type",
            "required_optional",
            "delivery_method",
        ],
        "guide_workflow_stage_codes": ["C1-1", "C1-2", "C2-1", "C2-2"],
        "guide_workflow_stages": guide_workflow_stages,
        "guide_workflow": {
            "schema": "aihr_guide_workflow_v1",
            "steps": guide_workflow_stages,
            "missing_codes": [],
        },
        "checks": [
            {"code": "job_scope", "label": "job/NCS scope", "status": "ready", "evidence": "a -> b"},
            {"code": "task_ksa", "label": "task and KSA evidence", "status": "ready", "evidence": "rows=1"},
            {"code": "course_link", "label": "training course linkage", "status": "ready", "evidence": "rows=1"},
            {"code": "required_optional", "label": "required/optional classification", "status": "ready", "evidence": "1/1"},
            {"code": "level_delivery", "label": "level, hours, method, facility", "status": "ready", "evidence": "1/1"},
            {"code": "human_review", "label": "human review gate", "status": "ready", "evidence": "0"},
        ],
    }


def valid_query_route() -> dict:
    return {
        "schema": "ncs_query_route_v1",
        "query": "labor management to HR planning education system",
        "scenario": "education_system_design",
        "tool": "plan_ncs_education_path",
        "params": {
            "current_query": "labor management",
            "target_query": "HR planning",
            "save": False,
            "limit": 5,
        },
        "required_params": ["current_query", "target_query"],
        "missing_params": [],
        "available": True,
        "confidence": 0.96,
        "expected_tool_chain": [
            "plan_ncs_education_path",
            "recommend_training_transition",
            "get_concept_evidence",
            "ncs_analysis",
        ],
        "guard_flags": [],
        "risk_flags": [],
        "route_fingerprint": "route-fingerprint-test",
        "route_contract": {
            "schema": "ncs_query_route_v1",
            "fingerprint_version": "route-fingerprint-v1",
            "route_first": True,
            "primary_tool": "plan_ncs_education_path",
            "allowed_tools": [
                "plan_ncs_education_path",
                "recommend_training_transition",
                "get_concept_evidence",
                "ncs_analysis",
            ],
            "required_params": ["current_query", "target_query"],
            "provided_params": ["current_query", "target_query"],
            "missing_params": [],
            "route_fingerprint": "route-fingerprint-test",
        },
    }


def valid_demo_matrix_row(
    *,
    facilities: list[str] | None = None,
    facility_status: str = "fit",
) -> dict:
    facilities = ["classroom"] if facilities is None else facilities
    facility_fit = {
        "status": facility_status,
        "requested": [] if facility_status == "not_requested" else ["classroom"],
        "available": facilities,
        "matched": facilities if facility_status == "fit" else [],
        "missing": [] if facility_status in {"fit", "unknown", "not_requested"} else ["classroom"],
        "rationale": f"facility status is {facility_status}",
    }
    return {
        "course_name": "HR planning",
        "job_scope": {"current": "labor management", "target": "HR planning"},
        "target_level_band": {"code": "level_5_6", "label": "level 5-6"},
        "education_type": {"code": "reskill", "label": "reskill"},
        "required_optional_basis": {"code": "required", "label": "required"},
        "delivery_operation": {"code": "method_specified", "facility_constraint_fit": dict(facility_fit)},
        "planner_grouping": {
            "job_scope": "labor management -> HR planning",
            "target_level_band": "level_5_6",
            "education_type": "reskill",
            "required_optional": "required",
            "delivery_method": "offline",
            "course_scope_relation": "direct_scope_unit",
        },
        "need_classification": {"code": "required", "label": "required"},
        "evidence_directness": {"code": "training_goal_token", "label": "direct"},
        "course_scope_fit": {
            "relation": "direct_scope_unit",
            "label": "Direct NCS unit link",
            "alignment": "direct",
            "fields": ["unit_code"],
            "target_scope": {"major_code": "02"},
            "course_scope": {"major_code": "02"},
            "direct_unit_codes": ["0202020101_23v3"],
            "is_direct_or_near_scope": True,
            "requires_scope_review": False,
        },
        "task_ksa_basis": {
            "basis_types": ["gap_ksa", "competency_element"],
            "gap_ksa": ["labor law"],
            "target_scope_ksa": ["HR planning"],
            "training_goal_ksa": ["labor law"],
            "covered_elements": ["HR strategy planning"],
            "target_scope_ksa_count": 1,
            "gap_ksa_count": 1,
            "training_goal_ksa_count": 1,
            "covered_element_count": 1,
        },
        "course_link": {
            "course_name": "HR planning",
            "training_course_id": 1,
            "mapping_chain": [
                "job_or_ncs_scope",
                "duty_or_task",
                "performance_criterion",
                "ksa",
                "training_course",
            ],
            "evidence_directness": {"code": "training_goal_token", "label": "direct"},
            "need_classification": {"code": "required", "label": "required"},
            "basis_types": ["gap_ksa", "competency_element"],
            "mapping_strength": {
                "target_scope_ksa_count": 1,
                "gap_ksa_count": 1,
                "training_goal_ksa_count": 1,
                "covered_element_count": 1,
                "course_scope_relation": "direct_scope_unit",
                "course_scope_alignment": "direct",
                "evidence_directness": "training_goal_token",
                "required_optional": "required",
                "review_required": False,
            },
            "course_scope_fit": {
                "relation": "direct_scope_unit",
                "label": "Direct NCS unit link",
                "alignment": "direct",
                "fields": ["unit_code"],
                "target_scope": {"major_code": "02"},
                "course_scope": {"major_code": "02"},
                "direct_unit_codes": ["0202020101_23v3"],
                "is_direct_or_near_scope": True,
                "requires_scope_review": False,
            },
            "why_recommended": ["Training goal covers the target KSA evidence."],
        },
        "course_fit": {
            "level": 5,
            "hours": 24,
            "methods": ["offline"],
            "facilities": facilities,
        },
        "required_optional": "required",
        "mapping_strength": {
            "target_scope_ksa_count": 1,
            "gap_ksa_count": 1,
            "training_goal_ksa_count": 1,
            "covered_element_count": 1,
            "course_scope_relation": "direct_scope_unit",
            "course_scope_alignment": "direct",
            "evidence_directness": "training_goal_token",
            "required_optional": "required",
            "review_required": False,
        },
        "mapping_strength_warning": {
            "status": "clear",
            "codes": [],
            "basis": {
                "target_scope_ksa_count": 1,
                "gap_ksa_count": 1,
                "training_goal_ksa_count": 1,
                "covered_element_count": 1,
                "course_scope_relation": "direct_scope_unit",
                "course_scope_alignment": "direct",
                "evidence_directness": "training_goal_token",
                "required_optional": "required",
            },
            "message": "Mapping-strength evidence risk was not detected.",
        },
        "decision_state": {
            "schema": "aihr_training_row_decision_state_v1",
            "status": "pending_human_decision",
            "decision_required": True,
            "system_suggestion": "required",
            "allowed_decisions": [
                "required",
                "optional",
                "supporting",
                "adjacent_reference",
                "defer",
                "reject",
            ],
            "approval_claim": False,
            "evidence_attention_required": False,
            "basis": {
                "review_flag_count": 0,
                "mapping_strength_warning_codes": [],
                "need_classification": "required",
            },
            "message": "No row is approved by automation; confirm the final decision with a human reviewer.",
        },
        "evidence_chain": {
            "schema": "aihr_course_evidence_chain_v1",
            "chain_order": [
                "job_scope",
                "duty_task",
                "performance_criterion",
                "ksa",
                "training_course",
            ],
            "links": [
                {"stage": "job_scope", "label": "Job or NCS scope", "value": "labor -> HR", "evidence_source": "scope_baseline"},
                {"stage": "duty_task", "label": "Duty/task or competency element", "value": "HR strategy planning", "evidence_source": "target_task.element_name_or_scope_task_element"},
                {"stage": "performance_criterion", "label": "Performance criterion", "value": "Plan workforce", "evidence_source": "target_task.criteria_text_or_covered_elements"},
                {"stage": "ksa", "label": "KSA evidence", "value": ["labor law"], "evidence_source": "task_ksa_basis"},
                {"stage": "training_course", "label": "Training course", "value": "HR planning", "evidence_source": "ncs_training_courses"},
            ],
            "basis_types": ["gap_ksa", "competency_element"],
            "covered_elements": ["HR strategy planning"],
            "course_scope_relation": "direct_scope_unit",
            "completeness": {"status": "complete", "missing_stages": []},
            "message": "Evidence chain follows the NCS guide order: job scope -> duty/task -> performance criterion -> KSA -> training course.",
        },
        "facility_constraint_fit": facility_fit,
        "specificity_warning": {
            "status": "clear",
            "codes": [],
            "basis": {
                "evidence_directness": "training_goal_token",
                "required_optional": "required",
                "basis_types": ["gap_ksa", "competency_element"],
            },
            "message": "Task/KSA/course-link specificity risk was not detected.",
        },
        "duplicate_or_generic_warning": {
            "status": "clear",
            "codes": [],
            "basis": {
                "course_name": "HR planning",
                "duplicate_count": 1,
                "significant_tokens": ["planning"],
            },
            "message": "Duplicate/generic course risk was not detected.",
        },
        "human_review": {
            "severity": "ready",
            "action": "review_training_system_row",
            "flags": [],
            "review_board_hint": "No blocking review flag.",
            "prompt": "Confirm required or optional status before use.",
        },
        "review_flags": [],
    }


def valid_public_demo_payload(
    *,
    facilities: list[str] | None = None,
    facility_status: str = "fit",
) -> dict:
    row = valid_demo_matrix_row(facilities=facilities, facility_status=facility_status)
    annual_operation_plan = {
        "schema": "aihr_annual_operation_plan_seed_v1",
        "guide_stage": "C2-2",
        "status": "needs_human_review",
        "purpose": "Draft annual operation-plan seed generated from the training-system matrix; it is not an approved annual plan.",
        "target_population": "HR staff",
        "requested_constraints": {
            "preferred_max_hours": 24,
            "preferred_methods": ["offline"],
            "preferred_facilities": [],
        },
        "summary": {
            "row_count": 1,
            "estimated_total_hours": 24,
            "hours_known_count": 1,
            "pending_human_decision_rows": 1,
            "review_required_rows": 0,
        },
        "rows": [
            {
                "sequence": 1,
                "recommended_window": "Q1",
                "phase": "core_gap_training",
                "course_name": row["course_name"],
                "training_course_id": 1,
                "target_population": "HR staff",
                "need_classification": "required",
                "system_suggestion": "required",
                "decision_status": "pending_human_decision",
                "human_review_severity": "ready",
                "evidence_chain_status": "complete",
                "hours": 24,
                "methods": ["offline"],
                "facilities": facilities or ["classroom"],
                "constraint_status": "fit",
                "method_status": "not_requested",
                "time_status": "not_requested",
                "facility_status": facility_status,
                "review_flags": [],
                "scheduling_rationale": "Run after scope confirmation.",
            }
        ],
        "review_gate": {
            "status": "blocked_until_human_decision",
            "approval_claim": False,
            "message": "Use this as an operation-planning seed only.",
        },
        "export_fields": [
            "recommended_window",
            "phase",
            "target_population",
            "course_name",
            "need_classification",
            "hours",
            "methods",
            "facilities",
            "constraint_status",
            "decision_status",
            "human_review_severity",
        ],
    }
    course_intake_requirements = {
        "schema": "aihr_course_intake_requirements_v1",
        "guide_stage": "C1-1",
        "status": "needs_collection_or_review",
        "purpose": "Minimum course-investigation fields required before task/KSA mapping.",
        "current_scope": "labor management",
        "target_scope": "HR planning",
        "target_population": "HR staff",
        "requested_constraints": {
            "preferred_max_hours": 24,
            "preferred_methods": ["offline"],
            "preferred_facilities": [],
        },
        "required_fields": [
            {"field": field, "purpose": f"Collect {field}.", "maps_to": ["training_system_matrix"]}
            for field in [
                "course_name",
                "course_goal",
                "target_learners",
                "content_outline",
                "ncs_scope_or_unit",
                "performance_criteria_or_task",
                "ksa_evidence",
                "level",
                "hours",
                "methods",
                "facilities",
                "assessment_method",
            ]
        ],
        "optional_fields": ["provider", "source_url_or_document", "reviewer_notes"],
        "mapping_policy": {
            "title_only_mapping_allowed": False,
            "n_to_n_job_course_mapping_allowed": True,
            "generic_course_requires_warning": True,
            "framework_reference_is_not_scoring_source": True,
            "human_review_required_before_approval": True,
        },
        "prefill_from_recommendations": {
            "matrix_rows": 1,
            "course_count": 1,
            "course_names": ["HR planning"],
            "missing_hours_rows": 0,
            "missing_methods_rows": 0,
            "missing_facilities_rows": 0,
        },
        "review_gate": {
            "status": "intake_template_only",
            "approval_claim": False,
            "message": "No course is approved by the intake template.",
        },
    }
    inventory_required_columns = [
        "source_type",
        "course_name",
        "course_goal",
        "target_learners",
        "content_outline",
        "ncs_scope_or_unit",
        "performance_criteria_or_task",
        "ksa_evidence",
        "level",
        "hours",
        "methods",
        "facilities",
        "education_type",
        "required_optional_basis",
        "assessment_method",
        "duplicate_or_generic_risk",
        "review_state",
    ]
    training_course_inventory_template = {
        "schema": "aihr_training_course_inventory_template_v1",
        "guide_stage": "C1-1",
        "status": "template_with_prefill",
        "purpose": "Inventory table contract for investigated training courses.",
        "target_population": "HR staff",
        "requested_constraints": {
            "preferred_max_hours": 24,
            "preferred_methods": ["offline"],
            "preferred_facilities": [],
        },
        "columns": [
            {
                "column": column,
                "required": True,
                "purpose": f"Collect {column}.",
                "maps_to": ["training_system_matrix"],
                "validation": "text_or_review_needed",
            }
            for column in inventory_required_columns
        ],
        "required_columns": inventory_required_columns,
        "row_template": {column: "" for column in inventory_required_columns},
        "prefill_rows": [
            {
                "source_type": "ncs_training_api",
                "course_name": row["course_name"],
                "course_goal": "Training goal covers HR planning.",
                "target_learners": "HR staff",
                "content_outline": "HR strategy planning",
                "ncs_scope_or_unit": ["0202020101_23v3"],
                "performance_criteria_or_task": ["HR strategy planning"],
                "ksa_evidence": ["labor law"],
                "level": 5,
                "hours": 24,
                "methods": ["offline"],
                "facilities": ["classroom"],
                "education_type": "reskill",
                "required_optional_basis": "required",
                "assessment_method": "not_collected",
                "duplicate_or_generic_risk": "clear",
                "review_state": "pending_human_decision",
                "evidence_chain_status": "complete",
            }
        ],
        "validation_rules": ["Do not classify a course as required from course_name alone."],
        "review_gate": {
            "status": "inventory_template_only",
            "approval_claim": False,
            "message": "No inventory row is approved by the template.",
        },
    }
    training_necessity_review = {
        "schema": "aihr_training_necessity_review_v1",
        "guide_stage": "C1-2",
        "status": "review_evidence_prepared",
        "purpose": "C1-2 necessity-review contract.",
        "target_population": "HR staff",
        "requested_constraints": {
            "preferred_max_hours": 24,
            "preferred_methods": ["offline"],
            "preferred_facilities": [],
        },
        "review_dimensions": [
            "job_linkage",
            "level_fit",
            "required_optional_review",
            "duplicate_or_generic_review",
            "delivery_feasibility",
            "performance_contribution",
            "human_review",
        ],
        "summary": {
            "row_count": 1,
            "review_required_rows": 1,
            "approval_blocked_rows": 1,
            "job_linkage_status_counts": {"evidence_visible": 1},
            "level_fit_status_counts": {"evidence_visible": 1},
            "delivery_feasibility_status_counts": {"fit": 1},
            "duplicate_or_generic_status_counts": {"clear": 1},
            "performance_contribution_status_counts": {"evidence_visible": 1},
            "required_optional_counts": {"required": 1},
            "decision_state_counts": {"pending_human_decision": 1},
        },
        "rows": [
            {
                "sequence": 1,
                "course_name": row["course_name"],
                "training_course_id": row.get("training_course_id"),
                "job_linkage": {
                    "status": "evidence_visible",
                    "course_scope_relation": "direct_scope_unit",
                    "course_scope_alignment": "direct",
                    "evidence_directness": "training_goal_token",
                    "task_ksa_basis_counts": {
                        "target_scope_ksa": 1,
                        "gap_ksa": 1,
                        "training_goal_ksa": 1,
                        "covered_elements": 1,
                    },
                    "review_reason": "Task/KSA and scope evidence are visible.",
                },
                "level_fit": {
                    "status": "evidence_visible",
                    "target_level_band": row["target_level_band"],
                    "course_level": row["course_fit"]["level"],
                    "review_reason": "Course level and target level band are visible.",
                },
                "required_optional_review": {
                    "code": "required",
                    "label": "required",
                    "rationale": "Core gap coverage.",
                    "statutory_or_mandatory_basis": "not_supplied",
                    "approval_claim": False,
                },
                "duplicate_or_generic_review": {
                    "status": "clear",
                    "codes": [],
                    "duplicate_or_generic_warning": row["duplicate_or_generic_warning"],
                    "specificity_warning": row["specificity_warning"],
                    "mapping_strength_warning": row["mapping_strength_warning"],
                },
                "delivery_feasibility": {
                    "status": "fit",
                    "constraint_status": "fit",
                    "hours": row["course_fit"]["hours"],
                    "methods": row["course_fit"]["methods"],
                    "facilities": row["course_fit"]["facilities"],
                    "requested_constraints": {"preferred_max_hours": 24},
                    "constraint_fit": {"status": "fit", "dimensions": {"method": "fit", "time": "fit", "facility": "fit"}},
                },
                "performance_contribution": {
                    "status": "evidence_visible",
                    "evidence_chain_status": "complete",
                    "gap_ksa": ["labor law"],
                    "training_goal_ksa": ["labor law"],
                    "covered_elements": ["HR strategy planning"],
                    "review_reason": "KSA or covered element evidence is visible.",
                },
                "decision_state": row["decision_state"],
                "human_review": row["human_review"],
                "review_flags": [],
                "recommended_review_action": "human_confirm_before_use",
            }
        ],
        "validation_rules": ["Do not confirm a course from course_name alone."],
        "review_gate": {
            "status": "pending_human_confirmation",
            "approval_claim": False,
            "message": "No course list is approved by automation.",
        },
    }
    return {
        "ok": True,
        "view": "ncs_education_plan",
        "public_demo_schema": "aihr_public_demo_v1",
        "review_context_policy": {
            "schema": "aihr_public_review_context_policy_v1",
            "review_only": True,
            "non_scoring": True,
            "approval_claim": False,
            "db_writes": False,
            "status_update_allowed": False,
            "source_payload_exposed": False,
            "official_learning_module_rule": "official EDU direct links only",
            "ocr_report_context_rule": "OCR/report context is human-review-only and non-scoring",
        },
        "query_route": valid_query_route(),
        "scope_baseline": {
            "schema": "aihr_scope_baseline_v1",
            "guide_stage": "C1-1",
            "purpose": "Record the job/NCS scope resolution used before task/KSA/course mapping.",
            "current": {
                "role": "current",
                "requested_query": "labor management",
                "resolved_scope": "labor management",
                "match_level": "sub_classification",
                "unit_count": 1,
                "task_element": "labor relations planning",
                "query_alias": None,
                "alternative_count": 0,
                "scope_resolution_basis": ["sub_classification"],
            },
            "target": {
                "role": "target",
                "requested_query": "HR planning",
                "resolved_scope": "HR planning",
                "match_level": "competency_unit",
                "unit_count": 1,
                "task_element": "HR strategy planning",
                "query_alias": None,
                "alternative_count": 0,
                "scope_resolution_basis": ["competency_unit"],
            },
            "ncs_scope_relation": "same_small_classification",
            "current_scope_subset_of_target": False,
            "exact_ksa_overlap_ratio": 0.1,
            "ontology_adjusted_transferability_ratio": 0.3,
            "adjusted_transferability_components": {"exact_ksa_overlap_ratio": 0.1},
            "target_role_overlay": None,
            "human_review": {"status": "ready", "flags": [], "prompt": "Confirm scope."},
        },
        "training_system_guide_trace": valid_guide_trace(),
        "training_system_summary": {"course_count": 1},
        "course_intake_requirements": course_intake_requirements,
        "training_course_inventory_template": training_course_inventory_template,
        "training_necessity_review": training_necessity_review,
        "annual_operation_plan": annual_operation_plan,
        "recommended_path": [
            {
                "stage": 1,
                "role": "scope_confirmation",
                "guide_stage": "C1-1",
                "guide_stage_status": "ready",
                "guide_stage_evidence": {"evidence": "mapped_rows=1/1"},
                "title": "Scope confirmation",
                "actions": ["Confirm current and target NCS scope."],
            },
            {
                "stage": 2,
                "role": "core_gap_training",
                "guide_stage": "C1-2",
                "guide_stage_status": "ready",
                "guide_stage_evidence": {"evidence": "classified_rows=1/1"},
                "title": "Core gap training",
                "courses": [{"course_name": row["course_name"], "hours": row["course_fit"]["hours"]}],
            },
            {
                "stage": 3,
                "role": "supporting_or_adjacent_training",
                "guide_stage": "C2-1",
                "guide_stage_status": "ready",
                "guide_stage_evidence": {"evidence": "matrix_rows=1"},
                "title": "Supporting training",
                "courses": [],
            },
            {
                "stage": 4,
                "role": "delivery_fit_review",
                "guide_stage": "C2-2",
                "guide_stage_status": "ready",
                "guide_stage_evidence": {"evidence": "delivery_rows=1/1"},
                "title": "Delivery fit review",
                "actions": ["Check method and facility fit."],
            },
        ],
        "training_system_matrix": [row],
        "audit": {"sqf_used": False, "learning_modules_used": False},
    }


def valid_static_artifacts() -> list[dict]:
    def safe_definition_packet() -> dict:
        return {
            "exists": True,
            "ok": True,
            "schema": "ncs_ksa_definition_review_operator_packet_v1",
            "safety_ok": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "source_payload_exposed": False,
            "trusted_status_write_allowed": False,
            "raw_source_mutation_allowed": False,
            "sidecar_safety_ok": True,
            "sidecar_consistency_issues": [],
        }

    checkpoint_artifacts = []
    checkpoint_stems = {
        "ncs006_element_api_checkpoint_json": "checkpoint_ncs006_element_api_status",
        "human_review_safe_ops_checkpoint_json": "human_review_safe_ops_checkpoint",
        "sqf_db_readiness_checkpoint_json": "sqf_db_readiness_checkpoint",
        "overnight_ncs_sqf_work_checkpoint_json": "overnight_ncs_sqf_work_checkpoint",
    }
    for name, schema in [
        ("ncs006_element_api_checkpoint_json", "ncs006_element_api_checkpoint_v1"),
        ("human_review_safe_ops_checkpoint_json", "human_review_safe_ops_checkpoint_v1"),
        ("sqf_db_readiness_checkpoint_json", "sqf_db_readiness_checkpoint_v1"),
        ("overnight_ncs_sqf_work_checkpoint_json", "overnight_ncs_sqf_work_checkpoint_v1"),
    ]:
        stem = checkpoint_stems[name]
        checkpoint_artifacts.append(
            {
                "name": name,
                "path": f"reports/{stem}_20260624.json",
                "exists": True,
                "size_bytes": 512,
                "mtime_utc": "2026-06-19T00:00:00+00:00",
                "non_empty": True,
                "checkpoint": {
                    "schema": schema,
                    "contract_ok": True,
                    "read_only_checkpoint": True,
                    "db_writes": False,
                    "status_updates": False,
                    "secrets_included": False,
                    "sensitive_markers": [],
                    "forbidden_paths": [],
                },
            }
        )
        checkpoint_artifacts.append(
            {
                "name": name.removesuffix("_json") + "_md",
                "path": f"reports/{stem}_20260624.md",
                "exists": True,
                "size_bytes": 256,
                "mtime_utc": "2026-06-19T00:00:00+00:00",
                "non_empty": True,
            }
        )
    artifacts = [
        {
            "name": "demo_html",
            "path": "reports/aihr_plan_demo_20260624.html",
            "exists": True,
            "size_bytes": 1024,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
        },
        {
            "name": "demo_json",
            "path": "reports/aihr_plan_demo_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
        },
        {
            "name": "demo_alias_json",
            "path": "reports/aihr_plan_demo_alias_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
        },
        {
            "name": "queue_status_json",
            "path": "reports/aihr_agent_queue_status_20260624.json",
            "exists": True,
            "size_bytes": 512,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "queue_status": {
                "schema": "aihr_agent_queue_status_v1",
                "source_queue_path": "reports/aihr_agent_queue_20260624.json",
                "contract_ok": True,
                "blocked_count": 0,
                "manual_ready_count": 2,
                "auto_startable_count": 1,
                "state_counts": {"ready_to_start": 1, "manual_ready": 2},
                "human_gated_execution": [],
                "unsafe_manual_items": [],
                "guarded_manual_items": ["manual-human", "manual-guarded"],
            },
        },
        {
            "name": "queue_run_json",
            "path": "reports/aihr_agent_queue_run_20260624.json",
            "exists": True,
            "size_bytes": 512,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "queue_run": {
                "schema": "aihr_agent_queue_run_v1",
                "source_queue_path": "reports/aihr_agent_queue_20260624.json",
                "source_queue_sha256": (
                    "sha256:"
                    "0000000000000000000000000000000000000000000000000000000000000000"
                ),
                "queue_status_snapshot_sha256": (
                    "sha256:"
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "contract_ok": True,
                "dry_run": False,
                "dry_run_count": 0,
                "selected_count": 1,
                "run_count": 1,
                "actual_run": True,
                "output_issues": [],
                "lineage_issues": [],
                "run_statuses": ["succeeded"],
            },
            "queue_run_source_queue_sync": {
                "checked": True,
                "source_queue_path": "reports/aihr_agent_queue_20260624.json",
                "source_queue_exists": True,
                "source_queue_sha256": (
                    "sha256:"
                    "0000000000000000000000000000000000000000000000000000000000000000"
                ),
                "current_source_queue_sha256": (
                    "sha256:"
                    "0000000000000000000000000000000000000000000000000000000000000000"
                ),
                "source_queue_matches_run": True,
            },
            "queue_status_snapshot_sync": {
                "checked": True,
                "source_queue_path": "reports/aihr_agent_queue_20260624.json",
                "source_queue_exists": True,
                "queue_status_snapshot_sha256": (
                    "sha256:"
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "current_queue_status_snapshot_sha256": (
                    "sha256:"
                    "1111111111111111111111111111111111111111111111111111111111111111"
                ),
                "queue_status_snapshot_matches_run": True,
                "reason": None,
            },
        },
        {
            "name": "readiness_json",
            "path": "reports/aihr_release_readiness_20260624.json",
            "exists": True,
            "size_bytes": 4096,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "release_readiness": {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "agent_work_queue_path": "reports/aihr_agent_queue_20260624.json",
                "contract_ok": True,
            },
        },
        {
            "name": "hrd_guide_prompt_coverage_json",
            "path": "reports/hrd_guide_prompt_coverage_20260624.json",
            "exists": True,
            "size_bytes": 1024,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
        },
        {
            "name": "guide_surface_audit_json",
            "path": "reports/aihr_guide_surface_audit_20260624.json",
            "exists": True,
            "size_bytes": 1024,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "guide_surface_audit": {
                "schema": "aihr_guide_surface_audit_v1",
                "ok": True,
                "blocker_count": 0,
                "review_finding_count": 2,
                "artifact_count": 2,
                "matrix_rows": 6,
                "unsafe_approval_claim_artifacts": 0,
                "guide_stage_codes": {"C1-1": 2, "C1-2": 2, "C2-1": 2, "C2-2": 2},
                "approval_claim": False,
                "db_writes": False,
                "guide_role": "framework_reference",
                "sensitive_markers": [],
                "human_decision_required_for_approval": True,
            },
        },
        {
            "name": "ontology_transferability_education_audit_json",
            "path": "reports/ontology_transferability_education_system_audit_20260624.json",
            "exists": True,
            "size_bytes": 1024,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "ontology_transferability_education_audit": {
                "schema": "ncs_ontology_transferability_education_system_audit_v1",
                "ok": False,
                "contract_ok": True,
                "approval_ready": False,
                "status": "review_required",
                "major_count": 24,
                "scope_count": 24,
                "matrix_row_count": 308,
                "rows_requiring_human_review": 308,
                "unsafe_review_status_count": 0,
                "invalid_review_status_count": 0,
                "course_link_row_coverage": 0.9091,
                "guide_stage_counts": {"C1-1": 24, "C1-2": 24, "C2-1": 24, "C2-2": 24},
                "approval_claim": False,
                "db_writes": False,
                "guide_role": "framework_reference",
                "review_gate_status": "open",
                "review_gate_approval_claim": False,
                "sensitive_markers": [],
            },
        },
        {
            "name": "review_workflow_handoff_json",
            "path": "reports/aihr_plan_review_workflow_handoff_20260624.json",
            "exists": True,
            "size_bytes": 1024,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "review_workflow_handoff": {
                "schema": "aihr_plan_review_workflow_handoff_v1",
                "contract_ok": True,
                "review_surface_contract_source": "nested_unit_triage",
                "next_request_unit_triage_present": True,
                "nested_review_surface_contract_present": True,
                "packet_index_exists": True,
                "packet_index_non_empty": True,
                "packet_index_contract_ok": True,
                "source_payload_exposed": False,
                "sensitive_markers": [],
                "issues": [],
            },
        },
        {
            "name": "human_review_backlog_json",
            "path": "reports/human_review_backlog_report_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "human_review_backlog": {
                "schema": "aihr_human_review_backlog_v1",
                "contract_ok": True,
                "review_status_policy": {
                    "contract_ok": True,
                    "human_decision_required_for_status_update": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
                },
                "all_seedpacks_safe": True,
                "total_review_items": 170,
                "total_forbidden_true_field_violations": 0,
                "total_status_update_allowed_violations": 0,
                "total_missing_status_update_allowed": 0,
                "total_trusted_status_proposals": 0,
                "total_seedpack_structure_issues": 0,
                "source_hash_contract_ok": True,
                "source_release_hash_scope": "cycle_safe_release_readiness",
                "source_release_cycle_safe_hash_present": True,
                "queue_input_hash_count": 1,
                "queue_supporting_report_inputs_present": True,
                "blocker_count": 4,
                "ksa_definition_packet": safe_definition_packet(),
            },
        },
        {
            "name": "goal_completion_audit_json",
            "path": "reports/goal_completion_audit_report_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "goal_completion_audit": {
                "schema": "aihr_goal_completion_audit_v1",
                "contract_ok": True,
                "review_status_policy": {
                    "contract_ok": True,
                    "human_decision_required_for_status_update": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
                },
                "release_ready": False,
                "release_ready_consistent": True,
                "open_requirement_count": 4,
                "verified_requirement_count": 3,
                "human_review_backlog_all_seedpacks_safe": True,
                "human_review_backlog_forbidden_true_field_violations": 0,
                "human_review_backlog_status_update_allowed_violations": 0,
                "human_review_backlog_missing_status_update_allowed": 0,
                "human_review_backlog_trusted_status_proposals": 0,
                "human_review_backlog_seedpack_structure_issues": 0,
                "ksa_definition_packet": safe_definition_packet(),
            },
        },
        {
            "name": "query_route_contract_audit_json",
            "path": "reports/query_route_contract_audit_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "query_route_contract_audit": {
                "schema": "ncs_query_route_contract_audit_v1",
                "ok": True,
                "status": "pass",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "case_count": 2,
                "pass_count": 2,
                "failure_count": 0,
                "row_count": 2,
                "failure_summary_count": 0,
                "passed_row_count": 2,
                "failed_row_count": 0,
                "malformed_row_count": 0,
                "row_issue_count": 0,
                "contract_ok": True,
            },
        },
        {
            "name": "api_linkage_summary_json",
            "path": "reports/api_linkage_summary_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "api_linkage_summary": {
                "schema": "ncs_api_linkage_summary_v1",
                "ok": True,
                "major_count": 24,
                "unit_count": 13435,
                "training_unit_coverage": 0.879494,
                "job_base_unit_coverage": 0.940528,
                "qualification_collection_coverage": 0.322069,
                "db_writes": False,
                "api_calls": False,
                "human_review_status_updates": False,
                "sqf_active_scoring_source": False,
                "safe_next_action_count": 1,
                "guarded_collection_candidate_count": 28,
                "unguarded_collection_candidate_count": 0,
                "unsafe_safe_next_action_count": 0,
                "qualification_coverage_plan_hint": {
                    "scope": "all_majors",
                    "scope_major_codes": [],
                    "coverage_plan_command_scope": "all_units",
                    "coverage_plan_matches_summary_scope": True,
                    "target_ratio": 0.9,
                    "batch_size": 100,
                    "total_unit_count": 13435,
                    "attempted_unit_count": 4327,
                    "collection_coverage": 0.322069,
                    "additional_attempted_units_needed": 7765,
                    "estimated_batch_count": 78,
                    "must_run_qualification_retry_hygiene_first": True,
                    "guard_required": True,
                    "operator_timing_required": True,
                    "db_writes": False,
                    "api_calls": False,
                    "human_review_status_updates": False,
                    "coverage_plan_command_present": True,
                    "global_coverage_plan_command_present": True,
                },
            },
        },
        {
            "name": "qualification_retry_hygiene_json",
            "path": "reports/qualification_retry_hygiene_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "qualification_retry_hygiene": {
                "ok": True,
                "mode": "dry_run",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "approval_claim": False,
                "execution_authorized": False,
                "retry_collection_authorized": False,
                "automatic_queue_execution_allowed": False,
                "authorization_status": "not_authorized_read_only_report",
                "collection_coverage": 0.3221,
                "additional_attempted_units_needed": 7765,
                "status_count_rows": 2,
                "major_coverage_gap_count": 13,
                "do_not_call_api": True,
                "api_guard_status": "allowed",
                "api_call_allowed_now": False,
                "qualification_retry_allowed_now": True,
                "next_safe_action_status": "complete_no_collection_needed",
                "retry_hygiene_status_scope": "retry_preflight_only_not_collection_coverage",
                "coverage_gap_open": True,
                "coverage_gap_normalized_next_safe_action": (
                    "plan_guarded_qualification_collection_for_unattempted_units"
                ),
                "safety_violation_count": 0,
            },
        },
        {
            "name": "qualification_collection_coverage_plan_json",
            "path": "reports/qualification_collection_coverage_plan_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "qualification_collection_coverage_plan": {
                "schema": "ncs_qualification_collection_coverage_plan_v1",
                "ok": True,
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "api_calls": False,
                "execution_authorized": False,
                "top_level_automatic_queue_execution_allowed": False,
                "automatic_collection_allowed_now": False,
                "operator_timed_guarded_api_commands_only": True,
                "human_review_status_updates": False,
                "approval_claim": False,
                "checkpoint_path": "reports\\checkpoint_ncs006_element_api_status_20260624_current.json",
                "batch_commands_have_ncs006_checkpoint_values": True,
                "batch_commands_checkpoint_values_match_plan": True,
                "batch_commands_unique_checkpoint_paths": [
                    "reports\\checkpoint_ncs006_element_api_status_20260624_current.json"
                ],
                "target_ratio": 0.9,
                "batch_size": 100,
                "total_unit_count": 13435,
                "attempted_unit_count": 5349,
                "collection_coverage": 0.398139,
                "additional_attempted_units_needed": 6743,
                "estimated_batch_count": 68,
                "batch_count": 68,
                "unsafe_batch_count": 0,
                "raw_batch_count": 68,
                "raw_batch_count_matches_batches": True,
                "raw_unsafe_batch_count": 0,
                "raw_unsafe_batch_count_matches_batches": True,
                "raw_unsafe_batches_count": 0,
                "raw_unsafe_batches_match_batches": True,
                "must_run_qualification_retry_hygiene_first": True,
                "must_use_ncs006_checkpoint_path": True,
                "must_not_write_human_review_statuses": True,
                "operator_timing_required": True,
                "operator_must_confirm_api_timing": True,
                "batch_commands_are_operator_timed": True,
                "batch_commands_are_not_queue_items": True,
                "batch_commands_checkpoint_path_must_match_plan": True,
                "automatic_queue_execution_allowed": False,
                "forbidden_status_updates": [
                    "human_reviewed",
                    "accepted",
                    "reviewed",
                ],
                "forbidden_status_updates_exact": True,
            },
        },
        {
            "name": "human_review_provenance_reconfirmation_packet_json",
            "path": "reports/human_review_provenance_reconfirmation_packet_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "content_sha256": "sha256:packet",
            "human_review_provenance_reconfirmation_packet": {
                "schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "ok": True,
                "contract_ok": True,
                "row_count": 34,
                "legacy_status_needs_reconfirmation_count": 34,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            },
        },
        {
            "name": "human_review_provenance_reconfirmation_decision_sheet_json",
            "path": "reports/human_review_provenance_reconfirmation_decision_sheet_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "human_review_provenance_reconfirmation_decision_sheet": {
                "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
                "ok": True,
                "contract_ok": True,
                "row_count": 34,
                "blank_decision_count": 34,
                "completed_decision_count": 0,
                "source_packet_schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "source_packet_sha256": "sha256:packet",
                "source_packet_contract_ok": True,
                "source_packet_contract_issues": [],
                "source_packet_row_identity_issue_count": 0,
                "source_packet_row_identity_issues": [],
                "row_safety_flag_type_issues": [],
                "row_safety_flag_type_issue_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "human_decision_required": True,
            },
        },
        {
            "name": "human_review_provenance_reconfirmation_decision_audit_json",
            "path": "reports/human_review_provenance_reconfirmation_decision_audit_20260624.json",
            "exists": True,
            "size_bytes": 2048,
            "mtime_utc": "2026-06-19T00:00:00+00:00",
            "non_empty": True,
            "human_review_provenance_reconfirmation_decision_audit": {
                "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
                "ok": True,
                "contract_ok": True,
                "row_count": 34,
                "pending_decision_count": 34,
                "completed_decision_count": 0,
                "invalid_decision_count": 0,
                "action_eligible_count": 0,
                "invalid_evidence_refs_json_count": 0,
                "invalid_reviewer_id_count": 0,
                "invalid_reviewed_at_count": 0,
                "source_packet_schema": "aihr_human_review_provenance_reconfirmation_packet_v1",
                "source_packet_sha256": "sha256:packet",
                "source_packet_contract_ok": True,
                "source_packet_contract_issues": [],
                "source_packet_row_identity_issue_count": 0,
                "source_packet_row_identity_issues": [],
                "source_packet_row_count": 34,
                "duplicate_csv_key_count": 0,
                "missing_packet_row_count": 0,
                "unexpected_csv_row_count": 0,
                "source_decision_packet_not_found_count": 0,
                "source_decision_packet_not_portable_count": 0,
                "source_decision_packet_unsupported_type_count": 0,
                "source_decision_packet_unrecognized_count": 0,
                "source_identity_mismatch_count": 0,
                "unsafe_flag_count": 0,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "guarded_apply_ready": False,
            },
        },
        *checkpoint_artifacts,
    ]
    for item in artifacts:
        if item.get("name") not in {"queue_status_json", "queue_run_json"}:
            continue
        path = Path(str(item.get("path") or ""))
        resolved_path = path if path.is_absolute() else ROOT / path
        if resolved_path.exists():
            item["content_sha256"] = "sha256:" + hashlib.sha256(
                resolved_path.read_bytes()
            ).hexdigest()
    return artifacts


def valid_live_plan_summary(name: str = "baseline", matrix_rows: int = 3) -> dict:
    return {
        "name": name,
        "ok": True,
        "schema": "aihr_live_plan_v1",
        "run_mode": "live_no_save",
        "view": "ncs_education_plan",
        "matrix_rows": matrix_rows,
        "missing_matrix_fields": [],
        "missing_plan_fields": [],
        "guide_trace_schema": "aihr_training_system_guide_trace_v1",
        "missing_guide_trace_fields": [],
        "query_route_schema": "ncs_query_route_v1",
        "query_route_tool": "plan_ncs_education_path",
        "query_route_fingerprint": f"{name}-route-fingerprint",
        "query_route_expected_tool_chain": [
            "plan_ncs_education_path",
            "recommend_training_transition",
            "get_concept_evidence",
            "ncs_analysis",
        ],
        "query_route_guard_flags": [],
        "query_route_contract_schema": "ncs_query_route_v1",
        "query_route_contract_route_first": True,
        "query_route_contract_primary_tool": "plan_ncs_education_path",
        "query_route_contract_fingerprint": f"{name}-route-fingerprint",
        "missing_query_route_fields": [],
        "training_necessity_review_schema": "aihr_training_necessity_review_v1",
        "training_necessity_review_summary": {
            "schema": "aihr_training_necessity_review_v1",
            "guide_stage": "C1-2",
            "status": "review_evidence_prepared",
            "row_count": matrix_rows,
            "review_required_rows": matrix_rows,
            "approval_blocked_rows": matrix_rows,
            "job_linkage_status_counts": {"evidence_visible": matrix_rows},
            "level_fit_status_counts": {"evidence_visible": matrix_rows},
            "delivery_feasibility_status_counts": {"fit": matrix_rows},
            "duplicate_or_generic_status_counts": {"clear": matrix_rows},
            "performance_contribution_status_counts": {"evidence_visible": matrix_rows},
            "required_optional_counts": {"required": matrix_rows},
            "decision_state_counts": {"pending_human_decision": matrix_rows},
            "recommended_review_action_counts": {"human_confirm_before_use": matrix_rows},
            "review_gate_status": "pending_human_confirmation",
            "approval_claim": False,
            "approval_claim_safe": True,
            "unsafe_row_approval_claim_count": 0,
        },
        "annual_operation_plan_schema": "aihr_annual_operation_plan_seed_v1",
        "annual_operation_plan_summary": {
            "schema": "aihr_annual_operation_plan_seed_v1",
            "guide_stage": "C2-2",
            "status": "needs_human_review",
            "row_count": matrix_rows,
            "estimated_total_hours": 12 * matrix_rows,
            "hours_known_count": matrix_rows,
            "pending_human_decision_rows": matrix_rows,
            "review_required_rows": matrix_rows,
            "window_counts": {"Q1": matrix_rows},
            "phase_counts": {"core_gap_training": matrix_rows},
            "decision_status_counts": {"pending_human_decision": matrix_rows},
            "constraint_status_counts": {"fit": matrix_rows},
            "human_review_severity_counts": {"needs_review": matrix_rows},
            "review_gate_status": "blocked_until_human_decision",
            "approval_claim": False,
            "approval_claim_safe": True,
        },
        "sensitive_markers": [],
    }


def valid_dashboard_endpoint_checks() -> list[dict]:
    return [
        {"name": "static_artifacts", "ok": True},
        {
            "name": "review_chain_safety",
            "ok": True,
            "review_chain_safety_summary": valid_review_chain_safety_summary(),
        },
        {"name": "live_page", "ok": True},
        {"name": "training_system_builder_page", "ok": True},
        {"name": "demo_page", "ok": True},
        {"name": "readiness_page", "ok": True},
        {"name": "review_board_page", "ok": True},
        {"name": "ksa_definitions_page", "ok": True},
        {
            "name": "ksa_definitions_api",
            "ok": True,
            "ksa_definition_summary": valid_ksa_definition_summary(),
        },
        {"name": "provenance_reconfirmation_page", "ok": True},
        {"name": "provenance_reconfirmation_api", "ok": True},
        {"name": "agent_queue_page", "ok": True},
        {"name": "query_router_page", "ok": True},
        {"name": "queue_status_page", "ok": True},
        {
            "name": "queue_status_api",
            "ok": True,
            "source_queue_path": "reports/aihr_agent_queue_20260624.json",
            "summary": {
                "item_count": 3,
                "blocked_count": 0,
                "manual_ready_count": 2,
                "auto_startable_count": 1,
                "state_counts": {"ready_to_start": 1, "manual_ready": 2},
            },
            "human_gated_execution": [],
            "unsafe_manual_items": [],
            "guarded_manual_items": ["manual-human", "manual-guarded"],
        },
        {"name": "agent_queue_run_page", "ok": True},
        {
            "name": "agent_queue_run_api",
            "ok": True,
            "source_queue_path": "reports/aihr_agent_queue_20260624.json",
            "actual_run": True,
            "output_tails_suppressed": True,
            "output_issues": [],
        },
        {
            "name": "live_queue_source_path_consistency",
            "ok": True,
            "detail": "source_queue_path_matches_release_queue",
            "release_readiness_queue_path": "reports/aihr_agent_queue_20260624.json",
            "queue_status_api_source_queue_path": "reports/aihr_agent_queue_20260624.json",
            "agent_queue_run_api_source_queue_path": "reports/aihr_agent_queue_20260624.json",
            "bad_queue_source": [],
        },
    ]


def valid_ksa_definition_summary() -> dict:
    return {
        "schema": "ncs_ksa_definition_dashboard_v1",
        "item_count": 1,
        "matching_ksa": 1124,
        "llm_reviewed_label_concepts": 1118,
        "trusted_label_candidate_concepts": 1118,
        "label_review_status_counts": {"llm_reviewed": 1124},
        "first_short_label_status": "llm_reviewed",
        "first_review_priority": "machine_reviewed",
        "missing_item_fields": [],
        "raw_to_label_visible": True,
        "source_provenance_visible": True,
        "status_update_allowed": False,
        "raw_ksa_preserved": True,
    }


def valid_review_chain_safety_summary() -> dict:
    return {
        "schema": "aihr_plan_review_workflow_handoff_v1",
        "contract_ok": True,
        "approval_claim": False,
        "db_writes": False,
        "status_update_allowed": False,
        "source_payload_exposed": False,
        "do_not_set_human_reviewed_accepted_reviewed_automatically": True,
        "human_decision_required_for_status_update": True,
        "review_surface_contract_source": "nested_unit_triage",
        "next_request_unit_triage_present": True,
        "nested_review_surface_contract_present": True,
        "packet_index_exists": True,
        "packet_index_non_empty": True,
        "packet_index_contract_ok": True,
        "learning_module_visible_items": 3,
        "ncs_report_visible_items": 59,
        "ocr_context_card_count": 15,
        "blocked_automation_actions": [
            "auto_approve",
            "score_boost_from_report_or_derived_diagnostics",
            "treat_report_training_as_official_learning_module",
            "write_human_reviewed_accepted_or_reviewed",
        ],
        "missing_blocked_automation_actions": [],
        "issues": [],
    }


def valid_aihr_demo_contract() -> dict:
    return {"ok": True, "failure_count": 0, "failures": []}


def valid_dashboard_surface_contract() -> dict:
    return {"ok": True, "failure_count": 0, "failures": []}


def required_quality_gates(*, human_review_status: str = "pass", qualification_value: float = 1.0) -> list[dict]:
    return [
        {
            "name": "review_debt:candidate_definition_ratio",
            "status": human_review_status,
            "message": "candidate definition ratio gate",
            "value": 0 if human_review_status == "pass" else 1,
            "threshold": "pass",
        },
        {
            "name": "review_debt:human_reviewed_concepts",
            "status": human_review_status,
            "message": "human reviewed concepts gate",
            "value": 1 if human_review_status == "pass" else 0,
            "threshold": "> 0",
        },
        {
            "name": "review_debt:human_reviewed_goal_links",
            "status": human_review_status,
            "message": "human reviewed goal links gate",
            "value": 1 if human_review_status == "pass" else 0,
            "threshold": "> 0",
        },
        {
            "name": "review_debt:human_reviewed_task_relations",
            "status": human_review_status,
            "message": "human reviewed task relations gate",
            "value": 1 if human_review_status == "pass" else 0,
            "threshold": "> 0",
        },
        {
            "name": "qualification:collection_coverage",
            "status": "pass" if qualification_value >= 0.9 else "warn",
            "message": "Qualification coverage gate.",
            "value": qualification_value,
            "threshold": ">= 0.90",
        },
        {
            "name": "transition_eval:trusted_scenarios",
            "status": "pass",
            "message": "Trusted transition scenarios gate.",
            "value": 10,
            "threshold": ">= 10",
        },
    ]


class ReleaseReadinessReportTests(unittest.TestCase):
    def test_dashboard_surface_contract_validates_live_scenarios_and_queue_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"])
        self.assertEqual(contract["failure_count"], 0)
        self.assertEqual(contract["artifact"]["scenario_count"], 2)
        self.assertEqual(len(contract["artifact"]["live_plan_summaries"]), 2)
        self.assertEqual(len(contract["artifact"]["static_artifacts"]), 27)
        self.assertTrue(contract["artifact"]["review_chain_safety_summary"]["contract_ok"])
        self.assertFalse(contract["artifact"]["review_chain_safety_summary"]["source_payload_exposed"])
        self.assertIn(
            "Guide surface audit artifact",
            {check["name"] for check in contract["artifact"]["checks"]},
        )
        self.assertIn(
            "Review chain safety contract",
            {check["name"] for check in contract["artifact"]["checks"]},
        )
        self.assertIn(
            "Human review backlog artifact",
            {check["name"] for check in contract["artifact"]["checks"]},
        )
        self.assertIn(
            "Goal completion audit artifact",
            {check["name"] for check in contract["artifact"]["checks"]},
        )
        self.assertIn(
            "KSA definition dashboard contract",
            {check["name"] for check in contract["artifact"]["checks"]},
        )

    def test_dashboard_surface_contract_rejects_stale_qualification_raw_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            static_artifacts = valid_static_artifacts()
            qualification_artifact = next(
                item
                for item in static_artifacts
                if item["name"] == "qualification_collection_coverage_plan_json"
            )
            plan = qualification_artifact["qualification_collection_coverage_plan"]
            plan["raw_batch_count_matches_batches"] = False
            plan["raw_unsafe_batch_count_matches_batches"] = False
            plan["raw_unsafe_batches_count"] = 1
            plan["raw_unsafe_batches_match_batches"] = False
            plan["forbidden_status_updates_exact"] = False
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        static_check = next(
            check
            for check in contract["artifact"]["checks"]
            if check["name"] == "Static artifact snapshot"
        )
        self.assertFalse(static_check["ok"])
        self.assertIn(
            "bad_qualification_coverage_plan=reports/qualification_collection_coverage_plan_20260624.json",
            static_check["detail"],
        )

    def test_dashboard_surface_contract_fails_for_static_artifact_local_path_leaks(self) -> None:
        static_artifacts = valid_static_artifacts()
        demo_json = next(item for item in static_artifacts if item["name"] == "demo_json")
        demo_json["local_path_markers"] = [
            "db_path",
            "configured_workspace_path",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertIn(
            "Static artifact snapshot",
            {failure["check"] for failure in contract["failures"]},
        )

    def test_dashboard_surface_contract_rejects_internal_static_artifact_local_path_metadata(self) -> None:
        static_artifacts = valid_static_artifacts()
        readiness_json = next(item for item in static_artifacts if item["name"] == "readiness_json")
        readiness_json["local_path_markers"] = [
            "configured_workspace_path",
            "ncs_processed_db_path",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertIn(
            "Static artifact snapshot",
            {failure["check"] for failure in contract["failures"]},
        )

    def test_dashboard_surface_contract_rejects_stale_static_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            stale_path = tmp_path / "query_route_contract_audit_20260624.json"
            stale_path.write_text("current artifact body", encoding="utf-8")
            stale_hash = "sha256:" + hashlib.sha256(b"previous artifact body").hexdigest()
            static_artifacts = valid_static_artifacts()
            audit_artifact = next(
                item
                for item in static_artifacts
                if item["name"] == "query_route_contract_audit_json"
            )
            audit_artifact["path"] = str(stale_path)
            audit_artifact["content_sha256"] = stale_hash
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        freshness_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Static artifact freshness"
        )
        self.assertIn("query_route_contract_audit_json:sha256_mismatch", freshness_failure["detail"])
        self.assertEqual(
            contract["artifact"]["stale_static_artifacts"],
            ["query_route_contract_audit_json:sha256_mismatch"],
        )

    def test_dashboard_surface_contract_resolves_root_relative_static_artifacts_from_any_cwd(self) -> None:
        with tempfile.TemporaryDirectory(dir=ROOT) as artifact_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
            artifact_path = Path(artifact_tmp) / "query_route_contract_audit_20260624.json"
            artifact_body = b"current artifact body"
            artifact_path.write_bytes(artifact_body)
            static_artifacts = valid_static_artifacts()
            audit_artifact = next(
                item
                for item in static_artifacts
                if item["name"] == "query_route_contract_audit_json"
            )
            audit_artifact["path"] = artifact_path.relative_to(ROOT).as_posix()
            audit_artifact["content_sha256"] = "sha256:" + hashlib.sha256(artifact_body).hexdigest()
            dashboard_path = Path(cwd_tmp) / "dashboard_verification_20260624.json"
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with contextlib.chdir(cwd_tmp):
                contract = build_dashboard_surface_contract(dashboard_path)

        self.assertNotIn(
            "query_route_contract_audit_json:sha256_mismatch",
            contract["artifact"]["stale_static_artifacts"],
        )
        self.assertNotIn(
            "query_route_contract_audit_json:current_file_unreadable",
            " ".join(contract["artifact"]["stale_static_artifacts"]),
        )

    def test_dashboard_verification_lineage_rechecks_dashboard_artifact_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            dashboard_path = tmp_path / "dashboard_verification_20260624.json"
            payload = {
                "ok": True,
                "schema": "aihr_dashboard_surface_verification_v1",
                "scenario_count": 2,
                "checks": valid_dashboard_endpoint_checks(),
                "queue_status_summary": {"blocked_count": 0},
                "static_artifacts": valid_static_artifacts(),
                "live_plan_summaries": [
                    valid_live_plan_summary("baseline"),
                    valid_live_plan_summary("extra"),
                ],
            }
            dashboard_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )
            contract = build_dashboard_surface_contract(dashboard_path)

            lineage = _dashboard_verification_lineage_contract(
                contract,
                release_readiness_path="reports/aihr_release_readiness_20260624.json",
                agent_queue_path="reports/aihr_agent_queue_20260624.json",
            )

            self.assertTrue(lineage["ok"])
            self.assertTrue(lineage["dashboard_verification_content_hash_ok"])
            self.assertRegex(
                lineage["dashboard_verification_content_sha256"],
                r"^sha256:[0-9a-f]{64}$",
            )

            dashboard_path.write_text(
                json.dumps({**payload, "scenario_count": 1}, ensure_ascii=False),
                encoding="utf-8",
            )
            stale_lineage = _dashboard_verification_lineage_contract(
                contract,
                release_readiness_path="reports/aihr_release_readiness_20260624.json",
                agent_queue_path="reports/aihr_agent_queue_20260624.json",
            )

        self.assertFalse(stale_lineage["ok"])
        self.assertFalse(stale_lineage["dashboard_verification_content_hash_ok"])
        self.assertEqual(
            stale_lineage["dashboard_verification_content_hash_issue"],
            "dashboard_verification_sha256_mismatch",
        )

    def test_dashboard_surface_contract_rechecks_queue_artifact_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            static_artifacts = valid_static_artifacts()
            for artifact_name, file_name in [
                ("readiness_json", "aihr_release_readiness_20260624.json"),
                ("queue_status_json", "aihr_agent_queue_status_20260624.json"),
                ("queue_run_json", "aihr_agent_queue_run_20260624.json"),
            ]:
                artifact_path = tmp_path / file_name
                artifact_path.write_text(f"new {artifact_name} body", encoding="utf-8")
                stale_hash = (
                    "sha256:"
                    + hashlib.sha256(f"previous {artifact_name} body".encode("utf-8")).hexdigest()
                )
                artifact = next(item for item in static_artifacts if item["name"] == artifact_name)
                artifact["path"] = str(artifact_path)
                artifact["content_sha256"] = stale_hash
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertEqual(contract["artifact"]["stale_static_artifacts"], [])
        self.assertEqual(
            contract["artifact"]["freshness_hash_skip_names"],
            ["queue_run_json", "queue_status_json", "readiness_json"],
        )
        self.assertIn(
            "cycle_aware_release_dashboard_reference",
            contract["artifact"]["freshness_hash_skip_reason"]["readiness_json"],
        )
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact freshness", failure_names)
        self.assertIn("Queue status artifact", failure_names)
        self.assertIn("Queue run artifact", failure_names)
        queue_status_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue status artifact"
        )
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("queue_status_json:sha256_mismatch", queue_status_failure["detail"])
        self.assertIn("queue_run_json:sha256_mismatch", queue_run_failure["detail"])

    def test_dashboard_surface_contract_rechecks_readiness_cycle_safe_hash(self) -> None:
        for hash_mutation, expected_issue in [
            ("missing", "readiness_json:cycle_safe_content_sha256_missing"),
            ("invalid", "readiness_json:cycle_safe_content_sha256_invalid"),
            ("stale", "readiness_json:cycle_safe_content_sha256_mismatch"),
        ]:
            with self.subTest(hash_mutation=hash_mutation):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
                    readiness_payload = {
                        "schema": "aihr_release_readiness_v1",
                        "ok": True,
                        "release_ready": False,
                        "agent_work_queue_path": "reports/aihr_agent_queue_20260624.json",
                        "dashboard_surface_contract": {
                            "artifact": {"content_sha256": "sha256:" + ("0" * 64)}
                        },
                        "artifact_lineage_contract": {
                            "dashboard_verification_content_sha256": "sha256:" + ("1" * 64)
                        },
                    }
                    readiness_path.write_text(
                        json.dumps(readiness_payload, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    static_artifacts = valid_static_artifacts()
                    readiness_artifact = next(
                        item for item in static_artifacts if item["name"] == "readiness_json"
                    )
                    readiness_artifact["path"] = str(readiness_path)
                    readiness_artifact["content_sha256"] = (
                        "sha256:" + hashlib.sha256(b"raw hash is cycle-aware skipped").hexdigest()
                    )
                    if hash_mutation == "missing":
                        readiness_artifact.pop("cycle_safe_content_sha256", None)
                    elif hash_mutation == "invalid":
                        readiness_artifact["cycle_safe_content_sha256"] = "not-a-sha256"
                    else:
                        readiness_artifact["cycle_safe_content_sha256"] = (
                            "sha256:" + hashlib.sha256(b"previous projection").hexdigest()
                        )
                    path = tmp_path / "dashboard_verification.json"
                    path.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "schema": "aihr_dashboard_surface_verification_v1",
                                "scenario_count": 2,
                                "checks": valid_dashboard_endpoint_checks(),
                                "queue_status_summary": {"blocked_count": 0},
                                "static_artifacts": static_artifacts,
                                "live_plan_summaries": [
                                    valid_live_plan_summary("baseline"),
                                    valid_live_plan_summary("extra"),
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )

                    contract = build_dashboard_surface_contract(path)

                self.assertFalse(contract["ok"])
                snapshot_failure = next(
                    failure
                    for failure in contract["failures"]
                    if failure["check"] == "Static artifact snapshot"
                )
                self.assertIn(expected_issue, snapshot_failure["detail"])

    def test_dashboard_surface_contract_allows_readiness_cycle_field_changes_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            readiness_payload = {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "agent_work_queue_path": "reports/aihr_agent_queue_20260624.json",
                "dashboard_surface_contract": {
                    "ok": True,
                    "artifact": {
                        "content_sha256": "sha256:" + ("0" * 64),
                        "mtime_utc": "2026-06-24T00:00:00+00:00",
                    },
                },
                "artifact_lineage_contract": {
                    "dashboard_verification_content_sha256": "sha256:" + ("1" * 64)
                },
            }
            readiness_path.write_text(
                json.dumps(
                    {
                        **readiness_payload,
                        "dashboard_surface_contract": {
                            "ok": True,
                            "artifact": {
                                "content_sha256": "sha256:" + ("0" * 64),
                                "mtime_utc": "2026-06-24T01:00:00+00:00",
                            },
                        },
                        "artifact_lineage_contract": {
                            "dashboard_verification_content_sha256": "sha256:" + ("3" * 64)
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            static_artifacts = valid_static_artifacts()
            readiness_artifact = next(
                item for item in static_artifacts if item["name"] == "readiness_json"
            )
            readiness_artifact["path"] = str(readiness_path)
            readiness_artifact["content_sha256"] = (
                "sha256:" + hashlib.sha256(b"raw hash is cycle-aware skipped").hexdigest()
            )
            readiness_artifact["cycle_safe_content_sha256"] = (
                _release_readiness_cycle_safe_sha256(readiness_payload)
            )
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            dashboard_hash = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            readiness_path.write_text(
                json.dumps(
                    {
                        **readiness_payload,
                        "dashboard_surface_contract": {
                            "ok": True,
                            "artifact": {
                                "content_sha256": "sha256:" + ("0" * 64),
                                "mtime_utc": "2026-06-24T01:00:00+00:00",
                            },
                        },
                        "artifact_lineage_contract": {
                            "dashboard_verification_path": str(path),
                            "dashboard_verification_content_sha256": dashboard_hash,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact snapshot", failure_names)
        self.assertNotIn("Static artifact freshness", failure_names)

    def test_release_cycle_safe_hash_ignores_nested_dashboard_digest_metadata(
        self,
    ) -> None:
        payload = {
            "schema": "aihr_release_readiness_v1",
            "ok": True,
            "dashboard_surface_contract": {
                "ok": True,
                "artifact": {
                    "size_bytes": 100,
                    "mtime_utc": "2026-06-24T00:00:00+00:00",
                    "static_artifacts": [
                        {
                            "name": "readiness_json",
                            "size_bytes": 200,
                            "mtime_utc": "2026-06-24T00:00:00+00:00",
                            "content_sha256": "sha256:" + ("1" * 64),
                            "cycle_safe_content_sha256": "sha256:" + ("2" * 64),
                        }
                    ],
                },
            },
        }
        changed_mtime = json.loads(json.dumps(payload, ensure_ascii=False))
        changed_mtime["dashboard_surface_contract"]["artifact"]["mtime_utc"] = (
            "2026-06-24T01:00:00+00:00"
        )
        changed_mtime["dashboard_surface_contract"]["artifact"]["static_artifacts"][0][
            "mtime_utc"
        ] = "2026-06-24T01:00:00+00:00"
        changed_mtime["dashboard_surface_contract"]["artifact"]["size_bytes"] = 101
        changed_mtime["dashboard_surface_contract"]["artifact"]["static_artifacts"][0][
            "size_bytes"
        ] = 201
        changed_hash = json.loads(json.dumps(changed_mtime, ensure_ascii=False))
        changed_hash["dashboard_surface_contract"]["artifact"]["static_artifacts"][0][
            "content_sha256"
        ] = "sha256:" + ("3" * 64)
        changed_hash["dashboard_surface_contract"]["artifact"]["static_artifacts"][0][
            "cycle_safe_content_sha256"
        ] = "sha256:" + ("4" * 64)

        self.assertEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed_mtime),
        )
        self.assertEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed_hash),
        )

    def test_release_cycle_safe_hash_ignores_backlog_self_revalidation(
        self,
    ) -> None:
        payload = {
            "schema": "aihr_release_readiness_v1",
            "ok": True,
            "dashboard_surface_contract": {
                "ok": True,
                "artifact": {
                    "static_artifacts": [
                        {
                            "name": "human_review_backlog_json",
                            "human_review_backlog": {
                                "schema": "aihr_human_review_backlog_v1",
                                "contract_ok": True,
                                "all_seedpacks_safe": True,
                                "source_hash_contract_ok": True,
                                "source_hash_revalidation_ok": True,
                                "source_hash_revalidation_checked_count": 10,
                                "source_hash_revalidation_mismatch_count": 0,
                                "source_hash_revalidation_issues": [],
                                "source_release_hash_scope": "cycle_safe_release_readiness",
                                "source_release_cycle_safe_hash_present": True,
                                "queue_input_hash_count": 4,
                            },
                        }
                    ],
                },
            },
        }
        changed_backlog_self_check = json.loads(json.dumps(payload, ensure_ascii=False))
        backlog = changed_backlog_self_check["dashboard_surface_contract"]["artifact"][
            "static_artifacts"
        ][0]["human_review_backlog"]
        backlog["contract_ok"] = False
        backlog["source_hash_contract_ok"] = False
        backlog["source_hash_revalidation_ok"] = False
        backlog["source_hash_revalidation_mismatch_count"] = 1
        backlog["source_hash_revalidation_issues"] = [
            {"code": "release_readiness_hash_mismatch"}
        ]

        self.assertEqual(
            _release_readiness_cycle_safe_sha256(payload),
            _release_readiness_cycle_safe_sha256(changed_backlog_self_check),
        )

    def test_dashboard_surface_contract_rejects_stale_readiness_artifact_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            readiness_payload = {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "agent_work_queue_path": "reports/aihr_agent_queue_20260624.json",
                "dashboard_surface_contract": {
                    "artifact": {"content_sha256": "sha256:" + ("0" * 64)}
                },
                "artifact_lineage_contract": {
                    "dashboard_verification_path": str(tmp_path / "dashboard_verification.json"),
                    "dashboard_verification_content_sha256": "sha256:" + ("1" * 64),
                },
            }
            readiness_path.write_text(
                json.dumps(readiness_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            static_artifacts = valid_static_artifacts()
            readiness_artifact = next(
                item for item in static_artifacts if item["name"] == "readiness_json"
            )
            readiness_artifact["path"] = str(readiness_path)
            readiness_artifact["content_sha256"] = (
                "sha256:" + hashlib.sha256(b"raw hash is cycle-aware skipped").hexdigest()
            )
            readiness_artifact["cycle_safe_content_sha256"] = (
                _release_readiness_cycle_safe_sha256(readiness_payload)
            )
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        snapshot_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Static artifact snapshot"
        )
        self.assertIn(
            "artifact_lineage_dashboard_verification_sha256_mismatch",
            snapshot_failure["detail"],
        )

    def test_dashboard_surface_contract_defers_pending_release_readiness_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            readiness_path = tmp_path / "aihr_release_readiness_20260624.json"
            snapshot_readiness_payload = {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "agent_work_queue_path": "reports/aihr_agent_queue_20260624.json",
                "blockers": [],
                "artifact_lineage_contract": {
                    "dashboard_verification_path": str(tmp_path / "old_dashboard.json"),
                    "dashboard_verification_content_sha256": "sha256:" + ("1" * 64),
                },
            }
            current_readiness_payload = {
                **snapshot_readiness_payload,
                "blockers": [{"name": "aihr_dashboard_surface"}],
            }
            readiness_path.write_text(
                json.dumps(current_readiness_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            static_artifacts = valid_static_artifacts()
            readiness_artifact = next(
                item for item in static_artifacts if item["name"] == "readiness_json"
            )
            readiness_artifact["path"] = str(readiness_path)
            readiness_artifact["content_sha256"] = (
                "sha256:" + hashlib.sha256(b"raw hash is cycle-aware skipped").hexdigest()
            )
            readiness_artifact["cycle_safe_content_sha256"] = (
                _release_readiness_cycle_safe_sha256(snapshot_readiness_payload)
            )
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(
                path,
                pending_release_readiness_path=readiness_path,
            )

        self.assertTrue(contract["ok"], contract["failures"])
        artifact = contract["artifact"]
        deferred = artifact.get("deferred_readiness_snapshot_issues") or []
        self.assertTrue(
            any("cycle_safe_content_sha256_mismatch" in issue for issue in deferred),
            deferred,
        )
        self.assertTrue(
            any("artifact_lineage_dashboard_verification_sha256_mismatch" in issue for issue in deferred),
            deferred,
        )
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_rejects_missing_or_invalid_queue_artifact_hashes(self) -> None:
        for hash_value, expected_issue in [
            (None, "content_sha256_missing"),
            ("not-a-sha256", "content_sha256_invalid"),
        ]:
            with self.subTest(expected_issue=expected_issue):
                with tempfile.TemporaryDirectory() as tmp:
                    tmp_path = Path(tmp)
                    static_artifacts = valid_static_artifacts()
                    for artifact_name, file_name in [
                        ("queue_status_json", "aihr_agent_queue_status_20260624.json"),
                        ("queue_run_json", "aihr_agent_queue_run_20260624.json"),
                    ]:
                        artifact_path = tmp_path / file_name
                        artifact_path.write_text(f"{artifact_name} body", encoding="utf-8")
                        artifact = next(
                            item for item in static_artifacts if item["name"] == artifact_name
                        )
                        artifact["path"] = str(artifact_path)
                        if hash_value is None:
                            artifact.pop("content_sha256", None)
                        else:
                            artifact["content_sha256"] = hash_value
                    path = tmp_path / "dashboard_verification.json"
                    path.write_text(
                        json.dumps(
                            {
                                "ok": True,
                                "schema": "aihr_dashboard_surface_verification_v1",
                                "scenario_count": 2,
                                "checks": valid_dashboard_endpoint_checks(),
                                "queue_status_summary": {"blocked_count": 0},
                                "static_artifacts": static_artifacts,
                                "live_plan_summaries": [
                                    valid_live_plan_summary("baseline"),
                                    valid_live_plan_summary("extra"),
                                ],
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )

                    contract = build_dashboard_surface_contract(path)

                self.assertFalse(contract["ok"])
                queue_status_failure = next(
                    failure
                    for failure in contract["failures"]
                    if failure["check"] == "Queue status artifact"
                )
                queue_run_failure = next(
                    failure
                    for failure in contract["failures"]
                    if failure["check"] == "Queue run artifact"
                )
                self.assertIn(f"queue_status_json:{expected_issue}", queue_status_failure["detail"])
                self.assertIn(f"queue_run_json:{expected_issue}", queue_run_failure["detail"])

    def test_dashboard_surface_contract_rejects_self_attested_all_major_api_linkage_scope(self) -> None:
        static_artifacts = valid_static_artifacts()
        api_artifact = next(
            item for item in static_artifacts if item["name"] == "api_linkage_summary_json"
        )
        summary = api_artifact["api_linkage_summary"]
        summary["major_count"] = 1
        summary["unit_count"] = 14
        summary["qualification_coverage_plan_hint"].update(
            {
                "scope": "all_majors",
                "scope_major_codes": ["02"],
                "total_unit_count": 14,
                "attempted_unit_count": 14,
                "collection_coverage": 1.0,
                "additional_attempted_units_needed": 0,
                "estimated_batch_count": 0,
                "coverage_plan_matches_summary_scope": True,
                "coverage_plan_command_present": True,
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        snapshot_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Static artifact snapshot"
        )
        self.assertIn("bad_api_linkage_summary", snapshot_failure["detail"])

    def test_dashboard_surface_contract_rejects_provenance_reconfirmation_lineage_mismatch(self) -> None:
        static_artifacts = valid_static_artifacts()
        sheet = next(
            item
            for item in static_artifacts
            if item["name"] == "human_review_provenance_reconfirmation_decision_sheet_json"
        )
        sheet["human_review_provenance_reconfirmation_decision_sheet"][
            "source_packet_sha256"
        ] = "sha256:different-packet"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertIn(
            "Static artifact snapshot",
            {failure["check"] for failure in contract["failures"]},
        )

    def test_dashboard_surface_contract_allows_review_chain_without_optional_basis_counts(self) -> None:
        endpoint_checks = []
        summary = valid_review_chain_safety_summary()
        summary["learning_module_visible_items"] = None
        summary["ncs_report_visible_items"] = None
        summary["ocr_context_card_count"] = 0
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"])
        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertTrue(checks["Review chain safety contract"]["ok"])

    def test_dashboard_surface_contract_allows_root_review_chain_when_unit_triage_absent(self) -> None:
        endpoint_checks = []
        summary = valid_review_chain_safety_summary()
        summary["review_surface_contract_source"] = "root_fallback_no_unit_triage"
        summary["next_request_unit_triage_present"] = False
        summary["nested_review_surface_contract_present"] = False
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertTrue(checks["Review chain safety contract"]["ok"])

    def test_dashboard_surface_contract_rejects_review_chain_without_contract_provenance(self) -> None:
        endpoint_checks = []
        summary = valid_review_chain_safety_summary()
        summary.pop("review_surface_contract_source")
        summary.pop("next_request_unit_triage_present")
        summary.pop("nested_review_surface_contract_present")
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["Review chain safety contract"]["ok"])
        self.assertIn("provenance_issues", checks["Review chain safety contract"]["detail"])

    def test_dashboard_surface_contract_rejects_root_review_chain_when_unit_triage_present(self) -> None:
        endpoint_checks = []
        summary = valid_review_chain_safety_summary()
        summary["review_surface_contract_source"] = "root_fallback_no_unit_triage"
        summary["next_request_unit_triage_present"] = True
        summary["nested_review_surface_contract_present"] = False
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["Review chain safety contract"]["ok"])
        self.assertIn("nested_review_surface_contract_missing", checks["Review chain safety contract"]["detail"])

    def test_dashboard_surface_contract_rejects_review_chain_packet_index_gap(self) -> None:
        endpoint_checks = []
        summary = valid_review_chain_safety_summary()
        summary["packet_index_exists"] = False
        summary["packet_index_non_empty"] = False
        summary["packet_index_contract_ok"] = False
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["Review chain safety contract"]["ok"])
        self.assertIn("packet_index_contract_ok=False", checks["Review chain safety contract"]["detail"])

    def test_dashboard_surface_contract_rejects_top_level_review_chain_provenance_mismatch(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": valid_review_chain_safety_summary(),
                }
            endpoint_checks.append(item)
        top_level_summary = valid_review_chain_safety_summary()
        top_level_summary["rows_without_packet_backed_provenance"] = 2
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "review_chain_safety_summary": top_level_summary,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["Review chain safety contract"]["ok"])
        self.assertIn("rows_without_packet_backed_provenance", checks["Review chain safety contract"]["detail"])

    def test_dashboard_surface_contract_allows_safe_blocked_queue_status(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "queue_status_api":
                item = item | {
                    "summary": {
                        "item_count": 5,
                        "blocked_count": 2,
                        "manual_ready_count": 2,
                        "auto_startable_count": 1,
                        "state_counts": {
                            "blocked_missing_prerequisites": 1,
                            "blocked_safety": 1,
                            "ready_to_start": 1,
                            "manual_ready": 2,
                        },
                    }
                }
            endpoint_checks.append(item)
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "queue_status_json":
                item["queue_status"]["blocked_count"] = 2
                item["queue_status"]["queue_ready"] = False
                item["queue_status"]["state_counts"] = {
                    "blocked_missing_prerequisites": 1,
                    "blocked_safety": 1,
                    "ready_to_start": 1,
                    "manual_ready": 2,
                }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 2},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"])
        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertTrue(checks["Queue status is guarded"]["ok"])
        self.assertEqual(contract["artifact"]["queue_status_summary"]["blocked_count"], 2)

    def test_dashboard_surface_contract_fails_for_missing_queue_run_actual_contract(self) -> None:
        endpoint_checks = [
            {key: value for key, value in item.items() if key not in {"actual_run", "output_tails_suppressed"}}
            if item.get("name") == "agent_queue_run_api"
            else item
            for item in valid_dashboard_endpoint_checks()
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue run API actual execution evidence", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_review_chain_safety(self) -> None:
        endpoint_checks = [
            item for item in valid_dashboard_endpoint_checks() if item.get("name") != "review_chain_safety"
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Endpoint checks", failure_names)
        self.assertIn("Review chain safety contract", failure_names)

    def test_dashboard_surface_contract_fails_for_unsafe_ksa_definition_contract(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "ksa_definitions_api":
                bad_summary = valid_ksa_definition_summary()
                bad_summary["status_update_allowed"] = True
                bad_summary["missing_item_fields"] = ["short_label_candidate"]
                item = {
                    "name": "ksa_definitions_api",
                    "ok": True,
                    "ksa_definition_summary": bad_summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("KSA definition dashboard contract", failure_names)

    def test_dashboard_surface_contract_prefers_endpoint_ksa_summary_over_top_level(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "ksa_definitions_api":
                bad_summary = valid_ksa_definition_summary()
                bad_summary["status_update_allowed"] = True
                bad_summary["source_provenance_visible"] = False
                item = {
                    "name": "ksa_definitions_api",
                    "ok": True,
                    "ksa_definition_summary": bad_summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "ksa_definition_summary": valid_ksa_definition_summary(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["KSA definition dashboard contract"]["ok"])
        self.assertIn("summary_mismatches", checks["KSA definition dashboard contract"]["detail"])

    def test_dashboard_surface_contract_fails_for_unsafe_review_chain_safety(self) -> None:
        summary = valid_review_chain_safety_summary()
        summary["source_payload_exposed"] = True
        summary["issues"] = ["source_payload_exposed"]
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": False,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Endpoint checks", failure_names)
        self.assertIn("Review chain safety contract", failure_names)

    def test_dashboard_surface_contract_prefers_endpoint_review_chain_over_top_level(self) -> None:
        summary = valid_review_chain_safety_summary()
        summary["source_payload_exposed"] = True
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "review_chain_safety":
                item = {
                    "name": "review_chain_safety",
                    "ok": True,
                    "review_chain_safety_summary": summary,
                }
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "review_chain_safety_summary": valid_review_chain_safety_summary(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        checks = {check["name"]: check for check in contract["artifact"]["checks"]}
        self.assertFalse(checks["Review chain safety contract"]["ok"])
        self.assertIn("summary_mismatches", checks["Review chain safety contract"]["detail"])

    def test_dashboard_surface_contract_fails_for_dryrun_queue_run_contract(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "agent_queue_run_api":
                item = item | {"actual_run": False, "output_tails_suppressed": True, "output_issues": []}
            endpoint_checks.append(item)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue run API actual execution evidence", failure_names)

    def test_dashboard_surface_contract_fails_when_queue_run_has_failed_status(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "agent_queue_run_api":
                item = item | {
                    "actual_run": True,
                    "output_tails_suppressed": True,
                    "output_issues": [],
                    "summary": {
                        "selected_count": 1,
                        "failed_count": 1,
                        "acceptance_failed_count": 1,
                    },
                    "run_statuses": ["failed"],
                }
            endpoint_checks.append(item)
        static_artifacts = valid_static_artifacts()
        queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
        queue_run["queue_run"]["failed_count"] = 1
        queue_run["queue_run"]["acceptance_failed_count"] = 1
        queue_run["queue_run"]["run_statuses"] = ["failed"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue run API actual execution evidence", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_stale_public_queue_run_artifact(self) -> None:
        static_artifacts = valid_static_artifacts()
        queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
        queue_run["path"] = "reports/aihr_agent_queue_run_20260624_public.json"
        queue_run["queue_run_public_sync"] = {
            "checked": True,
            "private_exists": True,
            "private_path": "reports/aihr_agent_queue_run_20260624.json",
            "private_newer": True,
            "public_is_current": False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact snapshot", failure_names)
        self.assertIn("Queue run artifact", failure_names)
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("public_stale_private_newer", queue_run_failure["detail"])

    def test_dashboard_surface_contract_detects_stale_public_queue_run_without_inline_sync(self) -> None:
        queue_payload = {
            "schema": "aihr_agent_queue_run_v1",
            "source_queue_path": "reports/aihr_agent_queue_20260624.json",
            "summary": {
                "dry_run": False,
                "dry_run_count": 0,
                "selected_count": 1,
                "failed_count": 0,
                "acceptance_failed_count": 0,
                "skipped_unsafe_count": 0,
            },
            "runs": [{"status": "succeeded"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            public_path = tmp_path / "aihr_agent_queue_run_20260624_public.json"
            private_path = tmp_path / "aihr_agent_queue_run_20260624.json"
            public_path.write_text(json.dumps(queue_payload, ensure_ascii=False), encoding="utf-8")
            private_path.write_text(json.dumps(queue_payload, ensure_ascii=False), encoding="utf-8")
            os.utime(public_path, (1704067200, 1704067200))
            os.utime(private_path, (1704067300, 1704067300))
            static_artifacts = valid_static_artifacts()
            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run["path"] = str(public_path)
            queue_run.pop("queue_run_public_sync", None)
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("public_stale_private_newer", queue_run_failure["detail"])

    def test_dashboard_surface_contract_detects_queue_run_source_queue_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_queue = tmp_path / "aihr_agent_queue_20260624.json"
            source_queue.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[]}\n',
                encoding="utf-8",
            )
            original_hash = "sha256:" + hashlib.sha256(source_queue.read_bytes()).hexdigest()
            source_queue.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[{"id":"changed"}]}\n',
                encoding="utf-8",
            )
            static_artifacts = valid_static_artifacts()
            queue_status = next(item for item in static_artifacts if item["name"] == "queue_status_json")
            queue_status["queue_status"]["source_queue_path"] = str(source_queue)
            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run["queue_run"]["source_queue_path"] = str(source_queue)
            queue_run["queue_run"]["source_queue_sha256"] = original_hash
            queue_run["queue_run_source_queue_sync"] = {
                "checked": True,
                "source_queue_path": str(source_queue),
                "source_queue_exists": True,
                "source_queue_sha256": original_hash,
                "current_source_queue_sha256": (
                    "sha256:" + hashlib.sha256(source_queue.read_bytes()).hexdigest()
                ),
                "source_queue_matches_run": False,
            }
            readiness = next(item for item in static_artifacts if item["name"] == "readiness_json")
            readiness["release_readiness"]["agent_work_queue_path"] = str(source_queue)
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("source_queue_hash_mismatch", queue_run_failure["detail"])

    def test_dashboard_surface_contract_fails_when_source_queue_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            missing_source_queue = "reports/does_not_exist.json"
            queue_status_file = tmp_path / "aihr_agent_queue_status_20260624.json"
            queue_run_file = tmp_path / "aihr_agent_queue_run_20260624.json"
            queue_status_file.write_text('{"ok":true}\n', encoding="utf-8")
            queue_run_file.write_text('{"ok":true}\n', encoding="utf-8")

            static_artifacts = valid_static_artifacts()
            queue_status = next(
                item for item in static_artifacts if item["name"] == "queue_status_json"
            )
            queue_status["path"] = str(queue_status_file)
            queue_status["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_status_file.read_bytes()).hexdigest()
            )
            queue_status["queue_status"]["source_queue_path"] = missing_source_queue

            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run["path"] = str(queue_run_file)
            queue_run["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_run_file.read_bytes()).hexdigest()
            )
            queue_run["queue_run"]["source_queue_path"] = missing_source_queue
            queue_run.pop("queue_run_source_queue_sync", None)
            queue_run.pop("queue_status_snapshot_sync", None)

            readiness = next(item for item in static_artifacts if item["name"] == "readiness_json")
            readiness["release_readiness"]["agent_work_queue_path"] = missing_source_queue

            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        source_contract_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Queue source artifact contract"
        )
        self.assertIn("source_queue_missing", source_contract_failure["detail"])

    def test_dashboard_surface_contract_prefers_bundle_queue_over_repo_collision(self) -> None:
        with (
            tempfile.TemporaryDirectory(dir=ROOT / "reports") as repo_tmp,
            tempfile.TemporaryDirectory() as bundle_tmp,
        ):
            repo_tmp_path = Path(repo_tmp)
            source_ref = repo_tmp_path.relative_to(ROOT) / "queue.json"
            repo_queue_path = ROOT / source_ref
            repo_queue_path.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[{"id":"repo-copy"}]}\n',
                encoding="utf-8",
            )

            bundle_queue_dir = Path(bundle_tmp) / source_ref.parent
            bundle_queue_dir.mkdir(parents=True, exist_ok=True)
            bundle_queue_path = bundle_queue_dir / source_ref.name
            source_queue_payload = {
                "schema": "aihr_agent_work_queue_v1",
                "report_only": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "generated_at_basis": "artifact_date",
                "generated_at": "2026-06-24T00:00:00+00:00",
                "item_count": 0,
                "input_artifact_hashes": {},
                "input_artifact_hash_count": 0,
                "items": [],
            }
            bundle_queue_path.write_text(
                json.dumps(source_queue_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            expected_status = build_agent_queue_status_from_file(
                bundle_queue_path,
                workspace=ROOT,
            )
            expected_status_sha = (
                "sha256:"
                + hashlib.sha256(
                    json.dumps(
                        expected_status,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            )

            static_artifacts = valid_static_artifacts()
            queue_status = next(
                item for item in static_artifacts if item["name"] == "queue_status_json"
            )
            queue_status_file = bundle_queue_dir / "aihr_agent_queue_status_20260624.json"
            queue_status_file.write_text('{"ok":true}\n', encoding="utf-8")
            queue_status["path"] = str(queue_status_file)
            queue_status["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_status_file.read_bytes()).hexdigest()
            )
            queue_status["queue_status"]["source_queue_path"] = str(source_ref)

            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run_file = bundle_queue_dir / "aihr_agent_queue_run_20260624.json"
            queue_run["path"] = str(queue_run_file)
            queue_run["queue_run"]["source_queue_path"] = str(source_ref)
            queue_run["queue_run"]["source_queue_sha256"] = (
                "sha256:" + hashlib.sha256(bundle_queue_path.read_bytes()).hexdigest()
            )
            queue_run["queue_run"]["queue_status_snapshot_sha256"] = expected_status_sha
            queue_run.pop("queue_run_source_queue_sync", None)
            queue_run.pop("queue_status_snapshot_sync", None)
            queue_run_file.write_text(
                json.dumps(queue_run["queue_run"], ensure_ascii=False),
                encoding="utf-8",
            )
            queue_run["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_run_file.read_bytes()).hexdigest()
            )

            readiness = next(item for item in static_artifacts if item["name"] == "readiness_json")
            readiness["release_readiness"]["agent_work_queue_path"] = str(source_ref)

            path = bundle_queue_dir / "dashboard_verification.json"
            endpoint_checks = valid_dashboard_endpoint_checks()
            for check in endpoint_checks:
                if check.get("name") in {"queue_status_api", "agent_queue_run_api"}:
                    check["source_queue_path"] = str(source_ref)
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"], contract["failures"])

    def test_dashboard_surface_contract_detects_queue_run_missing_lineage_hashes(self) -> None:
        static_artifacts = valid_static_artifacts()
        queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
        queue_run["queue_run"].pop("source_queue_sha256", None)
        queue_run["queue_run"].pop("queue_status_snapshot_sha256", None)
        queue_run["queue_run"].pop("lineage_issues", None)
        queue_run.pop("queue_run_source_queue_sync", None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("source_queue_sha256_missing", queue_run_failure["detail"])
        self.assertIn(
            "queue_status_snapshot_sha256_missing",
            queue_run_failure["detail"],
        )

    def test_dashboard_surface_contract_detects_queue_status_snapshot_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_queue = tmp_path / "aihr_agent_queue_20260624.json"
            source_queue.write_text(
                '{"schema":"aihr_agent_work_queue_v1","items":[]}\n',
                encoding="utf-8",
            )
            source_hash = "sha256:" + hashlib.sha256(source_queue.read_bytes()).hexdigest()
            static_artifacts = valid_static_artifacts()
            queue_status = next(item for item in static_artifacts if item["name"] == "queue_status_json")
            queue_status["queue_status"]["source_queue_path"] = str(source_queue)
            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run["queue_run"]["source_queue_path"] = str(source_queue)
            queue_run["queue_run"]["source_queue_sha256"] = source_hash
            queue_run["queue_run"]["queue_status_snapshot_sha256"] = (
                "sha256:"
                "2222222222222222222222222222222222222222222222222222222222222222"
            )
            queue_run.pop("queue_status_snapshot_sync", None)
            readiness = next(item for item in static_artifacts if item["name"] == "readiness_json")
            readiness["release_readiness"]["agent_work_queue_path"] = str(source_queue)
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        queue_run_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue run artifact"
        )
        self.assertIn("queue_status_snapshot_sha256_mismatch", queue_run_failure["detail"])

    def test_dashboard_surface_contract_fails_for_unsafe_manual_queue_items(self) -> None:
        endpoint_checks = []
        for item in valid_dashboard_endpoint_checks():
            if item.get("name") == "queue_status_api":
                item = item | {
                    "ok": False,
                    "unsafe_manual_items": ["manual-without-guard"],
                    "guarded_manual_items": [],
                }
            endpoint_checks.append(item)
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "queue_status_json":
                item["queue_status"]["contract_ok"] = False
                item["queue_status"]["unsafe_manual_items"] = ["manual-without-guard"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("No unsafe human-gated execution items", failure_names)
        self.assertIn("Manual queue guardrails", failure_names)
        self.assertIn("Queue status artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_raw_queue_auto_start_policy_violation(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "queue_status_json":
                item["queue_status"]["items"] = [
                    {
                        "id": "inspect-only",
                        "state": "ready_to_start",
                        "mutation_policy": "inspect_only",
                        "can_start_automated": True,
                        "requires_human_decision": False,
                    }
                ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue raw auto-start contract", failure_names)
        self.assertIn("Queue status raw auto-start artifact", failure_names)
        raw_contract_check = {
            check["name"]: check for check in contract["artifact"]["checks"]
        }["Queue raw auto-start contract"]
        self.assertIn("inspect-only", raw_contract_check["detail"])

    def test_dashboard_surface_contract_fails_for_unsafe_static_queue_run_artifact(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "queue_run_json":
                item["queue_run"]["contract_ok"] = False
                item["queue_run"]["actual_run"] = False
                item["queue_run"]["output_issues"] = ["run[0]:stdout_tail_sensitive_markers:source_payload"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact snapshot", failure_names)
        self.assertIn("Queue run artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_unsafe_checkpoint_artifact(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "ncs006_element_api_checkpoint_json":
                item["checkpoint"]["contract_ok"] = False
                item["checkpoint"]["forbidden_paths"] = ["collection_processes[0].command"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_marks_review_gated_checkpoint_without_static_failure(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "human_review_safe_ops_checkpoint_json":
                item["checkpoint"].update(
                    {
                        "ok": False,
                        "contract_ok": False,
                        "review_gated": True,
                        "review_gate_code": (
                            "human_review_provenance_reconfirmation_required"
                        ),
                        "legacy_trusted_status_rows_pending_reconfirmation": 34,
                        "rows_without_packet_backed_provenance": 34,
                        "provenance_gap_present": True,
                        "unresolved_provenance_gap": True,
                        "reconfirmation_blank_decision_count": 34,
                    }
                )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact snapshot", failure_names)
        static_check = {
            check["name"]: check for check in contract["artifact"]["checks"]
        }["Static artifact snapshot"]
        self.assertTrue(static_check["ok"])
        self.assertEqual(
            static_check["review_gated_checkpoint_artifacts"],
            ["reports/human_review_safe_ops_checkpoint_20260624.json"],
        )
        self.assertEqual(
            static_check["review_gated_checkpoint_details"][0]["reason"],
            "human_review_provenance_reconfirmation_required",
        )

    def test_dashboard_surface_contract_canonicalizes_reconfirmation_checkpoint_detail(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "human_review_safe_ops_checkpoint_json":
                item["checkpoint"].update(
                    {
                        "ok": False,
                        "contract_ok": False,
                        "review_gated": True,
                        "review_gate_code": (
                            "human_review_provenance_reconfirmation_required"
                        ),
                        "legacy_trusted_status_rows_pending_reconfirmation": 34,
                        "source_audit_rows_without_packet_backed_provenance": 0,
                        "rows_without_packet_backed_provenance": 0,
                        "provenance_gap_present": False,
                        "unresolved_provenance_gap": True,
                        "reconfirmation_blank_decision_count": 34,
                    }
                )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        static_check = {
            check["name"]: check for check in contract["artifact"]["checks"]
        }["Static artifact snapshot"]
        detail = static_check["review_gated_checkpoint_details"][0]
        self.assertEqual(detail["source_audit_rows_without_packet_backed_provenance"], 0)
        self.assertEqual(detail["rows_without_packet_backed_provenance"], 34)
        self.assertEqual(detail["canonical_provenance_reconfirmation_blocker_count"], 34)

    def test_dashboard_surface_contract_fails_for_mixed_core_static_artifact_dates(self) -> None:
        static_artifacts = valid_static_artifacts()
        static_artifacts[0]["path"] = "reports/aihr_plan_demo_20260618.html"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertTrue(contract["artifact"]["mixed_static_artifact_dates"])
        self.assertTrue(contract["artifact"]["core_static_artifact_date_mismatch"])
        self.assertEqual(contract["artifact"]["static_artifact_dates"], ["20260618", "20260624"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact date consistency", failure_names)

    def test_dashboard_surface_contract_fails_for_mixed_core_static_artifact_stamp_families(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if isinstance(item.get("path"), str):
                item["path"] = item["path"].replace("20260624", "20260629_8h")
        static_artifacts[0]["path"] = "reports/aihr_plan_demo_20260629_2h.html"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260629_8h.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertFalse(contract["artifact"]["core_static_artifact_date_mismatch"])
        self.assertTrue(contract["artifact"]["core_static_artifact_stamp_family_mismatch"])
        self.assertEqual(
            contract["artifact"]["core_static_artifact_dates"],
            ["20260629"],
        )
        self.assertIn(
            "20260629_2h",
            contract["artifact"]["core_static_artifact_stamp_families"],
        )
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact date consistency", failure_names)

    def test_dashboard_surface_contract_allows_public_static_artifact_stamp_variant(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if isinstance(item.get("path"), str):
                item["path"] = item["path"].replace("20260624", "20260630_9h")
        for item in static_artifacts:
            if item.get("name") == "queue_run_json":
                item["path"] = "reports/aihr_agent_queue_run_20260630_9h_public.json"
            if item.get("name") in {"queue_status_json", "queue_run_json", "readiness_json"}:
                path = Path(str(item.get("path") or ""))
                resolved_path = path if path.is_absolute() else ROOT / path
                if resolved_path.exists():
                    item["content_sha256"] = "sha256:" + hashlib.sha256(
                        resolved_path.read_bytes()
                    ).hexdigest()
                    if item.get("name") == "readiness_json":
                        readiness_payload = json.loads(
                            resolved_path.read_text(encoding="utf-8-sig")
                        )
                        item["cycle_safe_content_sha256"] = (
                            _release_readiness_cycle_safe_sha256(readiness_payload)
                        )
                else:
                    item.pop("content_sha256", None)
                    item.pop("cycle_safe_content_sha256", None)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260630_9h.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertEqual(
            contract["artifact"]["core_static_artifact_stamp_families"],
            ["20260630_9h"],
        )
        self.assertFalse(contract["artifact"]["core_static_artifact_date_mismatch"])
        self.assertFalse(contract["artifact"]["core_static_artifact_stamp_family_mismatch"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact date consistency", failure_names)

    def test_dashboard_surface_contract_allows_current_and_latest_static_aliases(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if isinstance(item.get("path"), str):
                item["path"] = item["path"].replace("20260624", "20260630_9h")
            if item.get("name") == "readiness_json":
                item["path"] = "reports/aihr_release_readiness_20260630_9h_latest.json"
            if item.get("name") == "ncs006_element_api_checkpoint_json":
                item["path"] = (
                    "reports/checkpoint_ncs006_element_api_status_20260630_9h_current.json"
                )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260630_9h.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertEqual(
            contract["artifact"]["core_static_artifact_stamp_families"],
            ["20260630_9h"],
        )
        self.assertFalse(contract["artifact"]["core_static_artifact_date_mismatch"])
        self.assertFalse(contract["artifact"]["core_static_artifact_stamp_family_mismatch"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertNotIn("Static artifact date consistency", failure_names)

    def test_dashboard_surface_contract_allows_mixed_reference_static_artifact_dates(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item.get("name") == "ncs006_element_api_checkpoint_json":
                item["path"] = "reports/checkpoint_ncs006_element_api_status_20260618_current.json"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification_20260624.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertTrue(contract["ok"])
        self.assertTrue(contract["artifact"]["mixed_static_artifact_dates"])
        self.assertFalse(contract["artifact"]["core_static_artifact_date_mismatch"])

    def test_dashboard_surface_contract_fails_for_missing_query_route_evidence(self) -> None:
        baseline = valid_live_plan_summary("baseline")
        baseline.pop("query_route_schema")
        baseline["missing_query_route_fields"] = ["query_route.schema"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "demo_page", "ok": True},
                            {"name": "readiness_page", "ok": True},
                            {"name": "queue_status_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            baseline,
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_route_chain_summary(self) -> None:
        baseline = valid_live_plan_summary("baseline")
        baseline.pop("query_route_expected_tool_chain")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            baseline,
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)

    def test_dashboard_surface_contract_fails_for_route_contract_mismatch(self) -> None:
        baseline = valid_live_plan_summary("baseline")
        baseline["query_route_contract_primary_tool"] = "recommend_training_transition"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            baseline,
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_matrix_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 1,
                        "checks": [{"name": "live_page", "ok": True}],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            {
                                "name": "baseline",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": ["job_scope"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        self.assertGreater(contract["failure_count"], 0)
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)
        self.assertIn("Multiple live scenarios", failure_names)

    def test_dashboard_surface_contract_fails_for_sensitive_marker_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "demo_page", "ok": True},
                            {"name": "readiness_page", "ok": True},
                            {"name": "queue_status_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            {
                                "name": "baseline",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": ["source_payload"],
                            },
                            {
                                "name": "extra",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)

    def test_dashboard_surface_contract_fails_when_sensitive_markers_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "demo_page", "ok": True},
                            {"name": "readiness_page", "ok": True},
                            {"name": "queue_status_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            {
                                "name": "baseline",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                            },
                            {
                                "name": "extra",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)

    def test_dashboard_surface_contract_fails_closed_for_malformed_numeric_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            static_artifacts = valid_static_artifacts()
            static_artifacts[0]["size_bytes"] = "not-a-number"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": "not-a-number",
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "demo_page", "ok": True},
                            {"name": "readiness_page", "ok": True},
                            {"name": "queue_status_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            {
                                "name": "baseline",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": "not-a-number",
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": [],
                            },
                            {
                                "name": "extra",
                                "ok": True,
                                "schema": "aihr_live_plan_v1",
                                "run_mode": "live_no_save",
                                "view": "ncs_education_plan",
                                "matrix_rows": 3,
                                "missing_matrix_fields": [],
                                "guide_trace_schema": "aihr_training_system_guide_trace_v1",
                                "missing_guide_trace_fields": [],
                                "sensitive_markers": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live plan scenario contracts", failure_names)
        self.assertIn("Multiple live scenarios", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_report_artifact_policy_violations(self) -> None:
        for artifact_name, mutator in [
            (
                "api_linkage_summary_json",
                lambda artifact: artifact["api_linkage_summary"].update({"api_calls": True}),
            ),
            (
                "api_linkage_summary_json",
                lambda artifact: artifact["api_linkage_summary"][
                    "qualification_coverage_plan_hint"
                ].update(
                    {
                        "scope": "selected_majors_report_only",
                        "coverage_plan_matches_summary_scope": False,
                        "coverage_plan_command_present": False,
                    }
                ),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update({"mode": "apply"}),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update(
                    {"execution_authorized": True}
                ),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update(
                    {"authorization_status": "authorized"}
                ),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update(
                    {"do_not_call_api": False}
                ),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update(
                    {"api_call_allowed_now": True}
                ),
            ),
            (
                "qualification_retry_hygiene_json",
                lambda artifact: artifact["qualification_retry_hygiene"].update(
                    {"safety_violation_count": 1}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update({"api_calls": True}),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"execution_authorized": True}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"top_level_automatic_queue_execution_allowed": True}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"automatic_collection_allowed_now": True}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"batch_commands_are_not_queue_items": False}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"checkpoint_path": ""}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"batch_commands_have_ncs006_checkpoint_values": False}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"batch_commands_checkpoint_values_match_plan": False}
                ),
            ),
            (
                "qualification_collection_coverage_plan_json",
                lambda artifact: artifact["qualification_collection_coverage_plan"].update(
                    {"batch_commands_checkpoint_path_must_match_plan": False}
                ),
            ),
            (
                "query_route_contract_audit_json",
                lambda artifact: artifact["query_route_contract_audit"].update(
                    {
                        "status": "fail",
                        "failure_count": 1,
                        "contract_ok": False,
                    }
                ),
            ),
            (
                "query_route_contract_audit_json",
                lambda artifact: artifact["query_route_contract_audit"].update(
                    {
                        "passed_row_count": 1,
                        "failed_row_count": 1,
                        "row_issue_count": 1,
                        "contract_ok": False,
                    }
                ),
            ),
            (
                "human_review_provenance_reconfirmation_packet_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_packet"
                ].update({"approval_claim": True}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_sheet_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_sheet"
                ].update({"db_writes": True}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_sheet_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_sheet"
                ].update({"source_packet_sha256": ""}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_sheet_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_sheet"
                ].update(
                    {
                        "row_safety_flag_type_issues": [
                            "row_1.status_update_allowed_not_false_boolean"
                        ],
                        "row_safety_flag_type_issue_count": 1,
                    }
                ),
            ),
            (
                "human_review_provenance_reconfirmation_decision_audit_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_audit"
                ].update({"guarded_apply_ready": True}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_audit_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_audit"
                ].update({"source_identity_mismatch_count": 9}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_audit_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_audit"
                ].update({"missing_packet_row_count": 4}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_audit_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_audit"
                ].update({"source_decision_packet_not_found_count": 3}),
            ),
            (
                "human_review_provenance_reconfirmation_decision_audit_json",
                lambda artifact: artifact[
                    "human_review_provenance_reconfirmation_decision_audit"
                ].update({"source_packet_contract_ok": False}),
            ),
        ]:
            with self.subTest(artifact_name=artifact_name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "dashboard_verification.json"
                static_artifacts = valid_static_artifacts()
                artifact = next(item for item in static_artifacts if item["name"] == artifact_name)
                mutator(artifact)
                path.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "schema": "aihr_dashboard_surface_verification_v1",
                            "scenario_count": 2,
                            "checks": [
                                {"name": "static_artifacts", "ok": True},
                                {"name": "live_page", "ok": True},
                                {"name": "demo_page", "ok": True},
                                {"name": "readiness_page", "ok": True},
                                {"name": "queue_status_page", "ok": True},
                                {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                            ],
                            "queue_status_summary": {"blocked_count": 0},
                            "static_artifacts": static_artifacts,
                            "live_plan_summaries": [
                                valid_live_plan_summary("baseline"),
                                valid_live_plan_summary("extra"),
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                contract = build_dashboard_surface_contract(path)

            self.assertFalse(contract["ok"])
            static_failure = next(
                failure for failure in contract["failures"] if failure["check"] == "Static artifact snapshot"
            )
            self.assertIn(artifact_name.removesuffix("_json"), static_failure["detail"])

    def test_dashboard_surface_contract_fails_for_queue_source_path_mismatch(self) -> None:
        static_artifacts = valid_static_artifacts()
        queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
        queue_run["queue_run"]["source_queue_path"] = "reports/aihr_agent_queue_stale.json"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "demo_page", "ok": True},
                            {"name": "readiness_page", "ok": True},
                            {"name": "queue_status_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue source path consistency", failure_names)
        source_failure = next(
            failure for failure in contract["failures"] if failure["check"] == "Queue source path consistency"
        )
        self.assertIn("aihr_agent_queue_20260624.json", source_failure["detail"])
        self.assertIn("aihr_agent_queue_stale.json", source_failure["detail"])

    def test_dashboard_surface_contract_fails_for_live_queue_source_path_mismatch(self) -> None:
        endpoint_checks = json.loads(json.dumps(valid_dashboard_endpoint_checks()))
        queue_status_api = next(item for item in endpoint_checks if item["name"] == "queue_status_api")
        queue_run_api = next(item for item in endpoint_checks if item["name"] == "agent_queue_run_api")
        queue_status_api["source_queue_path"] = "reports/aihr_agent_queue_stale.json"
        queue_run_api["source_queue_path"] = "reports/aihr_agent_queue_stale.json"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Live queue source path consistency", failure_names)
        source_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Live queue source path consistency"
        )
        self.assertIn("aihr_agent_queue_20260624.json", source_failure["detail"])
        self.assertIn("aihr_agent_queue_stale.json", source_failure["detail"])

    def test_queue_source_path_matcher_normalizes_relative_and_windows_paths(self) -> None:
        expected = "reports/aihr_agent_queue_20260624.json"
        self.assertTrue(
            _queue_source_path_matches(
                ".\\reports\\aihr_agent_queue_20260624.json",
                expected,
            )
        )
        self.assertTrue(
            _queue_source_path_matches(
                str(ROOT / "reports" / "aihr_agent_queue_20260624.json"),
                expected,
            )
        )
        self.assertFalse(
            _queue_source_path_matches(
                "C:\\other-workspace\\reports\\aihr_agent_queue_20260624.json",
                expected,
            )
        )
        self.assertFalse(
            _queue_source_path_matches(
                "C:\\workspace\\NCS_MCP\\reports\\aihr_agent_queue_stale.json",
                expected,
            )
        )

    def test_dashboard_surface_contract_fails_for_missing_queue_source_provenance(self) -> None:
        for case_name, mutator, expected_detail in [
            (
                "readiness_queue_missing",
                lambda artifacts: next(
                    item for item in artifacts if item["name"] == "readiness_json"
                )["release_readiness"].pop("agent_work_queue_path"),
                "expected_release_queue_missing",
            ),
            (
                "queue_status_source_missing",
                lambda artifacts: next(
                    item for item in artifacts if item["name"] == "queue_status_json"
                )["queue_status"].pop("source_queue_path"),
                "source_queue_path_missing",
            ),
            (
                "queue_run_source_missing",
                lambda artifacts: next(
                    item for item in artifacts if item["name"] == "queue_run_json"
                )["queue_run"].pop("source_queue_path"),
                "source_queue_path_missing",
            ),
        ]:
            with self.subTest(case_name=case_name), tempfile.TemporaryDirectory() as tmp:
                static_artifacts = valid_static_artifacts()
                mutator(static_artifacts)
                path = Path(tmp) / "dashboard_verification.json"
                path.write_text(
                    json.dumps(
                        {
                            "ok": True,
                            "schema": "aihr_dashboard_surface_verification_v1",
                            "scenario_count": 2,
                            "checks": [
                                {"name": "static_artifacts", "ok": True},
                                {"name": "live_page", "ok": True},
                                {"name": "demo_page", "ok": True},
                                {"name": "readiness_page", "ok": True},
                                {"name": "queue_status_page", "ok": True},
                                {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                            ],
                            "queue_status_summary": {"blocked_count": 0},
                            "static_artifacts": static_artifacts,
                            "live_plan_summaries": [
                                valid_live_plan_summary("baseline"),
                                valid_live_plan_summary("extra"),
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                contract = build_dashboard_surface_contract(path)

            self.assertFalse(contract["ok"])
            failure_names = {failure["check"] for failure in contract["failures"]}
            self.assertIn("Queue source path consistency", failure_names)
            source_failure = next(
                failure
                for failure in contract["failures"]
                if failure["check"] == "Queue source path consistency"
            )
            self.assertIn(expected_detail, source_failure["detail"])

    def test_dashboard_surface_contract_fails_for_bad_source_queue_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_queue = tmp_path / "aihr_agent_queue_20260624.json"
            source_queue.write_text(
                json.dumps(
                    {
                        "schema": "aihr_agent_work_queue_v1",
                        "items": [],
                        "item_count": 0,
                        "generated_at": "2026-06-24T00:00:00+00:00",
                        "generated_at_basis": "artifact_date",
                        "report_only": True,
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "input_artifact_hashes": {},
                        "input_artifact_hash_count": 1,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            source_queue_hash = "sha256:" + hashlib.sha256(source_queue.read_bytes()).hexdigest()
            static_artifacts = valid_static_artifacts()
            readiness = next(item for item in static_artifacts if item["name"] == "readiness_json")
            readiness["release_readiness"]["agent_work_queue_path"] = str(source_queue)
            queue_status = next(item for item in static_artifacts if item["name"] == "queue_status_json")
            queue_status_file = tmp_path / "aihr_agent_queue_status_20260624.json"
            queue_status_file.write_text('{"ok":true}\n', encoding="utf-8")
            queue_status["path"] = str(queue_status_file)
            queue_status["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_status_file.read_bytes()).hexdigest()
            )
            queue_status["queue_status"]["source_queue_path"] = str(source_queue)
            queue_run = next(item for item in static_artifacts if item["name"] == "queue_run_json")
            queue_run_file = tmp_path / "aihr_agent_queue_run_20260624.json"
            queue_run_file.write_text('{"ok":true}\n', encoding="utf-8")
            queue_run["path"] = str(queue_run_file)
            queue_run["content_sha256"] = (
                "sha256:" + hashlib.sha256(queue_run_file.read_bytes()).hexdigest()
            )
            queue_run["queue_run"]["source_queue_path"] = str(source_queue)
            queue_run["queue_run"]["source_queue_sha256"] = source_queue_hash
            queue_run["queue_run_source_queue_sync"] = {
                "checked": True,
                "source_queue_path": str(source_queue),
                "source_queue_exists": True,
                "source_queue_sha256": source_queue_hash,
                "current_source_queue_sha256": source_queue_hash,
                "source_queue_matches_run": True,
            }
            endpoint_checks = valid_dashboard_endpoint_checks()
            for check in endpoint_checks:
                if check.get("name") in {"queue_status_api", "agent_queue_run_api"}:
                    check["source_queue_path"] = str(source_queue)
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Queue source artifact contract", failure_names)
        source_contract_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Queue source artifact contract"
        )
        self.assertIn("input_artifact_hash_count_mismatch", source_contract_failure["detail"])

    def test_dashboard_surface_contract_fails_for_human_gated_execution_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {
                                "name": "queue_status_api",
                                "ok": True,
                                "human_gated_execution": ["aihr-human-gate"],
                            }
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("No unsafe human-gated execution items", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_static_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "live_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": [
                            {
                                "name": "demo_html",
                                "path": "reports/aihr_plan_demo_20260624.html",
                                "exists": False,
                            },
                            {
                                "name": "queue_status_json",
                                "path": "reports/aihr_agent_queue_status_20260624.json",
                                "exists": True,
                                "size_bytes": 0,
                            },
                        ],
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_wrong_static_artifact_role(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "demo_json":
                item["path"] = "reports/aihr_agent_queue_status_20260624.json"
                item["role_contract_ok"] = True
                break
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "static_artifacts", "ok": True},
                            {"name": "live_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Static artifact snapshot", failure_names)
        static_failure = next(
            failure
            for failure in contract["failures"]
            if failure["check"] == "Static artifact snapshot"
        )
        self.assertIn("bad_role=demo_json", static_failure["detail"])

    def test_dashboard_surface_contract_fails_for_unsafe_human_review_backlog(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_backlog_json":
                item["human_review_backlog"]["all_seedpacks_safe"] = False
                item["human_review_backlog"]["total_forbidden_true_field_violations"] = 1
                item["human_review_backlog"]["contract_ok"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_backlog_source_hash_contract(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_backlog_json":
                item["human_review_backlog"]["source_hash_contract_ok"] = False
                item["human_review_backlog"]["queue_input_hash_count"] = 0
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_rechecks_backlog_source_hashes(self) -> None:
        static_artifacts = valid_static_artifacts()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "source.json"
            source_path.write_text("{}", encoding="utf-8")
            backlog_path = tmp_path / "human_review_backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "source_artifact_hashes": {
                            "source": {
                                "path": str(source_path),
                                "sha256": "sha256:" + ("0" * 64),
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for item in static_artifacts:
                if item["name"] == "human_review_backlog_json":
                    item["path"] = str(backlog_path)
                    item["human_review_backlog"]["source_hash_contract_ok"] = True
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_rechecks_backlog_release_cycle_safe_hash(
        self,
    ) -> None:
        static_artifacts = valid_static_artifacts()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            release_path = tmp_path / "aihr_release_readiness.json"
            release_payload = {
                "schema": "aihr_release_readiness_v1",
                "ok": True,
                "release_ready": False,
                "blockers": [],
                "dashboard_surface_contract": {
                    "artifact": {"mtime_utc": "2026-07-12T00:00:00+00:00"}
                },
            }
            release_payload["cycle_safe_content_sha256"] = (
                _release_readiness_cycle_safe_sha256(release_payload)
            )
            release_path.write_text(
                json.dumps(release_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            backlog_path = tmp_path / "human_review_backlog.json"
            backlog_path.write_text(
                json.dumps(
                    {
                        "source_artifact_hashes": {
                            "release_readiness": {
                                "path": str(release_path),
                                "sha256": "sha256:" + ("0" * 64),
                                "sha256_scope": "cycle_safe_release_readiness",
                                "cycle_safe_content_sha256": "sha256:" + ("0" * 64),
                            }
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            for item in static_artifacts:
                if item["name"] == "human_review_backlog_json":
                    item["path"] = str(backlog_path)
                    item["human_review_backlog"]["source_hash_contract_ok"] = True
            path = tmp_path / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_seedpack_structure_issues(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_backlog_json":
                item["human_review_backlog"]["total_seedpack_structure_issues"] = 1
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"][
                    "human_review_backlog_seedpack_structure_issues"
                ] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Goal completion audit artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_human_review_backlog_policy(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_backlog_json":
                item["human_review_backlog"].pop("review_status_policy")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Human review backlog artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_unsafe_goal_completion_audit(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"]["human_review_backlog_all_seedpacks_safe"] = False
                item["goal_completion_audit"]["ksa_definition_packet"]["sidecar_safety_ok"] = False
                item["goal_completion_audit"]["contract_ok"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Goal completion audit artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_missing_goal_completion_policy(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"].pop("review_status_policy")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Goal completion audit artifact", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_dashboard_surface_contract_fails_for_goal_completion_backlog_safety_counts(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"]["human_review_backlog_trusted_status_proposals"] = 1
                item["goal_completion_audit"]["human_review_backlog_status_update_allowed_violations"] = 1
                item["goal_completion_audit"]["human_review_backlog_missing_status_update_allowed"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Goal completion audit artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_goal_completion_release_ready_mismatch(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"]["release_ready"] = True
                item["goal_completion_audit"]["release_ready_consistent"] = False
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Goal completion audit artifact", failure_names)

    def test_dashboard_surface_contract_recomputes_goal_completion_release_ready(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "goal_completion_audit_json":
                item["goal_completion_audit"]["release_ready"] = True
                item["goal_completion_audit"]["release_ready_consistent"] = True
                item["goal_completion_audit"]["open_requirement_count"] = 4
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Goal completion audit artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_unsafe_guide_surface_audit(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "guide_surface_audit_json":
                item["guide_surface_audit"]["ok"] = False
                item["guide_surface_audit"]["unsafe_approval_claim_artifacts"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Guide surface audit artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_unsafe_ontology_education_audit(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "ontology_transferability_education_audit_json":
                item["ontology_transferability_education_audit"]["approval_ready"] = True
                item["ontology_transferability_education_audit"]["status"] = "approved"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Ontology transferability education audit artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_sensitive_guide_surface_audit_markers(self) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "guide_surface_audit_json":
                item["guide_surface_audit"]["sensitive_markers"] = ["source_payload", "authKey"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Guide surface audit artifact", failure_names)

    def test_dashboard_surface_contract_fails_for_omitted_required_checks_and_artifact_names(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dashboard_verification.json"
            path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": [
                            {"name": "live_page", "ok": True},
                            {"name": "queue_status_api", "ok": True, "human_gated_execution": []},
                        ],
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": [
                            {
                                "name": "demo_html",
                                "path": "reports/aihr_plan_demo_20260624.html",
                                "exists": True,
                                "size_bytes": 1024,
                            }
                        ],
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            contract = build_dashboard_surface_contract(path)

        self.assertFalse(contract["ok"])
        failure_names = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Endpoint checks", failure_names)
        self.assertIn("Static artifact snapshot", failure_names)

    def test_build_aihr_demo_contract_validates_json_and_html_artifacts(self) -> None:
        payload = {
            "ok": True,
            "view": "ncs_education_plan",
            "public_demo_schema": "aihr_public_demo_v1",
            "training_system_guide_trace": valid_guide_trace(),
            "training_system_summary": {"course_count": 1},
            "training_system_matrix": [
                {
                    "course_name": "인사기획",
                    "job_scope": {"current": "노무관리", "target": "인사기획"},
                    "target_level_band": {"code": "level_5_6", "label": "level 5 6"},
                    "education_type": {"code": "unknown", "label": "unknown"},
                    "required_optional_basis": {"code": "required", "label": "필수"},
                    "delivery_operation": {"code": "method_specified"},
                    "planner_grouping": {
                        "job_scope": "노무관리 -> 인사기획",
                        "target_level_band": "level_5_6",
                        "education_type": "unknown",
                        "required_optional": "required",
                        "delivery_method": "집체훈련",
                    },
                    "need_classification": {"code": "required", "label": "필수"},
                    "evidence_directness": {"code": "training_goal_token", "label": "훈련목표 토큰"},
                    "course_fit": {
                        "level": 5,
                        "hours": 24,
                        "methods": ["집체훈련"],
                        "facilities": ["전산강의실"],
                    },
                }
            ],
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }
        payload = valid_public_demo_payload()
        html_contract_markers = (
            "<h2>Query Route</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
            "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
            "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
            "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
            "<span>guide_stage</span><span>C1-1</span><span>C2-2</span>"
            "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
            "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span>"
        )
        html = """
        <h2>2026 Guide Trace</h2>
        <html><head><title>AI-HR 교육훈련체계 데모 | AI-HR Education Plan Demo</title></head>
        <body><h1>AI-HR 교육훈련체계 데모</h1><h2>Training-System Summary</h2><h2>Training-System Matrix</h2></body></html>
        """
        html += html_contract_markers
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_path = tmp_path / "demo.json"
            html_path = tmp_path / "demo.html"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            html_path.write_text(html, encoding="utf-8")

            contract = build_aihr_demo_contract([json_path], html_path)

        self.assertIsNotNone(contract)
        self.assertTrue(contract["ok"])
        self.assertEqual(contract["failure_count"], 0)
        self.assertEqual(contract["json_artifacts"][0]["matrix_rows"], 1)
        self.assertTrue(contract["html_artifact"]["ok"])

    def test_build_aihr_demo_contract_rejects_missing_planner_evidence_fields(self) -> None:
        payload = valid_public_demo_payload()
        payload.pop("recommended_path")
        row = payload["training_system_matrix"][0]
        row.pop("task_ksa_basis")
        row.pop("facility_constraint_fit")
        row.pop("human_review")
        row["delivery_operation"].pop("facility_constraint_fit")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failures_by_check = {failure["check"]: failure["detail"] for failure in contract["failures"]}
        self.assertIn("Recommended path", failures_by_check)
        self.assertIn("Planner row contract", failures_by_check)
        self.assertIn("Planner matrix fields", failures_by_check)
        self.assertTrue(
            "recommended_path" in failures_by_check["Recommended path"]
            or "stages=0" in failures_by_check["Recommended path"]
        )
        planner_details = f"{failures_by_check['Planner row contract']} {failures_by_check['Planner matrix fields']}"
        self.assertIn("task_ksa_basis", planner_details)
        self.assertIn("facility_constraint_fit", planner_details)
        self.assertIn("human_review", planner_details)

    def test_build_aihr_demo_contract_allows_empty_facilities_with_explicit_facility_status(self) -> None:
        for status in ("unknown", "not_requested"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as tmp:
                payload = valid_public_demo_payload(facilities=[], facility_status=status)
                json_path = Path(tmp) / "demo.json"
                json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

                contract = build_aihr_demo_contract([json_path])

                self.assertTrue(contract["ok"], contract["failures"])

    def test_build_aihr_demo_contract_rejects_missing_query_route(self) -> None:
        payload = valid_public_demo_payload()
        payload.pop("query_route")
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failures_by_check = {failure["check"]: failure["detail"] for failure in contract["failures"]}
        self.assertIn("Query route", failures_by_check)
        self.assertIn("query_route", failures_by_check["Query route"])

    def test_build_aihr_demo_contract_rejects_incomplete_recommended_path_guide_metadata(self) -> None:
        payload = valid_public_demo_payload()
        payload["recommended_path"] = payload["recommended_path"][:3]
        payload["recommended_path"][0].pop("guide_stage_evidence")
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failures_by_check = {failure["check"]: failure["detail"] for failure in contract["failures"]}
        self.assertIn("Recommended path", failures_by_check)
        detail = failures_by_check["Recommended path"]
        self.assertIn("guide_stage_evidence", detail)
        self.assertIn("delivery_fit_review", detail)

    def test_build_aihr_demo_contract_rejects_malformed_guide_trace_checks(self) -> None:
        payload = valid_public_demo_payload()
        for item in payload["training_system_guide_trace"]["checks"]:
            item.pop("label")
            item.pop("evidence")
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failures_by_check = {failure["check"]: failure["detail"] for failure in contract["failures"]}
        self.assertIn("Training system guide trace", failures_by_check)
        self.assertIn("training_system_guide_trace.checks.row_1.label", failures_by_check["Training system guide trace"])

    def test_build_aihr_demo_contract_rejects_missing_public_json_contract_fields(self) -> None:
        payload = {
            "ok": True,
            "view": "ncs_education_plan",
            "public_demo_schema": "aihr_public_demo_v1",
            "training_system_summary": {"course_count": 99},
            "training_system_matrix": [
                {
                    "course_name": "인사기획",
                    "course_fit": {
                        "level": 5,
                        "hours": 24,
                        "methods": ["집체훈련"],
                        "facilities": ["전산강의실"],
                    },
                }
            ],
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failed_checks = {failure["check"] for failure in contract["failures"]}
        self.assertIn("Summary course count", failed_checks)
        self.assertIn("Need classification", failed_checks)
        self.assertIn("Evidence directness", failed_checks)
        self.assertIn("Planner matrix fields", failed_checks)
        self.assertIn("Training system guide trace", failed_checks)

    def test_build_aihr_demo_contract_rejects_missing_guide_workflow_stages(self) -> None:
        payload = valid_public_demo_payload()
        payload["training_system_guide_trace"].pop("guide_workflow_stages")
        payload["training_system_guide_trace"].pop("guide_workflow")
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            contract = build_aihr_demo_contract([json_path])

        self.assertFalse(contract["ok"])
        failures_by_check = {failure["check"]: failure for failure in contract["failures"]}
        self.assertIn("Training system guide trace", failures_by_check)
        self.assertIn(
            "training_system_guide_trace.guide_workflow_stages",
            failures_by_check["Training system guide trace"]["detail"],
        )

    def test_build_aihr_demo_contract_rejects_html_internal_metadata_leaks(self) -> None:
        payload = {
            "ok": True,
            "view": "ncs_education_plan",
            "public_demo_schema": "aihr_public_demo_v1",
            "training_system_guide_trace": valid_guide_trace(),
            "training_system_summary": {"course_count": 1},
            "training_system_matrix": [
                {
                    "course_name": "인사기획",
                    "job_scope": {"current": "?몃Т愿由?", "target": "?몄궗湲고쉷"},
                    "target_level_band": {"code": "level_5_6", "label": "level 5 6"},
                    "education_type": {"code": "unknown", "label": "unknown"},
                    "required_optional_basis": {"code": "required", "label": "?꾩닔"},
                    "delivery_operation": {"code": "method_specified"},
                    "planner_grouping": {
                        "job_scope": "?몃Т愿由? -> ?몄궗湲고쉷",
                        "target_level_band": "level_5_6",
                        "education_type": "unknown",
                        "required_optional": "required",
                        "delivery_method": "吏묒껜?덈젴",
                    },
                    "need_classification": {"code": "required"},
                    "evidence_directness": {"code": "training_goal_token"},
                    "course_fit": {
                        "level": 5,
                        "hours": 24,
                        "methods": ["집체훈련"],
                        "facilities": ["전산강의실"],
                    },
                }
            ],
            "audit": {"sqf_used": False, "learning_modules_used": False},
        }
        html = """
        <h2>2026 Guide Trace</h2>
        <html><head><title>AI-HR 교육훈련체계 데모 | AI-HR Education Plan Demo</title></head>
        <body><h1>AI-HR 교육훈련체계 데모</h1><h2>Training-System Summary</h2><h2>Training-System Matrix</h2>
        relation_id=123 created_at=2026-06-17</body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            json_path = tmp_path / "demo.json"
            html_path = tmp_path / "demo.html"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            html_path.write_text(html, encoding="utf-8")

            contract = build_aihr_demo_contract([json_path], html_path)

        self.assertFalse(contract["ok"])
        self.assertFalse(contract["html_artifact"]["ok"])
        self.assertIn("Public metadata redacted", {failure["check"] for failure in contract["failures"]})

    def test_build_release_readiness_separates_hygiene_from_release_blockers(self) -> None:
        gates = required_quality_gates(qualification_value=0.5)
        gates[1] = {
            "name": "review_debt:human_reviewed_concepts",
            "status": "warn",
            "message": "human_reviewed_concepts is still zero.",
            "value": 0,
            "threshold": "> 0",
        }
        quality_report = {
            "status": "warn",
            "summary": {"fail_count": 0, "warn_count": 1},
            "gates": gates,
        }
        contract = valid_mcp_contract()

        report = build_release_readiness(
            quality_report,
            contract,
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260624",
        )

        self.assertTrue(report["engineering_hygiene_ok"])
        self.assertFalse(report["release_ready"])
        self.assertEqual(report["schema"], "aihr_release_readiness_v1")
        self.assertEqual(report["ok_meaning"], "report_generated_and_contract_checks_evaluated")
        self.assertFalse(report["approval_claim"])
        self.assertEqual(
            report["release_decision"]["status"],
            "blocked_until_requirements_met",
        )
        self.assertFalse(report["release_decision"]["approval_claim"])
        self.assertTrue(report["release_decision"]["human_decision_required_for_release_claim"])
        self.assertEqual(
            {blocker["name"] for blocker in report["blockers"]},
            {
                "review_debt:human_reviewed_concepts",
                "qualification:collection_coverage",
            },
        )
        self.assertEqual(
            {item["blocker"] for item in report["next_actions"]},
            {
                "review_debt:human_reviewed_concepts",
                "qualification:collection_coverage",
            },
        )
        next_action_commands = " ".join(item["command"] for item in report["next_actions"])
        self.assertIn("qualification-coverage-plan", next_action_commands)
        self.assertNotIn("collect-qualification-items", next_action_commands)
        queue = report["agent_work_queue"]
        self.assertEqual(queue["schema"], "aihr_agent_work_queue_v1")
        self.assertIn("generated_at", queue)
        self.assertTrue(queue["report_only"])
        self.assertFalse(queue["status_update_allowed"])
        self.assertFalse(queue["db_writes"])
        self.assertFalse(queue["approval_claim"])
        self.assertEqual(queue["item_count"], 2)
        self.assertGreater(queue["input_artifact_hash_count"], 0)
        queue_by_blocker = {item["blocker"]: item for item in queue["items"]}
        self.assertEqual(
            queue_by_blocker["review_debt:human_reviewed_concepts"]["agent_file"],
            ".agents/ontology-review-agent.md",
        )
        qualification_item = queue_by_blocker["qualification:collection_coverage"]
        self.assertEqual(qualification_item["agent_file"], ".agents/data-collection-agent.md")
        self.assertFalse(qualification_item["auto_runnable"])
        self.assertEqual(qualification_item["mutation_policy"], "requires_existing_artifacts")
        self.assertIn("qualification-coverage-plan", qualification_item["command"])
        self.assertNotIn("collect-qualification-items", qualification_item["command"])
        self.assertIn("--target-ratio 0.9", qualification_item["command"])
        self.assertIn("--batch-size 100", qualification_item["command"])
        self.assertIn("--ncs006-checkpoint-path", qualification_item["command"])
        self.assertIn(
            "reports\\checkpoint_ncs006_element_api_status_20260624_current.json",
            qualification_item["command"],
        )
        self.assertNotIn(
            "checkpoint_ncs006_element_api_status_20260624_public.json",
            qualification_item["command"],
        )
        self.assertEqual(
            qualification_item["prerequisite_artifacts"],
            [
                "reports/checkpoint_ncs006_element_api_status_20260624_current.json",
                "reports/qualification_retry_hygiene_20260624.json",
                "reports/qualification_retry_hygiene_20260624.md",
            ],
        )
        self.assertEqual(qualification_item["prerequisite_commands"], [])
        self.assertIn(
            "reports/checkpoint_ncs006_element_api_status_20260624_current.json",
            qualification_item["input_artifacts"],
        )
        self.assertIn(
            "reports/qualification_retry_hygiene_20260624.json",
            qualification_item["input_artifacts"],
        )
        self.assertIn(
            "reports/checkpoint_ncs006_element_api_status_20260624_current.json",
            qualification_item["input_artifact_hashes"],
        )
        self.assertIn(
            "reports/qualification_retry_hygiene_20260624.json",
            queue["input_artifact_hashes"],
        )
        self.assertEqual(
            qualification_item["expected_artifacts"],
            [
                "reports/qualification_collection_coverage_plan_20260624.json",
                "reports/qualification_collection_coverage_plan_20260624.md",
                "reports/qualification_collection_coverage_plan_20260624.csv",
            ],
        )
        qualification_text = json.dumps(qualification_item, ensure_ascii=False).replace("\\\\", "/")
        self.assertNotIn("data/processed/ncs.db", qualification_text)
        self.assertIn("qualification-retry-hygiene", " ".join(qualification_item["acceptance_checks"]))
        self.assertIn("qualification-coverage-plan", " ".join(qualification_item["acceptance_checks"]))
        self.assertIn("does not call external APIs", " ".join(qualification_item["acceptance_checks"]))
        self.assertIn("Do not run collect-qualification-items", " ".join(qualification_item["acceptance_checks"]))

    def test_build_release_readiness_uses_dashboard_ncs006_checkpoint_for_qualification_plan(self) -> None:
        dashboard_contract = {
            **valid_dashboard_surface_contract(),
            "artifact": {
                "static_artifacts": [
                    {
                        "name": "ncs006_element_api_checkpoint_json",
                        "path": "reports/checkpoint_ncs006_element_api_status_20260703_10h_public.json",
                    }
                ]
            },
        }

        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": required_quality_gates(qualification_value=0.5),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_contract,
            artifact_date="20260707",
            dashboard_static_artifact_dir="reports/overnight_sessions",
        )

        qualification_action = next(
            item
            for item in report["next_actions"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertIn(
            "reports/checkpoint_ncs006_element_api_status_20260703_10h_public.json",
            qualification_action["command"],
        )
        self.assertNotIn(
            "checkpoint_ncs006_element_api_status_20260707_public.json",
            qualification_action["command"],
        )
        self.assertNotIn(
            "checkpoint_ncs006_element_api_status_20260707_current.json",
            qualification_action["command"],
        )
        qualification_item = next(
            item
            for item in report["agent_work_queue"]["items"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertIn(
            "reports/checkpoint_ncs006_element_api_status_20260703_10h_public.json",
            qualification_item["command"],
        )

    def test_build_release_readiness_checkpoint_fallback_uses_date_for_stamp_variant(self) -> None:
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": required_quality_gates(qualification_value=0.4),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260629_8h",
        )

        qualification_item = next(
            item
            for item in report["agent_work_queue"]["items"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertIn(
            "reports\\checkpoint_ncs006_element_api_status_20260629_current.json",
            qualification_item["command"],
        )
        self.assertNotIn(
            "checkpoint_ncs006_element_api_status_20260629_8h_current.json",
            qualification_item["command"],
        )
        self.assertIn(
            "qualification_collection_coverage_plan_20260629_8h.json",
            qualification_item["command"],
        )

    def test_release_queue_keeps_guarded_qualification_collection_manual_ready(self) -> None:
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": required_quality_gates(qualification_value=0.4),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260624",
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            reports_dir = workspace / "reports"
            agents_dir = workspace / ".agents"
            reports_dir.mkdir()
            agents_dir.mkdir()
            (agents_dir / "data-collection-agent.md").write_text("agent", encoding="utf-8")
            queue_path = reports_dir / "aihr_agent_queue_20260624.json"
            queue_path.write_text(
                json.dumps(report["agent_work_queue"], ensure_ascii=False),
                encoding="utf-8",
            )
            for relative_path in (
                "reports/qualification_retry_hygiene_20260624.json",
                "reports/qualification_retry_hygiene_20260624.md",
                "reports/qualification_collection_coverage_plan_20260624.json",
                "reports/qualification_collection_coverage_plan_20260624.md",
                "reports/qualification_collection_coverage_plan_20260624.csv",
            ):
                path = workspace / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("artifact", encoding="utf-8")
            checkpoint_path = reports_dir / "checkpoint_ncs006_element_api_status_20260624_current.json"
            checkpoint_path.write_text(
                json.dumps(
                    {
                        "generated_at": "2026-06-24T00:00:00+00:00",
                        "next_safe_action": {
                            "status": "complete_no_collection_needed",
                            "api_call_allowed_now": False,
                            "qualification_retry_allowed_now": True,
                            "blocked_automation": [
                                "start_duplicate_ncs006_collector",
                                "retry_qualification_api_during_ncs006_cooldown",
                                "write_human_reviewed_accepted_or_reviewed_without_human_decision",
                            ],
                        },
                        "rate_limit_cooldown": {"status": "cooldown_consumed_by_later_activity"},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            status = build_agent_queue_status_from_file(
                queue_path,
                workspace=workspace,
                ncs006_checkpoint_path=checkpoint_path,
            )

        self.assertTrue(status["ok"])
        self.assertEqual(status["summary"]["auto_startable_count"], 0)
        self.assertEqual(status["summary"]["manual_ready_count"], 1)
        qualification_status = next(
            item
            for item in status["items"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertEqual(qualification_status["state"], "manual_ready")
        self.assertFalse(qualification_status["can_start_automated"])
        self.assertEqual(qualification_status["safety_violations"], [])
        self.assertEqual(
            qualification_status["automation_block_reason"],
            "mutation_policy:requires_existing_artifacts",
        )
        self.assertEqual(
            qualification_status["operator_action_recommended"],
            "verify_existing_artifacts_before_execution",
        )
        self.assertNotIn("collect-qualification-items", qualification_status["command"])

    def test_build_release_readiness_includes_productization_strategy_check(self) -> None:
        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
        )

        strategy_check = report["checks"]["productization_strategy"]

        self.assertTrue(strategy_check["ok"])
        self.assertTrue(strategy_check["path"].endswith("AIHR_PRODUCTIZATION_STRATEGY.md"))
        self.assertEqual(strategy_check["missing_markers"], [])
        self.assertTrue(report["engineering_hygiene_ok"])

    def test_build_release_readiness_includes_deployment_runbook_check(self) -> None:
        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
        )

        runbook_check = report["checks"]["deployment_runbook"]

        self.assertTrue(runbook_check["ok"])
        self.assertTrue(runbook_check["path"].endswith("AIHR_DEPLOYMENT_RUNBOOK.md"))
        self.assertEqual(runbook_check["missing_markers"], [])

    def test_build_release_readiness_treats_deployment_runbook_as_hygiene_gate(self) -> None:
        failing_runbook = {
            "name": "deployment_runbook",
            "ok": False,
            "path": "docs/AIHR_DEPLOYMENT_RUNBOOK.md",
            "missing_markers": ["rollback"],
            "detail": "missing required deployment markers",
        }

        with mock.patch(
            "scripts.release_readiness_report.build_deployment_runbook_check",
            return_value=failing_runbook,
        ):
            report = build_release_readiness(
                {
                    "status": "pass",
                    "summary": {"fail_count": 0, "warn_count": 0},
                    "gates": required_quality_gates(),
                },
                valid_mcp_contract(),
                demo_contract=valid_aihr_demo_contract(),
                dashboard_surface_contract=valid_dashboard_surface_contract(),
            )

        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn(
            "deployment_runbook",
            {blocker["name"] for blocker in report["blockers"]},
        )

        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "readiness.md"
            write_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("## Deployment Runbook", markdown)
        self.assertIn("fail: docs/AIHR_DEPLOYMENT_RUNBOOK.md", markdown)
        self.assertIn("missing_markers: rollback", markdown)

    def test_review_artifact_readability_contract_keeps_out_of_scope_findings_advisory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            audit_path = tmp_path / "review_artifact_readability_audit_20260624.json"
            active_path = tmp_path / "aihr_quality_gates_with_transition_20260624.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "schema": "review_artifact_readability_audit_v1",
                        "ok": False,
                        "status": "review_required",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required": True,
                        "artifact_count": 10,
                        "finding_count": 1,
                        "findings": [
                            {
                                "path": "reports/old_human_review_packet_20260619.md",
                                "severity": "medium",
                                "code": "question_mark_noise_detected",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            contract = build_review_artifact_readability_contract(
                audit_path,
                active_artifact_paths=[active_path],
            )

        self.assertTrue(contract["ok"])
        artifact = contract["artifact"]
        self.assertFalse(artifact["audit_ok"])
        self.assertEqual(artifact["finding_count"], 1)
        self.assertEqual(artifact["blocking_finding_count"], 0)

    def test_build_release_readiness_blocks_scoped_readability_findings_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            active_path = tmp_path / "aihr_quality_gates_with_transition_20260624.json"
            audit_path = tmp_path / "review_artifact_readability_audit_20260624.json"
            audit_path.write_text(
                json.dumps(
                    {
                        "schema": "review_artifact_readability_audit_v1",
                        "ok": False,
                        "status": "review_required",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required": True,
                        "artifact_count": 10,
                        "finding_count": 2,
                        "findings": [
                            {
                                "path": str(active_path),
                                "severity": "high",
                                "code": "non_utf8_bom_detected",
                            },
                            {
                                "path": "reports/old_human_review_packet_20260619.md",
                                "severity": "medium",
                                "code": "question_mark_noise_detected",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            readability_contract = build_review_artifact_readability_contract(
                audit_path,
                active_artifact_paths=[active_path],
            )
            report = build_release_readiness(
                {
                    "status": "pass",
                    "summary": {"fail_count": 0, "warn_count": 0},
                    "gates": required_quality_gates(),
                },
                valid_mcp_contract(),
                demo_contract=valid_aihr_demo_contract(),
                dashboard_surface_contract=valid_dashboard_surface_contract(),
                review_readability_contract=readability_contract,
                artifact_date="20260624",
            )
            markdown_path = tmp_path / "readiness.md"
            write_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertFalse(readability_contract["ok"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn(
            "review_artifact:readability_audit",
            {blocker["name"] for blocker in report["blockers"]},
        )
        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        readability_item = queue_by_blocker["review_artifact:readability_audit"]
        self.assertTrue(readability_item["auto_runnable"])
        self.assertEqual(readability_item["mutation_policy"], "regenerate_reports_only")
        self.assertIn("audit-review-artifact-readability", readability_item["command"])
        self.assertIn("## Review Artifact Readability", markdown)
        self.assertIn("- blocking_finding_count: 1", markdown)

    def test_markdown_documents_dashboard_freshness_hash_skip_names(self) -> None:
        dashboard_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {
                "path": "reports/aihr_dashboard_surface_verification_20260624.json",
                "scenario_count": 2,
                "queue_status_summary": {"blocked_count": 0},
                "review_chain_safety_summary": {"contract_ok": True},
                "static_artifacts": valid_static_artifacts(),
                "static_artifact_dates": ["20260624"],
                "mixed_static_artifact_dates": False,
                "freshness_hash_skip_names": [
                    "queue_run_json",
                    "queue_status_json",
                    "readiness_json",
                ],
                "freshness_hash_skip_reason": {
                    "readiness_json": "cycle_aware_release_dashboard_reference"
                },
            },
        }
        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_contract,
            artifact_date="20260624",
        )
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "release.md"
            write_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn(
            "- freshness_hash_skip_names: ['queue_run_json', 'queue_status_json', 'readiness_json']",
            markdown,
        )
        self.assertIn(
            "- freshness_hash_skip_reason.readiness_json: cycle_aware_release_dashboard_reference",
            markdown,
        )
        self.assertIn("- qualification_coverage_plan_batch_count: 68", markdown)
        self.assertIn("- qualification_coverage_plan_attempted_unit_count: 5349", markdown)
        self.assertIn("- qualification_coverage_plan_total_unit_count: 13435", markdown)
        self.assertIn("- qualification_coverage_plan_collection_coverage: 0.398139", markdown)
        self.assertIn(
            "- qualification_coverage_plan_additional_attempted_units_needed: 6743",
            markdown,
        )
        self.assertIn("- qualification_coverage_plan_estimated_batch_count: 68", markdown)
        self.assertIn(
            "- qualification_coverage_plan_raw_batch_count_matches_batches: True",
            markdown,
        )
        self.assertIn("- qualification_coverage_plan_unsafe_batch_count: 0", markdown)
        self.assertIn(
            "- qualification_coverage_plan_raw_unsafe_batch_count_matches_batches: True",
            markdown,
        )
        self.assertIn(
            "- qualification_coverage_plan_raw_unsafe_batches_count: 0",
            markdown,
        )
        self.assertIn(
            "- qualification_coverage_plan_must_run_qualification_retry_hygiene_first: True",
            markdown,
        )
        self.assertIn(
            "- qualification_coverage_plan_must_use_ncs006_checkpoint_path: True",
            markdown,
        )
        self.assertIn(
            "- qualification_coverage_plan_operator_timing_required: True",
            markdown,
        )
        self.assertIn(
            "- qualification_coverage_plan_forbidden_status_updates_exact: True",
            markdown,
        )
        self.assertIn("- qualification_coverage_hint_scope: all_majors", markdown)
        self.assertIn("- qualification_coverage_hint_command_scope: all_units", markdown)
        self.assertIn(
            "- qualification_coverage_hint_matches_summary_scope: True",
            markdown,
        )
        self.assertIn("- qualification_coverage_hint_command_present: True", markdown)
        self.assertIn(
            "- qualification_coverage_hint_global_command_present: True",
            markdown,
        )
        self.assertIn("### Static Artifacts", markdown)
        self.assertIn("content_sha256=sha256:", markdown)

    def test_build_release_readiness_copies_guarded_retry_preflight(self) -> None:
        retry_command = (
            "python scripts\\ncs_harness.py collect-qualification-items --all-units "
            "--limit-units 10 --num-of-rows 50 --max-pages 1 "
            "--request-delay 2 --max-retries 1 "
            "--retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 "
            "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_20260624_current.json"
        )
        dashboard_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {
                "static_artifacts": [
                    {
                        "name": "queue_status_json",
                        "path": "reports/aihr_agent_queue_status_20260624.json",
                        "queue_status": {
                            "schema": "aihr_agent_queue_status_v1",
                            "contract_ok": True,
                            "items": [
                                {
                                    "id": "aihr-qualification",
                                    "blocker": "qualification:collection_coverage",
                                    "mutation_policy": "guarded_api_collection",
                                    "command": retry_command,
                                    "state": "blocked_safety",
                                    "preflight_ok": False,
                                    "can_start_automated": False,
                                    "safety_violations": [
                                        "ncs006_checkpoint_api_call_not_allowed"
                                    ],
                                    "operational_guard": {
                                        "status": "blocked",
                                        "checkpoint_path": "reports/checkpoint_ncs006_element_api_status_20260624.json",
                                        "api_call_allowed_now": False,
                                        "element_api_call_allowed_now": False,
                                        "qualification_retry_allowed_now": False,
                                        "qualification_retry_guard_reason": "rate_limit_cooldown_active",
                                        "next_safe_action_status": "start_guarded_watchdog_if_no_active_process",
                                        "cooldown_status": "cooldown_consumed_by_later_activity",
                                        "cooldown_until": None,
                                        "blocked_automation": [
                                            "retry_qualification_api_during_ncs006_cooldown"
                                        ],
                                        "safety_violations": [
                                            "ncs006_checkpoint_api_call_not_allowed"
                                        ],
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        }

        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": required_quality_gates(qualification_value=0.5),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_contract,
        )

        retry_action = next(
            item
            for item in report["next_actions"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertNotIn("preflight", retry_action)
        queue_item = next(
            item
            for item in report["agent_work_queue"]["items"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertNotIn("preflight", queue_item)
        self.assertEqual(queue_item["command"].split()[2], "qualification-coverage-plan")
        self.assertFalse(queue_item["auto_runnable"])
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "release.md"
            write_markdown(report, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertNotIn("state=blocked_safety", markdown)
        self.assertIn("qualification-coverage-plan", markdown)
        self.assertNotIn("collect-qualification-items", markdown)
        self.assertNotIn("retry-qualification-errors", markdown)

    def test_guarded_preflight_suppresses_absolute_source_and_checkpoint_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            source_path = tmp_path / "reports" / "aihr_agent_queue_status_20260624.json"
            checkpoint_path = tmp_path / "reports" / "checkpoint_ncs006_element_api_status_20260624.json"
            source_path.parent.mkdir()
            checkpoint_path.write_text("{}", encoding="utf-8")

            preflight = _guarded_preflight_from_status_item(
                {
                    "state": "blocked_safety",
                    "preflight_ok": False,
                    "can_start_automated": False,
                    "safety_violations": ["ncs006_checkpoint_api_call_not_allowed"],
                    "operational_guard": {
                        "status": "blocked",
                        "checkpoint_path": str(checkpoint_path),
                        "api_call_allowed_now": False,
                        "qualification_retry_allowed_now": False,
                        "safety_violations": [],
                    },
                },
                source_path=str(source_path),
            )

        preflight_text = json.dumps(preflight, ensure_ascii=False)
        self.assertNotIn(str(tmp_path), preflight_text)
        self.assertEqual(preflight["source_path"], source_path.name)
        self.assertEqual(preflight["checkpoint_path"], checkpoint_path.name)

    def test_guarded_preflight_prefers_current_checkpoint_variant_over_public(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reports_dir = tmp_path / "reports"
            source_path = reports_dir / "aihr_agent_queue_status_20260625.json"
            current_checkpoint = reports_dir / "checkpoint_ncs006_element_api_status_20260625_current.json"
            public_checkpoint = reports_dir / "checkpoint_ncs006_element_api_status_20260625_public.json"
            reports_dir.mkdir()
            source_path.write_text("{}", encoding="utf-8")
            current_checkpoint.write_text("{}", encoding="utf-8")
            public_checkpoint.write_text("{}", encoding="utf-8")

            preflight = _guarded_preflight_from_status_item(
                {
                    "state": "manual_ready",
                    "preflight_ok": True,
                    "can_start_automated": False,
                    "safety_violations": [],
                    "operational_guard": {
                        "status": "allowed",
                        "checkpoint_path": str(public_checkpoint),
                        "api_call_allowed_now": False,
                        "qualification_retry_allowed_now": True,
                        "safety_violations": [],
                    },
                },
                source_path=str(source_path),
            )

        self.assertEqual(preflight["checkpoint_path"], current_checkpoint.name)

    def test_guarded_preflight_prefers_newer_checkpoint_date_over_current_variant(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            reports_dir = tmp_path / "reports"
            source_path = reports_dir / "aihr_agent_queue_status_20260625.json"
            older_current_checkpoint = (
                reports_dir / "checkpoint_ncs006_element_api_status_20260625_current.json"
            )
            newer_public_checkpoint = (
                reports_dir / "checkpoint_ncs006_element_api_status_20260626_public.json"
            )
            reports_dir.mkdir()
            source_path.write_text("{}", encoding="utf-8")
            older_current_checkpoint.write_text("{}", encoding="utf-8")
            newer_public_checkpoint.write_text("{}", encoding="utf-8")

            preflight = _guarded_preflight_from_status_item(
                {
                    "state": "manual_ready",
                    "preflight_ok": True,
                    "can_start_automated": False,
                    "safety_violations": [],
                    "operational_guard": {
                        "status": "allowed",
                        "checkpoint_path": str(older_current_checkpoint),
                        "api_call_allowed_now": False,
                        "qualification_retry_allowed_now": True,
                        "safety_violations": [],
                    },
                },
                source_path=str(source_path),
            )

        self.assertEqual(preflight["checkpoint_path"], newer_public_checkpoint.name)

    def test_build_release_readiness_preserves_mixed_guarded_retry_preflight(self) -> None:
        retry_command = (
            "python scripts\\ncs_harness.py collect-qualification-items --all-units "
            "--limit-units 10 --num-of-rows 50 --max-pages 1 "
            "--request-delay 2 --max-retries 1 "
            "--retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 "
            "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_20260625_current.json"
        )
        dashboard_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {
                "static_artifacts": [
                    {
                        "name": "queue_status_json",
                        "path": "reports/aihr_agent_queue_status_20260625.json",
                        "queue_status": {
                            "schema": "aihr_agent_queue_status_v1",
                            "contract_ok": True,
                            "items": [
                                {
                                    "id": "aihr-qualification",
                                    "blocker": "qualification:collection_coverage",
                                    "mutation_policy": "guarded_api_collection",
                                    "command": retry_command,
                                    "state": "manual_ready",
                                    "preflight_ok": True,
                                    "can_start_automated": False,
                                    "safety_violations": [],
                                    "operational_guard": {
                                        "status": "allowed",
                                        "checkpoint_path": "reports/checkpoint_ncs006_element_api_status_20260625.json",
                                        "api_call_allowed_now": False,
                                        "element_api_call_allowed_now": False,
                                        "qualification_retry_allowed_now": True,
                                        "qualification_retry_guard_reason": (
                                            "next_safe_action:start_guarded_watchdog_if_no_active_process"
                                        ),
                                        "next_safe_action_status": "start_guarded_watchdog_if_no_active_process",
                                        "cooldown_status": "no_rate_limit_cooldown",
                                        "cooldown_until": None,
                                        "blocked_automation": [
                                            "retry_qualification_api_during_ncs006_cooldown"
                                        ],
                                        "safety_violations": [],
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        }

        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": required_quality_gates(qualification_value=0.5),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_contract,
        )

        retry_action = next(
            item
            for item in report["next_actions"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertNotIn("preflight", retry_action)
        queue_item = next(
            item
            for item in report["agent_work_queue"]["items"]
            if item["blocker"] == "qualification:collection_coverage"
        )
        self.assertNotIn("preflight", queue_item)
        self.assertEqual(queue_item["command"].split()[2], "qualification-coverage-plan")
        self.assertFalse(queue_item["auto_runnable"])

    def test_preflight_blocker_fallback_skips_readonly_qualification_coverage_plan(self) -> None:
        guarded_preflight = {
            "by_command": {},
            "by_blocker": {
                "qualification:collection_coverage": {
                    "state": "blocked_safety",
                    "api_call_allowed_now": False,
                }
            },
        }

        read_only_action = {
            "blocker": "review_debt:human_reviewed_concepts",
            "command": (
                "python scripts\\ncs_harness.py export-ontology-definition-seedpack "
                "--out reports\\aihr_ontology_definition_review_seedpack_20260624.jsonl"
            ),
        }
        coverage_plan_action = {
            "blocker": "qualification:collection_coverage",
            "command": (
                "python scripts\\ncs_harness.py qualification-coverage-plan "
                "--target-ratio 0.9 --batch-size 100 "
                "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_20260624_current.json "
                "--out reports\\qualification_collection_coverage_plan_20260624.json "
                "--markdown-out reports\\qualification_collection_coverage_plan_20260624.md "
                "--csv-out reports\\qualification_collection_coverage_plan_20260624.csv"
            ),
        }
        guarded_action = {
            "blocker": "qualification:collection_coverage",
            "command": (
                "python scripts\\ncs_harness.py collect-qualification-items --all-units "
                "--limit-units 10 --num-of-rows 50 --max-pages 1 "
                "--request-delay 2 --max-retries 1 "
                "--retry-backoff-seconds 30 --stop-after-rate-limit-errors 3 "
                "--ncs006-checkpoint-path reports\\checkpoint_ncs006_element_api_status_20260624_current.json"
            ),
        }

        self.assertIsNone(_preflight_for_action(read_only_action, guarded_preflight))
        self.assertIsNone(_preflight_for_action(coverage_plan_action, guarded_preflight))
        self.assertEqual(
            _preflight_for_action(guarded_action, guarded_preflight),
            {"state": "blocked_safety", "api_call_allowed_now": False},
        )

    def test_agent_queue_does_not_treat_review_priority_input_as_output(self) -> None:
        gates = required_quality_gates()
        gates[2] = {
            "name": "review_debt:human_reviewed_goal_links",
            "status": "warn",
            "message": "human_reviewed_goal_links is still zero.",
            "value": 0,
            "threshold": "> 0",
        }
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260624",
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        triage_item = queue_by_blocker["review_debt:human_reviewed_goal_links"]
        self.assertTrue(triage_item["auto_runnable"])
        self.assertEqual(triage_item["mutation_policy"], "regenerate_reports_only")
        self.assertEqual(
            triage_item["prerequisite_artifacts"],
            [
                "reports/aihr_quality_gates_with_transition_20260624.json",
                "reports/aihr_review_priority_20260624.json",
                "reports/aihr_transition_scenario_seedpack_20260624.jsonl",
            ],
        )
        self.assertIn("review-priority", " ".join(triage_item["prerequisite_commands"]))
        self.assertIn("export-transition-scenario-seedpack", " ".join(triage_item["prerequisite_commands"]))
        self.assertIn("--transition-seedpack", triage_item["command"])
        self.assertIn("aihr_transition_scenario_seedpack_20260624.jsonl", triage_item["command"])
        self.assertEqual(
            triage_item["expected_artifacts"],
            [
                "reports/aihr_review_triage_20260624.json",
                "reports/aihr_review_triage_20260624.md",
            ],
        )
        self.assertNotIn("Review-priority report", " ".join(triage_item["acceptance_checks"]))

    def test_agent_queue_preserves_supplied_quality_report_path_for_review_triage(self) -> None:
        gates = required_quality_gates()
        gates[2] = {
            "name": "review_debt:human_reviewed_goal_links",
            "status": "warn",
            "message": "human_reviewed_goal_links is still zero.",
            "value": 0,
            "threshold": "> 0",
        }
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            quality_report_path=Path("reports") / "aihr_quality_gates_with_transition_20260625_next.json",
            quality_report_markdown_path=Path("reports") / "aihr_quality_gates_with_transition_20260625_next.md",
            review_priority_report_path=Path("reports") / "review_priority_20260625_next.json",
            artifact_date="20260625",
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        triage_item = queue_by_blocker["review_debt:human_reviewed_goal_links"]
        serialized = json.dumps(triage_item, ensure_ascii=False).replace("\\\\", "/").replace("\\", "/")

        self.assertIn("reports/aihr_quality_gates_with_transition_20260625_next.json", serialized)
        self.assertIn("reports/aihr_transition_scenario_seedpack_20260625_next.jsonl", serialized)
        self.assertIn(
            "reports/aihr_quality_gates_with_transition_20260625_next.md",
            " ".join(triage_item["prerequisite_commands"]).replace("\\", "/"),
        )
        self.assertNotIn("reports/aihr_quality_gates_with_transition_20260625.json", serialized)
        self.assertIn(
            "reports/aihr_quality_gates_with_transition_20260625_next.json",
            triage_item["prerequisite_artifacts"],
        )
        self.assertIn(
            "reports/aihr_transition_scenario_seedpack_20260625_next.jsonl",
            triage_item["prerequisite_artifacts"],
        )

    def test_agent_queue_quotes_and_parses_paths_with_spaces_for_review_triage(self) -> None:
        gates = required_quality_gates()
        gates[2] = {
            "name": "review_debt:human_reviewed_goal_links",
            "status": "warn",
            "message": "human_reviewed_goal_links is still zero.",
            "value": 0,
            "threshold": "> 0",
        }
        quality_report_path = Path("reports") / "with space" / "aihr_quality_gates_with_transition_20260625_next.json"
        quality_markdown_path = Path("reports") / "with space" / "aihr_quality_gates_with_transition_20260625_next.md"
        review_priority_path = Path("reports") / "with space" / "review_priority_20260625_next.json"
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            quality_report_path=quality_report_path,
            quality_report_markdown_path=quality_markdown_path,
            review_priority_report_path=review_priority_path,
            artifact_date="20260625",
        )

        triage_item = {
            item["blocker"]: item for item in report["agent_work_queue"]["items"]
        }["review_debt:human_reviewed_goal_links"]
        command = triage_item["command"]

        self.assertIn('"reports\\with space\\aihr_quality_gates_with_transition_20260625_next.json"', command)
        self.assertEqual(
            _command_option_value(command, "--quality-report"),
            "reports\\with space\\aihr_quality_gates_with_transition_20260625_next.json",
        )
        self.assertIn(
            "reports/with space/aihr_quality_gates_with_transition_20260625_next.json",
            triage_item["prerequisite_artifacts"],
        )
        self.assertIn(
            "reports/with space/aihr_transition_scenario_seedpack_20260625_next.jsonl",
            triage_item["prerequisite_artifacts"],
        )
        self.assertEqual(
            _prerequisite_artifacts_for_command(command, "20260625")[0],
            "reports/with space/aihr_quality_gates_with_transition_20260625_next.json",
        )
        self.assertEqual(
            _command_option_value(command, "--transition-seedpack"),
            "reports/with space/aihr_transition_scenario_seedpack_20260625_next.jsonl",
        )
        self.assertIn(
            '"reports/with space/aihr_quality_gates_with_transition_20260625_next.md"',
            " ".join(triage_item["prerequisite_commands"]),
        )
        self.assertIn(
            '"reports/with space/aihr_transition_scenario_seedpack_20260625_next.jsonl"',
            " ".join(triage_item["prerequisite_commands"]),
        )

    def test_agent_queue_merges_duplicate_execution_items(self) -> None:
        review_priority_report_path = Path("reports") / "aihr_review_priority_20260624_refresh.json"
        review_priority_markdown_path = Path("reports") / "aihr_review_priority_20260624_autoresolve_2.md"
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 4},
                "gates": required_quality_gates(human_review_status="warn"),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            review_priority_report_path=review_priority_report_path,
            review_priority_markdown_path=review_priority_markdown_path,
            artifact_date="20260624",
        )

        queue = report["agent_work_queue"]
        ontology_definition_items = [
            item
            for item in queue["items"]
            if item["owner"] == "ontology-review-agent"
            and "export-ontology-definition-seedpack" in item["command"]
        ]

        self.assertEqual(len(ontology_definition_items), 1)
        self.assertEqual(queue["item_count"], 3)
        self.assertEqual(
            set(ontology_definition_items[0]["covered_blockers"]),
            {
                "review_debt:candidate_definition_ratio",
                "review_debt:human_reviewed_concepts",
            },
        )
        self.assertEqual(
            ontology_definition_items[0]["blocker_display_label"],
            "review_debt:candidate_definition_ratio",
        )
        self.assertIn(
            "needs explicit human review: ontology concept definitions",
            ontology_definition_items[0]["covered_blocker_display_labels"],
        )
        self.assertEqual(
            ontology_definition_items[0]["expected_artifacts"],
            [
                "reports/aihr_ontology_definition_review_seedpack_20260624.jsonl",
                "reports/aihr_ontology_definition_review_seedpack_20260624.md",
                "reports/aihr_ontology_definition_review_seedpack_20260624.csv",
            ],
        )
        self.assertIn(
            f"--source-report-path {review_priority_markdown_path}",
            ontology_definition_items[0]["command"],
        )
        self.assertNotIn(
            "reports\\aihr_review_priority_20260624.md",
            ontology_definition_items[0]["command"],
        )
        goal_review_items = [
            item for item in queue["items"] if item["blocker"] == "review_debt:human_reviewed_goal_links"
        ]
        self.assertEqual(len(goal_review_items), 1)
        self.assertEqual(
            goal_review_items[0]["blocker_display_label"],
            "needs explicit human review: training-goal KSA links",
        )
        self.assertIn(
            f"--review-priority-report {review_priority_report_path}",
            goal_review_items[0]["command"],
        )
        self.assertTrue(goal_review_items[0]["auto_runnable"])
        self.assertEqual(goal_review_items[0]["mutation_policy"], "regenerate_reports_only")
        self.assertTrue(ontology_definition_items[0]["auto_runnable"])
        self.assertEqual(ontology_definition_items[0]["mutation_policy"], "regenerate_reports_only")
        self.assertFalse(ontology_definition_items[0]["requires_human_decision"])
        self.assertIn(
            "status_update_allowed remains false",
            " ".join(ontology_definition_items[0]["acceptance_checks"]),
        )
        self.assertFalse(
            any(
                item["auto_runnable"] and item["requires_human_decision"]
                for item in queue["items"]
            )
        )

    def test_agent_queue_transition_seedpack_uses_valid_flags_and_markdown_output(self) -> None:
        gates = required_quality_gates()
        gates[-1] = {
            "name": "transition_eval:trusted_scenarios",
            "status": "warn",
            "message": "Trusted transition scenarios gate.",
            "value": 1,
            "threshold": ">= 10",
        }
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260624",
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        transition_item = queue_by_blocker["transition_eval:trusted_scenarios"]
        self.assertIn("export-transition-scenario-seedpack", transition_item["command"])
        self.assertIn("--scenario-limit 20", transition_item["command"])
        self.assertIn("--recommendation-limit 5", transition_item["command"])
        self.assertNotIn("--limit 100", transition_item["command"])
        self.assertFalse(transition_item["auto_runnable"])
        self.assertEqual(transition_item["mutation_policy"], "requires_human_decision")
        self.assertIn("Prepare transition scenario review seedpack", transition_item["action"])
        self.assertEqual(
            transition_item["expected_artifacts"],
            [
                "reports/aihr_transition_scenario_seedpack_20260624.jsonl",
                "reports/aihr_transition_scenario_seedpack_20260624.md",
            ],
        )

    def test_agent_queue_review_seedpack_includes_csv_decision_sheet(self) -> None:
        gates = required_quality_gates()
        gates[3] = {
            "name": "review_debt:human_reviewed_task_relations",
            "status": "warn",
            "message": "human reviewed task relations gate.",
            "value": 0,
            "threshold": "> 0",
        }
        release_readiness_markdown_path = Path("reports") / "aihr_release_readiness_20260624_autoresolve_2.md"
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            release_readiness_markdown_path=release_readiness_markdown_path,
            artifact_date="20260624",
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        seedpack_item = queue_by_blocker["review_debt:human_reviewed_task_relations"]
        self.assertIn("export-review-seedpack", seedpack_item["command"])
        self.assertIn(
            "--issue-types ontology_training_goal_link_human_review_required,hr_training_goal_link_human_review_required,ontology_task_ksa_relation_human_review_required,hr_core_concept_human_review_required,ontology_core_concept_human_review_required",
            seedpack_item["command"],
        )
        self.assertIn("--csv-out", seedpack_item["command"])
        self.assertIn("reports\\aihr_review_priority_20260624.md", seedpack_item["command"])
        self.assertNotIn("reports\\aihr_release_readiness_20260624.md", seedpack_item["command"])
        self.assertEqual(
            seedpack_item["expected_artifacts"],
            [
                "reports/aihr_review_seedpack_blocker_ranked_20260624.jsonl",
                "reports/aihr_review_seedpack_blocker_ranked_20260624.md",
                "reports/aihr_review_seedpack_blocker_ranked_20260624.csv",
            ],
        )
        self.assertTrue(seedpack_item["auto_runnable"])
        self.assertEqual(seedpack_item["mutation_policy"], "regenerate_reports_only")
        self.assertFalse(seedpack_item["requires_human_decision"])
        self.assertIn("do not promote review statuses", seedpack_item["action"])
        self.assertIn(
            "unless a human decides otherwise",
            " ".join(seedpack_item["acceptance_checks"]),
        )

    def test_agent_queue_artifacts_use_requested_date_stamp(self) -> None:
        gates = required_quality_gates()
        gates[2] = {
            "name": "review_debt:human_reviewed_goal_links",
            "status": "warn",
            "message": "human_reviewed_goal_links is still zero.",
            "value": 0,
            "threshold": "> 0",
        }
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 1},
                "gates": gates,
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260618",
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        triage_item = queue_by_blocker["review_debt:human_reviewed_goal_links"]
        serialized = json.dumps(triage_item, ensure_ascii=False)
        self.assertIn("20260618", serialized)
        self.assertNotIn("20260617", serialized)
        self.assertIn("aihr_quality_gates_with_transition_20260618.json", serialized)
        self.assertIn("aihr_review_triage_20260618.md", serialized)

    def test_build_release_readiness_fails_closed_when_required_quality_gates_are_missing(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": [
                {
                    "name": "transition_eval:trusted_scenarios",
                    "status": "pass",
                    "value": 10,
                }
            ],
        }
        contract = valid_mcp_contract()

        report = build_release_readiness(
            quality_report,
            contract,
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
        )

        blocker_names = {blocker["name"] for blocker in report["blockers"]}
        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn(
            "missing_quality_gate:qualification:collection_coverage",
            blocker_names,
        )
        self.assertIn(
            "missing_quality_gate:review_debt:human_reviewed_concepts",
            blocker_names,
        )
        self.assertIn(
            "quality-gates --include-transition-eval",
            " ".join(item["command"] for item in report["next_actions"]),
        )

    def test_build_release_readiness_treats_demo_contract_failures_as_blockers(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": required_quality_gates(),
        }
        contract = valid_mcp_contract()
        demo_contract = {
            "ok": False,
            "failure_count": 1,
            "failures": [{"path": "reports/demo.html", "check": "No sensitive payload markers"}],
        }

        report = build_release_readiness(
            quality_report,
            contract,
            demo_contract=demo_contract,
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            min_trusted_scenarios=0,
            artifact_date="20260624",
        )

        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn("aihr_demo_contract", {blocker["name"] for blocker in report["blockers"]})
        self.assertIn("run-aihr-plan-demo", report["next_actions"][0]["command"])
        demo_queue_item = report["agent_work_queue"]["items"][0]
        self.assertIn("reports/aihr_plan_demo_internal_20260624.json", demo_queue_item["expected_artifacts"])
        self.assertIn("reports/aihr_plan_demo_alias_internal_20260624.json", demo_queue_item["expected_artifacts"])

    def test_build_release_readiness_blocks_demo_json_missing_planner_evidence_fields(self) -> None:
        payload = valid_public_demo_payload()
        payload.pop("recommended_path")
        row = payload["training_system_matrix"][0]
        row.pop("task_ksa_basis")
        row.pop("facility_constraint_fit")
        row.pop("human_review")

        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "demo.json"
            json_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            demo_contract = build_aihr_demo_contract([json_path])

        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=demo_contract,
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            min_trusted_scenarios=0,
        )

        self.assertFalse(report["release_ready"])
        self.assertIn("aihr_demo_contract", {blocker["name"] for blocker in report["blockers"]})
        demo_blocker = next(blocker for blocker in report["blockers"] if blocker["name"] == "aihr_demo_contract")
        failure_checks = {failure["check"] for failure in demo_blocker["details"]["failures"]}
        self.assertIn("Recommended path", failure_checks)
        self.assertIn("Planner row contract", failure_checks)
        self.assertIn("Planner matrix fields", failure_checks)

    def test_build_release_readiness_treats_dashboard_surface_failures_as_blockers(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": required_quality_gates(),
        }
        contract = valid_mcp_contract()
        dashboard_surface_contract = {
            "ok": False,
            "failure_count": 1,
            "failures": [{"check": "Live plan scenario contracts", "detail": "baseline"}],
        }

        report = build_release_readiness(
            quality_report,
            contract,
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_surface_contract,
            dashboard_static_artifact_dir=Path("reports/overnight_sessions/readonly_refresh"),
            min_trusted_scenarios=0,
        )

        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn("aihr_dashboard_surface", {blocker["name"] for blocker in report["blockers"]})
        self.assertIn("verify-aihr-dashboard", report["next_actions"][0]["command"])
        self.assertIn(
            "--static-artifact-dir reports/overnight_sessions/readonly_refresh",
            report["next_actions"][0]["command"].replace("\\", "/"),
        )
        queue_item = report["agent_work_queue"]["items"][0]
        self.assertEqual(queue_item["owner"], "aihr-demo-runner-agent")
        self.assertEqual(queue_item["mutation_policy"], "requires_existing_artifacts")
        self.assertFalse(queue_item["auto_runnable"])
        self.assertIn("Dashboard verification checks live planner", " ".join(queue_item["acceptance_checks"]))

    def test_build_release_readiness_surfaces_human_review_provenance_gap_blocker(self) -> None:
        static_artifacts = valid_static_artifacts()
        checkpoint_artifact = next(
            item
            for item in static_artifacts
            if item["name"] == "human_review_safe_ops_checkpoint_json"
        )
        checkpoint_artifact["checkpoint"].update(
            {
                "ok": False,
                "contract_ok": False,
                "legacy_trusted_status_rows_pending_reconfirmation": 34,
                "rows_without_packet_backed_provenance": 34,
                "provenance_gap_present": True,
                "unresolved_provenance_gap": True,
                "reconfirmation_blank_decision_count": 34,
            }
        )
        dashboard_surface_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {"static_artifacts": static_artifacts},
        }

        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_surface_contract,
            min_trusted_scenarios=0,
            artifact_date="20260629",
        )

        blocker_names = {blocker["name"] for blocker in report["blockers"]}
        self.assertNotIn("aihr_dashboard_surface", blocker_names)
        self.assertIn("human_review:provenance_reconfirmation_required", blocker_names)
        provenance_blocker = next(
            blocker
            for blocker in report["blockers"]
            if blocker["name"] == "human_review:provenance_reconfirmation_required"
        )
        self.assertEqual(provenance_blocker["value"], 34)
        self.assertEqual(
            provenance_blocker["details"]["reconfirmation_blank_decision_count"],
            34,
        )

        queue_by_blocker = {item["blocker"]: item for item in report["agent_work_queue"]["items"]}
        provenance_item = queue_by_blocker["human_review:provenance_reconfirmation_required"]
        self.assertEqual(provenance_item["owner"], "ontology-review-agent")
        self.assertTrue(provenance_item["auto_runnable"])
        self.assertFalse(provenance_item["requires_human_decision"])
        self.assertEqual(provenance_item["mutation_policy"], "regenerate_reports_only")
        self.assertIn(
            "export-human-review-provenance-reconfirmation-proofset",
            provenance_item["command"],
        )
        self.assertIn(
            "reports/human_review_provenance_reconfirmation_packet_20260629.json",
            provenance_item["expected_artifacts"],
        )
        self.assertIn(
            "reports/human_review_provenance_reconfirmation_decision_sheet_20260629.json",
            provenance_item["expected_artifacts"],
        )
        self.assertIn(
            "reports/human_review_provenance_reconfirmation_decision_audit_20260629.json",
            provenance_item["expected_artifacts"],
        )
        self.assertIn(
            "source packet hash",
            " ".join(provenance_item["acceptance_checks"]),
        )

    def test_build_release_readiness_surfaces_provenance_lineage_break_as_specific_blocker(
        self,
    ) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_safe_ops_checkpoint_json":
                item["checkpoint"]["provenance_gap_present"] = False
                item["checkpoint"]["unresolved_provenance_gap"] = False
                item["checkpoint"]["rows_without_packet_backed_provenance"] = 0
                item["checkpoint"]["reconfirmation_blank_decision_count"] = 0
                item["checkpoint"]["legacy_trusted_status_rows_pending_reconfirmation"] = 0
            if item["name"] == "human_review_provenance_reconfirmation_decision_sheet_json":
                item["human_review_provenance_reconfirmation_decision_sheet"][
                    "source_packet_sha256"
                ] = "sha256:different"
        with tempfile.TemporaryDirectory() as tmp:
            dashboard_path = Path(tmp) / "dashboard_verification.json"
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            dashboard_surface_contract = build_dashboard_surface_contract(dashboard_path)

            report = build_release_readiness(
                {
                    "status": "pass",
                    "summary": {"fail_count": 0, "warn_count": 0},
                    "gates": required_quality_gates(),
                },
                valid_mcp_contract(),
                demo_contract=valid_aihr_demo_contract(),
                dashboard_surface_contract=dashboard_surface_contract,
                min_trusted_scenarios=0,
                artifact_date="20260629",
            )

        blocker_names = {blocker["name"] for blocker in report["blockers"]}
        self.assertIn("aihr_dashboard_surface", blocker_names)
        self.assertIn("human_review:provenance_reconfirmation_required", blocker_names)
        provenance_blocker = next(
            blocker
            for blocker in report["blockers"]
            if blocker["name"] == "human_review:provenance_reconfirmation_required"
        )
        self.assertTrue(provenance_blocker["details"]["lineage_mismatch"])

    def test_build_release_readiness_keeps_provenance_lineage_detail_when_gap_also_exists(
        self,
    ) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_safe_ops_checkpoint_json":
                item["checkpoint"].update(
                    {
                        "ok": False,
                        "contract_ok": False,
                        "legacy_trusted_status_rows_pending_reconfirmation": 34,
                        "rows_without_packet_backed_provenance": 34,
                        "provenance_gap_present": True,
                        "unresolved_provenance_gap": True,
                        "reconfirmation_blank_decision_count": 34,
                    }
                )
            if item["name"] == "human_review_provenance_reconfirmation_decision_sheet_json":
                item["human_review_provenance_reconfirmation_decision_sheet"][
                    "source_packet_sha256"
                ] = "sha256:different"
        dashboard_surface_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {"static_artifacts": static_artifacts},
        }

        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_surface_contract,
            min_trusted_scenarios=0,
            artifact_date="20260629",
        )

        provenance_blocker = next(
            blocker
            for blocker in report["blockers"]
            if blocker["name"] == "human_review:provenance_reconfirmation_required"
        )
        self.assertEqual(provenance_blocker["value"], 34)
        self.assertEqual(
            provenance_blocker["details"]["rows_without_packet_backed_provenance"],
            34,
        )
        self.assertTrue(provenance_blocker["details"]["lineage_mismatch"])
        self.assertGreater(provenance_blocker["details"]["proofset_artifact_issue_count"], 0)
        self.assertEqual(
            provenance_blocker["details"]["lineage_hashes"]["decision_sheet"],
            "sha256:different",
        )

    def test_build_release_readiness_uses_review_chain_counts_for_provenance_blocker(
        self,
    ) -> None:
        static_artifacts = valid_static_artifacts()
        for item in static_artifacts:
            if item["name"] == "human_review_safe_ops_checkpoint_json":
                item["checkpoint"].update(
                    {
                        "ok": False,
                        "contract_ok": False,
                        "legacy_trusted_status_rows_pending_reconfirmation": None,
                        "rows_without_packet_backed_provenance": 0,
                        "provenance_gap_present": False,
                        "unresolved_provenance_gap": True,
                        "reconfirmation_blank_decision_count": 0,
                    }
                )
        review_chain_summary = valid_review_chain_safety_summary()
        review_chain_summary["legacy_status_needs_reconfirmation_count"] = 34
        review_chain_summary["rows_without_packet_backed_provenance"] = 34
        review_chain_summary["pending_decision_count"] = 34
        review_chain_summary["blank_decision_count"] = 34
        dashboard_surface_contract = {
            "ok": True,
            "failure_count": 0,
            "failures": [],
            "artifact": {
                "static_artifacts": static_artifacts,
                "review_chain_safety_summary": review_chain_summary,
            },
        }

        report = build_release_readiness(
            {
                "status": "pass",
                "summary": {"fail_count": 0, "warn_count": 0},
                "gates": required_quality_gates(),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=dashboard_surface_contract,
            min_trusted_scenarios=0,
            artifact_date="20260629",
        )

        provenance_blocker = next(
            blocker
            for blocker in report["blockers"]
            if blocker["name"] == "human_review:provenance_reconfirmation_required"
        )
        self.assertEqual(provenance_blocker["value"], 34)
        self.assertEqual(
            provenance_blocker["details"]["rows_without_packet_backed_provenance"],
            0,
        )
        self.assertEqual(
            provenance_blocker["details"][
                "source_audit_rows_without_packet_backed_provenance"
            ],
            0,
        )
        self.assertEqual(
            provenance_blocker["details"][
                "canonical_provenance_reconfirmation_blocker_count"
            ],
            34,
        )
        self.assertEqual(
            provenance_blocker["details"][
                "legacy_trusted_status_rows_pending_reconfirmation"
            ],
            0,
        )
        self.assertEqual(
            provenance_blocker["details"]["legacy_status_needs_reconfirmation_count"],
            34,
        )
        self.assertEqual(
            provenance_blocker["details"]["pending_decision_count"],
            34,
        )

    def test_build_release_readiness_requires_aihr_demo_and_dashboard_contracts(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": required_quality_gates(),
        }

        report = build_release_readiness(
            quality_report,
            valid_mcp_contract(),
            min_trusted_scenarios=0,
        )

        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        blocker_names = {blocker["name"] for blocker in report["blockers"]}
        self.assertIn("aihr_demo_contract", blocker_names)
        self.assertIn("aihr_dashboard_surface", blocker_names)

    def test_build_release_readiness_treats_query_router_contract_failures_as_hygiene_blockers(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": required_quality_gates(),
        }
        contract = valid_mcp_contract()
        contract.pop("query_router")

        report = build_release_readiness(
            quality_report,
            contract,
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            min_trusted_scenarios=0,
        )

        self.assertFalse(report["release_ready"])
        self.assertFalse(report["engineering_hygiene_ok"])
        self.assertIn("Query router present", {blocker["name"] for blocker in report["blockers"]})

    def test_build_release_readiness_accepts_real_exported_mcp_contract_shape(self) -> None:
        quality_report = {
            "status": "pass",
            "summary": {"fail_count": 0, "warn_count": 0},
            "gates": required_quality_gates(),
        }

        report = build_release_readiness(
            quality_report,
            build_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
        )

        self.assertTrue(report["release_ready"])
        self.assertTrue(report["engineering_hygiene_ok"])
        self.assertEqual(report["schema"], "aihr_release_readiness_v1")
        self.assertEqual(report["release_decision"]["status"], "release_candidate_ready")
        self.assertTrue(report["release_decision"]["release_ready"])
        self.assertFalse(report["release_decision"]["approval_claim"])
        self.assertEqual(report["blockers"], [])
        self.assertEqual(report["next_actions"], [])
        self.assertEqual(report["agent_work_queue"]["items"], [])
        self.assertTrue(all(check["ok"] for check in report["checks"]["mcp_contract"]))

    def test_build_agent_work_queue_handles_unknown_owner_with_project_maintainer(self) -> None:
        queue = build_agent_work_queue(
            {
                "release_ready": False,
                "engineering_hygiene_ok": False,
                "blockers": [{"name": "custom", "category": "engineering_hygiene"}],
                "next_actions": [
                    {
                        "owner": "unknown-agent",
                        "blocker": "custom",
                        "action": "Inspect.",
                        "command": "python scripts\\ncs_harness.py inspect",
                    }
                ],
            }
        )

        self.assertEqual(queue["item_count"], 1)
        self.assertEqual(queue["items"][0]["agent_file"], "AGENTS.md")
        self.assertFalse(queue["items"][0]["auto_runnable"])
        self.assertEqual(queue["items"][0]["mutation_policy"], "inspect_only")

    def test_agent_queue_markdown_shows_queue_status_auto_start_contract(self) -> None:
        queue = {
            "schema": "aihr_agent_work_queue_v1",
            "release_ready": False,
            "engineering_hygiene_ok": False,
            "item_count": 1,
            "global_guardrails": [],
            "items": [
                {
                    "id": "manual-review",
                    "owner": "project-maintainer",
                    "agent_file": "AGENTS.md",
                    "priority": 5,
                    "blocker": "custom",
                    "covered_blockers": ["custom"],
                    "blocker_category": "engineering_hygiene",
                    "auto_runnable": False,
                    "mutation_policy": "inspect_only",
                    "requires_human_decision": False,
                    "action": "Inspect queue.",
                    "command": "python scripts\\ncs_harness.py inspect",
                    "preflight": {"can_start_automated": False, "state": "manual_ready"},
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            markdown_path = Path(tmp) / "queue.md"
            write_agent_queue_markdown(queue, markdown_path)
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertIn("automation_contract", markdown)
        self.assertIn("queue_status_can_start_automated: false", markdown)

    def test_release_markdown_uses_neutral_blocker_display_labels(self) -> None:
        report = build_release_readiness(
            {
                "status": "warn",
                "summary": {"fail_count": 0, "warn_count": 4},
                "gates": required_quality_gates(human_review_status="warn"),
            },
            valid_mcp_contract(),
            demo_contract=valid_aihr_demo_contract(),
            dashboard_surface_contract=valid_dashboard_surface_contract(),
            artifact_date="20260624",
        )
        with tempfile.TemporaryDirectory() as tmp:
            release_markdown_path = Path(tmp) / "release.md"
            queue_markdown_path = Path(tmp) / "queue.md"
            write_markdown(report, release_markdown_path)
            write_agent_queue_markdown(report["agent_work_queue"], queue_markdown_path)
            release_markdown = release_markdown_path.read_text(encoding="utf-8")
            queue_markdown = queue_markdown_path.read_text(encoding="utf-8")

        goal_link_blocker = next(
            blocker
            for blocker in report["blockers"]
            if blocker["name"] == "review_debt:human_reviewed_goal_links"
        )
        self.assertEqual(
            goal_link_blocker["display_label"],
            "needs explicit human review: training-goal KSA links",
        )
        self.assertEqual(
            goal_link_blocker["display_message"],
            "Packet-backed manual-review evidence count for training-goal KSA links is still zero.",
        )
        self.assertIn("needs explicit human review: training-goal KSA links", release_markdown)
        self.assertIn(
            "Packet-backed manual-review evidence count for training-goal KSA links is still zero.",
            release_markdown,
        )
        self.assertIn("machine: `review_debt:human_reviewed_goal_links`", release_markdown)
        self.assertNotIn("human_reviewed_goal_links is still zero", release_markdown)
        self.assertIn("needs explicit human review: task-KSA relations", queue_markdown)
        self.assertIn("blocker_display_label:", queue_markdown)

    def test_main_derives_agent_queue_artifact_date_from_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            contract_path = tmp_path / "contract.json"
            out_path = tmp_path / "aihr_release_readiness_20260618.json"
            queue_path = tmp_path / "aihr_agent_queue_20260618.json"
            queue_markdown_path = tmp_path / "aihr_agent_queue_20260618.md"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--out",
                        str(out_path),
                    ]
                )

            queue_text = queue_path.read_text(encoding="utf-8")
            queue_markdown_text = queue_markdown_path.read_text(encoding="utf-8")
            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_work_queue_path"], str(queue_path))
        self.assertEqual(payload["agent_work_queue_markdown_path"], str(queue_markdown_path))
        self.assertIn("20260618", queue_text)
        self.assertNotIn("20260617", queue_text)
        self.assertIn("automation_contract", queue_markdown_text)

    def test_main_preserves_agent_queue_artifact_stamp_suffix_from_output_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "aihr_quality_gates_with_transition_20260629_8h.json"
            contract_path = tmp_path / "mcp_tool_contract_20260629_8h.json"
            out_path = tmp_path / "aihr_release_readiness_20260629_8h.json"
            queue_path = tmp_path / "aihr_agent_queue_20260629_8h.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "warn",
                        "summary": {"fail_count": 0, "warn_count": 3},
                        "gates": required_quality_gates(human_review_status="warn"),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--out",
                        str(out_path),
                    ]
                )

            payload = json.loads(stdout.getvalue())
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            serialized_queue = json.dumps(queue, ensure_ascii=False)

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["agent_work_queue_path"], str(queue_path))
        self.assertIn("aihr_ontology_definition_review_seedpack_20260629_8h.jsonl", serialized_queue)
        self.assertIn("aihr_review_triage_20260629_8h.json", serialized_queue)
        self.assertIn("aihr_review_seedpack_blocker_ranked_20260629_8h.jsonl", serialized_queue)
        self.assertIn("aihr_transition_scenario_seedpack_20260629_8h.jsonl", serialized_queue)
        self.assertNotIn("aihr_review_triage_20260629.json", serialized_queue)
        self.assertNotIn("aihr_review_seedpack_blocker_ranked_20260629.jsonl", serialized_queue)

    def test_main_rejects_mixed_same_day_proof_artifact_stamps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "aihr_quality_gates_with_transition_20260629_8h.json"
            contract_path = tmp_path / "mcp_tool_contract_20260629_8h.json"
            demo_json_path = tmp_path / "aihr_plan_demo_20260629_2h.json"
            demo_html_path = tmp_path / "aihr_plan_demo_20260629_2h.html"
            out_path = tmp_path / "aihr_release_readiness_20260629_8h.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_json_path.write_text(
                json.dumps(valid_public_demo_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_html_path.write_text(
                "<html><head><title>AI-HR Education Plan Demo</title></head>"
                "<body><h1>AI-HR \uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub370\ubaa8</h1>"
                "<h2>Training-System Summary</h2><h2>Training-System Matrix</h2>"
                "<h2>2026 Guide Trace</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
                "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
                "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
                "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
                "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
                "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span></body></html>",
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--demo-json",
                        str(demo_json_path),
                        "--demo-html",
                        str(demo_html_path),
                        "--out",
                        str(out_path),
                    ]
                )

        payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["release_ready"])
        blocker_names = {blocker["name"] for blocker in payload["blockers"]}
        self.assertIn("artifact_date:proof_artifacts", blocker_names)
        proof_artifacts = payload["artifact_date_contract"]["proof_artifacts"]
        self.assertFalse(proof_artifacts["ok"])
        self.assertEqual(proof_artifacts["expected_date"], "20260629")
        self.assertEqual(proof_artifacts["expected_stamp"], "20260629_8h")
        self.assertIn("demo_json[0]", proof_artifacts["mismatched_stamp_roles"])
        self.assertIn("demo_html", proof_artifacts["mismatched_stamp_roles"])
        self.assertNotIn("demo_json[0]", proof_artifacts["mismatched_roles"])

    def test_main_preserves_artifact_stamp_suffix_in_markdown_prerequisites(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "aihr_quality_gates_with_transition_20260629_8h.json"
            contract_path = tmp_path / "mcp_tool_contract_20260629_8h.json"
            out_path = tmp_path / "aihr_release_readiness_20260629_8h.json"
            markdown_path = tmp_path / "aihr_release_readiness_20260629_8h.md"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "warn",
                        "summary": {"fail_count": 0, "warn_count": 1},
                        "gates": required_quality_gates(
                            human_review_status="pass",
                            qualification_value=0.39,
                        ),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--out",
                        str(out_path),
                        "--markdown-out",
                        str(markdown_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "qualification:collection_coverage",
            {blocker["name"] for blocker in payload["blockers"]},
        )
        self.assertIn("qualification_retry_hygiene_20260629_8h.json", markdown)
        self.assertNotIn("qualification_retry_hygiene_20260629.json", markdown)

    def test_main_accepts_scoped_review_readability_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "aihr_quality_gates_with_transition_20260618.json"
            contract_path = tmp_path / "contract.json"
            readability_path = tmp_path / "review_artifact_readability_audit_20260618.json"
            out_path = tmp_path / "aihr_release_readiness_20260618.json"
            markdown_path = tmp_path / "aihr_release_readiness_20260618.md"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            readability_path.write_text(
                json.dumps(
                    {
                        "schema": "review_artifact_readability_audit_v1",
                        "ok": False,
                        "status": "review_required",
                        "status_update_allowed": False,
                        "db_writes": False,
                        "approval_claim": False,
                        "human_decision_required": True,
                        "artifact_count": 2,
                        "finding_count": 1,
                        "findings": [
                            {
                                "path": str(quality_path),
                                "severity": "high",
                                "code": "non_utf8_bom_detected",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--review-readability-audit",
                        str(readability_path),
                        "--out",
                        str(out_path),
                        "--markdown-out",
                        str(markdown_path),
                    ]
                )
            payload = json.loads(stdout.getvalue())
            markdown = markdown_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertIn(
            "review_artifact:readability_audit",
            {blocker["name"] for blocker in payload["blockers"]},
        )
        self.assertEqual(
            payload["review_readability_contract"]["artifact"]["blocking_finding_count"],
            1,
        )
        self.assertIn("review_readability_audit", payload["artifact_date_contract"]["proof_artifacts"]["path_dates"])
        self.assertIn("## Review Artifact Readability", markdown)

    def test_main_rejects_conflicting_agent_queue_artifact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            contract_path = tmp_path / "contract.json"
            out_path = tmp_path / "aihr_release_readiness_20260618.json"
            queue_path = tmp_path / "aihr_agent_queue_20260617.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--out",
                        str(out_path),
                        "--agent-queue-out",
                        str(queue_path),
                    ]
                )

        payload = json.loads(stdout.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["release_ready"])
        self.assertFalse(payload["engineering_hygiene_ok"])
        self.assertIn("agent_work_queue", payload)
        blocker_names = {blocker["name"] for blocker in payload["blockers"]}
        self.assertIn("artifact_date:release_outputs", blocker_names)
        release_outputs = payload["artifact_date_contract"]["release_outputs"]
        self.assertFalse(release_outputs["ok"])
        self.assertEqual(release_outputs["expected_date"], "20260618")
        self.assertIn("agent_queue_json", release_outputs["mismatched_roles"])

    def test_main_rejects_conflicting_proof_artifact_dates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "aihr_quality_gates_with_transition_20260618.json"
            contract_path = tmp_path / "contract.json"
            demo_json_path = tmp_path / "aihr_plan_demo_20260617.json"
            demo_html_path = tmp_path / "aihr_plan_demo_20260617.html"
            dashboard_path = tmp_path / "aihr_dashboard_surface_verification_20260617.json"
            out_path = tmp_path / "aihr_release_readiness_20260618.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_json_path.write_text(
                json.dumps(valid_public_demo_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_html_path.write_text(
                "<html><head><title>AI-HR Education Plan Demo</title></head>"
                "<body><h1>AI-HR \uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub370\ubaa8</h1>"
                "<h2>Training-System Summary</h2><h2>Training-System Matrix</h2>"
                "<h2>2026 Guide Trace</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
                "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
                "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
                "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
                "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
                "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span></body></html>",
                encoding="utf-8",
            )
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline", matrix_rows=1),
                            valid_live_plan_summary("extra", matrix_rows=1),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--demo-json",
                        str(demo_json_path),
                        "--demo-html",
                        str(demo_html_path),
                        "--dashboard-verification",
                        str(dashboard_path),
                        "--out",
                        str(out_path),
                    ]
                )

        payload = json.loads(stdout.getvalue())

        self.assertTrue(payload["ok"])
        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["release_ready"])
        self.assertFalse(payload["engineering_hygiene_ok"])
        self.assertIn("agent_work_queue", payload)
        blocker_names = {blocker["name"] for blocker in payload["blockers"]}
        self.assertIn("artifact_date:proof_artifacts", blocker_names)
        proof_artifacts = payload["artifact_date_contract"]["proof_artifacts"]
        self.assertFalse(proof_artifacts["ok"])
        self.assertEqual(proof_artifacts["expected_date"], "20260618")
        self.assertIn("demo_json[0]", proof_artifacts["mismatched_roles"])
        self.assertIn("demo_html", proof_artifacts["mismatched_roles"])
        self.assertIn("dashboard_verification", proof_artifacts["mismatched_roles"])

    def test_main_blocks_stale_dashboard_queue_run_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            contract_path = tmp_path / "contract.json"
            demo_json_path = tmp_path / "demo.json"
            demo_html_path = tmp_path / "demo.html"
            dashboard_path = tmp_path / "dashboard_verification.json"
            out_path = tmp_path / "readiness.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_json_path.write_text(
                json.dumps(valid_public_demo_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_html_path.write_text(
                "<html><head><title>AI-HR Education Plan Demo</title></head>"
                "<body><h1>AI-HR \uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub370\ubaa8</h1>"
                "<h2>Training-System Summary</h2><h2>Training-System Matrix</h2>"
                "<h2>2026 Guide Trace</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
                "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
                "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
                "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
                "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
                "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span></body></html>",
                encoding="utf-8",
            )
            stale_endpoint_checks = [
                {
                    key: value
                    for key, value in item.items()
                    if key not in {"actual_run", "output_tails_suppressed", "output_issues"}
                }
                if item.get("name") == "agent_queue_run_api"
                else item
                for item in valid_dashboard_endpoint_checks()
            ]
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": stale_endpoint_checks,
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline", matrix_rows=1),
                            valid_live_plan_summary("extra", matrix_rows=1),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--demo-json",
                        str(demo_json_path),
                        "--demo-html",
                        str(demo_html_path),
                        "--dashboard-verification",
                        str(dashboard_path),
                        "--out",
                        str(out_path),
                    ]
                )

            printed = json.loads(stdout.getvalue())
            written = json.loads(out_path.read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(written, printed)
        self.assertFalse(written["release_ready"])
        self.assertFalse(written["engineering_hygiene_ok"])
        blocker = next(blocker for blocker in written["blockers"] if blocker["name"] == "aihr_dashboard_surface")
        failure_names = {failure["check"] for failure in blocker["details"]["failures"]}
        self.assertIn("Queue run API actual execution evidence", failure_names)

    def test_main_rejects_same_date_stale_dashboard_verification_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality_20260629.json"
            contract_path = tmp_path / "contract_20260629.json"
            demo_json_path = tmp_path / "demo_20260629.json"
            demo_html_path = tmp_path / "demo_20260629.html"
            dashboard_path = tmp_path / "dashboard_verification_20260629.json"
            out_path = tmp_path / "aihr_release_readiness_20260629.json"
            queue_path = tmp_path / "aihr_agent_queue_20260629.json"
            stale_release_path = "reports/aihr_release_readiness_20260629_stale.json"
            stale_queue_path = "reports/aihr_agent_queue_20260629_stale.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_json_path.write_text(
                json.dumps(valid_public_demo_payload(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_html_path.write_text(
                "<html><head><title>AI-HR Education Plan Demo</title></head>"
                "<body><h1>AI-HR \uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub370\ubaa8</h1>"
                "<h2>Training-System Summary</h2><h2>Training-System Matrix</h2>"
                "<h2>2026 Guide Trace</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
                "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
                "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
                "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
                "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
                "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span></body></html>",
                encoding="utf-8",
            )
            static_artifacts = valid_static_artifacts()
            for item in static_artifacts:
                if isinstance(item.get("path"), str):
                    item["path"] = item["path"].replace("20260624", "20260629")
                if item.get("name") == "readiness_json":
                    item["path"] = stale_release_path
                    item["release_readiness"]["agent_work_queue_path"] = stale_queue_path
                if item.get("name") == "queue_status_json":
                    item["queue_status"]["source_queue_path"] = stale_queue_path
                if item.get("name") == "queue_run_json":
                    item["queue_run"]["source_queue_path"] = stale_queue_path
            endpoint_checks = valid_dashboard_endpoint_checks()
            for item in endpoint_checks:
                if item.get("name") == "queue_status_api":
                    item["source_queue_path"] = stale_queue_path
                if item.get("name") == "agent_queue_run_api":
                    item["source_queue_path"] = stale_queue_path
                if item.get("name") == "live_queue_source_path_consistency":
                    item["release_readiness_queue_path"] = stale_queue_path
                    item["queue_status_api_source_queue_path"] = stale_queue_path
                    item["agent_queue_run_api_source_queue_path"] = stale_queue_path
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": endpoint_checks,
                        "review_chain_safety_summary": valid_review_chain_safety_summary(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": static_artifacts,
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline"),
                            valid_live_plan_summary("extra"),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--demo-json",
                        str(demo_json_path),
                        "--demo-html",
                        str(demo_html_path),
                        "--dashboard-verification",
                        str(dashboard_path),
                        "--out",
                        str(out_path),
                        "--agent-queue-out",
                        str(queue_path),
                    ]
                )

            payload = json.loads(stdout.getvalue())

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["engineering_hygiene_ok"])
        blocker_names = {blocker["name"] for blocker in payload["blockers"]}
        self.assertIn("artifact_lineage:dashboard_verification", blocker_names)
        lineage = payload["artifact_lineage_contract"]
        self.assertFalse(lineage["ok"])
        self.assertFalse(lineage["release_path_ok"])
        self.assertFalse(lineage["queue_path_ok"])

    def test_main_writes_same_json_shape_that_it_prints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            quality_path = tmp_path / "quality.json"
            contract_path = tmp_path / "contract.json"
            out_path = tmp_path / "readiness.json"
            markdown_path = tmp_path / "readiness.md"
            queue_path = tmp_path / "agent_queue.json"
            queue_markdown_path = tmp_path / "agent_queue.md"
            demo_json_path = tmp_path / "demo.json"
            demo_html_path = tmp_path / "demo.html"
            dashboard_path = tmp_path / "dashboard_verification.json"
            quality_path.write_text(
                json.dumps(
                    {
                        "status": "pass",
                        "summary": {"fail_count": 0, "warn_count": 0},
                        "gates": required_quality_gates(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            contract_path.write_text(
                json.dumps(valid_mcp_contract(), ensure_ascii=False),
                encoding="utf-8",
            )
            demo_json_path.write_text(
                json.dumps(
                    valid_public_demo_payload(),
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            demo_html_path.write_text(
                "<html><head><title>AI-HR Education Plan Demo</title></head>"
                "<body><h1>AI-HR \uad50\uc721\ud6c8\ub828\uccb4\uacc4 \ub370\ubaa8</h1>"
                "<h2>Training-System Summary</h2><h2>Training-System Matrix</h2>"
                "<h2>2026 Guide Trace</h2><h2>Scope Baseline</h2><span>scope_baseline</span>"
                "<h2>Course Intake Requirements</h2><span>course_intake_requirements</span><span>aihr_course_intake_requirements_v1</span><h2>Recommended Path</h2>"
                "<h2>Training Course Inventory Template</h2><span>training_course_inventory_template</span><span>aihr_training_course_inventory_template_v1</span>"
                "<h2>Training Necessity Review</h2><span>training_necessity_review</span><span>aihr_training_necessity_review_v1</span>"
                "<h2>Annual Operation Plan Seed</h2><span>annual_operation_plan</span><span>aihr_annual_operation_plan_seed_v1</span>"
                "<th>Task/KSA Basis</th><span>evidence_chain</span><span>aihr_course_evidence_chain_v1</span><span>mapping_strength</span><span>mapping_strength_warning</span><span>decision_state</span><span>pending_human_decision</span><span>facility_constraint_fit</span><span>human_review</span></body></html>",
                encoding="utf-8",
            )
            dashboard_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "schema": "aihr_dashboard_surface_verification_v1",
                        "scenario_count": 2,
                        "checks": valid_dashboard_endpoint_checks(),
                        "queue_status_summary": {"blocked_count": 0},
                        "static_artifacts": valid_static_artifacts(),
                        "live_plan_summaries": [
                            valid_live_plan_summary("baseline", matrix_rows=1),
                            valid_live_plan_summary("extra", matrix_rows=1),
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = main(
                    [
                        "--quality-report",
                        str(quality_path),
                        "--contract",
                        str(contract_path),
                        "--demo-json",
                        str(demo_json_path),
                        "--demo-html",
                        str(demo_html_path),
                        "--dashboard-verification",
                        str(dashboard_path),
                        "--out",
                        str(out_path),
                        "--markdown-out",
                        str(markdown_path),
                        "--agent-queue-out",
                        str(queue_path),
                        "--agent-queue-markdown-out",
                        str(queue_markdown_path),
                    ]
                )

            printed = json.loads(stdout.getvalue())
            written = json.loads(out_path.read_text(encoding="utf-8"))
            queue = json.loads(queue_path.read_text(encoding="utf-8"))
            readiness_markdown = markdown_path.read_text(encoding="utf-8")
            queue_markdown = queue_markdown_path.read_text(encoding="utf-8")

        self.assertEqual(exit_code, 0)
        self.assertEqual(written, printed)
        self.assertEqual(written["markdown_path"], str(markdown_path))
        self.assertEqual(written["agent_work_queue_path"], str(queue_path))
        self.assertEqual(queue, written["agent_work_queue"])
        self.assertIn("release_ready", written)
        self.assertIn("ok", written)
        self.assertEqual(written["schema"], "aihr_release_readiness_v1")
        self.assertEqual(written["sha256_scope"], "cycle_safe_release_readiness")
        self.assertEqual(
            written["cycle_safe_content_sha256"],
            _release_readiness_cycle_safe_sha256(written),
        )
        self.assertFalse(written["approval_claim"])
        self.assertIn("release_decision", written)
        self.assertIn("checks", written)
        self.assertIn("# NCS MCP Release Readiness", readiness_markdown)
        self.assertIn("readiness report was generated", readiness_markdown)
        self.assertIn("approval_claim=false", readiness_markdown)
        self.assertIn("release_agent_work_queue_path: reports/aihr_agent_queue_20260624.json", readiness_markdown)
        self.assertIn("queue_status_source_queue_path: reports/aihr_agent_queue_20260624.json", readiness_markdown)
        self.assertIn("queue_run_source_queue_path: reports/aihr_agent_queue_20260624.json", readiness_markdown)
        self.assertIn("final automatic execution is allowed only when agent-queue-status reports", readiness_markdown)
        self.assertGreater(len(queue_markdown), 0)
        self.assertIn("automation_contract", queue_markdown)


if __name__ == "__main__":
    unittest.main()
