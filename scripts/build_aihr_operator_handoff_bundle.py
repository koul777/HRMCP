from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
NEXT_ACTION_SCHEMA = "aihr_operator_next_actions_v3"
HANDOFF_SCHEMA = "overnight_10h_operator_handoff_v3"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
NEXT_ACTION_SOURCE_KEYS = (
    "release_readiness",
    "queue_run",
    "transition_trusted_scenario_provenance_gap",
    "qualification_guarded_batch_operator_decision",
    "provenance_reconfirmation_proofset_log",
    "blocker_reduction_sprint_queue",
    "blocker_reduction_sprint_queue_audit",
    "transition_provenance_crosswalk",
    "transition_provenance_crosswalk_csv",
    "transition_provenance_crosswalk_audit",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_fragment(value: str | Path | None) -> str:
    return str(value or "").partition("#")[0].strip()


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def declared_hash_scope(path: Path | None) -> str | None:
    if path is None or path.suffix.lower() != ".json" or not path.is_file():
        return None
    try:
        payload = read_json(path)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    scope = str(payload.get("sha256_scope") or "").strip()
    scoped_hash = str(payload.get("cycle_safe_content_sha256") or "").strip()
    if scope == "cycle_safe_release_readiness" and re.fullmatch(
        r"sha256:[0-9a-f]{64}", scoped_hash
    ):
        return scope
    return None


def sha256_artifact(path: Path | None, *, scope: str | None = None) -> str | None:
    if scope == "cycle_safe_release_readiness" and path is not None:
        try:
            payload = read_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if payload.get("sha256_scope") != scope:
            return None
        value = str(payload.get("cycle_safe_content_sha256") or "").strip()
        return value if re.fullmatch(r"sha256:[0-9a-f]{64}", value) else None
    return sha256_file(path)


def portable_path(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> str | None:
    if path is None:
        return None
    text = strip_fragment(path)
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def resolve_artifact(value: str | Path | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    text = strip_fragment(value)
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return root / path


def artifact_status(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    resolved = resolve_artifact(path, root=root)
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
        ),
        "sha256": sha256_file(resolved),
    }


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def dated_artifact_sort_key(path: Path) -> tuple[int, str, float]:
    match = re.search(r"(\d{8}(?:_\w+)?)", path.stem)
    stamp = match.group(1) if match else ""
    date = int(stamp[:8]) if stamp[:8].isdigit() else 0
    return date, stamp, path.stat().st_mtime


def latest_report_path(
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            name = path.name
            if any(token in name for token in exclude_substrings):
                continue
            candidates.append(path)
            seen.add(path)
    if not candidates:
        raise FileNotFoundError(f"no report artifact matched: {patterns}")
    return max(candidates, key=dated_artifact_sort_key)


def input_or_latest(
    value: Path | None,
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
) -> Path:
    return value if value is not None else latest_report_path(
        *patterns,
        reports_dir=reports_dir,
        exclude_substrings=exclude_substrings,
    )


def stamp_from_path(path: Path, *, fallback: str = "20260712_10h") -> str:
    match = re.search(r"(\d{8}(?:_\w+)?)", path.stem)
    return match.group(1) if match else fallback


def report_path(name: str, stamp: str, suffix: str = "json") -> str:
    return f"reports/{name}_{stamp}.{suffix}"


def source_hashes(source_paths: dict[str, str | None], *, root: Path) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for key, value in source_paths.items():
        path = resolve_artifact(value, root=root)
        hashes[key] = sha256_artifact(path, scope=declared_hash_scope(path))
    return hashes


def source_hash_scopes(source_paths: dict[str, str | None], *, root: Path) -> dict[str, str]:
    scopes: dict[str, str] = {}
    for key, value in source_paths.items():
        scope = declared_hash_scope(resolve_artifact(value, root=root))
        if scope:
            scopes[key] = scope
    return scopes


def release_blocker_map(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    blockers: dict[str, dict[str, Any]] = {}
    for blocker in release.get("blockers") or []:
        if not isinstance(blocker, dict):
            continue
        blocker_id = str(blocker.get("name") or blocker.get("id") or "").strip()
        if blocker_id:
            blockers[blocker_id] = blocker
    return blockers


def release_next_action_map(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    actions: dict[str, dict[str, Any]] = {}
    for action in release.get("next_actions") or []:
        if not isinstance(action, dict):
            continue
        blocker_id = str(action.get("blocker") or "").strip()
        if blocker_id:
            actions[blocker_id] = action
    return actions


def action_artifact_paths(
    *,
    blocker_id: str,
    stamp: str,
    transition_crosswalk_csv: Path,
    transition_crosswalk_audit_json: Path,
    transition_gap_json: Path,
    qualification_decision_json: Path,
    provenance_decision_sheet_json: Path,
) -> tuple[str, list[str], str | None, str]:
    transition_gap_csv = transition_gap_json.with_suffix(".csv")
    qualification_csv = qualification_decision_json.with_suffix(".csv")
    qualification_md = qualification_decision_json.with_suffix(".md")
    provenance_decision_csv = provenance_decision_sheet_json.with_suffix(".csv")
    provenance_packet_md = report_path("human_review_provenance_reconfirmation_packet", stamp, "md")
    provenance_audit_md = report_path(
        "human_review_provenance_reconfirmation_decision_audit",
        stamp,
        "md",
    )

    mapping: dict[str, tuple[str, list[str], str | None, str]] = {
        "review_debt:human_reviewed_concepts": (
            report_path("aihr_ontology_definition_review_seedpack", stamp, "csv"),
            [
                report_path("aihr_ontology_definition_review_seedpack", stamp, "csv"),
                report_path("aihr_ontology_definition_review_seedpack", stamp, "md"),
                report_path("aihr_ontology_definition_review_seedpack", stamp, "jsonl"),
            ],
            None,
            (
                "Review high-priority ontology concept definition rows; keep decisions blank "
                "until a human reviewer fills the CSV."
            ),
        ),
        "review_debt:human_reviewed_goal_links": (
            report_path("aihr_review_seedpack_blocker_ranked", stamp, "csv"),
            [
                report_path("aihr_review_seedpack_blocker_ranked", stamp, "csv"),
                report_path("aihr_review_seedpack_blocker_ranked", stamp, "md"),
                report_path("aihr_review_triage", stamp, "md"),
                report_path("aihr_review_triage", stamp, "json"),
                report_path("aihr_transition_scenario_seedpack", stamp, "md"),
                report_path("aihr_transition_scenario_seedpack", stamp, "jsonl"),
            ],
            None,
            (
                "Review training-goal to KSA link evidence surfaced by triage; do not promote "
                "review statuses automatically."
            ),
        ),
        "review_debt:human_reviewed_task_relations": (
            report_path("aihr_review_seedpack_blocker_ranked", stamp, "csv"),
            [
                report_path("aihr_review_seedpack_blocker_ranked", stamp, "csv"),
                report_path("aihr_review_seedpack_blocker_ranked", stamp, "md"),
                report_path("aihr_review_seedpack_blocker_ranked", stamp, "jsonl"),
            ],
            None,
            (
                "Review blocker-ranked task/KSA rows in the CSV decision surface; no status "
                "promotion without human decision."
            ),
        ),
        "qualification:collection_coverage": (
            portable_path(qualification_csv, root=PROJECT_ROOT) or str(qualification_csv),
            [
                portable_path(qualification_csv, root=PROJECT_ROOT) or str(qualification_csv),
                portable_path(qualification_md, root=PROJECT_ROOT) or str(qualification_md),
                portable_path(qualification_decision_json, root=PROJECT_ROOT)
                or str(qualification_decision_json),
                report_path("qualification_retry_hygiene", stamp, "md"),
                report_path("qualification_collection_coverage_plan", stamp, "md"),
                report_path("qualification_collection_coverage_plan", stamp, "csv"),
            ],
            portable_path(qualification_decision_json, root=PROJECT_ROOT)
            or str(qualification_decision_json),
            (
                "Use the guarded batch decision packet to choose a pilot window; do not run "
                "qualification collection from automation."
            ),
        ),
        "transition_eval:trusted_scenarios": (
            portable_path(transition_crosswalk_csv, root=PROJECT_ROOT) or str(transition_crosswalk_csv),
            [
                portable_path(transition_crosswalk_csv, root=PROJECT_ROOT)
                or str(transition_crosswalk_csv),
                portable_path(transition_crosswalk_csv.with_suffix(".md"), root=PROJECT_ROOT)
                or str(transition_crosswalk_csv.with_suffix(".md")),
                portable_path(transition_crosswalk_audit_json, root=PROJECT_ROOT)
                or str(transition_crosswalk_audit_json),
                portable_path(transition_crosswalk_audit_json.with_suffix(".md"), root=PROJECT_ROOT)
                or str(transition_crosswalk_audit_json.with_suffix(".md")),
                portable_path(transition_gap_csv, root=PROJECT_ROOT) or str(transition_gap_csv),
                portable_path(provenance_decision_csv, root=PROJECT_ROOT)
                or str(provenance_decision_csv),
            ],
            report_path("transition_provenance_operator_crosswalk", stamp, "json"),
            (
                "Open the transition provenance crosswalk first; it maps each scenario_id to "
                "the provenance decision row, source packet ref, and packet hash. Do not trust "
                "scenarios automatically."
            ),
        ),
        "human_review:provenance_reconfirmation_required": (
            portable_path(provenance_decision_csv, root=PROJECT_ROOT) or str(provenance_decision_csv),
            [
                portable_path(transition_crosswalk_csv, root=PROJECT_ROOT)
                or str(transition_crosswalk_csv),
                portable_path(provenance_decision_csv, root=PROJECT_ROOT)
                or str(provenance_decision_csv),
                portable_path(provenance_decision_sheet_json.with_suffix(".md"), root=PROJECT_ROOT)
                or str(provenance_decision_sheet_json.with_suffix(".md")),
                provenance_packet_md,
                provenance_audit_md,
            ],
            portable_path(provenance_decision_sheet_json, root=PROJECT_ROOT)
            or str(provenance_decision_sheet_json),
            (
                "Use the crosswalk to fill only human-approved provenance rows; decisions "
                "remain blank until a human reviewer supplies rationale and evidence refs."
            ),
        ),
    }
    return mapping.get(blocker_id, ("", [], None, "Review the listed blocker evidence."))


def normalize_path_for_root(path_value: str | None, *, root: Path) -> str | None:
    path = resolve_artifact(path_value, root=root)
    if path is None:
        return path_value
    return portable_path(path, root=root)


def build_next_actions(
    *,
    release_readiness_path: Path,
    queue_run_path: Path,
    transition_gap_json: Path,
    qualification_decision_json: Path,
    provenance_proofset_log: Path,
    blocker_sprint_queue_json: Path,
    blocker_sprint_queue_audit_json: Path,
    transition_crosswalk_json: Path,
    transition_crosswalk_csv: Path,
    transition_crosswalk_audit_json: Path,
    provenance_decision_sheet_json: Path,
    operator_audit_json: Path,
    stamp: str,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    release = read_json(release_readiness_path)
    queue_run = read_json(queue_run_path)
    transition_gap = read_json(transition_gap_json)
    qualification = read_json(qualification_decision_json)
    operator_audit = read_json(operator_audit_json) if operator_audit_json.exists() else {}
    operator_audit_ok = operator_audit.get("ok") if isinstance(operator_audit.get("ok"), bool) else None
    blockers = release_blocker_map(release)
    release_actions = release_next_action_map(release)

    source_paths = {
        "release_readiness": portable_path(release_readiness_path, root=root),
        "queue_run": portable_path(queue_run_path, root=root),
        "transition_trusted_scenario_provenance_gap": portable_path(transition_gap_json, root=root),
        "qualification_guarded_batch_operator_decision": portable_path(
            qualification_decision_json,
            root=root,
        ),
        "provenance_reconfirmation_proofset_log": portable_path(provenance_proofset_log, root=root),
        "blocker_reduction_sprint_queue": portable_path(blocker_sprint_queue_json, root=root),
        "blocker_reduction_sprint_queue_audit": portable_path(
            blocker_sprint_queue_audit_json,
            root=root,
        ),
        "transition_provenance_crosswalk": portable_path(transition_crosswalk_json, root=root),
        "transition_provenance_crosswalk_csv": portable_path(transition_crosswalk_csv, root=root),
        "transition_provenance_crosswalk_audit": portable_path(
            transition_crosswalk_audit_json,
            root=root,
        ),
    }

    ordered_ids = [
        "review_debt:human_reviewed_concepts",
        "review_debt:human_reviewed_goal_links",
        "review_debt:human_reviewed_task_relations",
        "qualification:collection_coverage",
        "transition_eval:trusted_scenarios",
        "human_review:provenance_reconfirmation_required",
    ]
    ordered_ids.extend(blocker_id for blocker_id in blockers if blocker_id not in ordered_ids)

    actions: list[dict[str, Any]] = []
    for blocker_id in ordered_ids:
        blocker = blockers.get(blocker_id)
        release_action = release_actions.get(blocker_id, {})
        if blocker is None and not release_action:
            continue
        open_first, artifacts, supporting_artifact, safe_next_action = action_artifact_paths(
            blocker_id=blocker_id,
            stamp=stamp,
            transition_crosswalk_csv=transition_crosswalk_csv,
            transition_crosswalk_audit_json=transition_crosswalk_audit_json,
            transition_gap_json=transition_gap_json,
            qualification_decision_json=qualification_decision_json,
            provenance_decision_sheet_json=provenance_decision_sheet_json,
        )
        normalized_artifacts = [
            normalize_path_for_root(path, root=root) or path for path in artifacts if path
        ]
        normalized_open_first = normalize_path_for_root(open_first, root=root) or open_first
        normalized_supporting = normalize_path_for_root(supporting_artifact, root=root)
        action = {
            "id": blocker_id,
            "blocker": blocker_id,
            "category": (blocker or {}).get("category") or "unknown",
            "message": (blocker or {}).get("message") or release_action.get("action"),
            "value": (blocker or {}).get("value"),
            "threshold": (blocker or {}).get("threshold"),
            "owner": release_action.get("owner") or "operator",
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "api_calls": False,
            "approval_claim": False,
            "human_decision_required": True,
            "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
            "automation_boundary": (
                "guarded operator timing required"
                if blocker_id == "qualification:collection_coverage"
                else "human decision required"
            ),
            "safe_next_action": safe_next_action,
            "open_first": normalized_open_first,
            "artifacts_to_open": normalized_artifacts,
            "command": release_action.get("command"),
            "artifact_status": [
                artifact_status(path, root=root) for path in normalized_artifacts
            ],
        }
        if normalized_supporting:
            action["supporting_artifact"] = normalized_supporting
        actions.append(action)

    queue_summary = queue_run.get("summary") if isinstance(queue_run.get("summary"), dict) else {}
    batch_summary = (
        qualification.get("batch_summary")
        if isinstance(qualification.get("batch_summary"), dict)
        else {}
    )
    coverage_state = (
        qualification.get("coverage_state")
        if isinstance(qualification.get("coverage_state"), dict)
        else {}
    )
    return {
        "schema": NEXT_ACTION_SCHEMA,
        "generated_at": generated_at or now_iso(),
        "release_ready": release.get("release_ready"),
        "blocker_count": release.get("blocker_count", len(blockers)),
        "queue_run_acceptance_unverified_count": queue_summary.get(
            "acceptance_unverified_count",
            queue_run.get("acceptance_unverified_count"),
        ),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_paths": source_paths,
        "source_hashes": source_hashes(source_paths, root=root),
        "source_hash_scopes": source_hash_scopes(source_paths, root=root),
        "operator_packet_integrity_ok": operator_audit_ok,
        "operator_packet_integrity_path": portable_path(operator_audit_json, root=root),
        "operator_packet_integrity_issue_count": operator_audit.get("issue_count"),
        "transition_provenance_gap_summary": {
            "scenario_count": transition_gap.get("scenario_count"),
            "scenario_gap_count": transition_gap.get("scenario_gap_count"),
            "ready_packet_backed_scenario_count": transition_gap.get(
                "ready_packet_backed_scenario_count"
            ),
        },
        "qualification_guarded_batch_summary": {
            "ok": qualification.get("ok"),
            "current_coverage": coverage_state.get("collection_coverage"),
            "additional_attempted_units_needed": batch_summary.get(
                "additional_attempted_units_needed"
            ),
            "batch_count": batch_summary.get("batch_count"),
            "execution_authorized": qualification.get("execution_authorized"),
            "automatic_queue_execution_allowed": qualification.get(
                "automatic_queue_execution_allowed"
            ),
        },
        "actions": actions,
        "supporting_gap_sheets": {
            "transition_eval": portable_path(transition_gap_json.with_suffix(".csv"), root=root),
            "qualification_collection_coverage": portable_path(
                qualification_decision_json.with_suffix(".md"),
                root=root,
            ),
            "blocker_reduction_sprint_queue": portable_path(
                blocker_sprint_queue_json.with_suffix(".md"),
                root=root,
            ),
            "transition_provenance_crosswalk": portable_path(transition_crosswalk_csv, root=root),
        },
    }


def write_next_actions_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Operator Next Actions",
        "",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- schema: `{report.get('schema')}`",
        f"- release_ready: `{report.get('release_ready')}`",
        f"- blocker_count: `{report.get('blocker_count')}`",
        (
            "- queue_run_acceptance_unverified_count: "
            f"`{report.get('queue_run_acceptance_unverified_count')}`"
        ),
        f"- operator_packet_integrity_ok: `{report.get('operator_packet_integrity_ok')}`",
        f"- operator_packet_integrity_path: `{report.get('operator_packet_integrity_path')}`",
        (
            "- operator_packet_integrity_issue_count: "
            f"`{report.get('operator_packet_integrity_issue_count')}`"
        ),
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Actions",
    ]
    for action in report.get("actions") or []:
        lines.extend(
            [
                f"### {action.get('id')}",
                (
                    f"- category: `{action.get('category')}`; value: `{action.get('value')}`; "
                    f"threshold: `{action.get('threshold')}`"
                ),
                f"- owner: `{action.get('owner')}`",
                (
                    f"- report_only: `{action.get('report_only')}`; "
                    f"status_update_allowed: `{action.get('status_update_allowed')}`; "
                    f"db_writes: `{action.get('db_writes')}`; "
                    f"api_calls: `{action.get('api_calls')}`; "
                    f"approval_claim: `{action.get('approval_claim')}`"
                ),
                f"- human_decision_required: `{action.get('human_decision_required')}`",
                f"- boundary: `{action.get('automation_boundary')}`",
                f"- open_first: `{action.get('open_first')}`",
                f"- next: {action.get('safe_next_action')}",
                "- artifacts_to_open:",
            ]
        )
        for status in action.get("artifact_status") or []:
            lines.append(
                f"  - `{status.get('path')}` exists_nonempty=`{status.get('exists_nonempty')}` "
                f"sha256=`{status.get('sha256')}`"
            )
        if action.get("supporting_artifact"):
            lines.append(f"- supporting_artifact: `{action.get('supporting_artifact')}`")
        if action.get("command"):
            lines.append(f"- command: `{action.get('command')}`")
        lines.append("")

    lines.append("No human_reviewed, accepted, or reviewed status is authorized by this report.")
    lines.extend(["", "## Supporting Gap Sheets"])
    for key, value in (report.get("supporting_gap_sheets") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Source Hashes"])
    for key, value in (report.get("source_hashes") or {}).items():
        lines.append(f"- {key}: `{value}`")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def bundle_status(
    *,
    json_path: Path,
    markdown_path: Path | None = None,
    audit_path: Path | None = None,
    root: Path,
) -> dict[str, Any]:
    payload = read_json(json_path) if json_path.exists() and json_path.suffix == ".json" else {}
    bundle = {
        "path": portable_path(json_path, root=root),
        "markdown_path": portable_path(markdown_path, root=root) if markdown_path else None,
        "schema": payload.get("schema"),
        "generated_at": payload.get("generated_at"),
        "sha256": sha256_file(json_path),
        "markdown_sha256": sha256_file(markdown_path) if markdown_path else None,
    }
    if "ok" in payload:
        bundle["ok"] = payload.get("ok")
    if "issue_count" in payload:
        bundle["issue_count"] = payload.get("issue_count")
    if "warning_count" in payload:
        bundle["warning_count"] = payload.get("warning_count")
    if isinstance(payload.get("actions"), list):
        bundle["action_count"] = len(payload.get("actions") or [])
    if audit_path:
        bundle["audit_path"] = portable_path(audit_path, root=root)
        bundle["audit_sha256"] = sha256_file(audit_path)
    return {key: value for key, value in bundle.items() if value is not None}


def build_handoff(
    *,
    release_readiness_path: Path,
    quality_report_path: Path | None,
    dashboard_verification_path: Path | None,
    queue_run_path: Path,
    next_actions_json: Path,
    next_actions_markdown: Path,
    operator_audit_json: Path,
    operator_audit_markdown: Path,
    blocker_sprint_queue_json: Path,
    blocker_sprint_queue_markdown: Path | None,
    blocker_sprint_queue_audit_json: Path,
    transition_crosswalk_json: Path,
    transition_crosswalk_csv: Path,
    transition_crosswalk_audit_json: Path,
    qualification_decision_json: Path,
    qualification_decision_markdown: Path | None,
    transition_gap_json: Path,
    qualification_decision_audit_json: Path | None = None,
    previous_handoff_json: Path | None = None,
    verification_logs: list[Path] | None = None,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    release = read_json(release_readiness_path)
    queue_run = read_json(queue_run_path)
    quality = read_json(quality_report_path) if quality_report_path and quality_report_path.exists() else {}
    previous = (
        read_json(previous_handoff_json)
        if previous_handoff_json and previous_handoff_json.exists()
        else {}
    )
    quality_summary = release.get("inputs", {}).get("quality_summary")
    if not isinstance(quality_summary, dict):
        quality_summary = quality.get("summary") if isinstance(quality.get("summary"), dict) else {}
    release_state = {
        "release_ready": release.get("release_ready"),
        "blocker_count": release.get("blocker_count"),
        "warning_count": release.get("warning_count"),
        "dashboard_ok": None,
        "dashboard_failed_checks": None,
        "quality_status": release.get("inputs", {}).get("quality_status") or quality.get("status"),
        "quality_summary": quality_summary,
    }
    if dashboard_verification_path and dashboard_verification_path.exists():
        dashboard = read_json(dashboard_verification_path)
        release_state["dashboard_ok"] = dashboard.get("ok")
        release_state["dashboard_failed_checks"] = dashboard.get("failure_count")

    qualification_audit_json = (
        qualification_decision_audit_json
        or qualification_decision_json.with_name(
            "qualification_guarded_batch_operator_decision_audit_"
            f"{stamp_from_path(qualification_decision_json)}.json"
        )
    )
    queue_summary = queue_run.get("summary") if isinstance(queue_run.get("summary"), dict) else {}
    canonical_paths = [
        release_readiness_path,
        dashboard_verification_path,
        quality_report_path,
        queue_run_path,
        next_actions_json,
        next_actions_markdown,
        operator_audit_json,
        operator_audit_markdown,
        blocker_sprint_queue_json,
        blocker_sprint_queue_markdown,
        blocker_sprint_queue_audit_json,
        transition_crosswalk_json,
        transition_crosswalk_csv,
        transition_crosswalk_audit_json,
        qualification_decision_json,
        qualification_decision_markdown,
        qualification_audit_json,
        transition_gap_json,
    ]
    return {
        "schema": HANDOFF_SCHEMA,
        "generated_at": generated_at or now_iso(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "release_state": release_state,
        "queue_state": {"queue_run_summary": queue_summary},
        "code_changes": previous.get("code_changes", []),
        "canonical_artifacts": [
            artifact_status(path, root=root) for path in canonical_paths if path is not None
        ],
        "operator_next_actions": bundle_status(
            json_path=next_actions_json,
            markdown_path=next_actions_markdown,
            root=root,
        ),
        "operator_packet_integrity_audit": bundle_status(
            json_path=operator_audit_json,
            markdown_path=operator_audit_markdown,
            root=root,
        ),
        "blocker_reduction_sprint_queue": bundle_status(
            json_path=blocker_sprint_queue_json,
            markdown_path=blocker_sprint_queue_markdown,
            audit_path=blocker_sprint_queue_audit_json,
            root=root,
        ),
        "transition_provenance_operator_crosswalk": {
            **bundle_status(
                json_path=transition_crosswalk_json,
                markdown_path=transition_crosswalk_json.with_suffix(".md"),
                audit_path=transition_crosswalk_audit_json,
                root=root,
            ),
            "csv_path": portable_path(transition_crosswalk_csv, root=root),
            "csv_sha256": sha256_file(transition_crosswalk_csv),
        },
        "qualification_guarded_batch_decision": bundle_status(
            json_path=qualification_decision_json,
            markdown_path=qualification_decision_markdown,
            audit_path=qualification_audit_json,
            root=root,
        ),
        "transition_provenance_gap": bundle_status(
            json_path=transition_gap_json,
            markdown_path=transition_gap_json.with_suffix(".md"),
            root=root,
        ),
        "remaining_blockers": [
            {
                "id": blocker.get("name") or blocker.get("id"),
                "category": blocker.get("category"),
                "message": blocker.get("message"),
                "value": blocker.get("value"),
                "threshold": blocker.get("threshold"),
            }
            for blocker in release.get("blockers") or []
            if isinstance(blocker, dict)
        ],
        "verification_logs": [
            artifact_status(path, root=root) for path in (verification_logs or [])
        ],
        "workspace_status_note": previous.get(
            "workspace_status_note",
            "Generated by report-only operator handoff bundle automation.",
        ),
    }


def write_handoff_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    release_state = report.get("release_state") if isinstance(report.get("release_state"), dict) else {}
    next_actions = (
        report.get("operator_next_actions")
        if isinstance(report.get("operator_next_actions"), dict)
        else {}
    )
    operator_audit = (
        report.get("operator_packet_integrity_audit")
        if isinstance(report.get("operator_packet_integrity_audit"), dict)
        else {}
    )
    crosswalk = (
        report.get("transition_provenance_operator_crosswalk")
        if isinstance(report.get("transition_provenance_operator_crosswalk"), dict)
        else {}
    )
    sprint_queue = (
        report.get("blocker_reduction_sprint_queue")
        if isinstance(report.get("blocker_reduction_sprint_queue"), dict)
        else {}
    )
    lines = [
        "# Overnight 10h Operator Handoff",
        "",
        "## Safety Contract",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        f"- forbidden_automatic_statuses: `{report.get('forbidden_automatic_statuses')}`",
        "",
        "## Current State",
        f"- generated_at: `{report.get('generated_at')}`",
        (
            f"- release_ready: `{release_state.get('release_ready')}`; "
            f"blockers: `{release_state.get('blocker_count')}`"
        ),
        (
            f"- quality_status: `{release_state.get('quality_status')}`; "
            f"summary: `{release_state.get('quality_summary')}`"
        ),
        "",
        "## Operator Next Actions",
        f"- `{next_actions.get('path')}` sha256=`{next_actions.get('sha256')}`",
        f"- `{next_actions.get('markdown_path')}` sha256=`{next_actions.get('markdown_sha256')}`",
        "",
        "## Operator Packet Integrity Audit",
        f"- `{operator_audit.get('path')}` sha256=`{operator_audit.get('sha256')}`",
        (
            f"- `{operator_audit.get('markdown_path')}` "
            f"sha256=`{operator_audit.get('markdown_sha256')}`"
        ),
        (
            f"- ok: `{operator_audit.get('ok')}`; issues: "
            f"`{operator_audit.get('issue_count')}`; warnings: "
            f"`{operator_audit.get('warning_count')}`"
        ),
        "",
        "## Transition Provenance Crosswalk",
        f"- `{crosswalk.get('csv_path')}` sha256=`{crosswalk.get('csv_sha256')}`",
        f"- `{crosswalk.get('audit_path')}` sha256=`{crosswalk.get('audit_sha256')}`",
        "",
        "## Blocker Reduction Sprint Queue",
        f"- `{sprint_queue.get('path')}` sha256=`{sprint_queue.get('sha256')}`",
        (
            f"- `{sprint_queue.get('markdown_path')}` "
            f"sha256=`{sprint_queue.get('markdown_sha256')}`"
        ),
        f"- `{sprint_queue.get('audit_path')}` sha256=`{sprint_queue.get('audit_sha256')}`",
        "",
        "## Canonical Artifacts",
    ]
    for artifact in report.get("canonical_artifacts") or []:
        lines.append(
            f"- `{artifact.get('path')}` exists_nonempty=`{artifact.get('exists_nonempty')}` "
            f"sha256=`{artifact.get('sha256')}`"
        )
    lines.extend(
        [
            "",
            "## Verification Logs",
        ]
    )
    for artifact in report.get("verification_logs") or []:
        lines.append(
            f"- `{artifact.get('path')}` exists_nonempty=`{artifact.get('exists_nonempty')}` "
            f"sha256=`{artifact.get('sha256')}`"
        )
    lines.extend(
        [
            "",
        "No human_reviewed, accepted, or reviewed status was written or claimed by this automation block.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def import_audit_helpers() -> tuple[Any, Any, Any, Any]:
    try:
        from scripts.audit_operator_report_lineage_sync import (
            build_lineage_sync_audit,
            write_markdown as write_lineage_markdown,
        )
        from scripts.audit_operator_review_packet_integrity import (
            build_integrity_audit,
            write_markdown as write_integrity_markdown,
        )
    except ModuleNotFoundError:
        from audit_operator_report_lineage_sync import (  # type: ignore
            build_lineage_sync_audit,
            write_markdown as write_lineage_markdown,
        )
        from audit_operator_review_packet_integrity import (  # type: ignore
            build_integrity_audit,
            write_markdown as write_integrity_markdown,
        )
    return (
        build_integrity_audit,
        write_integrity_markdown,
        build_lineage_sync_audit,
        write_lineage_markdown,
    )


def build_bundle(
    *,
    release_readiness_path: Path,
    queue_run_path: Path,
    transition_gap_json: Path,
    qualification_decision_json: Path,
    provenance_proofset_log: Path,
    blocker_sprint_queue_json: Path,
    blocker_sprint_queue_audit_json: Path,
    transition_crosswalk_json: Path,
    transition_crosswalk_csv: Path,
    transition_crosswalk_audit_json: Path,
    provenance_decision_sheet_json: Path,
    concept_seedpack_csv: Path,
    blocker_ranked_seedpack_csv: Path,
    provenance_decision_sheet_csv: Path,
    provenance_decision_audit_json: Path,
    qualification_decision_csv: Path,
    quality_report_path: Path | None,
    dashboard_verification_path: Path | None,
    next_actions_out: Path,
    next_actions_markdown_out: Path,
    operator_audit_out: Path,
    operator_audit_markdown_out: Path,
    handoff_out: Path,
    handoff_markdown_out: Path,
    lineage_audit_out: Path,
    lineage_audit_markdown_out: Path,
    qualification_decision_audit_json: Path | None = None,
    previous_handoff_json: Path | None = None,
    verification_logs: list[Path] | None = None,
    stamp: str | None = None,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    stamp = stamp or stamp_from_path(next_actions_out)
    generated_at = generated_at or now_iso()
    (
        build_integrity_audit,
        write_integrity_markdown,
        build_lineage_sync_audit,
        write_lineage_markdown,
    ) = import_audit_helpers()

    next_actions = build_next_actions(
        release_readiness_path=release_readiness_path,
        queue_run_path=queue_run_path,
        transition_gap_json=transition_gap_json,
        qualification_decision_json=qualification_decision_json,
        provenance_proofset_log=provenance_proofset_log,
        blocker_sprint_queue_json=blocker_sprint_queue_json,
        blocker_sprint_queue_audit_json=blocker_sprint_queue_audit_json,
        transition_crosswalk_json=transition_crosswalk_json,
        transition_crosswalk_csv=transition_crosswalk_csv,
        transition_crosswalk_audit_json=transition_crosswalk_audit_json,
        provenance_decision_sheet_json=provenance_decision_sheet_json,
        operator_audit_json=operator_audit_out,
        stamp=stamp,
        root=root,
        generated_at=generated_at,
    )
    write_json(next_actions_out, next_actions)
    write_next_actions_markdown(next_actions_markdown_out, next_actions)

    integrity_audit = build_integrity_audit(
        concept_seedpack_csv=concept_seedpack_csv,
        blocker_ranked_seedpack_csv=blocker_ranked_seedpack_csv,
        provenance_decision_sheet_csv=provenance_decision_sheet_csv,
        transition_crosswalk_csv=transition_crosswalk_csv,
        qualification_decision_csv=qualification_decision_csv,
        provenance_decision_sheet_json=provenance_decision_sheet_json,
        provenance_decision_audit_json=provenance_decision_audit_json,
        qualification_decision_json=qualification_decision_json,
        transition_gap_json=transition_gap_json,
        transition_crosswalk_json=transition_crosswalk_json,
        transition_crosswalk_audit_json=transition_crosswalk_audit_json,
        blocker_sprint_queue_json=blocker_sprint_queue_json,
        blocker_sprint_queue_audit_json=blocker_sprint_queue_audit_json,
        operator_next_actions_json=next_actions_out,
        root=root,
        generated_at=generated_at,
    )
    write_json(operator_audit_out, integrity_audit)
    write_integrity_markdown(operator_audit_markdown_out, integrity_audit)

    next_actions["operator_packet_integrity_ok"] = integrity_audit.get("ok")
    next_actions["operator_packet_integrity_issue_count"] = integrity_audit.get("issue_count")
    write_json(next_actions_out, next_actions)
    write_next_actions_markdown(next_actions_markdown_out, next_actions)

    integrity_audit = build_integrity_audit(
        concept_seedpack_csv=concept_seedpack_csv,
        blocker_ranked_seedpack_csv=blocker_ranked_seedpack_csv,
        provenance_decision_sheet_csv=provenance_decision_sheet_csv,
        transition_crosswalk_csv=transition_crosswalk_csv,
        qualification_decision_csv=qualification_decision_csv,
        provenance_decision_sheet_json=provenance_decision_sheet_json,
        provenance_decision_audit_json=provenance_decision_audit_json,
        qualification_decision_json=qualification_decision_json,
        transition_gap_json=transition_gap_json,
        transition_crosswalk_json=transition_crosswalk_json,
        transition_crosswalk_audit_json=transition_crosswalk_audit_json,
        blocker_sprint_queue_json=blocker_sprint_queue_json,
        blocker_sprint_queue_audit_json=blocker_sprint_queue_audit_json,
        operator_next_actions_json=next_actions_out,
        root=root,
        generated_at=generated_at,
    )
    write_json(operator_audit_out, integrity_audit)
    write_integrity_markdown(operator_audit_markdown_out, integrity_audit)

    handoff = build_handoff(
        release_readiness_path=release_readiness_path,
        quality_report_path=quality_report_path,
        dashboard_verification_path=dashboard_verification_path,
        queue_run_path=queue_run_path,
        next_actions_json=next_actions_out,
        next_actions_markdown=next_actions_markdown_out,
        operator_audit_json=operator_audit_out,
        operator_audit_markdown=operator_audit_markdown_out,
        blocker_sprint_queue_json=blocker_sprint_queue_json,
        blocker_sprint_queue_markdown=blocker_sprint_queue_json.with_suffix(".md"),
        blocker_sprint_queue_audit_json=blocker_sprint_queue_audit_json,
        transition_crosswalk_json=transition_crosswalk_json,
        transition_crosswalk_csv=transition_crosswalk_csv,
        transition_crosswalk_audit_json=transition_crosswalk_audit_json,
        qualification_decision_json=qualification_decision_json,
        qualification_decision_markdown=qualification_decision_json.with_suffix(".md"),
        transition_gap_json=transition_gap_json,
        qualification_decision_audit_json=qualification_decision_audit_json,
        previous_handoff_json=previous_handoff_json,
        verification_logs=verification_logs,
        root=root,
        generated_at=generated_at,
    )
    write_json(handoff_out, handoff)
    write_handoff_markdown(handoff_markdown_out, handoff)

    lineage_audit = build_lineage_sync_audit(
        next_actions_json=next_actions_out,
        next_actions_markdown=next_actions_markdown_out,
        handoff_json=handoff_out,
        operator_audit_json=operator_audit_out,
        operator_audit_markdown=operator_audit_markdown_out,
        decision_sheet_json=provenance_decision_sheet_json,
        base_dir=root,
        generated_at=generated_at,
    )
    write_json(lineage_audit_out, lineage_audit)
    write_lineage_markdown(lineage_audit_markdown_out, lineage_audit)

    return {
        "ok": bool(integrity_audit.get("ok") and lineage_audit.get("ok")),
        "schema": "aihr_operator_handoff_bundle_run_v1",
        "generated_at": generated_at,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "next_actions_path": portable_path(next_actions_out, root=root),
        "operator_audit_path": portable_path(operator_audit_out, root=root),
        "handoff_path": portable_path(handoff_out, root=root),
        "lineage_audit_path": portable_path(lineage_audit_out, root=root),
        "next_actions_sha256": sha256_file(next_actions_out),
        "operator_audit_ok": integrity_audit.get("ok"),
        "operator_audit_issue_count": integrity_audit.get("issue_count"),
        "lineage_audit_ok": lineage_audit.get("ok"),
        "lineage_audit_issue_count": lineage_audit.get("issue_count"),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build AI-HR operator next-actions, integrity audit, handoff, and lineage audit."
    )
    parser.add_argument("--stamp")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--release-readiness", type=Path)
    parser.add_argument("--queue-run", type=Path)
    parser.add_argument("--transition-gap-json", type=Path)
    parser.add_argument("--qualification-decision-json", type=Path)
    parser.add_argument("--provenance-proofset-log", type=Path)
    parser.add_argument("--blocker-sprint-queue-json", type=Path)
    parser.add_argument("--blocker-sprint-queue-audit-json", type=Path)
    parser.add_argument("--transition-crosswalk-json", type=Path)
    parser.add_argument("--transition-crosswalk-csv", type=Path)
    parser.add_argument("--transition-crosswalk-audit-json", type=Path)
    parser.add_argument("--provenance-decision-sheet-json", type=Path)
    parser.add_argument("--provenance-decision-sheet-csv", type=Path)
    parser.add_argument("--provenance-decision-audit-json", type=Path)
    parser.add_argument("--concept-seedpack-csv", type=Path)
    parser.add_argument("--blocker-ranked-seedpack-csv", type=Path)
    parser.add_argument("--qualification-decision-csv", type=Path)
    parser.add_argument("--qualification-decision-audit-json", type=Path)
    parser.add_argument("--quality-report", type=Path)
    parser.add_argument("--dashboard-verification", type=Path)
    parser.add_argument("--previous-handoff-json", type=Path)
    parser.add_argument("--verification-log", type=Path, action="append", dest="verification_logs")
    parser.add_argument("--next-actions-out", type=Path, required=True)
    parser.add_argument("--next-actions-markdown-out", type=Path, required=True)
    parser.add_argument("--operator-audit-out", type=Path, required=True)
    parser.add_argument("--operator-audit-markdown-out", type=Path, required=True)
    parser.add_argument("--handoff-out", type=Path, required=True)
    parser.add_argument("--handoff-markdown-out", type=Path, required=True)
    parser.add_argument("--lineage-audit-out", type=Path, required=True)
    parser.add_argument("--lineage-audit-markdown-out", type=Path, required=True)
    parser.add_argument("--strict", action="store_true")
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    reports_dir = args.root / "reports"
    release_readiness = input_or_latest(
        args.release_readiness,
        "aihr_release_readiness_*.json",
        reports_dir=reports_dir,
    )
    queue_run = input_or_latest(
        args.queue_run,
        "aihr_agent_queue_run_*.json",
        reports_dir=reports_dir,
        exclude_substrings=("_probe", "_dryrun_"),
    )
    transition_gap_json = input_or_latest(
        args.transition_gap_json,
        "transition_trusted_scenario_provenance_gap_*.json",
        reports_dir=reports_dir,
    )
    qualification_json = input_or_latest(
        args.qualification_decision_json,
        "qualification_guarded_batch_operator_decision_*.json",
        reports_dir=reports_dir,
        exclude_substrings=("_audit", "_probe"),
    )
    result = build_bundle(
        release_readiness_path=release_readiness,
        queue_run_path=queue_run,
        transition_gap_json=transition_gap_json,
        qualification_decision_json=qualification_json,
        provenance_proofset_log=input_or_latest(
            args.provenance_proofset_log,
            "command_provenance_reconfirmation_proofset_*.log",
            reports_dir=reports_dir,
        ),
        blocker_sprint_queue_json=input_or_latest(
            args.blocker_sprint_queue_json,
            "aihr_blocker_reduction_operator_sprint_queue_*.json",
            reports_dir=reports_dir,
            exclude_substrings=("_audit", "_probe"),
        ),
        blocker_sprint_queue_audit_json=input_or_latest(
            args.blocker_sprint_queue_audit_json,
            "aihr_blocker_reduction_operator_sprint_queue_audit_*.json",
            reports_dir=reports_dir,
        ),
        transition_crosswalk_json=input_or_latest(
            args.transition_crosswalk_json,
            "transition_provenance_operator_crosswalk_*.json",
            reports_dir=reports_dir,
            exclude_substrings=("_audit", "_probe"),
        ),
        transition_crosswalk_csv=input_or_latest(
            args.transition_crosswalk_csv,
            "transition_provenance_operator_crosswalk_*.csv",
            reports_dir=reports_dir,
        ),
        transition_crosswalk_audit_json=input_or_latest(
            args.transition_crosswalk_audit_json,
            "transition_provenance_operator_crosswalk_audit_*.json",
            reports_dir=reports_dir,
        ),
        provenance_decision_sheet_json=input_or_latest(
            args.provenance_decision_sheet_json,
            "human_review_provenance_reconfirmation_decision_sheet_*.json",
            reports_dir=reports_dir,
        ),
        concept_seedpack_csv=input_or_latest(
            args.concept_seedpack_csv,
            "aihr_ontology_definition_review_seedpack_*.csv",
            reports_dir=reports_dir,
        ),
        blocker_ranked_seedpack_csv=input_or_latest(
            args.blocker_ranked_seedpack_csv,
            "aihr_review_seedpack_blocker_ranked_*.csv",
            reports_dir=reports_dir,
        ),
        provenance_decision_sheet_csv=input_or_latest(
            args.provenance_decision_sheet_csv,
            "human_review_provenance_reconfirmation_decision_sheet_*.csv",
            reports_dir=reports_dir,
        ),
        provenance_decision_audit_json=input_or_latest(
            args.provenance_decision_audit_json,
            "human_review_provenance_reconfirmation_decision_audit_*.json",
            reports_dir=reports_dir,
        ),
        qualification_decision_csv=input_or_latest(
            args.qualification_decision_csv,
            "qualification_guarded_batch_operator_decision_*.csv",
            reports_dir=reports_dir,
        ),
        qualification_decision_audit_json=input_or_latest(
            args.qualification_decision_audit_json,
            "qualification_guarded_batch_operator_decision_audit_*.json",
            reports_dir=reports_dir,
        ),
        quality_report_path=args.quality_report,
        dashboard_verification_path=args.dashboard_verification,
        next_actions_out=args.next_actions_out,
        next_actions_markdown_out=args.next_actions_markdown_out,
        operator_audit_out=args.operator_audit_out,
        operator_audit_markdown_out=args.operator_audit_markdown_out,
        handoff_out=args.handoff_out,
        handoff_markdown_out=args.handoff_markdown_out,
        lineage_audit_out=args.lineage_audit_out,
        lineage_audit_markdown_out=args.lineage_audit_markdown_out,
        previous_handoff_json=args.previous_handoff_json,
        verification_logs=args.verification_logs or [],
        stamp=args.stamp,
        root=args.root,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.strict and not result.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
