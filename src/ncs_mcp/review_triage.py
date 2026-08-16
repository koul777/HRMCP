from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any


QUALITY_CATEGORY_ACTIONS = {
    "human_review": "Route to human reviewers; do not auto-promote review statuses.",
    "collection_stability": "Investigate collection retry strategy before broad API mutation.",
    "data_quality": "Prepare review evidence; preserve raw source values.",
    "other": "Inspect gate details and decide the owner.",
}

TRANSITION_SCENARIO_SEEDPACK_FORMAT_VERSION = "ncs-transition-scenario-review-v1"
TRUSTED_TRANSITION_SCENARIO_REVIEW_STATUSES = {"human_reviewed", "reviewed", "accepted"}


REVIEW_ISSUE_ORDER = {
    "ontology_training_goal_link_human_review_required": 10,
    "hr_training_goal_link_human_review_required": 11,
    "ontology_task_ksa_relation_human_review_required": 20,
    "hr_core_concept_human_review_required": 30,
    "ontology_core_concept_human_review_required": 31,
    "criteria_format_issue": 40,
    "api_element_unmatched": 50,
    "api_element_value_mismatch": 51,
    "api_value_mismatch": 52,
    "suspected_typo": 60,
}


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
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
        with path.open(encoding="utf-8") as handle:
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


def _transition_flags(item: dict[str, Any]) -> list[str]:
    case = item.get("evaluation_case") or {}
    recall = _as_float(item.get("expected_recall_at_k", case.get("expected_recall_at_k")))
    precision = _as_float(item.get("precision_at_k", case.get("precision_at_k")))
    transferability = _as_float(case.get("transferability_ratio"), default=1.0)
    flags: list[str] = []
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
        "expected_course_count": len(item.get("expected_courses") or []),
        "recommended_course_count": len(item.get("recommended_courses") or []),
        "expected_course_hits": item.get("expected_course_hits", case.get("expected_course_hits", [])),
    }


def _review_priority_item(rank: int, item: dict[str, Any]) -> dict[str, Any]:
    issue = item.get("issue") or {}
    context = item.get("context") or {}
    issue_type = str(issue.get("issue_type") or "")
    return {
        "rank": rank,
        "review_order": REVIEW_ISSUE_ORDER.get(issue_type, 999),
        "issue_type": issue_type,
        "target_type": issue.get("target_type"),
        "target_id": str(issue.get("target_id") or ""),
        "severity": issue.get("severity"),
        "priority_score": item.get("priority_score"),
        "priority_reason": item.get("priority_reason"),
        "suggested_action": issue.get("suggested_action"),
        "context_excerpt": _context_excerpt(context),
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

    review_issue_counts = Counter(str(item.get("issue_type") or "unknown") for item in review_items)
    transition_attention_count = sum(1 for item in transition_priorities if item.get("flags"))
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
            "name": "trusted_transition_scenarios",
            "status": "warn"
            if any(warning.get("name") == "transition_eval:trusted_scenarios" for warning in warnings)
            else "pass",
            "value": next(
                (warning.get("value") for warning in warnings if warning.get("name") == "transition_eval:trusted_scenarios"),
                None,
            ),
            "message": "No trusted transition scenarios are available for hard gating." if any(warning.get("name") == "transition_eval:trusted_scenarios" for warning in warnings) else "Trusted transition scenario gate is not warning.",
        },
    ]

    return {
        "ok": True,
        "summary": {
            "quality_status": quality_report.get("status"),
            "quality_warning_count": len(warnings),
            "quality_warning_categories": dict(sorted(warning_counts.items())),
            "review_priority_item_count": len(review_items),
            "review_issue_type_counts": dict(sorted(review_issue_counts.items())),
            "transition_seedpack_item_count": len(transition_items),
            "transition_attention_count": transition_attention_count,
            "transition_seedpack_id": (transition_seedpack_batch or {}).get("seedpack_id"),
            "transition_status_snapshot": transition_status_snapshot,
            "source_paths": source_paths or {},
        },
        "quality_warnings": warnings,
        "transition_review_priorities": transition_priorities,
        "review_priority_items": review_items,
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
        lines.append(
            "- "
            f"#{item.get('rank')} {item.get('scenario_name')} "
            f"(score={item.get('priority_score')}, band={item.get('priority_band')}, "
            f"recall={item.get('expected_recall_at_k')}, precision={item.get('precision_at_k')}, "
            f"flags={flags})"
        )

    lines.extend(["", "## Review Priority Items", ""])
    for item in report.get("review_priority_items") or []:
        lines.append(
            "- "
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
