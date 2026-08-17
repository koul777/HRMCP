from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.hrd_guide_reference import (
    hrd_guide_reference_metadata,
    load_hrd_guide_reference_index,
)
from ncs_mcp.query_router import route_ncs_query


PROMPT_COVERAGE_SCHEMA = "hrd_guide_prompt_coverage_v1"
NEEDS_CONTEXT_CONTRACT_SCHEMA = "hrd_guide_prompt_needs_context_v1"

TEMPLATE_ALLOWED_MISSING_PARAMS: dict[str, set[str]] = {
    "annual_operation_plan_draft": {"current_query"},
    "training_course_inventory_table": {"current_query"},
    "internal_training_intake_questionnaire": {"current_query"},
}


def build_hrd_guide_prompt_coverage_report(
    *,
    index_path: Path | None = None,
    available_tool_names: set[str] | None = None,
    example_limit: int | None = None,
) -> dict[str, Any]:
    index = load_hrd_guide_reference_index(index_path)
    guide_reference = hrd_guide_reference_metadata(index)
    templates = [
        dict(item)
        for item in index.get("prompt_scenario_templates") or []
        if isinstance(item, dict)
    ]
    examples = [
        dict(item)
        for item in index.get("prompt_examples") or []
        if isinstance(item, dict)
    ]
    if example_limit is not None:
        examples = examples[: max(0, int(example_limit))]

    template_checks = [
        _template_coverage_check(template, available_tool_names=available_tool_names)
        for template in templates
    ]
    example_checks = [
        _example_coverage_check(example, available_tool_names=available_tool_names)
        for example in examples
    ]
    blockers = [
        check
        for check in template_checks
        if check.get("status") == "fail"
    ]
    needs_context_checks = [
        check
        for check in template_checks
        if check.get("status") == "needs_context"
    ]
    needs_context_contract = _needs_context_contract(needs_context_checks)
    warnings: list[dict[str, Any]] = []
    if guide_reference.get("schema") != "ncs_hrd_guide_reference_v1":
        warnings.append(
            {
                "code": "guide_reference_schema_mismatch",
                "severity": "medium",
                "schema": guide_reference.get("schema"),
                "message": "Guide reference index schema differs from the expected preprocessed schema.",
            }
        )
    if not templates:
        warnings.append(
            {
                "code": "missing_prompt_scenario_templates",
                "severity": "high",
                "message": "No prompt scenario templates were found in the guide reference index.",
            }
        )
    training_examples_without_match = [
        item for item in example_checks
        if item.get("section") == "training_system_building" and not item.get("matched_template_id")
    ]
    if training_examples_without_match:
        warnings.append(
            {
                "code": "training_system_prompt_examples_without_template_match",
                "severity": "low",
                "count": len(training_examples_without_match),
                "message": "Some training-system guide prompt examples did not match a prompt scenario template.",
            }
        )
    if needs_context_checks:
        warnings.append(
            {
                "code": "prompt_templates_need_user_context",
                "severity": "medium",
                "count": len(needs_context_checks),
                "template_ids": [str(item.get("template_id") or "") for item in needs_context_checks],
                "contract_schema": NEEDS_CONTEXT_CONTRACT_SCHEMA,
                "missing_params": {
                    str(item.get("template_id") or ""): item.get("missing_params") or []
                    for item in needs_context_checks
                },
                "context_requirement_codes": {
                    str(item.get("template_id") or ""): [
                        str(requirement.get("code") or "")
                        for requirement in item.get("context_requirements") or []
                        if isinstance(requirement, dict)
                    ]
                    for item in needs_context_checks
                },
                "message": "Some guide prompt templates route correctly but require user-provided context before delivery.",
                "action": "Collect or surface the missing context instead of treating these templates as fully specified requests.",
            }
        )
    return {
        "schema": PROMPT_COVERAGE_SCHEMA,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ok": not blockers and bool(templates),
        "guide_reference": guide_reference,
        "policy": {
            "template_checks_are_gate": True,
            "prompt_examples_are_observational": True,
            "guide_examples_are_not_source_training_data": True,
        },
        "needs_context_contract": needs_context_contract,
        "summary": {
            "template_total": len(template_checks),
            "template_passed": sum(1 for item in template_checks if item.get("passed")),
            "template_failed": len(blockers),
            "template_needs_context": sum(1 for item in template_checks if item.get("status") == "needs_context"),
            "template_needs_context_ids": [
                str(item.get("template_id") or "")
                for item in template_checks
                if item.get("status") == "needs_context"
            ],
            "prompt_example_total": len(example_checks),
            "prompt_examples_with_template_match": sum(1 for item in example_checks if item.get("matched_template_id")),
            "prompt_examples_without_template_match": sum(1 for item in example_checks if not item.get("matched_template_id")),
            "section_counts": _count_by(example_checks, "section"),
            "section_template_match_counts": _count_by(
                [item for item in example_checks if item.get("matched_template_id")],
                "section",
            ),
            "training_system_prompt_total": sum(
                1 for item in example_checks
                if item.get("section") == "training_system_building"
            ),
            "training_system_prompt_template_matches": sum(
                1 for item in example_checks
                if item.get("section") == "training_system_building" and item.get("matched_template_id")
            ),
            "out_of_active_training_scope_prompt_total": sum(
                1 for item in example_checks
                if item.get("section") != "training_system_building"
            ),
            "routed_tool_counts": _count_by(example_checks, "tool"),
            "matched_template_counts": _count_by(example_checks, "matched_template_id"),
        },
        "template_checks": template_checks,
        "prompt_example_observations": example_checks,
        "blockers": blockers,
        "warnings": warnings,
    }


def write_hrd_guide_prompt_coverage_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    guide = report.get("guide_reference") if isinstance(report.get("guide_reference"), dict) else {}
    lines = [
        "# HRD Guide Prompt Coverage",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- guide_schema: `{guide.get('schema')}`",
        f"- guide_hash: `{guide.get('source_hash_sha256')}`",
        f"- template_total: {summary.get('template_total', 0)}",
        f"- template_passed: {summary.get('template_passed', 0)}",
        f"- template_failed: {summary.get('template_failed', 0)}",
        f"- template_needs_context: {summary.get('template_needs_context', 0)}",
        f"- prompt_example_total: {summary.get('prompt_example_total', 0)}",
        f"- prompt_examples_with_template_match: {summary.get('prompt_examples_with_template_match', 0)}",
        f"- training_system_prompt_total: {summary.get('training_system_prompt_total', 0)}",
        f"- training_system_prompt_template_matches: {summary.get('training_system_prompt_template_matches', 0)}",
        "",
        "## Template Checks",
        "",
        "| Template | Expected Tool | Routed Tool | Status | Missing Params |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in report.get("template_checks") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {template} | {expected} | {tool} | {status} | {missing} |".format(
                template=item.get("template_id") or "",
                expected=item.get("expected_tool") or "",
                tool=item.get("tool") or "",
                status=item.get("status") or "",
                missing=", ".join(item.get("missing_params") or []),
            )
        )
    lines.extend(["", "## Prompt Example Observations", ""])
    for item in (report.get("prompt_example_observations") or [])[:20]:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- page {page}: tool `{tool}`, template `{template}`, snippet: {snippet}".format(
                page=item.get("page_number") or "",
                tool=item.get("tool") or "",
                template=item.get("matched_template_id") or "none",
                snippet=_markdown_inline(item.get("snippet") or "")[:180],
            )
        )
    if report.get("blockers"):
        lines.extend(["", "## Blockers", ""])
        for item in report.get("blockers") or []:
            lines.append(f"- `{item.get('template_id')}`: {', '.join(item.get('issues') or [])}")
    if report.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for item in report.get("warnings") or []:
            action = f" Action: {item.get('action')}" if item.get("action") else ""
            lines.append(f"- `{item.get('code')}`: {item.get('message')}{action}")
    needs_context = report.get("needs_context_contract")
    if isinstance(needs_context, dict):
        lines.extend(["", "## Needs Context Contract", ""])
        lines.append(f"- schema: `{needs_context.get('schema')}`")
        lines.append(f"- status: `{needs_context.get('status')}`")
        lines.append(f"- count: {needs_context.get('count', 0)}")
        lines.extend(["", "| Template | Missing Param | Requirement | Collection Stage |", "| --- | --- | --- | --- |"])
        for item in needs_context.get("items") or []:
            if not isinstance(item, dict):
                continue
            for requirement in item.get("context_requirements") or []:
                if not isinstance(requirement, dict):
                    continue
                lines.append(
                    "| {template} | {param} | {label} | {stage} |".format(
                        template=item.get("template_id") or "",
                        param=requirement.get("param") or "",
                        label=requirement.get("label") or "",
                        stage=requirement.get("collection_stage") or "",
                    )
                )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _template_coverage_check(
    template: dict[str, Any],
    *,
    available_tool_names: set[str] | None,
) -> dict[str, Any]:
    query = str(template.get("korean_example") or template.get("prompt_intent") or "")
    route = route_ncs_query(query, available_tool_names=available_tool_names)
    template_id = str(template.get("id") or "")
    expected_tool = str(template.get("expected_tool") or "")
    actual_template_id = str((route.get("guide_prompt_template") or {}).get("id") or "")
    missing_params = [str(item) for item in route.get("missing_params") or []]
    allowed_missing = sorted(TEMPLATE_ALLOWED_MISSING_PARAMS.get(template_id, set()))
    unexpected_missing = [item for item in missing_params if item not in set(allowed_missing)]
    issues: list[str] = []
    if expected_tool and route.get("tool") != expected_tool:
        issues.append("tool_mismatch")
    if actual_template_id != template_id:
        issues.append("template_mismatch")
    if unexpected_missing:
        issues.append("unexpected_missing_params")
    if issues:
        status = "fail"
    elif missing_params:
        status = "needs_context"
    else:
        status = "pass"
    context_requirements = [
        _context_requirement_for_param(param, allowed_deferred=param in set(allowed_missing))
        for param in missing_params
    ]
    return {
        "template_id": template_id,
        "query": query,
        "expected_tool": expected_tool,
        "tool": route.get("tool"),
        "scenario": route.get("scenario"),
        "matched_template_id": actual_template_id or None,
        "missing_params": missing_params,
        "allowed_missing_params": allowed_missing,
        "unexpected_missing_params": unexpected_missing,
        "context_requirements": context_requirements,
        "route_fingerprint": route.get("route_fingerprint"),
        "status": status,
        "passed": not issues,
        "issues": issues,
    }


def _example_coverage_check(
    example: dict[str, Any],
    *,
    available_tool_names: set[str] | None,
) -> dict[str, Any]:
    snippet = str(example.get("snippet") or "")
    route = route_ncs_query(snippet, available_tool_names=available_tool_names)
    guide_template = route.get("guide_prompt_template") if isinstance(route.get("guide_prompt_template"), dict) else {}
    return {
        "page_number": example.get("page_number"),
        "section": example.get("section"),
        "stage_candidates": example.get("stage_candidates") or [],
        "snippet": snippet,
        "tool": route.get("tool"),
        "scenario": route.get("scenario"),
        "matched_template_id": guide_template.get("id"),
        "missing_params": route.get("missing_params") or [],
        "route_fingerprint": route.get("route_fingerprint"),
    }


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = item.get(key)
        label = str(value or "none")
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


def _needs_context_contract(needs_context_checks: list[dict[str, Any]]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    missing_param_counts: dict[str, int] = {}
    requirement_code_counts: dict[str, int] = {}
    for check in needs_context_checks:
        requirements = [
            dict(item)
            for item in check.get("context_requirements") or []
            if isinstance(item, dict)
        ]
        for param in check.get("missing_params") or []:
            param_key = str(param or "")
            if param_key:
                missing_param_counts[param_key] = missing_param_counts.get(param_key, 0) + 1
        for requirement in requirements:
            code = str(requirement.get("code") or "")
            if code:
                requirement_code_counts[code] = requirement_code_counts.get(code, 0) + 1
        items.append(
            {
                "template_id": check.get("template_id"),
                "expected_tool": check.get("expected_tool"),
                "tool": check.get("tool"),
                "missing_params": check.get("missing_params") or [],
                "allowed_missing_params": check.get("allowed_missing_params") or [],
                "context_requirements": requirements,
                "delivery_policy": "collect_or_surface_before_delivery",
            }
        )
    return {
        "schema": NEEDS_CONTEXT_CONTRACT_SCHEMA,
        "status": "needs_context" if items else "complete",
        "count": len(items),
        "missing_param_counts": dict(sorted(missing_param_counts.items())),
        "requirement_code_counts": dict(sorted(requirement_code_counts.items())),
        "items": items,
    }


def _context_requirement_for_param(param: str, *, allowed_deferred: bool) -> dict[str, Any]:
    normalized = str(param or "")
    if normalized == "current_query":
        return {
            "code": "job_scope_required",
            "param": normalized,
            "label": "Current job or NCS scope",
            "guide_check": "job_scope",
            "guide_stage": "C1-1",
            "collection_stage": "scope_confirmation",
            "allowed_deferred": allowed_deferred,
            "review_state": "needs_user_context",
            "prompt": "Collect the current job, task, or NCS scope before building the education-system output.",
        }
    if normalized == "target_query":
        return {
            "code": "target_scope_required",
            "param": normalized,
            "label": "Target job or NCS scope",
            "guide_check": "job_scope",
            "guide_stage": "C1-1",
            "collection_stage": "scope_confirmation",
            "allowed_deferred": allowed_deferred,
            "review_state": "needs_user_context",
            "prompt": "Collect the target job, task, or NCS scope before building transition training.",
        }
    return {
        "code": "missing_route_parameter",
        "param": normalized,
        "label": normalized or "unknown",
        "guide_check": "human_review",
        "guide_stage": "C1-1",
        "collection_stage": "intake",
        "allowed_deferred": allowed_deferred,
        "review_state": "needs_user_context",
        "prompt": "Collect the missing route parameter before treating the template as fully specified.",
    }


def _markdown_inline(value: Any) -> str:
    return " ".join(str(value or "").replace("|", "\\|").split())


def write_hrd_guide_prompt_coverage_json(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
