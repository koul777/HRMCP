from __future__ import annotations

from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


SQF_REVIEW_DECISION_AUDIT_SCHEMA = "ncs_sqf_review_decision_audit_v1"
FORMAT_VERSION = "ncs-sqf-report-claim-decision-audit-v1"
ALLOWED_DECISIONS = ("blank", "approve", "reject", "defer")
REPORT_ONLY_CONTRACT = {
    "status_update_allowed": False,
    "used_for_scoring": False,
    "approval_claim": False,
    "db_writes": False,
}
RECORD_DECISION_FIELDS = (
    "decision",
    "reason",
    "reject_reason_code",
    "defer_reason_code",
    "notes",
    "reviewer_id",
    "reviewed_at",
    "source_packet",
    "status_update_allowed",
    "used_for_scoring",
    "approval_claim",
)
FORBIDDEN_TOKENS = {
    "asset_path",
    "local_path",
    "db_path",
    "source_payload",
    "raw_payload",
    "raw_response",
}
SAFE_ROW_FIELDS = (
    "order",
    "claim_id",
    "claim_type",
    "recommended_priority",
    "job_name",
    "duty_name",
    "ncs_unit_code",
    "ncs_unit_name",
    "mapping_relation",
    "evidence_strength",
    "scope_alignment",
    "review_risk_flags",
    "review_action_hint",
)
MAX_MARKDOWN_ROWS = 80


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def build_sqf_review_decision_audit(decision_sheet_path: str | Path) -> dict[str, Any]:
    """Build a report-only audit for an SQF claim decision-sheet CSV."""
    path = Path(decision_sheet_path)
    rows, fieldnames = _read_csv_rows(path)
    sensitive_header_count = sum(1 for field in fieldnames if _contains_forbidden_token(field))

    audited_rows: list[dict[str, Any]] = []
    missing_required_counts: Counter[str] = Counter()
    decision_counts: Counter[str] = Counter()
    invalid_count = 0
    guardrail_issue_count = 0
    sensitive_reference_count = sensitive_header_count
    valid_completed_count = 0
    import_ready_count = 0
    defer_ready_count = 0

    for row_number, row in enumerate(rows, start=1):
        audited = _audit_row(row_number, row, has_sensitive_header=sensitive_header_count > 0)
        audited_rows.append(audited)
        decision_counts[audited["decision"]] += 1
        if not audited["valid"]:
            invalid_count += 1
        elif audited["decision"] in {"approve", "reject", "defer"}:
            valid_completed_count += 1
        if audited.get("status_update_candidate"):
            import_ready_count += 1
        if audited["valid"] and audited["decision"] == "defer":
            defer_ready_count += 1
        if "guardrail_contract_not_false" in audited["issue_codes"]:
            guardrail_issue_count += 1
        if "sensitive_reference" in audited["issue_codes"]:
            sensitive_reference_count += audited.get("sensitive_reference_count", 0)
        missing_required_counts.update(audited["missing_required"])

    summary = {
        "row_count": len(rows),
        "blank_count": decision_counts.get("blank", 0),
        "approve_count": decision_counts.get("approve", 0),
        "reject_count": decision_counts.get("reject", 0),
        "defer_count": decision_counts.get("defer", 0),
        "invalid_count": invalid_count,
        "completed_decision_count": sum(decision_counts.get(decision, 0) for decision in ("approve", "reject", "defer")),
        "valid_completed_decision_count": valid_completed_count,
        "pending_review_count": decision_counts.get("blank", 0),
        "status_update_candidate_count": import_ready_count,
        "guarded_import_candidate_count": import_ready_count,
        "pre_import_annotation_count": valid_completed_count,
        "import_ready_count": import_ready_count,
        "defer_ready_count": defer_ready_count,
        "missing_required_counts": dict(sorted(missing_required_counts.items())),
    }

    return {
        "ok": invalid_count == 0,
        "schema": SQF_REVIEW_DECISION_AUDIT_SCHEMA,
        "format_version": FORMAT_VERSION,
        "record_type": "sqf_report_claim_decision_audit",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "decision_sheet_name": path.name,
        **REPORT_ONLY_CONTRACT,
        "execution_allowed": False,
        "pre_import_annotation_only": True,
        "allowed_decisions": list(ALLOWED_DECISIONS),
        "review_policy": {
            "report_only": True,
            "pre_import_annotation_only": True,
            "no_status_updates_performed": True,
            "guarded_import_required_for_status_change": True,
            "requires_explicit_human_decision": True,
            "allowed_decisions": list(ALLOWED_DECISIONS),
            "approve_required_fields": [
                "reviewer_id",
                "reviewed_at",
                "reason_or_rationale",
                "source_packet",
                "top_evidence_refs",
            ],
            "reject_required_fields": ["reject_reason_code_or_reason"],
            "defer_required_fields": ["defer_reason_code_or_reason"],
            **REPORT_ONLY_CONTRACT,
        },
        "summary": summary,
        "guardrail_issue_count": guardrail_issue_count,
        "sensitive_reference_count": sensitive_reference_count,
        "rows": audited_rows,
        "notes": [
            "This audit is report-only and performs no DB writes.",
            "Recorded approve/reject/defer values are validation inputs, not automatic status changes.",
            "A separate guarded import with explicit human authorization is required before any status update.",
        ],
    }


def record_sqf_review_decision(
    *,
    decision_sheet_path: str | Path,
    out_csv_path: str | Path,
    decision: str,
    order: str | None = None,
    claim_id: str | None = None,
    reason: str = "",
    reject_reason_code: str = "",
    defer_reason_code: str = "",
    notes: str = "",
    reviewer_id: str = "human_reviewer",
    reviewed_at: str | None = None,
    source_packet: str | None = None,
    overwrite_existing: bool = False,
) -> dict[str, Any]:
    created_at = datetime.now(timezone.utc).isoformat()
    sheet_path = Path(decision_sheet_path)
    output_path = Path(out_csv_path)
    findings: list[dict[str, Any]] = []
    normalized_decision = _normalize_decision(decision)
    if normalized_decision not in {"approve", "reject", "defer"}:
        findings.append(
            {
                "severity": "blocker",
                "code": "decision_not_allowed_for_record",
                "message": "Recorded SQF review decisions must be approve, reject, or defer.",
            }
        )
    if not order and not claim_id:
        findings.append(
            {
                "severity": "blocker",
                "code": "row_locator_required",
                "message": "Provide --order or --claim-id.",
            }
        )
    if not _present(reviewer_id):
        findings.append(
            {
                "severity": "blocker",
                "code": "reviewer_id_required",
                "message": "A reviewer_id is required for a recorded human decision.",
            }
        )
    if _is_internal_path_reference(source_packet):
        findings.append(
            {
                "severity": "blocker",
                "code": "source_packet_sensitive_reference",
                "message": "source_packet must not contain internal path or raw payload markers.",
            }
        )

    try:
        rows, fieldnames = _read_csv_rows(sheet_path)
    except FileNotFoundError:
        rows, fieldnames = [], []
        findings.append(
            {
                "severity": "blocker",
                "code": "decision_sheet_missing",
                "message": "SQF review decision sheet CSV is missing.",
            }
        )

    matched_indexes: list[int] = []
    for index, row in enumerate(rows):
        order_match = order is not None and str(row.get("order") or "") == str(order)
        claim_match = claim_id is not None and str(row.get("claim_id") or "") == str(claim_id)
        if order_match or claim_match:
            matched_indexes.append(index)
    if rows and len(matched_indexes) != 1:
        findings.append(
            {
                "severity": "blocker",
                "code": "decision_row_match_count_invalid",
                "matched_count": len(matched_indexes),
                "order": order,
                "claim_id": claim_id,
                "message": "Decision row locator must match exactly one row.",
            }
        )

    if len(matched_indexes) == 1:
        row = dict(rows[matched_indexes[0]])
        if _present(row.get("decision")) and not overwrite_existing:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "decision_row_already_filled",
                    "order": row.get("order"),
                    "claim_id": row.get("claim_id"),
                    "message": "Decision row already has a decision; pass overwrite_existing for an explicit correction.",
                }
            )
        packet = str(source_packet or row.get("source_packet") or "").strip()
        row.update(
            {
                "decision": normalized_decision,
                "reason": reason.strip(),
                "reject_reason_code": reject_reason_code.strip(),
                "defer_reason_code": defer_reason_code.strip(),
                "notes": notes.strip(),
                "reviewer_id": reviewer_id.strip(),
                "reviewed_at": reviewed_at or created_at,
                "source_packet": packet,
                "status_update_allowed": "false",
                "used_for_scoring": "false",
                "approval_claim": "false",
            }
        )
        row_audit = _audit_row(matched_indexes[0] + 1, row, has_sensitive_header=False)
        if not row_audit["valid"]:
            findings.append(
                {
                    "severity": "blocker",
                    "code": "recorded_decision_row_invalid",
                    "order": row.get("order"),
                    "claim_id": row.get("claim_id"),
                    "missing_required": row_audit.get("missing_required"),
                    "issue_codes": row_audit.get("issue_codes"),
                    "message": "Recorded decision does not satisfy SQF decision audit requirements.",
                }
            )

    blocker_count = sum(1 for finding in findings if finding.get("severity") == "blocker")
    if blocker_count:
        return {
            "ok": False,
            "schema": "ncs_sqf_review_decision_record_v1",
            "created_at": created_at,
            "decision_sheet_name": sheet_path.name,
            "out_csv_name": output_path.name,
            "matched_count": len(matched_indexes),
            "updated_count": 0,
            **REPORT_ONLY_CONTRACT,
            "status": "pre_import_annotation_rejected",
            "execution_allowed": False,
            "pre_import_annotation_only": True,
            "findings": findings,
            "blocker_count": blocker_count,
        }

    updated_row = rows[matched_indexes[0]]
    updated_row.update(row)
    output_fields = list(fieldnames)
    for field in RECORD_DECISION_FIELDS:
        if field not in output_fields:
            output_fields.append(field)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields)
        writer.writeheader()
        for item in rows:
            writer.writerow({field: _csv_cell(item.get(field, "")) for field in output_fields})

    return {
        "ok": True,
        "schema": "ncs_sqf_review_decision_record_v1",
        "created_at": created_at,
        "decision_sheet_name": sheet_path.name,
        "out_csv_name": output_path.name,
        "matched_count": 1,
        "updated_count": 1,
        "status": "pre_import_annotation_recorded",
        "execution_allowed": False,
        "pre_import_annotation_only": True,
        "annotation_note": "CSV annotation only; no DB row was reviewed, accepted, or approved.",
        "updated_row": {
            "order": updated_row.get("order"),
            "claim_id": updated_row.get("claim_id"),
            "decision": updated_row.get("decision"),
            "reason_present": _present(updated_row.get("reason")),
            "reviewer_id": updated_row.get("reviewer_id"),
            "reviewed_at": updated_row.get("reviewed_at"),
            "source_packet_present": _present(updated_row.get("source_packet")),
            "top_evidence_refs_present": _present(updated_row.get("top_evidence_refs")),
            "status_update_allowed": updated_row.get("status_update_allowed"),
            "used_for_scoring": updated_row.get("used_for_scoring"),
            "approval_claim": updated_row.get("approval_claim"),
        },
        **REPORT_ONLY_CONTRACT,
        "findings": [],
        "blocker_count": 0,
    }


def write_sqf_review_decision_audit_json(report: dict[str, Any], out_path: str | Path) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def render_sqf_review_decision_audit_markdown(
    report: dict[str, Any],
    *,
    max_rows: int = MAX_MARKDOWN_ROWS,
) -> str:
    summary = report.get("summary") or {}
    missing = summary.get("missing_required_counts") or {}
    lines = [
        "# SQF Review Decision Audit",
        "",
        "## Contract",
        "",
        f"- ok: {str(report.get('ok')).lower()}",
        f"- schema: {report.get('schema')}",
        f"- format_version: {report.get('format_version')}",
        f"- decision_sheet_name: {report.get('decision_sheet_name')}",
        f"- status_update_allowed: {str(report.get('status_update_allowed')).lower()}",
        f"- used_for_scoring: {str(report.get('used_for_scoring')).lower()}",
        f"- approval_claim: {str(report.get('approval_claim')).lower()}",
        f"- db_writes: {str(report.get('db_writes')).lower()}",
        f"- execution_allowed: {str(report.get('execution_allowed')).lower()}",
        f"- pre_import_annotation_only: {str(report.get('pre_import_annotation_only')).lower()}",
        "",
        "## Summary",
        "",
        f"- row_count: {summary.get('row_count', 0)}",
        f"- blank_count: {summary.get('blank_count', 0)}",
        f"- approve_count: {summary.get('approve_count', 0)}",
        f"- reject_count: {summary.get('reject_count', 0)}",
        f"- defer_count: {summary.get('defer_count', 0)}",
        f"- invalid_count: {summary.get('invalid_count', 0)}",
        f"- pending_review_count: {summary.get('pending_review_count', 0)}",
        f"- completed_decision_count: {summary.get('completed_decision_count', 0)}",
        f"- valid_completed_decision_count: {summary.get('valid_completed_decision_count', 0)}",
        f"- status_update_candidate_count: {summary.get('status_update_candidate_count', 0)}",
        f"- guarded_import_candidate_count: {summary.get('guarded_import_candidate_count', 0)}",
        f"- pre_import_annotation_count: {summary.get('pre_import_annotation_count', 0)}",
        f"- import_ready_count: {summary.get('import_ready_count', 0)}",
        f"- defer_ready_count: {summary.get('defer_ready_count', 0)}",
        f"- missing_required_counts: {json.dumps(missing, ensure_ascii=False, sort_keys=True)}",
        f"- guardrail_issue_count: {report.get('guardrail_issue_count', 0)}",
        f"- sensitive_reference_count: {report.get('sensitive_reference_count', 0)}",
        "",
        "## Rows",
        "",
        "| row | order | claim | decision | valid | missing required | issues | evidence refs |",
        "|---:|---|---|---|---|---|---|---:|",
    ]
    for row in (report.get("rows") or [])[: max(0, max_rows)]:
        if not isinstance(row, dict):
            continue
        claim = row.get("claim_id") or row.get("ncs_unit_code") or ""
        lines.append(
            "| "
            f"{row.get('row_number')} | "
            f"{_markdown_cell(row.get('order'))} | "
            f"{_markdown_cell(claim)} | "
            f"{_markdown_cell(row.get('decision'))} | "
            f"{str(row.get('valid')).lower()} | "
            f"{_markdown_cell(', '.join(row.get('missing_required') or []))} | "
            f"{_markdown_cell(', '.join(row.get('issue_codes') or []))} | "
            f"{row.get('top_evidence_ref_count', 0)} |"
        )

    notes = [note for note in report.get("notes") or [] if note]
    if notes:
        lines.extend(["", "## Notes", ""])
        for note in notes:
            lines.append(f"- {_markdown_cell(note)}")
    return "\n".join(lines).rstrip() + "\n"


def write_sqf_review_decision_audit_markdown(
    report: dict[str, Any],
    out_path: str | Path,
    *,
    max_rows: int = MAX_MARKDOWN_ROWS,
) -> None:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_sqf_review_decision_audit_markdown(report, max_rows=max_rows),
        encoding="utf-8",
    )


def _read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows = [{str(key): _text(value) for key, value in row.items() if key is not None} for row in reader]
    return rows, fieldnames


def _audit_row(row_number: int, row: dict[str, str], *, has_sensitive_header: bool) -> dict[str, Any]:
    normalized_decision = _normalize_decision(row.get("decision"))
    decision_allowed = normalized_decision in ALLOWED_DECISIONS
    decision = normalized_decision if decision_allowed else "invalid"
    missing_required = _missing_required_fields(decision, row)
    issue_codes: list[str] = []
    if not decision_allowed:
        issue_codes.append("decision_not_allowed")
    if missing_required:
        issue_codes.append("missing_required_fields")

    contract_issues = _guardrail_contract_issues(row)
    if contract_issues:
        issue_codes.append("guardrail_contract_not_false")

    sensitive_value_count = _sensitive_value_count(row)
    sensitive_count = sensitive_value_count + (1 if has_sensitive_header else 0)
    if sensitive_count:
        issue_codes.append("sensitive_reference")

    top_evidence_refs = row.get("top_evidence_refs") or ""
    status_update_candidate = decision in {"approve", "reject"} and not issue_codes
    safe_projection = {
        field: _safe_projection_value(row.get(field))
        for field in SAFE_ROW_FIELDS
        if _present(row.get(field))
    }
    result = {
        "row_number": row_number,
        **safe_projection,
        "decision": decision,
        "valid": not issue_codes,
        "missing_required": missing_required,
        "issue_codes": issue_codes,
        "source_packet_present": _present(row.get("source_packet")),
        "top_evidence_refs_present": _present(top_evidence_refs),
        "top_evidence_ref_count": _evidence_ref_count(top_evidence_refs),
        "guardrail_contract_ok": not contract_issues,
        "status_update_candidate": status_update_candidate,
        "guarded_import_candidate": status_update_candidate,
        "pre_import_annotation": decision in {"approve", "reject", "defer"} and not issue_codes,
        "execution_allowed": False,
        "import_policy": "guarded_human_import_only" if status_update_candidate else "no_status_update",
    }
    if sensitive_count:
        result["sensitive_reference_count"] = sensitive_count
    return result


def _normalize_decision(value: Any) -> str:
    text = _text(value).strip().lower()
    return "blank" if text == "" else text


def _missing_required_fields(decision: str, row: dict[str, str]) -> list[str]:
    if decision == "approve":
        missing = [
            field
            for field in ("reviewer_id", "reviewed_at", "source_packet", "top_evidence_refs")
            if not _present(row.get(field))
        ]
        if not (_present(row.get("reason")) or _present(row.get("rationale"))):
            missing.append("reason_or_rationale")
        return sorted(missing)
    if decision == "reject":
        if not (_present(row.get("reject_reason_code")) or _present(row.get("reason"))):
            return ["reject_reason_code_or_reason"]
    if decision == "defer":
        if not (_present(row.get("defer_reason_code")) or _present(row.get("reason"))):
            return ["defer_reason_code_or_reason"]
    return []


def _guardrail_contract_issues(row: dict[str, str]) -> list[str]:
    issues: list[str] = []
    for field in REPORT_ONLY_CONTRACT:
        if field in row and _present(row.get(field)) and not _is_false_value(row.get(field)):
            issues.append(field)
    return issues


def _is_false_value(value: Any) -> bool:
    return _text(value).strip().lower() in {"false", "0", "no", "n", "off"}


def _sensitive_value_count(row: dict[str, str]) -> int:
    return sum(1 for value in row.values() if _is_internal_path_reference(value))


def _contains_forbidden_token(value: Any) -> bool:
    text = _text(value).strip().lower()
    return any(token in text for token in FORBIDDEN_TOKENS)


def _is_internal_path_reference(value: Any) -> bool:
    text = _text(value).strip()
    if not text:
        return False
    return (
        _contains_forbidden_token(text)
        or PureWindowsPath(text).is_absolute()
        or PurePosixPath(text).is_absolute()
    )


def _safe_projection_value(value: Any) -> str:
    text = _text(value).strip()
    if not text or _contains_forbidden_token(text):
        return ""
    return text


def _evidence_ref_count(value: Any) -> int:
    text = _text(value)
    if not text.strip():
        return 0
    parts = [part.strip() for chunk in text.split(";") for part in chunk.split(",")]
    return len([part for part in parts if part])


def _present(value: Any) -> bool:
    return bool(_text(value).strip())


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _markdown_cell(value: Any) -> str:
    text = _safe_projection_value(value)
    return text.replace("|", "\\|").replace("\n", " ")
