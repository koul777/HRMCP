from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA = "ncs_source_package_install_smoke_v1"
METADATA_MANIFEST_SCHEMA = "ncs_path_metadata_manifest_v1"
PROTECTED_PATHS_SCHEMA = "ncs_package_install_protected_paths_v1"
SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_SAMPLE_LIMIT = 50
REQUIRED_CONSOLE_SCRIPTS = ("ncs-mcp", "ncs-institutional-chat")
SECRET_ENV_MARKERS = (
    "API_KEY",
    "SERVICE_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "PRIVATE_KEY",
    "ACCESS_KEY",
)
NETWORK_ENV_NAMES = {
    "ALL_PROXY",
    "FTP_PROXY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "PIP_EXTRA_INDEX_URL",
    "PIP_INDEX_URL",
    "PIP_TRUSTED_HOST",
}
RUNTIME_PATH_ENV_NAMES = {
    "NCS_DB_PATH",
    "NCS_EXCEL_PATH",
    "NCS_REPORTS_DIR",
    "PYTHONHOME",
    "PYTHONPATH",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(value: str | None, limit: int) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _metadata_kind(mode: int) -> str:
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "other"


def _metadata_record(stat_result: os.stat_result) -> dict[str, int | str]:
    return {
        "kind": _metadata_kind(stat_result.st_mode),
        "size": int(stat_result.st_size),
        "mtime_ns": int(stat_result.st_mtime_ns),
    }


def _metadata_fingerprint(
    *,
    exists: bool,
    root_metadata: dict[str, int | str] | None,
    entries: dict[str, dict[str, int | str]],
) -> str:
    digest = hashlib.sha256()
    digest.update(f"exists\0{int(exists)}\n".encode("utf-8"))
    if root_metadata is not None:
        digest.update(
            (
                "root\0"
                f"{root_metadata['kind']}\0{root_metadata['size']}\0"
                f"{root_metadata['mtime_ns']}\n"
            ).encode("utf-8")
        )
    for relative_path in sorted(entries):
        metadata = entries[relative_path]
        digest.update(
            (
                f"{relative_path}\0{metadata['kind']}\0{metadata['size']}\0"
                f"{metadata['mtime_ns']}\n"
            ).encode("utf-8")
        )
    return digest.hexdigest()


def capture_metadata_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve(strict=False)
    entries: dict[str, dict[str, int | str]] = {}
    scan_errors: list[dict[str, str]] = []
    root_metadata: dict[str, int | str] | None = None
    exists = False

    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        root_stat = None
    except OSError as exc:
        root_stat = None
        scan_errors.append(
            {
                "relative_path": ".",
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
    if root_stat is not None:
        exists = True
        root_metadata = _metadata_record(root_stat)

    if root_metadata and root_metadata["kind"] == "directory":
        pending: list[tuple[Path, str]] = [(root, "")]
        while pending:
            current_path, current_relative = pending.pop()
            try:
                with os.scandir(current_path) as iterator:
                    children = sorted(iterator, key=lambda item: item.name)
            except OSError as exc:
                if len(scan_errors) < MANIFEST_SAMPLE_LIMIT:
                    scan_errors.append(
                        {
                            "relative_path": current_relative or ".",
                            "type": type(exc).__name__,
                            "message": str(exc),
                        }
                    )
                continue
            for child in children:
                relative_path = (
                    f"{current_relative}/{child.name}"
                    if current_relative
                    else child.name
                )
                try:
                    child_stat = child.stat(follow_symlinks=False)
                except OSError as exc:
                    if len(scan_errors) < MANIFEST_SAMPLE_LIMIT:
                        scan_errors.append(
                            {
                                "relative_path": relative_path,
                                "type": type(exc).__name__,
                                "message": str(exc),
                            }
                        )
                    continue
                metadata = _metadata_record(child_stat)
                entries[relative_path] = metadata
                if metadata["kind"] == "directory":
                    pending.append((Path(child.path), relative_path))

    kind_counts = {
        kind: sum(item["kind"] == kind for item in entries.values())
        for kind in ("file", "directory", "symlink", "other")
    }
    total_file_size = sum(
        int(item["size"])
        for item in entries.values()
        if item["kind"] == "file"
    )
    return {
        "schema": METADATA_MANIFEST_SCHEMA,
        "path": str(root),
        "exists": exists,
        "root_metadata": root_metadata,
        "entries": entries,
        "entry_count": len(entries),
        "file_count": kind_counts["file"],
        "directory_count": kind_counts["directory"],
        "symlink_count": kind_counts["symlink"],
        "other_count": kind_counts["other"],
        "total_file_size_bytes": total_file_size,
        "scan_ok": not scan_errors,
        "scan_errors": scan_errors,
        "metadata_fingerprint": _metadata_fingerprint(
            exists=exists,
            root_metadata=root_metadata,
            entries=entries,
        ),
        "file_contents_hashed": False,
    }


def _manifest_summary(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        name: value
        for name, value in manifest.items()
        if name != "entries"
    }


def compare_metadata_manifests(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    before_entries = before.get("entries", {})
    after_entries = after.get("entries", {})
    before_paths = set(before_entries)
    after_paths = set(after_entries)
    created_paths = sorted(after_paths - before_paths)
    deleted_paths = sorted(before_paths - after_paths)
    modified_paths = sorted(
        path
        for path in before_paths & after_paths
        if before_entries[path] != after_entries[path]
    )
    root_changed = bool(
        before.get("exists") != after.get("exists")
        or before.get("root_metadata") != after.get("root_metadata")
    )
    changed = bool(root_changed or created_paths or deleted_paths or modified_paths)
    return {
        "changed": changed,
        "root_changed": root_changed,
        "change_count": (
            int(root_changed)
            + len(created_paths)
            + len(deleted_paths)
            + len(modified_paths)
        ),
        "created_count": len(created_paths),
        "deleted_count": len(deleted_paths),
        "modified_count": len(modified_paths),
        "created_samples": [
            {"path": path, "after": after_entries[path]}
            for path in created_paths[:MANIFEST_SAMPLE_LIMIT]
        ],
        "deleted_samples": [
            {"path": path, "before": before_entries[path]}
            for path in deleted_paths[:MANIFEST_SAMPLE_LIMIT]
        ],
        "modified_samples": [
            {
                "path": path,
                "before": before_entries[path],
                "after": after_entries[path],
            }
            for path in modified_paths[:MANIFEST_SAMPLE_LIMIT]
        ],
        "samples_truncated": bool(
            len(created_paths) > MANIFEST_SAMPLE_LIMIT
            or len(deleted_paths) > MANIFEST_SAMPLE_LIMIT
            or len(modified_paths) > MANIFEST_SAMPLE_LIMIT
        ),
    }


def _protected_path_specs(
    source_preview_dir: Path,
    repository_root: Path,
) -> dict[str, dict[str, Any]]:
    return {
        "source_preview_original": {
            "path": source_preview_dir,
            "role": "source_preview_original",
        },
        "repository_reports": {
            "path": repository_root / "reports",
            "role": "active_repository_reports",
        },
        "repository_data_processed": {
            "path": repository_root / "data" / "processed",
            "role": "active_repository_data_processed",
        },
    }


def _protected_paths_report(
    specs: dict[str, dict[str, Any]],
    before_manifests: dict[str, dict[str, Any]],
    after_manifests: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for name, spec in specs.items():
        before = before_manifests[name]
        path_report: dict[str, Any] = {
            "path": str(spec["path"]),
            "role": spec["role"],
            "protected": True,
            "temporary_workspace_path": False,
            "before": _manifest_summary(before),
        }
        if after_manifests is not None:
            after = after_manifests[name]
            comparison = compare_metadata_manifests(before, after)
            path_report.update(
                {
                    "after": _manifest_summary(after),
                    "manifest_capture_ok": bool(
                        before.get("scan_ok") and after.get("scan_ok")
                    ),
                    **comparison,
                }
            )
        paths[name] = path_report

    completed = after_manifests is not None
    return {
        "schema": PROTECTED_PATHS_SCHEMA,
        "metadata_only": True,
        "file_contents_hashed": False,
        "temporary_workspace_changes_allowed": True,
        "temporary_source_preview_copy": None,
        "comparison_completed": completed,
        "manifest_capture_ok": bool(
            completed
            and all(item.get("manifest_capture_ok") for item in paths.values())
        ),
        "all_unchanged": bool(
            completed
            and all(item.get("changed") is False for item in paths.values())
        ),
        "paths": paths,
    }


def _is_secret_like_environment_name(name: str) -> bool:
    upper_name = name.upper()
    return any(marker in upper_name for marker in SECRET_ENV_MARKERS)


def build_safe_environment(
    *,
    target_dir: Path,
    temporary_db_path: Path,
    temporary_reports_dir: Path,
) -> tuple[dict[str, str], dict[str, Any]]:
    env = os.environ.copy()
    removed_secret_names = sorted(
        name for name in env if _is_secret_like_environment_name(name)
    )
    for name in removed_secret_names:
        env.pop(name, None)

    removed_network_names = sorted(
        name for name in env if name.upper() in NETWORK_ENV_NAMES
    )
    for name in removed_network_names:
        env.pop(name, None)

    removed_runtime_path_names = sorted(
        name for name in env if name.upper() in RUNTIME_PATH_ENV_NAMES
    )
    for name in removed_runtime_path_names:
        env.pop(name, None)

    env.update(
        {
            "NCS_DB_PATH": str(temporary_db_path.resolve()),
            "NCS_MCP_READ_ONLY": "1",
            "NCS_REPORTS_DIR": str(temporary_reports_dir.resolve()),
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_NO_CACHE_DIR": "1",
            "PIP_NO_INDEX": "1",
            "PIP_NO_INPUT": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(target_dir.resolve()),
        }
    )
    return env, {
        "secret_like_names_removed": removed_secret_names,
        "secret_like_name_count": len(removed_secret_names),
        "network_names_removed": removed_network_names,
        "network_name_count": len(removed_network_names),
        "runtime_path_names_removed": removed_runtime_path_names,
        "runtime_path_name_count": len(removed_runtime_path_names),
        "active_db_path_inherited": "NCS_DB_PATH" in removed_runtime_path_names,
        "temporary_db_path_forced": True,
        "temporary_reports_path_forced": True,
        "read_only_mode_forced": True,
        "pip_no_index_forced": True,
    }


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, 15)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    tail_chars: int,
) -> dict[str, Any]:
    started = time.monotonic()
    popen_kwargs: dict[str, Any] = {}
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(
            args,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
            **popen_kwargs,
        )
    except OSError as exc:
        return {
            "argv": args,
            "command": subprocess.list2cmdline(args),
            "returncode": 127,
            "timed_out": False,
            "duration_seconds": round(time.monotonic() - started, 3),
            "stdout_tail": "",
            "stdout_truncated": False,
            "stderr_tail": str(exc),
            "stderr_truncated": False,
        }

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        returncode = int(process.returncode or 0)
    except subprocess.TimeoutExpired:
        timed_out = True
        returncode = 124
        _terminate_process_tree(process)
        stdout, stderr = process.communicate()
    except BaseException:
        _terminate_process_tree(process)
        process.communicate()
        raise

    stdout_tail, stdout_truncated = _tail(stdout, tail_chars)
    stderr_tail, stderr_truncated = _tail(stderr, tail_chars)
    return {
        "argv": args,
        "command": subprocess.list2cmdline(args),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": stdout_tail,
        "stdout_truncated": stdout_truncated,
        "stderr_tail": stderr_tail,
        "stderr_truncated": stderr_truncated,
    }


def _entrypoint_candidates(
    target_dir: Path,
    executable_name: str = "ncs-mcp",
) -> list[Path]:
    executable_names = (
        [f"{executable_name}.exe", executable_name]
        if os.name == "nt"
        else [executable_name, f"{executable_name}.exe"]
    )
    return [
        target_dir / script_dir / executable_name
        for script_dir in ("bin", "Scripts")
        for executable_name in executable_names
    ]


def _unsafe_link_kind(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return "junction"
        except OSError:
            return "unreadable_reparse_candidate"
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return None
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse_flag and attributes & reparse_flag:
        return "reparse_point"
    return None


def _find_unsafe_links(root: Path) -> list[dict[str, str]]:
    root_kind = _unsafe_link_kind(root)
    if root_kind:
        return [{"path": ".", "kind": root_kind}]
    findings: list[dict[str, str]] = []
    pending = [root]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                path = Path(entry.path)
                kind = _unsafe_link_kind(path)
                if kind:
                    findings.append(
                        {
                            "path": path.relative_to(root).as_posix(),
                            "kind": kind,
                        }
                    )
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(path)
    return findings


SOURCE_PREVIEW_BLOCKED_TOP_LEVEL_PATHS = (
    ".git",
    ".venv",
    "data",
    "exports",
    "reports",
    "tmp",
    "venv",
)
MAX_SOURCE_PREVIEW_FILE_COUNT = 10_000
MAX_SOURCE_PREVIEW_BYTES = 1_073_741_824


def _declared_console_scripts(pyproject_path: Path) -> tuple[list[str], str | None]:
    try:
        payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    scripts = payload.get("project", {}).get("scripts", {})
    if not isinstance(scripts, dict):
        return [], "project.scripts must be a TOML table"
    return sorted(str(name) for name in scripts), None


def _source_preview_size(root: Path) -> tuple[int, int, bool]:
    file_count = 0
    size_bytes = 0
    within_limits = True
    pending = [root]
    try:
        while pending and within_limits:
            current = pending.pop()
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if _unsafe_link_kind(path):
                        within_limits = False
                        break
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    file_count += 1
                    size_bytes += entry.stat(follow_symlinks=False).st_size
                    if (
                        file_count > MAX_SOURCE_PREVIEW_FILE_COUNT
                        or size_bytes > MAX_SOURCE_PREVIEW_BYTES
                    ):
                        within_limits = False
                        break
    except OSError:
        within_limits = False
    return file_count, size_bytes, within_limits


def _preflight(source_preview_dir: Path) -> dict[str, Any]:
    exists = source_preview_dir.exists()
    is_dir = source_preview_dir.is_dir()
    pyproject_exists = (source_preview_dir / "pyproject.toml").is_file() if is_dir else False
    package_init_exists = (
        source_preview_dir / "src" / "ncs_mcp" / "__init__.py"
    ).is_file() if is_dir else False
    declared_console_scripts, pyproject_parse_error = (
        _declared_console_scripts(source_preview_dir / "pyproject.toml")
        if pyproject_exists
        else ([], None)
    )
    missing_required_console_scripts = sorted(
        set(REQUIRED_CONSOLE_SCRIPTS) - set(declared_console_scripts)
    )
    blocked_top_level_paths = (
        [
            name
            for name in SOURCE_PREVIEW_BLOCKED_TOP_LEVEL_PATHS
            if (source_preview_dir / name).exists()
        ]
        if is_dir
        else []
    )
    unsafe_link_scan_error = None
    try:
        unsafe_links = (
            _find_unsafe_links(source_preview_dir)
            if is_dir and not blocked_top_level_paths
            else []
        )
    except OSError as exc:
        unsafe_links = []
        unsafe_link_scan_error = f"{type(exc).__name__}: {exc}"
    symlink_paths = [
        item["path"] for item in unsafe_links if item["kind"] == "symlink"
    ]
    junction_paths = [
        item["path"] for item in unsafe_links if item["kind"] == "junction"
    ]
    reparse_point_paths = [
        item["path"] for item in unsafe_links if item["kind"] == "reparse_point"
    ]
    file_count, size_bytes, within_size_limits = (
        _source_preview_size(source_preview_dir)
        if is_dir and not blocked_top_level_paths and not unsafe_links
        else (0, 0, False)
    )
    ok = bool(
        exists
        and is_dir
        and pyproject_exists
        and package_init_exists
        and pyproject_parse_error is None
        and not missing_required_console_scripts
        and not blocked_top_level_paths
        and unsafe_link_scan_error is None
        and not unsafe_links
        and within_size_limits
    )
    return {
        "ok": ok,
        "exists": exists,
        "is_dir": is_dir,
        "pyproject_exists": pyproject_exists,
        "package_init_exists": package_init_exists,
        "declared_console_scripts": declared_console_scripts,
        "required_console_scripts": list(REQUIRED_CONSOLE_SCRIPTS),
        "missing_required_console_scripts": missing_required_console_scripts,
        "pyproject_parse_error": pyproject_parse_error,
        "blocked_top_level_paths": blocked_top_level_paths,
        "symlink_count": len(symlink_paths),
        "symlink_paths": symlink_paths,
        "junction_count": len(junction_paths),
        "junction_paths": junction_paths,
        "reparse_point_count": len(reparse_point_paths),
        "reparse_point_paths": reparse_point_paths,
        "unsafe_link_count": len(unsafe_links),
        "unsafe_links": unsafe_links,
        "unsafe_link_scan_error": unsafe_link_scan_error,
        "file_count": file_count,
        "size_bytes": size_bytes,
        "max_file_count": MAX_SOURCE_PREVIEW_FILE_COUNT,
        "max_size_bytes": MAX_SOURCE_PREVIEW_BYTES,
        "within_size_limits": within_size_limits,
    }


def _step(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "status": "passed" if result.get("returncode") == 0 else "failed",
    }


def _skipped(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason}


def _parse_last_json_line(text: str | None) -> dict[str, Any] | None:
    for line in reversed((text or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _base_report(
    source_preview_dir: Path,
    repository_root: Path,
    preflight: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "generated_at": _now(),
        "ok": False,
        "status": "failed",
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "active_db_writes": False,
        "temporary_db_writes": False,
        "db_write_scope": "none",
        "api_calls": False,
        "external_network_allowed": False,
        "approval_claim": False,
        "acceptance_claim": False,
        "human_review_status_writes": False,
        "temporary_install": True,
        "active_environment_modified": False,
        "source_preview_modified": False,
        "source_preview_dir": str(source_preview_dir),
        "repository_root": str(repository_root),
        "preflight": preflight,
        "offline_install": True,
        "pip_no_deps": True,
        "pip_no_index": True,
        "pip_no_build_isolation": True,
        "dependency_installation_requested": False,
        "temporary_workspace_created": False,
        "temporary_workspace_cleaned_up": True,
        "protected_paths": {
            "schema": PROTECTED_PATHS_SCHEMA,
            "metadata_only": True,
            "file_contents_hashed": False,
            "temporary_workspace_changes_allowed": True,
            "temporary_source_preview_copy": None,
            "comparison_completed": False,
            "manifest_capture_ok": False,
            "all_unchanged": False,
            "paths": {},
        },
        "environment_safety": {
            "secret_like_names_removed": [],
            "secret_like_name_count": 0,
            "network_names_removed": [],
            "network_name_count": 0,
            "runtime_path_names_removed": [],
            "runtime_path_name_count": 0,
            "active_db_path_inherited": False,
            "temporary_db_path_forced": False,
            "temporary_reports_path_forced": False,
            "read_only_mode_forced": False,
            "pip_no_index_forced": False,
        },
        "checks": {
            "source_preview_shape_ok": bool(preflight.get("ok")),
            "install_ok": False,
            "import_ok": False,
            "import_from_temporary_target": False,
            "entrypoint_found": False,
            "entrypoint_help_ok": False,
            "institutional_chat_entrypoint_found": False,
            "institutional_chat_entrypoint_help_ok": False,
            "temporary_db_absent": True,
            "protected_path_manifests_ok": False,
            "protected_paths_unchanged": False,
            "source_preview_original_unchanged": False,
            "repository_reports_unchanged": False,
            "repository_data_processed_unchanged": False,
            "cleanup_ok": True,
        },
        "steps": {
            "install": _skipped("source preview preflight did not pass"),
            "import": _skipped("package was not installed"),
            "entrypoint_help": _skipped("package was not installed"),
            "institutional_chat_entrypoint_help": _skipped(
                "package was not installed"
            ),
        },
        "summary": {
            "executed_command_count": 0,
            "failed_command_count": 0,
            "timed_out_command_count": 0,
        },
        "commands": [],
        "errors": [],
    }


def run_package_install_smoke(
    source_preview_dir: Path,
    *,
    timeout_seconds: int = 120,
    tail_chars: int = 4000,
    python_executable: str | None = None,
    repository_root: Path | None = None,
) -> dict[str, Any]:
    source_preview_dir = source_preview_dir.resolve()
    repository_root = (
        repository_root.resolve(strict=False)
        if repository_root is not None
        else SCRIPT_REPOSITORY_ROOT
    )
    preflight = _preflight(source_preview_dir)
    report = _base_report(source_preview_dir, repository_root, preflight)
    if not preflight["ok"]:
        report["status"] = "invalid_source_preview"
        return report

    protected_specs = _protected_path_specs(source_preview_dir, repository_root)
    before_manifests = {
        name: capture_metadata_manifest(spec["path"])
        for name, spec in protected_specs.items()
    }
    report["protected_paths"] = _protected_paths_report(
        protected_specs,
        before_manifests,
    )
    if not all(manifest["scan_ok"] for manifest in before_manifests.values()):
        report["status"] = "protected_path_manifest_failed"
        report["errors"].append(
            {
                "type": "ProtectedPathManifestError",
                "message": "one or more protected paths could not be scanned safely",
            }
        )
        return report

    temporary_directory = tempfile.TemporaryDirectory(prefix="ncs-package-install-smoke-")
    temporary_root = Path(temporary_directory.name)
    report["temporary_workspace_created"] = True
    commands: list[dict[str, Any]] = []
    temporary_db_path = temporary_root / "db-sentinel" / "ncs.db"
    cleanup_error: str | None = None

    try:
        build_source = temporary_root / "source-preview"
        target_dir = temporary_root / "target"
        work_dir = temporary_root / "work"
        reports_dir = temporary_root / "reports"
        shutil.copytree(source_preview_dir, build_source)
        report["protected_paths"]["temporary_source_preview_copy"] = {
            "path": str(build_source),
            "role": "temporary_source_preview_copy",
            "protected": False,
            "temporary_workspace_path": True,
            "changes_allowed": True,
        }
        target_dir.mkdir()
        work_dir.mkdir()
        reports_dir.mkdir()

        env, environment_safety = build_safe_environment(
            target_dir=target_dir,
            temporary_db_path=temporary_db_path,
            temporary_reports_dir=reports_dir,
        )
        report["environment_safety"] = environment_safety
        interpreter = python_executable or sys.executable
        install_args = [
            interpreter,
            "-m",
            "pip",
            "--isolated",
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "--no-deps",
            "--no-index",
            "--no-build-isolation",
            "--no-cache-dir",
            "--no-compile",
            "--no-warn-script-location",
            "--target",
            str(target_dir),
            str(build_source),
        ]
        install_result = _step(
            run_command(
                install_args,
                cwd=work_dir,
                env=env,
                timeout_seconds=timeout_seconds,
                tail_chars=tail_chars,
            )
        )
        report["steps"]["install"] = install_result
        commands.append(install_result)
        install_ok = install_result["returncode"] == 0
        report["checks"]["install_ok"] = install_ok

        if install_ok:
            import_probe = (
                "import json, pathlib, sys; "
                "import ncs_mcp; "
                "target = pathlib.Path(sys.argv[1]).resolve(); "
                "package_file = pathlib.Path(ncs_mcp.__file__).resolve(); "
                "from_target = package_file.is_relative_to(target); "
                "print(json.dumps({'module': ncs_mcp.__name__, "
                "'version': getattr(ncs_mcp, '__version__', None), "
                "'package_file': str(package_file), 'from_target': from_target})); "
                "raise SystemExit(0 if from_target else 3)"
            )
            import_result = _step(
                run_command(
                    [interpreter, "-c", import_probe, str(target_dir)],
                    cwd=work_dir,
                    env=env,
                    timeout_seconds=timeout_seconds,
                    tail_chars=tail_chars,
                )
            )
            import_details = _parse_last_json_line(import_result.get("stdout_tail"))
            if import_details is not None:
                import_result["probe"] = import_details
            report["steps"]["import"] = import_result
            commands.append(import_result)
            import_ok = import_result["returncode"] == 0
            import_from_target = bool(
                import_ok
                and import_details
                and import_details.get("module") == "ncs_mcp"
                and import_details.get("from_target") is True
            )
            report["checks"]["import_ok"] = import_ok
            report["checks"]["import_from_temporary_target"] = import_from_target

            entrypoint = next(
                (
                    path
                    for path in _entrypoint_candidates(target_dir, "ncs-mcp")
                    if path.is_file()
                ),
                None,
            )
            report["checks"]["entrypoint_found"] = entrypoint is not None
            if entrypoint is not None:
                entrypoint_result = _step(
                    run_command(
                        [str(entrypoint), "--help"],
                        cwd=work_dir,
                        env=env,
                        timeout_seconds=timeout_seconds,
                        tail_chars=tail_chars,
                    )
                )
                help_text = (
                    str(entrypoint_result.get("stdout_tail") or "")
                    + "\n"
                    + str(entrypoint_result.get("stderr_tail") or "")
                ).lower()
                entrypoint_result["help_contract_ok"] = bool(
                    entrypoint_result["returncode"] == 0
                    and "usage:" in help_text
                    and "--transport" in help_text
                )
                report["steps"]["entrypoint_help"] = entrypoint_result
                commands.append(entrypoint_result)
                report["checks"]["entrypoint_help_ok"] = entrypoint_result[
                    "help_contract_ok"
                ]
            else:
                report["steps"]["entrypoint_help"] = _skipped(
                    "pip install did not create the ncs-mcp entrypoint"
                )

            chat_entrypoint = next(
                (
                    path
                    for path in _entrypoint_candidates(
                        target_dir,
                        "ncs-institutional-chat",
                    )
                    if path.is_file()
                ),
                None,
            )
            report["checks"]["institutional_chat_entrypoint_found"] = (
                chat_entrypoint is not None
            )
            if chat_entrypoint is not None:
                chat_entrypoint_result = _step(
                    run_command(
                        [str(chat_entrypoint), "--help"],
                        cwd=work_dir,
                        env=env,
                        timeout_seconds=timeout_seconds,
                        tail_chars=tail_chars,
                    )
                )
                chat_help_text = (
                    str(chat_entrypoint_result.get("stdout_tail") or "")
                    + "\n"
                    + str(chat_entrypoint_result.get("stderr_tail") or "")
                ).lower()
                chat_entrypoint_result["help_contract_ok"] = bool(
                    chat_entrypoint_result["returncode"] == 0
                    and "usage:" in chat_help_text
                    and "--auth-mode" in chat_help_text
                    and "--allow-remote-bind" in chat_help_text
                )
                report["steps"]["institutional_chat_entrypoint_help"] = (
                    chat_entrypoint_result
                )
                commands.append(chat_entrypoint_result)
                report["checks"]["institutional_chat_entrypoint_help_ok"] = (
                    chat_entrypoint_result["help_contract_ok"]
                )
            else:
                report["steps"]["institutional_chat_entrypoint_help"] = _skipped(
                    "pip install did not create the ncs-institutional-chat entrypoint"
                )
        else:
            report["steps"]["import"] = _skipped("package installation failed")
            report["steps"]["entrypoint_help"] = _skipped(
                "package installation failed"
            )
            report["steps"]["institutional_chat_entrypoint_help"] = _skipped(
                "package installation failed"
            )

    except Exception as exc:
        report["errors"].append(
            {"type": type(exc).__name__, "message": str(exc)}
        )
    finally:
        after_manifests = {
            name: capture_metadata_manifest(spec["path"])
            for name, spec in protected_specs.items()
        }
        protected_paths = _protected_paths_report(
            protected_specs,
            before_manifests,
            after_manifests,
        )
        protected_paths["temporary_source_preview_copy"] = report[
            "protected_paths"
        ].get("temporary_source_preview_copy")
        report["protected_paths"] = protected_paths
        protected_path_reports = protected_paths["paths"]
        report["checks"]["protected_path_manifests_ok"] = protected_paths[
            "manifest_capture_ok"
        ]
        report["checks"]["protected_paths_unchanged"] = protected_paths[
            "all_unchanged"
        ]
        report["checks"]["source_preview_original_unchanged"] = not bool(
            protected_path_reports["source_preview_original"]["changed"]
        )
        report["checks"]["repository_reports_unchanged"] = not bool(
            protected_path_reports["repository_reports"]["changed"]
        )
        report["checks"]["repository_data_processed_unchanged"] = not bool(
            protected_path_reports["repository_data_processed"]["changed"]
        )
        report["source_preview_modified"] = not report["checks"][
            "source_preview_original_unchanged"
        ]
        report["active_environment_modified"] = not protected_paths[
            "all_unchanged"
        ]
        active_data_modified = not report["checks"][
            "repository_data_processed_unchanged"
        ]
        report["active_db_writes"] = active_data_modified
        if not protected_paths["manifest_capture_ok"]:
            report["errors"].append(
                {
                    "type": "ProtectedPathManifestError",
                    "message": "a protected path could not be scanned after command execution",
                }
            )
        changed_labels = [
            name
            for name, path_report in protected_path_reports.items()
            if path_report["changed"]
        ]
        if changed_labels:
            report["errors"].append(
                {
                    "type": "ProtectedPathModified",
                    "message": (
                        "protected paths changed outside the temporary workspace: "
                        + ", ".join(changed_labels)
                    ),
                }
            )

        temporary_db_created = temporary_db_path.exists()
        report["temporary_db_writes"] = temporary_db_created
        report["db_writes"] = bool(temporary_db_created or active_data_modified)
        if active_data_modified and temporary_db_created:
            report["db_write_scope"] = (
                "active_data_processed_and_ephemeral_sentinel"
            )
        elif active_data_modified:
            report["db_write_scope"] = "active_data_processed_modified"
        elif temporary_db_created:
            report["db_write_scope"] = "ephemeral_sentinel_only"
        else:
            report["db_write_scope"] = "none"
        report["checks"]["temporary_db_absent"] = not temporary_db_created
        report["commands"] = commands
        report["summary"] = {
            "executed_command_count": len(commands),
            "failed_command_count": sum(
                item.get("returncode") != 0 for item in commands
            ),
            "timed_out_command_count": sum(
                bool(item.get("timed_out")) for item in commands
            ),
        }
        try:
            temporary_directory.cleanup()
        except OSError as exc:
            cleanup_error = str(exc)

    cleanup_ok = not temporary_root.exists()
    report["temporary_workspace_cleaned_up"] = cleanup_ok
    report["checks"]["cleanup_ok"] = cleanup_ok
    if cleanup_error:
        report["errors"].append(
            {"type": "CleanupError", "message": cleanup_error}
        )

    required_checks = (
        "source_preview_shape_ok",
        "install_ok",
        "import_ok",
        "import_from_temporary_target",
        "entrypoint_found",
        "entrypoint_help_ok",
        "institutional_chat_entrypoint_found",
        "institutional_chat_entrypoint_help_ok",
        "temporary_db_absent",
        "protected_path_manifests_ok",
        "protected_paths_unchanged",
        "source_preview_original_unchanged",
        "repository_reports_unchanged",
        "repository_data_processed_unchanged",
        "cleanup_ok",
    )
    report["ok"] = bool(
        not report["errors"]
        and all(report["checks"].get(name) is True for name in required_checks)
    )
    report["status"] = "passed" if report["ok"] else "failed"
    return report


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Source Package Install Smoke",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- status: `{report.get('status')}`",
        f"- source_preview_dir: `{report.get('source_preview_dir')}`",
        f"- report_only: `{str(report.get('report_only')).lower()}`",
        f"- active_environment_modified: `{str(report.get('active_environment_modified')).lower()}`",
        f"- source_preview_modified: `{str(report.get('source_preview_modified')).lower()}`",
        f"- active_db_writes: `{str(report.get('active_db_writes')).lower()}`",
        f"- api_calls: `{str(report.get('api_calls')).lower()}`",
        f"- external_network_allowed: `{str(report.get('external_network_allowed')).lower()}`",
        f"- temporary_workspace_cleaned_up: `{str(report.get('temporary_workspace_cleaned_up')).lower()}`",
        "",
        "## Checks",
        "",
    ]
    for name, value in report.get("checks", {}).items():
        lines.append(f"- {name}: `{str(value).lower()}`")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Install an exported source-preview package into a temporary target and "
            "verify import plus both public console entrypoints without dependency "
            "or network access."
        )
    )
    parser.add_argument(
        "--source-preview-dir",
        "--output-dir",
        dest="source_preview_dir",
        type=Path,
        required=True,
    )
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--timeout-per-command", type=int, default=120)
    parser.add_argument("--tail-chars", type=int, default=4000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report = run_package_install_smoke(
        args.source_preview_dir,
        timeout_seconds=max(1, args.timeout_per_command),
        tail_chars=max(100, args.tail_chars),
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    print(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
