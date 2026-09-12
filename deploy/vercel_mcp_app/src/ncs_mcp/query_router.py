from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from ncs_mcp.contracts import PLAN_NCS_EDUCATION_PATH_TOOL, QUERY_ROUTE_SCHEMA
from ncs_mcp.hrd_guide_reference import (
    hrd_guide_reference_metadata,
    match_hrd_guide_prompt_template,
)


@dataclass(frozen=True)
class RoutePattern:
    scenario: str
    tool: str
    reason: str
    signals: tuple[str, ...]
    required_params: tuple[str, ...]
    priority: int
    default_params: dict[str, Any] | None = None
    pipeline: tuple[str, ...] = ()


EDUCATION_SYSTEM = "education_system_design"
TRAINING_TRANSITION = "training_transition"
TASK_TRAINING = "task_training"
TASK_TRANSITION = "task_transition"
EVIDENCE_ANALYSIS = "evidence_analysis"
STRUCTURE_SEARCH = "structure_search"
OPERATOR_REVIEW = "operator_review"

ROUTE_CONTRACT_SCHEMA = QUERY_ROUTE_SCHEMA
ROUTE_FINGERPRINT_VERSION = "route-fingerprint-v1"
CONTEXT_ROUTE_FINGERPRINT_VERSION = "route-fingerprint-v2"
NCS_SEARCH_CONTEXT_SCHEMA = "ncs_search_context_v1"
NCS_SEARCH_CONTEXT_RESOLVER_VERSION = "ncs-classification-context-resolver-v1"
NCS_SEARCH_CONTEXT_TEXT_MAX_LENGTH = 500
NCS_SEARCH_JOB_SCOPE_MAX_LENGTH = 100
ROUTE_META_SAVE_FORCED_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
    PLAN_NCS_EDUCATION_PATH_TOOL,
}
ROUTE_META_COMPACT_DEFAULT_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
}


ROUTE_PATTERNS: tuple[RoutePattern, ...] = (
    RoutePattern(
        scenario=EDUCATION_SYSTEM,
        tool=PLAN_NCS_EDUCATION_PATH_TOOL,
        reason="education/training-system design intent should return a plan and fit matrix",
        signals=(
            "education system",
            "training system",
            "curriculum",
            "roadmap",
            "annual operation plan",
            "operation plan",
            "course inventory",
            "training inventory",
            "investigated courses",
            "interview questionnaire",
            "internal/external",
            "ai-hr",
            "\uad50\uc721\uccb4\uacc4",
            "\uad50\uc721\ud6c8\ub828\uccb4\uacc4",
            "\ud6c8\ub828\uccb4\uacc4",
            "\uccb4\uacc4\ub3c4",
            "\uc5f0\uac04 \uc6b4\uc601\uacc4\ud68d",
            "\uc6b4\uc601\uacc4\ud68d",
            "\uc6b4\uc601\uacc4\ud68d \ucd08\uc548",
            "\uc870\uc0ac\uacb0\uacfc",
            "\uc870\uc0ac\ub41c \uad50\uc721\uacfc\uc815",
            "\ub0b4\ubd80 \uad50\uc721",
            "\uad50\uc721\uba85",
            "\ub300\uc0c1",
            "\ubaa9\uc801",
            "\uc6b4\uc601\ubc29\uc2dd",
            "\uc9c8\ubb38\uc9c0",
            "\ub0b4\ubd80/\uc678\ubd80",
            "\uad50\uc721\uc720\ud615",
            "\uae30\ucd08\u00b7\uc2e4\ubb34\u00b7\uad00\ub9ac\uc790",
            "\ub85c\ub4dc\ub9f5",
            "\uc2dc\uc81c\ud488",
        ),
        required_params=("current_query", "target_query"),
        priority=100,
        default_params={
            "plan_objective": "NCS-based AI-HR education/training-system prototype",
            "scenario": EDUCATION_SYSTEM,
            "save": False,
            "limit": 5,
        },
        pipeline=("recommend_training_transition", "get_concept_evidence", "ncs_analysis"),
    ),
    RoutePattern(
        scenario=TRAINING_TRANSITION,
        tool="recommend_training_transition",
        reason="career/task transition intent should compare current and target NCS evidence",
        signals=(
            "transition",
            "career move",
            "gap training",
            "reskilling",
            "upskilling",
            "\uc804\ud658",
            "\uc774\ub3d9",
            "\uac2d",
            "\ubd80\uc871\uc5ed\ub7c9",
            "\ub9ac\uc2a4\ud0ac",
            "\uc5c5\uc2a4\ud0ac",
        ),
        required_params=("current_query", "target_query"),
        priority=80,
        default_params={"compact": True, "save": False, "limit": 5},
        pipeline=("recommend_task_transitions", "recommend_training_for_task"),
    ),
    RoutePattern(
        scenario=TASK_TRAINING,
        tool="recommend_training_for_task",
        reason="single task or unit training intent should rank courses against KSA evidence",
        signals=(
            "task training",
            "for this task",
            "course recommendation",
            "training course",
            "course ksa",
            "course alignment",
            "ksa",
            "\uacfc\uc5c5",
            "\uc218\ud589\uc900\uac70",
            "\ub2a5\ub825\ub2e8\uc704",
            "\ud6c8\ub828\uacfc\uc815",
            "\uad50\uc721\uacfc\uc815",
            "\uc9c0\uc2dd",
            "\uae30\uc220",
            "\ud0dc\ub3c4",
            "\uc815\ub82c",
            "\ub9e4\ud551 \uae30\uc900",
            "\uc694\uad6c \uc218\uc900",
            "\ucd94\ucc9c",
        ),
        required_params=("query",),
        priority=60,
        default_params={"compact": True, "save": False, "limit": 5},
        pipeline=("get_concept_evidence", "ncs_training"),
    ),
    RoutePattern(
        scenario=TASK_TRANSITION,
        tool="recommend_task_transitions",
        reason="task similarity/transferability intent should use task-KSA similarity links",
        signals=(
            "similar task",
            "similar tasks",
            "transferability",
            "task transition",
            "\uc720\uc0ac\uacfc\uc5c5",
            "\uc720\uc0ac \uacfc\uc5c5",
            "\uc720\uc0ac\ud55c \uacfc\uc5c5",
            "\uc804\uc774",
            "\uc804\uc774\uac00\ub2a5",
            "\uacfc\uc5c5\uc804\ud658",
            "\uacfc\uc5c5 \uc804\ud658",
        ),
        required_params=("query",),
        priority=70,
        default_params={"limit": 10, "evidence_limit": 12},
        pipeline=("recommend_training_for_task",),
    ),
    RoutePattern(
        scenario=EVIDENCE_ANALYSIS,
        tool="ncs_analysis",
        reason="evidence inquiry should inspect NCS ontology, career, qualification, or job-base data",
        signals=(
            "evidence",
            "ontology",
            "qualification",
            "career path",
            "job base",
            "concept",
            "mapping evidence",
            "mapping basis",
            "classification evidence",
            "\uadfc\uac70",
            "\uc628\ud1a8\ub85c\uc9c0",
            "\uc790\uaca9",
            "\uacbd\ub825\uac1c\ubc1c",
            "\uc9c1\uc5c5\uae30\ucd08",
            "\uac1c\ub150",
            "\ub9e4\ud551\uadfc\uac70",
            "\ub9e4\ud551 \uadfc\uac70",
            "\ubd84\ub958 \uadfc\uac70",
            "\ubd84\uc11d",
        ),
        required_params=("mode",),
        priority=50,
        default_params={"mode": "ontology", "limit": 20},
    ),
    RoutePattern(
        scenario=OPERATOR_REVIEW,
        tool="get_quality_issues",
        reason="review/readiness intent belongs to the operator quality surface",
        signals=(
            "review",
            "readiness",
            "quality issue",
            "human review",
            "\uac80\ud1a0",
            "\uc900\ube44\ub3c4",
            "\ud488\uc9c8",
            "\uc0ac\ub78c\uac80\ud1a0",
        ),
        required_params=(),
        priority=40,
        default_params={"limit": 20},
    ),
    RoutePattern(
        scenario=STRUCTURE_SEARCH,
        tool="ncs_search",
        reason="general NCS lookup intent should search structure records first",
        signals=(
            "ncs",
            "search",
            "classification",
            "job classification",
            "job code",
            "job information",
            "job function",
            "duty",
            "unit",
            "element",
            "\ubd84\ub958",
            "\uac80\uc0c9",
            "\uc870\ud68c",
            "\ub2a5\ub825\ub2e8\uc704",
            "\ub2a5\ub825\ub2e8\uc704\uc694\uc18c",
            "\uc9c1\ubb34\ubd84\ub958",
            "\uc9c1\ubb34\ucf54\ub4dc",
            "\uc9c1\ubb34\uc815\ubcf4",
            "\uc9c1\ubb34\uae30\ub2a5",
            "\uc8fc\uc694\uc5c5\ubb34",
        ),
        required_params=("query",),
        priority=10,
        default_params={"scope": "all", "limit": 20},
    ),
)


SENSITIVE_CLAIM_SIGNALS = (
    "\uacf5\uc2dd \uc2b9\uc778",
    "\uc815\ubd80 \uc2b9\uc778",
    "\uc790\uaca9 \uc778\uc815",
    "\ubc95\uc801 \uc801\uaca9\uc131",
    "\uc778\uc99d",
    "official approval",
    "legal eligibility",
)

LEGACY_SCOPE_SIGNALS = (
    "sqf",
    "\ud559\uc2b5\ubaa8\ub4c8",
    "learning module",
)

LEGACY_SCOPE_OPT_OUT_SIGNALS = (
    "sqf는 쓰지",
    "sqf 쓰지",
    "sqf는 제외",
    "sqf 제외",
    "sqf 없이",
    "sqf 말고",
    "학습모듈은 쓰지",
    "학습모듈 쓰지",
    "학습모듈은 제외",
    "학습모듈 제외",
    "학습모듈 없이",
    "학습모듈 말고",
    "without sqf",
    "exclude sqf",
    "do not use sqf",
    "don't use sqf",
    "without learning module",
    "exclude learning module",
    "do not use learning module",
    "don't use learning module",
)

LEGACY_SCOPE_REQUEST_SIGNALS = (
    "sqf based",
    "sqf-based",
    "based on sqf",
    "use sqf",
    "using sqf",
    "include sqf",
    "with sqf",
    "sqf evidence",
    "sqf reference",
    "sqf path",
    "activate sqf",
    "reactivate sqf",
    "learning module based",
    "learning-module based",
    "based on learning module",
    "use learning module",
    "using learning module",
    "include learning module",
    "with learning module",
    "learning module evidence",
    "learning module reference",
    "learning module path",
    "activate learning module",
    "reactivate learning module",
    "sqf 기반",
    "sqf 사용",
    "sqf 활용",
    "sqf 포함",
    "sqf 참조",
    "sqf 근거",
    "학습모듈 기반",
    "학습모듈 사용",
    "학습모듈 활용",
    "학습모듈 포함",
    "학습모듈 참조",
    "학습모듈 근거",
)

DIRECT_STRUCTURE_SEARCH_SIGNALS = (
    "search",
    "lookup",
    "find",
    "ncs classification",
    "job classification",
    "job code",
    "\uac80\uc0c9",
    "\uc870\ud68c",
    "\uc9c1\ubb34\ubd84\ub958",
    "\uc9c1\ubb34\ucf54\ub4dc",
    "ncs \ub300\ubd84\ub958",
    "ncs \uc911\ubd84\ub958",
    "ncs \uc18c\ubd84\ub958",
    "\ubd84\ub958 \uae30\uc900",
    "찾기",
    "찾아",
    "목록",
    "알려줘",
)

STRUCTURE_ENTITY_SIGNALS = (
    "ncs",
    "classification",
    "job code",
    "job function",
    "unit",
    "element",
    "task",
    "criterion",
    "ksa",
    "분류",
    "직무",
    "직무코드",
    "직무기능",
    "주요업무",
    "능력단위",
    "능력단위요소",
    "과업",
    "수행준거",
    "지식",
    "기술",
    "태도",
)

EXPLICIT_TRAINING_INTENT_SIGNALS = (
    "training",
    "education",
    "course",
    "recommend",
    "curriculum",
    "교육",
    "훈련",
    "과정",
    "추천",
    "커리큘럼",
    "교육체계",
    "훈련체계",
)

TASK_TRAINING_CONTEXT_ONLY_SIGNALS = {
    "ksa",
    "과업",
    "수행준거",
    "능력단위",
    "지식",
    "기술",
    "태도",
    "요구 수준",
}

SEARCH_CLASSIFICATION_FILTER_FIELDS = (
    "major_code",
    "middle_code",
    "small_code",
    "sub_code",
    "major_name",
    "middle_name",
    "small_name",
    "sub_name",
)

DIRECT_TASK_TRANSITION_SIGNALS = (
    "similar task",
    "similar tasks",
    "transferability",
    "task transition",
    "\uc720\uc0ac\uacfc\uc5c5",
    "\uc720\uc0ac \uacfc\uc5c5",
    "\uc720\uc0ac\ud55c \uacfc\uc5c5",
    "\uc804\uc774\uac00\ub2a5",
    "\uacfc\uc5c5\uc804\ud658",
    "\uacfc\uc5c5 \uc804\ud658",
)

OPERATOR_REVIEW_INTENT_SIGNALS = (
    "review",
    "operator review",
    "review queue",
    "review target",
    "readiness",
    "quality issue",
    "quality",
    "human review",
    "검토 대상",
    "리뷰",
    "리뷰 대상",
    "운영자 검토",
    "검토",
    "준비도",
    "품질 이슈",
    "품질",
    "사람검토",
    "사람 검토",
)

OPERATOR_REVIEW_TARGET_SIGNALS = (
    "ksa",
    "definition",
    "ksa definition",
    "definition review",
    "concept definition",
    "training goal",
    "goal link",
    "training goal link",
    "task ksa",
    "task relation",
    "ontology concept",
    "concept link",
    "ksa link",
    "link quality",
    "review target",
    "human review target",
    "human review 대상",
    "operator",
    "operator surface",
    "review queue",
    "seedpack",
    "정의",
    "ksa 정의",
    "개념 정의",
    "검토 대상",
    "리뷰 대상",
    "운영자",
    "훈련목표",
    "목표 링크",
    "과업 ksa",
    "과업 관계",
    "온톨로지 개념",
    "개념 링크",
    "ksa 링크",
    "링크",
)

OPERATOR_REVIEW_SURFACE_SIGNALS = (
    "operator",
    "operator surface",
    "operator review",
    "review queue",
    "review target",
    "quality issue",
    "readiness",
    "검토 대상",
    "리뷰 대상",
    "운영자",
    "운영자 검토",
    "품질 이슈",
    "준비도",
)


def route_ncs_query(
    query: str,
    *,
    available_tool_names: set[str] | None = None,
    classification_filter: dict[str, Any] | None = None,
    context_text: str | None = None,
    job_scope: str | None = None,
) -> dict[str, Any]:
    normalized = normalize_query(query)
    internal_role_request = _is_internal_role_request(normalized)
    scored: list[tuple[int, int, RoutePattern]] = []
    for pattern in ROUTE_PATTERNS:
        score = _score_pattern(pattern, normalized)
        if available_tool_names is None or pattern.tool in available_tool_names or score > 0:
            scored.append((score, pattern.priority, pattern))
    if not scored:
        scored.append((0, 0, _pattern_by_scenario(STRUCTURE_SEARCH)))
    scored = sorted(
        scored,
        key=lambda item: (item[0], item[1]),
        reverse=True,
    )
    score, _, pattern = scored[0]
    if internal_role_request:
        pattern = _pattern_by_scenario(EVIDENCE_ANALYSIS)
        score = max(score, 1)
    if score <= 0:
        pattern = _pattern_by_scenario(STRUCTURE_SEARCH)
        score = 1

    params = _params_for_pattern(pattern, query)
    normalized_classification_filter = _normalize_route_classification_filter(
        classification_filter
    )
    normalized_context_text, normalized_job_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    context_requested = bool(
        pattern.tool == "ncs_search"
        and (normalized_context_text or normalized_job_scope)
    )
    if pattern.tool == "ncs_search" and normalized_classification_filter:
        params["classification_filter"] = normalized_classification_filter
    if pattern.tool == "ncs_search" and normalized_job_scope:
        # job_scope is a short, caller-supplied product field.  Free-form
        # context_text is deliberately represented only by its digest below.
        params["job_scope"] = normalized_job_scope
    required_params = list(pattern.required_params)
    if (
        pattern.scenario == EVIDENCE_ANALYSIS
        and params.get("mode") == "internal_role"
        and "internal_role_id" not in required_params
    ):
        required_params.append("internal_role_id")
    missing = [name for name in required_params if _is_missing(params.get(name))]
    unavailable = bool(available_tool_names is not None and pattern.tool not in available_tool_names)
    matched_signals = _matched_signals(pattern, normalized)
    risk_flags = risk_flags_for_query(query)
    pipeline_tools = list(pattern.pipeline)
    if (
        pattern.scenario == EVIDENCE_ANALYSIS
        and params.get("mode") == "internal_role"
        and params.get("include_training") is True
        and "recommend_training_for_task" not in pipeline_tools
    ):
        pipeline_tools.append("recommend_training_for_task")
    pipeline = [{"tool": tool_name} for tool_name in pipeline_tools]
    allowed_tools = [pattern.tool, *[tool_name for tool_name in pipeline_tools if tool_name != pattern.tool]]
    guard_flags = _guard_flags_for_route(
        missing_params=missing,
        unavailable=unavailable,
        risk_flags=risk_flags,
        pattern=pattern,
    )
    legacy_scope_mentioned = any(flag["code"] == "legacy_sqf_or_learning_module_scope" for flag in risk_flags)
    legacy_scope_requested = legacy_scope_mentioned and _legacy_scope_requested(normalized)
    classification_context = _classification_context_contract(
        pattern,
        normalized_classification_filter,
        context_text=normalized_context_text,
        job_scope=normalized_job_scope,
    )
    fingerprint_version = (
        CONTEXT_ROUTE_FINGERPRINT_VERSION
        if context_requested
        else ROUTE_FINGERPRINT_VERSION
    )
    route_contract = {
        "schema": ROUTE_CONTRACT_SCHEMA,
        "fingerprint_version": fingerprint_version,
        "route_first": True,
        "primary_tool": pattern.tool,
        "allowed_tools": allowed_tools,
        "required_params": required_params,
        "provided_params": sorted(
            key for key, value in params.items()
            if key in required_params and not _is_missing(value)
        ),
        "missing_params": missing,
        "execution_policy": {
            "meta_executable": pattern.scenario != OPERATOR_REVIEW,
            "save_forced_false": pattern.tool in ROUTE_META_SAVE_FORCED_TOOLS,
            "compact_default_true": pattern.tool in ROUTE_META_COMPACT_DEFAULT_TOOLS,
            "operator_review_requires_operator_surface": pattern.scenario == OPERATOR_REVIEW,
            "legacy_sqf_or_learning_module_inactive": True,
            "legacy_sqf_or_learning_module_mentioned": legacy_scope_mentioned,
            "legacy_sqf_or_learning_module_requested": legacy_scope_requested,
        },
        "classification_context": classification_context,
    }
    guide_prompt_template = match_hrd_guide_prompt_template(
        query,
        tool=pattern.tool,
        scenario=pattern.scenario,
    )
    guide_reference = hrd_guide_reference_metadata()
    if guide_prompt_template:
        route_contract["guide_reference"] = guide_reference
        route_contract["guide_prompt_template"] = {
            "id": guide_prompt_template.get("id"),
            "guide_stage": guide_prompt_template.get("guide_stage") or [],
            "expected_tool": guide_prompt_template.get("expected_tool"),
            "required_response_fields": guide_prompt_template.get("required_response_fields") or [],
        }
    route_fingerprint = route_fingerprint_for_payload(
        {
            "schema": ROUTE_CONTRACT_SCHEMA,
            "version": fingerprint_version,
            "query": normalized,
            "scenario": pattern.scenario,
            "tool": pattern.tool,
            "params": params,
            "required_params": required_params,
            "missing_params": missing,
            "available": not unavailable,
            "matched_signals": matched_signals,
            "pipeline": [step["tool"] for step in pipeline],
            "risk_flags": [flag["code"] for flag in risk_flags],
            "guide_reference": {
                "schema": guide_reference.get("schema"),
                "source_hash_sha256": guide_reference.get("source_hash_sha256"),
            },
            "guide_prompt_template": {
                "id": guide_prompt_template.get("id"),
                "required_response_fields": guide_prompt_template.get("required_response_fields") or [],
            }
            if guide_prompt_template
            else None,
            "classification_context": classification_context,
        }
    )
    route_contract["route_fingerprint"] = route_fingerprint

    return {
        "schema": ROUTE_CONTRACT_SCHEMA,
        "query": query,
        "scenario": pattern.scenario,
        "tool": pattern.tool,
        "params": params,
        "required_params": required_params,
        "missing_params": missing,
        "available": not unavailable,
        "reason": pattern.reason,
        "score": score,
        "confidence": _route_confidence(score),
        "matched_signals": matched_signals,
        "pipeline": pipeline,
        "expected_tool_chain": allowed_tools,
        "risk_flags": risk_flags,
        "guard_flags": guard_flags,
        "route_contract": route_contract,
        "route_fingerprint": route_fingerprint,
        "guide_reference": guide_reference,
        "guide_prompt_template": guide_prompt_template,
        "classification_context": classification_context,
    }


def aihr_plan_route_evidence(
    current_query: str,
    target_query: str,
    *,
    available_tool_names: set[str] | None = None,
    major_code: str | None = None,
    current_major_code: str | None = None,
    target_major_code: str | None = None,
    current_middle_code: str | None = None,
    target_middle_code: str | None = None,
    current_small_code: str | None = None,
    target_small_code: str | None = None,
    current_sub_code: str | None = None,
    target_sub_code: str | None = None,
) -> dict[str, Any]:
    route_query = f"{current_query}에서 {target_query}으로 교육훈련체계"
    route = route_ncs_query(route_query, available_tool_names=available_tool_names)
    scope_inputs = {
        "major_code": major_code,
        "current_major_code": current_major_code,
        "target_major_code": target_major_code,
        "current_middle_code": current_middle_code,
        "target_middle_code": target_middle_code,
        "current_small_code": current_small_code,
        "target_small_code": target_small_code,
        "current_sub_code": current_sub_code,
        "target_sub_code": target_sub_code,
    }
    scope_params = {
        key: text
        for key, value in scope_inputs.items()
        if value is not None and (text := str(value).strip())
    }
    current_filter = {
        key: value
        for key, value in {
            "major_code": scope_params.get("current_major_code")
            or scope_params.get("major_code"),
            "middle_code": scope_params.get("current_middle_code"),
            "small_code": scope_params.get("current_small_code"),
            "sub_code": scope_params.get("current_sub_code"),
        }.items()
        if value
    }
    target_filter = {
        key: value
        for key, value in {
            "major_code": scope_params.get("target_major_code")
            or scope_params.get("major_code"),
            "middle_code": scope_params.get("target_middle_code"),
            "small_code": scope_params.get("target_small_code"),
            "sub_code": scope_params.get("target_sub_code"),
        }.items()
        if value
    }
    classification_context = {
        "supported": True,
        "parameter": "current/target classification code fields",
        "mode": "explicit_current_target_filters",
        "source": "caller_supplied",
        "fields": list(scope_inputs),
        "provided": bool(scope_params),
        "filter": None,
        "shared_filter": (
            {"major_code": scope_params["major_code"]}
            if "major_code" in scope_params
            else None
        ),
        "current_filter": current_filter or None,
        "target_filter": target_filter or None,
    }
    params = dict(route.get("params") or {})
    params.update(scope_params)
    route_contract = dict(route.get("route_contract") or {})
    route_contract["classification_context"] = classification_context
    route_contract["provided_params"] = list(
        dict.fromkeys(
            [
                *(route_contract.get("provided_params") or []),
                *scope_params,
            ]
        )
    )
    route_fingerprint = route_fingerprint_for_payload(
        {
            "schema": ROUTE_CONTRACT_SCHEMA,
            "version": ROUTE_FINGERPRINT_VERSION,
            "base_route_fingerprint": route.get("route_fingerprint"),
            "classification_context": classification_context,
        }
    )
    route_contract["route_fingerprint"] = route_fingerprint
    return {
        "schema": route.get("schema"),
        "query": route.get("query"),
        "scenario": route.get("scenario"),
        "tool": route.get("tool"),
        "params": params,
        "required_params": route.get("required_params") or [],
        "missing_params": route.get("missing_params") or [],
        "available": route.get("available"),
        "confidence": route.get("confidence"),
        "expected_tool_chain": route.get("expected_tool_chain") or [],
        "guard_flags": route.get("guard_flags") or [],
        "risk_flags": route.get("risk_flags") or [],
        "route_contract": route_contract,
        "route_fingerprint": route_fingerprint,
        "classification_context": classification_context,
        "guide_reference": route.get("guide_reference") or {},
        "guide_prompt_template": route.get("guide_prompt_template") or {},
    }


def risk_flags_for_query(query: str) -> list[dict[str, str]]:
    normalized = normalize_query(query)
    flags: list[dict[str, str]] = []
    if any(signal in normalized for signal in SENSITIVE_CLAIM_SIGNALS):
        flags.append(
            {
                "code": "official_or_legal_claim_risk",
                "severity": "high",
                "message": (
                    "Do not present NCS recommendations as official approval, "
                    "qualification recognition, or legal eligibility."
                ),
            }
        )
    if any(signal in normalized for signal in LEGACY_SCOPE_SIGNALS):
        flags.append(
            {
                "code": "legacy_sqf_or_learning_module_scope",
                "severity": "medium",
                "message": (
                    "SQF and NCS learning modules are legacy/reference paths unless "
                    "the user explicitly activates them."
                ),
            }
        )
    return flags


def route_fingerprint_for_payload(payload: dict[str, Any]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:24]


def normalize_query(query: str) -> str:
    return " ".join(str(query or "").lower().split())


def _legacy_scope_opted_out(normalized_query: str) -> bool:
    return any(signal in normalized_query for signal in LEGACY_SCOPE_OPT_OUT_SIGNALS)


def _legacy_scope_requested(normalized_query: str) -> bool:
    if _legacy_scope_opted_out(normalized_query):
        return False
    return any(signal in normalized_query for signal in LEGACY_SCOPE_REQUEST_SIGNALS)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, dict)):
        return not value
    return False


def _pattern_by_scenario(scenario: str) -> RoutePattern:
    for pattern in ROUTE_PATTERNS:
        if pattern.scenario == scenario:
            return pattern
    raise KeyError(scenario)


def _score_pattern(pattern: RoutePattern, normalized: str) -> int:
    if not normalized:
        return 0
    direct_structure_search = _has_direct_structure_search_intent(normalized)
    structure_entity = _has_structure_entity_intent(normalized)
    explicit_training = _has_explicit_training_intent(normalized)
    explicit_evidence = bool(
        _matched_signals(_pattern_by_scenario(EVIDENCE_ANALYSIS), normalized)
    )
    prefer_structure_search = (
        direct_structure_search
        and structure_entity
        and not explicit_training
        and not explicit_evidence
    )
    matched_signals = _matched_signals(pattern, normalized)
    score = pattern.priority if matched_signals else 0
    if pattern.scenario == TASK_TRAINING and matched_signals:
        actionable_signals = [
            signal
            for signal in matched_signals
            if signal not in TASK_TRAINING_CONTEXT_ONLY_SIGNALS
        ]
        if not actionable_signals and not explicit_training:
            score = 0
    operator_review_intent = _has_operator_review_intent(normalized)
    strong_operator_review_intent = _has_operator_review_surface_intent(normalized)
    if pattern.scenario == OPERATOR_REVIEW and strong_operator_review_intent:
        score += 90
    elif pattern.scenario == OPERATOR_REVIEW and operator_review_intent:
        score += 35
    elif strong_operator_review_intent and pattern.scenario in {
        EDUCATION_SYSTEM,
        TRAINING_TRANSITION,
        TASK_TRAINING,
        TASK_TRANSITION,
    }:
        score -= 35
    if pattern.scenario == STRUCTURE_SEARCH and prefer_structure_search:
        score += 60
    if pattern.scenario == TASK_TRANSITION and any(
        signal in normalized for signal in DIRECT_TASK_TRANSITION_SIGNALS
    ):
        score += 40
    guide_match = match_hrd_guide_prompt_template(
        normalized,
        tool=pattern.tool,
        scenario=pattern.scenario,
    )
    if guide_match and not (prefer_structure_search and pattern.scenario != STRUCTURE_SEARCH):
        score += 20 + min(20, int(guide_match.get("match_score") or 0))
    if pattern.scenario in {EDUCATION_SYSTEM, TRAINING_TRANSITION} and _extract_transition_terms(normalized):
        score += 15
    if pattern.scenario == EVIDENCE_ANALYSIS and "risk" in normalized:
        score += 3
    return score


def _route_confidence(score: int) -> float:
    if score <= 0:
        return 0.0
    return round(min(0.99, max(0.1, score / 120)), 2)


def _guard_flags_for_route(
    *,
    missing_params: list[str],
    unavailable: bool,
    risk_flags: list[dict[str, str]],
    pattern: RoutePattern,
) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    if missing_params:
        flags.append(
            {
                "code": "missing_required_params",
                "severity": "high",
                "params": missing_params,
                "message": "Route is not directly executable until required parameters are supplied.",
            }
        )
    if unavailable:
        flags.append(
            {
                "code": "route_tool_unavailable",
                "severity": "high",
                "tool": pattern.tool,
                "message": "The routed tool is outside the currently available MCP surface.",
            }
        )
    if pattern.scenario == OPERATOR_REVIEW:
        flags.append(
            {
                "code": "operator_review_route",
                "severity": "medium",
                "message": "Human-review routes require the operator surface and must not auto-approve data.",
            }
        )
    flags.extend(
        {
            "code": flag["code"],
            "severity": flag["severity"],
            "message": flag["message"],
        }
        for flag in risk_flags
    )
    return flags


def _matched_signals(pattern: RoutePattern, normalized: str) -> list[str]:
    return [signal for signal in pattern.signals if signal.lower() in normalized]


def _has_direct_structure_search_intent(normalized: str) -> bool:
    return any(signal in normalized for signal in DIRECT_STRUCTURE_SEARCH_SIGNALS)


def _has_structure_entity_intent(normalized: str) -> bool:
    return any(signal in normalized for signal in STRUCTURE_ENTITY_SIGNALS)


def _has_explicit_training_intent(normalized: str) -> bool:
    return any(signal in normalized for signal in EXPLICIT_TRAINING_INTENT_SIGNALS)


def _has_operator_review_intent(normalized: str) -> bool:
    has_review_signal = any(signal in normalized for signal in OPERATOR_REVIEW_INTENT_SIGNALS)
    has_target_signal = any(signal in normalized for signal in OPERATOR_REVIEW_TARGET_SIGNALS)
    return has_review_signal and has_target_signal


def _has_operator_review_surface_intent(normalized: str) -> bool:
    return _has_operator_review_intent(normalized) and any(
        signal in normalized for signal in OPERATOR_REVIEW_SURFACE_SIGNALS
    )


def _params_for_pattern(pattern: RoutePattern, query: str) -> dict[str, Any]:
    params = dict(pattern.default_params or {})
    transition = _extract_transition_terms(query)
    if pattern.scenario in {EDUCATION_SYSTEM, TRAINING_TRANSITION}:
        params.update(transition)
        if not params.get("target_query") and query:
            params["target_query"] = _strip_route_noise(query)
        return params
    if pattern.scenario == EVIDENCE_ANALYSIS:
        params.setdefault("query", _analysis_scope_query(query))
        params["mode"] = _analysis_mode(query)
        if params["mode"] == "internal_role":
            role_id = _extract_internal_role_id(query)
            if role_id:
                params["internal_role_id"] = role_id
            params["include_training"] = _requests_training(query)
        return params
    if pattern.scenario == OPERATOR_REVIEW:
        params.update(_operator_review_params(query))
        params.setdefault("query", _strip_route_noise(query))
        return params
    if pattern.scenario == TASK_TRANSITION:
        params.setdefault("query", _task_transition_scope_query(query))
        return params
    params.setdefault("query", _strip_route_noise(query))
    return params


def _normalize_route_classification_filter(
    classification_filter: dict[str, Any] | None,
) -> dict[str, str]:
    """Normalize only caller-supplied classification constraints for routing."""
    if not isinstance(classification_filter, dict):
        return {}
    normalized: dict[str, str] = {}
    for field in SEARCH_CLASSIFICATION_FILTER_FIELDS:
        value = classification_filter.get(field)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            normalized[field] = text
    return normalized


def _classification_context_contract(
    pattern: RoutePattern,
    classification_filter: dict[str, str] | None = None,
    *,
    context_text: str | None = None,
    job_scope: str | None = None,
) -> dict[str, Any]:
    """Describe the explicit classification scope supported by structure search.

    The router does not infer a major from ordinary words and never invents a
    filter.  Callers may supply ``classification_filter`` when executing the
    routed ``ncs_search`` tool; the search layer applies it as a hard,
    parameter-bound scope constraint.
    """
    supported = pattern.tool == "ncs_search"
    normalized_filter = classification_filter if supported else {}
    if supported and (context_text or job_scope):
        return {
            "schema": NCS_SEARCH_CONTEXT_SCHEMA,
            "resolver_version": NCS_SEARCH_CONTEXT_RESOLVER_VERSION,
            "supported": True,
            "parameter": "classification_filter",
            "mode": "soft_prior_shadow",
            "source": "caller_supplied_context",
            "fields": list(SEARCH_CLASSIFICATION_FILTER_FIELDS),
            "context_fields": ["context_text", "job_scope"],
            "provided": True,
            "filter": normalized_filter or None,
            "requested": search_context_request_contract(
                context_text=context_text,
                job_scope=job_scope,
                classification_filter=normalized_filter,
            ),
            "resolution": None,
            "policy": {
                "query_inference_allowed": False,
                "soft_prior_source": "caller_supplied_context",
                "hard_filter_source": (
                    "caller_supplied" if normalized_filter else None
                ),
                "lexical_tier_preserved": True,
                "rollout_phase": "shadow",
            },
            "prior_applied": False,
        }
    return {
        "supported": supported,
        "parameter": "classification_filter" if supported else None,
        "mode": "explicit_hard_filter" if supported else "not_applicable",
        "source": "caller_supplied" if supported else None,
        "fields": list(SEARCH_CLASSIFICATION_FILTER_FIELDS) if supported else [],
        "provided": bool(normalized_filter),
        "filter": normalized_filter or None,
    }


def normalize_search_context_inputs(
    *,
    context_text: Any = None,
    job_scope: Any = None,
) -> tuple[str | None, str | None]:
    """Canonicalize explicit context without deriving it from the search query."""

    def normalize(value: Any, *, max_length: int, field: str) -> str | None:
        if value is None:
            return None
        text = unicodedata.normalize("NFKC", str(value))
        text = " ".join(text.strip().split()).casefold()
        if not text:
            return None
        if len(text) > max_length:
            raise ValueError(f"{field} exceeds maxLength={max_length}")
        return text

    return (
        normalize(
            context_text,
            max_length=NCS_SEARCH_CONTEXT_TEXT_MAX_LENGTH,
            field="context_text",
        ),
        normalize(
            job_scope,
            max_length=NCS_SEARCH_JOB_SCOPE_MAX_LENGTH,
            field="job_scope",
        ),
    )


def search_context_request_contract(
    *,
    context_text: Any = None,
    job_scope: Any = None,
    classification_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the public-safe request binding for explicit search context."""
    normalized_context, normalized_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    return {
        "context_text_present": bool(normalized_context),
        "context_text_length": len(normalized_context or ""),
        "context_text_digest": (
            hashlib.sha256(normalized_context.encode("utf-8")).hexdigest()[:16]
            if normalized_context
            else None
        ),
        "job_scope": normalized_scope,
        "classification_filter": (
            _normalize_route_classification_filter(classification_filter) or None
        ),
    }


def _extract_transition_terms(query: str) -> dict[str, str]:
    text = str(query or "").strip()
    patterns = (
        "(?P<current>.+?)\\s*(?:from|\\uc5d0\\uc11c|\\ubd80\\ud130)\\s*"
        "(?P<target>.+?)\\s*(?:to|\\uc73c\\ub85c|\\ub85c|\\uae4c\\uc9c0|\\uc804\\ud658|\\uc774\\ub3d9)",
        "(?:from|\\ud604\\uc7ac|as-is|current)\\s*[:=]?\\s*(?P<current>.+?)\\s*"
        "(?:to|\\ubaa9\\ud45c|target)\\s*[:=]?\\s*(?P<target>.+)",
        "(?P<current>.+?)\\s*(?:->|=>|\\u2192)\\s*(?P<target>.+)",
        "(?P<current>.+?)\\s*\\ub2f4\\ub2f9\\uc790(?:\\uac00)?\\s+"
        "(?P<target>.+?)\\s*(?:\\uc73c\\ub85c|\\ub85c)\\s*(?:\\uc804\\ud658|\\uc774\\ub3d9)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        current = _clean_term(match.group("current"))
        target = _clean_term(match.group("target"))
        if current and target:
            return {"current_query": current, "target_query": target}
    return {}


def _analysis_mode(query: str) -> str:
    normalized = normalize_query(query)
    if any(
        signal in normalized
        for signal in ("internal role", "enterprise role", "사내 직무", "내부 직무")
    ):
        return "internal_role"
    if any(signal in normalized for signal in ("semantic", "vector", "시맨틱", "벡터")):
        return "semantic"
    if any(signal in normalized for signal in ("qualification", "\uc790\uaca9")):
        return "qualification"
    if any(signal in normalized for signal in ("career path", "\uacbd\ub825\uac1c\ubc1c")):
        return "career_path"
    if any(signal in normalized for signal in ("job base", "\uc9c1\uc5c5\uae30\ucd08")):
        return "job_base"
    return "ontology"


def _is_internal_role_request(normalized: str) -> bool:
    """Give explicit internal-role context precedence over generic training terms."""
    role_language = any(signal in normalized for signal in (
        "internal role", "enterprise role",
        "\uc0ac\ub0b4 \uc9c1\ubb34", "\ub0b4\ubd80 \uc9c1\ubb34",
    ))
    return bool(role_language or _extract_internal_role_id(normalized))


def _requests_training(query: str) -> bool:
    normalized = normalize_query(query)
    return any(
        signal in normalized
        for signal in (
            "training",
            "course",
            "recommend",
            "\uad50\uc721",
            "\ud6c8\ub828",
            "\ucd94\ucc9c",
        )
    )


def _extract_internal_role_id(query: str) -> str | None:
    """Extract only an explicit role identifier; never infer one from a title."""

    text = str(query or "")
    patterns = (
        r"\bncs:internal_job_role:[A-Za-z0-9:_-]+\b",
        r"ijr[_-][A-Za-z0-9_-]+",
        r"\b(?:internal_role_id|role_id)\s*[:=]\s*([A-Za-z0-9:_-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1) if match.lastindex else match.group(0)
    return None


def _operator_review_params(query: str) -> dict[str, Any]:
    normalized = normalize_query(query)
    params: dict[str, Any] = {}
    if any(
        signal in normalized
        for signal in (
            "training goal",
            "goal link",
            "training goal link",
            "\ud6c8\ub828\ubaa9\ud45c",
            "\ubaa9\ud45c \ub9c1\ud06c",
        )
    ):
        params["target_type"] = "training_goal_concept_link"
    elif any(
        signal in normalized
        for signal in (
            "task ksa",
            "task relation",
            "\uacfc\uc5c5 ksa",
            "\uacfc\uc5c5 \uad00\uacc4",
        )
    ):
        params["target_type"] = "task_ksa_concept_relation"
    elif any(
        signal in normalized
        for signal in (
            "ontology concept",
            "concept",
            "ksa",
            "definition",
            "ksa definition",
            "concept definition",
            "정의",
            "ksa 정의",
            "개념 정의",
            "\uc628\ud1a8\ub85c\uc9c0 \uac1c\ub150",
            "\uac1c\ub150",
        )
    ):
        params["target_type"] = "ontology_concept"
    if any(
        signal in normalized
        for signal in (
            "human review",
            "사람검토",
            "\uc0ac\ub78c \uac80\ud1a0",
        )
    ):
        params.setdefault("issue_type", "human_review_required")
    return params


def _clean_term(term: str) -> str:
    cleaned = _strip_route_noise(term)
    cleaned = re.sub(r"^(current|target|as-is|to|from)\s*[:=]\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" :;,.")


def _analysis_scope_query(query: str) -> str:
    text = str(query or "").strip()
    suffixes = (
        "ontology evidence analysis",
        "qualification evidence analysis",
        "career path evidence analysis",
        "job base evidence analysis",
        "evidence analysis",
        "\uc628\ud1a8\ub85c\uc9c0 \uadfc\uac70 \ubd84\uc11d",
        "\uc790\uaca9 \uadfc\uac70 \ubd84\uc11d",
        "\uacbd\ub825\uac1c\ubc1c \uadfc\uac70 \ubd84\uc11d",
        "\uc9c1\uc5c5\uae30\ucd08 \uadfc\uac70 \ubd84\uc11d",
        "\uadfc\uac70 \ubd84\uc11d",
    )
    for suffix in suffixes:
        text = re.sub(
            rf"(?:^|\s+){re.escape(suffix)}\s*$",
            " ",
            text,
            flags=re.IGNORECASE,
        )
    return _strip_route_noise(text)


def _task_transition_scope_query(query: str) -> str:
    text = str(query or "").strip()
    match = re.search(
        r"^(?P<scope>.+?)(?:(?:\uacfc|\uc640)\s+|\s+)"
        r"(?:\uc720\uc0ac\ud55c|\uc720\uc0ac)\s*\uacfc\uc5c5(?:\s|$)",
        text,
    )
    if match:
        return _clean_term(match.group("scope"))
    return _strip_route_noise(text)


def _strip_route_noise(query: str) -> str:
    text = str(query or "").strip()
    replacements = (
        "training course recommendation",
        "course recommendation",
        "training course",
        "recommend",
        "ncs search",
        "ncs lookup",
        "ncs find",
        "training",
        "course",
        "search",
        "lookup",
        "find",
        "education system",
        "training system",
        "curriculum",
        "roadmap",
        "\ud6c8\ub828\uacfc\uc815 \ucd94\ucc9c",
        "\uad50\uc721\uacfc\uc815 \ucd94\ucc9c",
        "\uacfc\uc815 \ucd94\ucc9c",
        "\ud6c8\ub828\uacfc\uc815",
        "\uad50\uc721\uacfc\uc815",
        "\ucd94\ucc9c",
        "\uac80\uc0c9",
        "\uc870\ud68c",
        "\ud6c8\ub828",
        "\uad50\uc721",
        "\uad50\uc721\uccb4\uacc4",
        "\ud6c8\ub828\uccb4\uacc4",
        "\uc804\ud658",
        "\uc774\ub3d9",
        "\ub9cc\ub4e4\uc5b4\uc918",
        "\ubcf4\uc5ec\uc918",
    )
    cleaned = text
    for token in replacements:
        cleaned = re.sub(re.escape(token), " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()
