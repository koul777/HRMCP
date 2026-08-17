from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "ncs.db"
DEFAULT_PID_FILE = PROJECT_ROOT / "reports" / "element_api_collection_all_20260620.pid"
DEFAULT_WATCHDOG_PID_FILE = PROJECT_ROOT / "reports" / "element_api_collection_all_watchdog_20260624.pid"
DEFAULT_LOG_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_watchdog_20260624.jsonl"
DEFAULT_RUN_LOG_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260620.jsonl"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260620_summary.json"
ALL_MAJOR_CODES = [f"{value:02d}" for value in range(1, 25)]
RATE_LIMIT_PAUSE_EXIT_CODE = 75


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_date_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def append_event(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"timestamp": now_iso(), **event}, ensure_ascii=False, sort_keys=True) + "\n")


def mirror_file(source: Path, target: Path) -> None:
    if source.exists():
        if source.resolve() == target.resolve():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    else:
        target.unlink(missing_ok=True)


def write_alias_outputs(args: argparse.Namespace) -> None:
    date_stamp = current_date_stamp()
    mirror_file(args.pid_file, PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}.pid")
    mirror_file(
        args.watchdog_pid_file,
        PROJECT_ROOT / "reports" / f"element_api_collection_all_watchdog_{date_stamp}.pid",
    )
    mirror_file(args.log_path, PROJECT_ROOT / "reports" / f"element_api_collection_all_watchdog_{date_stamp}.jsonl")
    mirror_file(args.run_log_path, PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}.jsonl")
    mirror_file(args.summary_path, PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}_summary.json")


def process_alive(pid: int) -> bool:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ exit 0 }} else {{ exit 1 }}",
        ],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def process_command_line(pid: int) -> str | None:
    completed = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            (
                "$p = Get-CimInstance Win32_Process -Filter \"ProcessId = "
                f"{pid}\" -ErrorAction SilentlyContinue; "
                "if ($p) { $p.CommandLine }"
            ),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    text = completed.stdout.strip()
    return text or None


def is_collection_command(command_line: str | None) -> bool:
    if not command_line:
        return False
    return "run_element_api_collection.py" in command_line or "collect_api.py --mode elements" in command_line


def is_watchdog_command(command_line: str | None) -> bool:
    if not command_line:
        return False
    return "watch_element_api_collection.py" in command_line


def collection_process_alive(pid: int) -> bool:
    return is_collection_command(process_command_line(pid))


def watchdog_process_alive(pid: int) -> bool:
    return is_watchdog_command(process_command_line(pid))


def active_collection_processes() -> list[dict[str, Any]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*run_element_api_collection.py*' -or "
            "$_.CommandLine -like '*collect_api.py --mode elements*' } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Depth 3"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        return []
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    current_pid = os.getpid()
    processes: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if pid == current_pid:
            continue
        command_line = str(item.get("CommandLine") or "")
        if "run_element_api_collection.py" in command_line:
            role = "parent_runner"
        elif "collect_api.py --mode elements" in command_line:
            role = "child_collector"
        else:
            role = "collector"
        processes.append({"pid": pid, "role": role})
    return processes


def active_watchdog_processes(exclude_pid: int) -> list[dict[str, Any]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*watch_element_api_collection.py*' } | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Depth 3"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except Exception:
        return []
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    processes: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if pid == exclude_pid:
            continue
        processes.append({"pid": pid, "role": "watchdog"})
    return processes


def defer_while_active_collectors(args: argparse.Namespace, deadline: float, reason: str) -> bool:
    reported_signature: tuple[tuple[Any, Any], ...] | None = None
    deferred = False
    while time.monotonic() < deadline:
        active = active_collection_processes()
        if not active:
            return deferred
        deferred = True
        signature = tuple(sorted((item.get("pid"), item.get("role")) for item in active))
        if signature != reported_signature:
            append_event(
                args.log_path,
                {
                    "event": "active_collector_detected",
                    "reason": reason,
                    "active_collectors": active,
                    "action": "defer_sweep_no_duplicate_db_writer",
                },
            )
            reported_signature = signature
        time.sleep(max(5, args.poll_seconds))
    return deferred


def read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def remove_pid_file_if_owned(path: Path, pid: int) -> None:
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if current == pid:
        path.unlink(missing_ok=True)


def watchdog_start_blockers(pid_file: Path, current_pid: int) -> list[dict[str, Any]]:
    blockers: dict[int, dict[str, Any]] = {}
    existing_pid = read_pid(pid_file)
    if existing_pid and existing_pid != current_pid and watchdog_process_alive(existing_pid):
        blockers[existing_pid] = {"pid": existing_pid, "role": "watchdog_pid_file_owner"}
    for process in active_watchdog_processes(current_pid):
        pid = process.get("pid")
        if isinstance(pid, int):
            blockers[pid] = process
    return list(blockers.values())


def claim_watchdog_pid_file(path: Path, pid: int, attempts: int = 2) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(max(1, attempts)):
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(str(pid))
            return []
        except FileExistsError:
            blockers = watchdog_start_blockers(path, pid)
            if blockers:
                return blockers
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                existing_pid = read_pid(path)
                return [{"pid": existing_pid, "role": "watchdog_pid_file_unclaimable"}]
    return watchdog_start_blockers(path, pid)


def sleep_with_cooldown_heartbeat(args: argparse.Namespace, seconds: float, sweep: int, deadline: float) -> None:
    remaining = max(0.0, min(seconds, max(0.0, deadline - time.monotonic())))
    heartbeat_seconds = max(5.0, float(args.cooldown_heartbeat_seconds))
    while remaining > 0:
        append_event(
            args.log_path,
            {
                "event": "rate_limit_cooldown_heartbeat",
                "sweep": sweep,
                "cooldown_remaining_seconds": round(remaining, 3),
            },
        )
        chunk = min(remaining, heartbeat_seconds)
        time.sleep(chunk)
        remaining = round(remaining - chunk, 3)


def read_jsonl_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                events.append(payload)
    return events


def parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.timestamp()


def pending_rate_limit_cooldown(events: list[dict[str, Any]], now_epoch: float | None = None) -> dict[str, Any] | None:
    resume_events = {
        "sweep_needed",
        "full_sweep_start",
        "full_sweep_complete",
        "sweep_counts_after",
        "complete",
        "stop_with_remaining",
    }
    resumed_after_pause = False
    for event in reversed(events):
        event_name = event.get("event")
        if event_name in resume_events:
            resumed_after_pause = True
            continue
        if event_name != "rate_limit_pause_from_sweep":
            continue
        if resumed_after_pause:
            return None
        pause_epoch = parse_timestamp(event.get("timestamp"))
        if pause_epoch is None:
            return None
        try:
            cooldown_seconds = int(event.get("cooldown_seconds") or 0)
        except (TypeError, ValueError):
            cooldown_seconds = 0
        if cooldown_seconds <= 0:
            return None
        now_value = datetime.now(timezone.utc).timestamp() if now_epoch is None else now_epoch
        cooldown_until_epoch = pause_epoch + cooldown_seconds
        remaining = max(0.0, cooldown_until_epoch - now_value)
        if remaining <= 0:
            return None
        return {
            "sweep": event.get("sweep"),
            "returncode": event.get("returncode"),
            "latest_pause_at": event.get("timestamp"),
            "cooldown_seconds": cooldown_seconds,
            "cooldown_remaining_seconds": round(remaining, 3),
            "cooldown_until": datetime.fromtimestamp(cooldown_until_epoch, timezone.utc)
            .replace(microsecond=0)
            .isoformat(),
        }
    return None


def active_batch_from_log(path: Path) -> dict[str, Any] | None:
    events = read_jsonl_events(path)
    if not events:
        return None
    latest_start_index = -1
    for index, event in enumerate(events):
        if event.get("event") == "start":
            latest_start_index = index
    scoped = events[latest_start_index:] if latest_start_index >= 0 else events
    latest_batch_start = next((event for event in reversed(scoped) if event.get("event") == "batch_start"), None)
    latest_batch_complete = next((event for event in reversed(scoped) if event.get("event") == "batch_complete"), None)
    if not latest_batch_start:
        return None
    start_ts = str(latest_batch_start.get("timestamp") or "")
    complete_ts = str(latest_batch_complete.get("timestamp") or "") if latest_batch_complete else ""
    start_key = (latest_batch_start.get("phase"), latest_batch_start.get("major_code"))
    complete_key = (
        latest_batch_complete.get("phase") if latest_batch_complete else None,
        latest_batch_complete.get("major_code") if latest_batch_complete else None,
    )
    if latest_batch_complete and complete_ts >= start_ts and complete_key == start_key:
        return None
    timestamp_epoch = parse_timestamp(start_ts)
    if timestamp_epoch is None:
        return latest_batch_start
    return {
        **latest_batch_start,
        "age_seconds": round(datetime.now(timezone.utc).timestamp() - timestamp_epoch, 3),
    }


def remaining_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN api_match_status = 'matched' THEN 1 ELSE 0 END) AS matched,
                   SUM(CASE WHEN api_match_status = 'not_collected' THEN 1 ELSE 0 END) AS not_collected,
                   SUM(CASE WHEN api_match_status = 'api_failed' THEN 1 ELSE 0 END) AS api_failed,
                   SUM(CASE WHEN api_match_status = 'no_data' THEN 1 ELSE 0 END) AS no_data
            FROM competency_elements
            """
        ).fetchone()
    finally:
        conn.close()
    return {
        "total": int(row[0] or 0),
        "matched": int(row[1] or 0),
        "not_collected": int(row[2] or 0),
        "api_failed": int(row[3] or 0),
        "no_data": int(row[4] or 0),
    }


def run_full_sweep(args: argparse.Namespace) -> int:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_element_api_collection.py"),
        "--batch-size",
        str(args.batch_size),
        "--timeout",
        str(args.timeout),
        "--concurrency",
        str(args.concurrency),
        "--max-retries",
        str(args.max_retries),
        "--child-timeout",
        str(args.child_timeout),
        "--max-hours",
        str(args.sweep_max_hours),
        "--retry-failed",
        "--pid-file",
        str(args.pid_file),
        "--log-path",
        str(args.run_log_path),
        "--summary-path",
        str(args.summary_path),
    ]
    for major_code in ALL_MAJOR_CODES:
        command.extend(["--major-code", major_code])
    append_event(args.log_path, {"event": "full_sweep_start", "command": "python scripts/run_element_api_collection.py ..."})
    write_alias_outputs(args)
    completed = subprocess.run(command, cwd=PROJECT_ROOT)
    append_event(args.log_path, {"event": "full_sweep_complete", "returncode": completed.returncode})
    write_alias_outputs(args)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Continue NCS006 element collection with full 01-24 sweeps after an active run exits.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    parser.add_argument("--watchdog-pid-file", type=Path, default=DEFAULT_WATCHDOG_PID_FILE)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--run-log-path", type=Path, default=DEFAULT_RUN_LOG_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--max-hours", type=float, default=168.0)
    parser.add_argument("--sweep-max-hours", type=float, default=168.0)
    parser.add_argument("--max-sweeps", type=int, default=3)
    parser.add_argument("--sleep-between-sweeps", type=int, default=3600)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=2)
    parser.add_argument("--child-timeout", type=int, default=900)
    parser.add_argument("--stale-batch-buffer-seconds", type=int, default=120)
    parser.add_argument("--cooldown-heartbeat-seconds", type=int, default=600)
    parser.add_argument(
        "--rate-limit-cooldown-seconds",
        type=int,
        default=1800,
        help="When a sweep pauses on full rate-limit responses, wait this long before the next sweep.",
    )
    args = parser.parse_args()

    deadline = time.monotonic() + args.max_hours * 3600
    watchdog_pid = os.getpid()
    blockers = claim_watchdog_pid_file(args.watchdog_pid_file, watchdog_pid)
    if blockers:
        remove_pid_file_if_owned(args.watchdog_pid_file, watchdog_pid)
        append_event(
            args.log_path,
            {
                "event": "watch_start_blocked_active_watchdog_detected",
                "pid": watchdog_pid,
                "blockers": blockers,
                "action": "exit_no_duplicate_watchdog",
            },
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "active_element_api_watchdog_detected",
                    "blockers": blockers,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 2

    try:
        append_event(args.log_path, {"event": "watch_start", "pid": watchdog_pid, "initial_counts": remaining_counts(args.db_path)})
        write_alias_outputs(args)

        pid = read_pid(args.pid_file)
        if pid and collection_process_alive(pid):
            append_event(args.log_path, {"event": "watch_initial_pid", "pid": pid})
            stale_signature: tuple[Any, Any, Any] | None = None
            while time.monotonic() < deadline and collection_process_alive(pid):
                active_batch = active_batch_from_log(args.run_log_path)
                if active_batch:
                    age_seconds = float(active_batch.get("age_seconds") or 0)
                    stale_after = args.child_timeout + max(0, args.stale_batch_buffer_seconds)
                    signature = (
                        active_batch.get("timestamp"),
                        active_batch.get("phase"),
                        active_batch.get("major_code"),
                    )
                    if age_seconds >= stale_after and signature != stale_signature:
                        append_event(
                            args.log_path,
                            {
                                "event": "stale_batch_detected",
                                "pid": pid,
                                "stale_after_seconds": stale_after,
                                "active_batch": active_batch,
                                "action": "report_only_no_restart_while_parent_alive",
                            },
                        )
                        stale_signature = signature
                time.sleep(max(5, args.poll_seconds))
            append_event(args.log_path, {"event": "initial_process_finished_or_deadline", "pid": pid, "counts": remaining_counts(args.db_path)})
            write_alias_outputs(args)
        elif pid:
            append_event(args.log_path, {"event": "watch_stale_pid", "pid": pid, "action": "check_active_collectors_before_sweep"})
            write_alias_outputs(args)
        else:
            append_event(args.log_path, {"event": "watch_missing_pid", "action": "check_active_collectors_before_sweep"})
            write_alias_outputs(args)

        defer_while_active_collectors(args, deadline, "missing_or_stale_pid_guard")
        pending_cooldown = pending_rate_limit_cooldown(read_jsonl_events(args.log_path))
        if pending_cooldown:
            append_event(
                args.log_path,
                {
                    "event": "rate_limit_cooldown_recovered_from_log",
                    "cooldown_seconds": pending_cooldown["cooldown_seconds"],
                    "cooldown_remaining_seconds": pending_cooldown["cooldown_remaining_seconds"],
                    "cooldown_until": pending_cooldown["cooldown_until"],
                    "latest_pause_at": pending_cooldown["latest_pause_at"],
                    "latest_pause_sweep": pending_cooldown.get("sweep"),
                    "latest_pause_returncode": pending_cooldown.get("returncode"),
                    "action": "sleep_before_next_sweep",
                },
            )
            write_alias_outputs(args)
            sleep_with_cooldown_heartbeat(
                args,
                float(pending_cooldown["cooldown_remaining_seconds"]),
                int(pending_cooldown.get("sweep") or 0),
                deadline,
            )

        sweeps = 0
        while time.monotonic() < deadline and sweeps < args.max_sweeps:
            if defer_while_active_collectors(args, deadline, "pre_sweep_guard"):
                continue
            counts = remaining_counts(args.db_path)
            if counts["not_collected"] <= 0 and counts["api_failed"] <= 0:
                append_event(args.log_path, {"event": "complete", "counts": counts})
                write_alias_outputs(args)
                return 0
            sweeps += 1
            append_event(args.log_path, {"event": "sweep_needed", "sweep": sweeps, "counts": counts})
            write_alias_outputs(args)
            code = run_full_sweep(args)
            counts_after = remaining_counts(args.db_path)
            append_event(args.log_path, {"event": "sweep_counts_after", "sweep": sweeps, "returncode": code, "counts": counts_after})
            write_alias_outputs(args)
            if counts_after["not_collected"] <= 0 and counts_after["api_failed"] <= 0:
                append_event(args.log_path, {"event": "complete", "counts": counts_after})
                write_alias_outputs(args)
                return 0
            if code == RATE_LIMIT_PAUSE_EXIT_CODE:
                pause_seconds = max(0.0, float(args.rate_limit_cooldown_seconds))
                append_event(
                    args.log_path,
                    {
                        "event": "rate_limit_pause_from_sweep",
                        "sweep": sweeps,
                        "returncode": code,
                        "counts": counts_after,
                        "cooldown_seconds": pause_seconds,
                        "action": "sleep_before_next_sweep",
                    },
                )
                write_alias_outputs(args)
                sleep_with_cooldown_heartbeat(args, pause_seconds, sweeps, deadline)
                continue
            if code != 0:
                return code
            if sweeps < args.max_sweeps:
                time.sleep(max(0, args.sleep_between_sweeps))

        append_event(args.log_path, {"event": "stop_with_remaining", "counts": remaining_counts(args.db_path), "sweeps": sweeps})
        write_alias_outputs(args)
        return 0
    finally:
        remove_pid_file_if_owned(args.watchdog_pid_file, watchdog_pid)
        write_alias_outputs(args)


if __name__ == "__main__":
    raise SystemExit(main())
