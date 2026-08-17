from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
READONLY_REFRESH_REPORTS = REPORTS / "overnight_sessions" / "readonly_refresh"


def _dated_artifact_sort_key(path: Path) -> tuple[int, float]:
    for part in reversed(path.stem.split("_")):
        if len(part) == 8 and part.isdigit():
            return int(part), path.stat().st_mtime
    return 0, path.stat().st_mtime


def _latest_report_path(*patterns: str, fallback: Path) -> Path:
    candidate_dirs = (REPORTS, READONLY_REFRESH_REPORTS)
    candidates = []
    seen: set[Path] = set()
    for directory in candidate_dirs:
        for pattern in patterns:
            for path in directory.glob(pattern):
                if path.is_file() and path not in seen:
                    seen.add(path)
                    candidates.append(path)
    if not candidates:
        return fallback
    return max(candidates, key=_dated_artifact_sort_key)


DEFAULT_SOURCE_PACKET = (
    _latest_report_path(
        "aihr_human_review_provenance_reconfirmation_packet_20*.json",
        "human_review_provenance_reconfirmation_packet_20*.json",
        fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_packet_20260620.json",
    )
)
DEFAULT_CSV = (
    _latest_report_path(
        "aihr_human_review_provenance_reconfirmation_decision_sheet_20*.csv",
        "human_review_provenance_reconfirmation_decision_sheet_20*.csv",
        fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_decision_sheet_20260620.csv",
    )
)


def _artifact_suffix(path: Path, *, prefixes: tuple[str, ...]) -> str:
    stem = path.stem
    for prefix in prefixes:
        if stem.startswith(prefix):
            return stem.removeprefix(prefix)
    return stem


def default_output_path(csv_path: Path, suffix: str) -> Path:
    sheet_suffix = _artifact_suffix(
        csv_path,
        prefixes=(
            "aihr_human_review_provenance_reconfirmation_decision_sheet_",
            "human_review_provenance_reconfirmation_decision_sheet_",
        ),
    )
    return REPORTS / f"human_review_provenance_reconfirmation_decision_audit_{sheet_suffix}{suffix}"

ALLOWED_DECISIONS = {"reconfirm", "downgrade_to_review_required", "defer"}
ACTION_DECISIONS = {"reconfirm", "downgrade_to_review_required"}
REQUIRED_FIELDS = [
    "rationale",
    "reviewer_id",
    "reviewed_at",
    "source_decision_packet",
    "evidence_refs_json",
]
EXPECTED_FALSE_FIELDS = ["status_update_allowed", "db_writes", "approval_claim"]
EXPECTED_CSV_FIELDS = [
    "order",
    "surface",
    "target_table",
    "target_id",
    "entity_type",
    "review_status_display",
    "status_trust",
    "provenance_state",
    "display",
    "scope_summary",
    "requested_decision_options",
    "decision",
    "rationale",
    "reviewer_id",
    "reviewed_at",
    "source_decision_packet",
    "source_packet_sha256",
    "source_packet_row_sha256",
    "evidence_refs_json",
    "notes",
    *EXPECTED_FALSE_FIELDS,
]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def load_packet(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("source packet JSON root must be an object")
    schema = payload.get("schema")
    if schema != "aihr_human_review_provenance_reconfirmation_packet_v1":
        raise ValueError(f"unsupported source packet schema: {schema!r}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("source packet rows must be a list")
    return payload


def packet_contract_issues(packet: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in ("status_update_allowed", "db_writes", "approval_claim"):
        if packet.get(field) is not False:
            issues.append(f"{field}_not_false")
    if packet.get("human_decision_required") is not True:
        issues.append("human_decision_required_not_true")
    if packet.get("ok") is not True:
        issues.append("source_packet_ok_not_true")
    return issues


def packet_row_identity_issues(packet: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    rows = packet.get("rows") if isinstance(packet, dict) else None
    if not isinstance(rows, list):
        return ["rows_not_list"]
    required_fields = ("surface", "target_table", "target_id")
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            issues.append(f"row_{index}_not_object")
            continue
        row_label = str(row.get("order") or index).strip() or str(index)
        for field in required_fields:
            if not str(row.get(field) or "").strip():
                issues.append(f"row_{row_label}_missing_{field}")
    return issues


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


def read_csv_table(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader], list(reader.fieldnames or [])


def content_sha256(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").split())


def safe_review_status_display(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.startswith("legacy_status_needs_reconfirmation"):
        return "legacy_status_needs_reconfirmation"
    if text in {"human_reviewed", "accepted", "reviewed"}:
        return "trusted_status_suppressed"
    return text


def packet_row_identity(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "order": row.get("order"),
        "surface": row.get("surface"),
        "target_table": row.get("target_table"),
        "target_id": row.get("target_id"),
        "entity_type": row.get("entity_type"),
        "review_status_display": safe_review_status_display(row.get("review_status_display")),
        "status_trust": row.get("status_trust"),
        "provenance_state": row.get("provenance_state"),
        "display": row.get("display"),
        "scope": row.get("scope"),
    }


def packet_row_sha256(row: dict[str, Any]) -> str:
    return content_sha256(stable_json(packet_row_identity(row)))


def packet_row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("order") or "").strip(),
        str(row.get("surface") or "").strip(),
        str(row.get("target_table") or "").strip(),
        str(row.get("target_id") or "").strip(),
    )


def csv_row_key(row: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        str(row.get("order") or "").strip(),
        str(row.get("surface") or "").strip(),
        str(row.get("target_table") or "").strip(),
        str(row.get("target_id") or "").strip(),
    )


def is_false_string(value: Any) -> bool:
    return str(value or "").strip().lower() == "false"


def packet_ref_issue(value: str, *, base_dir: Path) -> str | None:
    artifact = value.strip().partition("#")[0].strip()
    if not artifact:
        return "source_decision_packet_not_found"
    path = Path(artifact)
    if path.is_absolute() or ".." in path.parts:
        return "source_decision_packet_not_portable"
    if path.suffix.lower() not in {".json", ".jsonl", ".csv", ".md", ".html"}:
        return "source_decision_packet_unsupported_type"
    stem = path.stem.lower()
    if not any(marker in stem for marker in ("packet", "review", "provenance", "evidence", "decision", "audit")):
        return "source_decision_packet_unrecognized"
    candidates = [PROJECT_ROOT / path, base_dir / path]
    if any(candidate.exists() for candidate in candidates):
        return None
    return "source_decision_packet_not_found"


def evidence_refs_valid(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False
    if isinstance(parsed, dict):
        return bool(parsed) and all(str(key).strip() for key in parsed)
    if isinstance(parsed, list):
        return bool(parsed) and all(
            bool(item) if isinstance(item, (dict, list)) else bool(str(item).strip())
            for item in parsed
        )
    return False


def reviewer_id_valid(value: str) -> bool:
    text = value.strip()
    if not text or len(text) > 120:
        return False
    if any(char in text for char in ("\r", "\n", "\t")):
        return False
    return True


def reviewed_at_valid(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def audit_row(
    row: dict[str, str],
    *,
    packet_row_hashes: dict[tuple[str, str, str, str], str],
    packet_row_identity_issue_keys: set[tuple[str, str, str, str]],
    source_packet_sha256: str,
    csv_base_dir: Path,
) -> dict[str, Any]:
    decision = str(row.get("decision") or "").strip()
    issues: list[str] = []
    missing_fields: list[str] = []
    key = csv_row_key(row)

    expected_row_sha256 = packet_row_hashes.get(key)
    if expected_row_sha256 is None:
        issues.append("source_packet_row_mismatch")
    if key in packet_row_identity_issue_keys:
        issues.append("source_packet_row_identity_incomplete")

    if not decision:
        status = "pending"
    elif decision not in ALLOWED_DECISIONS:
        status = "invalid"
        issues.append("invalid_decision")
    else:
        status = "completed"

    if decision:
        for field in REQUIRED_FIELDS:
            if not str(row.get(field) or "").strip():
                missing_fields.append(field)
        if missing_fields:
            issues.append("missing_required_fields")
        reviewer_id = str(row.get("reviewer_id") or "")
        if reviewer_id and not reviewer_id_valid(reviewer_id):
            issues.append("invalid_reviewer_id")
        reviewed_at = str(row.get("reviewed_at") or "")
        if reviewed_at and not reviewed_at_valid(reviewed_at):
            issues.append("invalid_reviewed_at")
        source_packet = str(row.get("source_decision_packet") or "").strip()
        if source_packet:
            source_packet_issue = packet_ref_issue(source_packet, base_dir=csv_base_dir)
            if source_packet_issue:
                issues.append(source_packet_issue)
        evidence_refs = str(row.get("evidence_refs_json") or "")
        if evidence_refs and not evidence_refs_valid(evidence_refs):
            issues.append("invalid_evidence_refs_json")
        csv_source_sha256 = str(row.get("source_packet_sha256") or "").strip()
        if not csv_source_sha256:
            issues.append("source_packet_sha256_missing")
        elif csv_source_sha256 != source_packet_sha256:
            issues.append("source_packet_sha256_mismatch")
        csv_row_sha256 = str(row.get("source_packet_row_sha256") or "").strip()
        if not csv_row_sha256:
            issues.append("source_packet_row_sha256_missing")
        elif expected_row_sha256 and csv_row_sha256 != expected_row_sha256:
            issues.append("source_packet_row_sha256_mismatch")

    unsafe_fields = [
        field
        for field in EXPECTED_FALSE_FIELDS
        if not is_false_string(row.get(field))
    ]
    if unsafe_fields:
        issues.append("unsafe_true_fields")

    action_eligible = (
        decision in ACTION_DECISIONS
        and status == "completed"
        and not issues
        and not missing_fields
    )

    return {
        "order": row.get("order") or "",
        "surface": row.get("surface") or "",
        "target_table": row.get("target_table") or "",
        "target_id": row.get("target_id") or "",
        "decision": decision,
        "status": status,
        "action_eligible": action_eligible,
        "missing_fields": missing_fields,
        "unsafe_fields": unsafe_fields,
        "issues": issues,
    }


def build_decision_audit(csv_path: Path, source_packet_path: Path) -> dict[str, Any]:
    source_packet = load_packet(source_packet_path)
    source_packet_contract_issues = packet_contract_issues(source_packet)
    source_packet_row_identity_issues = packet_row_identity_issues(source_packet)
    csv_rows, csv_fields = read_csv_table(csv_path)
    source_packet_sha256 = content_sha256(source_packet_path.read_bytes())
    packet_row_hashes = {
        packet_row_key(row): packet_row_sha256(row)
        for row in source_packet.get("rows") or []
        if isinstance(row, dict)
    }
    packet_row_identity_issue_keys = {
        packet_row_key(row)
        for row in source_packet.get("rows") or []
        if isinstance(row, dict)
        and any(not str(row.get(field) or "").strip() for field in ("surface", "target_table", "target_id"))
    }
    csv_keys = [csv_row_key(row) for row in csv_rows]
    csv_key_counts: dict[tuple[str, str, str, str], int] = {}
    for key in csv_keys:
        csv_key_counts[key] = csv_key_counts.get(key, 0) + 1
    duplicate_csv_key_count = sum(1 for count in csv_key_counts.values() if count > 1)
    missing_packet_row_count = sum(1 for key in packet_row_hashes if key not in csv_key_counts)
    unexpected_csv_row_count = 1 if len(csv_rows) != len(packet_row_hashes) else 0
    missing_csv_columns = [field for field in EXPECTED_CSV_FIELDS if field not in csv_fields]
    audited_rows = [
        audit_row(
            row,
            packet_row_hashes=packet_row_hashes,
            packet_row_identity_issue_keys=packet_row_identity_issue_keys,
            source_packet_sha256=source_packet_sha256,
            csv_base_dir=csv_path.parent,
        )
        for row in csv_rows
    ]

    pending_count = sum(1 for row in audited_rows if row["status"] == "pending")
    invalid_count = sum(1 for row in audited_rows if row["status"] == "invalid")
    completed_count = sum(1 for row in audited_rows if row["status"] == "completed")
    source_mismatch_count = sum(
        1 for row in audited_rows if "source_packet_row_mismatch" in row["issues"]
    )
    unsafe_flag_count = sum(1 for row in audited_rows if row["unsafe_fields"])
    missing_required_count = sum(1 for row in audited_rows if row["missing_fields"])
    source_packet_missing_count = sum(
        1 for row in audited_rows if "source_decision_packet_not_found" in row["issues"]
    )
    source_packet_not_portable_count = sum(
        1 for row in audited_rows if "source_decision_packet_not_portable" in row["issues"]
    )
    source_packet_unsupported_type_count = sum(
        1
        for row in audited_rows
        if "source_decision_packet_unsupported_type" in row["issues"]
    )
    source_packet_unrecognized_count = sum(
        1 for row in audited_rows if "source_decision_packet_unrecognized" in row["issues"]
    )
    invalid_evidence_refs_count = sum(
        1 for row in audited_rows if "invalid_evidence_refs_json" in row["issues"]
    )
    invalid_reviewer_id_count = sum(
        1 for row in audited_rows if "invalid_reviewer_id" in row["issues"]
    )
    invalid_reviewed_at_count = sum(
        1 for row in audited_rows if "invalid_reviewed_at" in row["issues"]
    )
    source_identity_mismatch_count = sum(
        1
        for row in audited_rows
        if any(
            issue.startswith("source_packet_sha256")
            or issue.startswith("source_packet_row_sha256")
            for issue in row["issues"]
        )
    )
    action_eligible_count = sum(1 for row in audited_rows if row["action_eligible"])

    issue_type_counts: dict[str, int] = {}
    for row in audited_rows:
        for issue in row["issues"]:
            issue_type_counts[issue] = issue_type_counts.get(issue, 0) + 1
    if missing_csv_columns:
        issue_type_counts["missing_csv_columns"] = len(missing_csv_columns)
    if duplicate_csv_key_count:
        issue_type_counts["duplicate_csv_rows"] = duplicate_csv_key_count
    if missing_packet_row_count:
        issue_type_counts["missing_packet_rows"] = missing_packet_row_count
    if unexpected_csv_row_count:
        issue_type_counts["unexpected_csv_row_count"] = unexpected_csv_row_count
    for issue in source_packet_contract_issues:
        issue_type_counts[f"source_packet_{issue}"] = 1
    if source_packet_row_identity_issues:
        issue_type_counts["source_packet_row_identity_issues"] = len(
            source_packet_row_identity_issues
        )

    return {
        "ok": (
            sum(len(row["issues"]) for row in audited_rows) == 0
            and not source_packet_contract_issues
            and not source_packet_row_identity_issues
            and not missing_csv_columns
            and duplicate_csv_key_count == 0
            and missing_packet_row_count == 0
            and unexpected_csv_row_count == 0
        ),
        "schema": "aihr_provenance_reconfirmation_decision_audit_v1",
        "generated_at": now_iso(),
        "report_only": True,
        "api_calls": False,
        "acceptance_claim": False,
        "human_decision_required": True,
        "csv": rel(csv_path),
        "source_packet": rel(source_packet_path),
        "source_packet_sha256": source_packet_sha256,
        "source_packet_schema": source_packet.get("schema"),
        "source_packet_contract_ok": not source_packet_contract_issues,
        "source_packet_contract_issues": source_packet_contract_issues,
        "source_packet_row_identity_issue_count": len(source_packet_row_identity_issues),
        "source_packet_row_identity_issues": source_packet_row_identity_issues,
        "row_count": len(audited_rows),
        "source_packet_row_count": len(packet_row_hashes),
        "csv_field_count": len(csv_fields),
        "missing_csv_columns": missing_csv_columns,
        "duplicate_csv_key_count": duplicate_csv_key_count,
        "missing_packet_row_count": missing_packet_row_count,
        "unexpected_csv_row_count": unexpected_csv_row_count,
        "pending_decision_count": pending_count,
        "completed_decision_count": completed_count,
        "invalid_decision_count": invalid_count,
        "missing_required_field_row_count": missing_required_count,
        "source_mismatch_count": source_mismatch_count,
        "source_decision_packet_not_found_count": source_packet_missing_count,
        "source_decision_packet_not_portable_count": source_packet_not_portable_count,
        "source_decision_packet_unsupported_type_count": source_packet_unsupported_type_count,
        "source_decision_packet_unrecognized_count": source_packet_unrecognized_count,
        "invalid_evidence_refs_json_count": invalid_evidence_refs_count,
        "invalid_reviewer_id_count": invalid_reviewer_id_count,
        "invalid_reviewed_at_count": invalid_reviewed_at_count,
        "source_identity_mismatch_count": source_identity_mismatch_count,
        "unsafe_flag_count": unsafe_flag_count,
        "action_eligible_count": action_eligible_count,
        "decision_counts": {
            "reconfirm": sum(1 for row in audited_rows if row["decision"] == "reconfirm"),
            "downgrade_to_review_required": sum(
                1 for row in audited_rows if row["decision"] == "downgrade_to_review_required"
            ),
            "defer": sum(1 for row in audited_rows if row["decision"] == "defer"),
            "blank": pending_count,
            "invalid": invalid_count,
        },
        "issue_type_counts": issue_type_counts,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "guarded_apply_ready": False,
        "policy": {
            "report_only": True,
            "does_not_update_db": True,
            "does_not_change_existing_statuses": True,
            "blank_rows_are_not_import_ready": True,
            "reconfirm_is_evidence_review_only": True,
            "reconfirm_does_not_apply_or_preserve_status": True,
            "requires_separate_guarded_apply_with_explicit_operator_approval": True,
        },
        "rows": audited_rows,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    lines = [
        "# AI-HR Provenance Reconfirmation Decision Audit",
        "",
        f"- ok: `{report['ok']}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- csv: `{report['csv']}`",
        f"- source_packet: `{report['source_packet']}`",
        f"- source_packet_sha256: `{report['source_packet_sha256']}`",
        f"- source_packet_row_identity_issue_count: `{report['source_packet_row_identity_issue_count']}`",
        f"- row_count: `{report['row_count']}`",
        f"- pending_decision_count: `{report['pending_decision_count']}`",
        f"- completed_decision_count: `{report['completed_decision_count']}`",
        f"- invalid_decision_count: `{report['invalid_decision_count']}`",
        f"- missing_csv_columns: `{len(report['missing_csv_columns'])}`",
        f"- duplicate_csv_key_count: `{report['duplicate_csv_key_count']}`",
        f"- missing_packet_row_count: `{report['missing_packet_row_count']}`",
        f"- unexpected_csv_row_count: `{report['unexpected_csv_row_count']}`",
        f"- missing_required_field_row_count: `{report['missing_required_field_row_count']}`",
        f"- source_mismatch_count: `{report['source_mismatch_count']}`",
        f"- source_decision_packet_not_found_count: `{report['source_decision_packet_not_found_count']}`",
        f"- source_decision_packet_not_portable_count: `{report['source_decision_packet_not_portable_count']}`",
        f"- source_decision_packet_unsupported_type_count: `{report['source_decision_packet_unsupported_type_count']}`",
        f"- source_decision_packet_unrecognized_count: `{report['source_decision_packet_unrecognized_count']}`",
        f"- invalid_evidence_refs_json_count: `{report['invalid_evidence_refs_json_count']}`",
        f"- invalid_reviewer_id_count: `{report['invalid_reviewer_id_count']}`",
        f"- invalid_reviewed_at_count: `{report['invalid_reviewed_at_count']}`",
        f"- source_identity_mismatch_count: `{report['source_identity_mismatch_count']}`",
        f"- unsafe_flag_count: `{report['unsafe_flag_count']}`",
        f"- action_eligible_count: `{report['action_eligible_count']}`",
        f"- status_update_allowed: `{report['status_update_allowed']}`",
        f"- db_writes: `{report['db_writes']}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report['approval_claim']}`",
        f"- acceptance_claim: `{report.get('acceptance_claim')}`",
        f"- human_decision_required: `{report.get('human_decision_required')}`",
        f"- guarded_apply_ready: `{report['guarded_apply_ready']}`",
        "",
        "This audit is report-only. It does not approve, downgrade, preserve, or write any review status.",
        "A `reconfirm` decision is evidence-review input only until a separate guarded apply is explicitly approved.",
        "",
        "## Issue Type Counts",
        "",
    ]
    if report["issue_type_counts"]:
        for issue, count in sorted(report["issue_type_counts"].items()):
            lines.append(f"- `{issue}`: `{count}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Rows With Issues",
            "",
            "| order | target | decision | issues | missing_fields |",
            "|---:|---|---|---|---|",
        ]
    )
    issue_rows = [row for row in report["rows"] if row["issues"] or row["missing_fields"]]
    if not issue_rows:
        lines.append("|  |  |  | none |  |")
    else:
        for row in issue_rows:
            target = f"{row['target_table']}:{row['target_id']}"
            lines.append(
                f"| {row['order']} | {target} | {row['decision']} | "
                f"{', '.join(row['issues'])} | {', '.join(row['missing_fields'])} |"
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit a filled AI-HR provenance reconfirmation decision CSV without DB writes."
    )
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--source-packet", type=Path, default=DEFAULT_SOURCE_PACKET)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    report = build_decision_audit(args.csv, args.source_packet)
    args.out = args.out or default_output_path(args.csv, ".json")
    args.markdown_out = args.markdown_out or default_output_path(args.csv, ".md")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "out": rel(args.out),
                "markdown_out": rel(args.markdown_out),
                "ok": report["ok"],
                "row_count": report["row_count"],
                "pending_decision_count": report["pending_decision_count"],
                "completed_decision_count": report["completed_decision_count"],
                "invalid_decision_count": report["invalid_decision_count"],
                "action_eligible_count": report["action_eligible_count"],
                "db_writes": report["db_writes"],
                "status_update_allowed": report["status_update_allowed"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
