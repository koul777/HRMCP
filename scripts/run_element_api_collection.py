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
DEFAULT_LOG_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260620.jsonl"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260620_summary.json"
DEFAULT_MAJOR_CODES = [f"{value:02d}" for value in range(1, 25)]
RATE_LIMIT_PAUSE_EXIT_CODE = 75
RATE_LIMIT_COOLDOWN_SECONDS = 1800
SAFE_MAJOR_CODE_DISPLAY_NAMES = {
    "01": "\uc0ac\uc5c5\uad00\ub9ac",
    "02": "\uacbd\uc601\u00b7\ud68c\uacc4\u00b7\uc0ac\ubb34",
    "03": "\uae08\uc735\u00b7\ubcf4\ud5d8",
    "04": "\uad50\uc721\u00b7\uc790\uc5f0\u00b7\uc0ac\ud68c\uacfc\ud559",
    "05": "\ubc95\ub960\u00b7\uacbd\ucc30\u00b7\uc18c\ubc29\u00b7\uad50\ub3c4\u00b7\uad6d\ubc29",
    "06": "\ubcf4\uac74\u00b7\uc758\ub8cc",
    "07": "\uc0ac\ud68c\ubcf5\uc9c0\u00b7\uc885\uad50",
    "08": "\ubb38\ud654\u00b7\uc608\uc220\u00b7\ub514\uc790\uc778\u00b7\ubc29\uc1a1",
    "09": "\uc6b4\uc804\u00b7\uc6b4\uc1a1",
    "10": "\uc601\uc5c5\ud310\ub9e4",
    "11": "\uacbd\ube44\u00b7\uccad\uc18c",
    "12": "\uc774\uc6a9\u00b7\uc219\ubc15\u00b7\uc5ec\ud589\u00b7\uc624\ub77d\u00b7\uc2a4\ud3ec\uce20",
    "13": "\uc74c\uc2dd\uc11c\ube44\uc2a4",
    "14": "\uac74\uc124",
    "15": "\uae30\uacc4",
    "16": "\uc7ac\ub8cc",
    "17": "\ud654\ud559\u00b7\ubc14\uc774\uc624",
    "18": "\uc12c\uc720\u00b7\uc758\ubcf5",
    "19": "\uc804\uae30\u00b7\uc804\uc790",
    "20": "\uc815\ubcf4\ud1b5\uc2e0",
    "21": "\uc2dd\ud488\uac00\uacf5",
    "22": "\uc778\uc1c4\u00b7\ubaa9\uc7ac\u00b7\uac00\uad6c\u00b7\uacf5\uc608",
    "23": "\ud658\uacbd\u00b7\uc5d0\ub108\uc9c0\u00b7\uc548\uc804",
    "24": "\ub18d\ub9bc\uc5b4\uc5c5",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def current_date_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y%m%d")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def status_for_major(conn: sqlite3.Connection, major_code: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT c.major_code,
               MAX(c.major_name) AS major_name,
               COUNT(*) AS total,
               SUM(CASE WHEN ce.api_match_status = 'matched' THEN 1 ELSE 0 END) AS matched,
               SUM(CASE WHEN ce.api_match_status = 'not_collected' THEN 1 ELSE 0 END) AS not_collected,
               SUM(CASE WHEN ce.api_match_status = 'api_failed' THEN 1 ELSE 0 END) AS api_failed,
               SUM(CASE WHEN ce.api_match_status = 'no_data' THEN 1 ELSE 0 END) AS no_data
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE c.major_code = ?
        GROUP BY c.major_code
        """,
        (major_code,),
    ).fetchone()
    if row is None:
        return {
            "major_code": major_code,
            "major_name": "",
            "total": 0,
            "matched": 0,
            "not_collected": 0,
            "api_failed": 0,
            "no_data": 0,
        }
    status = {key: int(row[key]) if key not in {"major_code", "major_name"} else row[key] for key in row.keys()}
    status["major_name_display"] = SAFE_MAJOR_CODE_DISPLAY_NAMES.get(str(status["major_code"]), status["major_name"])
    return status


def all_statuses(db_path: Path, major_codes: list[str]) -> list[dict[str, Any]]:
    conn = connect(db_path)
    try:
        return [status_for_major(conn, major_code) for major_code in major_codes]
    finally:
        conn.close()


def append_event(log_path: Path, event: dict[str, Any]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"timestamp": now_iso(), **event}
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


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


def read_pid_file(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def write_pid_file(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")


def remove_pid_file_if_owned(path: Path, pid: int) -> None:
    try:
        current = int(path.read_text(encoding="utf-8").strip())
    except Exception:
        return
    if current == pid:
        path.unlink(missing_ok=True)


def active_collection_processes(exclude_pid: int) -> list[dict[str, Any]]:
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
    processes: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        if pid == exclude_pid:
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


def start_blockers(pid_file: Path, current_pid: int) -> list[dict[str, Any]]:
    blockers: dict[int, dict[str, Any]] = {}
    existing_pid = read_pid_file(pid_file)
    if existing_pid and existing_pid != current_pid:
        command_line = process_command_line(existing_pid)
        if is_collection_command(command_line):
            blockers[existing_pid] = {"pid": existing_pid, "role": "pid_file_owner"}
    for process in active_collection_processes(current_pid):
        pid = process.get("pid")
        if isinstance(pid, int):
            blockers[pid] = process
    return list(blockers.values())


def claim_pid_file(path: Path, pid: int, attempts: int = 2) -> list[dict[str, Any]]:
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(max(1, attempts)):
        try:
            with path.open("x", encoding="utf-8") as handle:
                handle.write(str(pid))
            return []
        except FileExistsError:
            blockers = start_blockers(path, pid)
            if blockers:
                return blockers
            try:
                path.unlink()
            except FileNotFoundError:
                continue
            except OSError:
                existing_pid = read_pid_file(path)
                return [{"pid": existing_pid, "role": "pid_file_unclaimable"}]
    return start_blockers(path, pid)


def parse_json_object(stdout: str) -> dict[str, Any] | None:
    text = stdout.strip()
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def batch_fully_rate_limited(result: dict[str, Any], delta_matched: int, delta_remaining: int) -> bool:
    summary = result.get("summary")
    if not isinstance(summary, dict):
        return False
    try:
        requested = int(summary.get("elements_requested") or 0)
        rate_limited = int(summary.get("elements_rate_limited") or 0)
    except (TypeError, ValueError):
        return False
    return requested > 0 and rate_limited >= requested and delta_matched <= 0 and delta_remaining <= 0


def rate_limit_batch_summary(result: dict[str, Any], major_code: str, phase: str) -> dict[str, Any]:
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    return {
        "major_code": major_code,
        "phase": phase,
        "returncode": result.get("returncode"),
        "elements_requested": summary.get("elements_requested"),
        "elements_rate_limited": summary.get("elements_rate_limited"),
        "elements_successful": summary.get("elements_successful"),
        "elements_failed": summary.get("elements_failed"),
        "elements_no_data": summary.get("elements_no_data"),
    }


def write_summary(summary_path: Path, db_path: Path, major_codes: list[str], log_path: Path) -> None:
    statuses = all_statuses(db_path, major_codes)
    totals = {
        "total": sum(item["total"] for item in statuses),
        "matched": sum(item["matched"] for item in statuses),
        "not_collected": sum(item["not_collected"] for item in statuses),
        "api_failed": sum(item["api_failed"] for item in statuses),
        "no_data": sum(item["no_data"] for item in statuses),
    }
    payload = {
        "generated_at": now_iso(),
        "database": "configured_ncs_db",
        "log_artifact": str(log_path.relative_to(PROJECT_ROOT)) if log_path.is_absolute() else str(log_path),
        "major_codes": major_codes,
        "totals": totals,
        "by_major": statuses,
    }
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def mirror_file(source: Path, target: Path) -> None:
    if source.resolve() == target.resolve():
        return
    if source.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    else:
        target.unlink(missing_ok=True)


def write_alias_outputs(args: argparse.Namespace) -> None:
    date_stamp = current_date_stamp()
    mirror_file(args.pid_file, PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}.pid")
    mirror_file(args.log_path, PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}.jsonl")
    mirror_file(
        args.summary_path,
        PROJECT_ROOT / "reports" / f"element_api_collection_all_{date_stamp}_summary.json",
    )


def run_batch(args: argparse.Namespace, major_code: str, phase: str) -> dict[str, Any]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "src" / "ncs_mcp" / "collect_api.py"),
        "--mode",
        "elements",
        "--major-code",
        major_code,
        "--element-limit",
        str(args.batch_size),
        "--timeout",
        str(args.timeout),
        "--concurrency",
        str(args.concurrency),
        "--max-retries",
        str(args.max_retries),
    ]
    if phase == "uncollected":
        command.append("--only-uncollected")
    elif phase == "failed":
        command.append("--only-failed")
    else:
        raise ValueError(f"unknown phase: {phase}")
    command_display = [
        "python",
        "src/ncs_mcp/collect_api.py",
        *command[2:],
    ]

    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    append_event(
        args.log_path,
        {
            "event": "batch_child_started",
            "phase": phase,
            "major_code": major_code,
            "child_pid": process.pid,
        },
    )
    last_heartbeat = started
    timed_out = False
    while process.poll() is None:
        elapsed_now = time.monotonic() - started
        if elapsed_now >= args.child_timeout:
            timed_out = True
            process.kill()
            break
        if time.monotonic() - last_heartbeat >= max(30, args.heartbeat_seconds):
            append_event(
                args.log_path,
                {
                    "event": "batch_heartbeat",
                    "phase": phase,
                    "major_code": major_code,
                    "child_pid": process.pid,
                    "elapsed_seconds": round(elapsed_now, 3),
                },
            )
            last_heartbeat = time.monotonic()
        time.sleep(1)
    stdout, stderr = process.communicate()
    elapsed = round(time.monotonic() - started, 3)
    if timed_out:
        return {
            "command": " ".join(command_display),
            "returncode": "timeout",
            "elapsed_seconds": elapsed,
            "stdout_tail": (stdout or "")[-4000:],
            "stderr_tail": (stderr or "")[-4000:],
            "summary": None,
        }
    parsed = parse_json_object(stdout or "")
    return {
        "command": " ".join(command_display),
        "returncode": process.returncode,
        "elapsed_seconds": elapsed,
        "stdout_tail": (stdout or "")[-4000:],
        "stderr_tail": (stderr or "")[-4000:],
        "summary": parsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume /NCS006 element API collection across NCS major codes.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--pid-file", type=Path, default=DEFAULT_PID_FILE)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--major-code", action="append", dest="major_codes")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--max-retries", type=int, default=1)
    parser.add_argument("--child-timeout", type=int, default=900)
    parser.add_argument("--heartbeat-seconds", type=int, default=60)
    parser.add_argument("--max-hours", type=float, default=72.0)
    parser.add_argument("--max-batches", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args()

    major_codes = args.major_codes or DEFAULT_MAJOR_CODES
    deadline = time.monotonic() + max(args.max_hours, 0.1) * 3600
    batches_run = 0
    pid = os.getpid()

    blockers = claim_pid_file(args.pid_file, pid)
    if not blockers:
        blockers = start_blockers(args.pid_file, pid)
    if blockers:
        remove_pid_file_if_owned(args.pid_file, pid)
        append_event(
            args.log_path,
            {
                "event": "start_blocked_active_collector_detected",
                "pid": pid,
                "blockers": blockers,
                "action": "exit_no_duplicate_db_writer",
            },
        )
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "active_element_api_collector_detected",
                    "blockers": blockers,
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 2
    write_alias_outputs(args)

    append_event(
        args.log_path,
        {
            "event": "start",
            "pid": pid,
            "major_codes": major_codes,
            "batch_size": args.batch_size,
            "timeout": args.timeout,
            "concurrency": args.concurrency,
            "max_retries": args.max_retries,
            "retry_failed": args.retry_failed,
        },
    )
    write_summary(args.summary_path, args.db_path, major_codes, args.log_path)
    write_alias_outputs(args)

    phases = ["uncollected"]
    if args.retry_failed:
        phases.append("failed")
    rate_limit_pause_required = False
    rate_limited_batches: list[dict[str, Any]] = []

    try:
        for phase in phases:
            made_progress = True
            while made_progress and time.monotonic() < deadline:
                made_progress = False
                for major_code in major_codes:
                    if time.monotonic() >= deadline:
                        break
                    if args.max_batches and batches_run >= args.max_batches:
                        write_summary(args.summary_path, args.db_path, major_codes, args.log_path)
                        append_event(args.log_path, {"event": "max_batches_reached", "batches_run": batches_run})
                        return 0
                    before = all_statuses(args.db_path, [major_code])[0]
                    remaining_key = "not_collected" if phase == "uncollected" else "api_failed"
                    if before[remaining_key] <= 0:
                        continue
                    append_event(args.log_path, {"event": "batch_start", "phase": phase, "major_code": major_code, "before": before})
                    try:
                        result = run_batch(args, major_code, phase)
                    except subprocess.TimeoutExpired as exc:
                        result = {
                            "returncode": "timeout",
                            "elapsed_seconds": args.child_timeout,
                            "stdout_tail": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
                            "stderr_tail": (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else "",
                            "summary": None,
                        }
                    after = all_statuses(args.db_path, [major_code])[0]
                    delta_matched = after["matched"] - before["matched"]
                    delta_remaining = before[remaining_key] - after[remaining_key]
                    batches_run += 1
                    made_progress = made_progress or delta_matched > 0 or delta_remaining > 0
                    append_event(
                        args.log_path,
                        {
                            "event": "batch_complete",
                            "phase": phase,
                            "major_code": major_code,
                            "before": before,
                            "after": after,
                            "delta_matched": delta_matched,
                            "delta_remaining": delta_remaining,
                            "batch_result": result,
                            "batches_run": batches_run,
                        },
                    )
                    write_summary(args.summary_path, args.db_path, major_codes, args.log_path)
                    write_alias_outputs(args)
                    if batch_fully_rate_limited(result, delta_matched, delta_remaining):
                        rate_limit_pause_required = True
                        rate_limited_batches.append(rate_limit_batch_summary(result, major_code, phase))
                        append_event(
                            args.log_path,
                            {
                                "event": "rate_limit_observed",
                                "phase": phase,
                                "major_code": major_code,
                                "reason": "batch_fully_rate_limited",
                                "action": "continue_to_next_batch",
                                "batches_run": batches_run,
                                "batch_summary": result.get("summary"),
                            },
                        )
    finally:
        write_summary(args.summary_path, args.db_path, major_codes, args.log_path)
        write_alias_outputs(args)
        stop_event = {
            "event": "stop",
            "batches_run": batches_run,
            "stop_reason": "rate_limit_pause_required" if rate_limit_pause_required else "completed_or_no_progress",
            "returncode": RATE_LIMIT_PAUSE_EXIT_CODE if rate_limit_pause_required else 0,
        }
        if rate_limit_pause_required:
            stop_event.update(
                {
                    "cooldown_recommended_seconds": RATE_LIMIT_COOLDOWN_SECONDS,
                    "rate_limited_batch_count": len(rate_limited_batches),
                    "latest_rate_limited_batches": rate_limited_batches[-5:],
                    "next_safe_action": "wait_for_rate_limit_cooldown",
                }
            )
        append_event(args.log_path, stop_event)
        remove_pid_file_if_owned(args.pid_file, pid)
        write_alias_outputs(args)

    return RATE_LIMIT_PAUSE_EXIT_CODE if rate_limit_pause_required else 0


if __name__ == "__main__":
    raise SystemExit(main())
