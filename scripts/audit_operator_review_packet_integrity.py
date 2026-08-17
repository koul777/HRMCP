from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
DECISION_FIELDS = (
    "decision",
    "approved_definition",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "source_decision_packet",
    "evidence_refs_json",
)
GUARD_FALSE_FIELDS = (
    "status_update_allowed",
    "db_writes",
    "api_calls",
    "approval_claim",
    "execution_authorized",
    "automatic_queue_execution_allowed",
)
REQUIRED_CSV_GUARD_FALSE_FIELDS = ("status_update_allowed", "db_writes", "approval_claim")
REQUIRED_JSON_GUARD_FALSE_FIELDS = (
    "status_update_allowed",
    "db_writes",
    "approval_claim",
)
FORBIDDEN_AUTOMATIC_STATUSES = ("human_reviewed", "accepted", "reviewed")
NEXT_ACTION_REQUIRED_SOURCE_KEYS = (
    "blocker_reduction_sprint_queue",
    "blocker_reduction_sprint_queue_audit",
    "transition_provenance_crosswalk",
    "transition_provenance_crosswalk_csv",
    "transition_provenance_crosswalk_audit",
)
CROSSWALK_REQUIRED_COLUMNS = (
    "scenario_id",
    "decision_sheet_order",
    "operator_source_decision_packet_ref",
    "operator_source_artifact_hash",
    "operator_decision_fields_blank",
    "operator_guard_fields_false",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def portable_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    resolved = Path(path).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def resolve_artifact(value: Any, *, base_dir: Path = PROJECT_ROOT) -> Path | None:
    text = str(value or "").strip().partition("#")[0].strip()
    if not text:
        return None
    path = Path(text)
    candidate = path if path.is_absolute() else base_dir / path
    return candidate


def is_reports_artifact(path: Path | None, *, root: Path = PROJECT_ROOT) -> bool:
    if path is None:
        return False
    try:
        resolved = path.resolve(strict=False)
        reports_root = (root / "reports").resolve(strict=False)
        resolved.relative_to(reports_root)
    except (OSError, ValueError):
        return False
    return True


def artifact_exists_nonempty(path: Path | None) -> bool:
    return bool(path and path.exists() and path.is_file() and path.stat().st_size > 0)


def artifact_status(path: Path | None, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    return {
        "path": portable_path(path, root=root) if path else None,
        "exists_nonempty": artifact_exists_nonempty(path),
        "sha256": sha256_file(path),
    }


def is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


def is_true_text(value: Any) -> bool:
    return value is True or str(value).strip().lower() == "true"


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def sha256_artifact(path: Path | None, *, scope: str | None = None) -> str | None:
    if scope == "cycle_safe_release_readiness" and path is not None:
        try:
            payload = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if payload.get("sha256_scope") != scope:
            return None
        value = str(payload.get("cycle_safe_content_sha256") or "").strip()
        if value.startswith("sha256:") and len(value) == 71:
            return value
        return None
    return sha256_file(path)


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def add_issue(
    issues: list[dict[str, Any]],
    code: str,
    *,
    severity: str = "fail",
    **extra: Any,
) -> None:
    issue = {"severity": severity, "code": code}
    issue.update(extra)
    issues.append(issue)


def inspect_csv_decision_surface(
    label: str,
    path: Path,
    *,
    issues: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    status = artifact_status(path, root=root)
    if not status["exists_nonempty"]:
        add_issue(issues, "csv_decision_surface_missing", label=label, path=status["path"])
        return {
            **status,
            "label": label,
            "row_count": 0,
            "decision_fields_checked": [],
            "nonblank_counts": {},
            "decision_fields_blank_ok": False,
            "guard_columns_false_ok": False,
        }

    fieldnames, rows = read_csv_rows(path)
    checked = [field for field in DECISION_FIELDS if field in fieldnames]
    nonblank_counts: dict[str, int] = {}
    for row in rows:
        for field in checked:
            if str(row.get(field) or "").strip():
                nonblank_counts[field] = nonblank_counts.get(field, 0) + 1

    missing_required_guard_fields = [
        field for field in REQUIRED_CSV_GUARD_FALSE_FIELDS if field not in fieldnames
    ]
    guard_fields = [field for field in GUARD_FALSE_FIELDS if field in fieldnames]
    guard_columns_false_ok = all(
        is_false(row.get(field))
        for row in rows
        for field in guard_fields
    ) and not missing_required_guard_fields
    if nonblank_counts:
        add_issue(
            issues,
            "csv_decision_fields_not_blank",
            label=label,
            path=status["path"],
            nonblank_counts=nonblank_counts,
        )
    if not guard_columns_false_ok:
        add_issue(issues, "csv_guard_columns_not_false", label=label, path=status["path"])
    if missing_required_guard_fields:
        add_issue(
            issues,
            "csv_required_guard_columns_missing",
            label=label,
            path=status["path"],
            missing_columns=missing_required_guard_fields,
        )

    return {
        **status,
        "label": label,
        "row_count": len(rows),
        "decision_fields_checked": checked,
        "required_guard_fields": list(REQUIRED_CSV_GUARD_FALSE_FIELDS),
        "missing_required_guard_fields": missing_required_guard_fields,
        "nonblank_counts": nonblank_counts,
        "decision_fields_blank_ok": not nonblank_counts,
        "guard_columns_false_ok": guard_columns_false_ok,
    }


def inspect_transition_crosswalk_csv(
    label: str,
    path: Path,
    *,
    issues: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    status = artifact_status(path, root=root)
    if not status["exists_nonempty"]:
        add_issue(issues, "crosswalk_csv_missing", label=label, path=status["path"])
        return {**status, "label": label, "row_count": 0, "required_columns_present": False}

    fieldnames, rows = read_csv_rows(path)
    missing_columns = [field for field in CROSSWALK_REQUIRED_COLUMNS if field not in fieldnames]
    if missing_columns:
        add_issue(
            issues,
            "crosswalk_csv_required_columns_missing",
            label=label,
            path=status["path"],
            missing_columns=missing_columns,
        )

    source_ref_missing = [
        row.get("scenario_id")
        for row in rows
        if not str(row.get("operator_source_decision_packet_ref") or "").strip()
        or not str(row.get("operator_source_artifact_hash") or "").strip()
    ]
    decision_blank_bad = [
        row.get("scenario_id")
        for row in rows
        if not is_true_text(row.get("operator_decision_fields_blank"))
    ]
    guard_false_bad = [
        row.get("scenario_id")
        for row in rows
        if not is_true_text(row.get("operator_guard_fields_false"))
    ]
    source_artifact_checks: list[dict[str, Any]] = []
    source_hash_mismatch: list[dict[str, Any]] = []
    source_artifact_missing: list[dict[str, Any]] = []
    source_artifact_outside_reports: list[dict[str, Any]] = []
    for row in rows:
        scenario_id = row.get("scenario_id")
        ref = str(row.get("operator_source_decision_packet_ref") or "").strip()
        expected_hash = str(row.get("operator_source_artifact_hash") or "").strip()
        packet_path = resolve_artifact(ref, base_dir=root)
        actual_hash = sha256_file(packet_path)
        exists_nonempty = artifact_exists_nonempty(packet_path)
        inside_reports = is_reports_artifact(packet_path, root=root)
        check = {
            "scenario_id": scenario_id,
            "ref": ref,
            "path": portable_path(packet_path, root=root) if packet_path else None,
            "exists_nonempty": exists_nonempty,
            "inside_reports": inside_reports,
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "hash_matches": bool(expected_hash and actual_hash and expected_hash == actual_hash),
        }
        source_artifact_checks.append(check)
        if ref and not inside_reports:
            source_artifact_outside_reports.append(check)
        if ref and not exists_nonempty:
            source_artifact_missing.append(check)
        elif ref and expected_hash != actual_hash:
            source_hash_mismatch.append(check)
    if source_ref_missing:
        add_issue(
            issues,
            "crosswalk_operator_source_ref_missing",
            label=label,
            scenario_ids=source_ref_missing,
        )
    if decision_blank_bad:
        add_issue(
            issues,
            "crosswalk_decision_fields_not_blank",
            label=label,
            scenario_ids=decision_blank_bad,
        )
    if guard_false_bad:
        add_issue(
            issues,
            "crosswalk_guard_fields_not_false",
            label=label,
            scenario_ids=guard_false_bad,
        )
    if "review_status" in fieldnames:
        add_issue(issues, "crosswalk_leaks_review_status_column", label=label)
    if source_artifact_outside_reports:
        add_issue(
            issues,
            "crosswalk_operator_source_artifact_outside_reports",
            label=label,
            items=source_artifact_outside_reports,
        )
    if source_artifact_missing:
        add_issue(
            issues,
            "crosswalk_operator_source_artifact_missing",
            label=label,
            items=source_artifact_missing,
        )
    if source_hash_mismatch:
        add_issue(
            issues,
            "crosswalk_operator_source_artifact_hash_mismatch",
            label=label,
            items=source_hash_mismatch,
        )

    return {
        **status,
        "label": label,
        "row_count": len(rows),
        "required_columns_present": not missing_columns,
        "missing_columns": missing_columns,
        "operator_source_ref_missing_count": len(source_ref_missing),
        "operator_source_artifact_checks": source_artifact_checks,
        "operator_source_artifact_missing_count": len(source_artifact_missing),
        "operator_source_artifact_hash_mismatch_count": len(source_hash_mismatch),
        "operator_source_artifact_outside_reports_count": len(source_artifact_outside_reports),
        "operator_decision_fields_blank_ok": not decision_blank_bad,
        "operator_guard_fields_false_ok": not guard_false_bad,
        "review_status_column_present": "review_status" in fieldnames,
    }


def inspect_reference_csv(
    label: str,
    path: Path,
    *,
    issues: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    status = artifact_status(path, root=root)
    if not status["exists_nonempty"]:
        add_issue(issues, "reference_csv_missing", label=label, path=status["path"])
        return {**status, "label": label, "row_count": 0}
    _, rows = read_csv_rows(path)
    return {**status, "label": label, "row_count": len(rows)}


def source_hash_checks(
    payload: dict[str, Any],
    *,
    label: str,
    base_dir: Path,
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    source_paths = payload.get("source_paths")
    source_hashes = payload.get("source_hashes")
    source_hash_scopes = payload.get("source_hash_scopes")
    if "source_paths" in payload and not isinstance(source_paths, dict):
        add_issue(issues, "json_source_paths_not_object", label=label)
        return {}
    if not isinstance(source_paths, dict) or not source_paths:
        return {}
    if not isinstance(source_hashes, dict):
        add_issue(issues, "json_source_hashes_missing", label=label)
        source_hashes = {}
    if not isinstance(source_hash_scopes, dict):
        source_hash_scopes = {}
    checks: dict[str, dict[str, Any]] = {}
    for key, path_value in source_paths.items():
        if not path_value:
            continue
        path = resolve_artifact(path_value, base_dir=base_dir)
        expected = source_hashes.get(key)
        scope = source_hash_scopes.get(key)
        actual = sha256_artifact(path, scope=scope)
        exists_nonempty = artifact_exists_nonempty(path)
        inside_reports = is_reports_artifact(path, root=base_dir)
        checks[str(key)] = {
            "path": str(path_value),
            "exists_nonempty": exists_nonempty,
            "inside_reports": inside_reports,
            "expected_sha256": expected,
            "actual_sha256": actual,
            "sha256_scope": scope,
            "hash_matches": bool(expected and actual and expected == actual),
        }
        if not inside_reports:
            add_issue(
                issues,
                "json_source_artifact_outside_reports",
                label=label,
                source_key=key,
                path=str(path_value),
            )
        if not exists_nonempty:
            add_issue(
                issues,
                "json_source_artifact_missing",
                label=label,
                source_key=key,
                path=str(path_value),
            )
        elif expected != actual:
            add_issue(
                issues,
                "json_source_hash_stale",
                label=label,
                source_key=key,
                expected=actual,
                actual=expected,
            )
    return checks


def inspect_json_artifact(
    label: str,
    path: Path,
    *,
    issues: list[dict[str, Any]],
    root: Path,
    expected_schema: str | None = None,
    check_sources: bool = True,
    require_markdown: bool = True,
) -> dict[str, Any]:
    status = artifact_status(path, root=root)
    if not status["exists_nonempty"]:
        add_issue(issues, "json_artifact_missing", label=label, path=status["path"])
        return {**status, "label": label, "payload_loaded": False}

    payload = read_json(path)
    schema = payload.get("schema")
    if expected_schema and schema != expected_schema:
        add_issue(
            issues,
            "json_schema_mismatch",
            label=label,
            path=status["path"],
            expected_schema=expected_schema,
            actual_schema=schema,
        )

    guard: dict[str, Any] = {}
    if not payload.get("generated_at"):
        add_issue(issues, "json_generated_at_missing", label=label, path=status["path"])
    for field in ("report_only", "human_decision_required"):
        if field in payload:
            guard[field] = payload.get(field)
    missing_required_guard_fields = [
        field for field in REQUIRED_JSON_GUARD_FALSE_FIELDS if field not in payload
    ]
    if missing_required_guard_fields:
        add_issue(
            issues,
            "json_required_guard_fields_missing",
            label=label,
            path=status["path"],
            missing_fields=missing_required_guard_fields,
        )
    for field in GUARD_FALSE_FIELDS:
        if field in payload:
            guard[field] = payload.get(field)
            if not is_false(payload.get(field)):
                add_issue(
                    issues,
                    "json_guard_field_not_false",
                    label=label,
                    field=field,
                    actual=payload.get(field),
                )
    if "report_only" in payload and payload.get("report_only") is not True:
        add_issue(issues, "json_report_only_not_true", label=label)
    if "forbidden_automatic_statuses" in payload:
        statuses = tuple(payload.get("forbidden_automatic_statuses") or ())
        missing = [value for value in FORBIDDEN_AUTOMATIC_STATUSES if value not in statuses]
        if missing:
            add_issue(issues, "json_forbidden_statuses_missing", label=label, missing=missing)

    markdown = path.with_suffix(".md")
    markdown_status = artifact_status(markdown, root=root)
    if require_markdown and not markdown_status["exists_nonempty"]:
        add_issue(
            issues,
            "paired_markdown_missing",
            label=label,
            path=markdown_status["path"],
        )

    checks = source_hash_checks(payload, label=label, base_dir=root, issues=issues) if check_sources else {}
    return {
        **status,
        "label": label,
        "payload_loaded": True,
        "schema": schema,
        "generated_at": payload.get("generated_at"),
        "ok": payload.get("ok"),
        "guard": guard,
        "paired_markdown": markdown_status,
        "source_hash_checks": checks,
    }


def inspect_next_actions(
    payload: dict[str, Any] | None,
    *,
    issues: list[dict[str, Any]],
    root: Path,
) -> dict[str, Any]:
    if payload is None:
        return {"loaded": False}

    source_paths = payload.get("source_paths") if isinstance(payload.get("source_paths"), dict) else {}
    missing_source_keys = [key for key in NEXT_ACTION_REQUIRED_SOURCE_KEYS if not source_paths.get(key)]
    if missing_source_keys:
        add_issue(
            issues,
            "next_actions_required_source_keys_missing",
            missing_source_keys=missing_source_keys,
        )

    actions = payload.get("actions")
    missing_open_first: list[dict[str, Any]] = []
    missing_artifacts: list[dict[str, Any]] = []
    transition_open_first = None
    if isinstance(actions, list):
        for action in actions:
            if not isinstance(action, dict):
                continue
            action_id = action.get("id") or action.get("blocker")
            open_first = action.get("open_first")
            open_first_path = resolve_artifact(open_first, base_dir=root)
            if not artifact_exists_nonempty(open_first_path):
                missing_open_first.append({"id": action_id, "open_first": open_first})
            if action_id and "transition_eval" in str(action_id):
                transition_open_first = open_first
            for artifact in action.get("artifacts_to_open") or []:
                artifact_path = resolve_artifact(artifact, base_dir=root)
                if not artifact_exists_nonempty(artifact_path):
                    missing_artifacts.append({"id": action_id, "artifact": artifact})
    else:
        add_issue(issues, "next_actions_actions_not_list")

    if missing_open_first:
        add_issue(issues, "next_actions_open_first_missing", items=missing_open_first)
    if missing_artifacts:
        add_issue(issues, "next_actions_artifacts_missing", items=missing_artifacts)
    if transition_open_first and "transition_provenance_operator_crosswalk" not in str(transition_open_first):
        add_issue(
            issues,
            "next_actions_transition_open_first_not_crosswalk",
            open_first=transition_open_first,
        )

    return {
        "loaded": True,
        "action_count": len(actions) if isinstance(actions, list) else None,
        "missing_source_keys": missing_source_keys,
        "missing_open_first": missing_open_first,
        "missing_artifacts": missing_artifacts,
        "transition_open_first": transition_open_first,
    }


def inspect_sprint_queue(
    payload: dict[str, Any] | None,
    *,
    issues: list[dict[str, Any]],
) -> dict[str, Any]:
    if payload is None:
        return {"loaded": False}
    queue = payload.get("queue")
    s1 = None
    if isinstance(queue, list):
        for item in queue:
            if isinstance(item, dict) and item.get("sprint_id") == "S1-transition-provenance-crosswalk":
                s1 = item
                break
    if not s1:
        add_issue(issues, "sprint_queue_s1_crosswalk_missing")
        return {"loaded": True, "s1_found": False}
    open_first = str(s1.get("open_first") or "")
    if "transition_provenance_operator_crosswalk" not in open_first:
        add_issue(issues, "sprint_queue_s1_open_first_not_crosswalk", open_first=open_first)
    return {"loaded": True, "s1_found": True, "s1_open_first": open_first}


def build_integrity_audit(
    *,
    concept_seedpack_csv: Path,
    blocker_ranked_seedpack_csv: Path,
    provenance_decision_sheet_csv: Path,
    transition_crosswalk_csv: Path,
    qualification_decision_csv: Path,
    provenance_decision_sheet_json: Path,
    provenance_decision_audit_json: Path,
    qualification_decision_json: Path,
    transition_gap_json: Path,
    transition_crosswalk_json: Path,
    transition_crosswalk_audit_json: Path,
    blocker_sprint_queue_json: Path,
    blocker_sprint_queue_audit_json: Path,
    operator_next_actions_json: Path,
    generated_at: str | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    csv_decision_surfaces = [
        inspect_csv_decision_surface(
            "ontology_definition_seedpack",
            concept_seedpack_csv,
            issues=issues,
            root=root,
        ),
        inspect_csv_decision_surface(
            "blocker_ranked_review_seedpack",
            blocker_ranked_seedpack_csv,
            issues=issues,
            root=root,
        ),
        inspect_csv_decision_surface(
            "provenance_reconfirmation_decision_sheet",
            provenance_decision_sheet_csv,
            issues=issues,
            root=root,
        ),
    ]
    csv_reference_surfaces = [
        inspect_transition_crosswalk_csv(
            "transition_provenance_operator_crosswalk",
            transition_crosswalk_csv,
            issues=issues,
            root=root,
        ),
        inspect_reference_csv(
            "qualification_guarded_batch_operator_decision",
            qualification_decision_csv,
            issues=issues,
            root=root,
        ),
    ]

    json_specs = [
        (
            "provenance_reconfirmation_decision_sheet",
            provenance_decision_sheet_json,
            "aihr_provenance_reconfirmation_decision_sheet_v1",
        ),
        (
            "provenance_reconfirmation_decision_audit",
            provenance_decision_audit_json,
            "aihr_provenance_reconfirmation_decision_audit_v1",
        ),
        (
            "qualification_guarded_batch_operator_decision",
            qualification_decision_json,
            "qualification_guarded_batch_operator_decision_v1",
        ),
        (
            "transition_trusted_scenario_provenance_gap",
            transition_gap_json,
            "transition_trusted_scenario_provenance_gap_v1",
        ),
        (
            "transition_provenance_operator_crosswalk",
            transition_crosswalk_json,
            "transition_provenance_operator_crosswalk_v1",
        ),
        (
            "transition_provenance_operator_crosswalk_audit",
            transition_crosswalk_audit_json,
            "transition_provenance_operator_crosswalk_audit_v1",
        ),
        (
            "blocker_reduction_sprint_queue",
            blocker_sprint_queue_json,
            "aihr_blocker_reduction_operator_sprint_queue_v1",
        ),
        (
            "blocker_reduction_sprint_queue_audit",
            blocker_sprint_queue_audit_json,
            "aihr_blocker_reduction_operator_sprint_queue_audit_v1",
        ),
        (
            "operator_next_actions",
            operator_next_actions_json,
            "aihr_operator_next_actions_v3",
        ),
    ]
    json_artifacts = [
        inspect_json_artifact(
            label,
            path,
            issues=issues,
            root=root,
            expected_schema=schema,
            check_sources=True,
        )
        for label, path, schema in json_specs
    ]
    payloads = {
        artifact["label"]: read_json(resolve_artifact(artifact["path"], base_dir=root))
        if artifact.get("payload_loaded")
        else None
        for artifact in json_artifacts
    }
    next_actions_checks = inspect_next_actions(
        payloads.get("operator_next_actions"),
        issues=issues,
        root=root,
    )
    sprint_queue_checks = inspect_sprint_queue(
        payloads.get("blocker_reduction_sprint_queue"),
        issues=issues,
    )

    for label in (
        "provenance_reconfirmation_decision_audit",
        "transition_provenance_operator_crosswalk_audit",
        "blocker_reduction_sprint_queue_audit",
    ):
        payload = payloads.get(label)
        if isinstance(payload, dict) and payload.get("ok") is not True:
            add_issue(issues, "supporting_audit_not_ok", label=label, ok=payload.get("ok"))

    warnings: list[dict[str, Any]] = []
    transition_crosswalk_audit = payloads.get("transition_provenance_operator_crosswalk_audit")
    if isinstance(transition_crosswalk_audit, dict) and transition_crosswalk_audit.get("warning_count"):
        warnings.append(
            {
                "code": "transition_crosswalk_audit_warnings_present",
                "warning_count": transition_crosswalk_audit.get("warning_count"),
            }
        )

    return {
        "schema": "operator_review_packet_integrity_audit_v2",
        "generated_at": generated_at or now_iso(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "ok": not issues,
        "issue_count": len(issues),
        "warning_count": len(warnings),
        "issues": issues,
        "warnings": warnings,
        "csv_seedpacks": csv_decision_surfaces,
        "csv_reference_surfaces": csv_reference_surfaces,
        "json_artifacts": json_artifacts,
        "next_actions_checks": next_actions_checks,
        "sprint_queue_checks": sprint_queue_checks,
        "operator_guidance": {
            "decision_fields_must_remain_blank_until_human_review": True,
            "forbidden_automatic_statuses": list(FORBIDDEN_AUTOMATIC_STATUSES),
            "crosswalk_is_reference_not_approval": True,
            "qualification_batches_require_explicit_operator_start": True,
            "db_writes": False,
            "approval_claim": False,
        },
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Operator Review Packet Integrity Audit",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- issue_count: `{report.get('issue_count')}`",
        f"- warning_count: `{report.get('warning_count')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## CSV Decision Surfaces",
    ]
    for surface in report.get("csv_seedpacks") or []:
        lines.append(
            "- "
            f"{surface.get('label')}: rows `{surface.get('row_count')}`, "
            f"blank_ok `{surface.get('decision_fields_blank_ok')}`, "
            f"guard_false_ok `{surface.get('guard_columns_false_ok')}`"
        )
    lines.extend(["", "## CSV Reference Surfaces"])
    for surface in report.get("csv_reference_surfaces") or []:
        lines.append(
            "- "
            f"{surface.get('label')}: rows `{surface.get('row_count')}`, "
            f"exists `{surface.get('exists_nonempty')}`"
        )
    lines.extend(["", "## JSON Artifacts"])
    for artifact in report.get("json_artifacts") or []:
        lines.append(
            "- "
            f"{artifact.get('label')}: schema `{artifact.get('schema')}`, "
            f"ok `{artifact.get('ok')}`, sha256 `{artifact.get('sha256')}`"
        )
    if report.get("issues"):
        lines.extend(["", "## Issues"])
        for issue in report.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: `{issue}`")
    else:
        lines.extend(["", "No operator packet integrity issues found."])
    if report.get("warnings"):
        lines.extend(["", "## Warnings"])
        for warning in report.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: `{warning}`")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def default_report(name: str) -> Path:
    return REPORTS / name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit report-only AI-HR operator review packet integrity."
    )
    parser.add_argument(
        "--concept-seedpack-csv",
        type=Path,
        default=default_report("aihr_ontology_definition_review_seedpack_20260712_10h.csv"),
    )
    parser.add_argument(
        "--blocker-ranked-seedpack-csv",
        type=Path,
        default=default_report("aihr_review_seedpack_blocker_ranked_20260712_10h.csv"),
    )
    parser.add_argument(
        "--provenance-decision-sheet-csv",
        type=Path,
        default=default_report("human_review_provenance_reconfirmation_decision_sheet_20260712_10h.csv"),
    )
    parser.add_argument(
        "--transition-crosswalk-csv",
        type=Path,
        default=default_report("transition_provenance_operator_crosswalk_20260712_10h.csv"),
    )
    parser.add_argument(
        "--qualification-decision-csv",
        type=Path,
        default=default_report("qualification_guarded_batch_operator_decision_20260712_10h.csv"),
    )
    parser.add_argument(
        "--provenance-decision-sheet-json",
        type=Path,
        default=default_report("human_review_provenance_reconfirmation_decision_sheet_20260712_10h.json"),
    )
    parser.add_argument(
        "--provenance-decision-audit-json",
        type=Path,
        default=default_report("human_review_provenance_reconfirmation_decision_audit_20260712_10h.json"),
    )
    parser.add_argument(
        "--qualification-decision-json",
        type=Path,
        default=default_report("qualification_guarded_batch_operator_decision_20260712_10h.json"),
    )
    parser.add_argument(
        "--transition-gap-json",
        type=Path,
        default=default_report("transition_trusted_scenario_provenance_gap_20260712_10h.json"),
    )
    parser.add_argument(
        "--transition-crosswalk-json",
        type=Path,
        default=default_report("transition_provenance_operator_crosswalk_20260712_10h.json"),
    )
    parser.add_argument(
        "--transition-crosswalk-audit-json",
        type=Path,
        default=default_report("transition_provenance_operator_crosswalk_audit_20260712_10h.json"),
    )
    parser.add_argument(
        "--blocker-sprint-queue-json",
        type=Path,
        default=default_report("aihr_blocker_reduction_operator_sprint_queue_20260712_10h.json"),
    )
    parser.add_argument(
        "--blocker-sprint-queue-audit-json",
        type=Path,
        default=default_report("aihr_blocker_reduction_operator_sprint_queue_audit_20260712_10h.json"),
    )
    parser.add_argument(
        "--operator-next-actions-json",
        type=Path,
        default=default_report("aihr_operator_next_actions_20260712_10h.json"),
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    report = build_integrity_audit(
        concept_seedpack_csv=args.concept_seedpack_csv,
        blocker_ranked_seedpack_csv=args.blocker_ranked_seedpack_csv,
        provenance_decision_sheet_csv=args.provenance_decision_sheet_csv,
        transition_crosswalk_csv=args.transition_crosswalk_csv,
        qualification_decision_csv=args.qualification_decision_csv,
        provenance_decision_sheet_json=args.provenance_decision_sheet_json,
        provenance_decision_audit_json=args.provenance_decision_audit_json,
        qualification_decision_json=args.qualification_decision_json,
        transition_gap_json=args.transition_gap_json,
        transition_crosswalk_json=args.transition_crosswalk_json,
        transition_crosswalk_audit_json=args.transition_crosswalk_audit_json,
        blocker_sprint_queue_json=args.blocker_sprint_queue_json,
        blocker_sprint_queue_audit_json=args.blocker_sprint_queue_audit_json,
        operator_next_actions_json=args.operator_next_actions_json,
        root=args.root,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "issue_count": report.get("issue_count"),
                "warning_count": report.get("warning_count"),
                "out": str(args.out),
                "markdown_out": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and not report.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
