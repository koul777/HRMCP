from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.contracts import (
    AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
    PLAN_NCS_EDUCATION_PATH_TOOL,
)


HRD_GUIDE_REFERENCE_SCHEMA = "ncs_hrd_guide_reference_v1"
DEFAULT_HRD_GUIDE_RAW_FILENAME = "ncs_hrd_guide_codex_readable.md"
DEFAULT_HRD_GUIDE_INDEX_FILENAME = "ncs_hrd_guide_reference.index.json"
DEFAULT_HRD_GUIDE_MARKDOWN_FILENAME = "ncs_hrd_guide_reference.md"
DEFAULT_HRD_GUIDE_CHUNKS_FILENAME = "ncs_hrd_guide_reference.chunks.jsonl"
BUILTIN_HRD_GUIDE_SOURCE_HASH_SHA256 = (
    "84f545893faab5c463314f7b5940717b275b3c14ab9ef85e3cd0b6636857465c"
)
BUILTIN_HRD_GUIDE_PAGE_COUNT = 94
PROJECT_ROOT = Path(__file__).resolve().parents[2]

PAGE_MARKER_RE = re.compile(r"^<!--\s*page:\s*(?P<page>\d+)\s*-->\s*$", re.MULTILINE)
PAGE_HEADING_RE = re.compile(r"^##\s+Page\s+(?P<page>\d+)\s*-\s*(?P<title>.*)$", re.MULTILINE)
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(?P<body>.*?)\n---\s*", re.DOTALL)
PROMPT_MARKER_RE = re.compile(
    r"(프롬프트|prompt|Prompt|ChatGPT|GPT|질문|요청|작성해|수립|도출|매핑|교육훈련체계)",
    re.IGNORECASE,
)
PROMPT_CONTEXT_RE = re.compile(r"(프롬프트|prompt|ChatGPT|GPT|GPTs|질문|요청)", re.IGNORECASE)
PROMPT_COMMAND_RE = re.compile(
    r"(해줘|작성해줘|정리해줘|도출해줘|추천해줘|설계해줘|수립해줘|만들어줘|제시해줘|분류해줘|구성해줘|표.*정리)",
    re.IGNORECASE,
)
PROMPT_QUOTE_RE = re.compile(r"[“\"](?P<quote>[^”\"]{8,500})[”\"]")

GUIDE_WORKFLOW_STAGES: tuple[dict[str, Any], ...] = (
    {
        "code": "C1-1",
        "name": "education_course_investigation_and_job_mapping",
        "korean_name": "교육과정 조사 및 직무기반 매핑",
        "product_contract": (
            "Collect purpose, target, content, hours, type, delivery method, and evaluation "
            "metadata, then map courses to job, duty, task, performance criteria, KSA, and level. "
            "Do not map by course title alone."
        ),
        "required_output": [
            "course_intake_requirements",
            "training_course_inventory_template",
            "job_scope",
            "task_ksa_basis",
            "evidence_chain",
            "course_link",
            "course_fit.level",
            "course_fit.hours",
            "course_fit.methods",
            "course_fit.facilities",
        ],
    },
    {
        "code": "C1-2",
        "name": "training_need_review",
        "korean_name": "교육 필요성 검토",
        "product_contract": (
            "Classify required, optional, supplemental, and adjacent courses using job relevance, "
            "level fit, legal or mandatory basis when supplied, duplication, feasibility, and "
            "performance contribution. Automation can prepare review evidence only."
        ),
        "required_output": [
            "training_necessity_review",
            "required_optional_basis",
            "planner_grouping",
            "decision_state",
            "human_review",
            "specificity_warning",
            "duplicate_or_generic_warning",
            "mapping_strength_warning",
        ],
    },
    {
        "code": "C2-1",
        "name": "training_system_matrix",
        "korean_name": "교육훈련체계도 구성",
        "product_contract": (
            "Restructure recommendation results by job scope, target level band, education type, "
            "course grouping, required or optional status, and delivery operation."
        ),
        "required_output": [
            "recommended_path",
            "training_system_summary",
            "training_system_matrix",
            "education_type",
            "target_level_band",
        ],
    },
    {
        "code": "C2-2",
        "name": "annual_operation_plan_readiness",
        "korean_name": "연간 운영계획 준비",
        "product_contract": (
            "Preserve scheduling, target population, delivery, facility, review-state, and "
            "management-plan fields so annual operation planning can be added without losing "
            "evidence."
        ),
        "required_output": [
            "annual_operation_plan",
            "delivery_operation",
            "facility_constraint_fit",
            "target_population",
            "preferred_methods",
            "preferred_facilities",
            "decision_state",
            "review_state",
        ],
    },
)

GUIDE_TRACE_CHECKS: tuple[dict[str, str], ...] = (
    {
        "check": "job_scope",
        "stage": "C1-1",
        "description": "The plan states the NCS job or transition scope before recommending courses.",
    },
    {
        "check": "task_ksa",
        "stage": "C1-1",
        "description": "The plan links recommendations to task, performance-criteria, and KSA evidence.",
    },
    {
        "check": "course_link",
        "stage": "C1-1",
        "description": "The plan explains why each course is linked beyond title matching.",
    },
    {
        "check": "required_optional",
        "stage": "C1-2",
        "description": "The plan distinguishes required, optional, supplemental, and adjacent courses.",
    },
    {
        "check": "level_delivery",
        "stage": "C2-1",
        "description": "The plan exposes level, hours, method, facility, and delivery fit.",
    },
    {
        "check": "human_review",
        "stage": "C1-2/C2-2",
        "description": "Automation leaves human decision states as review-needed evidence.",
    },
)

DEVELOPMENT_RULES: tuple[dict[str, str], ...] = (
    {
        "id": "framework_reference_only",
        "rule": (
            "Use the guide as a planning and validation rubric only. Do not inject guide examples "
            "as NCS source data or score-boosting training facts."
        ),
    },
    {
        "id": "job_task_ksa_course_chain",
        "rule": (
            "Answer education-plan prompts through job scope -> task/performance criteria -> KSA "
            "gap -> training-course evidence -> education-system matrix."
        ),
    },
    {
        "id": "course_title_is_weak_evidence",
        "rule": (
            "Course names alone are weak evidence. Prefer training objective, NCS unit, KSA, hours, "
            "method, facility, and level evidence."
        ),
    },
    {
        "id": "human_review_required",
        "rule": (
            "Automated outputs may mark rows for human review, but must not set human_reviewed, "
            "accepted, or reviewed without an explicit human decision."
        ),
    },
    {
        "id": "no_official_recognition_claim",
        "rule": (
            "Recommendations are training-planning guidance and must not claim official approval, "
            "qualification recognition, or legal eligibility."
        ),
    },
)

PROMPT_SCENARIO_TEMPLATES: tuple[dict[str, Any], ...] = (
    {
        "id": "education_system_from_transition",
        "guide_stage": ["C1-1", "C1-2", "C2-1", "C2-2"],
        "prompt_intent": "current job to target job education/training system plan",
        "korean_example": "노무관리 담당자가 인사기획으로 전환하기 위한 교육훈련체계를 수립해줘.",
        "expected_tool": PLAN_NCS_EDUCATION_PATH_TOOL,
        "required_response_fields": [
            "query_route",
            "recommended_path",
            "course_intake_requirements",
            "training_course_inventory_template",
            "training_necessity_review",
            "training_system_summary",
            "training_system_matrix",
            "training_system_guide_trace",
        ],
    },
    {
        "id": "task_based_course_recommendation",
        "guide_stage": ["C1-1", "C1-2"],
        "prompt_intent": "recommend courses for an NCS task or competency unit",
        "korean_example": "인력채용 과업을 수행하기 위한 교육과정을 추천해줘.",
        "expected_tool": "recommend_training_for_task",
        "required_response_fields": [
            "scope",
            "recommendation_groups",
            "evidence_highlights",
            "delivery",
            "human_review",
        ],
    },
    {
        "id": "training_transition_gap_analysis",
        "guide_stage": ["C1-1", "C1-2"],
        "prompt_intent": "compare current and target job KSA gaps and course coverage",
        "korean_example": "복무관리에서 인사기획으로 이동할 때 부족한 KSA와 보완 교육을 알려줘.",
        "expected_tool": "recommend_training_transition",
        "required_response_fields": [
            "source_scope",
            "target_scope",
            "gap_concepts",
            "recommendation_groups",
            "coverage_summary",
        ],
    },
    {
        "id": "annual_operation_plan_draft",
        "guide_stage": ["C2-2"],
        "prompt_intent": "turn an education-system matrix into annual operation planning fields",
        "korean_example": "추천된 교육과정을 연간 운영계획 초안으로 정리해줘.",
        "expected_tool": PLAN_NCS_EDUCATION_PATH_TOOL,
        "required_response_fields": [
            "training_system_matrix",
            "delivery_operation",
            "facility_constraint_fit",
            "target_population",
            "human_review",
        ],
    },
    {
        "id": "training_course_inventory_table",
        "guide_stage": ["C1-1", "C2-2"],
        "prompt_intent": "organize investigated internal and external training courses, survey results, source, provider, type, and classification display fields; \uc870\uc0ac\uacb0\uacfc \uc815\ub9ac \ub0b4\ubd80 \uc678\ubd80 \uad6c\ubd84 \ud45c\uc2dc",
        "korean_example": "\uc870\uc0ac\ub41c \uad50\uc721\uacfc\uc815\uc744 \ub0b4\ubd80/\uc678\ubd80 \uad6c\ubd84\uacfc \uad50\uc721\uc720\ud615 \uae30\uc900\uc73c\ub85c \uc815\ub9ac\ud574\uc918.",
        "expected_tool": PLAN_NCS_EDUCATION_PATH_TOOL,
        "required_response_fields": [
            "training_course_inventory_template",
            "training_system_matrix",
            "education_type",
            "delivery_operation",
            "target_population",
            "human_review",
        ],
    },
    {
        "id": "internal_training_intake_questionnaire",
        "guide_stage": ["C1-1"],
        "prompt_intent": "create interview questions for internal training course investigation including course name, target, purpose, and delivery method",
        "korean_example": "\ub0b4\ubd80 \uad50\uc721\uacfc\uc815 \uc218\uc9d1\uc744 \uc704\ud574 \uad50\uc721\uba85\u00b7\ub300\uc0c1\u00b7\ubaa9\uc801\u00b7\uc6b4\uc601\ubc29\uc2dd \uc870\uc0ac \uc9c8\ubb38\uc9c0\ub97c \ub9cc\ub4e4\uc5b4\uc918.",
        "expected_tool": PLAN_NCS_EDUCATION_PATH_TOOL,
        "required_response_fields": [
            "course_intake_requirements",
            "training_course_inventory_template",
            "target_population",
            "delivery_operation",
            "training_system_guide_trace",
            "human_review",
        ],
    },
    {
        "id": "job_structure_mapping",
        "guide_stage": ["C1-1"],
        "prompt_intent": "find NCS structure candidates from job functions, duties, or job information",
        "korean_example": "\uc9c1\ubb34\uae30\ub2a5\uacfc \uc8fc\uc694\uc5c5\ubb34\ub97c \uae30\uc900\uc73c\ub85c NCS \uc9c1\ubb34\ubd84\ub958 \ud6c4\ubcf4\ub97c \ucc3e\uc544\uc918.",
        "expected_tool": "ncs_search",
        "required_response_fields": [
            "query_route",
            "job_scope",
            "classification_candidates",
            "mapping_basis",
            "human_review",
        ],
    },
    {
        "id": "job_course_mapping_framework",
        "guide_stage": ["C1-1", "C1-2"],
        "prompt_intent": "design a job education mapping framework based on job, task, KSA, grade, and required level",
        "korean_example": "\uc544\ub798 \uc9c1\ubb34\u00b7\uacfc\uc5c5\u00b7KSA \ud45c\ub97c \uae30\ubc18\uc73c\ub85c \uad50\uc721 \ub9e4\ud551 \uae30\uc900 \ud504\ub808\uc784\uc744 \uc124\uacc4\ud574\uc918.",
        "expected_tool": "recommend_training_for_task",
        "required_response_fields": [
            "scope",
            "task_ksa_basis",
            "course_link",
            "required_optional_basis",
            "human_review",
        ],
    },
    {
        "id": "course_ksa_alignment",
        "guide_stage": ["C1-1", "C1-2"],
        "prompt_intent": "align a training course with job, task, knowledge, skill, attitude, and required level evidence",
        "korean_example": "\uc774 \uad50\uc721\uacfc\uc815\uc774 \ucda9\uc871\uc2dc\ud0a4\ub294 \uc9c0\uc2dd\u00b7\uae30\uc220\u00b7\ud0dc\ub3c4\ub97c \ubd84\uc11d\ud574\uc918.",
        "expected_tool": "recommend_training_for_task",
        "required_response_fields": [
            "scope",
            "task_ksa_basis",
            "course_link",
            "course_fit.level",
            "human_review",
        ],
    },
    {
        "id": "ncs_mapping_evidence_summary",
        "guide_stage": ["C1-1"],
        "prompt_intent": "summarize evidence for mapping a job list to NCS classifications or units",
        "korean_example": "\uc9c1\ubb34 \ubaa9\ub85d\uc744 NCS \ubd84\ub958\uc640 \ub9e4\ud551\ud55c \uadfc\uac70\ub97c \uc815\ub9ac\ud574\uc918.",
        "expected_tool": "ncs_analysis",
        "required_response_fields": [
            "query_route",
            "source_scope",
            "evidence_basis",
            "mapping_caveats",
            "human_review",
        ],
    },
)

GUIDE_ACCEPTANCE_GATES: dict[str, Any] = {
    "blockers": [
        {
            "id": "missing_prompt_template_required_field",
            "description": "A guide prompt scenario output is missing one of its required response fields.",
        },
        {
            "id": "missing_guide_trace",
            "description": (
                "training_system_guide_trace is missing, has the wrong schema, or lacks one "
                "of the six required checks."
            ),
        },
        {
            "id": "missing_plan_or_matrix_contract",
            "description": (
                "recommended_path or training_system_matrix is missing required planner fields "
                "for guide-aligned education-system design."
            ),
        },
        {
            "id": "missing_query_route_contract",
            "description": (
                "Live planner output is missing query_route, route_contract, expected_tool_chain, "
                "or route_fingerprint."
            ),
        },
        {
            "id": "guide_used_as_source_training_data",
            "description": (
                "Guide examples are used as source training data, score-boosting facts, or "
                "authority for official approval."
            ),
        },
        {
            "id": "automatic_human_decision_status",
            "description": "Automation writes human_reviewed, accepted, or reviewed without explicit human decision.",
        },
    ],
    "warnings": [
        {
            "id": "unknown_or_not_requested_facility",
            "description": (
                "facility_constraint_fit is unknown or not_requested without a direct conflict. "
                "This is a review state, not a hard failure."
            ),
        },
        {
            "id": "generic_or_duplicate_course",
            "description": "A specificity or duplicate/generic warning is present and should be surfaced to reviewers.",
        },
        {
            "id": "course_title_heavy_evidence",
            "description": (
                "Evidence relies heavily on course title. It becomes a blocker only when task/KSA "
                "and course-link evidence are absent."
            ),
        },
    ],
}


def default_hrd_guide_reference_dir() -> Path:
    return PROJECT_ROOT / "docs" / "reference"


def default_hrd_guide_reference_index_path() -> Path:
    return default_hrd_guide_reference_dir() / DEFAULT_HRD_GUIDE_INDEX_FILENAME


def fallback_hrd_guide_reference_index() -> dict[str, Any]:
    return {
        "schema": HRD_GUIDE_REFERENCE_SCHEMA,
        "generated_at": None,
        "source": {
            "original_path": None,
            "project_copy_path": _display_reference_path(
                default_hrd_guide_reference_dir() / DEFAULT_HRD_GUIDE_RAW_FILENAME
            ),
            "source_hash_sha256": BUILTIN_HRD_GUIDE_SOURCE_HASH_SHA256,
            "source_hash_origin": "built_in_framework_reference_metadata",
            "encoding": None,
            "bytes": 0,
            "front_matter": {},
            "page_count": BUILTIN_HRD_GUIDE_PAGE_COUNT,
            "available": False,
        },
        "policy": {
            "reference_role": "framework_reference",
            "not_source_training_data": True,
            "scoring_use": "validation_rubric_only",
            "forbidden_uses": [
                "Do not insert guide examples into source tables.",
                "Do not increase recommendation scores only because of the guide.",
                "Do not claim official qualification, approval, or legal eligibility.",
            ],
        },
        "guide_workflow": list(GUIDE_WORKFLOW_STAGES),
        "guide_trace_contract": {
            "schema": AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
            "checks": list(GUIDE_TRACE_CHECKS),
        },
        "acceptance_gates": GUIDE_ACCEPTANCE_GATES,
        "development_rules": list(DEVELOPMENT_RULES),
        "prompt_scenario_templates": list(PROMPT_SCENARIO_TEMPLATES),
        "prompt_examples": [],
        "page_index": [],
    }


def load_hrd_guide_reference_index(index_path: Path | None = None) -> dict[str, Any]:
    path = index_path or default_hrd_guide_reference_index_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback_hrd_guide_reference_index()
    if not isinstance(data, dict) or data.get("schema") != HRD_GUIDE_REFERENCE_SCHEMA:
        return fallback_hrd_guide_reference_index()
    return data


def hrd_guide_reference_metadata(index: dict[str, Any] | None = None) -> dict[str, Any]:
    data = index or load_hrd_guide_reference_index()
    source = data.get("source") if isinstance(data.get("source"), dict) else {}
    policy = data.get("policy") if isinstance(data.get("policy"), dict) else {}
    return {
        "schema": data.get("schema"),
        "reference_role": policy.get("reference_role", "framework_reference"),
        "not_source_training_data": policy.get("not_source_training_data", True),
        "scoring_use": policy.get("scoring_use", "validation_rubric_only"),
        "index_path": _display_reference_path(default_hrd_guide_reference_index_path()),
        "project_copy_path": source.get("project_copy_path"),
        "source_hash_sha256": source.get("source_hash_sha256"),
        "page_count": source.get("page_count", 0),
    }


def hrd_guide_trace_check_codes(index: dict[str, Any] | None = None) -> list[str]:
    data = index or load_hrd_guide_reference_index()
    contract = data.get("guide_trace_contract") if isinstance(data.get("guide_trace_contract"), dict) else {}
    checks = contract.get("checks") if isinstance(contract.get("checks"), list) else []
    return [str(item.get("check")) for item in checks if isinstance(item, dict) and item.get("check")]


def hrd_guide_workflow_stage_codes(index: dict[str, Any] | None = None) -> list[str]:
    data = index or load_hrd_guide_reference_index()
    workflow = data.get("guide_workflow") if isinstance(data.get("guide_workflow"), list) else []
    return [str(item.get("code")) for item in workflow if isinstance(item, dict) and item.get("code")]


def hrd_guide_prompt_scenario_templates(index: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    data = index or load_hrd_guide_reference_index()
    templates = data.get("prompt_scenario_templates")
    if not isinstance(templates, list):
        return []
    return [dict(item) for item in templates if isinstance(item, dict)]


def match_hrd_guide_prompt_template(
    query: str,
    *,
    tool: str | None = None,
    scenario: str | None = None,
    index: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    normalized = _normalize_match_text(query)
    if not normalized:
        return None
    scenario_by_template = {
        "education_system_from_transition": "education_system_design",
        "annual_operation_plan_draft": "education_system_design",
        "training_course_inventory_table": "education_system_design",
        "internal_training_intake_questionnaire": "education_system_design",
        "task_based_course_recommendation": "task_training",
        "job_course_mapping_framework": "task_training",
        "course_ksa_alignment": "task_training",
        "training_transition_gap_analysis": "training_transition",
        "job_structure_mapping": "structure_search",
        "ncs_mapping_evidence_summary": "evidence_analysis",
    }
    best: tuple[int, dict[str, Any]] | None = None
    for template in hrd_guide_prompt_scenario_templates(index):
        expected_tool = str(template.get("expected_tool") or "")
        template_scenario = scenario_by_template.get(str(template.get("id") or ""))
        if tool and expected_tool and expected_tool != tool:
            continue
        if scenario and template_scenario and template_scenario != scenario:
            continue
        score = 0
        if tool and expected_tool == tool:
            score += 4
        if scenario and template_scenario == scenario:
            score += 4
        matched_terms: list[str] = []
        for text_field in ("prompt_intent", "korean_example"):
            for token in _match_tokens(template.get(text_field)):
                if _token_matches(normalized, token) and token not in matched_terms:
                    matched_terms.append(token)
                    score += 2 if len(token) >= 4 else 1
        for required_field in template.get("required_response_fields") or []:
            token = _normalize_match_text(required_field)
            if token and token in normalized and token not in matched_terms:
                matched_terms.append(token)
                score += 1
        if score >= 6 and matched_terms:
            result = {
                "id": template.get("id"),
                "guide_stage": template.get("guide_stage") or [],
                "expected_tool": expected_tool,
                "required_response_fields": template.get("required_response_fields") or [],
                "prompt_intent": template.get("prompt_intent"),
                "match_score": score,
                "matched_terms": matched_terms[:12],
            }
            if best is None or score > best[0]:
                best = (score, result)
    return best[1] if best else None


def _normalize_match_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def _match_tokens(value: Any) -> list[str]:
    text = _normalize_match_text(value)
    if not text:
        return []
    raw_tokens = re.split(r"[\s,.;:()\[\]{}<>/\\|\"']+", text)
    tokens: list[str] = []
    for token in raw_tokens:
        cleaned = token.strip("?!~`")
        if len(cleaned) >= 3 and cleaned not in tokens:
            tokens.append(cleaned)
    return tokens


def _token_matches(normalized: str, token: str) -> bool:
    if not token:
        return False
    if re.fullmatch(r"[a-z0-9_-]+", token):
        return bool(re.search(rf"(?<![a-z0-9_-]){re.escape(token)}(?![a-z0-9_-])", normalized))
    return token in normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_text_with_encoding(path: Path) -> tuple[str, str, bytes]:
    raw = path.read_bytes()
    best_text = ""
    best_encoding = "utf-8"
    best_score = -1
    for encoding in ("utf-8-sig", "utf-8", "cp949"):
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            text = raw.decode(encoding, errors="replace")
        score = len(text) - (text.count("\ufffd") * 200) - (text.count("?") * 2)
        if score > best_score:
            best_text = text
            best_encoding = encoding
            best_score = score
    return best_text, best_encoding, raw


def _parse_front_matter(text: str) -> dict[str, str]:
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}
    metadata: dict[str, str] = {}
    for raw_line in match.group("body").splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip('"')
    return metadata


def _section_for_page(page_number: int | None) -> str:
    if page_number is None:
        return "unknown"
    if 1 <= page_number <= 2:
        return "front_matter"
    if 3 <= page_number <= 7:
        return "guide_overview"
    if 8 <= page_number <= 16:
        return "ncs_concept_and_structure"
    if 17 <= page_number <= 56:
        return "job_system_building"
    if 57 <= page_number <= 94:
        return "training_system_building"
    return "unknown"


def _stage_candidates(page_number: int | None, text: str) -> list[str]:
    candidates: list[str] = []
    haystack = text.lower()
    if page_number is not None and 57 <= page_number <= 94:
        candidates.extend(["C1-1", "C1-2", "C2-1", "C2-2"])
    marker_map = {
        "C1-1": ("c1-1", "교육과정 조사", "직무기반 매핑"),
        "C1-2": ("c1-2", "필요성 검토"),
        "C2-1": ("c2-1", "교육훈련체계도"),
        "C2-2": ("c2-2", "운영계획"),
    }
    for stage, markers in marker_map.items():
        if any(marker.lower() in haystack for marker in markers) and stage not in candidates:
            candidates.append(stage)
    return candidates


def _clean_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def _snippet(text: str, limit: int = 240) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."


def split_markdown_pages(text: str) -> list[dict[str, Any]]:
    markers = list(PAGE_MARKER_RE.finditer(text))
    pages: list[dict[str, Any]] = []
    if not markers:
        heading_match = PAGE_HEADING_RE.search(text)
        title = heading_match.group("title").strip() if heading_match else "Document"
        pages.append(
            {
                "page_number": None,
                "title": title,
                "text": text.strip(),
            }
        )
        return pages

    for index, marker in enumerate(markers):
        start = marker.end()
        end = markers[index + 1].start() if index + 1 < len(markers) else len(text)
        block = text[start:end].strip()
        page_number = int(marker.group("page"))
        heading = PAGE_HEADING_RE.search(block)
        title = heading.group("title").strip() if heading else f"Page {page_number:03d}"
        if heading:
            block = block[heading.end() :].strip()
        pages.append(
            {
                "page_number": page_number,
                "title": title,
                "text": block,
            }
        )
    return pages


def extract_prompt_like_examples(pages: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page in pages:
        page_text = str(page.get("text") or "")
        compact_page_text = _clean_line(page_text)
        for quote_match in PROMPT_QUOTE_RE.finditer(compact_page_text):
            quote = _clean_line(quote_match.group("quote"))
            if not _looks_like_prompt_example(quote):
                continue
            if _append_prompt_example(
                examples,
                seen,
                page=page,
                snippet=quote,
                extraction_method="quoted_prompt",
                limit=limit,
            ):
                return examples
        lines = [_clean_line(line) for line in page_text.splitlines()]
        for line_index, line in enumerate(lines):
            if not line or len(line) < 8:
                continue
            if PROMPT_QUOTE_RE.search(line):
                continue
            has_context = bool(PROMPT_CONTEXT_RE.search(line))
            if not has_context and not _looks_like_prompt_example(line):
                continue
            window = [candidate for candidate in lines[max(0, line_index - 1) : line_index + 3] if candidate]
            snippet = _snippet(" ".join(window), limit=420)
            if not _looks_like_prompt_example(snippet):
                continue
            if _append_prompt_example(
                examples,
                seen,
                page=page,
                snippet=snippet,
                extraction_method="prompt_context_window",
                limit=limit,
            ):
                return examples
    return examples


def _looks_like_prompt_example(text: str) -> bool:
    cleaned = _clean_line(text)
    if len(cleaned) < 8:
        return False
    return bool(PROMPT_COMMAND_RE.search(cleaned))


def _append_prompt_example(
    examples: list[dict[str, Any]],
    seen: set[str],
    *,
    page: dict[str, Any],
    snippet: str,
    extraction_method: str,
    limit: int,
) -> bool:
    compact = _snippet(snippet, limit=420)
    key = compact.casefold()
    if key in seen:
        return False
    seen.add(key)
    examples.append(
        {
            "page_number": page.get("page_number"),
            "section": page.get("section"),
            "stage_candidates": page.get("stage_candidates", []),
            "snippet": compact,
            "extraction_method": extraction_method,
            "usage_policy": "Use as prompt coverage input only; do not treat guide examples as source training data.",
        }
    )
    return len(examples) >= limit


def build_page_index(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: list[dict[str, Any]] = []
    for page in pages:
        page_number = page.get("page_number")
        page_text = str(page.get("text") or "")
        section = _section_for_page(page_number)
        stage_candidates = _stage_candidates(page_number, page_text)
        prompt_markers = len(PROMPT_MARKER_RE.findall(page_text))
        indexed_page = {
            "page_number": page_number,
            "title": page.get("title") or "",
            "section": section,
            "stage_candidates": stage_candidates,
            "char_count": len(page_text),
            "prompt_marker_count": prompt_markers,
            "summary": _snippet(page_text, limit=320),
            "text": page_text,
        }
        indexed.append(indexed_page)
    return indexed


def build_chunks(page_index: list[dict[str, Any]], *, chunk_chars: int, overlap_chars: int) -> list[dict[str, Any]]:
    chunk_chars = max(400, int(chunk_chars))
    overlap_chars = max(0, min(int(overlap_chars), chunk_chars // 2))
    chunks: list[dict[str, Any]] = []
    chunk_id = 1
    for page in page_index:
        text = str(page.get("text") or "")
        if not text:
            continue
        start = 0
        while start < len(text):
            end = min(len(text), start + chunk_chars)
            chunks.append(
                {
                    "chunk_id": f"hrd-guide-{chunk_id:05d}",
                    "page_number": page.get("page_number"),
                    "section": page.get("section"),
                    "stage_candidates": page.get("stage_candidates", []),
                    "start_char": start,
                    "end_char": end,
                    "text": text[start:end].strip(),
                    "usage_policy": "framework_reference_only",
                    "retrieval_scope": "manual_reference_or_validation_only",
                    "source_data_policy": {
                        "reference_role": "framework_reference",
                        "not_source_training_data": True,
                        "scoring_allowed": False,
                        "evidence_weight": 0,
                        "allowed_uses": [
                            "prompt_coverage",
                            "validation_rubric",
                            "manual_reference",
                        ],
                        "forbidden_uses": [
                            "source_training_data",
                            "recommendation_score_boost",
                            "official_approval_or_eligibility_claim",
                            "automatic_human_review_status",
                        ],
                    },
                }
            )
            chunk_id += 1
            if end >= len(text):
                break
            start = max(end - overlap_chars, start + 1)
    return chunks


def build_hrd_guide_reference_index(
    *,
    source_path: Path,
    project_copy_path: Path,
    text: str,
    raw_bytes: bytes,
    encoding: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    pages = split_markdown_pages(text)
    page_index = build_page_index(pages)
    prompt_examples = extract_prompt_like_examples(page_index)
    return {
        "schema": HRD_GUIDE_REFERENCE_SCHEMA,
        "generated_at": generated_at or _utc_now(),
        "source": {
            "original_path": _display_reference_path(source_path),
            "project_copy_path": _display_reference_path(project_copy_path),
            "absolute_paths_redacted": True,
            "source_hash_sha256": _sha256(raw_bytes),
            "encoding": encoding,
            "bytes": len(raw_bytes),
            "front_matter": _parse_front_matter(text),
            "page_count": len(page_index),
        },
        "policy": {
            "reference_role": "framework_reference",
            "not_source_training_data": True,
            "scoring_use": "validation_rubric_only",
            "forbidden_uses": [
                "Do not insert guide sample organizations, hotel examples, or sample course rows into source tables.",
                "Do not increase recommendation scores only because a course resembles a guide example.",
                "Do not claim official qualification, approval, or legal eligibility from this guide.",
            ],
        },
        "guide_workflow": list(GUIDE_WORKFLOW_STAGES),
        "guide_trace_contract": {
            "schema": AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
            "checks": list(GUIDE_TRACE_CHECKS),
        },
        "acceptance_gates": GUIDE_ACCEPTANCE_GATES,
        "development_rules": list(DEVELOPMENT_RULES),
        "prompt_scenario_templates": list(PROMPT_SCENARIO_TEMPLATES),
        "prompt_examples": prompt_examples,
        "page_index": [
            {key: value for key, value in page.items() if key != "text"}
            for page in page_index
        ],
    }


def render_hrd_guide_reference_markdown(index: dict[str, Any], chunks_path: Path) -> str:
    chunks_display_path = _display_reference_path(chunks_path)
    lines: list[str] = [
        "# NCS HRD Guide Reference",
        "",
        "This file is generated by `python scripts\\ncs_harness.py preprocess-hrd-guide-reference`.",
        "Use it as the local development index for the 2026 NCS HR practical guide.",
        "",
        "## Source",
        "",
        f"- Original path: `{index['source']['original_path']}`",
        f"- Project copy: `{index['source']['project_copy_path']}`",
        f"- SHA-256: `{index['source']['source_hash_sha256']}`",
        f"- Pages indexed: `{index['source']['page_count']}`",
        f"- Chunks JSONL: `{chunks_display_path}`",
        "",
        "## Policy",
        "",
        "- Role: `framework_reference`",
        "- Use: validation and planning rubric only.",
        "- Do not use guide examples as source training data or score-boosting facts.",
        "- Do not mark `human_reviewed`, `accepted`, or `reviewed` without an explicit human decision.",
        "",
        "## Guide Workflow",
        "",
        "| Stage | Product Contract | Required Output |",
        "| --- | --- | --- |",
    ]
    for stage in index["guide_workflow"]:
        lines.append(
            "| {code} {name} | {contract} | {outputs} |".format(
                code=stage["code"],
                name=stage["korean_name"],
                contract=stage["product_contract"],
                outputs=", ".join(f"`{item}`" for item in stage["required_output"]),
            )
        )

    lines.extend(
        [
            "",
            "## Guide Trace Contract",
            "",
            "| Check | Stage | Description |",
            "| --- | --- | --- |",
        ]
    )
    for check in index["guide_trace_contract"]["checks"]:
        lines.append(f"| `{check['check']}` | {check['stage']} | {check['description']} |")

    lines.extend(
        [
            "",
            "## Prompt Scenario Coverage",
            "",
            "| Scenario | Expected Tool | Required Fields | Example Intent |",
            "| --- | --- | --- | --- |",
        ]
    )
    for scenario in index["prompt_scenario_templates"]:
        lines.append(
            "| `{id}` | `{tool}` | {fields} | {example} |".format(
                id=scenario["id"],
                tool=scenario["expected_tool"],
                fields=", ".join(f"`{field}`" for field in scenario["required_response_fields"]),
                example=scenario["korean_example"],
            )
        )

    lines.extend(
        [
            "",
            "## Acceptance Gates",
            "",
            "### Blockers",
            "",
        ]
    )
    for blocker in index["acceptance_gates"]["blockers"]:
        lines.append(f"- `{blocker['id']}`: {blocker['description']}")
    lines.extend(["", "### Warnings", ""])
    for warning in index["acceptance_gates"]["warnings"]:
        lines.append(f"- `{warning['id']}`: {warning['description']}")

    if index["prompt_examples"]:
        lines.extend(["", "## Extracted Prompt-Like Snippets", ""])
        for example in index["prompt_examples"][:20]:
            page = example.get("page_number") or "unknown"
            lines.append(f"- Page `{page}`: {example['snippet']}")

    lines.extend(
        [
            "",
            "## Page Index",
            "",
            "| Page | Section | Stage Candidates | Prompt Markers | Title |",
            "| --- | --- | --- | ---: | --- |",
        ]
    )
    for page in index["page_index"]:
        lines.append(
            "| {page} | `{section}` | {stages} | {markers} | {title} |".format(
                page=page.get("page_number") or "",
                section=page.get("section") or "",
                stages=", ".join(f"`{stage}`" for stage in page.get("stage_candidates", [])),
                markers=page.get("prompt_marker_count", 0),
                title=page.get("title") or "",
            )
        )
    lines.append("")
    return "\n".join(lines)


def _display_reference_path(path: Path) -> str:
    try:
        resolved = path.expanduser().resolve()
    except OSError:
        resolved = path
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return f"<external>/{resolved.name}"


def preprocess_hrd_guide_reference(
    *,
    source_path: Path,
    reference_dir: Path,
    index_path: Path,
    markdown_path: Path,
    chunks_path: Path,
    copy_source: bool = True,
    chunk_chars: int = 2200,
    overlap_chars: int = 250,
) -> dict[str, Any]:
    source_path = source_path.expanduser().resolve()
    reference_dir = reference_dir.resolve()
    index_path = index_path.resolve()
    markdown_path = markdown_path.resolve()
    chunks_path = chunks_path.resolve()
    if not source_path.exists():
        raise FileNotFoundError(f"HRD guide Markdown not found: {source_path}")

    reference_dir.mkdir(parents=True, exist_ok=True)
    project_copy_path = reference_dir / DEFAULT_HRD_GUIDE_RAW_FILENAME
    source_bytes = source_path.read_bytes()
    copied = False
    if copy_source:
        if source_path != project_copy_path:
            project_copy_path.write_bytes(source_bytes)
            copied = True
        elif not project_copy_path.exists():
            project_copy_path.write_bytes(source_bytes)
        read_path = project_copy_path
    else:
        read_path = source_path
    text, encoding, raw_bytes = _read_text_with_encoding(read_path)
    generated_at = _utc_now()
    page_index_with_text = build_page_index(split_markdown_pages(text))
    index = build_hrd_guide_reference_index(
        source_path=source_path,
        project_copy_path=read_path,
        text=text,
        raw_bytes=raw_bytes,
        encoding=encoding,
        generated_at=generated_at,
    )
    chunks = build_chunks(page_index_with_text, chunk_chars=chunk_chars, overlap_chars=overlap_chars)

    index_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    chunks_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with chunks_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False, sort_keys=True) + "\n")
    markdown_path.write_text(render_hrd_guide_reference_markdown(index, chunks_path), encoding="utf-8")

    return {
        "ok": True,
        "schema": HRD_GUIDE_REFERENCE_SCHEMA,
        "generated_at": generated_at,
        "source_path": _display_reference_path(source_path),
        "project_copy_path": _display_reference_path(read_path),
        "copied_source": copied,
        "index_path": _display_reference_path(index_path),
        "markdown_path": _display_reference_path(markdown_path),
        "chunks_path": _display_reference_path(chunks_path),
        "page_count": index["source"]["page_count"],
        "chunk_count": len(chunks),
        "prompt_examples_count": len(index["prompt_examples"]),
        "prompt_scenario_templates_count": len(index["prompt_scenario_templates"]),
        "policy": index["policy"],
        "guide_trace_checks": [
            check["check"] for check in index["guide_trace_contract"]["checks"]
        ],
    }
