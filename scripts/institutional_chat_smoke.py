from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def request_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = dict(headers or {})
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(url, data=body, headers=request_headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def start_chat_process(
    *,
    env: dict[str, str],
    auth_mode: str,
    timeout_seconds: float,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ncs_mcp.institutional_chat",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
            "--auth-mode",
            auth_mode,
            *(extra_args or []),
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    started = time.monotonic()
    startup: dict[str, Any] | None = None
    assert process.stdout is not None
    while time.monotonic() - started < timeout_seconds:
        line = process.stdout.readline()
        if line:
            startup = json.loads(line)
            break
        if process.poll() is not None:
            break
        time.sleep(0.05)
    if startup is None:
        stderr = process.stderr.read() if process.stderr else ""
        process.kill()
        process.wait(timeout=5)
        raise RuntimeError(f"chat service did not start: {stderr[-1000:]}")
    return process, startup


def stop_chat_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_smoke(*, timeout_seconds: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ncs-institutional-chat-smoke-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "ncs.db"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(SRC)
        subprocess.run(
            [sys.executable, "-m", "ncs_mcp.smoke_data", "--out", str(db_path)],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        before = {
            "size_bytes": db_path.stat().st_size,
            "mtime_ns": db_path.stat().st_mtime_ns,
            "sha256": sha256(db_path),
        }
        env.update(
            {
                "NCS_DB_PATH": str(db_path),
                "NCS_MCP_READ_ONLY": "1",
                "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0",
                "NCS_CHAT_AUTH_MODE": "local",
            }
        )
        process, startup = start_chat_process(
            env=env,
            auth_mode="local",
            timeout_seconds=timeout_seconds,
        )
        try:
            base_url = str(startup["url"])
            ready_status, ready = request_json(base_url + "/ready")
            chat_status, chat = request_json(
                base_url + "/api/chat",
                payload={
                    "message": "Recommend a training course for this task",
                    "context": {"query": "HR planning", "limit": 3},
                },
            )
            operator_status, operator = request_json(
                base_url + "/api/chat",
                payload={"message": "Open the operator review queue for quality issues"},
            )
        finally:
            stop_chat_process(process)

        gateway_origin = "https://chat.example.invalid"
        gateway_audit_path = tmp_path / "gateway-audit.jsonl"
        gateway_secret_path = tmp_path / "gateway-secret.txt"
        gateway_salt_path = tmp_path / "gateway-audit-salt.txt"
        gateway_secret_path.write_text("smoke-gateway-secret\n", encoding="utf-8")
        gateway_salt_path.write_text("smoke-audit-salt\n", encoding="utf-8")
        gateway_env = dict(env)
        gateway_env.pop("NCS_CHAT_GATEWAY_SECRET", None)
        gateway_env.pop("NCS_CHAT_AUDIT_HASH_SALT", None)
        gateway_env.update(
            {
                "NCS_CHAT_AUTH_MODE": "gateway",
                "NCS_CHAT_GATEWAY_SECRET_FILE": str(gateway_secret_path),
                "NCS_CHAT_AUDIT_HASH_SALT_FILE": str(gateway_salt_path),
            }
        )
        gateway_process, gateway_startup = start_chat_process(
            env=gateway_env,
            auth_mode="gateway",
            timeout_seconds=timeout_seconds,
            extra_args=[
                "--allowed-origin",
                gateway_origin,
                "--allowed-group",
                "hrd-users",
                "--audit-log",
                str(gateway_audit_path),
            ],
        )
        authorized_headers = {
            "Origin": gateway_origin,
            "X-NCS-Gateway-Secret": "smoke-gateway-secret",
            "X-Authenticated-User": "employee@example.invalid",
            "X-Authenticated-Groups": "hrd-users",
        }
        gateway_prompt = "Recommend a training course for this task"
        try:
            gateway_url = str(gateway_startup["url"])
            bad_origin_status, bad_origin = request_json(
                gateway_url + "/api/chat",
                payload={"message": gateway_prompt},
                headers={**authorized_headers, "Origin": "https://not-allowed.invalid"},
            )
            missing_secret_headers = dict(authorized_headers)
            missing_secret_headers.pop("X-NCS-Gateway-Secret")
            missing_secret_status, missing_secret = request_json(
                gateway_url + "/api/chat",
                payload={"message": gateway_prompt},
                headers=missing_secret_headers,
            )
            gateway_chat_status, gateway_chat = request_json(
                gateway_url + "/api/chat",
                payload={
                    "message": gateway_prompt,
                    "context": {"query": "HR planning", "limit": 3},
                },
                headers=authorized_headers,
            )
        finally:
            stop_chat_process(gateway_process)
        gateway_audit_text = gateway_audit_path.read_text(encoding="utf-8")
        gateway_audit_events = [
            json.loads(line)
            for line in gateway_audit_text.splitlines()
            if line.strip()
        ]
        after = {
            "size_bytes": db_path.stat().st_size,
            "mtime_ns": db_path.stat().st_mtime_ns,
            "sha256": sha256(db_path),
        }
        checks = {
            "startup_ready": startup.get("status") == "ready",
            "read_only_startup": startup.get("read_only") is True,
            "operator_tools_disabled": startup.get("operator_tools_enabled") is False,
            "ready_endpoint": ready_status == 200 and ready.get("ready") is True,
            "chat_completed": chat_status == 200 and chat.get("ok") is True,
            "chat_route_public": (chat.get("route") or {}).get("tool") in {
                "recommend_training_for_task",
                "recommend_training_transition",
                "plan_ncs_education_path",
                "ncs_search",
            },
            "operator_route_blocked": (
                operator_status == 403
                and (operator.get("error") or {}).get("code") == "operator_route_blocked"
            ),
            "database_unchanged": before == after,
            "local_prompt_not_audit_logged": (chat.get("audit") or {}).get("logged") is False,
            "gateway_startup_ready": gateway_startup.get("status") == "ready",
            "gateway_auth_mode": gateway_startup.get("auth_mode") == "gateway",
            "gateway_file_backed_secrets": True,
            "bad_origin_rejected": (
                bad_origin_status == 403
                and (bad_origin.get("error") or {}).get("code") == "origin_not_allowed"
            ),
            "missing_secret_rejected": (
                missing_secret_status == 401
                and (missing_secret.get("error") or {}).get("code")
                == "authentication_required"
            ),
            "gateway_chat_completed": (
                gateway_chat_status == 200 and gateway_chat.get("ok") is True
            ),
            "gateway_audit_events_present": len(gateway_audit_events) == 3,
            "gateway_rejections_audited": (
                [event.get("error_code") for event in gateway_audit_events[:2]]
                == ["origin_not_allowed", "authentication_required"]
            ),
            "gateway_prompt_not_in_audit": gateway_prompt not in gateway_audit_text,
            "gateway_identity_not_in_audit": (
                "employee@example.invalid" not in gateway_audit_text
            ),
            "gateway_secret_not_in_audit": (
                "smoke-gateway-secret" not in gateway_audit_text
            ),
        }
        return {
            "schema": "ncs_institutional_chat_smoke_v1",
            "ok": all(checks.values()),
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
            "checks": checks,
            "startup": startup,
            "ready": ready,
            "chat": {
                "status": chat_status,
                "state": chat.get("state"),
                "tool": (chat.get("route") or {}).get("tool"),
                "route_fingerprint": (chat.get("route") or {}).get("route_fingerprint"),
                "course_count": (chat.get("evidence") or {}).get("course_count"),
                "audit": chat.get("audit"),
            },
            "operator_block": {
                "status": operator_status,
                "error_code": (operator.get("error") or {}).get("code"),
            },
            "gateway": {
                "startup": gateway_startup,
                "secret_source": "file",
                "bad_origin_status": bad_origin_status,
                "bad_origin_error": (bad_origin.get("error") or {}).get("code"),
                "missing_secret_status": missing_secret_status,
                "missing_secret_error": (missing_secret.get("error") or {}).get("code"),
                "chat_status": gateway_chat_status,
                "chat_state": gateway_chat.get("state"),
                "audit_event_count": len(gateway_audit_events),
                "audit_error_codes": [
                    event.get("error_code") for event in gateway_audit_events
                ],
                "audit_identity_hash_present": any(
                    bool(event.get("identity_hash")) for event in gateway_audit_events
                ),
            },
            "database_before": before,
            "database_after": after,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the reference institutional chat service.")
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    report = run_smoke(timeout_seconds=max(5.0, args.timeout_seconds))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
