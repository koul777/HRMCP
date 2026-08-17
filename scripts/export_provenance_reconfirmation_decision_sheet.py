from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timezone
from html import escape
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


DEFAULT_PACKET = _latest_report_path(
    "aihr_human_review_provenance_reconfirmation_packet_20*.json",
    "human_review_provenance_reconfirmation_packet_20*.json",
    fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_packet_20260620.json",
)


def _artifact_suffix(path: Path, *, prefixes: tuple[str, ...]) -> str:
    stem = path.stem
    for prefix in prefixes:
        if stem.startswith(prefix):
            return stem.removeprefix(prefix)
    return stem


def default_output_path(packet_path: Path, suffix: str) -> Path:
    packet_suffix = _artifact_suffix(
        packet_path,
        prefixes=(
            "aihr_human_review_provenance_reconfirmation_packet_",
            "human_review_provenance_reconfirmation_packet_",
        ),
    )
    return REPORTS / f"human_review_provenance_reconfirmation_decision_sheet_{packet_suffix}{suffix}"


DECISION_OPTIONS = "reconfirm | downgrade_to_review_required | defer"
DECISION_FIELDS = [
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
    "status_update_allowed",
    "db_writes",
    "approval_claim",
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
        raise ValueError("packet JSON root must be an object")
    schema = payload.get("schema")
    if schema != "aihr_human_review_provenance_reconfirmation_packet_v1":
        raise ValueError(f"unsupported packet schema: {schema!r}")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("packet rows must be a list")
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


def content_sha256(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


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


def clean_text(value: Any, *, max_chars: int = 700) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "... [truncated]"


def safe_review_status_display(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.startswith("legacy_status_needs_reconfirmation"):
        return "legacy_status_needs_reconfirmation"
    if text in {"human_reviewed", "accepted", "reviewed"}:
        return "trusted_status_suppressed"
    return text


def safe_review_status_display_counts(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        safe_key = safe_review_status_display(key)
        if not safe_key:
            continue
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            numeric_count = 0
        counts[safe_key] = counts.get(safe_key, 0) + numeric_count
    return counts


def scope_summary(scope: Any) -> str:
    if not isinstance(scope, dict):
        return ""
    preferred = [
        "major_code",
        "major_name",
        "middle_name",
        "small_name",
        "sub_name",
        "matched_unit_code",
        "matched_unit_name",
        "unit_code",
        "unit_name",
        "expected_current_match_text",
        "expected_target_match_text",
        "expected_course_names_json",
        "competency_level_raw",
        "position_level_raw",
    ]
    parts: list[str] = []
    for key in preferred:
        value = scope.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={clean_text(value, max_chars=160)}")
    return clean_text("; ".join(parts), max_chars=1200)


def decision_row(row: dict[str, Any], *, source_packet_sha256: str) -> dict[str, Any]:
    return {
        "order": str(row.get("order") or ""),
        "surface": clean_text(row.get("surface")),
        "target_table": clean_text(row.get("target_table")),
        "target_id": clean_text(row.get("target_id")),
        "entity_type": clean_text(row.get("entity_type")),
        "review_status_display": safe_review_status_display(row.get("review_status_display")),
        "status_trust": clean_text(row.get("status_trust")),
        "provenance_state": clean_text(row.get("provenance_state")),
        "display": clean_text(row.get("display"), max_chars=500),
        "scope_summary": scope_summary(row.get("scope")),
        "requested_decision_options": DECISION_OPTIONS,
        "decision": "",
        "rationale": "",
        "reviewer_id": "",
        "reviewed_at": "",
        "source_decision_packet": "",
        "source_packet_sha256": source_packet_sha256,
        "source_packet_row_sha256": packet_row_sha256(row),
        "evidence_refs_json": "",
        "notes": "",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }


def build_decision_sheet(packet_path: Path) -> dict[str, Any]:
    source_packet_sha256 = content_sha256(packet_path.read_bytes())
    packet = load_packet(packet_path)
    source_packet_contract_issues = packet_contract_issues(packet)
    source_packet_row_identity_issues = packet_row_identity_issues(packet)
    rows = [
        decision_row(row, source_packet_sha256=source_packet_sha256)
        for row in packet.get("rows") or []
        if isinstance(row, dict)
    ]
    blank_count = sum(1 for row in rows if not row["decision"].strip())
    generated_at = now_iso()
    report = {
        "ok": not source_packet_contract_issues and not source_packet_row_identity_issues,
        "schema": "aihr_provenance_reconfirmation_decision_sheet_v1",
        "created_at": generated_at,
        "generated_at": generated_at,
        "source_packet": rel(packet_path),
        "source_packet_sha256": source_packet_sha256,
        "source_packet_schema": packet.get("schema"),
        "source_packet_contract_ok": not source_packet_contract_issues,
        "source_packet_contract_issues": source_packet_contract_issues,
        "source_packet_row_identity_issue_count": len(source_packet_row_identity_issues),
        "source_packet_row_identity_issues": source_packet_row_identity_issues,
        "row_count": len(rows),
        "blank_decision_count": blank_count,
        "completed_decision_count": len(rows) - blank_count,
        "allowed_decisions": [
            "reconfirm",
            "downgrade_to_review_required",
            "defer",
        ],
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "minimum_packet_fields": [
            "reviewer_id",
            "reviewed_at",
            "source_decision_packet",
            "rationale",
            "evidence_refs_json",
        ],
        "surface_counts": packet.get("selected_surface_counts")
        or packet.get("surface_counts")
        or {},
        "review_status_display_counts": safe_review_status_display_counts(
            packet.get("review_status_display_counts")
        ),
        "rows": rows,
        "policy": {
            "decision_fields_are_blank_by_default": True,
            "does_not_change_existing_statuses": True,
            "not_a_guarded_import_plan": True,
            "requires_separate_audit_before_any_future_import": True,
            "reconfirm_is_evidence_review_only": True,
            "reconfirm_does_not_apply_or_preserve_status": True,
        },
    }
    report["content_sha256_excluding_self_hash"] = content_sha256(stable_json(report))
    report["content_hash_algorithm"] = (
        "sha256(stable_json(report_without_content_sha256_excluding_self_hash))"
    )
    return report


def write_csv(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DECISION_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in report["rows"]:
            writer.writerow({field: export_cell_value(row.get(field)) for field in DECISION_FIELDS})


def export_cell_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = str(value).lower()
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text[:1] in {"=", "+", "-", "@"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def write_html(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows_html = []
    for row in report["rows"]:
        cells = "".join(
            f"<td>{escape(str(export_cell_value(row.get(field)) or ''))}</td>"
            for field in DECISION_FIELDS
        )
        rows_html.append(f"<tr>{cells}</tr>")
    head_cells = "".join(f"<th>{escape(field)}</th>" for field in DECISION_FIELDS)
    html = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <title>AI-HR Provenance Reconfirmation Decision Sheet</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 24px; color: #202124; }}
    table {{ border-collapse: collapse; width: 100%; font-size: 12px; }}
    th, td {{ border: 1px solid #d0d7de; padding: 6px; vertical-align: top; }}
    th {{ background: #f6f8fa; position: sticky; top: 0; }}
    .meta {{ margin-bottom: 16px; }}
    .warning {{ color: #8a4b00; font-weight: 600; }}
  </style>
</head>
<body>
  <h1>AI-HR Provenance Reconfirmation Decision Sheet</h1>
  <div class="meta">
    <p>Source packet: <code>{escape(str(report.get("source_packet") or ""))}</code></p>
    <p>Rows: <strong>{report["row_count"]}</strong>; blank decisions: <strong>{report["blank_decision_count"]}</strong></p>
    <p class="warning">This sheet is review-only. It does not update DB status, approve legacy rows, or preserve any status. A reconfirm value is evidence-review input for a later audited guarded workflow.</p>
  </div>
  <table>
    <thead><tr>{head_cells}</tr></thead>
    <tbody>{''.join(rows_html)}</tbody>
  </table>
</body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Provenance Reconfirmation Decision Sheet",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Source packet: `{report['source_packet']}`",
        f"- Content hash: `{report.get('content_sha256_excluding_self_hash')}`",
        f"- Source packet row identity issues: `{report['source_packet_row_identity_issue_count']}`",
        f"- Row count: `{report['row_count']}`",
        f"- Blank decisions: `{report['blank_decision_count']}`",
        f"- DB writes: `{report['db_writes']}`",
        f"- API calls: `{report.get('api_calls')}`",
        f"- Status update allowed: `{report['status_update_allowed']}`",
        f"- Approval claim: `{report['approval_claim']}`",
        "",
        "## Required Human Fields",
        "",
    ]
    for field in report["minimum_packet_fields"]:
        lines.append(f"- `{field}`")
    lines.extend(["", "## Decision Options", ""])
    for decision in report["allowed_decisions"]:
        lines.append(f"- `{decision}`")
    lines.extend(
        [
            "",
            "A `reconfirm` value is evidence-review input only. It is not a DB write, approval claim, or status-preservation action.",
        ]
    )
    lines.extend(["", "## Rows", "", "| order | surface | target | display | decision |", "|---:|---|---|---|---|"])
    for row in report["rows"]:
        target = f"{row['target_table']}:{row['target_id']}"
        lines.append(
            "| "
            f"{markdown_cell(row['order'])} | "
            f"{markdown_cell(row['surface'])} | "
            f"{markdown_cell(target)} | "
            f"{markdown_cell(row['display'])} |  |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def markdown_cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export a blank decision sheet from an AI-HR provenance reconfirmation packet."
    )
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--html-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    report = build_decision_sheet(args.packet)
    args.out = args.out or default_output_path(args.packet, ".json")
    args.csv_out = args.csv_out or default_output_path(args.packet, ".csv")
    args.html_out = args.html_out or default_output_path(args.packet, ".html")
    args.markdown_out = args.markdown_out or default_output_path(args.packet, ".md")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(args.csv_out, report)
    write_html(args.html_out, report)
    write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "out": rel(args.out),
                "csv_out": rel(args.csv_out),
                "html_out": rel(args.html_out),
                "markdown_out": rel(args.markdown_out),
                "row_count": report["row_count"],
                "blank_decision_count": report["blank_decision_count"],
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
