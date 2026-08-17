from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
SCHEMA = "aihr_agent_queue_acceptance_closure_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]


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
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def artifact_reference_check(
    payload: dict[str, Any],
    *,
    key: str,
    actual_path: Path,
    root: Path,
) -> dict[str, Any]:
    source_paths = payload.get("source_paths") if isinstance(payload.get("source_paths"), dict) else {}
    source_hashes = (
        payload.get("source_hashes") if isinstance(payload.get("source_hashes"), dict) else {}
    )
    expected_path_text = source_paths.get(key)
    expected_hash = source_hashes.get(key)
    actual_hash = sha256_file(actual_path)
    actual_portable = portable_path(actual_path, root=root)
    expected_resolved = resolve_artifact(expected_path_text, root=root) if expected_path_text else None
    path_matches = (
        expected_resolved.resolve(strict=False) == actual_path.resolve(strict=False)
        if expected_resolved and expected_path_text
        else expected_path_text in (None, "")
    )
    hash_matches = bool(expected_hash and actual_hash and expected_hash == actual_hash)
    ok = bool(expected_hash) and bool(actual_hash) and hash_matches and path_matches
    return {
        "key": key,
        "expected_path": portable_path(expected_path_text, root=root),
        "actual_path": actual_portable,
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "expected_hash_present": bool(expected_hash),
        "expected_path_present": bool(expected_path_text),
        "path_matches": path_matches,
        "hash_matches": hash_matches,
        "ok": ok,
    }


def artifact_list_reference_check(
    payload: dict[str, Any],
    *,
    list_key: str,
    actual_path: Path,
    root: Path,
) -> dict[str, Any]:
    records = [item for item in (payload.get(list_key) or []) if isinstance(item, dict)]
    actual_resolved = actual_path.resolve(strict=False)
    actual_hash = sha256_file(actual_path)
    matched_record: dict[str, Any] | None = None
    for record in records:
        record_path = record.get("path")
        resolved = resolve_artifact(record_path, root=root) if record_path else None
        if resolved and resolved.resolve(strict=False) == actual_resolved:
            matched_record = record
            break
    expected_hash = matched_record.get("sha256") if matched_record else None
    hash_matches = bool(expected_hash and actual_hash and expected_hash == actual_hash)
    return {
        "list_key": list_key,
        "expected_path": portable_path(matched_record.get("path"), root=root)
        if matched_record
        else None,
        "actual_path": portable_path(actual_path, root=root),
        "expected_sha256": expected_hash,
        "actual_sha256": actual_hash,
        "matched_record_present": matched_record is not None,
        "expected_hash_present": bool(expected_hash),
        "hash_matches": hash_matches,
        "ok": bool(matched_record) and bool(expected_hash) and hash_matches,
    }


def artifact_status(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    resolved = resolve_artifact(path, root=root)
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
        ),
        "sha256": sha256_file(resolved),
    }


def status_false_contract(payload: dict[str, Any], field: str) -> bool:
    return payload.get(field) is False


def safe_report_contract(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": status_false_contract(payload, "status_update_allowed"),
        "db_writes_is_false": status_false_contract(payload, "db_writes"),
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": status_false_contract(payload, "approval_claim"),
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
    }


def manual_handoff_check(value: Any) -> bool:
    text = str(value or "").lower()
    return "record command" in text and "handoff" in text


def run_manual_handoff_evidence(run: dict[str, Any]) -> dict[str, Any]:
    expected_artifacts = [
        str(item)
        for item in (run.get("expected_artifacts") or [])
        if str(item or "").strip()
    ]
    expected_artifact_checks = [
        item for item in (run.get("expected_artifact_checks") or []) if isinstance(item, dict)
    ]
    checked_artifacts = [
        str(item.get("path"))
        for item in expected_artifact_checks
        if str(item.get("path") or "").strip()
    ]
    generated_artifacts = expected_artifacts or checked_artifacts
    command = str(run.get("command") or "").strip()
    return {
        "command_recorded": bool(command),
        "command": command or None,
        "generated_artifact_count": len(generated_artifacts),
        "generated_artifacts": generated_artifacts,
        "expected_artifact_check_count": len(expected_artifact_checks),
        "stdout_tail_recorded": bool(run.get("stdout_tail")),
        "stderr_tail_recorded": bool(run.get("stderr_tail")),
        "stdout_tail_truncated": bool(run.get("stdout_truncated")),
        "stderr_tail_truncated": bool(run.get("stderr_truncated")),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "duration_seconds": run.get("duration_seconds"),
        "evidence_recorded": bool(command) and bool(generated_artifacts),
        "evidence_note": (
            "Command and generated artifacts are copied from the queue run for operator handoff; "
            "this does not verify or close the manual handoff check."
        ),
    }


def run_closure_record(run: dict[str, Any]) -> dict[str, Any]:
    expected_artifact_checks = [
        item for item in (run.get("expected_artifact_checks") or []) if isinstance(item, dict)
    ]
    missing_artifacts = [
        item.get("path")
        for item in expected_artifact_checks
        if item.get("exists") is not True
    ]
    empty_artifacts = [
        item.get("path")
        for item in expected_artifact_checks
        if item.get("exists") is True and item.get("non_empty") is not True
    ]
    acceptance_results = [
        item for item in (run.get("acceptance_check_results") or []) if isinstance(item, dict)
    ]
    machine_contract_checks = [
        item for item in acceptance_results if item.get("machine_contract") is True
    ]
    failed_machine_contracts = [
        item.get("check") for item in machine_contract_checks if item.get("ok") is not True
    ]
    failed_acceptance_checks = [
        item.get("check") for item in acceptance_results if item.get("ok") is False
    ]
    manual_unverified = [
        str(item)
        for item in (run.get("manual_unverified_declared_acceptance_checks") or [])
        if str(item or "").strip()
    ]
    machine_unverified = [
        str(item)
        for item in (run.get("machine_unverified_declared_acceptance_checks") or [])
        if str(item or "").strip()
    ]
    unexpected_manual = [item for item in manual_unverified if not manual_handoff_check(item)]
    handoff_evidence = run_manual_handoff_evidence(run)
    machine_ok = (
        run.get("status") == "succeeded"
        and int(run.get("exit_code") or 0) == 0
        and not missing_artifacts
        and not empty_artifacts
        and not failed_machine_contracts
        and not failed_acceptance_checks
        and not machine_unverified
        and not unexpected_manual
    )
    return {
        "id": run.get("id"),
        "owner": run.get("owner"),
        "status": run.get("status"),
        "exit_code": run.get("exit_code"),
        "acceptance_verified_in_source_run": run.get("acceptance_verified") is True,
        "source_acceptance_verification_status": run.get("acceptance_verification_status"),
        "machine_closure_ok": machine_ok,
        "expected_artifact_count": len(expected_artifact_checks),
        "missing_expected_artifacts": missing_artifacts,
        "empty_expected_artifacts": empty_artifacts,
        "machine_contract_check_count": len(machine_contract_checks),
        "failed_machine_contracts": failed_machine_contracts,
        "failed_acceptance_checks": failed_acceptance_checks,
        "machine_unverified_declared_acceptance_checks": machine_unverified,
        "manual_handoff_pending_checks": manual_unverified,
        "unexpected_manual_handoff_checks": unexpected_manual,
        "remaining_manual_handoff_pending_count": len(manual_unverified),
        "manual_handoff_evidence": handoff_evidence,
    }


def build_acceptance_closure(
    *,
    queue_run_path: Path,
    operator_handoff_path: Path,
    operator_integrity_audit_path: Path,
    lineage_audit_path: Path,
    operator_next_actions_path: Path | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    queue_run = read_json(queue_run_path)
    handoff = read_json(operator_handoff_path)
    integrity = read_json(operator_integrity_audit_path)
    lineage = read_json(lineage_audit_path)
    next_actions = read_json(operator_next_actions_path) if operator_next_actions_path else {}
    runs = [item for item in (queue_run.get("runs") or []) if isinstance(item, dict)]
    run_records = [run_closure_record(run) for run in runs]
    summary = queue_run.get("summary") if isinstance(queue_run.get("summary"), dict) else {}
    selected_ids = [str(item) for item in (summary.get("selected_item_ids") or [])]
    handoff_queue_state = (
        handoff.get("queue_state") if isinstance(handoff.get("queue_state"), dict) else {}
    )
    handoff_summary = (
        handoff_queue_state.get("queue_run_summary")
        if isinstance(handoff_queue_state.get("queue_run_summary"), dict)
        else {}
    )
    handoff_selected_ids = [
        str(item) for item in (handoff_summary.get("selected_item_ids") or [])
    ]
    operator_handoff_matches_run = bool(selected_ids) and selected_ids == handoff_selected_ids
    handoff_queue_run_hash_check = artifact_list_reference_check(
        handoff,
        list_key="canonical_artifacts",
        actual_path=queue_run_path,
        root=root,
    )
    source_contracts = {
        "queue_run": safe_report_contract(queue_run),
        "operator_handoff": safe_report_contract(handoff),
        "operator_integrity_audit": safe_report_contract(integrity),
        "lineage_audit": safe_report_contract(lineage),
    }
    if next_actions:
        source_contracts["operator_next_actions"] = safe_report_contract(next_actions)
    source_contract_ok = all(
        all(contract.values()) for contract in source_contracts.values()
    )
    integrity_ok = integrity.get("ok") is True and int(integrity.get("issue_count") or 0) == 0
    lineage_ok = lineage.get("ok") is True and int(lineage.get("issue_count") or 0) == 0
    next_actions_integrity_ok = (
        not next_actions
        or (
            next_actions.get("operator_packet_integrity_ok") is True
            and int(next_actions.get("operator_packet_integrity_issue_count") or 0) == 0
        )
    )
    next_actions_queue_run_hash_check = (
        artifact_reference_check(
            next_actions,
            key="queue_run",
            actual_path=queue_run_path,
            root=root,
        )
        if next_actions
        else {}
    )
    next_actions_queue_run_hash_ok = (
        not next_actions or next_actions_queue_run_hash_check.get("ok") is True
    )
    handoff_queue_run_hash_ok = handoff_queue_run_hash_check.get("ok") is True
    machine_closure_ok = all(item["machine_closure_ok"] for item in run_records)
    remaining_manual_handoff_pending_count = sum(
        int(item["remaining_manual_handoff_pending_count"]) for item in run_records
    )
    manual_handoff_evidence_recorded_count = sum(
        1
        for item in run_records
        if item["remaining_manual_handoff_pending_count"]
        and item.get("manual_handoff_evidence", {}).get("evidence_recorded") is True
    )
    closure_support_ok = (
        source_contract_ok
        and integrity_ok
        and lineage_ok
        and next_actions_integrity_ok
        and next_actions_queue_run_hash_ok
        and operator_handoff_matches_run
        and handoff_queue_run_hash_ok
    )
    ok = machine_closure_ok and closure_support_ok
    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "ok": ok,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_paths": {
            "queue_run": portable_path(queue_run_path, root=root),
            "operator_handoff": portable_path(operator_handoff_path, root=root),
            "operator_integrity_audit": portable_path(
                operator_integrity_audit_path,
                root=root,
            ),
            "lineage_audit": portable_path(lineage_audit_path, root=root),
            "operator_next_actions": portable_path(operator_next_actions_path, root=root)
            if operator_next_actions_path
            else None,
        },
        "source_hashes": {
            "queue_run": sha256_file(queue_run_path),
            "operator_handoff": sha256_file(operator_handoff_path),
            "operator_integrity_audit": sha256_file(operator_integrity_audit_path),
            "lineage_audit": sha256_file(lineage_audit_path),
            "operator_next_actions": sha256_file(operator_next_actions_path)
            if operator_next_actions_path
            else None,
        },
        "source_contracts": source_contracts,
        "source_contract_ok": source_contract_ok,
        "queue_run_original_acceptance": {
            "acceptance_unverified_count": summary.get("acceptance_unverified_count"),
            "acceptance_unverified_declared_check_count": summary.get(
                "acceptance_unverified_declared_check_count"
            ),
            "acceptance_manual_unverified_declared_check_count": summary.get(
                "acceptance_manual_unverified_declared_check_count"
            ),
            "acceptance_machine_unverified_declared_check_count": summary.get(
                "acceptance_machine_unverified_declared_check_count"
            ),
            "acceptance_machine_contract_manual_handoff_pending_count": summary.get(
                "acceptance_machine_contract_manual_handoff_pending_count"
            ),
        },
        "closure_summary": {
            "selected_count": summary.get("selected_count"),
            "succeeded_count": summary.get("succeeded_count"),
            "failed_count": summary.get("failed_count"),
            "machine_closure_ok": machine_closure_ok,
            "operator_handoff_matches_run": operator_handoff_matches_run,
            "operator_handoff_queue_run_hash_ok": handoff_queue_run_hash_ok,
            "operator_integrity_ok": integrity_ok,
            "lineage_ok": lineage_ok,
            "operator_next_actions_integrity_ok": next_actions_integrity_ok,
            "operator_next_actions_queue_run_hash_ok": next_actions_queue_run_hash_ok,
            "remaining_manual_handoff_pending_count": remaining_manual_handoff_pending_count,
            "manual_handoff_evidence_recorded_count": manual_handoff_evidence_recorded_count,
            "manual_handoff_evidence_missing_count": max(
                remaining_manual_handoff_pending_count - manual_handoff_evidence_recorded_count,
                0,
            ),
            "remaining_machine_unverified_declared_check_count": sum(
                len(item["machine_unverified_declared_acceptance_checks"])
                for item in run_records
            ),
            "missing_expected_artifact_count": sum(
                len(item["missing_expected_artifacts"]) for item in run_records
            ),
            "empty_expected_artifact_count": sum(
                len(item["empty_expected_artifacts"]) for item in run_records
            ),
            "failed_machine_contract_count": sum(
                len(item["failed_machine_contracts"]) for item in run_records
            ),
            "source_queue_acceptance_verified_remains_unchanged": True,
            "acceptance_verified_by_this_report": False,
            "closure_status": (
                "machine_evidence_closed_manual_handoff_review_required"
                if ok and remaining_manual_handoff_pending_count
                else "machine_evidence_closed"
                if ok
                else "machine_evidence_incomplete"
            ),
        },
        "runs": run_records,
        "supporting_artifacts": {
            "operator_handoff": artifact_status(operator_handoff_path, root=root),
            "operator_integrity_audit": artifact_status(operator_integrity_audit_path, root=root),
            "lineage_audit": artifact_status(lineage_audit_path, root=root),
            "operator_next_actions": artifact_status(operator_next_actions_path, root=root)
            if operator_next_actions_path
            else None,
        },
        "supporting_source_hash_checks": {
            "operator_handoff_queue_run": handoff_queue_run_hash_check,
            "operator_next_actions_queue_run": next_actions_queue_run_hash_check
            if next_actions
            else None,
        },
        "notes": [
            "This report does not modify aihr_agent_queue_run acceptance fields.",
            "Manual handoff checks remain pending human/operator confirmation.",
            "Manual handoff evidence records queue-run commands and generated artifacts only; it is not an acceptance claim.",
            "A pass here means machine-checkable evidence is present and safe, not that release blockers are approved.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("closure_summary") if isinstance(report.get("closure_summary"), dict) else {}
    lines = [
        "# AI-HR Agent Queue Acceptance Closure",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        f"- acceptance_claim: `{report.get('acceptance_claim')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        "",
        "## Closure Summary",
        "",
    ]
    for key in (
        "selected_count",
        "succeeded_count",
        "failed_count",
        "machine_closure_ok",
        "operator_handoff_matches_run",
        "operator_handoff_queue_run_hash_ok",
        "operator_integrity_ok",
        "lineage_ok",
        "operator_next_actions_integrity_ok",
        "operator_next_actions_queue_run_hash_ok",
        "remaining_manual_handoff_pending_count",
        "manual_handoff_evidence_recorded_count",
        "manual_handoff_evidence_missing_count",
        "remaining_machine_unverified_declared_check_count",
        "missing_expected_artifact_count",
        "empty_expected_artifact_count",
        "failed_machine_contract_count",
        "acceptance_verified_by_this_report",
        "closure_status",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Runs", ""])
    for run in report.get("runs") or []:
        if not isinstance(run, dict):
            continue
        lines.append(
            "- "
            f"{run.get('id')}: machine_closure_ok=`{run.get('machine_closure_ok')}`, "
            f"source_status=`{run.get('source_acceptance_verification_status')}`, "
            f"manual_pending=`{run.get('remaining_manual_handoff_pending_count')}`"
        )
        for check in run.get("manual_handoff_pending_checks") or []:
            lines.append(f"  - manual_pending_check: `{check}`")
        evidence = (
            run.get("manual_handoff_evidence")
            if isinstance(run.get("manual_handoff_evidence"), dict)
            else {}
        )
        lines.append(
            "  - handoff_evidence_recorded: "
            f"`{evidence.get('evidence_recorded')}`; "
            f"generated_artifact_count=`{evidence.get('generated_artifact_count')}`"
        )
        if evidence.get("command"):
            lines.append(f"  - command: `{evidence.get('command')}`")
        for artifact in evidence.get("generated_artifacts") or []:
            lines.append(f"  - generated_artifact: `{artifact}`")
    lines.extend(["", "## Notes", ""])
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a report-only AI-HR agent queue acceptance closure audit."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--queue-run", type=Path, required=True)
    parser.add_argument("--operator-handoff", type=Path, required=True)
    parser.add_argument("--operator-integrity-audit", type=Path, required=True)
    parser.add_argument("--lineage-audit", type=Path, required=True)
    parser.add_argument("--operator-next-actions", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit nonzero when machine closure is incomplete.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_acceptance_closure(
        queue_run_path=resolve_artifact(args.queue_run, root=args.root) or args.queue_run,
        operator_handoff_path=resolve_artifact(args.operator_handoff, root=args.root)
        or args.operator_handoff,
        operator_integrity_audit_path=resolve_artifact(
            args.operator_integrity_audit,
            root=args.root,
        )
        or args.operator_integrity_audit,
        lineage_audit_path=resolve_artifact(args.lineage_audit, root=args.root)
        or args.lineage_audit,
        operator_next_actions_path=resolve_artifact(args.operator_next_actions, root=args.root)
        if args.operator_next_actions
        else None,
        root=args.root,
    )
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    result = {
        "ok": report.get("ok"),
        "schema": report.get("schema"),
        "out_path": str(args.out),
        "markdown_path": str(args.markdown_out) if args.markdown_out else None,
        "closure_status": report.get("closure_summary", {}).get("closure_status"),
        "remaining_manual_handoff_pending_count": report.get("closure_summary", {}).get(
            "remaining_manual_handoff_pending_count"
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and report.get("ok") is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())
