from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_release_blocker_operator_addendum_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
REQUIRED_HUMAN_DECISION_FIELDS = {"decision", "reviewer_id", "reviewed_at", "rationale"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_fragment(value: str | Path | None) -> str:
    return str(value or "").partition("#")[0].strip()


def resolve_artifact(value: str | Path | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    text = strip_fragment(value)
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    rooted = root / path
    if rooted.exists():
        return rooted
    if path.exists():
        return path
    return rooted


def portable_path(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> str | None:
    text = strip_fragment(path)
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists() or not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    return payload if isinstance(payload, dict) else {}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def source_status(path: Path | None, *, root: Path) -> dict[str, Any]:
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            path and path.exists() and path.is_file() and path.stat().st_size > 0
        ),
        "sha256": sha256_file(path),
    }


def safe_report_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true_or_absent": payload.get("human_decision_required")
        in (True, None),
    }


def terminal_cycle_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "terminal_evidence_only_is_true": payload.get("terminal_evidence_only") is True,
        "include_in_release_refresh_dag_is_false": payload.get("include_in_release_refresh_dag")
        is False,
        "include_in_operator_handoff_is_false": payload.get("include_in_operator_handoff")
        is False,
    }


def embedded_source_hash_mismatches(
    payload: dict[str, Any],
    *,
    root: Path,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    checks = payload.get("source_hash_checks")
    if not isinstance(checks, dict):
        return mismatches
    for key, check in checks.items():
        if not isinstance(check, dict):
            continue
        path_value = check.get("path") or check.get("source_path")
        expected = check.get("expected_sha256") or check.get("source_sha256") or check.get("sha256")
        reported_match = check.get("hash_matches")
        if reported_match is None:
            reported_match = check.get("matches")
        resolved = resolve_artifact(path_value, root=root) if path_value else None
        actual = sha256_file(resolved)
        reason = None
        if not path_value:
            reason = "missing_source_path"
        elif not expected:
            reason = "missing_expected_sha256"
        elif not actual:
            reason = "source_missing_or_empty"
        elif expected != actual:
            reason = "current_source_hash_mismatch"
        elif reported_match is not True:
            reason = "reported_hash_mismatch"
        if reason:
            mismatches.append(
                {
                    "key": key,
                    "path": portable_path(path_value, root=root),
                    "expected_sha256": expected,
                    "actual_sha256": actual,
                    "reported_hash_matches": reported_match,
                    "reason": reason,
                }
            )
    return mismatches


def bool_all(values: dict[str, bool]) -> bool:
    return all(value is True for value in values.values())


def split_blocker_expression(value: Any) -> set[str]:
    text = str(value or "")
    parts = [part.strip() for part in re.split(r"\s*\+\s*|,\s*", text) if part.strip()]
    return set(parts)


def normalize_blockers(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    blockers: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("blocker") or item.get("machine_name") or "")
            if name:
                blockers.append(item)
    return blockers


def issue(code: str, message: str, **details: Any) -> dict[str, Any]:
    record = {"code": code, "message": message}
    record.update({key: value for key, value in details.items() if value is not None})
    return record


def int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def source_total_row_count(sprint: dict[str, Any]) -> int:
    for key in ("source_total_row_count", "declared_row_count", "row_count"):
        count = int_value(sprint.get(key))
        if count > 0:
            return count
    return int_value(sprint.get("selected_row_count"))


def sprint_record(sprint: dict[str, Any]) -> dict[str, Any]:
    rows = [row for row in (sprint.get("rows") or []) if isinstance(row, dict)]
    selected_count = int_value(sprint.get("selected_row_count"))
    source_count = source_total_row_count(sprint)
    unselected_count = max(source_count - selected_count, 0)
    return {
        "rank": sprint.get("rank"),
        "sprint_id": sprint.get("sprint_id"),
        "blocker": sprint.get("blocker"),
        "next_safe_action": sprint.get("next_safe_action"),
        "open_first": sprint.get("open_first"),
        "source_path": sprint.get("source_path"),
        "row_selector": sprint.get("row_selector"),
        "selected_row_count": selected_count,
        "source_total_row_count": source_count,
        "unselected_source_row_count": unselected_count,
        "selected_subset": unselected_count > 0,
        "scope_match_ok": sprint.get("scope_match_ok"),
        "missing_expected_first_row_ids": sprint.get("missing_expected_first_row_ids") or [],
        "required_human_fields": sprint.get("required_human_fields") or [],
        "decision_options": sprint.get("decision_options") or [],
        "row_keys": [row.get("source_row_key") for row in rows],
    }


def sprint_has_rows(sprint: dict[str, Any]) -> bool:
    rows = [row for row in (sprint.get("rows") or []) if isinstance(row, dict)]
    return int_value(sprint.get("selected_row_count")) > 0 and bool(rows)


def human_decision_contract(sprint: dict[str, Any]) -> dict[str, Any]:
    required_fields = {str(field) for field in (sprint.get("required_human_fields") or [])}
    missing_fields = sorted(REQUIRED_HUMAN_DECISION_FIELDS - required_fields)
    decision_options = [option for option in (sprint.get("decision_options") or []) if option]
    return {
        "sprint_id": sprint.get("sprint_id"),
        "required_human_fields_present": not missing_fields,
        "missing_required_human_fields": missing_fields,
        "decision_options_present": bool(decision_options),
        "ok": not missing_fields and bool(decision_options),
    }


def is_human_decision_blocker(blocker: dict[str, Any]) -> bool:
    name = str(blocker.get("name") or blocker.get("blocker") or blocker.get("machine_name") or "")
    category = str(blocker.get("category") or "")
    return (
        category in {"human_review", "evaluation"}
        or name.startswith("review_debt:")
        or name.startswith("human_review:")
        or name.startswith("transition_eval:")
    )


def qualification_guard_contract(blocker: dict[str, Any]) -> dict[str, bool]:
    evidence = blocker.get("evidence") if isinstance(blocker.get("evidence"), dict) else {}
    return {
        "remaining_status_is_guarded_manual_ready": blocker.get("status") == "guarded_manual_ready",
        "operator_timing_required_is_true": evidence.get("operator_timing_required") is True,
        "guarded_collection_required_is_true": evidence.get("guarded_collection_required") is True,
        "automatic_collection_allowed_now_is_false": evidence.get("automatic_collection_allowed_now")
        is False,
    }


def operator_next_safe_action(
    blocker: dict[str, Any],
    *,
    matching_sprints: list[dict[str, Any]],
) -> Any:
    direct = blocker.get("next_safe_action")
    if direct:
        return direct
    for sprint in matching_sprints:
        action = sprint.get("next_safe_action")
        if action:
            return action
    name = str(blocker.get("name") or blocker.get("blocker") or blocker.get("machine_name") or "")
    if name == "transition_eval:trusted_scenarios" and matching_sprints:
        return "review-transition-provenance-crosswalk-human-decisions"
    return None


def blocker_operator_status(
    blocker: dict[str, Any],
    *,
    sprints: list[dict[str, Any]],
) -> dict[str, Any]:
    name = str(blocker.get("name") or blocker.get("blocker") or blocker.get("machine_name") or "")
    matching_sprints = [
        sprint
        for sprint in sprints
        if name in split_blocker_expression(sprint.get("blocker"))
    ]
    sprint_summaries = [sprint_record(sprint) for sprint in matching_sprints]
    row_count = sum(int_value(sprint.get("selected_row_count")) for sprint in matching_sprints)
    source_row_count = sum(source_total_row_count(sprint) for sprint in matching_sprints)
    unselected_row_count = max(source_row_count - row_count, 0)
    sprint_scope_ok = bool(matching_sprints) and all(
        sprint.get("scope_match_ok") is True for sprint in matching_sprints
    )
    missing_expected_rows = [
        row_id
        for sprint in matching_sprints
        for row_id in (sprint.get("missing_expected_first_row_ids") or [])
    ]
    empty_sprints = [
        str(sprint.get("sprint_id") or sprint.get("rank") or "")
        for sprint in matching_sprints
        if not sprint_has_rows(sprint)
    ]
    operator_has_rows = bool(matching_sprints) and not empty_sprints
    human_decision_required = is_human_decision_blocker(blocker)
    human_contracts = [
        human_decision_contract(sprint) for sprint in matching_sprints
    ] if human_decision_required else []
    human_contract_ok = (
        all(contract.get("ok") is True for contract in human_contracts)
        if human_decision_required
        else None
    )
    human_contract_issue_sprints = [
        contract for contract in human_contracts if contract.get("ok") is not True
    ]
    is_qualification_blocker = name.startswith("qualification:")
    qualification_contract = (
        qualification_guard_contract(blocker) if is_qualification_blocker else {}
    )
    qualification_contract_ok = (
        bool_all(qualification_contract) if is_qualification_blocker else None
    )
    evidence = blocker.get("evidence") if isinstance(blocker.get("evidence"), dict) else {}
    if not matching_sprints:
        readiness = "missing_operator_sprint"
    elif not sprint_scope_ok or missing_expected_rows:
        readiness = "operator_scope_issue"
    elif not operator_has_rows:
        readiness = "operator_row_issue"
    elif is_qualification_blocker and qualification_contract_ok is not True:
        readiness = "qualification_guard_contract_issue"
    elif human_decision_required and human_contract_ok is not True:
        readiness = "human_decision_contract_issue"
    elif is_qualification_blocker:
        readiness = "guarded_manual_ready_operator_timing_required"
    else:
        readiness = "operator_prepared_human_decision_pending"
    operator_ready_contract_ok = readiness in {
        "guarded_manual_ready_operator_timing_required",
        "operator_prepared_human_decision_pending",
    }
    return {
        "name": name,
        "category": blocker.get("category"),
        "remaining_status": blocker.get("status"),
        "display_label": blocker.get("display_label"),
        "display_message": blocker.get("display_message"),
        "next_safe_action": operator_next_safe_action(
            blocker,
            matching_sprints=matching_sprints,
        ),
        "current_count": evidence.get("current_count"),
        "required_threshold": evidence.get("required_threshold") or evidence.get("target"),
        "operator_readiness": readiness,
        "operator_sprint_count": len(matching_sprints),
        "operator_row_count": row_count,
        "operator_row_count_meaning": "selected_workbench_rows",
        "selected_operator_row_count": row_count,
        "operator_source_total_row_count": source_row_count,
        "operator_unselected_source_row_count": unselected_row_count,
        "operator_workbench_selected_subset": unselected_row_count > 0,
        "operator_scope_ok": sprint_scope_ok,
        "missing_expected_first_row_ids": missing_expected_rows,
        "operator_has_rows": operator_has_rows,
        "empty_operator_sprints": empty_sprints,
        "human_decision_contract_required": human_decision_required,
        "human_decision_contract_ok": human_contract_ok,
        "human_decision_contract_issue_sprints": human_contract_issue_sprints,
        "qualification_guard_contract_ok": qualification_contract_ok,
        "qualification_guard_contract": qualification_contract,
        "operator_ready_contract_ok": operator_ready_contract_ok,
        "operator_sprints": sprint_summaries,
    }


def build_addendum(
    *,
    release_readiness_path: Path,
    remaining_blockers_path: Path,
    goal_completion_audit_path: Path,
    operator_workbench_path: Path,
    terminal_evidence_index_path: Path,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    paths = {
        "release_readiness": release_readiness_path,
        "remaining_blockers": remaining_blockers_path,
        "goal_completion_audit": goal_completion_audit_path,
        "operator_decision_workbench": operator_workbench_path,
        "terminal_evidence_index": terminal_evidence_index_path,
    }
    payloads = {name: read_json(path) for name, path in paths.items()}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    source_artifacts = {name: source_status(path, root=root) for name, path in paths.items()}
    for name, artifact in source_artifacts.items():
        if artifact.get("exists_nonempty") is not True:
            issues.append(
                issue(
                    "missing_source_artifact",
                    "A required source artifact is missing or empty.",
                    source=name,
                    path=artifact.get("path"),
                )
            )

    source_contracts = {
        name: safe_report_contract(payload)
        for name, payload in payloads.items()
        if payload
    }
    for name, contract in source_contracts.items():
        if not bool_all(contract):
            issues.append(
                issue(
                    "unsafe_source_contract",
                    "Source artifact does not preserve report-only human-gated contract.",
                    source=name,
                    contract=contract,
                )
            )

    embedded_hash_mismatches: dict[str, list[dict[str, Any]]] = {}
    for name, payload in payloads.items():
        if not payload:
            continue
        mismatches = embedded_source_hash_mismatches(payload, root=root)
        if not mismatches:
            continue
        embedded_hash_mismatches[name] = mismatches
        issues.append(
            issue(
                "embedded_source_hash_mismatch",
                "A source artifact contains stale embedded source hash checks.",
                source=name,
                mismatches=mismatches,
            )
        )

    terminal_payload = payloads.get("terminal_evidence_index") or {}
    terminal_contract = terminal_cycle_contract(terminal_payload)
    if terminal_payload and not bool_all(terminal_contract):
        issues.append(
            issue(
                "unsafe_terminal_cycle_contract",
                "Terminal evidence index must stay outside the release refresh DAG and operator handoff.",
                contract=terminal_contract,
            )
        )

    release = payloads.get("release_readiness") or {}
    remaining = payloads.get("remaining_blockers") or {}
    goal = payloads.get("goal_completion_audit") or {}
    workbench = payloads.get("operator_decision_workbench") or {}

    release_blockers = normalize_blockers(release.get("blockers"))
    remaining_blockers = normalize_blockers(remaining.get("remaining_blockers"))
    release_names = {str(item.get("name") or item.get("blocker") or "") for item in release_blockers}
    remaining_names = {
        str(item.get("name") or item.get("blocker") or item.get("machine_name") or "")
        for item in remaining_blockers
    }
    if release_names != remaining_names:
        issues.append(
            issue(
                "blocker_set_mismatch",
                "Release-readiness blockers and remaining-blockers report do not name the same blocker set.",
                release_only=sorted(release_names - remaining_names),
                remaining_only=sorted(remaining_names - release_names),
            )
        )

    if release.get("release_ready") is True and remaining_blockers:
        issues.append(
            issue(
                "release_ready_with_remaining_blockers",
                "Release readiness claims ready while remaining blockers are still present.",
            )
        )
    if goal.get("release_ready") != release.get("release_ready"):
        warnings.append(
            issue(
                "goal_release_ready_alignment_warning",
                "Goal-completion audit and release-readiness differ on release_ready.",
                release_ready=release.get("release_ready"),
                goal_release_ready=goal.get("release_ready"),
            )
        )

    workbench_summary = (
        workbench.get("summary") if isinstance(workbench.get("summary"), dict) else {}
    )
    terminal_summary = (
        terminal_payload.get("summary")
        if isinstance(terminal_payload.get("summary"), dict)
        else {}
    )
    terminal_warnings = [
        item for item in (terminal_payload.get("warnings") or []) if isinstance(item, dict)
    ]
    terminal_warning_codes = [
        str(item.get("code"))
        for item in terminal_warnings
        if str(item.get("code") or "").strip()
    ]
    terminal_warning_sources = [
        {
            "code": item.get("code"),
            "label": item.get("label"),
            "path": item.get("path"),
            "counters": item.get("counters"),
            "source_warning_code_counts": item.get("source_warning_code_counts"),
        }
        for item in terminal_warnings
    ]
    if workbench.get("ok") is not True:
        issues.append(
            issue(
                "operator_workbench_not_ok",
                "Operator decision workbench is not passing.",
                status=workbench.get("status"),
            )
        )
    if terminal_payload.get("ok") is not True:
        issues.append(
            issue(
                "terminal_evidence_index_not_ok",
                "Terminal evidence index is not passing.",
                status=terminal_payload.get("status"),
            )
        )
    if int_value(terminal_summary.get("warning_count")):
        warnings.append(
            issue(
                "terminal_evidence_index_warnings",
                "Terminal evidence index has warnings.",
                warning_count=terminal_summary.get("warning_count"),
                warning_codes=terminal_warning_codes,
            )
        )

    sprints = [item for item in (workbench.get("sprints") or []) if isinstance(item, dict)]
    blocker_statuses = [
        blocker_operator_status(blocker, sprints=sprints)
        for blocker in remaining_blockers
    ]
    workbench_selected_row_count = sum(
        int_value(sprint.get("selected_row_count")) for sprint in sprints
    )
    workbench_reported_row_count = int_value(workbench_summary.get("workbench_row_count"))
    workbench_source_total_row_count = sum(source_total_row_count(sprint) for sprint in sprints)
    workbench_unselected_source_row_count = max(
        workbench_source_total_row_count - workbench_selected_row_count,
        0,
    )
    selected_subset_sprints = [
        sprint_record(sprint)
        for sprint in sprints
        if source_total_row_count(sprint) > int_value(sprint.get("selected_row_count"))
    ]
    uncovered = [
        item["name"]
        for item in blocker_statuses
        if item.get("operator_sprint_count", 0) == 0
    ]
    scope_issues = [
        item["name"]
        for item in blocker_statuses
        if item.get("operator_sprint_count", 0) > 0 and item.get("operator_scope_ok") is not True
    ]
    row_issues = [
        item["name"]
        for item in blocker_statuses
        if item.get("operator_sprint_count", 0) > 0
        and item.get("operator_has_rows") is not True
    ]
    human_contract_issues = [
        item["name"]
        for item in blocker_statuses
        if item.get("human_decision_contract_required") is True
        and item.get("human_decision_contract_ok") is not True
    ]
    qualification_guard_issues = [
        item["name"]
        for item in blocker_statuses
        if item.get("qualification_guard_contract_ok") is False
    ]
    if uncovered:
        issues.append(
            issue(
                "remaining_blocker_without_operator_sprint",
                "A remaining blocker has no matching operator workbench sprint.",
                blockers=uncovered,
            )
        )
    if scope_issues:
        issues.append(
            issue(
                "remaining_blocker_operator_scope_issue",
                "A matching operator workbench sprint has scope or expected-row issues.",
                blockers=scope_issues,
            )
        )
    if row_issues:
        issues.append(
            issue(
                "remaining_blocker_without_operator_rows",
                "A matching operator workbench sprint has no actionable selected rows.",
                blockers=row_issues,
            )
        )
    if human_contract_issues:
        issues.append(
            issue(
                "remaining_blocker_human_decision_contract_issue",
                "A human-gated blocker has an operator sprint without required human decision fields or decision options.",
                blockers=human_contract_issues,
            )
        )
    if qualification_guard_issues:
        issues.append(
            issue(
                "qualification_guard_contract_issue",
                "Qualification collection blockers must be explicitly guarded_manual_ready with operator timing and guarded collection evidence before being marked operator-timed.",
                blockers=qualification_guard_issues,
            )
        )
    if workbench_reported_row_count != workbench_selected_row_count:
        issues.append(
            issue(
                "operator_workbench_summary_row_count_mismatch",
                "Operator workbench summary row count does not match the selected sprint row total.",
                reported_workbench_row_count=workbench_summary.get("workbench_row_count"),
                recomputed_selected_row_count=workbench_selected_row_count,
            )
        )
    if selected_subset_sprints:
        warnings.append(
            issue(
                "operator_workbench_selected_subset",
                "Operator workbench row counts are selected first-pass slices; source artifacts contain additional rows.",
                selected_sprint_count=len(selected_subset_sprints),
                selected_row_count=workbench_selected_row_count,
                source_total_row_count=workbench_source_total_row_count,
                unselected_source_row_count=workbench_unselected_source_row_count,
                sprints=[
                    {
                        "sprint_id": sprint.get("sprint_id"),
                        "selected_row_count": sprint.get("selected_row_count"),
                        "source_total_row_count": sprint.get("source_total_row_count"),
                        "unselected_source_row_count": sprint.get("unselected_source_row_count"),
                    }
                    for sprint in selected_subset_sprints
                ],
            )
        )

    notes = [
        "This addendum is report-only and reads already-generated artifacts; it does not recalculate release readiness.",
        "A pass means every remaining blocker has a safe operator-facing workbench entry, not that the blocker is approved or closed.",
        "Operator row counts distinguish selected workbench rows from total source rows when the workbench intentionally presents a first-pass slice.",
        "human_reviewed, accepted, and reviewed statuses remain forbidden unless a human decision is imported through a guarded process.",
        "Qualification coverage remains guarded and operator-timed; this addendum does not authorize API collection.",
        "Terminal evidence index is intentionally excluded from the release refresh DAG and operator handoff inputs.",
    ]

    summary = {
        "release_ready": release.get("release_ready"),
        "release_blocker_count": len(release_blockers),
        "remaining_blocker_count": len(remaining_blockers),
        "covered_remaining_blocker_count": sum(
            1 for item in blocker_statuses if item.get("operator_ready_contract_ok") is True
        ),
        "uncovered_remaining_blocker_count": len(uncovered),
        "operator_scope_issue_count": len(scope_issues),
        "operator_row_issue_count": len(row_issues),
        "human_decision_contract_issue_count": len(human_contract_issues),
        "qualification_guard_contract_issue_count": len(qualification_guard_issues),
        "workbench_sprint_count": workbench_summary.get("sprint_count"),
        "workbench_row_count": workbench_summary.get("workbench_row_count"),
        "workbench_summary_row_count_matches_selected": (
            workbench_reported_row_count == workbench_selected_row_count
        ),
        "workbench_selected_row_count": workbench_selected_row_count,
        "workbench_source_total_row_count": workbench_source_total_row_count,
        "workbench_unselected_source_row_count": workbench_unselected_source_row_count,
        "workbench_selected_subset_sprint_count": len(selected_subset_sprints),
        "terminal_artifact_count": terminal_summary.get("artifact_count"),
        "terminal_issue_count": terminal_summary.get("issue_count"),
        "terminal_warning_count": terminal_summary.get("warning_count"),
        "terminal_warning_codes": terminal_warning_codes,
        "terminal_warning_source_count": len(terminal_warning_sources),
        "embedded_source_hash_mismatch_count": sum(
            len(items) for items in embedded_hash_mismatches.values()
        ),
        "goal_open_requirement_count": goal.get("open_requirement_count"),
        "goal_verified_requirement_count": goal.get("verified_requirement_count"),
    }
    status = "pass" if not issues else "fail"
    summary["issue_count"] = len(issues)
    summary["warning_count"] = len(warnings)

    return {
        "schema": SCHEMA,
        "generated_at": generated_at or now_iso(),
        "ok": not issues,
        "status": status,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_artifacts": source_artifacts,
        "source_contracts": source_contracts,
        "embedded_source_hash_mismatches": embedded_hash_mismatches,
        "terminal_cycle_contract": terminal_contract,
        "summary": summary,
        "goal_completion_alignment": {
            "objective": goal.get("objective"),
            "release_ready": goal.get("release_ready"),
            "open_requirement_count": goal.get("open_requirement_count"),
            "verified_requirement_count": goal.get("verified_requirement_count"),
        },
        "terminal_evidence": {
            "ok": terminal_payload.get("ok"),
            "status": terminal_payload.get("status"),
            "terminal_evidence_only": terminal_payload.get("terminal_evidence_only"),
            "include_in_release_refresh_dag": terminal_payload.get(
                "include_in_release_refresh_dag"
            ),
            "include_in_operator_handoff": terminal_payload.get("include_in_operator_handoff"),
            "summary": terminal_summary,
            "warning_codes": terminal_warning_codes,
            "warning_sources": terminal_warning_sources,
            "warnings": terminal_warnings,
        },
        "blocker_operator_status": blocker_statuses,
        "issues": issues,
        "warnings": warnings,
        "notes": notes,
    }


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Release Blocker Operator Addendum",
        "",
        f"- schema: `{payload.get('schema')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- status: `{payload.get('status')}`",
        f"- ok: `{payload.get('ok')}`",
        f"- report_only: `{payload.get('report_only')}`",
        f"- status_update_allowed: `{payload.get('status_update_allowed')}`",
        f"- db_writes: `{payload.get('db_writes')}`",
        f"- api_calls: `{payload.get('api_calls')}`",
        f"- approval_claim: `{payload.get('approval_claim')}`",
        f"- acceptance_claim: `{payload.get('acceptance_claim')}`",
        f"- human_decision_required: `{payload.get('human_decision_required')}`",
        "",
        "## Summary",
        "",
    ]
    for key in (
        "release_ready",
        "release_blocker_count",
        "remaining_blocker_count",
        "covered_remaining_blocker_count",
        "uncovered_remaining_blocker_count",
        "operator_scope_issue_count",
        "operator_row_issue_count",
        "human_decision_contract_issue_count",
        "qualification_guard_contract_issue_count",
        "workbench_sprint_count",
        "workbench_row_count",
        "workbench_summary_row_count_matches_selected",
        "workbench_selected_row_count",
        "workbench_source_total_row_count",
        "workbench_unselected_source_row_count",
        "workbench_selected_subset_sprint_count",
        "terminal_artifact_count",
        "terminal_issue_count",
        "terminal_warning_count",
        "terminal_warning_codes",
        "terminal_warning_source_count",
        "embedded_source_hash_mismatch_count",
        "goal_open_requirement_count",
        "goal_verified_requirement_count",
        "issue_count",
        "warning_count",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Blocker Operator Status", ""])
    for blocker in payload.get("blocker_operator_status") or []:
        lines.append(f"### {blocker.get('name')}")
        lines.append(f"- category: `{blocker.get('category')}`")
        lines.append(f"- remaining_status: `{blocker.get('remaining_status')}`")
        lines.append(f"- operator_readiness: `{blocker.get('operator_readiness')}`")
        lines.append(f"- operator_sprint_count: `{blocker.get('operator_sprint_count')}`")
        lines.append(f"- operator_row_count: `{blocker.get('operator_row_count')}`")
        lines.append(f"- operator_row_count_meaning: `{blocker.get('operator_row_count_meaning')}`")
        lines.append(f"- selected_operator_row_count: `{blocker.get('selected_operator_row_count')}`")
        lines.append(
            f"- operator_source_total_row_count: `{blocker.get('operator_source_total_row_count')}`"
        )
        lines.append(
            f"- operator_unselected_source_row_count: `{blocker.get('operator_unselected_source_row_count')}`"
        )
        lines.append(f"- operator_has_rows: `{blocker.get('operator_has_rows')}`")
        lines.append(
            f"- human_decision_contract_ok: `{blocker.get('human_decision_contract_ok')}`"
        )
        lines.append(
            f"- qualification_guard_contract_ok: `{blocker.get('qualification_guard_contract_ok')}`"
        )
        lines.append(f"- next_safe_action: `{blocker.get('next_safe_action')}`")
        for sprint in blocker.get("operator_sprints") or []:
            lines.append(
                f"  - sprint `{sprint.get('sprint_id')}` "
                f"selected_rows=`{sprint.get('selected_row_count')}` "
                f"source_total_rows=`{sprint.get('source_total_row_count')}` "
                f"unselected_source_rows=`{sprint.get('unselected_source_row_count')}` "
                f"scope_ok=`{sprint.get('scope_match_ok')}` "
                f"next_safe_action=`{sprint.get('next_safe_action')}` "
                f"open_first=`{sprint.get('open_first')}`"
            )
        lines.append("")
    terminal = payload.get("terminal_evidence") if isinstance(payload.get("terminal_evidence"), dict) else {}
    lines.extend(
        [
            "## Terminal Evidence",
            "",
            f"- ok: `{terminal.get('ok')}`",
            f"- status: `{terminal.get('status')}`",
            f"- terminal_evidence_only: `{terminal.get('terminal_evidence_only')}`",
            f"- include_in_release_refresh_dag: `{terminal.get('include_in_release_refresh_dag')}`",
            f"- include_in_operator_handoff: `{terminal.get('include_in_operator_handoff')}`",
            f"- warning_codes: `{terminal.get('warning_codes')}`",
            "",
        ]
    )
    for warning in terminal.get("warning_sources") or []:
        lines.append(
            f"- warning_source `{warning.get('label')}` code=`{warning.get('code')}` "
            f"source_codes=`{warning.get('source_warning_code_counts')}`"
        )
    if terminal.get("warning_sources"):
        lines.append("")
    if payload.get("issues"):
        lines.extend(["## Issues", ""])
        for item in payload.get("issues") or []:
            lines.append(f"- `{item.get('code')}`: {item.get('message')}")
    else:
        lines.append("No release blocker addendum issues found.")
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for item in payload.get("warnings") or []:
            lines.append(f"- `{item.get('code')}`: {item.get('message')}")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def default_report(name: str, stamp: str, *, root: Path = PROJECT_ROOT) -> Path:
    return root / "reports" / name.format(stamp=stamp)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a report-only addendum that maps remaining AI-HR blockers to operator workbench evidence."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--stamp", default="20260712_10h")
    parser.add_argument("--release-readiness", type=Path)
    parser.add_argument("--remaining-blockers", type=Path)
    parser.add_argument("--goal-completion-audit", type=Path)
    parser.add_argument("--operator-workbench", type=Path)
    parser.add_argument("--terminal-evidence-index", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    stamp = args.stamp
    report = build_addendum(
        release_readiness_path=resolve_artifact(
            args.release_readiness
            or default_report("aihr_release_readiness_{stamp}.json", stamp, root=args.root),
            root=args.root,
        )
        or default_report("aihr_release_readiness_{stamp}.json", stamp, root=args.root),
        remaining_blockers_path=resolve_artifact(
            args.remaining_blockers
            or default_report("remaining_blockers_{stamp}.json", stamp, root=args.root),
            root=args.root,
        )
        or default_report("remaining_blockers_{stamp}.json", stamp, root=args.root),
        goal_completion_audit_path=resolve_artifact(
            args.goal_completion_audit
            or default_report("goal_completion_audit_{stamp}.json", stamp, root=args.root),
            root=args.root,
        )
        or default_report("goal_completion_audit_{stamp}.json", stamp, root=args.root),
        operator_workbench_path=resolve_artifact(
            args.operator_workbench
            or default_report("aihr_operator_decision_workbench_{stamp}.json", stamp, root=args.root),
            root=args.root,
        )
        or default_report("aihr_operator_decision_workbench_{stamp}.json", stamp, root=args.root),
        terminal_evidence_index_path=resolve_artifact(
            args.terminal_evidence_index
            or default_report("aihr_terminal_evidence_index_{stamp}.json", stamp, root=args.root),
            root=args.root,
        )
        or default_report("aihr_terminal_evidence_index_{stamp}.json", stamp, root=args.root),
        root=args.root,
    )
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "status": report.get("status"),
                "remaining_blocker_count": report.get("summary", {}).get(
                    "remaining_blocker_count"
                ),
                "covered_remaining_blocker_count": report.get("summary", {}).get(
                    "covered_remaining_blocker_count"
                ),
                "issue_count": report.get("summary", {}).get("issue_count"),
                "warning_count": report.get("summary", {}).get("warning_count"),
                "out_path": str(args.out),
                "markdown_path": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and report.get("ok") is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())
