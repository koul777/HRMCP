from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath
from typing import Any

from ncs_mcp.release_labels import add_blocker_display_fields, blocker_display_label
from ncs_mcp.review_safety import neutralize_suggested_action


DEFAULT_RELEASE_READINESS_PATH = Path("reports/aihr_release_readiness_20260624_autoresolve.json")
DEFAULT_RELEASE_READINESS_AFTER_RUNBOOK_PATH = Path("reports/aihr_release_readiness_20260624_after_runbook_v2.json")
DEFAULT_REVIEW_TRIAGE_PATH = Path("reports/aihr_review_triage_20260624.json")
DEFAULT_QUEUE_STATUS_PATH = Path("reports/aihr_agent_queue_status_20260624_autoresolve.json")
DEFAULT_QUALIFICATION_HYGIENE_PATH = Path("reports/qualification_retry_hygiene_20260624_current.json")
DEFAULT_QUALIFICATION_COVERAGE_PLAN_PATH = Path(
    "reports/qualification_collection_coverage_plan_20260624_current.json"
)
DEFAULT_API_LINKAGE_PATH = Path("reports/api_linkage_summary_major_14_15_19_20_20260624_final4.json")
DEFAULT_ONT_DEF_SEEDPACK_PATH = Path("reports/aihr_ontology_definition_review_seedpack_20260624.jsonl")
DEFAULT_HUMAN_REVIEW_BACKLOG_PATH = Path("reports/aihr_human_review_backlog_20260624_latest.json")
DEFAULT_REMAINING_BLOCKERS_PATH = Path("reports/aihr_remaining_blockers_20260624_latest.json")
DEFAULT_KSA_DEFINITION_OPERATOR_PACKET_PATH = Path("reports/ksa_definition_review_operator_packet_20260627.json")
DEFAULT_KSA_IMMUTABILITY_AUDIT_PATH = Path("reports/ksa_immutability_audit_20260703_10h.json")
RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS = (
    "artifact_lineage_contract",
    "cycle_safe_content_sha256",
    "cycle_safe_hash_excluded_fields",
    "sha256_scope",
)
RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_FIELDS = (
    "artifact.mtime_utc",
    "**.mtime_utc",
    "**.size_bytes",
    "**.content_sha256",
    "**.cycle_safe_content_sha256",
    "**.human_review_backlog.contract_ok",
    "**.human_review_backlog.source_hash_contract_ok",
    "**.human_review_backlog.source_hash_revalidation_ok",
    "**.human_review_backlog.source_hash_revalidation_checked_count",
    "**.human_review_backlog.source_hash_revalidation_mismatch_count",
    "**.human_review_backlog.source_hash_revalidation_issues",
)
RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_KEYS = {
    "mtime_utc",
    "size_bytes",
    "content_sha256",
    "cycle_safe_content_sha256",
}
RELEASE_READINESS_CYCLE_SAFE_HUMAN_REVIEW_BACKLOG_EXCLUDED_KEYS = {
    "contract_ok",
    "source_hash_contract_ok",
    "source_hash_revalidation_ok",
    "source_hash_revalidation_checked_count",
    "source_hash_revalidation_mismatch_count",
    "source_hash_revalidation_issues",
}

HUMAN_REVIEW_DECISION_FIELDS = (
    "decision",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "proposed_target_review_status",
    "proposed_issue_resolution",
    "proposed_review_status",
)
TRUSTED_REVIEW_STATUS_VALUES = {"accepted", "human_reviewed", "reviewed"}
FORBIDDEN_AUTOMATIC_STATUSES = ("human_reviewed", "accepted", "reviewed")
PROVENANCE_RECONFIRMATION_BLOCKER = "human_review:provenance_reconfirmation_required"
PROVENANCE_RECONFIRMATION_NEXT_SAFE_ACTION = (
    "export-human-review-provenance-reconfirmation-proofset"
)
SUPPORT_FORBIDDEN_TRUE_FIELDS = (
    "approval_claim",
    "acceptance_claim",
    "status_update_allowed",
    "db_writes",
    "api_calls",
    "status_updates",
    "human_review_status_updates",
    "trusted_status_write_allowed",
    "raw_source_mutation_allowed",
    "source_payload_exposed",
    "approval_ready",
)
SUPPORT_FORBIDDEN_PRESENT_FIELDS = ("source_payload",)
REVIEW_SEEDPACK_FORBIDDEN_TRUE_FIELDS = (
    "approval_claim",
    "db_writes",
    "source_payload",
    "source_payload_exposed",
    "trusted_status_write_allowed",
    "raw_source_mutation_allowed",
)
PUBLIC_REVIEW_SEEDPACK_FORBIDDEN_FIELD_LABELS = {
    "source_payload": "internal_payload_marker",
}


def _review_status_policy() -> dict[str, Any]:
    return {
        "human_decision_required_for_status_update": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "forbidden_automatic_statuses": list(FORBIDDEN_AUTOMATIC_STATUSES),
    }


def _support_payload_path(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if parent == "$":
        return f"$.{key}"
    return f"{parent}.{key}"


def _support_payload_unsafe_flag_violations(
    value: Any, *, path: str = "$"
) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = _support_payload_path(path, str(key))
            key_text = str(key)
            if key_text in SUPPORT_FORBIDDEN_TRUE_FIELDS and child is True:
                violations.append(
                    {
                        "path": child_path,
                        "key": key_text,
                        "value": True,
                        "reason": "forbidden_true_value",
                    }
                )
            if key_text in SUPPORT_FORBIDDEN_PRESENT_FIELDS:
                violations.append(
                    {
                        "path": child_path,
                        "key": key_text,
                        "value": "<present>",
                        "reason": "forbidden_field_present",
                    }
                )
            violations.extend(_support_payload_unsafe_flag_violations(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            violations.extend(
                _support_payload_unsafe_flag_violations(
                    child,
                    path=_support_payload_path(path, index),
                )
            )
    return violations


def _safe_support_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true_or_absent": payload.get("human_decision_required")
        in (True, None),
        "nested_unsafe_flags_absent": not _support_payload_unsafe_flag_violations(payload),
    }


def _support_artifact_source_refs(payload: dict[str, Any]) -> list[dict[str, str | None]]:
    refs: list[dict[str, str | None]] = []

    def walk(value: Any, *, path: str = "$") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = _support_payload_path(path, str(key))
                if key == "source_paths" and isinstance(child, dict):
                    for ref_key, ref_value in child.items():
                        refs.append(
                            {
                                "container": child_path,
                                "key": str(ref_key),
                                "path": str(ref_value or ""),
                            }
                        )
                elif key == "source_artifacts" and isinstance(child, dict):
                    for ref_key, ref_value in child.items():
                        path_value = (
                            ref_value.get("path") if isinstance(ref_value, dict) else ref_value
                        )
                        refs.append(
                            {
                                "container": child_path,
                                "key": str(ref_key),
                                "path": str(path_value or ""),
                            }
                        )
                walk(child, path=child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, path=_support_payload_path(path, index))

    walk(payload)
    return refs


def _operator_addendum_cycle_refs(payload: dict[str, Any]) -> list[dict[str, str | None]]:
    cycle_keys = {
        "goal_completion_audit",
        "terminal_evidence_index",
        "operator_addendum",
        "operator_entrypoint_manifest",
    }
    cycle_path_tokens = (
        "goal_completion_audit",
        "aihr_terminal_evidence_index",
        "aihr_release_blocker_operator_addendum",
        "aihr_operator_entrypoint_manifest",
    )
    cycle_refs: list[dict[str, str | None]] = []
    for ref in _support_artifact_source_refs(payload):
        key = (ref.get("key") or "").lower()
        path = (ref.get("path") or "").lower().replace("\\", "/")
        if key in cycle_keys or any(token in path for token in cycle_path_tokens):
            cycle_refs.append(ref)
    return cycle_refs


def _operator_entrypoint_cycle_refs(payload: dict[str, Any]) -> list[dict[str, str | None]]:
    return _operator_addendum_cycle_refs(payload)


def _operator_entrypoint_terminal_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "terminal_evidence_only_is_true": payload.get("terminal_evidence_only") is True,
        "include_in_release_refresh_dag_is_false": payload.get("include_in_release_refresh_dag")
        is False,
        "include_in_operator_handoff_is_false_or_absent": payload.get("include_in_operator_handoff")
        in (False, None),
    }


def _neutral_review_action_for_item(item: dict[str, Any]) -> str:
    issue = item.get("issue")
    if not isinstance(issue, dict):
        issue = {}
    return neutralize_suggested_action(
        item.get("suggested_action") or issue.get("suggested_action"),
        issue_type=item.get("issue_type") or issue.get("issue_type"),
        target_type=item.get("target_type") or issue.get("target_type"),
    )


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _canonical_json_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _release_readiness_cycle_safe_hash_excluded_fields() -> list[str]:
    return list(RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS) + [
        f"dashboard_surface_contract.{key}"
        for key in RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_FIELDS
    ]


def _dashboard_surface_contract_cycle_safe_projection(
    value: Any,
    *,
    path: tuple[str, ...] = (),
) -> Any:
    if isinstance(value, dict):
        return {
            key: _dashboard_surface_contract_cycle_safe_projection(
                child,
                path=(*path, str(key)),
            )
            for key, child in value.items()
            if key not in RELEASE_READINESS_CYCLE_SAFE_DASHBOARD_CONTRACT_EXCLUDED_KEYS
            and not (
                "human_review_backlog" in path
                and key in RELEASE_READINESS_CYCLE_SAFE_HUMAN_REVIEW_BACKLOG_EXCLUDED_KEYS
            )
        }
    if isinstance(value, list):
        return [
            _dashboard_surface_contract_cycle_safe_projection(child, path=path)
            for child in value
        ]
    return value


def _release_readiness_cycle_safe_sha256(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    projected = dict(payload)
    for key in RELEASE_READINESS_CYCLE_SAFE_HASH_EXCLUDED_FIELDS:
        projected.pop(key, None)
    if "dashboard_surface_contract" in projected:
        projected["dashboard_surface_contract"] = (
            _dashboard_surface_contract_cycle_safe_projection(
                projected.get("dashboard_surface_contract")
            )
        )
    return _canonical_json_sha256(projected)


def _source_artifact_snapshot(path_value: Any) -> dict[str, Any]:
    path_text = str(path_value or "")
    path = Path(path_text)
    exists = path.is_file()
    size_bytes = path.stat().st_size if exists else None
    snapshot: dict[str, Any] = {
        "path": path_text,
        "exists": exists,
        "non_empty": bool(size_bytes) if exists else False,
        "size_bytes": size_bytes,
        "sha256": (
            "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            if exists
            else None
        ),
    }
    if exists and path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        is_release_readiness_payload = (
            isinstance(payload, dict)
            and (
                payload.get("schema") == "aihr_release_readiness_v1"
                or (
                    "release_readiness" in path.stem
                    and "release_ready" in payload
                    and "blockers" in payload
                )
            )
        )
        if is_release_readiness_payload:
            cycle_safe_sha256 = _release_readiness_cycle_safe_sha256(payload)
            snapshot["sha256"] = cycle_safe_sha256
            snapshot["sha256_scope"] = "cycle_safe_release_readiness"
            snapshot["cycle_safe_content_sha256"] = cycle_safe_sha256
            snapshot["cycle_safe_hash_excluded_fields"] = (
                _release_readiness_cycle_safe_hash_excluded_fields()
            )
    return snapshot


def _source_artifact_snapshots(paths: dict[str, Any]) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    for key, value in paths.items():
        if value is None:
            continue
        if isinstance(value, (str, Path)):
            snapshots[str(key)] = _source_artifact_snapshot(value)
    return snapshots


def _nested_source_artifact_paths(prefix: str, value: Any) -> dict[str, Any]:
    if isinstance(value, (str, Path)):
        return {prefix: value}
    if isinstance(value, list):
        paths: dict[str, Any] = {}
        for index, item in enumerate(value):
            paths.update(_nested_source_artifact_paths(f"{prefix}[{index}]", item))
        return paths
    if isinstance(value, dict):
        paths: dict[str, Any] = {}
        for key, item in value.items():
            paths.update(_nested_source_artifact_paths(f"{prefix}.{key}", item))
        return paths
    return {}


def _append_source_artifact_hashes(
    lines: list[str],
    hashes: dict[str, Any] | None,
) -> None:
    if not isinstance(hashes, dict) or not hashes:
        return
    lines.extend(["", "## Source Artifact Hashes", ""])
    for key in sorted(hashes):
        item = hashes.get(key)
        if not isinstance(item, dict):
            continue
        lines.append(
            f"- `{key}`: exists=`{item.get('exists')}` "
            f"non_empty=`{item.get('non_empty')}` "
            f"size_bytes=`{item.get('size_bytes')}` "
            f"sha256=`{item.get('sha256')}` "
            f"sha256_scope=`{item.get('sha256_scope')}` "
            f"cycle_safe_content_sha256=`{item.get('cycle_safe_content_sha256')}` "
            f"path=`{item.get('path')}`"
        )


WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]|^\\\\")
LOCAL_DATABASE_PATH_RE = re.compile(
    r"(^|[\\/])data[\\/]processed[\\/][^\\/]+\.db(?:-(?:wal|shm|journal))?$",
    re.IGNORECASE,
)
PUBLIC_WORKSPACE_REF = "configured_workspace"
PUBLIC_DATABASE_REF = "configured_ncs_database"
PUBLIC_PATH_CONTEXT_KEYS = {
    "artifacts",
    "evidence",
    "latest_supporting_reports",
    "queue_supporting_report_inputs",
    "source_paths",
}


def _normalized_path_text(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/")


def _looks_like_windows_absolute_path(value: Any) -> bool:
    return bool(WINDOWS_ABSOLUTE_PATH_RE.match(str(value or "").strip()))


def _looks_like_local_database_path(value: Any) -> bool:
    return bool(LOCAL_DATABASE_PATH_RE.search(_normalized_path_text(value)))


def _public_artifact_path_text(value: Any) -> Any:
    if not isinstance(value, (str, Path)):
        return value
    text = str(value)
    if not text:
        return text
    if _looks_like_local_database_path(text):
        return PUBLIC_DATABASE_REF

    normalized = _normalized_path_text(text).rstrip("/")
    workspace = Path.cwd().resolve(strict=False)
    workspace_text = _normalized_path_text(workspace).rstrip("/")
    if normalized and workspace_text:
        if normalized.lower() == workspace_text.lower():
            return PUBLIC_WORKSPACE_REF
        prefix = f"{workspace_text}/"
        if normalized.lower().startswith(prefix.lower()):
            return normalized[len(prefix) :]

    candidate = Path(text)
    if candidate.is_absolute():
        try:
            return candidate.resolve(strict=False).relative_to(workspace).as_posix()
        except (OSError, ValueError):
            return candidate.name
    if _looks_like_windows_absolute_path(text):
        return PureWindowsPath(text).name
    return text


def _sanitize_public_artifact_paths(value: Any, *, path_context: bool = False) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            key_lower = key_text.lower()
            child_path_context = (
                path_context
                or key_lower in PUBLIC_PATH_CONTEXT_KEYS
                or key_lower == "path"
                or key_lower.endswith("_path")
                or key_lower.endswith("_paths")
            )
            sanitized[key_text] = _sanitize_public_artifact_paths(
                child,
                path_context=child_path_context,
            )
        return sanitized
    if isinstance(value, list):
        return [
            _sanitize_public_artifact_paths(item, path_context=path_context)
            for item in value
        ]
    if path_context:
        return _public_artifact_path_text(value)
    return value


def _resolve_report_artifact_reference(reference: str | None, anchor_path: Path) -> Path | None:
    if not reference:
        return None
    artifact_path = Path(reference)
    if artifact_path.is_absolute():
        return artifact_path
    cwd_path = Path.cwd() / artifact_path
    if cwd_path.exists():
        return cwd_path
    anchor_relative = anchor_path.parent / artifact_path
    if anchor_relative.exists():
        return anchor_relative
    return cwd_path


def _queue_source_path_key(value: Any) -> str:
    text = str(value or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return ""
    path = Path(text)
    workspace = Path.cwd()
    try:
        resolved = path.resolve(strict=False) if path.is_absolute() else (workspace / path).resolve(strict=False)
    except OSError:
        return text.lower()
    return resolved.as_posix().lower()


def _queue_source_path_matches(source: Any, expected: Any) -> bool:
    source_key = _queue_source_path_key(source)
    expected_key = _queue_source_path_key(expected)
    if not source_key or not expected_key:
        return False
    return source_key == expected_key


def _queue_source_path_consistency(
    release_readiness: dict[str, Any],
    queue_status: dict[str, Any],
) -> dict[str, Any]:
    expected = release_readiness.get("agent_work_queue_path")
    source = queue_status.get("source_queue_path")
    if not expected:
        return {
            "ok": True,
            "status": "not_applicable",
            "expected_agent_work_queue_path": expected,
            "queue_status_source_queue_path": source,
            "message": "release readiness artifact does not declare agent_work_queue_path",
        }
    if not source:
        return {
            "ok": False,
            "status": "missing_queue_status_source",
            "expected_agent_work_queue_path": expected,
            "queue_status_source_queue_path": source,
            "message": "queue status artifact is missing source_queue_path",
        }
    if not _queue_source_path_matches(source, expected):
        return {
            "ok": False,
            "status": "mismatch",
            "expected_agent_work_queue_path": expected,
            "queue_status_source_queue_path": source,
            "message": "queue status artifact source_queue_path does not match release readiness agent_work_queue_path",
        }
    return {
        "ok": True,
        "status": "matched",
        "expected_agent_work_queue_path": expected,
        "queue_status_source_queue_path": source,
        "message": "queue status source_queue_path matches release readiness agent_work_queue_path",
    }


def _ksa_definition_sidecar_summary(
    reference: str | None,
    *,
    anchor_path: Path,
    expected_schema: str,
    kind: str,
) -> dict[str, Any]:
    path = _resolve_report_artifact_reference(reference, anchor_path)
    summary: dict[str, Any] = {
        "path": _public_artifact_path_text(path) if path else None,
        "exists": bool(path and path.exists()),
        "ok": False,
        "schema": None,
        "schema_ok": False,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "trusted_status_write_allowed": False,
        "safety_ok": False,
    }
    if path is None:
        summary["error"] = f"{kind} sidecar reference missing"
        return summary
    if not path.exists():
        summary["error"] = f"{kind} sidecar file does not exist"
        return summary
    payload = _read_json(path)
    status_update_allowed = bool(payload.get("status_update_allowed"))
    db_writes = bool(payload.get("db_writes"))
    approval_claim = bool(payload.get("approval_claim"))
    trusted_status_write_allowed = bool(payload.get("trusted_status_write_allowed"))
    summary.update(
        {
            "ok": bool(payload.get("ok")),
            "schema": payload.get("schema"),
            "schema_ok": payload.get("schema") == expected_schema,
            "status_update_allowed": status_update_allowed,
            "db_writes": db_writes,
            "approval_claim": approval_claim,
            "trusted_status_write_allowed": trusted_status_write_allowed,
        }
    )
    if kind == "decision_audit":
        completed_decision_count = _safe_int(payload.get("completed_decision_count"))
        invalid_decision_count = _safe_int(payload.get("invalid_decision_count"))
        action_eligible_count = _safe_int(payload.get("action_eligible_count"))
        unsafe_flag_count = _safe_int(payload.get("unsafe_flag_count"))
        source_mismatch_count = _safe_int(payload.get("source_mismatch_count"))
        summary.update(
            {
                "completed_decision_count": completed_decision_count,
                "invalid_decision_count": invalid_decision_count,
                "action_eligible_count": action_eligible_count,
                "unsafe_flag_count": unsafe_flag_count,
                "source_mismatch_count": source_mismatch_count,
            }
        )
        summary["safety_ok"] = (
            bool(summary["ok"])
            and bool(summary["schema_ok"])
            and not status_update_allowed
            and not db_writes
            and not approval_claim
            and not trusted_status_write_allowed
            and completed_decision_count == 0
            and invalid_decision_count == 0
            and action_eligible_count == 0
            and unsafe_flag_count == 0
            and source_mismatch_count == 0
        )
    elif kind == "action_plan":
        actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
        action_count = _safe_int(payload.get("action_count"))
        explicit_operator_apply_count = sum(
            1 for action in actions if isinstance(action, dict) and action.get("requires_explicit_operator_apply") is True
        )
        summary.update(
            {
                "action_count": action_count,
                "blocked_by_invalid_audit": bool(payload.get("blocked_by_invalid_audit")),
                "explicit_operator_apply_count": explicit_operator_apply_count,
            }
        )
        summary["safety_ok"] = (
            bool(summary["ok"])
            and bool(summary["schema_ok"])
            and not status_update_allowed
            and not db_writes
            and not approval_claim
            and not trusted_status_write_allowed
            and not bool(summary["blocked_by_invalid_audit"])
            and action_count == 0
        )
    return summary


def _ksa_term_operator_packet_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    exists = path.exists()
    summary: dict[str, Any] = {
        "path": _public_artifact_path_text(path),
        "exists": exists,
        "ok": False,
        "ready_for_minimal_human_review": False,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "concept_review_group_count": 0,
        "pending_decision_count": 0,
        "decision_blank_count": 0,
        "suggested_decision_counts": {},
        "first_review_queue": [],
        "minimal_review_csv": None,
    }
    if not exists:
        return summary
    payload = _read_json(path)
    workflow_summary = payload.get("workflow_summary")
    if not isinstance(workflow_summary, dict):
        workflow_summary = {}
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    first_review_queue = workflow_summary.get("first_review_queue")
    if not isinstance(first_review_queue, list):
        first_review_queue = []
    summary.update(
        {
            "ok": bool(payload.get("ok")),
            "ready_for_minimal_human_review": bool(
                payload.get("ready_for_minimal_human_review")
                or (payload.get("readiness_summary") or {}).get("ready_for_minimal_human_review")
            ),
            "status_update_allowed": bool(payload.get("status_update_allowed")),
            "db_writes": bool(payload.get("db_writes")),
            "approval_claim": bool(payload.get("approval_claim")),
            "concept_review_group_count": _safe_int(
                payload.get("concept_review_group_count")
                or workflow_summary.get("concept_review_group_count")
            ),
            "pending_decision_count": _safe_int(
                payload.get("pending_decision_count")
                or workflow_summary.get("pending_decision_count")
            ),
            "decision_blank_count": _safe_int(workflow_summary.get("decision_blank_count")),
            "suggested_decision_counts": workflow_summary.get("suggested_decision_counts")
            if isinstance(workflow_summary.get("suggested_decision_counts"), dict)
            else {},
            "first_review_queue": [
                {
                    "concept_id": item.get("concept_id"),
                    "concept_name": item.get("concept_name"),
                    "concept_type": item.get("concept_type"),
                    "suggested_decision": item.get("suggested_decision"),
                    "suggested_decision_confidence": item.get("suggested_decision_confidence"),
                    "max_priority_score": item.get("max_priority_score"),
                    "item_count": item.get("item_count"),
                    "task_relation_count": item.get("task_relation_count"),
                    "training_course_link_count": item.get("training_course_link_count"),
                }
                for item in first_review_queue[:20]
                if isinstance(item, dict)
            ],
            "minimal_review_csv": _public_artifact_path_text(artifacts.get("minimal_review_csv")),
        }
    )
    return summary


def _ksa_definition_operator_packet_summary(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    exists = path.exists()
    summary: dict[str, Any] = {
        "path": _public_artifact_path_text(path),
        "exists": exists,
        "ok": False,
        "schema": None,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "source_payload_exposed": False,
        "trusted_status_write_allowed": False,
        "raw_source_mutation_allowed": False,
        "review_pack_row_count": 0,
        "review_csv_record_count": 0,
        "decision_blank_count": 0,
        "pending_decision_count": 0,
        "completed_decision_count": 0,
        "invalid_decision_count": 0,
        "action_plan_action_count": 0,
        "draft_definition_candidate_count": 0,
        "priority_report_row_count": 0,
        "first_review_queue": [],
        "artifacts": {},
        "safety_ok": False,
    }
    if not exists:
        return summary
    payload = _read_json(path)
    packet_summary = payload.get("summary")
    if not isinstance(packet_summary, dict):
        packet_summary = {}
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, dict):
        artifacts = {}
    safety_contract = payload.get("safety_contract")
    if not isinstance(safety_contract, dict):
        safety_contract = {}
    first_review_queue = packet_summary.get("first_review_queue")
    if not isinstance(first_review_queue, list):
        first_review_queue = []

    status_update_allowed = bool(
        payload.get("status_update_allowed")
        or safety_contract.get("status_update_allowed")
    )
    db_writes = bool(payload.get("db_writes") or safety_contract.get("db_writes"))
    approval_claim = bool(payload.get("approval_claim") or safety_contract.get("approval_claim"))
    source_payload_exposed = bool(
        payload.get("source_payload_exposed")
        or safety_contract.get("source_payload_exposed")
    )
    trusted_status_write_allowed = bool(
        payload.get("trusted_status_write_allowed")
        or safety_contract.get("trusted_status_write_allowed")
    )
    raw_source_mutation_allowed = bool(
        payload.get("raw_source_mutation_allowed")
        or safety_contract.get("raw_source_mutation_allowed")
    )
    review_csv_record_count = _safe_int(packet_summary.get("review_csv_record_count"))
    decision_blank_count = _safe_int(packet_summary.get("decision_blank_count"))
    completed_decision_count = _safe_int(packet_summary.get("completed_decision_count"))
    invalid_decision_count = _safe_int(packet_summary.get("invalid_decision_count"))
    action_plan_action_count = _safe_int(packet_summary.get("action_plan_action_count"))
    decision_audit_sidecar = _ksa_definition_sidecar_summary(
        artifacts.get("decision_audit") if isinstance(artifacts.get("decision_audit"), str) else None,
        anchor_path=path,
        expected_schema="ncs_ksa_definition_review_decision_audit_v1",
        kind="decision_audit",
    )
    action_plan_sidecar = _ksa_definition_sidecar_summary(
        artifacts.get("action_plan") if isinstance(artifacts.get("action_plan"), str) else None,
        anchor_path=path,
        expected_schema="ncs_ksa_definition_review_action_plan_v1",
        kind="action_plan",
    )
    sidecar_consistency_issues: list[str] = []
    if (
        decision_audit_sidecar.get("exists")
        and _safe_int(decision_audit_sidecar.get("completed_decision_count"))
        != completed_decision_count
    ):
        sidecar_consistency_issues.append("decision_audit_completed_decision_count_mismatch")
    if (
        decision_audit_sidecar.get("exists")
        and _safe_int(decision_audit_sidecar.get("invalid_decision_count"))
        != invalid_decision_count
    ):
        sidecar_consistency_issues.append("decision_audit_invalid_decision_count_mismatch")
    if (
        action_plan_sidecar.get("exists")
        and _safe_int(action_plan_sidecar.get("action_count")) != action_plan_action_count
    ):
        sidecar_consistency_issues.append("action_plan_action_count_mismatch")
    sidecar_safety_ok = (
        bool(decision_audit_sidecar.get("safety_ok"))
        and bool(action_plan_sidecar.get("safety_ok"))
        and not sidecar_consistency_issues
    )
    summary.update(
        {
            "ok": bool(payload.get("ok")),
            "schema": payload.get("schema"),
            "status_update_allowed": status_update_allowed,
            "db_writes": db_writes,
            "approval_claim": approval_claim,
            "source_payload_exposed": source_payload_exposed,
            "trusted_status_write_allowed": trusted_status_write_allowed,
            "raw_source_mutation_allowed": raw_source_mutation_allowed,
            "review_pack_row_count": _safe_int(packet_summary.get("review_pack_row_count")),
            "review_csv_record_count": review_csv_record_count,
            "decision_blank_count": decision_blank_count,
            "pending_decision_count": _safe_int(packet_summary.get("pending_decision_count")),
            "completed_decision_count": completed_decision_count,
            "invalid_decision_count": invalid_decision_count,
            "action_plan_action_count": action_plan_action_count,
            "draft_definition_candidate_count": _safe_int(
                packet_summary.get("draft_definition_candidate_count")
            ),
            "priority_report_row_count": _safe_int(packet_summary.get("priority_report_row_count")),
            "first_review_queue": [
                {
                    "concept_id": item.get("concept_id"),
                    "concept_name": item.get("concept_name"),
                    "concept_type": item.get("concept_type"),
                    "appearance_count": item.get("appearance_count"),
                    "unit_count": item.get("unit_count"),
                    "major_count": item.get("major_count"),
                    "recommended_review_action": item.get("recommended_review_action"),
                    "draft_review_policy": item.get("draft_review_policy"),
                    "draft_confidence": item.get("draft_confidence"),
                }
                for item in first_review_queue[:20]
                if isinstance(item, dict)
            ],
            "artifacts": {
                key: _public_artifact_path_text(value)
                for key, value in artifacts.items()
                if isinstance(value, str)
            },
            "sidecar_safety": {
                "safety_ok": sidecar_safety_ok,
                "consistency_issues": sidecar_consistency_issues,
                "decision_audit": decision_audit_sidecar,
                "action_plan": action_plan_sidecar,
            },
        }
    )
    summary["safety_ok"] = (
        bool(summary["ok"])
        and not status_update_allowed
        and not db_writes
        and not approval_claim
        and not source_payload_exposed
        and not trusted_status_write_allowed
        and not raw_source_mutation_allowed
        and completed_decision_count == 0
        and invalid_decision_count == 0
        and action_plan_action_count == 0
        and (review_csv_record_count == 0 or decision_blank_count == review_csv_record_count)
        and sidecar_safety_ok
    )
    return summary


def _dated_artifact_sort_key(path: Path) -> tuple[int, float]:
    for part in reversed(path.stem.split("_")):
        if len(part) == 8 and part.isdigit():
            return int(part), path.stat().st_mtime
    return 0, path.stat().st_mtime


def _latest_report_path(*patterns: str, fallback: Path) -> Path:
    reports_dir = fallback.parent
    candidates = [
        path
        for pattern in patterns
        for path in reports_dir.glob(pattern)
        if path.is_file()
    ]
    if not candidates:
        return fallback
    return max(candidates, key=_dated_artifact_sort_key)


KSA_DEFINITION_OPERATOR_PACKET_SIDECAR_MARKERS = (
    "_promotion_status",
    "_priority_report",
    "_priority_review_pack",
    "_decision_audit",
    "_action_plan",
)


def _is_ksa_definition_operator_packet(path: Path) -> bool:
    stem = path.stem
    if not stem.startswith("ksa_definition_review_operator_packet"):
        return False
    return not any(marker in stem for marker in KSA_DEFINITION_OPERATOR_PACKET_SIDECAR_MARKERS)


def latest_ksa_definition_operator_packet_path(*, fallback: Path | None = None) -> Path:
    fallback = fallback or DEFAULT_KSA_DEFINITION_OPERATOR_PACKET_PATH
    candidates = [
        path
        for path in fallback.parent.glob("ksa_definition_review_operator_packet*.json")
        if path.is_file() and _is_ksa_definition_operator_packet(path)
    ]
    if not candidates:
        return fallback
    return max(candidates, key=_dated_artifact_sort_key)


def _latest_report_path_near(
    anchor_path: Path | None,
    *patterns: str,
    fallback: Path,
) -> Path:
    def collect_candidates(reports_dir: Path) -> list[Path]:
        candidates: list[Path] = []
        seen: set[Path] = set()
        for pattern in patterns:
            for path in reports_dir.glob(pattern):
                try:
                    resolved = path.resolve()
                except OSError:
                    resolved = path
                if path.is_file() and resolved not in seen:
                    seen.add(resolved)
                    candidates.append(path)
        return candidates

    if anchor_path is not None:
        near_candidates = collect_candidates(anchor_path.parent)
        if near_candidates:
            return max(near_candidates, key=_dated_artifact_sort_key)

    candidates = collect_candidates(fallback.parent)
    if not candidates:
        return fallback
    return max(candidates, key=_dated_artifact_sort_key)


def _sibling_artifact_path_from_release(
    release_readiness_path: Path,
    *,
    prefix: str,
    suffix: str,
) -> Path:
    release_prefix = "aihr_release_readiness_"
    stem = release_readiness_path.stem
    if stem.startswith(release_prefix):
        stamp = stem[len(release_prefix):]
        if stamp:
            return release_readiness_path.parent / f"{prefix}{stamp}{suffix}"
    return release_readiness_path.parent / f"{prefix.rstrip('_')}{suffix}"


def _release_artifact_stamp_candidates(release_readiness_path: Path) -> list[str]:
    release_prefix = "aihr_release_readiness_"
    stem = release_readiness_path.stem
    if not stem.startswith(release_prefix):
        return []
    stamp = stem[len(release_prefix):]
    if not stamp:
        return []
    candidates = [stamp]
    date_match = re.match(r"^(\d{8})(?:_|$)", stamp)
    if date_match:
        date_stamp = date_match.group(1)
        if date_stamp not in candidates:
            candidates.append(date_stamp)
    return candidates


def _session_artifact_candidates_from_release(
    release_readiness_path: Path,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
) -> list[Path]:
    candidates: list[Path] = []
    for stamp in _release_artifact_stamp_candidates(release_readiness_path):
        for prefix in prefixes:
            for suffix in suffixes:
                candidates.append(release_readiness_path.parent / f"{prefix}{stamp}{suffix}")
    return candidates


def _release_family_artifact_path(
    release_readiness_path: Path | None,
    *,
    prefix: str,
    suffix: str,
    fallback: Path,
) -> Path:
    if release_readiness_path is None:
        return fallback
    candidates = _session_artifact_candidates_from_release(
        release_readiness_path,
        prefixes=(prefix,),
        suffixes=(suffix,),
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    if candidates:
        return candidates[0]
    return fallback


def _session_artifact_path_from_release(
    release_readiness_path: Path,
    *,
    prefixes: tuple[str, ...],
    suffixes: tuple[str, ...],
    patterns: tuple[str, ...],
) -> Path:
    candidates = _session_artifact_candidates_from_release(
        release_readiness_path,
        prefixes=prefixes,
        suffixes=suffixes,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    fallback = (
        candidates[0]
        if candidates
        else release_readiness_path.parent / f"{prefixes[0].rstrip('_')}{suffixes[0]}"
    )
    return _latest_report_path_near(
        release_readiness_path,
        *patterns,
        fallback=fallback,
    )


def _queue_snapshot_from_report(report: dict[str, Any]) -> dict[str, Any]:
    summary = report.get("summary")
    items = report.get("items")
    manual_queue = report.get("manual_queue")
    blocked_queue = report.get("blocked_queue")
    if not isinstance(items, list):
        items = []
    if not isinstance(manual_queue, list):
        manual_queue = []
    if not isinstance(blocked_queue, list):
        blocked_queue = []
    has_detailed_queue_sections = bool(items or manual_queue or blocked_queue)
    summary_values = summary if isinstance(summary, dict) else {}
    if isinstance(summary, dict) and summary and not has_detailed_queue_sections:
        return {
            "item_count": summary.get("item_count"),
            "blocked_count": summary.get("blocked_count"),
            "manual_ready_count": summary.get("manual_ready_count"),
            "auto_startable_count": summary.get("auto_startable_count"),
            "state_counts": summary.get("state_counts", {}),
        }

    state_counts: dict[str, int] = {}
    auto_startable_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if not state:
            if item.get("blocker_category") == "data_collection":
                state = "blocked_safety"
            elif (
                item.get("auto_runnable") is True
                and item.get("mutation_policy") == "regenerate_reports_only"
                and item.get("requires_human_decision") is not True
            ):
                state = "ready_to_start"
            elif item.get("requires_human_decision") is True:
                state = "manual_ready"
            else:
                state = "manual_ready"
        if state:
            state_counts[state] = state_counts.get(state, 0) + 1
        if (
            item.get("can_start_automated") is True
            and item.get("mutation_policy") == "regenerate_reports_only"
            and item.get("requires_human_decision") is not True
            or (
                state == "ready_to_start"
                and item.get("auto_runnable") is True
                and item.get("mutation_policy") == "regenerate_reports_only"
                and item.get("requires_human_decision") is not True
            )
        ):
            auto_startable_count += 1
    if not state_counts:
        for item in manual_queue:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "manual_ready")
            state_counts[state] = state_counts.get(state, 0) + 1
        for item in blocked_queue:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "blocked_safety")
            state_counts[state] = state_counts.get(state, 0) + 1

    return {
        "item_count": _first_present(
            summary_values.get("item_count"),
            report.get("item_count"),
            len(items) if items else None,
            len(manual_queue) + len(blocked_queue)
            if manual_queue or blocked_queue
            else None,
        ),
        "blocked_count": _first_present(
            summary_values.get("blocked_count"),
            report.get("blocked_count"),
            len(blocked_queue) if blocked_queue else None,
            state_counts.get("blocked_safety"),
        ),
        "manual_ready_count": _first_present(
            summary_values.get("manual_ready_count"),
            report.get("manual_ready_count"),
            len(manual_queue) if manual_queue else None,
            state_counts.get("manual_ready"),
        ),
        "auto_startable_count": auto_startable_count
        if has_detailed_queue_sections
        else _first_present(
            summary_values.get("auto_startable_count"),
            report.get("auto_startable_count"),
            state_counts.get("ready_to_start"),
        ),
        "state_counts": state_counts or summary_values.get("state_counts", {}),
    }


def _fallback_actions_from_queue_status(queue_status: dict[str, Any]) -> list[dict[str, Any]]:
    fallback_actions: list[dict[str, Any]] = []
    for item in queue_status.get("items", []):
        if not isinstance(item, dict):
            continue
        state = str(item.get("state") or "")
        if state != "ready_to_start":
            continue
        if item.get("can_start_automated") is not True:
            continue
        if item.get("mutation_policy") == "guarded_api_collection":
            continue
        fallback_actions.append(
            {
                "id": item.get("id"),
                "priority": item.get("priority"),
                "owner": item.get("owner"),
                "agent_file": item.get("agent_file"),
                "state": state,
                "mutation_policy": item.get("mutation_policy"),
                "command": item.get("command"),
                "reason": "preferred_automated_path",
            }
        )
    return fallback_actions


def _provenance_reconfirmation_queue_action(queue_status: dict[str, Any]) -> dict[str, Any]:
    sections = ("items", "fallback_actions", "execution_order")
    for section_name in sections:
        section = queue_status.get(section_name)
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            command = item.get("command")
            if not isinstance(command, str):
                continue
            if PROVENANCE_RECONFIRMATION_NEXT_SAFE_ACTION not in command:
                continue
            if item.get("can_start_automated") is False:
                continue
            if item.get("mutation_policy") == "guarded_api_collection":
                continue
            return {
                "next_safe_action": PROVENANCE_RECONFIRMATION_NEXT_SAFE_ACTION,
                "queue_item_id": item.get("id"),
                "owner": item.get("owner"),
                "state": item.get("state"),
                "mutation_policy": item.get("mutation_policy"),
                "source_section": section_name,
                "command": command,
            }
    return {
        "next_safe_action": PROVENANCE_RECONFIRMATION_NEXT_SAFE_ACTION,
        "queue_item_id": None,
        "owner": None,
        "state": None,
        "mutation_policy": "regenerate_reports_only",
        "source_section": None,
        "command": None,
    }


QUEUE_REPORT_INPUT_FLAGS = {
    "ncs006-checkpoint-path",
    "quality-report",
    "review-priority-report",
    "source-report-path",
    "transition-seedpack",
}
QUEUE_COMMAND_FLAG_RE = re.compile(r"--([A-Za-z0-9-]+)\s+(\"[^\"]+\"|'[^']+'|\S+)")
ARTIFACT_DATE_RE = re.compile(r"20\d{6}")


def _queue_command_report_inputs(command: Any) -> dict[str, list[str]]:
    if not isinstance(command, str):
        return {}
    inputs: dict[str, list[str]] = {}
    for flag, raw_value in QUEUE_COMMAND_FLAG_RE.findall(command):
        if flag not in QUEUE_REPORT_INPUT_FLAGS:
            continue
        key = flag.replace("-", "_")
        value = raw_value.strip("\"'")
        if value:
            inputs.setdefault(key, []).append(value)
    return inputs


def _queue_item_declared_report_inputs(item: dict[str, Any]) -> dict[str, list[str]]:
    inputs: dict[str, list[str]] = {}
    for field_name, key in (
        ("input_artifacts", "input_artifact"),
        ("prerequisite_artifacts", "prerequisite_artifact"),
    ):
        values = item.get(field_name)
        if not isinstance(values, list):
            continue
        for value in values:
            text = str(value or "").strip()
            if text:
                inputs.setdefault(key, []).append(text)
    return inputs


def _queue_supporting_report_inputs(queue_status: dict[str, Any]) -> dict[str, Any]:
    item_inputs: list[dict[str, Any]] = []
    aggregate: dict[str, set[str]] = {}
    seen: set[tuple[str, str]] = set()
    for source_section in ("execution_order", "items", "manual_queue", "fallback_actions"):
        section = queue_status.get(source_section)
        if not isinstance(section, list):
            continue
        for item in section:
            if not isinstance(item, dict):
                continue
            command = item.get("command")
            inputs = _queue_command_report_inputs(command)
            declared_inputs = _queue_item_declared_report_inputs(item)
            for key, values in declared_inputs.items():
                inputs.setdefault(key, []).extend(values)
            if not inputs:
                continue
            identity = (str(item.get("id") or ""), str(command or ""))
            if identity in seen:
                continue
            seen.add(identity)
            for key, values in inputs.items():
                aggregate.setdefault(key, set()).update(values)
            item_inputs.append(
                {
                    "id": item.get("id"),
                    "owner": item.get("owner"),
                    "state": item.get("state"),
                    "mutation_policy": item.get("mutation_policy"),
                    "source_section": source_section,
                    "inputs": inputs,
                }
            )
    return {
        "item_count": len(item_inputs),
        "inputs": {key: sorted(values) for key, values in sorted(aggregate.items())},
        "items": item_inputs,
    }


def _single_queue_report_input(
    queue_supporting_report_inputs: dict[str, Any],
    key: str,
) -> str | None:
    inputs = queue_supporting_report_inputs.get("inputs")
    if not isinstance(inputs, dict):
        return None
    values = inputs.get(key)
    if not isinstance(values, list) or len(values) != 1:
        return None
    value = values[0]
    return value if isinstance(value, str) and value else None


def _prefer_queue_report_input_for_same_date(
    recorded_path: Any,
    queue_path: str | None,
) -> Any:
    if not queue_path:
        return recorded_path
    recorded_dates = _artifact_date_tokens(recorded_path)
    queue_dates = _artifact_date_tokens(queue_path)
    if recorded_dates and queue_dates and recorded_dates == queue_dates:
        return queue_path
    return recorded_path


def _preferred_triage_source_paths(
    triage_source_paths: dict[str, Any],
    queue_supporting_report_inputs: dict[str, Any],
) -> dict[str, Any]:
    preferred = dict(triage_source_paths)
    preferred["review_priority_report"] = _prefer_queue_report_input_for_same_date(
        preferred.get("review_priority_report"),
        _single_queue_report_input(queue_supporting_report_inputs, "review_priority_report"),
    )
    preferred["transition_seedpack"] = _prefer_queue_report_input_for_same_date(
        preferred.get("transition_seedpack"),
        _single_queue_report_input(queue_supporting_report_inputs, "transition_seedpack"),
    )
    return {key: value for key, value in preferred.items() if value not in {None, ""}}


def _summary_with_source_paths(
    summary: Any,
    source_paths: dict[str, Any],
) -> dict[str, Any]:
    if isinstance(summary, dict):
        result = dict(summary)
    else:
        result = {}
    if source_paths:
        result["source_paths"] = source_paths
    return result


def _artifact_date_tokens(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(ARTIFACT_DATE_RE.findall(value))
    if isinstance(value, Path):
        return set(ARTIFACT_DATE_RE.findall(str(value)))
    if isinstance(value, dict):
        tokens: set[str] = set()
        for nested in value.values():
            tokens.update(_artifact_date_tokens(nested))
        return tokens
    if isinstance(value, (list, tuple, set)):
        tokens: set[str] = set()
        for nested in value:
            tokens.update(_artifact_date_tokens(nested))
        return tokens
    return set()


def _artifact_date_path_tokens(value: Any, *, prefix: str) -> dict[str, list[str]]:
    if isinstance(value, dict):
        paths: dict[str, list[str]] = {}
        for key, nested in value.items():
            nested_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.update(_artifact_date_path_tokens(nested, prefix=nested_prefix))
        return paths
    dates = sorted(_artifact_date_tokens(value))
    return {prefix: dates} if dates else {}


def _artifact_date_alignment(
    sections: dict[str, Any],
    *,
    active_date_source: Any | None = None,
) -> dict[str, Any]:
    section_dates = {
        key: sorted(_artifact_date_tokens(value))
        for key, value in sections.items()
    }
    all_dates = sorted({date for dates in section_dates.values() for date in dates})
    path_dates: dict[str, list[str]] = {}
    for key, value in sections.items():
        path_dates.update(_artifact_date_path_tokens(value, prefix=key))
    active_dates = sorted(_artifact_date_tokens(active_date_source)) if active_date_source is not None else []
    active_date = active_dates[-1] if active_dates else (all_dates[-1] if all_dates else None)
    stale_keys = [
        key
        for key, dates in path_dates.items()
        if active_date and dates and dates[-1] < active_date
    ]
    if not all_dates and not active_date:
        status = "no_dates"
    elif stale_keys:
        status = "stale_against_active_date"
    elif len(set(all_dates)) > 1:
        status = "mixed_artifact_dates"
    else:
        status = "aligned"
    return {
        "status": status,
        "active_date": active_date,
        "all_dates": all_dates,
        "section_dates": section_dates,
        "path_dates": path_dates,
        "stale_keys": stale_keys,
        "mixed_dates": len(set(all_dates)) > 1,
    }


def _release_readiness_demo_evidence_paths(release_readiness: dict[str, Any]) -> list[str]:
    evidence: list[str] = []
    demo_contract = (
        release_readiness.get("demo_contract")
        if isinstance(release_readiness.get("demo_contract"), dict)
        else {}
    )
    for artifact in demo_contract.get("json_artifacts") or []:
        if isinstance(artifact, dict) and artifact.get("path"):
            evidence.append(str(artifact.get("path")))
    html_artifact = (
        demo_contract.get("html_artifact")
        if isinstance(demo_contract.get("html_artifact"), dict)
        else {}
    )
    if html_artifact.get("path"):
        evidence.append(str(html_artifact.get("path")))
    dashboard_contract = (
        release_readiness.get("dashboard_surface_contract")
        if isinstance(release_readiness.get("dashboard_surface_contract"), dict)
        else {}
    )
    dashboard_artifact = (
        dashboard_contract.get("artifact")
        if isinstance(dashboard_contract.get("artifact"), dict)
        else {}
    )
    if dashboard_artifact.get("path"):
        evidence.append(str(dashboard_artifact.get("path")))
    return evidence


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Blocker report input file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Blocker report input file cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Blocker report input file is not valid JSON: {path}: {exc}") from exc


def _read_optional_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    return _read_json(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"Blocker report input file does not exist: {path}") from exc
    except OSError as exc:
        raise ValueError(f"Blocker report input file cannot be read: {path}: {exc}") from exc
    items: list[dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Blocker report input file is not valid JSONL: {path}: {exc}") from exc
        if isinstance(payload, dict):
            items.append(payload)
    return items


def _review_item_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if isinstance(record, dict) and record.get("record_type") != "batch"
    ]


def _record_issue_type(record: dict[str, Any]) -> str:
    issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
    value = record.get("issue_type") or issue.get("issue_type") or record.get("record_type")
    return str(value or "unknown")


def _record_target_type(record: dict[str, Any]) -> str:
    issue = record.get("issue") if isinstance(record.get("issue"), dict) else {}
    value = record.get("target_type") or issue.get("target_type") or record.get("record_type")
    return str(value or "unknown")


def _has_nonblank_decision_field(record: dict[str, Any]) -> bool:
    return any(str(record.get(field) or "").strip() for field in HUMAN_REVIEW_DECISION_FIELDS)


def _trusted_status_proposal(record: dict[str, Any]) -> str | None:
    for field in ("proposed_target_review_status", "proposed_review_status"):
        value = str(record.get(field) or "").strip()
        if value in TRUSTED_REVIEW_STATUS_VALUES:
            return value
    return None


def _seedpack_safety_field_violation_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        field: sum(
            1
            for record in records
            if field in record
            and (
                record.get(field) is not False
                or (field == "source_payload" and bool(record.get(field)))
            )
        )
        for field in REVIEW_SEEDPACK_FORBIDDEN_TRUE_FIELDS
    }


def _public_seedpack_forbidden_true_field_counts(counts: dict[str, int]) -> dict[str, int]:
    return {
        PUBLIC_REVIEW_SEEDPACK_FORBIDDEN_FIELD_LABELS.get(field, field): count
        for field, count in counts.items()
    }


def _audit_review_seedpack(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "path": str(path),
            "exists": False,
            "review_item_count": 0,
            "blank_decision_item_count": 0,
            "nonblank_decision_item_count": 0,
            "trusted_status_proposal_count": 0,
            "status_update_allowed_violations": 0,
            "missing_status_update_allowed_count": 0,
            "db_writes_violations": 0,
            "missing_db_writes_count": 0,
            "approval_claim_violations": 0,
            "missing_approval_claim_count": 0,
            "forbidden_true_field_counts": _public_seedpack_forbidden_true_field_counts({
                field: 0 for field in REVIEW_SEEDPACK_FORBIDDEN_TRUE_FIELDS
            }),
            "forbidden_true_field_violation_count": 0,
            "issue_type_counts": {},
            "target_type_counts": {},
            "safety_ok": False,
            "error": "seedpack file does not exist",
        }
    records = _read_jsonl(path)
    batch = next(
        (record for record in records if isinstance(record, dict) and record.get("record_type") == "batch"),
        {},
    )
    review_items = _review_item_records(records)
    issue_counts = Counter(_record_issue_type(record) for record in review_items)
    target_counts = Counter(_record_target_type(record) for record in review_items)
    batch_seedpack_id = str(batch.get("seedpack_id") or "").strip()
    batch_item_count = _safe_int(batch.get("item_count"))
    batch_item_count_matches = batch_item_count == len(review_items)
    item_seedpack_id_mismatch_count = sum(
        1
        for record in review_items
        if not batch_seedpack_id
        or str(record.get("seedpack_id") or "").strip() != batch_seedpack_id
    )
    sequence_values = [str(record.get("sequence") or "").strip() for record in review_items]
    missing_sequence_count = sum(1 for value in sequence_values if not value)
    duplicate_sequence_count = sum(
        count - 1 for value, count in Counter(sequence_values).items() if value and count > 1
    )
    missing_target_snapshot_hash_count = sum(
        1 for record in review_items if not str(record.get("target_snapshot_hash") or "").strip()
    )
    blank_decision_item_count = sum(1 for record in review_items if not _has_nonblank_decision_field(record))
    nonblank_decision_item_count = len(review_items) - blank_decision_item_count
    trusted_status_proposal_count = sum(1 for record in review_items if _trusted_status_proposal(record))
    status_update_allowed_violations = sum(
        1
        for record in records
        if "status_update_allowed" in record and record.get("status_update_allowed") is not False
    )
    missing_status_update_allowed_count = sum(
        1 for record in records if "status_update_allowed" not in record
    )
    db_writes_violations = sum(
        1
        for record in records
        if "db_writes" in record and record.get("db_writes") is not False
    )
    missing_db_writes_count = sum(1 for record in records if "db_writes" not in record)
    approval_claim_violations = sum(
        1
        for record in records
        if "approval_claim" in record and record.get("approval_claim") is not False
    )
    missing_approval_claim_count = sum(1 for record in records if "approval_claim" not in record)
    forbidden_true_field_counts = _seedpack_safety_field_violation_counts(records)
    forbidden_true_field_violation_count = sum(forbidden_true_field_counts.values())
    structure_issue_count = (
        (0 if batch_item_count_matches else 1)
        + item_seedpack_id_mismatch_count
        + missing_sequence_count
        + duplicate_sequence_count
        + missing_target_snapshot_hash_count
    )
    safety_ok = (
        nonblank_decision_item_count == 0
        and trusted_status_proposal_count == 0
        and status_update_allowed_violations == 0
        and missing_status_update_allowed_count == 0
        and db_writes_violations == 0
        and missing_db_writes_count == 0
        and approval_claim_violations == 0
        and missing_approval_claim_count == 0
        and forbidden_true_field_violation_count == 0
        and structure_issue_count == 0
    )
    return {
        "path": str(path),
        "exists": True,
        "format_version": batch.get("format_version"),
        "seedpack_id": batch.get("seedpack_id"),
        "batch_item_count": batch_item_count,
        "batch_item_count_matches": batch_item_count_matches,
        "item_seedpack_id_mismatch_count": item_seedpack_id_mismatch_count,
        "missing_sequence_count": missing_sequence_count,
        "duplicate_sequence_count": duplicate_sequence_count,
        "missing_target_snapshot_hash_count": missing_target_snapshot_hash_count,
        "structure_issue_count": structure_issue_count,
        "review_item_count": len(review_items),
        "blank_decision_item_count": blank_decision_item_count,
        "nonblank_decision_item_count": nonblank_decision_item_count,
        "trusted_status_proposal_count": trusted_status_proposal_count,
        "status_update_allowed_violations": status_update_allowed_violations,
        "missing_status_update_allowed_count": missing_status_update_allowed_count,
        "db_writes_violations": db_writes_violations,
        "missing_db_writes_count": missing_db_writes_count,
        "approval_claim_violations": approval_claim_violations,
        "missing_approval_claim_count": missing_approval_claim_count,
        "forbidden_true_field_counts": _public_seedpack_forbidden_true_field_counts(
            forbidden_true_field_counts
        ),
        "forbidden_true_field_violation_count": forbidden_true_field_violation_count,
        "issue_type_counts": dict(sorted(issue_counts.items())),
        "target_type_counts": dict(sorted(target_counts.items())),
        "safety_ok": safety_ok,
    }


def _find_blocker(report: dict[str, Any], name: str) -> dict[str, Any] | None:
    for blocker in report.get("blockers", []):
        if isinstance(blocker, dict) and blocker.get("name") == name:
            return blocker
    return None


def _qualification_next_safe_action(qualification_hygiene: dict[str, Any]) -> str | None:
    coverage_gap = (
        qualification_hygiene.get("coverage_gap")
        if isinstance(qualification_hygiene.get("coverage_gap"), dict)
        else {}
    )
    additional_attempted_units_needed = _safe_int(
        coverage_gap.get("additional_attempted_units_needed") or 0
    )
    unattempted_unit_count = _safe_int(coverage_gap.get("unattempted_unit_count") or 0)
    guard = qualification_hygiene.get("api_execution_guard")
    guard = guard if isinstance(guard, dict) else {}
    resolution_status = guard.get("next_safe_action_resolution_status")
    if isinstance(resolution_status, str) and resolution_status.strip():
        if (
            resolution_status == "complete_no_collection_needed"
            and (additional_attempted_units_needed > 0 or unattempted_unit_count > 0)
        ):
            return "plan_guarded_qualification_collection_for_unattempted_units"
        return resolution_status
    next_safe_action = qualification_hygiene.get("next_safe_action_status")
    if isinstance(next_safe_action, str) and next_safe_action.strip():
        if (
            next_safe_action == "complete_no_collection_needed"
            and (additional_attempted_units_needed > 0 or unattempted_unit_count > 0)
        ):
            return "plan_guarded_qualification_collection_for_unattempted_units"
        return next_safe_action
    if additional_attempted_units_needed > 0 or unattempted_unit_count > 0:
        return "plan_guarded_qualification_collection_for_unattempted_units"
    return None


def _qualification_collection_guard_evidence(
    qualification_hygiene: dict[str, Any],
    *,
    qualification_retry_allowed_now: Any,
) -> dict[str, Any]:
    coverage_gap = (
        qualification_hygiene.get("coverage_gap")
        if isinstance(qualification_hygiene.get("coverage_gap"), dict)
        else {}
    )
    guard = qualification_hygiene.get("api_execution_guard")
    guard = guard if isinstance(guard, dict) else {}
    coverage = coverage_gap.get("collection_coverage")
    if coverage is None:
        coverage = qualification_hygiene.get("collection_coverage")
    coverage_gap_open = bool(
        _safe_int(coverage_gap.get("additional_attempted_units_needed") or 0) > 0
        or _safe_int(coverage_gap.get("unattempted_unit_count") or 0) > 0
        or (isinstance(coverage, int | float) and coverage < 0.9)
    )
    next_safe_action = _qualification_next_safe_action(qualification_hygiene)
    api_call_allowed_now = (
        qualification_hygiene.get("api_call_allowed_now")
        if "api_call_allowed_now" in qualification_hygiene
        else guard.get("api_call_allowed_now")
    )
    element_api_call_allowed_now = guard.get("element_api_call_allowed_now")
    return {
        "automatic_collection_allowed_now": bool(
            api_call_allowed_now is True and element_api_call_allowed_now is True
        ),
        "operator_timing_required": coverage_gap_open,
        "guarded_collection_required": bool(coverage_gap_open),
        "retry_gate_complete_but_coverage_gap_open": bool(
            qualification_retry_allowed_now is True
            and coverage_gap_open
            and next_safe_action == "plan_guarded_qualification_collection_for_unattempted_units"
        ),
    }


def _unsafe_qualification_collection_batches(batches: list[Any]) -> list[Any]:
    return [
        batch
        for batch in batches
        if not isinstance(batch, dict)
        or batch.get("command_role") != "operator_timed_guarded_api_collection"
        or batch.get("auto_runnable") is not False
        or batch.get("automatic_queue_execution_allowed") is not False
        or batch.get("execution_authorized") is not False
        or batch.get("do_not_execute_from_report") is not True
        or batch.get("not_queue_item") is not True
        or batch.get("requires_operator_ticket") is not True
        or batch.get("requires_explicit_operator_start") is not True
        or batch.get("guard_required") is not True
        or batch.get("requires_operator_timing") is not True
        or batch.get("mutation_policy") != "guarded_api_collection"
        or "--ncs006-checkpoint-path" not in str(batch.get("command") or "")
    ]


def _qualification_coverage_plan_summary(
    path: Path | None,
) -> dict[str, Any]:
    payload = _read_optional_json(path)
    summary: dict[str, Any] = {
        "path": _public_artifact_path_text(path) if path else None,
        "exists": bool(path and path.exists()),
        "ok": False,
        "schema": None,
        "report_only": None,
        "status_update_allowed": None,
        "db_writes": None,
        "api_calls": None,
        "human_review_status_updates": None,
        "approval_claim": None,
        "automatic_collection_allowed_now": None,
        "operator_timed_guarded_api_commands_only": None,
        "automatic_queue_execution_allowed": None,
        "attempted_unit_count": None,
        "total_unit_count": None,
        "collection_coverage": None,
        "additional_attempted_units_needed": None,
        "estimated_batch_count": None,
        "batch_count": None,
        "raw_batch_count": None,
        "raw_batch_count_matches_batches": False,
        "unsafe_batch_count": None,
        "raw_unsafe_batch_count": None,
        "raw_unsafe_batch_count_matches_batches": False,
        "raw_unsafe_batches_count": None,
        "raw_unsafe_batches_match_batches": False,
        "must_not_write_human_review_statuses": None,
        "forbidden_status_updates_exact": False,
        "guard_summary_ok": False,
    }
    if payload is None:
        return summary
    current_state = (
        payload.get("current_state")
        if isinstance(payload.get("current_state"), dict)
        else {}
    )
    target_state = (
        payload.get("target_state")
        if isinstance(payload.get("target_state"), dict)
        else {}
    )
    guard_policy = (
        payload.get("guard_policy")
        if isinstance(payload.get("guard_policy"), dict)
        else {}
    )
    batches = payload.get("batches") if isinstance(payload.get("batches"), list) else []
    unsafe_batches = _unsafe_qualification_collection_batches(batches)
    raw_batch_count = payload.get("batch_count")
    raw_unsafe_batch_count = payload.get("unsafe_batch_count")
    raw_unsafe_batches = (
        payload.get("unsafe_batches")
        if isinstance(payload.get("unsafe_batches"), list)
        else None
    )
    forbidden_status_updates = guard_policy.get("forbidden_status_updates")
    forbidden_status_updates_exact = (
        isinstance(forbidden_status_updates, list)
        and tuple(str(value) for value in forbidden_status_updates)
        == FORBIDDEN_AUTOMATIC_STATUSES
    )
    raw_batch_count_matches_batches = raw_batch_count == len(batches)
    raw_unsafe_batch_count_matches_batches = raw_unsafe_batch_count == len(unsafe_batches)
    raw_unsafe_batches_match_batches = raw_unsafe_batches == unsafe_batches
    automatic_queue_execution_allowed = guard_policy.get("automatic_queue_execution_allowed")
    summary.update(
        {
            "ok": bool(payload.get("ok")),
            "schema": payload.get("schema"),
            "report_only": payload.get("report_only"),
            "status_update_allowed": payload.get("status_update_allowed"),
            "db_writes": payload.get("db_writes"),
            "api_calls": payload.get("api_calls"),
            "human_review_status_updates": payload.get("human_review_status_updates"),
            "approval_claim": payload.get("approval_claim"),
            "automatic_collection_allowed_now": payload.get("automatic_collection_allowed_now"),
            "operator_timed_guarded_api_commands_only": payload.get(
                "operator_timed_guarded_api_commands_only"
            ),
            "automatic_queue_execution_allowed": automatic_queue_execution_allowed,
            "attempted_unit_count": current_state.get("attempted_unit_count"),
            "total_unit_count": current_state.get("total_unit_count"),
            "collection_coverage": current_state.get("collection_coverage"),
            "additional_attempted_units_needed": target_state.get(
                "additional_attempted_units_needed"
            ),
            "estimated_batch_count": target_state.get("estimated_batch_count"),
            "batch_count": len(batches),
            "raw_batch_count": raw_batch_count,
            "raw_batch_count_matches_batches": raw_batch_count_matches_batches,
            "unsafe_batch_count": len(unsafe_batches),
            "raw_unsafe_batch_count": raw_unsafe_batch_count,
            "raw_unsafe_batch_count_matches_batches": raw_unsafe_batch_count_matches_batches,
            "raw_unsafe_batches_count": (
                len(raw_unsafe_batches) if raw_unsafe_batches is not None else None
            ),
            "raw_unsafe_batches_match_batches": raw_unsafe_batches_match_batches,
            "must_run_qualification_retry_hygiene_first": guard_policy.get(
                "must_run_qualification_retry_hygiene_first"
            ),
            "must_use_ncs006_checkpoint_path": guard_policy.get(
                "must_use_ncs006_checkpoint_path"
            ),
            "must_not_write_human_review_statuses": guard_policy.get(
                "must_not_write_human_review_statuses"
            ),
            "operator_timing_required": guard_policy.get("operator_timing_required"),
            "forbidden_status_updates_exact": forbidden_status_updates_exact,
        }
    )
    summary["guard_summary_ok"] = (
        summary["schema"] == "ncs_qualification_collection_coverage_plan_v1"
        and summary["ok"] is True
        and summary["report_only"] is True
        and summary["status_update_allowed"] is False
        and summary["db_writes"] is False
        and summary["api_calls"] is False
        and summary["human_review_status_updates"] is False
        and summary["approval_claim"] is False
        and summary["automatic_collection_allowed_now"] is False
        and summary["operator_timed_guarded_api_commands_only"] is True
        and automatic_queue_execution_allowed is False
        and summary["must_not_write_human_review_statuses"] is True
        and raw_batch_count_matches_batches
        and raw_unsafe_batch_count_matches_batches
        and len(unsafe_batches) == 0
        and raw_unsafe_batches == []
        and forbidden_status_updates_exact
    )
    return summary


def _ksa_immutability_audit_summary(path: Path | None) -> dict[str, Any]:
    payload = _read_optional_json(path)
    summary: dict[str, Any] = {
        "path": _public_artifact_path_text(path) if path else None,
        "exists": bool(path and path.exists()),
        "ok": False,
        "schema": None,
        "report_only": None,
        "human_decision_required_for_status_update": None,
        "forbidden_automatic_statuses": None,
        "safety_contract_ok": False,
        "status_update_allowed": None,
        "db_writes": None,
        "approval_claim": None,
        "raw_source_mutation_allowed": None,
        "trusted_status_write_allowed": None,
        "ksa_items_row_count": None,
        "ksa_items_sha256": None,
        "ksa_items_raw_text_multiset_sha256": None,
        "baseline_provided": None,
        "baseline_matches_current": None,
        "baseline_raw_text_multiset_matches_current": None,
        "baseline_source_text_matches_current": None,
        "boilerplate_definition_count": None,
        "boilerplate_trusted_status_count": None,
        "draft_or_template_trusted_status_count": None,
        "contract_ok": False,
    }
    if payload is None:
        return summary
    ksa_items = payload.get("ksa_items") if isinstance(payload.get("ksa_items"), dict) else {}
    baseline = payload.get("baseline") if isinstance(payload.get("baseline"), dict) else {}
    definitions = (
        payload.get("ontology_definitions")
        if isinstance(payload.get("ontology_definitions"), dict)
        else {}
    )
    safety = payload.get("safety") if isinstance(payload.get("safety"), dict) else {}
    baseline_matches_current = baseline.get("matches_current")
    baseline_source_text_matches_current = baseline.get("source_text_matches_current")
    forbidden_statuses = payload.get("forbidden_automatic_statuses")
    forbidden_statuses_exact = (
        isinstance(forbidden_statuses, list)
        and [str(value) for value in forbidden_statuses]
        == list(FORBIDDEN_AUTOMATIC_STATUSES)
    )
    safety_forbidden_statuses = safety.get("forbidden_automatic_statuses")
    safety_contract_ok = (
        payload.get("human_decision_required_for_status_update") is True
        and forbidden_statuses_exact
        and safety.get("human_decision_required_for_status_update") is True
        and safety.get("status_update_allowed") is False
        and safety.get("db_writes") is False
        and safety.get("approval_claim") is False
        and safety.get("raw_source_mutation_allowed") is False
        and safety.get("trusted_status_write_allowed") is False
        and isinstance(safety_forbidden_statuses, list)
        and [str(value) for value in safety_forbidden_statuses]
        == list(FORBIDDEN_AUTOMATIC_STATUSES)
    )
    contract_ok = (
        payload.get("schema") == "ncs_ksa_immutability_audit_v1"
        and payload.get("ok") is True
        and not payload.get("issues")
        and payload.get("report_only") is True
        and safety_contract_ok
        and payload.get("status_update_allowed") is False
        and payload.get("db_writes") is False
        and payload.get("approval_claim") is False
        and payload.get("raw_source_mutation_allowed") is False
        and payload.get("trusted_status_write_allowed") is False
        and isinstance(ksa_items.get("row_count"), int)
        and isinstance(ksa_items.get("sha256"), str)
        and str(ksa_items.get("sha256")).startswith("sha256:")
        and isinstance(ksa_items.get("raw_text_multiset_sha256"), str)
        and str(ksa_items.get("raw_text_multiset_sha256")).startswith("sha256:")
        and baseline.get("provided") is True
        and baseline_source_text_matches_current is True
        and definitions.get("total_concepts") is not None
        and definitions.get("boilerplate_trusted_status_count") == 0
        and definitions.get("draft_or_template_trusted_status_count") == 0
    )
    summary.update(
        {
            "ok": payload.get("ok"),
            "schema": payload.get("schema"),
            "report_only": payload.get("report_only"),
            "human_decision_required_for_status_update": payload.get(
                "human_decision_required_for_status_update"
            ),
            "forbidden_automatic_statuses": forbidden_statuses,
            "safety_contract_ok": safety_contract_ok,
            "status_update_allowed": payload.get("status_update_allowed"),
            "db_writes": payload.get("db_writes"),
            "approval_claim": payload.get("approval_claim"),
            "raw_source_mutation_allowed": payload.get("raw_source_mutation_allowed"),
            "trusted_status_write_allowed": payload.get("trusted_status_write_allowed"),
            "ksa_items_row_count": ksa_items.get("row_count"),
            "ksa_items_sha256": ksa_items.get("sha256"),
            "ksa_items_raw_text_multiset_sha256": ksa_items.get(
                "raw_text_multiset_sha256"
            ),
            "baseline_provided": baseline.get("provided"),
            "baseline_matches_current": baseline_matches_current,
            "baseline_raw_text_multiset_matches_current": baseline.get(
                "raw_text_multiset_matches_current"
            ),
            "baseline_source_text_matches_current": baseline_source_text_matches_current,
            "boilerplate_definition_count": definitions.get("boilerplate_definition_count"),
            "boilerplate_trusted_status_count": definitions.get("boilerplate_trusted_status_count"),
            "draft_or_template_trusted_status_count": definitions.get(
                "draft_or_template_trusted_status_count"
            ),
            "contract_ok": contract_ok,
        }
    )
    return summary


def _qualification_guard_status(qualification_hygiene: dict[str, Any]) -> str:
    guard = qualification_hygiene.get("api_execution_guard")
    guard = guard if isinstance(guard, dict) else {}
    qualification_retry_allowed_now = (
        qualification_hygiene.get("qualification_retry_allowed_now")
        if "qualification_retry_allowed_now" in qualification_hygiene
        else guard.get("qualification_retry_allowed_now")
    )
    if guard.get("status") == "blocked" or qualification_retry_allowed_now in {False, None}:
        return "guarded_blocked"
    if qualification_retry_allowed_now is True:
        return "guarded_manual_ready"
    return "open"


def _qualification_guard_note(qualification_hygiene: dict[str, Any]) -> str:
    status = _qualification_guard_status(qualification_hygiene)
    if status == "guarded_manual_ready":
        return (
            "Coverage is below target. Retry preflight may be clear, but coverage "
            "recovery still requires a guarded operator-timed collection plan."
        )
    if status == "guarded_blocked":
        return "Coverage is below target and guarded API retry is blocked by the current checkpoint."
    return "Coverage is below target; run qualification retry hygiene before any collection."


def _review_artifact_freshness_summary(
    *,
    remaining_blockers: dict[str, Any],
    human_review_backlog: dict[str, Any],
) -> dict[str, Any]:
    alignments = {
        "remaining_blockers": remaining_blockers.get("review_artifact_date_alignment"),
        "human_review_backlog": human_review_backlog.get("review_artifact_date_alignment"),
    }
    statuses: dict[str, str] = {}
    all_dates: set[str] = set()
    active_dates: list[str] = []
    stale_keys: list[str] = []
    for label, alignment in alignments.items():
        if not isinstance(alignment, dict):
            continue
        status = str(alignment.get("status") or "unknown")
        statuses[label] = status
        all_dates.update(str(date) for date in alignment.get("all_dates") or [])
        active_date = alignment.get("active_date")
        if active_date:
            active_dates.append(str(active_date))
        stale_keys.extend(f"{label}.{key}" for key in alignment.get("stale_keys") or [])
    if stale_keys:
        status = "stale_against_active_date"
    elif any(value == "mixed_artifact_dates" for value in statuses.values()):
        status = "mixed_artifact_dates"
    elif statuses:
        status = "aligned"
    else:
        status = "no_alignment_metadata"
    return {
        "status": status,
        "active_date": sorted(active_dates)[-1] if active_dates else (sorted(all_dates)[-1] if all_dates else None),
        "all_dates": sorted(all_dates),
        "stale_keys": sorted(stale_keys),
        "alignment_statuses": statuses,
    }


def _review_note_with_freshness(
    base_note: str,
    freshness: dict[str, Any],
    *,
    keywords: tuple[str, ...],
) -> str:
    stale_keys = [
        str(key)
        for key in freshness.get("stale_keys") or []
        if any(keyword in str(key) for keyword in keywords)
    ]
    if not stale_keys:
        return base_note
    visible_keys = ", ".join(stale_keys[:5])
    if len(stale_keys) > 5:
        visible_keys = f"{visible_keys}, ..."
    return f"{base_note} Freshness warning: stale supporting artifacts: {visible_keys}."


def build_remaining_blockers_report_from_files(
    *,
    release_readiness_path: Path | None = None,
    review_triage_path: Path | None = None,
    queue_status_path: Path | None = None,
    qualification_hygiene_path: Path | None = None,
    qualification_coverage_plan_path: Path | None = None,
    api_linkage_path: Path | None = None,
) -> dict[str, Any]:
    release_readiness_path_provided = release_readiness_path is not None
    release_readiness_path = release_readiness_path or _latest_report_path(
        "aihr_release_readiness_*.json",
        fallback=DEFAULT_RELEASE_READINESS_PATH,
    )
    review_triage_path = review_triage_path or _latest_report_path(
        "aihr_review_triage_*.json",
        fallback=DEFAULT_REVIEW_TRIAGE_PATH,
    )
    queue_status_path = queue_status_path or _latest_report_path(
        "aihr_agent_queue_status_*.json",
        fallback=DEFAULT_QUEUE_STATUS_PATH,
    )
    qualification_hygiene_path = qualification_hygiene_path or _latest_report_path(
        "qualification_retry_hygiene_*.json",
        fallback=DEFAULT_QUALIFICATION_HYGIENE_PATH,
    )
    qualification_coverage_plan_path = (
        qualification_coverage_plan_path
        or _latest_report_path_near(
            release_readiness_path if release_readiness_path_provided else None,
            "qualification_collection_coverage_plan_*.json",
            fallback=(
                _sibling_artifact_path_from_release(
                    release_readiness_path,
                    prefix="qualification_collection_coverage_plan_",
                    suffix=".json",
                )
                if release_readiness_path_provided
                else DEFAULT_QUALIFICATION_COVERAGE_PLAN_PATH
            ),
        )
    )
    api_linkage_path = api_linkage_path or _latest_report_path(
        "api_linkage_summary_20*.json",
        "api_linkage_summary_major_14_15_19_20_*.json",
        fallback=DEFAULT_API_LINKAGE_PATH,
    )
    release_readiness = _read_json(release_readiness_path)
    review_triage = _read_json(review_triage_path)
    queue_status = _read_json(queue_status_path)
    queue_source_consistency = _queue_source_path_consistency(
        release_readiness,
        queue_status,
    )
    qualification_hygiene = _read_json(qualification_hygiene_path)
    qualification_coverage_plan = _qualification_coverage_plan_summary(
        qualification_coverage_plan_path
    )
    api_linkage = _read_json(api_linkage_path)
    api_execution_guard = (
        qualification_hygiene.get("api_execution_guard")
        if isinstance(qualification_hygiene.get("api_execution_guard"), dict)
        else {}
    )
    qualification_retry_allowed_now = (
        qualification_hygiene.get("qualification_retry_allowed_now")
        if "qualification_retry_allowed_now" in qualification_hygiene
        else api_execution_guard.get("qualification_retry_allowed_now")
    )

    blockers = [
        {
            "name": blocker.get("name"),
            "category": blocker.get("category"),
            "status": "open",
            "evidence": {
                "release_readiness": str(release_readiness_path),
                "current_count": blocker.get("value"),
                "required_threshold": blocker.get("threshold"),
            },
        }
        for blocker in release_readiness.get("blockers", [])
        if isinstance(blocker, dict) and blocker.get("name")
    ]

    for blocker in blockers:
        add_blocker_display_fields(blocker)
        if blocker["name"] == "qualification:collection_coverage":
            blocker["status"] = _qualification_guard_status(qualification_hygiene)
            coverage_gap = (
                qualification_hygiene.get("coverage_gap")
                if isinstance(qualification_hygiene.get("coverage_gap"), dict)
                else {}
            )
            blocker["evidence"] = {
                "release_readiness": str(release_readiness_path),
                "qualification_retry_hygiene": str(qualification_hygiene_path),
                "qualification_coverage_plan": qualification_coverage_plan.get("path"),
                "coverage": coverage_gap.get("collection_coverage")
                if coverage_gap
                else qualification_hygiene.get("coverage"),
                "target": 0.9,
                "unattempted_unit_count": coverage_gap.get("unattempted_unit_count"),
                "additional_attempted_units_needed": coverage_gap.get(
                    "additional_attempted_units_needed"
                ),
                "retry_candidate_unit_count": qualification_hygiene.get(
                    "retry_candidate_unit_count"
                ),
                "api_call_allowed_now": (
                    qualification_hygiene.get("api_call_allowed_now")
                    if "api_call_allowed_now" in qualification_hygiene
                    else api_execution_guard.get("api_call_allowed_now")
                ),
                "element_api_call_allowed_now": api_execution_guard.get("element_api_call_allowed_now"),
                "qualification_retry_allowed_now": qualification_retry_allowed_now,
                "qualification_retry_guard_reason": api_execution_guard.get("qualification_retry_guard_reason"),
                "blocked_by_checkpoint": qualification_hygiene.get("blocked_by_checkpoint"),
                "checkpoint_path": qualification_hygiene.get("checkpoint_path"),
                "retry_hygiene_next_safe_action_resolution_status": (
                    api_execution_guard.get("next_safe_action_resolution_status")
                ),
                "coverage_gap_normalized_next_safe_action": _qualification_next_safe_action(
                    qualification_hygiene
                ),
                "retry_hygiene_status_scope": "retry_preflight_only_not_collection_coverage",
                "coverage_plan_guard_summary_ok": qualification_coverage_plan.get(
                    "guard_summary_ok"
                ),
                "coverage_plan_raw_batch_count_matches_batches": (
                    qualification_coverage_plan.get("raw_batch_count_matches_batches")
                ),
                "coverage_plan_raw_unsafe_batch_count_matches_batches": (
                    qualification_coverage_plan.get(
                        "raw_unsafe_batch_count_matches_batches"
                    )
                ),
                "coverage_plan_raw_unsafe_batches_count": qualification_coverage_plan.get(
                    "raw_unsafe_batches_count"
                ),
                "coverage_plan_raw_unsafe_batches_match_batches": (
                    qualification_coverage_plan.get("raw_unsafe_batches_match_batches")
                ),
                "coverage_plan_forbidden_status_updates_exact": (
                    qualification_coverage_plan.get("forbidden_status_updates_exact")
                ),
                "coverage_plan_human_review_status_updates": (
                    qualification_coverage_plan.get("human_review_status_updates")
                ),
                "coverage_plan_approval_claim": (
                    qualification_coverage_plan.get("approval_claim")
                ),
                "coverage_plan_must_not_write_human_review_statuses": (
                    qualification_coverage_plan.get(
                        "must_not_write_human_review_statuses"
                    )
                ),
            }
            blocker["evidence"].update(
                _qualification_collection_guard_evidence(
                    qualification_hygiene,
                    qualification_retry_allowed_now=qualification_retry_allowed_now,
                )
            )
            blocker["next_safe_action"] = _qualification_next_safe_action(qualification_hygiene)
        elif blocker["name"].startswith("review_debt:human_reviewed_"):
            blocker["next_safe_action"] = {
                "review_debt:human_reviewed_concepts": "export-ontology-definition-seedpack",
                "review_debt:human_reviewed_goal_links": "review-triage",
                "review_debt:human_reviewed_task_relations": "export-review-seedpack",
            }.get(blocker["name"])
        elif blocker["name"] == PROVENANCE_RECONFIRMATION_BLOCKER:
            queue_action = _provenance_reconfirmation_queue_action(queue_status)
            blocker["next_safe_action"] = queue_action["next_safe_action"]
            blocker["evidence"].update(
                {
                    "agent_queue_status": str(queue_status_path),
                    "queue_action_id": queue_action.get("queue_item_id"),
                    "queue_action_owner": queue_action.get("owner"),
                    "queue_action_state": queue_action.get("state"),
                    "queue_action_mutation_policy": queue_action.get("mutation_policy"),
                    "queue_action_source_section": queue_action.get("source_section"),
                    "queue_action_command": queue_action.get("command"),
                }
            )

    demo_contract = (
        release_readiness.get("demo_contract")
        if isinstance(release_readiness.get("demo_contract"), dict)
        else {}
    )
    dashboard_contract = (
        release_readiness.get("dashboard_surface_contract")
        if isinstance(release_readiness.get("dashboard_surface_contract"), dict)
        else {}
    )
    demo_evidence = [
        str(item.get("path"))
        for item in demo_contract.get("json_artifacts", [])
        if isinstance(item, dict) and item.get("path")
    ]
    html_artifact = (
        demo_contract.get("html_artifact")
        if isinstance(demo_contract.get("html_artifact"), dict)
        else {}
    )
    if html_artifact.get("path"):
        demo_evidence.append(str(html_artifact.get("path")))
    dashboard_artifact = (
        dashboard_contract.get("artifact")
        if isinstance(dashboard_contract.get("artifact"), dict)
        else {}
    )
    if dashboard_artifact.get("path"):
        demo_evidence.append(str(dashboard_artifact.get("path")))
    if not demo_evidence:
        demo_evidence = [
            str(
                _latest_report_path(
                    "aihr_plan_demo_*.json",
                    fallback=Path("reports/aihr_plan_demo_20260624.json"),
                )
            ),
            str(
                _latest_report_path(
                    "aihr_plan_demo_*.html",
                    fallback=Path("reports/aihr_plan_demo_20260624.html"),
                )
            ),
            str(
                _latest_report_path(
                    "aihr_dashboard_surface_verification_*.json",
                    fallback=Path("reports/aihr_dashboard_surface_verification_20260624_autoresolve.json"),
                )
            ),
        ]

    completed_items = [
        {
            "name": "query_route demo contract",
            "status": "verified",
            "evidence": demo_evidence,
        },
        {
            "name": "SQF legacy server.py split",
            "status": "verified",
            "evidence": [
                "src/ncs_mcp/server.py",
                "src/ncs_mcp/server_legacy_wrappers.py",
                "tests/test_server_legacy_wrappers.py",
            ],
        },
        {
            "name": "productization strategy documentation",
            "status": "verified",
            "evidence": [
                "docs/AIHR_PRODUCTIZATION_STRATEGY.md",
                str(release_readiness_path),
            ],
        },
    ]
    triage_source_paths = {}
    if isinstance(review_triage.get("summary"), dict):
        source_paths = review_triage["summary"].get("source_paths")
        if isinstance(source_paths, dict):
            triage_source_paths = source_paths
    queue_supporting_report_inputs = _queue_supporting_report_inputs(queue_status)
    triage_source_paths = _preferred_triage_source_paths(
        triage_source_paths,
        queue_supporting_report_inputs,
    )
    latest_supporting_reports = {
        "review_priority": str(
            triage_source_paths.get("review_priority_report")
            or _latest_report_path(
                "aihr_review_priority_*.json",
                fallback=Path("reports/aihr_review_priority_20260624.json"),
            )
        ),
        "review_triage": str(review_triage_path),
        "transition_seedpack": str(
            triage_source_paths.get("transition_seedpack")
            or _latest_report_path(
                "aihr_transition_scenario_seedpack_*.jsonl",
                fallback=Path("reports/aihr_transition_scenario_seedpack_20260624.jsonl"),
            )
        ),
        "review_seedpack": str(
            _latest_report_path_near(
                release_readiness_path,
                "aihr_review_seedpack_blocker_ranked_*.jsonl",
                "aihr_review_seedpack_*.jsonl",
                fallback=Path("reports/aihr_review_seedpack_20260624.jsonl"),
            )
        ),
        "agent_queue_status": str(queue_status_path),
        "qualification_hygiene": str(qualification_hygiene_path),
        "qualification_coverage_plan": str(qualification_coverage_plan_path),
        "api_linkage_summary": str(api_linkage_path),
    }
    source_artifact_hashes = _source_artifact_snapshots(
        {
            "release_readiness": release_readiness_path,
            "review_triage": review_triage_path,
            "queue_status": queue_status_path,
            "qualification_hygiene": qualification_hygiene_path,
            "qualification_coverage_plan": qualification_coverage_plan_path,
            "api_linkage_summary": api_linkage_path,
            **{
                f"latest_supporting_reports.{key}": value
                for key, value in latest_supporting_reports.items()
            },
            **_nested_source_artifact_paths(
                "queue_supporting_report_inputs",
                queue_supporting_report_inputs.get("inputs", {}),
            ),
        }
    )
    review_triage_summary = _summary_with_source_paths(
        review_triage.get("summary", {}),
        triage_source_paths,
    )

    report = {
        "schema": "aihr_remaining_blockers_v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "objective": "Continue AI-HR/NCS release blocker work with evidence-based status and possible code changes.",
        "completed_items": completed_items,
        "remaining_blockers": blockers,
        "latest_supporting_reports": latest_supporting_reports,
        "source_artifact_hashes": source_artifact_hashes,
        "queue_snapshot": _queue_snapshot_from_report(queue_status),
        "queue_source_path_consistency": queue_source_consistency,
        "fallback_actions": _fallback_actions_from_queue_status(queue_status),
        "queue_supporting_report_inputs": queue_supporting_report_inputs,
        "review_artifact_date_alignment": _artifact_date_alignment(
            {
                "latest_supporting_reports": latest_supporting_reports,
                "queue_supporting_report_inputs": queue_supporting_report_inputs.get("inputs", {}),
            },
            active_date_source=queue_supporting_report_inputs,
        ),
        "qualification_snapshot": {
            "coverage": qualification_hygiene.get("coverage_gap", {}).get("collection_coverage")
            if isinstance(qualification_hygiene.get("coverage_gap"), dict)
            else qualification_hygiene.get("collection_coverage"),
            "attempted_unit_count": qualification_hygiene.get("coverage_gap", {}).get("attempted_unit_count")
            if isinstance(qualification_hygiene.get("coverage_gap"), dict)
            else qualification_hygiene.get("attempted_unit_count"),
            "total_unit_count": qualification_hygiene.get("coverage_gap", {}).get("total_unit_count")
            if isinstance(qualification_hygiene.get("coverage_gap"), dict)
            else qualification_hygiene.get("total_unit_count"),
            "status_counts": qualification_hygiene.get("status_counts", {}),
            "api_execution_guard": api_execution_guard,
            "qualification_retry_allowed_now": qualification_retry_allowed_now,
        },
        "qualification_coverage_plan_snapshot": qualification_coverage_plan,
        "release_readiness": {
            "release_ready": release_readiness.get("release_ready"),
            "engineering_hygiene_ok": release_readiness.get("engineering_hygiene_ok"),
            "blocker_count": release_readiness.get("blocker_count"),
            "warning_count": release_readiness.get("warning_count"),
        },
        "review_triage": {
            "summary": review_triage_summary,
            "top_items_count": len(review_triage.get("transition_review_priorities", [])),
        },
        "api_linkage": {
            "summary": api_linkage.get("summary", {}),
            "safe_next_actions": api_linkage.get("safe_next_actions", []),
        },
    }
    return _sanitize_public_artifact_paths(report)


def write_remaining_blockers_markdown(report: dict[str, Any], out_path: Path) -> None:
    remaining_blockers = [
        blocker
        for blocker in report.get("remaining_blockers", [])
        if isinstance(blocker, dict)
    ]
    lead = (
        "Current state is evidence-backed, but unresolved blockers remain."
        if remaining_blockers
        else "Current state is stable and evidence-backed."
    )
    lines = [
        "# Remaining Blockers",
        "",
        lead,
        "",
        "## Safety Contract",
        "",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- forbidden_automatic_statuses: `{', '.join(FORBIDDEN_AUTOMATIC_STATUSES)}`",
        "",
        "## Completed",
        "",
    ]
    for item in report.get("completed_items", []):
        if not isinstance(item, dict):
            continue
        evidence = ", ".join(f"`{value}`" for value in item.get("evidence", []))
        lines.append(f"- {item.get('name')}: {item.get('status')} ({evidence})")
    lines.extend(["", "## Open Blockers", ""])
    for index, blocker in enumerate(remaining_blockers, start=1):
        name = str(blocker.get("name") or "")
        label = str(blocker.get("display_label") or blocker_display_label(name))
        lines.append(f"{index}. {label}")
        if name:
            lines.append(f"   - machine_blocker: `{name}`")
        evidence = blocker.get("evidence", {})
        if isinstance(evidence, dict):
            for key, value in evidence.items():
                lines.append(f"   - {key}: `{value}`")
        next_safe_action = blocker.get("next_safe_action")
        if next_safe_action:
            lines.append(f"   - Safe next action: `{next_safe_action}`")
        lines.append("")
    lines.extend(
        [
            "## Queue Snapshot",
            "",
        ]
    )
    queue_snapshot = report.get("queue_snapshot", {})
    for key in ["item_count", "blocked_count", "manual_ready_count", "auto_startable_count"]:
        lines.append(f"- `{key}`: `{queue_snapshot.get(key)}`")
    queue_source = report.get("queue_source_path_consistency")
    if isinstance(queue_source, dict):
        lines.extend(
            [
                "",
                "## Queue Source Path Consistency",
                "",
                f"- `ok`: `{queue_source.get('ok')}`",
                f"- `status`: `{queue_source.get('status')}`",
                f"- `expected_agent_work_queue_path`: `{queue_source.get('expected_agent_work_queue_path')}`",
                f"- `queue_status_source_queue_path`: `{queue_source.get('queue_status_source_queue_path')}`",
                f"- `message`: `{queue_source.get('message')}`",
            ]
        )
    coverage_plan = report.get("qualification_coverage_plan_snapshot")
    if isinstance(coverage_plan, dict):
        lines.extend(
            [
                "",
                "## Qualification Coverage Plan Guard",
                "",
                f"- `path`: `{coverage_plan.get('path')}`",
                f"- `exists`: `{coverage_plan.get('exists')}`",
                f"- `guard_summary_ok`: `{coverage_plan.get('guard_summary_ok')}`",
                f"- `report_only`: `{coverage_plan.get('report_only')}`",
                f"- `status_update_allowed`: `{coverage_plan.get('status_update_allowed')}`",
                f"- `db_writes`: `{coverage_plan.get('db_writes')}`",
                f"- `api_calls`: `{coverage_plan.get('api_calls')}`",
                f"- `human_review_status_updates`: `{coverage_plan.get('human_review_status_updates')}`",
                f"- `approval_claim`: `{coverage_plan.get('approval_claim')}`",
                f"- `automatic_collection_allowed_now`: `{coverage_plan.get('automatic_collection_allowed_now')}`",
                f"- `operator_timed_guarded_api_commands_only`: `{coverage_plan.get('operator_timed_guarded_api_commands_only')}`",
                f"- `automatic_queue_execution_allowed`: `{coverage_plan.get('automatic_queue_execution_allowed')}`",
                f"- `attempted_unit_count`: `{coverage_plan.get('attempted_unit_count')}`",
                f"- `total_unit_count`: `{coverage_plan.get('total_unit_count')}`",
                f"- `collection_coverage`: `{coverage_plan.get('collection_coverage')}`",
                f"- `additional_attempted_units_needed`: `{coverage_plan.get('additional_attempted_units_needed')}`",
                f"- `estimated_batch_count`: `{coverage_plan.get('estimated_batch_count')}`",
                f"- `batch_count`: `{coverage_plan.get('batch_count')}`",
                f"- `raw_batch_count`: `{coverage_plan.get('raw_batch_count')}`",
                f"- `raw_batch_count_matches_batches`: `{coverage_plan.get('raw_batch_count_matches_batches')}`",
                f"- `unsafe_batch_count`: `{coverage_plan.get('unsafe_batch_count')}`",
                f"- `raw_unsafe_batch_count`: `{coverage_plan.get('raw_unsafe_batch_count')}`",
                f"- `raw_unsafe_batch_count_matches_batches`: `{coverage_plan.get('raw_unsafe_batch_count_matches_batches')}`",
                f"- `raw_unsafe_batches_count`: `{coverage_plan.get('raw_unsafe_batches_count')}`",
                f"- `raw_unsafe_batches_match_batches`: `{coverage_plan.get('raw_unsafe_batches_match_batches')}`",
                f"- `must_run_qualification_retry_hygiene_first`: `{coverage_plan.get('must_run_qualification_retry_hygiene_first')}`",
                f"- `must_use_ncs006_checkpoint_path`: `{coverage_plan.get('must_use_ncs006_checkpoint_path')}`",
                f"- `must_not_write_human_review_statuses`: `{coverage_plan.get('must_not_write_human_review_statuses')}`",
                f"- `operator_timing_required`: `{coverage_plan.get('operator_timing_required')}`",
                f"- `forbidden_status_updates_exact`: `{coverage_plan.get('forbidden_status_updates_exact')}`",
            ]
        )
    fallback_actions = report.get("fallback_actions", [])
    if fallback_actions:
        lines.extend(["", "## Fallback Actions", ""])
        for action in fallback_actions[:10]:
            lines.append(
                f"- `{action.get('id')}` owner=`{action.get('owner')}` state=`{action.get('state')}` reason=`{action.get('reason')}`"
            )
    lines.append("")
    lines.extend(
        [
            "## Supporting Evidence",
            "",
        ]
    )
    for key, value in report.get("latest_supporting_reports", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    _append_source_artifact_hashes(lines, report.get("source_artifact_hashes"))
    queue_inputs = report.get("queue_supporting_report_inputs")
    if isinstance(queue_inputs, dict) and queue_inputs.get("inputs"):
        lines.extend(["", "## Queue Report Inputs", ""])
        for key, values in queue_inputs.get("inputs", {}).items():
            if isinstance(values, list):
                rendered_values = ", ".join(f"`{value}`" for value in values)
            else:
                rendered_values = f"`{values}`"
            lines.append(f"- `{key}`: {rendered_values}")
    alignment = report.get("review_artifact_date_alignment")
    if isinstance(alignment, dict):
        lines.extend(
            [
                "",
                "## Review Artifact Date Alignment",
                "",
                f"- `status`: `{alignment.get('status')}`",
                f"- `active_date`: `{alignment.get('active_date')}`",
                f"- `stale_keys`: `{', '.join(alignment.get('stale_keys') or [])}`",
            ]
        )
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_human_review_backlog_report_from_files(
    *,
    release_readiness_path: Path | None = None,
    review_triage_path: Path | None = None,
    ontology_seedpack_path: Path | None = None,
    review_seedpack_path: Path | None = None,
    transition_seedpack_path: Path | None = None,
    ksa_definition_operator_packet_path: Path | None = None,
    transition_provenance_crosswalk_json_path: Path | None = None,
    transition_provenance_crosswalk_csv_path: Path | None = None,
    transition_provenance_crosswalk_markdown_path: Path | None = None,
    transition_provenance_crosswalk_audit_path: Path | None = None,
    operator_packet_integrity_audit_path: Path | None = None,
) -> dict[str, Any]:
    release_readiness_path = release_readiness_path or _latest_report_path(
        "aihr_release_readiness_*.json",
        fallback=DEFAULT_RELEASE_READINESS_PATH,
    )
    review_triage_path = review_triage_path or _latest_report_path(
        "aihr_review_triage_*.json",
        fallback=DEFAULT_REVIEW_TRIAGE_PATH,
    )
    ontology_seedpack_path = ontology_seedpack_path or _latest_report_path(
        "aihr_ontology_definition_review_seedpack_*.jsonl",
        fallback=DEFAULT_ONT_DEF_SEEDPACK_PATH,
    )
    review_seedpack_path = review_seedpack_path or _latest_report_path(
        "aihr_review_seedpack_blocker_ranked_*.jsonl",
        "aihr_review_seedpack_*.jsonl",
        fallback=Path("reports/aihr_review_seedpack_blocker_ranked_20260624.jsonl"),
    )
    transition_seedpack_path = transition_seedpack_path or _latest_report_path(
        "aihr_transition_scenario_seedpack_*.jsonl",
        fallback=Path("reports/aihr_transition_scenario_seedpack_20260624.jsonl"),
    )
    ksa_definition_operator_packet_path = (
        ksa_definition_operator_packet_path
        or latest_ksa_definition_operator_packet_path(
            fallback=DEFAULT_KSA_DEFINITION_OPERATOR_PACKET_PATH
        )
    )
    release_readiness = _read_json(release_readiness_path)
    review_triage = _read_json(review_triage_path)
    ksa_definition_operator_packet = _ksa_definition_operator_packet_summary(
        ksa_definition_operator_packet_path
    )
    seedpack_audits = {
        "ontology_definition_seedpack": _audit_review_seedpack(ontology_seedpack_path),
        "blocker_ranked_seedpack": _audit_review_seedpack(review_seedpack_path),
        "transition_scenario_seedpack": _audit_review_seedpack(transition_seedpack_path),
    }
    seedpack_audit_values = list(seedpack_audits.values())

    blockers = []
    for blocker in release_readiness.get("blockers", []):
        if not isinstance(blocker, dict):
            continue
        name = str(blocker.get("name") or "")
        if name.startswith("review_debt:human_reviewed_"):
            blockers.append(
                add_blocker_display_fields({
                    "name": name,
                    "value": blocker.get("value"),
                    "threshold": blocker.get("threshold"),
                    "safe_next_action": {
                        "review_debt:human_reviewed_concepts": "export-ontology-definition-seedpack",
                        "review_debt:human_reviewed_goal_links": "review-triage",
                        "review_debt:human_reviewed_task_relations": "export-review-seedpack",
                    }.get(name),
                    "review_artifacts": {
                        "review_debt:human_reviewed_concepts": [
                            "ontology_definition_seedpack",
                            "ksa_definition_review_operator_packet",
                        ],
                        "review_debt:human_reviewed_goal_links": [
                            "blocker_ranked_seedpack",
                            "transition_scenario_seedpack",
                        ],
                        "review_debt:human_reviewed_task_relations": [
                            "blocker_ranked_seedpack",
                        ],
                    }.get(name, []),
                })
            )
        elif name == PROVENANCE_RECONFIRMATION_BLOCKER:
            blockers.append(
                add_blocker_display_fields({
                    "name": name,
                    "value": blocker.get("value"),
                    "threshold": blocker.get("threshold"),
                    "safe_next_action": PROVENANCE_RECONFIRMATION_NEXT_SAFE_ACTION,
                    "open_first": "transition_provenance_operator_crosswalk_csv",
                    "review_artifacts": [
                        "transition_provenance_operator_crosswalk_csv",
                        "transition_provenance_operator_crosswalk_markdown",
                        "transition_provenance_operator_crosswalk_audit",
                        "operator_packet_integrity_audit",
                        "provenance_reconfirmation_packet",
                        "provenance_reconfirmation_decision_sheet_markdown",
                        "provenance_reconfirmation_decision_sheet_csv",
                        "provenance_reconfirmation_decision_audit",
                    ],
                })
            )

    top_items = review_triage.get("review_priority_items", [])
    focus_overlays = review_triage.get("focus_review_priority_overlays", [])
    source_paths = {
        "release_readiness": str(release_readiness_path),
        "review_triage": str(review_triage_path),
        "ontology_definition_seedpack": str(ontology_seedpack_path),
        "blocker_ranked_seedpack": str(review_seedpack_path),
        "transition_scenario_seedpack": str(transition_seedpack_path),
        "ksa_definition_review_operator_packet": str(ksa_definition_operator_packet_path),
    }
    if any(blocker.get("name") == PROVENANCE_RECONFIRMATION_BLOCKER for blocker in blockers):
        transition_provenance_crosswalk_json_path = (
            transition_provenance_crosswalk_json_path
            or _session_artifact_path_from_release(
                release_readiness_path,
                prefixes=("transition_provenance_operator_crosswalk_",),
                suffixes=(".json",),
                patterns=("transition_provenance_operator_crosswalk_*.json",),
            )
        )
        transition_provenance_crosswalk_csv_path = (
            transition_provenance_crosswalk_csv_path
            or _session_artifact_path_from_release(
                release_readiness_path,
                prefixes=("transition_provenance_operator_crosswalk_",),
                suffixes=(".csv",),
                patterns=("transition_provenance_operator_crosswalk_*.csv",),
            )
        )
        transition_provenance_crosswalk_markdown_path = (
            transition_provenance_crosswalk_markdown_path
            or _session_artifact_path_from_release(
                release_readiness_path,
                prefixes=("transition_provenance_operator_crosswalk_",),
                suffixes=(".md",),
                patterns=("transition_provenance_operator_crosswalk_*.md",),
            )
        )
        transition_provenance_crosswalk_audit_path = (
            transition_provenance_crosswalk_audit_path
            or _session_artifact_path_from_release(
                release_readiness_path,
                prefixes=("transition_provenance_operator_crosswalk_audit_",),
                suffixes=(".json",),
                patterns=("transition_provenance_operator_crosswalk_audit_*.json",),
            )
        )
        provenance_source_paths = {
            "transition_provenance_operator_crosswalk_json": str(
                transition_provenance_crosswalk_json_path
            ),
            "transition_provenance_operator_crosswalk_csv": str(
                transition_provenance_crosswalk_csv_path
            ),
            "transition_provenance_operator_crosswalk_markdown": str(
                transition_provenance_crosswalk_markdown_path
            ),
            "transition_provenance_operator_crosswalk_audit": str(
                transition_provenance_crosswalk_audit_path
            ),
            "provenance_reconfirmation_packet": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_packet_",
                        "aihr_human_review_provenance_reconfirmation_packet_",
                    ),
                    suffixes=(".json",),
                    patterns=(
                        "human_review_provenance_reconfirmation_packet_*.json",
                        "aihr_human_review_provenance_reconfirmation_packet_*.json",
                    ),
                )
            ),
            "provenance_reconfirmation_decision_sheet_json": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_decision_sheet_",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_",
                    ),
                    suffixes=(".json",),
                    patterns=(
                        "human_review_provenance_reconfirmation_decision_sheet_*.json",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_*.json",
                    ),
                )
            ),
            "provenance_reconfirmation_decision_sheet_csv": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_decision_sheet_",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_",
                    ),
                    suffixes=(".csv",),
                    patterns=(
                        "human_review_provenance_reconfirmation_decision_sheet_*.csv",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_*.csv",
                    ),
                )
            ),
            "provenance_reconfirmation_decision_sheet_markdown": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_decision_sheet_",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_",
                    ),
                    suffixes=(".md",),
                    patterns=(
                        "human_review_provenance_reconfirmation_decision_sheet_*.md",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_*.md",
                    ),
                )
            ),
            "provenance_reconfirmation_decision_sheet_html": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_decision_sheet_",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_",
                    ),
                    suffixes=(".html",),
                    patterns=(
                        "human_review_provenance_reconfirmation_decision_sheet_*.html",
                        "aihr_human_review_provenance_reconfirmation_decision_sheet_*.html",
                    ),
                )
            ),
            "provenance_reconfirmation_decision_audit": str(
                _session_artifact_path_from_release(
                    release_readiness_path,
                    prefixes=(
                        "human_review_provenance_reconfirmation_decision_audit_",
                        "aihr_human_review_provenance_reconfirmation_decision_audit_",
                    ),
                    suffixes=(".json",),
                    patterns=(
                        "human_review_provenance_reconfirmation_decision_audit_*.json",
                        "aihr_human_review_provenance_reconfirmation_decision_audit_*.json",
                    ),
                )
            ),
        }
        if operator_packet_integrity_audit_path is not None:
            provenance_source_paths["operator_packet_integrity_audit"] = str(
                operator_packet_integrity_audit_path
            )
        source_paths.update(provenance_source_paths)
    triage_source_paths = {}
    if isinstance(review_triage.get("summary"), dict):
        raw_source_paths = review_triage["summary"].get("source_paths")
        if isinstance(raw_source_paths, dict):
            triage_source_paths = raw_source_paths
    release_queue = (
        release_readiness.get("agent_work_queue")
        if isinstance(release_readiness.get("agent_work_queue"), dict)
        else {}
    )
    queue_supporting_report_inputs = _queue_supporting_report_inputs(release_queue)
    triage_source_paths = _preferred_triage_source_paths(
        triage_source_paths,
        queue_supporting_report_inputs,
    )
    review_triage_summary = _summary_with_source_paths(
        review_triage.get("summary", {}),
        triage_source_paths,
    )
    definition_packet_safe = (
        not isinstance(ksa_definition_operator_packet, dict)
        or ksa_definition_operator_packet.get("exists") is False
        or bool(ksa_definition_operator_packet.get("safety_ok"))
    )
    report = {
        "schema": "aihr_human_review_backlog_v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "review_status_policy": _review_status_policy(),
        "source_paths": source_paths,
        "source_artifact_hashes": _source_artifact_snapshots(
            {
                **source_paths,
                **_nested_source_artifact_paths(
                    "queue_supporting_report_inputs",
                    queue_supporting_report_inputs.get("inputs", {}),
                ),
            }
        ),
        "queue_supporting_report_inputs": queue_supporting_report_inputs,
        "review_artifact_date_alignment": _artifact_date_alignment(
            {
                "source_paths": source_paths,
                "triage_source_paths": triage_source_paths,
                "queue_supporting_report_inputs": queue_supporting_report_inputs.get("inputs", {}),
            },
            active_date_source=release_readiness_path,
        ),
        "blockers": blockers,
        "seedpack_safety": {
            "all_seedpacks_safe": all(audit.get("safety_ok") for audit in seedpack_audit_values)
            and definition_packet_safe,
            "total_review_items": sum(int(audit.get("review_item_count") or 0) for audit in seedpack_audit_values),
            "total_nonblank_decision_items": sum(
                int(audit.get("nonblank_decision_item_count") or 0) for audit in seedpack_audit_values
            ),
            "total_trusted_status_proposals": sum(
                int(audit.get("trusted_status_proposal_count") or 0) for audit in seedpack_audit_values
            ),
            "total_status_update_allowed_violations": sum(
                int(audit.get("status_update_allowed_violations") or 0) for audit in seedpack_audit_values
            ),
            "total_missing_status_update_allowed": sum(
                int(audit.get("missing_status_update_allowed_count") or 0) for audit in seedpack_audit_values
            ),
            "total_db_writes_violations": sum(
                int(audit.get("db_writes_violations") or 0) for audit in seedpack_audit_values
            ),
            "total_missing_db_writes": sum(
                int(audit.get("missing_db_writes_count") or 0) for audit in seedpack_audit_values
            ),
            "total_approval_claim_violations": sum(
                int(audit.get("approval_claim_violations") or 0) for audit in seedpack_audit_values
            ),
            "total_missing_approval_claim": sum(
                int(audit.get("missing_approval_claim_count") or 0) for audit in seedpack_audit_values
            ),
            "total_forbidden_true_field_violations": sum(
                int(audit.get("forbidden_true_field_violation_count") or 0)
                for audit in seedpack_audit_values
            ),
            "total_seedpack_structure_issues": sum(
                int(audit.get("structure_issue_count") or 0)
                for audit in seedpack_audit_values
            ),
            "audits": seedpack_audits,
            "ksa_definition_review_operator_packet": ksa_definition_operator_packet,
        },
        "triage_summary": review_triage_summary,
        "top_items": [
            {
                "priority_score": item.get("priority_score"),
                "issue_type": item.get("issue_type") or item.get("issue", {}).get("issue_type"),
                "target_type": item.get("target_type") or item.get("issue", {}).get("target_type"),
                "target_id": item.get("target_id") or item.get("issue", {}).get("target_id"),
                "priority_reason": item.get("priority_reason"),
                "context_excerpt": item.get("context_excerpt"),
                "suggested_action": _neutral_review_action_for_item(item),
            }
            for item in top_items[:20]
            if isinstance(item, dict)
        ],
        "focus_overlays": [
            {
                "code": overlay.get("code"),
                "label": overlay.get("label"),
                "major_code": overlay.get("major_code"),
                "item_count": overlay.get("item_count"),
                "reason": overlay.get("reason"),
            }
            for overlay in focus_overlays
            if isinstance(overlay, dict)
        ],
    }
    return _sanitize_public_artifact_paths(report)


def write_human_review_backlog_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# Human Review Backlog",
        "",
        "This report isolates the human-review portion of the release blockers.",
        "",
        "## Blockers",
        "",
    ]
    for blocker in report.get("blockers", []):
        if not isinstance(blocker, dict):
            continue
        name = str(blocker.get("name") or "")
        label = str(blocker.get("display_label") or blocker_display_label(name))
        lines.append(
            f"- {label}: value `{blocker.get('value')}` threshold `{blocker.get('threshold')}`"
        )
        if name:
            lines.append(f"  - Machine blocker: `{name}`")
        if blocker.get("safe_next_action"):
            lines.append(f"  - Safe next action: `{blocker.get('safe_next_action')}`")
        if blocker.get("open_first"):
            lines.append(f"  - Open first: `{blocker.get('open_first')}`")
        review_artifacts = blocker.get("review_artifacts")
        if isinstance(review_artifacts, list) and review_artifacts:
            artifact_text = ", ".join(f"`{value}`" for value in review_artifacts)
            lines.append(f"  - Review artifacts: {artifact_text}")
            source_paths = report.get("source_paths")
            if isinstance(source_paths, dict):
                for artifact_name in review_artifacts:
                    artifact_path = source_paths.get(str(artifact_name))
                    if artifact_path:
                        lines.append(f"    - `{artifact_name}`: `{artifact_path}`")
    _append_source_artifact_hashes(lines, report.get("source_artifact_hashes"))
    queue_inputs = report.get("queue_supporting_report_inputs")
    if isinstance(queue_inputs, dict) and queue_inputs.get("inputs"):
        lines.extend(["", "## Queue Report Inputs", ""])
        for key, values in queue_inputs.get("inputs", {}).items():
            if isinstance(values, list):
                rendered_values = ", ".join(f"`{value}`" for value in values)
            else:
                rendered_values = f"`{values}`"
            lines.append(f"- `{key}`: {rendered_values}")
    policy = report.get("review_status_policy")
    if isinstance(policy, dict):
        lines.extend(["", "## Review Status Policy", ""])
        lines.append(
            "- human_decision_required_for_status_update: "
            f"`{policy.get('human_decision_required_for_status_update')}`"
        )
        lines.append(f"- status_update_allowed: `{policy.get('status_update_allowed')}`")
        lines.append(f"- db_writes: `{policy.get('db_writes')}`")
        lines.append(f"- approval_claim: `{policy.get('approval_claim')}`")
        forbidden_statuses = policy.get("forbidden_automatic_statuses")
        if isinstance(forbidden_statuses, list):
            lines.append(
                "- forbidden_automatic_statuses: "
                f"`{', '.join(str(value) for value in forbidden_statuses)}`"
            )
    alignment = report.get("review_artifact_date_alignment")
    if isinstance(alignment, dict):
        lines.extend(
            [
                "",
                "## Review Artifact Date Alignment",
                "",
                f"- status: `{alignment.get('status')}`",
                f"- active_date: `{alignment.get('active_date')}`",
                f"- stale_keys: `{', '.join(alignment.get('stale_keys') or [])}`",
            ]
        )
    seedpack_safety = report.get("seedpack_safety")
    if isinstance(seedpack_safety, dict):
        lines.extend(["", "## Seedpack Safety Audit", ""])
        lines.append(f"- all_seedpacks_safe: `{seedpack_safety.get('all_seedpacks_safe')}`")
        lines.append(f"- total_review_items: `{seedpack_safety.get('total_review_items')}`")
        lines.append(
            f"- total_nonblank_decision_items: `{seedpack_safety.get('total_nonblank_decision_items')}`"
        )
        lines.append(
            f"- total_trusted_status_proposals: `{seedpack_safety.get('total_trusted_status_proposals')}`"
        )
        lines.append(
            "- total_status_update_allowed_violations: "
            f"`{seedpack_safety.get('total_status_update_allowed_violations')}`"
        )
        lines.append(
            "- total_missing_status_update_allowed: "
            f"`{seedpack_safety.get('total_missing_status_update_allowed')}`"
        )
        lines.append(
            "- total_db_writes_violations: "
            f"`{seedpack_safety.get('total_db_writes_violations')}`"
        )
        lines.append(
            "- total_missing_db_writes: "
            f"`{seedpack_safety.get('total_missing_db_writes')}`"
        )
        lines.append(
            "- total_approval_claim_violations: "
            f"`{seedpack_safety.get('total_approval_claim_violations')}`"
        )
        lines.append(
            "- total_missing_approval_claim: "
            f"`{seedpack_safety.get('total_missing_approval_claim')}`"
        )
        lines.append(
            "- total_forbidden_true_field_violations: "
            f"`{seedpack_safety.get('total_forbidden_true_field_violations')}`"
        )
        lines.append(
            "- total_seedpack_structure_issues: "
            f"`{seedpack_safety.get('total_seedpack_structure_issues')}`"
        )
        audits = seedpack_safety.get("audits")
        if isinstance(audits, dict):
            lines.append("")
            for name, audit in audits.items():
                if not isinstance(audit, dict):
                    continue
                lines.append(
                    f"- `{name}`: safe=`{audit.get('safety_ok')}` "
                    f"items=`{audit.get('review_item_count')}` "
                    f"nonblank_decisions=`{audit.get('nonblank_decision_item_count')}` "
                    f"trusted_status_proposals=`{audit.get('trusted_status_proposal_count')}` "
                    f"status_update_allowed_violations=`{audit.get('status_update_allowed_violations')}` "
                    f"missing_status_update_allowed=`{audit.get('missing_status_update_allowed_count')}` "
                    f"db_writes_violations=`{audit.get('db_writes_violations')}` "
                    f"missing_db_writes=`{audit.get('missing_db_writes_count')}` "
                    f"approval_claim_violations=`{audit.get('approval_claim_violations')}` "
                    f"missing_approval_claim=`{audit.get('missing_approval_claim_count')}` "
                    f"forbidden_true_fields=`{audit.get('forbidden_true_field_violation_count')}` "
                    f"structure_issues=`{audit.get('structure_issue_count')}`"
                )
                lines.append(f"  - path: `{audit.get('path')}`")
        definition_packet = seedpack_safety.get("ksa_definition_review_operator_packet")
        if isinstance(definition_packet, dict):
            lines.extend(["", "### KSA Definition Operator Packet", ""])
            lines.append(f"- path: `{definition_packet.get('path')}`")
            lines.append(f"- exists: `{definition_packet.get('exists')}`")
            lines.append(f"- safety_ok: `{definition_packet.get('safety_ok')}`")
            lines.append(f"- review_pack_row_count: `{definition_packet.get('review_pack_row_count')}`")
            lines.append(f"- decision_blank_count: `{definition_packet.get('decision_blank_count')}`")
            lines.append(f"- action_plan_action_count: `{definition_packet.get('action_plan_action_count')}`")
            lines.append(f"- status_update_allowed: `{definition_packet.get('status_update_allowed')}`")
            lines.append(f"- db_writes: `{definition_packet.get('db_writes')}`")
            lines.append(f"- approval_claim: `{definition_packet.get('approval_claim')}`")
            sidecar_safety = definition_packet.get("sidecar_safety")
            if isinstance(sidecar_safety, dict):
                lines.append(f"- sidecar_safety_ok: `{sidecar_safety.get('safety_ok')}`")
                lines.append(
                    "- sidecar_consistency_issues: "
                    f"`{', '.join(sidecar_safety.get('consistency_issues') or [])}`"
                )
                for sidecar_name in ("decision_audit", "action_plan"):
                    sidecar = sidecar_safety.get(sidecar_name)
                    if not isinstance(sidecar, dict):
                        continue
                    lines.append(
                        f"  - `{sidecar_name}`: exists=`{sidecar.get('exists')}` "
                        f"safe=`{sidecar.get('safety_ok')}` schema=`{sidecar.get('schema')}`"
                    )
    lines.extend(["", "## Focus Overlays", ""])
    for overlay in report.get("focus_overlays", []):
        if not isinstance(overlay, dict):
            continue
        lines.append(
            f"- `{overlay.get('code')}` `{overlay.get('label')}` major `{overlay.get('major_code')}` items `{overlay.get('item_count')}`"
        )
    lines.extend(["", "## Top Items", ""])
    for index, item in enumerate(report.get("top_items", []), start=1):
        if not isinstance(item, dict):
            continue
        lines.append(
            f"{index}. score `{item.get('priority_score')}` `{item.get('issue_type')}` target `{item.get('target_type')}:{item.get('target_id')}`"
        )
        if item.get("context_excerpt"):
            lines.append(f"   - context: `{item.get('context_excerpt')}`")
        if item.get("suggested_action"):
            lines.append(f"   - action: `{item.get('suggested_action')}`")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def build_goal_completion_audit_report_from_files(
    *,
    release_readiness_path: Path | None = None,
    remaining_blockers_path: Path | None = None,
    human_review_backlog_path: Path | None = None,
    qualification_hygiene_path: Path | None = None,
    qualification_coverage_plan_path: Path | None = None,
    ontology_seedpack_path: Path | None = None,
    ksa_definition_operator_packet_path: Path | None = None,
    ksa_immutability_audit_path: Path | None = None,
    operator_addendum_path: Path | None = None,
    operator_entrypoint_manifest_path: Path | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    release_readiness_path = release_readiness_path or _latest_report_path(
        "aihr_release_readiness_*_after_runbook_v2.json",
        "aihr_release_readiness_*.json",
        fallback=DEFAULT_RELEASE_READINESS_AFTER_RUNBOOK_PATH,
    )
    remaining_blockers_path = remaining_blockers_path or _latest_report_path(
        "aihr_remaining_blockers_*.json",
        "remaining_blockers_*.json",
        fallback=DEFAULT_REMAINING_BLOCKERS_PATH,
    )
    human_review_backlog_path = human_review_backlog_path or _latest_report_path(
        "aihr_human_review_backlog_*.json",
        "human_review_backlog_*.json",
        fallback=DEFAULT_HUMAN_REVIEW_BACKLOG_PATH,
    )
    qualification_hygiene_path = qualification_hygiene_path or _latest_report_path(
        "qualification_retry_hygiene_*.json",
        fallback=DEFAULT_QUALIFICATION_HYGIENE_PATH,
    )
    qualification_coverage_plan_path = (
        qualification_coverage_plan_path
        or _latest_report_path(
            "qualification_collection_coverage_plan_*.json",
            fallback=DEFAULT_QUALIFICATION_COVERAGE_PLAN_PATH,
        )
    )
    ontology_seedpack_path = ontology_seedpack_path or _latest_report_path(
        "aihr_ontology_definition_review_seedpack_*.jsonl",
        fallback=DEFAULT_ONT_DEF_SEEDPACK_PATH,
    )
    ksa_definition_operator_packet_path = (
        ksa_definition_operator_packet_path
        or latest_ksa_definition_operator_packet_path(
            fallback=DEFAULT_KSA_DEFINITION_OPERATOR_PACKET_PATH
        )
    )
    ksa_immutability_audit_path = ksa_immutability_audit_path or _release_family_artifact_path(
        release_readiness_path,
        prefix="ksa_immutability_audit_",
        suffix=".json",
        fallback=release_readiness_path.parent / DEFAULT_KSA_IMMUTABILITY_AUDIT_PATH.name,
    )
    release_readiness = _read_json(release_readiness_path)
    remaining_blockers = _read_json(remaining_blockers_path)
    human_review_backlog = _read_json(human_review_backlog_path)
    qualification_hygiene = _read_json(qualification_hygiene_path)
    qualification_coverage_plan = _qualification_coverage_plan_summary(
        qualification_coverage_plan_path
    )
    ksa_immutability_audit = _ksa_immutability_audit_summary(ksa_immutability_audit_path)
    ontology_seedpack = _read_jsonl(ontology_seedpack_path)
    ksa_definition_operator_packet = _ksa_definition_operator_packet_summary(
        ksa_definition_operator_packet_path
    )
    operator_addendum = _read_optional_json(operator_addendum_path) or {}
    operator_entrypoint_manifest = _read_optional_json(operator_entrypoint_manifest_path) or {}
    ontology_seedpack_item_count = next(
        (
            int(item["item_count"])
            for item in ontology_seedpack
            if isinstance(item, dict)
            and item.get("record_type") == "batch"
            and item.get("item_count") is not None
        ),
        sum(
            1
            for item in ontology_seedpack
            if isinstance(item, dict) and item.get("record_type") == "review_item"
        ),
    )

    def blocker_open(name: str) -> bool:
        return any(
            isinstance(blocker, dict) and blocker.get("name") == name
            for blocker in remaining_blockers.get("remaining_blockers", [])
        )

    def release_blocker_open(name: str) -> bool:
        return any(
            isinstance(blocker, dict) and blocker.get("name") == name
            for blocker in release_readiness.get("blockers", [])
        )

    def any_blocker_open(name: str) -> bool:
        return blocker_open(name) or release_blocker_open(name)

    supporting_reports = remaining_blockers.get("latest_supporting_reports")
    if not isinstance(supporting_reports, dict):
        supporting_reports = {}
    review_artifact_freshness = _review_artifact_freshness_summary(
        remaining_blockers=remaining_blockers,
        human_review_backlog=human_review_backlog,
    )
    definition_packet_artifact_paths = []
    if isinstance(ksa_definition_operator_packet, dict):
        definition_packet_artifact_paths.append(str(ksa_definition_operator_packet_path))
        artifacts = ksa_definition_operator_packet.get("artifacts")
        if isinstance(artifacts, dict):
            for key in (
                "priority_review_pack",
                "priority_review_csv",
                "decision_audit",
                "action_plan",
            ):
                value = artifacts.get(key)
                if isinstance(value, str) and value:
                    definition_packet_artifact_paths.append(value)
    definition_packet_safe = (
        not isinstance(ksa_definition_operator_packet, dict)
        or bool(ksa_definition_operator_packet.get("safety_ok"))
    )
    ksa_immutability_audit_safe = (
        ksa_immutability_audit.get("exists") is True
        and ksa_immutability_audit.get("contract_ok") is True
    )
    definition_packet_safety_note = (
        " Definition operator packet safety check failed; keep ontology definition review open."
        if not definition_packet_safe
        else ""
    )
    ksa_immutability_note = (
        " KSA immutability audit confirms source hash and no trusted boilerplate/draft definition promotion."
        if ksa_immutability_audit.get("contract_ok") is True
        else (
            " KSA immutability audit is missing or not contract-clean; keep source immutability as residual risk."
            if ksa_immutability_audit.get("exists")
            else " KSA immutability audit is missing; missing proof artifacts remain blockers."
        )
    )

    demo_contract_ok = bool(
        isinstance(release_readiness.get("demo_contract"), dict)
        and release_readiness["demo_contract"].get("ok")
    )
    dashboard_surface_contract_ok = bool(
        isinstance(release_readiness.get("dashboard_surface_contract"), dict)
        and release_readiness["dashboard_surface_contract"].get("ok")
    )
    query_route_contract_verified = demo_contract_ok and dashboard_surface_contract_ok
    if query_route_contract_verified:
        query_route_contract_note = "Demo and dashboard route contracts are both present in release readiness."
    else:
        missing_route_contracts = []
        if not demo_contract_ok:
            missing_route_contracts.append("demo_contract.ok")
        if not dashboard_surface_contract_ok:
            missing_route_contracts.append("dashboard_surface_contract.ok")
        query_route_contract_note = (
            "Route evidence remains open until release readiness verifies "
            + ", ".join(missing_route_contracts)
            + "."
        )
    query_route_evidence = [str(release_readiness_path)]
    query_route_evidence.extend(_release_readiness_demo_evidence_paths(release_readiness))
    if len(query_route_evidence) == 1:
        query_route_evidence.extend(
            [
                str(
                    _latest_report_path(
                        "aihr_plan_demo_*.json",
                        fallback=Path("reports/aihr_plan_demo_20260624.json"),
                    )
                ),
                str(
                    _latest_report_path(
                        "aihr_dashboard_surface_verification_*.json",
                        fallback=Path(
                            "reports/aihr_dashboard_surface_verification_20260624_autoresolve.json"
                        ),
                    )
                ),
            ]
        )
    qualification_evidence = [
        str(qualification_hygiene_path),
        str(qualification_coverage_plan_path),
        qualification_hygiene.get("checkpoint_path"),
    ]
    if not qualification_hygiene.get("checkpoint_path"):
        qualification_evidence.append(
            str(
                _latest_report_path(
                    "checkpoint_ncs006_element_api_status_*_persistent_watchdog.json",
                    fallback=Path(
                        "reports/checkpoint_ncs006_element_api_status_persistent_watchdog.json"
                    ),
                )
            )
        )
    provenance_blocker_name = "human_review:provenance_reconfirmation_required"

    def optional_artifact_exists(path: Path | None) -> bool:
        return bool(path and path.exists() and path.is_file() and path.stat().st_size > 0)

    operator_addendum_summary = (
        operator_addendum.get("summary") if isinstance(operator_addendum.get("summary"), dict) else {}
    )
    operator_entrypoint_summary = (
        operator_entrypoint_manifest.get("summary")
        if isinstance(operator_entrypoint_manifest.get("summary"), dict)
        else {}
    )
    operator_support: dict[str, Any] = {}
    if operator_addendum_path or operator_entrypoint_manifest_path:
        support_issues: list[dict[str, Any]] = []
        operator_support = {
            "support_only": True,
            "approval_or_review_status_claim": False,
            "support_ok": True,
            "issues": support_issues,
        }
        if operator_addendum_path:
            addendum_contract = _safe_support_contract(operator_addendum)
            addendum_unsafe_flag_violations = _support_payload_unsafe_flag_violations(
                operator_addendum
            )
            addendum_cycle_refs = _operator_addendum_cycle_refs(operator_addendum)
            addendum_exists = optional_artifact_exists(operator_addendum_path)
            addendum_support_ok = (
                addendum_exists and all(addendum_contract.values()) and not addendum_cycle_refs
            )
            if not addendum_exists:
                support_issues.append(
                    {
                        "code": "operator_addendum_missing",
                        "message": "Optional operator addendum support artifact is missing or empty.",
                        "path": str(operator_addendum_path),
                    }
                )
            if not all(addendum_contract.values()):
                support_issues.append(
                    {
                        "code": "operator_addendum_unsafe_contract",
                        "message": "Operator addendum support artifact does not preserve report-only safety flags.",
                        "contract": addendum_contract,
                        "unsafe_flag_violations": addendum_unsafe_flag_violations,
                    }
                )
            if addendum_cycle_refs:
                support_issues.append(
                    {
                        "code": "operator_addendum_cycle_source",
                        "message": "Operator addendum depends on goal/terminal artifacts and must not be fed back into goal audit lineage.",
                        "cycle_refs": addendum_cycle_refs,
                    }
                )
            operator_support["operator_addendum"] = {
                "path": str(operator_addendum_path) if operator_addendum_path else None,
                "exists_nonempty": addendum_exists,
                "schema": operator_addendum.get("schema"),
                "ok": operator_addendum.get("ok"),
                "status": operator_addendum.get("status"),
                "report_only": operator_addendum.get("report_only"),
                "status_update_allowed": operator_addendum.get("status_update_allowed"),
                "db_writes": operator_addendum.get("db_writes"),
                "api_calls": operator_addendum.get("api_calls"),
                "approval_claim": operator_addendum.get("approval_claim"),
                "acceptance_claim": operator_addendum.get("acceptance_claim"),
                "human_decision_required": operator_addendum.get("human_decision_required"),
                "remaining_blocker_count": operator_addendum_summary.get(
                    "remaining_blocker_count"
                ),
                "covered_remaining_blocker_count": operator_addendum_summary.get(
                    "covered_remaining_blocker_count"
                ),
                "issue_count": operator_addendum_summary.get("issue_count"),
                "warning_count": operator_addendum_summary.get("warning_count"),
                "workbench_summary_row_count_matches_selected": operator_addendum_summary.get(
                    "workbench_summary_row_count_matches_selected"
                ),
                "workbench_selected_row_count": operator_addendum_summary.get(
                    "workbench_selected_row_count"
                ),
                "workbench_source_total_row_count": operator_addendum_summary.get(
                    "workbench_source_total_row_count"
                ),
                "workbench_unselected_source_row_count": operator_addendum_summary.get(
                    "workbench_unselected_source_row_count"
                ),
                "safety_contract": addendum_contract,
                "unsafe_flag_violations": addendum_unsafe_flag_violations,
                "cycle_refs": addendum_cycle_refs,
                "support_ok": addendum_support_ok,
            }
        if operator_entrypoint_manifest_path:
            entrypoint_contract = _safe_support_contract(operator_entrypoint_manifest)
            entrypoint_unsafe_flag_violations = _support_payload_unsafe_flag_violations(
                operator_entrypoint_manifest
            )
            entrypoint_terminal_contract = _operator_entrypoint_terminal_contract(
                operator_entrypoint_manifest
            )
            entrypoint_cycle_refs = _operator_entrypoint_cycle_refs(operator_entrypoint_manifest)
            entrypoint_exists = optional_artifact_exists(operator_entrypoint_manifest_path)
            entrypoint_support_ok = (
                entrypoint_exists
                and all(entrypoint_contract.values())
                and all(entrypoint_terminal_contract.values())
                and not entrypoint_cycle_refs
            )
            if not entrypoint_exists:
                support_issues.append(
                    {
                        "code": "operator_entrypoint_manifest_missing",
                        "message": "Optional operator entrypoint manifest support artifact is missing or empty.",
                        "path": str(operator_entrypoint_manifest_path),
                    }
                )
            if not all(entrypoint_contract.values()):
                support_issues.append(
                    {
                        "code": "operator_entrypoint_manifest_unsafe_contract",
                        "message": "Operator entrypoint manifest does not preserve report-only safety flags.",
                        "contract": entrypoint_contract,
                        "unsafe_flag_violations": entrypoint_unsafe_flag_violations,
                    }
                )
            if not all(entrypoint_terminal_contract.values()):
                support_issues.append(
                    {
                        "code": "operator_entrypoint_manifest_terminal_cycle_contract",
                        "message": "Operator entrypoint manifest must remain terminal evidence and stay outside refresh/handoff cycles.",
                        "contract": entrypoint_terminal_contract,
                    }
                )
            if entrypoint_cycle_refs:
                support_issues.append(
                    {
                        "code": "operator_entrypoint_manifest_cycle_source",
                        "message": "Operator entrypoint manifest depends on goal/terminal artifacts and must not be fed back into goal audit lineage.",
                        "cycle_refs": entrypoint_cycle_refs,
                    }
                )
            operator_support["operator_entrypoint_manifest"] = {
                "path": str(operator_entrypoint_manifest_path)
                if operator_entrypoint_manifest_path
                else None,
                "exists_nonempty": entrypoint_exists,
                "schema": operator_entrypoint_manifest.get("schema"),
                "ok": operator_entrypoint_manifest.get("ok"),
                "status": operator_entrypoint_manifest.get("status"),
                "terminal_evidence_only": operator_entrypoint_manifest.get(
                    "terminal_evidence_only"
                ),
                "include_in_release_refresh_dag": operator_entrypoint_manifest.get(
                    "include_in_release_refresh_dag"
                ),
                "report_only": operator_entrypoint_manifest.get("report_only"),
                "status_update_allowed": operator_entrypoint_manifest.get(
                    "status_update_allowed"
                ),
                "db_writes": operator_entrypoint_manifest.get("db_writes"),
                "api_calls": operator_entrypoint_manifest.get("api_calls"),
                "approval_claim": operator_entrypoint_manifest.get("approval_claim"),
                "acceptance_claim": operator_entrypoint_manifest.get("acceptance_claim"),
                "human_decision_required": operator_entrypoint_manifest.get(
                    "human_decision_required"
                ),
                "entry_count": operator_entrypoint_summary.get("entry_count"),
                "entry_ok_count": operator_entrypoint_summary.get("entry_ok_count"),
                "issue_count": operator_entrypoint_summary.get("issue_count"),
                "warning_count": operator_entrypoint_summary.get("warning_count"),
                "csv_decision_surface_count": operator_entrypoint_summary.get(
                    "csv_decision_surface_count"
                ),
                "guarded_api_timing_surface_count": operator_entrypoint_summary.get(
                    "guarded_api_timing_surface_count"
                ),
                "safety_contract": entrypoint_contract,
                "unsafe_flag_violations": entrypoint_unsafe_flag_violations,
                "terminal_cycle_contract": entrypoint_terminal_contract,
                "cycle_refs": entrypoint_cycle_refs,
                "support_ok": entrypoint_support_ok,
            }
        operator_support["support_ok"] = not support_issues
        operator_support["approval_or_review_status_claim"] = any(
            (
                isinstance(entry, dict)
                and (
                    entry.get("approval_claim") is True
                    or entry.get("acceptance_claim") is True
                    or entry.get("status_update_allowed") is True
                    or entry.get("db_writes") is True
                    or bool(entry.get("unsafe_flag_violations"))
                )
            )
            for entry in (
                operator_support.get("operator_addendum"),
                operator_support.get("operator_entrypoint_manifest"),
            )
        )

    def session_provenance_artifact_path(
        *,
        prefixes: tuple[str, ...],
        suffixes: tuple[str, ...],
        patterns: tuple[str, ...],
    ) -> Path:
        sibling_candidates = _session_artifact_candidates_from_release(
            release_readiness_path,
            prefixes=prefixes,
            suffixes=suffixes,
        )
        for sibling_path in sibling_candidates:
            if sibling_path.exists():
                return sibling_path
        fallback_path = (
            sibling_candidates[0]
            if sibling_candidates
            else release_readiness_path.parent / f"{prefixes[0].rstrip('_')}{suffixes[0]}"
        )
        if sibling_candidates:
            return fallback_path
        return _latest_report_path_near(
            release_readiness_path,
            *patterns,
            fallback=fallback_path,
        )

    provenance_evidence = [
        str(release_readiness_path),
        str(
            session_provenance_artifact_path(
                prefixes=(
                    "human_review_provenance_reconfirmation_packet_",
                    "aihr_human_review_provenance_reconfirmation_packet_",
                ),
                suffixes=(".json",),
                patterns=(
                    "human_review_provenance_reconfirmation_packet_*.json",
                    "aihr_human_review_provenance_reconfirmation_packet_*.json",
                ),
            )
        ),
        str(
            session_provenance_artifact_path(
                prefixes=(
                    "human_review_provenance_reconfirmation_decision_sheet_",
                    "aihr_human_review_provenance_reconfirmation_decision_sheet_",
                ),
                suffixes=(".csv", ".json"),
                patterns=(
                    "human_review_provenance_reconfirmation_decision_sheet_*.csv",
                    "human_review_provenance_reconfirmation_decision_sheet_*.json",
                    "aihr_human_review_provenance_reconfirmation_decision_sheet_*.csv",
                    "aihr_human_review_provenance_reconfirmation_decision_sheet_*.json",
                ),
            )
        ),
        str(
            session_provenance_artifact_path(
                prefixes=(
                    "human_review_provenance_reconfirmation_decision_audit_",
                    "aihr_human_review_provenance_reconfirmation_decision_audit_",
                ),
                suffixes=(".json",),
                patterns=(
                    "human_review_provenance_reconfirmation_decision_audit_*.json",
                    "aihr_human_review_provenance_reconfirmation_decision_audit_*.json",
                ),
            )
        ),
    ]

    requirements = [
        {
            "name": "query_route demo contract",
            "status": "verified" if query_route_contract_verified else "open",
            "evidence": query_route_evidence,
            "notes": query_route_contract_note,
        },
        {
            "name": "SQF legacy server.py split",
            "status": "verified",
            "evidence": [
                "src/ncs_mcp/server.py",
                "src/ncs_mcp/server_legacy_wrappers.py",
                "tests/test_server_legacy_wrappers.py",
            ],
            "notes": "Legacy SQF wrappers are split out and tested.",
        },
        {
            "name": "deployment strategy",
            "status": "verified"
            if release_readiness.get("checks", {}).get("deployment_runbook", {}).get("ok")
            and release_readiness.get("checks", {}).get("productization_strategy", {}).get("ok")
            else "open",
            "evidence": [
                "docs/AIHR_PRODUCTIZATION_STRATEGY.md",
                "docs/AIHR_DEPLOYMENT_RUNBOOK.md",
                "docs/NCS_MCP_PRD.md",
                "docs/MCP_RELEASE_CHECKLIST.md",
                str(release_readiness_path),
            ],
            "notes": "Runbook and productization docs are linked into release-readiness.",
        },
        {
            "name": "ontology definition review",
            "status": "open"
            if (
                blocker_open("review_debt:human_reviewed_concepts")
                or not definition_packet_safe
                or not ksa_immutability_audit_safe
            )
            else "verified",
            "evidence": [
                str(release_readiness_path),
                str(ontology_seedpack_path),
                str(ksa_immutability_audit_path),
                str(
                    supporting_reports.get("review_priority")
                    or _latest_report_path(
                        "aihr_review_priority_*.json",
                        fallback=Path("reports/aihr_review_priority_20260624.json"),
                    )
                ),
            ]
            + definition_packet_artifact_paths,
            "notes": _review_note_with_freshness(
                "Human-review concepts remain gated; ontology seedpack and KSA definition operator packet are export-only evidence.",
                review_artifact_freshness,
                keywords=(
                    "review_priority",
                    "ontology_definition_seedpack",
                    "ksa_immutability_audit",
                    "ksa_definition_review_operator_packet",
                    "priority_review_csv",
                    "priority_review_pack",
                ),
            )
            + definition_packet_safety_note
            + ksa_immutability_note,
            "source_immutability": ksa_immutability_audit,
        },
        {
            "name": "training-goal link review",
            "status": "open" if blocker_open("review_debt:human_reviewed_goal_links") else "verified",
            "evidence": [
                str(release_readiness_path),
                str(
                    supporting_reports.get("review_triage")
                    or _latest_report_path(
                        "aihr_review_triage_*.json",
                        fallback=Path("reports/aihr_review_triage_20260624.json"),
                    )
                ),
                str(
                    supporting_reports.get("transition_seedpack")
                    or _latest_report_path(
                        "aihr_transition_scenario_seedpack_*.jsonl",
                        fallback=Path("reports/aihr_transition_scenario_seedpack_20260624.jsonl"),
                    )
                ),
            ],
            "notes": _review_note_with_freshness(
                "Goal-link review remains human-gated.",
                review_artifact_freshness,
                keywords=("review_triage", "transition_seedpack", "quality_report", "review_priority_report"),
            ),
        },
        {
            "name": "task-KSA relation review",
            "status": "open" if blocker_open("review_debt:human_reviewed_task_relations") else "verified",
            "evidence": [
                str(release_readiness_path),
                str(
                    supporting_reports.get("review_seedpack")
                    or _latest_report_path(
                        "aihr_review_seedpack_*.jsonl",
                        fallback=Path("reports/aihr_review_seedpack_20260624.jsonl"),
                    )
                ),
            ],
            "notes": _review_note_with_freshness(
                "Task-relation review remains export-only until a human decision exists.",
                review_artifact_freshness,
                keywords=("review_seedpack", "blocker_ranked_seedpack"),
            ),
        },
        {
            "name": "provenance reconfirmation review",
            "status": "open" if any_blocker_open(provenance_blocker_name) else "verified",
            "evidence": provenance_evidence,
            "notes": (
                "Legacy trusted-status provenance remains human-gated; "
                "packet, blank decision sheet, and decision audit are export-only evidence."
            ),
        },
        {
            "name": "qualification coverage",
            "status": _qualification_guard_status(qualification_hygiene),
            "evidence": qualification_evidence,
            "notes": _qualification_guard_note(qualification_hygiene),
        },
    ]

    open_count = sum(1 for requirement in requirements if requirement["status"] != "verified")
    source_paths = {
        "release_readiness": str(release_readiness_path),
        "remaining_blockers": str(remaining_blockers_path),
        "human_review_backlog": str(human_review_backlog_path),
        "qualification_hygiene": str(qualification_hygiene_path),
        "qualification_coverage_plan": str(qualification_coverage_plan_path),
        "ontology_seedpack": str(ontology_seedpack_path),
        "ksa_definition_review_operator_packet": str(ksa_definition_operator_packet_path),
        "ksa_immutability_audit": str(ksa_immutability_audit_path),
    }
    if (
        operator_addendum_path
        and isinstance(operator_support.get("operator_addendum"), dict)
        and operator_support["operator_addendum"].get("support_ok") is True
    ):
        source_paths["operator_addendum"] = str(operator_addendum_path)
    if (
        operator_entrypoint_manifest_path
        and isinstance(operator_support.get("operator_entrypoint_manifest"), dict)
        and operator_support["operator_entrypoint_manifest"].get("support_ok") is True
    ):
        source_paths["operator_entrypoint_manifest"] = str(operator_entrypoint_manifest_path)
    report = {
        "schema": "aihr_goal_completion_audit_v1",
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "objective": objective
        or "Continue AI-HR/NCS release blocker work with evidence-based status and guarded automation.",
        "review_status_policy": _review_status_policy(),
        "release_ready": release_readiness.get("release_ready"),
        "review_artifact_freshness": review_artifact_freshness,
        "requirements": requirements,
        "open_requirement_count": open_count,
        "verified_requirement_count": len(requirements) - open_count,
        "source_paths": source_paths,
        "supporting_snapshots": {
            "remaining_blockers": remaining_blockers,
            "human_review_backlog": human_review_backlog,
            "qualification_hygiene": qualification_hygiene,
            "qualification_coverage_plan": qualification_coverage_plan,
            "ontology_seedpack_item_count": ontology_seedpack_item_count,
            "ksa_definition_review_operator_packet": ksa_definition_operator_packet,
            "ksa_immutability_audit": ksa_immutability_audit,
            "operator_support": operator_support,
        },
    }
    return _sanitize_public_artifact_paths(report)


def write_goal_completion_audit_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# Goal Completion Audit",
        "",
        "Requirement-by-requirement audit of the active AI-HR/NCS release blocker objective.",
        "",
        f"- objective: `{report.get('objective')}`",
        f"- verified_requirement_count: `{report.get('verified_requirement_count')}`",
        f"- open_requirement_count: `{report.get('open_requirement_count')}`",
        f"- release_ready: `{report.get('release_ready')}`",
        "",
    ]
    policy = report.get("review_status_policy")
    if isinstance(policy, dict):
        lines.extend(
            [
                "## Review Status Policy",
                "",
                "- human_decision_required_for_status_update: "
                f"`{policy.get('human_decision_required_for_status_update')}`",
                f"- status_update_allowed: `{policy.get('status_update_allowed')}`",
                f"- db_writes: `{policy.get('db_writes')}`",
                f"- approval_claim: `{policy.get('approval_claim')}`",
                "- forbidden_automatic_statuses: "
                f"`{', '.join(str(value) for value in policy.get('forbidden_automatic_statuses') or [])}`",
                "",
            ]
        )
    freshness = report.get("review_artifact_freshness")
    if isinstance(freshness, dict):
        lines.extend(
            [
                "## Review Artifact Freshness",
                "",
                f"- status: `{freshness.get('status')}`",
                f"- active_date: `{freshness.get('active_date')}`",
                f"- stale_keys: `{', '.join(freshness.get('stale_keys') or [])}`",
                "",
            ]
        )
    lines.extend(["## Requirements", ""])
    for index, requirement in enumerate(report.get("requirements", []), start=1):
        if not isinstance(requirement, dict):
            continue
        lines.append(f"{index}. `{requirement.get('name')}` - `{requirement.get('status')}`")
        for evidence in requirement.get("evidence", []):
            lines.append(f"   - evidence: `{evidence}`")
        notes = requirement.get("notes")
        if notes:
            lines.append(f"   - notes: {notes}")
        lines.append("")
    snapshots = report.get("supporting_snapshots")
    operator_support = (
        snapshots.get("operator_support")
        if isinstance(snapshots, dict) and isinstance(snapshots.get("operator_support"), dict)
        else {}
    )
    if operator_support:
        addendum = (
            operator_support.get("operator_addendum")
            if isinstance(operator_support.get("operator_addendum"), dict)
            else {}
        )
        entrypoint = (
            operator_support.get("operator_entrypoint_manifest")
            if isinstance(operator_support.get("operator_entrypoint_manifest"), dict)
            else {}
        )
        lines.extend(
            [
                "## Operator Support Evidence",
                "",
                "These artifacts are support evidence only; they do not approve, accept, or human-review records.",
                "",
            ]
        )
        if addendum:
            lines.extend(
                [
                "### Operator Addendum",
                "",
                f"- `path`: `{addendum.get('path')}`",
                f"- `exists_nonempty`: `{addendum.get('exists_nonempty')}`",
                f"- `ok`: `{addendum.get('ok')}`",
                f"- `status`: `{addendum.get('status')}`",
                f"- `report_only`: `{addendum.get('report_only')}`",
                f"- `status_update_allowed`: `{addendum.get('status_update_allowed')}`",
                f"- `db_writes`: `{addendum.get('db_writes')}`",
                f"- `approval_claim`: `{addendum.get('approval_claim')}`",
                f"- `acceptance_claim`: `{addendum.get('acceptance_claim')}`",
                f"- `human_decision_required`: `{addendum.get('human_decision_required')}`",
                f"- `remaining_blocker_count`: `{addendum.get('remaining_blocker_count')}`",
                "- `covered_remaining_blocker_count`: "
                f"`{addendum.get('covered_remaining_blocker_count')}`",
                f"- `issue_count`: `{addendum.get('issue_count')}`",
                f"- `warning_count`: `{addendum.get('warning_count')}`",
                "- `workbench_summary_row_count_matches_selected`: "
                f"`{addendum.get('workbench_summary_row_count_matches_selected')}`",
                f"- `workbench_selected_row_count`: `{addendum.get('workbench_selected_row_count')}`",
                "- `workbench_source_total_row_count`: "
                f"`{addendum.get('workbench_source_total_row_count')}`",
                "- `workbench_unselected_source_row_count`: "
                f"`{addendum.get('workbench_unselected_source_row_count')}`",
                "",
                ]
            )
        if entrypoint:
            lines.extend(
                [
                "### Operator Entrypoint Manifest",
                "",
                f"- `path`: `{entrypoint.get('path')}`",
                f"- `exists_nonempty`: `{entrypoint.get('exists_nonempty')}`",
                f"- `ok`: `{entrypoint.get('ok')}`",
                f"- `status`: `{entrypoint.get('status')}`",
                f"- `terminal_evidence_only`: `{entrypoint.get('terminal_evidence_only')}`",
                f"- `include_in_release_refresh_dag`: `{entrypoint.get('include_in_release_refresh_dag')}`",
                f"- `report_only`: `{entrypoint.get('report_only')}`",
                f"- `status_update_allowed`: `{entrypoint.get('status_update_allowed')}`",
                f"- `db_writes`: `{entrypoint.get('db_writes')}`",
                f"- `approval_claim`: `{entrypoint.get('approval_claim')}`",
                f"- `acceptance_claim`: `{entrypoint.get('acceptance_claim')}`",
                f"- `human_decision_required`: `{entrypoint.get('human_decision_required')}`",
                f"- `entry_count`: `{entrypoint.get('entry_count')}`",
                f"- `entry_ok_count`: `{entrypoint.get('entry_ok_count')}`",
                f"- `csv_decision_surface_count`: `{entrypoint.get('csv_decision_surface_count')}`",
                "- `guarded_api_timing_surface_count`: "
                f"`{entrypoint.get('guarded_api_timing_surface_count')}`",
                f"- `issue_count`: `{entrypoint.get('issue_count')}`",
                f"- `warning_count`: `{entrypoint.get('warning_count')}`",
                "",
                ]
            )
    coverage_plan = (
        snapshots.get("qualification_coverage_plan")
        if isinstance(snapshots, dict)
        and isinstance(snapshots.get("qualification_coverage_plan"), dict)
        else {}
    )
    if coverage_plan:
        lines.extend(
            [
                "## Qualification Coverage Plan Guard",
                "",
                f"- `path`: `{coverage_plan.get('path')}`",
                f"- `exists`: `{coverage_plan.get('exists')}`",
                f"- `guard_summary_ok`: `{coverage_plan.get('guard_summary_ok')}`",
                f"- `report_only`: `{coverage_plan.get('report_only')}`",
                f"- `status_update_allowed`: `{coverage_plan.get('status_update_allowed')}`",
                f"- `db_writes`: `{coverage_plan.get('db_writes')}`",
                f"- `api_calls`: `{coverage_plan.get('api_calls')}`",
                f"- `human_review_status_updates`: `{coverage_plan.get('human_review_status_updates')}`",
                f"- `approval_claim`: `{coverage_plan.get('approval_claim')}`",
                f"- `automatic_collection_allowed_now`: `{coverage_plan.get('automatic_collection_allowed_now')}`",
                f"- `operator_timed_guarded_api_commands_only`: `{coverage_plan.get('operator_timed_guarded_api_commands_only')}`",
                f"- `automatic_queue_execution_allowed`: `{coverage_plan.get('automatic_queue_execution_allowed')}`",
                f"- `attempted_unit_count`: `{coverage_plan.get('attempted_unit_count')}`",
                f"- `total_unit_count`: `{coverage_plan.get('total_unit_count')}`",
                f"- `collection_coverage`: `{coverage_plan.get('collection_coverage')}`",
                f"- `additional_attempted_units_needed`: `{coverage_plan.get('additional_attempted_units_needed')}`",
                f"- `estimated_batch_count`: `{coverage_plan.get('estimated_batch_count')}`",
                f"- `batch_count`: `{coverage_plan.get('batch_count')}`",
                f"- `raw_batch_count`: `{coverage_plan.get('raw_batch_count')}`",
                f"- `raw_batch_count_matches_batches`: `{coverage_plan.get('raw_batch_count_matches_batches')}`",
                f"- `unsafe_batch_count`: `{coverage_plan.get('unsafe_batch_count')}`",
                f"- `raw_unsafe_batch_count`: `{coverage_plan.get('raw_unsafe_batch_count')}`",
                f"- `raw_unsafe_batch_count_matches_batches`: `{coverage_plan.get('raw_unsafe_batch_count_matches_batches')}`",
                f"- `raw_unsafe_batches_count`: `{coverage_plan.get('raw_unsafe_batches_count')}`",
                f"- `raw_unsafe_batches_match_batches`: `{coverage_plan.get('raw_unsafe_batches_match_batches')}`",
                f"- `must_run_qualification_retry_hygiene_first`: `{coverage_plan.get('must_run_qualification_retry_hygiene_first')}`",
                f"- `must_use_ncs006_checkpoint_path`: `{coverage_plan.get('must_use_ncs006_checkpoint_path')}`",
                f"- `must_not_write_human_review_statuses`: `{coverage_plan.get('must_not_write_human_review_statuses')}`",
                f"- `operator_timing_required`: `{coverage_plan.get('operator_timing_required')}`",
                f"- `forbidden_status_updates_exact`: `{coverage_plan.get('forbidden_status_updates_exact')}`",
                "",
            ]
        )
    ksa_immutability = (
        snapshots.get("ksa_immutability_audit")
        if isinstance(snapshots, dict)
        and isinstance(snapshots.get("ksa_immutability_audit"), dict)
        else {}
    )
    if ksa_immutability:
        lines.extend(
            [
                "## KSA Immutability Audit",
                "",
                f"- `path`: `{ksa_immutability.get('path')}`",
                f"- `exists`: `{ksa_immutability.get('exists')}`",
                f"- `contract_ok`: `{ksa_immutability.get('contract_ok')}`",
                f"- `schema`: `{ksa_immutability.get('schema')}`",
                f"- `report_only`: `{ksa_immutability.get('report_only')}`",
                f"- `human_decision_required_for_status_update`: `{ksa_immutability.get('human_decision_required_for_status_update')}`",
                f"- `forbidden_automatic_statuses`: `{ksa_immutability.get('forbidden_automatic_statuses')}`",
                f"- `safety_contract_ok`: `{ksa_immutability.get('safety_contract_ok')}`",
                f"- `status_update_allowed`: `{ksa_immutability.get('status_update_allowed')}`",
                f"- `db_writes`: `{ksa_immutability.get('db_writes')}`",
                f"- `approval_claim`: `{ksa_immutability.get('approval_claim')}`",
                f"- `raw_source_mutation_allowed`: `{ksa_immutability.get('raw_source_mutation_allowed')}`",
                f"- `trusted_status_write_allowed`: `{ksa_immutability.get('trusted_status_write_allowed')}`",
                f"- `ksa_items_row_count`: `{ksa_immutability.get('ksa_items_row_count')}`",
                f"- `ksa_items_sha256`: `{ksa_immutability.get('ksa_items_sha256')}`",
                f"- `ksa_items_raw_text_multiset_sha256`: `{ksa_immutability.get('ksa_items_raw_text_multiset_sha256')}`",
                f"- `baseline_provided`: `{ksa_immutability.get('baseline_provided')}`",
                f"- `baseline_matches_current`: `{ksa_immutability.get('baseline_matches_current')}`",
                f"- `baseline_raw_text_multiset_matches_current`: `{ksa_immutability.get('baseline_raw_text_multiset_matches_current')}`",
                f"- `baseline_source_text_matches_current`: `{ksa_immutability.get('baseline_source_text_matches_current')}`",
                f"- `boilerplate_trusted_status_count`: `{ksa_immutability.get('boilerplate_trusted_status_count')}`",
                f"- `draft_or_template_trusted_status_count`: `{ksa_immutability.get('draft_or_template_trusted_status_count')}`",
                "",
            ]
        )
    lines.extend(["## Source Paths", ""])
    for key, value in report.get("source_paths", {}).items():
        lines.append(f"- `{key}`: `{value}`")
    out_path.write_text("\n".join(lines), encoding="utf-8")
