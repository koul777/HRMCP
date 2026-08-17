from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

ALLOWED_TRACKED_PATHS = {
    ".env.example",
    "data/raw/.gitkeep",
    "data/processed/.gitkeep",
    "reports/.gitkeep",
}

PROHIBITED_TRACKED_EXACT = {
    ".env": "local secret configuration",
    ".mcp.json": "local MCP configuration",
    "docs/reference/ncs_hrd_guide_codex_readable.md": "converted HRD guide reference artifact",
}

PROHIBITED_TRACKED_PREFIXES = {
    ".codex/": "local Codex configuration",
    ".venv/": "virtual environment",
    "venv/": "virtual environment",
    "__pycache__/": "python bytecode cache",
    ".pytest_cache/": "test cache",
    ".ruff_cache/": "tool cache",
    ".mypy_cache/": "tool cache",
    "logs/": "log artifact",
    "data/raw/": "raw source download",
    "data/ocr/tessdata/": "OCR model artifact",
    "data/processed/": "generated SQLite or data artifact",
    "docs/reference/ncs_hrd_guide_reference.": "generated HRD guide reference artifact",
    "reports/": "generated report or copied reference artifact",
    "exports/": "generated export artifact",
    "tmp/": "temporary working artifact",
    "build/": "generated Python build artifact",
    "dist/": "generated Python distribution artifact",
    "ncs_mcp.egg-info/": "generated Python package metadata",
    "src/ncs_mcp.egg-info/": "generated Python package metadata",
}

PROHIBITED_TRACKED_SUFFIXES = {
    ".pyc": "python bytecode cache",
    ".pyo": "python bytecode cache",
    ".log": "log artifact",
}

IGNORE_EXPECTATIONS = [
    ("data/raw/source.xlsx", True),
    ("data/processed/ncs.db", True),
    ("data/processed/ncs.db-wal", True),
    ("reports/release_readiness.json", True),
    ("tmp/scratch.txt", True),
    ("exports/ncs_hr_ontology.jsonld", True),
    ("ci-smoke/ncs.db", True),
    ("src/ncs_mcp.egg-info/PKG-INFO", True),
    ("build/lib/ncs_mcp/server.py", True),
    ("dist/ncs_mcp-0.1.0.whl", True),
    ("ncs_mcp.egg-info/PKG-INFO", True),
    (".codex/config.toml", True),
    (".mcp.json", True),
    ("data/ocr/tessdata/kor.traineddata", True),
    ("docs/reference/ncs_hrd_guide_codex_readable.md", True),
    ("docs/reference/ncs_hrd_guide_reference.index.json", True),
    ("docs/reference/ncs_hrd_guide_reference.chunks.jsonl", True),
    (".env", True),
    (".env.example", False),
    ("data/raw/.gitkeep", False),
    ("data/processed/.gitkeep", False),
    ("reports/.gitkeep", False),
]

ATTRIBUTE_EXPECTATIONS = [
    ("data/raw/source.xlsx", "lfs"),
    ("data/raw/source.xls", "lfs"),
    ("data/processed/ncs.db", "lfs"),
    ("data/processed/ncs.db-wal", "lfs"),
    ("data/ocr/tessdata/kor.traineddata", "lfs"),
]


def _normalize_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    while normalized.startswith("/"):
        normalized = normalized[1:]
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _run_git(args: list[str], *, cwd: Path = ROOT, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def tracked_path_reason(path: str) -> str | None:
    normalized = _normalize_path(path)
    if normalized in ALLOWED_TRACKED_PATHS:
        return None
    if normalized in PROHIBITED_TRACKED_EXACT:
        return PROHIBITED_TRACKED_EXACT[normalized]
    for prefix, reason in PROHIBITED_TRACKED_PREFIXES.items():
        if normalized.startswith(prefix):
            return reason
    for suffix, reason in PROHIBITED_TRACKED_SUFFIXES.items():
        if normalized.endswith(suffix):
            return reason
    return None


def find_tracked_blockers(paths: list[str]) -> list[dict[str, str]]:
    blockers: list[dict[str, str]] = []
    seen: set[str] = set()
    for path in paths:
        normalized = _normalize_path(path)
        if normalized in seen:
            continue
        reason = tracked_path_reason(normalized)
        if reason:
            blockers.append({"path": normalized, "reason": reason})
            seen.add(normalized)
    return blockers


def list_tracked_paths() -> tuple[list[str], list[str]]:
    result = _run_git(["ls-files"])
    if result.returncode != 0:
        return [], [result.stderr.strip() or "git ls-files failed"]
    return [
        _normalize_path(line)
        for line in result.stdout.splitlines()
        if line.strip()
    ], []


def check_ignore_expectations() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path, expected_ignored in IGNORE_EXPECTATIONS:
        result = _run_git(["check-ignore", "--no-index", "-q", path])
        ignored = result.returncode == 0
        checks.append(
            {
                "path": path,
                "expected_ignored": expected_ignored,
                "ignored": ignored,
                "ok": ignored == expected_ignored,
            }
        )
    return checks


def _parse_check_attr_filter(output: str) -> str | None:
    # Example: "data/processed/ncs.db: filter: lfs"
    for line in output.splitlines():
        parts = line.split(": ", 2)
        if len(parts) == 3 and parts[1] == "filter":
            value = parts[2].strip()
            return None if value in {"unspecified", "unset"} else value
    return None


def check_attribute_expectations() -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path, expected_filter in ATTRIBUTE_EXPECTATIONS:
        result = _run_git(["check-attr", "filter", "--", path])
        actual_filter = _parse_check_attr_filter(result.stdout)
        checks.append(
            {
                "path": path,
                "expected_filter": expected_filter,
                "filter": actual_filter,
                "ok": actual_filter == expected_filter,
            }
        )
    return checks


def list_lfs_history_paths() -> tuple[list[str], list[str]]:
    result = _run_git(["lfs", "ls-files", "--all", "--name-only"], timeout=60)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip() or "git lfs ls-files failed"
        return [], [message]
    return [
        _normalize_path(line)
        for line in result.stdout.splitlines()
        if line.strip()
    ], []


def build_report(*, check_lfs_history: bool, fail_on_lfs_history: bool) -> dict[str, Any]:
    tracked_paths, git_errors = list_tracked_paths()
    tracked_blockers = find_tracked_blockers(tracked_paths)
    ignore_checks = check_ignore_expectations()
    attribute_checks = check_attribute_expectations()

    lfs_history_paths: list[str] = []
    lfs_history_errors: list[str] = []
    lfs_history_blockers: list[dict[str, str]] = []
    if check_lfs_history:
        lfs_history_paths, lfs_history_errors = list_lfs_history_paths()
        lfs_history_blockers = find_tracked_blockers(lfs_history_paths)

    ignore_failures = [item for item in ignore_checks if not item["ok"]]
    attribute_failures = [item for item in attribute_checks if not item["ok"]]
    blocking_lfs_history = lfs_history_blockers if fail_on_lfs_history else []
    ok = not (
        git_errors
        or tracked_blockers
        or ignore_failures
        or attribute_failures
        or lfs_history_errors
        or blocking_lfs_history
    )
    return {
        "schema": "ncs_deployment_source_boundary_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "policy": {
            "source_preview_only": True,
            "fail_on_lfs_history": fail_on_lfs_history,
            "check_lfs_history": check_lfs_history,
        },
        "git_errors": git_errors,
        "tracked_blockers": tracked_blockers,
        "ignore_checks": ignore_checks,
        "ignore_failures": ignore_failures,
        "attribute_checks": attribute_checks,
        "attribute_failures": attribute_failures,
        "lfs_history_errors": lfs_history_errors,
        "lfs_history_blockers": lfs_history_blockers,
        "summary": {
            "tracked_blocker_count": len(tracked_blockers),
            "ignore_failure_count": len(ignore_failures),
            "attribute_failure_count": len(attribute_failures),
            "lfs_history_blocker_count": len(lfs_history_blockers),
        },
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Source Boundary Audit",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- tracked_blocker_count: `{report['summary']['tracked_blocker_count']}`",
        f"- ignore_failure_count: `{report['summary']['ignore_failure_count']}`",
        f"- attribute_failure_count: `{report['summary']['attribute_failure_count']}`",
        f"- lfs_history_blocker_count: `{report['summary']['lfs_history_blocker_count']}`",
        "",
        "## Tracked Blockers",
        "",
    ]
    blockers = report.get("tracked_blockers") or []
    if blockers:
        lines.append("| Path | Reason |")
        lines.append("| --- | --- |")
        for item in blockers:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
    else:
        lines.append("None.")

    lines.extend(["", "## Ignore Failures", ""])
    ignore_failures = report.get("ignore_failures") or []
    if ignore_failures:
        lines.append("| Path | Expected Ignored | Actual Ignored |")
        lines.append("| --- | --- | --- |")
        for item in ignore_failures:
            lines.append(
                f"| `{item['path']}` | `{item['expected_ignored']}` | `{item['ignored']}` |"
            )
    else:
        lines.append("None.")

    lines.extend(["", "## Attribute Failures", ""])
    attribute_failures = report.get("attribute_failures") or []
    if attribute_failures:
        lines.append("| Path | Expected Filter | Actual Filter |")
        lines.append("| --- | --- | --- |")
        for item in attribute_failures:
            lines.append(
                f"| `{item['path']}` | `{item['expected_filter']}` | `{item.get('filter')}` |"
            )
    else:
        lines.append("None.")

    lines.extend(["", "## LFS History Blockers", ""])
    lfs_blockers = report.get("lfs_history_blockers") or []
    if lfs_blockers:
        lines.append("| Path | Reason |")
        lines.append("| --- | --- |")
        for item in lfs_blockers:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
    else:
        lines.append("None.")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit that a GitHub preview branch contains source material only."
    )
    parser.add_argument("--json-out", type=Path, help="Optional JSON report path.")
    parser.add_argument("--markdown-out", type=Path, help="Optional Markdown report path.")
    parser.add_argument(
        "--check-lfs-history",
        action="store_true",
        help="Inspect git-lfs history for source-boundary artifacts.",
    )
    parser.add_argument(
        "--fail-on-lfs-history",
        action="store_true",
        help="Treat restricted git-lfs history objects as blockers.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = build_report(
        check_lfs_history=args.check_lfs_history or args.fail_on_lfs_history,
        fail_on_lfs_history=args.fail_on_lfs_history,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
