from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXIT_DEPLOYMENT_AUTHORIZED = 0
EXIT_NOT_DEPLOYABLE = 1
EXIT_PRIVATE_PREVIEW_ONLY = 2


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _get(payload: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = payload
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_status(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {
            "output_dir_exists": False,
            "output_dir_is_dir": False,
            "output_dir_resolved": None,
            "output_dir_issue": "missing_output_dir",
        }
    path = Path(value)
    try:
        resolved = path.resolve()
    except OSError:
        return {
            "output_dir_exists": False,
            "output_dir_is_dir": False,
            "output_dir_resolved": str(path),
            "output_dir_issue": "unresolvable_output_dir",
        }
    exists = resolved.exists()
    is_dir = resolved.is_dir()
    return {
        "output_dir_exists": exists,
        "output_dir_is_dir": is_dir,
        "output_dir_resolved": str(resolved),
        "output_dir_issue": None
        if is_dir
        else "output_dir_not_found"
        if not exists
        else "output_dir_not_directory",
    }


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _preview_artifact_checks(
    *,
    source_output_dir: str | None,
    source_generated_at: str | None,
    artifacts: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    missing_artifacts: list[str] = []
    missing_source_fields: list[str] = []
    output_dir_mismatches: list[dict[str, Any]] = []
    freshness_failures: list[dict[str, Any]] = []
    source_generated = _parse_timestamp(source_generated_at)
    source_path_status = _path_status(source_output_dir)

    if not source_output_dir:
        missing_source_fields.append("output_dir")
    if source_generated is None:
        missing_source_fields.append("generated_at")

    for name, payload in artifacts:
        if not payload:
            missing_artifacts.append(name)
            continue
        output_dir = payload.get("output_dir")
        if not output_dir:
            output_dir_mismatches.append(
                {
                    "artifact": name,
                    "expected_output_dir": source_output_dir,
                    "actual_output_dir": output_dir,
                    "reason": "missing_output_dir",
                }
            )
        elif source_output_dir and output_dir != source_output_dir:
            output_dir_mismatches.append(
                {
                    "artifact": name,
                    "expected_output_dir": source_output_dir,
                    "actual_output_dir": output_dir,
                    "reason": "mismatched_output_dir",
                }
            )

        generated_at = payload.get("generated_at")
        generated = _parse_timestamp(generated_at)
        if generated is None:
            freshness_failures.append(
                {
                    "artifact": name,
                    "reason": "missing_or_invalid_generated_at",
                    "source_generated_at": source_generated_at,
                    "artifact_generated_at": generated_at,
                }
            )
        elif source_generated is not None and generated is not None and generated < source_generated:
            freshness_failures.append(
                {
                    "artifact": name,
                    "reason": "artifact_older_than_source_preview_export",
                    "source_generated_at": source_generated_at,
                    "artifact_generated_at": generated_at,
                }
            )

    return {
        "required_artifacts_present": not missing_artifacts,
        "source_metadata_ok": not missing_source_fields,
        "source_output_dir_exists": source_path_status["output_dir_exists"],
        "source_output_dir_is_dir": source_path_status["output_dir_is_dir"],
        "source_output_dir_resolved": source_path_status["output_dir_resolved"],
        "source_output_dir_issue": source_path_status["output_dir_issue"],
        "same_tree_ok": not missing_source_fields and not output_dir_mismatches,
        "freshness_ok": source_generated is not None and not freshness_failures,
        "missing_artifacts": missing_artifacts,
        "missing_source_fields": missing_source_fields,
        "output_dir_mismatches": output_dir_mismatches,
        "freshness_failures": freshness_failures,
    }


def build_decision(
    *,
    preview_summary: dict[str, Any],
    tree_verification: dict[str, Any] | None = None,
    runtime_smoke: dict[str, Any] | None = None,
    secret_scan: dict[str, Any] | None = None,
    preview_summary_path: str | None = None,
    tree_verification_path: str | None = None,
    runtime_smoke_path: str | None = None,
    secret_scan_path: str | None = None,
) -> dict[str, Any]:
    tree_verification = tree_verification or {}
    runtime_smoke = runtime_smoke or {}
    secret_scan = secret_scan or {}

    source = preview_summary.get("source_preview_export") or {}
    source_boundary = preview_summary.get("source_boundary") or {}
    product = preview_summary.get("dashboard") or {}
    release = preview_summary.get("release") or {}
    source_boundary_ok = preview_summary.get("source_boundary_ok")
    source_preview_export_ok = source.get("ok") is True
    tree_ok = bool(tree_verification.get("ok", True))
    runtime_ok = bool(runtime_smoke.get("ok", True))
    secret_ok = bool(secret_scan.get("ok", True))
    supporting_evidence_freshness_ok = (
        preview_summary.get("supporting_evidence_freshness_ok", True) is True
    )
    artifact_consistency = _preview_artifact_checks(
        source_output_dir=source.get("output_dir"),
        source_generated_at=source.get("generated_at"),
        artifacts=[
            ("tree_verification", tree_verification),
            ("runtime_smoke", runtime_smoke),
            ("secret_scan", secret_scan),
        ],
    )
    tree_file_count = _int_or_none(tree_verification.get("file_count"))
    tree_expected_file_count = _int_or_none(tree_verification.get("expected_file_count"))
    tree_hash_mismatch_count = _int_or_none(tree_verification.get("hash_mismatch_count"))
    tree_missing_required_count = _int_or_none(
        tree_verification.get("missing_required_count")
    )
    tree_extra_file_count = _int_or_none(tree_verification.get("extra_file_count"))
    tree_hash_consistency_ok = bool(
        tree_ok
        and tree_hash_mismatch_count in (0, None)
        and tree_missing_required_count in (0, None)
        and tree_extra_file_count in (0, None)
        and (
            tree_file_count is None
            or tree_expected_file_count is None
            or tree_file_count == tree_expected_file_count
        )
    )

    private_preview_deployable_now = bool(
        preview_summary.get("ok")
        and source_preview_export_ok
        and tree_ok
        and runtime_ok
        and secret_ok
        and artifact_consistency["required_artifacts_present"]
        and artifact_consistency["source_metadata_ok"]
        and artifact_consistency["source_output_dir_is_dir"]
        and artifact_consistency["same_tree_ok"]
        and artifact_consistency["freshness_ok"]
        and tree_hash_consistency_ok
        and supporting_evidence_freshness_ok
    )
    stable_release_ready = bool(preview_summary.get("stable_release_ready"))
    github_push_current_branch = bool(stable_release_ready and source_boundary_ok is True)
    if stable_release_ready:
        timing = "after source branch boundary is clean"
    else:
        timing = (
            "after source-preview export review; stable release after "
            "human review and qualification coverage blockers close"
        )
    if source_boundary_ok is False:
        lfs_history_evaluated = source_boundary.get("lfs_history_evaluated") is True
        lfs_history_blocker_count = int(source_boundary.get("lfs_history_blocker_count") or 0)
        if lfs_history_evaluated and lfs_history_blocker_count:
            current_branch_step = (
                "Keep current branch GitHub push blocked until source-boundary and "
                "LFS history blockers close."
            )
        elif lfs_history_evaluated:
            current_branch_step = (
                "Keep current branch GitHub push blocked until source-boundary blockers "
                "close; LFS history was evaluated with no blockers."
            )
        else:
            current_branch_step = (
                "Keep current branch GitHub push blocked by source-boundary blockers; "
                "LFS history was not evaluated in this run."
            )
    else:
        current_branch_step = (
            "Keep current branch GitHub push blocked until source-boundary evidence is "
            "refreshed on the publication branch."
        )

    report = {
        "schema": "aihr_deployment_decision_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "deployment_execution_authorized": False,
        "human_signoff_required": True,
        "private_preview_is_not_human_signoff": True,
        "private_preview_deployable_now": private_preview_deployable_now,
        "private_preview_contract_satisfied": private_preview_deployable_now,
        "stable_release_ready": stable_release_ready,
        "github_push_current_branch": github_push_current_branch,
        "recommended_publication_level": (
            "private/draft developer preview"
            if private_preview_deployable_now and not stable_release_ready
            else "stable release"
            if stable_release_ready
            else "do not deploy"
        ),
        "recommended_timing": timing,
        "source_preview": {
            "source_package_ok": preview_summary.get("source_package_ok"),
            "source_boundary_current_branch_ok": source_boundary_ok,
            "source_boundary_path": source_boundary.get("path"),
            "source_boundary_tracked_blocker_count": source_boundary.get("tracked_blocker_count"),
            "source_boundary_lfs_history_evaluated": source_boundary.get(
                "lfs_history_evaluated"
            ),
            "source_boundary_lfs_history_blocker_count": source_boundary.get(
                "lfs_history_blocker_count"
            ),
            "source_preview_export_ok": source_preview_export_ok,
            "source_preview_export_path": source.get("path"),
            "output_dir": source.get("output_dir"),
            "output_dir_exists": artifact_consistency["source_output_dir_exists"],
            "output_dir_is_dir": artifact_consistency["source_output_dir_is_dir"],
            "output_dir_resolved": artifact_consistency["source_output_dir_resolved"],
            "output_dir_issue": artifact_consistency["source_output_dir_issue"],
            "source_preview_export_generated_at": source.get("generated_at"),
            "copied_file_count": source.get("copied_file_count"),
            "included_untracked_path_count": source.get("included_untracked_path_count"),
            "tree_verification_ok": tree_ok,
            "tree_hash_consistency_ok": tree_hash_consistency_ok,
            "tree_file_count": tree_file_count,
            "tree_expected_file_count": tree_expected_file_count,
            "tree_hash_mismatch_count": tree_hash_mismatch_count,
            "tree_missing_required_count": tree_missing_required_count,
            "tree_extra_file_count": tree_extra_file_count,
            "tree_blocked_path_count": _get(tree_verification, "summary", "blocked_path_count", default=0),
            "tree_compile_error_count": _get(tree_verification, "summary", "compile_error_count", default=0),
            "required_artifacts_present": artifact_consistency["required_artifacts_present"],
            "source_metadata_ok": artifact_consistency["source_metadata_ok"],
            "same_tree_ok": artifact_consistency["same_tree_ok"],
            "freshness_ok": artifact_consistency["freshness_ok"],
            "supporting_evidence_freshness_ok": supporting_evidence_freshness_ok,
            "supporting_evidence_freshness": preview_summary.get(
                "supporting_evidence_freshness"
            )
            or {},
            "missing_artifacts": artifact_consistency["missing_artifacts"],
            "missing_source_fields": artifact_consistency["missing_source_fields"],
            "output_dir_mismatches": artifact_consistency["output_dir_mismatches"],
            "freshness_failures": artifact_consistency["freshness_failures"],
        },
        "product_evidence": {
            "preview_allowed_by_product_evidence": preview_summary.get(
                "preview_allowed_by_product_evidence"
            ),
            "preview_evidence_complete": preview_summary.get(
                "preview_evidence_complete",
                preview_summary.get("preview_allowed_by_product_evidence"),
            ),
            "preview_is_not_approval": True,
            "dashboard_ok": product.get("ok"),
            "static_artifacts_ok": product.get("static_artifacts_ok"),
            "release_engineering_hygiene_ok": release.get("engineering_hygiene_ok"),
            "queue_run_failed_count": _get(preview_summary, "queue", "run_failed_count"),
        },
        "human_review_guardrail": product.get("review_chain_safety") or {},
        "qualification_planning": _get(
            preview_summary,
            "remaining_blockers",
            "qualification_coverage_plan_snapshot",
            default={},
        ),
        "open_stable_blockers": release.get("blocked_by") or [],
        "required_next_steps": [
            "Publish only a private/draft preview from the reviewed source-preview export tree.",
            current_branch_step,
            "Complete packet-backed human review before claiming stable recommendation quality.",
            "Run qualification collection only as operator-timed guarded API batches.",
        ],
        "source_preview_runtime_smoke": {
            "path": runtime_smoke_path,
            "ok": runtime_ok,
            "output_dir": runtime_smoke.get("output_dir"),
            "command_count": len(runtime_smoke.get("commands") or []),
            "commands": runtime_smoke.get("commands") or [],
        },
        "source_preview_secret_artifact_scan": {
            "path": secret_scan_path,
            "ok": secret_ok,
            "blocked_name_finding_count": secret_scan.get("blocked_name_finding_count"),
            "high_confidence_secret_finding_count": secret_scan.get(
                "high_confidence_secret_finding_count"
            ),
            "large_file_count": secret_scan.get("large_file_count"),
        },
        "evidence_files": [
            value
            for value in [
                preview_summary_path,
                source_boundary.get("path"),
                source.get("path"),
                tree_verification_path,
                runtime_smoke_path,
                secret_scan_path,
            ]
            if value
        ],
    }
    report["cli_exit_semantics"] = {
        "zero_exit_means": "deployment_execution_authorized",
        "default_exit_code": deployment_decision_exit_code(report),
        "deployment_authorized_exit_code": EXIT_DEPLOYMENT_AUTHORIZED,
        "not_deployable_exit_code": EXIT_NOT_DEPLOYABLE,
        "private_preview_only_exit_code": EXIT_PRIVATE_PREVIEW_ONLY,
        "private_preview_exit_is_success_for_report_generation_only": False,
    }
    return report


def deployment_decision_exit_code(report: dict[str, Any]) -> int:
    if report.get("deployment_execution_authorized") is True:
        return EXIT_DEPLOYMENT_AUTHORIZED
    if report.get("private_preview_deployable_now") is True:
        return EXIT_PRIVATE_PREVIEW_ONLY
    return EXIT_NOT_DEPLOYABLE


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Decision",
        "",
        f"- private_preview_deployable_now: `{report.get('private_preview_deployable_now')}`",
        f"- private_preview_contract_satisfied: `{report.get('private_preview_contract_satisfied')}`",
        f"- private_preview_is_not_human_signoff: `{report.get('private_preview_is_not_human_signoff')}`",
        f"- deployment_execution_authorized: `{report.get('deployment_execution_authorized')}`",
        f"- human_signoff_required: `{report.get('human_signoff_required')}`",
        f"- stable_release_ready: `{report.get('stable_release_ready')}`",
        f"- github_push_current_branch: `{report.get('github_push_current_branch')}`",
        f"- recommended_publication_level: `{report.get('recommended_publication_level')}`",
        f"- recommended_timing: `{report.get('recommended_timing')}`",
        f"- same_tree_ok: `{report.get('source_preview', {}).get('same_tree_ok')}`",
        f"- freshness_ok: `{report.get('source_preview', {}).get('freshness_ok')}`",
        f"- output_dir_is_dir: `{report.get('source_preview', {}).get('output_dir_is_dir')}`",
        f"- tree_hash_consistency_ok: `{report.get('source_preview', {}).get('tree_hash_consistency_ok')}`",
        f"- cli_exit_code: `{report.get('cli_exit_semantics', {}).get('default_exit_code')}`",
        f"- zero_exit_means: `{report.get('cli_exit_semantics', {}).get('zero_exit_means')}`",
        "",
        "## Stable Blockers",
        "",
    ]
    for blocker in report.get("open_stable_blockers") or []:
        lines.append(f"- {blocker}")
    lines.extend(["", "## Required Next Steps", ""])
    for step in report.get("required_next_steps") or []:
        lines.append(f"- {step}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview-summary", type=Path, required=True)
    parser.add_argument("--tree-verification", type=Path)
    parser.add_argument("--runtime-smoke", type=Path)
    parser.add_argument("--secret-scan", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    report = build_decision(
        preview_summary=_read_json(args.preview_summary),
        tree_verification=_read_json(args.tree_verification) if args.tree_verification else None,
        runtime_smoke=_read_json(args.runtime_smoke) if args.runtime_smoke else None,
        secret_scan=_read_json(args.secret_scan) if args.secret_scan else None,
        preview_summary_path=str(args.preview_summary),
        tree_verification_path=str(args.tree_verification) if args.tree_verification else None,
        runtime_smoke_path=str(args.runtime_smoke) if args.runtime_smoke else None,
        secret_scan_path=str(args.secret_scan) if args.secret_scan else None,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        write_markdown(report, args.markdown_out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return deployment_decision_exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
