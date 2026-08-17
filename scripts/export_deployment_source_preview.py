from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from build_deployment_source_manifest import build_manifest
    from check_deployment_source_boundary import ROOT, _normalize_path, tracked_path_reason
except ModuleNotFoundError:  # pragma: no cover - package-style test import
    from scripts.build_deployment_source_manifest import build_manifest
    from scripts.check_deployment_source_boundary import ROOT, _normalize_path, tracked_path_reason


def _is_relative_to(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _default_output_dir() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return ROOT / "tmp" / f"deployment_source_preview_{stamp}"


def _validate_output_dir(output_dir: Path) -> tuple[Path | None, str | None]:
    resolved = output_dir.resolve()
    tmp_root = (ROOT / "tmp").resolve()
    if not _is_relative_to(resolved, tmp_root):
        return None, "output_dir must be under tmp/ to avoid touching deployment branches"
    if resolved.exists():
        return None, "output_dir already exists; choose a fresh tmp/ path"
    return resolved, None


def _dedupe(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for path in paths:
        normalized = _normalize_path(path)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _path_has_parent_traversal(path: str) -> bool:
    parts = Path(path).parts
    return any(part == ".." for part in parts)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_source_file(source_path: Path, destination_path: Path) -> dict[str, Any]:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, destination_path)
    stat = destination_path.stat()
    return {
        "bytes": stat.st_size,
        "sha256": _sha256(destination_path),
    }


def export_preview(
    *,
    output_dir: Path | None = None,
    include_untracked_paths: list[str] | None = None,
) -> dict[str, Any]:
    manifest = build_manifest()
    target_dir = output_dir or _default_output_dir()
    resolved_target, output_error = _validate_output_dir(target_dir)

    tracked_paths = list(manifest.get("tracked_source_paths") or [])
    untracked_paths = list(manifest.get("untracked_source_candidates") or [])
    untracked_candidate_set = set(_dedupe(untracked_paths))
    requested_untracked_paths = _dedupe(include_untracked_paths or [])
    included_untracked_paths: list[str] = []

    copied_files: list[dict[str, Any]] = []
    skipped_files: list[dict[str, str]] = []
    copy_errors: list[dict[str, str]] = []
    copied_blockers: list[dict[str, str]] = []

    for path in requested_untracked_paths:
        if path not in untracked_candidate_set:
            copy_errors.append({"path": path, "reason": "path is not an untracked source candidate"})
        else:
            included_untracked_paths.append(path)

    selected_paths = _dedupe(tracked_paths + included_untracked_paths)

    if output_error is None and resolved_target is not None:
        resolved_target.mkdir(parents=True, exist_ok=False)
        for path in selected_paths:
            reason = tracked_path_reason(path)
            if reason:
                skipped_files.append({"path": path, "reason": reason})
                continue
            if _path_has_parent_traversal(path):
                skipped_files.append({"path": path, "reason": "path traversal is not allowed"})
                continue
            source_path = ROOT / path
            if not source_path.exists():
                skipped_files.append({"path": path, "reason": "source path missing"})
                continue
            if source_path.is_symlink():
                skipped_files.append({"path": path, "reason": "symlinks are not copied"})
                continue
            if not source_path.is_file():
                skipped_files.append({"path": path, "reason": "source path is not a file"})
                continue
            try:
                destination_path = resolved_target / path
                info = _copy_source_file(source_path, destination_path)
                copied_reason = tracked_path_reason(path)
                if copied_reason:
                    copied_blockers.append({"path": path, "reason": copied_reason})
                copied_files.append({"path": path, **info})
            except OSError as exc:
                copy_errors.append({"path": path, "reason": str(exc)})
    else:
        copy_errors.append({"path": str(target_dir), "reason": output_error or "invalid output_dir"})

    current_summary = manifest.get("summary") or {}
    copied_total_bytes = sum(int(item.get("bytes") or 0) for item in copied_files)
    ok = not (
        manifest.get("errors")
        or output_error
        or copy_errors
        or copied_blockers
        or not copied_files
    )
    return {
        "schema": "ncs_deployment_source_preview_export_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "policy": {
            "does_not_modify_git_index": True,
            "source_preview_only": True,
            "output_dir_must_be_under_tmp": True,
            "include_untracked_source_candidates_by_default": False,
            "explicit_untracked_paths": included_untracked_paths,
            "current_branch_blockers_are_excluded_from_export": True,
        },
        "output_dir": str(resolved_target or target_dir),
        "current_manifest": {
            "ok_for_preview_commit": bool(manifest.get("ok_for_preview_commit")),
            "tracked_source_count": int(current_summary.get("tracked_source_count") or 0),
            "tracked_blocker_count": int(current_summary.get("tracked_blocker_count") or 0),
            "untracked_source_candidate_count": int(
                current_summary.get("untracked_source_candidate_count") or 0
            ),
            "untracked_blocker_count": int(current_summary.get("untracked_blocker_count") or 0),
            "errors": manifest.get("errors") or [],
        },
        "summary": {
            "selected_path_count": len(selected_paths),
            "requested_untracked_path_count": len(requested_untracked_paths),
            "included_untracked_path_count": len(included_untracked_paths),
            "excluded_untracked_candidate_count": max(0, len(untracked_paths) - len(included_untracked_paths)),
            "copied_file_count": len(copied_files),
            "copied_total_bytes": copied_total_bytes,
            "skipped_file_count": len(skipped_files),
            "copy_error_count": len(copy_errors),
            "copied_blocker_count": len(copied_blockers),
        },
        "copied_files": copied_files,
        "skipped_files": skipped_files,
        "copy_errors": copy_errors,
        "copied_blockers": copied_blockers,
        "recommended_next_steps": [
            "Inspect the tmp source preview tree before publication.",
            "Use the preview tree or a clean deployment branch as the GitHub private preview source.",
            "Review untracked_source_candidates separately and include only intentional paths.",
            "Do not commit reports/, generated DB files, local config, tmp/, exports/, cache, or egg-info paths.",
            "Run source-boundary audit again on the clean branch before pushing.",
        ],
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Source Preview Export",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- output_dir: `{report.get('output_dir')}`",
        f"- current_branch_ok_for_preview_commit: `{str(report['current_manifest']['ok_for_preview_commit']).lower()}`",
        f"- current_tracked_blocker_count: `{report['current_manifest']['tracked_blocker_count']}`",
        f"- included_untracked_path_count: `{report['summary']['included_untracked_path_count']}`",
        f"- excluded_untracked_candidate_count: `{report['summary']['excluded_untracked_candidate_count']}`",
        f"- copied_file_count: `{report['summary']['copied_file_count']}`",
        f"- copied_total_bytes: `{report['summary']['copied_total_bytes']}`",
        f"- skipped_file_count: `{report['summary']['skipped_file_count']}`",
        f"- copy_error_count: `{report['summary']['copy_error_count']}`",
        f"- copied_blocker_count: `{report['summary']['copied_blocker_count']}`",
        "",
        "## Skipped Files",
        "",
    ]
    skipped = report.get("skipped_files") or []
    if skipped:
        lines.append("| Path | Reason |")
        lines.append("| --- | --- |")
        for item in skipped[:100]:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
        if len(skipped) > 100:
            lines.append(f"| ... | {len(skipped) - 100} more skipped files omitted |")
    else:
        lines.append("None.")

    lines.extend(["", "## Copy Errors", ""])
    errors = report.get("copy_errors") or []
    if errors:
        lines.append("| Path | Reason |")
        lines.append("| --- | --- |")
        for item in errors:
            lines.append(f"| `{item['path']}` | {item['reason']} |")
    else:
        lines.append("None.")

    lines.extend(["", "## Recommended Next Steps", ""])
    for step in report.get("recommended_next_steps") or []:
        lines.append(f"- {step}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a non-destructive source-only preview tree under tmp/."
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument(
        "--include-untracked",
        action="append",
        default=[],
        metavar="PATH",
        help="Explicit untracked source candidate to include. Repeat for each reviewed path.",
    )
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = export_preview(
        output_dir=args.output_dir,
        include_untracked_paths=args.include_untracked,
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
