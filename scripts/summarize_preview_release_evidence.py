from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _get(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _count_review_gated_static_artifacts(dashboard: dict[str, Any]) -> int:
    artifacts = dashboard.get("static_artifacts")
    if not isinstance(artifacts, list):
        artifacts = _get(dashboard, "static_artifacts", "artifacts", default=[])
    if not isinstance(artifacts, list):
        return 0
    count = 0
    for item in artifacts:
        if isinstance(item, dict) and item.get("review_gated"):
            count += 1
    return count


def _static_artifact_count(dashboard: dict[str, Any], static_check: dict[str, Any]) -> int:
    artifacts = dashboard.get("static_artifacts")
    if isinstance(artifacts, list):
        return len(artifacts)
    check_artifacts = static_check.get("artifacts")
    if isinstance(check_artifacts, list):
        return len(check_artifacts)
    detail = static_check.get("detail")
    if isinstance(detail, str) and detail.startswith("count="):
        try:
            return int(detail.split("=", 1)[1])
        except ValueError:
            return 0
    return int(static_check.get("artifact_count") or 0)


def _review_gated_static_artifact_count(dashboard: dict[str, Any], static_check: dict[str, Any]) -> int:
    named = static_check.get("review_gated_checkpoint_artifacts")
    if isinstance(named, list):
        return len(named)
    return _count_review_gated_static_artifacts(dashboard)


def _named_check(payload: dict[str, Any], name: str) -> dict[str, Any]:
    checks = payload.get("checks") or []
    if isinstance(checks, dict):
        value = checks.get(name)
        return value if isinstance(value, dict) else {}
    if isinstance(checks, list):
        for item in checks:
            if isinstance(item, dict) and item.get("name") == name:
                return item
    return {}


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_mtime_utc(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    except OSError:
        return None


def _output_dir_status(output_dir: Any, *, artifact_path: Path | None = None) -> dict[str, Any]:
    if not isinstance(output_dir, str) or not output_dir:
        return {
            "exists": False,
            "is_dir": False,
            "resolved": None,
            "issue": "missing_output_dir",
        }
    path = Path(output_dir)
    if not path.is_absolute() and artifact_path is not None:
        path = artifact_path.parent / path
    try:
        resolved = path.resolve()
    except OSError:
        return {
            "exists": False,
            "is_dir": False,
            "resolved": str(path),
            "issue": "unresolvable_output_dir",
        }
    exists = resolved.exists()
    is_dir = resolved.is_dir()
    return {
        "exists": exists,
        "is_dir": is_dir,
        "resolved": str(resolved),
        "issue": None
        if is_dir
        else "output_dir_not_found"
        if not exists
        else "output_dir_not_directory",
    }


def _supporting_evidence_freshness(
    *,
    source_generated_at: Any,
    required_paths: dict[str, Path],
) -> dict[str, Any]:
    source_generated = _parse_timestamp(source_generated_at)
    artifact_mtimes: dict[str, str | None] = {}
    stale_artifacts: list[dict[str, Any]] = []
    missing_artifacts: list[str] = []
    if source_generated is None:
        return {
            "ok": False,
            "source_generated_at": source_generated_at,
            "source_generated_at_valid": False,
            "artifact_mtimes": artifact_mtimes,
            "missing_artifacts": list(required_paths),
            "stale_artifacts": [],
        }
    for name, path in required_paths.items():
        mtime = _path_mtime_utc(path)
        artifact_mtimes[name] = mtime.isoformat() if mtime else None
        if mtime is None:
            missing_artifacts.append(name)
        elif mtime < source_generated:
            stale_artifacts.append(
                {
                    "artifact": name,
                    "path": str(path),
                    "artifact_mtime_utc": mtime.isoformat(),
                    "source_generated_at": source_generated.isoformat(),
                }
            )
    return {
        "ok": not missing_artifacts and not stale_artifacts,
        "source_generated_at": source_generated.isoformat(),
        "source_generated_at_valid": True,
        "artifact_mtimes": artifact_mtimes,
        "missing_artifacts": missing_artifacts,
        "stale_artifacts": stale_artifacts,
    }


def build_summary(
    *,
    release_path: Path,
    dashboard_path: Path,
    blockers_path: Path,
    queue_status_path: Path,
    queue_run_path: Path,
    source_boundary_path: Path | None,
    source_preview_export_path: Path | None = None,
) -> dict[str, Any]:
    release = _read_json(release_path)
    dashboard = _read_json(dashboard_path)
    blockers = _read_json(blockers_path)
    queue_status = _read_json(queue_status_path)
    queue_run = _read_json(queue_run_path)
    source_boundary = _read_json(source_boundary_path) if source_boundary_path else None
    source_preview_export = (
        _read_json(source_preview_export_path) if source_preview_export_path else None
    )

    release_decision = release.get("release_decision") or {}
    dashboard_static_artifacts = dashboard.get("static_artifacts")
    static_artifacts = (
        dashboard_static_artifacts
        if isinstance(dashboard_static_artifacts, dict)
        else _named_check(dashboard, "static_artifacts")
    )
    review_chain = dashboard.get("review_chain_safety_summary") or _named_check(
        dashboard, "review_chain_safety"
    )
    queue_summary = queue_status.get("summary") or {}
    run_summary = queue_run.get("summary") or {}
    qualification_snapshot = (
        (blockers.get("qualification_coverage_plan_snapshot") or {})
        if isinstance(blockers, dict)
        else {}
    )
    remaining_blockers = blockers.get("remaining_blockers") or blockers.get("blockers") or []

    blocked_by = release_decision.get("blocked_by") or []
    preview_allowed = bool(
        release.get("engineering_hygiene_ok")
        and dashboard.get("ok")
        and static_artifacts.get("ok")
        and review_chain.get("do_not_set_human_reviewed_accepted_reviewed_automatically")
        and not qualification_snapshot.get("automatic_collection_allowed_now", True)
    )

    stable_ready = bool(release_decision.get("release_ready"))
    source_boundary_ok = None
    if source_boundary is not None:
        source_boundary_ok = bool(source_boundary.get("ok"))
    source_preview_export_ok = None
    source_preview_export_missing_fields: list[str] = []
    if source_preview_export is not None:
        if not source_preview_export.get("output_dir"):
            source_preview_export_missing_fields.append("output_dir")
        if not source_preview_export.get("generated_at"):
            source_preview_export_missing_fields.append("generated_at")
        source_preview_output_dir_status = _output_dir_status(
            source_preview_export.get("output_dir"),
            artifact_path=source_preview_export_path,
        )
        source_preview_export_ok = bool(
            source_preview_export.get("ok")
            and not source_preview_export_missing_fields
            and source_preview_output_dir_status.get("is_dir")
        )
    else:
        source_preview_output_dir_status = _output_dir_status(None)
    source_package_ok = bool(source_boundary_ok is True or source_preview_export_ok is True)
    required_freshness_paths = {
        "release_readiness": release_path,
        "dashboard_verification": dashboard_path,
    }
    if source_boundary_path is not None:
        required_freshness_paths["source_boundary"] = source_boundary_path
    if source_preview_export_path is not None:
        required_freshness_paths["source_preview_export"] = source_preview_export_path
    supporting_evidence_freshness = _supporting_evidence_freshness(
        source_generated_at=_get(source_preview_export or {}, "generated_at"),
        required_paths=required_freshness_paths,
    )
    supporting_evidence_freshness_ok = bool(supporting_evidence_freshness.get("ok"))

    preview_blockers = []
    preview_warnings = []
    if not preview_allowed:
        preview_blockers.append("preview_evidence_contract_not_satisfied")
    if not supporting_evidence_freshness_ok:
        preview_blockers.append("supporting_evidence_older_than_source_preview_export")
    if (
        source_preview_export is not None
        and not source_preview_export_missing_fields
        and not source_preview_output_dir_status.get("is_dir")
    ):
        preview_blockers.append("source_preview_export_output_dir_missing")
    if source_boundary_ok is None:
        preview_blockers.append("missing_source_boundary_evidence")
    if source_boundary_ok is False and source_preview_export_ok is not True:
        preview_blockers.append("source_boundary_branch_not_clean")
    if source_boundary_ok is False and source_preview_export_ok is True:
        preview_warnings.append("current_branch_source_boundary_not_clean")

    preview_contract_ok = bool(
        preview_allowed
        and source_package_ok
        and supporting_evidence_freshness_ok
        and not preview_blockers
    )

    return {
        "schema": "ncs_preview_release_evidence_summary_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "ok": preview_contract_ok,
        "contract_ok": preview_contract_ok,
        "execution_authorized": False,
        "human_signoff_required": True,
        "preview_is_not_approval": True,
        "preview_allowed_by_product_evidence": preview_allowed,
        "preview_evidence_complete": preview_contract_ok,
        "supporting_evidence_freshness_ok": supporting_evidence_freshness_ok,
        "supporting_evidence_freshness": supporting_evidence_freshness,
        "stable_release_ready": stable_ready,
        "source_boundary_ok": source_boundary_ok,
        "source_preview_export_ok": source_preview_export_ok,
        "source_package_ok": source_package_ok,
        "preview_blockers": preview_blockers,
        "preview_warnings": preview_warnings,
        "source_boundary": {
            "path": str(source_boundary_path) if source_boundary_path else None,
            "ok": source_boundary_ok,
            "generated_at": _get(source_boundary or {}, "generated_at"),
            "tracked_blocker_count": _get(source_boundary or {}, "summary", "tracked_blocker_count"),
            "lfs_history_evaluated": bool(_get(source_boundary or {}, "policy", "check_lfs_history")),
            "lfs_history_blocker_count": _get(
                source_boundary or {}, "summary", "lfs_history_blocker_count"
            ),
        },
        "release": {
            "path": str(release_path),
            "release_ready": stable_ready,
            "engineering_hygiene_ok": bool(release.get("engineering_hygiene_ok")),
            "blocker_count": int(release.get("blocker_count") or 0),
            "blocked_by": blocked_by,
        },
        "dashboard": {
            "path": str(dashboard_path),
            "ok": bool(dashboard.get("ok")),
            "static_artifacts_ok": bool(static_artifacts.get("ok")),
            "static_artifact_count": _static_artifact_count(dashboard, static_artifacts),
            "review_gated_static_artifact_count": _review_gated_static_artifact_count(
                dashboard, static_artifacts
            ),
            "artifact_date_contract_ok": bool(
                _get(release, "artifact_date_contract", "release_outputs", "ok", default=False)
                and _get(release, "artifact_date_contract", "proof_artifacts", "ok", default=False)
            ),
            "artifact_lineage_contract_ok": bool(
                _get(release, "artifact_lineage_contract", "ok", default=False)
            ),
            "review_chain_safety": {
                "do_not_set_human_reviewed_accepted_reviewed_automatically": bool(
                    review_chain.get("do_not_set_human_reviewed_accepted_reviewed_automatically")
                ),
                "rows_without_packet_backed_provenance": review_chain.get(
                    "rows_without_packet_backed_provenance"
                ),
                "legacy_status_needs_reconfirmation_count": review_chain.get(
                    "legacy_status_needs_reconfirmation_count"
                ),
                "provenance_date_matches_plan_review_family": review_chain.get(
                    "provenance_date_matches_plan_review_family"
                ),
                "pending_decision_count": _first_present(
                    review_chain.get("pending_decision_count"),
                    review_chain.get("legacy_status_needs_reconfirmation_count"),
                ),
                "blank_decision_count": _first_present(
                    review_chain.get("blank_decision_count"),
                    review_chain.get("reconfirmation_blank_decision_count"),
                    review_chain.get("legacy_status_needs_reconfirmation_count"),
                ),
                "reconfirmation_blank_decision_count": _first_present(
                    review_chain.get("reconfirmation_blank_decision_count"),
                    review_chain.get("legacy_status_needs_reconfirmation_count"),
                ),
            },
        },
        "remaining_blockers": {
            "path": str(blockers_path),
            "count": len(remaining_blockers) if isinstance(remaining_blockers, list) else None,
            "qualification_collection_coverage_blocker_present": any(
                isinstance(item, dict)
                and (
                    item.get("blocker_key") == "qualification:collection_coverage"
                    or item.get("name") == "qualification:collection_coverage"
                )
                for item in remaining_blockers
            )
            if isinstance(remaining_blockers, list)
            else None,
            "qualification_coverage_plan_snapshot": {
                key: qualification_snapshot.get(key)
                for key in (
                    "collection_coverage",
                    "automatic_collection_allowed_now",
                    "automatic_queue_execution_allowed",
                    "must_run_qualification_retry_hygiene_first",
                    "operator_timing_required",
                    "must_not_write_human_review_statuses",
                    "forbidden_status_updates_exact",
                )
            },
        },
        "queue": {
            "status_path": str(queue_status_path),
            "run_path": str(queue_run_path),
            "manual_ready_count": queue_summary.get("manual_ready_count"),
            "manual_human_decision_count": queue_summary.get("manual_human_decision_count"),
            "guarded_manual_count": queue_summary.get("guarded_manual_count"),
            "auto_startable_count": queue_summary.get("auto_startable_count"),
            "run_selected_count": run_summary.get("selected_count"),
            "run_failed_count": run_summary.get("failed_count"),
        },
        "source_preview_export": {
            "path": str(source_preview_export_path) if source_preview_export_path else None,
            "ok": source_preview_export_ok,
            "generated_at": _get(source_preview_export or {}, "generated_at"),
            "output_dir": _get(source_preview_export or {}, "output_dir"),
            "output_dir_exists": source_preview_output_dir_status.get("exists"),
            "output_dir_is_dir": source_preview_output_dir_status.get("is_dir"),
            "output_dir_resolved": source_preview_output_dir_status.get("resolved"),
            "output_dir_issue": source_preview_output_dir_status.get("issue"),
            "missing_required_fields": source_preview_export_missing_fields,
            "copied_file_count": _get(source_preview_export or {}, "summary", "copied_file_count"),
            "copied_blocker_count": _get(source_preview_export or {}, "summary", "copied_blocker_count"),
            "included_untracked_path_count": _get(
                source_preview_export or {}, "summary", "included_untracked_path_count"
            ),
            "excluded_untracked_candidate_count": _get(
                source_preview_export or {}, "summary", "excluded_untracked_candidate_count"
            ),
        },
    }


def write_markdown(summary: dict[str, Any], path: Path) -> None:
    lines = [
        "# Preview Release Evidence Summary",
        "",
        f"- ok: `{str(summary.get('ok')).lower()}`",
        f"- contract_ok: `{str(summary.get('contract_ok')).lower()}`",
        f"- execution_authorized: `{str(summary.get('execution_authorized')).lower()}`",
        f"- human_signoff_required: `{str(summary.get('human_signoff_required')).lower()}`",
        f"- preview_is_not_approval: `{str(summary.get('preview_is_not_approval')).lower()}`",
        f"- preview_allowed_by_product_evidence: `{str(summary.get('preview_allowed_by_product_evidence')).lower()}`",
        f"- preview_evidence_complete: `{str(summary.get('preview_evidence_complete')).lower()}`",
        f"- supporting_evidence_freshness_ok: `{str(summary.get('supporting_evidence_freshness_ok')).lower()}`",
        f"- stable_release_ready: `{str(summary.get('stable_release_ready')).lower()}`",
        f"- source_boundary_ok: `{summary.get('source_boundary_ok')}`",
        f"- source_preview_export_ok: `{summary.get('source_preview_export_ok')}`",
        f"- source_package_ok: `{summary.get('source_package_ok')}`",
        f"- preview_blockers: `{', '.join(summary.get('preview_blockers') or []) or 'none'}`",
        f"- preview_warnings: `{', '.join(summary.get('preview_warnings') or []) or 'none'}`",
        "",
        "## Release",
        "",
        f"- engineering_hygiene_ok: `{summary['release']['engineering_hygiene_ok']}`",
        f"- release_ready: `{summary['release']['release_ready']}`",
        f"- blocker_count: `{summary['release']['blocker_count']}`",
        f"- blocked_by: `{', '.join(summary['release']['blocked_by'])}`",
        "",
        "## Dashboard",
        "",
        f"- dashboard_ok: `{summary['dashboard']['ok']}`",
        f"- static_artifacts_ok: `{summary['dashboard']['static_artifacts_ok']}`",
        f"- static_artifact_count: `{summary['dashboard']['static_artifact_count']}`",
        f"- review_gated_static_artifact_count: `{summary['dashboard']['review_gated_static_artifact_count']}`",
        "",
        "## Human Review Safety",
        "",
    ]
    safety = summary["dashboard"]["review_chain_safety"]
    for key, value in safety.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Qualification Planning", ""])
    qualification = summary["remaining_blockers"]["qualification_coverage_plan_snapshot"]
    for key, value in qualification.items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Queue", ""])
    for key, value in summary["queue"].items():
        if key.endswith("_path"):
            continue
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Source Preview Export", ""])
    for key, value in summary["source_preview_export"].items():
        if key == "path":
            continue
        lines.append(f"- {key}: `{value}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarize private-preview release evidence.")
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--dashboard", type=Path, required=True)
    parser.add_argument("--remaining-blockers", type=Path, required=True)
    parser.add_argument("--queue-status", type=Path, required=True)
    parser.add_argument("--queue-run", type=Path, required=True)
    parser.add_argument("--source-boundary", type=Path, required=True)
    parser.add_argument("--source-preview-export", type=Path)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    summary = build_summary(
        release_path=args.release,
        dashboard_path=args.dashboard,
        blockers_path=args.remaining_blockers,
        queue_status_path=args.queue_status,
        queue_run_path=args.queue_run,
        source_boundary_path=args.source_boundary,
        source_preview_export_path=args.source_preview_export,
    )
    payload = json.dumps(summary, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(summary, args.markdown_out)
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
