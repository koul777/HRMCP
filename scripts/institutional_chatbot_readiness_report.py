from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "institutional_chatbot_readiness_report_v1"
RELEASE_SCHEMA = "aihr_release_readiness_v1"
DEPLOYMENT_SCHEMA = "aihr_deployment_decision_v1"
BENCHMARK_SCHEMA = "ncs_chatbot_readiness_benchmark_v1"
PREVIEW_SCHEMA = "ncs_preview_release_evidence_summary_v1"
INTEGRATION_SCHEMA = "institutional_chatbot_integration_evidence_v1"
INSTITUTIONAL_CHAT_SMOKE_SCHEMA = "ncs_institutional_chat_smoke_v1"
SQLITE_FILE_MANIFEST_SCHEMA = "sqlite_database_file_manifest_v1"
SQLITE_SIDECAR_SUFFIXES = {"-wal", "-shm", "-journal"}
DEFAULT_MAX_EVIDENCE_AGE_HOURS = 72.0
FUTURE_CLOCK_SKEW_MINUTES = 5.0

REQUIRED_BENCHMARK_SCENARIOS = {
    "structure_search",
    "task_training",
    "training_transition",
    "education_system_design",
}

PUBLIC_REFERENCE_CHAT_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
    "plan_ncs_education_path",
    "ncs_search",
}

REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS = (
    "startup_ready",
    "read_only_startup",
    "operator_tools_disabled",
    "ready_endpoint",
    "chat_completed",
    "chat_route_public",
    "operator_route_blocked",
    "database_unchanged",
    "local_prompt_not_audit_logged",
    "gateway_startup_ready",
    "gateway_auth_mode",
    "gateway_file_backed_secrets",
    "bad_origin_rejected",
    "missing_secret_rejected",
    "gateway_chat_completed",
    "gateway_audit_events_present",
    "gateway_rejections_audited",
    "gateway_prompt_not_in_audit",
    "gateway_identity_not_in_audit",
    "gateway_secret_not_in_audit",
)

INTEGRATION_REQUIREMENTS = (
    {
        "id": "identity_access",
        "name": "Institution SSO and private access",
        "required": True,
        "responsibility": (
            "Enforce SSO, user/group authorization, session expiry, and private "
            "network exposure."
        ),
    },
    {
        "id": "private_mcp_hosting",
        "name": "Institution gateway, TLS, and private hosting",
        "required": True,
        "responsibility": (
            "Place the supplied reference chat API behind an institution gateway and "
            "TLS or reverse proxy with process supervision and readiness checks."
        ),
    },
    {
        "id": "read_only_data_volume",
        "name": "Read-only serving database",
        "required": True,
        "responsibility": (
            "Mount the prepared database read-only, separate serving from collection, "
            "and assign refresh ownership."
        ),
    },
    {
        "id": "audit_logging",
        "name": "Audit logging",
        "required": True,
        "responsibility": (
            "Record request identity, route fingerprint, tool, timing, outcome, and "
            "release version without secrets or unnecessary personal data."
        ),
    },
    {
        "id": "operator_separation",
        "name": "Operator-path separation",
        "required": True,
        "responsibility": (
            "Disable operator tools for chatbot users and keep collection, review, "
            "and guarded apply work outside chat requests."
        ),
    },
    {
        "id": "security_privacy",
        "name": "Security and privacy controls",
        "required": True,
        "responsibility": (
            "Define data classification, transcript retention, log redaction, "
            "vulnerability response, and institutional security review."
        ),
    },
    {
        "id": "backup_restore_rollback",
        "name": "Backup, restore, and rollback",
        "required": True,
        "responsibility": (
            "Assign RPO/RTO, encrypted backup, restore-test, DB/source compatibility, "
            "and rollback authority."
        ),
    },
    {
        "id": "capacity_incident_response",
        "name": "Capacity and incident response",
        "required": True,
        "responsibility": (
            "Validate target-host concurrency and overload behavior, and assign "
            "monitoring, shutdown, secret rotation, and incident owners."
        ),
    },
    {
        "id": "llm_gateway",
        "name": "Optional institution-approved LLM integration",
        "required": False,
        "responsibility": (
            "Optionally add an approved model contract and tool-calling layer with "
            "prompt, rate-limit, timeout, and cost controls. The reference chat UI/API "
            "does not require this integration to start."
        ),
    },
)


class EvidenceValidationError(ValueError):
    """Raised when an input file does not satisfy its declared evidence schema."""


def _read_json(path: Path, role: str) -> dict[str, Any]:
    if not path.exists():
        raise EvidenceValidationError(f"{role}: input file does not exist: {path}")
    if not path.is_file():
        raise EvidenceValidationError(f"{role}: input path is not a file: {path}")
    if path.stat().st_size <= 0:
        raise EvidenceValidationError(f"{role}: input file is empty: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise EvidenceValidationError(f"{role}: invalid JSON file: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EvidenceValidationError(f"{role}: top-level JSON value must be an object")
    return payload


def _nested(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(path)
        value = value[part]
    return value


def _type_label(expected: type[Any] | tuple[type[Any], ...]) -> str:
    types = expected if isinstance(expected, tuple) else (expected,)
    return " or ".join(item.__name__ for item in types)


def _matches_type(value: Any, expected: type[Any] | tuple[type[Any], ...]) -> bool:
    types = expected if isinstance(expected, tuple) else (expected,)
    for expected_type in types:
        if expected_type is bool and type(value) is bool:
            return True
        if expected_type is int and type(value) is int:
            return True
        if expected_type is float and type(value) is float:
            return True
        if expected_type not in {bool, int, float} and isinstance(value, expected_type):
            return True
    return False


def _require(
    payload: dict[str, Any],
    role: str,
    path: str,
    expected: type[Any] | tuple[type[Any], ...],
    *,
    non_empty: bool = False,
) -> Any:
    try:
        value = _nested(payload, path)
    except KeyError as exc:
        raise EvidenceValidationError(f"{role}: missing required key: {path}") from exc
    if not _matches_type(value, expected):
        raise EvidenceValidationError(
            f"{role}: {path} must be {_type_label(expected)}, got {type(value).__name__}"
        )
    if non_empty and not value:
        raise EvidenceValidationError(f"{role}: {path} must not be empty")
    return value


def _require_schema(payload: dict[str, Any], role: str, expected: str) -> None:
    actual = _require(payload, role, "schema", str, non_empty=True)
    if actual != expected:
        raise EvidenceValidationError(
            f"{role}: unsupported schema {actual!r}; expected {expected!r}"
        )


def _require_sha256(payload: dict[str, Any], role: str, path: str) -> None:
    value = _require(payload, role, path, str, non_empty=True)
    prefix, separator, digest = value.partition(":")
    if prefix != "sha256" or separator != ":" or len(digest) != 64:
        raise EvidenceValidationError(f"{role}: {path} must be a sha256:<64 hex> value")
    try:
        int(digest, 16)
    except ValueError as exc:
        raise EvidenceValidationError(f"{role}: {path} contains non-hex characters") from exc


def _require_raw_sha256(payload: dict[str, Any], role: str, path: str) -> None:
    value = _require(payload, role, path, str, non_empty=True)
    if len(value) != 64:
        raise EvidenceValidationError(f"{role}: {path} must contain 64 hex characters")
    try:
        int(value, 16)
    except ValueError as exc:
        raise EvidenceValidationError(f"{role}: {path} contains non-hex characters") from exc


def _require_string_list(
    payload: dict[str, Any],
    role: str,
    path: str,
    *,
    non_empty: bool = False,
) -> list[str]:
    values = _require(payload, role, path, list, non_empty=non_empty)
    if any(not isinstance(item, str) or not item for item in values):
        raise EvidenceValidationError(f"{role}: {path} must contain non-empty strings")
    return values


def _parse_timestamp(value: Any, role: str, path: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise EvidenceValidationError(f"{role}: {path} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvidenceValidationError(f"{role}: {path} is not a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise EvidenceValidationError(f"{role}: {path} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _validate_common_flags(payload: dict[str, Any], role: str) -> None:
    for path in ("status_update_allowed", "approval_claim"):
        _require(payload, role, path, bool)
    _require(
        payload,
        role,
        "db_writes",
        (bool, type(None)) if role == "chatbot_benchmark" else bool,
    )


def _validate_release(payload: dict[str, Any]) -> None:
    role = "release_readiness"
    _require_schema(payload, role, RELEASE_SCHEMA)
    _validate_common_flags(payload, role)
    for path in (
        "ok",
        "report_only",
        "release_ready",
        "engineering_hygiene_ok",
        "release_decision.release_ready",
        "release_decision.approval_claim",
        "release_decision.human_decision_required_for_release_claim",
        "artifact_date_contract.release_outputs.ok",
        "artifact_date_contract.proof_artifacts.ok",
        "artifact_lineage_contract.ok",
        "dashboard_surface_contract.ok",
    ):
        _require(payload, role, path, bool)
    _require(payload, role, "release_decision.status", str, non_empty=True)
    _require_string_list(payload, role, "release_decision.blocked_by")
    release_date = _require(
        payload,
        role,
        "artifact_date_contract.release_outputs.expected_date",
        str,
        non_empty=True,
    )
    proof_date = _require(
        payload,
        role,
        "artifact_date_contract.proof_artifacts.expected_date",
        str,
        non_empty=True,
    )
    for path, value in (
        ("artifact_date_contract.release_outputs.expected_date", release_date),
        ("artifact_date_contract.proof_artifacts.expected_date", proof_date),
    ):
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError as exc:
            raise EvidenceValidationError(f"{role}: {path} must use YYYYMMDD") from exc
    if release_date != proof_date:
        raise EvidenceValidationError(
            f"{role}: release output date {release_date!r} does not match proof date {proof_date!r}"
        )
    _require(payload, role, "blocker_count", int)
    blockers = _require(payload, role, "blockers", list)
    for index, blocker in enumerate(blockers):
        if not isinstance(blocker, dict):
            raise EvidenceValidationError(f"{role}: blockers[{index}] must be an object")
        for key in ("category", "name", "message"):
            _require(blocker, role, key, str, non_empty=True)
    _require_sha256(payload, role, "cycle_safe_content_sha256")


def _validate_deployment(payload: dict[str, Any]) -> None:
    role = "deployment_decision"
    _require_schema(payload, role, DEPLOYMENT_SCHEMA)
    _validate_common_flags(payload, role)
    _parse_timestamp(
        _require(payload, role, "generated_at", str, non_empty=True),
        role,
        "generated_at",
    )
    for path in (
        "report_only",
        "deployment_execution_authorized",
        "human_signoff_required",
        "private_preview_is_not_human_signoff",
        "private_preview_deployable_now",
        "private_preview_contract_satisfied",
        "stable_release_ready",
        "source_preview.source_package_ok",
        "source_preview.source_preview_export_ok",
        "source_preview.output_dir_exists",
        "source_preview.output_dir_is_dir",
        "source_preview.tree_verification_ok",
        "source_preview.tree_hash_consistency_ok",
        "source_preview.required_artifacts_present",
        "source_preview.source_metadata_ok",
        "source_preview.same_tree_ok",
        "source_preview.freshness_ok",
        "source_preview.supporting_evidence_freshness_ok",
        "source_preview.supporting_evidence_freshness.ok",
        "product_evidence.preview_allowed_by_product_evidence",
        "product_evidence.preview_evidence_complete",
        "product_evidence.preview_is_not_approval",
        "product_evidence.dashboard_ok",
        "product_evidence.static_artifacts_ok",
        "product_evidence.release_engineering_hygiene_ok",
        "human_review_guardrail.do_not_set_human_reviewed_accepted_reviewed_automatically",
    ):
        _require(payload, role, path, bool)
    for path in (
        "source_preview.source_preview_export_path",
        "source_preview.output_dir",
        "source_preview.source_preview_export_generated_at",
    ):
        _require(payload, role, path, str, non_empty=True)
    _parse_timestamp(
        _nested(payload, "source_preview.source_preview_export_generated_at"),
        role,
        "source_preview.source_preview_export_generated_at",
    )
    _require(payload, role, "source_preview.missing_artifacts", list)
    _require(payload, role, "source_preview.output_dir_mismatches", list)
    _require(payload, role, "source_preview.freshness_failures", list)
    _require_string_list(payload, role, "open_stable_blockers")
    _require_string_list(payload, role, "evidence_files", non_empty=True)


def _validate_benchmark(payload: dict[str, Any]) -> None:
    role = "chatbot_benchmark"
    _require_schema(payload, role, BENCHMARK_SCHEMA)
    _validate_common_flags(payload, role)
    _parse_timestamp(
        _require(payload, role, "generated_at", str, non_empty=True),
        role,
        "generated_at",
    )
    _require(payload, role, "ok", bool)
    _require(payload, role, "readiness_status", str, non_empty=True)
    _require(payload, role, "mutation_policy", str, non_empty=True)
    for path in (
        "external_api_calls",
        "network_access_required",
        "database.before.stable_during_hash",
        "database.after.stable_during_hash",
        "database.immutability.sha256_unchanged",
        "database.immutability.size_unchanged",
        "database.immutability.mtime_unchanged",
        "database.immutability.storage_content_unchanged",
        "database.immutability.all_unchanged",
        "database.filesystem_mutation_observed",
        "database.storage_content_unchanged",
        "read_only_preflight.ok",
        "read_only_preflight.configured_read_only_mode",
        "read_only_preflight.sqlite_query_only",
        "read_only_preflight.database_readiness.ready",
    ):
        _require(payload, role, path, bool)
    _require(payload, role, "human_status_changes_observed", (bool, type(None)))
    _require_sha256(payload, role, "database.before.sha256")
    _require_sha256(payload, role, "database.after.sha256")
    for path in (
        "summary.scenario_count",
        "summary.valid_scenario_count",
        "summary.total_measured_runs",
        "summary.valid_measured_runs",
        "summary.invalid_measured_runs",
        "summary.latency_ms.sample_count",
    ):
        _require(payload, role, path, int)
    _require(payload, role, "summary.result_validity_rate", (int, float))
    for path in ("summary.latency_ms.p50", "summary.latency_ms.p95", "summary.latency_ms.max"):
        _require(payload, role, path, (int, float, type(None)))
    scenarios = _require(payload, role, "scenarios", list, non_empty=True)
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise EvidenceValidationError(f"{role}: scenarios[{index}] must be an object")
        for path in (
            "id",
            "tool",
            "route.schema",
            "route.scenario",
            "route.tool",
            "route.route_fingerprint",
        ):
            _require(scenario, role, path, str, non_empty=True)
        for path in ("valid", "route.available"):
            _require(scenario, role, path, bool)
        _require(scenario, role, "route.missing_params", list)


def _validate_institutional_chat_smoke(payload: dict[str, Any]) -> None:
    role = "institutional_chat_smoke"
    _require_schema(payload, role, INSTITUTIONAL_CHAT_SMOKE_SCHEMA)
    _validate_common_flags(payload, role)
    _require(payload, role, "ok", bool)
    _require(payload, role, "report_only", bool)

    checks = _require(payload, role, "checks", dict)
    missing_checks = sorted(set(REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS) - set(checks))
    if missing_checks:
        raise EvidenceValidationError(
            f"{role}: checks missing required ids: " + ", ".join(missing_checks)
        )
    for check_id in REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS:
        _require(checks, role, check_id, bool)

    for prefix in ("startup", "gateway.startup"):
        for path in ("status", "url", "auth_mode"):
            _require(payload, role, f"{prefix}.{path}", str, non_empty=True)
        for path in ("read_only", "operator_tools_enabled", "audit_logging"):
            _require(payload, role, f"{prefix}.{path}", bool)

    for path in ("schema", "status", "release_version"):
        _require(payload, role, f"ready.{path}", str, non_empty=True)
    for path in (
        "ready",
        "read_only_mode",
        "operator_tools_enabled",
        "database_ready",
        "gateway_auth_required",
        "audit_logging_required",
    ):
        _require(payload, role, f"ready.{path}", bool)
    for path in ("public_tool_count", "max_http_workers"):
        _require(payload, role, f"ready.{path}", int)
    _require(payload, role, "ready.request_socket_timeout_seconds", (int, float))

    for path in ("state", "tool", "route_fingerprint"):
        _require(payload, role, f"chat.{path}", str, non_empty=True)
    for path in ("status", "course_count"):
        _require(payload, role, f"chat.{path}", int)
    for path in ("request_id", "release_version"):
        _require(payload, role, f"chat.audit.{path}", str, non_empty=True)
    _require(payload, role, "chat.audit.duration_ms", (int, float))
    for path in ("logged", "db_writes", "operator_tool_execution"):
        _require(payload, role, f"chat.audit.{path}", bool)

    _require(payload, role, "operator_block.status", int)
    _require(payload, role, "gateway.secret_source", str, non_empty=True)
    _require(payload, role, "operator_block.error_code", str, non_empty=True)
    for path in (
        "bad_origin_status",
        "missing_secret_status",
        "chat_status",
        "audit_event_count",
    ):
        _require(payload, role, f"gateway.{path}", int)
    for path in (
        "bad_origin_error",
        "missing_secret_error",
        "chat_state",
    ):
        _require(payload, role, f"gateway.{path}", str, non_empty=True)
    audit_error_codes = _require(payload, role, "gateway.audit_error_codes", list)
    if any(item is not None and (not isinstance(item, str) or not item) for item in audit_error_codes):
        raise EvidenceValidationError(
            f"{role}: gateway.audit_error_codes must contain strings or null"
        )
    _require(payload, role, "gateway.audit_identity_hash_present", bool)

    for prefix in ("database_before", "database_after"):
        _require(payload, role, f"{prefix}.size_bytes", int)
        _require(payload, role, f"{prefix}.mtime_ns", int)
        _require_raw_sha256(payload, role, f"{prefix}.sha256")


def _validate_preview(payload: dict[str, Any]) -> None:
    role = "source_preview_summary"
    _require_schema(payload, role, PREVIEW_SCHEMA)
    _validate_common_flags(payload, role)
    _parse_timestamp(
        _require(payload, role, "generated_at", str, non_empty=True),
        role,
        "generated_at",
    )
    for path in (
        "report_only",
        "ok",
        "contract_ok",
        "execution_authorized",
        "human_signoff_required",
        "preview_is_not_approval",
        "preview_allowed_by_product_evidence",
        "preview_evidence_complete",
        "supporting_evidence_freshness_ok",
        "supporting_evidence_freshness.ok",
        "stable_release_ready",
        "source_preview_export_ok",
        "source_package_ok",
        "release.release_ready",
        "release.engineering_hygiene_ok",
        "dashboard.ok",
        "dashboard.static_artifacts_ok",
        "dashboard.artifact_date_contract_ok",
        "dashboard.artifact_lineage_contract_ok",
        "dashboard.review_chain_safety.do_not_set_human_reviewed_accepted_reviewed_automatically",
        "source_preview_export.ok",
        "source_preview_export.output_dir_exists",
        "source_preview_export.output_dir_is_dir",
    ):
        _require(payload, role, path, bool)
    for path in (
        "release.path",
        "dashboard.path",
        "source_preview_export.path",
        "source_preview_export.generated_at",
        "source_preview_export.output_dir",
    ):
        _require(payload, role, path, str, non_empty=True)
    _parse_timestamp(
        _nested(payload, "source_preview_export.generated_at"),
        role,
        "source_preview_export.generated_at",
    )
    _require(payload, role, "release.blocker_count", int)
    _require_string_list(payload, role, "release.blocked_by")
    _require_string_list(payload, role, "preview_blockers")
    _require_string_list(payload, role, "preview_warnings")
    _require_string_list(payload, role, "supporting_evidence_freshness.missing_artifacts")
    _require(payload, role, "supporting_evidence_freshness.stale_artifacts", list)


def _validate_integration(payload: dict[str, Any]) -> None:
    role = "institution_integration"
    _require_schema(payload, role, INTEGRATION_SCHEMA)
    _validate_common_flags(payload, role)
    _parse_timestamp(
        _require(payload, role, "generated_at", str, non_empty=True),
        role,
        "generated_at",
    )
    _require(payload, role, "report_only", bool)
    controls = _require(payload, role, "controls", dict)
    known_ids = {item["id"] for item in INTEGRATION_REQUIREMENTS}
    required_ids = {
        item["id"] for item in INTEGRATION_REQUIREMENTS if item["required"]
    }
    missing_ids = sorted(required_ids - set(controls))
    if missing_ids:
        raise EvidenceValidationError(
            "institution_integration: controls missing required ids: " + ", ".join(missing_ids)
        )
    for control_id in sorted(known_ids & set(controls)):
        control = controls[control_id]
        if not isinstance(control, dict):
            raise EvidenceValidationError(
                f"institution_integration: controls.{control_id} must be an object"
            )
        _require(control, role, "implemented", bool)
        _require(control, role, "tested", bool)
        _require(control, role, "owner", str)
        refs = _require(control, role, "evidence_refs", list)
        if any(not isinstance(item, str) or not item for item in refs):
            raise EvidenceValidationError(
                f"institution_integration: controls.{control_id}.evidence_refs "
                "must contain non-empty strings"
            )


def validate_evidence(payloads: dict[str, dict[str, Any]]) -> None:
    validators = {
        "release_readiness": _validate_release,
        "deployment_decision": _validate_deployment,
        "chatbot_benchmark": _validate_benchmark,
        "institutional_chat_smoke": _validate_institutional_chat_smoke,
        "source_preview_summary": _validate_preview,
        "institution_integration": _validate_integration,
    }
    for role, payload in payloads.items():
        if role not in validators:
            raise EvidenceValidationError(f"unsupported evidence role: {role}")
        validators[role](payload)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(path)


def _path_candidates(value: str, owner: Path) -> set[Path]:
    path = Path(value)
    if path.is_absolute():
        return {path.resolve()}
    return {
        (Path.cwd() / path).resolve(),
        (owner.parent / path).resolve(),
        (owner.parent.parent / path).resolve(),
    }


def _embedded_path_matches(value: Any, target: Path, owner: Path) -> bool:
    return isinstance(value, str) and target.resolve() in _path_candidates(value, owner)


def _embedded_path_exists(value: Any, owner: Path) -> bool:
    if not isinstance(value, str) or not value:
        return False
    return any(candidate.exists() for candidate in _path_candidates(value, owner))


def _safe_input_contract(role: str, payload: dict[str, Any]) -> bool:
    common = (
        payload.get("status_update_allowed") is False
        and payload.get("db_writes") is False
        and payload.get("approval_claim") is False
    )
    if role == "chatbot_benchmark":
        return bool(
            common
            and payload.get("mutation_policy") == "report_only"
            and payload.get("external_api_calls") is False
            and payload.get("network_access_required") is False
            and payload.get("human_status_changes_observed") is False
        )
    return bool(common and payload.get("report_only") is True)


def _effective_evidence_time(
    role: str,
    payload: dict[str, Any],
    path: Path,
) -> tuple[datetime, str]:
    generated_at = payload.get("generated_at")
    if generated_at is not None:
        return _parse_timestamp(generated_at, role, "generated_at"), "generated_at"
    file_mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if role == "release_readiness":
        expected_date = payload["artifact_date_contract"]["release_outputs"]["expected_date"]
        artifact_date_end = datetime.strptime(expected_date, "%Y%m%d").replace(
            hour=23,
            minute=59,
            second=59,
            tzinfo=timezone.utc,
        )
        return min(file_mtime, artifact_date_end), "artifact_date_contract_or_file_mtime"
    return file_mtime, "file_mtime"


def _fingerprint(
    role: str,
    payload: dict[str, Any],
    path: Path,
    *,
    now: datetime,
    max_age_hours: float,
) -> dict[str, Any]:
    evidence_time, time_source = _effective_evidence_time(role, payload, path)
    age_hours = (now - evidence_time).total_seconds() / 3600.0
    future_hours = -age_hours if age_hours < 0 else 0.0
    fresh = age_hours <= max_age_hours and future_hours <= FUTURE_CLOCK_SKEW_MINUTES / 60.0
    stat = path.stat()
    return {
        "path": _display_path(path),
        "schema": payload.get("schema"),
        "sha256": _sha256_file(path),
        "size_bytes": stat.st_size,
        "mtime_utc": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        "evidence_time_utc": evidence_time.isoformat(),
        "evidence_time_source": time_source,
        "age_hours": round(age_hours, 6),
        "fresh": fresh,
    }


def _blocker_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _append_issue(
    issues: list[dict[str, Any]],
    code: str,
    message: str,
    roles: Iterable[str],
) -> None:
    issues.append({"code": code, "message": message, "evidence_roles": list(roles)})


def _lineage_issues(
    payloads: dict[str, dict[str, Any]], paths: dict[str, Path]
) -> list[dict[str, Any]]:
    release = payloads["release_readiness"]
    deployment = payloads["deployment_decision"]
    preview = payloads["source_preview_summary"]
    issues: list[dict[str, Any]] = []

    if not _embedded_path_matches(
        preview["release"]["path"], paths["release_readiness"], paths["source_preview_summary"]
    ):
        _append_issue(
            issues,
            "preview_release_path_mismatch",
            "The source-preview summary does not reference the supplied release-readiness file.",
            ("source_preview_summary", "release_readiness"),
        )

    evidence_files = deployment.get("evidence_files") or []
    if not any(
        _embedded_path_matches(value, paths["source_preview_summary"], paths["deployment_decision"])
        for value in evidence_files
    ):
        _append_issue(
            issues,
            "deployment_preview_summary_path_mismatch",
            "The deployment decision does not reference the supplied source-preview summary.",
            ("deployment_decision", "source_preview_summary"),
        )

    release_ready = release["release_ready"]
    stable_values = {
        "release_readiness": release_ready,
        "release_decision": release["release_decision"]["release_ready"],
        "deployment_decision": deployment["stable_release_ready"],
        "source_preview_summary": preview["stable_release_ready"],
        "source_preview_release": preview["release"]["release_ready"],
    }
    if len(set(stable_values.values())) != 1:
        _append_issue(
            issues,
            "stable_release_value_mismatch",
            "Stable-release flags disagree across the supplied evidence.",
            stable_values.keys(),
        )

    release_blockers = _blocker_names(release["release_decision"]["blocked_by"])
    if _blocker_names(preview["release"]["blocked_by"]) != release_blockers:
        _append_issue(
            issues,
            "preview_release_blockers_mismatch",
            "The source-preview summary carries a different stable blocker list.",
            ("source_preview_summary", "release_readiness"),
        )
    if _blocker_names(deployment["open_stable_blockers"]) != release_blockers:
        _append_issue(
            issues,
            "deployment_release_blockers_mismatch",
            "The deployment decision carries a different stable blocker list.",
            ("deployment_decision", "release_readiness"),
        )

    if preview["release"]["engineering_hygiene_ok"] is not release["engineering_hygiene_ok"]:
        _append_issue(
            issues,
            "preview_engineering_hygiene_mismatch",
            "The source-preview summary disagrees with release engineering hygiene.",
            ("source_preview_summary", "release_readiness"),
        )
    if (
        deployment["product_evidence"]["release_engineering_hygiene_ok"]
        is not release["engineering_hygiene_ok"]
    ):
        _append_issue(
            issues,
            "deployment_engineering_hygiene_mismatch",
            "The deployment decision disagrees with release engineering hygiene.",
            ("deployment_decision", "release_readiness"),
        )

    preview_flags = (
        "preview_allowed_by_product_evidence",
        "preview_evidence_complete",
    )
    for key in preview_flags:
        if deployment["product_evidence"][key] is not preview[key]:
            _append_issue(
                issues,
                f"deployment_{key}_mismatch",
                f"The deployment decision disagrees with source-preview {key}.",
                ("deployment_decision", "source_preview_summary"),
            )

    if (
        deployment["source_preview"]["supporting_evidence_freshness_ok"]
        is not preview["supporting_evidence_freshness_ok"]
    ):
        _append_issue(
            issues,
            "supporting_evidence_freshness_mismatch",
            "Deployment and source-preview freshness flags disagree.",
            ("deployment_decision", "source_preview_summary"),
        )

    deployment_export = deployment["source_preview"]["source_preview_export_path"]
    preview_export = preview["source_preview_export"]["path"]
    deployment_owner = paths["deployment_decision"]
    preview_owner = paths["source_preview_summary"]
    if not (
        _path_candidates(deployment_export, deployment_owner)
        & _path_candidates(preview_export, preview_owner)
    ):
        _append_issue(
            issues,
            "source_preview_export_path_mismatch",
            "Deployment and source-preview summary reference different export artifacts.",
            ("deployment_decision", "source_preview_summary"),
        )

    if (
        deployment["private_preview_contract_satisfied"]
        is not deployment["private_preview_deployable_now"]
    ):
        _append_issue(
            issues,
            "deployment_private_preview_contract_mismatch",
            "Deployment private-preview decision and contract result disagree.",
            ("deployment_decision",),
        )

    blocker_objects = release["blockers"]
    object_names = [item["name"] for item in blocker_objects]
    if release["blocker_count"] != len(blocker_objects) or object_names != release_blockers:
        _append_issue(
            issues,
            "release_blocker_count_or_names_mismatch",
            "Release blocker count or names disagree with the release decision.",
            ("release_readiness",),
        )

    referenced_paths = list(deployment.get("evidence_files") or [])
    referenced_paths.append(preview["source_preview_export"]["path"])
    missing_references = [
        value
        for value in referenced_paths
        if not _embedded_path_exists(value, deployment_owner)
        and not _embedded_path_exists(value, preview_owner)
    ]
    if missing_references:
        _append_issue(
            issues,
            "referenced_evidence_files_missing",
            "One or more evidence files referenced by the deployment bundle are missing.",
            ("deployment_decision", "source_preview_summary"),
        )

    output_dir = Path(deployment["source_preview"]["output_dir"])
    if not output_dir.exists() or not output_dir.is_dir():
        _append_issue(
            issues,
            "source_preview_output_directory_missing",
            "The source-preview output directory is not available as a directory.",
            ("deployment_decision",),
        )
    return issues


def _release_core_ready(release: dict[str, Any]) -> bool:
    return bool(
        release["ok"]
        and release["engineering_hygiene_ok"]
        and release["artifact_date_contract"]["release_outputs"]["ok"]
        and release["artifact_date_contract"]["proof_artifacts"]["ok"]
        and release["artifact_lineage_contract"]["ok"]
        and release["dashboard_surface_contract"]["ok"]
    )


def _benchmark_sidecar_evidence_ready(benchmark: dict[str, Any]) -> bool:
    database = benchmark.get("database")
    if not isinstance(database, dict):
        return False
    for phase in ("before", "after"):
        snapshot = database.get(phase)
        if not isinstance(snapshot, dict):
            return False
        if snapshot.get("manifest_schema") != SQLITE_FILE_MANIFEST_SCHEMA:
            return False
        sidecars = snapshot.get("sidecars")
        if not isinstance(sidecars, dict) or set(sidecars) != SQLITE_SIDECAR_SUFFIXES:
            return False
        for item in sidecars.values():
            if not isinstance(item, dict):
                return False
            exists = item.get("exists")
            if type(exists) is not bool or item.get("stable_during_hash") is not True:
                return False
            if exists:
                if not (
                    isinstance(item.get("sha256"), str)
                    and item["sha256"].startswith("sha256:")
                    and type(item.get("size_bytes")) is int
                    and type(item.get("mtime_ns")) is int
                ):
                    return False
            elif any(
                item.get(key) is not None
                for key in ("sha256", "size_bytes", "mtime_ns")
            ):
                return False
    immutability = database.get("immutability")
    return bool(
        isinstance(immutability, dict)
        and immutability.get("base_unchanged") is True
        and immutability.get("sidecars_unchanged") is True
        and immutability.get("changed_sidecars") == []
    )


def _benchmark_ready(benchmark: dict[str, Any]) -> bool:
    scenarios = benchmark["scenarios"]
    scenario_ids = {item["id"] for item in scenarios}
    valid_routes = all(
        item["valid"]
        and item["route"]["schema"] == "ncs_query_route_v1"
        and item["route"]["available"]
        and not item["route"]["missing_params"]
        and bool(item["route"]["route_fingerprint"])
        for item in scenarios
    )
    summary = benchmark["summary"]
    database = benchmark["database"]
    return bool(
        benchmark["ok"]
        and benchmark["readiness_status"] == "ready"
        and benchmark["read_only_preflight"]["ok"]
        and benchmark["read_only_preflight"]["configured_read_only_mode"]
        and benchmark["read_only_preflight"]["sqlite_query_only"]
        and benchmark["read_only_preflight"]["database_readiness"]["ready"]
        and _benchmark_sidecar_evidence_ready(benchmark)
        and database["filesystem_mutation_observed"] is False
        and database["storage_content_unchanged"] is True
        and database["immutability"]["storage_content_unchanged"] is True
        and database["immutability"]["all_unchanged"]
        and database["before"]["sha256"] == database["after"]["sha256"]
        and scenario_ids == REQUIRED_BENCHMARK_SCENARIOS
        and summary["scenario_count"] == len(scenarios)
        and summary["valid_scenario_count"] == len(scenarios)
        and summary["invalid_measured_runs"] == 0
        and summary["valid_measured_runs"] == summary["total_measured_runs"]
        and float(summary["result_validity_rate"]) == 1.0
        and valid_routes
    )


def _private_preview_evidence_ready(
    deployment: dict[str, Any], preview: dict[str, Any]
) -> bool:
    source = deployment["source_preview"]
    product = deployment["product_evidence"]
    return bool(
        deployment["private_preview_deployable_now"]
        and deployment["private_preview_contract_satisfied"]
        and deployment["private_preview_is_not_human_signoff"]
        and source["source_package_ok"]
        and source["source_preview_export_ok"]
        and source["output_dir_exists"]
        and source["output_dir_is_dir"]
        and source["tree_verification_ok"]
        and source["tree_hash_consistency_ok"]
        and source["required_artifacts_present"]
        and source["source_metadata_ok"]
        and source["same_tree_ok"]
        and source["freshness_ok"]
        and source["supporting_evidence_freshness_ok"]
        and source["supporting_evidence_freshness"]["ok"]
        and not source["missing_artifacts"]
        and not source["output_dir_mismatches"]
        and not source["freshness_failures"]
        and product["preview_allowed_by_product_evidence"]
        and product["preview_evidence_complete"]
        and product["preview_is_not_approval"]
        and product["dashboard_ok"]
        and product["static_artifacts_ok"]
        and product["release_engineering_hygiene_ok"]
        and preview["ok"]
        and preview["contract_ok"]
        and preview["preview_is_not_approval"]
        and preview["preview_allowed_by_product_evidence"]
        and preview["preview_evidence_complete"]
        and preview["supporting_evidence_freshness_ok"]
        and preview["supporting_evidence_freshness"]["ok"]
        and preview["source_preview_export_ok"]
        and preview["source_package_ok"]
        and preview["dashboard"]["ok"]
        and preview["dashboard"]["static_artifacts_ok"]
        and preview["dashboard"]["artifact_date_contract_ok"]
        and preview["dashboard"]["artifact_lineage_contract_ok"]
        and preview["source_preview_export"]["ok"]
        and preview["source_preview_export"]["output_dir_exists"]
        and preview["source_preview_export"]["output_dir_is_dir"]
        and not preview["preview_blockers"]
        and not preview["supporting_evidence_freshness"]["missing_artifacts"]
        and not preview["supporting_evidence_freshness"]["stale_artifacts"]
    )


def _institutional_chat_smoke_ready(smoke: dict[str, Any]) -> bool:
    checks = smoke.get("checks")
    if not isinstance(checks, dict) or not all(
        checks.get(check_id) is True
        for check_id in REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS
    ):
        return False

    startup = smoke.get("startup")
    ready = smoke.get("ready")
    chat = smoke.get("chat")
    operator_block = smoke.get("operator_block")
    gateway = smoke.get("gateway")
    database_before = smoke.get("database_before")
    database_after = smoke.get("database_after")
    if not all(
        isinstance(item, dict)
        for item in (
            startup,
            ready,
            chat,
            operator_block,
            gateway,
            database_before,
            database_after,
        )
    ):
        return False

    gateway_startup = gateway.get("startup")
    chat_audit = chat.get("audit")
    if not isinstance(gateway_startup, dict) or not isinstance(chat_audit, dict):
        return False

    audit_error_codes = gateway.get("audit_error_codes")
    gateway_rejections_match = bool(
        isinstance(audit_error_codes, list)
        and audit_error_codes[:2] == ["origin_not_allowed", "authentication_required"]
    )
    return bool(
        smoke.get("ok") is True
        and smoke.get("report_only") is True
        and smoke.get("status_update_allowed") is False
        and smoke.get("db_writes") is False
        and smoke.get("approval_claim") is False
        and startup.get("status") == "ready"
        and startup.get("auth_mode") == "local"
        and startup.get("read_only") is True
        and startup.get("operator_tools_enabled") is False
        and startup.get("audit_logging") is False
        and ready.get("schema") == "ncs_institutional_chat_health_v1"
        and ready.get("status") == "ready"
        and ready.get("ready") is True
        and ready.get("read_only_mode") is True
        and ready.get("operator_tools_enabled") is False
        and ready.get("database_ready") is True
        and type(ready.get("public_tool_count")) is int
        and ready["public_tool_count"] > 0
        and chat.get("status") == 200
        and chat.get("state") == "completed"
        and chat.get("tool") in PUBLIC_REFERENCE_CHAT_TOOLS
        and bool(chat.get("route_fingerprint"))
        and chat_audit.get("logged") is False
        and chat_audit.get("db_writes") is False
        and chat_audit.get("operator_tool_execution") is False
        and operator_block.get("status") == 403
        and operator_block.get("error_code") == "operator_route_blocked"
        and gateway_startup.get("status") == "ready"
        and gateway_startup.get("auth_mode") == "gateway"
        and gateway_startup.get("read_only") is True
        and gateway_startup.get("operator_tools_enabled") is False
        and gateway_startup.get("audit_logging") is True
        and gateway.get("secret_source") == "file"
        and gateway.get("bad_origin_status") == 403
        and gateway.get("bad_origin_error") == "origin_not_allowed"
        and gateway.get("missing_secret_status") == 401
        and gateway.get("missing_secret_error") == "authentication_required"
        and gateway.get("chat_status") == 200
        and gateway.get("chat_state") == "completed"
        and gateway.get("audit_event_count") == 3
        and gateway_rejections_match
        and gateway.get("audit_identity_hash_present") is True
        and database_before == database_after
    )


def _institutional_chat_smoke_summary(
    smoke: dict[str, Any] | None,
    *,
    fresh: bool,
    safe: bool,
    ready: bool,
) -> dict[str, Any]:
    checks = smoke.get("checks", {}) if smoke else {}
    required_results = {
        check_id: checks.get(check_id) is True
        for check_id in REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS
    }
    failed_checks = [
        check_id for check_id, passed in required_results.items() if not passed
    ]
    chat = smoke.get("chat", {}) if smoke else {}
    gateway = smoke.get("gateway", {}) if smoke else {}
    return {
        "provided": smoke is not None,
        "schema": smoke.get("schema") if smoke else None,
        "current": fresh,
        "safe_report_only_contract": safe,
        "passing": ready,
        "required_check_count": len(REQUIRED_INSTITUTIONAL_CHAT_SMOKE_CHECKS),
        "passed_check_count": sum(required_results.values()),
        "failed_required_checks": failed_checks,
        "required_checks": required_results,
        "public_chat": {
            "status": chat.get("status"),
            "state": chat.get("state"),
            "tool": chat.get("tool"),
            "route_fingerprint": chat.get("route_fingerprint"),
        },
        "operator_route_blocked": required_results["operator_route_blocked"],
        "database_unchanged": required_results["database_unchanged"],
        "gateway": {
            "auth_enforced": required_results["missing_secret_rejected"],
            "origin_enforced": required_results["bad_origin_rejected"],
            "rejections_audited": required_results["gateway_rejections_audited"],
            "audit_event_count": gateway.get("audit_event_count"),
            "audit_error_codes": gateway.get("audit_error_codes", []),
            "secret_source": gateway.get("secret_source"),
        },
        "audit_exclusions": {
            "prompt_excluded": required_results["gateway_prompt_not_in_audit"],
            "raw_identity_excluded": required_results["gateway_identity_not_in_audit"],
            "secret_excluded": required_results["gateway_secret_not_in_audit"],
        },
    }


def _integration_requirements(
    integration: dict[str, Any] | None,
    *,
    integration_fresh: bool,
) -> tuple[list[dict[str, Any]], bool]:
    controls = integration.get("controls", {}) if integration else {}
    results: list[dict[str, Any]] = []
    for requirement in INTEGRATION_REQUIREMENTS:
        control = controls.get(requirement["id"], {})
        implemented = control.get("implemented") is True
        tested = control.get("tested") is True
        owner = control.get("owner") if isinstance(control.get("owner"), str) else ""
        evidence_refs = (
            control.get("evidence_refs")
            if isinstance(control.get("evidence_refs"), list)
            else []
        )
        required = requirement["required"]
        ready = bool(
            integration
            and integration_fresh
            and implemented
            and tested
            and owner.strip()
            and evidence_refs
        )
        status = (
            "ready"
            if ready
            else "optional_not_configured"
            if not required and not (implemented or tested or owner.strip() or evidence_refs)
            else "evidence_not_provided"
            if integration is None
            else "stale_evidence"
            if not integration_fresh
            else "implementation_or_test_evidence_missing"
        )
        results.append(
            {
                **requirement,
                "required_for": (
                    ["private_pilot", "stable_internal_release"] if required else []
                ),
                "optional_for": [] if required else ["approved_llm_integration"],
                "status": status,
                "ready": ready,
                "implemented": implemented,
                "tested": tested,
                "owner": owner,
                "evidence_refs": evidence_refs,
            }
        )
    return results, all(item["ready"] for item in results if item["required"])


def build_report(
    *,
    payloads: dict[str, dict[str, Any]],
    paths: dict[str, Path],
    max_evidence_age_hours: float = DEFAULT_MAX_EVIDENCE_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    if max_evidence_age_hours <= 0:
        raise EvidenceValidationError("max_evidence_age_hours must be greater than zero")
    validate_evidence(payloads)
    expected_roles = {
        "release_readiness",
        "deployment_decision",
        "chatbot_benchmark",
        "source_preview_summary",
    }
    missing_roles = sorted(expected_roles - set(payloads))
    if missing_roles:
        raise EvidenceValidationError(
            "missing required evidence roles: " + ", ".join(missing_roles)
        )
    if set(payloads) != set(paths):
        raise EvidenceValidationError("payload and path roles must match exactly")

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise EvidenceValidationError("now must include a timezone")
    now = now.astimezone(timezone.utc)

    fingerprints = {
        role: _fingerprint(
            role,
            payload,
            paths[role],
            now=now,
            max_age_hours=max_evidence_age_hours,
        )
        for role, payload in payloads.items()
    }
    evidence_paths = {role: item["path"] for role, item in fingerprints.items()}
    safety_by_role = {
        role: _safe_input_contract(role, payload) for role, payload in payloads.items()
    }
    freshness_by_role = {role: item["fresh"] for role, item in fingerprints.items()}
    lineage_issues = _lineage_issues(payloads, paths)

    release = payloads["release_readiness"]
    deployment = payloads["deployment_decision"]
    benchmark = payloads["chatbot_benchmark"]
    preview = payloads["source_preview_summary"]
    institutional_chat_smoke = payloads.get("institutional_chat_smoke")
    integration = payloads.get("institution_integration")

    release_core_ready = _release_core_ready(release)
    benchmark_ready = _benchmark_ready(benchmark)
    core_roles = ("release_readiness", "chatbot_benchmark")
    core_backend_ready = bool(
        release_core_ready
        and benchmark_ready
        and all(safety_by_role[role] for role in core_roles)
        and all(freshness_by_role[role] for role in core_roles)
    )

    required_roles_fresh = all(freshness_by_role[role] for role in expected_roles)
    required_roles_safe = all(safety_by_role[role] for role in expected_roles)
    private_preview_evidence_ready = _private_preview_evidence_ready(deployment, preview)
    institutional_chat_smoke_fresh = bool(
        institutional_chat_smoke is not None
        and freshness_by_role.get("institutional_chat_smoke")
    )
    institutional_chat_smoke_safe = bool(
        institutional_chat_smoke is not None
        and safety_by_role.get("institutional_chat_smoke")
    )
    reference_chat_runtime_ready = bool(
        institutional_chat_smoke is not None
        and institutional_chat_smoke_fresh
        and institutional_chat_smoke_safe
        and _institutional_chat_smoke_ready(institutional_chat_smoke)
    )
    reference_chat_smoke_evidence = _institutional_chat_smoke_summary(
        institutional_chat_smoke,
        fresh=institutional_chat_smoke_fresh,
        safe=institutional_chat_smoke_safe,
        ready=reference_chat_runtime_ready,
    )
    private_pilot_backend_ready = bool(
        core_backend_ready
        and private_preview_evidence_ready
        and reference_chat_runtime_ready
        and required_roles_fresh
        and required_roles_safe
        and not lineage_issues
    )

    integration_fresh = bool(
        integration is not None
        and freshness_by_role.get("institution_integration")
        and safety_by_role.get("institution_integration")
    )
    integration_requirements, integration_ready = _integration_requirements(
        integration,
        integration_fresh=integration_fresh,
    )
    private_pilot_ready = bool(private_pilot_backend_ready and integration_ready)

    stable_blockers = _blocker_names(release["release_decision"]["blocked_by"])
    stable_release_ready = bool(
        private_pilot_ready
        and release["release_ready"]
        and release["release_decision"]["release_ready"]
        and deployment["stable_release_ready"]
        and preview["stable_release_ready"]
        and not stable_blockers
        and release["blocker_count"] == 0
    )

    blockers: list[dict[str, Any]] = []
    for role, safe in safety_by_role.items():
        if not safe:
            blockers.append(
                {
                    "code": f"unsafe_input_contract:{role}",
                    "scope": "all_readiness_levels",
                    "message": "Input evidence does not preserve the report-only safety contract.",
                    "evidence_roles": [role],
                }
            )
    for role, fresh in freshness_by_role.items():
        if not fresh:
            blockers.append(
                {
                    "code": f"stale_or_future_evidence:{role}",
                    "scope": "private_pilot",
                    "message": (
                        "Evidence is older than the configured limit or too far in the future; "
                        "it cannot support current readiness."
                    ),
                    "evidence_roles": [role],
                }
            )
    for issue in lineage_issues:
        blockers.append({**issue, "scope": "private_pilot"})
    if not release_core_ready:
        blockers.append(
            {
                "code": "release_engineering_contract_not_ready",
                "scope": "core_backend",
                "message": "Release engineering, dashboard, date, or lineage checks are not green.",
                "evidence_roles": ["release_readiness"],
            }
        )
    if not benchmark_ready:
        blockers.append(
            {
                "code": "chatbot_benchmark_not_ready",
                "scope": "core_backend",
                "message": (
                    "Representative read-only chatbot workflows or DB immutability "
                    "checks failed."
                ),
                "evidence_roles": ["chatbot_benchmark"],
            }
        )
    if not private_preview_evidence_ready:
        blockers.append(
            {
                "code": "private_preview_package_not_ready",
                "scope": "private_pilot",
                "message": (
                    "Deployment and source-preview evidence do not support a current "
                    "private preview."
                ),
                "evidence_roles": ["deployment_decision", "source_preview_summary"],
            }
        )
    if institutional_chat_smoke is None:
        blockers.append(
            {
                "code": "institutional_chat_smoke_not_provided",
                "scope": "private_pilot",
                "message": (
                    "Current ncs_institutional_chat_smoke_v1 evidence was not supplied; "
                    "the repository reference chat runtime is therefore unverified."
                ),
                "evidence_roles": [],
            }
        )
    elif not reference_chat_runtime_ready:
        blockers.append(
            {
                "code": "institutional_chat_smoke_not_ready",
                "scope": "private_pilot",
                "message": (
                    "Reference chat smoke evidence is not current, safe, and passing "
                    "for read-only startup, public/operator routing, DB immutability, "
                    "gateway enforcement, rejection audit, and audit-data exclusion."
                ),
                "evidence_roles": ["institutional_chat_smoke"],
            }
        )
    if integration is None:
        blockers.append(
            {
                "code": "institution_integration_evidence_not_provided",
                "scope": "private_pilot",
                "message": (
                    "Institution-owned SSO, gateway/TLS, privacy, audit, hosting, backup, "
                    "rollback, and operations controls remain unverified."
                ),
                "evidence_roles": [],
            }
        )
    elif not integration_ready:
        blockers.append(
            {
                "code": "institution_integration_requirements_incomplete",
                "scope": "private_pilot",
                "message": (
                    "One or more institution integration controls lack current "
                    "owner/test evidence."
                ),
                "evidence_roles": ["institution_integration"],
            }
        )
    for blocker in stable_blockers:
        blockers.append(
            {
                "code": f"stable_release_blocker:{blocker}",
                "scope": "stable_internal_release",
                "message": (
                    "The active release-readiness artifact retains this stable-release "
                    "blocker."
                ),
                "evidence_roles": ["release_readiness"],
            }
        )

    all_evidence_safe = all(safety_by_role.values())
    all_evidence_fresh = all(freshness_by_role.values())
    evidence_contract_ok = bool(all_evidence_safe and all_evidence_fresh and not lineage_issues)
    outsourcing_status = (
        "selective_service_integration"
        if core_backend_ready
        else "defer_procurement_scope_until_core_evidence_is_current"
    )
    outsourcing_assessment = {
        "status": outsourcing_status,
        "core_ncs_engine_replacement_needed": False if core_backend_ready else None,
        "recommended_in_house_scope": [
            "NCS ontology and task/KSA mapping",
            "training recommendation and evidence workflow",
            "reference institutional chat UI and API",
            "public MCP tool contract and report-only readiness controls",
        ],
        "external_service_candidates": [
            "institution SSO, gateway/reverse-proxy, TLS, and access integration",
            "privacy, audit, monitoring, backup, rollback, and incident operations",
            "optional institution-approved LLM integration",
            "independent security, privacy, accessibility, or compliance assessment",
        ],
        "institution_owned_required_scope": [
            "SSO and authorization integration",
            "gateway/reverse-proxy and TLS integration",
            "privacy, audit, retention, and security controls",
            "hosting, data refresh, backup, monitoring, rollback, and incident operations",
        ],
        "institution_owned_optional_scope": [
            "institution-approved LLM integration",
        ],
        "human_procurement_decision_required": True,
        "approval_claim": False,
        "basis": (
            "The repository supplies the NCS domain backend and a reference chat UI/API. "
            "Institution-owned SSO, gateway/TLS, privacy, and operations integration remain "
            "separate work; an approved LLM integration is optional."
        ),
    }

    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "ok": True,
        "ok_meaning": "report_generated_after_strict_input_validation",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "safety_flags": {
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "human_review_status_writes": False,
            "deployment_execution_authorized": False,
        },
        "core_backend_ready": core_backend_ready,
        "reference_chat_runtime_ready": reference_chat_runtime_ready,
        "private_pilot_backend_ready": private_pilot_backend_ready,
        "private_pilot_ready": private_pilot_ready,
        "stable_release_ready": stable_release_ready,
        "readiness_basis": {
            "release_engineering_contract_ready": release_core_ready,
            "chatbot_benchmark_ready": benchmark_ready,
            "private_preview_evidence_ready": private_preview_evidence_ready,
            "reference_chat_runtime_ready": reference_chat_runtime_ready,
            "institution_integration_ready": integration_ready,
            "active_stable_blocker_count": len(stable_blockers),
        },
        "evidence_contract": {
            "schemas_valid": True,
            "required_input_roles": sorted(expected_roles),
            "private_pilot_required_input_roles": sorted(
                expected_roles | {"institutional_chat_smoke"}
            ),
            "institutional_chat_smoke_provided": institutional_chat_smoke is not None,
            "optional_integration_evidence_provided": integration is not None,
            "max_evidence_age_hours": max_evidence_age_hours,
            "safety_ok": all_evidence_safe,
            "safety_by_role": safety_by_role,
            "freshness_ok": all_evidence_fresh,
            "freshness_by_role": freshness_by_role,
            "lineage_ok": not lineage_issues,
            "lineage_issues": lineage_issues,
            "contract_ok": evidence_contract_ok,
        },
        "repository_reference_chat": {
            "supplied_by_repository": True,
            "surfaces": ["reference_chat_ui", "reference_chat_api"],
            "runtime_evidence_required_for_private_pilot": True,
            "runtime_ready": reference_chat_runtime_ready,
            "institution_integration_completion_claimed": False,
            "boundary": (
                "Runtime smoke proves the supplied reference chat starts and preserves "
                "its safety contract; it does not prove institution SSO, gateway/TLS, "
                "privacy, or operational integration."
            ),
        },
        "reference_chat_smoke_evidence": reference_chat_smoke_evidence,
        "outsourcing_assessment": outsourcing_assessment,
        "institution_integration_requirements": integration_requirements,
        "blockers": blockers,
        "evidence_paths": evidence_paths,
        "evidence_fingerprints": fingerprints,
    }


def synthesize_from_paths(
    *,
    release_readiness_path: Path,
    deployment_decision_path: Path,
    chatbot_benchmark_path: Path,
    source_preview_summary_path: Path,
    institutional_chat_smoke_path: Path | None = None,
    institution_integration_path: Path | None = None,
    max_evidence_age_hours: float = DEFAULT_MAX_EVIDENCE_AGE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    paths = {
        "release_readiness": release_readiness_path,
        "deployment_decision": deployment_decision_path,
        "chatbot_benchmark": chatbot_benchmark_path,
        "source_preview_summary": source_preview_summary_path,
    }
    if institutional_chat_smoke_path is not None:
        paths["institutional_chat_smoke"] = institutional_chat_smoke_path
    if institution_integration_path is not None:
        paths["institution_integration"] = institution_integration_path
    payloads = {role: _read_json(path, role) for role, path in paths.items()}
    return build_report(
        payloads=payloads,
        paths=paths,
        max_evidence_age_hours=max_evidence_age_hours,
        now=now,
    )


def _markdown_text(report: dict[str, Any]) -> str:
    smoke = report["reference_chat_smoke_evidence"]
    public_chat = smoke["public_chat"]
    gateway = smoke["gateway"]
    audit_exclusions = smoke["audit_exclusions"]
    lines = [
        "# Institutional Chatbot Readiness",
        "",
        f"- core_backend_ready: `{str(report['core_backend_ready']).lower()}`",
        (
            "- reference_chat_runtime_ready: "
            f"`{str(report['reference_chat_runtime_ready']).lower()}`"
        ),
        f"- private_pilot_backend_ready: `{str(report['private_pilot_backend_ready']).lower()}`",
        f"- private_pilot_ready: `{str(report['private_pilot_ready']).lower()}`",
        f"- stable_release_ready: `{str(report['stable_release_ready']).lower()}`",
        f"- status_update_allowed: `{str(report['status_update_allowed']).lower()}`",
        f"- db_writes: `{str(report['db_writes']).lower()}`",
        f"- approval_claim: `{str(report['approval_claim']).lower()}`",
        "",
        "## Evidence Contract",
        "",
        f"- contract_ok: `{str(report['evidence_contract']['contract_ok']).lower()}`",
        f"- safety_ok: `{str(report['evidence_contract']['safety_ok']).lower()}`",
        f"- freshness_ok: `{str(report['evidence_contract']['freshness_ok']).lower()}`",
        f"- lineage_ok: `{str(report['evidence_contract']['lineage_ok']).lower()}`",
        "",
        "## Repository Reference Chat",
        "",
        "- The repository supplies the reference chat UI and API.",
        (
            "- Runtime smoke passing: "
            f"`{str(report['reference_chat_smoke_evidence']['passing']).lower()}`; "
            f"checks: `{report['reference_chat_smoke_evidence']['passed_check_count']}/"
            f"{report['reference_chat_smoke_evidence']['required_check_count']}`."
        ),
        (
            "- Public chat route: "
            f"state=`{public_chat['state']}`; tool=`{public_chat['tool']}`."
        ),
        (
            "- Operator route blocked / database unchanged: "
            f"`{str(smoke['operator_route_blocked']).lower()}` / "
            f"`{str(smoke['database_unchanged']).lower()}`."
        ),
        (
            "- Gateway auth / origin / rejection audit: "
            f"`{str(gateway['auth_enforced']).lower()}` / "
            f"`{str(gateway['origin_enforced']).lower()}` / "
            f"`{str(gateway['rejections_audited']).lower()}`."
        ),
        (
            "- Audit excludes prompt / raw identity / secret: "
            f"`{str(audit_exclusions['prompt_excluded']).lower()}` / "
            f"`{str(audit_exclusions['raw_identity_excluded']).lower()}` / "
            f"`{str(audit_exclusions['secret_excluded']).lower()}`."
        ),
        (
            "- Reference runtime evidence does not complete institution SSO, gateway/TLS, "
            "privacy, or operations integration."
        ),
        "- Institution-approved LLM integration is optional.",
        "",
        "## Outsourcing Assessment",
        "",
        f"- status: `{report['outsourcing_assessment']['status']}`",
        (
            "- Core NCS ontology, recommendation, and evidence logic remains the "
            "in-house domain scope."
        ),
        (
            "- Institution-owned work is SSO, gateway/TLS, privacy, audit, hosting, and "
            "operations integration, plus independent assurance where needed."
        ),
        "- This report is not procurement approval.",
        "",
        "## Institution Integration Requirements",
        "",
    ]
    for requirement in report["institution_integration_requirements"]:
        owner = requirement["owner"] or "unassigned"
        requirement_kind = "required" if requirement["required"] else "optional"
        lines.append(
            f"- `{requirement['id']}` ({requirement_kind}): "
            f"`{requirement['status']}`; owner: `{owner}`; "
            f"{requirement['responsibility']}"
        )
    lines.extend(["", "## Blockers", ""])
    if report["blockers"]:
        for blocker in report["blockers"]:
            lines.append(
                f"- `{blocker['code']}` ({blocker['scope']}): {blocker['message']}"
            )
    else:
        lines.append("- None.")
    lines.extend(["", "## Evidence Paths And Fingerprints", ""])
    for role, fingerprint in report["evidence_fingerprints"].items():
        lines.append(
            f"- `{role}`: `{fingerprint['path']}`; `{fingerprint['sha256']}`; "
            f"fresh=`{str(fingerprint['fresh']).lower()}`"
        )
    return "\n".join(lines) + "\n"


def _validate_output_paths(output_paths: Iterable[Path], input_paths: Iterable[Path]) -> None:
    resolved_outputs = [path.resolve() for path in output_paths]
    if len(set(resolved_outputs)) != len(resolved_outputs):
        raise EvidenceValidationError("JSON and Markdown output paths must be different")
    resolved_inputs = {path.resolve() for path in input_paths}
    overlap = [path for path in resolved_outputs if path in resolved_inputs]
    if overlap:
        raise EvidenceValidationError("output paths must not overwrite input evidence files")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthesize report-only institutional chatbot publication readiness."
    )
    parser.add_argument("--release-readiness", type=Path, required=True)
    parser.add_argument("--deployment-decision", type=Path, required=True)
    parser.add_argument("--chatbot-benchmark", type=Path, required=True)
    parser.add_argument("--source-preview-summary", type=Path, required=True)
    parser.add_argument(
        "--institutional-chat-smoke",
        type=Path,
        help=(
            "Optional ncs_institutional_chat_smoke_v1 evidence; private-pilot backend "
            "readiness remains blocked when omitted."
        ),
    )
    parser.add_argument("--institution-integration-evidence", type=Path)
    parser.add_argument(
        "--max-evidence-age-hours",
        type=float,
        default=DEFAULT_MAX_EVIDENCE_AGE_HOURS,
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    input_paths = [
        args.release_readiness,
        args.deployment_decision,
        args.chatbot_benchmark,
        args.source_preview_summary,
    ]
    if args.institution_integration_evidence:
        input_paths.append(args.institution_integration_evidence)
    if args.institutional_chat_smoke:
        input_paths.append(args.institutional_chat_smoke)
    try:
        _validate_output_paths((args.out, args.markdown_out), input_paths)
        report = synthesize_from_paths(
            release_readiness_path=args.release_readiness,
            deployment_decision_path=args.deployment_decision,
            chatbot_benchmark_path=args.chatbot_benchmark,
            source_preview_summary_path=args.source_preview_summary,
            institutional_chat_smoke_path=args.institutional_chat_smoke,
            institution_integration_path=args.institution_integration_evidence,
            max_evidence_age_hours=args.max_evidence_age_hours,
        )
    except EvidenceValidationError as exc:
        raise SystemExit(f"input validation failed: {exc}") from exc

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.markdown_out.write_text(_markdown_text(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
