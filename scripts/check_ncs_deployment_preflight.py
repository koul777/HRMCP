"""Read-only GitHub/Vercel deployment preflight for the NCS MCP release."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA = "ncs_deployment_preflight_v1"
EXPECTED_PUBLIC_TOOLS = {
    "ncs_analysis",
    "ncs_discover_tools",
    "ncs_execute_tool",
    "ncs_search",
    "ncs_training",
    "ncs_unit_detail",
    "recommend_training_for_task",
}
REQUIRED_VERCEL_ENV_NAMES = {
    "NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE",
    "NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION",
    "NCS_MCP_ENABLE_ADVANCED_TOOLS",
    "NCS_MCP_ENABLE_OPERATOR_TOOLS",
    "NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS",
    "NCS_MCP_READINESS_EXTRA_TABLES",
    "NCS_MCP_READINESS_MIN_ROWS",
    "NCS_MCP_READ_ONLY",
    "NCS_MCP_STREAMABLE_HTTP_PATH",
}
HARD_SNAPSHOT_BYTES = 480_000_000
SOFT_SNAPSHOT_BYTES = 460_000_000
EXPECTED_MAX_DURATION = 30
GIT_EXCLUDES = (
    ":(exclude)data/processed/*.db",
    ":(exclude)data/processed/*.db-*",
    ":(exclude)data/raw/*.xlsx",
    ":(exclude)data/raw/*.xls",
    ":(exclude)data/ocr/tessdata/*.traineddata",
)
OWNED_PATHS = (
    "scripts/check_ncs_deployment_preflight.py",
    "tests/test_check_ncs_deployment_preflight.py",
    "reports/ncs_deployment_preflight_20260830.json",
    "reports/ncs_deployment_preflight_20260830.md",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _check(check_id: str, status: str, summary: str, **details: Any) -> dict[str, Any]:
    return {"id": check_id, "status": status, "summary": summary, "details": details}


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        return tomllib.load(stream)


def _literal_string_set(path: Path, variable: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == variable for target in targets):
            continue
        value = node.value
        if not isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            raise ValueError(f"{variable} is not a literal string collection")
        result: set[str] = set()
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                raise ValueError(f"{variable} contains a non-string literal")
            result.add(item.value)
        return result
    raise ValueError(f"missing literal collection: {variable}")


def _async_app_defined(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return any(isinstance(node, ast.AsyncFunctionDef) and node.name == "app" for node in tree.body)


def _env_names_from_example(path: Path) -> set[str]:
    names: set[str] = set()
    if not path.is_file():
        return names
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name and (name[0].isalpha() or name[0] == "_") and all(
            char.isalnum() or char == "_" for char in name
        ):
            names.add(name)
    return names


def _run(command: list[str], cwd: Path, timeout: int = 20) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return completed.returncode, completed.stdout


def _classify_cli_failure(stderr: str) -> str:
    normalized = stderr.casefold()
    if any(token in normalized for token in ("not logged in", "log in", "login", "token", "unauthorized", "authentication", "credentials")):
        return "credential_missing_or_invalid"
    if any(token in normalized for token in ("network", "timeout", "timed out", "dns", "enotfound", "econn", "fetch failed")):
        return "network_error"
    if any(token in normalized for token in ("permission denied", "access denied", "forbidden")):
        return "permission_denied"
    return "command_failed"


def _run_cli_probe(command: list[str], cwd: Path, timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except OSError:
        return 127, "execution_error"
    if completed.returncode == 0:
        return 0, "none"
    return completed.returncode, _classify_cli_failure(completed.stderr)


def _parse_git_status(output: str) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for raw_line in output.splitlines():
        if not raw_line.strip() or len(raw_line) < 4:
            continue
        changes.append({"code": raw_line[:2], "path": raw_line[3:].strip().replace("\\", "/")})
    return changes


def _is_release_related(path: str) -> bool:
    normalized = path.replace("\\", "/")
    exact = {
        "api/mcp.py",
        "src/ncs_mcp/config.py",
        "src/ncs_mcp/db.py",
        "src/ncs_mcp/server.py",
        "src/ncs_mcp/vercel_snapshot.py",
        "deploy/vercel_mcp_app/api/mcp.py",
        "deploy/vercel_mcp_app/src/ncs_mcp/config.py",
        "deploy/vercel_mcp_app/src/ncs_mcp/db.py",
        "deploy/vercel_mcp_app/src/ncs_mcp/server.py",
        "deploy/vercel_mcp_app/src/ncs_mcp/vercel_snapshot.py",
    }
    if normalized in exact or normalized in OWNED_PATHS:
        return True
    return normalized.startswith(("reports/ncs_search_", "scripts/benchmark_ncs_search", "tests/test_benchmark_ncs_search", "tests/test_ncs_search"))


def _git_status_commands() -> tuple[list[str], list[str]]:
    tracked_command = [
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
        *GIT_EXCLUDES,
    ]
    separator = tracked_command.index("--")
    owned_command = tracked_command[:separator]
    owned_command.remove("--untracked-files=no")
    owned_command.extend(["--untracked-files=all", "--", *OWNED_PATHS])
    return tracked_command, owned_command


def _inspect_changes(repo_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    tracked_command, owned_command = _git_status_commands()
    exit_code, output = _run(tracked_command, repo_root)
    tracked = _parse_git_status(output) if exit_code == 0 else []

    owned_exit, owned_output = _run(owned_command, repo_root)
    owned = _parse_git_status(owned_output) if owned_exit == 0 else []

    merged = {(item["code"], item["path"]): item for item in [*tracked, *owned]}
    changes = list(merged.values())
    related = [item for item in changes if _is_release_related(item["path"])]
    unrelated = [item for item in changes if not _is_release_related(item["path"])]
    details = {
        "tracked_scan_uses_lfs_safe_filters": True,
        "general_untracked_scan_performed": False,
        "owned_paths_scanned_for_untracked": list(OWNED_PATHS),
        "related": related,
        "unrelated": unrelated,
    }
    if exit_code != 0 or owned_exit != 0:
        return details, _check("worktree_scope", "block", "Unable to inspect the worktree safely.")
    if unrelated:
        return details, _check(
            "worktree_scope",
            "warn",
            "Tracked changes outside the known release scope require selective staging.",
            unrelated_count=len(unrelated),
        )
    if changes:
        return details, _check(
            "worktree_scope",
            "warn",
            "Release-related changes are pending commit.",
            related_count=len(related),
        )
    return details, _check("worktree_scope", "pass", "No pending tracked release changes were found.")


def _inspect_snapshot(deploy_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    archive = deploy_root / "api" / "ncs_ontology_compact.zip"
    manifest_path = deploy_root / "api" / "ncs_ontology_compact.manifest.json"
    details: dict[str, Any] = {
        "archive_path": archive.relative_to(deploy_root).as_posix(),
        "manifest_path": manifest_path.relative_to(deploy_root).as_posix(),
        "archive_exists": archive.is_file(),
        "manifest_exists": manifest_path.is_file(),
        "hard_cap_bytes": HARD_SNAPSHOT_BYTES,
        "soft_cap_bytes": SOFT_SNAPSHOT_BYTES,
    }
    if not archive.is_file() or not manifest_path.is_file():
        return details, _check("compact_snapshot", "block", "Compact archive or manifest is missing.", **details)
    details["archive_bytes"] = archive.stat().st_size
    details["manifest_bytes"] = manifest_path.stat().st_size
    try:
        manifest = _read_json(manifest_path)
        member_name = manifest.get("archive_member")
        sqlite_bytes = manifest.get("sqlite_bytes")
        if not isinstance(member_name, str) or not isinstance(sqlite_bytes, int):
            raise ValueError("manifest archive_member/sqlite_bytes is invalid")
        with zipfile.ZipFile(archive) as bundle:
            member = bundle.getinfo(member_name)
            member_count = len(bundle.infolist())
        details.update(
            {
                "manifest_schema": manifest.get("schema"),
                "archive_member": member_name,
                "sqlite_bytes": sqlite_bytes,
                "zip_member_bytes": member.file_size,
                "zip_member_count": member_count,
            }
        )
    except (OSError, ValueError, KeyError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        details["validation_error_type"] = type(exc).__name__
        return details, _check("compact_snapshot", "block", "Compact archive metadata is invalid.", **details)
    if details["archive_bytes"] >= HARD_SNAPSHOT_BYTES or sqlite_bytes >= HARD_SNAPSHOT_BYTES:
        return details, _check("compact_snapshot", "block", "Compact snapshot exceeds the 480 MB hard cap.", **details)
    if member.file_size != sqlite_bytes or member_count != 1:
        return details, _check("compact_snapshot", "block", "ZIP member metadata does not match the manifest.", **details)
    if sqlite_bytes > SOFT_SNAPSHOT_BYTES:
        return details, _check("compact_snapshot", "warn", "Compact SQLite exceeds the 460 MB soft cap.", **details)
    return details, _check("compact_snapshot", "pass", "Compact snapshot is present and within both size caps.", **details)


def _inspect_tool_contract(repo_root: Path, deploy_root: Path, deploy_config: dict[str, Any]) -> dict[str, Any]:
    root_registry = repo_root / "src" / "ncs_mcp" / "tool_registry.py"
    deploy_registry = deploy_root / "src" / "ncs_mcp" / "tool_registry.py"
    try:
        root_user = _literal_string_set(root_registry, "USER_MCP_TOOLS")
        root_advanced = _literal_string_set(root_registry, "ADVANCED_MCP_TOOLS")
        deploy_user = _literal_string_set(deploy_registry, "USER_MCP_TOOLS")
        deploy_advanced = _literal_string_set(deploy_registry, "ADVANCED_MCP_TOOLS")
    except (OSError, SyntaxError, ValueError) as exc:
        return _check("public_tool_contract", "block", "Unable to parse the public tool registry.", error_type=type(exc).__name__)
    env = deploy_config.get("env", {})
    advanced_enabled = str(env.get("NCS_MCP_ENABLE_ADVANCED_TOOLS", "0")).strip().lower() in {"1", "true", "yes", "on"}
    root_effective = root_user if advanced_enabled else root_user - root_advanced
    deploy_effective = deploy_user if advanced_enabled else deploy_user - deploy_advanced
    details = {
        "expected_count": len(EXPECTED_PUBLIC_TOOLS),
        "expected_tools": sorted(EXPECTED_PUBLIC_TOOLS),
        "root_effective_count": len(root_effective),
        "root_effective_tools": sorted(root_effective),
        "deploy_effective_count": len(deploy_effective),
        "deploy_effective_tools": sorted(deploy_effective),
        "advanced_tools_enabled": advanced_enabled,
    }
    if root_effective != EXPECTED_PUBLIC_TOOLS or deploy_effective != EXPECTED_PUBLIC_TOOLS:
        return _check("public_tool_contract", "block", "Effective public MCP surface is not the required seven-tool contract.", **details)
    return _check("public_tool_contract", "pass", "Effective public MCP surface remains the required seven tools.", **details)


def _mirror_check(check_id: str, root_path: Path, deploy_path: Path) -> dict[str, Any]:
    if not root_path.is_file() or not deploy_path.is_file():
        return _check(check_id, "block", "A required source mirror is missing.")
    root_hash = _sha256(root_path)
    deploy_hash = _sha256(deploy_path)
    details = {
        "root_path": root_path.as_posix(),
        "deploy_path": deploy_path.as_posix(),
        "root_sha256": root_hash,
        "deploy_sha256": deploy_hash,
    }
    if root_hash != deploy_hash:
        return _check(check_id, "block", "Root and deployment source mirrors differ.", **details)
    return _check(check_id, "pass", "Root and deployment source mirrors are identical.", **details)


def _inspect_vercel_config(repo_root: Path, deploy_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    root_path = repo_root / "vercel.json"
    deploy_path = deploy_root / "vercel.json"
    try:
        root_config = _read_json(root_path)
        deploy_config = _read_json(deploy_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, _check("vercel_config", "block", "Unable to load Vercel configuration.", error_type=type(exc).__name__)
    function_config = deploy_config.get("functions", {}).get("api/**/*.py", {})
    max_duration = function_config.get("maxDuration")
    include_files = str(function_config.get("includeFiles", ""))
    env_names = set(deploy_config.get("env", {}))
    missing_env = sorted(REQUIRED_VERCEL_ENV_NAMES - env_names)
    details = {
        "root_deploy_configs_equal": root_config == deploy_config,
        "max_duration_seconds": max_duration,
        "expected_max_duration_seconds": EXPECTED_MAX_DURATION,
        "archive_included": "api/ncs_ontology_compact.zip" in include_files,
        "manifest_included": "api/ncs_ontology_compact.manifest.json" in include_files,
        "required_env_names": sorted(REQUIRED_VERCEL_ENV_NAMES),
        "missing_required_env_names": missing_env,
        "env_values_reported": False,
    }
    if root_config != deploy_config or max_duration != EXPECTED_MAX_DURATION or missing_env:
        return deploy_config, _check("vercel_config", "block", "Vercel configuration contract is not satisfied.", **details)
    if not details["archive_included"] or not details["manifest_included"]:
        return deploy_config, _check("vercel_config", "block", "Compact artifacts are absent from includeFiles.", **details)
    return deploy_config, _check("vercel_config", "pass", "Vercel duration, artifact, and environment-name contracts are satisfied.", **details)


def _inspect_entrypoints(repo_root: Path, deploy_root: Path) -> dict[str, Any]:
    details: dict[str, Any] = {}
    try:
        root_entry = _read_toml(repo_root / "pyproject.toml").get("tool", {}).get("vercel", {}).get("entrypoint")
        deploy_entry = _read_toml(deploy_root / "pyproject.toml").get("tool", {}).get("vercel", {}).get("entrypoint")
        root_app = _async_app_defined(repo_root / "api" / "index.py")
        deploy_app = _async_app_defined(deploy_root / "api" / "index.py")
        details = {
            "expected_entrypoint": "api.index:app",
            "root_entrypoint": root_entry,
            "deploy_entrypoint": deploy_entry,
            "root_async_app_defined": root_app,
            "deploy_async_app_defined": deploy_app,
        }
    except (OSError, SyntaxError, tomllib.TOMLDecodeError) as exc:
        return _check("package_entrypoint", "block", "Unable to validate the package entrypoint.", error_type=type(exc).__name__)
    if root_entry != "api.index:app" or deploy_entry != "api.index:app" or not root_app or not deploy_app:
        return _check("package_entrypoint", "block", "Vercel package entrypoint is incomplete or inconsistent.", **details)
    return _check("package_entrypoint", "pass", "Both packages expose api.index:app.", **details)


def _inspect_env_documentation(repo_root: Path) -> dict[str, Any]:
    names = _env_names_from_example(repo_root / ".env.example")
    missing = sorted(REQUIRED_VERCEL_ENV_NAMES - names)
    details = {
        "source": ".env.example",
        "documented_name_count": len(names),
        "documented_required_names": sorted(REQUIRED_VERCEL_ENV_NAMES & names),
        "undocumented_vercel_only_names": missing,
        "secret_values_read": False,
        "secret_values_reported": False,
    }
    if missing:
        return _check("environment_name_documentation", "warn", "Some Vercel-only environment names are not documented in .env.example.", **details)
    return _check("environment_name_documentation", "pass", "Required environment names are documented without exposing values.", **details)


def _inspect_cli(repo_root: Path, deploy_root: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    auth_states: dict[str, bool] = {}
    for name, auth_args, check_id in (
        ("gh", ["auth", "status"], "github_cli_auth"),
        ("vercel", ["whoami"], "vercel_cli_auth"),
    ):
        executable = shutil.which(name)
        available = executable is not None
        exit_code = 127
        failure_classification = "cli_unavailable"
        if available:
            exit_code, failure_classification = _run_cli_probe(
                [str(executable), *auth_args],
                deploy_root if name == "vercel" else repo_root,
                timeout=30,
            )
        authenticated = exit_code == 0
        auth_states[name] = authenticated
        details = {
            "cli": name,
            "available": available,
            "authenticated": authenticated,
            "auth_exit_code": exit_code,
            "failure_classification": failure_classification,
            "stdout_reported": False,
            "stderr_reported": False,
        }
        if not available or exit_code != 0:
            checks.append(_check(check_id, "block", f"{name} CLI is unavailable or not authenticated.", **details))
        else:
            checks.append(_check(check_id, "pass", f"{name} CLI is available and authenticated.", **details))
    linked = (deploy_root / ".vercel" / "project.json").is_file()
    vercel_token_configured = bool(os.environ.get("VERCEL_TOKEN", "").strip())
    checks.append(
        _check(
            "vercel_project_link",
            "pass" if linked else "block",
            "Deployment staging directory is linked to a Vercel project." if linked else "Deployment staging directory is not linked to a Vercel project.",
            linked=linked,
            project_metadata_reported=False,
        )
    )
    available_paths: list[str] = []
    blocked_paths: list[str] = []
    if linked and auth_states.get("vercel"):
        available_paths.append("linked_project_cli_session")
    else:
        blocked_paths.append("linked_project_cli_session")
    if linked and vercel_token_configured:
        available_paths.append("linked_project_token_env")
    else:
        blocked_paths.append("linked_project_token_env")
    deployment_paths = {
        "project_linked": linked,
        "vercel_cli_authenticated": auth_states.get("vercel", False),
        "vercel_token_configured": vercel_token_configured,
        "available_paths": available_paths,
        "blocked_paths": blocked_paths,
        "recommended_path": available_paths[0] if available_paths else None,
        "token_value_reported": False,
        "project_metadata_reported": False,
    }
    return checks, deployment_paths


def build_report(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()
    deploy_root = repo_root / "deploy" / "vercel_mcp_app"
    checks: list[dict[str, Any]] = []

    deploy_config, vercel_check = _inspect_vercel_config(repo_root, deploy_root)
    checks.append(vercel_check)
    checks.append(
        _mirror_check(
            "server_source_mirror",
            repo_root / "src" / "ncs_mcp" / "server.py",
            deploy_root / "src" / "ncs_mcp" / "server.py",
        )
    )
    checks.append(
        _mirror_check(
            "tool_registry_mirror",
            repo_root / "src" / "ncs_mcp" / "tool_registry.py",
            deploy_root / "src" / "ncs_mcp" / "tool_registry.py",
        )
    )
    checks.append(_inspect_tool_contract(repo_root, deploy_root, deploy_config))
    snapshot_details, snapshot_check = _inspect_snapshot(deploy_root)
    checks.append(snapshot_check)
    checks.append(_inspect_entrypoints(repo_root, deploy_root))
    checks.append(_inspect_env_documentation(repo_root))
    cli_checks, deployment_paths = _inspect_cli(repo_root, deploy_root)
    checks.extend(cli_checks)
    change_details, change_check = _inspect_changes(repo_root)
    checks.append(change_check)

    blockers = [check for check in checks if check["status"] == "block"]
    warnings = [check for check in checks if check["status"] == "warn"]
    status = "blocked" if blockers else "ready_with_warnings" if warnings else "ready"
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "ready": not blockers,
        "summary": {
            "pass_count": sum(check["status"] == "pass" for check in checks),
            "warning_count": len(warnings),
            "blocker_count": len(blockers),
            "blocker_ids": [check["id"] for check in blockers],
            "warning_ids": [check["id"] for check in warnings],
        },
        "checks": checks,
        "compact_snapshot": snapshot_details,
        "worktree": change_details,
        "deployment_paths": deployment_paths,
        "safety": {
            "read_only_preflight": True,
            "git_commit_performed": False,
            "git_push_performed": False,
            "vercel_deploy_performed": False,
            "secret_files_read": False,
            "secret_values_reported": False,
        },
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# NCS Deployment Preflight",
        "",
        f"- Status: **{report['status']}**",
        f"- Ready: `{str(report['ready']).lower()}`",
        f"- Checks: {summary['pass_count']} pass / {summary['warning_count']} warning / {summary['blocker_count']} blocker",
        "- Mode: read-only; no commit, push, deploy, or secret-value output",
        "",
        "## Checks",
        "",
        "| Check | Status | Result |",
        "| --- | --- | --- |",
    ]
    for check in report["checks"]:
        summary_text = str(check["summary"]).replace("|", "\\|")
        lines.append(f"| `{check['id']}` | **{check['status']}** | {summary_text} |")
    snapshot = report["compact_snapshot"]
    deployment_paths = report["deployment_paths"]
    lines.extend(
        [
            "",
            "## Compact snapshot",
            "",
            f"- Archive: `{snapshot.get('archive_bytes', 'missing')}` bytes",
            f"- SQLite member: `{snapshot.get('sqlite_bytes', 'missing')}` bytes",
            f"- Soft / hard cap: `{snapshot.get('soft_cap_bytes')}` / `{snapshot.get('hard_cap_bytes')}` bytes",
            "",
            "## Vercel deployment paths",
            "",
            f"- Linked project: `{str(deployment_paths['project_linked']).lower()}`",
            f"- CLI session authenticated: `{str(deployment_paths['vercel_cli_authenticated']).lower()}`",
            f"- `VERCEL_TOKEN` configured: `{str(deployment_paths['vercel_token_configured']).lower()}`",
            f"- Recommended path: `{deployment_paths['recommended_path'] or 'none'}`",
            f"- Available paths: `{', '.join(deployment_paths['available_paths']) or 'none'}`",
            "",
            "## Pending change scope",
            "",
            f"- Release-related: `{len(report['worktree']['related'])}`",
            f"- Unrelated tracked: `{len(report['worktree']['unrelated'])}`",
        ]
    )
    for item in report["worktree"]["related"]:
        lines.append(f"- `{item['code']}` `{item['path']}`")
    if report["worktree"]["unrelated"]:
        lines.extend(["", "### Unrelated tracked changes"])
        for item in report["worktree"]["unrelated"]:
            lines.append(f"- `{item['code']}` `{item['path']}`")
    lines.extend(
        [
            "",
            "## Deployment decision",
            "",
            "Deployment must not start while any blocker remains." if not report["ready"] else "Preflight permits the final commit/push/deploy sequence; warnings still require review.",
            "",
        ]
    )
    return "\n".join(lines)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = build_report(args.repo_root)
    _write(args.out, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    _write(args.markdown_out, _markdown(report))
    print(json.dumps({"status": report["status"], **report["summary"]}, ensure_ascii=False))
    return 0 if report["ready"] else 2


if __name__ == "__main__":
    sys.exit(main())
