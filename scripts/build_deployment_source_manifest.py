from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from check_deployment_source_boundary import ROOT, _normalize_path, tracked_path_reason
except ModuleNotFoundError:  # pragma: no cover - package-style test import
    from scripts.check_deployment_source_boundary import ROOT, _normalize_path, tracked_path_reason


def _run_git(args: list[str], *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _git_lines(args: list[str]) -> tuple[list[str], list[str]]:
    result = _run_git(args)
    if result.returncode != 0:
        return [], [result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"]
    return [_normalize_path(line) for line in result.stdout.splitlines() if line.strip()], []


def _source_path_allowed(path: str) -> bool:
    return tracked_path_reason(path) is None


def _path_sort_key(path: str) -> tuple[int, str]:
    order = [
        ".github/",
        "src/",
        "scripts/",
        "tests/",
        "docs/",
        "mcp/",
        ".agents/",
    ]
    for index, prefix in enumerate(order):
        if path.startswith(prefix):
            return index, path
    return len(order), path


def _source_summary(paths: list[str]) -> dict[str, int]:
    buckets = {
        "root": 0,
        "src": 0,
        "scripts": 0,
        "tests": 0,
        "docs": 0,
        "mcp": 0,
        "github": 0,
        "agents": 0,
        "other": 0,
    }
    for path in paths:
        if "/" not in path:
            buckets["root"] += 1
        elif path.startswith("src/"):
            buckets["src"] += 1
        elif path.startswith("scripts/"):
            buckets["scripts"] += 1
        elif path.startswith("tests/"):
            buckets["tests"] += 1
        elif path.startswith("docs/"):
            buckets["docs"] += 1
        elif path.startswith("mcp/"):
            buckets["mcp"] += 1
        elif path.startswith(".github/"):
            buckets["github"] += 1
        elif path.startswith(".agents/"):
            buckets["agents"] += 1
        else:
            buckets["other"] += 1
    return buckets


def build_manifest() -> dict[str, Any]:
    tracked_paths, tracked_errors = _git_lines(["ls-files"])
    untracked_paths, untracked_errors = _git_lines(["ls-files", "--others", "--exclude-standard"])

    tracked_source_paths = sorted(
        [path for path in tracked_paths if _source_path_allowed(path)],
        key=_path_sort_key,
    )
    tracked_blockers = sorted(
        [
            {"path": path, "reason": tracked_path_reason(path)}
            for path in tracked_paths
            if tracked_path_reason(path)
        ],
        key=lambda item: _path_sort_key(item["path"]),
    )
    untracked_source_candidates = sorted(
        [path for path in untracked_paths if _source_path_allowed(path)],
        key=_path_sort_key,
    )
    untracked_blockers = sorted(
        [
            {"path": path, "reason": tracked_path_reason(path)}
            for path in untracked_paths
            if tracked_path_reason(path)
        ],
        key=lambda item: _path_sort_key(item["path"]),
    )

    errors = tracked_errors + untracked_errors
    ok_for_preview_commit = not (errors or tracked_blockers or untracked_blockers)
    return {
        "schema": "ncs_deployment_source_manifest_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok_for_preview_commit": ok_for_preview_commit,
        "policy": {
            "does_not_stage_or_modify_git_index": True,
            "source_preview_only": True,
        },
        "errors": errors,
        "summary": {
            "tracked_source_count": len(tracked_source_paths),
            "tracked_blocker_count": len(tracked_blockers),
            "untracked_source_candidate_count": len(untracked_source_candidates),
            "untracked_blocker_count": len(untracked_blockers),
        },
        "tracked_source_summary": _source_summary(tracked_source_paths),
        "untracked_source_summary": _source_summary(untracked_source_candidates),
        "tracked_source_paths": tracked_source_paths,
        "tracked_blockers": tracked_blockers,
        "untracked_source_candidates": untracked_source_candidates,
        "untracked_blockers": untracked_blockers,
        "recommended_next_steps": [
            "Create a clean deployment branch.",
            "Remove tracked_blockers from the branch index without deleting local working data.",
            "Review and intentionally add only relevant untracked_source_candidates.",
            "Run check_deployment_source_boundary.py until ok=true before pushing.",
        ],
    }


def write_markdown(manifest: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Source Manifest",
        "",
        f"- ok_for_preview_commit: `{str(manifest.get('ok_for_preview_commit')).lower()}`",
        f"- tracked_source_count: `{manifest['summary']['tracked_source_count']}`",
        f"- tracked_blocker_count: `{manifest['summary']['tracked_blocker_count']}`",
        f"- untracked_source_candidate_count: `{manifest['summary']['untracked_source_candidate_count']}`",
        f"- untracked_blocker_count: `{manifest['summary']['untracked_blocker_count']}`",
        "",
        "## Tracked Blockers",
        "",
    ]
    blockers = manifest.get("tracked_blockers") or []
    if blockers:
        lines.append("| Path | Reason |")
        lines.append("| --- | --- |")
        for item in blockers[:200]:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
        if len(blockers) > 200:
            lines.append(f"| ... | {len(blockers) - 200} more tracked blockers omitted |")
    else:
        lines.append("None.")

    lines.extend(["", "## Untracked Source Candidates", ""])
    candidates = manifest.get("untracked_source_candidates") or []
    if candidates:
        lines.append("| Path |")
        lines.append("| --- |")
        for candidate in candidates[:200]:
            lines.append(f"| `{candidate}` |")
        if len(candidates) > 200:
            lines.append(f"| ... {len(candidates) - 200} more candidates omitted |")
    else:
        lines.append("None.")

    lines.extend(["", "## Recommended Next Steps", ""])
    for step in manifest.get("recommended_next_steps") or []:
        lines.append(f"- {step}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a source-only GitHub preview manifest.")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    manifest = build_manifest()
    payload = json.dumps(manifest, ensure_ascii=False, indent=2)
    print(payload)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(manifest, args.markdown_out)
    return 0 if manifest.get("ok_for_preview_commit") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
