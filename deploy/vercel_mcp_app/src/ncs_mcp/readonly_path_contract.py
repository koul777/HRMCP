from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


SCHEMA = "ncs_readonly_refresh_path_contract_v1"
REPORT_PATH_RE = re.compile(r"reports[\\/][^\s`'\"|)>,]+")
DATE_RE = re.compile(r"(20\d{6})")
DATED_SUFFIX_RE = re.compile(r"20\d{6}(?:_[A-Za-z0-9]+)*(?=\.)")
PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")
ALLOWED_REWRITE_PLACEHOLDERS = {"date", "artifact_suffix"}


def _norm_path(value: str) -> str:
    normalized = str(value or "").replace("/", "\\").strip()
    return re.sub(r"\\{2,}", r"\\", normalized)


def _artifact_dir_duplicate_path(path_text: str, artifact_dir: str) -> str | None:
    normalized = _norm_path(path_text).rstrip("\\")
    allowed = _norm_path(artifact_dir).rstrip("\\")
    suffix = allowed.rsplit("\\", 1)[-1]
    duplicate = f"{allowed}\\{suffix}"
    if normalized.lower() == duplicate.lower():
        return duplicate
    return None


def _path_tokens(value: Any) -> list[str]:
    if not isinstance(value, str):
        return []
    text = _norm_path(value)
    if "<" in text or ">" in text:
        return []
    if text.startswith("reports\\"):
        return [text]
    return [
        token
        for token in (_norm_path(match.group(0)) for match in REPORT_PATH_RE.finditer(value))
        if "<" not in token and ">" not in token
    ]


def _walk_strings(value: Any, path: str = "$") -> list[tuple[str, str]]:
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, list):
        rows: list[tuple[str, str]] = []
        for index, item in enumerate(value):
            rows.extend(_walk_strings(item, f"{path}[{index}]"))
        return rows
    if isinstance(value, dict):
        rows = []
        for key, item in value.items():
            rows.extend(_walk_strings(item, f"{path}.{key}"))
        return rows
    return []


def _load_json(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, {
            "artifact": str(path),
            "code": "malformed_or_unreadable_json",
            "message": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return None, {
            "artifact": str(path),
            "code": "json_root_not_object",
            "message": "Readonly path contract artifacts must have a JSON object root.",
        }
    return payload, None


def build_readonly_refresh_path_contract(
    artifacts: list[Path],
    *,
    artifact_dir: str = "reports\\overnight_sessions\\readonly_refresh",
    date_family: str | None = None,
) -> dict[str, Any]:
    """Audit report-only rehearsal artifacts for path and date-family drift."""

    allowed_prefix = _norm_path(artifact_dir).rstrip("\\")
    issues: list[dict[str, Any]] = []
    artifact_summaries: list[dict[str, Any]] = []
    scanned_path_count = 0

    for artifact in artifacts:
        payload, load_issue = _load_json(artifact)
        summary = {
            "artifact": str(artifact),
            "exists": artifact.exists(),
            "schema": payload.get("schema") if isinstance(payload, dict) else None,
            "scanned_report_path_count": 0,
            "outside_artifact_dir_count": 0,
            "date_family_mismatch_count": 0,
        }
        if load_issue:
            issues.append(load_issue)
            artifact_summaries.append(summary)
            continue

        for json_path, value in _walk_strings(payload):
            for token in _path_tokens(value):
                scanned_path_count += 1
                summary["scanned_report_path_count"] += 1
                token_lower = token.lower()
                allowed_lower = allowed_prefix.lower()
                duplicate_dir = _artifact_dir_duplicate_path(token, allowed_prefix)
                if token_lower != allowed_lower and not token_lower.startswith(allowed_lower + "\\"):
                    summary["outside_artifact_dir_count"] += 1
                    issues.append(
                        {
                            "artifact": str(artifact),
                            "json_path": json_path,
                            "code": "report_path_outside_artifact_dir",
                            "path": token,
                            "allowed_prefix": allowed_prefix,
                        }
                    )
                if duplicate_dir:
                    issues.append(
                        {
                            "artifact": str(artifact),
                            "json_path": json_path,
                            "code": "report_path_nested_artifact_dir",
                            "path": token,
                            "allowed_prefix": allowed_prefix,
                        }
                    )
                if date_family:
                    dates = sorted(set(DATE_RE.findall(token)))
                    mismatched = [date for date in dates if date != date_family]
                    if mismatched:
                        summary["date_family_mismatch_count"] += 1
                        issues.append(
                            {
                                "artifact": str(artifact),
                                "json_path": json_path,
                                "code": "report_path_date_family_mismatch",
                                "path": token,
                                "expected_date_family": date_family,
                                "observed_dates": dates,
                            }
                        )
        artifact_summaries.append(summary)

    return {
        "schema": SCHEMA,
        "ok": not issues,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "human_decision_required": False,
        "artifact_dir": allowed_prefix,
        "date_family": date_family,
        "artifact_count": len(artifacts),
        "scanned_report_path_count": scanned_path_count,
        "issue_count": len(issues),
        "issues": issues,
        "artifacts": artifact_summaries,
    }


def _rewrite_report_path(path_text: str, *, artifact_dir: str, artifact_suffix: str | None) -> str:
    normalized = _norm_path(path_text)
    filename = normalized.rsplit("\\", 1)[-1]
    if artifact_suffix:
        filename = DATED_SUFFIX_RE.sub(artifact_suffix, filename)
    return _runbook_style_join(artifact_dir, filename)


def _runbook_style_join(base: str, filename: str) -> str:
    return _norm_path(base).rstrip("\\") + "\\" + filename


def _rewrite_report_paths_in_string(
    value: str,
    *,
    artifact_dir: str,
    artifact_suffix: str | None,
) -> tuple[str, int]:
    rewritten_count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal rewritten_count
        token = match.group(0)
        if "<" in token or ">" in token:
            return token
        if _norm_path(token).rstrip("\\").lower() == _norm_path(artifact_dir).rstrip("\\").lower():
            return token
        if _artifact_dir_duplicate_path(token, artifact_dir):
            rewritten_count += 1
            return _norm_path(artifact_dir).rstrip("\\")
        rewritten = _rewrite_report_path(
            token,
            artifact_dir=artifact_dir,
            artifact_suffix=artifact_suffix,
        )
        if _norm_path(rewritten).lower() == _norm_path(token).lower():
            return token
        rewritten_count += 1
        return rewritten

    return REPORT_PATH_RE.sub(replace, value), rewritten_count


def _rewrite_queue_value(
    value: Any,
    *,
    artifact_dir: str,
    artifact_suffix: str | None,
) -> tuple[Any, int]:
    if isinstance(value, str):
        return _rewrite_report_paths_in_string(
            value,
            artifact_dir=artifact_dir,
            artifact_suffix=artifact_suffix,
        )
    if isinstance(value, list):
        rewritten_items = []
        count = 0
        for item in value:
            rewritten, item_count = _rewrite_queue_value(
                item,
                artifact_dir=artifact_dir,
                artifact_suffix=artifact_suffix,
            )
            rewritten_items.append(rewritten)
            count += item_count
        return rewritten_items, count
    if isinstance(value, dict):
        rewritten_dict = {}
        count = 0
        for key, item in value.items():
            rewritten, item_count = _rewrite_queue_value(
                item,
                artifact_dir=artifact_dir,
                artifact_suffix=artifact_suffix,
            )
            rewritten_dict[key] = rewritten
            count += item_count
        return rewritten_dict, count
    return value, 0


def _rewrite_source_issues(source_queue: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for json_path, value in _walk_strings(source_queue):
        normalized_value = _norm_path(value)
        if "reports\\" in normalized_value:
            if "<" in normalized_value and ">" not in normalized_value:
                issues.append(
                    {
                        "json_path": json_path,
                        "code": "report_path_malformed_placeholder",
                        "value": value,
                    }
                )
            for placeholder in PLACEHOLDER_RE.findall(normalized_value):
                if placeholder not in ALLOWED_REWRITE_PLACEHOLDERS:
                    issues.append(
                        {
                            "json_path": json_path,
                            "code": "report_path_unknown_placeholder",
                            "placeholder": placeholder,
                            "value": value,
                        }
                    )
        for token in _path_tokens(value):
            if ".." in _norm_path(token).split("\\"):
                issues.append(
                    {
                        "json_path": json_path,
                        "code": "report_path_traversal",
                        "path": token,
                    }
                )
    return issues


def build_readonly_refresh_queue_copy(
    source_queue: dict[str, Any],
    *,
    source_queue_path: str,
    artifact_dir: str = "reports\\overnight_sessions\\readonly_refresh",
    artifact_suffix: str | None = None,
) -> dict[str, Any]:
    """Create a report-only queue copy whose report paths stay in artifact_dir."""

    source_issues = _rewrite_source_issues(source_queue)
    rewritten, rewritten_count = _rewrite_queue_value(
        source_queue,
        artifact_dir=artifact_dir,
        artifact_suffix=artifact_suffix,
    )
    if not isinstance(rewritten, dict):
        rewritten = {}
    metadata = {
        "ok": not source_issues,
        "source_queue_path": source_queue_path,
        "artifact_dir": _norm_path(artifact_dir).rstrip("\\"),
        "artifact_suffix": artifact_suffix,
        "rewritten_report_path_count": rewritten_count,
        "source_issue_count": len(source_issues),
        "source_issues": source_issues,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
        "note": "Queue copy for isolated readonly_refresh rehearsal; it does not execute items.",
    }
    rewritten["readonly_refresh_rewrite"] = metadata
    return rewritten


def write_readonly_refresh_path_contract_markdown(report: dict[str, Any], out_path: Path) -> None:
    lines = [
        "# Readonly Refresh Path Contract",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- report_only: `{str(report.get('report_only')).lower()}`",
        f"- status_update_allowed: `{str(report.get('status_update_allowed')).lower()}`",
        f"- db_writes: `{str(report.get('db_writes')).lower()}`",
        f"- approval_claim: `{str(report.get('approval_claim')).lower()}`",
        f"- artifact_dir: `{report.get('artifact_dir')}`",
        f"- date_family: `{report.get('date_family') or '-'}`",
        f"- scanned_report_path_count: {report.get('scanned_report_path_count')}",
        f"- issue_count: {report.get('issue_count')}",
        "",
        "## Artifacts",
        "",
        "| Artifact | Schema | Paths | Outside Dir | Date Mismatch |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in report.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "| {artifact} | {schema} | {paths} | {outside} | {date_mismatch} |".format(
                artifact=str(item.get("artifact") or "").replace("|", "\\|"),
                schema=str(item.get("schema") or "").replace("|", "\\|"),
                paths=item.get("scanned_report_path_count"),
                outside=item.get("outside_artifact_dir_count"),
                date_mismatch=item.get("date_family_mismatch_count"),
            )
        )
    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    if issues:
        lines.extend(["", "## Issues", "", "| Code | Artifact | JSON Path | Path |", "|---|---|---|---|"])
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            lines.append(
                "| {code} | {artifact} | {json_path} | `{path}` |".format(
                    code=str(issue.get("code") or "").replace("|", "\\|"),
                    artifact=str(issue.get("artifact") or "").replace("|", "\\|"),
                    json_path=str(issue.get("json_path") or "").replace("|", "\\|"),
                    path=str(issue.get("path") or "").replace("|", "\\|"),
                )
            )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
