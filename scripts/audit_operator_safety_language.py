from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = PROJECT_ROOT / "reports" / "operator_safety_language_audit_20260629.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "operator_safety_language_audit_20260629.md"
DEFAULT_PATHS = (
    PROJECT_ROOT / "reports" / "overnight_sessions" / "readonly_refresh" / "human_review_afterfix_runbook_20260629.md",
    PROJECT_ROOT / "reports" / "overnight_sessions" / "readonly_refresh" / "overnight_afterfix_rollup_20260629.md",
    PROJECT_ROOT / "reports" / "aihr_agent_queue_20260627_extra.md",
    PROJECT_ROOT / "reports" / "aihr_agent_queue_status_20260627_extra_safe.md",
    PROJECT_ROOT
    / "reports"
    / "overnight_sessions"
    / "readonly_refresh"
    / "ksa_definition_review_operator_packet_20260629_next_priority_review_pack.csv",
    PROJECT_ROOT / "reports" / "overnight_sessions" / "readonly_refresh" / "remaining_blockers_report_20260629_next.md",
)


@dataclass(frozen=True)
class FindingRule:
    code: str
    severity: str
    description: str
    suggestion: str
    pattern: re.Pattern[str]
    file_name_pattern: re.Pattern[str] | None = None
    require_context_pattern: re.Pattern[str] | None = None
    skip_context_pattern: re.Pattern[str] | None = None


RULES = (
    FindingRule(
        code="ambiguous_trusted_count",
        severity="medium",
        description="Bare trusted_row_count can be read as audit-backed trust.",
        suggestion=(
            "Use legacy_trusted_status_rows_pending_reconfirmation or explicitly split "
            "audit-backed trusted rows from pending reconfirmation rows."
        ),
        pattern=re.compile(r"\btrusted_row_count\b", re.IGNORECASE),
    ),
    FindingRule(
        code="ambiguous_legacy_trusted_wording",
        severity="medium",
        description="Legacy trusted/reviewed wording is ambiguous without a provenance qualifier.",
        suggestion="Use legacy trusted-status rows pending packet-backed reconfirmation.",
        pattern=re.compile(r"legacy\s+trusted/reviewed\s+provenance\s+rows", re.IGNORECASE),
    ),
    FindingRule(
        code="unqualified_human_reviewed_count",
        severity="medium",
        description="A raw human_reviewed count needs an audit-backed or pending qualifier.",
        suggestion="Report human_reviewed counts with provenance status, not as a standalone trust claim.",
        pattern=re.compile(r"\bhuman_reviewed\s*:\s*\d+\b", re.IGNORECASE),
        skip_context_pattern=re.compile(r"audit-backed|packet-backed|pending|not_trusted", re.IGNORECASE),
    ),
    FindingRule(
        code="guarded_api_command_presented",
        severity="high",
        description="A runnable API collection command is shown while the same artifact says calls are blocked.",
        suggestion=(
            "Suppress the command or label it disabled when api_call_allowed_now=false or "
            "next_safe_action_status=complete_no_collection_needed."
        ),
        pattern=re.compile(r"retry-qualification-errors|collect-(training-courses|job-base|qualification-items)", re.IGNORECASE),
        require_context_pattern=re.compile(
            r"api_call_allowed_now\s*:\s*false|next_safe_action_status\s*:\s*complete_no_collection_needed",
            re.IGNORECASE,
        ),
    ),
    FindingRule(
        code="definition_review_action_promotional_wording",
        severity="medium",
        description="Manual definition review helper wording can be misread as a promotion instruction.",
        suggestion="Use draft_for_human_review_only or review_assist_only_no_db_write.",
        pattern=re.compile(r"write_manual_definition_from_task_evidence", re.IGNORECASE),
    ),
    FindingRule(
        code="blocker_count_field_ambiguous",
        severity="medium",
        description="A blocker count/threshold pair can be misread as no blocker remaining.",
        suggestion="Use current_trusted_count and required_minimum_count, or state the comparison explicitly.",
        pattern=re.compile(r"count\s*:\s*0", re.IGNORECASE),
        file_name_pattern=re.compile(r"remaining_blockers_report", re.IGNORECASE),
        require_context_pattern=re.compile(r"threshold\s*:\s*>\s*0", re.IGNORECASE),
    ),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def resolve_paths(paths: list[Path] | None) -> list[Path]:
    selected = paths or list(DEFAULT_PATHS)
    resolved: list[Path] = []
    for path in selected:
        candidate = path if path.is_absolute() else PROJECT_ROOT / path
        if candidate.exists() and candidate.is_file():
            resolved.append(candidate)
    return resolved


def audit_paths(paths: list[Path]) -> dict[str, Any]:
    resolved = resolve_paths(paths)
    findings: list[dict[str, Any]] = []
    for path in resolved:
        findings.extend(audit_file(path))
    severity_counts = {
        severity: sum(1 for finding in findings if finding["severity"] == severity)
        for severity in ("high", "medium", "low")
    }
    return {
        "schema": "operator_safety_language_audit_v1",
        "generated_at": now_iso(),
        "ok": not findings,
        "status": "pass" if not findings else "review_required",
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "paths_checked": [rel(path) for path in resolved],
        "path_count": len(resolved),
        "finding_count": len(findings),
        "severity_counts": severity_counts,
        "findings": findings,
    }


def audit_file(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    findings: list[dict[str, Any]] = []
    for rule in RULES:
        if rule.file_name_pattern and not rule.file_name_pattern.search(path.name):
            continue
        if rule.require_context_pattern and not rule.require_context_pattern.search(text):
            continue
        for line_no, line in enumerate(lines, start=1):
            if not rule.pattern.search(line):
                continue
            context = _context(lines, line_no)
            if rule.skip_context_pattern and rule.skip_context_pattern.search(context):
                continue
            findings.append(
                {
                    "code": rule.code,
                    "severity": rule.severity,
                    "path": rel(path),
                    "line": line_no,
                    "matched_text": line.strip()[:300],
                    "description": rule.description,
                    "suggestion": rule.suggestion,
                }
            )
    return findings


def _context(lines: list[str], line_no: int, radius: int = 2) -> str:
    start = max(0, line_no - radius - 1)
    end = min(len(lines), line_no + radius)
    return "\n".join(lines[start:end])


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Operator Safety Language Audit",
        "",
        f"- schema: `{payload['schema']}`",
        f"- generated_at: `{payload['generated_at']}`",
        f"- ok: `{str(payload['ok']).lower()}`",
        f"- status: `{payload['status']}`",
        f"- paths_checked: `{payload['path_count']}`",
        f"- findings: `{payload['finding_count']}`",
        f"- high: `{payload['severity_counts']['high']}`",
        f"- medium: `{payload['severity_counts']['medium']}`",
        f"- status_update_allowed: `{str(payload['status_update_allowed']).lower()}`",
        f"- db_writes: `{str(payload['db_writes']).lower()}`",
        f"- approval_claim: `{str(payload['approval_claim']).lower()}`",
        "",
    ]
    if payload["findings"]:
        lines.extend(
            [
                "## Findings",
                "",
                "| severity | code | file | line | suggestion |",
                "| --- | --- | --- | ---: | --- |",
            ]
        )
        for finding in payload["findings"]:
            lines.append(
                "| {severity} | `{code}` | `{path}` | {line} | {suggestion} |".format(
                    severity=finding["severity"],
                    code=finding["code"],
                    path=finding["path"],
                    line=finding["line"],
                    suggestion=finding["suggestion"].replace("|", "\\|"),
                )
            )
    else:
        lines.append("No risky operator-safety wording was detected in the selected artifacts.")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Audit report wording that could imply human approval, definition promotion, "
            "or guarded API execution."
        )
    )
    parser.add_argument("--path", action="append", type=Path, help="Artifact path to scan.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when findings are present. Default is report-only exit 0.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    payload = audit_paths(args.path or [])
    write_json(args.out, payload)
    write_markdown(args.markdown_out, payload)
    print(
        json.dumps(
            {
                "ok": payload["ok"],
                "status": payload["status"],
                "finding_count": payload["finding_count"],
                "out": rel(args.out),
                "markdown_out": rel(args.markdown_out),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and payload["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
