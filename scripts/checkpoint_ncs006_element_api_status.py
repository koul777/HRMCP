from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "processed" / "ncs.db"
DEFAULT_LOG_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260624.jsonl"
DEFAULT_SUMMARY_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_20260624_summary.json"
DEFAULT_WATCH_LOG_PATH = PROJECT_ROOT / "reports" / "element_api_collection_all_watchdog_20260624.jsonl"
DEFAULT_OUT = PROJECT_ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "checkpoint_ncs006_element_api_status_20260624.md"

MAJOR_CODE_DISPLAY_NAMES = {
    "01": "사업관리",
    "02": "경영·회계·사무",
    "03": "금융·보험",
    "04": "교육·자연·사회과학",
    "05": "법률·경찰·소방·교도·국방",
    "06": "보건·의료",
    "07": "사회복지·종교",
    "08": "문화·예술·디자인·방송",
    "09": "운전·운송",
    "10": "영업판매",
    "11": "경비·청소",
    "12": "이용·숙박·여행·오락·스포츠",
    "13": "음식서비스",
    "14": "건설",
    "15": "기계",
    "16": "재료",
    "17": "화학·바이오",
    "18": "섬유·의복",
    "19": "전기·전자",
    "20": "정보통신",
    "21": "식품가공",
    "22": "인쇄·목재·가구·공예",
    "23": "환경·에너지·안전",
    "24": "농림어업",
}


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


def portable_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def element_counts(db_path: Path) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN api_match_status = 'matched' THEN 1 ELSE 0 END) AS matched,
                   SUM(CASE WHEN api_match_status = 'not_collected' THEN 1 ELSE 0 END) AS not_collected,
                   SUM(CASE WHEN api_match_status = 'api_failed' THEN 1 ELSE 0 END) AS api_failed,
                   SUM(CASE WHEN api_match_status = 'no_data' THEN 1 ELSE 0 END) AS no_data
            FROM competency_elements
            """
        ).fetchone()
        rows = conn.execute(
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
            GROUP BY c.major_code
            ORDER BY c.major_code
            """
        ).fetchall()
    finally:
        conn.close()

    def status_payload(row: sqlite3.Row) -> dict[str, Any]:
        total = int(row["total"] or 0)
        matched = int(row["matched"] or 0)
        return {
            "total": total,
            "matched": matched,
            "not_collected": int(row["not_collected"] or 0),
            "api_failed": int(row["api_failed"] or 0),
            "no_data": int(row["no_data"] or 0),
            "matched_ratio": round(matched / total, 6) if total else 0.0,
        }

    by_major = []
    for row in rows:
        item = {
            "major_code": row["major_code"],
            "major_name": row["major_name"],
            "major_name_display": SAFE_MAJOR_CODE_DISPLAY_NAMES.get(str(row["major_code"]), row["major_name"]),
            **status_payload(row),
        }
        by_major.append(item)

    total_count = int(totals["total"] or 0)
    matched_count = int(totals["matched"] or 0)
    return {
        "totals": {
            "total": total_count,
            "matched": matched_count,
            "not_collected": int(totals["not_collected"] or 0),
            "api_failed": int(totals["api_failed"] or 0),
            "no_data": int(totals["no_data"] or 0),
            "matched_ratio": round(matched_count / total_count, 6) if total_count else 0.0,
        },
        "by_major": by_major,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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


def event_digest(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {
            "event_count": 0,
            "latest_event": None,
            "latest_run": None,
            "latest_batch_start": None,
            "latest_batch_complete": None,
            "active_or_incomplete_batch": None,
            "completed_batches_since_latest_start": 0,
            "rate_limited_since_latest_start": 0,
        }

    latest_start_index = -1
    for index, event in enumerate(events):
        if event.get("event") == "start":
            latest_start_index = index
    latest_events = events[latest_start_index:] if latest_start_index >= 0 else events
    latest_batch_start = next((event for event in reversed(latest_events) if event.get("event") == "batch_start"), None)
    latest_batch_complete = next((event for event in reversed(latest_events) if event.get("event") == "batch_complete"), None)

    active_or_incomplete = None
    if latest_batch_start:
        start_ts = str(latest_batch_start.get("timestamp") or "")
        complete_ts = str(latest_batch_complete.get("timestamp") or "") if latest_batch_complete else ""
        start_key = (latest_batch_start.get("phase"), latest_batch_start.get("major_code"))
        complete_key = (
            latest_batch_complete.get("phase") if latest_batch_complete else None,
            latest_batch_complete.get("major_code") if latest_batch_complete else None,
        )
        if not latest_batch_complete or complete_ts < start_ts or complete_key != start_key:
            active_or_incomplete = latest_batch_start

    completed_batches = [event for event in latest_events if event.get("event") == "batch_complete"]
    rate_limited = 0
    for event in completed_batches:
        summary = ((event.get("batch_result") or {}).get("summary") or {})
        rate_limited += int(summary.get("elements_rate_limited") or 0)

    return {
        "event_count": len(events),
        "latest_event": events[-1],
        "latest_run": next((event for event in reversed(events) if event.get("event") == "start"), None),
        "latest_batch_start": latest_batch_start,
        "latest_batch_complete": latest_batch_complete,
        "active_or_incomplete_batch": active_or_incomplete,
        "completed_batches_since_latest_start": len(completed_batches),
        "rate_limited_since_latest_start": rate_limited,
    }


def progress_forecast(
    events: list[dict[str, Any]],
    totals: dict[str, Any],
    sample_size: int = 20,
) -> dict[str, Any]:
    completed = [event for event in events if event.get("event") == "batch_complete"]
    recent = completed[-sample_size:]
    if not recent:
        return {
            "sample_batch_count": 0,
            "average_elapsed_seconds": None,
            "average_requested_per_batch": None,
            "average_matched_per_batch": None,
            "remaining_api_targets": int(totals.get("not_collected") or 0) + int(totals.get("api_failed") or 0),
            "estimated_remaining_batches": None,
            "estimated_remaining_hours": None,
        }

    elapsed_values: list[float] = []
    requested_values: list[int] = []
    matched_values: list[int] = []
    rate_limited_values: list[int] = []
    for event in recent:
        result = event.get("batch_result") if isinstance(event.get("batch_result"), dict) else {}
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        elapsed = result.get("elapsed_seconds")
        if isinstance(elapsed, (int, float)) and elapsed > 0:
            elapsed_values.append(float(elapsed))
        requested = summary.get("elements_requested")
        if isinstance(requested, int) and requested > 0:
            requested_values.append(requested)
        matched = event.get("delta_matched")
        if isinstance(matched, int) and matched >= 0:
            matched_values.append(matched)
        rate_limited = summary.get("elements_rate_limited")
        if isinstance(rate_limited, int):
            rate_limited_values.append(rate_limited)

    average_elapsed = sum(elapsed_values) / len(elapsed_values) if elapsed_values else None
    average_requested = sum(requested_values) / len(requested_values) if requested_values else None
    average_matched = sum(matched_values) / len(matched_values) if matched_values else None
    remaining_api_targets = int(totals.get("not_collected") or 0) + int(totals.get("api_failed") or 0)
    estimated_batches = math.ceil(remaining_api_targets / average_requested) if average_requested else None
    estimated_hours = (
        round((estimated_batches * average_elapsed) / 3600, 3)
        if estimated_batches is not None and average_elapsed is not None
        else None
    )

    return {
        "sample_batch_count": len(recent),
        "average_elapsed_seconds": round(average_elapsed, 3) if average_elapsed is not None else None,
        "average_requested_per_batch": round(average_requested, 3) if average_requested is not None else None,
        "average_matched_per_batch": round(average_matched, 3) if average_matched is not None else None,
        "rate_limited_in_sample": sum(rate_limited_values),
        "remaining_api_targets": remaining_api_targets,
        "estimated_remaining_batches": estimated_batches,
        "estimated_remaining_hours": estimated_hours,
        "estimate_note": "Rough forecast from recent completed batches; retry-failed phase can change actual runtime.",
    }


def process_snapshot() -> list[dict[str, Any]]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | "
            "Where-Object { $_.CommandLine -like '*collect_api.py*' -or "
            "$_.CommandLine -like '*run_element_api_collection.py*' -or "
            "$_.CommandLine -like '*watch_element_api_collection.py*' } | "
            "Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Depth 3"
        ),
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
    except Exception as exc:
        return [{"error": str(exc)}]
    text = completed.stdout.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [{"error": "process_json_decode_failed", "stdout_tail": text[-1000:]}]
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    return [
        {
            "pid": item.get("ProcessId"),
            "creation_date": item.get("CreationDate"),
            "role": process_role(str(item.get("CommandLine") or "")),
        }
        for item in payload
        if isinstance(item, dict)
    ]


def process_role(command: str) -> str:
    lower = command.lower()
    if "watch_element_api_collection.py" in lower:
        return "watchdog"
    if "run_element_api_collection.py" in lower:
        return "parent_runner"
    if "collect_api.py" in lower and "--mode elements" in lower:
        return "child_collector"
    return "collector_related"


def redact_command(command: str) -> str:
    secret_flag = re.compile(
        r"(?i)(--(?:service[-_]?key|api[-_]?key|token|secret|password)(?:=|\s+))([^\s]+)"
    )
    env_secret = re.compile(
        r"(?i)\b([A-Z0-9_]*(?:SERVICE_KEY|API_KEY|TOKEN|SECRET|PASSWORD)[A-Z0-9_]*=)([^\s;]+)"
    )
    command = secret_flag.sub(lambda match: f"{match.group(1)}[REDACTED]", command)
    command = env_secret.sub(lambda match: f"{match.group(1)}[REDACTED]", command)
    return command


def strip_command_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_command_fields(child)
            for key, child in value.items()
            if str(key).lower() not in {"command", "commandline", "command_line", "process_command_line"}
        }
    if isinstance(value, list):
        return [strip_command_fields(child) for child in value]
    return value


def process_role_counts(processes: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "parent_runner": 0,
        "watchdog": 0,
        "child_collector": 0,
        "collector_related": 0,
    }
    for process in processes:
        role = str(process.get("role") or "collector_related")
        counts[role] = counts.get(role, 0) + 1
    return counts


def artifact_info(path: Path) -> dict[str, Any]:
    exists = path.exists()
    display_path = portable_path(path)
    return {
        "path": display_path,
        "exists": exists,
        "bytes": path.stat().st_size if exists else 0,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0).isoformat() if exists else None,
    }


def active_batch_monitoring(
    run_digest: dict[str, Any],
    generated_at: str,
    child_timeout_seconds: int,
) -> dict[str, Any]:
    active = run_digest.get("active_or_incomplete_batch") or {}
    generated_dt = parse_iso_datetime(generated_at)
    started_dt = parse_iso_datetime(str(active.get("timestamp") or ""))
    age_seconds = None
    if generated_dt and started_dt:
        age_seconds = max(0, int((generated_dt - started_dt).total_seconds()))

    stale = age_seconds is not None and age_seconds > child_timeout_seconds
    if not active:
        status = "idle_or_between_batches"
    elif stale:
        status = "timeout_exceeded_inspect_child"
    else:
        status = "within_child_timeout"

    return {
        "status": status,
        "active_batch_age_seconds": age_seconds,
        "child_timeout_seconds": child_timeout_seconds,
        "timeout_exceeded": stale,
        "active_major_code": active.get("major_code"),
        "active_phase": active.get("phase"),
    }


def rate_limit_cooldown_monitoring(
    watch_events: list[dict[str, Any]],
    generated_at: str,
    default_cooldown_seconds: int = 3600,
) -> dict[str, Any]:
    resume_events = {
        "start",
        "batch_start",
        "batch_complete",
        "sweep_needed",
        "full_sweep_start",
        "full_sweep_complete",
        "sweep_counts_after",
        "complete",
        "stop_with_remaining",
    }
    pause_events = {"rate_limit_pause_from_sweep", "rate_limit_pause"}
    resumed_after_pause = False
    pause = None
    for event in reversed(watch_events):
        event_name = event.get("event")
        if event_name in resume_events:
            resumed_after_pause = True
            continue
        if event_name not in pause_events:
            continue
        pause = event
        break
    if not pause:
        return {
            "status": "no_rate_limit_cooldown",
            "latest_pause_at": None,
            "cooldown_seconds": None,
            "cooldown_until": None,
            "cooldown_remaining_seconds": None,
            "latest_pause_sweep": None,
            "latest_pause_returncode": None,
        }

    if resumed_after_pause:
        return {
            "status": "cooldown_consumed_by_later_activity",
            "latest_pause_at": pause.get("timestamp"),
            "cooldown_seconds": int(pause.get("cooldown_seconds") or default_cooldown_seconds),
            "cooldown_until": None,
            "cooldown_remaining_seconds": 0,
            "latest_pause_sweep": pause.get("sweep"),
            "latest_pause_returncode": pause.get("returncode"),
        }

    generated_dt = parse_iso_datetime(generated_at)
    pause_dt = parse_iso_datetime(str(pause.get("timestamp") or ""))
    cooldown_seconds = int(pause.get("cooldown_seconds") or default_cooldown_seconds)
    cooldown_until_dt = pause_dt + timedelta(seconds=cooldown_seconds) if pause_dt else None
    remaining_seconds = None
    status = "cooldown_unknown"
    if generated_dt and cooldown_until_dt:
        remaining_seconds = max(0, int((cooldown_until_dt - generated_dt).total_seconds()))
        status = "cooldown_active" if remaining_seconds > 0 else "cooldown_elapsed_or_retry_due"

    return {
        "status": status,
        "latest_pause_at": pause.get("timestamp"),
        "cooldown_seconds": cooldown_seconds,
        "cooldown_until": cooldown_until_dt.replace(microsecond=0).isoformat() if cooldown_until_dt else None,
        "cooldown_remaining_seconds": remaining_seconds,
        "latest_pause_sweep": pause.get("sweep"),
        "latest_pause_returncode": pause.get("returncode"),
    }


def next_safe_collection_action(
    role_counts: dict[str, int],
    monitoring: dict[str, Any],
    cooldown: dict[str, Any],
    totals: dict[str, Any],
) -> dict[str, Any]:
    remaining = int(totals.get("not_collected") or 0) + int(totals.get("api_failed") or 0)
    parent_count = int(role_counts.get("parent_runner") or 0)
    child_count = int(role_counts.get("child_collector") or 0)
    watchdog_count = int(role_counts.get("watchdog") or 0)
    cooldown_status = str(cooldown.get("status") or "")
    monitor_status = str(monitoring.get("status") or "")

    base = {
        "remaining_api_targets": remaining,
        "should_start_collector": False,
        "should_start_watchdog": False,
        "avoid_duplicate_collector": True,
        "api_call_allowed_now": False,
        "allowed_automation": [
            "refresh_read_only_checkpoints",
            "run_report_only_agent_queue_items",
            "run_lint_smoke_or_read_only_quality_reports",
        ],
        "blocked_automation": [
            "start_duplicate_ncs006_collector",
            "retry_qualification_api_during_ncs006_cooldown",
            "write_human_reviewed_accepted_or_reviewed_without_human_decision",
        ],
    }
    if remaining <= 0:
        return {
            **base,
            "status": "complete_no_collection_needed",
            "summary": "All NCS006 element API targets are already resolved.",
        }
    if parent_count or child_count:
        return {
            **base,
            "status": "collector_active_monitor_only",
            "summary": "A collection process is active; monitor progress and do not start another collector.",
        }
    if cooldown_status == "cooldown_active":
        return {
            **base,
            "status": "wait_for_rate_limit_cooldown",
            "summary": "Rate-limit cooldown is active; keep the watchdog running and run only read-only/report-only work.",
            "cooldown_until": cooldown.get("cooldown_until"),
            "cooldown_remaining_seconds": cooldown.get("cooldown_remaining_seconds"),
        }
    if monitor_status == "timeout_exceeded_inspect_child":
        return {
            **base,
            "status": "inspect_stale_batch_before_retry",
            "summary": "The latest batch appears stale; inspect child process state before any retry.",
        }
    if watchdog_count:
        return {
            **base,
            "status": "watchdog_active_observe_next_sweep",
            "summary": "The watchdog is active; let it own the next sweep and avoid manual duplicate collection.",
        }
    if cooldown_status in {"cooldown_elapsed_or_retry_due", "cooldown_consumed_by_later_activity", "no_rate_limit_cooldown"}:
        return {
            **base,
            "status": "start_guarded_watchdog_if_no_active_process",
            "summary": "No active collector/watchdog was detected; start only the PID-guarded watchdog, not an ad hoc collector.",
            "should_start_watchdog": True,
        }
    return {
        **base,
        "status": "manual_inspection_required",
        "summary": "Collection state is ambiguous; inspect watchdog logs and process state before calling the API.",
    }


def build_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    run_events = read_jsonl(args.log_path)
    watch_events = read_jsonl(args.watch_log_path)
    generated_at = now_iso()
    run_digest = strip_command_fields(event_digest(run_events))
    child_timeout_seconds = int(getattr(args, "child_timeout_seconds", 900) or 900)
    element_status = element_counts(args.db_path)
    processes = process_snapshot()
    role_counts = process_role_counts(processes)
    monitoring = active_batch_monitoring(
        run_digest,
        generated_at,
        child_timeout_seconds,
    )
    cooldown_events = sorted(
        run_events + watch_events,
        key=lambda event: parse_iso_datetime(str(event.get("timestamp") or "")) or datetime.min.replace(tzinfo=timezone.utc),
    )
    cooldown = rate_limit_cooldown_monitoring(cooldown_events, generated_at)
    return {
        "schema": "ncs006_element_api_checkpoint_v1",
        "generated_at": generated_at,
        "database_ref": "configured_ncs_database",
        "collection_processes": processes,
        "collection_process_role_counts": role_counts,
        "process_roles": role_counts,
        "element_api_status": element_status,
        "totals": element_status["totals"],
        "run_log": {
            "path": portable_path(args.log_path),
            **run_digest,
        },
        "watchdog_log": {
            "path": portable_path(args.watch_log_path),
            **strip_command_fields(event_digest(watch_events)),
        },
        "monitoring": monitoring,
        "rate_limit_cooldown": cooldown,
        "next_safe_action": next_safe_collection_action(
            role_counts,
            monitoring,
            cooldown,
            element_status["totals"],
        ),
        "throughput_forecast": progress_forecast(run_events, element_status["totals"]),
        "artifacts": {
            "runner_log": artifact_info(args.log_path),
            "runner_summary": artifact_info(args.summary_path),
            "watchdog_log": artifact_info(args.watch_log_path),
        },
        "policy": {
            "read_only_checkpoint": True,
            "db_writes": False,
            "status_updates": False,
            "secrets_included": False,
            "human_review_status_updates": False,
        },
    }


def write_markdown(path: Path, checkpoint: dict[str, Any]) -> None:
    totals = checkpoint["element_api_status"]["totals"]
    latest = checkpoint["run_log"].get("latest_event") or {}
    active = checkpoint["run_log"].get("active_or_incomplete_batch") or {}
    processes = checkpoint.get("collection_processes") or []
    role_counts = checkpoint.get("collection_process_role_counts") or {}
    monitoring = checkpoint.get("monitoring") or {}
    cooldown = checkpoint.get("rate_limit_cooldown") or {}
    next_action = checkpoint.get("next_safe_action") or {}
    forecast = checkpoint.get("throughput_forecast") or {}
    lines = [
        "# NCS006 Element API Checkpoint",
        "",
        f"- Generated at: `{checkpoint['generated_at']}`",
        f"- Matched: `{totals['matched']:,} / {totals['total']:,}` ({totals['matched_ratio']:.2%})",
        f"- Remaining not_collected: `{totals['not_collected']:,}`",
        f"- API failed: `{totals['api_failed']:,}`",
        f"- No data: `{totals['no_data']:,}`",
        f"- Active collection processes: `{len(processes)}`",
        f"- Process roles: parent `{role_counts.get('parent_runner', 0)}`, child `{role_counts.get('child_collector', 0)}`, watchdog `{role_counts.get('watchdog', 0)}`",
        f"- Latest run event: `{latest.get('event')}` at `{latest.get('timestamp')}`",
        f"- Active/incomplete batch: `{active.get('phase') or ''}` major `{active.get('major_code') or ''}`",
        f"- Active batch age seconds: `{monitoring.get('active_batch_age_seconds')}`",
        f"- Active batch monitor status: `{monitoring.get('status')}`",
        f"- Rate-limit cooldown status: `{cooldown.get('status')}`",
        f"- Cooldown until: `{cooldown.get('cooldown_until')}`",
        f"- Cooldown remaining seconds: `{cooldown.get('cooldown_remaining_seconds')}`",
        f"- Completed batches since latest start: `{checkpoint['run_log'].get('completed_batches_since_latest_start')}`",
        f"- Rate limited since latest start: `{checkpoint['run_log'].get('rate_limited_since_latest_start')}`",
        f"- Recent avg batch seconds: `{forecast.get('average_elapsed_seconds')}`",
        f"- Estimated remaining batches: `{forecast.get('estimated_remaining_batches')}`",
        f"- Estimated remaining hours: `{forecast.get('estimated_remaining_hours')}`",
        f"- Next safe action: `{next_action.get('status')}`",
        f"- Next safe action summary: {next_action.get('summary')}",
        "",
        "## Next Safe Action",
        "",
        f"- Status: `{next_action.get('status')}`",
        f"- Start collector now: `{next_action.get('should_start_collector')}`",
        f"- Start watchdog now: `{next_action.get('should_start_watchdog')}`",
        f"- API call allowed now: `{next_action.get('api_call_allowed_now')}`",
        f"- Avoid duplicate collector: `{next_action.get('avoid_duplicate_collector')}`",
        f"- Cooldown until: `{next_action.get('cooldown_until')}`",
        f"- Cooldown remaining seconds: `{next_action.get('cooldown_remaining_seconds')}`",
        "",
        "## Major Gaps",
        "",
        "| Major | Matched | Total | Not Collected | API Failed | No Data |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for item in checkpoint["element_api_status"]["by_major"]:
        display_item = {**item, "major_name": item.get("major_name_display") or item["major_name"]}
        lines.append(
            "| {major_code} {major_name} | {matched:,} | {total:,} | {not_collected:,} | {api_failed:,} | {no_data:,} |".format(
                **display_item
            )
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This checkpoint is read-only.",
            "- It does not update `human_reviewed`, `accepted`, or `reviewed` statuses.",
            "- It does not include API keys or `.env` values.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def checkpoint_json_text(checkpoint: dict[str, Any]) -> str:
    return json.dumps(checkpoint, ensure_ascii=True, indent=2) + "\n"


def write_alias_outputs(args: argparse.Namespace, checkpoint: dict[str, Any]) -> None:
    date_stamp = current_date_stamp()
    reports_dir = PROJECT_ROOT / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    alias_out = reports_dir / f"checkpoint_ncs006_element_api_status_{date_stamp}.json"
    alias_markdown_out = reports_dir / f"checkpoint_ncs006_element_api_status_{date_stamp}.md"
    current_alias_out = reports_dir / f"checkpoint_ncs006_element_api_status_{date_stamp}_current.json"
    current_alias_markdown_out = reports_dir / f"checkpoint_ncs006_element_api_status_{date_stamp}_current.md"
    if alias_out != args.out:
        alias_out.write_text(checkpoint_json_text(checkpoint), encoding="utf-8")
    if alias_markdown_out != args.markdown_out:
        write_markdown(alias_markdown_out, checkpoint)
    if current_alias_out not in {args.out, alias_out}:
        current_alias_out.write_text(checkpoint_json_text(checkpoint), encoding="utf-8")
    if current_alias_markdown_out not in {args.markdown_out, alias_markdown_out}:
        write_markdown(current_alias_markdown_out, checkpoint)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a read-only NCS006 element API checkpoint report.")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--log-path", type=Path, default=DEFAULT_LOG_PATH)
    parser.add_argument("--summary-path", type=Path, default=DEFAULT_SUMMARY_PATH)
    parser.add_argument("--watch-log-path", type=Path, default=DEFAULT_WATCH_LOG_PATH)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--child-timeout-seconds", type=int, default=900)
    args = parser.parse_args()

    checkpoint = build_checkpoint(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(checkpoint_json_text(checkpoint), encoding="utf-8")
    write_markdown(args.markdown_out, checkpoint)
    write_alias_outputs(args, checkpoint)
    print(json.dumps({"out": str(args.out), "markdown_out": str(args.markdown_out), "totals": checkpoint["element_api_status"]["totals"]}, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
