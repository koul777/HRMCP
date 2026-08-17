from __future__ import annotations

import csv
import json
import hashlib
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ncs_mcp.release_labels import blocker_display_label, blocker_display_labels


AGENT_QUEUE_SCHEMA = "aihr_agent_work_queue_v1"
GUARDED_COLLECTION_FLAGS = (
    "--limit-units",
    "--num-of-rows",
    "--max-pages",
    "--request-delay",
    "--max-retries",
    "--retry-backoff-seconds",
    "--stop-after-rate-limit-errors",
)
READ_ONLY_REPORT_COMMANDS = (
    " quality-gates ",
    " review-priority ",
    " export-review-seedpack ",
    " export-ontology-definition-seedpack ",
    " export-human-review-provenance-reconfirmation-proofset ",
    " export-transition-scenario-seedpack ",
    " review-triage ",
    " audit-review-artifact-readability ",
    " run-aihr-plan-demo ",
    " verify-aihr-dashboard ",
)
READ_ONLY_REPORT_COMMAND_NAMES = {
    "quality-gates",
    "review-priority",
    "export-review-seedpack",
    "export-ontology-definition-seedpack",
    "export-human-review-provenance-reconfirmation-proofset",
    "export-transition-scenario-seedpack",
    "review-triage",
    "audit-review-artifact-readability",
    "run-aihr-plan-demo",
    "verify-aihr-dashboard",
}
GUARDED_MANUAL_MUTATION_POLICIES = {
    "guarded_api_collection",
    "inspect_only",
    "requires_existing_artifacts",
}
AGENT_QUEUE_RUN_SCHEMA = "aihr_agent_queue_run_v1"
AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT = 2000
REVIEW_SEEDPACK_BLANK_DECISION_FIELDS = (
    "decision",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "approved_definition",
    "proposed_target_review_status",
    "proposed_issue_resolution",
)
PROVENANCE_RECONFIRMATION_ALLOWED_DECISIONS = (
    "reconfirm",
    "downgrade_to_review_required",
    "defer",
)
PROVENANCE_RECONFIRMATION_REQUESTED_DECISION = (
    "reconfirm | downgrade_to_review_required | defer"
)
PROVENANCE_RECONFIRMATION_DECISION_SEMANTICS = (
    "review_input_only_not_status_update"
)
NCS006_CHECKPOINT_ENV = "NCS_NCS006_ELEMENT_API_CHECKPOINT_JSON_PATH"
NCS006_CHECKPOINT_GLOB = "checkpoint_ncs006_element_api_status_20*.json"
NCS006_CHECKPOINT_DATE_RE = re.compile(r"checkpoint_ncs006_element_api_status_(\d{8})")
NCS006_QUALIFICATION_RETRY_ALLOWED_ACTIONS = {
    "complete_no_collection_needed",
    "start_guarded_watchdog_if_no_active_process",
}
NCS006_QUALIFICATION_RETRY_BLOCKED_ACTIONS = {
    "collector_active_monitor_only",
    "inspect_stale_batch_before_retry",
    "manual_inspection_required",
    "wait_for_rate_limit_cooldown",
    "watchdog_active_observe_next_sweep",
}
AIHR_LOCAL_DATABASE_REF = "configured_ncs_database"
SENSITIVE_OUTPUT_MARKER_NAMES = (
    "source_payload",
    "authKey",
    "serviceKey",
    "service_key",
    "NCS_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
    "relation_id",
    "created_at",
    "updated_at",
    "review_status",
    "data_sources",
    "source_rows",
    "source_json",
    "raw_payload",
    "raw_response",
)
HUMAN_DECISION_OUTPUT_MARKER_NAMES = (
    "downgrade_to_review_required",
    "allowed_decisions",
    "requested_decision",
    "human_reviewed",
    "approval_claim",
    "trusted/reviewed",
    "reconfirm",
    "accepted",
    "reviewed",
    "approve",
    "reject",
    "defer",
    "trusted",
)
_SECRET_ENV_ASSIGNMENT_RE = re.compile(
    r"\b(?P<key>NCS_(?:TRAINING_COURSE_|QUALIFICATION_|JOB_BASE_)?SERVICE_KEY)\b"
    r"(?P<sep>[\s\"']*[:=][\s\"']*)"
    r"(?P<value>[^\"'\s,&}]+)",
    re.IGNORECASE,
)
_AUTHORIZATION_HEADER_RE = re.compile(r"\bAuthorization\s*:\s*[^\r\n]+", re.IGNORECASE)
_BEARER_TOKEN_RE = re.compile(r"\bBearer\b(?P<sep>\s+)(?P<value>[A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
_URL_SECRET_PARAM_RE = re.compile(
    r"(?P<prefix>[?&](?:serviceKey|service_key|apiKey|api_key|certKey|authKey)=)"
    r"(?P<value>[^&\s\"']+)",
    re.IGNORECASE,
)
_SENSITIVE_STRUCTURED_FIELD_PREFIX_RE = re.compile(
    r"(?P<field>[\"']?"
    r"(?:" + "|".join(re.escape(marker) for marker in SENSITIVE_OUTPUT_MARKER_NAMES) + r"|apiKey|api_key|certKey)"
    r"[\"']?\s*[:=]\s*)",
    re.IGNORECASE,
)
_SENSITIVE_OUTPUT_MARKER_RE = re.compile(
    "|".join(re.escape(marker) for marker in (*SENSITIVE_OUTPUT_MARKER_NAMES, "Authorization", "Bearer")),
    re.IGNORECASE,
)
_HUMAN_DECISION_OUTPUT_MARKER_RE = re.compile(
    r"(?<![\w-])("
    + "|".join(
        re.escape(marker)
        for marker in sorted(HUMAN_DECISION_OUTPUT_MARKER_NAMES, key=len, reverse=True)
    )
    + r")(?![\w-])",
    re.IGNORECASE,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Agent queue file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Agent queue file cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Agent queue file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Agent queue file must contain a JSON object: {path}")
    return payload


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _workspace_path(path_text: str, workspace: Path) -> Path:
    path = Path(path_text)
    if path.is_absolute():
        return path
    return workspace / path


def _contract_path_identity(path_text: str | Path | None, workspace: Path) -> str:
    path = _workspace_path(str(path_text or ""), workspace)
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path
    return os.path.normcase(str(resolved))


def _resolve_workspace_file_path(path: Path, workspace: Path) -> Path:
    return path if path.is_absolute() else workspace / path


def _display_path(path: Path | str | None, workspace: Path) -> str | None:
    if path in (None, ""):
        return None
    if _looks_like_local_database_path(path):
        return AIHR_LOCAL_DATABASE_REF
    candidate = Path(str(path))
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    try:
        return resolved.relative_to(workspace.resolve()).as_posix()
    except ValueError:
        if candidate.is_absolute():
            return candidate.name
        return candidate.as_posix()


def _looks_like_local_database_path(path: Path | str | None) -> bool:
    text = str(path or "").replace("\\", "/").lower()
    if not text:
        return False
    if "/data/processed/" not in f"/{text}":
        return False
    return bool(
        re.search(r"/data/processed/[^/]+\.db(?:-(?:wal|shm|journal))?$", f"/{text}")
    )


def _public_artifact_list(paths: list[str], workspace: Path) -> list[str]:
    result: list[str] = []
    for path in paths:
        display = _display_path(path, workspace)
        if display:
            result.append(display)
    return result


_COMMAND_ABSOLUTE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<path>[A-Za-z]:[\\/][^\s\"'<>|;]+|\\\\[^\s\"'<>|;]+)"
)
_COMMAND_LOCAL_DB_RE = re.compile(
    r"(?P<path>(?:^|(?<=[\s\"']))data[\\/]processed[\\/][^\s\"'<>|;]+\.db"
    r"(?:-(?:wal|shm|journal))?)",
    re.IGNORECASE,
)


def _public_command_text(command: Any, workspace: Path) -> str:
    text = str(command or "")
    if not text:
        return ""

    def replace_absolute(match: re.Match[str]) -> str:
        return _display_path(match.group("path"), workspace) or ""

    text = _COMMAND_ABSOLUTE_PATH_RE.sub(replace_absolute, text)

    def replace_local_db(match: re.Match[str]) -> str:
        return _display_path(match.group("path"), workspace) or ""

    return _COMMAND_LOCAL_DB_RE.sub(replace_local_db, text)


def _public_command_list(commands: list[str], workspace: Path) -> list[str]:
    return [_public_command_text(command, workspace) for command in commands]


def _existing_paths(paths: list[str], workspace: Path) -> list[str]:
    return [
        display
        for path in paths
        if _workspace_path(path, workspace).exists()
        for display in [_display_path(path, workspace)]
        if display
    ]


def _missing_paths(paths: list[str], workspace: Path) -> list[str]:
    return [
        display
        for path in paths
        if not _workspace_path(path, workspace).exists()
        for display in [_display_path(path, workspace)]
        if display
    ]


def _command_words(command: str) -> str:
    return f" {command.replace(chr(92), '/')} "


def _command_has_any(command: str, tokens: tuple[str, ...]) -> bool:
    words = _command_words(command)
    return any(token in words for token in tokens)


def _is_guarded_qualification_collection_command(command: str) -> bool:
    words = _command_words(command)
    return " retry-qualification-errors " in words or " collect-qualification-items " in words


def _dashboard_base_url_violation(args: list[str]) -> str | None:
    if len(args) < 3 or args[2] != "verify-aihr-dashboard":
        return None
    base_url: str | None = None
    for index, arg in enumerate(args):
        if arg == "--base-url" and index + 1 < len(args):
            base_url = args[index + 1]
            break
        if arg.startswith("--base-url="):
            base_url = arg.split("=", 1)[1]
            break
    if not base_url:
        return None
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if parsed.scheme not in {"http", "https"} or not hostname:
        return "verify_aihr_dashboard_base_url_invalid"
    host = hostname.lower()
    if host == "localhost":
        return None
    try:
        if ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return "verify_aihr_dashboard_base_url_not_loopback"


def _safety_violations(item: dict[str, Any]) -> list[str]:
    command = str(item.get("command") or "")
    mutation_policy = str(item.get("mutation_policy") or "")
    violations: list[str] = []
    if not command.strip():
        return ["missing_command"]
    if mutation_policy == "guarded_api_collection":
        if not _is_guarded_qualification_collection_command(command):
            violations.append("guarded_api_collection_command_not_recognized")
        for flag in GUARDED_COLLECTION_FLAGS:
            if flag not in command:
                violations.append(f"missing_guard_flag:{flag}")
        if " collect-qualification-items " in _command_words(command) and "--ncs006-checkpoint-path" not in command:
            violations.append("missing_guard_flag:--ncs006-checkpoint-path")
        if "--refresh" in command:
            violations.append("guarded_api_collection_must_not_refresh_by_default")
    elif mutation_policy == "regenerate_reports_only":
        if not _command_has_any(command, READ_ONLY_REPORT_COMMANDS):
            violations.append("regenerate_reports_only_command_not_recognized_as_read_only")
        else:
            try:
                args = _split_agent_queue_command(command)
            except ValueError as exc:
                if str(exc).startswith("verify_aihr_dashboard_base_url_"):
                    violations.append(str(exc))
                args = []
            if args:
                violation = _dashboard_base_url_violation(args)
                if violation:
                    violations.append(violation)
        if any(token in command for token in (" collect-", " retry-qualification-errors ", " ontology-review ", " hr-review ")):
            violations.append("regenerate_reports_only_command_may_mutate_data")
    return violations


def _read_json_or_none(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _ncs006_checkpoint_sort_key(path: Path) -> tuple[float, int, float]:
    payload = _read_json_or_none(path)
    generated_at = (
        _parse_iso_datetime(payload.get("generated_at"))
        if isinstance(payload, dict)
        else None
    )
    generated_ts = generated_at.timestamp() if generated_at is not None else -1.0
    match = NCS006_CHECKPOINT_DATE_RE.search(path.name)
    embedded_date = int(match.group(1)) if match else 0
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (generated_ts, embedded_date, mtime)


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _resolve_ncs006_checkpoint_path(
    workspace: Path,
    *,
    explicit_path: Path | None = None,
    preferred_dir: Path | None = None,
) -> Path | None:
    if explicit_path is not None:
        return explicit_path if explicit_path.is_absolute() else workspace / explicit_path
    configured = os.environ.get(NCS006_CHECKPOINT_ENV)
    if configured:
        configured_path = Path(configured)
        return configured_path if configured_path.is_absolute() else workspace / configured_path
    if preferred_dir is not None:
        preferred_path = preferred_dir if preferred_dir.is_absolute() else workspace / preferred_dir
        preferred_candidates = [
            path for path in preferred_path.glob(NCS006_CHECKPOINT_GLOB) if path.is_file()
        ]
        if preferred_candidates:
            return max(preferred_candidates, key=_ncs006_checkpoint_sort_key)
    reports_dir = workspace / "reports"
    candidates = [path for path in reports_dir.glob(NCS006_CHECKPOINT_GLOB) if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=_ncs006_checkpoint_sort_key)


def _derive_ncs006_qualification_retry_permission(
    *,
    next_safe: dict[str, Any],
    cooldown: dict[str, Any],
    cooldown_expired: bool,
) -> tuple[bool | None, str]:
    explicit = next_safe.get("qualification_retry_allowed_now")
    if isinstance(explicit, bool):
        return explicit, "checkpoint_explicit"
    if cooldown_expired:
        return False, "cooldown_expired_checkpoint_refresh_required"
    if cooldown.get("status") == "cooldown_active":
        return False, "rate_limit_cooldown_active"
    action_status = str(next_safe.get("status") or "").strip()
    if action_status in NCS006_QUALIFICATION_RETRY_BLOCKED_ACTIONS:
        return False, f"next_safe_action:{action_status}"
    if action_status in NCS006_QUALIFICATION_RETRY_ALLOWED_ACTIONS:
        return True, f"next_safe_action:{action_status}"
    return None, "not_evaluated"


def _ncs006_guarded_api_gate(
    workspace: Path,
    *,
    checkpoint_path: Path | None = None,
    preferred_checkpoint_dir: Path | None = None,
) -> dict[str, Any]:
    resolved_path = _resolve_ncs006_checkpoint_path(
        workspace,
        explicit_path=checkpoint_path,
        preferred_dir=preferred_checkpoint_dir,
    )
    if resolved_path is None:
        return {
            "status": "not_evaluated",
            "checkpoint_path": None,
            "safety_violations": [],
        }
    payload = _read_json_or_none(resolved_path)
    if payload is None:
        return {
            "status": "checkpoint_unreadable",
            "checkpoint_path": _display_path(resolved_path, workspace),
            "safety_violations": ["ncs006_checkpoint_unreadable"],
        }
    next_safe = payload.get("next_safe_action") if isinstance(payload.get("next_safe_action"), dict) else {}
    cooldown = (
        payload.get("rate_limit_cooldown")
        if isinstance(payload.get("rate_limit_cooldown"), dict)
        else {}
    )
    blocked_automation = _string_list(next_safe.get("blocked_automation"))
    api_allowed = next_safe.get("api_call_allowed_now")
    cooldown_active = cooldown.get("status") == "cooldown_active"
    cooldown_until = _parse_iso_datetime(cooldown.get("cooldown_until"))
    cooldown_expired = cooldown_until is not None and cooldown_until <= datetime.now(timezone.utc)
    qualification_retry_allowed, qualification_retry_reason = _derive_ncs006_qualification_retry_permission(
        next_safe=next_safe,
        cooldown=cooldown,
        cooldown_expired=cooldown_expired,
    )
    safety_violations: list[str] = []
    if qualification_retry_allowed is not True and api_allowed is False:
        safety_violations.append("ncs006_checkpoint_api_call_not_allowed")
    if qualification_retry_allowed is False:
        safety_violations.append("ncs006_qualification_retry_not_allowed")
    if qualification_retry_allowed is None:
        safety_violations.append("ncs006_qualification_retry_permission_unknown")
    if cooldown_active:
        safety_violations.append("ncs006_rate_limit_cooldown_active")
    if cooldown_active and "retry_qualification_api_during_ncs006_cooldown" in blocked_automation:
        safety_violations.append("ncs006_blocks_qualification_retry_during_cooldown")
    if cooldown_expired and api_allowed is False:
        safety_violations.append("ncs006_stale_checkpoint_after_cooldown")
    resolution_status = next_safe.get("status")
    if cooldown_expired and api_allowed is False:
        resolution_status = "refresh_qualification_retry_hygiene_before_retry"
    return {
        "status": "blocked" if safety_violations else "allowed",
        "checkpoint_path": _display_path(resolved_path, workspace),
        "generated_at": payload.get("generated_at"),
        "api_call_allowed_now": api_allowed,
        "element_api_call_allowed_now": api_allowed,
        "qualification_retry_allowed_now": qualification_retry_allowed,
        "qualification_retry_guard_reason": qualification_retry_reason,
        "next_safe_action_status": next_safe.get("status"),
        "next_safe_action_resolution_status": resolution_status,
        "cooldown_status": cooldown.get("status"),
        "cooldown_until": cooldown.get("cooldown_until"),
        "blocked_automation": blocked_automation,
        "safety_violations": safety_violations,
    }


def _queue_state(
    *,
    agent_file_exists: bool,
    missing_prerequisites: list[str],
    safety_violations: list[str],
    auto_runnable: bool,
    requires_human_decision: bool,
) -> str:
    if safety_violations:
        return "blocked_safety"
    if not agent_file_exists or missing_prerequisites:
        return "blocked_missing_prerequisites"
    if requires_human_decision:
        return "manual_ready"
    if auto_runnable:
        return "ready_to_start"
    return "manual_ready"


def _automation_block_reason(
    *,
    state: str,
    can_start_automated: bool,
    auto_runnable: bool,
    mutation_policy: Any,
    requires_human_decision: bool,
    missing_prerequisites: list[str],
    safety_violations: list[str],
) -> str:
    policy = str(mutation_policy or "")
    if can_start_automated:
        return "auto_startable"
    if safety_violations:
        return "safety_violations"
    if missing_prerequisites or state == "blocked_missing_prerequisites":
        return "missing_prerequisite_artifacts"
    if requires_human_decision:
        return "requires_human_decision"
    if policy == "guarded_api_collection":
        return "guarded_api_collection"
    if policy in GUARDED_MANUAL_MUTATION_POLICIES:
        return f"mutation_policy:{policy}"
    if auto_runnable and policy != "regenerate_reports_only":
        return f"unsupported_auto_mutation_policy:{policy or 'missing'}"
    if not auto_runnable:
        return "not_auto_runnable"
    return "manual_operator_review"


def _manual_classification(*, requires_human_decision: bool, mutation_policy: Any) -> str:
    policy = str(mutation_policy or "")
    if requires_human_decision:
        return "human_decision_required"
    if policy == "guarded_api_collection":
        return "operator_timed_guarded_api_collection"
    if policy in GUARDED_MANUAL_MUTATION_POLICIES:
        return "guarded_manual_prerequisite"
    return "manual_operator_review"


def _operator_action_recommended(reason: str) -> str:
    if reason == "auto_startable":
        return "run_with_agent_queue_run_ready"
    if reason == "requires_human_decision":
        return "collect_explicit_human_decision"
    if reason == "guarded_api_collection":
        return "operator_timed_guarded_api_collection"
    if reason == "missing_prerequisite_artifacts":
        return "generate_or_attach_prerequisite_artifacts"
    if reason == "safety_violations":
        return "resolve_safety_violations"
    if reason.startswith("mutation_policy:requires_existing_artifacts"):
        return "verify_existing_artifacts_before_execution"
    if reason.startswith("mutation_policy:inspect_only"):
        return "manual_inspection_required"
    if reason.startswith("unsupported_auto_mutation_policy:"):
        return "keep_manual_until_policy_is_explicitly_allowed"
    return "manual_operator_review_required"


def build_agent_queue_status(
    queue: dict[str, Any],
    *,
    queue_path: Path | None = None,
    workspace: Path | None = None,
    ncs006_checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    workspace = (workspace or Path.cwd()).resolve()
    if queue.get("schema") != AGENT_QUEUE_SCHEMA:
        raise ValueError(
            f"Unsupported agent queue schema: {queue.get('schema')!r}. Expected {AGENT_QUEUE_SCHEMA}."
        )
    items = queue.get("items")
    if not isinstance(items, list):
        raise ValueError("Agent queue is missing list field 'items'.")

    status_items: list[dict[str, Any]] = []
    for index, raw_item in enumerate(items, start=1):
        if not isinstance(raw_item, dict):
            raise ValueError(f"Agent queue item #{index} is not an object.")
        agent_file = str(raw_item.get("agent_file") or "")
        prerequisite_artifacts = _string_list(raw_item.get("prerequisite_artifacts"))
        expected_artifacts = _string_list(raw_item.get("expected_artifacts"))
        missing_prerequisites = _missing_paths(prerequisite_artifacts, workspace)
        existing_outputs = _existing_paths(expected_artifacts, workspace)
        missing_outputs = _missing_paths(expected_artifacts, workspace)
        safety = _safety_violations(raw_item)
        operational_guard: dict[str, Any] | None = None
        if raw_item.get("mutation_policy") == "guarded_api_collection":
            operational_guard = _ncs006_guarded_api_gate(
                workspace,
                checkpoint_path=ncs006_checkpoint_path,
                preferred_checkpoint_dir=queue_path.parent if queue_path else None,
            )
            safety.extend(_string_list(operational_guard.get("safety_violations")))
        mutation_policy = raw_item.get("mutation_policy")
        auto_runnable = bool(raw_item.get("auto_runnable"))
        auto_executable = auto_runnable and mutation_policy == "regenerate_reports_only"
        requires_human_decision = bool(raw_item.get("requires_human_decision"))
        agent_file_exists = bool(agent_file) and _workspace_path(agent_file, workspace).exists()
        state = _queue_state(
            agent_file_exists=agent_file_exists,
            missing_prerequisites=missing_prerequisites,
            safety_violations=safety,
            auto_runnable=auto_executable,
            requires_human_decision=requires_human_decision,
        )
        preflight_ok = state in {"ready_to_start", "manual_ready"}
        can_start_automated = preflight_ok and auto_executable and not requires_human_decision
        automation_block_reason = _automation_block_reason(
            state=state,
            can_start_automated=can_start_automated,
            auto_runnable=auto_runnable,
            mutation_policy=mutation_policy,
            requires_human_decision=requires_human_decision,
            missing_prerequisites=missing_prerequisites,
            safety_violations=safety,
        )
        manual_classification = _manual_classification(
            requires_human_decision=requires_human_decision,
            mutation_policy=mutation_policy,
        )
        try:
            priority = int(raw_item.get("priority", 999))
        except (TypeError, ValueError):
            priority = 999
        public_command = _public_command_text(raw_item.get("command"), workspace)
        blocker_name = str(raw_item.get("blocker") or "")
        covered_blockers = _string_list(raw_item.get("covered_blockers")) or [blocker_name]
        status_item = {
            "id": raw_item.get("id") or f"item-{index:02d}",
            "priority": priority,
            "owner": raw_item.get("owner"),
            "agent_file": agent_file,
            "agent_file_exists": agent_file_exists,
            "blocker": raw_item.get("blocker"),
            "blocker_display_label": raw_item.get("blocker_display_label")
            or blocker_display_label(blocker_name),
            "covered_blockers": covered_blockers,
            "covered_blocker_display_labels": _string_list(
                raw_item.get("covered_blocker_display_labels")
            )
            or blocker_display_labels(covered_blockers),
            "blocker_category": raw_item.get("blocker_category"),
            "mutation_policy": mutation_policy,
            "auto_runnable": auto_runnable,
            "requires_human_decision": requires_human_decision,
            "command": public_command,
            "state": state,
            "preflight_ok": preflight_ok,
            "can_start_automated": can_start_automated,
            "manual_classification": manual_classification,
            "automation_block_reason": automation_block_reason,
            "operator_action_recommended": _operator_action_recommended(
                automation_block_reason
            ),
            "pending_human_decision_ids": (
                [raw_item.get("id") or f"item-{index:02d}"]
                if requires_human_decision
                else []
            ),
            "prerequisite_artifacts": _public_artifact_list(prerequisite_artifacts, workspace),
            "prerequisite_commands": _public_command_list(
                _string_list(raw_item.get("prerequisite_commands")),
                workspace,
            ),
            "missing_prerequisite_artifacts": missing_prerequisites,
            "expected_artifacts": _public_artifact_list(expected_artifacts, workspace),
            "existing_expected_artifacts": existing_outputs,
            "missing_expected_artifacts": missing_outputs,
            "safety_violations": safety,
            "acceptance_checks": _string_list(raw_item.get("acceptance_checks")),
        }
        if operational_guard is not None:
            status_item["operational_guard"] = operational_guard
        status_items.append(status_item)

    state_counts = Counter(str(item["state"]) for item in status_items)
    sorted_items = sorted(status_items, key=lambda item: (int(item["priority"]), str(item["id"])))
    execution_order = [
        {
            "id": item["id"],
            "priority": item["priority"],
            "owner": item["owner"],
            "agent_file": item["agent_file"],
            "state": item["state"],
            "mutation_policy": item["mutation_policy"],
            "can_start_automated": item["can_start_automated"],
            "requires_human_decision": item["requires_human_decision"],
            "blocker_display_label": item.get("blocker_display_label"),
            "command": item["command"],
        }
        for item in sorted_items
        if item["can_start_automated"]
    ]
    manual_queue = [item for item in sorted_items if item["state"] == "manual_ready"]
    blocked_queue = [item for item in sorted_items if str(item["state"]).startswith("blocked_")]
    fallback_actions = [
        {
            "id": item["id"],
            "priority": item["priority"],
            "owner": item["owner"],
            "agent_file": item["agent_file"],
            "state": item["state"],
            "mutation_policy": item["mutation_policy"],
            "blocker_display_label": item.get("blocker_display_label"),
            "command": item["command"],
            "reason": "preferred_automated_path",
        }
        for item in sorted_items
        if item["can_start_automated"]
        and item["state"] == "ready_to_start"
        and item["mutation_policy"] != "guarded_api_collection"
    ]
    return {
        "ok": not blocked_queue,
        "schema": "aihr_agent_queue_status_v1",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "source_queue_schema": queue.get("schema"),
        "source_queue_path": _display_path(queue_path, workspace) if queue_path else None,
        "workspace_ref": "configured_workspace",
        "summary": {
            "item_count": len(status_items),
            "preflight_ok_count": sum(1 for item in status_items if item["preflight_ok"]),
            "blocked_count": len(blocked_queue),
            "manual_ready_count": len(manual_queue),
            "manual_human_decision_count": sum(
                1 for item in manual_queue if item.get("requires_human_decision") is True
            ),
            "guarded_manual_count": sum(
                1
                for item in manual_queue
                if item.get("requires_human_decision") is True
                or str(item.get("mutation_policy") or "") in GUARDED_MANUAL_MUTATION_POLICIES
            ),
            "manual_classification_counts": dict(
                sorted(
                    Counter(
                        str(item.get("manual_classification") or "manual_operator_review")
                        for item in manual_queue
                    ).items()
                )
            ),
            "auto_startable_count": len(execution_order),
            "state_counts": dict(sorted(state_counts.items())),
            "global_guardrail_count": len(queue.get("global_guardrails") or []),
        },
        "execution_order": execution_order,
        "manual_queue": manual_queue,
        "blocked_queue": blocked_queue,
        "fallback_actions": fallback_actions,
        "next_fallback_action": fallback_actions[0] if fallback_actions else None,
        "items": sorted_items,
        "global_guardrails": _string_list(queue.get("global_guardrails")),
    }


def build_agent_queue_status_from_file(
    queue_path: Path,
    *,
    workspace: Path | None = None,
    ncs006_checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    workspace = (workspace or Path.cwd()).resolve()
    resolved_queue_path = _resolve_workspace_file_path(queue_path, workspace)
    queue = _read_json(resolved_queue_path)
    return build_agent_queue_status(
        queue,
        queue_path=resolved_queue_path,
        workspace=workspace,
        ncs006_checkpoint_path=ncs006_checkpoint_path,
    )


def build_ncs006_guarded_api_gate(
    *,
    workspace: Path | None = None,
    checkpoint_path: Path | None = None,
) -> dict[str, Any]:
    return _ncs006_guarded_api_gate(
        (workspace or Path.cwd()).resolve(),
        checkpoint_path=checkpoint_path,
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _short_text(value: str, *, limit: int = AGENT_QUEUE_RUN_OUTPUT_TAIL_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def _mask_nonspace(value: str) -> str:
    return "".join(char if char.isspace() else "*" for char in value)


def _structured_value_end(value: str, start: int) -> int:
    if start >= len(value):
        return start
    opener = value[start]
    if opener in ("'", '"'):
        index = start + 1
        escaped = False
        while index < len(value):
            char = value[index]
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == opener:
                return index + 1
            index += 1
        return len(value)
    if opener in ("{", "["):
        closers = {"{": "}", "[": "]"}
        stack = [closers[opener]]
        index = start + 1
        quote: str | None = None
        escaped = False
        while index < len(value):
            char = value[index]
            if quote:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
            elif char in ("'", '"'):
                quote = char
            elif char in closers:
                stack.append(closers[char])
            elif stack and char == stack[-1]:
                stack.pop()
                if not stack:
                    return index + 1
            index += 1
        return len(value)
    index = start
    while index < len(value) and value[index] not in ",\r\n}] \t":
        index += 1
    return index


def _redact_sensitive_structured_fields(value: str) -> tuple[str, int]:
    redacted_parts: list[str] = []
    redaction_count = 0
    cursor = 0
    while True:
        match = _SENSITIVE_STRUCTURED_FIELD_PREFIX_RE.search(value, cursor)
        if match is None:
            redacted_parts.append(value[cursor:])
            break
        value_end = _structured_value_end(value, match.end())
        redacted_parts.append(value[cursor : match.start()])
        redacted_parts.append(_mask_nonspace(value[match.start() : value_end]))
        redaction_count += 1
        cursor = value_end
    return "".join(redacted_parts), redaction_count


def _redact_sensitive_output(value: str) -> tuple[str, int]:
    redaction_count = 0

    def replace_env_assignment(match: re.Match[str]) -> str:
        return (
            _mask_nonspace(match.group("key"))
            + match.group("sep")
            + _mask_nonspace(match.group("value"))
        )

    value, count = _SECRET_ENV_ASSIGNMENT_RE.subn(replace_env_assignment, value)
    redaction_count += count

    value, count = _AUTHORIZATION_HEADER_RE.subn(lambda match: _mask_nonspace(match.group(0)), value)
    redaction_count += count

    def replace_bearer(match: re.Match[str]) -> str:
        return _mask_nonspace("Bearer") + match.group("sep") + _mask_nonspace(match.group("value"))

    value, count = _BEARER_TOKEN_RE.subn(replace_bearer, value)
    redaction_count += count

    def replace_secret_param(match: re.Match[str]) -> str:
        return _mask_nonspace(match.group("prefix")) + _mask_nonspace(match.group("value"))

    value, count = _URL_SECRET_PARAM_RE.subn(replace_secret_param, value)
    redaction_count += count

    value, count = _redact_sensitive_structured_fields(value)
    redaction_count += count

    value, count = _SENSITIVE_OUTPUT_MARKER_RE.subn(lambda match: _mask_nonspace(match.group(0)), value)
    redaction_count += count
    return value, redaction_count


def _redact_human_decision_output(value: str) -> tuple[str, int]:
    return _HUMAN_DECISION_OUTPUT_MARKER_RE.subn(
        lambda match: _mask_nonspace(match.group(0)),
        value,
    )


def _output_tail_fields(prefix: str, value: str) -> dict[str, Any]:
    redacted_value, redaction_count = _redact_sensitive_output(value)
    redacted_value, decision_redaction_count = _redact_human_decision_output(redacted_value)
    tail = _short_text(redacted_value)
    return {
        f"{prefix}_tail": tail,
        f"{prefix}_original_chars": len(value),
        f"{prefix}_tail_chars": len(tail),
        f"{prefix}_truncated": len(tail) < len(value),
        f"{prefix}_redacted": redaction_count > 0 or decision_redaction_count > 0,
        f"{prefix}_redaction_count": redaction_count,
        f"{prefix}_human_decision_vocab_redacted": decision_redaction_count > 0,
        f"{prefix}_human_decision_vocab_redaction_count": decision_redaction_count,
    }


def _expected_artifact_checks(paths: list[str], workspace: Path) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path_text in paths:
        path = _workspace_path(path_text, workspace)
        exists = path.exists()
        size: int | None = None
        if exists and path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = None
        checks.append(
            {
                "path": path_text,
                "exists": exists,
                "non_empty": bool(size and size > 0),
                "size_bytes": size,
            }
        )
    return checks


def _strip_cli_quotes(value: str | None) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _arg_value(args: list[str], flag: str) -> str | None:
    try:
        index = args.index(flag)
    except ValueError:
        return None
    if index + 1 >= len(args):
        return None
    return _strip_cli_quotes(args[index + 1])


def _false_value(value: Any) -> bool:
    if value is False:
        return True
    if value in (0, "0"):
        return True
    if isinstance(value, str):
        return value.strip().lower() == "false"
    return False


def _true_value(value: Any) -> bool:
    if value is True:
        return True
    if value in (1, "1"):
        return True
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _blank_value(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _int_value(value: Any, default: int = -1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _machine_contract_result(
    check: str,
    ok: bool,
    detail: str,
    *,
    contract_id: str,
) -> dict[str, Any]:
    return {
        "check": check,
        "ok": ok,
        "detail": detail,
        "machine_contract": True,
        "machine_contract_id": contract_id,
        "non_decisional": True,
    }


def _read_json_file(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("json_payload_not_object")
    return payload


def _read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            payload = json.loads(line)
            if not isinstance(payload, dict):
                raise ValueError(f"jsonl_row_not_object:{line_number}")
            rows.append(payload)
    return rows


def _read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def _flags_are_false(payload: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return all(_false_value(payload.get(field)) for field in fields)


def _rows_keep_decisions_blank(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bool:
    return all(all(_blank_value(row.get(field)) for field in fields) for row in rows)


def _rows_keep_flags_false(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bool:
    return all(_flags_are_false(row, fields) for row in rows)


def _rows_keep_flags_true(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> bool:
    return all(all(_true_value(row.get(field)) for field in fields) for row in rows)


def _verify_review_seedpack_machine_contract(
    *,
    args: list[str],
    workspace: Path,
    command_name: str,
) -> list[dict[str, Any]]:
    contract_id = f"{command_name}_review_seedpack_contract_v1"
    out = _arg_value(args, "--out")
    csv_out = _arg_value(args, "--csv-out")
    if not out:
        return [
            _machine_contract_result(
                "machine_contract:review_seedpack_paths",
                False,
                "missing --out",
                contract_id=contract_id,
            )
        ]

    try:
        jsonl_path = _workspace_path(out, workspace)
        jsonl_rows = _read_jsonl_file(jsonl_path)
        if not jsonl_rows:
            raise ValueError("jsonl_empty")
        metadata = jsonl_rows[0]
        item_rows = jsonl_rows[1:]
        item_count = _int_value(metadata.get("item_count"), default=0)
        issue_types = {str(item).strip() for item in _string_list(metadata.get("issue_types"))}
        row_issue_types = {str(row.get("issue_type") or "").strip() for row in item_rows}
        row_issue_types.discard("")
        requested_issue_types = {
            item.strip()
            for item in str(_arg_value(args, "--issue-types") or "").split(",")
            if item.strip()
        }
        if command_name == "export-ontology-definition-seedpack":
            requested_issue_types = {
                "hr_core_concept_human_review_required",
                "ontology_core_concept_human_review_required",
            }
        issue_type_ok = (
            not requested_issue_types
            or issue_types.issubset(requested_issue_types)
            and row_issue_types.issubset(requested_issue_types)
        )
        jsonl_ok = (
            metadata.get("format_version") == "ncs-review-seedpack-v1"
            and metadata.get("record_type") in {"batch", "metadata"}
            and item_count == len(item_rows)
            and _flags_are_false(
                metadata,
                (
                    "status_update_allowed",
                    "db_writes",
                    "approval_claim",
                    "raw_source_mutation_allowed",
                    "trusted_status_write_allowed",
                ),
            )
            and _true_value(metadata.get("human_decision_required"))
            and _rows_keep_flags_false(
                item_rows,
                ("status_update_allowed", "db_writes", "approval_claim"),
            )
            and _rows_keep_flags_true(item_rows, ("human_decision_required",))
            and _rows_keep_decisions_blank(
                item_rows,
                REVIEW_SEEDPACK_BLANK_DECISION_FIELDS,
            )
            and issue_type_ok
        )
        detail = (
            f"jsonl_items={len(item_rows)} item_count={item_count} "
            f"issue_types={sorted(issue_types)}"
        )
    except Exception as exc:
        jsonl_ok = False
        item_count = -1
        detail = f"jsonl_contract_error:{exc}"

    results = [
        _machine_contract_result(
            "machine_contract:review_seedpack_jsonl_contract",
            jsonl_ok,
            detail,
            contract_id=contract_id,
        )
    ]

    if csv_out:
        try:
            csv_rows = _read_csv_rows(_workspace_path(csv_out, workspace))
            csv_ok = (
                item_count == len(csv_rows)
                and _rows_keep_flags_false(
                    csv_rows,
                    ("status_update_allowed", "db_writes", "approval_claim"),
                )
                and _rows_keep_flags_true(csv_rows, ("human_decision_required",))
                and _rows_keep_decisions_blank(
                    csv_rows,
                    REVIEW_SEEDPACK_BLANK_DECISION_FIELDS,
                )
            )
            csv_detail = f"csv_rows={len(csv_rows)} item_count={item_count}"
        except Exception as exc:
            csv_ok = False
            csv_detail = f"csv_contract_error:{exc}"
        results.append(
            _machine_contract_result(
                "machine_contract:review_seedpack_csv_contract",
                csv_ok,
                csv_detail,
                contract_id=contract_id,
            )
        )
    return results


def _verify_provenance_reconfirmation_machine_contract(
    *,
    args: list[str],
    workspace: Path,
) -> list[dict[str, Any]]:
    contract_id = "human_review_provenance_reconfirmation_proofset_contract_v1"
    packet_out = _arg_value(args, "--out")
    sheet_out = _arg_value(args, "--decision-sheet-out")
    sheet_csv_out = _arg_value(args, "--decision-sheet-csv-out")
    audit_out = _arg_value(args, "--decision-audit-out")
    missing_flags = [
        flag
        for flag, value in (
            ("--out", packet_out),
            ("--decision-sheet-out", sheet_out),
            ("--decision-sheet-csv-out", sheet_csv_out),
            ("--decision-audit-out", audit_out),
        )
        if not value
    ]
    if missing_flags:
        return [
            _machine_contract_result(
                "machine_contract:provenance_reconfirmation_paths",
                False,
                f"missing {' '.join(missing_flags)}",
                contract_id=contract_id,
            )
        ]

    try:
        packet_path = _workspace_path(packet_out or "", workspace)
        sheet_path = _workspace_path(sheet_out or "", workspace)
        audit_path = _workspace_path(audit_out or "", workspace)
        csv_path = _workspace_path(sheet_csv_out or "", workspace)
        packet = _read_json_file(packet_path)
        sheet = _read_json_file(sheet_path)
        audit = _read_json_file(audit_path)
        csv_rows = _read_csv_rows(csv_path)
        packet_sha = "sha256:" + hashlib.sha256(packet_path.read_bytes()).hexdigest()
        packet_row_count = _int_value(packet.get("row_count"), default=0)
        sheet_row_count = _int_value(sheet.get("row_count"))
        audit_row_count = _int_value(audit.get("row_count"))
        packet_rows = packet.get("rows") if isinstance(packet.get("rows"), list) else []
        packet_review_policy = (
            packet.get("review_policy") if isinstance(packet.get("review_policy"), dict) else {}
        )
        packet_row_semantics_ok = (
            len(packet_rows) == packet_row_count
            and all(
                isinstance(row, dict)
                and row.get("requested_decision")
                == PROVENANCE_RECONFIRMATION_REQUESTED_DECISION
                and row.get("decision_semantics")
                == PROVENANCE_RECONFIRMATION_DECISION_SEMANTICS
                and _flags_are_false(row, ("db_writes", "approval_claim"))
                for row in packet_rows
            )
        )
        packet_review_policy_ok = (
            packet_review_policy.get("does_not_change_existing_statuses") is True
            and packet_review_policy.get("reconfirm_is_evidence_review_only") is True
            and packet_review_policy.get("reconfirm_does_not_apply_or_preserve_status")
            is True
            and set(_string_list(packet_review_policy.get("allowed_decisions")))
            == set(PROVENANCE_RECONFIRMATION_ALLOWED_DECISIONS)
        )
        proofset_ok = (
            packet.get("ok") is True
            and sheet.get("ok") is True
            and audit.get("ok") is True
            and packet.get("schema") == "aihr_human_review_provenance_reconfirmation_packet_v1"
            and sheet.get("schema") == "aihr_provenance_reconfirmation_decision_sheet_v1"
            and audit.get("schema") == "aihr_provenance_reconfirmation_decision_audit_v1"
            and sheet.get("source_packet_sha256") == packet_sha
            and audit.get("source_packet_sha256") == packet_sha
            and sheet_row_count == packet_row_count
            and audit_row_count == packet_row_count
            and len(csv_rows) == packet_row_count
            and packet_row_semantics_ok
            and packet_review_policy_ok
            and _int_value(sheet.get("blank_decision_count")) == packet_row_count
            and _int_value(sheet.get("completed_decision_count")) == 0
            and _int_value(audit.get("pending_decision_count")) == packet_row_count
            and _int_value(audit.get("completed_decision_count")) == 0
            and _int_value(audit.get("action_eligible_count")) == 0
            and audit.get("guarded_apply_ready") is False
            and _flags_are_false(
                packet,
                ("status_update_allowed", "db_writes", "api_calls", "approval_claim"),
            )
            and _flags_are_false(
                sheet,
                ("status_update_allowed", "db_writes", "api_calls", "approval_claim"),
            )
            and _flags_are_false(
                audit,
                ("status_update_allowed", "db_writes", "approval_claim"),
            )
            and _rows_keep_decisions_blank(
                csv_rows,
                (
                    "decision",
                    "rationale",
                    "reviewer_id",
                    "reviewed_at",
                    "source_decision_packet",
                    "evidence_refs_json",
                ),
            )
            and _rows_keep_flags_false(
                csv_rows,
                ("status_update_allowed", "db_writes", "approval_claim"),
            )
        )
        detail = (
            f"packet_rows={packet_row_count} sheet_rows={sheet_row_count} "
            f"audit_rows={audit_row_count} csv_rows={len(csv_rows)} "
            f"row_semantics_ok={packet_row_semantics_ok} "
            f"review_policy_ok={packet_review_policy_ok}"
        )
    except Exception as exc:
        proofset_ok = False
        detail = f"proofset_contract_error:{exc}"
    return [
        _machine_contract_result(
            "machine_contract:provenance_reconfirmation_proofset_contract",
            proofset_ok,
            detail,
            contract_id=contract_id,
        )
    ]


REVIEW_TRIAGE_FORBIDDEN_STATUS_VALUES = {
    "human_reviewed",
    "accepted",
    "reviewed",
    "trusted/reviewed",
}
REVIEW_TRIAGE_STATUS_CLAIM_KEYS = {
    "review_status",
    "status",
    "target_review_status",
    "proposed_target_review_status",
    "proposed_review_status",
    "new_status",
}
REVIEW_TRIAGE_FORBIDDEN_TRUE_FLAG_KEYS = {
    "accepted",
    "acceptance_claim",
    "approval_claim",
    "approval_ready",
    "db_writes",
    "guarded_apply_ready",
    "human_reviewed",
    "raw_source_mutation_allowed",
    "reviewed",
    "status_update_allowed",
    "trusted_status_write_allowed",
}


def _review_triage_payload_safety_issues(payload: Any, *, path: str = "$") -> list[str]:
    issues: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized_key = str(key).strip().lower()
            child_path = f"{path}.{key}"
            if (
                normalized_key in REVIEW_TRIAGE_FORBIDDEN_TRUE_FLAG_KEYS
                and _true_value(value)
            ):
                issues.append(f"{child_path}=true")
            if (
                normalized_key in REVIEW_TRIAGE_STATUS_CLAIM_KEYS
                or normalized_key.endswith("_status")
            ) and str(value or "").strip().lower() in REVIEW_TRIAGE_FORBIDDEN_STATUS_VALUES:
                issues.append(f"{child_path}={value}")
            issues.extend(_review_triage_payload_safety_issues(value, path=child_path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            issues.extend(_review_triage_payload_safety_issues(item, path=f"{path}[{index}]"))
    return issues


def _verify_review_triage_machine_contract(
    *,
    args: list[str],
    workspace: Path,
) -> list[dict[str, Any]]:
    contract_id = "review_triage_readonly_contract_v1"
    out = _arg_value(args, "--out")
    required_inputs = {
        "--quality-report": _arg_value(args, "--quality-report"),
        "--review-priority-report": _arg_value(args, "--review-priority-report"),
        "--transition-seedpack": _arg_value(args, "--transition-seedpack"),
    }
    missing_flags = [
        flag
        for flag, value in ({"--out": out} | required_inputs).items()
        if not value
    ]
    if missing_flags:
        return [
            _machine_contract_result(
                "machine_contract:review_triage_readonly_contract",
                False,
                f"missing {' '.join(missing_flags)}",
                contract_id=contract_id,
            )
        ]

    try:
        out_path = _workspace_path(out or "", workspace)
        payload = _read_json_file(out_path)
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        source_paths = (
            summary.get("source_paths")
            if isinstance(summary.get("source_paths"), dict)
            else {}
        )
        input_paths_exist = True
        source_paths_match_args = True
        input_details: list[str] = []
        source_key_by_flag = {
            "--quality-report": "quality_report",
            "--review-priority-report": "review_priority_report",
            "--transition-seedpack": "transition_seedpack",
        }
        for flag, value in required_inputs.items():
            expected_path = _workspace_path(value or "", workspace)
            input_exists = (
                expected_path.exists()
                and expected_path.is_file()
                and expected_path.stat().st_size > 0
            )
            input_paths_exist = input_paths_exist and input_exists
            source_value = str(source_paths.get(source_key_by_flag[flag]) or "")
            source_match = bool(source_value) and (
                _contract_path_identity(source_value, workspace)
                == _contract_path_identity(value or "", workspace)
            )
            source_paths_match_args = source_paths_match_args and source_match
            input_details.append(f"{flag}={input_exists}")
        payload_safety_issues = _review_triage_payload_safety_issues(payload)
        payload_safety_ok = not payload_safety_issues
        readonly_ok = (
            payload.get("schema") == "ncs_review_triage_v1"
            and payload.get("ok") is True
            and payload.get("report_only") is True
            and payload.get("human_decision_required") is True
            and payload.get("status_update_allowed") is False
            and payload.get("db_writes") is False
            and payload.get("approval_claim") is False
            and payload.get("api_calls") in (False, None)
        )
        triage_ok = (
            readonly_ok
            and input_paths_exist
            and source_paths_match_args
            and payload_safety_ok
        )
        detail = (
            f"readonly_ok={readonly_ok} input_paths_exist={input_paths_exist} "
            f"source_paths_match_args={source_paths_match_args} "
            f"payload_safety_ok={payload_safety_ok} "
            f"payload_safety_issue_count={len(payload_safety_issues)} "
            f"inputs=[{', '.join(input_details)}]"
        )
    except Exception as exc:
        triage_ok = False
        detail = f"review_triage_contract_error:{exc}"
    return [
        _machine_contract_result(
            "machine_contract:review_triage_readonly_contract",
            triage_ok,
            detail,
            contract_id=contract_id,
        )
    ]


def _machine_acceptance_contract_results(
    *,
    args: list[str],
    workspace: Path,
) -> list[dict[str, Any]]:
    if len(args) < 3:
        return []
    command_name = args[2]
    if command_name in {"export-review-seedpack", "export-ontology-definition-seedpack"}:
        return _verify_review_seedpack_machine_contract(
            args=args,
            workspace=workspace,
            command_name=command_name,
        )
    if command_name == "export-human-review-provenance-reconfirmation-proofset":
        return _verify_provenance_reconfirmation_machine_contract(
            args=args,
            workspace=workspace,
        )
    if command_name == "review-triage":
        return _verify_review_triage_machine_contract(
            args=args,
            workspace=workspace,
        )
    return []


def _acceptance_check_results(
    *,
    declared_checks: list[str],
    artifact_checks: list[dict[str, Any]],
    exit_code: int | None,
    dry_run: bool = False,
    machine_contract_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    if dry_run:
        return [
            {
                "check": "dry_run_only",
                "ok": True,
                "detail": "Command was validated but not executed.",
            }
        ]
    missing = [item["path"] for item in artifact_checks if not item.get("exists")]
    empty = [item["path"] for item in artifact_checks if item.get("exists") and not item.get("non_empty")]
    missing_or_empty = missing + empty
    results = [
        {
            "check": "command_exited_zero",
            "ok": exit_code == 0,
            "detail": f"exit_code={exit_code}",
        },
        {
            "check": "expected_artifacts_exist",
            "ok": not missing,
            "detail": "all present" if not missing else ", ".join(missing),
        },
        {
            "check": "expected_artifacts_non_empty",
            "ok": not missing_or_empty,
            "detail": "all non-empty" if not missing_or_empty else ", ".join(missing_or_empty),
        },
    ]
    if declared_checks:
        results.append(
            {
                "check": "declared_acceptance_checks_recorded",
                "ok": True,
                "detail": (
                    f"{len(declared_checks)} declared checks recorded for operator review; "
                    "not automatically verified."
                ),
            }
        )
    results.extend(machine_contract_results or [])
    return results


def _declared_acceptance_check_requires_manual_handoff(declared_check: str) -> bool:
    text = str(declared_check or "").strip().lower()
    if not text:
        return False
    manual_markers = (
        "record commands",
        "touched files",
        "handoff",
        "signs off",
        "sign off",
        "sign-off",
        "operator confirms",
        "operator decision",
        "manual confirmation",
        "human review",
        "human reviews",
        "human reviewer",
        "human-reviewed",
    )
    return any(marker in text for marker in manual_markers)


def _machine_covers_declared_acceptance_check(
    declared_check: str,
    machine_contract_ids: set[str],
) -> bool:
    text = str(declared_check or "").strip().lower()
    if not text:
        return False
    if _declared_acceptance_check_requires_manual_handoff(declared_check):
        return False

    has_review_seedpack_contract = any(
        contract_id.endswith("_review_seedpack_contract_v1")
        for contract_id in machine_contract_ids
    )
    if has_review_seedpack_contract and "seedpack" in text:
        seedpack_shape_check = (
            "jsonl" in text
            and ("review-pending" in text or "candidate" in text)
        )
        seedpack_issue_type_check = (
            "issue_types" in text and "status_update_allowed" in text and "false" in text
        )
        return seedpack_shape_check or seedpack_issue_type_check

    has_provenance_contract = (
        "human_review_provenance_reconfirmation_proofset_contract_v1"
        in machine_contract_ids
    )
    if has_provenance_contract:
        if (
            "reconfirmation packet" in text
            and "decision audit" in text
            and "source packet hash" in text
        ):
            return True
        if "proofset" in text and (
            "report-only" in text
            or "report only" in text
            or "does not update" in text
        ):
            return True

    has_review_triage_contract = "review_triage_readonly_contract_v1" in machine_contract_ids
    if has_review_triage_contract:
        mentions_review_priority = "review-priority" in text or "review priority" in text
        mentions_quality_gates = "quality-gates" in text or "quality gates" in text
        mentions_transition_seedpack = (
            "transition seedpack" in text or "transition-seedpack" in text
        )
        if (
            "prerequisite" in text
            and mentions_review_priority
            and mentions_quality_gates
            and mentions_transition_seedpack
        ):
            return True
        does_not_mutate_review_statuses = any(
            marker in text
            for marker in (
                "does not mutate review statuses",
                "does not update review statuses",
                "does not write review statuses",
            )
        )
        if (
            ("review-triage" in text or "review triage" in text)
            and ("reads existing artifacts" in text or "read existing artifacts" in text)
            and does_not_mutate_review_statuses
        ):
            return True

    return False


def _machine_declared_acceptance_coverage(
    declared_checks: list[str],
    machine_contract_checks: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    machine_contract_ids = {
        str(check.get("machine_contract_id") or "")
        for check in machine_contract_checks
        if isinstance(check, dict) and str(check.get("machine_contract_id") or "").strip()
    }
    machine_verified: list[str] = []
    unverified: list[str] = []
    for declared_check in declared_checks:
        if _machine_covers_declared_acceptance_check(
            declared_check,
            machine_contract_ids,
        ):
            machine_verified.append(declared_check)
        else:
            unverified.append(declared_check)
    return machine_verified, unverified


def _acceptance_failed_count(runs: list[dict[str, Any]]) -> int:
    failed_count = 0
    for run in runs:
        checks = run.get("acceptance_check_results")
        if not isinstance(checks, list):
            continue
        for check in checks:
            if isinstance(check, dict) and not check.get("ok"):
                failed_count += 1
    return failed_count


def _acceptance_unverified_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        1
        for run in runs
        if run.get("acceptance_verification_status")
        in {
            "declared_checks_recorded_not_auto_verified",
            "machine_contract_partially_verified_non_decisional",
            "machine_contract_verified_manual_handoff_pending",
        }
    )


def _acceptance_unverified_declared_check_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        _int_value(run.get("unverified_declared_acceptance_check_count"), default=0)
        for run in runs
    )


def _acceptance_manual_unverified_declared_check_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        _int_value(
            run.get("manual_unverified_declared_acceptance_check_count"),
            default=0,
        )
        for run in runs
    )


def _acceptance_machine_unverified_declared_check_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        _int_value(
            run.get("machine_unverified_declared_acceptance_check_count"),
            default=0,
        )
        for run in runs
    )


def _acceptance_machine_verified_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        1
        for run in runs
        if run.get("acceptance_verification_status")
        == "machine_contract_verified_non_decisional"
    )


def _acceptance_machine_partially_verified_count(runs: list[dict[str, Any]]) -> int:
    return sum(
        1
        for run in runs
        if run.get("acceptance_verification_status")
        == "machine_contract_partially_verified_non_decisional"
    )


def _acceptance_machine_contract_manual_handoff_pending_count(
    runs: list[dict[str, Any]],
) -> int:
    return sum(
        1
        for run in runs
        if run.get("acceptance_verification_status")
        == "machine_contract_verified_manual_handoff_pending"
    )


def _annotate_acceptance_verification(run_record: dict[str, Any]) -> None:
    checks = (
        run_record.get("acceptance_check_results")
        if isinstance(run_record.get("acceptance_check_results"), list)
        else []
    )
    declared_checks = _string_list(run_record.get("declared_acceptance_checks"))
    failed = [check for check in checks if isinstance(check, dict) and not check.get("ok")]
    machine_contract_checks = [
        check
        for check in checks
        if isinstance(check, dict) and check.get("machine_contract") is True
    ]
    machine_verified_declared_checks, unverified_declared_checks = (
        _machine_declared_acceptance_coverage(declared_checks, machine_contract_checks)
    )
    manual_unverified_declared_checks = [
        check
        for check in unverified_declared_checks
        if _declared_acceptance_check_requires_manual_handoff(check)
    ]
    machine_unverified_declared_checks = [
        check
        for check in unverified_declared_checks
        if not _declared_acceptance_check_requires_manual_handoff(check)
    ]
    run_record["declared_acceptance_check_count"] = len(declared_checks)
    run_record["acceptance_failed_check_count"] = len(failed)
    run_record["machine_contract_check_count"] = len(machine_contract_checks)
    run_record["machine_verified_declared_acceptance_checks"] = (
        machine_verified_declared_checks
    )
    run_record["machine_verified_declared_acceptance_check_count"] = len(
        machine_verified_declared_checks
    )
    run_record["unverified_declared_acceptance_checks"] = unverified_declared_checks
    run_record["unverified_declared_acceptance_check_count"] = len(
        unverified_declared_checks
    )
    run_record["manual_unverified_declared_acceptance_checks"] = (
        manual_unverified_declared_checks
    )
    run_record["manual_unverified_declared_acceptance_check_count"] = len(
        manual_unverified_declared_checks
    )
    run_record["machine_unverified_declared_acceptance_checks"] = (
        machine_unverified_declared_checks
    )
    run_record["machine_unverified_declared_acceptance_check_count"] = len(
        machine_unverified_declared_checks
    )
    if run_record.get("status") == "dry_run":
        run_record["acceptance_verified"] = False
        run_record["acceptance_verification_status"] = "dry_run_not_executed"
    elif failed:
        run_record["acceptance_verified"] = False
        run_record["acceptance_verification_status"] = "execution_or_artifact_checks_failed"
    elif declared_checks:
        if machine_contract_checks and not unverified_declared_checks:
            run_record["acceptance_verified"] = True
            run_record["acceptance_verification_status"] = (
                "machine_contract_verified_non_decisional"
            )
        elif (
            machine_contract_checks
            and machine_verified_declared_checks
            and manual_unverified_declared_checks
            and not machine_unverified_declared_checks
        ):
            run_record["acceptance_verified"] = False
            run_record["acceptance_verification_status"] = (
                "machine_contract_verified_manual_handoff_pending"
            )
        elif machine_contract_checks and machine_verified_declared_checks:
            run_record["acceptance_verified"] = False
            run_record["acceptance_verification_status"] = (
                "machine_contract_partially_verified_non_decisional"
            )
        else:
            run_record["acceptance_verified"] = False
            run_record["acceptance_verification_status"] = (
                "declared_checks_recorded_not_auto_verified"
            )
    else:
        run_record["acceptance_verified"] = True
        run_record["acceptance_verification_status"] = "no_declared_acceptance_checks"


def _split_agent_queue_command(command: str) -> list[str]:
    if any(token in command for token in ("&", "|", ";", "<", ">", "\n", "\r")):
        raise ValueError("command_contains_shell_metacharacters")
    try:
        args = shlex.split(command, posix=False)
    except ValueError as exc:
        raise ValueError(f"command_parse_failed:{exc}") from exc
    if len(args) < 3:
        raise ValueError("command_too_short")
    if args[0].lower() not in {"python", "python.exe"}:
        raise ValueError("command_must_start_with_python")
    if args[1].replace("\\", "/") != "scripts/ncs_harness.py":
        raise ValueError("command_must_target_ncs_harness")
    if args[2] not in READ_ONLY_REPORT_COMMAND_NAMES:
        raise ValueError(f"command_not_allowed:{args[2]}")
    dashboard_url_violation = _dashboard_base_url_violation(args)
    if dashboard_url_violation:
        raise ValueError(dashboard_url_violation)
    return args


def _validate_auto_run_item(item: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    reasons: list[str] = []
    command = str(item.get("command") or "")
    if item.get("state") != "ready_to_start":
        reasons.append(f"state:{item.get('state')}")
    if item.get("can_start_automated") is not True:
        reasons.append("can_start_automated:false")
    if item.get("requires_human_decision"):
        reasons.append("requires_human_decision:true")
    if item.get("mutation_policy") != "regenerate_reports_only":
        reasons.append(f"mutation_policy:{item.get('mutation_policy')}")
    for violation in item.get("safety_violations") or []:
        reasons.append(f"safety:{violation}")
    try:
        args = _split_agent_queue_command(command)
    except ValueError as exc:
        reasons.append(str(exc))
        args = []
    return not reasons, reasons, args


def _agent_queue_execution_args(args: list[str]) -> list[str]:
    if not args:
        return args
    return [sys.executable, *args[1:]]


def run_agent_queue_ready_from_file(
    queue_path: Path,
    *,
    workspace: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    timeout_seconds: float = 300.0,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    workspace = (workspace or Path.cwd()).resolve()
    resolved_queue_path = _resolve_workspace_file_path(queue_path, workspace)
    source_queue_sha256 = "sha256:" + hashlib.sha256(resolved_queue_path.read_bytes()).hexdigest()
    status = build_agent_queue_status_from_file(resolved_queue_path, workspace=workspace)
    queue_status_snapshot_sha256 = _canonical_json_sha256(status)
    items_by_id = {str(item.get("id")): item for item in status.get("items") or []}
    execution_order = status.get("execution_order") if isinstance(status.get("execution_order"), list) else []
    if limit is not None:
        execution_order = execution_order[: max(limit, 0)]

    runs: list[dict[str, Any]] = []
    for order, execution_item in enumerate(execution_order, start=1):
        item_id = str(execution_item.get("id") or "")
        item = items_by_id.get(item_id) or execution_item
        valid, validation_errors, args = _validate_auto_run_item(item)
        run_record: dict[str, Any] = {
            "order": order,
            "id": item_id,
            "owner": item.get("owner"),
            "mutation_policy": item.get("mutation_policy"),
            "blocker_display_label": item.get("blocker_display_label"),
            "command": item.get("command"),
            "expected_artifacts": _string_list(item.get("expected_artifacts")),
            "declared_acceptance_checks": _string_list(item.get("acceptance_checks")),
            "validation_errors": validation_errors,
        }
        if not valid:
            run_record["status"] = "skipped_unsafe"
            runs.append(run_record)
            continue
        if dry_run:
            run_record.update(
                {
                    "status": "dry_run",
                    "args": args,
                    "expected_artifact_checks": _expected_artifact_checks(
                        run_record["expected_artifacts"],
                        workspace,
                    ),
                    "acceptance_check_results": _acceptance_check_results(
                        declared_checks=run_record["declared_acceptance_checks"],
                        artifact_checks=[],
                        exit_code=None,
                        dry_run=True,
                    ),
                }
            )
            _annotate_acceptance_verification(run_record)
            runs.append(run_record)
            continue

        started_at = _utc_now()
        start_time = time.monotonic()
        try:
            execution_args = _agent_queue_execution_args(args)
            completed = runner(
                execution_args,
                cwd=str(workspace),
                text=True,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
            )
            exit_code = int(getattr(completed, "returncode", 1))
            stdout = str(getattr(completed, "stdout", "") or "")
            stderr = str(getattr(completed, "stderr", "") or "")
            run_record.update(
                {
                    "status": "succeeded" if exit_code == 0 else "failed",
                    "exit_code": exit_code,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "duration_seconds": round(time.monotonic() - start_time, 3),
                }
            )
            run_record.update(_output_tail_fields("stdout", stdout))
            run_record.update(_output_tail_fields("stderr", stderr))
            artifact_checks = _expected_artifact_checks(run_record["expected_artifacts"], workspace)
            run_record["expected_artifact_checks"] = artifact_checks
            machine_contract_results = (
                _machine_acceptance_contract_results(args=args, workspace=workspace)
                if exit_code == 0
                else []
            )
            run_record["acceptance_check_results"] = _acceptance_check_results(
                declared_checks=run_record["declared_acceptance_checks"],
                artifact_checks=artifact_checks,
                exit_code=exit_code,
                machine_contract_results=machine_contract_results,
            )
            _annotate_acceptance_verification(run_record)
        except subprocess.TimeoutExpired as exc:
            stdout = str(exc.stdout or "")
            stderr = str(exc.stderr or "")
            run_record.update(
                {
                    "status": "failed_timeout",
                    "exit_code": None,
                    "started_at": started_at,
                    "finished_at": _utc_now(),
                    "duration_seconds": round(time.monotonic() - start_time, 3),
                }
            )
            run_record.update(_output_tail_fields("stdout", stdout))
            run_record.update(_output_tail_fields("stderr", stderr))
            artifact_checks = _expected_artifact_checks(run_record["expected_artifacts"], workspace)
            run_record["expected_artifact_checks"] = artifact_checks
            run_record["acceptance_check_results"] = _acceptance_check_results(
                declared_checks=run_record["declared_acceptance_checks"],
                artifact_checks=artifact_checks,
                exit_code=None,
            )
            _annotate_acceptance_verification(run_record)
        runs.append(run_record)

    summary = {
        "candidate_count": len(status.get("execution_order") or []),
        "selected_count": len(execution_order),
        "selected_item_ids": [
            str(item.get("id") or "")
            for item in execution_order
            if str(item.get("id") or "").strip()
        ],
        "dry_run": dry_run,
        "succeeded_count": sum(1 for item in runs if item.get("status") == "succeeded"),
        "failed_count": sum(1 for item in runs if str(item.get("status", "")).startswith("failed")),
        "skipped_unsafe_count": sum(1 for item in runs if item.get("status") == "skipped_unsafe"),
        "dry_run_count": sum(1 for item in runs if item.get("status") == "dry_run"),
    }
    summary["acceptance_failed_count"] = _acceptance_failed_count(runs)
    summary["acceptance_unverified_count"] = _acceptance_unverified_count(runs)
    summary["acceptance_unverified_declared_check_count"] = (
        _acceptance_unverified_declared_check_count(runs)
    )
    summary["acceptance_manual_unverified_declared_check_count"] = (
        _acceptance_manual_unverified_declared_check_count(runs)
    )
    summary["acceptance_machine_unverified_declared_check_count"] = (
        _acceptance_machine_unverified_declared_check_count(runs)
    )
    summary["acceptance_machine_verified_count"] = _acceptance_machine_verified_count(runs)
    summary["acceptance_machine_partially_verified_count"] = (
        _acceptance_machine_partially_verified_count(runs)
    )
    summary["acceptance_machine_contract_manual_handoff_pending_count"] = (
        _acceptance_machine_contract_manual_handoff_pending_count(runs)
    )
    return {
        "ok": (
            summary["failed_count"] == 0
            and summary["skipped_unsafe_count"] == 0
            and summary["acceptance_failed_count"] == 0
        ),
        "schema": AGENT_QUEUE_RUN_SCHEMA,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "source_queue_path": _display_path(resolved_queue_path, workspace),
        "source_queue_sha256": source_queue_sha256,
        "queue_status_snapshot_sha256": queue_status_snapshot_sha256,
        "workspace_ref": "configured_workspace",
        "summary": summary,
        "queue_status_summary": status.get("summary"),
        "runs": runs,
    }


def write_agent_queue_status_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# AI-HR Agent Queue Status",
        "",
        "## Summary",
        "",
        f"- ok: {report.get('ok')}",
        f"- status_update_allowed: {report.get('status_update_allowed')}",
        f"- db_writes: {report.get('db_writes')}",
        f"- approval_claim: {report.get('approval_claim')}",
        f"- item_count: {summary.get('item_count')}",
        f"- preflight_ok_count: {summary.get('preflight_ok_count')}",
        f"- auto_startable_count: {summary.get('auto_startable_count')}",
        f"- manual_ready_count: {summary.get('manual_ready_count')}",
        f"- manual_human_decision_count: {summary.get('manual_human_decision_count')}",
        f"- guarded_manual_count: {summary.get('guarded_manual_count')}",
        f"- manual_classification_counts: {summary.get('manual_classification_counts')}",
        f"- blocked_count: {summary.get('blocked_count')}",
        "- auto_startable_policy: can_start_automated=true and mutation_policy=regenerate_reports_only",
        f"- source_queue_path: {report.get('source_queue_path')}",
        "",
        "## Automated Start Order",
        "",
    ]
    for item in report.get("execution_order") or []:
        label = str(item.get("blocker_display_label") or item.get("id") or "")
        human_flag = (
            "human decision required after export"
            if item.get("requires_human_decision")
            else "no human decision required for this report-generation step"
        )
        lines.append(
            "- "
            f"priority {item.get('priority')} {label} "
            f"(id `{item.get('id')}`) "
            f"owner={item.get('owner')} state={item.get('state')} "
            f"can_start_automated={str(item.get('can_start_automated')).lower()} "
            f"policy={item.get('mutation_policy')} {human_flag}"
        )
        lines.append(f"  command: `{_markdown_queue_command(item)}`")
    if not report.get("execution_order"):
        lines.append("- No auto-startable items.")

    lines.extend(["", "## Manual Ready", ""])
    for item in report.get("manual_queue") or []:
        mutation_policy = str(item.get("mutation_policy") or "")
        guarded_manual = (
            item.get("requires_human_decision") is True
            or mutation_policy in GUARDED_MANUAL_MUTATION_POLICIES
        )
        label = str(item.get("blocker_display_label") or item.get("id") or "")
        covered_labels = item.get("covered_blocker_display_labels")
        if not isinstance(covered_labels, list) or not covered_labels:
            covered_labels = blocker_display_labels(item.get("covered_blockers") or [])
        lines.append(
            "- "
            f"{label} (id `{item.get('id')}`) owner={item.get('owner')} "
            f"state={item.get('state')} "
            f"can_start_automated={str(item.get('can_start_automated')).lower()} "
            f"requires_human_decision={str(item.get('requires_human_decision')).lower()} "
            f"guarded_manual={str(guarded_manual).lower()} "
            f"manual_classification={item.get('manual_classification')} "
            f"policy={item.get('mutation_policy')} blocker={item.get('blocker')} "
            f"covered_labels={', '.join(str(value) for value in covered_labels)}"
        )
        lines.append(
            "  automation: "
            f"block_reason={item.get('automation_block_reason')} "
            f"operator_action={item.get('operator_action_recommended')} "
            f"pending_human_decision_ids={item.get('pending_human_decision_ids') or []}"
        )
        guard = item.get("operational_guard") if isinstance(item.get("operational_guard"), dict) else {}
        lines.append(f"  command: `{_markdown_queue_command(item, guard=guard)}`")
        if guard:
            next_safe = guard.get("next_safe_action_resolution_status") or guard.get("next_safe_action_status")
            lines.append(
                "  guard: "
                f"qualification_retry_allowed_now={str(guard.get('qualification_retry_allowed_now')).lower()} "
                f"api_call_allowed_now={str(guard.get('api_call_allowed_now')).lower()} "
                f"next_safe_action_status={next_safe}"
            )
    if not report.get("manual_queue"):
        lines.append("- No manual-ready items.")

    lines.extend(["", "## Blocked", ""])
    for item in report.get("blocked_queue") or []:
        label = str(item.get("blocker_display_label") or item.get("id") or "")
        reasons = []
        if not item.get("agent_file_exists"):
            reasons.append(f"missing_agent_file={item.get('agent_file')}")
        reasons.extend(f"missing_prerequisite={path}" for path in item.get("missing_prerequisite_artifacts") or [])
        reasons.extend(f"safety={violation}" for violation in item.get("safety_violations") or [])
        guard = item.get("operational_guard") if isinstance(item.get("operational_guard"), dict) else {}
        if guard:
            reasons.append(
                "qualification_retry_allowed_now="
                f"{str(guard.get('qualification_retry_allowed_now')).lower()}"
            )
            reasons.append(f"api_call_allowed_now={str(guard.get('api_call_allowed_now')).lower()}")
            next_safe = guard.get("next_safe_action_resolution_status") or guard.get("next_safe_action_status")
            reasons.append(f"next_safe_action_status={next_safe}")
            if guard.get("checkpoint_path"):
                reasons.append(f"checkpoint_path={guard.get('checkpoint_path')}")
        lines.append(
            "- "
            f"{label} (id `{item.get('id')}`) state={item.get('state')} "
            f"owner={item.get('owner')} reasons={'; '.join(reasons) or 'unknown'}"
        )
    if not report.get("blocked_queue"):
        lines.append("- No blocked items.")

    lines.extend(["", "## Guardrails", ""])
    for guardrail in report.get("global_guardrails") or []:
        lines.append(f"- {guardrail}")
    if not report.get("global_guardrails"):
        lines.append("- No global guardrails declared.")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _markdown_queue_command(item: dict[str, Any], *, guard: dict[str, Any] | None = None) -> str:
    command = str(item.get("command") or "")
    mutation_policy = str(item.get("mutation_policy") or "")
    guard = guard if isinstance(guard, dict) else (
        item.get("operational_guard") if isinstance(item.get("operational_guard"), dict) else {}
    )
    safety_violations = item.get("safety_violations")
    if (
        mutation_policy == "guarded_api_collection"
        and (
            item.get("state") == "blocked_safety"
            or item.get("preflight_ok") is False
            or guard.get("qualification_retry_allowed_now") is False
            or (isinstance(safety_violations, list) and bool(safety_violations))
        )
    ):
        return "disabled_until_guard_allows_api_call"
    return command


def write_agent_queue_run_markdown(report: dict[str, Any], out_path: Path) -> None:
    summary = report.get("summary") or {}
    lines = [
        "# AI-HR Agent Queue Automated Run",
        "",
        "## Summary",
        "",
        f"- ok: {report.get('ok')}",
        f"- status_update_allowed: {report.get('status_update_allowed')}",
        f"- db_writes: {report.get('db_writes')}",
        f"- approval_claim: {report.get('approval_claim')}",
        f"- source_queue_path: {report.get('source_queue_path')}",
        f"- source_queue_sha256: {report.get('source_queue_sha256')}",
        f"- queue_status_snapshot_sha256: {report.get('queue_status_snapshot_sha256')}",
        f"- selected_count: {summary.get('selected_count')}",
        f"- succeeded_count: {summary.get('succeeded_count')}",
        f"- failed_count: {summary.get('failed_count')}",
        f"- skipped_unsafe_count: {summary.get('skipped_unsafe_count')}",
        f"- acceptance_failed_count: {summary.get('acceptance_failed_count')}",
        f"- acceptance_unverified_count: {summary.get('acceptance_unverified_count')}",
        "- acceptance_unverified_declared_check_count: "
        f"{summary.get('acceptance_unverified_declared_check_count')}",
        "- acceptance_manual_unverified_declared_check_count: "
        f"{summary.get('acceptance_manual_unverified_declared_check_count')}",
        "- acceptance_machine_unverified_declared_check_count: "
        f"{summary.get('acceptance_machine_unverified_declared_check_count')}",
        f"- acceptance_machine_verified_count: {summary.get('acceptance_machine_verified_count')}",
        "- acceptance_machine_partially_verified_count: "
        f"{summary.get('acceptance_machine_partially_verified_count')}",
        "- acceptance_machine_contract_manual_handoff_pending_count: "
        f"{summary.get('acceptance_machine_contract_manual_handoff_pending_count')}",
        f"- dry_run: {summary.get('dry_run')}",
        "",
        "## Runs",
        "",
    ]
    for item in report.get("runs") or []:
        label = str(item.get("blocker_display_label") or item.get("id") or "")
        lines.append(
            "- "
            f"{item.get('order')}. {label} (id `{item.get('id')}`): status={item.get('status')} "
            f"exit_code={item.get('exit_code')} owner={item.get('owner')}"
        )
        command_label = item.get("command") or item.get("command_label") or "command_suppressed"
        lines.append(f"  command: `{command_label}`")
        if item.get("validation_errors"):
            lines.append(f"  validation_errors: {item.get('validation_errors')}")
        acceptance = item.get("acceptance_check_results") if isinstance(item.get("acceptance_check_results"), list) else []
        if acceptance:
            failed = [check for check in acceptance if isinstance(check, dict) and not check.get("ok")]
            failed_names = [
                str(check.get("check") or "unknown_check")
                for check in failed
                if isinstance(check, dict)
            ]
            if failed:
                detail = f"{len(failed)} check(s) need attention"
                if failed_names:
                    detail += f" ({', '.join(failed_names)})"
            elif (
                item.get("acceptance_verification_status")
                == "declared_checks_recorded_not_auto_verified"
            ):
                detail = (
                    "execution_artifacts_ok; "
                    f"declared_checks={item.get('declared_acceptance_check_count')} "
                    "recorded_not_auto_verified"
                )
            elif (
                item.get("acceptance_verification_status")
                == "machine_contract_verified_non_decisional"
            ):
                detail = (
                    "execution_artifacts_ok; "
                    "machine_contract_verified_non_decisional; "
                    f"declared_checks={item.get('declared_acceptance_check_count')}"
                )
            elif (
                item.get("acceptance_verification_status")
                == "machine_contract_verified_manual_handoff_pending"
            ):
                detail = (
                    "execution_artifacts_ok; "
                    "machine_contract_verified_manual_handoff_pending; "
                    "machine_contract_verified_non_decisional; "
                    "manual_handoff_checks_pending="
                    f"{item.get('manual_unverified_declared_acceptance_check_count')} "
                    "machine_unverified_declared_checks="
                    f"{item.get('machine_unverified_declared_acceptance_check_count')}"
                )
            elif (
                item.get("acceptance_verification_status")
                == "machine_contract_partially_verified_non_decisional"
            ):
                detail = (
                    "execution_artifacts_ok; "
                    "machine_contract_partially_verified_non_decisional; "
                    "machine_verified_declared_checks="
                    f"{item.get('machine_verified_declared_acceptance_check_count')} "
                    "unverified_declared_checks="
                    f"{item.get('unverified_declared_acceptance_check_count')}"
                )
            elif item.get("acceptance_verification_status") == "dry_run_not_executed":
                detail = "dry_run_only"
            else:
                detail = "ok"
            lines.append(
                "  acceptance: "
                + detail
            )
        artifacts = item.get("expected_artifact_checks") if isinstance(item.get("expected_artifact_checks"), list) else []
        if artifacts:
            missing = [artifact.get("path") for artifact in artifacts if isinstance(artifact, dict) and not artifact.get("exists")]
            empty = [
                artifact.get("path")
                for artifact in artifacts
                if isinstance(artifact, dict) and artifact.get("exists") and not artifact.get("non_empty")
            ]
            detail = "all expected artifacts present and non-empty"
            if missing or empty:
                detail = f"missing={missing or []}; empty={empty or []}"
            lines.append(f"  artifacts: {detail}")
        if item.get("status") not in {"dry_run", "skipped_unsafe"}:
            output_parts = []
            for prefix in ("stdout", "stderr"):
                original_chars = item.get(f"{prefix}_original_chars")
                tail_chars = item.get(f"{prefix}_tail_chars")
                truncated = item.get(f"{prefix}_truncated")
                redacted = item.get(f"{prefix}_redacted")
                redaction_count = item.get(f"{prefix}_redaction_count")
                decision_redacted = item.get(f"{prefix}_human_decision_vocab_redacted")
                decision_redaction_count = item.get(
                    f"{prefix}_human_decision_vocab_redaction_count"
                )
                if original_chars is None or tail_chars is None or truncated is None:
                    continue
                output_parts.append(
                    f"{prefix}_chars={original_chars} "
                    f"tail_chars={tail_chars} "
                    f"truncated={truncated} "
                    f"redacted={bool(redacted)} "
                    f"redactions={int(redaction_count or 0)} "
                    f"human_decision_vocab_redacted={bool(decision_redacted)} "
                    f"human_decision_vocab_redactions={int(decision_redaction_count or 0)}"
                )
            if output_parts:
                lines.append(f"  output: {'; '.join(output_parts)}")
    if not report.get("runs"):
        lines.append("- No automated items selected.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
