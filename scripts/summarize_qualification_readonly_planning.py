from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CHECKPOINT_RE = re.compile(r"--ncs006-checkpoint-path\s+([^\s]+)")
FORBIDDEN_HUMAN_REVIEW_STATUSES = {"human_reviewed", "accepted", "reviewed"}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _write_json(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _guard_policy(plan: dict[str, Any]) -> dict[str, Any]:
    value = plan.get("guard_policy")
    return value if isinstance(value, dict) else {}


def _current_state(plan: dict[str, Any]) -> dict[str, Any]:
    value = plan.get("current_state")
    return value if isinstance(value, dict) else {}


def _target_state(plan: dict[str, Any]) -> dict[str, Any]:
    value = plan.get("target_state")
    return value if isinstance(value, dict) else {}


def _batches(plan: dict[str, Any]) -> list[dict[str, Any]]:
    batches = plan.get("batches")
    if not isinstance(batches, list):
        return []
    return [item for item in batches if isinstance(item, dict)]


def _checkpoint_path_from_batches(batches: list[dict[str, Any]]) -> str | None:
    for batch in batches:
        command = batch.get("command")
        if not isinstance(command, str):
            continue
        match = CHECKPOINT_RE.search(command)
        if match:
            return match.group(1)
    return None


def _coverage_gap_open(plan: dict[str, Any]) -> bool:
    current = _current_state(plan)
    target = _target_state(plan)
    coverage = float(current.get("collection_coverage") or 0.0)
    target_ratio = float(plan.get("target_ratio") or 0.0)
    additional_needed = int(target.get("additional_attempted_units_needed") or 0)
    return additional_needed > 0 or coverage < target_ratio


def _normalized_collection_status(plan: dict[str, Any]) -> str:
    if not _coverage_gap_open(plan):
        return "target_coverage_met_no_collection_needed"
    guard = _guard_policy(plan)
    if guard.get("operator_timing_required") is True:
        return "operator_timed_collection_required"
    return "coverage_gap_open_guard_review_required"


def _readonly_plan_contract_issues(plan: dict[str, Any], guard: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if plan.get("schema") != "ncs_qualification_collection_coverage_plan_v1":
        issues.append("schema")
    if plan.get("ok") is not True:
        issues.append("ok")
    if plan.get("report_only") is not True:
        issues.append("report_only")
    for key in (
        "status_update_allowed",
        "db_writes",
        "api_calls",
        "human_review_status_updates",
        "automatic_collection_allowed_now",
        "automatic_queue_execution_allowed",
        "approval_claim",
        "execution_authorized",
    ):
        if key not in plan:
            issues.append(f"missing:{key}")
        elif plan.get(key) is not False:
            issues.append(key)
    if "operator_timed_guarded_api_commands_only" not in plan:
        issues.append("missing:operator_timed_guarded_api_commands_only")
    elif plan.get("operator_timed_guarded_api_commands_only") is not True:
        issues.append("operator_timed_guarded_api_commands_only")
    if int(plan.get("unsafe_batch_count") or 0) != 0:
        issues.append("unsafe_batch_count")
    if guard.get("must_run_qualification_retry_hygiene_first") is not True:
        issues.append("guard_policy.must_run_qualification_retry_hygiene_first")
    if guard.get("must_use_ncs006_checkpoint_path") is not True:
        issues.append("guard_policy.must_use_ncs006_checkpoint_path")
    if guard.get("must_not_write_human_review_statuses") is not True:
        issues.append("guard_policy.must_not_write_human_review_statuses")
    if guard.get("operator_must_confirm_api_timing") is not True:
        issues.append("guard_policy.operator_must_confirm_api_timing")
    if guard.get("batch_commands_are_operator_timed") is not True:
        issues.append("guard_policy.batch_commands_are_operator_timed")
    if guard.get("batch_commands_are_not_queue_items") is not True:
        issues.append("guard_policy.batch_commands_are_not_queue_items")
    if guard.get("operator_timing_required") is not True:
        issues.append("guard_policy.operator_timing_required")
    if guard.get("automatic_queue_execution_allowed") is not False:
        issues.append("guard_policy.automatic_queue_execution_allowed")
    forbidden_statuses = [str(value) for value in guard.get("forbidden_status_updates") or []]
    if forbidden_statuses != ["human_reviewed", "accepted", "reviewed"]:
        issues.append("guard_policy.forbidden_status_updates")
    for index, batch in enumerate(_batches(plan), start=1):
        if batch.get("auto_runnable") is not False:
            issues.append(f"batch[{index}].auto_runnable")
        if batch.get("automatic_queue_execution_allowed") is not False:
            issues.append(f"batch[{index}].automatic_queue_execution_allowed")
        if batch.get("execution_authorized") is not False:
            issues.append(f"batch[{index}].execution_authorized")
        if batch.get("do_not_execute_from_report") is not True:
            issues.append(f"batch[{index}].do_not_execute_from_report")
        if batch.get("not_queue_item") is not True:
            issues.append(f"batch[{index}].not_queue_item")
        if batch.get("requires_operator_ticket") is not True:
            issues.append(f"batch[{index}].requires_operator_ticket")
        if batch.get("requires_explicit_operator_start") is not True:
            issues.append(f"batch[{index}].requires_explicit_operator_start")
        if batch.get("requires_operator_timing") is not True:
            issues.append(f"batch[{index}].requires_operator_timing")
        if batch.get("guard_required") is not True:
            issues.append(f"batch[{index}].guard_required")
        if batch.get("mutation_policy") != "guarded_api_collection":
            issues.append(f"batch[{index}].mutation_policy")
        if batch.get("command_role") != "operator_timed_guarded_api_collection":
            issues.append(f"batch[{index}].command_role")
    return issues


def build_summary(
    *,
    coverage_plan: dict[str, Any],
    retry_hygiene: dict[str, Any] | None = None,
    coverage_plan_path: str | None = None,
    retry_hygiene_path: str | None = None,
) -> dict[str, Any]:
    current = _current_state(coverage_plan)
    target = _target_state(coverage_plan)
    guard = _guard_policy(coverage_plan)
    batches = _batches(coverage_plan)
    retry_hygiene = retry_hygiene or {}
    coverage_gap_open = _coverage_gap_open(coverage_plan)
    next_safe_action_status = _normalized_collection_status(coverage_plan)
    contract_issues = _readonly_plan_contract_issues(coverage_plan, guard)
    retry_candidate_count = int(
        retry_hygiene.get("retry_candidate_unit_count")
        or retry_hygiene.get("retry_ready_unit_count")
        or 0
    )
    qualification_retry_allowed = retry_hygiene.get("qualification_retry_allowed_now")
    retry_needed_now = retry_candidate_count > 0
    retry_preflight_clear_only = bool(qualification_retry_allowed) and not retry_needed_now
    execution_readiness_warnings: list[str] = []
    if retry_preflight_clear_only:
        execution_readiness_warnings.append(
            "qualification_retry_allowed_now_is_retry_preflight_only_not_collection_authorization"
        )
    if coverage_gap_open and batches:
        execution_readiness_warnings.append(
            "coverage_gap_requires_operator_timed_guarded_collection_not_automatic_execution"
        )
    if any(isinstance(batch.get("command"), str) and batch.get("command") for batch in batches):
        execution_readiness_warnings.append(
            "operator_commands_present_but_execution_authorized_false"
        )

    return {
        "schema": "ncs_qualification_readonly_planning_summary_v1",
        "generated_at": _now(),
        "ok": bool(coverage_plan.get("ok")),
        "ok_meaning": "read-only artifact shape only; not execution approval",
        "artifact_ok": bool(coverage_plan.get("ok")),
        "contract_ok": not contract_issues,
        "read_only_contract_ok": not contract_issues,
        "input_contract_issues": contract_issues,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "human_review_status_updates": False,
        "approval_claim": False,
        "execution_authorized": False,
        "execution_ready": False,
        "authorization_status": "not_authorized_read_only_report",
        "execution_readiness_warnings": execution_readiness_warnings,
        "human_start_required": True,
        "coverage_plan_path": coverage_plan_path,
        "retry_hygiene_path": retry_hygiene_path,
        "coverage": current.get("collection_coverage"),
        "attempted_unit_count": current.get("attempted_unit_count"),
        "total_unit_count": current.get("total_unit_count"),
        "unattempted_unit_count": current.get("unattempted_unit_count"),
        "target_ratio": coverage_plan.get("target_ratio"),
        "additional_attempted_units_needed": target.get("additional_attempted_units_needed"),
        "batch_count": coverage_plan.get("batch_count", len(batches)),
        "unsafe_batch_count": coverage_plan.get("unsafe_batch_count", 0),
        "coverage_gap_open": coverage_gap_open,
        "collection_execution_status": next_safe_action_status,
        "next_safe_action_status": next_safe_action_status,
        "next_safe_action_status_meaning": (
            "Top-level status describes remaining coverage collection work. "
            "Retry-only hygiene status is reported separately."
        ),
        "retry_error_next_safe_action_status": retry_hygiene.get("next_safe_action_status"),
        "retry_candidate_unit_count": retry_candidate_count,
        "retry_needed_now": retry_needed_now,
        "qualification_retry_allowed_now": qualification_retry_allowed,
        "qualification_retry_allowed_meaning": (
            "Retry preflight is clear for retry candidates only; this is not "
            "coverage collection authorization."
        ),
        "retry_preflight_clear_only": retry_preflight_clear_only,
        "retry_collection_authorized": False,
        "blocked_by_checkpoint": retry_hygiene.get("blocked_by_checkpoint"),
        "automatic_collection_allowed_now": False,
        "automatic_queue_execution_allowed": False,
        "operator_timed_guarded_api_commands_only": bool(
            coverage_plan.get("operator_timed_guarded_api_commands_only")
        ),
        "operator_timing_required": bool(guard.get("operator_timing_required")),
        "must_run_qualification_retry_hygiene_first": bool(
            guard.get("must_run_qualification_retry_hygiene_first")
        ),
        "must_use_ncs006_checkpoint_path": bool(
            guard.get("must_use_ncs006_checkpoint_path")
        ),
        "must_not_write_human_review_statuses": bool(
            guard.get("must_not_write_human_review_statuses")
        ),
        "operator_must_confirm_api_timing": bool(
            guard.get("operator_must_confirm_api_timing")
        ),
        "batch_commands_are_operator_timed": bool(
            guard.get("batch_commands_are_operator_timed")
        ),
        "batch_commands_are_not_queue_items": bool(
            guard.get("batch_commands_are_not_queue_items")
        ),
        "forbidden_status_updates": guard.get("forbidden_status_updates") or [],
        "checkpoint_path": _checkpoint_path_from_batches(batches),
    }


def build_timing_schedule(
    *,
    coverage_plan: dict[str, Any],
    coverage_plan_path: str | None = None,
    wave_size: int = 5,
) -> dict[str, Any]:
    if wave_size < 1:
        raise ValueError("wave_size must be positive")

    current = _current_state(coverage_plan)
    target = _target_state(coverage_plan)
    guard = _guard_policy(coverage_plan)
    batches = _batches(coverage_plan)
    checkpoint_path = _checkpoint_path_from_batches(batches)
    waves: list[dict[str, Any]] = []
    for wave_index, start in enumerate(range(0, len(batches), wave_size), start=1):
        wave_batches = batches[start : start + wave_size]
        limit_units = [
            int(item.get("limit_units") or 0)
            for item in wave_batches
            if isinstance(item.get("limit_units"), int)
            or str(item.get("limit_units", "")).isdigit()
        ]
        commands = [item.get("command") for item in wave_batches if isinstance(item.get("command"), str)]
        waves.append(
            {
                "wave_index": wave_index,
                "batch_start": start + 1,
                "batch_end": start + len(wave_batches),
                "batch_count": len(wave_batches),
                "max_units": sum(limit_units),
                "operator_command": commands[0] if commands else None,
                "operator_commands": commands,
                "operator_command_template": commands[0] if commands else None,
                "operator_command_templates": commands,
                "repeat_command_count": len(commands),
                "operator_command_authorized": False,
                "requires_preflight_before_wave": True,
                "requires_checkpoint_review_after_wave": True,
                "auto_runnable": False,
                "execution_authorized": False,
                "do_not_execute_from_report": True,
                "not_queue_item": True,
                "requires_operator_ticket": True,
                "human_start_required": True,
            }
        )

    batch_count = len(batches)
    return {
        "schema": "ncs_qualification_operator_timing_schedule_v1",
        "generated_at": _now(),
        "report_only": True,
        "db_writes": False,
        "api_calls": False,
        "status_update_allowed": False,
        "approval_claim": False,
        "execution_authorized": False,
        "execution_ready": False,
        "authorization_status": "not_authorized_read_only_report",
        "automatic_queue_execution_allowed": False,
        "do_not_execute_from_report": True,
        "not_queue_item": True,
        "requires_operator_ticket": True,
        "human_start_required": True,
        "source_plan": coverage_plan_path,
        "checkpoint_path": checkpoint_path,
        "coverage": current.get("collection_coverage"),
        "target_ratio": coverage_plan.get("target_ratio"),
        "additional_attempted_units_needed": target.get("additional_attempted_units_needed"),
        "batch_count": coverage_plan.get("batch_count", batch_count),
        "wave_size": wave_size,
        "wave_count": len(waves),
        "estimated_minimum_runtime_hours": round(batch_count * 200 / 3600, 2),
        "estimated_conservative_runtime_hours": round(batch_count * 292 / 3600, 2),
        "operator_policy": {
            "automatic_collection_allowed_now": False,
            "automatic_queue_execution_allowed": False,
            "requires_explicit_operator_start": True,
            "execution_authorized": False,
            "execution_ready": False,
            "authorization_status": "not_authorized_read_only_report",
            "do_not_execute_from_report": True,
            "not_queue_item": True,
            "requires_operator_ticket": True,
            "human_start_required": True,
            "requires_operator_timing": bool(guard.get("operator_timing_required", True)),
            "must_run_preflight_before_each_wave": True,
            "must_not_write_human_review_statuses": bool(
                guard.get("must_not_write_human_review_statuses", True)
            ),
            "forbidden_status_updates": guard.get("forbidden_status_updates") or [],
        },
        "preflight_commands": [
            (
                "python scripts\\ncs_harness.py qualification-retry-hygiene "
                f"--ncs006-checkpoint-path {checkpoint_path or '<checkpoint>'} "
                "--out reports\\qualification_retry_hygiene_<DATE>.json "
                "--markdown-out reports\\qualification_retry_hygiene_<DATE>.md"
            ),
            (
                "python scripts\\ncs_harness.py agent-queue-status "
                "--queue reports\\aihr_agent_queue_<DATE>.json "
                "--out reports\\aihr_agent_queue_status_<DATE>.json "
                "--markdown-out reports\\aihr_agent_queue_status_<DATE>.md"
            ),
        ],
        "stop_conditions": [
            "rate_limit_errors_reach_stop_threshold",
            "checkpoint_safety_violation_detected",
            "human_review_status_write_requested",
            "unexpected_db_write_surface_detected",
            "operator_timing_window_closed",
        ],
        "post_wave_checks": [
            "Regenerate qualification-summary.",
            "Regenerate qualification-retry-hygiene.",
            "Regenerate qualification-coverage-plan.",
            "Confirm no human_reviewed, accepted, or reviewed statuses were written.",
        ],
        "largest_major_gaps": coverage_plan.get("major_gaps") or [],
        "waves": waves,
    }


def write_summary_markdown(summary: dict[str, Any], path: Path) -> None:
    keys = [
        "ok",
        "ok_meaning",
        "artifact_ok",
        "contract_ok",
        "read_only_contract_ok",
        "input_contract_issues",
        "report_only",
        "status_update_allowed",
        "db_writes",
        "api_calls",
        "human_review_status_updates",
        "approval_claim",
        "execution_authorized",
        "execution_ready",
        "authorization_status",
        "execution_readiness_warnings",
        "human_start_required",
        "coverage",
        "attempted_unit_count",
        "total_unit_count",
        "additional_attempted_units_needed",
        "batch_count",
        "unsafe_batch_count",
        "coverage_gap_open",
        "next_safe_action_status",
        "retry_error_next_safe_action_status",
        "retry_candidate_unit_count",
        "retry_needed_now",
        "qualification_retry_allowed_now",
        "qualification_retry_allowed_meaning",
        "retry_preflight_clear_only",
        "retry_collection_authorized",
        "automatic_collection_allowed_now",
        "automatic_queue_execution_allowed",
        "operator_timed_guarded_api_commands_only",
        "operator_timing_required",
        "must_run_qualification_retry_hygiene_first",
        "must_use_ncs006_checkpoint_path",
        "must_not_write_human_review_statuses",
        "operator_must_confirm_api_timing",
        "batch_commands_are_operator_timed",
        "batch_commands_are_not_queue_items",
        "checkpoint_path",
    ]
    lines = ["# Qualification Read-Only Planning Summary", ""]
    for key in keys:
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(
        [
            "",
            "## Status Meaning",
            "",
            "- `next_safe_action_status` describes remaining coverage collection work.",
            "- `retry_error_next_safe_action_status` describes retry-only hygiene state.",
            "- `ok=true` means the read-only artifact shape is valid; it is not execution approval.",
            "- This report is read-only and does not authorize API calls, DB writes, or human-review status updates.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_timing_markdown(schedule: dict[str, Any], path: Path) -> None:
    lines = [
        "# Qualification Operator Timing Schedule",
        "",
        f"- report_only: `{schedule.get('report_only')}`",
        f"- db_writes: `{schedule.get('db_writes')}`",
        f"- api_calls: `{schedule.get('api_calls')}`",
        f"- status_update_allowed: `{schedule.get('status_update_allowed')}`",
        f"- execution_authorized: `{schedule.get('execution_authorized')}`",
        f"- execution_ready: `{schedule.get('execution_ready')}`",
        f"- authorization_status: `{schedule.get('authorization_status')}`",
        f"- automatic_queue_execution_allowed: `{schedule.get('automatic_queue_execution_allowed')}`",
        f"- do_not_execute_from_report: `{schedule.get('do_not_execute_from_report')}`",
        f"- not_queue_item: `{schedule.get('not_queue_item')}`",
        f"- requires_operator_ticket: `{schedule.get('requires_operator_ticket')}`",
        f"- human_start_required: `{schedule.get('human_start_required')}`",
        f"- checkpoint_path: `{schedule.get('checkpoint_path')}`",
        f"- coverage: `{schedule.get('coverage')}`",
        f"- target_ratio: `{schedule.get('target_ratio')}`",
        f"- additional_attempted_units_needed: `{schedule.get('additional_attempted_units_needed')}`",
        f"- batch_count: `{schedule.get('batch_count')}`",
        f"- wave_count: `{schedule.get('wave_count')}`",
        f"- estimated_conservative_runtime_hours: `{schedule.get('estimated_conservative_runtime_hours')}`",
        "",
        "## Operator Policy",
        "",
    ]
    for key, value in (schedule.get("operator_policy") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Waves", ""])
    for wave in schedule.get("waves") or []:
        lines.append(
            f"- wave {wave.get('wave_index')}: batches {wave.get('batch_start')}-{wave.get('batch_end')}, "
            f"max_units `{wave.get('max_units')}`, auto_runnable `{wave.get('auto_runnable')}`"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--coverage-plan", required=True)
    parser.add_argument("--retry-hygiene")
    parser.add_argument("--summary-out", required=True)
    parser.add_argument("--summary-markdown-out", required=True)
    parser.add_argument("--timing-out")
    parser.add_argument("--timing-markdown-out")
    parser.add_argument("--wave-size", type=int, default=5)
    args = parser.parse_args()

    if bool(args.timing_out) != bool(args.timing_markdown_out):
        parser.error("--timing-out and --timing-markdown-out must be provided together")

    coverage_path = Path(args.coverage_plan)
    coverage_plan = _read_json(coverage_path)
    retry_hygiene = _read_json(Path(args.retry_hygiene)) if args.retry_hygiene else None

    summary = build_summary(
        coverage_plan=coverage_plan,
        retry_hygiene=retry_hygiene,
        coverage_plan_path=str(coverage_path),
        retry_hygiene_path=args.retry_hygiene,
    )
    _write_json(summary, Path(args.summary_out))
    write_summary_markdown(summary, Path(args.summary_markdown_out))

    if not summary.get("contract_ok"):
        return 2

    if args.timing_out or args.timing_markdown_out:
        timing = build_timing_schedule(
            coverage_plan=coverage_plan,
            coverage_plan_path=str(coverage_path),
            wave_size=args.wave_size,
        )
        _write_json(timing, Path(args.timing_out))
        write_timing_markdown(timing, Path(args.timing_markdown_out))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
