from __future__ import annotations

from collections import Counter
import csv
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
from typing import Any

from ncs_mcp.sqf_review_decision_audit import build_sqf_review_decision_audit


SQF_GUARDED_IMPORT_PLAN_SCHEMA = "ncs_sqf_guarded_import_plan_v1"
FORMAT_VERSION = "ncs-sqf-guarded-import-plan-v1"
FORBIDDEN_MARKERS = (
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
)
REPORT_ONLY_GUARDRAILS = {
    "approval_ready": False,
    "db_writes": False,
    "status_update_allowed": False,
    "used_for_scoring": False,
    "approval_claim": False,
}


def build_sqf_guarded_import_plan(
    *,
    decision_sheet_path: str | Path,
    claim_report_path: str | Path,
    decision_audit_path: str | Path | None = None,
    db_path: str | Path | None = None,
    run_artifact_name: str | None = None,
) -> dict[str, Any]:
    sheet_path = Path(decision_sheet_path)
    claim_path = Path(claim_report_path)
    findings: list[dict[str, Any]] = []
    rows = _read_decision_rows(sheet_path, findings)
    claims = _load_claims(claim_path, findings)
    claim_by_id = {str(claim.get("claim_id")): claim for claim in claims if claim.get("claim_id")}
    audit = _load_or_build_audit(sheet_path, decision_audit_path, findings)
    audit_by_order = {
        str(row.get("order") or row.get("row_number")): row
        for row in (audit.get("rows") or [])
        if isinstance(row, dict)
    }
    audit_by_claim = {
        str(row.get("claim_id")): row
        for row in (audit.get("rows") or [])
        if isinstance(row, dict) and row.get("claim_id")
    }

    plan_items: list[dict[str, Any]] = []
    deferred_items: list[dict[str, Any]] = []
    pending_count = 0
    invalid_count = _as_int((audit.get("summary") or {}).get("invalid_count"))
    decision_counts: Counter[str] = Counter()
    decision_sheet_hash = _file_sha256(sheet_path) if sheet_path.exists() else None
    source_decision_packet = _safe_artifact_name(sheet_path)
    run_artifact = _safe_artifact_name(Path(run_artifact_name)) if run_artifact_name else None
    db_matches = _load_db_match_snapshots(db_path, findings)
    db_check = {
        "performed": db_matches is not None,
        "checked_plan_item_count": 0,
        "stale_or_missing_count": 0,
    }

    if audit.get("ok") is False:
        findings.append(
            {
                "severity": "blocker",
                "code": "decision_audit_not_ok",
                "message": "Decision audit must be ok=true before an import plan can be considered.",
            }
        )

    for row in rows:
        decision = str(row.get("decision") or "").strip().lower()
        if not decision:
            pending_count += 1
            continue
        decision_counts[decision] += 1
        if decision == "defer":
            deferred_items.append(_deferred_projection(row))
            continue
        if decision not in {"approve", "reject"}:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "decision_not_plannable",
                    "order": row.get("order"),
                    "claim_id": row.get("claim_id"),
                    "message": "Only approve/reject rows can become guarded import plan items.",
                }
            )
            continue

        audit_row = audit_by_order.get(str(row.get("order") or "")) or audit_by_claim.get(str(row.get("claim_id") or ""))
        if not audit_row or audit_row.get("valid") is not True:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "decision_row_not_audit_valid",
                    "order": row.get("order"),
                    "claim_id": row.get("claim_id"),
                    "message": "Decision row must be valid in the SQF decision audit before planning import.",
                }
            )
            continue

        claim = claim_by_id.get(str(row.get("claim_id") or ""))
        if claim is None:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "claim_not_found",
                    "order": row.get("order"),
                    "claim_id": row.get("claim_id"),
                    "message": "Decision row claim_id is missing from the claim report.",
                }
            )
            continue

        item = _build_plan_item(
            row=row,
            claim=claim,
            decision=decision,
            source_decision_packet=source_decision_packet,
            source_artifact_hash=decision_sheet_hash,
            run_artifact=run_artifact,
            findings=findings,
        )
        if item:
            if not _db_match_still_candidate(item, db_matches, findings):
                db_check["stale_or_missing_count"] += 1
                continue
            if db_matches is not None:
                db_check["checked_plan_item_count"] += 1
            plan_items.append(item)

    blocker_count = sum(1 for finding in findings if finding.get("severity") == "blocker")
    plan_decision_counts = Counter(item["decision"] for item in plan_items)
    return {
        "ok": blocker_count == 0,
        "schema": SQF_GUARDED_IMPORT_PLAN_SCHEMA,
        "format_version": FORMAT_VERSION,
        "record_type": "sqf_guarded_import_plan",
        "status": "dry_run_only",
        **REPORT_ONLY_GUARDRAILS,
        "execution_allowed": False,
        "operator_authorization_required": True,
        "planned_db_writes": 0,
        "allowed_use": "supplementary_review_context_only",
        "sources": {
            "decision_sheet": {"name": _safe_artifact_name(sheet_path), "sha256": decision_sheet_hash},
            "claim_report": {"name": _safe_artifact_name(claim_path), "claim_count": len(claims)},
            "decision_audit": {
                "name": _safe_artifact_name(Path(decision_audit_path)) if decision_audit_path else None,
                "ok": audit.get("ok"),
            },
            "db_status_check": db_check,
        },
        "summary": {
            "row_count": len(rows),
            "pending_count": pending_count,
            "defer_count": decision_counts.get("defer", 0),
            "completed_decision_count": sum(decision_counts.values()),
            "invalid_count": invalid_count,
            "plan_item_count": len(plan_items),
            "approve_plan_count": plan_decision_counts.get("approve", 0),
            "reject_plan_count": plan_decision_counts.get("reject", 0),
            "db_status_check_performed": db_check["performed"],
            "db_status_stale_or_missing_count": db_check["stale_or_missing_count"],
            "blocker_count": blocker_count,
        },
        "review_policy": {
            "dry_run_only": True,
            "no_status_updates_performed": True,
            "guarded_import_execution_required": True,
            "operator_authorization_required": True,
            "status_mapping_policy_required": True,
            "approve_does_not_auto_map_to_trusted_status": True,
            "reject_status_requires_operator_policy": True,
            "prohibited_auto_statuses": ["human_reviewed", "accepted", "reviewed"],
            "sqf_active_recommendation_scoring": False,
            **REPORT_ONLY_GUARDRAILS,
        },
        "plan_items": plan_items,
        "deferred_items": deferred_items,
        "findings": findings,
        "notes": [
            "This is a dry-run plan and performs no DB writes.",
            "Server call templates are not executable without a separate operator authorization step.",
            "SQF review output remains supplementary context and must not become active recommendation scoring evidence.",
        ],
    }


def write_sqf_guarded_import_plan_json(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_sqf_guarded_import_plan_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# SQF Guarded Import Plan",
        "",
        "## Contract",
        "",
        f"- ok: {str(report.get('ok')).lower()}",
        f"- schema: {report.get('schema')}",
        f"- format_version: {report.get('format_version')}",
        f"- status: {report.get('status')}",
        f"- execution_allowed: {str(report.get('execution_allowed')).lower()}",
        f"- operator_authorization_required: {str(report.get('operator_authorization_required')).lower()}",
        f"- planned_db_writes: {summary.get('planned_db_writes', report.get('planned_db_writes', 0))}",
        f"- db_writes: {str(report.get('db_writes')).lower()}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        f"- used_for_scoring: {str(report.get('used_for_scoring')).lower()}",
        f"- approval_claim: {str(report.get('approval_claim')).lower()}",
        "",
        "## Summary",
        "",
        f"- row_count: {summary.get('row_count', 0)}",
        f"- pending_count: {summary.get('pending_count', 0)}",
        f"- completed_decision_count: {summary.get('completed_decision_count', 0)}",
        f"- invalid_count: {summary.get('invalid_count', 0)}",
        f"- plan_item_count: {summary.get('plan_item_count', 0)}",
        f"- approve_plan_count: {summary.get('approve_plan_count', 0)}",
        f"- reject_plan_count: {summary.get('reject_plan_count', 0)}",
        f"- defer_count: {summary.get('defer_count', 0)}",
        f"- blocker_count: {summary.get('blocker_count', 0)}",
        "",
        "## Plan Items",
        "",
        "| order | claim | decision | match | status policy | execution allowed |",
        "|---:|---|---|---:|---|---|",
    ]
    for item in report.get("plan_items") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| "
            f"{_md(item.get('order'))} | "
            f"{_md(item.get('claim_id'))} | "
            f"{_md(item.get('decision'))} | "
            f"{_md(item.get('match_id'))} | "
            f"{_md((item.get('server_call_template') or {}).get('new_status_policy'))} | "
            f"{str(item.get('execution_allowed')).lower()} |"
        )
    findings = [finding for finding in report.get("findings") or [] if isinstance(finding, dict)]
    if findings:
        lines.extend(["", "## Findings", ""])
        for finding in findings:
            lines.append(
                "- "
                f"{_md(finding.get('severity'))}:{_md(finding.get('code'))} "
                f"{_md(finding.get('claim_id') or finding.get('order') or '')} "
                f"{_md(finding.get('message'))}"
            )
    return "\n".join(lines).rstrip() + "\n"


def write_sqf_guarded_import_plan_markdown(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_sqf_guarded_import_plan_markdown(report), encoding="utf-8")


def _build_plan_item(
    *,
    row: dict[str, str],
    claim: dict[str, Any],
    decision: str,
    source_decision_packet: str,
    source_artifact_hash: str | None,
    run_artifact: str | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any] | None:
    match = claim.get("sqf_ncs_match") if isinstance(claim.get("sqf_ncs_match"), dict) else {}
    match_id = _as_int(match.get("match_id"))
    if match_id <= 0:
        findings.append(
            {
                "severity": "blocker",
                "code": "match_id_missing",
                "order": row.get("order"),
                "claim_id": row.get("claim_id"),
                "message": "Claim is missing sqf_ncs_match.match_id.",
            }
        )
        return None

    evidence_refs = _split_refs(row.get("top_evidence_refs"))
    relation = _safe_text(row.get("mapping_relation") or match.get("relation"))
    rationale = _safe_text(row.get("reason"))
    notes = _safe_text(row.get("notes"))
    reviewer_id = _safe_text(row.get("reviewer_id"))
    reviewed_at = _safe_text(row.get("reviewed_at"))
    claim_packet = _safe_text(row.get("source_packet"))
    return {
        "order": _safe_text(row.get("order")),
        "claim_id": _safe_text(row.get("claim_id")),
        "decision": decision,
        "action": "review_sqf_ncs_match",
        "execution_allowed": False,
        "operator_authorization_required": True,
        "match_id": match_id,
        "ncs_unit_code": _safe_text(row.get("ncs_unit_code")),
        "ncs_unit_name": _safe_text(row.get("ncs_unit_name")),
        "source_claim_packet": claim_packet,
        "evidence_ref_count": len(evidence_refs),
        "server_call_template": {
            "tool": "review_sqf_ncs_match",
            "match_id": match_id,
            "new_status": None,
            "new_status_policy": "operator_status_mapping_required",
            "source_decision": decision,
            "suggested_status_options": ["reviewed"] if decision == "approve" else ["rejected"],
            "reviewer_id": reviewer_id,
            "notes": notes,
            "relation": relation,
            "source_decision_packet": source_decision_packet,
            "source_artifact_hash": source_artifact_hash,
            "rationale": rationale,
            "evidence_refs": evidence_refs,
            "run_artifact": run_artifact,
        },
        "safety": {
            "dry_run_only": True,
            "db_writes": False,
            "status_update_allowed": False,
            "used_for_scoring": False,
            "approval_claim": False,
            "sqf_active_recommendation_scoring": False,
        },
    }


def _read_decision_rows(path: Path, findings: list[dict[str, Any]]) -> list[dict[str, str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [{str(key): str(value or "") for key, value in row.items() if key is not None} for row in csv.DictReader(handle)]
    except FileNotFoundError:
        findings.append(
            {
                "severity": "blocker",
                "code": "decision_sheet_missing",
                "message": "Decision sheet CSV is missing.",
            }
        )
        return []


def _load_claims(path: Path, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        findings.append(
            {
                "severity": "blocker",
                "code": "claim_report_unreadable",
                "message": "Claim report JSON could not be loaded.",
            }
        )
        return []
    claims = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(claims, list):
        findings.append(
            {
                "severity": "blocker",
                "code": "claim_report_missing_claims",
                "message": "Claim report must contain a claims list.",
            }
        )
        return []
    return [claim for claim in claims if isinstance(claim, dict)]


def _load_or_build_audit(
    sheet_path: Path,
    audit_path: str | Path | None,
    findings: list[dict[str, Any]],
) -> dict[str, Any]:
    if audit_path:
        try:
            payload = json.loads(Path(audit_path).read_text(encoding="utf-8-sig"))
            return payload if isinstance(payload, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            findings.append(
                {
                    "severity": "blocker",
                    "code": "decision_audit_unreadable",
                    "message": "Decision audit JSON could not be loaded.",
                }
            )
            return {}
    return build_sqf_review_decision_audit(sheet_path)


def _load_db_match_snapshots(
    db_path: str | Path | None,
    findings: list[dict[str, Any]],
) -> dict[int, dict[str, Any]] | None:
    if db_path is None:
        findings.append(
            {
                "severity": "warning",
                "code": "db_status_check_not_performed",
                "message": "No DB path was supplied, so current sqf_ncs_matches status was not checked.",
            }
        )
        return None
    path = Path(db_path)
    if not path.exists():
        findings.append(
            {
                "severity": "blocker",
                "code": "db_status_check_unavailable",
                "message": "DB status check was requested but the SQLite DB was not found.",
            }
        )
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT match_id, review_status, relation, target_id
                FROM sqf_ncs_matches
                """
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        findings.append(
            {
                "severity": "blocker",
                "code": "db_status_check_failed",
                "message": "Could not read sqf_ncs_matches for guarded import planning.",
            }
        )
        return {}
    return {int(row["match_id"]): dict(row) for row in rows}


def _db_match_still_candidate(
    item: dict[str, Any],
    db_matches: dict[int, dict[str, Any]] | None,
    findings: list[dict[str, Any]],
) -> bool:
    if db_matches is None:
        return True
    match_id = _as_int(item.get("match_id"))
    row = db_matches.get(match_id)
    if row is None:
        findings.append(
            {
                "severity": "blocker",
                "code": "db_match_missing",
                "order": item.get("order"),
                "claim_id": item.get("claim_id"),
                "message": "sqf_ncs_matches row no longer exists for this decision.",
            }
        )
        return False
    status = str(row.get("review_status") or "").strip()
    if status != "candidate":
        findings.append(
            {
                "severity": "blocker",
                "code": "db_match_status_not_candidate",
                "order": item.get("order"),
                "claim_id": item.get("claim_id"),
                "match_id": match_id,
                "current_status": status,
                "message": "sqf_ncs_matches row must still be candidate before any guarded import planning.",
            }
        )
        return False
    target_id = str(row.get("target_id") or "").strip()
    if target_id and target_id != str(item.get("ncs_unit_code") or "").strip():
        findings.append(
            {
                "severity": "blocker",
                "code": "db_match_target_drift",
                "order": item.get("order"),
                "claim_id": item.get("claim_id"),
                "match_id": match_id,
                "message": "sqf_ncs_matches target_id no longer matches the decision row.",
            }
        )
        return False
    return True


def _deferred_projection(row: dict[str, str]) -> dict[str, Any]:
    return {
        "order": _safe_text(row.get("order")),
        "claim_id": _safe_text(row.get("claim_id")),
        "decision": "defer",
        "reason_present": bool(str(row.get("reason") or row.get("defer_reason_code") or "").strip()),
        "execution_allowed": False,
    }


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _safe_artifact_name(path: Path) -> str:
    name = path.name
    return "redacted_artifact" if _contains_forbidden(name) else name


def _safe_text(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    if _contains_forbidden(text) or _looks_absolute(text):
        return ""
    return text


def _split_refs(value: Any) -> list[str]:
    refs: list[str] = []
    for chunk in str(value or "").replace(",", ";").split(";"):
        ref = _safe_text(chunk)
        if ref:
            refs.append(ref)
    return refs


def _contains_forbidden(value: Any) -> bool:
    text = str(value or "").casefold()
    return any(marker in text for marker in FORBIDDEN_MARKERS)


def _looks_absolute(value: str) -> bool:
    text = value.strip()
    if not text or "://" in text:
        return False
    return PureWindowsPath(text).is_absolute() or PurePosixPath(text).is_absolute()


def _as_int(value: Any) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _md(value: Any) -> str:
    return _safe_text(value).replace("|", "\\|").replace("\n", " ")
