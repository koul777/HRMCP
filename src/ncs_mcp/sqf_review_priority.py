from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


SQF_REVIEW_PRIORITY_SCHEMA = "ncs_sqf_review_priority_v1"
SQF_REVIEW_PRIORITY_ITEM_RECORD = "sqf_report_claim_review_priority"
PRIORITY_ORDER = ["P0", "P1", "P2", "P3", "reject_review"]
PRIORITY_SORT = {priority: index for index, priority in enumerate(PRIORITY_ORDER)}
REPORT_ONLY_GUARDRAILS = {
    "used_for_scoring": False,
    "status_update_allowed": False,
    "approval_claim": False,
}
MAX_MARKDOWN_ITEMS = 80
MAX_TEXT_CHARS = 360

ADJACENT_JOB_KEYWORDS = [
    "인사",
    "회계",
    "재무",
    "세무",
    "노무",
    "hr",
    "human resources",
    "accounting",
    "finance",
    "tax",
    "labor",
    "?몄궗",
    "?뚭퀎",
    "?щТ",
    "?몃Т",
]

FINANCE_TAX_KEYWORDS = [
    "재무",
    "세무",
    "finance",
    "tax",
    "?щТ",
    "?몃Т",
]

BROAD_SCOPE_KEYWORDS = [
    "경영",
    "총무",
    "일반사무",
    "사무",
    "관리",
    "기획",
    "business",
    "management",
    "administration",
    "general affairs",
    "寃쎌쁺",
    "珥앸Т",
]

RELATED_ONLY_RELATIONS = {"related", "relatedmatch", "relatedonly"}
STRONG_EVIDENCE_RELATION = "strongevidence"
SUPPORTING_EVIDENCE_RELATION = "supportingevidence"
CLOSE_MATCH_RELATION = "closematch"
PARTIAL_RELATION = "partiallycovers"


def parse_level(value: Any) -> int | None:
    """Parse SQF/NCS level values such as 6, "L6", "NCS 5수준", or "인사(6)"."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= 20 else None
    if isinstance(value, float):
        if value.is_integer():
            as_int = int(value)
            return as_int if 0 <= as_int <= 20 else None
        return None

    text = str(value).strip()
    if not text:
        return None
    for token in re.findall(r"\d+", text):
        parsed = int(token)
        if 0 <= parsed <= 20:
            return parsed
    return None


def prioritize_sqf_claim(claim: dict[str, Any]) -> dict[str, Any]:
    analysis = _claim_analysis(claim)
    priority, reasons = _source_or_classified_priority(claim, analysis)
    sqf = claim.get("sqf") or {}
    ncs = claim.get("ncs_candidate") or {}
    match = claim.get("sqf_ncs_match") or {}
    basis = claim.get("basis_strength") or {}

    return {
        "record_type": SQF_REVIEW_PRIORITY_ITEM_RECORD,
        "schema": SQF_REVIEW_PRIORITY_SCHEMA,
        "claim_id": claim.get("claim_id"),
        "sequence": claim.get("sequence"),
        "source_seedpack_sequence": claim.get("source_seedpack_sequence"),
        "priority": priority,
        "priority_sort": PRIORITY_SORT[priority],
        "priority_reasons": reasons,
        "review_status": "candidate_requires_human_review",
        "review_gate": "explicit_human_decision_required",
        "approval_ready": False,
        "status": "review_required",
        "decision": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "rationale": "",
        **REPORT_ONLY_GUARDRAILS,
        "allowed_use": "supplementary_review_context_only",
        "import_policy": "guarded_human_import_only",
        "claim": {
            "claim_status": claim.get("claim_status"),
            "claim_type": claim.get("claim_type"),
            "claim_statement": _trim_text(claim.get("claim_statement")),
            "source_priority_score": _as_float(claim.get("priority_score")),
        },
        "scope": {
            "target_major_code": analysis["major_code"],
            "cross_major": analysis["cross_major"],
            "adjacent_job_keyword": analysis["has_adjacent_job_keyword"],
            "finance_tax_reference": analysis["has_finance_tax_keyword"],
            "broad_scope_reference": analysis["has_broad_scope_keyword"],
            "sqf_job_name": sqf.get("job_name"),
            "sqf_duty_name": sqf.get("duty_name"),
            "ncs_unit_code": ncs.get("unit_code"),
            "ncs_unit_name": ncs.get("unit_name"),
        },
        "level_fit": {
            "sqf_level": analysis["sqf_level"],
            "ncs_unit_level": analysis["ncs_level"],
            "level_gap": analysis["level_gap"],
        },
        "relation": {
            "mapping_relation": analysis["mapping_relation"],
            "match_score": _as_float(match.get("score")),
            "mapping_score": _as_float(basis.get("mapping_score")),
            "has_close_match": analysis["has_close_match"],
            "has_partial": analysis["has_partial"],
            "related_only": analysis["related_only"],
        },
        "evidence": {
            "report_evidence_count": analysis["report_evidence_count"],
            "evidence_relations": analysis["evidence_relations"],
            "has_report_evidence": analysis["has_report_evidence"],
            "has_strong_evidence": analysis["has_strong_evidence"],
            "has_supporting_evidence": analysis["has_supporting_evidence"],
            "max_report_score": _as_float(basis.get("max_report_score")),
            "evidence_refs": analysis["evidence_refs"],
        },
        "review_action_bundle": _priority_review_action_bundle(
            claim=claim,
            ncs=ncs,
            analysis=analysis,
            priority=priority,
            reasons=reasons,
        ),
    }


def _priority_review_action_bundle(
    *,
    claim: dict[str, Any],
    ncs: dict[str, Any],
    analysis: dict[str, Any],
    priority: str,
    reasons: list[str],
) -> dict[str, Any]:
    classification = ncs.get("classification") or {}
    return {
        "claim_id": claim.get("claim_id"),
        "claim_type": claim.get("claim_type"),
        "ncs_scope": {
            "unit_code": ncs.get("unit_code"),
            "unit_name": ncs.get("unit_name"),
            "unit_level": ncs.get("api_unit_level") or ncs.get("unit_level"),
            "major_code": classification.get("major_code") or analysis.get("major_code"),
            "middle_code": classification.get("middle_code"),
            "small_code": classification.get("small_code"),
            "sub_code": classification.get("sub_code"),
        },
        "evidence_strength": _priority_evidence_strength(analysis),
        "review_risk_flags": _priority_review_risk_flags(analysis, priority=priority),
        "decision_facets": {
            "approve_for_reference": {
                "decision": "approve",
                "effect": "pre_import_annotation_only",
                "requires": ["reviewer_id", "reviewed_at", "reason", "source_packet", "top_evidence_refs"],
            },
            "reject": {
                "decision": "reject",
                "effect": "exclude_from_sqf_review_context",
                "requires": ["reviewer_id", "reviewed_at", "reason"],
            },
            "needs_domain_context": {
                "decision": "defer",
                "effect": "keep_pending_for_subject_matter_review",
                "requires": ["reviewer_id", "reviewed_at", "reason"],
            },
        },
        "human_notes_prompt": (
            "Check work-scope fit, level fit, report grounding, and whether SQF should remain "
            "supplementary review context only."
        ),
        "blocking_rules": {
            "status_update_allowed": False,
            "mutates_scoring": False,
            "saves_review_state": False,
            "requires_guarded_import": True,
            "requires_operator_status_mapping_policy": True,
        },
        "diagnostics": {
            "priority": priority,
            "priority_reasons": reasons,
            "mapping_relation": analysis["mapping_relation"],
            "level_gap": analysis["level_gap"],
        },
    }


def prioritize_sqf_claim_candidates(
    candidate_payload: dict[str, Any] | list[dict[str, Any]],
    *,
    source_path: str | Path | None = None,
) -> dict[str, Any]:
    claims = _claim_records(candidate_payload)
    items = [prioritize_sqf_claim(claim) for claim in claims]
    items.sort(key=_priority_item_sort_key)
    priority_counts = Counter(item["priority"] for item in items)
    reject_reason_counts = Counter(
        reason for item in items if item["priority"] == "reject_review" for reason in item["priority_reasons"]
    )
    source_batch = candidate_payload.get("batch") if isinstance(candidate_payload, dict) else {}
    source_batch = source_batch or {}
    source_guardrail_issues = _source_guardrail_issues(candidate_payload)

    return {
        "ok": not source_guardrail_issues,
        "schema": SQF_REVIEW_PRIORITY_SCHEMA,
        "status": "review_required",
        "approval_ready": False,
        **REPORT_ONLY_GUARDRAILS,
        "source": {
            "name": Path(source_path).name if source_path is not None else None,
            "format_version": source_batch.get("format_version"),
            "claim_batch_id": source_batch.get("claim_batch_id"),
            "source_seedpack_id": source_batch.get("source_seedpack_id"),
            "source_claim_count": source_batch.get("claim_count"),
            "loaded_claim_count": len(claims),
        },
        "review_policy": {
            "report_only": True,
            "db_writes": False,
            "requires_explicit_human_decision": True,
            "allowed_decisions": ["approve", "reject", "defer"],
            "prohibited_auto_statuses": ["human_reviewed", "accepted", "reviewed"],
            **REPORT_ONLY_GUARDRAILS,
        },
        "summary": {
            "claim_count": len(items),
            "priority_counts": {priority: priority_counts.get(priority, 0) for priority in PRIORITY_ORDER},
            "reject_reason_counts": dict(sorted(reject_reason_counts.items())),
            "source_guardrail_issues": source_guardrail_issues,
            "source_guardrail_issue_count": len(source_guardrail_issues),
        },
        "priority_groups": {
            priority: {
                "count": priority_counts.get(priority, 0),
                "claim_ids": [item.get("claim_id") for item in items if item["priority"] == priority],
            }
            for priority in PRIORITY_ORDER
        },
        "items": items,
        "notes": [
            "This priority report is report-only and must not update the DB.",
            "SQF report evidence is supplementary review context, not scored training evidence.",
            "Do not write human_reviewed, accepted, or reviewed without an explicit human decision.",
        ],
    }


def prioritize_sqf_claim_candidates_from_file(input_path: str | Path) -> dict[str, Any]:
    path = Path(input_path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return prioritize_sqf_claim_candidates(payload, source_path=path)


def write_sqf_review_priority_json(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_sqf_review_priority_markdown(
    report: dict[str, Any],
    out_path: str | Path,
    *,
    max_items: int = MAX_MARKDOWN_ITEMS,
) -> None:
    path = Path(out_path)
    summary = report.get("summary") or {}
    priority_counts = summary.get("priority_counts") or {}
    lines = [
        "# SQF Review Priority",
        "",
        f"- ok: {str(report.get('ok')).lower()}",
        f"- schema: {report.get('schema')}",
        f"- status: {report.get('status')}",
        f"- approval_ready: {str(report.get('approval_ready')).lower()}",
        f"- used_for_scoring: {str(report.get('used_for_scoring')).lower()}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        f"- approval_claim: {str(report.get('approval_claim')).lower()}",
        f"- claim_count: {summary.get('claim_count', 0)}",
        "",
        "## Priority Counts",
        "",
    ]
    for priority in PRIORITY_ORDER:
        lines.append(f"- {priority}: {priority_counts.get(priority, 0)}")

    guardrail_issues = summary.get("source_guardrail_issues") or []
    lines.extend(["", "## Review Policy", ""])
    lines.extend(
        [
            "- report_only: true",
            "- db_writes: false",
            "- requires_explicit_human_decision: true",
            "- prohibited_auto_statuses: human_reviewed, accepted, reviewed",
        ]
    )
    if guardrail_issues:
        lines.extend(["", "## Source Guardrail Issues", ""])
        for issue in guardrail_issues:
            lines.append(f"- {issue}")

    lines.extend(["", "## Prioritized Claims", ""])
    for item in (report.get("items") or [])[: max(0, max_items)]:
        claim = item.get("claim") or {}
        scope = item.get("scope") or {}
        level_fit = item.get("level_fit") or {}
        relation = item.get("relation") or {}
        evidence = item.get("evidence") or {}
        action_bundle = item.get("review_action_bundle") or {}
        lines.extend(
            [
                f"### {item.get('priority')} {item.get('sequence')}. {scope.get('sqf_job_name')} / {scope.get('sqf_duty_name')} -> {scope.get('ncs_unit_name')}",
                "",
                f"- claim_id: {item.get('claim_id')}",
                f"- reasons: {', '.join(item.get('priority_reasons') or [])}",
                f"- review_risk_flags: {', '.join(action_bundle.get('review_risk_flags') or []) or 'none'}",
                f"- human_notes_prompt: {action_bundle.get('human_notes_prompt')}",
                f"- target_major_code: {scope.get('target_major_code')}",
                f"- relation: {relation.get('mapping_relation')}",
                f"- report_evidence_count: {evidence.get('report_evidence_count')}",
                f"- evidence_relations: {', '.join(evidence.get('evidence_relations') or [])}",
                f"- level_gap: {level_fit.get('level_gap')} (sqf={level_fit.get('sqf_level')}, ncs={level_fit.get('ncs_unit_level')})",
                f"- used_for_scoring: {str(item.get('used_for_scoring')).lower()}",
                f"- status_update_allowed: {str(item.get('status_update_allowed')).lower()}",
                f"- approval_claim: {str(item.get('approval_claim')).lower()}",
                f"- decision: `{item.get('decision')}`",
                f"- statement: {_trim_text(claim.get('claim_statement'), max_chars=MAX_TEXT_CHARS)}",
                "",
            ]
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _claim_records(candidate_payload: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(candidate_payload, list):
        return [record for record in candidate_payload if isinstance(record, dict)]
    claims = candidate_payload.get("claims") if isinstance(candidate_payload, dict) else None
    if isinstance(claims, list):
        return [record for record in claims if isinstance(record, dict)]
    return []


def _claim_analysis(claim: dict[str, Any]) -> dict[str, Any]:
    sqf_level = _claim_sqf_level(claim)
    ncs_level = _claim_ncs_level(claim)
    level_gap = abs(sqf_level - ncs_level) if sqf_level is not None and ncs_level is not None else None
    mapping_relation = _mapping_relation(claim)
    evidence_relations = _evidence_relations(claim)
    all_relations = [_normalize_relation(mapping_relation), *[_normalize_relation(rel) for rel in evidence_relations]]
    all_relations = [rel for rel in all_relations if rel]
    report_evidence = claim.get("report_evidence") or []
    report_evidence_count = max(
        _as_int((claim.get("basis_strength") or {}).get("report_evidence_count")) or 0,
        len(report_evidence) if isinstance(report_evidence, list) else 0,
    )
    scope_text = _scope_text(claim)
    major_code = _target_major_code(claim)

    return {
        "major_code": major_code,
        "cross_major": major_code != "02",
        "sqf_level": sqf_level,
        "ncs_level": ncs_level,
        "level_gap": level_gap,
        "mapping_relation": mapping_relation,
        "evidence_relations": evidence_relations,
        "report_evidence_count": report_evidence_count,
        "has_report_evidence": report_evidence_count >= 1,
        "has_close_match": _normalize_relation(mapping_relation) == CLOSE_MATCH_RELATION,
        "has_partial": _normalize_relation(mapping_relation) == PARTIAL_RELATION,
        "has_strong_evidence": any(_normalize_relation(rel) == STRONG_EVIDENCE_RELATION for rel in evidence_relations),
        "has_supporting_evidence": any(
            _normalize_relation(rel) == SUPPORTING_EVIDENCE_RELATION for rel in evidence_relations
        ),
        "related_only": bool(all_relations) and all(rel in RELATED_ONLY_RELATIONS for rel in all_relations),
        "has_adjacent_job_keyword": _contains_any(scope_text, ADJACENT_JOB_KEYWORDS),
        "has_finance_tax_keyword": _contains_any(scope_text, FINANCE_TAX_KEYWORDS),
        "has_broad_scope_keyword": _contains_any(scope_text, BROAD_SCOPE_KEYWORDS),
        "evidence_refs": _evidence_refs(claim),
    }


def _classify_claim(analysis: dict[str, Any]) -> tuple[str, list[str]]:
    if analysis["cross_major"]:
        return "reject_review", ["target_major_not_02"]
    if not analysis["has_report_evidence"]:
        return "reject_review", ["no_report_evidence"]
    if analysis["related_only"]:
        return "reject_review", ["related_only_mapping_or_evidence"]

    if (
        analysis["has_close_match"]
        and analysis["has_strong_evidence"]
        and _level_gap_within(analysis["level_gap"], 1)
    ):
        return "P0", ["major_02_close_match_strong_evidence_level_gap_le_1"]

    if (
        (analysis["has_partial"] or analysis["has_supporting_evidence"])
        and _level_gap_within(analysis["level_gap"], 1)
    ):
        return "P1", ["major_02_partial_or_supporting_evidence_level_gap_le_1"]

    p2_reasons: list[str] = []
    if analysis["has_adjacent_job_keyword"]:
        p2_reasons.append("major_02_adjacent_job_keyword")
    if _level_gap_within(analysis["level_gap"], 2):
        p2_reasons.append("level_gap_le_2")
    if p2_reasons:
        return "P2", p2_reasons

    p3_reasons: list[str] = []
    if analysis["has_broad_scope_keyword"]:
        p3_reasons.append("broad_scope_reference")
    if analysis["has_finance_tax_keyword"]:
        p3_reasons.append("finance_tax_adjacent_reference")
    if analysis["level_gap"] is None:
        p3_reasons.append("missing_level_fit_for_lower_priority_review")
    if not p3_reasons:
        p3_reasons.append("major_02_evidence_requires_low_priority_review")
    return "P3", p3_reasons


def _priority_evidence_strength(analysis: dict[str, Any]) -> str:
    if not analysis["has_report_evidence"]:
        return "missing"
    if analysis["has_strong_evidence"]:
        return "strong"
    if analysis["has_supporting_evidence"]:
        return "medium"
    return "weak"


def _priority_review_risk_flags(analysis: dict[str, Any], *, priority: str) -> list[str]:
    flags: list[str] = []
    if analysis["cross_major"]:
        flags.append("target_major_not_02")
    if not analysis["has_report_evidence"]:
        flags.append("no_report_evidence")
    if analysis["related_only"]:
        flags.append("related_only_mapping_or_evidence")
    if analysis["level_gap"] is None:
        flags.append("level_unknown")
    elif analysis["level_gap"] > 2:
        flags.append("level_gap_gt_2")
    elif analysis["level_gap"] > 1:
        flags.append("level_gap_warning")
    if analysis["has_broad_scope_keyword"] and priority not in {"P0", "P1"}:
        flags.append("broad_scope_reference")
    if priority == "reject_review":
        flags.append("reject_review_bucket")
    return flags


def _source_or_classified_priority(
    claim: dict[str, Any],
    analysis: dict[str, Any],
) -> tuple[str, list[str]]:
    classified_priority, reasons = _classify_claim(analysis)
    source_priority = str(claim.get("recommended_priority") or "").strip()
    if source_priority in PRIORITY_ORDER:
        if source_priority == classified_priority:
            return classified_priority, [*reasons, "source_claim_recommended_priority_confirmed"]
        if PRIORITY_SORT[source_priority] > PRIORITY_SORT[classified_priority]:
            return source_priority, [*reasons, f"source_claim_recommended_priority_downgraded:{source_priority}"]
        return classified_priority, [*reasons, f"source_claim_recommended_priority_ignored:{source_priority}"]
    return classified_priority, reasons


def _priority_item_sort_key(item: dict[str, Any]) -> tuple[int, float, int, str]:
    claim = item.get("claim") or {}
    sequence = _as_int(item.get("sequence"))
    return (
        int(item.get("priority_sort", len(PRIORITY_ORDER))),
        -float(claim.get("source_priority_score") or 0.0),
        sequence if sequence is not None else 999_999,
        str(item.get("claim_id") or ""),
    )


def _claim_sqf_level(claim: dict[str, Any]) -> int | None:
    sqf = claim.get("sqf") or {}
    for key in ("sqf_level", "level", "level_name", "duty_name"):
        level = parse_level(sqf.get(key))
        if level is not None:
            return level
    return None


def _claim_ncs_level(claim: dict[str, Any]) -> int | None:
    ncs = claim.get("ncs_candidate") or {}
    for key in ("unit_level", "api_unit_level", "unit_level_raw", "level"):
        level = parse_level(ncs.get(key))
        if level is not None:
            return level
    return None


def _target_major_code(claim: dict[str, Any]) -> str | None:
    ncs = claim.get("ncs_candidate") or {}
    classification = ncs.get("classification") or {}
    for value in (
        classification.get("major_code"),
        ncs.get("major_code"),
        ncs.get("ncs_lclas_cd"),
    ):
        normalized = _normalize_major_code(value)
        if normalized:
            return normalized

    unit_code = str(ncs.get("unit_code") or "").strip()
    if len(unit_code) >= 2 and unit_code[:2].isdigit():
        return unit_code[:2]
    return None


def _normalize_major_code(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit() and len(text) == 1:
        return f"0{text}"
    if len(text) >= 2 and text[:2].isdigit():
        return text[:2]
    return text[:2]


def _mapping_relation(claim: dict[str, Any]) -> str:
    basis = claim.get("basis_strength") or {}
    match = claim.get("sqf_ncs_match") or {}
    return str(basis.get("mapping_relation") or match.get("relation") or "").strip()


def _evidence_relations(claim: dict[str, Any]) -> list[str]:
    relations: list[str] = []
    for evidence in claim.get("report_evidence") or []:
        if not isinstance(evidence, dict):
            continue
        relation = str(evidence.get("relation") or "").strip()
        if relation:
            relations.append(relation)
    return relations


def _evidence_refs(claim: dict[str, Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for evidence in (claim.get("report_evidence") or [])[:3]:
        if not isinstance(evidence, dict):
            continue
        document = evidence.get("document") or {}
        refs.append(
            {
                "evidence_ref_id": evidence.get("evidence_ref_id"),
                "relation": evidence.get("relation"),
                "score": _as_float(evidence.get("score")),
                "document_title": document.get("title"),
                "page_start": document.get("page_start"),
                "page_end": document.get("page_end"),
            }
        )
    return refs


def _scope_text(claim: dict[str, Any]) -> str:
    parts: list[str] = []
    parts.append(str(claim.get("claim_statement") or ""))
    sqf = claim.get("sqf") or {}
    ncs = claim.get("ncs_candidate") or {}
    classification = ncs.get("classification") or {}
    sector = sqf.get("sector") or {}
    scope_fit = claim.get("scope_fit") or {}
    match = claim.get("sqf_ncs_match") or {}
    for payload in (sqf, sector, ncs, classification, scope_fit, match):
        for value in payload.values():
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
            elif isinstance(value, (str, int, float)):
                parts.append(str(value))
    for evidence in (claim.get("report_evidence") or [])[:3]:
        if not isinstance(evidence, dict):
            continue
        matched_terms = evidence.get("matched_terms") or {}
        for value in matched_terms.values():
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
        parts.extend(str(item) for item in evidence.get("keyword_hits") or [])
    return "\n".join(part for part in parts if part).casefold()


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword.casefold() in text for keyword in keywords if keyword)


def _level_gap_within(level_gap: int | None, maximum: int) -> bool:
    return level_gap is not None and level_gap <= maximum


def _normalize_relation(value: Any) -> str:
    return re.sub(r"[^a-z]", "", str(value or "").casefold())


def _source_guardrail_issues(candidate_payload: dict[str, Any] | list[dict[str, Any]]) -> list[str]:
    records: list[tuple[str, dict[str, Any]]] = []
    if isinstance(candidate_payload, dict):
        if isinstance(candidate_payload.get("batch"), dict):
            records.append(("batch", candidate_payload["batch"]))
        for claim in _claim_records(candidate_payload):
            records.append((f"claim:{claim.get('claim_id')}", claim))
    elif isinstance(candidate_payload, list):
        for index, claim in enumerate(_claim_records(candidate_payload), start=1):
            records.append((f"claim:{claim.get('claim_id') or index}", claim))

    issues: list[str] = []
    for label, record in records:
        for key in REPORT_ONLY_GUARDRAILS:
            if record.get(key) is True:
                issues.append(f"{label}.{key}_not_false")
    return issues


def _trim_text(value: Any, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... [truncated]"


def _as_int(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None
