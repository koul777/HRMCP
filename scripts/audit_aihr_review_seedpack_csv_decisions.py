from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_review_seedpack_csv_decision_audit_v1"
FORBIDDEN_AUTOMATIC_STATUSES = {"human_reviewed", "accepted", "reviewed"}
DEFAULT_ALLOWED_DECISIONS = {
    "approve",
    "reject",
    "defer",
    "accept_concept",
    "revise_definition",
    "reject_concept",
    "accept_relation",
    "reject_relation",
    "needs_more_evidence",
    "accept_link",
    "reject_link",
}
ISSUE_TYPE_ALLOWED_DECISIONS = {
    "hr_training_goal_link_human_review_required": {
        "accept_link",
        "reject_link",
        "needs_more_evidence",
        "defer",
    },
    "ontology_training_goal_link_human_review_required": {
        "accept_link",
        "reject_link",
        "needs_more_evidence",
        "defer",
    },
    "ontology_task_ksa_relation_human_review_required": {
        "accept_relation",
        "reject_relation",
        "needs_more_evidence",
        "defer",
    },
    "hr_core_concept_human_review_required": {
        "accept_concept",
        "revise_definition",
        "reject_concept",
        "defer",
    },
    "ontology_core_concept_human_review_required": {
        "accept_concept",
        "revise_definition",
        "reject_concept",
        "defer",
    },
    "ontology_definition_human_review_required": {
        "accept_concept",
        "revise_definition",
        "reject_concept",
        "defer",
    },
}
DECISION_FIELDS = ("decision", "reviewer_id", "reviewed_at", "rationale")
REQUIRED_GUARD_FIELDS = ("status_update_allowed", "db_writes", "approval_claim")
OPTIONAL_GUARD_FIELDS = ("acceptance_claim",)
GUARD_FIELDS = (*REQUIRED_GUARD_FIELDS, *OPTIONAL_GUARD_FIELDS)
REQUIRED_COLUMNS = (*DECISION_FIELDS, "human_decision_required", *REQUIRED_GUARD_FIELDS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def portable_path(path: str | Path, *, root: Path = PROJECT_ROOT) -> str:
    resolved = Path(path).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def safe_bool_false(value: Any) -> bool:
    return value is False or str(value or "").strip().lower() == "false"


def safe_bool_true(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() == "true"


def read_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader], list(reader.fieldnames or [])


def valid_reviewed_at(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def nonblank(value: Any) -> bool:
    return bool(str(value or "").strip())


def row_key(row: dict[str, str], index: int) -> str:
    for field in ("sequence", "order", "scenario_id", "target_id"):
        value = str(row.get(field) or "").strip()
        if value:
            return value
    return str(index)


def audit_row(
    row: dict[str, str],
    *,
    index: int,
    allowed_decisions: set[str],
    missing_columns: set[str],
) -> dict[str, Any]:
    decision = str(row.get("decision") or "").strip()
    issues: list[str] = []
    missing_required_fields: list[str] = []
    if missing_columns:
        issues.append("missing_required_columns")
    for field in GUARD_FIELDS:
        if field in row and not safe_bool_false(row.get(field)):
            issues.append(f"{field}_not_false")
    if "human_decision_required" in row and not safe_bool_true(row.get("human_decision_required")):
        issues.append("human_decision_required_not_true")

    if decision:
        if decision not in allowed_decisions:
            issues.append("decision_not_allowed")
        issue_type = str(row.get("issue_type") or "").strip()
        issue_allowed_decisions = ISSUE_TYPE_ALLOWED_DECISIONS.get(issue_type)
        if issue_type and issue_allowed_decisions is None:
            issues.append("issue_type_decision_vocabulary_missing")
        elif issue_allowed_decisions is not None and decision not in issue_allowed_decisions:
            issues.append("decision_not_allowed_for_issue_type")
        for field in ("reviewer_id", "reviewed_at", "rationale"):
            if not nonblank(row.get(field)):
                missing_required_fields.append(field)
        if nonblank(row.get("reviewed_at")) and not valid_reviewed_at(str(row.get("reviewed_at"))):
            issues.append("reviewed_at_invalid_iso")
    else:
        orphan_fields = [
            field for field in ("reviewer_id", "reviewed_at", "rationale") if nonblank(row.get(field))
        ]
        if orphan_fields:
            issues.append("decision_blank_with_reviewer_fields")
            missing_required_fields.extend(orphan_fields)

    trusted_status_fields = (
        "proposed_target_review_status",
        "proposed_review_status",
        "target_review_status",
        "review_status",
        "status",
    )
    trusted_status_proposals = [
        str(row.get(field) or "").strip()
        for field in trusted_status_fields
        if str(row.get(field) or "").strip() in FORBIDDEN_AUTOMATIC_STATUSES
    ]
    if trusted_status_proposals:
        issues.append("trusted_status_proposal_requires_separate_guarded_apply")

    return {
        "row_number": index,
        "row_key": row_key(row, index),
        "issue_type": row.get("issue_type"),
        "target_type": row.get("target_type"),
        "target_id": row.get("target_id"),
        "decision": decision,
        "pending": not decision,
        "completed": bool(decision) and not issues and not missing_required_fields,
        "invalid": bool(issues or missing_required_fields),
        "issues": sorted(set(issues)),
        "missing_required_fields": sorted(set(missing_required_fields)),
        "trusted_status_proposals": trusted_status_proposals,
    }


def build_audit(
    csv_path: Path,
    *,
    allowed_decisions: set[str] | None = None,
    issue_types: set[str] | None = None,
    require_completed_decisions: bool = False,
    generated_at: str | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    allowed = set(allowed_decisions or DEFAULT_ALLOWED_DECISIONS)
    selected_issue_types = {str(value).strip() for value in issue_types or set() if str(value).strip()}
    rows, fieldnames = read_csv_rows(csv_path)
    source_row_count = len(rows)
    if selected_issue_types:
        rows = [
            row
            for row in rows
            if str(row.get("issue_type") or "").strip() in selected_issue_types
        ]
    missing_columns = set(REQUIRED_COLUMNS) - set(fieldnames)
    audited_rows = [
        audit_row(
            row,
            index=index,
            allowed_decisions=allowed,
            missing_columns=missing_columns,
        )
        for index, row in enumerate(rows, start=1)
    ]
    invalid_rows = [row for row in audited_rows if row["invalid"]]
    trusted_status_proposal_rows = [
        row for row in audited_rows if row.get("trusted_status_proposals")
    ]
    guard_issue_rows = [
        row
        for row in audited_rows
        if any(issue.endswith("_not_false") for issue in row.get("issues") or [])
    ]
    pending_count = sum(1 for row in audited_rows if row["pending"])
    completed_count = sum(1 for row in audited_rows if row["completed"])
    completion_issue = bool(require_completed_decisions and pending_count > 0)
    issue_type_counts: dict[str, int] = {}
    for row in audited_rows:
        key = str(row.get("issue_type") or "unknown")
        issue_type_counts[key] = issue_type_counts.get(key, 0) + 1
    ok = not missing_columns and not invalid_rows and not guard_issue_rows and not completion_issue
    report = {
        "schema": SCHEMA,
        "generated_at": generated_at or now_iso(),
        "ok": ok,
        "status": "pass" if ok else "fail",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": sorted(FORBIDDEN_AUTOMATIC_STATUSES),
        "source_csv": portable_path(csv_path, root=root),
        "source_row_count": source_row_count,
        "row_filter": {
            "issue_types": sorted(selected_issue_types),
            "filtered_out_row_count": source_row_count - len(audited_rows),
        },
        "fieldnames": fieldnames,
        "allowed_decisions": sorted(allowed),
        "issue_type_allowed_decisions": {
            key: sorted(value) for key, value in sorted(ISSUE_TYPE_ALLOWED_DECISIONS.items())
        },
        "require_completed_decisions": require_completed_decisions,
        "missing_required_columns": sorted(missing_columns),
        "row_count": len(audited_rows),
        "pending_decision_count": pending_count,
        "completed_decision_count": completed_count,
        "invalid_decision_count": len(invalid_rows),
        "guard_issue_row_count": len(guard_issue_rows),
        "trusted_status_proposal_row_count": len(trusted_status_proposal_rows),
        "completion_issue": completion_issue,
        "issue_type_counts": dict(sorted(issue_type_counts.items())),
        "rows": audited_rows,
        "invalid_rows": invalid_rows[:50],
        "notes": [
            "This audit is report-only and never updates DB review status.",
            "A completed decision is human input only; status updates require a separate guarded apply process.",
            "status_update_allowed, db_writes, approval_claim, and any acceptance_claim column must remain false.",
        ],
    }
    return report


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# AI-HR Review Seedpack CSV Decision Audit",
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
        f"- source_csv: `{payload.get('source_csv')}`",
        f"- source_row_count: `{payload.get('source_row_count')}`",
        f"- row_filter.issue_types: `{', '.join((payload.get('row_filter') or {}).get('issue_types') or [])}`",
        f"- require_completed_decisions: `{payload.get('require_completed_decisions')}`",
        f"- row_count: `{payload.get('row_count')}`",
        f"- pending_decision_count: `{payload.get('pending_decision_count')}`",
        f"- completed_decision_count: `{payload.get('completed_decision_count')}`",
        f"- invalid_decision_count: `{payload.get('invalid_decision_count')}`",
        f"- guard_issue_row_count: `{payload.get('guard_issue_row_count')}`",
        f"- trusted_status_proposal_row_count: `{payload.get('trusted_status_proposal_row_count')}`",
        f"- completion_issue: `{payload.get('completion_issue')}`",
        "",
        "## Issues",
        "",
    ]
    if payload.get("invalid_rows"):
        lines.extend(
            [
                "| row | key | decision | issues | missing_required_fields |",
                "|---:|---|---|---|---|",
            ]
        )
        for row in payload.get("invalid_rows") or []:
            lines.append(
                f"| {row.get('row_number')} | {row.get('row_key')} | {row.get('decision')} | "
                f"{', '.join(row.get('issues') or [])} | "
                f"{', '.join(row.get('missing_required_fields') or [])} |"
            )
    else:
        lines.append("No invalid decision rows found.")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a filled AI-HR review seedpack CSV decision sheet without DB writes."
    )
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument(
        "--allowed-decision",
        action="append",
        dest="allowed_decisions",
        help="Allowed decision value. May be repeated. Defaults cover current AI-HR review seedpack surfaces.",
    )
    parser.add_argument(
        "--issue-type",
        action="append",
        dest="issue_types",
        help="Only audit rows with this issue_type. May be repeated.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument(
        "--require-completed-decisions",
        action="store_true",
        help="Fail if any CSV row still has a blank decision.",
    )
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_audit(
        args.csv,
        allowed_decisions=set(args.allowed_decisions or DEFAULT_ALLOWED_DECISIONS),
        issue_types=set(args.issue_types or []),
        require_completed_decisions=args.require_completed_decisions,
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
                "row_count": report.get("row_count"),
                "pending_decision_count": report.get("pending_decision_count"),
                "completed_decision_count": report.get("completed_decision_count"),
                "invalid_decision_count": report.get("invalid_decision_count"),
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
