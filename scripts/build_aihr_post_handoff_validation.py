from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_post_handoff_validation_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
ALLOWED_CROSSWALK_WARNING_CODES = {"legacy_gap_recommended_packet_artifact_missing"}


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


def artifact_status(path: str | Path | None, *, root: Path) -> dict[str, Any]:
    resolved = resolve_artifact(path, root=root)
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
        ),
        "sha256": sha256_file(resolved),
    }


def safety_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True or payload.get("schema")
        == "aihr_release_operator_refresh_dag_v1",
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
    }


def add_issue(issues: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    issues.append({"code": code, "message": message, **extra})


def add_warning(
    warnings: list[dict[str, Any]],
    code: str,
    message: str,
    **extra: Any,
) -> None:
    warnings.append({"code": code, "message": message, **extra})


def check_safe_source(
    *,
    label: str,
    payload: dict[str, Any],
    issues: list[dict[str, Any]],
) -> dict[str, bool]:
    contract = safety_contract(payload)
    if not all(contract.values()):
        add_issue(
            issues,
            "unsafe_source_contract",
            "Source artifact does not preserve the report-only safety contract.",
            source=label,
            contract=contract,
        )
    return contract


def source_hash_checks(
    source_hashes: dict[str, str | None],
    *,
    artifacts: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        key: {
            "check_scope": "current_generation_snapshot_only",
            "lineage_validation": False,
            "path": artifacts[key].get("path"),
            "expected_sha256": expected,
            "actual_sha256": artifacts[key].get("sha256"),
            "hash_matches": bool(expected and artifacts[key].get("sha256") == expected),
        }
        for key, expected in source_hashes.items()
        if key in artifacts
    }


def dag_contains_post_handoff_node(release_dag: dict[str, Any]) -> bool:
    nodes = release_dag.get("nodes") if isinstance(release_dag.get("nodes"), list) else []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        text = json.dumps(node, ensure_ascii=False).lower()
        if "post_handoff" in text or "post-handoff" in text:
            return True
    return False


def build_validation(
    *,
    handoff_path: Path,
    release_dag_path: Path,
    release_dag_audit_path: Path,
    acceptance_closure_path: Path,
    powershell_compatibility_path: Path,
    readability_audit_path: Path,
    operator_integrity_audit_path: Path,
    lineage_audit_path: Path,
    crosswalk_audit_path: Path,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    source_paths = {
        "operator_handoff": handoff_path,
        "release_operator_refresh_dag": release_dag_path,
        "release_operator_refresh_dag_audit": release_dag_audit_path,
        "agent_queue_acceptance_closure": acceptance_closure_path,
        "operator_json_powershell_compatibility_audit": powershell_compatibility_path,
        "operator_primary_packet_readability_audit": readability_audit_path,
        "operator_packet_integrity_audit": operator_integrity_audit_path,
        "operator_report_lineage_sync_audit": lineage_audit_path,
        "transition_provenance_crosswalk_audit": crosswalk_audit_path,
    }
    payloads = {key: read_json(resolve_artifact(path, root=root)) for key, path in source_paths.items()}
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    artifacts = {
        key: artifact_status(path, root=root)
        for key, path in source_paths.items()
    }
    expected_source_hashes = {
        key: status.get("sha256")
        for key, status in artifacts.items()
    }
    for key, status in artifacts.items():
        if not status["exists_nonempty"]:
            add_issue(issues, "source_artifact_missing", "Required source artifact is missing.", source=key)

    source_contracts = {
        key: check_safe_source(label=key, payload=payload, issues=issues)
        for key, payload in payloads.items()
        if payload
    }

    handoff = payloads["operator_handoff"]
    if handoff.get("human_decision_required") is not True:
        add_issue(
            issues,
            "handoff_missing_human_decision_gate",
            "Operator handoff must preserve human_decision_required=true.",
        )

    release_dag = payloads["release_operator_refresh_dag"]
    release_dag_audit = payloads["release_operator_refresh_dag_audit"]
    if release_dag.get("ok") is not True:
        add_issue(issues, "release_operator_refresh_dag_not_ok", "Refresh DAG did not report ok=true.")
    if dag_contains_post_handoff_node(release_dag):
        add_issue(
            issues,
            "post_handoff_in_release_dag",
            "Post-handoff validation must stay out of the release refresh DAG.",
        )
    if release_dag_audit.get("ok") is not True or int(release_dag_audit.get("issue_count") or 0) != 0:
        add_issue(issues, "release_operator_refresh_dag_audit_not_clean", "Refresh DAG audit has issues.")

    acceptance = payloads["agent_queue_acceptance_closure"]
    closure = acceptance.get("closure_summary") if isinstance(acceptance.get("closure_summary"), dict) else {}
    if acceptance.get("ok") is not True:
        add_issue(issues, "acceptance_closure_not_ok", "Agent queue acceptance closure did not pass.")
    if acceptance.get("acceptance_claim") is not False:
        add_issue(issues, "acceptance_closure_claims_acceptance", "Closure must not claim acceptance.")
    if closure.get("acceptance_verified_by_this_report") is not False:
        add_issue(
            issues,
            "acceptance_closure_claims_verification",
            "Closure must leave source acceptance verification unchanged.",
        )

    powershell = payloads["operator_json_powershell_compatibility_audit"]
    if powershell.get("ok") is not True or int(powershell.get("finding_count") or 0) != 0:
        add_issue(
            issues,
            "operator_json_powershell_compatibility_not_clean",
            "Operator JSON PowerShell compatibility audit has findings.",
        )
    if int(powershell.get("python_ok_powershell_failed_count") or 0) != 0:
        add_issue(
            issues,
            "python_ok_powershell_failed",
            "At least one Python-valid JSON artifact failed PowerShell ConvertFrom-Json.",
        )

    readability = payloads["operator_primary_packet_readability_audit"]
    if readability.get("ok") is not True or int(readability.get("finding_count") or 0) != 0:
        add_issue(
            issues,
            "operator_readability_audit_not_clean",
            "Operator readability audit has findings.",
        )

    integrity = payloads["operator_packet_integrity_audit"]
    if integrity.get("ok") is not True or int(integrity.get("issue_count") or 0) != 0:
        add_issue(issues, "operator_packet_integrity_not_clean", "Operator packet integrity audit has issues.")
    if int(integrity.get("warning_count") or 0) > 0:
        add_warning(
            warnings,
            "operator_packet_integrity_warnings_present",
            "Operator packet integrity warnings are non-blocking only when all warnings are explainable.",
            warning_count=integrity.get("warning_count"),
            source_warnings=integrity.get("warnings") or [],
        )

    lineage = payloads["operator_report_lineage_sync_audit"]
    if lineage.get("ok") is not True or int(lineage.get("issue_count") or 0) != 0:
        add_issue(issues, "operator_lineage_not_clean", "Operator report lineage audit has issues.")

    crosswalk = payloads["transition_provenance_crosswalk_audit"]
    crosswalk_warning_codes = {str(item.get("code")) for item in crosswalk.get("warnings") or []}
    if crosswalk.get("ok") is not True or int(crosswalk.get("issue_count") or 0) != 0:
        add_issue(issues, "crosswalk_audit_not_clean", "Transition crosswalk audit has issues.")
    disallowed = sorted(crosswalk_warning_codes - ALLOWED_CROSSWALK_WARNING_CODES)
    if disallowed:
        add_issue(
            issues,
            "crosswalk_audit_has_disallowed_warning_codes",
            "Transition crosswalk audit has warnings that are not approved as terminal diagnostics.",
            warning_codes=disallowed,
        )
    if crosswalk_warning_codes:
        add_warning(
            warnings,
            "crosswalk_audit_terminal_warnings_present",
            "Transition crosswalk warnings are terminal diagnostics and must not block when issue_count is zero.",
            warning_codes=sorted(crosswalk_warning_codes),
            warning_count=crosswalk.get("warning_count"),
        )

    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "ok": not issues,
        "status": "pass" if not issues else "review_required",
        "terminal_evidence_only": True,
        "include_in_release_refresh_dag": False,
        "include_in_operator_handoff": False,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_hash_cycle_policy": {
            "depends_on_operator_handoff": True,
            "must_not_be_source_for_handoff_or_refresh_dag": True,
            "reason": "This report validates post-handoff terminal evidence and would create a cycle if used as an upstream handoff/DAG source.",
        },
        "source_paths": {
            key: portable_path(path, root=root)
            for key, path in source_paths.items()
        },
        "source_hashes": {
            key: expected_source_hashes.get(key)
            for key in artifacts
        },
        "source_hash_check_scope": {
            "source_hash_checks": "current_generation_snapshot_only",
            "lineage_validation": False,
            "lineage_validation_sources": ["operator_report_lineage_sync_audit"],
        },
        "source_hash_checks": source_hash_checks(expected_source_hashes, artifacts=artifacts),
        "source_contracts": source_contracts,
        "artifact_status": artifacts,
        "summary": {
            "issue_count": len(issues),
            "warning_count": len(warnings),
            "acceptance_closure_status": closure.get("closure_status"),
            "acceptance_manual_pending_count": closure.get(
                "remaining_manual_handoff_pending_count"
            ),
            "powershell_artifact_count": powershell.get("artifact_count"),
            "python_ok_powershell_failed_count": powershell.get(
                "python_ok_powershell_failed_count"
            ),
            "readability_artifact_count": readability.get("artifact_count"),
            "crosswalk_warning_count": crosswalk.get("warning_count"),
            "crosswalk_warning_codes": sorted(crosswalk_warning_codes),
        },
        "issues": issues,
        "warnings": warnings,
        "notes": [
            "This terminal validation summarizes evidence produced after the operator handoff.",
            "It must not be fed back into the handoff or release refresh DAG as a source artifact.",
            "A pass is not approval and does not modify acceptance, human_reviewed, accepted, or reviewed statuses.",
            "source_hash_checks are same-run snapshot checks; upstream report lineage is represented by operator_report_lineage_sync_audit.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Post-Handoff Validation",
        "",
        f"- schema: `{payload.get('schema')}`",
        f"- generated_at: `{payload.get('generated_at')}`",
        f"- status: `{payload.get('status')}`",
        f"- ok: `{payload.get('ok')}`",
        f"- terminal_evidence_only: `{payload.get('terminal_evidence_only')}`",
        f"- include_in_release_refresh_dag: `{payload.get('include_in_release_refresh_dag')}`",
        f"- include_in_operator_handoff: `{payload.get('include_in_operator_handoff')}`",
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
        "issue_count",
        "warning_count",
        "acceptance_closure_status",
        "acceptance_manual_pending_count",
        "powershell_artifact_count",
        "python_ok_powershell_failed_count",
        "readability_artifact_count",
        "crosswalk_warning_count",
        "crosswalk_warning_codes",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Cycle Policy", ""])
    for key, value in (payload.get("source_hash_cycle_policy") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Sources", ""])
    for key, status in (payload.get("artifact_status") or {}).items():
        lines.append(
            f"- {key}: `{status.get('path')}` exists_nonempty=`{status.get('exists_nonempty')}` "
            f"sha256=`{status.get('sha256')}`"
        )
    if payload.get("issues"):
        lines.extend(["", "## Issues", ""])
        for issue in payload.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: {issue.get('message')}")
    else:
        lines.extend(["", "No post-handoff validation issues found."])
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in payload.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: {warning.get('message')}")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build terminal AI-HR post-handoff validation.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--handoff", type=Path, required=True)
    parser.add_argument("--release-dag", type=Path, required=True)
    parser.add_argument("--release-dag-audit", type=Path, required=True)
    parser.add_argument("--acceptance-closure", type=Path, required=True)
    parser.add_argument("--powershell-compatibility", type=Path, required=True)
    parser.add_argument("--readability-audit", type=Path, required=True)
    parser.add_argument("--operator-integrity-audit", type=Path, required=True)
    parser.add_argument("--lineage-audit", type=Path, required=True)
    parser.add_argument("--crosswalk-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_validation(
        handoff_path=resolve_artifact(args.handoff, root=args.root) or args.handoff,
        release_dag_path=resolve_artifact(args.release_dag, root=args.root) or args.release_dag,
        release_dag_audit_path=resolve_artifact(args.release_dag_audit, root=args.root)
        or args.release_dag_audit,
        acceptance_closure_path=resolve_artifact(args.acceptance_closure, root=args.root)
        or args.acceptance_closure,
        powershell_compatibility_path=resolve_artifact(args.powershell_compatibility, root=args.root)
        or args.powershell_compatibility,
        readability_audit_path=resolve_artifact(args.readability_audit, root=args.root)
        or args.readability_audit,
        operator_integrity_audit_path=resolve_artifact(args.operator_integrity_audit, root=args.root)
        or args.operator_integrity_audit,
        lineage_audit_path=resolve_artifact(args.lineage_audit, root=args.root) or args.lineage_audit,
        crosswalk_audit_path=resolve_artifact(args.crosswalk_audit, root=args.root)
        or args.crosswalk_audit,
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
