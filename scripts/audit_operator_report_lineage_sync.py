from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def sha256_artifact(path: Path | None, *, scope: str | None = None) -> str | None:
    if scope == "cycle_safe_release_readiness" and path is not None:
        try:
            payload = load_json(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return None
        if not payload or payload.get("sha256_scope") != scope:
            return None
        value = str(payload.get("cycle_safe_content_sha256") or "").strip()
        if value.startswith("sha256:") and len(value) == 71:
            return value
        return None
    return sha256_file(path) if path is not None else None


def markdown_generated_at(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    match = re.search(r"^- generated_at: `([^`]+)`", path.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def resolve_artifact(path_value: Any, *, base_dir: Path) -> Path | None:
    text = str(path_value or "").strip()
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    return base_dir / path


def source_hash_checks(
    *,
    source_paths: Any,
    source_hashes: Any,
    source_hash_scopes: Any = None,
    base_dir: Path,
    issue_prefix: str,
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(source_paths, dict) or not isinstance(source_hashes, dict):
        return {}
    if not isinstance(source_hash_scopes, dict):
        source_hash_scopes = {}
    checks: dict[str, dict[str, Any]] = {}
    for key, path_value in source_paths.items():
        if not path_value:
            continue
        path = resolve_artifact(path_value, base_dir=base_dir)
        expected_hash = source_hashes.get(key)
        scope = source_hash_scopes.get(key)
        actual_hash = sha256_artifact(path, scope=scope)
        exists_nonempty = bool(path and path.exists() and path.is_file() and path.stat().st_size > 0)
        checks[str(key)] = {
            "path": str(path_value),
            "exists_nonempty": exists_nonempty,
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "sha256_scope": scope,
            "hash_matches": bool(expected_hash and actual_hash and expected_hash == actual_hash),
        }
        if not exists_nonempty:
            issues.append(
                {
                    "severity": "fail",
                    "code": f"{issue_prefix}_source_artifact_missing",
                    "source_key": key,
                    "path": str(path_value),
                }
            )
        elif expected_hash != actual_hash:
            issues.append(
                {
                    "severity": "fail",
                    "code": f"{issue_prefix}_source_hash_stale",
                    "source_key": key,
                    "expected": actual_hash,
                    "actual": expected_hash,
                }
            )
    return checks


def handoff_bundle_hash_checks(
    *,
    bundle: Any,
    base_dir: Path,
    issue_prefix: str,
    issues: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(bundle, dict):
        return {}
    pairs = {
        "json": ("path", "sha256"),
        "markdown": ("markdown_path", "markdown_sha256"),
        "audit_json": ("audit_path", "audit_sha256"),
    }
    checks: dict[str, dict[str, Any]] = {}
    for label, (path_key, hash_key) in pairs.items():
        path_value = bundle.get(path_key)
        if not path_value:
            continue
        path = resolve_artifact(path_value, base_dir=base_dir)
        expected_hash = bundle.get(hash_key)
        actual_hash = sha256_file(path) if path is not None else None
        exists_nonempty = bool(path and path.exists() and path.is_file() and path.stat().st_size > 0)
        checks[label] = {
            "path": path_value,
            "exists_nonempty": exists_nonempty,
            "expected_sha256": expected_hash,
            "actual_sha256": actual_hash,
            "hash_matches": bool(expected_hash and actual_hash and expected_hash == actual_hash),
        }
        if not exists_nonempty:
            issues.append(
                {
                    "severity": "fail",
                    "code": f"{issue_prefix}_{label}_missing",
                    "path": path_value,
                }
            )
        elif expected_hash != actual_hash:
            issues.append(
                {
                    "severity": "fail",
                    "code": f"{issue_prefix}_{label}_hash_stale",
                    "expected": actual_hash,
                    "actual": expected_hash,
                }
            )
    return checks


def add_missing_issue(
    issues: list[dict[str, Any]],
    *,
    code: str,
    path: Path,
    severity: str = "fail",
) -> None:
    issues.append(
        {
            "severity": severity,
            "code": code,
            "path": str(path),
        }
    )


def build_lineage_sync_audit(
    *,
    next_actions_json: Path,
    next_actions_markdown: Path,
    handoff_json: Path,
    operator_audit_json: Path,
    operator_audit_markdown: Path,
    decision_sheet_json: Path,
    base_dir: Path = PROJECT_ROOT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    next_actions = load_json(next_actions_json)
    handoff = load_json(handoff_json)
    decision_sheet = load_json(decision_sheet_json)

    for label, path, payload in (
        ("next_actions_json", next_actions_json, next_actions),
        ("handoff_json", handoff_json, handoff),
        ("decision_sheet_json", decision_sheet_json, decision_sheet),
    ):
        if payload is None:
            add_missing_issue(issues, code=f"{label}_missing", path=path)

    next_md_generated_at = markdown_generated_at(next_actions_markdown)
    if not next_actions_markdown.exists():
        add_missing_issue(issues, code="next_actions_markdown_missing", path=next_actions_markdown)

    next_json_generated_at = next_actions.get("generated_at") if next_actions else None
    if next_actions is not None and next_json_generated_at != next_md_generated_at:
        issues.append(
            {
                "severity": "fail",
                "code": "next_actions_json_md_generated_at_mismatch",
                "json_generated_at": next_json_generated_at,
                "markdown_generated_at": next_md_generated_at,
            }
        )

    next_json_sha = sha256_file(next_actions_json)
    next_markdown_sha = sha256_file(next_actions_markdown)
    operator_audit_json_sha = sha256_file(operator_audit_json)
    operator_audit_markdown_sha = sha256_file(operator_audit_markdown)

    if not operator_audit_json.exists():
        add_missing_issue(issues, code="operator_audit_json_missing", path=operator_audit_json)
    if not operator_audit_markdown.exists():
        add_missing_issue(
            issues,
            code="operator_audit_markdown_missing",
            path=operator_audit_markdown,
        )

    handoff_next = (
        handoff.get("operator_next_actions")
        if isinstance(handoff, dict) and isinstance(handoff.get("operator_next_actions"), dict)
        else {}
    )
    handoff_operator_audit = (
        handoff.get("operator_packet_integrity_audit")
        if isinstance(handoff, dict)
        and isinstance(handoff.get("operator_packet_integrity_audit"), dict)
        else {}
    )
    if handoff is not None:
        expected_hashes = {
            "handoff_next_actions_json_hash_stale": (
                handoff_next.get("sha256"),
                next_json_sha,
            ),
            "handoff_next_actions_markdown_hash_stale": (
                handoff_next.get("markdown_sha256"),
                next_markdown_sha,
            ),
            "handoff_operator_audit_json_hash_stale": (
                handoff_operator_audit.get("sha256"),
                operator_audit_json_sha,
            ),
            "handoff_operator_audit_markdown_hash_stale": (
                handoff_operator_audit.get("markdown_sha256"),
                operator_audit_markdown_sha,
            ),
        }
        for code, (actual, expected) in expected_hashes.items():
            if actual != expected:
                issues.append(
                    {
                        "severity": "fail",
                        "code": code,
                        "expected": expected,
                        "actual": actual,
                    }
                )

    if decision_sheet is not None:
        generated = decision_sheet.get("generated_at")
        created = decision_sheet.get("created_at")
        content_hash = decision_sheet.get("content_sha256_excluding_self_hash")
        if not generated or generated != created:
            issues.append(
                {
                    "severity": "fail",
                    "code": "decision_sheet_generated_at_missing_or_mismatched",
                    "generated_at": generated,
                    "created_at": created,
                }
            )
        if not str(content_hash or "").startswith("sha256:"):
            issues.append(
                {
                    "severity": "fail",
                    "code": "decision_sheet_content_hash_missing",
                    "content_sha256_excluding_self_hash": content_hash,
                }
            )

    missing_open_first: list[dict[str, Any]] = []
    action_count = 0
    if next_actions is not None:
        next_actions_source_checks = source_hash_checks(
            source_paths=next_actions.get("source_paths"),
            source_hashes=next_actions.get("source_hashes"),
            source_hash_scopes=next_actions.get("source_hash_scopes"),
            base_dir=base_dir,
            issue_prefix="next_actions",
            issues=issues,
        )
        actions = next_actions.get("actions")
        if not isinstance(actions, list):
            issues.append({"severity": "fail", "code": "next_actions_actions_not_list"})
        else:
            action_count = len(actions)
            for action in actions:
                if not isinstance(action, dict):
                    missing_open_first.append({"id": None, "open_first": None})
                    continue
                open_first = resolve_artifact(action.get("open_first"), base_dir=base_dir)
                if open_first is None or not open_first.exists() or open_first.stat().st_size == 0:
                    missing_open_first.append(
                        {
                            "id": action.get("id") or action.get("blocker"),
                            "open_first": action.get("open_first"),
                        }
                    )
    if missing_open_first:
        issues.append(
            {
                "severity": "fail",
                "code": "next_actions_open_first_missing_or_empty",
                "items": missing_open_first,
            }
        )

    handoff_blocker_queue = (
        handoff.get("blocker_reduction_sprint_queue")
        if isinstance(handoff, dict)
        and isinstance(handoff.get("blocker_reduction_sprint_queue"), dict)
        else {}
    )
    handoff_blocker_queue_checks = handoff_bundle_hash_checks(
        bundle=handoff_blocker_queue,
        base_dir=base_dir,
        issue_prefix="handoff_blocker_reduction_sprint_queue",
        issues=issues,
    )

    return {
        "schema": "operator_report_lineage_sync_audit_v1",
        "generated_at": generated_at or now_iso(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "checks": {
            "next_actions_json_md_generated_at": {
                "json": next_json_generated_at,
                "markdown": next_md_generated_at,
            },
            "next_actions_hashes": {
                "json": next_json_sha,
                "markdown": next_markdown_sha,
            },
            "next_actions_source_hash_checks": next_actions_source_checks
            if next_actions is not None
            else {},
            "handoff_embedded_next_action_hashes": handoff_next,
            "handoff_embedded_operator_audit_hashes": handoff_operator_audit,
            "handoff_blocker_reduction_sprint_queue_hash_checks": handoff_blocker_queue_checks,
            "decision_sheet_generated_at": (
                decision_sheet.get("generated_at") if decision_sheet else None
            ),
            "decision_sheet_content_sha256_excluding_self_hash": (
                decision_sheet.get("content_sha256_excluding_self_hash")
                if decision_sheet
                else None
            ),
            "next_actions_open_first_count": action_count,
        },
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    checks = report.get("checks") if isinstance(report.get("checks"), dict) else {}
    next_generated = (
        checks.get("next_actions_json_md_generated_at")
        if isinstance(checks.get("next_actions_json_md_generated_at"), dict)
        else {}
    )
    next_hashes = (
        checks.get("next_actions_hashes")
        if isinstance(checks.get("next_actions_hashes"), dict)
        else {}
    )
    lines = [
        "# Operator Report Lineage Sync Audit",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- ok: `{report.get('ok')}`",
        f"- issue_count: `{report.get('issue_count')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## Checks",
        f"- next_actions JSON generated_at: `{next_generated.get('json')}`",
        f"- next_actions MD generated_at: `{next_generated.get('markdown')}`",
        f"- next_actions JSON sha256: `{next_hashes.get('json')}`",
        f"- next_actions MD sha256: `{next_hashes.get('markdown')}`",
        f"- decision_sheet generated_at: `{checks.get('decision_sheet_generated_at')}`",
        "- decision_sheet content hash: "
        f"`{checks.get('decision_sheet_content_sha256_excluding_self_hash')}`",
    ]
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    if issues:
        lines.extend(["", "## Issues"])
        for issue in issues:
            lines.append(f"- `{issue.get('code')}`")
    else:
        lines.extend(["", "No lineage sync issues found."])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def default_path(name: str) -> Path:
    return PROJECT_ROOT / "reports" / name


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit AI-HR operator next-actions and handoff lineage hashes."
    )
    parser.add_argument(
        "--next-actions-json",
        type=Path,
        default=default_path("aihr_operator_next_actions_20260712_10h.json"),
    )
    parser.add_argument(
        "--next-actions-markdown",
        type=Path,
        default=default_path("aihr_operator_next_actions_20260712_10h.md"),
    )
    parser.add_argument(
        "--handoff-json",
        type=Path,
        default=default_path("overnight_10h_operator_handoff_20260712_10h.json"),
    )
    parser.add_argument(
        "--operator-audit-json",
        type=Path,
        default=default_path("operator_review_packet_integrity_audit_20260712_10h.json"),
    )
    parser.add_argument(
        "--operator-audit-markdown",
        type=Path,
        default=default_path("operator_review_packet_integrity_audit_20260712_10h.md"),
    )
    parser.add_argument(
        "--decision-sheet-json",
        type=Path,
        default=default_path("human_review_provenance_reconfirmation_decision_sheet_20260712_10h.json"),
    )
    parser.add_argument("--base-dir", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    report = build_lineage_sync_audit(
        next_actions_json=args.next_actions_json,
        next_actions_markdown=args.next_actions_markdown,
        handoff_json=args.handoff_json,
        operator_audit_json=args.operator_audit_json,
        operator_audit_markdown=args.operator_audit_markdown,
        decision_sheet_json=args.decision_sheet_json,
        base_dir=args.base_dir,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "issue_count": report.get("issue_count"),
                "out": str(args.out),
                "markdown_out": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and not report.get("ok") else 0


if __name__ == "__main__":
    raise SystemExit(main())
