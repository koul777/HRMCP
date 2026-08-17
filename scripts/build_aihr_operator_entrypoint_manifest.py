from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_operator_entrypoint_manifest_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
DECISION_FIELDS = (
    "decision",
    "approved_definition",
    "reviewer_id",
    "reviewed_at",
    "rationale",
    "source_decision_packet",
    "evidence_refs_json",
)
GUARD_FALSE_FIELDS = ("status_update_allowed", "db_writes", "approval_claim")


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


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


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


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), [dict(row) for row in reader]


def is_false(value: Any) -> bool:
    return value is False or str(value).strip().lower() == "false"


def field_like(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value or ""))


def artifact_status(path: str | Path | None, *, root: Path) -> dict[str, Any]:
    resolved = resolve_artifact(path, root=root)
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
        ),
        "sha256": sha256_file(resolved),
    }


def source_hash_checks(
    source_paths: dict[str, Path],
    source_status: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "check_scope": "current_generation_snapshot_only",
            "lineage_validation": False,
            "path": source_status[key].get("path"),
            "expected_sha256": source_status[key].get("sha256"),
            "actual_sha256": sha256_file(path),
            "hash_matches": bool(
                source_status[key].get("sha256")
                and source_status[key].get("sha256") == sha256_file(path)
            ),
        }
        for key, path in source_paths.items()
        if key in source_status
    }


def safe_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true": payload.get("human_decision_required") is True,
    }


def next_actions_source_hash_checks(
    payload: dict[str, Any],
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    paths = payload.get("source_paths") if isinstance(payload.get("source_paths"), dict) else {}
    hashes = payload.get("source_hashes") if isinstance(payload.get("source_hashes"), dict) else {}
    scopes = (
        payload.get("source_hash_scopes")
        if isinstance(payload.get("source_hash_scopes"), dict)
        else {}
    )
    checks: dict[str, dict[str, Any]] = {}
    for key, value in paths.items():
        if not value:
            continue
        resolved = resolve_artifact(value, root=root)
        scope = scopes.get(key)
        actual = sha256_artifact(resolved, scope=scope)
        expected = hashes.get(key)
        checks[str(key)] = {
            "path": portable_path(value, root=root),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "sha256_scope": scope,
            "hash_matches": bool(expected and actual and expected == actual),
        }
    return checks


def add_issue(issues: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    issues.append({"code": code, "message": message, **extra})


def bad_claims_for_markdown(path: str | Path | None, *, root: Path) -> list[str]:
    resolved = resolve_artifact(path, root=root)
    if not resolved or not resolved.exists() or resolved.suffix.lower() != ".md":
        return []
    bad: list[str] = []
    status_token = re.compile(r"(?<![A-Za-z0-9_])(human_reviewed|accepted|reviewed)(?![A-Za-z0-9_])")
    explicit_true_flag = re.compile(
        r"(?<![A-Za-z0-9_])(human_reviewed|accepted|reviewed)(?![A-Za-z0-9_])"
        r"\s*[:=]\s*`?(true|yes|1)`?",
    )
    status_assignment = re.compile(
        r"\b(status|review_status|decision|claim|approval)\b"
        r"[^.\n]{0,80}?(?:\b(?:to|as|is|was)\b|=|:)"
        r"[^.\n]{0,40}?"
        r"(?<![A-Za-z0-9_])(human_reviewed|accepted|reviewed)(?![A-Za-z0-9_])",
    )
    passive_claim = re.compile(
        r"\b(was|were|is|are|has been|have been)\s+"
        r"(?<![A-Za-z0-9_])(human_reviewed|accepted|reviewed)(?![A-Za-z0-9_])",
    )
    safe_patterns = (
        re.compile(r"\b(decision[_ -]?options|allowed values|choices)\b"),
        re.compile(r"\b(forbidden|must not|do not|does not|not a|not an|no approval|no acceptance)\b"),
        re.compile(r"\b(approval_claim|acceptance_claim|db_writes|status_update_allowed)\b.*\bfalse\b"),
        re.compile(r"\b(human_decision_required|required_human_fields|reviewed_at)\b"),
        re.compile(r"\b(manual|operator|human)\b[^.\n]{0,80}\brequired\b[^.\n]{0,80}\bbefore\b"),
        re.compile(r"\b(blank|pending|still zero|until_guarded_apply|invalid_reviewed_at_count)\b"),
    )
    for line in resolved.read_text(encoding="utf-8").splitlines():
        lowered = line.lower()
        if not status_token.search(lowered):
            continue
        if any(pattern.search(lowered) for pattern in safe_patterns):
            continue
        if (
            explicit_true_flag.search(lowered)
            or status_assignment.search(lowered)
            or passive_claim.search(lowered)
        ):
            bad.append(line.strip()[:240])
    return bad


def csv_summary(path: Path | None, *, root: Path) -> dict[str, Any]:
    status = artifact_status(path, root=root)
    if not path or not path.exists() or not path.is_file():
        return {**status, "fieldnames": [], "row_count": 0}
    fieldnames, rows = read_csv(path)
    decision_fields = [field for field in DECISION_FIELDS if field in fieldnames]
    guard_fields = [field for field in GUARD_FALSE_FIELDS if field in fieldnames]
    nonblank_decision_counts: dict[str, int] = {}
    for row in rows:
        for field in decision_fields:
            if str(row.get(field) or "").strip():
                nonblank_decision_counts[field] = nonblank_decision_counts.get(field, 0) + 1
    guard_false_ok = all(is_false(row.get(field)) for row in rows for field in guard_fields)
    nonfalse_guard_cell_count = sum(
        1
        for row in rows
        for field in guard_fields
        if not is_false(row.get(field))
    )
    return {
        **status,
        "fieldnames": fieldnames,
        "row_count": len(rows),
        "decision_fields": decision_fields,
        "guard_fields": guard_fields,
        "nonblank_decision_counts": nonblank_decision_counts,
        "nonblank_decision_cell_count": sum(nonblank_decision_counts.values()),
        "decision_fields_blank_ok": not nonblank_decision_counts,
        "guard_fields_false_ok": guard_false_ok,
        "nonfalse_guard_cell_count": nonfalse_guard_cell_count,
    }


def entry_kind(open_first: str | None) -> str:
    text = str(open_first or "").lower()
    if "transition_provenance_operator_crosswalk" in text:
        return "crosswalk_map"
    if "qualification_guarded_batch_operator_decision" in text:
        return "guarded_api_timing_surface"
    if text.endswith(".csv"):
        return "csv_decision_surface"
    return "read_only_context"


def candidate_csv_paths(entry: dict[str, Any]) -> list[str]:
    paths = [str(entry.get("open_first") or "")]
    paths.extend(str(item or "") for item in (entry.get("artifacts_to_open") or []))
    seen: set[str] = set()
    ordered: list[str] = []
    for path in paths:
        if not path.lower().partition("#")[0].endswith(".csv"):
            continue
        key = strip_fragment(path).lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def find_decision_surface(
    entry: dict[str, Any],
    *,
    root: Path,
) -> dict[str, Any]:
    required_fields = [
        str(field)
        for field in (entry.get("required_human_fields") or [])
        if field_like(str(field))
    ]
    candidates: list[dict[str, Any]] = []
    for value in candidate_csv_paths(entry):
        path = resolve_artifact(value, root=root)
        summary = csv_summary(path, root=root)
        summary["required_fields_present"] = all(
            field in summary.get("fieldnames", []) for field in required_fields
        )
        candidates.append(summary)
    if not required_fields:
        return {
            "path": None,
            "required_fields": [],
            "required_fields_present": None,
            "candidates": candidates,
            "status": "not_applicable",
        }
    for item in candidates:
        if item.get("required_fields_present"):
            return {
                **item,
                "required_fields": required_fields,
                "candidates": candidates,
                "status": "found",
            }
    return {
        "path": None,
        "required_fields": required_fields,
        "required_fields_present": False,
        "candidates": candidates,
        "status": "missing",
    }


def normalize_entries(payload: dict[str, Any], *, source: str) -> list[dict[str, Any]]:
    key = "queue" if source == "sprint_queue" else "actions"
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(payload.get(key) or [], start=1):
        if not isinstance(item, dict):
            continue
        human_decision_required = item.get("human_decision_required")
        if human_decision_required is None:
            human_decision_required = payload.get("human_decision_required", True)
        status_update_allowed = item.get("status_update_allowed")
        if status_update_allowed is None:
            status_update_allowed = payload.get("status_update_allowed", False)
        db_writes = item.get("db_writes")
        if db_writes is None:
            db_writes = payload.get("db_writes", False)
        api_calls = item.get("api_calls")
        if api_calls is None:
            api_calls = payload.get("api_calls", False)
        approval_claim = item.get("approval_claim")
        if approval_claim is None:
            approval_claim = payload.get("approval_claim", False)
        entry_id = str(item.get("id") or item.get("sprint_id") or item.get("blocker") or index)
        entries.append(
            {
                "source": source,
                "index": index,
                "id": entry_id,
                "blocker": item.get("blocker"),
                "rank": item.get("rank"),
                "open_first": item.get("open_first"),
                "artifacts_to_open": item.get("artifacts_to_open") or [],
                "required_human_fields": item.get("required_human_fields") or [],
                "decision_options": item.get("decision_options") or [],
                "row_selector": item.get("row_selector"),
                "row_count": item.get("row_count"),
                "forbidden": item.get("forbidden") or [],
                "human_decision_required": human_decision_required,
                "status_update_allowed": status_update_allowed,
                "db_writes": db_writes,
                "api_calls": api_calls,
                "approval_claim": approval_claim,
            }
        )
    return entries


def build_manifest(
    *,
    next_actions_path: Path,
    sprint_queue_path: Path,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    next_actions = read_json(next_actions_path)
    sprint_queue = read_json(sprint_queue_path)
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    source_paths = {
        "operator_next_actions": next_actions_path,
        "blocker_reduction_sprint_queue": sprint_queue_path,
    }
    source_status = {
        key: artifact_status(path, root=root)
        for key, path in source_paths.items()
    }
    for key, status in source_status.items():
        if not status["exists_nonempty"]:
            add_issue(issues, "source_artifact_missing", "Source artifact is missing.", source=key)

    source_contracts = {
        "operator_next_actions": safe_contract(next_actions),
        "blocker_reduction_sprint_queue": safe_contract(sprint_queue),
    }
    top_source_hash_checks = source_hash_checks(source_paths, source_status)
    next_source_hash_checks = next_actions_source_hash_checks(next_actions, root=root)
    for key, check in next_source_hash_checks.items():
        if check.get("hash_matches") is not True:
            add_issue(
                issues,
                "next_actions_source_hash_mismatch",
                "Next-actions source hash is stale or missing.",
                source_key=key,
                check=check,
            )
    for key, contract in source_contracts.items():
        if not all(contract.values()):
            add_issue(
                issues,
                "unsafe_source_contract",
                "Source does not preserve the report-only safety contract.",
                source=key,
                contract=contract,
            )

    entries = normalize_entries(next_actions, source="next_actions") + normalize_entries(
        sprint_queue,
        source="sprint_queue",
    )
    entry_records: list[dict[str, Any]] = []
    for entry in entries:
        open_status = artifact_status(entry.get("open_first"), root=root)
        artifact_statuses = [
            artifact_status(path, root=root) for path in entry.get("artifacts_to_open") or []
        ]
        kind = entry_kind(entry.get("open_first"))
        surface = find_decision_surface(entry, root=root)
        entry_issues: list[str] = []
        if not open_status["exists_nonempty"]:
            entry_issues.append("open_first_missing")
            add_issue(
                issues,
                "entry_open_first_missing",
                "Operator entrypoint open_first artifact is missing.",
                entry_id=entry["id"],
                path=entry.get("open_first"),
            )
        missing_artifacts = [
            status for status in artifact_statuses if status.get("exists_nonempty") is not True
        ]
        if missing_artifacts:
            entry_issues.append("artifact_missing")
            add_issue(
                issues,
                "entry_artifact_missing",
                "One or more operator entry artifacts are missing.",
                entry_id=entry["id"],
                artifacts=missing_artifacts,
            )
        if surface.get("status") == "missing" and kind != "guarded_api_timing_surface":
            entry_issues.append("decision_surface_missing")
            add_issue(
                issues,
                "entry_decision_surface_missing",
                "No CSV decision surface contains the required human fields.",
                entry_id=entry["id"],
                required_fields=surface.get("required_fields"),
            )
        if surface.get("status") == "found":
            if surface.get("decision_fields_blank_ok") is not True:
                entry_issues.append("decision_fields_not_blank")
                add_issue(
                    issues,
                    "entry_decision_fields_not_blank",
                    "Decision surface has prefilled human decision fields.",
                    entry_id=entry["id"],
                    path=surface.get("path"),
                    nonblank_counts=surface.get("nonblank_decision_counts"),
                )
            if surface.get("guard_fields") and surface.get("guard_fields_false_ok") is not True:
                entry_issues.append("guard_fields_not_false")
                add_issue(
                    issues,
                    "entry_guard_fields_not_false",
                    "Decision surface guard fields are not all false.",
                    entry_id=entry["id"],
                    path=surface.get("path"),
                )
            if not surface.get("guard_fields") and kind != "crosswalk_map":
                warnings.append(
                    {
                        "code": "entry_decision_surface_without_guard_fields",
                        "entry_id": entry["id"],
                        "path": surface.get("path"),
                    }
                )
        if entry.get("human_decision_required") is not True:
            entry_issues.append("human_decision_required_not_true")
            add_issue(
                issues,
                "entry_human_decision_required_not_true",
                "Operator entry must keep human_decision_required=true.",
                entry_id=entry["id"],
            )
        for guard_field in ("status_update_allowed", "db_writes", "api_calls", "approval_claim"):
            if entry.get(guard_field) is not False:
                entry_issues.append(f"{guard_field}_not_false")
                add_issue(
                    issues,
                    "entry_guard_flag_not_false",
                    "Operator entry guard flag must be false.",
                    entry_id=entry["id"],
                    field=guard_field,
                    value=entry.get(guard_field),
                )
        md_claim_findings = [
            {"path": status["path"], "bad_claims": bad_claims_for_markdown(status["path"], root=root)}
            for status in artifact_statuses
            if status.get("path") and str(status.get("path")).lower().endswith(".md")
        ]
        md_claim_findings = [item for item in md_claim_findings if item["bad_claims"]]
        if md_claim_findings:
            entry_issues.append("markdown_bad_claims")
            add_issue(
                issues,
                "entry_markdown_bad_claims",
                "Markdown entry artifacts contain affirmative review-status claims.",
                entry_id=entry["id"],
                findings=md_claim_findings,
            )
        record = {
            **entry,
            "kind": kind,
            "open_first_status": open_status,
            "artifact_statuses": artifact_statuses,
            "all_artifacts_exist": all(
                status.get("exists_nonempty") is True for status in artifact_statuses
            ),
            "decision_surface": surface,
            "row_count_declared": entry.get("row_count"),
            "row_count_actual": surface.get("row_count"),
            "markdown_claim_scan_ok": not md_claim_findings,
            "markdown_bad_claims": md_claim_findings,
            "entry_ok": not entry_issues,
            "entry_issues": entry_issues,
        }
        entry_records.append(record)

    unique_open_first = sorted(
        {str(entry.get("open_first") or "") for entry in entries if entry.get("open_first")}
    )
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "ok": not issues,
        "status": "pass" if not issues else "review_required",
        "terminal_evidence_only": True,
        "include_in_release_refresh_dag": False,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_paths": {key: portable_path(path, root=root) for key, path in source_paths.items()},
        "source_hashes": {key: status.get("sha256") for key, status in source_status.items()},
        "source_hash_check_scope": {
            "source_hash_checks": "current_generation_snapshot_only",
            "lineage_validation": False,
            "lineage_validation_sources": ["next_actions_source_hash_checks"],
        },
        "source_hash_checks": top_source_hash_checks,
        "next_actions_source_hash_checks": next_source_hash_checks,
        "source_status": source_status,
        "source_contracts": source_contracts,
        "cycle_avoidance_contract": {
            "terminal_evidence_only": True,
            "context_only_keys": [
                "operator_next_actions",
                "lineage_sync_audit",
                "operator_packet_integrity_audit",
            ],
            "source_hashes_exclude_context_only_keys": True,
            "must_not_be_source_for": [
                "operator_next_actions",
                "blocker_reduction_sprint_queue",
                "operator_handoff",
                "release_operator_refresh_dag",
            ],
        },
        "summary": {
            "next_action_count": len(normalize_entries(next_actions, source="next_actions")),
            "sprint_entry_count": len(normalize_entries(sprint_queue, source="sprint_queue")),
            "entry_count": len(entry_records),
            "entry_ok_count": sum(1 for item in entry_records if item["entry_ok"]),
            "unique_open_first_count": len(unique_open_first),
            "csv_decision_surface_count": sum(
                1 for item in entry_records if item["decision_surface"].get("status") == "found"
            ),
            "guarded_api_timing_surface_count": sum(
                1 for item in entry_records if item["kind"] == "guarded_api_timing_surface"
            ),
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "unique_open_first": unique_open_first,
        "entries": entry_records,
        "issues": issues,
        "warnings": warnings,
        "notes": [
            "This manifest validates operator entrypoints only; it does not authorize DB writes, API calls, or status updates.",
            "Crosswalk CSVs are treated as packet-evidence maps; the manifest locates the actual decision sheet CSV when required fields are listed.",
            "Guarded API timing surfaces require a separate operator decision and are not treated as blank decision CSVs.",
            "source_hash_checks are same-run snapshot checks; upstream lineage validation is limited to declared next-actions source hashes.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Operator Entrypoint Manifest",
        "",
        f"- schema: `{payload.get('schema')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- status: `{payload.get('status')}`",
        f"- ok: `{payload.get('ok')}`",
        f"- terminal_evidence_only: `{payload.get('terminal_evidence_only')}`",
        f"- include_in_release_refresh_dag: `{payload.get('include_in_release_refresh_dag')}`",
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
        "next_action_count",
        "sprint_entry_count",
        "entry_count",
        "entry_ok_count",
        "unique_open_first_count",
        "csv_decision_surface_count",
        "guarded_api_timing_surface_count",
        "issue_count",
        "warning_count",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Entries", ""])
    for entry in payload.get("entries") or []:
        surface = entry.get("decision_surface") if isinstance(entry.get("decision_surface"), dict) else {}
        lines.append(
            "- "
            f"{entry.get('source')}::{entry.get('id')} "
            f"kind=`{entry.get('kind')}` entry_ok=`{entry.get('entry_ok')}`"
        )
        lines.append(f"  - open_first: `{entry.get('open_first')}`")
        lines.append(
            f"  - decision_surface: `{surface.get('path')}` status=`{surface.get('status')}`"
        )
    if payload.get("issues"):
        lines.extend(["", "## Issues", ""])
        for issue in payload.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: {issue.get('message')}")
    else:
        lines.extend(["", "No entrypoint manifest issues found."])
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in payload.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: `{warning}`")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build terminal AI-HR operator entrypoint manifest.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--next-actions", type=Path, required=True)
    parser.add_argument("--sprint-queue", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_manifest(
        next_actions_path=resolve_artifact(args.next_actions, root=args.root) or args.next_actions,
        sprint_queue_path=resolve_artifact(args.sprint_queue, root=args.root) or args.sprint_queue,
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
                "entry_count": report.get("summary", {}).get("entry_count"),
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
