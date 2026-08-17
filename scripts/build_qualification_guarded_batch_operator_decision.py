from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
SCHEMA = "qualification_guarded_batch_operator_decision_v1"
AUDIT_SCHEMA = "qualification_guarded_batch_operator_decision_audit_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
CSV_FIELDS = [
    "wave",
    "batch_count",
    "max_units_attempted",
    "estimated_min_runtime_minutes_before_retries",
    "planning_window_minutes_with_operator_buffer",
    "requires_operator_start",
    "purpose",
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def portable_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    resolved = Path(path).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def declared_hash_scope(path: Path) -> str | None:
    if path.suffix.lower() != ".json" or not path.is_file():
        return None
    try:
        payload = read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    scope = str(payload.get("sha256_scope") or "").strip()
    value = str(payload.get("cycle_safe_content_sha256") or "").strip()
    if scope == "cycle_safe_release_readiness" and re.fullmatch(
        r"sha256:[0-9a-f]{64}", value
    ):
        return scope
    return None


def sha256_artifact(path: Path, *, scope: str | None = None) -> str | None:
    if scope == "cycle_safe_release_readiness":
        try:
            payload = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if payload.get("sha256_scope") != scope:
            return None
        value = str(payload.get("cycle_safe_content_sha256") or "").strip()
        return value if re.fullmatch(r"sha256:[0-9a-f]{64}", value) else None
    return sha256_file(path)


def dated_artifact_sort_key(path: Path) -> tuple[int, str, float]:
    match = re.search(r"(20\d{6}(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?)", path.stem)
    stamp = match.group(1) if match else ""
    date = int(stamp[:8]) if stamp[:8].isdigit() else 0
    return date, stamp, path.stat().st_mtime


def latest_report_path(
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
    stamp: str | None = None,
) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            if any(token in path.name for token in exclude_substrings):
                continue
            if stamp and stamp not in path.stem:
                continue
            candidates.append(path)
            seen.add(path)
    if not candidates:
        raise FileNotFoundError(f"no report artifact matched: {patterns}")
    return max(candidates, key=dated_artifact_sort_key)


def resolve_artifact(value: Path, *, root: Path = PROJECT_ROOT) -> Path:
    if value.is_absolute():
        return value
    rooted = root / value
    if rooted.exists():
        return rooted
    return value


def input_or_latest(
    value: Path | None,
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
    root: Path = PROJECT_ROOT,
    stamp: str | None = None,
) -> Path:
    if value is not None:
        return resolve_artifact(value, root=root)
    return latest_report_path(
        *patterns,
        reports_dir=reports_dir,
        exclude_substrings=exclude_substrings,
        stamp=stamp,
    )


def safe_bool_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


def safe_bool_true(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def int_or_zero(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def compact_batch_templates(batches: list[dict[str, Any]], *, limit: int = 3) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for batch in batches[:limit]:
        templates.append(
            {
                "batch_index": batch.get("batch_index"),
                "limit_units": batch.get("limit_units"),
                "command": batch.get("command"),
                "execution_authorized": False,
                "automatic_queue_execution_allowed": False,
                "requires_operator_ticket": True,
                "requires_explicit_operator_start": True,
                "requires_operator_timing": True,
                "mutation_policy": "guarded_api_collection",
            }
        )
    return templates


def build_wave_plan(
    *,
    batch_count: int,
    batch_size: int,
    additional_attempted_units_needed: int,
) -> list[dict[str, Any]]:
    first_wave_batches = min(10, batch_count) if batch_count else 0
    pilot_batches = min(3, batch_count) if batch_count else 0

    def runtime_minutes(units: int) -> float:
        # The guarded collection command uses --request-delay 2 and max one page
        # per unit. Keep this as a conservative planning estimate, not execution proof.
        return round(units * 0.033, 1)

    def planning_window(units: int) -> float:
        return round(runtime_minutes(units) + max(45.0, runtime_minutes(units) * 4.5), 1)

    waves = [
        (
            "pilot",
            pilot_batches,
            min(pilot_batches * batch_size, additional_attempted_units_needed),
            "Validate API timing, checkpoint continuity, and status summaries before a longer run.",
        ),
        (
            "first_wave",
            first_wave_batches,
            min(first_wave_batches * batch_size, additional_attempted_units_needed),
            "Move coverage materially while still keeping one operator window reviewable.",
        ),
        (
            "target_completion",
            batch_count,
            additional_attempted_units_needed,
            "Reach 90% attempted-unit target if provider limits remain stable.",
        ),
    ]
    return [
        {
            "wave": wave,
            "batch_count": batches,
            "max_units_attempted": units,
            "estimated_min_runtime_minutes_before_retries": runtime_minutes(units),
            "planning_window_minutes_with_operator_buffer": planning_window(units),
            "purpose": purpose,
            "requires_operator_start": True,
        }
        for wave, batches, units, purpose in waves
    ]


def build_operator_decisions(checkpoint_path: str) -> list[dict[str, Any]]:
    return [
        {
            "decision_id": "qualification_api_timing",
            "label": "API timing approval",
            "required": True,
            "default_for_automation": "do_not_run",
            "operator_question": (
                "Confirm a guarded API collection window before running any qualification batch."
            ),
            "evidence_to_check": [
                "retry_hygiene_ok=true",
                "unsafe_batch_count=0",
                "checkpoint_command_consistency all true",
                "current API/provider rate-limit condition acceptable",
            ],
            "forbidden_automation": [
                "automatic_queue_execution_allowed",
                *FORBIDDEN_AUTOMATIC_STATUSES,
            ],
        },
        {
            "decision_id": "pilot_batch_count",
            "label": "pilot batch count",
            "required": True,
            "recommended_first_value": 3,
            "reason": (
                "Start with a small pilot before expanding toward the full coverage target."
            ),
        },
        {
            "decision_id": "stop_conditions",
            "label": "stop conditions",
            "required": True,
            "recommended_conditions": [
                "rate-limit errors reach --stop-after-rate-limit-errors threshold",
                "checkpoint path mismatch or checkpoint write failure",
                "unexpected DB status update outside qualification collection tables",
                "command output indicates credential/provider failure",
            ],
        },
        {
            "decision_id": "post_run_verification",
            "label": "post-run verification",
            "required": True,
            "recommended_commands": [
                "python scripts\\ncs_harness.py qualification-summary",
                (
                    "python scripts\\ncs_harness.py qualification-retry-hygiene "
                    f"--ncs006-checkpoint-path {checkpoint_path} "
                    "--out reports\\qualification_retry_hygiene_after_operator_batch_<DATE>.json "
                    "--markdown-out reports\\qualification_retry_hygiene_after_operator_batch_<DATE>.md"
                ),
                (
                    "python scripts\\ncs_harness.py qualification-coverage-plan "
                    f"--target-ratio 0.9 --batch-size 100 --ncs006-checkpoint-path {checkpoint_path} "
                    "--out reports\\qualification_collection_coverage_plan_after_operator_batch_<DATE>.json "
                    "--markdown-out reports\\qualification_collection_coverage_plan_after_operator_batch_<DATE>.md "
                    "--csv-out reports\\qualification_collection_coverage_plan_after_operator_batch_<DATE>.csv"
                ),
            ],
        },
    ]


def build_decision_packet(
    *,
    coverage_plan_path: Path,
    retry_hygiene_path: Path,
    release_readiness_path: Path,
    queue_status_path: Path,
    queue_run_path: Path,
    generated_at: str | None = None,
) -> dict[str, Any]:
    coverage_plan = read_json(coverage_plan_path)
    retry_hygiene = read_json(retry_hygiene_path)
    release_readiness = read_json(release_readiness_path)
    queue_status = read_json(queue_status_path)
    queue_run = read_json(queue_run_path)

    current_state = dict(coverage_plan.get("current_state") or retry_hygiene.get("coverage_gap") or {})
    target_state = dict(coverage_plan.get("target_state") or {})
    batch_count = int_or_zero(coverage_plan.get("batch_count") or target_state.get("estimated_batch_count"))
    batch_size = int_or_zero(coverage_plan.get("batch_size")) or 100
    additional_needed = int_or_zero(target_state.get("additional_attempted_units_needed"))
    batches = [dict(batch) for batch in coverage_plan.get("batches") or [] if isinstance(batch, dict)]
    first_command = str((batches[0] if batches else {}).get("command") or "")
    checkpoint_path = str(coverage_plan.get("checkpoint_path") or retry_hygiene.get("checkpoint_path") or "")

    source_path_objects = {
        "coverage_plan": coverage_plan_path,
        "retry_hygiene": retry_hygiene_path,
        "release_readiness": release_readiness_path,
        "queue_status": queue_status_path,
        "queue_run": queue_run_path,
    }
    release_blockers = [
        blocker
        for blocker in release_readiness.get("blockers", [])
        if isinstance(blocker, dict) and blocker.get("name") == "qualification:collection_coverage"
    ]
    api_guard = retry_hygiene.get("api_execution_guard") or {}
    preflight_summary = {
        "retry_hygiene_ok": retry_hygiene.get("ok"),
        "retry_error_unit_count": retry_hygiene.get("error_unit_count"),
        "retry_candidate_unit_count": retry_hygiene.get("retry_candidate_unit_count"),
        "retry_ready_unit_count": retry_hygiene.get("retry_ready_unit_count"),
        "retry_broad_retry_risk": retry_hygiene.get("broad_retry_risk"),
        "retry_api_call_allowed_now": retry_hygiene.get("api_call_allowed_now"),
        "retry_collection_authorized": retry_hygiene.get("retry_collection_authorized"),
        "coverage_plan_ok": coverage_plan.get("ok"),
        "unsafe_batch_count": coverage_plan.get("unsafe_batch_count"),
        "automatic_collection_allowed_now": coverage_plan.get("automatic_collection_allowed_now"),
        "execution_authorized": False,
        "operator_timed_guarded_api_commands_only": coverage_plan.get(
            "operator_timed_guarded_api_commands_only"
        ),
        "checkpoint_path": checkpoint_path,
        "checkpoint_command_consistency": {
            "batch_commands_have_ncs006_checkpoint_values": coverage_plan.get(
                "batch_commands_have_ncs006_checkpoint_values"
            ),
            "batch_commands_checkpoint_values_match_plan": coverage_plan.get(
                "batch_commands_checkpoint_values_match_plan"
            ),
            "batch_commands_unique_checkpoint_paths": coverage_plan.get(
                "batch_commands_unique_checkpoint_paths"
            ),
        },
        "api_guard_status": api_guard.get("status"),
        "api_guard_safety_violation_count": len(api_guard.get("safety_violations") or []),
    }

    report = {
        "schema": SCHEMA,
        "generated_at": generated_at or now_iso(),
        "ok": True,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "execution_authorized": False,
        "automatic_queue_execution_allowed": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_paths": {
            key: portable_path(path) for key, path in source_path_objects.items()
        },
        "source_hashes": {
            key: sha256_artifact(path, scope=declared_hash_scope(path))
            for key, path in source_path_objects.items()
        },
        "source_hash_scopes": {
            key: scope
            for key, path in source_path_objects.items()
            if (scope := declared_hash_scope(path))
        },
        "release_blockers": release_blockers,
        "preflight_summary": preflight_summary,
        "coverage_state": current_state,
        "target_state": target_state,
        "batch_summary": {
            "batch_count": batch_count,
            "batch_size": batch_size,
            "additional_attempted_units_needed": additional_needed,
            "first_batch_command_templates": compact_batch_templates(batches),
            "commands_are_identical_templates": len({str(batch.get("command")) for batch in batches}) == 1
            if batches
            else False,
            "operator_must_rerun_status_between_waves": True,
        },
        "priority_major_gaps": list(coverage_plan.get("major_gaps") or [])[:12],
        "wave_plan": build_wave_plan(
            batch_count=batch_count,
            batch_size=batch_size,
            additional_attempted_units_needed=additional_needed,
        ),
        "operator_decisions": build_operator_decisions(checkpoint_path),
        "queue_snapshot": {
            "queue_ready": queue_status.get("queue_ready"),
            "blocked_count": queue_status.get("blocked_count"),
            "manual_ready_count": queue_status.get("manual_ready_count"),
            "auto_startable_count": queue_status.get("auto_startable_count"),
            "queue_run_actual_run": queue_run.get("actual_run"),
            "queue_run_selected_count": queue_run.get("selected_count"),
            "queue_run_failed_count": queue_run.get("failed_count"),
        },
        "command_template": first_command,
        "non_goals": [
            "This artifact does not authorize API execution.",
            "This artifact does not write qualification collection rows.",
            "This artifact does not set human_reviewed, accepted, or reviewed.",
            "This artifact does not claim qualification coverage blocker is resolved.",
        ],
        "next_evidence_after_operator_run": [
            "Updated qualification summary counts",
            "Updated retry hygiene report with zero unexpected safety violations",
            "Updated coverage plan showing reduced additional_attempted_units_needed",
            "Release-readiness report regenerated after dashboard/static artifact verification",
        ],
    }
    return report


def audit_decision_packet(report: dict[str, Any], *, base_dir: Path = PROJECT_ROOT) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    def add_issue(code: str, **extra: Any) -> None:
        issue = {"code": code}
        issue.update(extra)
        issues.append(issue)

    for field in ("report_only", "human_decision_required"):
        if not safe_bool_true(report.get(field)):
            add_issue("required_true_field_not_true", field=field, value=report.get(field))
    for field in (
        "status_update_allowed",
        "db_writes",
        "api_calls",
        "execution_authorized",
        "automatic_queue_execution_allowed",
        "approval_claim",
    ):
        if not safe_bool_false(report.get(field)):
            add_issue("required_false_field_not_false", field=field, value=report.get(field))

    forbidden = report.get("forbidden_automatic_statuses")
    if forbidden != FORBIDDEN_AUTOMATIC_STATUSES:
        add_issue("forbidden_automatic_statuses_mismatch", value=forbidden)

    preflight = report.get("preflight_summary") if isinstance(report.get("preflight_summary"), dict) else {}
    if not safe_bool_true(preflight.get("coverage_plan_ok")):
        add_issue("coverage_plan_not_ok", value=preflight.get("coverage_plan_ok"))
    if not safe_bool_true(preflight.get("retry_hygiene_ok")):
        add_issue("retry_hygiene_not_ok", value=preflight.get("retry_hygiene_ok"))
    if int_or_zero(preflight.get("unsafe_batch_count")) != 0:
        add_issue("unsafe_batches_present", value=preflight.get("unsafe_batch_count"))
    if not safe_bool_true(preflight.get("operator_timed_guarded_api_commands_only")):
        add_issue(
            "operator_timed_guarded_api_commands_only_missing",
            value=preflight.get("operator_timed_guarded_api_commands_only"),
        )

    source_paths = report.get("source_paths")
    source_hashes = report.get("source_hashes")
    source_hash_scopes = report.get("source_hash_scopes")
    source_hash_checks: dict[str, dict[str, Any]] = {}
    if not isinstance(source_paths, dict) or not isinstance(source_hashes, dict):
        add_issue("source_provenance_missing")
    else:
        if not isinstance(source_hash_scopes, dict):
            source_hash_scopes = {}
        for key, value in source_paths.items():
            path = base_dir / str(value)
            scope = source_hash_scopes.get(key)
            actual = sha256_artifact(path, scope=scope)
            expected = source_hashes.get(key)
            matches = bool(expected and actual and expected == actual)
            source_hash_checks[key] = {
                "path": str(value),
                "expected_sha256": expected,
                "actual_sha256": actual,
                "sha256_scope": scope,
                "hash_matches": matches,
            }
            if not matches:
                add_issue("source_hash_stale", source_key=key, expected=expected, actual=actual)

    for row in report.get("wave_plan") or []:
        if not safe_bool_true(row.get("requires_operator_start")):
            add_issue("wave_missing_operator_start", wave=row.get("wave"))
    if report.get("command_template") and "collect-qualification-items" not in str(
        report.get("command_template")
    ):
        add_issue("command_template_not_qualification_collection")

    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": now_iso(),
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "source_hash_checks": source_hash_checks,
    }


def csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def write_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in report.get("wave_plan") or []:
            writer.writerow({field: csv_cell(row.get(field)) for field in CSV_FIELDS})


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    coverage = report.get("coverage_state") if isinstance(report.get("coverage_state"), dict) else {}
    target = report.get("target_state") if isinstance(report.get("target_state"), dict) else {}
    batch = report.get("batch_summary") if isinstance(report.get("batch_summary"), dict) else {}
    preflight = (
        report.get("preflight_summary") if isinstance(report.get("preflight_summary"), dict) else {}
    )
    lines = [
        "# Qualification Guarded Batch Operator Decision",
        "",
        f"- Schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- Report only: `{report.get('report_only')}`",
        f"- API calls made by this report: `{report.get('api_calls')}`",
        f"- Execution authorized: `{report.get('execution_authorized')}`",
        f"- Automatic queue execution allowed: `{report.get('automatic_queue_execution_allowed')}`",
        f"- Human decision required: `{report.get('human_decision_required')}`",
        (
            "- Current attempted-unit coverage: "
            f"{coverage.get('attempted_unit_count')} / {coverage.get('total_unit_count')} "
            f"({coverage.get('collection_coverage')})"
        ),
        f"- Target attempted-unit coverage: `{target.get('target_ratio', 0.9)}`",
        f"- Additional attempted units needed: `{target.get('additional_attempted_units_needed')}`",
        f"- Planned guarded batches: `{batch.get('batch_count')}` x `{batch.get('batch_size')}` units",
        f"- Unsafe batch count: `{preflight.get('unsafe_batch_count')}`",
        "",
        "## Preflight",
    ]
    for key, value in preflight.items():
        lines.append(f"- {key}: `{csv_cell(value)}`")
    lines.extend(
        [
            "",
            "## Wave Plan",
            "| Wave | Batches | Max units | Min runtime before retries | Planning window | Requires operator start |",
            "|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in report.get("wave_plan") or []:
        lines.append(
            "| "
            f"{row.get('wave')} | {row.get('batch_count')} | {row.get('max_units_attempted')} | "
            f"{row.get('estimated_min_runtime_minutes_before_retries')} min | "
            f"{row.get('planning_window_minutes_with_operator_buffer')} min | "
            f"{row.get('requires_operator_start')} |"
        )

    lines.extend(
        [
            "",
            "## Priority Major Gaps",
            "| Major | Name | Coverage | Unattempted | Attempted / Total |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for row in report.get("priority_major_gaps") or []:
        lines.append(
            "| "
            f"{row.get('major_code')} | {row.get('major_name')} | {row.get('coverage')} | "
            f"{row.get('unattempted_unit_count')} | {row.get('attempted_unit_count')} / "
            f"{row.get('total_unit_count')} |"
        )

    lines.extend(["", "## Operator Decisions"])
    for decision in report.get("operator_decisions") or []:
        lines.append(
            f"- `{decision.get('decision_id')}`: {decision.get('label')} "
            f"(required={decision.get('required')})"
        )
        if decision.get("recommended_first_value") is not None:
            lines.append(f"  - recommended_first_value: `{decision.get('recommended_first_value')}`")
        for condition in decision.get("recommended_conditions") or []:
            lines.append(f"  - stop_condition: {condition}")
        for command in decision.get("recommended_commands") or []:
            lines.append(f"  - post_run_command: `{command}`")

    lines.extend(["", "## Command Template", "```powershell", str(report.get("command_template") or ""), "```"])
    lines.extend(["", "## Non Goals"])
    for item in report.get("non_goals") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Source Hashes"])
    for key, value in (report.get("source_hashes") or {}).items():
        lines.append(f"- {key}: `{value}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_audit_markdown(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Qualification Guarded Batch Operator Decision Audit",
        "",
        f"- schema: `{audit.get('schema')}`",
        f"- generated_at: `{audit.get('generated_at')}`",
        f"- ok: `{audit.get('ok')}`",
        f"- issue_count: `{audit.get('issue_count')}`",
        f"- report_only: `{audit.get('report_only')}`",
        f"- status_update_allowed: `{audit.get('status_update_allowed')}`",
        f"- db_writes: `{audit.get('db_writes')}`",
        f"- api_calls: `{audit.get('api_calls')}`",
        f"- approval_claim: `{audit.get('approval_claim')}`",
        f"- acceptance_claim: `{audit.get('acceptance_claim')}`",
        f"- human_decision_required: `{audit.get('human_decision_required')}`",
        "",
    ]
    if audit.get("issues"):
        lines.append("## Issues")
        for issue in audit.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: `{issue}`")
    else:
        lines.append("No qualification guarded-batch packet issues found.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a report-only qualification guarded batch operator decision packet."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--stamp")
    parser.add_argument("--coverage-plan", type=Path)
    parser.add_argument("--retry-hygiene", type=Path)
    parser.add_argument("--release-readiness", type=Path)
    parser.add_argument("--queue-status", type=Path)
    parser.add_argument("--queue-run", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--audit-markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    reports_dir = args.root / "reports"
    report = build_decision_packet(
        coverage_plan_path=input_or_latest(
            args.coverage_plan,
            "qualification_collection_coverage_plan_*.json",
            reports_dir=reports_dir,
            root=args.root,
            stamp=args.stamp,
        ),
        retry_hygiene_path=input_or_latest(
            args.retry_hygiene,
            "qualification_retry_hygiene_*.json",
            reports_dir=reports_dir,
            root=args.root,
            stamp=args.stamp,
        ),
        release_readiness_path=input_or_latest(
            args.release_readiness,
            "aihr_release_readiness_*.json",
            reports_dir=reports_dir,
            root=args.root,
            stamp=args.stamp,
        ),
        queue_status_path=input_or_latest(
            args.queue_status,
            "aihr_agent_queue_status_*.json",
            reports_dir=reports_dir,
            root=args.root,
            stamp=args.stamp,
        ),
        queue_run_path=input_or_latest(
            args.queue_run,
            "aihr_agent_queue_run_*.json",
            reports_dir=reports_dir,
            root=args.root,
            stamp=args.stamp,
            exclude_substrings=("_dryrun", "_probe"),
        ),
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    if args.csv_out:
        write_csv(args.csv_out, report)

    audit = None
    if args.audit_out or args.audit_markdown_out or args.strict:
        audit = audit_decision_packet(report)
        if args.audit_out:
            args.audit_out.parent.mkdir(parents=True, exist_ok=True)
            args.audit_out.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.audit_markdown_out:
            write_audit_markdown(args.audit_markdown_out, audit)

    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "batch_count": (report.get("batch_summary") or {}).get("batch_count"),
                "execution_authorized": report.get("execution_authorized"),
                "automatic_queue_execution_allowed": report.get(
                    "automatic_queue_execution_allowed"
                ),
                "out": str(args.out),
                "audit_ok": audit.get("ok") if audit else None,
                "audit_issue_count": audit.get("issue_count") if audit else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.strict and audit is not None and not audit.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
