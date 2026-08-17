from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ncs_mcp.review_safety import neutralize_suggested_action


QUALITY_CATEGORY_ACTIONS = {
    "human_review": "Route to human reviewers; do not auto-promote review statuses.",
    "collection_stability": "Investigate collection retry strategy before broad API mutation.",
    "data_quality": "Prepare review evidence; preserve raw source values.",
    "other": "Inspect gate details and decide the owner.",
}

TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION = "ncs-transition-scenario-review-v1"
TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES = {"human_reviewed", "reviewed", "accepted"}
MIN_TRUSTED_TRANSITION_SCENARIOS = 10


REVIEW_ISSUE_ORDER = {
    "ontology_training_goal_link_human_review_required": 10,
    "hr_training_goal_link_human_review_required": 11,
    "ontology_task_ksa_relation_human_review_required": 20,
    "hr_core_concept_human_review_required": 30,
    "ontology_core_concept_human_review_required": 31,
    "criteria_format_issue": 40,
    "api_element_collection_failure": 50,
    "api_element_unmatched": 51,
    "api_element_value_mismatch": 52,
    "api_value_mismatch": 53,
    "suspected_typo": 60,
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ValueError(f"Review triage input file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Review triage input file cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Review triage input file is not valid JSON: {path}: {exc}") from exc


def _validate_quality_report(report: dict[str, Any], path: Path) -> None:
    summary = report.get("summary")
    gates = report.get("gates")
    if not isinstance(summary, dict):
        raise ValueError(f"Quality report is missing object field 'summary': {path}")
    if "fail_count" not in summary:
        raise ValueError(f"Quality report summary is missing 'fail_count': {path}")
    if not isinstance(gates, list):
        raise ValueError(f"Quality report is missing list field 'gates': {path}")


def _validate_review_priority_report(report: dict[str, Any], path: Path) -> None:
    top_items = report.get("top_items")
    if not isinstance(top_items, list):
        raise ValueError(f"Review priority report is missing list field 'top_items': {path}")


def _quality_category(gate_name: str) -> str:
    if gate_name.startswith("review_debt:") or gate_name.startswith("transition_eval:"):
        return "human_review"
    if gate_name.startswith("qualification:"):
        return "collection_stability"
    if gate_name == "quality_issues:api_element_collection_failure":
        return "collection_stability"
    if gate_name.startswith("quality_issues:") or gate_name.startswith("recommendation_evidence:"):
        return "data_quality"
    return "other"


def _as_float(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default


def _context_excerpt(context: dict[str, Any], *, max_chars: int = 240) -> str:
    preferred = [
        "compe_unit_name",
        "train_goal",
        "criteria_text_raw",
        "concept_name",
        "unit_name_raw",
        "unit_name",
        "source_concept_name",
        "target_concept_name",
    ]
    parts = [str(context[key]) for key in preferred if context.get(key)]
    if not parts:
        parts = [str(value) for value in context.values() if isinstance(value, str) and value.strip()]
    text = " | ".join(parts)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _load_transition_seedpack(path: Path | None) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if path is None:
        return None, []
    batch: dict[str, Any] | None = None
    items: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8-sig") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"Transition seedpack is not valid JSONL at {path}:{line_number}: {exc}"
                    ) from exc
                if record.get("record_type") == "batch":
                    batch = record
                elif record.get("record_type") == "transition_scenario_review_item":
                    items.append(record)
    except FileNotFoundError as exc:
        raise ValueError(f"Transition seedpack file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Transition seedpack file cannot be read: {path}: {exc}") from exc
    if batch is None:
        raise ValueError(f"Transition seedpack is missing a batch record: {path}")
    if batch.get("format_version") != TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION:
        raise ValueError(
            "Unsupported transition seedpack format_version: "
            f"{batch.get('format_version')!r}. Expected {TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION}."
        )
    invalid_items = [
        item.get("scenario_id")
        for item in items
        if item.get("format_version") != TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION
    ]
    if invalid_items:
        raise ValueError(
            "Transition seedpack contains item records with an unsupported format_version: "
            + ", ".join(str(value) for value in invalid_items[:10])
        )
    if not items:
        raise ValueError(f"Transition seedpack has no transition scenario review items: {path}")
    batch_seedpack_id = batch.get("seedpack_id")
    if not batch_seedpack_id:
        raise ValueError(f"Transition seedpack batch is missing seedpack_id: {path}")
    mismatched_seedpack_items = [
        item.get("scenario_id")
        for item in items
        if item.get("seedpack_id") != batch_seedpack_id
    ]
    if mismatched_seedpack_items:
        raise ValueError(
            "Transition seedpack contains item records with a mismatched seedpack_id: "
            + ", ".join(str(value) for value in mismatched_seedpack_items[:10])
        )
    if batch.get("item_count") is not None:
        try:
            expected_item_count = int(batch.get("item_count"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Transition seedpack batch item_count is not an integer: {path}") from exc
        if expected_item_count != len(items):
            raise ValueError(
                f"Transition seedpack item_count mismatch: batch={expected_item_count}, "
                f"items={len(items)} at {path}"
            )
    return batch, items


def _transition_status_snapshot(
    batch: dict[str, Any] | None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    requested_statuses = [
        str(status)
        for status in ((batch or {}).get("review_statuses") or [])
        if str(status).strip()
    ]
    actual_status_counts = dict(
        sorted(
            Counter(str(item.get("current_review_status") or "unknown") for item in items).items()
        )
    )
    missing_requested_statuses = [
        status for status in requested_statuses if actual_status_counts.get(status, 0) == 0
    ]
    declared_counts = (batch or {}).get("actual_review_status_counts")
    declared_missing = (batch or {}).get("missing_requested_review_statuses")
    mismatches: list[str] = []
    if declared_counts is not None and (
        not isinstance(declared_counts, dict) or dict(sorted(declared_counts.items())) != actual_status_counts
    ):
        mismatches.append("actual_review_status_counts")
    if declared_missing is not None and (
        not isinstance(declared_missing, list)
        or sorted(str(value) for value in declared_missing) != sorted(missing_requested_statuses)
    ):
        mismatches.append("missing_requested_review_statuses")
    trusted_status_count = sum(
        count
        for status, count in actual_status_counts.items()
        if status in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES
    )
    declared_trusted_count = (batch or {}).get("trusted_review_status_count")
    try:
        declared_trusted_count_int = int(declared_trusted_count) if declared_trusted_count is not None else None
    except (TypeError, ValueError):
        declared_trusted_count_int = None
        mismatches.append("trusted_review_status_count")
    if (
        declared_trusted_count is not None
        and declared_trusted_count_int is not None
        and declared_trusted_count_int != trusted_status_count
    ):
        mismatches.append("trusted_review_status_count")
    return {
        "requested_review_statuses": requested_statuses,
        "actual_review_status_counts": actual_status_counts,
        "missing_requested_review_statuses": missing_requested_statuses,
        "trusted_review_status_count": trusted_status_count,
        "batch_status_snapshot_mismatches": mismatches,
    }


def _transition_course_scope_summary(item: dict[str, Any]) -> dict[str, Any]:
    case = item.get("evaluation_case") if isinstance(item.get("evaluation_case"), dict) else {}
    summary = item.get("recommended_course_scope_summary")
    if not isinstance(summary, dict):
        summary = case.get("recommended_course_scope_summary") if isinstance(case, dict) else {}
    if isinstance(summary, dict) and summary:
        return {
            "course_count": int(_as_float(summary.get("course_count"), default=0.0)),
            "relation_counts": dict(summary.get("relation_counts") or {}),
            "alignment_counts": dict(summary.get("alignment_counts") or {}),
            "direct_or_near_count": int(_as_float(summary.get("direct_or_near_count"), default=0.0)),
            "requires_scope_review_count": int(
                _as_float(summary.get("requires_scope_review_count"), default=0.0)
            ),
            "review_flag_counts": dict(summary.get("review_flag_counts") or {}),
        }
    evidence = item.get("recommended_course_evidence")
    if not isinstance(evidence, list):
        evidence = case.get("recommended_course_evidence") if isinstance(case, dict) else []
    relation_counts: Counter[str] = Counter()
    alignment_counts: Counter[str] = Counter()
    review_flag_counts: Counter[str] = Counter()
    direct_or_near_count = 0
    requires_scope_review_count = 0
    for row in evidence or []:
        if not isinstance(row, dict):
            continue
        scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        relation = str(scope_fit.get("relation") or "unknown")
        alignment = str(scope_fit.get("alignment") or "unknown")
        relation_counts[relation] += 1
        alignment_counts[alignment] += 1
        if alignment in {"direct", "near"}:
            direct_or_near_count += 1
        if scope_fit.get("requires_scope_review") or alignment in {"adjacent", "distant", "unknown"}:
            requires_scope_review_count += 1
        for flag in row.get("review_flags") or []:
            if str(flag).strip():
                review_flag_counts[str(flag)] += 1
    return {
        "course_count": len(evidence or []),
        "relation_counts": dict(sorted(relation_counts.items())),
        "alignment_counts": dict(sorted(alignment_counts.items())),
        "direct_or_near_count": direct_or_near_count,
        "requires_scope_review_count": requires_scope_review_count,
        "review_flag_counts": dict(sorted(review_flag_counts.items())),
    }


def _aggregate_transition_course_scope_summaries(items: list[dict[str, Any]]) -> dict[str, Any]:
    relation_counts: Counter[str] = Counter()
    alignment_counts: Counter[str] = Counter()
    review_flag_counts: Counter[str] = Counter()
    course_count = 0
    direct_or_near_count = 0
    requires_scope_review_count = 0
    requires_scope_review_item_count = 0

    for item in items:
        summary = _transition_course_scope_summary(item)
        course_count += int(summary.get("course_count") or 0)
        direct_or_near_count += int(summary.get("direct_or_near_count") or 0)
        item_review_count = int(summary.get("requires_scope_review_count") or 0)
        requires_scope_review_count += item_review_count
        if item_review_count > 0:
            requires_scope_review_item_count += 1
        relation_counts.update(summary.get("relation_counts") or {})
        alignment_counts.update(summary.get("alignment_counts") or {})
        review_flag_counts.update(summary.get("review_flag_counts") or {})

    return {
        "course_count": course_count,
        "relation_counts": dict(sorted(relation_counts.items())),
        "alignment_counts": dict(sorted(alignment_counts.items())),
        "direct_or_near_count": direct_or_near_count,
        "requires_scope_review_count": requires_scope_review_count,
        "requires_scope_review_item_count": requires_scope_review_item_count,
        "review_flag_counts": dict(sorted(review_flag_counts.items())),
    }


def _transition_flags(item: dict[str, Any]) -> list[str]:
    case = item.get("evaluation_case") or {}
    recall = _as_float(item.get("expected_recall_at_k", case.get("expected_recall_at_k")))
    precision = _as_float(item.get("precision_at_k", case.get("precision_at_k")))
    transferability = _as_float(case.get("transferability_ratio"), default=1.0)
    course_scope_summary = _transition_course_scope_summary(item)
    flags: list[str] = []
    if item.get("evaluation_case_missing"):
        flags.append("missing_evaluation_case")
    if recall < 0.75:
        flags.append("low_expected_recall")
    if precision < 0.6:
        flags.append("low_precision")
    if not item.get("top1_expected_hit", case.get("top1_expected_hit")):
        flags.append("top1_expected_miss")
    if transferability <= 0.05:
        flags.append("low_transferability")
    if not case.get("current_scope_hit", True):
        flags.append("current_scope_miss")
    if not case.get("target_scope_hit", True):
        flags.append("target_scope_miss")
    if not item.get("expected_courses"):
        flags.append("missing_expected_courses")
    if not item.get("recommended_courses"):
        flags.append("missing_recommendations")
    if int(course_scope_summary.get("requires_scope_review_count") or 0) > 0:
        flags.append("course_scope_review_required")
    if any(
        str(item.get(field) or "").strip()
        for field in ["decision", "reviewer_id", "reviewed_at", "proposed_review_status"]
    ):
        flags.append("contains_review_decision")
    if str(item.get("current_review_status") or "") in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES:
        flags.append("trusted_review_status_in_blank_seedpack")
    return flags


def _transition_priority_score(item: dict[str, Any]) -> float:
    case = item.get("evaluation_case") or {}
    recall = _as_float(item.get("expected_recall_at_k", case.get("expected_recall_at_k")))
    precision = _as_float(item.get("precision_at_k", case.get("precision_at_k")))
    transferability = _as_float(case.get("transferability_ratio"), default=1.0)
    score = 10.0
    score += max(0.0, 1.0 - recall) * 45.0
    score += max(0.0, 0.6 - precision) * 40.0
    if not item.get("top1_expected_hit", case.get("top1_expected_hit")):
        score += 25.0
    if transferability <= 0.05:
        score += 10.0
    if any(
        str(item.get(field) or "").strip()
        for field in ["decision", "reviewer_id", "reviewed_at", "proposed_review_status"]
    ):
        score += 50.0
    if str(item.get("current_review_status") or "") in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES:
        score += 50.0
    return round(score, 4)


def _priority_band(score: float) -> str:
    if score >= 50:
        return "high"
    if score >= 25:
        return "medium"
    return "low"


def _transition_priority_item(item: dict[str, Any]) -> dict[str, Any]:
    case = item.get("evaluation_case") or {}
    score = _transition_priority_score(item)
    course_scope_summary = _transition_course_scope_summary(item)
    evidence = item.get("recommended_course_evidence")
    if not isinstance(evidence, list):
        evidence = case.get("recommended_course_evidence") if isinstance(case, dict) else []
    return {
        "priority_score": score,
        "priority_band": _priority_band(score),
        "flags": _transition_flags(item),
        "scenario_id": item.get("scenario_id"),
        "scenario_name": item.get("scenario_name"),
        "review_status": item.get("current_review_status"),
        "current_query": item.get("current_query"),
        "target_query": item.get("target_query"),
        "expected_recall_at_k": item.get("expected_recall_at_k", case.get("expected_recall_at_k")),
        "precision_at_k": item.get("precision_at_k", case.get("precision_at_k")),
        "top1_expected_hit": item.get("top1_expected_hit", case.get("top1_expected_hit")),
        "transferability_ratio": case.get("transferability_ratio"),
        "evaluation_case_missing": bool(item.get("evaluation_case_missing")),
        "expected_course_count": len(item.get("expected_courses") or []),
        "recommended_course_count": len(item.get("recommended_courses") or []),
        "expected_course_hits": item.get("expected_course_hits", case.get("expected_course_hits", [])),
        "recommended_course_scope_summary": course_scope_summary,
        "course_scope_fit_relation_counts": course_scope_summary.get("relation_counts") or {},
        "course_scope_fit_alignment_counts": course_scope_summary.get("alignment_counts") or {},
        "course_scope_review_required_count": course_scope_summary.get("requires_scope_review_count", 0),
        "recommended_course_evidence": evidence[:5] if isinstance(evidence, list) else [],
    }


def _transition_trust_review_candidate_item(item: dict[str, Any]) -> dict[str, Any] | None:
    if item.get("evaluation_case_missing"):
        return None
    if any(
        str(item.get(field) or "").strip()
        for field in ["decision", "reviewer_id", "reviewed_at", "proposed_review_status"]
    ):
        return None
    current_status = str(item.get("current_review_status") or "")
    if current_status in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES:
        return None
    case = item.get("evaluation_case") or {}
    recall = _as_float(item.get("expected_recall_at_k", case.get("expected_recall_at_k")))
    precision = _as_float(item.get("precision_at_k", case.get("precision_at_k")))
    top1_hit = bool(item.get("top1_expected_hit", case.get("top1_expected_hit")))
    expected_count = len(item.get("expected_courses") or [])
    recommended_count = len(item.get("recommended_courses") or [])
    if expected_count == 0 or recommended_count == 0:
        return None
    if recall < 0.5 or precision <= 0.0 or not top1_hit:
        return None

    scope_summary = _transition_course_scope_summary(item)
    course_count = int(scope_summary.get("course_count") or 0)
    direct_or_near_count = int(scope_summary.get("direct_or_near_count") or 0)
    scope_review_count = int(scope_summary.get("requires_scope_review_count") or 0)
    direct_or_near_ratio = round(direct_or_near_count / course_count, 4) if course_count else 0.0
    flags = _transition_flags(item)
    risk_flags = [flag for flag in flags if flag]
    score = (
        recall * 50.0
        + precision * 30.0
        + (10.0 if top1_hit else 0.0)
        + direct_or_near_ratio * 10.0
        - min(scope_review_count, 5) * 2.0
        - len(risk_flags) * 5.0
    )
    readiness = "strong_review_candidate" if recall >= 0.75 and top1_hit and not risk_flags else "needs_careful_review"
    return {
        "scenario_id": item.get("scenario_id"),
        "scenario_name": item.get("scenario_name"),
        "review_status": current_status,
        "candidate_score": round(score, 4),
        "review_readiness": readiness,
        "expected_recall_at_k": recall,
        "precision_at_k": precision,
        "top1_expected_hit": top1_hit,
        "expected_course_count": expected_count,
        "recommended_course_count": recommended_count,
        "direct_or_near_course_ratio": direct_or_near_ratio,
        "course_scope_review_required_count": scope_review_count,
        "flags": flags,
        "human_decision_required": True,
        "decision_policy": "Report-only candidate; do not promote without human review.",
    }


def _review_priority_item(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    issue = item.get("issue") or {}
    context = item.get("context") or {}
    issue_type = str(issue.get("issue_type") or "")
    safe_suggested_action = neutralize_suggested_action(
        issue.get("suggested_action"),
        issue_type=issue_type,
        target_type=issue.get("target_type"),
    )
    return {
        "rank": rank,
        "review_order": REVIEW_ISSUE_ORDER.get(issue_type, 999),
        "issue_type": issue_type,
        "target_type": issue.get("target_type"),
        "target_id": str(issue.get("target_id") or ""),
        "severity": issue.get("severity"),
        "priority_score": item.get("priority_score"),
        "priority_reason": item.get("priority_reason"),
        "suggested_action": safe_suggested_action,
        "context_excerpt": _context_excerpt(context),
    }


def _review_priority_focus_overlay(overlay: dict[str, Any]) -> dict[str, Any]:
    raw_items = overlay.get("top_items") if isinstance(overlay.get("top_items"), list) else []
    items = [
        _review_priority_item(rank, item)
        for rank, item in enumerate(raw_items, start=1)
        if isinstance(item, dict)
    ]
    return {
        "code": overlay.get("code"),
        "label": overlay.get("label"),
        "major_code": overlay.get("major_code"),
        "reason": overlay.get("reason"),
        "item_count": overlay.get("item_count", len(items)),
        "items": items,
    }


def build_review_triage(
    *,
    quality_report: dict[str, Any],
    review_priority_report: dict[str, Any],
    transition_seedpack_batch: dict[str, Any] | None = None,
    transition_seedpack_items: list[dict[str, Any]] | None = None,
    source_paths: dict[str, str | None] | None = None,
    review_item_limit: int = 20,
    transition_item_limit: int = 20,
) -> dict[str, Any]:
    gates = quality_report.get("gates") or []
    warnings = [
        {
            "category": _quality_category(str(gate.get("name") or "")),
            "name": gate.get("name"),
            "status": gate.get("status"),
            "message": gate.get("message"),
            "value": gate.get("value"),
            "threshold": gate.get("threshold"),
            "action": QUALITY_CATEGORY_ACTIONS[_quality_category(str(gate.get("name") or ""))],
            "details": gate.get("details") or {},
        }
        for gate in gates
        if gate.get("status") in {"warn", "fail"}
    ]
    warning_counts = Counter(str(warning["category"]) for warning in warnings)

    transition_items = transition_seedpack_items or []
    transition_priorities = [
        _transition_priority_item(item)
        for item in transition_items
    ]
    transition_priorities.sort(
        key=lambda item: (
            -_as_float(item.get("priority_score")),
            _as_float(item.get("scenario_id"), default=0),
        )
    )
    transition_priorities = transition_priorities[: max(0, transition_item_limit)]
    for rank, item in enumerate(transition_priorities, start=1):
        item["rank"] = rank

    transition_trust_candidates = [
        candidate
        for item in transition_items
        for candidate in [_transition_trust_review_candidate_item(item)]
        if candidate is not None
    ]
    transition_trust_candidates.sort(
        key=lambda item: (
            -_as_float(item.get("candidate_score")),
            -_as_float(item.get("expected_recall_at_k")),
            -_as_float(item.get("precision_at_k")),
            _as_float(item.get("scenario_id"), default=0),
        )
    )
    transition_trust_candidates = transition_trust_candidates[: max(0, transition_item_limit)]
    for rank, item in enumerate(transition_trust_candidates, start=1):
        item["rank"] = rank

    raw_review_items = review_priority_report.get("top_items") or []
    review_items = [
        _review_priority_item(rank, item)
        for rank, item in enumerate(raw_review_items, start=1)
    ]
    review_items.sort(
        key=lambda item: (
            _as_float(item.get("review_order"), default=999),
            -_as_float(item.get("priority_score")),
            _as_float(item.get("rank")),
        )
    )
    review_items = review_items[: max(0, review_item_limit)]
    for rank, item in enumerate(review_items, start=1):
        item["rank"] = rank
    focus_review_overlays = [
        _review_priority_focus_overlay(overlay)
        for overlay in review_priority_report.get("focus_overlays") or []
        if isinstance(overlay, dict)
    ]

    review_issue_counts = Counter(str(item.get("issue_type") or "unknown") for item in review_items)
    transition_attention_count = sum(1 for item in transition_priorities if item.get("flags"))
    transition_course_scope_summary = _aggregate_transition_course_scope_summaries(transition_items)
    transition_decision_count = sum(
        1
        for item in transition_items
        if any(
            str(item.get(field) or "").strip()
            for field in ["decision", "reviewer_id", "reviewed_at", "proposed_review_status"]
        )
    )
    transition_trusted_status_count = sum(
        1
        for item in transition_items
        if str(item.get("current_review_status") or "") in TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES
    )
    transition_status_snapshot = _transition_status_snapshot(transition_seedpack_batch, transition_items)
    missing_requested_review_statuses = transition_status_snapshot["missing_requested_review_statuses"]
    status_snapshot_mismatches = transition_status_snapshot["batch_status_snapshot_mismatches"]
    batch_missing_evaluation_ids = (transition_seedpack_batch or {}).get("missing_evaluation_scenario_ids")
    if not isinstance(batch_missing_evaluation_ids, list):
        batch_missing_evaluation_ids = []
    item_missing_evaluation_ids = [
        item.get("scenario_id")
        for item in transition_items
        if item.get("evaluation_case_missing")
    ]
    missing_evaluation_scenario_ids = sorted(
        {
            int(item)
            for item in [*batch_missing_evaluation_ids, *item_missing_evaluation_ids]
            if item is not None
        }
    )
    trusted_transition_gate = next(
        (gate for gate in gates if gate.get("name") == "transition_eval:trusted_scenarios"),
        None,
    )
    seedpack_trusted_transition_count = transition_status_snapshot["trusted_review_status_count"]
    global_trusted_transition_count = seedpack_trusted_transition_count
    if trusted_transition_gate and trusted_transition_gate.get("value") is not None:
        global_trusted_transition_count = int(_as_float(trusted_transition_gate.get("value")))
    trusted_transition_threshold = f">= {MIN_TRUSTED_TRANSITION_SCENARIOS}"
    trusted_transition_warning = global_trusted_transition_count < MIN_TRUSTED_TRANSITION_SCENARIOS

    cross_checks = [
        {
            "name": "quality_fail_count",
            "status": "pass" if (quality_report.get("summary") or {}).get("fail_count", 0) == 0 else "fail",
            "value": (quality_report.get("summary") or {}).get("fail_count", 0),
            "message": "Quality gates have no fail status." if (quality_report.get("summary") or {}).get("fail_count", 0) == 0 else "At least one quality gate is failing.",
        },
        {
            "name": "transition_seedpack_decisions_empty",
            "status": "pass" if transition_decision_count == 0 else "warn",
            "value": transition_decision_count,
            "message": "Transition seedpack remains export-only with empty decision fields." if transition_decision_count == 0 else "Transition seedpack contains decision metadata; inspect before using as a blank review artifact.",
        },
        {
            "name": "transition_seedpack_no_trusted_items",
            "status": "pass" if transition_trusted_status_count == 0 else "warn",
            "value": transition_trusted_status_count,
            "message": "Transition seedpack contains only non-trusted candidate items." if transition_trusted_status_count == 0 else "Transition seedpack contains already trusted scenarios; do not use as a blank approval artifact.",
        },
        {
            "name": "transition_seedpack_requested_status_coverage",
            "status": "pass" if not missing_requested_review_statuses else "warn",
            "value": missing_requested_review_statuses,
            "message": "Transition seedpack includes at least one item for every requested review status." if not missing_requested_review_statuses else "Transition seedpack has no selected items for at least one requested review status.",
        },
        {
            "name": "transition_seedpack_status_snapshot_consistent",
            "status": "pass" if not status_snapshot_mismatches else "warn",
            "value": status_snapshot_mismatches,
            "message": "Transition seedpack batch status snapshot matches item rows." if not status_snapshot_mismatches else "Transition seedpack batch status snapshot disagrees with item rows.",
        },
        {
            "name": "transition_seedpack_evaluation_complete",
            "status": "pass" if not missing_evaluation_scenario_ids else "warn",
            "value": missing_evaluation_scenario_ids,
            "message": (
                "Transition seedpack has evaluation cases for every selected scenario."
                if not missing_evaluation_scenario_ids
                else "Transition seedpack is missing evaluation cases for selected scenarios."
            ),
        },
        {
            "name": "seedpack_trusted_transition_scenarios",
            "status": "pass" if seedpack_trusted_transition_count == 0 else "warn",
            "value": seedpack_trusted_transition_count,
            "message": (
                "Selected transition seedpack contains no trusted scenarios."
                if seedpack_trusted_transition_count == 0
                else "Selected transition seedpack contains trusted scenarios; do not use it as a blank review artifact."
            ),
        },
        {
            "name": "global_trusted_transition_scenarios",
            "status": "warn" if trusted_transition_warning else "pass",
            "value": global_trusted_transition_count,
            "threshold": trusted_transition_threshold,
            "message": (
                "Global trusted transition scenarios are below the release-readiness target."
                if trusted_transition_warning
                else "Global trusted transition scenario count meets the release-readiness target."
            ),
        },
    ]

    return {
        "schema": "ncs_review_triage_v1",
        "ok": True,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": True,
        "summary": {
            "quality_status": quality_report.get("status"),
            "quality_warning_count": len(warnings),
            "quality_warning_categories": dict(sorted(warning_counts.items())),
            "review_priority_item_count": len(review_items),
            "review_focus_overlay_count": len(focus_review_overlays),
            "review_issue_type_counts": dict(sorted(review_issue_counts.items())),
            "transition_seedpack_item_count": len(transition_items),
            "transition_attention_count": transition_attention_count,
            "transition_trust_review_candidate_count": len(transition_trust_candidates),
            "transition_course_scope_summary": transition_course_scope_summary,
            "transition_course_scope_relation_counts": transition_course_scope_summary.get("relation_counts"),
            "transition_course_scope_alignment_counts": transition_course_scope_summary.get("alignment_counts"),
            "transition_course_scope_review_required_count": transition_course_scope_summary.get("requires_scope_review_count"),
            "transition_course_scope_review_required_item_count": transition_course_scope_summary.get("requires_scope_review_item_count"),
                "transition_seedpack_id": (transition_seedpack_batch or {}).get("seedpack_id"),
                "transition_status_snapshot": transition_status_snapshot,
                "transition_evaluation_snapshot": {
                    "missing_evaluation_scenario_ids": missing_evaluation_scenario_ids,
                },
                "source_paths": source_paths or {},
            },
        "quality_warnings": warnings,
        "transition_review_priorities": transition_priorities,
        "transition_trust_review_candidates": transition_trust_candidates,
        "review_priority_items": review_items,
        "focus_review_priority_overlays": focus_review_overlays,
        "cross_checks": cross_checks,
        "operator_constraints": [
            "Do not mark candidate scenarios as trusted without human review.",
            "Do not mutate raw NCS/API source fields.",
            "Use seedpacks as review inputs, not as automatic approvals.",
        ],
    }


def build_review_triage_from_files(
    *,
    quality_report_path: Path,
    review_priority_path: Path,
    transition_seedpack_path: Path | None = None,
    review_item_limit: int = 20,
    transition_item_limit: int = 20,
) -> dict[str, Any]:
    batch, transition_items = _load_transition_seedpack(transition_seedpack_path)
    quality_report = _read_json(quality_report_path)
    review_priority_report = _read_json(review_priority_path)
    _validate_quality_report(quality_report, quality_report_path)
    _validate_review_priority_report(review_priority_report, review_priority_path)
    return build_review_triage(
        quality_report=quality_report,
        review_priority_report=review_priority_report,
        transition_seedpack_batch=batch,
        transition_seedpack_items=transition_items,
        source_paths={
            "quality_report": str(quality_report_path),
            "review_priority_report": str(review_priority_path),
            "transition_seedpack": str(transition_seedpack_path) if transition_seedpack_path else None,
        },
        review_item_limit=review_item_limit,
        transition_item_limit=transition_item_limit,
    )


def write_review_triage_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# NCS Review Triage",
        "",
        "## Summary",
        "",
        f"- quality_status: {summary.get('quality_status')}",
        f"- quality_warning_count: {summary.get('quality_warning_count')}",
        f"- transition_seedpack_item_count: {summary.get('transition_seedpack_item_count')}",
        f"- transition_attention_count: {summary.get('transition_attention_count')}",
        f"- transition_trust_review_candidate_count: {summary.get('transition_trust_review_candidate_count')}",
        f"- transition_course_scope_review_required_count: {summary.get('transition_course_scope_review_required_count')}",
        f"- transition_course_scope_review_required_item_count: {summary.get('transition_course_scope_review_required_item_count')}",
        f"- review_priority_item_count: {summary.get('review_priority_item_count')}",
        "",
        "## Operator Constraints",
        "",
    ]
    for constraint in report.get("operator_constraints") or []:
        lines.append(f"- {constraint}")

    lines.extend(["", "## Quality Warning Triage", ""])
    for warning in report.get("quality_warnings") or []:
        lines.append(
            "- "
            f"[{warning.get('category')}] {warning.get('name')}: "
            f"{warning.get('message')} "
            f"(value={warning.get('value')}, threshold={warning.get('threshold')})"
        )

    lines.extend(["", "## Transition Scenario Review Priority", ""])
    for item in report.get("transition_review_priorities") or []:
        flags = ", ".join(item.get("flags") or ["none"])
        scope_relations = json.dumps(
            item.get("course_scope_fit_relation_counts") or {},
            ensure_ascii=False,
            sort_keys=True,
        )
        lines.append(
            "- "
            f"#{item.get('rank')} {item.get('scenario_name')} "
            f"(score={item.get('priority_score')}, band={item.get('priority_band')}, "
            f"recall={item.get('expected_recall_at_k')}, precision={item.get('precision_at_k')}, "
            f"scope_relations={scope_relations}, flags={flags})"
        )

    lines.extend(["", "## Transition Trust Review Candidates", ""])
    for item in report.get("transition_trust_review_candidates") or []:
        flags = ", ".join(item.get("flags") or ["none"])
        lines.append(
            "- "
            f"#{item.get('rank')} {item.get('scenario_name')} "
            f"(score={item.get('candidate_score')}, readiness={item.get('review_readiness')}, "
            f"recall={item.get('expected_recall_at_k')}, precision={item.get('precision_at_k')}, "
            f"top1={item.get('top1_expected_hit')}, direct_or_near={item.get('direct_or_near_course_ratio')}, "
            f"scope_review={item.get('course_scope_review_required_count')}, flags={flags})"
        )
    if report.get("transition_trust_review_candidates"):
        lines.append("")
        lines.append(
            "These rows are report-only review candidates; do not promote any scenario without a human decision."
        )

    lines.extend(["", "## Review Priority Items", ""])
    for item in report.get("review_priority_items") or []:
        lines.append(
            "- "
            f"#{item.get('rank')} {item.get('issue_type')} "
            f"target={item.get('target_type')}:{item.get('target_id')} "
            f"score={item.get('priority_score')} - {item.get('context_excerpt')}"
        )

    lines.extend(["", "## Focus Review Priority Overlays", ""])
    for overlay in report.get("focus_review_priority_overlays") or []:
        lines.append(
            "- "
            f"{overlay.get('label') or overlay.get('code')} "
            f"(major={overlay.get('major_code')}, items={overlay.get('item_count')}) - "
            f"{overlay.get('reason')}"
        )
        for item in (overlay.get("items") or [])[:5]:
            lines.append(
                "  - "
                f"#{item.get('rank')} {item.get('issue_type')} "
                f"target={item.get('target_type')}:{item.get('target_id')} "
                f"score={item.get('priority_score')} - {item.get('context_excerpt')}"
            )

    lines.extend(["", "## Cross Checks", ""])
    for check in report.get("cross_checks") or []:
        lines.append(
            f"- {check.get('status')}: {check.get('name')} "
            f"(value={check.get('value')}) - {check.get('message')}"
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
