from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


PYTHON_CACHE_DIR_NAMES = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")
SQLITE_TRANSIENT_SUFFIXES = ("-wal", "-shm", "-journal")
HEAVY_LFS_STATUS_EXCLUDES = (
    "data/processed/*.db",
    "data/processed/*.db-*",
    "data/raw/*.xlsx",
    "data/raw/*.xls",
    "data/ocr/tessdata/*.traineddata",
)
SKIP_SCAN_DIR_NAMES = {".git", ".venv", "venv", "node_modules"}
SKIP_LARGE_FILE_DIR_NAMES = {".venv", "venv", "node_modules"}
DEFAULT_LARGE_FILE_THRESHOLD_MB = 512
DEFAULT_LARGE_FILE_LIMIT = 25
SIZE_UNITS = {
    "B": 1,
    "KB": 1024,
    "MB": 1024**2,
    "GB": 1024**3,
    "TB": 1024**4,
}


def build_workspace_hygiene_report(
    workspace: Path | str,
    *,
    apply: bool = False,
    include_git_status: bool = True,
    include_lfs_prune: bool = True,
    clean_lfs_tmp: bool = True,
    clean_python_caches: bool = True,
    clean_sqlite_orphans: bool = True,
    large_file_threshold_mb: int = DEFAULT_LARGE_FILE_THRESHOLD_MB,
    large_file_limit: int = DEFAULT_LARGE_FILE_LIMIT,
) -> dict[str, Any]:
    root = Path(workspace).resolve()
    report: dict[str, Any] = {
        "schema": "ncs_workspace_hygiene_v1",
        "workspace": str(root),
        "apply": bool(apply),
        "safe_git_status_command": safe_git_status_command(),
        "sizes": {},
        "large_files": [],
        "cleanups": [],
        "warnings": [],
        "errors": [],
    }
    report["safe_git_status"] = (
        _run_safe_git_status(root)
        if include_git_status and _looks_like_git_worktree(root)
        else {
            "skipped": True,
            "reason": "disabled" if not include_git_status else "not_git_worktree",
        }
    )
    for relative in (".git/lfs", ".git/lfs/tmp", "data", "reports", "logs"):
        path = root / relative
        if path.exists():
            report["sizes"][relative] = _path_summary(path)
    report["large_files"] = _large_file_report(
        root,
        threshold_bytes=max(0, int(large_file_threshold_mb)) * 1024 * 1024,
        limit=max(1, int(large_file_limit)),
    )
    if include_lfs_prune:
        report["git_lfs_prune"] = _run_git_lfs_prune(root, dry_run=not apply)
    else:
        report["git_lfs_prune"] = {"skipped": True}
    if clean_lfs_tmp:
        report["cleanups"].append(_cleanup_lfs_tmp(root, apply=apply))
    if clean_python_caches:
        report["cleanups"].append(_cleanup_python_caches(root, apply=apply))
    if clean_sqlite_orphans:
        report["cleanups"].append(_cleanup_sqlite_transients(root, apply=apply))
    for cleanup in report["cleanups"]:
        report["errors"].extend(cleanup.get("errors") or [])
    prune_result = report.get("git_lfs_prune") or {}
    if prune_result.get("error") or prune_result.get("ok") is False:
        report["errors"].append(
            {
                "target": "git lfs prune",
                "error": prune_result.get("error") or prune_result.get("stderr") or "git lfs prune failed",
            }
        )
    status_result = report.get("safe_git_status") or {}
    if status_result.get("error") or status_result.get("ok") is False:
        report["errors"].append(
            {
                "target": "safe git status",
                "error": status_result.get("error") or status_result.get("stderr") or "safe git status failed",
            }
        )
    prunable_bytes = int(prune_result.get("prunable_size_bytes") or 0)
    if prunable_bytes and not apply:
        report["warnings"].append(
            {
                "target": "git lfs prune",
                "message": "Regenerable Git LFS local objects can be pruned with workspace-hygiene --apply.",
                "prunable_size_bytes": prunable_bytes,
                "prunable_size_mb": round(prunable_bytes / (1024 * 1024), 2),
                "prunable_size_gb": round(prunable_bytes / (1024 * 1024 * 1024), 2),
            }
        )
    for cleanup in report["cleanups"]:
        if cleanup.get("name") == "sqlite_transients" and int(cleanup.get("report_only_size_bytes") or 0):
            report["warnings"].append(
                {
                    "target": "sqlite_transients",
                    "message": "SQLite sidecar files for existing DBs are reported only; stop DB users and checkpoint before manual cleanup.",
                    "report_only_size_bytes": cleanup.get("report_only_size_bytes"),
                    "report_only_size_mb": cleanup.get("report_only_size_mb"),
                }
            )
    report["ok"] = not report["errors"]
    return report


def write_workspace_hygiene_markdown(report: dict[str, Any], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Workspace Hygiene",
        "",
        f"- ok: {str(bool(report.get('ok'))).lower()}",
        f"- apply: {str(bool(report.get('apply'))).lower()}",
        f"- workspace: `{report.get('workspace')}`",
        f"- errors: {len(report.get('errors') or [])}",
        f"- warnings: {len(report.get('warnings') or [])}",
    ]

    prune = report.get("git_lfs_prune")
    if isinstance(prune, dict) and not prune.get("skipped"):
        lines.extend(
            [
                "",
                "## Git LFS Prune",
                "",
                f"- dry_run: {str(bool(prune.get('dry_run'))).lower()}",
                f"- ok: {str(bool(prune.get('ok'))).lower()}",
                f"- prunable_file_count: {prune.get('prunable_file_count', 0)}",
                f"- prunable_size: {prune.get('prunable_size_text') or '0 B'}",
            ]
        )

    sizes = report.get("sizes")
    if isinstance(sizes, dict) and sizes:
        lines.extend(["", "## Sizes", "", "| Path | Size GB | Files |", "| --- | ---: | ---: |"])
        for path, summary in sizes.items():
            if not isinstance(summary, dict):
                continue
            lines.append(
                "| "
                + str(path)
                + " | "
                + str(summary.get("logical_size_gb", 0))
                + " | "
                + str(summary.get("file_count", ""))
                + " |"
            )

    cleanups = report.get("cleanups")
    if isinstance(cleanups, list) and cleanups:
        lines.extend(["", "## Cleanups", "", "| Name | Count | Size MB | Removed | Errors |", "| --- | ---: | ---: | ---: | ---: |"])
        for cleanup in cleanups:
            if not isinstance(cleanup, dict):
                continue
            count = cleanup.get("count")
            if count is None and cleanup.get("exists_before"):
                count = 1
            lines.append(
                "| "
                + str(cleanup.get("name"))
                + " | "
                + str(count or 0)
                + " | "
                + str(cleanup.get("logical_size_mb", 0))
                + " | "
                + str(bool(cleanup.get("removed"))).lower()
                + " | "
                + str(len(cleanup.get("errors") or []))
                + " |"
            )

    large_files = report.get("large_files")
    if isinstance(large_files, list) and large_files:
        lines.extend(["", "## Large Files", "", "| Path | Size GB | Category |", "| --- | ---: | --- |"])
        for item in large_files[:10]:
            if not isinstance(item, dict):
                continue
            lines.append(
                "| "
                + str(item.get("path"))
                + " | "
                + str(item.get("logical_size_gb", 0))
                + " | "
                + str(item.get("category"))
                + " |"
            )

    warnings = report.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines.extend(["", "## Warnings", ""])
        for warning in warnings:
            if isinstance(warning, dict):
                lines.append("- " + str(warning.get("target")) + ": " + str(warning.get("message")))

    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        lines.extend(["", "## Errors", ""])
        for error in errors:
            if isinstance(error, dict):
                lines.append("- " + str(error.get("target")) + ": " + str(error.get("error")))

    out_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def safe_git_status_command() -> list[str]:
    command = [
        "git",
        "-c",
        "filter.lfs.clean=cat",
        "-c",
        "filter.lfs.smudge=cat",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.required=false",
        "status",
        "--short",
        "--untracked-files=no",
        "--",
        ".",
    ]
    command.extend(f":(exclude){pattern}" for pattern in HEAVY_LFS_STATUS_EXCLUDES)
    return command


def _looks_like_git_worktree(root: Path) -> bool:
    git_path = root / ".git"
    if git_path.is_dir():
        return (git_path / "HEAD").exists()
    return git_path.is_file()


def _run_safe_git_status(root: Path) -> dict[str, Any]:
    command = safe_git_status_command()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "ok": False,
            "error": str(exc),
        }
    lines = completed.stdout.splitlines()
    tail_limit = 80
    return {
        "command": command,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "line_count": len(lines),
        "stdout_tail": lines[-tail_limit:],
        "stdout_truncated": len(lines) > tail_limit,
        "stderr": completed.stderr.strip(),
    }


def _run_git_lfs_prune(root: Path, *, dry_run: bool) -> dict[str, Any]:
    command = ["git", "lfs", "prune"]
    if dry_run:
        command.append("--dry-run")
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "command": command,
            "dry_run": dry_run,
            "ok": False,
            "error": str(exc),
        }
    parsed = _parse_git_lfs_prune_output(completed.stdout)
    return {
        "command": command,
        "dry_run": dry_run,
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        **parsed,
    }


def _parse_git_lfs_prune_output(stdout: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    local_match = re.search(r"(\d+)\s+local objects?,\s+(\d+)\s+retained", stdout)
    if local_match:
        result["local_object_count"] = int(local_match.group(1))
        result["retained_object_count"] = int(local_match.group(2))
    prune_match = re.search(
        r"(\d+)\s+files?\s+(?:would be\s+)?pruned\s+\(([^)]+)\)",
        stdout,
        flags=re.IGNORECASE,
    )
    if prune_match:
        prunable_size = _parse_human_size(prune_match.group(2))
        result["prunable_file_count"] = int(prune_match.group(1))
        result["prunable_size_text"] = prune_match.group(2)
        result["prunable_size_bytes"] = prunable_size
        result["prunable_size_mb"] = round(prunable_size / (1024 * 1024), 2)
        result["prunable_size_gb"] = round(prunable_size / (1024 * 1024 * 1024), 2)
    return result


def _parse_human_size(value: str) -> int:
    match = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)\s*\Z", value, flags=re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).upper()
    return int(number * SIZE_UNITS.get(unit, 1))


def _cleanup_lfs_tmp(root: Path, *, apply: bool) -> dict[str, Any]:
    path = root / ".git" / "lfs" / "tmp"
    summary: dict[str, Any] = {
        "name": "lfs_tmp",
        "path": str(path),
        "exists_before": path.exists(),
        "apply": bool(apply),
        "removed": False,
        "errors": [],
    }
    if not path.exists():
        return summary
    summary.update(_path_summary(path))
    if not apply:
        return summary
    try:
        _assert_safe_cleanup_path(root, path, allowed_leaf="tmp")
        shutil.rmtree(path)
        summary["removed"] = True
    except (OSError, ValueError) as exc:
        summary["errors"].append({"target": str(path), "error": str(exc)})
    summary["exists_after"] = path.exists()
    return summary


def _cleanup_python_caches(root: Path, *, apply: bool) -> dict[str, Any]:
    cache_dirs = list(_iter_python_cache_dirs(root))
    total_bytes = 0
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in cache_dirs:
        path_summary = _path_summary(path)
        total_bytes += int(path_summary["logical_size_bytes"])
        item = {
            "path": str(path.relative_to(root)),
            "logical_size_bytes": path_summary["logical_size_bytes"],
            "logical_size_mb": path_summary["logical_size_mb"],
            "removed": False,
        }
        if apply:
            try:
                _assert_safe_cleanup_path(root, path, allowed_leaf=path.name)
                shutil.rmtree(path)
                item["removed"] = True
            except (OSError, ValueError) as exc:
                errors.append({"target": str(path), "error": str(exc)})
        items.append(item)
    return {
        "name": "python_caches",
        "apply": bool(apply),
        "count": len(cache_dirs),
        "logical_size_bytes": total_bytes,
        "logical_size_mb": round(total_bytes / (1024 * 1024), 2),
        "items": items,
        "errors": errors,
    }


def _cleanup_sqlite_transients(root: Path, *, apply: bool) -> dict[str, Any]:
    files = list(_iter_sqlite_transient_files(root))
    total_bytes = 0
    removable_bytes = 0
    report_only_bytes = 0
    items: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in files:
        path_summary = _path_summary(path)
        size = int(path_summary["logical_size_bytes"])
        base_db = _sqlite_base_for_sidecar(path)
        safe_to_remove = base_db is not None and not base_db.exists()
        reason = "orphaned_sqlite_sidecar" if safe_to_remove else "base_db_exists_report_only"
        total_bytes += size
        if safe_to_remove:
            removable_bytes += size
        else:
            report_only_bytes += size
        item = {
            "path": str(path.relative_to(root)),
            "base_db": str(base_db.relative_to(root)) if base_db and _is_under(root, base_db) else None,
            "logical_size_bytes": size,
            "logical_size_mb": path_summary["logical_size_mb"],
            "safe_to_remove": safe_to_remove,
            "reason": reason,
            "removed": False,
        }
        if apply and safe_to_remove:
            try:
                _assert_safe_cleanup_path(root, path, allowed_leaf=path.name)
                path.unlink()
                item["removed"] = True
            except (OSError, ValueError) as exc:
                errors.append({"target": str(path), "error": str(exc)})
        items.append(item)
    return {
        "name": "sqlite_transients",
        "apply": bool(apply),
        "count": len(files),
        "logical_size_bytes": total_bytes,
        "logical_size_mb": round(total_bytes / (1024 * 1024), 2),
        "removable_size_bytes": removable_bytes,
        "removable_size_mb": round(removable_bytes / (1024 * 1024), 2),
        "report_only_size_bytes": report_only_bytes,
        "report_only_size_mb": round(report_only_bytes / (1024 * 1024), 2),
        "items": items,
        "errors": errors,
    }


def _iter_sqlite_transient_files(root: Path):
    data_dir = root / "data" / "processed"
    if not data_dir.exists():
        return
    for path in data_dir.iterdir():
        if path.is_file() and path.name.endswith(SQLITE_TRANSIENT_SUFFIXES):
            yield path


def _sqlite_base_for_sidecar(path: Path) -> Path | None:
    for suffix in SQLITE_TRANSIENT_SUFFIXES:
        if path.name.endswith(suffix):
            return path.with_name(path.name[: -len(suffix)])
    return None


def _iter_python_cache_dirs(root: Path):
    for current, dirs, _files in os.walk(root):
        current_path = Path(current)
        kept_dirs = []
        cache_dirs = []
        for name in dirs:
            child = current_path / name
            if name in SKIP_SCAN_DIR_NAMES or _is_under(root / ".git", child):
                continue
            if name in PYTHON_CACHE_DIR_NAMES:
                cache_dirs.append(name)
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for dirname in cache_dirs:
            if dirname in PYTHON_CACHE_DIR_NAMES:
                yield current_path / dirname


def _path_summary(path: Path) -> dict[str, Any]:
    if path.is_file():
        size = path.stat().st_size
        return {
            "logical_size_bytes": size,
            "logical_size_mb": round(size / (1024 * 1024), 2),
            "logical_size_gb": round(size / (1024 * 1024 * 1024), 2),
        }
    total = 0
    file_count = 0
    dir_count = 0
    for current, dirs, files in os.walk(path, onerror=lambda _exc: None):
        dir_count += len(dirs)
        for filename in files:
            file_path = Path(current) / filename
            try:
                total += file_path.stat().st_size
                file_count += 1
            except OSError:
                continue
    return {
        "logical_size_bytes": total,
        "logical_size_mb": round(total / (1024 * 1024), 2),
        "logical_size_gb": round(total / (1024 * 1024 * 1024), 2),
        "file_count": file_count,
        "dir_count": dir_count,
    }


def _large_file_report(root: Path, *, threshold_bytes: int, limit: int) -> list[dict[str, Any]]:
    files: list[tuple[int, Path]] = []
    for current, dirs, filenames in os.walk(root, onerror=lambda _exc: None):
        dirs[:] = [name for name in dirs if name not in SKIP_LARGE_FILE_DIR_NAMES]
        current_path = Path(current)
        for filename in filenames:
            path = current_path / filename
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size >= threshold_bytes:
                files.append((size, path))
    files.sort(reverse=True, key=lambda item: item[0])
    return [
        {
            "path": str(path.relative_to(root)),
            "logical_size_bytes": size,
            "logical_size_mb": round(size / (1024 * 1024), 2),
            "logical_size_gb": round(size / (1024 * 1024 * 1024), 2),
            "category": _large_file_category(root, path),
        }
        for size, path in files[:limit]
    ]


def _large_file_category(root: Path, path: Path) -> str:
    if _is_under(root / ".git" / "lfs", path):
        return "git_lfs_object_or_cache"
    if _is_under(root / "data" / "processed", path):
        return "processed_data"
    if _is_under(root / "data" / "raw", path):
        return "raw_source_data"
    if _is_under(root / "reports", path):
        return "report_artifact"
    if _is_under(root / "logs", path):
        return "log_artifact"
    return "workspace_file"


def _assert_safe_cleanup_path(root: Path, path: Path, *, allowed_leaf: str) -> None:
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not _is_under(resolved_root, resolved_path):
        raise ValueError(f"Refusing to remove path outside workspace: {resolved_path}")
    if resolved_path.name != allowed_leaf:
        raise ValueError(f"Refusing to remove unexpected path: {resolved_path}")
    if resolved_path == resolved_root:
        raise ValueError(f"Refusing to remove workspace root: {resolved_path}")


def _is_under(parent: Path, child: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
