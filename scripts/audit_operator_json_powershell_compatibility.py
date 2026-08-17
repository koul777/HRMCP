from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"
SCHEMA = "operator_json_powershell_compatibility_audit_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]


PowerShellRunner = Callable[[Path, str | None, int], dict[str, Any]]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_fragment(value: str | Path | None) -> str:
    return str(value or "").partition("#")[0].strip()


def portable_path(path: str | Path | None, *, root: Path = ROOT) -> str | None:
    text = strip_fragment(path)
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def resolve_artifact(value: str | Path | None, *, root: Path = ROOT) -> Path | None:
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


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig")), None
    except Exception as exc:
        return None, str(exc)


def root_type(payload: Any) -> str:
    if isinstance(payload, dict):
        return "object"
    if isinstance(payload, list):
        return "array"
    return type(payload).__name__


def dated_sort_key(path: Path) -> tuple[int, str, float]:
    match = re.search(r"(\d{8}(?:_\w+)?)", path.stem)
    stamp = match.group(1) if match else ""
    date = int(stamp[:8]) if stamp[:8].isdigit() else 0
    return date, stamp, path.stat().st_mtime


def latest_handoff(reports_dir: Path = REPORTS) -> Path:
    candidates = [
        path
        for path in reports_dir.glob("overnight_10h_operator_handoff_*.json")
        if path.is_file()
    ]
    if not candidates:
        raise FileNotFoundError("No overnight_10h_operator_handoff_*.json artifact found.")
    return max(candidates, key=dated_sort_key)


def powershell_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def tail(text: str, *, limit: int = 1200) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def run_powershell_convert_from_json(
    path: Path,
    powershell_exe: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    exe = powershell_exe or shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return {
            "available": False,
            "ok": False,
            "status": "not_checked",
            "error": "powershell_executable_not_found",
        }
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$path = {powershell_quote(path)}; "
        "$text = [System.IO.File]::ReadAllText($path, [System.Text.Encoding]::UTF8); "
        "$null = $text | ConvertFrom-Json; "
        "Write-Output 'OK'"
    )
    try:
        completed = subprocess.run(
            [exe, "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "available": True,
            "ok": False,
            "status": "timeout",
            "powershell_executable": exe,
            "timeout_seconds": timeout_seconds,
            "stdout_tail": tail(exc.stdout or ""),
            "stderr_tail": tail(exc.stderr or ""),
            "error": "powershell_convertfrom_json_timeout",
        }
    return {
        "available": True,
        "ok": completed.returncode == 0,
        "status": "pass" if completed.returncode == 0 else "parse_failed",
        "powershell_executable": exe,
        "exit_code": completed.returncode,
        "stdout_tail": tail(completed.stdout),
        "stderr_tail": tail(completed.stderr),
    }


def finding(
    *,
    rule_code: str,
    severity: str,
    message: str,
    path: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "code": rule_code,
        "rule_code": rule_code,
        "severity": severity,
        "message": message,
        "recommended_action": recommended_action(rule_code),
    }
    if path is not None:
        payload["path"] = path
    payload.update(extra)
    return payload


def recommended_action(rule_code: str) -> str:
    actions = {
        "missing_artifact": "Regenerate or point the audit to the expected operator JSON artifact.",
        "empty_artifact": "Regenerate the JSON artifact before handing it to an operator.",
        "python_json_parse_failed": "Fix the JSON export first; PowerShell compatibility is not meaningful until Python JSON parsing passes.",
        "json_root_not_object": "Confirm this artifact shape is intentional; operator reports should normally be JSON objects.",
        "powershell_unavailable": "Run this audit on an operator Windows host with powershell.exe or pwsh available.",
        "powershell_convertfrom_json_failed": "Inspect the stderr tail and simplify or re-export the JSON for PowerShell ConvertFrom-Json compatibility.",
    }
    return actions.get(rule_code, "Inspect the operator artifact before relying on it.")


def discover_json_artifacts_from_handoff(
    handoff_path: Path,
    *,
    root: Path = ROOT,
) -> list[Path]:
    payload, error = load_json(handoff_path)
    if error or not isinstance(payload, dict):
        return []
    paths: list[Path] = []
    for item in payload.get("canonical_artifacts") or []:
        if not isinstance(item, dict):
            continue
        candidate = strip_fragment(item.get("path"))
        if not candidate.lower().endswith(".json"):
            continue
        resolved = resolve_artifact(candidate, root=root)
        if resolved is not None:
            paths.append(resolved)
    return dedupe_paths(paths)


def dedupe_paths(paths: list[Path]) -> list[Path]:
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path.resolve(strict=False)).lower()
        if key in seen:
            continue
        ordered.append(path)
        seen.add(key)
    return ordered


def audit_json_artifact(
    path: Path,
    *,
    root: Path = ROOT,
    powershell_runner: PowerShellRunner = run_powershell_convert_from_json,
    powershell_exe: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    display_path = portable_path(path, root=root)
    item: dict[str, Any] = {
        "path": display_path,
        "exists": path.exists(),
        "non_empty": False,
        "sha256": sha256_file(path),
        "python_json_ok": False,
        "python_json_root_type": None,
        "powershell_convertfrom_json_ok": False,
        "powershell": {},
        "status": "review_required",
        "findings": [],
    }
    if not path.exists() or not path.is_file():
        item["findings"].append(
            finding(
                rule_code="missing_artifact",
                severity="high",
                message="Operator JSON artifact does not exist.",
                path=display_path,
            )
        )
        return item
    byte_count = path.stat().st_size
    item["byte_count"] = byte_count
    item["non_empty"] = byte_count > 0
    if byte_count == 0:
        item["findings"].append(
            finding(
                rule_code="empty_artifact",
                severity="high",
                message="Operator JSON artifact is empty.",
                path=display_path,
            )
        )
        return item

    payload, error = load_json(path)
    if error:
        item["python_json_error"] = error
        item["findings"].append(
            finding(
                rule_code="python_json_parse_failed",
                severity="high",
                message="Python json.loads could not parse the artifact.",
                path=display_path,
                error=error,
            )
        )
        return item
    item["python_json_ok"] = True
    item["python_json_root_type"] = root_type(payload)
    if not isinstance(payload, dict):
        item["findings"].append(
            finding(
                rule_code="json_root_not_object",
                severity="medium",
                message="JSON root is not an object.",
                path=display_path,
                root_type=item["python_json_root_type"],
            )
        )

    powershell = powershell_runner(path, powershell_exe, timeout_seconds)
    item["powershell"] = powershell
    item["powershell_convertfrom_json_ok"] = powershell.get("ok") is True
    if powershell.get("available") is False:
        item["findings"].append(
            finding(
                rule_code="powershell_unavailable",
                severity="medium",
                message="No PowerShell executable was available for ConvertFrom-Json validation.",
                path=display_path,
            )
        )
    elif powershell.get("ok") is not True:
        item["findings"].append(
            finding(
                rule_code="powershell_convertfrom_json_failed",
                severity="high",
                message="PowerShell ConvertFrom-Json could not parse the Python-valid JSON artifact.",
                path=display_path,
                status=powershell.get("status"),
                stderr_tail=powershell.get("stderr_tail"),
            )
        )
    if not item["findings"]:
        item["status"] = "pass"
    return item


def build_audit(
    *,
    artifacts: list[Path],
    handoff_path: Path | None = None,
    root: Path = ROOT,
    powershell_runner: PowerShellRunner = run_powershell_convert_from_json,
    powershell_exe: str | None = None,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    paths = dedupe_paths(artifacts)
    items = [
        audit_json_artifact(
            path,
            root=root,
            powershell_runner=powershell_runner,
            powershell_exe=powershell_exe,
            timeout_seconds=timeout_seconds,
        )
        for path in paths
    ]
    findings = [
        finding_payload
        for item in items
        for finding_payload in item.get("findings", [])
    ]
    python_ok_count = sum(1 for item in items if item.get("python_json_ok") is True)
    powershell_ok_count = sum(
        1 for item in items if item.get("powershell_convertfrom_json_ok") is True
    )
    python_ok_powershell_failed_count = sum(
        1
        for item in items
        if item.get("python_json_ok") is True
        and item.get("powershell_convertfrom_json_ok") is not True
    )
    no_artifact_findings = (
        []
        if items
        else [
            finding(
                rule_code="missing_artifact",
                severity="medium",
                message="No JSON artifacts were supplied or discovered.",
            )
        ]
    )
    all_findings = findings or no_artifact_findings
    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "ok": not findings and bool(items),
        "status": "pass" if not findings and items else "review_required",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "source_handoff_path": portable_path(handoff_path, root=root) if handoff_path else None,
        "source_handoff_sha256": sha256_file(handoff_path) if handoff_path else None,
        "artifact_count": len(items),
        "finding_count": len(all_findings),
        "python_json_ok_count": python_ok_count,
        "powershell_convertfrom_json_ok_count": powershell_ok_count,
        "python_ok_powershell_failed_count": python_ok_powershell_failed_count,
        "findings": all_findings,
        "artifacts": items,
        "notes": [
            "Report-only parser compatibility audit for operator JSON artifacts.",
            "A pass means Python json.loads and PowerShell ConvertFrom-Json both parsed the JSON files.",
            "This audit is not a human approval signal and must not set human_reviewed, accepted, or reviewed.",
            "PowerShell parser failures are kept separate from Python JSON validity so export issues can be triaged precisely.",
        ],
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Operator JSON PowerShell Compatibility Audit",
        "",
        f"- schema: `{payload['schema']}`",
        f"- status: `{payload['status']}`",
        f"- ok: `{str(payload['ok']).lower()}`",
        f"- artifact_count: {payload['artifact_count']}",
        f"- finding_count: {payload['finding_count']}",
        f"- python_json_ok_count: {payload['python_json_ok_count']}",
        f"- powershell_convertfrom_json_ok_count: {payload['powershell_convertfrom_json_ok_count']}",
        f"- python_ok_powershell_failed_count: {payload['python_ok_powershell_failed_count']}",
        "- report_only: true",
        "- status_update_allowed: false",
        "- db_writes: false",
        "- approval_claim: false",
        "- human_decision_required: true",
        "",
        "## Notes",
        "",
    ]
    lines.extend(f"- {note}" for note in payload["notes"])
    lines.extend(["", "## Artifacts", ""])
    for item in payload.get("artifacts") or []:
        lines.append(f"### {item['path']}")
        lines.append("")
        lines.append(f"- status: `{item['status']}`")
        lines.append(f"- python_json_ok: `{str(item['python_json_ok']).lower()}`")
        lines.append(
            "- powershell_convertfrom_json_ok: "
            f"`{str(item['powershell_convertfrom_json_ok']).lower()}`"
        )
        lines.append(f"- python_json_root_type: `{item.get('python_json_root_type')}`")
        lines.append(f"- sha256: `{item.get('sha256')}`")
        if item.get("findings"):
            lines.append("- findings:")
            for entry in item["findings"]:
                lines.append(f"  - `{entry['rule_code']}` ({entry['severity']}): {entry['message']}")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit operator JSON artifacts with Python json and PowerShell ConvertFrom-Json."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--operator-handoff", type=Path)
    parser.add_argument("--artifact", action="append", type=Path, default=[])
    parser.add_argument("--powershell-exe")
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    *,
    powershell_runner: PowerShellRunner = run_powershell_convert_from_json,
) -> int:
    args = parse_args(argv)
    root = args.root
    handoff_path = resolve_artifact(args.operator_handoff, root=root) if args.operator_handoff else latest_handoff(root / "reports")
    artifacts = discover_json_artifacts_from_handoff(handoff_path, root=root)
    artifacts.extend(resolve_artifact(path, root=root) or path for path in args.artifact)
    report = build_audit(
        artifacts=artifacts,
        handoff_path=handoff_path,
        root=root,
        powershell_runner=powershell_runner,
        powershell_exe=args.powershell_exe,
        timeout_seconds=args.timeout_seconds,
    )
    write_json(args.out, report)
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    result = {
        "ok": report.get("ok"),
        "schema": report.get("schema"),
        "artifact_count": report.get("artifact_count"),
        "finding_count": report.get("finding_count"),
        "python_ok_powershell_failed_count": report.get("python_ok_powershell_failed_count"),
        "out_path": str(args.out),
        "markdown_path": str(args.markdown_out) if args.markdown_out else None,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if args.strict and report.get("ok") is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())
