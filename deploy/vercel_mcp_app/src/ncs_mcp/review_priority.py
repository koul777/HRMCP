from __future__ import annotations

from collections import Counter
import csv
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ncs_mcp.api_quality import normalize_api_element_issue
from ncs_mcp.db import connect, now_utc, rows_to_dicts
from ncs_mcp.review_safety import neutralize_suggested_action


DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES = [
    "hr_training_goal_link_human_review_required",
    "ontology_training_goal_link_human_review_required",
    "ontology_task_ksa_relation_human_review_required",
    "hr_core_concept_human_review_required",
    "ontology_core_concept_human_review_required",
    "criteria_format_issue",
    "api_element_collection_failure",
    "api_element_unmatched",
    "api_value_mismatch",
    "api_element_value_mismatch",
    "suspected_typo",
]
KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES = [
    "short_ksa",
    "duplicate_text",
]

ISSUE_TYPE_WEIGHTS = {
    "hr_training_goal_link_human_review_required": 100,
    "ontology_training_goal_link_human_review_required": 95,
    "ontology_task_ksa_relation_human_review_required": 90,
    "hr_core_concept_human_review_required": 85,
    "ontology_core_concept_human_review_required": 80,
    "criteria_format_issue": 60,
    "api_element_collection_failure": 56,
    "api_element_unmatched": 55,
    "api_value_mismatch": 50,
    "api_element_value_mismatch": 50,
    "suspected_typo": 40,
    "short_ksa": 38,
    "duplicate_text": 36,
}

SEVERITY_WEIGHTS = {
    "error": 40,
    "high": 30,
    "warning": 20,
    "medium": 15,
    "info": 5,
}
MAX_REVIEW_TEXT_CHARS = 900
MAX_REVIEW_PRIORITY_ITEMS = 200
MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE = 50
REVIEW_PRIORITY_SCHEMA = "ncs_review_priority_v1"
KSA_TERM_PREPROCESSING_REVIEW_PACK_SCHEMA = "ncs_ksa_term_preprocessing_review_pack_v1"
KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA = "ncs_ksa_term_ontology_impact_report_v1"
KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA = "ncs_ksa_term_minimal_review_slice_v1"
KSA_TERM_MINIMAL_REVIEW_DECISION_AUDIT_SCHEMA = "ncs_ksa_term_minimal_review_decision_audit_v1"
KSA_DEFINITION_CANDIDATE_FAMILY_REPORT_SCHEMA = "ncs_ksa_definition_candidate_family_report_v1"
KSA_TERM_MINIMAL_REVIEW_DECISION_ACTION_PLAN_SCHEMA = (
    "ncs_ksa_term_minimal_review_decision_action_plan_v1"
)
KSA_TERM_REVIEW_READINESS_SCHEMA = "ncs_ksa_term_review_readiness_v1"
KSA_REVIEW_MINIMIZATION_AUDIT_SCHEMA = "ncs_ksa_review_minimization_audit_v1"
KSA_TERM_MINIMAL_REVIEW_DECISION_ROW_SCHEMA = "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1"
KSA_TERM_MINIMAL_REVIEW_DECISION_CSV_FIELDS = [
    "schema",
    "concept_id",
    "concept_name",
    "concept_type",
    "review_status",
    "item_count",
    "item_ranks",
    "term_variants",
    "normalized_terms",
    "priority_levels",
    "max_priority_score",
    "issue_counts",
    "course_names",
    "task_relation_count",
    "training_course_link_count",
    "training_goal_link_count",
    "job_base_factor_labels",
    "operator_action",
    "suggested_decision",
    "suggested_decision_confidence",
    "suggested_decision_rationale",
    "suggested_decision_policy",
    "decision",
    "proposed_concept_action",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "human_decision_required",
    "status_update_allowed",
    "db_writes",
    "approval_claim",
]
KSA_TERM_MINIMAL_REVIEW_ALLOWED_DECISIONS = [
    "accept_term_as_specific_enough",
    "downweight_generic_term",
    "split_or_scope_term",
    "needs_more_evidence",
]
KSA_TERM_MINIMAL_REVIEW_DECISION_ACTIONS = {
    "accept_term_as_specific_enough": {
        "operator_action": "record_no_preprocessing_change_for_concept_group",
        "automation_policy": "no_status_write_no_db_write",
        "scoring_policy": "keep_existing_quality_penalty_until_guarded_operator_step",
    },
    "downweight_generic_term": {
        "operator_action": "prepare_candidate_downweight_rule_for_generic_term",
        "automation_policy": "candidate_plan_only_requires_guarded_operator_step",
        "scoring_policy": "candidate_penalty_review_only",
    },
    "split_or_scope_term": {
        "operator_action": "prepare_manual_split_or_scope_work_item",
        "automation_policy": "manual_ontology_edit_required_no_automatic_merge",
        "scoring_policy": "do_not_change_recommendation_score_from_plan",
    },
    "needs_more_evidence": {
        "operator_action": "queue_additional_task_ksa_evidence_sampling",
        "automation_policy": "evidence_collection_plan_only",
        "scoring_policy": "do_not_change_recommendation_score_from_plan",
    },
}


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _csv_row(row: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    return {field: _csv_cell(row.get(field)) for field in fieldnames}


def _csv_exported_value_matches_source(expected: Any, actual: Any) -> bool:
    expected_raw = "" if expected is None else str(expected)
    actual_raw = "" if actual is None else str(actual)
    expected_exported = _csv_cell(expected_raw)
    if expected_exported != expected_raw:
        return actual_raw == expected_exported
    return actual_raw == expected_raw


def _suggest_ksa_term_minimal_review_decision(group: dict[str, Any]) -> dict[str, Any]:
    def _safe_int(value: Any) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return 0

    issue_counts = group.get("issue_counts") if isinstance(group.get("issue_counts"), dict) else {}
    issue_keys = {str(key) for key, count in issue_counts.items() if _safe_int(count) > 0}
    term_variants = [str(value) for value in group.get("term_variants") or [] if value]
    normalized_terms = [str(value) for value in group.get("normalized_terms") or [] if value]
    task_relation_count = _safe_int(group.get("task_relation_count"))
    training_course_link_count = _safe_int(group.get("training_course_link_count"))
    max_priority_score = _safe_int(group.get("max_priority_score"))
    reasons: list[str] = []
    decision = "needs_more_evidence"
    confidence = "low"

    if "duplicate_text" in issue_keys and (len(set(normalized_terms)) > 1 or len(set(term_variants)) > 1):
        decision = "split_or_scope_term"
        confidence = "medium"
        reasons.append("duplicate_text appears across multiple term variants")
    elif "broad_generic_ksa" in issue_keys or (
        ("short_ksa" in issue_keys or "duplicate_text" in issue_keys)
        and (task_relation_count >= 500 or training_course_link_count >= 25)
    ):
        decision = "downweight_generic_term"
        confidence = "medium" if task_relation_count >= 500 or training_course_link_count >= 25 else "low"
        reasons.append("term is linked to broad recommendation-impact evidence")
    elif max_priority_score >= 95:
        decision = "needs_more_evidence"
        confidence = "medium"
        reasons.append("high priority group still needs human evidence sampling")
    else:
        reasons.append("no deterministic safe preprocessing action from current evidence")

    if task_relation_count:
        reasons.append(f"task_relation_count={task_relation_count}")
    if training_course_link_count:
        reasons.append(f"training_course_link_count={training_course_link_count}")
    if issue_counts:
        reasons.append(
            "issue_counts="
            + ",".join(f"{key}:{count}" for key, count in sorted(issue_counts.items()))
        )
    return {
        "suggested_decision": decision,
        "suggested_decision_confidence": confidence,
        "suggested_decision_rationale": "; ".join(reasons),
        "suggested_decision_policy": "review_assist_only_not_a_human_decision",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def _ksa_term_genericity_signal(group: dict[str, Any]) -> dict[str, Any]:
    def _safe_int(value: Any) -> int:
        try:
            return int(str(value))
        except (TypeError, ValueError):
            return 0

    issue_counts = group.get("issue_counts") if isinstance(group.get("issue_counts"), dict) else {}
    issue_keys = {str(key) for key, count in issue_counts.items() if _safe_int(count) > 0}
    task_relation_count = _safe_int(group.get("task_relation_count"))
    training_course_link_count = _safe_int(group.get("training_course_link_count"))
    item_count = _safe_int(group.get("item_count"))
    term_variant_count = len(group.get("term_variants") or [])
    score = 0
    reasons: list[str] = []
    if "broad_generic_ksa" in issue_keys:
        score += 45
        reasons.append("broad_generic_ksa_issue")
    if task_relation_count >= 1000:
        score += 25
        reasons.append("very_high_task_relation_spread")
    elif task_relation_count >= 500:
        score += 18
        reasons.append("high_task_relation_spread")
    elif task_relation_count >= 100:
        score += 10
        reasons.append("medium_task_relation_spread")
    if training_course_link_count >= 100:
        score += 20
        reasons.append("very_high_training_link_spread")
    elif training_course_link_count >= 25:
        score += 14
        reasons.append("high_training_link_spread")
    elif training_course_link_count > 0:
        score += 6
        reasons.append("training_link_spread")
    if item_count >= 3 or term_variant_count >= 3:
        score += 10
        reasons.append("multiple_term_variants")
    score = min(100, score)
    if score >= 70:
        level = "high"
        action = "review_for_downweight_or_scope_split"
    elif score >= 35:
        level = "medium"
        action = "sample_before_scoring_change"
    elif score > 0:
        level = "low"
        action = "keep_as_context_signal"
    else:
        level = "none"
        action = "no_genericity_signal"
    return {
        "schema": "ncs_ksa_term_genericity_signal_v1",
        "score": score,
        "level": level,
        "reasons": reasons,
        "operator_action": action,
        "scoring_role": "review_assist_only_not_a_human_decision",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


DEFAULT_FOCUS_OVERLAY_LIMIT = 20
DEFAULT_FOCUS_OVERLAYS = [
    {
        "code": "aihr_demo_major_02",
        "label": "AI-HR demo focus: NCS major 02 HR-visible evidence",
        "major_code": "02",
        "reason": "Current AI-HR demo and release-readiness examples use HR transition scenarios in NCS major 02.",
    }
]
KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPE_SET = set(KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES)
DEFAULT_MINIMAL_REVIEW_LEVELS = ["critical_minimal_review", "high_minimal_review"]
MINIMAL_REVIEW_LEVEL_SET = {
    "critical_minimal_review",
    "high_minimal_review",
    "medium_minimal_review",
    "low_minimal_review",
}
KSA_DEFINITION_FAMILY_RULES = [
    (
        "knowledge_general_meaning",
        "knowledge",
        "의 의미, 적용 조건, 판단 기준을 과업 맥락에서 이해하는 지식.",
        "의미·적용조건·판단기준 지식",
    ),
    (
        "knowledge_regulatory_or_standard",
        "knowledge",
        "의 적용 요건과 준수 기준을 과업 맥락에서 판단하는 지식.",
        "적용요건·준수기준 지식",
    ),
    (
        "knowledge_procedure_standard",
        "knowledge",
        "의 절차, 기준, 확인 항목을 이해하여 과업을 안정적으로 수행하는 지식.",
        "절차·기준·확인항목 지식",
    ),
    (
        "knowledge_analysis_context",
        "knowledge",
        "에 필요한 자료와 판단 기준을 해석하여 과업 의사결정에 활용하는 지식.",
        "자료해석·판단기준 지식",
    ),
    (
        "knowledge_purpose_scope",
        "knowledge",
        "의 목적, 범위, 실행 조건을 이해하여 과업 방향을 정하는 지식.",
        "목적·범위·실행조건 지식",
    ),
    (
        "skill_execute_apply",
        "skill",
        " 과업 상황에 맞게 실행하거나 적용하는 능력.",
        "실행·적용 능력",
    ),
    (
        "skill_data_analysis",
        "skill",
        " 위해 자료를 수집, 정리, 해석하여 과업 판단에 적용하는 능력.",
        "자료수집·정리·해석 능력",
    ),
    (
        "skill_operate_adjust_record",
        "skill",
        " 위해 현황을 확인하고 기준에 따라 조정, 실행, 기록하는 능력.",
        "현황확인·조정·기록 능력",
    ),
    (
        "skill_structure_output",
        "skill",
        " 위해 필요한 정보를 구조화하고 산출물로 표현하는 능력.",
        "정보구조화·산출물 표현 능력",
    ),
    (
        "skill_plan_design",
        "skill",
        " 위해 목표, 절차, 자원, 일정을 구조화하여 실행안을 만드는 능력.",
        "목표·절차·자원·일정 설계 능력",
    ),
    (
        "skill_stakeholder_communication",
        "skill",
        " 위해 이해관계자 정보를 교환하고 합의점을 도출하는 능력.",
        "이해관계자 소통·합의 능력",
    ),
    (
        "attitude_quality_collaboration",
        "attitude",
        " 기준으로 업무 품질, 협업, 책임 있는 실행을 유지하려는 태도.",
        "품질·협업·책임 태도",
    ),
    (
        "attitude_accuracy_compliance",
        "attitude",
        " 기준으로 결과의 정확성과 기준 준수를 유지하려는 태도.",
        "정확성·기준준수 태도",
    ),
    (
        "attitude_detail_check",
        "attitude",
        " 기준으로 요구와 상황을 세심하게 확인하려는 태도.",
        "요구·상황 세심확인 태도",
    ),
    (
        "attitude_information_sharing",
        "attitude",
        " 기준으로 필요한 정보를 공유하고 함께 문제를 해결하려는 태도.",
        "정보공유·공동문제해결 태도",
    ),
]
KSA_DEFINITION_FAMILY_LABELS = {
    key: label for key, _concept_type, _suffix, label in KSA_DEFINITION_FAMILY_RULES
}


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _validated_ksa_term_issue_types(issue_types: list[str] | None) -> list[str]:
    if not issue_types:
        return list(KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES)
    invalid = sorted({issue_type for issue_type in issue_types if issue_type not in KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPE_SET})
    if invalid:
        raise ValueError(
            "Unsupported KSA term preprocessing issue type(s): "
            + ", ".join(invalid)
            + ". Allowed values: "
            + ", ".join(KSA_TERM_PREPROCESSING_REVIEW_ISSUE_TYPES)
        )
    ordered: list[str] = []
    seen: set[str] = set()
    for issue_type in issue_types:
        if issue_type in seen:
            continue
        seen.add(issue_type)
        ordered.append(issue_type)
    return ordered


def _trim_text(value: str, *, max_chars: int = MAX_REVIEW_TEXT_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "... [truncated]"


def _trim_payload(payload: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    truncated_fields: list[str] = []
    for key, value in payload.items():
        if isinstance(value, str):
            trimmed_value = _trim_text(value)
            trimmed[key] = trimmed_value
            if trimmed_value != value:
                truncated_fields.append(key)
        else:
            trimmed[key] = value
    if truncated_fields:
        trimmed["_truncated_fields"] = truncated_fields
    return trimmed


def _split_ksa_definition_candidate(text: Any) -> tuple[str, str]:
    value = str(text or "").strip()
    if ": " not in value:
        return "", value
    term, body = value.split(": ", 1)
    return term.strip(), body.strip()


def _definition_family_for_candidate(concept_type: Any, body: str) -> tuple[str, str]:
    concept = str(concept_type or "").strip()
    for key, rule_concept_type, suffix, label in KSA_DEFINITION_FAMILY_RULES:
        if concept == rule_concept_type and suffix in body:
            return key, label
    return f"{concept or 'unknown'}_unknown_definition_family", "미분류 정의 패턴"


def _definition_candidate_risk_flags(
    *,
    term: str,
    body: str,
    review_status: str,
    confidence_score: float,
    family_key: str,
    ksa_id: Any,
) -> list[str]:
    flags: list[str] = []
    if family_key.endswith("_unknown_definition_family"):
        flags.append("unknown_definition_family")
    if not term:
        flags.append("missing_term_prefix")
    if re.search(r"[.!?。][을를](?:\s|$)", body):
        flags.append("sentence_punctuation_before_particle")
    if term and len(term) <= 2:
        flags.append("very_short_term")
    if term and len(term) >= 80:
        flags.append("overlong_term")
    if term in {"관리", "분석", "계획", "평가", "운영", "작성", "수립", "검토"}:
        flags.append("generic_single_term")
    if review_status != "candidate":
        flags.append("non_candidate_review_status")
    if confidence_score and confidence_score < 0.6:
        flags.append("low_confidence_candidate")
    if ksa_id in (None, ""):
        flags.append("missing_source_ksa")
    return flags


def _definition_family_review_level(family: dict[str, Any]) -> str:
    risk_count = int(family.get("risk_count") or 0)
    row_count = int(family.get("candidate_count") or 0)
    if risk_count:
        return "sample_risk_rows"
    if row_count >= 10_000:
        return "family_spotcheck_only"
    return "low_volume_family_spotcheck"


def build_ksa_definition_candidate_family_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 30,
    sample_limit: int = 5,
) -> dict[str, Any]:
    max_families = _clamp_int(limit, default=30, minimum=1, maximum=200)
    max_samples = _clamp_int(sample_limit, default=5, minimum=1, maximum=20)
    rows = conn.execute(
        """
        SELECT
            kmc.meaning_id,
            kmc.concept_id,
            kmc.concept_type,
            kmc.meaning_text,
            kmc.review_status,
            kmc.source_method,
            kmc.confidence_score,
            kmc.ksa_id,
            oc.concept_name,
            c.major_code,
            c.major_name,
            c.middle_code,
            c.middle_name,
            c.small_code,
            c.small_name,
            c.sub_code,
            c.sub_name,
            cu.unit_code,
            cu.unit_name_raw,
            ce.element_id,
            ce.element_name_raw
        FROM ksa_meaning_candidates kmc
        LEFT JOIN ontology_concepts oc ON oc.concept_id = kmc.concept_id
        LEFT JOIN ksa_items ki ON ki.ksa_id = kmc.ksa_id
        LEFT JOIN competency_elements ce ON ce.element_id = COALESCE(kmc.element_id, ki.element_id)
        LEFT JOIN competency_units cu ON cu.unit_code = COALESCE(kmc.unit_code, ce.unit_code)
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE kmc.meaning_role = 'term_definition_candidate'
          AND kmc.source_method = 'term_definition_template'
        ORDER BY kmc.meaning_id
        """
    ).fetchall()
    families: dict[str, dict[str, Any]] = {}
    status_counts: Counter[str] = Counter()
    type_counts: Counter[str] = Counter()
    risk_flag_counts: Counter[str] = Counter()
    risk_sample_rows: list[dict[str, Any]] = []
    for row in rows:
        review_status = str(row["review_status"] or "")
        concept_type = str(row["concept_type"] or "")
        confidence_score = float(row["confidence_score"] or 0.0)
        term, body = _split_ksa_definition_candidate(row["meaning_text"])
        family_key, family_label = _definition_family_for_candidate(concept_type, body)
        risk_flags = _definition_candidate_risk_flags(
            term=term,
            body=body,
            review_status=review_status,
            confidence_score=confidence_score,
            family_key=family_key,
            ksa_id=row["ksa_id"],
        )
        status_counts[review_status] += 1
        type_counts[concept_type] += 1
        for flag in risk_flags:
            risk_flag_counts[flag] += 1
        family = families.setdefault(
            family_key,
            {
                "family_key": family_key,
                "family_label": family_label,
                "concept_type": concept_type,
                "candidate_count": 0,
                "review_status_counts": Counter(),
                "risk_flag_counts": Counter(),
                "risk_count": 0,
                "major_codes": Counter(),
                "samples": [],
                "risk_samples": [],
            },
        )
        family["candidate_count"] += 1
        family["review_status_counts"][review_status] += 1
        if row["major_code"]:
            family["major_codes"][str(row["major_code"])] += 1
        if risk_flags:
            family["risk_count"] += 1
            for flag in risk_flags:
                family["risk_flag_counts"][flag] += 1
        sample = {
            "meaning_id": int(row["meaning_id"]),
            "concept_id": int(row["concept_id"]),
            "concept_name": row["concept_name"],
            "concept_type": concept_type,
            "term": term,
            "meaning_text": _trim_text(str(row["meaning_text"] or ""), max_chars=260),
            "review_status": review_status,
            "confidence_score": confidence_score,
            "risk_flags": risk_flags,
            "scope": {
                "major_code": row["major_code"],
                "major_name": row["major_name"],
                "middle_code": row["middle_code"],
                "middle_name": row["middle_name"],
                "small_code": row["small_code"],
                "small_name": row["small_name"],
                "sub_code": row["sub_code"],
                "sub_name": row["sub_name"],
                "unit_code": row["unit_code"],
                "unit_name_raw": row["unit_name_raw"],
                "element_id": row["element_id"],
                "element_name_raw": row["element_name_raw"],
            },
        }
        if len(family["samples"]) < max_samples:
            family["samples"].append(sample)
        if risk_flags and len(family["risk_samples"]) < max_samples:
            family["risk_samples"].append(sample)
        if risk_flags and len(risk_sample_rows) < max_families:
            risk_sample_rows.append(sample)

    family_rows: list[dict[str, Any]] = []
    for family in families.values():
        candidate_count = int(family["candidate_count"])
        family_rows.append(
            {
                "family_key": family["family_key"],
                "family_label": family["family_label"],
                "concept_type": family["concept_type"],
                "candidate_count": candidate_count,
                "candidate_percent": round((candidate_count / len(rows)) * 100, 3) if rows else 0.0,
                "review_status_counts": dict(family["review_status_counts"]),
                "risk_count": int(family["risk_count"]),
                "risk_percent": round((int(family["risk_count"]) / candidate_count) * 100, 3)
                if candidate_count
                else 0.0,
                "risk_flag_counts": dict(family["risk_flag_counts"]),
                "top_major_codes": [
                    {"major_code": code, "count": count}
                    for code, count in family["major_codes"].most_common(5)
                ],
                "recommended_review_level": _definition_family_review_level(family),
                "samples": family["samples"],
                "risk_samples": family["risk_samples"],
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    family_rows.sort(
        key=lambda family: (
            -int(family.get("risk_count") or 0),
            -int(family.get("candidate_count") or 0),
            str(family.get("family_key") or ""),
        )
    )
    candidate_count = len(rows)
    family_count = len(family_rows)
    review_units = family_count + len(risk_flag_counts)
    return {
        "ok": True,
        "schema": KSA_DEFINITION_CANDIDATE_FAMILY_REPORT_SCHEMA,
        "candidate_count": candidate_count,
        "definition_family_count": family_count,
        "risk_flag_family_count": sum(1 for family in family_rows if int(family.get("risk_count") or 0) > 0),
        "risk_candidate_count": sum(int(family.get("risk_count") or 0) for family in family_rows),
        "risk_flag_occurrence_count": sum(risk_flag_counts.values()),
        "review_unit_model": "definition_family_plus_risk_samples",
        "estimated_review_unit_count": review_units,
        "row_to_family_reduction_percent": round((1 - (family_count / candidate_count)) * 100, 3)
        if candidate_count
        else 0.0,
        "row_to_estimated_review_unit_reduction_percent": round(
            (1 - (review_units / candidate_count)) * 100,
            3,
        )
        if candidate_count
        else 0.0,
        "concept_type_counts": dict(type_counts),
        "review_status_counts": dict(status_counts),
        "risk_flag_counts": dict(risk_flag_counts),
        "top_families": family_rows[:max_families],
        "risk_samples": risk_sample_rows,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "safety": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "raw_ksa_preserved": True,
            "human_reviewed_written_by_report": False,
            "accepted_written_by_report": False,
            "reviewed_written_by_report": False,
        },
        "operator_guidance": [
            "Do not click every term-definition candidate row.",
            "Review one family-level sample for normal rows and only inspect risk_samples in detail.",
            "candidate means LLM/rule-generated definition candidate waiting for human confirmation, not preprocessing failure.",
            "llm_reviewed belongs to label candidate triage; it is not a trusted human approval state.",
            "This report is read-only and does not write ontology_concepts.definition.",
        ],
    }


def build_ksa_definition_candidate_family_report_from_db(
    db_path: Path,
    *,
    limit: int = 30,
    sample_limit: int = 5,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return build_ksa_definition_candidate_family_report(
            conn,
            limit=limit,
            sample_limit=sample_limit,
        )
    finally:
        conn.close()


def _issue_priority_score(issue: dict[str, Any]) -> int:
    return ISSUE_TYPE_WEIGHTS.get(issue["issue_type"], 10) + SEVERITY_WEIGHTS.get(
        issue["severity"],
        0,
    )


def _priority_reason(issue: dict[str, Any]) -> str:
    issue_type = issue["issue_type"]
    if issue_type == "api_element_collection_failure":
        return "API collection failures block fresh evidence before semantic review."
    if "training_goal_link" in issue_type:
        return "Training-goal concept links directly affect recommendation ranking and explanations."
    if "task_ksa_relation" in issue_type:
        return "Task-KSA relations affect transfer and gap reasoning."
    if "core_concept" in issue_type:
        return "Core ontology concepts affect many downstream KSA explanations."
    if issue_type == "criteria_format_issue":
        return "Criteria text quality affects task evidence shown to users."
    if issue_type.startswith("api_"):
        return "API mismatches affect NCS scope and evidence trust."
    if issue_type == "suspected_typo":
        return "Typos affect query matching and user-visible evidence quality."
    if issue_type == "short_ksa":
        return "Short KSA text may be an acronym, fragment, or valid compact term; inspect task evidence before refining ontology labels."
    if issue_type == "duplicate_text":
        return "Repeated KSA text may be a generic cross-scope term; inspect scope evidence before merging concepts or downweighting."
    return "Open quality issue selected for review."


def _major_code_from_unit_code(value: Any) -> str | None:
    text = str(value or "").strip()
    if len(text) >= 2 and text[:2].isdigit():
        return text[:2]
    return None


def _item_major_code(item: dict[str, Any]) -> str | None:
    issue = item.get("issue") or {}
    context = item.get("context") or {}
    for key in ("major_code", "ncs_lclas_cd"):
        value = context.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()[:2]
    for key in ("ncs_cl_cd", "unit_code", "target_id"):
        value = context.get(key) or issue.get(key)
        major_code = _major_code_from_unit_code(value)
        if major_code:
            return major_code
    return None


def _matches_focus_overlay(item: dict[str, Any], overlay: dict[str, Any]) -> bool:
    issue = item.get("issue") or {}
    major_code = str(overlay.get("major_code") or "").strip()
    if major_code and _item_major_code(item) == major_code:
        return True
    if major_code == "02" and str(issue.get("issue_type") or "").startswith("hr_"):
        return True
    return False


def _focus_overlays(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    overlays: list[dict[str, Any]] = []
    for overlay in DEFAULT_FOCUS_OVERLAYS:
        matched: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in items:
            if not _matches_focus_overlay(item, overlay):
                continue
            issue = item.get("issue") or {}
            signature = (
                str(issue.get("issue_type") or ""),
                str(issue.get("target_type") or ""),
                str(issue.get("target_id") or ""),
            )
            if signature in seen:
                continue
            seen.add(signature)
            matched.append(item)
            if len(matched) >= DEFAULT_FOCUS_OVERLAY_LIMIT:
                break
        overlays.append(
            {
                **overlay,
                "item_count": len(matched),
                "top_items": matched,
            }
        )
    return overlays


def _context_for_issue(conn: sqlite3.Connection, issue: dict[str, Any]) -> dict[str, Any]:
    target_type = issue["target_type"]
    if target_type == "unit":
        return _row_dict(
            conn.execute(
                """
                SELECT unit_code, unit_name_raw, api_unit_name, api_match_status
                FROM competency_units
                WHERE unit_code = ?
                """,
                (str(issue["target_id"]),),
            ).fetchone()
        )

    target_id = _as_int(issue["target_id"])

    if target_type == "task_ksa_concept_relation":
        if target_id is not None:
            context = _row_dict(
                conn.execute(
                    """
                    SELECT
                        r.relation_id, r.relation_type, r.confidence_score, r.review_status,
                        pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                        ce.element_id, ce.element_name_raw,
                        cu.unit_code, cu.unit_name_raw,
                        source.concept_name AS source_concept_name,
                        source.concept_type AS source_concept_type,
                        target.concept_name AS target_concept_name,
                        target.concept_type AS target_concept_type
                    FROM task_ksa_concept_relations r
                    LEFT JOIN performance_criteria pc ON pc.criteria_id = r.criteria_id
                    LEFT JOIN competency_elements ce ON ce.element_id = r.element_id
                    LEFT JOIN competency_units cu ON cu.unit_code = ce.unit_code
                    LEFT JOIN ontology_concepts source ON source.concept_id = r.source_concept_id
                    LEFT JOIN ontology_concepts target ON target.concept_id = r.target_concept_id
                    WHERE r.relation_id = ?
                    """,
                    (target_id,),
                ).fetchone()
            )
            if context:
                return context
        return {
            "issue_id": issue.get("issue_id"),
            "issue_type": issue.get("issue_type"),
            "target_type": target_type,
            "target_id": issue.get("target_id"),
            "issue_detail": issue.get("issue_detail"),
            "suggested_action": issue.get("suggested_action"),
            "context_source": "quality_issue_fallback",
        }

    if target_id is None:
        return {}

    if target_type == "ontology_concept":
        return _row_dict(
            conn.execute(
                """
                SELECT concept_id, concept_name, concept_type,
                       definition_status, relation_status, review_status
                FROM ontology_concepts
                WHERE concept_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "training_goal_concept_link":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    gl.link_id, gl.link_method, gl.confidence_score, gl.review_status,
                    tc.training_course_id, tc.compe_unit_name, tc.ncs_cl_cd,
                    tc.train_goal, tc.train_time, tc.meth_name,
                    oc.concept_id, oc.concept_name, oc.concept_type,
                    oc.definition_status AS concept_definition_status,
                    oc.review_status AS concept_review_status
                FROM training_goal_concept_links gl
                JOIN ncs_training_courses tc
                  ON tc.training_course_id = gl.training_course_id
                JOIN ontology_concepts oc
                  ON oc.concept_id = gl.concept_id
                WHERE gl.link_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "criteria":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    pc.criteria_id, pc.criteria_no, pc.criteria_text_raw,
                    pc.criteria_text_refined, pc.review_status,
                    ce.element_id, ce.element_name_raw,
                    cu.unit_code, cu.unit_name_raw
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE pc.criteria_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "ksa":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    ki.ksa_id, ki.ksa_type_name, ki.ksa_no, ki.ksa_text_raw,
                    ki.ksa_text_refined, ki.review_status,
                    ce.element_id, ce.element_name_raw,
                    cu.unit_code, cu.unit_name_raw
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ki.ksa_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    if target_type == "element":
        return _row_dict(
            conn.execute(
                """
                SELECT
                    ce.element_id, ce.element_code_raw, ce.element_name_raw,
                    ce.api_match_status, cu.unit_code, cu.unit_name_raw
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE ce.element_id = ?
                """,
                (target_id,),
            ).fetchone()
        )

    return {}


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {path}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _open_issue_counts(conn: sqlite3.Connection, issue_types: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in issue_types)
    counts: dict[tuple[str, str], int] = {}
    rows = conn.execute(
        f"""
        SELECT qi.issue_type, qi.severity, qi.issue_detail, ce.api_match_status, COUNT(*) AS count
        FROM quality_issues qi
        LEFT JOIN competency_elements ce
          ON qi.target_type = 'element'
         AND ce.element_id = CAST(qi.target_id AS INTEGER)
        WHERE qi.resolved_at IS NULL
          AND qi.issue_type IN ({placeholders})
        GROUP BY qi.issue_type, qi.severity, qi.issue_detail, ce.api_match_status
        """,
        issue_types,
    ).fetchall()
    for row in rows:
        issue = normalize_api_element_issue(
            {
                "issue_type": row["issue_type"],
                "issue_detail": row["issue_detail"],
            },
            api_match_status=row["api_match_status"],
        )
        key = (str(issue.get("issue_type") or "unknown"), str(row["severity"] or "unknown"))
        counts[key] = counts.get(key, 0) + int(row["count"] or 0)
    return [
        {"issue_type": issue_type, "severity": severity, "count": count}
        for (issue_type, severity), count in sorted(counts.items())
    ]


def _ksa_term_review_action(issue_types: list[str]) -> str:
    issue_type_set = set(issue_types)
    if issue_type_set == {"short_ksa"}:
        return "inspect_short_term_meaning"
    if issue_type_set == {"duplicate_text"}:
        return "inspect_generic_duplicate_term"
    if {"short_ksa", "duplicate_text"}.issubset(issue_type_set):
        return "inspect_term_quality_and_genericity"
    return "inspect_ksa_term_quality"


def _looks_like_ksa_display_noise(value: Any) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if "\ufffd" in text:
        return True
    question_ratio = text.count("?") / max(1, len(text))
    if text.count("?") >= 2 and question_ratio >= 0.15:
        return True
    mojibake_markers = ("以", "", "湲", "怨", "諛", "援", "嫄", "臾", "吏", "泥", "쨌")
    return "?" in text and any(marker in text for marker in mojibake_markers)


def _ksa_term_review_profile(
    *,
    issue_type_counts: dict[str, int],
    issue_count: int,
    unit_count: int,
    major_count: int,
    raw_variant_count: int,
    representative_ksa_text: Any,
) -> dict[str, Any]:
    has_short = int(issue_type_counts.get("short_ksa") or 0) > 0
    has_duplicate = int(issue_type_counts.get("duplicate_text") or 0) > 0
    broad_cross_scope = major_count >= 5 or unit_count >= 50
    high_volume = issue_count >= 100
    display_noise = _looks_like_ksa_display_noise(representative_ksa_text)
    flags: list[str] = []
    if has_short:
        flags.append("has_short_ksa_issue")
    if has_duplicate:
        flags.append("has_duplicate_text_issue")
    if broad_cross_scope:
        flags.append("broad_cross_scope")
    if high_volume:
        flags.append("high_volume")
    if raw_variant_count > 1:
        flags.append("raw_spacing_or_variant_group")
    if display_noise:
        flags.append("possible_encoding_or_display_noise")

    if display_noise:
        bucket = "encoding_display_triage"
        options = [
            "confirm_display_noise_before_semantic_review",
            "keep_as_valid_term_if_source_is_readable_elsewhere",
            "route_to_source_encoding_diagnostics",
        ]
        rationale = "Representative KSA text looks corrupted or display-noisy; inspect source/display handling before semantic merge or downweight decisions."
    elif has_short and has_duplicate and broad_cross_scope:
        bucket = "broad_generic_downweight_review"
        options = [
            "confirm_generic_downweight_candidate",
            "split_by_scope_if_meaning_differs",
            "keep_as_domain_core_term",
        ]
        rationale = "The term is both short/generic-looking and repeated across many scopes; one review decision can guide downweighting or scope splitting."
    elif has_short and has_duplicate:
        bucket = "term_quality_and_genericity_review"
        options = [
            "inspect_term_meaning_with_samples",
            "confirm_generic_downweight_candidate",
            "keep_as_valid_compact_term",
        ]
        rationale = "The term has both short and duplicate signals; inspect a few samples before treating it as generic or valid."
    elif has_duplicate and broad_cross_scope:
        bucket = "broad_duplicate_downweight_review"
        options = [
            "confirm_generic_downweight_candidate",
            "keep_as_cross_domain_core_term",
            "split_by_scope_if_samples_conflict",
        ]
        rationale = "The term repeats across broad NCS scopes; review whether it is a valid cross-domain core term or should be downweighted."
    elif has_duplicate:
        bucket = "duplicate_scope_review"
        options = [
            "inspect_duplicate_samples",
            "merge_or_alias_if_same_meaning",
            "keep_separate_if_scope_specific",
        ]
        rationale = "The term repeats, but not broadly enough to assume global genericity."
    elif has_short:
        bucket = "short_term_meaning_review"
        options = [
            "expand_or_define_if_valid_acronym",
            "keep_as_valid_compact_term",
            "mark_for_manual_definition_from_task_evidence",
        ]
        rationale = "The term is short; verify whether it is a valid acronym/compact domain term using task samples."
    else:
        bucket = "local_term_quality_review"
        options = [
            "inspect_samples",
            "keep_as_is",
            "route_to_manual_ontology_cleanup",
        ]
        rationale = "The term has local KSA quality signals requiring sample-based review."

    return {
        "review_bucket": bucket,
        "review_flags": flags,
        "operator_decision_options": options,
        "minimal_review_rationale": rationale,
        "auto_apply_allowed": False,
    }


def _issue_types_key(issue_types: list[str]) -> str:
    return "\x1f".join(issue_types)


def _ensure_ksa_term_issue_temp(
    conn: sqlite3.Connection,
    selected_issue_types: list[str],
    *,
    refresh: bool = False,
) -> None:
    issue_types_key = _issue_types_key(selected_issue_types)
    if not refresh:
        try:
            row = conn.execute("SELECT issue_types_key FROM temp_ksa_term_issue_meta LIMIT 1").fetchone()
            cached_key = row["issue_types_key"] if isinstance(row, sqlite3.Row) else row[0] if row is not None else None
            if cached_key == issue_types_key:
                return
        except sqlite3.OperationalError:
            pass

    placeholders = ",".join("?" for _ in selected_issue_types)
    normalized_expr = """
        LOWER(REPLACE(REPLACE(REPLACE(REPLACE(TRIM(COALESCE(ki.ksa_text_raw, '')), ' ', ''), CHAR(9), ''), CHAR(10), ''), CHAR(13), ''))
    """
    conn.execute("DROP TABLE IF EXISTS temp_ksa_term_issue_rows")
    conn.execute("DROP TABLE IF EXISTS temp_ksa_term_issue_meta")
    conn.execute(
        f"""
        CREATE TEMP TABLE temp_ksa_term_issue_rows AS
        SELECT
            qi.issue_id,
            qi.issue_type,
            qi.severity,
            qi.issue_detail,
            qi.suggested_action,
            qi.detected_at,
            ki.ksa_id,
            ki.ksa_type_name,
            ki.ksa_no,
            ki.ksa_text_raw,
            ki.review_status,
            {normalized_expr} AS normalized_ksa_text,
            ce.element_id,
            ce.element_name_raw,
            cu.unit_code,
            cu.unit_name_raw,
            c.major_code,
            c.major_name,
            c.middle_code,
            c.middle_name,
            c.small_code,
            c.small_name,
            c.sub_code,
            c.sub_name
        FROM quality_issues qi
        JOIN ksa_items ki
          ON ki.ksa_id = CAST(qi.target_id AS INTEGER)
        JOIN competency_elements ce ON ce.element_id = ki.element_id
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE qi.resolved_at IS NULL
          AND qi.target_type = 'ksa'
          AND qi.issue_type IN ({placeholders})
        """,
        selected_issue_types,
    )
    conn.execute("CREATE INDEX temp_ksa_term_issue_rows_norm_idx ON temp_ksa_term_issue_rows(normalized_ksa_text)")
    conn.execute("CREATE INDEX temp_ksa_term_issue_rows_ksa_idx ON temp_ksa_term_issue_rows(ksa_id)")
    conn.execute("CREATE TEMP TABLE temp_ksa_term_issue_meta(issue_types_key TEXT NOT NULL)")
    conn.execute("INSERT INTO temp_ksa_term_issue_meta(issue_types_key) VALUES (?)", (issue_types_key,))


def build_ksa_term_preprocessing_review_pack(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
    sample_limit: int = 3,
    min_issue_count: int = 1,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    selected_issue_types = _validated_ksa_term_issue_types(issue_types)
    max_groups = _clamp_int(limit, default=100, minimum=1, maximum=500)
    max_samples = _clamp_int(sample_limit, default=3, minimum=0, maximum=10)
    min_count = _clamp_int(min_issue_count, default=1, minimum=1, maximum=1000000)
    _ensure_ksa_term_issue_temp(conn, selected_issue_types, refresh=True)
    group_rows = rows_to_dicts(
        conn.execute(
            """
            SELECT
                normalized_ksa_text,
                MIN(ksa_text_raw) AS representative_ksa_text,
                COUNT(DISTINCT issue_id) AS issue_count,
                COUNT(DISTINCT ksa_id) AS ksa_count,
                COUNT(DISTINCT element_id) AS element_count,
                COUNT(DISTINCT unit_code) AS unit_count,
                COUNT(DISTINCT major_code) AS major_count,
                MIN(detected_at) AS first_detected_at,
                MAX(detected_at) AS last_detected_at
            FROM temp_ksa_term_issue_rows
            WHERE normalized_ksa_text <> ''
            GROUP BY normalized_ksa_text
            HAVING issue_count >= ?
            ORDER BY issue_count DESC, unit_count DESC, representative_ksa_text
            LIMIT ?
            """,
            (min_count, max_groups),
        ).fetchall()
    )
    detail_buckets: dict[str, list[dict[str, Any]]] = {}
    sample_buckets: dict[str, list[dict[str, Any]]] = {}
    normalized_keys = sorted({row["normalized_ksa_text"] for row in group_rows})
    if normalized_keys:
        key_placeholders = ",".join("?" for _ in normalized_keys)
        detail_rows = rows_to_dicts(
            conn.execute(
                f"""
                SELECT
                    *
                FROM temp_ksa_term_issue_rows
                WHERE normalized_ksa_text IN ({key_placeholders})
                ORDER BY normalized_ksa_text, issue_type, issue_id
                """,
                normalized_keys,
            ).fetchall()
        )
        for detail in detail_rows:
            key = str(detail.get("normalized_ksa_text") or "")
            detail_buckets.setdefault(key, []).append(detail)
            bucket = sample_buckets.setdefault(key, [])
            if len(bucket) < max_samples:
                bucket.append(
                    _trim_payload(
                        {
                            "issue_id": detail.get("issue_id"),
                            "issue_type": detail.get("issue_type"),
                            "severity": detail.get("severity"),
                            "ksa_id": detail.get("ksa_id"),
                            "ksa_no": detail.get("ksa_no"),
                            "ksa_type_name": detail.get("ksa_type_name"),
                            "ksa_text_raw": detail.get("ksa_text_raw"),
                            "review_status": detail.get("review_status"),
                            "unit_code": detail.get("unit_code"),
                            "unit_name_raw": detail.get("unit_name_raw"),
                            "element_id": detail.get("element_id"),
                            "element_name_raw": detail.get("element_name_raw"),
                            "major_code": detail.get("major_code"),
                            "major_name": detail.get("major_name"),
                            "sub_scope": ":".join(
                                str(detail.get(key_name) or "")
                                for key_name in ("major_code", "middle_code", "small_code", "sub_code")
                            ),
                            "issue_detail": detail.get("issue_detail"),
                            "suggested_action": neutralize_suggested_action(
                                detail.get("suggested_action"),
                                issue_type=detail.get("issue_type"),
                                target_type="ksa",
                            ),
                        }
                    )
                )
    groups: list[dict[str, Any]] = []
    represented_issue_count = 0
    represented_ksa_count = 0
    review_bucket_counts: dict[str, int] = {}
    for row in group_rows:
        key = str(row.get("normalized_ksa_text") or "")
        details = detail_buckets.get(key, [])
        issue_type_counts: dict[str, int] = {}
        ksa_type_counts: dict[str, int] = {}
        review_status_counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        raw_variants: set[str] = set()
        for detail in details:
            issue_type = str(detail.get("issue_type") or "unknown")
            issue_type_counts[issue_type] = issue_type_counts.get(issue_type, 0) + 1
            ksa_type = str(detail.get("ksa_type_name") or "unknown")
            ksa_type_counts[ksa_type] = ksa_type_counts.get(ksa_type, 0) + 1
            review_status = str(detail.get("review_status") or "unknown")
            review_status_counts[review_status] = review_status_counts.get(review_status, 0) + 1
            severity = str(detail.get("severity") or "unknown")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            raw_text = str(detail.get("ksa_text_raw") or "").strip()
            if raw_text:
                raw_variants.add(raw_text)
        ordered_issue_types = sorted(issue_type_counts)
        issue_count = int(row.get("issue_count") or 0)
        ksa_count = int(row.get("ksa_count") or 0)
        represented_issue_count += issue_count
        represented_ksa_count += ksa_count
        profile = _ksa_term_review_profile(
            issue_type_counts=issue_type_counts,
            issue_count=issue_count,
            unit_count=int(row.get("unit_count") or 0),
            major_count=int(row.get("major_count") or 0),
            raw_variant_count=len(raw_variants),
            representative_ksa_text=row.get("representative_ksa_text"),
        )
        review_bucket = str(profile["review_bucket"])
        review_bucket_counts[review_bucket] = review_bucket_counts.get(review_bucket, 0) + 1
        groups.append(
            {
                "normalized_ksa_term": row.get("normalized_ksa_text"),
                "normalized_ksa_text": row.get("normalized_ksa_text"),
                "representative_ksa_text": row.get("representative_ksa_text"),
                "raw_ksa_text_variants": sorted(raw_variants)[:10],
                "raw_ksa_text_variant_count": len(raw_variants),
                "issue_type_counts": dict(sorted(issue_type_counts.items())),
                "ksa_type_counts": dict(sorted(ksa_type_counts.items())),
                "review_status_counts": dict(sorted(review_status_counts.items())),
                "severity_counts": dict(sorted(severity_counts.items())),
                "issue_count": issue_count,
                "ksa_count": ksa_count,
                "element_count": int(row.get("element_count") or 0),
                "unit_count": int(row.get("unit_count") or 0),
                "major_count": int(row.get("major_count") or 0),
                "first_detected_at": row.get("first_detected_at"),
                "last_detected_at": row.get("last_detected_at"),
                "recommended_review_action": _ksa_term_review_action(ordered_issue_types),
                **profile,
                "human_decision_required": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "decision_fields": {
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "proposed_term_action": "",
                },
                "samples": sample_buckets.get(key, []),
            }
        )
    open_issue_counts = _open_issue_counts(conn, selected_issue_types)
    total_open_issue_count = sum(int(row.get("count") or 0) for row in open_issue_counts)
    return {
        "ok": True,
        "schema": KSA_TERM_PREPROCESSING_REVIEW_PACK_SCHEMA,
        "generated_at": now_utc(),
        "issue_types": selected_issue_types,
        "limit": max_groups,
        "sample_limit": max_samples,
        "min_issue_count": min_count,
        "status_update_allowed": False,
        "db_writes": False,
        "human_decision_required": True,
        "approval_claim": False,
        "group_count": len(groups),
        "total_open_issue_count": total_open_issue_count,
        "represented_issue_count": represented_issue_count,
        "represented_ksa_count": represented_ksa_count,
        "represented_issue_reduction": max(0, represented_issue_count - len(groups)),
        "review_bucket_counts": dict(sorted(review_bucket_counts.items())),
        "open_issue_counts": open_issue_counts,
        "groups": groups,
        "notes": [
            "Read-only grouped review pack for KSA term preprocessing quality issues.",
            "Groups collapse repeated KSA issue rows by normalized raw KSA text so one term can carry multiple issue types.",
            "Do not mutate ksa_items.ksa_text_raw; use this artifact only to decide future refined labels or ontology actions.",
            "No human_reviewed, accepted, reviewed, or resolved status is written by this report.",
        ],
    }


def build_ksa_term_preprocessing_review_pack_from_db(
    db_path: Path,
    *,
    limit: int = 100,
    sample_limit: int = 3,
    min_issue_count: int = 1,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return build_ksa_term_preprocessing_review_pack(
            conn,
            limit=limit,
            sample_limit=sample_limit,
            min_issue_count=min_issue_count,
            issue_types=issue_types,
        )
    finally:
        conn.close()


def _impact_operator_action(review_bucket: str) -> str:
    if review_bucket in {"broad_generic_downweight_review", "broad_duplicate_downweight_review"}:
        return "inspect_linked_concepts_for_generic_downweight_or_scope_split"
    if review_bucket == "short_term_meaning_review":
        return "inspect_linked_concepts_for_acronym_or_definition_cleanup"
    if review_bucket == "encoding_display_triage":
        return "inspect_source_display_before_ontology_action"
    return "inspect_linked_concepts_before_ontology_cleanup"


def _minimal_review_priority_profile(
    *,
    review_bucket: str,
    issue_count: int,
    linked_concept_count: int,
    linked_penalty_concept_count: int,
    task_relation_count: int,
    training_link_count: int,
) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    if linked_penalty_concept_count:
        score += min(75, 60 + linked_penalty_concept_count * 5)
        reasons.append("linked_transition_penalty_concepts")
    bucket_scores = {
        "broad_generic_downweight_review": 20,
        "broad_duplicate_downweight_review": 18,
        "term_quality_and_genericity_review": 16,
        "duplicate_scope_review": 12,
        "short_term_meaning_review": 10,
        "encoding_display_triage": 8,
        "local_term_quality_review": 6,
    }
    bucket_score = bucket_scores.get(review_bucket, 5)
    score += bucket_score
    if review_bucket:
        reasons.append(f"bucket:{review_bucket}")
    if issue_count:
        score += min(10, issue_count)
        reasons.append("open_ksa_quality_issues")
    if linked_concept_count:
        score += min(8, linked_concept_count)
        reasons.append("linked_ontology_concepts")
    if task_relation_count >= 1000:
        score += 15
        reasons.append("large_task_relation_impact")
    elif task_relation_count >= 100:
        score += 10
        reasons.append("medium_task_relation_impact")
    elif task_relation_count > 0:
        score += 5
        reasons.append("task_relation_impact")
    if training_link_count >= 100:
        score += 10
        reasons.append("large_training_link_impact")
    elif training_link_count > 0:
        score += 5
        reasons.append("training_link_impact")
    score = min(100, score)
    if score >= 80:
        level = "critical_minimal_review"
    elif score >= 60:
        level = "high_minimal_review"
    elif score >= 40:
        level = "medium_minimal_review"
    else:
        level = "low_minimal_review"
    if linked_penalty_concept_count:
        operator_action = "inspect_linked_penalized_concepts_before_any_scoring_decision"
    elif review_bucket in {"broad_generic_downweight_review", "broad_duplicate_downweight_review"}:
        operator_action = "sample_group_once_then_decide_downweight_or_scope_split"
    else:
        operator_action = _impact_operator_action(review_bucket)
    return {
        "minimal_review_priority_score": score,
        "minimal_review_priority_level": level,
        "minimal_review_priority_reasons": reasons,
        "minimal_review_operator_action": operator_action,
        "minimal_review_scope_note": (
            "Priority is for manual triage only; it is not approval evidence and does not authorize "
            "human_reviewed, accepted, reviewed, or rejected status writes."
        ),
    }


def _count_by_concept_id(
    conn: sqlite3.Connection,
    concept_ids: list[int],
    sql_template: str,
) -> dict[int, int]:
    concept_ids = [int(concept_id) for concept_id in concept_ids if int(concept_id or 0)]
    if not concept_ids:
        return {}
    counts: dict[int, int] = {}
    for offset in range(0, len(concept_ids), 900):
        batch = concept_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        placeholder_repeats = max(1, sql_template.count("{placeholders}"))
        rows = conn.execute(
            sql_template.format(placeholders=placeholders),
            batch * placeholder_repeats,
        ).fetchall()
        for row in rows:
            counts[int(row["concept_id"])] = int(row["count"] or 0)
    return counts


def _count_distinct_records_for_concepts(
    conn: sqlite3.Connection,
    concept_ids: list[int],
    sql_template: str,
) -> int:
    concept_ids = [int(concept_id) for concept_id in concept_ids if int(concept_id or 0)]
    if not concept_ids:
        return 0
    record_ids: set[str] = set()
    for offset in range(0, len(concept_ids), 900):
        batch = concept_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        placeholder_repeats = max(1, sql_template.count("{placeholders}"))
        rows = conn.execute(
            sql_template.format(placeholders=placeholders),
            batch * placeholder_repeats,
        ).fetchall()
        for row in rows:
            record_id = row["record_id"]
            if record_id is not None:
                record_ids.add(str(record_id))
    return len(record_ids)


def _record_ids_by_concept_id(
    conn: sqlite3.Connection,
    concept_ids: list[int],
    sql_template: str,
) -> dict[int, set[str]]:
    concept_ids = sorted({int(concept_id) for concept_id in concept_ids if int(concept_id or 0)})
    if not concept_ids:
        return {}
    record_ids: dict[int, set[str]] = {}
    for offset in range(0, len(concept_ids), 900):
        batch = concept_ids[offset : offset + 900]
        placeholders = ",".join("?" for _ in batch)
        placeholder_repeats = max(1, sql_template.count("{placeholders}"))
        rows = conn.execute(
            sql_template.format(placeholders=placeholders),
            batch * placeholder_repeats,
        ).fetchall()
        for row in rows:
            concept_id = _as_int(row["concept_id"])
            record_id = row["record_id"]
            if concept_id and record_id is not None:
                record_ids.setdefault(concept_id, set()).add(str(record_id))
    return record_ids


def _empty_job_base_auxiliary_signal(*, schema: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "evidence_role": "supporting_gap_context_not_primary_evidence",
        "scoring_role": "review_priority_context_only",
        "concept_signal_count": 0,
        "unit_count": 0,
        "competency_count": 0,
        "factor_count": 0,
        "competency_names": [],
        "factor_labels": [],
        "top_links": [],
        "operator_action": "no_job_base_auxiliary_signal",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def _job_base_label_from_names(competency_name: Any, factor_name: Any) -> str:
    competency = str(competency_name or "").strip()
    factor = str(factor_name or "").strip()
    if competency and factor:
        return f"{competency}:{factor}"
    return competency or factor


def _job_base_auxiliary_signals_by_concept_id(
    conn: sqlite3.Connection,
    concept_ids: list[int],
    *,
    limit_per_concept: int = 8,
) -> dict[int, dict[str, Any]]:
    concept_ids = sorted({int(concept_id) for concept_id in concept_ids if int(concept_id or 0)})
    if not concept_ids:
        return {}
    max_links = _clamp_int(limit_per_concept, default=8, minimum=1, maximum=20)
    buckets: dict[int, dict[str, Any]] = {}
    for offset in range(0, len(concept_ids), 400):
        batch = concept_ids[offset : offset + 400]
        placeholders = ",".join("?" for _ in batch)
        rows = rows_to_dicts(
            conn.execute(
                f"""
                WITH relation_units AS (
                    SELECT DISTINCT
                        rel.source_concept_id AS concept_id,
                        ce.unit_code
                    FROM task_ksa_concept_relations rel
                    JOIN competency_elements ce ON ce.element_id = rel.element_id
                    WHERE rel.source_concept_id IN ({placeholders})
                      AND COALESCE(rel.review_status, '') != 'rejected'
                    UNION
                    SELECT DISTINCT
                        rel.target_concept_id AS concept_id,
                        ce.unit_code
                    FROM task_ksa_concept_relations rel
                    JOIN competency_elements ce ON ce.element_id = rel.element_id
                    WHERE rel.target_concept_id IN ({placeholders})
                      AND COALESCE(rel.review_status, '') != 'rejected'
                ),
                concept_unit_counts AS (
                    SELECT concept_id, COUNT(DISTINCT unit_code) AS concept_unit_count
                    FROM relation_units
                    GROUP BY concept_id
                ),
                concept_job_base AS (
                    SELECT
                        ru.concept_id,
                        l.job_base_competency_id,
                        l.job_base_factor_id,
                        c.competency_name,
                        f.factor_name,
                        COUNT(DISTINCT l.unit_code) AS unit_count,
                        COUNT(DISTINCT l.link_id) AS link_count
                    FROM relation_units ru
                    JOIN ncs_unit_job_base_links l ON l.unit_code = ru.unit_code
                    JOIN ncs_job_base_competencies c
                      ON c.job_base_competency_id = l.job_base_competency_id
                    LEFT JOIN ncs_job_base_factors f
                      ON f.job_base_factor_id = l.job_base_factor_id
                    WHERE COALESCE(l.review_status, '') != 'rejected'
                    GROUP BY
                        ru.concept_id,
                        l.job_base_competency_id,
                        l.job_base_factor_id,
                        c.competency_name,
                        f.factor_name
                )
                SELECT
                    cjb.*,
                    cuc.concept_unit_count
                FROM concept_job_base cjb
                JOIN concept_unit_counts cuc ON cuc.concept_id = cjb.concept_id
                ORDER BY
                    cjb.concept_id,
                    cjb.unit_count DESC,
                    cjb.competency_name,
                    cjb.factor_name
                """,
                (*batch, *batch),
            ).fetchall()
        )
        for row in rows:
            concept_id = _as_int(row.get("concept_id"))
            if not concept_id:
                continue
            entry = buckets.setdefault(
                concept_id,
                {
                    "schema": "ncs_job_base_concept_auxiliary_signal_v1",
                    "evidence_role": "supporting_gap_context_not_primary_evidence",
                    "scoring_role": "review_priority_context_only",
                    "concept_signal_count": 1,
                    "unit_count": int(row.get("concept_unit_count") or 0),
                    "_competency_names": [],
                    "_factor_labels": [],
                    "_top_links": [],
                    "_competency_ids": set(),
                    "_factor_ids": set(),
                },
            )
            entry["unit_count"] = max(int(entry.get("unit_count") or 0), int(row.get("concept_unit_count") or 0))
            competency_name = str(row.get("competency_name") or "").strip()
            factor_name = str(row.get("factor_name") or "").strip()
            label = _job_base_label_from_names(competency_name, factor_name)
            competency_id = _as_int(row.get("job_base_competency_id"))
            factor_id = _as_int(row.get("job_base_factor_id"))
            if competency_id:
                entry["_competency_ids"].add(competency_id)
            if factor_id:
                entry["_factor_ids"].add(factor_id)
            if competency_name and competency_name not in entry["_competency_names"]:
                entry["_competency_names"].append(competency_name)
            if label and label not in entry["_factor_labels"]:
                entry["_factor_labels"].append(label)
            if len(entry["_top_links"]) < max_links:
                entry["_top_links"].append(
                    {
                        "job_base_competency_id": competency_id,
                        "job_base_factor_id": factor_id,
                        "competency_name": competency_name,
                        "factor_name": factor_name,
                        "label": label,
                        "unit_count": int(row.get("unit_count") or 0),
                        "link_count": int(row.get("link_count") or 0),
                    }
                )
    signals: dict[int, dict[str, Any]] = {}
    for concept_id, entry in buckets.items():
        signals[concept_id] = {
            "schema": entry["schema"],
            "evidence_role": entry["evidence_role"],
            "scoring_role": entry["scoring_role"],
            "concept_signal_count": int(entry.get("concept_signal_count") or 0),
            "unit_count": int(entry.get("unit_count") or 0),
            "competency_count": len(entry.get("_competency_ids") or set()),
            "factor_count": len(entry.get("_factor_ids") or set()),
            "competency_names": list(entry.get("_competency_names") or [])[:8],
            "factor_labels": list(entry.get("_factor_labels") or [])[:12],
            "top_links": list(entry.get("_top_links") or [])[:max_links],
            "operator_action": "use_only_as_supporting_transition_gap_context_not_primary_ksa_evidence",
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
        }
    return signals


def _group_job_base_auxiliary_signal(concept_rows: list[dict[str, Any]]) -> dict[str, Any]:
    signals = [
        signal
        for concept in concept_rows
        for signal in [concept.get("job_base_auxiliary_signal")]
        if isinstance(signal, dict) and int(signal.get("competency_count") or 0) > 0
    ]
    if not signals:
        return _empty_job_base_auxiliary_signal(schema="ncs_job_base_group_auxiliary_signal_v1")
    competency_names: list[str] = []
    factor_labels: list[str] = []
    top_links: list[dict[str, Any]] = []
    for signal in signals:
        for name in signal.get("competency_names") or []:
            if name and name not in competency_names:
                competency_names.append(name)
        for label in signal.get("factor_labels") or []:
            if label and label not in factor_labels:
                factor_labels.append(label)
        for link in signal.get("top_links") or []:
            if isinstance(link, dict) and len(top_links) < 12:
                top_links.append(link)
    return {
        "schema": "ncs_job_base_group_auxiliary_signal_v1",
        "evidence_role": "supporting_gap_context_not_primary_evidence",
        "scoring_role": "review_priority_context_only",
        "concept_signal_count": len(signals),
        "unit_count": max((int(signal.get("unit_count") or 0) for signal in signals), default=0),
        "competency_count": len(competency_names),
        "factor_count": len(factor_labels),
        "competency_names": competency_names[:8],
        "factor_labels": factor_labels[:12],
        "top_links": top_links,
        "operator_action": "use_only_as_supporting_transition_gap_context_not_primary_ksa_evidence",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def _transition_quality_penalty_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    concept_rows: dict[int, dict[str, Any]] = {}
    issue_counts: Counter[str] = Counter()
    course_names: list[str] = []
    penalty_course_count = 0
    if not isinstance(report, dict):
        return {
            "available": False,
            "scenario_count": 0,
            "penalized_recommendation_row_count": 0,
            "distinct_penalized_course_count": 0,
            "concept_count": 0,
            "issue_counts": {},
            "course_names": [],
            "concepts_by_id": {},
        }
    evidence = report.get("evidence") if isinstance(report.get("evidence"), dict) else {}
    evaluation = (
        evidence.get("transition_evaluation")
        if isinstance(evidence.get("transition_evaluation"), dict)
        else report.get("transition_evaluation")
        if isinstance(report.get("transition_evaluation"), dict)
        else report
    )
    if not isinstance(evaluation, dict):
        evaluation = {}
    cases = evaluation.get("cases") if isinstance(evaluation.get("cases"), list) else []
    if not cases:
        return {
            "available": False,
            "source_schema": report.get("schema"),
            "scenario_count": int(evaluation.get("scenario_count") or 0),
            "penalized_recommendation_row_count": 0,
            "distinct_penalized_course_count": 0,
            "concept_count": 0,
            "issue_counts": {},
            "course_names": [],
            "concepts_by_id": {},
        }
    for case in cases:
        if not isinstance(case, dict):
            continue
        for row in case.get("recommended_course_evidence") or []:
            if not isinstance(row, dict):
                continue
            penalty = row.get("quality_issue_penalty") if isinstance(row.get("quality_issue_penalty"), dict) else {}
            issue_types = [
                str(issue_type).strip()
                for issue_type in penalty.get("issue_types") or []
                if str(issue_type).strip()
            ]
            if not (penalty.get("applied") or issue_types):
                continue
            penalty_course_count += 1
            course_name = str(row.get("course_name") or "").strip()
            if course_name:
                course_names.append(course_name)
            for issue_type in issue_types:
                issue_counts[issue_type] += 1
            affected_concepts = penalty.get("affected_concepts") if isinstance(penalty.get("affected_concepts"), list) else []
            if not affected_concepts:
                concept_issue_types = penalty.get("concept_issue_types") if isinstance(penalty.get("concept_issue_types"), dict) else {}
                affected_concepts = [
                    {
                        "concept_id": concept_id,
                        "issue_types": concept_issue_types.get(str(concept_id), issue_types),
                    }
                    for concept_id in penalty.get("concept_ids") or []
                ]
            for concept in affected_concepts:
                if not isinstance(concept, dict):
                    continue
                concept_id = _as_int(concept.get("concept_id"))
                if not concept_id:
                    continue
                concept_issue_types = [
                    str(issue_type).strip()
                    for issue_type in (concept.get("issue_types") or issue_types)
                    if str(issue_type).strip()
                ]
                entry = concept_rows.setdefault(
                    concept_id,
                    {
                        "concept_id": concept_id,
                        "course_names": [],
                        "issue_counts": Counter(),
                        "penalty_course_count": 0,
                    },
                )
                entry["penalty_course_count"] = int(entry.get("penalty_course_count") or 0) + 1
                if course_name:
                    entry["course_names"].append(course_name)
                for issue_type in concept_issue_types:
                    entry["issue_counts"][issue_type] += 1
    concepts_by_id: dict[int, dict[str, Any]] = {}
    for concept_id, item in concept_rows.items():
        concepts_by_id[concept_id] = {
            "concept_id": concept_id,
            "recommendation_penalty_course_count": int(item.get("penalty_course_count") or 0),
            "penalized_recommendation_row_count": int(item.get("penalty_course_count") or 0),
            "recommendation_penalty_issue_counts": dict(sorted((item.get("issue_counts") or Counter()).items())),
            "source_transition_penalty_issue_counts": dict(sorted((item.get("issue_counts") or Counter()).items())),
            "recommendation_penalty_course_names": list(dict.fromkeys(item.get("course_names") or []))[:10],
            "source_transition_penalty_course_names": list(dict.fromkeys(item.get("course_names") or []))[:10],
        }
    return {
        "available": True,
        "source_schema": report.get("schema"),
        "scenario_count": int(evaluation.get("scenario_count") or 0),
        "penalized_recommendation_row_count": penalty_course_count,
        "distinct_penalized_course_count": len(dict.fromkeys(course_names)),
        "concept_count": len(concepts_by_id),
        "issue_counts": dict(sorted(issue_counts.items())),
        "course_names": list(dict.fromkeys(course_names))[:10],
        "concepts_by_id": concepts_by_id,
    }


def _transition_penalty_ksa_term_groups(
    conn: sqlite3.Connection,
    *,
    concept_ids: list[int],
    selected_issue_types: list[str],
    sample_limit: int,
    min_issue_count: int,
    limit: int,
) -> list[dict[str, Any]]:
    concept_ids = sorted({int(concept_id) for concept_id in concept_ids if int(concept_id or 0)})
    if not concept_ids:
        return []
    max_samples = _clamp_int(sample_limit, default=3, minimum=0, maximum=10)
    min_count = _clamp_int(min_issue_count, default=1, minimum=1, maximum=1000000)
    max_groups = _clamp_int(limit, default=100, minimum=1, maximum=500)
    _ensure_ksa_term_issue_temp(conn, selected_issue_types)
    concept_placeholders = ",".join("?" for _ in concept_ids)
    group_rows = rows_to_dicts(
        conn.execute(
            f"""
            WITH concept_ksa AS (
                SELECT DISTINCT kcl.concept_id, kcl.ksa_id
                FROM ksa_concept_links kcl
                WHERE kcl.concept_id IN ({concept_placeholders})
                UNION
                SELECT DISTINCT kacl.concept_id, kai.ksa_id
                FROM ksa_atomic_concept_links kacl
                JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
                WHERE kacl.concept_id IN ({concept_placeholders})
            ),
            issue_ksa AS (
                SELECT
                    ck.concept_id,
                    tir.*
                FROM concept_ksa ck
                JOIN temp_ksa_term_issue_rows tir ON tir.ksa_id = ck.ksa_id
            )
            SELECT
                normalized_ksa_text,
                MIN(ksa_text_raw) AS representative_ksa_text,
                COUNT(DISTINCT issue_id) AS issue_count,
                COUNT(DISTINCT ksa_id) AS ksa_count,
                COUNT(DISTINCT element_id) AS element_count,
                COUNT(DISTINCT unit_code) AS unit_count,
                COUNT(DISTINCT major_code) AS major_count,
                MIN(detected_at) AS first_detected_at,
                MAX(detected_at) AS last_detected_at,
                GROUP_CONCAT(DISTINCT concept_id) AS source_penalty_concept_ids
            FROM issue_ksa
            WHERE normalized_ksa_text <> ''
            GROUP BY normalized_ksa_text
            HAVING issue_count >= ?
            ORDER BY issue_count DESC, unit_count DESC, representative_ksa_text
            LIMIT ?
            """,
            (*concept_ids, *concept_ids, min_count, max_groups),
        ).fetchall()
    )
    normalized_keys = sorted({str(row.get("normalized_ksa_text") or "") for row in group_rows if row.get("normalized_ksa_text")})
    detail_buckets: dict[str, list[dict[str, Any]]] = {}
    sample_buckets: dict[str, list[dict[str, Any]]] = {}
    if normalized_keys:
        key_placeholders = ",".join("?" for _ in normalized_keys)
        detail_rows = rows_to_dicts(
            conn.execute(
                f"""
                WITH concept_ksa AS (
                    SELECT DISTINCT kcl.concept_id, kcl.ksa_id
                    FROM ksa_concept_links kcl
                    WHERE kcl.concept_id IN ({concept_placeholders})
                    UNION
                    SELECT DISTINCT kacl.concept_id, kai.ksa_id
                    FROM ksa_atomic_concept_links kacl
                    JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
                    WHERE kacl.concept_id IN ({concept_placeholders})
                ),
                issue_ksa AS (
                    SELECT DISTINCT
                        tir.*
                    FROM concept_ksa ck
                    JOIN temp_ksa_term_issue_rows tir ON tir.ksa_id = ck.ksa_id
                )
                SELECT *
                FROM issue_ksa
                WHERE normalized_ksa_text IN ({key_placeholders})
                ORDER BY normalized_ksa_text, issue_type, issue_id
                """,
                (*concept_ids, *concept_ids, *normalized_keys),
            ).fetchall()
        )
        for detail in detail_rows:
            key = str(detail.get("normalized_ksa_text") or "")
            detail_buckets.setdefault(key, []).append(detail)
            bucket = sample_buckets.setdefault(key, [])
            if len(bucket) < max_samples:
                bucket.append(
                    _trim_payload(
                        {
                            "issue_id": detail.get("issue_id"),
                            "issue_type": detail.get("issue_type"),
                            "severity": detail.get("severity"),
                            "ksa_id": detail.get("ksa_id"),
                            "ksa_no": detail.get("ksa_no"),
                            "ksa_type_name": detail.get("ksa_type_name"),
                            "ksa_text_raw": detail.get("ksa_text_raw"),
                            "review_status": detail.get("review_status"),
                            "unit_code": detail.get("unit_code"),
                            "unit_name_raw": detail.get("unit_name_raw"),
                            "element_id": detail.get("element_id"),
                            "element_name_raw": detail.get("element_name_raw"),
                            "major_code": detail.get("major_code"),
                            "major_name": detail.get("major_name"),
                            "sub_scope": ":".join(
                                str(detail.get(key_name) or "")
                                for key_name in ("major_code", "middle_code", "small_code", "sub_code")
                            ),
                            "issue_detail": detail.get("issue_detail"),
                            "suggested_action": neutralize_suggested_action(
                                detail.get("suggested_action"),
                                issue_type=detail.get("issue_type"),
                                target_type="ksa",
                            ),
                        }
                    )
                )
    groups: list[dict[str, Any]] = []
    for row in group_rows:
        key = str(row.get("normalized_ksa_text") or "")
        details = detail_buckets.get(key, [])
        issue_type_counts: dict[str, int] = {}
        ksa_type_counts: dict[str, int] = {}
        review_status_counts: dict[str, int] = {}
        severity_counts: dict[str, int] = {}
        raw_variants: set[str] = set()
        for detail in details:
            issue_type = str(detail.get("issue_type") or "unknown")
            issue_type_counts[issue_type] = issue_type_counts.get(issue_type, 0) + 1
            ksa_type = str(detail.get("ksa_type_name") or "unknown")
            ksa_type_counts[ksa_type] = ksa_type_counts.get(ksa_type, 0) + 1
            review_status = str(detail.get("review_status") or "unknown")
            review_status_counts[review_status] = review_status_counts.get(review_status, 0) + 1
            severity = str(detail.get("severity") or "unknown")
            severity_counts[severity] = severity_counts.get(severity, 0) + 1
            raw_text = str(detail.get("ksa_text_raw") or "").strip()
            if raw_text:
                raw_variants.add(raw_text)
        source_concept_ids = sorted(
            {
                int(value)
                for value in str(row.get("source_penalty_concept_ids") or "").split(",")
                if str(value).strip().isdigit()
            }
        )
        ordered_issue_types = sorted(issue_type_counts)
        issue_count = int(row.get("issue_count") or 0)
        profile = _ksa_term_review_profile(
            issue_type_counts=issue_type_counts,
            issue_count=issue_count,
            unit_count=int(row.get("unit_count") or 0),
            major_count=int(row.get("major_count") or 0),
            raw_variant_count=len(raw_variants),
            representative_ksa_text=row.get("representative_ksa_text"),
        )
        groups.append(
            {
                "normalized_ksa_term": key,
                "normalized_ksa_text": key,
                "representative_ksa_text": row.get("representative_ksa_text"),
                "raw_ksa_text_variants": sorted(raw_variants)[:10],
                "raw_ksa_text_variant_count": len(raw_variants),
                "issue_type_counts": dict(sorted(issue_type_counts.items())),
                "ksa_type_counts": dict(sorted(ksa_type_counts.items())),
                "review_status_counts": dict(sorted(review_status_counts.items())),
                "severity_counts": dict(sorted(severity_counts.items())),
                "issue_count": issue_count,
                "ksa_count": int(row.get("ksa_count") or 0),
                "element_count": int(row.get("element_count") or 0),
                "unit_count": int(row.get("unit_count") or 0),
                "major_count": int(row.get("major_count") or 0),
                "first_detected_at": row.get("first_detected_at"),
                "last_detected_at": row.get("last_detected_at"),
                "recommended_review_action": _ksa_term_review_action(ordered_issue_types),
                **profile,
                "human_decision_required": True,
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
                "review_pack_source": "transition_quality_penalty_concept",
                "included_by_transition_penalty": True,
                "source_penalty_concept_ids": source_concept_ids,
                "decision_fields": {
                    "decision": "",
                    "reviewer_id": "",
                    "reviewed_at": "",
                    "rationale": "",
                    "proposed_term_action": "",
                },
                "samples": sample_buckets.get(key, []),
            }
        )
    return groups


def _term_concept_impact_rows(
    conn: sqlite3.Connection,
    normalized_terms: list[str],
    *,
    selected_issue_types: list[str],
) -> list[dict[str, Any]]:
    normalized_terms = [term for term in normalized_terms if term]
    if not normalized_terms:
        return []
    _ensure_ksa_term_issue_temp(conn, selected_issue_types)
    placeholders = ",".join("?" for _ in normalized_terms)
    return rows_to_dicts(
        conn.execute(
            f"""
            WITH term_ksa AS (
                SELECT DISTINCT
                    ksa_id,
                    normalized_ksa_text AS normalized_ksa_term
                FROM temp_ksa_term_issue_rows
                WHERE normalized_ksa_text IN ({placeholders})
            ),
            direct_links AS (
                SELECT
                    tk.normalized_ksa_term,
                    tk.ksa_id,
                    kcl.concept_id,
                    'direct' AS link_source
                FROM term_ksa tk
                JOIN ksa_concept_links kcl ON kcl.ksa_id = tk.ksa_id
            ),
            atomic_links AS (
                SELECT
                    tk.normalized_ksa_term,
                    tk.ksa_id,
                    kacl.concept_id,
                    'atomic' AS link_source
                FROM term_ksa tk
                JOIN ksa_atomic_items kai ON kai.ksa_id = tk.ksa_id
                JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
            ),
            concept_links AS (
                SELECT * FROM direct_links
                UNION ALL
                SELECT * FROM atomic_links
            )
            SELECT
                cl.normalized_ksa_term,
                oc.concept_id,
                oc.concept_name,
                oc.concept_type,
                oc.definition_status,
                oc.review_status,
                COUNT(DISTINCT cl.ksa_id) AS linked_ksa_count,
                COUNT(DISTINCT CASE WHEN cl.link_source = 'direct' THEN cl.ksa_id END) AS direct_linked_ksa_count,
                COUNT(DISTINCT CASE WHEN cl.link_source = 'atomic' THEN cl.ksa_id END) AS atomic_linked_ksa_count
            FROM concept_links cl
            JOIN ontology_concepts oc ON oc.concept_id = cl.concept_id
            GROUP BY cl.normalized_ksa_term, oc.concept_id
            ORDER BY cl.normalized_ksa_term, linked_ksa_count DESC, oc.concept_id
            """,
            normalized_terms,
        ).fetchall()
    )


def build_ksa_term_ontology_impact_report(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    concept_limit_per_group: int = 10,
    sample_limit: int = 3,
    min_issue_count: int = 1,
    issue_types: list[str] | None = None,
    transition_quality_report: dict[str, Any] | None = None,
    transition_quality_report_path: str | None = None,
) -> dict[str, Any]:
    max_concepts = _clamp_int(concept_limit_per_group, default=10, minimum=1, maximum=50)
    review_pack = build_ksa_term_preprocessing_review_pack(
        conn,
        limit=limit,
        sample_limit=sample_limit,
        min_issue_count=min_issue_count,
        issue_types=issue_types,
    )
    final_group_limit = _clamp_int(review_pack.get("limit"), default=limit, minimum=1, maximum=500)
    recommendation_penalty_summary = _transition_quality_penalty_summary(transition_quality_report)
    recommendation_penalties_by_concept = recommendation_penalty_summary.get("concepts_by_id") or {}
    selected_issue_types = list(review_pack.get("issue_types") or _validated_ksa_term_issue_types(issue_types))
    groups = list(review_pack.get("groups") or [])
    for group in groups:
        group.setdefault("review_pack_source", "open_issue_frequency")
        group.setdefault("included_by_transition_penalty", False)
        group.setdefault("source_penalty_concept_ids", [])
    base_group_keys = {str(group.get("normalized_ksa_term") or "") for group in groups}
    transition_penalty_candidate_groups = _transition_penalty_ksa_term_groups(
        conn,
        concept_ids=[int(concept_id) for concept_id in recommendation_penalties_by_concept],
        selected_issue_types=selected_issue_types,
        sample_limit=sample_limit,
        min_issue_count=min_issue_count,
        limit=max(final_group_limit, len(recommendation_penalties_by_concept) * 3),
    )
    transition_penalty_supplemental_candidate_group_count = 0
    for group in transition_penalty_candidate_groups:
        key = str(group.get("normalized_ksa_term") or "")
        if not key or key in base_group_keys:
            continue
        groups.append(group)
        base_group_keys.add(key)
        transition_penalty_supplemental_candidate_group_count += 1
    normalized_terms = [str(group.get("normalized_ksa_term") or "") for group in groups]
    impact_rows = _term_concept_impact_rows(
        conn,
        normalized_terms,
        selected_issue_types=selected_issue_types,
    )
    concept_ids = sorted({int(row["concept_id"]) for row in impact_rows if int(row.get("concept_id") or 0)})
    task_relation_counts = _count_by_concept_id(
        conn,
        concept_ids,
        """
        WITH relation_concepts AS (
            SELECT source_concept_id AS concept_id, relation_id
            FROM task_ksa_concept_relations
            WHERE source_concept_id IN ({placeholders})
            UNION
            SELECT target_concept_id AS concept_id, relation_id
            FROM task_ksa_concept_relations
            WHERE target_concept_id IN ({placeholders})
        )
        SELECT concept_id, COUNT(DISTINCT relation_id) AS count
        FROM relation_concepts
        GROUP BY concept_id
        """,
    )
    criteria_link_counts = _count_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT concept_id, COUNT(DISTINCT criteria_id) AS count
        FROM criteria_concept_links
        WHERE concept_id IN ({placeholders})
        GROUP BY concept_id
        """,
    )
    course_link_counts = _count_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT concept_id, COUNT(DISTINCT training_course_id) AS count
        FROM ncs_training_course_concept_links
        WHERE concept_id IN ({placeholders})
        GROUP BY concept_id
        """,
    )
    goal_link_counts = _count_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT concept_id, COUNT(DISTINCT training_course_id) AS count
        FROM training_goal_concept_links
        WHERE concept_id IN ({placeholders})
        GROUP BY concept_id
        """,
    )
    same_as_source_counts = _count_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT source_concept_id AS concept_id, COUNT(*) AS count
        FROM ontology_concept_relations
        WHERE source_concept_id IN ({placeholders})
          AND relation_type = 'same_as'
          AND review_status != 'rejected'
        GROUP BY source_concept_id
        """,
    )
    job_base_auxiliary_signals = _job_base_auxiliary_signals_by_concept_id(conn, concept_ids)
    task_relation_record_ids = _record_ids_by_concept_id(
        conn,
        concept_ids,
        """
        WITH relation_concepts AS (
            SELECT source_concept_id AS concept_id, relation_id AS record_id
            FROM task_ksa_concept_relations
            WHERE source_concept_id IN ({placeholders})
            UNION
            SELECT target_concept_id AS concept_id, relation_id AS record_id
            FROM task_ksa_concept_relations
            WHERE target_concept_id IN ({placeholders})
        )
        SELECT concept_id, record_id
        FROM relation_concepts
        """,
    )
    training_course_record_ids = _record_ids_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT concept_id, training_course_id AS record_id
        FROM ncs_training_course_concept_links
        WHERE concept_id IN ({placeholders})
        """,
    )
    training_goal_record_ids = _record_ids_by_concept_id(
        conn,
        concept_ids,
        """
        SELECT concept_id, training_course_id AS record_id
        FROM training_goal_concept_links
        WHERE concept_id IN ({placeholders})
        """,
    )

    rows_by_term: dict[str, list[dict[str, Any]]] = {}
    for row in impact_rows:
        concept_id = int(row["concept_id"])
        recommendation_penalty = dict(recommendation_penalties_by_concept.get(concept_id) or {})
        enriched = {
            **row,
            "concept_id": concept_id,
            "linked_ksa_count": int(row.get("linked_ksa_count") or 0),
            "direct_linked_ksa_count": int(row.get("direct_linked_ksa_count") or 0),
            "atomic_linked_ksa_count": int(row.get("atomic_linked_ksa_count") or 0),
            "task_relation_count": int(task_relation_counts.get(concept_id, 0)),
            "criteria_link_count": int(criteria_link_counts.get(concept_id, 0)),
            "training_course_link_count": int(course_link_counts.get(concept_id, 0)),
            "training_goal_link_count": int(goal_link_counts.get(concept_id, 0)),
            "same_as_source_relation_count": int(same_as_source_counts.get(concept_id, 0)),
            "job_base_auxiliary_signal": job_base_auxiliary_signals.get(
                concept_id,
                _empty_job_base_auxiliary_signal(schema="ncs_job_base_concept_auxiliary_signal_v1"),
            ),
            "recommendation_penalty_course_count": int(
                recommendation_penalty.get("recommendation_penalty_course_count") or 0
            ),
            "recommendation_penalty_issue_counts": recommendation_penalty.get("recommendation_penalty_issue_counts")
            or {},
            "recommendation_penalty_course_names": recommendation_penalty.get("recommendation_penalty_course_names")
            or [],
        }
        rows_by_term.setdefault(str(row.get("normalized_ksa_term") or ""), []).append(enriched)

    impact_groups: list[dict[str, Any]] = []
    for group in groups:
        key = str(group.get("normalized_ksa_term") or "")
        concept_rows = rows_by_term.get(key, [])
        concept_rows.sort(
            key=lambda row: (
                -int(row.get("recommendation_penalty_course_count") or 0),
                -int(row.get("linked_ksa_count") or 0),
                -int(row.get("task_relation_count") or 0),
                -int(row.get("training_course_link_count") or 0),
                int(row.get("concept_id") or 0),
            )
        )
        group_penalty_issue_counts: Counter[str] = Counter()
        group_penalty_course_names: list[str] = []
        group_penalty_concept_rows: list[dict[str, Any]] = []
        group_penalty_concept_count = 0
        for concept in concept_rows:
            if int(concept.get("recommendation_penalty_course_count") or 0) <= 0:
                continue
            group_penalty_concept_count += 1
            group_penalty_course_names.extend(concept.get("recommendation_penalty_course_names") or [])
            group_penalty_issue_counts.update(concept.get("recommendation_penalty_issue_counts") or {})
            group_penalty_concept_rows.append(
                {
                    "concept_id": concept.get("concept_id"),
                    "concept_name": concept.get("concept_name"),
                    "concept_type": concept.get("concept_type"),
                    "review_status": concept.get("review_status"),
                    "linked_penalty_rows": concept.get("recommendation_penalty_course_count"),
                    "linked_penalty_issues": concept.get("recommendation_penalty_issue_counts") or {},
                    "linked_penalty_courses": concept.get("recommendation_penalty_course_names") or [],
                    "task_relation_count": concept.get("task_relation_count"),
                    "criteria_link_count": concept.get("criteria_link_count"),
                    "training_course_link_count": concept.get("training_course_link_count"),
                    "training_goal_link_count": concept.get("training_goal_link_count"),
                    "job_base_auxiliary_signal": concept.get("job_base_auxiliary_signal")
                    or _empty_job_base_auxiliary_signal(schema="ncs_job_base_concept_auxiliary_signal_v1"),
                }
            )
        group_concept_ids = [int(concept.get("concept_id") or 0) for concept in concept_rows]
        group_task_relation_ids: set[str] = set()
        group_training_course_ids: set[str] = set()
        group_training_goal_ids: set[str] = set()
        for concept_id in group_concept_ids:
            group_task_relation_ids.update(task_relation_record_ids.get(concept_id, set()))
            group_training_course_ids.update(training_course_record_ids.get(concept_id, set()))
            group_training_goal_ids.update(training_goal_record_ids.get(concept_id, set()))
        group_task_relation_count = len(group_task_relation_ids)
        group_training_course_link_count = len(group_training_course_ids)
        group_training_goal_link_count = len(group_training_goal_ids)
        group_training_link_count = len(group_training_course_ids | group_training_goal_ids)
        minimal_review_priority = _minimal_review_priority_profile(
            review_bucket=str(group.get("review_bucket") or ""),
            issue_count=int(group.get("issue_count") or 0),
            linked_concept_count=len(concept_rows),
            linked_penalty_concept_count=group_penalty_concept_count,
            task_relation_count=group_task_relation_count,
            training_link_count=group_training_link_count,
        )
        impact_groups.append(
            {
                "normalized_ksa_term": key,
                "representative_ksa_text": group.get("representative_ksa_text"),
                "review_bucket": group.get("review_bucket"),
                "review_pack_source": group.get("review_pack_source") or "open_issue_frequency",
                "included_by_transition_penalty": bool(group.get("included_by_transition_penalty")),
                "source_penalty_concept_ids": sorted(
                    {
                        int(concept_id)
                        for concept_id in [
                            *(group.get("source_penalty_concept_ids") or []),
                            *(concept.get("concept_id") for concept in group_penalty_concept_rows),
                        ]
                        if _as_int(concept_id)
                    }
                ),
                "issue_count": int(group.get("issue_count") or 0),
                "ksa_count": int(group.get("ksa_count") or 0),
                "unit_count": int(group.get("unit_count") or 0),
                "major_count": int(group.get("major_count") or 0),
                "operator_impact_action": _impact_operator_action(str(group.get("review_bucket") or "")),
                "auto_apply_allowed": False,
                "status_update_allowed": False,
                "linked_concept_count": len(concept_rows),
                "linked_concept_ids": group_concept_ids,
                "group_task_relation_count": group_task_relation_count,
                "group_training_course_link_count": group_training_course_link_count,
                "group_training_goal_link_count": group_training_goal_link_count,
                "group_training_link_count": group_training_link_count,
                "job_base_auxiliary_signal": _group_job_base_auxiliary_signal(concept_rows),
                **minimal_review_priority,
                "recommendation_penalty": {
                    "concept_count": group_penalty_concept_count,
                    "course_count": len(dict.fromkeys(group_penalty_course_names)),
                    "issue_counts": dict(sorted(group_penalty_issue_counts.items())),
                    "course_names": list(dict.fromkeys(group_penalty_course_names))[:10],
                    "operator_action": "review_before_accepting_penalized_recommendations"
                    if group_penalty_concept_count
                    else "no_transition_penalty_seen",
                },
                "linked_penalized_concepts": {
                    "concept_count": group_penalty_concept_count,
                    "distinct_course_count": len(dict.fromkeys(group_penalty_course_names)),
                    "issue_counts": dict(sorted(group_penalty_issue_counts.items())),
                    "course_names": list(dict.fromkeys(group_penalty_course_names))[:10],
                    "scope_note": "Concept-level transition penalty evidence linked to this KSA term group; not proof that this raw term directly caused the penalty.",
                    "operator_action": "inspect_linked_penalized_concepts_before_manual_decision"
                    if group_penalty_concept_count
                    else "no_linked_transition_penalty_seen",
                },
                "linked_penalized_concept_rows": group_penalty_concept_rows,
                "top_concepts": concept_rows[:max_concepts],
                "samples": group.get("samples") or [],
            }
        )

    impact_groups.sort(
        key=lambda group: (
            -int(group.get("minimal_review_priority_score") or 0),
            -int((group.get("linked_penalized_concepts") or {}).get("concept_count") or 0),
            -int(group.get("group_task_relation_count") or 0),
            str(group.get("normalized_ksa_term") or ""),
        )
    )
    candidate_group_count = len(impact_groups)
    if len(impact_groups) > final_group_limit:
        impact_groups = impact_groups[:final_group_limit]
    dropped_group_count = max(0, candidate_group_count - len(impact_groups))

    represented_issue_count = sum(int(group.get("issue_count") or 0) for group in impact_groups)
    represented_ksa_count = sum(int(group.get("ksa_count") or 0) for group in impact_groups)
    represented_issue_reduction = max(0, represented_issue_count - len(impact_groups))
    review_bucket_counts = dict(
        sorted(Counter(str(group.get("review_bucket") or "unknown") for group in impact_groups).items())
    )
    minimal_review_priority_level_counts = dict(
        sorted(Counter(str(group.get("minimal_review_priority_level") or "unknown") for group in impact_groups).items())
    )
    final_concept_ids = sorted(
        {
            int(concept_id)
            for group in impact_groups
            for concept_id in group.get("linked_concept_ids") or []
            if int(concept_id or 0)
        }
    )
    final_task_relation_ids: set[str] = set()
    final_training_course_ids: set[str] = set()
    final_training_goal_ids: set[str] = set()
    for concept_id in final_concept_ids:
        final_task_relation_ids.update(task_relation_record_ids.get(concept_id, set()))
        final_training_course_ids.update(training_course_record_ids.get(concept_id, set()))
        final_training_goal_ids.update(training_goal_record_ids.get(concept_id, set()))
    final_recommendation_penalty_concept_ids = sorted(
        set(final_concept_ids).intersection(int(concept_id) for concept_id in recommendation_penalties_by_concept)
    )
    recommendation_penalty_group_count = sum(
        1 for group in impact_groups if int((group.get("linked_penalized_concepts") or {}).get("concept_count") or 0)
    )
    impacted_group_count = sum(1 for group in impact_groups if int(group.get("linked_concept_count") or 0))
    total_linked_concept_rows = sum(int(group.get("linked_concept_count") or 0) for group in impact_groups)
    job_base_auxiliary_group_count = sum(
        1 for group in impact_groups if int((group.get("job_base_auxiliary_signal") or {}).get("competency_count") or 0)
    )
    job_base_auxiliary_concept_ids = sorted(
        {
            int(concept_id)
            for concept_id in final_concept_ids
            if int((job_base_auxiliary_signals.get(concept_id) or {}).get("competency_count") or 0) > 0
        }
    )
    transition_penalty_supplemental_group_count = sum(
        1 for group in impact_groups if group.get("review_pack_source") == "transition_quality_penalty_concept"
    )

    return {
        "ok": True,
        "schema": KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA,
        "generated_at": now_utc(),
        "source_schema": review_pack.get("schema"),
        "issue_types": review_pack.get("issue_types"),
        "limit": review_pack.get("limit"),
        "concept_limit_per_group": max_concepts,
        "sample_limit": review_pack.get("sample_limit"),
        "min_issue_count": review_pack.get("min_issue_count"),
        "status_update_allowed": False,
        "db_writes": False,
        "human_decision_required": True,
        "approval_claim": False,
        "transition_quality_report_path": transition_quality_report_path,
        "transition_quality_report_available": bool(recommendation_penalty_summary.get("available")),
        "transition_quality_report_schema": recommendation_penalty_summary.get("source_schema"),
        "transition_quality_scenario_count": recommendation_penalty_summary.get("scenario_count"),
        "transition_penalty_candidate_group_count": len(transition_penalty_candidate_groups),
        "transition_penalty_supplemental_candidate_group_count": transition_penalty_supplemental_candidate_group_count,
        "transition_penalty_supplemental_group_count": transition_penalty_supplemental_group_count,
        "candidate_group_count": candidate_group_count,
        "dropped_group_count": dropped_group_count,
        "group_limit_policy": "final_groups_capped_by_limit_after_transition_penalty_priority_sort",
        "group_count": len(impact_groups),
        "impacted_group_count": impacted_group_count,
        "recommendation_penalty_group_count": recommendation_penalty_group_count,
        "job_base_auxiliary_group_count": job_base_auxiliary_group_count,
        "job_base_auxiliary_concept_count": len(job_base_auxiliary_concept_ids),
        "job_base_auxiliary_signal_role": "supporting_gap_context_not_primary_evidence",
        "recommendation_penalty_concept_count": recommendation_penalty_summary.get("concept_count"),
        "source_transition_penalty_concept_count": recommendation_penalty_summary.get("concept_count"),
        "represented_recommendation_penalty_concept_count": len(final_recommendation_penalty_concept_ids),
        "unrepresented_recommendation_penalty_concept_count": max(
            0,
            int(recommendation_penalty_summary.get("concept_count") or 0)
            - len(final_recommendation_penalty_concept_ids),
        ),
        "recommendation_penalty_course_count": recommendation_penalty_summary.get("distinct_penalized_course_count"),
        "source_transition_penalized_recommendation_row_count": recommendation_penalty_summary.get(
            "penalized_recommendation_row_count"
        ),
        "source_transition_distinct_penalized_course_count": recommendation_penalty_summary.get(
            "distinct_penalized_course_count"
        ),
        "recommendation_penalty_issue_counts": recommendation_penalty_summary.get("issue_counts"),
        "source_transition_penalty_issue_counts": recommendation_penalty_summary.get("issue_counts"),
        "recommendation_penalty_course_names": recommendation_penalty_summary.get("course_names"),
        "source_transition_penalty_course_names": recommendation_penalty_summary.get("course_names"),
        "total_open_issue_count": review_pack.get("total_open_issue_count"),
        "represented_issue_count": represented_issue_count,
        "represented_ksa_count": represented_ksa_count,
        "represented_issue_reduction": represented_issue_reduction,
        "review_bucket_counts": review_bucket_counts,
        "minimal_review_priority_level_counts": minimal_review_priority_level_counts,
        "max_minimal_review_priority_score": max(
            (int(group.get("minimal_review_priority_score") or 0) for group in impact_groups),
            default=0,
        ),
        "total_unique_impacted_concept_count": len(final_concept_ids),
        "total_linked_concept_rows": total_linked_concept_rows,
        "total_task_relation_count": len(final_task_relation_ids),
        "total_criteria_link_count": _count_distinct_records_for_concepts(
            conn,
            final_concept_ids,
            """
            SELECT DISTINCT criteria_id AS record_id
            FROM criteria_concept_links
            WHERE concept_id IN ({placeholders})
            """,
        ),
        "total_training_course_link_count": len(final_training_course_ids),
        "total_training_goal_link_count": len(final_training_goal_ids),
        "groups": impact_groups,
        "notes": [
            "Read-only impact report for KSA term preprocessing groups and linked ontology concepts.",
            "This report proposes operator inspection targets only; it does not downweight, merge, approve, or update records.",
            "Use broad generic buckets to prioritize manual decisions that affect many concepts, tasks, or courses.",
            "When a transition quality report is supplied, recommendation_penalty fields identify KSA concepts that actually affected evaluated recommendations.",
        ],
    }


def build_ksa_term_ontology_impact_report_from_db(
    db_path: Path,
    *,
    limit: int = 50,
    concept_limit_per_group: int = 10,
    sample_limit: int = 3,
    min_issue_count: int = 1,
    issue_types: list[str] | None = None,
    transition_quality_report_path: Path | None = None,
) -> dict[str, Any]:
    transition_quality_report = None
    if transition_quality_report_path is not None:
        transition_quality_report = json.loads(transition_quality_report_path.read_text(encoding="utf-8"))
    conn = _connect_readonly(db_path)
    try:
        return build_ksa_term_ontology_impact_report(
            conn,
            limit=limit,
            concept_limit_per_group=concept_limit_per_group,
            sample_limit=sample_limit,
            min_issue_count=min_issue_count,
            issue_types=issue_types,
            transition_quality_report=transition_quality_report,
            transition_quality_report_path=str(transition_quality_report_path)
            if transition_quality_report_path is not None
            else None,
        )
    finally:
        conn.close()


def build_ksa_term_minimal_review_slice(
    impact_report: dict[str, Any],
    *,
    source_path: str | None = None,
    limit: int = 25,
    levels: list[str] | None = None,
) -> dict[str, Any]:
    if impact_report.get("schema") != KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA:
        raise ValueError(
            "KSA term minimal review slice requires "
            f"{KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA}; got {impact_report.get('schema')!r}."
        )
    selected_levels = [str(level).strip() for level in (levels or DEFAULT_MINIMAL_REVIEW_LEVELS) if str(level).strip()]
    if not selected_levels:
        selected_levels = list(DEFAULT_MINIMAL_REVIEW_LEVELS)
    invalid_levels = sorted({level for level in selected_levels if level not in MINIMAL_REVIEW_LEVEL_SET})
    if invalid_levels:
        raise ValueError(
            "Unsupported minimal review priority level(s): "
            + ", ".join(invalid_levels)
            + ". Allowed values: "
            + ", ".join(sorted(MINIMAL_REVIEW_LEVEL_SET))
        )
    selected_level_set = set(selected_levels)
    max_items = _clamp_int(limit, default=25, minimum=1, maximum=200)
    source_groups = [group for group in impact_report.get("groups") or [] if isinstance(group, dict)]
    filtered_groups = [
        group
        for group in source_groups
        if str(group.get("minimal_review_priority_level") or "") in selected_level_set
    ]
    filtered_groups.sort(
        key=lambda group: (
            -int(group.get("minimal_review_priority_score") or 0),
            -int((group.get("linked_penalized_concepts") or {}).get("concept_count") or 0),
            -int(group.get("group_task_relation_count") or 0),
            str(group.get("normalized_ksa_term") or ""),
        )
    )
    items: list[dict[str, Any]] = []
    represented_penalty_concept_ids: set[int] = set()
    for rank, group in enumerate(filtered_groups[:max_items], start=1):
        if isinstance(group.get("linked_penalized_concept_rows"), list):
            penalty_concepts = [
                concept for concept in group.get("linked_penalized_concept_rows") or [] if isinstance(concept, dict)
            ]
        else:
            penalty_concepts = [
                concept
                for concept in group.get("top_concepts") or []
                if isinstance(concept, dict) and int(concept.get("recommendation_penalty_course_count") or 0) > 0
            ]
        for concept in penalty_concepts:
            concept_id = _as_int(concept.get("concept_id"))
            if concept_id:
                represented_penalty_concept_ids.add(concept_id)
        for concept_id in group.get("source_penalty_concept_ids") or []:
            parsed = _as_int(concept_id)
            if parsed:
                represented_penalty_concept_ids.add(parsed)
        item = {
            "schema": "ncs_ksa_term_minimal_review_slice_item_v1",
            "rank": rank,
            "item_id": f"ksa-term-minimal-review-{rank:04d}",
            "normalized_ksa_term": group.get("normalized_ksa_term"),
            "representative_ksa_text": group.get("representative_ksa_text"),
            "review_bucket": group.get("review_bucket"),
            "review_pack_source": group.get("review_pack_source"),
            "included_by_transition_penalty": bool(group.get("included_by_transition_penalty")),
            "source_penalty_concept_ids": group.get("source_penalty_concept_ids") or [],
            "minimal_review_priority_score": int(group.get("minimal_review_priority_score") or 0),
            "minimal_review_priority_level": group.get("minimal_review_priority_level"),
            "minimal_review_priority_reasons": group.get("minimal_review_priority_reasons") or [],
            "minimal_review_operator_action": group.get("minimal_review_operator_action"),
            "minimal_review_scope_note": group.get("minimal_review_scope_note"),
            "operator_impact_action": group.get("operator_impact_action"),
            "issue_count": int(group.get("issue_count") or 0),
            "ksa_count": int(group.get("ksa_count") or 0),
            "unit_count": int(group.get("unit_count") or 0),
            "major_count": int(group.get("major_count") or 0),
            "linked_concept_count": int(group.get("linked_concept_count") or 0),
            "group_task_relation_count": int(group.get("group_task_relation_count") or 0),
            "group_training_course_link_count": int(group.get("group_training_course_link_count") or 0),
            "group_training_goal_link_count": int(group.get("group_training_goal_link_count") or 0),
            "linked_penalized_concepts": group.get("linked_penalized_concepts") or {},
            "top_penalized_concepts": [
                {
                    "concept_id": concept.get("concept_id"),
                    "concept_name": concept.get("concept_name"),
                    "concept_type": concept.get("concept_type"),
                    "review_status": concept.get("review_status"),
                    "linked_penalty_rows": concept.get("linked_penalty_rows")
                    if "linked_penalty_rows" in concept
                    else concept.get("recommendation_penalty_course_count"),
                    "linked_penalty_issues": concept.get("linked_penalty_issues")
                    if "linked_penalty_issues" in concept
                    else concept.get("recommendation_penalty_issue_counts") or {},
                    "linked_penalty_courses": concept.get("linked_penalty_courses")
                    if "linked_penalty_courses" in concept
                    else concept.get("recommendation_penalty_course_names") or [],
                    "task_relation_count": concept.get("task_relation_count"),
                    "training_course_link_count": concept.get("training_course_link_count"),
                    "training_goal_link_count": concept.get("training_goal_link_count"),
                    "job_base_auxiliary_signal": concept.get("job_base_auxiliary_signal")
                    or _empty_job_base_auxiliary_signal(schema="ncs_job_base_concept_auxiliary_signal_v1"),
                }
                for concept in penalty_concepts
            ],
            "job_base_auxiliary_signal": group.get("job_base_auxiliary_signal")
            or _empty_job_base_auxiliary_signal(schema="ncs_job_base_group_auxiliary_signal_v1"),
            "top_concepts": (group.get("top_concepts") or [])[:8],
            "samples": (group.get("samples") or [])[:3],
            "decision_fields": {
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "proposed_term_action": "",
            },
            "human_decision_required": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
        }
        items.append(item)
    level_counts = Counter(str(item.get("minimal_review_priority_level") or "unknown") for item in items)
    concept_groups_by_id: dict[int, dict[str, Any]] = {}
    for item in items:
        for concept in item.get("top_penalized_concepts") or []:
            if not isinstance(concept, dict):
                continue
            concept_id = _as_int(concept.get("concept_id"))
            if not concept_id:
                continue
            entry = concept_groups_by_id.setdefault(
                concept_id,
                {
                    "concept_id": concept_id,
                    "concept_name": concept.get("concept_name"),
                    "concept_type": concept.get("concept_type"),
                    "review_status": concept.get("review_status"),
                    "item_ranks": [],
                    "item_ids": [],
                    "term_variants": [],
                    "normalized_terms": [],
                    "priority_levels": [],
                    "max_priority_score": 0,
                    "issue_counts": Counter(),
                    "course_names": [],
                    "task_relation_count": 0,
                    "training_course_link_count": 0,
                    "training_goal_link_count": 0,
                    "job_base_competency_names": [],
                    "job_base_factor_labels": [],
                    "job_base_concept_signal_count": 0,
                },
            )
            entry["item_ranks"].append(item.get("rank"))
            entry["item_ids"].append(item.get("item_id"))
            entry["term_variants"].append(item.get("representative_ksa_text"))
            entry["normalized_terms"].append(item.get("normalized_ksa_term"))
            entry["priority_levels"].append(item.get("minimal_review_priority_level"))
            entry["max_priority_score"] = max(
                int(entry.get("max_priority_score") or 0),
                int(item.get("minimal_review_priority_score") or 0),
            )
            entry["issue_counts"].update(concept.get("linked_penalty_issues") or {})
            entry["course_names"].extend(concept.get("linked_penalty_courses") or [])
            entry["task_relation_count"] = max(
                int(entry.get("task_relation_count") or 0),
                int(concept.get("task_relation_count") or 0),
            )
            entry["training_course_link_count"] = max(
                int(entry.get("training_course_link_count") or 0),
                int(concept.get("training_course_link_count") or 0),
            )
            entry["training_goal_link_count"] = max(
                int(entry.get("training_goal_link_count") or 0),
                int(concept.get("training_goal_link_count") or 0),
            )
            job_base_signal = (
                concept.get("job_base_auxiliary_signal")
                if isinstance(concept.get("job_base_auxiliary_signal"), dict)
                else {}
            )
            if int(job_base_signal.get("competency_count") or 0) > 0:
                entry["job_base_concept_signal_count"] = 1
                for name in job_base_signal.get("competency_names") or []:
                    if name and name not in entry["job_base_competency_names"]:
                        entry["job_base_competency_names"].append(name)
                for label in job_base_signal.get("factor_labels") or []:
                    if label and label not in entry["job_base_factor_labels"]:
                        entry["job_base_factor_labels"].append(label)
    concept_review_groups: list[dict[str, Any]] = []
    for entry in concept_groups_by_id.values():
        concept_group = {
            "concept_id": entry["concept_id"],
            "concept_name": entry.get("concept_name"),
            "concept_type": entry.get("concept_type"),
            "review_status": entry.get("review_status"),
            "item_count": len(set(entry.get("item_ids") or [])),
            "item_ranks": sorted({int(rank) for rank in entry.get("item_ranks") or [] if _as_int(rank)}),
            "item_ids": list(dict.fromkeys(entry.get("item_ids") or [])),
            "term_variants": list(dict.fromkeys(value for value in entry.get("term_variants") or [] if value)),
            "normalized_terms": list(dict.fromkeys(value for value in entry.get("normalized_terms") or [] if value)),
            "priority_levels": sorted({str(value) for value in entry.get("priority_levels") or [] if value}),
            "max_priority_score": int(entry.get("max_priority_score") or 0),
            "issue_counts": dict(sorted((entry.get("issue_counts") or Counter()).items())),
            "course_names": list(dict.fromkeys(entry.get("course_names") or []))[:10],
            "task_relation_count": int(entry.get("task_relation_count") or 0),
            "training_course_link_count": int(entry.get("training_course_link_count") or 0),
            "training_goal_link_count": int(entry.get("training_goal_link_count") or 0),
            "job_base_auxiliary_signal": {
                "schema": "ncs_job_base_concept_review_group_auxiliary_signal_v1",
                "evidence_role": "supporting_gap_context_not_primary_evidence",
                "scoring_role": "review_priority_context_only",
                "concept_signal_count": int(entry.get("job_base_concept_signal_count") or 0),
                "competency_count": len(entry.get("job_base_competency_names") or []),
                "factor_count": len(entry.get("job_base_factor_labels") or []),
                "competency_names": list(entry.get("job_base_competency_names") or [])[:8],
                "factor_labels": list(entry.get("job_base_factor_labels") or [])[:12],
                "operator_action": "use_only_as_supporting_transition_gap_context_not_primary_ksa_evidence"
                if int(entry.get("job_base_concept_signal_count") or 0)
                else "no_job_base_auxiliary_signal",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            },
            "operator_action": "review_concept_term_variants_before_scoring_or_label_decision",
            "decision_fields": {
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "proposed_concept_action": "",
            },
            "human_decision_required": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
        }
        concept_group["genericity_signal"] = _ksa_term_genericity_signal(concept_group)
        concept_group["suggested_decision"] = _suggest_ksa_term_minimal_review_decision(concept_group)
        concept_review_groups.append(concept_group)
    concept_review_groups.sort(
        key=lambda group: (
            -int(group.get("max_priority_score") or 0),
            -int(group.get("item_count") or 0),
            -int(group.get("task_relation_count") or 0),
            int(group.get("concept_id") or 0),
        )
    )
    return {
        "ok": True,
        "schema": KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA,
        "generated_at": now_utc(),
        "source_impact_report_path": source_path,
        "source_impact_schema": impact_report.get("schema"),
        "source_group_count": impact_report.get("group_count"),
        "source_candidate_group_count": impact_report.get("candidate_group_count"),
        "source_dropped_group_count": impact_report.get("dropped_group_count"),
        "source_transition_penalty_concept_count": impact_report.get("source_transition_penalty_concept_count"),
        "source_represented_recommendation_penalty_concept_count": impact_report.get(
            "represented_recommendation_penalty_concept_count"
        ),
        "selected_priority_levels": selected_levels,
        "limit": max_items,
        "candidate_item_count": len(filtered_groups),
        "item_count": len(items),
        "dropped_item_count": max(0, len(filtered_groups) - len(items)),
        "item_priority_level_counts": dict(sorted(level_counts.items())),
        "represented_recommendation_penalty_concept_count": len(represented_penalty_concept_ids),
        "concept_review_group_count": len(concept_review_groups),
        "concept_review_groups": concept_review_groups,
        "jsonl_record_type": "concept_review_group",
        "status_update_allowed": False,
        "db_writes": False,
        "human_decision_required": True,
        "approval_claim": False,
        "manual_review_contract": {
            "scope": "minimal_ksa_term_triage",
            "allowed_decisions": list(KSA_TERM_MINIMAL_REVIEW_ALLOWED_DECISIONS),
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
            "raw_source_mutation_allowed": False,
            "preferred_decision_unit": "concept_review_group",
            "suggested_decision_policy": "review_assist_only_not_a_human_decision",
        },
        "items": items,
        "notes": [
            "Read-only slice of the KSA term ontology impact report for minimal human review.",
            "Items are selected by priority level and score; this artifact does not approve, reject, merge, or update records.",
            "Use decision_fields only as blank operator-entry placeholders in a controlled review workflow.",
        ],
    }


def build_ksa_term_minimal_review_slice_from_file(
    impact_report_path: Path,
    *,
    limit: int = 25,
    levels: list[str] | None = None,
) -> dict[str, Any]:
    impact_report = json.loads(impact_report_path.read_text(encoding="utf-8"))
    return build_ksa_term_minimal_review_slice(
        impact_report,
        source_path=str(impact_report_path),
        limit=limit,
        levels=levels,
    )


def _safe_ratio_percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 4)


def _safe_reduction_ratio(source_count: int, review_count: int) -> float:
    if review_count <= 0:
        return 0.0
    return round(source_count / review_count, 4)


def build_ksa_review_minimization_audit(
    impact_report: dict[str, Any],
    minimal_review_slice: dict[str, Any],
    *,
    readiness_report: dict[str, Any] | None = None,
    source_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    if impact_report.get("schema") != KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA:
        raise ValueError(
            "KSA review minimization audit requires impact report schema "
            f"{KSA_TERM_ONTOLOGY_IMPACT_REPORT_SCHEMA}; got {impact_report.get('schema')!r}."
        )
    if minimal_review_slice.get("schema") != KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA:
        raise ValueError(
            "KSA review minimization audit requires minimal slice schema "
            f"{KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA}; got {minimal_review_slice.get('schema')!r}."
        )
    if readiness_report is not None and readiness_report.get("schema") != KSA_TERM_REVIEW_READINESS_SCHEMA:
        raise ValueError(
            "KSA review minimization audit requires readiness schema "
            f"{KSA_TERM_REVIEW_READINESS_SCHEMA}; got {readiness_report.get('schema')!r}."
        )

    concept_groups = [
        group for group in minimal_review_slice.get("concept_review_groups") or [] if isinstance(group, dict)
    ]
    source_open_issue_count = int(impact_report.get("total_open_issue_count") or 0)
    represented_issue_count = int(impact_report.get("represented_issue_count") or 0)
    represented_ksa_count = int(impact_report.get("represented_ksa_count") or 0)
    concept_review_group_count = int(
        minimal_review_slice.get("concept_review_group_count") or len(concept_groups)
    )
    minimal_review_item_count = int(minimal_review_slice.get("item_count") or 0)
    candidate_group_count = int(impact_report.get("candidate_group_count") or 0)
    impact_group_count = int(impact_report.get("group_count") or 0)
    source_transition_penalty_concept_count = int(
        impact_report.get("source_transition_penalty_concept_count") or 0
    )
    represented_transition_penalty_concept_count = int(
        minimal_review_slice.get("represented_recommendation_penalty_concept_count")
        or impact_report.get("represented_recommendation_penalty_concept_count")
        or 0
    )
    source_transition_penalized_row_count = int(
        impact_report.get("source_transition_penalized_recommendation_row_count") or 0
    )

    genericity_level_counts: Counter[str] = Counter()
    genericity_action_counts: Counter[str] = Counter()
    high_genericity_groups: list[dict[str, Any]] = []
    suggested_decision_counts: Counter[str] = Counter()
    suggested_confidence_counts: Counter[str] = Counter()
    all_review_assist_safe = True
    for group in concept_groups:
        signal = group.get("genericity_signal") if isinstance(group.get("genericity_signal"), dict) else {}
        level = str(signal.get("level") or "missing")
        action = str(signal.get("operator_action") or "missing")
        genericity_level_counts[level] += 1
        genericity_action_counts[action] += 1
        if (
            signal.get("scoring_role") != "review_assist_only_not_a_human_decision"
            or signal.get("status_update_allowed") is not False
            or signal.get("db_writes") is not False
            or signal.get("approval_claim") is not False
        ):
            all_review_assist_safe = False
        if level == "high":
            high_genericity_groups.append(
                {
                    "concept_id": group.get("concept_id"),
                    "concept_name": group.get("concept_name"),
                    "concept_type": group.get("concept_type"),
                    "score": signal.get("score"),
                    "reasons": signal.get("reasons") or [],
                    "operator_action": signal.get("operator_action"),
                    "max_priority_score": group.get("max_priority_score"),
                    "task_relation_count": group.get("task_relation_count"),
                    "training_course_link_count": group.get("training_course_link_count"),
                    "suggested_decision": (
                        (group.get("suggested_decision") or {}).get("suggested_decision")
                        if isinstance(group.get("suggested_decision"), dict)
                        else None
                    ),
                }
            )
        suggested = group.get("suggested_decision") if isinstance(group.get("suggested_decision"), dict) else {}
        suggested_decision_counts[str(suggested.get("suggested_decision") or "missing")] += 1
        suggested_confidence_counts[str(suggested.get("suggested_decision_confidence") or "missing")] += 1

    readiness_summary = readiness_report.get("summary") if isinstance(readiness_report, dict) else {}
    pending_decision_count = int(
        (readiness_summary or {}).get("pending_decision_count")
        if readiness_summary
        else concept_review_group_count
    )
    completed_decision_count = int((readiness_summary or {}).get("completed_decision_count") or 0)
    action_count = int((readiness_summary or {}).get("action_count") or 0)

    safety_flags = {
        "impact_status_update_allowed": impact_report.get("status_update_allowed"),
        "impact_db_writes": impact_report.get("db_writes"),
        "impact_approval_claim": impact_report.get("approval_claim"),
        "slice_status_update_allowed": minimal_review_slice.get("status_update_allowed"),
        "slice_db_writes": minimal_review_slice.get("db_writes"),
        "slice_approval_claim": minimal_review_slice.get("approval_claim"),
        "readiness_status_update_allowed": readiness_report.get("status_update_allowed")
        if isinstance(readiness_report, dict)
        else False,
        "readiness_db_writes": readiness_report.get("db_writes") if isinstance(readiness_report, dict) else False,
        "readiness_approval_claim": readiness_report.get("approval_claim")
        if isinstance(readiness_report, dict)
        else False,
        "genericity_review_assist_safe": all_review_assist_safe,
    }
    safety_ok = all(
        value is False or key == "genericity_review_assist_safe" and value is True
        for key, value in safety_flags.items()
    )

    review_reduction = {
        "source_open_issue_count": source_open_issue_count,
        "represented_issue_count": represented_issue_count,
        "represented_ksa_count": represented_ksa_count,
        "impact_group_count": impact_group_count,
        "candidate_group_count": candidate_group_count,
        "minimal_review_item_count": minimal_review_item_count,
        "concept_review_group_count": concept_review_group_count,
        "pending_decision_count": pending_decision_count,
        "completed_decision_count": completed_decision_count,
        "action_count": action_count,
        "open_issue_to_concept_review_group_ratio": _safe_reduction_ratio(
            source_open_issue_count, concept_review_group_count
        ),
        "open_issue_to_pending_decision_ratio": _safe_reduction_ratio(
            source_open_issue_count, pending_decision_count
        ),
        "represented_issue_to_concept_review_group_ratio": _safe_reduction_ratio(
            represented_issue_count, concept_review_group_count
        ),
        "transition_penalty_concept_coverage_percent": _safe_ratio_percent(
            represented_transition_penalty_concept_count,
            source_transition_penalty_concept_count,
        ),
        "source_transition_penalty_concept_count": source_transition_penalty_concept_count,
        "represented_transition_penalty_concept_count": represented_transition_penalty_concept_count,
        "source_transition_penalized_recommendation_row_count": source_transition_penalized_row_count,
        "review_unit": "concept_review_group",
        "human_review_minimized_by": [
            "group_duplicate_or_short_ksa_terms",
            "collapse_linked_penalized_concepts",
            "surface_only_high_impact_concept_review_groups",
            "keep_decision_fields_blank_until_operator_click_or_csv_entry",
        ],
    }

    next_actions = [
        {
            "rank": 1,
            "action": "review_high_genericity_groups_first",
            "scope": "concept_review_group",
            "group_count": int(genericity_level_counts.get("high", 0)),
            "reason": "High genericity groups affect broad task/course evidence and are best suited for one-click downweight or scope-split review.",
            "db_writes": False,
            "approval_claim": False,
        },
        {
            "rank": 2,
            "action": "connect_review_assist_to_quality_issue_penalty_explanations",
            "scope": "recommendation_output_metadata",
            "group_count": concept_review_group_count,
            "reason": "Expose review-assist context beside existing quality penalties without creating a reviewed status.",
            "db_writes": False,
            "approval_claim": False,
        },
        {
            "rank": 3,
            "action": "prepare_guarded_operator_rules_only_after_human_decisions",
            "scope": "future_guarded_apply",
            "group_count": action_count,
            "reason": "Action plans stay empty until explicit human decisions are present.",
            "db_writes": False,
            "approval_claim": False,
        },
    ]

    return {
        "ok": safety_ok,
        "schema": KSA_REVIEW_MINIMIZATION_AUDIT_SCHEMA,
        "generated_at": now_utc(),
        "source_paths": source_paths or {},
        "review_reduction": review_reduction,
        "genericity_signal_summary": {
            "schema": "ncs_ksa_genericity_review_assist_summary_v1",
            "level_counts": dict(sorted(genericity_level_counts.items())),
            "operator_action_counts": dict(sorted(genericity_action_counts.items())),
            "high_signal_group_count": int(genericity_level_counts.get("high", 0)),
            "high_signal_groups": high_genericity_groups[:10],
            "suggested_decision_counts": dict(sorted(suggested_decision_counts.items())),
            "suggested_decision_confidence_counts": dict(sorted(suggested_confidence_counts.items())),
            "scoring_role": "review_assist_only_not_a_human_decision",
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
        },
        "safety_contract": {
            "raw_source_mutation_allowed": False,
            "trusted_status_write_allowed": False,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "forbidden_automatic_statuses": ["human_reviewed", "accepted", "reviewed"],
            "decision_fields_blank_until_human_action": pending_decision_count == concept_review_group_count
            and completed_decision_count == 0,
            "review_assist_only": True,
            "safety_flags": safety_flags,
        },
        "ontology_advancement_next_actions": next_actions,
        "notes": [
            "This audit measures how much KSA preprocessing review work is collapsed into concept-level review groups.",
            "Genericity signals are review assistance only; they are not human decisions and do not change reviewed status.",
            "Recommendation scoring changes must continue to use guarded quality-penalty paths, not automatic approval states.",
        ],
    }


def build_ksa_review_minimization_audit_from_files(
    impact_report_path: Path,
    minimal_review_slice_path: Path,
    *,
    readiness_report_path: Path | None = None,
) -> dict[str, Any]:
    impact_report = json.loads(impact_report_path.read_text(encoding="utf-8-sig"))
    if minimal_review_slice_path.suffix.lower() == ".csv":
        raise ValueError(
            "minimal_review_slice must be a JSON slice report; "
            f"got CSV path {minimal_review_slice_path}. "
            "Use the --out JSON from ksa-term-minimal-review-slice."
        )
    minimal_review_slice = json.loads(minimal_review_slice_path.read_text(encoding="utf-8-sig"))
    readiness_report = (
        json.loads(readiness_report_path.read_text(encoding="utf-8"))
        if readiness_report_path is not None
        else None
    )
    return build_ksa_review_minimization_audit(
        impact_report,
        minimal_review_slice,
        readiness_report=readiness_report,
        source_paths={
            "impact_report": str(impact_report_path),
            "minimal_review_slice": str(minimal_review_slice_path),
            "readiness_report": str(readiness_report_path) if readiness_report_path is not None else None,
        },
    )


def write_ksa_review_minimization_audit_markdown(report: dict[str, Any], out_path: Path) -> None:
    reduction = report.get("review_reduction") if isinstance(report.get("review_reduction"), dict) else {}
    genericity = (
        report.get("genericity_signal_summary")
        if isinstance(report.get("genericity_signal_summary"), dict)
        else {}
    )
    safety = report.get("safety_contract") if isinstance(report.get("safety_contract"), dict) else {}
    lines = [
        "# KSA Review Minimization Audit",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- ok: `{report.get('ok')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- source_open_issue_count: `{reduction.get('source_open_issue_count')}`",
        f"- concept_review_group_count: `{reduction.get('concept_review_group_count')}`",
        f"- pending_decision_count: `{reduction.get('pending_decision_count')}`",
        f"- open_issue_to_concept_review_group_ratio: `{reduction.get('open_issue_to_concept_review_group_ratio')}`",
        f"- transition_penalty_concept_coverage_percent: `{reduction.get('transition_penalty_concept_coverage_percent')}`",
        f"- genericity_level_counts: `{genericity.get('level_counts')}`",
        f"- suggested_decision_counts: `{genericity.get('suggested_decision_counts')}`",
        f"- status_update_allowed: `{safety.get('status_update_allowed')}`",
        f"- db_writes: `{safety.get('db_writes')}`",
        f"- approval_claim: `{safety.get('approval_claim')}`",
        f"- trusted_status_write_allowed: `{safety.get('trusted_status_write_allowed')}`",
        "",
        "## High Genericity Review-Assist Groups",
        "",
        "| concept_id | concept_name | type | score | task_relations | training_links | operator_action |",
        "| --- | --- | --- | ---: | ---: | ---: | --- |",
    ]
    for group in genericity.get("high_signal_groups") or []:
        if not isinstance(group, dict):
            continue
        lines.append(
            "| {concept_id} | {concept_name} | {concept_type} | {score} | {task_relation_count} | "
            "{training_course_link_count} | {operator_action} |".format(
                concept_id=group.get("concept_id"),
                concept_name=str(group.get("concept_name") or "").replace("|", "\\|"),
                concept_type=group.get("concept_type"),
                score=group.get("score"),
                task_relation_count=group.get("task_relation_count"),
                training_course_link_count=group.get("training_course_link_count"),
                operator_action=str(group.get("operator_action") or "").replace("|", "\\|"),
            )
        )
    lines.extend(
        [
            "",
            "## Next Actions",
            "",
        ]
    )
    for action in report.get("ontology_advancement_next_actions") or []:
        if not isinstance(action, dict):
            continue
        lines.append(
            "- `{rank}` `{action}`: {reason} db_writes=`{db_writes}` approval_claim=`{approval_claim}`".format(
                rank=action.get("rank"),
                action=action.get("action"),
                reason=action.get("reason"),
                db_writes=action.get("db_writes"),
                approval_claim=action.get("approval_claim"),
            )
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
        ]
    )
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_ksa_term_minimal_review_slice_jsonl(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for group in report.get("concept_review_groups") or []:
            row = {
                "schema": "ncs_ksa_term_minimal_review_slice_concept_group_v1",
                **group,
            }
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_ksa_term_minimal_review_slice_csv(report: dict[str, Any], out_path: Path) -> dict[str, Any]:
    def _csv_join(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        return "; ".join(str(item) for item in value if item is not None and str(item) != "")

    def _csv_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return "; ".join(f"{key}={count}" for key, count in sorted(value.items()))

    fieldnames = list(KSA_TERM_MINIMAL_REVIEW_DECISION_CSV_FIELDS)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for group in report.get("concept_review_groups") or []:
        decision_fields = group.get("decision_fields") if isinstance(group.get("decision_fields"), dict) else {}
        job_base_signal = (
            group.get("job_base_auxiliary_signal")
            if isinstance(group.get("job_base_auxiliary_signal"), dict)
            else {}
        )
        suggested_decision = (
            group.get("suggested_decision")
            if isinstance(group.get("suggested_decision"), dict)
            else _suggest_ksa_term_minimal_review_decision(group)
        )
        rows.append(
            {
                "schema": "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1",
                "concept_id": group.get("concept_id"),
                "concept_name": group.get("concept_name"),
                "concept_type": group.get("concept_type"),
                "review_status": group.get("review_status"),
                "item_count": int(group.get("item_count") or 0),
                "item_ranks": _csv_join(group.get("item_ranks")),
                "term_variants": _csv_join(group.get("term_variants")),
                "normalized_terms": _csv_join(group.get("normalized_terms")),
                "priority_levels": _csv_join(group.get("priority_levels")),
                "max_priority_score": int(group.get("max_priority_score") or 0),
                "issue_counts": _csv_counts(group.get("issue_counts")),
                "course_names": _csv_join(group.get("course_names")),
                "task_relation_count": int(group.get("task_relation_count") or 0),
                "training_course_link_count": int(group.get("training_course_link_count") or 0),
                "training_goal_link_count": int(group.get("training_goal_link_count") or 0),
                "job_base_factor_labels": _csv_join(job_base_signal.get("factor_labels")),
                "operator_action": group.get("operator_action"),
                "suggested_decision": suggested_decision.get("suggested_decision") or "",
                "suggested_decision_confidence": suggested_decision.get("suggested_decision_confidence") or "",
                "suggested_decision_rationale": suggested_decision.get("suggested_decision_rationale") or "",
                "suggested_decision_policy": suggested_decision.get("suggested_decision_policy") or "",
                "decision": decision_fields.get("decision") or "",
                "proposed_concept_action": decision_fields.get("proposed_concept_action") or "",
                "reviewer_id": decision_fields.get("reviewer_id") or "",
                "reviewed_at": decision_fields.get("reviewed_at") or "",
                "rationale": decision_fields.get("rationale") or "",
                "human_decision_required": bool(group.get("human_decision_required")),
                "status_update_allowed": bool(group.get("status_update_allowed")),
                "db_writes": bool(group.get("db_writes")),
                "approval_claim": bool(group.get("approval_claim")),
            }
        )
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(_csv_row(row, fieldnames) for row in rows)
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "schema": "ncs_ksa_term_minimal_review_slice_concept_group_decision_v1",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def audit_ksa_term_minimal_review_decision_csv(
    csv_path: Path,
    *,
    source_manifest_path: Path | None = None,
    source_slice_path: Path | None = None,
) -> dict[str, Any]:
    def _is_explicit_false(value: Any) -> bool:
        return str(value if value is not None else "").strip().lower() in {"false", "0", "no", "n"}

    def _is_explicit_true(value: Any) -> bool:
        return str(value if value is not None else "").strip().lower() in {"true", "1", "yes", "y"}

    def _read_json_report(path: Path | None, label: str) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if path is None:
            return None, []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return None, [{"type": f"{label}_read_error", "message": str(exc)}]
        except json.JSONDecodeError as exc:
            return None, [{"type": f"{label}_json_error", "message": str(exc)}]
        if not isinstance(payload, dict):
            return None, [{"type": f"{label}_not_object"}]
        return payload, []

    def _manifest_base_dir() -> Path | None:
        return source_manifest_path.parent if source_manifest_path is not None else None

    def _resolve_manifest_artifact_path(value: Any) -> Path | None:
        if not value:
            return None
        path = Path(str(value))
        if path.is_absolute():
            return path
        base_dir = _manifest_base_dir()
        if base_dir is not None:
            return base_dir / path
        candidates = []
        candidates.extend([Path.cwd() / path, path])
        try:
            root = Path(__file__).resolve().parents[2]
            candidates.append(root / path)
        except IndexError:
            pass
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return candidates[0] if candidates else path

    def _same_path(left: Any, right: Path) -> bool:
        left_path = _resolve_manifest_artifact_path(left)
        if left_path is None:
            return False
        try:
            return left_path.resolve() == right.resolve()
        except OSError:
            return str(left_path) == str(right)

    def _concept_id_sort_key(value: str) -> tuple[int, int | str]:
        return (0, int(value)) if value.isdigit() else (1, value)

    def _csv_join(value: Any) -> str:
        if not isinstance(value, list):
            return ""
        return "; ".join(str(item) for item in value if item is not None and str(item) != "")

    def _csv_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return "; ".join(f"{key}={count}" for key, count in sorted(value.items()))

    def _read_source_jsonl_rows(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text)
                    except json.JSONDecodeError as exc:
                        errors.append(
                            {
                                "type": "source_jsonl_json_error",
                                "line_number": line_number,
                                "message": str(exc),
                            }
                        )
                        continue
                    if not isinstance(row, dict):
                        errors.append(
                            {
                                "type": "source_jsonl_row_not_object",
                                "line_number": line_number,
                            }
                        )
                        continue
                    if row.get("schema") != "ncs_ksa_term_minimal_review_slice_concept_group_v1":
                        errors.append(
                            {
                                "type": "source_jsonl_schema_mismatch",
                                "line_number": line_number,
                                "actual": row.get("schema"),
                            }
                        )
                    concept_id = str(row.get("concept_id") or "").strip()
                    if concept_id:
                        rows.append(row)
                    else:
                        errors.append(
                            {
                                "type": "source_jsonl_missing_concept_id",
                                "line_number": line_number,
                            }
                        )
        except OSError as exc:
            errors.append({"type": "source_jsonl_read_error", "message": str(exc)})
        return rows, errors

    def _expected_jsonl_row(group: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "ncs_ksa_term_minimal_review_slice_concept_group_v1",
            **group,
        }

    def _json_value_equal(left: Any, right: Any) -> bool:
        return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
            right,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    required_fields = list(KSA_TERM_MINIMAL_REVIEW_DECISION_CSV_FIELDS)
    raw_text = csv_path.read_text(encoding="utf-8-sig")
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    allowed_decisions = set(KSA_TERM_MINIMAL_REVIEW_ALLOWED_DECISIONS)
    missing_fields = [field for field in required_fields if field not in fieldnames]
    invalid_rows: list[dict[str, Any]] = []
    completed_rows: list[dict[str, Any]] = []
    pending_rows: list[dict[str, Any]] = []
    decision_counts: Counter[str] = Counter()
    forbidden_status_terms = {"human_reviewed", "accepted", "reviewed"}
    forbidden_status_rows: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        decision = str(row.get("decision") or "").strip()
        concept_id = str(row.get("concept_id") or "").strip()
        row_errors: list[str] = []
        reviewer_id = str(row.get("reviewer_id") or "").strip()
        reviewed_at = str(row.get("reviewed_at") or "").strip()
        rationale = str(row.get("rationale") or "").strip()
        proposed_action = str(row.get("proposed_concept_action") or "").strip()
        if str(row.get("schema") or "").strip() != KSA_TERM_MINIMAL_REVIEW_DECISION_ROW_SCHEMA:
            row_errors.append("unexpected_schema")
        if not concept_id:
            row_errors.append("missing_concept_id")
        if not _is_explicit_true(row.get("human_decision_required")):
            row_errors.append("human_decision_required_not_true")
        for flag_field in ("status_update_allowed", "db_writes", "approval_claim"):
            if not _is_explicit_false(row.get(flag_field)):
                row_errors.append(f"{flag_field}_not_false")
        if decision:
            decision_counts[decision] += 1
            if decision not in allowed_decisions:
                row_errors.append("unsupported_decision")
            if not reviewer_id:
                row_errors.append("missing_reviewer_id")
            if not reviewed_at:
                row_errors.append("missing_reviewed_at")
            if not rationale:
                row_errors.append("missing_rationale")
        elif reviewer_id or reviewed_at or rationale or proposed_action:
            row_errors.append("pending_row_has_reviewer_or_action_metadata")
        if row_errors:
            invalid_rows.append(
                {
                    "row_number": index,
                    "concept_id": concept_id,
                    "decision": decision,
                    "errors": row_errors,
                }
            )
        elif decision:
            completed_rows.append(
                {
                    "row_number": index,
                    "concept_id": concept_id,
                    "decision": decision,
                }
            )
        else:
            pending_rows.append({"row_number": index, "concept_id": concept_id})
        status_text = " ".join([decision, proposed_action, str(row.get("review_status") or "")]).lower()
        matched_forbidden = sorted(
            term
            for term in forbidden_status_terms
            if re.search(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", status_text)
        )
        if matched_forbidden:
            forbidden_status_rows.append(
                {
                    "row_number": index,
                    "concept_id": concept_id,
                    "matched_terms": matched_forbidden,
                }
            )
    source_payload_exposed = "source_payload" in raw_text
    source_validation_errors: list[dict[str, Any]] = []
    if source_manifest_path is None:
        source_validation_errors.append({"type": "source_manifest_path_required"})
    if source_slice_path is None:
        source_validation_errors.append({"type": "source_slice_path_required"})
    source_slice_payload, source_slice_errors = _read_json_report(source_slice_path, "source_slice")
    source_slice_expected_count: int | None = None
    source_slice_match: bool | None = None
    source_slice_expected_concept_ids: list[str] | None = None
    source_slice_expected_jsonl_by_id: dict[str, dict[str, Any]] | None = None
    if source_slice_path is not None:
        source_slice_match = False
        if source_slice_payload is not None:
            if source_slice_payload.get("schema") != KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA:
                source_slice_errors.append(
                    {
                        "type": "source_slice_schema_mismatch",
                        "expected": KSA_TERM_MINIMAL_REVIEW_SLICE_SCHEMA,
                        "actual": source_slice_payload.get("schema"),
                    }
                )
            source_groups = source_slice_payload.get("concept_review_groups")
            if not isinstance(source_groups, list):
                source_slice_errors.append({"type": "source_slice_missing_concept_review_groups"})
                source_groups = []
            source_slice_expected_count = int(
                source_slice_payload.get("concept_review_group_count") or len(source_groups)
            )
            if source_slice_expected_count != len(rows):
                source_slice_errors.append(
                    {
                        "type": "source_slice_row_count_mismatch",
                        "expected": source_slice_expected_count,
                        "actual": len(rows),
                    }
                )
            expected_by_id: dict[str, dict[str, Any]] = {}
            duplicate_expected: list[str] = []
            for group in source_groups:
                if not isinstance(group, dict):
                    continue
                concept_id = str(group.get("concept_id") or "").strip()
                if not concept_id:
                    continue
                if concept_id in expected_by_id:
                    duplicate_expected.append(concept_id)
                expected_by_id[concept_id] = group
            csv_by_id: dict[str, dict[str, Any]] = {}
            duplicate_csv: list[str] = []
            for row in rows:
                concept_id = str(row.get("concept_id") or "").strip()
                if not concept_id:
                    continue
                if concept_id in csv_by_id:
                    duplicate_csv.append(concept_id)
                csv_by_id[concept_id] = row
            missing_in_csv = sorted(set(expected_by_id) - set(csv_by_id), key=_concept_id_sort_key)
            extra_in_csv = sorted(set(csv_by_id) - set(expected_by_id), key=_concept_id_sort_key)
            metadata_mismatches = []
            for concept_id in sorted(set(expected_by_id) & set(csv_by_id), key=_concept_id_sort_key):
                expected = expected_by_id[concept_id]
                actual = csv_by_id[concept_id]
                source_suggestion = (
                    expected.get("suggested_decision")
                    if isinstance(expected.get("suggested_decision"), dict)
                    else _suggest_ksa_term_minimal_review_decision(expected)
                )
                expected_fields = {
                    "concept_name": expected.get("concept_name"),
                    "concept_type": expected.get("concept_type"),
                }
                job_base_signal = (
                    expected.get("job_base_auxiliary_signal")
                    if isinstance(expected.get("job_base_auxiliary_signal"), dict)
                    else {}
                )
                optional_expected_fields = {
                    "review_status": expected.get("review_status"),
                    "item_count": int(expected.get("item_count") or 0),
                    "item_ranks": _csv_join(expected.get("item_ranks")),
                    "term_variants": _csv_join(expected.get("term_variants")),
                    "normalized_terms": _csv_join(expected.get("normalized_terms")),
                    "priority_levels": _csv_join(expected.get("priority_levels")),
                    "max_priority_score": int(expected.get("max_priority_score") or 0),
                    "issue_counts": _csv_counts(expected.get("issue_counts")),
                    "course_names": _csv_join(expected.get("course_names")),
                    "task_relation_count": int(expected.get("task_relation_count") or 0),
                    "training_course_link_count": int(expected.get("training_course_link_count") or 0),
                    "training_goal_link_count": int(expected.get("training_goal_link_count") or 0),
                    "job_base_factor_labels": _csv_join(job_base_signal.get("factor_labels")),
                    "operator_action": expected.get("operator_action"),
                    "suggested_decision": source_suggestion.get("suggested_decision") or "",
                    "suggested_decision_confidence": (
                        source_suggestion.get("suggested_decision_confidence") or ""
                    ),
                    "suggested_decision_rationale": (
                        source_suggestion.get("suggested_decision_rationale") or ""
                    ),
                    "suggested_decision_policy": (
                        source_suggestion.get("suggested_decision_policy") or ""
                    ),
                    "human_decision_required": bool(expected.get("human_decision_required")),
                    "status_update_allowed": bool(expected.get("status_update_allowed")),
                    "db_writes": bool(expected.get("db_writes")),
                    "approval_claim": bool(expected.get("approval_claim")),
                }
                expected_fields.update(
                    {
                        field: expected_value
                        for field, expected_value in optional_expected_fields.items()
                        if field in fieldnames
                    }
                )
                for field, expected_value in expected_fields.items():
                    if not _csv_exported_value_matches_source(expected_value, actual.get(field)):
                        metadata_mismatches.append(
                            {
                                "concept_id": concept_id,
                                "field": field,
                                "expected": expected_value,
                                "actual": actual.get(field),
                            }
                        )
            if duplicate_expected:
                source_slice_errors.append(
                    {"type": "source_slice_duplicate_concept_ids", "concept_ids": sorted(set(duplicate_expected))}
                )
            if duplicate_csv:
                source_slice_errors.append(
                    {"type": "csv_duplicate_concept_ids", "concept_ids": sorted(set(duplicate_csv))}
                )
            if missing_in_csv:
                source_slice_errors.append({"type": "source_slice_concepts_missing_in_csv", "concept_ids": missing_in_csv})
            if extra_in_csv:
                source_slice_errors.append({"type": "csv_concepts_not_in_source_slice", "concept_ids": extra_in_csv})
            if metadata_mismatches:
                source_slice_errors.append(
                    {
                        "type": "source_slice_concept_metadata_mismatch",
                        "mismatches": metadata_mismatches[:20],
                    }
                )
            source_slice_expected_concept_ids = sorted(expected_by_id, key=_concept_id_sort_key)
            source_slice_expected_jsonl_by_id = {
                concept_id: _expected_jsonl_row(group) for concept_id, group in expected_by_id.items()
            }
        source_slice_match = not source_slice_errors

    source_manifest_payload, source_manifest_errors = _read_json_report(source_manifest_path, "source_manifest")
    source_manifest_match: bool | None = None
    source_jsonl_path: Path | None = None
    source_jsonl_match: bool | None = None
    source_jsonl_errors: list[dict[str, Any]] = []
    if source_manifest_path is not None:
        source_manifest_match = False
        if source_manifest_payload is not None:
            if source_manifest_payload.get("schema") != "ncs_ksa_term_review_workflow_manifest_v1":
                source_manifest_errors.append(
                    {
                        "type": "source_manifest_schema_mismatch",
                        "expected": "ncs_ksa_term_review_workflow_manifest_v1",
                        "actual": source_manifest_payload.get("schema"),
                    }
                )
            for flag_field in ("status_update_allowed", "db_writes", "approval_claim"):
                if source_manifest_payload.get(flag_field) is not False:
                    source_manifest_errors.append(
                        {
                            "type": "source_manifest_top_level_flag_not_false",
                            "field": flag_field,
                            "actual": source_manifest_payload.get(flag_field),
                        }
                    )
            if source_manifest_payload.get("human_decision_required") is not True:
                source_manifest_errors.append(
                    {
                        "type": "source_manifest_human_decision_required_not_true",
                        "actual": source_manifest_payload.get("human_decision_required"),
                    }
                )
            safety_contract = source_manifest_payload.get("safety_contract")
            if not isinstance(safety_contract, dict):
                source_manifest_errors.append({"type": "source_manifest_missing_safety_contract"})
                safety_contract = {}
            for flag_field in (
                "raw_source_mutation_allowed",
                "trusted_status_write_allowed",
                "status_update_allowed",
                "db_writes",
                "approval_claim",
                "source_payload_exposed",
            ):
                if safety_contract.get(flag_field) is not False:
                    source_manifest_errors.append(
                        {
                            "type": "source_manifest_safety_flag_not_false",
                            "field": flag_field,
                            "actual": safety_contract.get(flag_field),
                        }
                    )
            if safety_contract.get("preferred_decision_unit") != "concept_review_group":
                source_manifest_errors.append(
                    {
                        "type": "source_manifest_preferred_decision_unit_mismatch",
                        "expected": "concept_review_group",
                        "actual": safety_contract.get("preferred_decision_unit"),
                    }
                )
            summary = source_manifest_payload.get("summary")
            if not isinstance(summary, dict):
                source_manifest_errors.append({"type": "source_manifest_missing_summary"})
                summary = {}
            for count_field in ("concept_review_group_count", "concept_review_csv_record_count"):
                if summary.get(count_field) is not None and int(summary.get(count_field) or 0) != len(rows):
                    source_manifest_errors.append(
                        {
                            "type": "source_manifest_row_count_mismatch",
                            "field": count_field,
                            "expected": int(summary.get(count_field) or 0),
                            "actual": len(rows),
                        }
                    )
            artifacts = source_manifest_payload.get("artifacts")
            if isinstance(artifacts, dict) and source_slice_path is not None:
                manifest_slice_path = artifacts.get("minimal_review_slice")
                if manifest_slice_path and not _same_path(manifest_slice_path, source_slice_path):
                    source_manifest_errors.append(
                        {
                            "type": "source_manifest_slice_path_mismatch",
                            "expected": str(source_slice_path),
                            "actual": manifest_slice_path,
                        }
                    )
                manifest_jsonl_path = artifacts.get("minimal_review_jsonl")
                if not manifest_jsonl_path:
                    source_jsonl_errors.append({"type": "source_manifest_missing_minimal_review_jsonl"})
                else:
                    source_jsonl_path = _resolve_manifest_artifact_path(manifest_jsonl_path)
                    if source_jsonl_path is None:
                        source_jsonl_errors.append({"type": "source_jsonl_path_empty"})
                    else:
                        source_jsonl_rows, jsonl_errors = _read_source_jsonl_rows(source_jsonl_path)
                        source_jsonl_errors.extend(jsonl_errors)
                        source_jsonl_ids = [
                            str(row.get("concept_id") or "").strip()
                            for row in source_jsonl_rows
                            if str(row.get("concept_id") or "").strip()
                        ]
                        if source_slice_expected_concept_ids is not None:
                            expected_ids = source_slice_expected_concept_ids
                            actual_ids = sorted(source_jsonl_ids, key=_concept_id_sort_key)
                            if len(source_jsonl_ids) != len(expected_ids):
                                source_jsonl_errors.append(
                                    {
                                        "type": "source_jsonl_row_count_mismatch",
                                        "expected": len(expected_ids),
                                        "actual": len(source_jsonl_ids),
                                    }
                                )
                            if actual_ids != expected_ids:
                                source_jsonl_errors.append(
                                    {
                                        "type": "source_jsonl_concept_id_mismatch",
                                        "expected": expected_ids[:50],
                                        "actual": actual_ids[:50],
                                    }
                                )
                        if source_slice_expected_jsonl_by_id is not None:
                            jsonl_by_id: dict[str, dict[str, Any]] = {}
                            for row in source_jsonl_rows:
                                concept_id = str(row.get("concept_id") or "").strip()
                                if concept_id and concept_id not in jsonl_by_id:
                                    jsonl_by_id[concept_id] = row
                            record_mismatches: list[dict[str, Any]] = []
                            for concept_id in sorted(
                                set(source_slice_expected_jsonl_by_id) & set(jsonl_by_id),
                                key=_concept_id_sort_key,
                            ):
                                expected_row = source_slice_expected_jsonl_by_id[concept_id]
                                actual_row = jsonl_by_id[concept_id]
                                if _json_value_equal(expected_row, actual_row):
                                    continue
                                differing_fields = sorted(
                                    field
                                    for field in set(expected_row) | set(actual_row)
                                    if not _json_value_equal(expected_row.get(field), actual_row.get(field))
                                )
                                record_mismatches.append(
                                    {
                                        "concept_id": concept_id,
                                        "fields": differing_fields[:25],
                                    }
                                )
                            if record_mismatches:
                                source_jsonl_errors.append(
                                    {
                                        "type": "source_jsonl_record_mismatch",
                                        "mismatches": record_mismatches[:20],
                                    }
                                )
                        duplicate_jsonl_ids = sorted(
                            {concept_id for concept_id in source_jsonl_ids if source_jsonl_ids.count(concept_id) > 1},
                            key=_concept_id_sort_key,
                        )
                        if duplicate_jsonl_ids:
                            source_jsonl_errors.append(
                                {
                                    "type": "source_jsonl_duplicate_concept_ids",
                                    "concept_ids": duplicate_jsonl_ids,
                                }
                            )
        source_manifest_match = not source_manifest_errors
        source_jsonl_match = not source_jsonl_errors if source_jsonl_path is not None else False

    ok = (
        not missing_fields
        and not invalid_rows
        and not forbidden_status_rows
        and not source_payload_exposed
        and not source_validation_errors
        and not source_slice_errors
        and not source_manifest_errors
        and not source_jsonl_errors
    )
    return {
        "ok": ok,
        "schema": KSA_TERM_MINIMAL_REVIEW_DECISION_AUDIT_SCHEMA,
        "expected_row_schema": KSA_TERM_MINIMAL_REVIEW_DECISION_ROW_SCHEMA,
        "generated_at": now_utc(),
        "csv_path": str(csv_path),
        "source_manifest_path": str(source_manifest_path) if source_manifest_path else None,
        "source_slice_path": str(source_slice_path) if source_slice_path else None,
        "source_manifest_validation_performed": source_manifest_path is not None,
        "source_slice_validation_performed": source_slice_path is not None,
        "source_validation_performed": source_manifest_path is not None
        and source_slice_path is not None,
        "source_validation_required_for_operator_evidence": True,
        "source_validation_errors": source_validation_errors[:50],
        "source_manifest_match": source_manifest_match,
        "source_manifest_errors": source_manifest_errors[:50],
        "source_slice_match": source_slice_match,
        "source_slice_expected_count": source_slice_expected_count,
        "source_slice_errors": source_slice_errors[:50],
        "source_jsonl_path": str(source_jsonl_path) if source_jsonl_path else None,
        "source_jsonl_validation_performed": source_jsonl_path is not None,
        "source_jsonl_match": source_jsonl_match,
        "source_jsonl_errors": source_jsonl_errors[:50],
        "row_count": len(rows),
        "field_count": len(fieldnames),
        "missing_required_fields": missing_fields,
        "allowed_decisions": list(KSA_TERM_MINIMAL_REVIEW_ALLOWED_DECISIONS),
        "decision_counts": dict(sorted(decision_counts.items())),
        "nonblank_decision_count": sum(decision_counts.values()),
        "completed_decision_count": len(completed_rows),
        "pending_decision_count": len(pending_rows),
        "invalid_decision_count": len(invalid_rows),
        "invalid_row_count": len(invalid_rows),
        "invalid_decision_rows": invalid_rows[:50],
        "invalid_rows": invalid_rows[:50],
        "forbidden_status_rows": forbidden_status_rows[:50],
        "source_payload_exposed": source_payload_exposed,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "raw_source_mutation_allowed": False,
        "trusted_status_write_allowed": False,
        "preferred_decision_unit": "concept_review_group",
        "next_action": "ready_for_guarded_operator_review"
        if ok and completed_rows
        else "await_human_decisions"
        if ok
        else "fix_decision_csv_before_any_review_accounting",
        "completed_rows": completed_rows[:50],
        "notes": [
            "Read-only audit of a KSA term minimal review CSV decision sheet.",
            "This audit does not update ontology concepts, quality issues, or review statuses.",
            "human_reviewed, accepted, and reviewed statuses remain forbidden automatic outputs.",
        ],
    }


def write_ksa_term_minimal_review_decision_audit_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# KSA Term Minimal Review Decision Audit",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- csv_path: `{_md_cell(report.get('csv_path'))}`",
        f"- row_count: `{report.get('row_count')}`",
        f"- nonblank_decision_count: `{report.get('nonblank_decision_count')}`",
        f"- completed_decision_count: `{report.get('completed_decision_count')}`",
        f"- pending_decision_count: `{report.get('pending_decision_count')}`",
        f"- invalid_decision_count: `{report.get('invalid_decision_count')}`",
        f"- source_validation_performed: `{report.get('source_validation_performed')}`",
        f"- source_validation_required_for_operator_evidence: `{report.get('source_validation_required_for_operator_evidence')}`",
        f"- source_validation_errors: `{len(report.get('source_validation_errors') or [])}`",
        f"- source_manifest_match: `{report.get('source_manifest_match')}`",
        f"- source_slice_match: `{report.get('source_slice_match')}`",
        f"- source_jsonl_match: `{report.get('source_jsonl_match')}`",
        f"- source_payload_exposed: `{report.get('source_payload_exposed')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- next_action: `{_md_cell(report.get('next_action'))}`",
        "",
        "## Allowed Decisions",
        "",
    ]
    for decision in report.get("allowed_decisions") or []:
        lines.append(f"- `{_md_cell(decision)}`")
    lines.extend(
        [
            "",
            "## Source Checks",
            "",
            "| Check | Status | Errors |",
            "| --- | --- | --- |",
            "| manifest | `{}` | {} |".format(
                _md_cell(report.get("source_manifest_match")),
                _md_cell(json.dumps(report.get("source_manifest_errors") or [], ensure_ascii=False)),
            ),
            "| minimal slice | `{}` | {} |".format(
                _md_cell(report.get("source_slice_match")),
                _md_cell(json.dumps(report.get("source_slice_errors") or [], ensure_ascii=False)),
            ),
        ]
    )
    lines.extend(
        [
            "",
            "## Invalid Rows",
            "",
            "| Row | Concept | Decision | Errors |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for row in report.get("invalid_decision_rows") or []:
        lines.append(
            "| {row_number} | `{concept_id}` | `{decision}` | {errors} |".format(
                row_number=int(row.get("row_number") or 0),
                concept_id=_md_cell(row.get("concept_id")),
                decision=_md_cell(row.get("decision")),
                errors=_md_cell(", ".join(row.get("errors") or [])),
            )
        )
    if not report.get("invalid_decision_rows"):
        lines.append("| 0 | none |  |  |")
    lines.extend(
        [
            "",
            "## Completed Rows",
            "",
            "| Row | Concept | Decision |",
            "| ---: | --- | --- |",
        ]
    )
    for row in report.get("completed_rows") or []:
        lines.append(
            "| {row_number} | `{concept_id}` | `{decision}` |".format(
                row_number=int(row.get("row_number") or 0),
                concept_id=_md_cell(row.get("concept_id")),
                decision=_md_cell(row.get("decision")),
            )
        )
    if not report.get("completed_rows"):
        lines.append("| 0 | none |  |")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_ksa_term_minimal_review_decision_action_plan(
    csv_path: Path,
    *,
    source_manifest_path: Path,
    source_slice_path: Path,
) -> dict[str, Any]:
    audit = audit_ksa_term_minimal_review_decision_csv(
        csv_path,
        source_manifest_path=source_manifest_path,
        source_slice_path=source_slice_path,
    )
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [dict(row) for row in csv.DictReader(handle)]

    source_slice_errors: list[dict[str, Any]] = []
    source_groups_by_id: dict[str, dict[str, Any]] = {}
    try:
        source_slice = json.loads(source_slice_path.read_text(encoding="utf-8"))
    except OSError as exc:
        source_slice_errors.append({"type": "source_slice_read_error", "message": str(exc)})
        source_slice = {}
    except json.JSONDecodeError as exc:
        source_slice_errors.append({"type": "source_slice_json_error", "message": str(exc)})
        source_slice = {}
    if isinstance(source_slice, dict):
        for group in source_slice.get("concept_review_groups") or []:
            if not isinstance(group, dict):
                continue
            concept_id = str(group.get("concept_id") or "").strip()
            if concept_id:
                source_groups_by_id[concept_id] = group
    else:
        source_slice_errors.append({"type": "source_slice_not_object"})

    action_items: list[dict[str, Any]] = []
    skipped_rows: list[dict[str, Any]] = []
    if audit.get("ok") and not source_slice_errors:
        for index, row in enumerate(rows, start=1):
            decision = str(row.get("decision") or "").strip()
            concept_id = str(row.get("concept_id") or "").strip()
            if not decision:
                skipped_rows.append(
                    {
                        "row_number": index,
                        "concept_id": concept_id,
                        "reason": "pending_human_decision",
                    }
                )
                continue
            action = KSA_TERM_MINIMAL_REVIEW_DECISION_ACTIONS.get(decision)
            if not action:
                skipped_rows.append(
                    {
                        "row_number": index,
                        "concept_id": concept_id,
                        "reason": "unsupported_decision",
                    }
                )
                continue
            source_group = source_groups_by_id.get(concept_id) or {}
            source_suggestion = (
                source_group.get("suggested_decision")
                if isinstance(source_group.get("suggested_decision"), dict)
                else _suggest_ksa_term_minimal_review_decision(source_group)
            )
            suggested_decision = source_suggestion.get("suggested_decision") or ""
            suggested_confidence = source_suggestion.get("suggested_decision_confidence") or ""
            suggested_policy = (
                source_suggestion.get("suggested_decision_policy")
                or "review_assist_only_not_a_human_decision"
            )
            action_items.append(
                {
                    "schema": "ncs_ksa_term_minimal_review_decision_action_item_v1",
                    "row_number": index,
                    "concept_id": concept_id,
                    "concept_name": row.get("concept_name"),
                    "concept_type": row.get("concept_type"),
                    "decision": decision,
                    "reviewer_id": row.get("reviewer_id"),
                    "reviewed_at": row.get("reviewed_at"),
                    "rationale": row.get("rationale"),
                    "proposed_concept_action": row.get("proposed_concept_action") or "",
                    "suggested_decision": suggested_decision,
                    "suggested_decision_confidence": suggested_confidence,
                    "suggested_decision_policy": suggested_policy,
                    "operator_action": action["operator_action"],
                    "automation_policy": action["automation_policy"],
                    "scoring_policy": action["scoring_policy"],
                    "status_update_allowed": False,
                    "db_writes": False,
                    "approval_claim": False,
                    "trusted_status_write_allowed": False,
                    "raw_source_mutation_allowed": False,
                    "source_context": {
                        "preferred_decision_unit": "concept_review_group",
                        "item_count": int(source_group.get("item_count") or row.get("item_count") or 0),
                        "max_priority_score": int(
                            source_group.get("max_priority_score") or row.get("max_priority_score") or 0
                        ),
                        "priority_levels": list(source_group.get("priority_levels") or []),
                        "issue_counts": dict(source_group.get("issue_counts") or {}),
                        "term_variants": list(source_group.get("term_variants") or [])[:12],
                        "normalized_terms": list(source_group.get("normalized_terms") or [])[:12],
                        "task_relation_count": int(
                            source_group.get("task_relation_count")
                            or row.get("task_relation_count")
                            or 0
                        ),
                        "training_course_link_count": int(
                            source_group.get("training_course_link_count")
                            or row.get("training_course_link_count")
                            or 0
                        ),
                        "training_goal_link_count": int(
                            source_group.get("training_goal_link_count")
                            or row.get("training_goal_link_count")
                            or 0
                        ),
                    },
                }
            )

    ok = bool(audit.get("ok")) and not source_slice_errors
    return {
        "ok": ok,
        "schema": KSA_TERM_MINIMAL_REVIEW_DECISION_ACTION_PLAN_SCHEMA,
        "generated_at": now_utc(),
        "csv_path": str(csv_path),
        "source_manifest_path": str(source_manifest_path),
        "source_slice_path": str(source_slice_path),
        "audit_ok": bool(audit.get("ok")),
        "audit_summary": {
            "schema": audit.get("schema"),
            "row_count": audit.get("row_count"),
            "completed_decision_count": audit.get("completed_decision_count"),
            "pending_decision_count": audit.get("pending_decision_count"),
            "invalid_decision_count": audit.get("invalid_decision_count"),
            "source_manifest_match": audit.get("source_manifest_match"),
            "source_slice_match": audit.get("source_slice_match"),
            "source_payload_exposed": audit.get("source_payload_exposed"),
        },
        "audit_errors": {
            "missing_required_fields": audit.get("missing_required_fields") or [],
            "invalid_rows": audit.get("invalid_rows") or [],
            "forbidden_status_rows": audit.get("forbidden_status_rows") or [],
            "source_manifest_errors": audit.get("source_manifest_errors") or [],
            "source_slice_errors": (audit.get("source_slice_errors") or []) + source_slice_errors,
        },
        "action_count": len(action_items),
        "pending_decision_count": int(audit.get("pending_decision_count") or 0),
        "skipped_row_count": len(skipped_rows),
        "actions": action_items,
        "skipped_rows": skipped_rows[:100],
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "raw_source_mutation_allowed": False,
        "trusted_status_write_allowed": False,
        "preferred_decision_unit": "concept_review_group",
        "next_action": "review_action_plan_before_any_guarded_operator_command"
        if ok and action_items
        else "await_human_decisions"
        if ok
        else "fix_decision_csv_before_action_planning",
        "notes": [
            "Read-only action plan generated from audited KSA minimal review decisions.",
            "This plan does not update ontology concepts, quality issues, recommendation scores, or trusted statuses.",
            "A separate guarded operator command would be required before any DB mutation.",
        ],
    }


def write_ksa_term_minimal_review_decision_action_plan_markdown(
    report: dict[str, Any],
    out_path: Path,
) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    lines = [
        "# KSA Term Minimal Review Action Plan",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- csv_path: `{_md_cell(report.get('csv_path'))}`",
        f"- source_manifest_path: `{_md_cell(report.get('source_manifest_path'))}`",
        f"- source_slice_path: `{_md_cell(report.get('source_slice_path'))}`",
        f"- action_count: `{report.get('action_count')}`",
        f"- pending_decision_count: `{report.get('pending_decision_count')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- next_action: `{_md_cell(report.get('next_action'))}`",
        "",
        "## Audit Summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    for key, value in (report.get("audit_summary") or {}).items():
        lines.append(f"| `{_md_cell(key)}` | `{_md_cell(value)}` |")
    lines.extend(
        [
            "",
            "## Actions",
            "",
            "| Row | Concept | Decision | Suggested | Operator Action | Scoring Policy |",
            "| ---: | --- | --- | --- | --- | --- |",
        ]
    )
    for item in report.get("actions") or []:
        lines.append(
            "| {row} | `{concept}` | `{decision}` | `{suggested}` | {action} | {policy} |".format(
                row=int(item.get("row_number") or 0),
                concept=_md_cell(item.get("concept_id")),
                decision=_md_cell(item.get("decision")),
                suggested=_md_cell(item.get("suggested_decision")),
                action=_md_cell(item.get("operator_action")),
                policy=_md_cell(item.get("scoring_policy")),
            )
        )
    if not report.get("actions"):
        lines.append("| 0 | none |  |  |  |  |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- raw_source_mutation_allowed: `False`",
            "- trusted_status_write_allowed: `False`",
            "- This action plan is not a DB write instruction.",
        ]
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_ksa_term_review_readiness_report(
    *,
    workflow_manifest_path: Path,
    decision_audit_path: Path,
    action_plan_path: Path,
) -> dict[str, Any]:
    def _read_json(path: Path, label: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            return {}, [{"gate": f"{label}_readable", "passed": False, "detail": str(exc)}]
        except json.JSONDecodeError as exc:
            return {}, [{"gate": f"{label}_valid_json", "passed": False, "detail": str(exc)}]
        if not isinstance(payload, dict):
            return {}, [{"gate": f"{label}_object", "passed": False, "detail": "artifact is not a JSON object"}]
        return payload, []

    def _flag_false(payload: dict[str, Any], key: str) -> bool:
        return payload.get(key) is False

    manifest, manifest_read_gates = _read_json(workflow_manifest_path, "workflow_manifest")
    audit, audit_read_gates = _read_json(decision_audit_path, "decision_audit")
    action_plan, action_read_gates = _read_json(action_plan_path, "action_plan")
    summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
    safety = manifest.get("safety_contract") if isinstance(manifest.get("safety_contract"), dict) else {}
    audit_summary = action_plan.get("audit_summary") if isinstance(action_plan.get("audit_summary"), dict) else {}
    artifacts = manifest.get("artifacts") if isinstance(manifest.get("artifacts"), dict) else {}

    artifact_checks = []
    for key in (
        "manifest",
        "impact_report",
        "impact_markdown",
        "minimal_review_slice",
        "minimal_review_jsonl",
        "minimal_review_csv",
        "minimal_review_markdown",
    ):
        value = artifacts.get(key)
        path = Path(str(value)) if value else None
        exists = bool(path and path.exists())
        nonempty = bool(exists and path and path.stat().st_size > 0)
        artifact_checks.append(
            {
                "artifact": key,
                "path": str(value) if value else None,
                "exists": exists,
                "nonempty": nonempty,
            }
        )

    gates: list[dict[str, Any]] = []
    gates.extend(manifest_read_gates + audit_read_gates + action_read_gates)
    gates.extend(
        [
            {
                "gate": "workflow_manifest_schema",
                "passed": manifest.get("schema") == "ncs_ksa_term_review_workflow_manifest_v1",
                "detail": manifest.get("schema"),
            },
            {
                "gate": "decision_audit_schema",
                "passed": audit.get("schema") == KSA_TERM_MINIMAL_REVIEW_DECISION_AUDIT_SCHEMA,
                "detail": audit.get("schema"),
            },
            {
                "gate": "action_plan_schema",
                "passed": action_plan.get("schema") == KSA_TERM_MINIMAL_REVIEW_DECISION_ACTION_PLAN_SCHEMA,
                "detail": action_plan.get("schema"),
            },
            {"gate": "workflow_ok", "passed": manifest.get("ok") is True, "detail": manifest.get("ok")},
            {"gate": "decision_audit_ok", "passed": audit.get("ok") is True, "detail": audit.get("ok")},
            {"gate": "action_plan_ok", "passed": action_plan.get("ok") is True, "detail": action_plan.get("ok")},
            {
                "gate": "source_artifacts_reconciled",
                "passed": audit.get("source_manifest_match") is True
                and audit.get("source_slice_match") is True
                and audit.get("source_jsonl_match") is True,
                "detail": {
                    "source_manifest_match": audit.get("source_manifest_match"),
                    "source_slice_match": audit.get("source_slice_match"),
                    "source_jsonl_match": audit.get("source_jsonl_match"),
                },
            },
            {
                "gate": "source_payload_not_exposed",
                "passed": safety.get("source_payload_exposed") is False
                and audit.get("source_payload_exposed") is False
                and audit_summary.get("source_payload_exposed") is False,
                "detail": {
                    "manifest": safety.get("source_payload_exposed"),
                    "audit": audit.get("source_payload_exposed"),
                    "action_plan_audit": audit_summary.get("source_payload_exposed"),
                },
            },
            {
                "gate": "readonly_flags",
                "passed": all(
                    _flag_false(payload, field)
                    for payload in (manifest, audit, action_plan)
                    for field in ("status_update_allowed", "db_writes", "approval_claim")
                ),
                "detail": "status_update_allowed/db_writes/approval_claim must be false in all artifacts",
            },
            {
                "gate": "trusted_status_write_forbidden",
                "passed": safety.get("trusted_status_write_allowed") is False
                and action_plan.get("trusted_status_write_allowed") is False,
                "detail": {
                    "manifest": safety.get("trusted_status_write_allowed"),
                    "action_plan": action_plan.get("trusted_status_write_allowed"),
                },
            },
            {
                "gate": "minimal_decision_unit",
                "passed": safety.get("preferred_decision_unit") == "concept_review_group"
                and action_plan.get("preferred_decision_unit") == "concept_review_group",
                "detail": {
                    "manifest": safety.get("preferred_decision_unit"),
                    "action_plan": action_plan.get("preferred_decision_unit"),
                },
            },
            {
                "gate": "suggested_decisions_are_assist_only",
                "passed": safety.get("suggested_decision_policy") == "review_assist_only_not_a_human_decision",
                "detail": safety.get("suggested_decision_policy"),
            },
            {
                "gate": "artifact_files_exist",
                "passed": all(item["exists"] and item["nonempty"] for item in artifact_checks),
                "detail": artifact_checks,
            },
        ]
    )
    passed = all(bool(gate.get("passed")) for gate in gates)
    pending_decision_count = int(audit.get("pending_decision_count") or action_plan.get("pending_decision_count") or 0)
    completed_decision_count = int(audit.get("completed_decision_count") or 0)
    action_count = int(action_plan.get("action_count") or 0)
    if not passed:
        next_step = "fix_readiness_gate_failures"
    elif action_count > 0:
        next_step = "review_action_plan_before_any_guarded_operator_command"
    elif pending_decision_count > 0:
        next_step = "fill_minimal_review_csv_decisions_for_first_review_queue"
    else:
        next_step = "no_pending_review_actions"
    return {
        "ok": passed,
        "schema": KSA_TERM_REVIEW_READINESS_SCHEMA,
        "generated_at": now_utc(),
        "workflow_manifest_path": str(workflow_manifest_path),
        "decision_audit_path": str(decision_audit_path),
        "action_plan_path": str(action_plan_path),
        "summary": {
            "concept_review_group_count": summary.get("concept_review_group_count"),
            "decision_blank_count": summary.get("decision_blank_count"),
            "pending_decision_count": pending_decision_count,
            "completed_decision_count": completed_decision_count,
            "action_count": action_count,
            "suggested_decision_counts": summary.get("suggested_decision_counts") or {},
            "suggested_decision_confidence_counts": summary.get("suggested_decision_confidence_counts") or {},
            "first_review_queue_count": len(summary.get("first_review_queue") or []),
        },
        "first_review_queue": summary.get("first_review_queue") or [],
        "artifact_checks": artifact_checks,
        "gates": gates,
        "failed_gates": [gate for gate in gates if not gate.get("passed")],
        "ready_for_minimal_human_review": bool(passed and pending_decision_count > 0),
        "ready_for_guarded_action_plan_review": bool(passed and action_count > 0),
        "next_step": next_step,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "trusted_status_write_allowed": False,
        "raw_source_mutation_allowed": False,
        "notes": [
            "Read-only readiness gate for the KSA term minimal review workflow.",
            "This report verifies artifacts and safety contracts only; it does not update review statuses or ontology data.",
        ],
    }


def write_ksa_term_review_readiness_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# KSA Term Review Readiness",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- ready_for_minimal_human_review: `{report.get('ready_for_minimal_human_review')}`",
        f"- ready_for_guarded_action_plan_review: `{report.get('ready_for_guarded_action_plan_review')}`",
        f"- next_step: `{_md_cell(report.get('next_step'))}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Summary",
        "",
        f"- concept_review_group_count: `{summary.get('concept_review_group_count')}`",
        f"- pending_decision_count: `{summary.get('pending_decision_count')}`",
        f"- completed_decision_count: `{summary.get('completed_decision_count')}`",
        f"- action_count: `{summary.get('action_count')}`",
        f"- suggested_decision_counts: `{json.dumps(summary.get('suggested_decision_counts') or {}, ensure_ascii=False)}`",
        "",
        "## Gates",
        "",
        "| Gate | Passed | Detail |",
        "| --- | --- | --- |",
    ]
    for gate in report.get("gates") or []:
        lines.append(
            "| `{gate}` | `{passed}` | {detail} |".format(
                gate=_md_cell(gate.get("gate")),
                passed=_md_cell(gate.get("passed")),
                detail=_md_cell(json.dumps(gate.get("detail"), ensure_ascii=False))
                if isinstance(gate.get("detail"), (dict, list))
                else _md_cell(gate.get("detail")),
            )
        )
    lines.extend(
        [
            "",
            "## First Review Queue",
            "",
            "| # | Concept | Suggested | Confidence | Score | Terms |",
            "| ---: | --- | --- | --- | ---: | ---: |",
        ]
    )
    for index, item in enumerate(report.get("first_review_queue") or [], start=1):
        lines.append(
            "| {index} | `{concept_id}` {concept_name} | `{suggested}` | `{confidence}` | {score} | {terms} |".format(
                index=index,
                concept_id=_md_cell(item.get("concept_id")),
                concept_name=_md_cell(item.get("concept_name")),
                suggested=_md_cell(item.get("suggested_decision")),
                confidence=_md_cell(item.get("suggested_decision_confidence")),
                score=int(item.get("max_priority_score") or 0),
                terms=int(item.get("item_count") or 0),
            )
        )
    if not report.get("first_review_queue"):
        lines.append("| 0 | none |  |  | 0 | 0 |")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_term_minimal_review_slice_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    def _md_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return ", ".join(f"{_md_cell(key)}={_md_cell(count)}" for key, count in sorted(value.items()))

    lines = [
        "# KSA Term Minimal Review Slice",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- source_impact_report_path: `{_md_cell(report.get('source_impact_report_path'))}`",
        f"- selected_priority_levels: `{', '.join(report.get('selected_priority_levels') or [])}`",
        f"- item_count: `{report.get('item_count')}`",
        f"- candidate_item_count: `{report.get('candidate_item_count')}`",
        f"- dropped_item_count: `{report.get('dropped_item_count')}`",
        f"- concept_review_group_count: `{report.get('concept_review_group_count')}`",
        f"- represented_recommendation_penalty_concept_count: `{report.get('represented_recommendation_penalty_concept_count')}`",
        f"- item_priority_level_counts: `{_md_counts(report.get('item_priority_level_counts'))}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Concept Review Groups",
        "",
        "| # | Concept | Type | Terms | Score | Suggested | Job Base | Issues | Courses | Action |",
        "| ---: | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for index, group in enumerate(report.get("concept_review_groups") or [], start=1):
        job_base_signal = (
            group.get("job_base_auxiliary_signal")
            if isinstance(group.get("job_base_auxiliary_signal"), dict)
            else {}
        )
        suggested = group.get("suggested_decision") if isinstance(group.get("suggested_decision"), dict) else {}
        lines.append(
            "| {index} | `{concept_id}` {concept_name} | `{concept_type}` | {terms} | {score} | `{suggested}` | {job_base} | {issues} | {courses} | `{action}` |".format(
                index=index,
                concept_id=_md_cell(group.get("concept_id")),
                concept_name=_md_cell(group.get("concept_name")),
                concept_type=_md_cell(group.get("concept_type")),
                terms=int(group.get("item_count") or 0),
                score=int(group.get("max_priority_score") or 0),
                suggested=_md_cell(suggested.get("suggested_decision")),
                job_base=_md_cell(", ".join((job_base_signal.get("factor_labels") or [])[:3])),
                issues=_md_cell(_md_counts(group.get("issue_counts"))),
                courses=_md_cell(", ".join(group.get("course_names") or [])),
                action=_md_cell(group.get("operator_action")),
            )
        )
    if not report.get("concept_review_groups"):
        lines.append("| 0 | none |  | 0 | 0 |  |  |  |  |  |")
    lines.extend(
        [
            "",
        "## Items",
        "",
        "| # | KSA Text | Score | Level | Penalty Concepts | Issues | Units | Action |",
        "| ---: | --- | ---: | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for item in report.get("items") or []:
        penalty = item.get("linked_penalized_concepts") if isinstance(item.get("linked_penalized_concepts"), dict) else {}
        lines.append(
            "| {rank} | {text} | {score} | `{level}` | {penalty_concepts} | {issues} | {units} | `{action}` |".format(
                rank=int(item.get("rank") or 0),
                text=_md_cell(item.get("representative_ksa_text")),
                score=int(item.get("minimal_review_priority_score") or 0),
                level=_md_cell(item.get("minimal_review_priority_level")),
                penalty_concepts=int(penalty.get("concept_count") or 0),
                issues=int(item.get("issue_count") or 0),
                units=int(item.get("unit_count") or 0),
                action=_md_cell(item.get("minimal_review_operator_action")),
            )
        )
    for item in report.get("items") or []:
        lines.extend(
            [
                "",
                f"## {int(item.get('rank') or 0)}. {_md_cell(item.get('representative_ksa_text'))}",
                "",
                f"- item_id: `{_md_cell(item.get('item_id'))}`",
                f"- normalized_ksa_term: `{_md_cell(item.get('normalized_ksa_term'))}`",
                f"- review_bucket: `{_md_cell(item.get('review_bucket'))}`",
                f"- review_pack_source: `{_md_cell(item.get('review_pack_source'))}`",
                f"- source_penalty_concept_ids: `{', '.join(str(value) for value in item.get('source_penalty_concept_ids') or [])}`",
                f"- minimal_review_priority_reasons: `{', '.join(item.get('minimal_review_priority_reasons') or [])}`",
                f"- job_base_auxiliary_signal: `{', '.join((item.get('job_base_auxiliary_signal') or {}).get('factor_labels') or [])}`",
                f"- linked_penalty_issues: `{_md_counts((item.get('linked_penalized_concepts') or {}).get('issue_counts'))}`",
                f"- linked_penalty_courses: `{', '.join((item.get('linked_penalized_concepts') or {}).get('course_names') or [])}`",
                f"- decision_fields_blank: `{all(not value for value in (item.get('decision_fields') or {}).values())}`",
                f"- status_update_allowed: `{item.get('status_update_allowed')}`",
                "",
                "Penalized concepts:",
            ]
        )
        for concept in item.get("top_penalized_concepts") or []:
            lines.append(
                "- `{concept_id}` {concept_name} ({concept_type}), rows={rows}, issues={issues}, courses={courses}".format(
                    concept_id=_md_cell(concept.get("concept_id")),
                    concept_name=_md_cell(concept.get("concept_name")),
                    concept_type=_md_cell(concept.get("concept_type")),
                    rows=_md_cell(concept.get("linked_penalty_rows")),
                    issues=_md_counts(concept.get("linked_penalty_issues")),
                    courses=", ".join(concept.get("linked_penalty_courses") or []),
                )
            )
        if not item.get("top_penalized_concepts"):
            lines.append("- none")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_term_ontology_impact_report_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    def _md_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return ", ".join(f"{_md_cell(key)}={_md_cell(count)}" for key, count in sorted(value.items()))

    lines = [
        "# KSA Term Ontology Impact Report",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- issue_types: `{', '.join(report.get('issue_types') or [])}`",
        f"- group_count: `{report.get('group_count')}`",
        f"- candidate_group_count: `{report.get('candidate_group_count')}`",
        f"- dropped_group_count: `{report.get('dropped_group_count')}`",
        f"- group_limit_policy: `{_md_cell(report.get('group_limit_policy'))}`",
        f"- impacted_group_count: `{report.get('impacted_group_count')}`",
        f"- job_base_auxiliary_group_count: `{report.get('job_base_auxiliary_group_count')}`",
        f"- job_base_auxiliary_concept_count: `{report.get('job_base_auxiliary_concept_count')}`",
        f"- job_base_auxiliary_signal_role: `{_md_cell(report.get('job_base_auxiliary_signal_role'))}`",
        f"- represented_issue_count: `{report.get('represented_issue_count')}`",
        f"- represented_issue_reduction: `{report.get('represented_issue_reduction')}`",
        f"- total_unique_impacted_concept_count: `{report.get('total_unique_impacted_concept_count')}`",
        f"- transition_quality_report_available: `{report.get('transition_quality_report_available')}`",
        f"- transition_quality_report_path: `{_md_cell(report.get('transition_quality_report_path'))}`",
        f"- transition_penalty_candidate_group_count: `{report.get('transition_penalty_candidate_group_count')}`",
        f"- transition_penalty_supplemental_candidate_group_count: `{report.get('transition_penalty_supplemental_candidate_group_count')}`",
        f"- transition_penalty_supplemental_group_count: `{report.get('transition_penalty_supplemental_group_count')}`",
        f"- linked_penalty_group_count: `{report.get('recommendation_penalty_group_count')}`",
        f"- source_transition_penalty_concept_count: `{report.get('source_transition_penalty_concept_count')}`",
        f"- represented_recommendation_penalty_concept_count: `{report.get('represented_recommendation_penalty_concept_count')}`",
        f"- unrepresented_recommendation_penalty_concept_count: `{report.get('unrepresented_recommendation_penalty_concept_count')}`",
        f"- source_transition_penalized_recommendation_row_count: `{report.get('source_transition_penalized_recommendation_row_count')}`",
        f"- source_transition_distinct_penalized_course_count: `{report.get('source_transition_distinct_penalized_course_count')}`",
        f"- source_transition_penalty_issue_counts: `{_md_counts(report.get('source_transition_penalty_issue_counts'))}`",
        f"- total_task_relation_count: `{report.get('total_task_relation_count')}`",
        f"- total_criteria_link_count: `{report.get('total_criteria_link_count')}`",
        f"- total_training_course_link_count: `{report.get('total_training_course_link_count')}`",
        f"- total_training_goal_link_count: `{report.get('total_training_goal_link_count')}`",
        f"- review_bucket_counts: `{_md_counts(report.get('review_bucket_counts'))}`",
        f"- minimal_review_priority_level_counts: `{_md_counts(report.get('minimal_review_priority_level_counts'))}`",
        f"- max_minimal_review_priority_score: `{report.get('max_minimal_review_priority_score')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Groups",
        "",
        "| # | KSA Text | Priority | Source | Bucket | Concepts | Job Base | Linked Penalty Concepts | Issues | Units | Majors | Action |",
        "| ---: | --- | ---: | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for index, group in enumerate(report.get("groups") or [], start=1):
        penalty = (
            group.get("linked_penalized_concepts")
            if isinstance(group.get("linked_penalized_concepts"), dict)
            else group.get("recommendation_penalty")
            if isinstance(group.get("recommendation_penalty"), dict)
            else {}
        )
        job_base_signal = (
            group.get("job_base_auxiliary_signal")
            if isinstance(group.get("job_base_auxiliary_signal"), dict)
            else {}
        )
        lines.append(
            "| {index} | {text} | {priority} | `{source}` | `{bucket}` | {concepts} | {job_base} | {penalty_concepts} | {issues} | {units} | {majors} | `{action}` |".format(
                index=index,
                text=_md_cell(group.get("representative_ksa_text")),
                priority=int(group.get("minimal_review_priority_score") or 0),
                source=_md_cell(group.get("review_pack_source")),
                bucket=_md_cell(group.get("review_bucket")),
                concepts=int(group.get("linked_concept_count") or 0),
                job_base=_md_cell(", ".join((job_base_signal.get("factor_labels") or [])[:3])),
                penalty_concepts=int(penalty.get("concept_count") or 0),
                issues=int(group.get("issue_count") or 0),
                units=int(group.get("unit_count") or 0),
                majors=int(group.get("major_count") or 0),
                action=_md_cell(group.get("operator_impact_action")),
            )
        )
    for index, group in enumerate(report.get("groups") or [], start=1):
        lines.extend(
            [
                "",
                f"## {index}. {_md_cell(group.get('representative_ksa_text'))}",
                "",
                f"- normalized_ksa_term: `{_md_cell(group.get('normalized_ksa_term'))}`",
                f"- review_bucket: `{_md_cell(group.get('review_bucket'))}`",
                f"- review_pack_source: `{_md_cell(group.get('review_pack_source'))}`",
                f"- included_by_transition_penalty: `{bool(group.get('included_by_transition_penalty'))}`",
                f"- source_penalty_concept_ids: `{', '.join(str(item) for item in group.get('source_penalty_concept_ids') or [])}`",
                f"- operator_impact_action: `{_md_cell(group.get('operator_impact_action'))}`",
                f"- minimal_review_priority_score: `{int(group.get('minimal_review_priority_score') or 0)}`",
                f"- minimal_review_priority_level: `{_md_cell(group.get('minimal_review_priority_level'))}`",
                f"- minimal_review_priority_reasons: `{', '.join(group.get('minimal_review_priority_reasons') or [])}`",
                f"- minimal_review_operator_action: `{_md_cell(group.get('minimal_review_operator_action'))}`",
                f"- minimal_review_scope_note: `{_md_cell(group.get('minimal_review_scope_note'))}`",
                f"- linked_concept_count: `{int(group.get('linked_concept_count') or 0)}`",
                f"- group_task_relation_count: `{int(group.get('group_task_relation_count') or 0)}`",
                f"- group_training_course_link_count: `{int(group.get('group_training_course_link_count') or 0)}`",
                f"- group_training_goal_link_count: `{int(group.get('group_training_goal_link_count') or 0)}`",
                f"- group_training_link_count: `{int(group.get('group_training_link_count') or 0)}`",
                f"- job_base_auxiliary_signal: `{', '.join((group.get('job_base_auxiliary_signal') or {}).get('factor_labels') or [])}`",
                f"- linked_penalized_concepts: `{_md_counts((group.get('linked_penalized_concepts') or {}).get('issue_counts'))}`",
                f"- linked_penalty_courses: `{', '.join((group.get('linked_penalized_concepts') or {}).get('course_names') or [])}`",
                f"- linked_penalty_scope_note: `{_md_cell((group.get('linked_penalized_concepts') or {}).get('scope_note'))}`",
                f"- auto_apply_allowed: `{bool(group.get('auto_apply_allowed'))}`",
                f"- status_update_allowed: `{bool(group.get('status_update_allowed'))}`",
                "",
                "Top concepts:",
            ]
        )
        for concept in group.get("top_concepts") or []:
            job_base_signal = (
                concept.get("job_base_auxiliary_signal")
                if isinstance(concept.get("job_base_auxiliary_signal"), dict)
                else {}
            )
            lines.append(
                "- `{concept_id}` {concept_name} ({concept_type}) "
                "linked_ksa={linked_ksa_count}, task_relations={task_relation_count}, "
                "criteria_links={criteria_link_count}, training_courses={training_course_link_count}, "
                "training_goals={training_goal_link_count}, linked_penalty_rows={recommendation_penalty_course_count}, "
                "linked_penalty_issues={recommendation_penalty_issue_counts}, job_base={job_base}, "
                "review_status={review_status}".format(
                    concept_id=_md_cell(concept.get("concept_id")),
                    concept_name=_md_cell(concept.get("concept_name")),
                    concept_type=_md_cell(concept.get("concept_type")),
                    linked_ksa_count=int(concept.get("linked_ksa_count") or 0),
                    task_relation_count=int(concept.get("task_relation_count") or 0),
                    criteria_link_count=int(concept.get("criteria_link_count") or 0),
                    training_course_link_count=int(concept.get("training_course_link_count") or 0),
                    training_goal_link_count=int(concept.get("training_goal_link_count") or 0),
                    recommendation_penalty_course_count=int(concept.get("recommendation_penalty_course_count") or 0),
                    recommendation_penalty_issue_counts=_md_counts(concept.get("recommendation_penalty_issue_counts")),
                    job_base=_md_cell(", ".join(job_base_signal.get("factor_labels") or [])),
                    review_status=_md_cell(concept.get("review_status")),
                )
            )
        if not group.get("top_concepts"):
            lines.append("- none")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_term_preprocessing_review_pack_markdown(report: dict[str, Any], out_path: Path) -> None:
    def _md_cell(value: Any) -> str:
        return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")

    def _md_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return ", ".join(f"{_md_cell(key)}={_md_cell(count)}" for key, count in sorted(value.items()))

    lines = [
        "# KSA Term Preprocessing Review Pack",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- issue_types: `{', '.join(report.get('issue_types') or [])}`",
        f"- group_count: `{report.get('group_count')}`",
        f"- total_open_issue_count: `{report.get('total_open_issue_count')}`",
        f"- represented_issue_count: `{report.get('represented_issue_count')}`",
        f"- represented_ksa_count: `{report.get('represented_ksa_count')}`",
        f"- represented_issue_reduction: `{report.get('represented_issue_reduction')}`",
        f"- review_bucket_counts: `{_md_counts(report.get('review_bucket_counts'))}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Open Issue Counts",
        "",
    ]
    for row in report.get("open_issue_counts") or []:
        lines.append(f"- `{row.get('issue_type')}` / `{row.get('severity')}`: {row.get('count')}")
    lines.extend(
        [
            "",
            "## Groups",
            "",
            "| # | KSA Text | Bucket | Issue Counts | KSA Type Counts | Issues | Units | Majors | Action |",
            "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for index, row in enumerate(report.get("groups") or [], start=1):
        lines.append(
            "| {index} | {text} | `{bucket}` | {issue_counts} | {ksa_type_counts} | {issue_count} | {unit_count} | {major_count} | `{action}` |".format(
                index=index,
                text=_md_cell(row.get("representative_ksa_text")),
                bucket=_md_cell(row.get("review_bucket")),
                issue_counts=_md_cell(_md_counts(row.get("issue_type_counts"))),
                ksa_type_counts=_md_cell(_md_counts(row.get("ksa_type_counts"))),
                issue_count=int(row.get("issue_count") or 0),
                unit_count=int(row.get("unit_count") or 0),
                major_count=int(row.get("major_count") or 0),
                action=_md_cell(row.get("recommended_review_action")),
            )
        )
    for index, row in enumerate(report.get("groups") or [], start=1):
        lines.extend(
            [
                "",
                f"## {index}. {_md_cell(row.get('representative_ksa_text'))}",
                "",
                f"- normalized_ksa_term: `{_md_cell(row.get('normalized_ksa_term') or row.get('normalized_ksa_text'))}`",
                f"- issue_type_counts: `{_md_counts(row.get('issue_type_counts'))}`",
                f"- ksa_type_counts: `{_md_counts(row.get('ksa_type_counts'))}`",
                f"- review_status_counts: `{_md_counts(row.get('review_status_counts'))}`",
                f"- raw_ksa_text_variant_count: `{int(row.get('raw_ksa_text_variant_count') or 0)}`",
                f"- review_bucket: `{_md_cell(row.get('review_bucket'))}`",
                f"- review_flags: `{', '.join(row.get('review_flags') or [])}`",
                f"- recommended_review_action: `{_md_cell(row.get('recommended_review_action'))}`",
                f"- minimal_review_rationale: {_md_cell(row.get('minimal_review_rationale'))}",
                f"- operator_decision_options: `{', '.join(row.get('operator_decision_options') or [])}`",
                f"- auto_apply_allowed: `{bool(row.get('auto_apply_allowed'))}`",
                f"- status_update_allowed: `{bool(row.get('status_update_allowed'))}`",
                f"- db_writes: `{bool(row.get('db_writes'))}`",
                f"- approval_claim: `{bool(row.get('approval_claim'))}`",
                "",
                "Raw variants:",
            ]
        )
        for variant in row.get("raw_ksa_text_variants") or []:
            lines.append(f"- {_md_cell(variant)}")
        if not row.get("raw_ksa_text_variants"):
            lines.append("- none")
        lines.extend(
            [
                "",
                "Samples:",
            ]
        )
        for sample in row.get("samples") or []:
            lines.append(
                "- `{issue_type}` `{unit_code}` {unit_name} / {element_name} / KSA `{ksa_id}`: {ksa_text}".format(
                    issue_type=_md_cell(sample.get("issue_type")),
                    unit_code=_md_cell(sample.get("unit_code")),
                    unit_name=_md_cell(sample.get("unit_name_raw")),
                    element_name=_md_cell(sample.get("element_name_raw")),
                    ksa_id=_md_cell(sample.get("ksa_id")),
                    ksa_text=_md_cell(sample.get("ksa_text_raw")),
                )
            )
        if not row.get("samples"):
            lines.append("- none")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def review_priority_summary(
    conn: sqlite3.Connection,
    *,
    limit: int = 20,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    selected_issue_types = issue_types or DEFAULT_REVIEW_PRIORITY_ISSUE_TYPES
    max_items = max(1, min(int(limit or 20), MAX_REVIEW_PRIORITY_ITEMS))
    max_per_type = max(1, min(int(per_issue_type_limit or 5), MAX_REVIEW_PRIORITY_PER_ISSUE_TYPE))
    placeholders = ",".join("?" for _ in selected_issue_types)
    rows = [
        dict(row)
        for row in conn.execute(
            f"""
            SELECT issue_id, target_type, target_id, issue_type, severity,
                   issue_detail, suggested_action, detected_at
            FROM quality_issues
            WHERE resolved_at IS NULL
              AND issue_type IN ({placeholders})
            """,
            selected_issue_types,
        ).fetchall()
    ]
    items: list[dict[str, Any]] = []
    for issue in rows:
        context = _context_for_issue(conn, issue)
        if issue["target_type"] == "element":
            issue = normalize_api_element_issue(issue, api_match_status=context.get("api_match_status"))
        if issue.get("suggested_action") is not None:
            issue = dict(issue)
            issue["suggested_action"] = neutralize_suggested_action(
                issue.get("suggested_action"),
                issue_type=issue.get("issue_type"),
                target_type=issue.get("target_type"),
            )
        if context.get("suggested_action") is not None:
            context = dict(context)
            context["suggested_action"] = neutralize_suggested_action(
                context.get("suggested_action"),
                issue_type=issue.get("issue_type"),
                target_type=context.get("target_type") or issue.get("target_type"),
            )
        priority_score = _issue_priority_score(issue)
        items.append(
            {
                "priority_score": priority_score,
                "priority_reason": _priority_reason(issue),
                "issue": _trim_payload(issue),
                "context": _trim_payload(context),
            }
        )
    items.sort(
        key=lambda item: (
            -item["priority_score"],
            item["issue"]["issue_type"],
            item["issue"]["issue_id"],
        )
    )
    groups: dict[str, list[dict[str, Any]]] = {}
    capped_items: list[dict[str, Any]] = []
    seen_target_signatures: set[tuple[str, str, str]] = set()
    duplicate_target_items_skipped = 0
    for item in items:
        issue_type = item["issue"]["issue_type"]
        target_signature = (
            str(issue_type),
            str(item["issue"].get("target_type") or ""),
            str(item["issue"].get("target_id") or ""),
        )
        if target_signature in seen_target_signatures:
            duplicate_target_items_skipped += 1
            continue
        bucket = groups.setdefault(issue_type, [])
        if len(bucket) < max_per_type:
            bucket.append(item)
            capped_items.append(item)
            seen_target_signatures.add(target_signature)
    capped_items.sort(
        key=lambda item: (
            -item["priority_score"],
            item["issue"]["issue_type"],
            item["issue"]["issue_id"],
        )
    )

    return {
        "ok": True,
        "schema": REVIEW_PRIORITY_SCHEMA,
        "issue_types": selected_issue_types,
        "open_issue_counts": _open_issue_counts(conn, selected_issue_types),
        "top_items": capped_items[:max_items],
        "groups": groups,
        "focus_overlays": _focus_overlays(items),
        "duplicate_target_items_skipped": duplicate_target_items_skipped,
        "next_actions": [
            "Review training-goal links before broad criteria cleanup because they affect ranking evidence directly.",
            "Route any trusted status update through the controlled human-review workflow; preserve raw source text.",
            "Use prepare-*-review-queue --dry-run before changing queue caps.",
        ],
    }


def review_priority_summary_from_db(
    db_path: Path,
    *,
    limit: int = 20,
    per_issue_type_limit: int = 5,
    issue_types: list[str] | None = None,
) -> dict[str, Any]:
    conn = _connect_readonly(db_path)
    try:
        return review_priority_summary(
            conn,
            limit=limit,
            per_issue_type_limit=per_issue_type_limit,
            issue_types=issue_types,
        )
    finally:
        conn.close()


def write_ksa_definition_candidate_family_report_markdown(
    report: dict[str, Any],
    out_path: Path,
) -> None:
    def md(value: Any) -> str:
        text = str(value if value is not None else "")
        return text.replace("|", "\\|").replace("\n", " ")

    safety = report.get("safety") if isinstance(report.get("safety"), dict) else {}
    lines = [
        "# KSA Definition Candidate Family Report",
        "",
        "This report groups LLM/rule-generated term definition candidates by definition family.",
        "It is read-only and does not approve, promote, or write any review status.",
        "",
        "## Summary",
        "",
        f"- ok: `{report.get('ok')}`",
        f"- schema: `{report.get('schema')}`",
        f"- candidate_count: `{report.get('candidate_count')}`",
        f"- definition_family_count: `{report.get('definition_family_count')}`",
        f"- estimated_review_unit_count: `{report.get('estimated_review_unit_count')}`",
        f"- row_to_family_reduction_percent: `{report.get('row_to_family_reduction_percent')}`",
        f"- row_to_estimated_review_unit_reduction_percent: `{report.get('row_to_estimated_review_unit_reduction_percent')}`",
        f"- status_update_allowed: `{safety.get('status_update_allowed')}`",
        f"- db_writes: `{safety.get('db_writes')}`",
        f"- approval_claim: `{safety.get('approval_claim')}`",
        "",
        "## Review Guidance",
        "",
    ]
    for item in report.get("operator_guidance") or []:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "## Status Counts",
            "",
            "| status | count |",
            "|---|---:|",
        ]
    )
    for status, count in sorted((report.get("review_status_counts") or {}).items()):
        lines.append(f"| {md(status)} | {count} |")
    lines.extend(
        [
            "",
            "## Risk Flags",
            "",
            "| flag | count |",
            "|---|---:|",
        ]
    )
    risk_counts = report.get("risk_flag_counts") or {}
    if risk_counts:
        for flag, count in sorted(risk_counts.items(), key=lambda item: (-int(item[1]), str(item[0]))):
            lines.append(f"| {md(flag)} | {count} |")
    else:
        lines.append("| none | 0 |")
    lines.extend(
        [
            "",
            "## Top Families",
            "",
            "| family | type | candidates | risk | review level | sample |",
            "|---|---|---:|---:|---|---|",
        ]
    )
    for family in report.get("top_families") or []:
        samples = family.get("samples") or []
        sample_text = samples[0].get("meaning_text") if samples else ""
        lines.append(
            "| "
            f"{md(family.get('family_label') or family.get('family_key'))} | "
            f"{md(family.get('concept_type'))} | "
            f"{family.get('candidate_count')} | "
            f"{family.get('risk_count')} | "
            f"{md(family.get('recommended_review_level'))} | "
            f"{md(sample_text)} |"
        )
    lines.extend(["", "## Risk Samples", ""])
    risk_samples = report.get("risk_samples") or []
    if not risk_samples:
        lines.append("- No risk samples detected.")
    for sample in risk_samples[:20]:
        lines.extend(
            [
                f"### meaning_id {sample.get('meaning_id')}",
                "",
                f"- concept: `{md(sample.get('concept_name'))}` #{sample.get('concept_id')}",
                f"- type: `{sample.get('concept_type')}`",
                f"- status: `{sample.get('review_status')}`",
                f"- risk_flags: `{', '.join(sample.get('risk_flags') or [])}`",
                f"- meaning: {md(sample.get('meaning_text'))}",
                "",
            ]
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def write_ksa_definition_candidate_family_report_csv(
    report: dict[str, Any],
    out_path: Path,
) -> dict[str, Any]:
    def _csv_counts(value: Any) -> str:
        if not isinstance(value, dict) or not value:
            return ""
        return "; ".join(f"{key}={count}" for key, count in sorted(value.items()))

    def _sample_text(samples: Any) -> str:
        if not isinstance(samples, list) or not samples:
            return ""
        sample = samples[0] if isinstance(samples[0], dict) else {}
        return str(sample.get("meaning_text") or "")

    def _major_codes(codes: Any) -> str:
        if not isinstance(codes, list):
            return ""
        parts = []
        for item in codes:
            if isinstance(item, dict) and item.get("major_code"):
                parts.append(f"{item.get('major_code')}={item.get('count')}")
        return "; ".join(parts)

    fieldnames = [
        "schema",
        "family_key",
        "family_label",
        "concept_type",
        "candidate_count",
        "candidate_percent",
        "risk_count",
        "risk_percent",
        "review_status_counts",
        "risk_flag_counts",
        "top_major_codes",
        "recommended_review_level",
        "operator_review_scope",
        "sample_meaning_text",
        "risk_sample_meaning_text",
        "decision",
        "reviewer_id",
        "reviewed_at",
        "rationale",
        "status_update_allowed",
        "db_writes",
        "approval_claim",
    ]
    rows: list[dict[str, Any]] = []
    for family in report.get("top_families") or []:
        rows.append(
            {
                "schema": "ncs_ksa_definition_candidate_family_decision_sheet_v1",
                "family_key": family.get("family_key") or "",
                "family_label": family.get("family_label") or "",
                "concept_type": family.get("concept_type") or "",
                "candidate_count": int(family.get("candidate_count") or 0),
                "candidate_percent": family.get("candidate_percent") or 0.0,
                "risk_count": int(family.get("risk_count") or 0),
                "risk_percent": family.get("risk_percent") or 0.0,
                "review_status_counts": _csv_counts(family.get("review_status_counts")),
                "risk_flag_counts": _csv_counts(family.get("risk_flag_counts")),
                "top_major_codes": _major_codes(family.get("top_major_codes")),
                "recommended_review_level": family.get("recommended_review_level") or "",
                "operator_review_scope": "family_sample_plus_risk_samples",
                "sample_meaning_text": _sample_text(family.get("samples")),
                "risk_sample_meaning_text": _sample_text(family.get("risk_samples")),
                "decision": "",
                "reviewer_id": "",
                "reviewed_at": "",
                "rationale": "",
                "status_update_allowed": False,
                "db_writes": False,
                "approval_claim": False,
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(_csv_row(row, fieldnames) for row in rows)
    return {
        "path": str(out_path),
        "record_count": len(rows),
        "schema": "ncs_ksa_definition_candidate_family_decision_sheet_v1",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def write_review_priority_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# NCS Review Priority",
        "",
        f"- ok: {report.get('ok')}",
        f"- schema: {report.get('schema')}",
        f"- issue_types: {', '.join(report.get('issue_types') or [])}",
        f"- duplicate_target_items_skipped: {report.get('duplicate_target_items_skipped', 0)}",
        "",
        "## Open Issue Counts",
        "",
    ]
    for row in report.get("open_issue_counts", []):
        lines.append(
            f"- {row.get('issue_type')} / {row.get('severity')}: {row.get('count')}"
        )
    lines.extend(["", "## Top Items", ""])
    for item in report.get("top_items", []):
        issue = item.get("issue") or {}
        context = item.get("context") or {}
        lines.extend(
            [
                f"### {issue.get('issue_type')} #{issue.get('issue_id')}",
                "",
                f"- priority_score: {item.get('priority_score')}",
                f"- reason: {item.get('priority_reason')}",
                f"- target: {issue.get('target_type')}:{issue.get('target_id')}",
                f"- detail: {issue.get('issue_detail')}",
                f"- suggested_action: {issue.get('suggested_action')}",
            ]
        )
        if issue.get("_truncated_fields"):
            lines.append(f"- truncated_issue_fields: {', '.join(issue['_truncated_fields'])}")
        label_parts = [
            context.get("compe_unit_name"),
            context.get("unit_name_raw"),
            context.get("unit_name"),
            context.get("concept_name"),
            context.get("criteria_text_raw"),
        ]
        label = next((str(part) for part in label_parts if part), None)
        if label:
            lines.append(f"- context: {label}")
        if context.get("_truncated_fields"):
            lines.append(f"- truncated_context_fields: {', '.join(context['_truncated_fields'])}")
        lines.append("")
    lines.extend(["", "## Focus Overlays", ""])
    for overlay in report.get("focus_overlays", []):
        lines.extend(
            [
                f"### {overlay.get('label') or overlay.get('code')}",
                "",
                f"- code: {overlay.get('code')}",
                f"- major_code: {overlay.get('major_code')}",
                f"- item_count: {overlay.get('item_count')}",
                f"- reason: {overlay.get('reason')}",
                "",
            ]
        )
        for item in overlay.get("top_items", [])[:10]:
            issue = item.get("issue") or {}
            context = item.get("context") or {}
            label_parts = [
                context.get("compe_unit_name"),
                context.get("unit_name_raw"),
                context.get("unit_name"),
                context.get("concept_name"),
                context.get("criteria_text_raw"),
            ]
            label = next((str(part) for part in label_parts if part), "")
            lines.append(
                "- "
                f"{issue.get('issue_type')} #{issue.get('issue_id')} "
                f"target={issue.get('target_type')}:{issue.get('target_id')} "
                f"score={item.get('priority_score')} - {label}"
            )
        lines.append("")
    lines.extend(["## Next Actions", ""])
    for action in report.get("next_actions", []):
        lines.append(f"- {action}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
