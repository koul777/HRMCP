from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "aihr_terminal_evidence_index_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
TERMINAL_LABELS = {
    "operator_entrypoint_manifest",
    "post_handoff_validation",
    "post_decision_validation_matrix",
    "terminal_evidence_index",
}
TERMINAL_SCHEMA_LABELS = {
    "aihr_operator_entrypoint_manifest_v1": "operator_entrypoint_manifest",
    "aihr_post_handoff_validation_v1": "post_handoff_validation",
    "aihr_post_decision_validation_matrix_v1": "post_decision_validation_matrix",
    "aihr_terminal_evidence_index_v1": "terminal_evidence_index",
}
DEFAULT_ARTIFACT_TEMPLATES = [
    ("operator_entrypoint_manifest", "reports/aihr_operator_entrypoint_manifest_{stamp}.json"),
    ("operator_entrypoint_manifest_markdown", "reports/aihr_operator_entrypoint_manifest_{stamp}.md"),
    ("post_handoff_validation", "reports/aihr_post_handoff_validation_{stamp}.json"),
    ("post_handoff_validation_markdown", "reports/aihr_post_handoff_validation_{stamp}.md"),
    ("operator_handoff", "reports/overnight_10h_operator_handoff_{stamp}.json"),
    ("operator_handoff_markdown", "reports/overnight_10h_operator_handoff_{stamp}.md"),
    ("operator_next_actions", "reports/aihr_operator_next_actions_{stamp}.json"),
    ("blocker_sprint_queue", "reports/aihr_blocker_reduction_operator_sprint_queue_{stamp}.json"),
    ("release_refresh_dag", "reports/aihr_release_operator_refresh_dag_{stamp}.json"),
    ("release_refresh_dag_audit", "reports/aihr_release_operator_refresh_dag_audit_{stamp}.json"),
    ("acceptance_closure", "reports/aihr_agent_queue_acceptance_closure_{stamp}.json"),
    (
        "operator_json_powershell_compatibility",
        "reports/operator_json_powershell_compatibility_audit_{stamp}.json",
    ),
    (
        "operator_primary_packet_readability",
        "reports/review_artifact_readability_operator_primary_packet_surface_{stamp}.json",
    ),
    (
        "operator_entrypoint_readability",
        "reports/review_artifact_readability_operator_entrypoint_manifest_{stamp}.json",
    ),
    (
        "post_handoff_readability",
        "reports/review_artifact_readability_post_handoff_validation_{stamp}.json",
    ),
    ("operator_packet_integrity", "reports/operator_review_packet_integrity_audit_{stamp}.json"),
    ("operator_report_lineage", "reports/operator_report_lineage_sync_audit_{stamp}.json"),
    (
        "transition_crosswalk_audit",
        "reports/transition_provenance_operator_crosswalk_audit_{stamp}.json",
    ),
]


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


def load_json(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return None, str(exc)
    if not isinstance(payload, dict):
        return None, "json_root_not_object"
    return payload, None


def sha256_artifact(path: Path | None, *, scope: str | None = None) -> str | None:
    if scope == "cycle_safe_release_readiness" and path is not None:
        payload, error = load_json(path)
        if error or not payload or payload.get("sha256_scope") != scope:
            return None
        value = str(payload.get("cycle_safe_content_sha256") or "").strip()
        if value.startswith("sha256:") and len(value) == 71:
            return value
        return None
    return sha256_file(path)


def artifact_kind(path: Path | None) -> str:
    suffix = (path.suffix if path else "").lower()
    if suffix == ".json":
        return "json"
    if suffix == ".md":
        return "markdown"
    if suffix == ".csv":
        return "csv"
    if suffix == ".log":
        return "log"
    return suffix.lstrip(".") or "unknown"


def add_issue(issues: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    issues.append({"code": code, "message": message, **extra})


def add_warning(warnings: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    warnings.append({"code": code, "message": message, **extra})


def safety_contract(payload: dict[str, Any]) -> dict[str, bool]:
    return {
        "report_only_is_true": payload.get("report_only") is True,
        "status_update_allowed_is_false": payload.get("status_update_allowed") is False,
        "db_writes_is_false": payload.get("db_writes") is False,
        "api_calls_is_false_or_absent": payload.get("api_calls") in (False, None),
        "approval_claim_is_false": payload.get("approval_claim") is False,
        "acceptance_claim_is_false_or_absent": payload.get("acceptance_claim") in (False, None),
        "human_decision_required_is_true_or_absent": payload.get("human_decision_required") is not False,
    }


def effective_terminal_label(label: str, payload: dict[str, Any]) -> str | None:
    schema_label = TERMINAL_SCHEMA_LABELS.get(str(payload.get("schema") or ""))
    if schema_label:
        return schema_label
    return label if label in TERMINAL_LABELS else None


def terminal_contract(label: str, payload: dict[str, Any]) -> dict[str, bool]:
    if effective_terminal_label(label, payload) not in TERMINAL_LABELS:
        return {}
    return {
        "terminal_evidence_only": payload.get("terminal_evidence_only") is True,
        "not_in_release_refresh_dag": payload.get("include_in_release_refresh_dag") is not True,
        "not_in_operator_handoff": payload.get("include_in_operator_handoff") is not True,
    }


def positive_json_counters(payload: dict[str, Any], counters: tuple[str, ...]) -> list[dict[str, Any]]:
    positives: list[dict[str, Any]] = []
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    for prefix, source in (("", payload), ("summary.", summary)):
        for counter in counters:
            value = source.get(counter)
            if isinstance(value, int) and value > 0:
                positives.append({"counter": f"{prefix}{counter}", "value": value})
    return positives


def warning_code_counts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}

    def visit(items: Any) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "").strip()
            if code:
                counts[code] = counts.get(code, 0) + 1
            visit(item.get("source_warnings"))

    visit(payload.get("warnings"))
    return [{"code": code, "count": count} for code, count in counts.items()]


def embedded_hash_mismatches(
    payload: dict[str, Any],
    *,
    root: Path = PROJECT_ROOT,
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field in ("source_hash_checks", "next_actions_source_hash_checks"):
        checks = payload.get(field)
        if not isinstance(checks, dict):
            continue
        for key, check in checks.items():
            if not isinstance(check, dict):
                continue
            reported_match = check.get("hash_matches")
            if reported_match is None:
                reported_match = check.get("matches")
            path_value = check.get("path") or check.get("source_path")
            expected = (
                check.get("expected_sha256")
                or check.get("source_sha256")
                or check.get("sha256")
            )
            scope = check.get("sha256_scope")
            resolved = resolve_artifact(path_value, root=root) if path_value else None
            actual = sha256_artifact(resolved, scope=scope)
            reason = None
            if not path_value:
                reason = "missing_source_path"
            elif not expected:
                reason = "missing_expected_sha256"
            elif not actual:
                reason = "source_missing_or_empty"
            elif expected != actual:
                reason = "current_source_hash_mismatch"
            elif reported_match is not True:
                reason = "reported_hash_mismatch"
            if reason:
                mismatches.append(
                    {
                        "field": field,
                        "key": key,
                        "path": portable_path(path_value, root=root),
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                        "sha256_scope": scope,
                        "reported_hash_matches": reported_match,
                        "reason": reason,
                    }
                )
    return mismatches


def unsafe_text_flag_lines(path: Path | None) -> list[str]:
    if not path or not path.exists() or path.suffix.lower() not in {".md", ".txt"}:
        return []
    unsafe_field = re.compile(
        r"\b(status_update_allowed|db_writes|approval_claim|acceptance_claim)\b.*\btrue\b",
        re.IGNORECASE,
    )
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if unsafe_field.search(line):
            lines.append(line.strip()[:240])
    return lines


def affirmative_status_claim_lines(path: Path | None) -> list[str]:
    if not path or not path.exists() or path.suffix.lower() not in {".md", ".txt"}:
        return []
    status_token = re.compile(r"(?<![A-Za-z0-9_])(human_reviewed|accepted|reviewed)(?![A-Za-z0-9_])")
    claim_context = re.compile(
        r"\b(status|decision|claim|approval|approved|set|marked|promoted|verified)\b",
        re.IGNORECASE,
    )
    safe_context = (
        "does not",
        "do not",
        "not ",
        "no ",
        "forbidden",
        "required",
        "pending",
        "reviewed_at",
        "human_reviewed_concepts",
        "human_reviewed_goal_links",
        "human_reviewed_task_relations",
    )
    lines: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        lowered = line.lower()
        if any(token in lowered for token in safe_context):
            continue
        if status_token.search(lowered) and claim_context.search(lowered):
            lines.append(line.strip()[:240])
    return lines


def normalize_artifacts(
    artifacts: list[tuple[str, str | Path]] | None,
    *,
    stamp: str | None,
) -> list[tuple[str, str | Path]]:
    items = list(artifacts or [])
    if not stamp:
        if items:
            return items
        raise ValueError("Either --stamp or at least one --artifact is required.")
    defaults = [(label, template.format(stamp=stamp)) for label, template in DEFAULT_ARTIFACT_TEMPLATES]
    return defaults + items


def infer_open_first(records: list[dict[str, Any]]) -> str | None:
    preferred_labels = (
        "operator_entrypoint_manifest_markdown",
        "post_decision_validation_matrix_markdown",
        "post_handoff_validation_markdown",
    )
    for label in preferred_labels:
        for item in records:
            if item.get("label") == label and item.get("exists_nonempty") is True:
                return item.get("path")
    for item in records:
        if item.get("kind") == "markdown" and item.get("exists_nonempty") is True:
            return item.get("path")
    for item in records:
        if item.get("exists_nonempty") is True:
            return item.get("path")
    return None


def summarize_artifact(
    label: str,
    value: str | Path,
    *,
    root: Path,
    issues: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved = resolve_artifact(value, root=root)
    exists_nonempty = bool(
        resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
    )
    record: dict[str, Any] = {
        "label": label,
        "path": portable_path(value, root=root),
        "kind": artifact_kind(resolved),
        "exists_nonempty": exists_nonempty,
        "sha256": sha256_file(resolved),
        "json_ok": None,
        "schema": None,
        "ok_field": None,
        "status_field": None,
        "safety_contract": {},
        "terminal_contract": {},
        "effective_terminal_label": None,
        "embedded_hash_mismatches": [],
        "reported_issue_counters": [],
        "reported_warning_counters": [],
        "source_warning_code_counts": [],
        "unsafe_text_flags": [],
        "affirmative_status_claims": [],
        "artifact_ok": True,
    }
    if not exists_nonempty:
        add_issue(
            issues,
            "artifact_missing_or_empty",
            "Required terminal evidence artifact is missing or empty.",
            label=label,
            path=record["path"],
        )
        record["artifact_ok"] = False
        return record

    if resolved and resolved.suffix.lower() == ".json":
        payload, error = load_json(resolved)
        if error or payload is None:
            add_issue(
                issues,
                "json_parse_failed",
                "Terminal evidence JSON artifact is not a JSON object.",
                label=label,
                path=record["path"],
                error=error,
            )
            record["artifact_ok"] = False
            record["json_ok"] = False
            return record
        record["json_ok"] = True
        record["schema"] = payload.get("schema")
        record["ok_field"] = payload.get("ok")
        record["status_field"] = payload.get("status")
        record["effective_terminal_label"] = effective_terminal_label(label, payload)
        contract = safety_contract(payload)
        record["safety_contract"] = contract
        if not all(contract.values()):
            add_issue(
                issues,
                "unsafe_json_contract",
                "JSON artifact does not preserve the report-only human-gated safety contract.",
                label=label,
                path=record["path"],
                contract=contract,
            )
            record["artifact_ok"] = False
        terminal = terminal_contract(label, payload)
        record["terminal_contract"] = terminal
        if terminal and not all(terminal.values()):
            add_issue(
                issues,
                "unsafe_terminal_cycle_contract",
                "Terminal evidence artifact can feed back into upstream handoff or DAG flow.",
                label=label,
                path=record["path"],
                contract=terminal,
            )
            record["artifact_ok"] = False
        if payload.get("ok") is False:
            add_issue(
                issues,
                "json_reports_not_ok",
                "JSON artifact explicitly reports ok=false.",
                label=label,
                path=record["path"],
            )
            record["artifact_ok"] = False
        reported_issue_counters = positive_json_counters(payload, ("issue_count", "finding_count"))
        record["reported_issue_counters"] = reported_issue_counters
        if reported_issue_counters:
            add_issue(
                issues,
                "json_reports_findings",
                "JSON artifact reports open issues or findings.",
                label=label,
                path=record["path"],
                counters=reported_issue_counters,
            )
            record["artifact_ok"] = False
        reported_warning_counters = positive_json_counters(payload, ("warning_count",))
        record["reported_warning_counters"] = reported_warning_counters
        if reported_warning_counters:
            source_warning_code_counts = warning_code_counts(payload)
            record["source_warning_code_counts"] = source_warning_code_counts
            add_warning(
                warnings,
                "json_reports_warnings",
                "JSON artifact reports warnings.",
                label=label,
                path=record["path"],
                counters=reported_warning_counters,
                source_warning_code_counts=source_warning_code_counts,
            )
        mismatches = embedded_hash_mismatches(payload, root=root)
        record["embedded_hash_mismatches"] = mismatches
        if mismatches:
            add_issue(
                issues,
                "embedded_source_hash_mismatch",
                "JSON artifact contains stale embedded source hash checks.",
                label=label,
                path=record["path"],
                mismatches=mismatches,
            )
            record["artifact_ok"] = False

    unsafe_flags = unsafe_text_flag_lines(resolved)
    record["unsafe_text_flags"] = unsafe_flags
    if unsafe_flags:
        add_issue(
            issues,
            "unsafe_markdown_flag",
            "Text artifact contains an unsafe true flag.",
            label=label,
            path=record["path"],
            lines=unsafe_flags,
        )
        record["artifact_ok"] = False
    status_claims = affirmative_status_claim_lines(resolved)
    record["affirmative_status_claims"] = status_claims
    if status_claims:
        add_warning(
            warnings,
            "affirmative_status_claim_text_present",
            "Text artifact contains language that may imply a human-gated status claim.",
            label=label,
            path=record["path"],
            lines=status_claims,
        )
    return record


def build_index(
    *,
    artifacts: list[tuple[str, str | Path]] | None = None,
    stamp: str | None = None,
    root: Path = PROJECT_ROOT,
    generated_at: str | None = None,
    require_post_decision_gate: bool = False,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    issues: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    normalized = normalize_artifacts(artifacts, stamp=stamp)
    records = [
        summarize_artifact(label, path, root=root, issues=issues, warnings=warnings)
        for label, path in normalized
    ]
    json_records = [item for item in records if item.get("kind") == "json"]
    terminal_records = [
        item for item in records if item.get("effective_terminal_label") in TERMINAL_LABELS
    ]
    post_decision_gate_included = any(
        item.get("effective_terminal_label") == "post_decision_validation_matrix"
        for item in records
    )
    post_decision_gate = {
        "included": post_decision_gate_included,
        "required": require_post_decision_gate,
        "default_included": False,
        "mode": "opt_in_terminal_evidence",
    }
    if require_post_decision_gate and not post_decision_gate_included:
        add_issue(
            issues,
            "post_decision_gate_missing",
            "Post-decision validation matrix was required but not included in terminal evidence artifacts.",
        )
    elif stamp and not post_decision_gate_included:
        add_warning(
            warnings,
            "post_decision_gate_not_in_default",
            "Default terminal evidence does not include the opt-in post-decision validation matrix.",
        )
    open_first = f"reports/aihr_operator_entrypoint_manifest_{stamp}.md" if stamp else infer_open_first(records)
    validate_first = f"reports/aihr_terminal_evidence_index_{stamp}.md" if stamp else None
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
        "operator_start": {
            "open_first": open_first,
            "validate_first": validate_first,
            "manual_decision_required_before_any_status_change": True,
        },
        "cycle_policy": {
            "must_not_be_source_for": [
                "operator_next_actions",
                "operator_handoff",
                "release_operator_refresh_dag",
            ],
            "terminal_reports_checked": [
                item.get("effective_terminal_label") or item["label"]
                for item in terminal_records
            ],
        },
        "post_decision_gate": post_decision_gate,
        "summary": {
            "artifact_count": len(records),
            "json_count": len(json_records),
            "artifact_ok_count": sum(1 for item in records if item.get("artifact_ok") is True),
            "terminal_artifact_count": len(terminal_records),
            "source_hash_mismatch_count": sum(
                len(item.get("embedded_hash_mismatches") or []) for item in records
            ),
            "unsafe_text_flag_count": sum(len(item.get("unsafe_text_flags") or []) for item in records),
            "affirmative_status_claim_warning_count": sum(
                len(item.get("affirmative_status_claims") or []) for item in records
            ),
            "issue_count": len(issues),
            "warning_count": len(warnings),
        },
        "artifacts": records,
        "issues": issues,
        "warnings": warnings,
        "notes": [
            "This index is terminal evidence only and must not feed back into handoff or refresh DAG source hashes.",
            "A pass confirms artifact shape and safety contracts; it is not approval, acceptance, or human review.",
            "Manual operator decisions remain required before any human_reviewed, accepted, or reviewed status can change.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    lines = [
        "# AI-HR Terminal Evidence Index",
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
        "## Operator Start",
        "",
    ]
    start = payload.get("operator_start") if isinstance(payload.get("operator_start"), dict) else {}
    lines.append(f"- validate_first: `{start.get('validate_first')}`")
    lines.append(f"- open_first: `{start.get('open_first')}`")
    lines.append(
        "- manual_decision_required_before_any_status_change: "
        f"`{start.get('manual_decision_required_before_any_status_change')}`"
    )
    gate = payload.get("post_decision_gate") if isinstance(payload.get("post_decision_gate"), dict) else {}
    lines.extend(["", "## Post-Decision Gate", ""])
    lines.append(f"- included: `{gate.get('included')}`")
    lines.append(f"- required: `{gate.get('required')}`")
    lines.append(f"- mode: `{gate.get('mode')}`")
    lines.extend(["", "## Summary", ""])
    for key in (
        "artifact_count",
        "json_count",
        "artifact_ok_count",
        "terminal_artifact_count",
        "source_hash_mismatch_count",
        "unsafe_text_flag_count",
        "affirmative_status_claim_warning_count",
        "issue_count",
        "warning_count",
    ):
        lines.append(f"- {key}: `{summary.get(key)}`")
    lines.extend(["", "## Artifacts", ""])
    for item in payload.get("artifacts") or []:
        lines.append(
            "- "
            f"{item.get('label')}: `{item.get('path')}` "
            f"kind=`{item.get('kind')}` artifact_ok=`{item.get('artifact_ok')}` "
            f"schema=`{item.get('schema')}` "
            f"terminal_label=`{item.get('effective_terminal_label')}`"
        )
    if payload.get("issues"):
        lines.extend(["", "## Issues", ""])
        for issue in payload.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: {issue.get('message')}")
    else:
        lines.extend(["", "No terminal evidence index issues found."])
    if payload.get("warnings"):
        lines.extend(["", "## Warnings", ""])
        for warning in payload.get("warnings") or []:
            lines.append(f"- `{warning.get('code')}`: {warning.get('message')}")
    lines.extend(["", "## Notes", ""])
    lines.extend(f"- {note}" for note in payload.get("notes") or [])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_artifact_spec(spec: str) -> tuple[str, str]:
    if "=" in spec:
        label, value = spec.split("=", 1)
    elif ":" in spec and not re.match(r"^[A-Za-z]:[\\/]", spec):
        label, value = spec.split(":", 1)
    else:
        value = spec
        label = Path(strip_fragment(spec)).stem
    label = label.strip()
    value = value.strip()
    if not label or not value:
        raise argparse.ArgumentTypeError("Artifact must be LABEL=PATH or PATH.")
    return label, value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build terminal AI-HR evidence index.")
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--stamp")
    parser.add_argument("--artifact", action="append", type=parse_artifact_spec, default=[])
    parser.add_argument("--require-post-decision-gate", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_index(
        artifacts=args.artifact,
        stamp=args.stamp,
        root=args.root,
        require_post_decision_gate=args.require_post_decision_gate,
    )
    if args.markdown_out and not report.get("operator_start", {}).get("validate_first"):
        report["operator_start"]["validate_first"] = portable_path(args.markdown_out, root=args.root)
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "status": report.get("status"),
                "artifact_count": report.get("summary", {}).get("artifact_count"),
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
