from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from scan_source_preview_artifacts import scan_tree
    from package_install_smoke import _preflight as package_source_preflight
except ModuleNotFoundError:  # pragma: no cover - package-style test import
    from scripts.scan_source_preview_artifacts import scan_tree
    from scripts.package_install_smoke import _preflight as package_source_preflight


SECRET_ENV_NAMES = {
    "OPENAI_API_KEY",
    "NCS_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
}
SECRET_ENV_MARKERS = (
    "API_KEY",
    "SERVICE_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tail(value: str | None, limit: int) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def run_command(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout_seconds: int,
    tail_chars: int,
) -> dict[str, Any]:
    started = time.monotonic()
    timed_out = False
    creationflags = 0
    popen_kwargs: dict[str, Any] = {}
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_kwargs["start_new_session"] = True
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
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
        returncode = process.returncode
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
        "command": subprocess.list2cmdline(args),
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "stdout_tail": stdout_tail,
        "stdout_truncated": stdout_truncated,
        "stderr_tail": stderr_tail,
        "stderr_truncated": stderr_truncated,
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


def _clean_environment(output_dir: Path, smoke_db: Path) -> tuple[dict[str, str], list[str]]:
    env = os.environ.copy()
    removed_names = sorted(
        name
        for name in env
        if name in SECRET_ENV_NAMES
        or any(marker in name.upper() for marker in SECRET_ENV_MARKERS)
    )
    for name in removed_names:
        env.pop(name, None)
    env["PYTHONPATH"] = str((output_dir / "src").resolve())
    env["NCS_DB_PATH"] = str(smoke_db.resolve())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env, removed_names


def _tree_fingerprint(root: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(
        (candidate for candidate in root.rglob("*") if candidate.is_file()),
        key=lambda candidate: candidate.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix()
        file_digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                file_digest.update(chunk)
        size = path.stat().st_size
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
        file_count += 1
        total_bytes += size
    return {
        "file_count": file_count,
        "total_bytes": total_bytes,
        "sha256": digest.hexdigest(),
    }


def run_preview_smoke(
    output_dir: Path,
    *,
    timeout_seconds: int = 240,
    tail_chars: int = 4000,
    full_tests: bool = False,
) -> dict[str, Any]:
    resolved_output = output_dir.resolve()
    commands: list[dict[str, Any]] = []
    source_shape_preflight = package_source_preflight(resolved_output)
    safe_for_artifact_scan = bool(
        resolved_output.is_dir()
        and source_shape_preflight.get("unsafe_link_count") == 0
        and source_shape_preflight.get("unsafe_link_scan_error") is None
    )
    preflight_scan = (
        scan_tree(resolved_output)
        if safe_for_artifact_scan
        else {
            "schema": "ncs_source_preview_secret_artifact_scan_v1",
            "ok": False,
            "skipped": True,
            "reason": "unsafe source link or reparse point detected",
        }
    )
    source_before = (
        _tree_fingerprint(resolved_output)
        if source_shape_preflight.get("ok")
        else None
    )
    command_specs: list[tuple[list[str], Path, dict[str, str]]] = []
    removed_secret_env_names: list[str] = []
    if not preflight_scan.get("ok") or not source_shape_preflight.get("ok"):
        return {
            "schema": "ncs_deployment_source_preview_runtime_smoke_v1",
            "generated_at": _now(),
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "active_db_writes": False,
            "temporary_db_writes": False,
            "db_write_scope": "none",
            "approval_claim": False,
            "api_calls": False,
            "temporary_smoke_db": False,
            "temporary_source_copies": False,
            "temporary_source_copies_cleaned_up": True,
            "source_preview_unchanged": True,
            "source_preview_before": source_before,
            "source_preview_after": source_before,
            "secrets_removed_from_subprocess_environment": [],
            "preflight_scan_ok": False,
            "preflight_scan": preflight_scan,
            "source_shape_preflight": source_shape_preflight,
            "ok": False,
            "output_dir": str(resolved_output),
            "full_tests": full_tests,
            "command_count": 0,
            "failed_command_count": 0,
            "timed_out_command_count": 0,
            "commands": [],
        }
    with tempfile.TemporaryDirectory(prefix="ncs-preview-smoke-") as tmp:
        temporary_root = Path(tmp)
        smoke_db = temporary_root / "ncs.db"
        runtime_source = temporary_root / "runtime-source"
        package_source = temporary_root / "package-source"
        shutil.copytree(resolved_output, runtime_source)
        shutil.copytree(resolved_output, package_source)
        env, removed_secret_env_names = _clean_environment(runtime_source, smoke_db)
        package_env, package_removed_names = _clean_environment(
            package_source,
            smoke_db,
        )
        removed_secret_env_names = sorted(
            set(removed_secret_env_names) | set(package_removed_names)
        )
        test_args = [sys.executable, "-m", "unittest"]
        if full_tests:
            test_args.extend(["discover", "-s", "tests", "-v"])
        else:
            test_args.extend(
                [
                    "tests.test_query_router",
                    "tests.test_ncs_mcp",
                    "tests.test_deployment_decision_reports",
                    "tests.test_operator_docs_safety",
                    "-v",
                ]
            )
        runtime_commands = [
            [sys.executable, "-m", "ncs_mcp.smoke_data", "--out", str(smoke_db)],
            [sys.executable, "scripts/ncs_harness.py", "lint"],
            test_args,
            [sys.executable, "scripts/mcp_stdio_smoke.py", "--timeout", "15"],
            [sys.executable, "scripts/mcp_http_health_smoke.py", "--timeout", "20"],
            [
                sys.executable,
                "scripts/institutional_chat_smoke.py",
                "--timeout-seconds",
                "60",
            ],
        ]
        command_specs = [
            *((command, runtime_source, env) for command in runtime_commands),
            (
                [
                    sys.executable,
                    "scripts/package_install_smoke.py",
                    "--source-preview-dir",
                    ".",
                    "--timeout-per-command",
                    "120",
                ],
                package_source,
                package_env,
            ),
            (
                [sys.executable, "scripts/ncs_harness.py", "smoke"],
                runtime_source,
                env,
            ),
        ]
        for command, command_cwd, command_env in command_specs:
            result = run_command(
                command,
                cwd=command_cwd,
                env=command_env,
                timeout_seconds=timeout_seconds,
                tail_chars=tail_chars,
            )
            commands.append(result)

    source_after = _tree_fingerprint(resolved_output)
    source_preview_unchanged = source_before == source_after
    temporary_source_copies_cleaned_up = not temporary_root.exists()

    failed_command_count = sum(item["returncode"] != 0 for item in commands)
    timed_out_command_count = sum(bool(item.get("timed_out")) for item in commands)
    ok = bool(
        resolved_output.is_dir()
        and len(commands) == len(command_specs)
        and failed_command_count == 0
        and source_preview_unchanged
        and temporary_source_copies_cleaned_up
    )
    return {
        "schema": "ncs_deployment_source_preview_runtime_smoke_v1",
        "generated_at": _now(),
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "active_db_writes": False,
        "temporary_db_writes": True,
        "db_write_scope": "ephemeral_smoke_db_only",
        "approval_claim": False,
        "api_calls": False,
        "temporary_smoke_db": True,
        "temporary_source_copies": True,
        "temporary_source_copies_cleaned_up": temporary_source_copies_cleaned_up,
        "source_preview_unchanged": source_preview_unchanged,
        "source_preview_before": source_before,
        "source_preview_after": source_after,
        "secrets_removed_from_subprocess_environment": removed_secret_env_names,
        "preflight_scan_ok": True,
        "preflight_scan": preflight_scan,
        "source_shape_preflight": source_shape_preflight,
        "ok": ok,
        "output_dir": str(resolved_output),
        "full_tests": full_tests,
        "command_count": len(commands),
        "failed_command_count": failed_command_count,
        "timed_out_command_count": timed_out_command_count,
        "commands": commands,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    lines = [
        "# Deployment Source Preview Runtime Smoke",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- output_dir: `{report.get('output_dir')}`",
        f"- full_tests: `{str(report.get('full_tests')).lower()}`",
        f"- command_count: `{report.get('command_count')}`",
        f"- failed_command_count: `{report.get('failed_command_count')}`",
        f"- timed_out_command_count: `{report.get('timed_out_command_count')}`",
        f"- api_calls: `{str(report.get('api_calls')).lower()}`",
        "",
        "## Commands",
        "",
        "| Command | Return code | Duration (s) | Timed out |",
        "| --- | ---: | ---: | --- |",
    ]
    for item in report.get("commands") or []:
        command = str(item.get("command") or "").replace("|", "\\|")
        lines.append(
            f"| `{command}` | {item.get('returncode')} | {item.get('duration_seconds')} | "
            f"{str(item.get('timed_out')).lower()} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run bounded verification commands from an exported source-preview tree."
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--timeout-per-command", type=int, default=240)
    parser.add_argument("--tail-chars", type=int, default=4000)
    parser.add_argument("--full-tests", action="store_true")
    args = parser.parse_args()

    report = run_preview_smoke(
        args.output_dir,
        timeout_seconds=max(1, args.timeout_per_command),
        tail_chars=max(100, args.tail_chars),
        full_tests=args.full_tests,
    )
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, args.markdown_out)
    print(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
