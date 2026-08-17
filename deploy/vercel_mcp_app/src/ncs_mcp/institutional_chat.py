from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import inspect
import json
import os
import secrets
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ncs_mcp import __version__
from ncs_mcp.config import load_settings
from ncs_mcp.query_router import OPERATOR_REVIEW, route_ncs_query
from ncs_mcp import server, tool_registry


CHAT_RESPONSE_SCHEMA = "ncs_institutional_chat_response_v1"
CHAT_HEALTH_SCHEMA = "ncs_institutional_chat_health_v1"
DEFAULT_MAX_BODY_BYTES = 65_536
DEFAULT_MAX_MESSAGE_CHARS = 2_000
IDENTITY_HEADER = "X-Authenticated-User"
GROUPS_HEADER = "X-Authenticated-Groups"
GATEWAY_SECRET_HEADER = "X-NCS-Gateway-Secret"
PUBLIC_EXECUTABLE_TOOLS = frozenset(tool_registry.NCS_EXECUTABLE_TOOL_NAMES)
RESERVED_TOOL_PARAMS = frozenset({"_route_query", "_route_fingerprint", "save"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _split_csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(sorted({part.strip() for part in value.split(",") if part.strip()}))


def _runtime_secret(env_name: str, file_env_name: str) -> str | None:
    direct_value = os.getenv(env_name)
    file_value = os.getenv(file_env_name)
    if direct_value and file_value:
        raise ValueError(f"Set only one of {env_name} or {file_env_name}.")
    if not file_value:
        return direct_value or None
    path = Path(file_value)
    try:
        if not path.is_file() or path.stat().st_size > 16_384:
            raise ValueError(f"{file_env_name} must identify a small readable file.")
        value = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(
            f"{file_env_name} must identify a small readable UTF-8 file."
        ) from exc
    if not value or "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError(f"{file_env_name} contains an invalid secret value.")
    return value


@dataclass(frozen=True)
class ChatRuntimeConfig:
    host: str = "127.0.0.1"
    port: int = 8780
    allow_remote_bind: bool = False
    auth_mode: str = "local"
    gateway_secret: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allowed_groups: tuple[str, ...] = ()
    audit_log_path: Path | None = None
    audit_hash_salt: str | None = None
    release_version: str = __version__
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS
    max_http_workers: int = 32
    request_socket_timeout_seconds: float = 15.0

    @property
    def gateway_auth_required(self) -> bool:
        return self.auth_mode == "gateway"


def load_chat_runtime_config(args: argparse.Namespace) -> ChatRuntimeConfig:
    audit_value = args.audit_log or os.getenv("NCS_CHAT_AUDIT_LOG_PATH")
    origins = args.allowed_origin or _split_csv(os.getenv("NCS_CHAT_ALLOWED_ORIGINS"))
    groups = args.allowed_group or _split_csv(os.getenv("NCS_CHAT_ALLOWED_GROUPS"))
    return ChatRuntimeConfig(
        host=args.host,
        port=args.port,
        allow_remote_bind=bool(args.allow_remote_bind),
        auth_mode=args.auth_mode,
        gateway_secret=_runtime_secret(
            "NCS_CHAT_GATEWAY_SECRET",
            "NCS_CHAT_GATEWAY_SECRET_FILE",
        ),
        allowed_origins=tuple(origins),
        allowed_groups=tuple(groups),
        audit_log_path=Path(audit_value) if audit_value else None,
        audit_hash_salt=_runtime_secret(
            "NCS_CHAT_AUDIT_HASH_SALT",
            "NCS_CHAT_AUDIT_HASH_SALT_FILE",
        ),
        release_version=os.getenv("NCS_CHAT_RELEASE_VERSION", __version__),
        max_body_bytes=_env_int(
            "NCS_CHAT_MAX_BODY_BYTES",
            DEFAULT_MAX_BODY_BYTES,
            minimum=1_024,
            maximum=1_048_576,
        ),
        max_message_chars=_env_int(
            "NCS_CHAT_MAX_MESSAGE_CHARS",
            DEFAULT_MAX_MESSAGE_CHARS,
            minimum=100,
            maximum=20_000,
        ),
        max_http_workers=_env_int(
            "NCS_CHAT_MAX_HTTP_WORKERS",
            32,
            minimum=1,
            maximum=128,
        ),
        request_socket_timeout_seconds=_env_float(
            "NCS_CHAT_REQUEST_SOCKET_TIMEOUT_SECONDS",
            15.0,
            minimum=1.0,
            maximum=120.0,
        ),
    )


def validate_chat_runtime(config: ChatRuntimeConfig) -> list[str]:
    issues: list[str] = []
    settings = load_settings()
    remote = not server.is_loopback_bind_host(config.host)
    if not bool(settings.read_only_mode):
        issues.append("NCS_MCP_READ_ONLY=1 is required for institutional chat serving.")
    if bool(settings.operator_tools_enabled):
        issues.append("NCS_MCP_ENABLE_OPERATOR_TOOLS must be 0 for institutional chat serving.")
    if config.auth_mode not in {"local", "gateway"}:
        issues.append("auth_mode must be local or gateway.")
    if remote and not config.allow_remote_bind:
        issues.append("Non-loopback chat binding requires --allow-remote-bind.")
    if remote and not config.gateway_auth_required:
        issues.append("Non-loopback chat binding requires --auth-mode gateway.")
    if config.gateway_auth_required and not config.gateway_secret:
        issues.append("NCS_CHAT_GATEWAY_SECRET is required in gateway auth mode.")
    if config.gateway_auth_required and not config.allowed_origins:
        issues.append("At least one NCS_CHAT_ALLOWED_ORIGINS value is required in gateway auth mode.")
    if config.gateway_auth_required and config.audit_log_path is None:
        issues.append("NCS_CHAT_AUDIT_LOG_PATH is required in gateway auth mode.")
    if config.gateway_auth_required and not config.audit_hash_salt:
        issues.append("NCS_CHAT_AUDIT_HASH_SALT is required in gateway auth mode.")
    return issues


class AuditLog:
    def __init__(self, path: Path | None, *, hash_salt: str | None, required: bool) -> None:
        self.path = path
        self.hash_salt = hash_salt
        self.required = required
        self._lock = threading.Lock()

    def preflight(self) -> None:
        if self.path is None:
            if self.required:
                raise RuntimeError("audit_log_required")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8"):
            pass

    def identity_hash(self, identity: str) -> str | None:
        if not self.hash_salt:
            return None
        digest = hmac.new(
            self.hash_salt.encode("utf-8"),
            identity.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"hmac-sha256:{digest}"

    def write(self, event: dict[str, Any]) -> bool:
        if self.path is None:
            return False
        safe_event = {
            key: value
            for key, value in event.items()
            if key not in {"message", "prompt", "response", "result", "gateway_secret"}
        }
        line = json.dumps(safe_event, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with self._lock:
                with self.path.open("a", encoding="utf-8", newline="") as handle:
                    handle.write(line)
                    handle.flush()
            return True
        except OSError:
            return False


def _value_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return not value
    return False


def _route_summary(route: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": route.get("schema"),
        "scenario": route.get("scenario"),
        "tool": route.get("tool"),
        "confidence": route.get("confidence"),
        "missing_params": route.get("missing_params") or [],
        "available": route.get("available"),
        "route_fingerprint": route.get("route_fingerprint"),
        "expected_tool_chain": route.get("expected_tool_chain") or [],
        "guard_flags": route.get("guard_flags") or [],
        "risk_flags": route.get("risk_flags") or [],
    }


def _error_code(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    if isinstance(error, dict):
        return str(error.get("code") or "tool_error")
    if error:
        return str(error)
    return None


def sanitize_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(result)
    error = sanitized.get("error")
    if not isinstance(error, dict):
        return sanitized
    code = str(error.get("code") or "")
    if code != "tool_execution_failed":
        return sanitized
    safe_error = {
        key: value
        for key, value in error.items()
        if key in {"code", "category", "retryable", "known", "severity", "tool_name"}
    }
    safe_error["message"] = "The requested NCS tool could not complete."
    sanitized["error"] = safe_error
    sanitized["data"] = {}
    sanitized.pop("content", None)
    return sanitized


def _course_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = result.get("training_system_matrix")
    if not isinstance(rows, list):
        rows = result.get("recommended_courses")
    if not isinstance(rows, list):
        rows = result.get("recommendations")
    return [row for row in (rows or []) if isinstance(row, dict)][:5]


def _scope_label(scope: Any) -> str | None:
    if not isinstance(scope, dict):
        return None
    for key in ("resolved_as", "requested", "unit_name", "name"):
        value = scope.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _direction_particle(value: str) -> str:
    """Return the Korean directional particle `로` or `으로`."""
    normalized = value.rstrip()
    if not normalized:
        return "로"
    codepoint = ord(normalized[-1])
    if not 0xAC00 <= codepoint <= 0xD7A3:
        return "로"
    final_consonant = (codepoint - 0xAC00) % 28
    return "으로" if final_consonant not in {0, 8} else "로"


def summarize_tool_result(tool_name: str, result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not result.get("ok"):
        code = _error_code(result) or "tool_error"
        if code == "service_busy":
            return (
                "현재 추천 요청이 많습니다. 잠시 후 다시 요청해 주세요.",
                {"error_code": code, "retryable": True},
            )
        if code in {"NOT_FOUND", "TASK_NOT_FOUND", "concept_not_found"}:
            return (
                "일치하는 NCS 근거를 찾지 못했습니다. 직무명이나 능력단위명을 더 구체적으로 입력해 주세요.",
                {"error_code": code, "retryable": False},
            )
        return (
            "요청을 처리하지 못했습니다. 입력을 확인하거나 기관 운영자에게 문의해 주세요.",
            {"error_code": code, "retryable": False},
        )

    courses = _course_rows(result)
    course_summaries = []
    for index, row in enumerate(courses, start=1):
        name = row.get("course_name") or row.get("name") or row.get("title")
        planner_grouping = row.get("planner_grouping")
        if isinstance(planner_grouping, dict):
            planner_grouping = (
                planner_grouping.get("planner_group")
                or planner_grouping.get("education_type")
                or planner_grouping.get("course_scope_relation")
            )
        course_summaries.append(
            {
                "rank": row.get("rank") or index,
                "course_name": name,
                "required_optional": row.get("required_optional"),
                "planner_grouping": planner_grouping,
                "why_recommended": row.get("why_recommended") or row.get("rationale"),
                "human_review": row.get("human_review"),
            }
        )
    current_scope = _scope_label(result.get("current_scope"))
    target_scope = _scope_label(result.get("target_scope"))
    if courses:
        message = f"NCS 근거를 바탕으로 교육과정 {len(courses)}건을 정리했습니다."
        if current_scope and target_scope:
            particle = _direction_particle(target_scope)
            message += f" {current_scope}에서 {target_scope}{particle}의 전환 기준입니다."
        course_names = [
            str(row.get("course_name") or "").strip()
            for row in course_summaries[:3]
            if str(row.get("course_name") or "").strip()
        ]
        if course_names:
            message += f" 우선 확인 과정은 {', '.join(course_names)}입니다."
        review_attention_required = any(
            isinstance(row.get("human_review"), dict)
            and (
                row["human_review"].get("severity") == "needs_review"
                or row["human_review"].get("status") == "review_required"
                or bool(row["human_review"].get("flags"))
            )
            for row in course_summaries
        )
        if review_attention_required:
            message += " 범위 검토가 필요한 참고 과정이 포함되어 있습니다."
    elif tool_name == "ncs_search":
        values = result.get("results") or result.get("items") or result.get("matches") or []
        count = len(values) if isinstance(values, list) else 0
        message = f"NCS 검색 결과 {count}건을 찾았습니다."
    else:
        message = "NCS 근거 조회를 완료했습니다."
    return (
        message,
        {
            "current_scope": current_scope,
            "target_scope": target_scope,
            "course_count": len(courses),
            "courses": course_summaries,
            "human_review_required": any(bool(row.get("human_review")) for row in courses),
        },
    )


CLARIFICATION_LABELS = {
    "current_query": "현재 직무 또는 현재 수행 과업",
    "target_query": "목표 직무 또는 목표 과업",
    "query": "조회할 직무, 과업 또는 능력단위",
    "mode": "분석 유형",
}


class InstitutionalChatService:
    def __init__(
        self,
        config: ChatRuntimeConfig,
        *,
        audit_log: AuditLog | None = None,
        router: Callable[..., dict[str, Any]] = route_ncs_query,
        executor: Callable[[str, dict[str, Any] | None], dict[str, Any]] = server.ncs_execute_tool,
    ) -> None:
        self.config = config
        self.audit_log = audit_log or AuditLog(
            config.audit_log_path,
            hash_salt=config.audit_hash_salt,
            required=config.gateway_auth_required,
        )
        self.router = router
        self.executor = executor

    def ready(self) -> dict[str, Any]:
        runtime = server.runtime_health_metadata()
        ready = bool(
            runtime.get("database", {}).get("ready")
            and runtime.get("read_only_mode") is True
            and runtime.get("operator_tools_enabled") is False
        )
        return {
            "schema": CHAT_HEALTH_SCHEMA,
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "release_version": self.config.release_version,
            "read_only_mode": runtime.get("read_only_mode"),
            "operator_tools_enabled": runtime.get("operator_tools_enabled"),
            "database_ready": runtime.get("database", {}).get("ready"),
            "gateway_auth_required": self.config.gateway_auth_required,
            "audit_logging_required": self.audit_log.required,
            "public_tool_count": len(PUBLIC_EXECUTABLE_TOOLS),
            "max_http_workers": self.config.max_http_workers,
            "request_socket_timeout_seconds": self.config.request_socket_timeout_seconds,
        }

    def audit_rejection(
        self,
        *,
        request_id: str,
        identity: str,
        groups: tuple[str, ...],
        code: str,
        status: int,
    ) -> bool:
        return self.audit_log.write(
            {
                "schema": "ncs_institutional_chat_audit_v1",
                "timestamp": _utc_now(),
                "request_id": request_id,
                "identity_hash": self.audit_log.identity_hash(identity),
                "group_count": len(groups),
                "route_fingerprint": None,
                "scenario": None,
                "tool": None,
                "duration_ms": 0.0,
                "outcome": "rejected",
                "http_status": status,
                "error_code": code,
                "release_version": self.config.release_version,
                "db_writes": False,
                "operator_tool_execution": False,
            }
        )

    def process(
        self,
        message: str,
        *,
        context: dict[str, Any] | None,
        identity: str,
        groups: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], int]:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        clean_message = str(message or "").strip()
        if not clean_message:
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=None,
                tool_name=None,
                started=started,
                payload=self._error_payload(
                    request_id,
                    "message_required",
                    "질문을 입력해 주세요.",
                ),
                status=400,
            )
        if len(clean_message) > self.config.max_message_chars:
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=None,
                tool_name=None,
                started=started,
                payload=self._error_payload(
                    request_id,
                    "message_too_long",
                    f"질문은 {self.config.max_message_chars}자 이내로 입력해 주세요.",
                ),
                status=400,
            )

        try:
            route = self.router(clean_message, available_tool_names=set(PUBLIC_EXECUTABLE_TOOLS))
        except Exception:
            route = None
        if not isinstance(route, dict):
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=None,
                tool_name=None,
                started=started,
                payload=self._error_payload(
                    request_id,
                    "route_failed",
                    "질문 경로를 해석하지 못했습니다.",
                ),
                status=500,
            )

        tool_name = str(route.get("tool") or "")
        if route.get("scenario") == OPERATOR_REVIEW or tool_name not in PUBLIC_EXECUTABLE_TOOLS:
            payload = self._error_payload(
                request_id,
                "operator_route_blocked",
                "검토·수집·승인 작업은 일반 챗봇에서 실행할 수 없습니다. 기관 운영자 경로를 이용해 주세요.",
                route=route,
            )
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=route,
                tool_name=tool_name or None,
                started=started,
                payload=payload,
                status=403,
            )
        if route.get("available") is not True:
            payload = self._error_payload(
                request_id,
                "route_tool_unavailable",
                "현재 챗봇에서 사용할 수 없는 기능입니다.",
                route=route,
            )
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=route,
                tool_name=tool_name,
                started=started,
                payload=payload,
                status=403,
            )

        tool_params = dict(route.get("params") or {})
        supplied_context = context if isinstance(context, dict) else {}
        handler = server.NCS_EXECUTABLE_TOOL_HANDLERS.get(tool_name)
        allowed_context_fields = (
            set(inspect.signature(handler).parameters)
            if handler is not None
            else set()
        )
        applied_context_fields: list[str] = []
        ignored_context_fields: list[str] = []
        for key, value in supplied_context.items():
            name = str(key)
            if (
                name not in RESERVED_TOOL_PARAMS
                and not name.startswith("_")
                and name in allowed_context_fields
            ):
                tool_params[name] = value
                applied_context_fields.append(name)
            else:
                ignored_context_fields.append(name)
        tool_params["_route_query"] = clean_message
        tool_params["_route_fingerprint"] = route.get("route_fingerprint")
        missing = [
            str(name)
            for name in route.get("required_params") or []
            if _value_missing(tool_params.get(str(name)))
        ]
        if missing:
            labels = [CLARIFICATION_LABELS.get(name, name) for name in missing]
            payload = {
                "schema": CHAT_RESPONSE_SCHEMA,
                "ok": True,
                "request_id": request_id,
                "state": "clarification_required",
                "assistant_message": f"다음 정보를 알려주세요: {', '.join(labels)}.",
                "route": _route_summary(route),
                "clarification": {"missing_params": missing, "labels": labels},
                "context": {
                    "applied_fields": sorted(applied_context_fields),
                    "ignored_fields": sorted(ignored_context_fields),
                },
                "result": None,
                "disclaimer": server.DISCLAIMER,
            }
            return self._finish(
                request_id=request_id,
                identity=identity,
                groups=groups,
                route=route,
                tool_name=tool_name,
                started=started,
                payload=payload,
                status=200,
            )

        try:
            result = self.executor(tool_name, tool_params)
        except Exception:
            result = {
                "ok": False,
                "error": {"code": "tool_execution_failed", "retryable": False},
            }
        if not isinstance(result, dict):
            result = {
                "ok": False,
                "error": {"code": "invalid_tool_response", "retryable": False},
            }
        result = sanitize_tool_result(result)
        assistant_message, evidence = summarize_tool_result(tool_name, result)
        payload = {
            "schema": CHAT_RESPONSE_SCHEMA,
            "ok": bool(result.get("ok")),
            "request_id": request_id,
            "state": "completed" if result.get("ok") else "tool_error",
            "assistant_message": assistant_message,
            "route": _route_summary(route),
            "evidence": evidence,
            "context": {
                "applied_fields": sorted(applied_context_fields),
                "ignored_fields": sorted(ignored_context_fields),
            },
            "result": result,
            "disclaimer": result.get("disclaimer") or server.DISCLAIMER,
        }
        status = 200
        if not result.get("ok") and _error_code(result) not in {
            "NOT_FOUND",
            "TASK_NOT_FOUND",
            "concept_not_found",
            "service_busy",
        }:
            status = 422
        return self._finish(
            request_id=request_id,
            identity=identity,
            groups=groups,
            route=route,
            tool_name=tool_name,
            started=started,
            payload=payload,
            status=status,
        )

    def _error_payload(
        self,
        request_id: str,
        code: str,
        assistant_message: str,
        *,
        route: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema": CHAT_RESPONSE_SCHEMA,
            "ok": False,
            "request_id": request_id,
            "state": "blocked" if code.endswith("_blocked") else "error",
            "assistant_message": assistant_message,
            "error": {"code": code},
            "route": _route_summary(route) if route else None,
            "result": None,
            "disclaimer": server.DISCLAIMER,
        }

    def _finish(
        self,
        *,
        request_id: str,
        identity: str,
        groups: tuple[str, ...],
        route: dict[str, Any] | None,
        tool_name: str | None,
        started: float,
        payload: dict[str, Any],
        status: int,
    ) -> tuple[dict[str, Any], int]:
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        error_code = None
        if isinstance(payload.get("error"), dict):
            error_code = payload["error"].get("code")
        if error_code is None and isinstance(payload.get("result"), dict):
            error_code = _error_code(payload["result"])
        event = {
            "schema": "ncs_institutional_chat_audit_v1",
            "timestamp": _utc_now(),
            "request_id": request_id,
            "identity_hash": self.audit_log.identity_hash(identity),
            "group_count": len(groups),
            "route_fingerprint": route.get("route_fingerprint") if route else None,
            "scenario": route.get("scenario") if route else None,
            "tool": tool_name,
            "duration_ms": duration_ms,
            "outcome": payload.get("state"),
            "http_status": status,
            "error_code": error_code,
            "release_version": self.config.release_version,
            "db_writes": False,
            "operator_tool_execution": False,
        }
        audit_logged = self.audit_log.write(event)
        if self.audit_log.required and not audit_logged:
            return (
                self._error_payload(
                    request_id,
                    "audit_log_unavailable",
                    "감사로그를 기록할 수 없어 응답을 제공하지 않습니다.",
                ),
                503,
            )
        payload["audit"] = {
            "request_id": request_id,
            "duration_ms": duration_ms,
            "logged": audit_logged,
            "release_version": self.config.release_version,
            "db_writes": False,
            "operator_tool_execution": False,
        }
        return payload, status


def render_chat_html(*, nonce: str = "static-test-nonce") -> str:
    title = "NCS 교육설계"
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style nonce="{html.escape(nonce, quote=True)}">
:root {{ color-scheme: light; --ink:#17201c; --muted:#5d6963; --line:#d7ddd9; --panel:#f6f8f7; --accent:#176b4d; --accent-2:#9b3d2f; --focus:#0b5cab; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; color:var(--ink); background:#eef1ef; font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; letter-spacing:0; }}
button,input {{ font:inherit; letter-spacing:0; }}
.shell {{ min-height:100vh; display:grid; grid-template-rows:56px 1fr; }}
header {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:0 20px; background:#fff; border-bottom:1px solid var(--line); }}
.brand {{ display:flex; align-items:baseline; gap:10px; min-width:0; }}
.brand strong {{ font-size:18px; }}
.brand span {{ color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.status {{ display:flex; align-items:center; gap:8px; color:var(--muted); font-size:12px; }}
.dot {{ width:8px; height:8px; border-radius:50%; background:#7a8580; }}
.dot.ready {{ background:#17804f; }}
main {{ width:min(1120px,100%); margin:0 auto; display:grid; grid-template-columns:minmax(0,1fr) 300px; min-height:calc(100vh - 56px); background:#fff; border-left:1px solid var(--line); border-right:1px solid var(--line); }}
.chat {{ min-width:0; display:grid; grid-template-rows:1fr auto; }}
#messages {{ padding:24px; overflow:auto; display:flex; flex-direction:column; gap:14px; }}
.message {{ max-width:82%; padding:12px 14px; border:1px solid var(--line); border-radius:6px; white-space:pre-wrap; overflow-wrap:anywhere; }}
.message.user {{ align-self:flex-end; background:#e9f3ee; border-color:#b8d5c7; }}
.message.assistant {{ align-self:flex-start; background:#fff; }}
.meta {{ margin-top:8px; color:var(--muted); font-size:12px; }}
.composer {{ padding:16px 20px 20px; border-top:1px solid var(--line); background:#fff; }}
.composer-row {{ display:grid; grid-template-columns:1fr auto; gap:10px; align-items:end; }}
label {{ display:block; color:var(--muted); font-size:12px; margin-bottom:6px; }}
input {{ width:100%; height:44px; border:1px solid #9da8a2; border-radius:5px; padding:0 12px; color:var(--ink); background:#fff; }}
input:focus,button:focus-visible {{ outline:3px solid rgba(11,92,171,.25); outline-offset:1px; border-color:var(--focus); }}
button {{ min-height:40px; border:1px solid var(--line); border-radius:5px; padding:0 14px; color:var(--ink); background:#fff; cursor:pointer; }}
button.primary {{ height:44px; color:#fff; background:var(--accent); border-color:var(--accent); font-weight:650; }}
button:disabled {{ opacity:.55; cursor:not-allowed; }}
aside {{ border-left:1px solid var(--line); background:var(--panel); padding:20px; overflow:auto; }}
aside h2 {{ margin:0 0 12px; font-size:14px; }}
.examples {{ display:grid; gap:8px; margin-bottom:24px; }}
.examples button {{ text-align:left; height:auto; min-height:42px; padding:9px 10px; background:#fff; }}
.route {{ border-top:1px solid var(--line); padding-top:16px; }}
.kv {{ display:grid; grid-template-columns:80px 1fr; gap:6px 10px; margin:0; }}
.kv dt {{ color:var(--muted); }}
.kv dd {{ margin:0; overflow-wrap:anywhere; }}
.course-list {{ display:grid; gap:8px; margin-top:10px; }}
.course {{ border-left:3px solid var(--accent); padding:8px 10px; background:#fff; }}
.course strong {{ display:block; }}
.course-meta,.course-reason,.course-review {{ display:block; font-size:12px; }}
.course-meta {{ color:var(--muted); }}
.course-reason {{ margin-top:5px; display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
.course-review {{ margin-top:5px; font-weight:650; }}
.warning {{ color:var(--accent-2); }}
.empty {{ color:var(--muted); }}
@media (max-width:780px) {{ main {{ grid-template-columns:1fr; border:0; }} .chat {{ min-height:calc(100vh - 56px); }} aside {{ display:block; border-left:0; border-top:1px solid var(--line); }} .message {{ max-width:94%; }} .brand span {{ display:none; }} }}
</style>
</head>
<body>
<div class="shell">
<header><div class="brand"><strong>{html.escape(title)}</strong><span>기관 내부 교육훈련 추천</span></div><div class="status"><span id="healthDot" class="dot"></span><span id="healthText">확인 중</span></div></header>
<main>
  <section class="chat" aria-label="NCS 교육설계 대화">
    <div id="messages" aria-live="polite"><div class="message assistant">어떤 직무나 과업에 필요한 교육을 찾으시나요?<div class="meta">추천 결과는 교육계획 참고자료이며 공식 자격·승인 판단이 아닙니다.</div></div></div>
    <form id="chatForm" class="composer">
      <label for="messageInput">질문</label>
      <div class="composer-row"><input id="messageInput" maxlength="2000" autocomplete="off" placeholder="예: 총무에서 인사기획으로 전환 교육체계 추천" required><button id="sendButton" class="primary" type="submit">전송</button></div>
    </form>
  </section>
  <aside>
    <h2>빠른 질문</h2>
    <div class="examples">
      <button type="button" data-prompt="총무에서 인사기획으로 전환 교육체계 추천">직무전환 교육체계</button>
      <button type="button" data-prompt="노무관리 과업에 필요한 훈련과정 추천">과업별 훈련과정</button>
      <button type="button" data-prompt="인사기획 NCS 능력단위 검색">NCS 구조 검색</button>
    </div>
    <div class="route"><h2>최근 근거</h2><dl id="routeDetail" class="kv"><dt>상태</dt><dd class="empty">대기 중</dd></dl><div id="courseList" class="course-list"></div></div>
  </aside>
</main>
</div>
<script nonce="{html.escape(nonce, quote=True)}">
const form=document.getElementById('chatForm'), input=document.getElementById('messageInput'), send=document.getElementById('sendButton'), messages=document.getElementById('messages'), routeDetail=document.getElementById('routeDetail'), courseList=document.getElementById('courseList');
function addMessage(text,role,meta){{const el=document.createElement('div');el.className='message '+role;el.textContent=text;if(meta){{const m=document.createElement('div');m.className='meta';m.textContent=meta;el.appendChild(m)}}messages.appendChild(el);messages.scrollTop=messages.scrollHeight;}}
function setRoute(data){{
  routeDetail.replaceChildren();courseList.replaceChildren();
  const route=data.route||{{}}, evidence=data.evidence||{{}};
  const rows=[['시나리오',route.scenario||'-'],['도구',route.tool||'-'],['경로 ID',route.route_fingerprint||'-'],['처리시간',data.audit?data.audit.duration_ms+' ms':'-']];
  for(const [k,v] of rows){{const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=k;dd.textContent=String(v);routeDetail.append(dt,dd)}}
  for(const row of evidence.courses||[]){{
    const el=document.createElement('div');el.className='course';
    const strong=document.createElement('strong');strong.textContent=(row.rank?row.rank+'. ':'')+(row.course_name||'과정명 확인 필요');el.appendChild(strong);
    const metaText=[row.planner_grouping,row.required_optional].filter(Boolean).join(' · ');
    if(metaText){{const meta=document.createElement('span');meta.className='course-meta';meta.textContent=metaText;el.appendChild(meta)}}
    const why=row.why_recommended;
    const reason=Array.isArray(why)?why.find(item=>typeof item==='string'&&item.trim()):(typeof why==='string'?why:'');
    if(reason){{const reasonEl=document.createElement('span');reasonEl.className='course-reason';reasonEl.textContent=reason;el.appendChild(reasonEl)}}
    if(row.human_review){{
      const review=row.human_review;
      const needsReview=typeof review==='object'&&(review.severity==='needs_review'||review.status==='review_required'||(Array.isArray(review.flags)&&review.flags.length>0));
      const reviewEl=document.createElement('span');reviewEl.className='course-review'+(needsReview?' warning':'');reviewEl.textContent=needsReview?'범위 검토 필요':'검토 후 확정';el.appendChild(reviewEl);
    }}
    courseList.appendChild(el);
  }}
}}
async function sendMessage(text){{addMessage(text,'user');send.disabled=true;input.disabled=true;try{{const res=await fetch('/api/chat',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{message:text}})}});const data=await res.json();addMessage(data.assistant_message||'응답을 처리하지 못했습니다.','assistant',data.disclaimer||'');setRoute(data)}}catch(e){{addMessage('서비스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.','assistant')}}finally{{send.disabled=false;input.disabled=false;input.focus()}}}}
form.addEventListener('submit',e=>{{e.preventDefault();const text=input.value.trim();if(!text)return;input.value='';sendMessage(text)}});
document.querySelectorAll('[data-prompt]').forEach(btn=>btn.addEventListener('click',()=>{{input.value=btn.dataset.prompt;form.requestSubmit()}}));
fetch('/ready').then(r=>r.json().then(d=>[r.ok,d])).then(([ok,d])=>{{document.getElementById('healthText').textContent=ok?'준비됨':'점검 필요';if(ok)document.getElementById('healthDot').classList.add('ready')}}).catch(()=>document.getElementById('healthText').textContent='연결 안 됨');
</script>
</body>
</html>"""


class InstitutionalChatHandler(BaseHTTPRequestHandler):
    server_version = "NCSInstitutionalChat"
    sys_version = ""

    @property
    def service(self) -> InstitutionalChatService:
        return self.server.chat_service  # type: ignore[attr-defined]

    @property
    def config(self) -> ChatRuntimeConfig:
        return self.service.config

    def _security_headers(self, *, nonce: str | None = None) -> None:
        if nonce:
            csp = (
                "default-src 'none'; "
                f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
                "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'"
            )
        else:
            csp = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _json_response(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers()
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self) -> None:
        nonce = secrets.token_urlsafe(18)
        body = render_chat_html(nonce=nonce).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._security_headers(nonce=nonce)
        self.end_headers()
        self.wfile.write(body)

    def _rejection_response(
        self,
        code: str,
        *,
        status: int,
        identity: str = "unauthenticated",
        groups: tuple[str, ...] = (),
    ) -> None:
        request_id = uuid.uuid4().hex
        audit_logged = self.service.audit_rejection(
            request_id=request_id,
            identity=identity,
            groups=groups,
            code=code,
            status=status,
        )
        if self.service.audit_log.required and not audit_logged:
            code = "audit_log_unavailable"
            status = 503
        self._json_response(
            {
                "ok": False,
                "request_id": request_id,
                "error": {"code": code},
            },
            status=status,
        )

    def _authenticate(self, *, require_origin: bool = True) -> tuple[str, tuple[str, ...]] | None:
        if not self.config.gateway_auth_required:
            return "local-user", ()
        origin = self.headers.get("Origin")
        if require_origin and origin not in self.config.allowed_origins:
            self._rejection_response("origin_not_allowed", status=403)
            return None
        supplied_secret = self.headers.get(GATEWAY_SECRET_HEADER, "")
        expected_secret = self.config.gateway_secret or ""
        if not supplied_secret or not hmac.compare_digest(supplied_secret, expected_secret):
            self._rejection_response("authentication_required", status=401)
            return None
        identity = self.headers.get(IDENTITY_HEADER, "").strip()
        if not identity or len(identity) > 256:
            self._rejection_response(
                "authenticated_identity_required",
                status=401,
                identity="missing-identity",
            )
            return None
        groups = _split_csv(self.headers.get(GROUPS_HEADER))
        if self.config.allowed_groups and not set(groups).intersection(self.config.allowed_groups):
            self._rejection_response(
                "group_not_authorized",
                status=403,
                identity=identity,
                groups=groups,
            )
            return None
        return identity, groups

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            if self.config.gateway_auth_required and self._authenticate(require_origin=False) is None:
                return
            self._html_response()
            return
        if path == "/health":
            ready = self.service.ready()
            self._json_response(
                {
                    "schema": CHAT_HEALTH_SCHEMA,
                    "status": "ok" if ready["ready"] else "degraded",
                    "name": "ncs-institutional-chat",
                    "release_version": self.config.release_version,
                }
            )
            return
        if path == "/ready":
            payload = self.service.ready()
            self._json_response(payload, status=200 if payload["ready"] else 503)
            return
        self._json_response({"ok": False, "error": {"code": "not_found"}}, status=404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/api/chat":
            self._json_response({"ok": False, "error": {"code": "not_found"}}, status=404)
            return
        auth = self._authenticate()
        if auth is None:
            return
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            self._rejection_response(
                "json_content_type_required",
                status=415,
                identity=auth[0],
                groups=auth[1],
            )
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = -1
        if content_length < 0 or content_length > self.config.max_body_bytes:
            self._rejection_response(
                "request_body_too_large",
                status=413,
                identity=auth[0],
                groups=auth[1],
            )
            return
        try:
            payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (socket.timeout, TimeoutError):
            self._rejection_response(
                "request_read_timeout",
                status=408,
                identity=auth[0],
                groups=auth[1],
            )
            return
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._rejection_response(
                "invalid_json_body",
                status=400,
                identity=auth[0],
                groups=auth[1],
            )
            return
        if not isinstance(payload, dict):
            self._rejection_response(
                "json_object_required",
                status=400,
                identity=auth[0],
                groups=auth[1],
            )
            return
        identity, groups = auth
        result, status = self.service.process(
            str(payload.get("message") or ""),
            context=payload.get("context") if isinstance(payload.get("context"), dict) else None,
            identity=identity,
            groups=groups,
        )
        self._json_response(result, status=status)

    def log_message(self, format: str, *args: Any) -> None:
        return


class InstitutionalChatHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], service: InstitutionalChatService) -> None:
        self.chat_service = service
        self._http_slots = threading.BoundedSemaphore(service.config.max_http_workers)
        super().__init__(address, InstitutionalChatHandler)

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.chat_service.config.request_socket_timeout_seconds)
        return request, client_address

    def process_request(self, request: socket.socket, client_address: Any) -> None:
        self._http_slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._http_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._http_slots.release()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the read-only institutional NCS chat reference service.")
    parser.add_argument("--host", default=os.getenv("NCS_CHAT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("NCS_CHAT_PORT", "8780")))
    parser.add_argument("--allow-remote-bind", action="store_true")
    parser.add_argument(
        "--auth-mode",
        choices=("local", "gateway"),
        default=os.getenv("NCS_CHAT_AUTH_MODE", "local"),
    )
    parser.add_argument("--allowed-origin", action="append", default=[])
    parser.add_argument("--allowed-group", action="append", default=[])
    parser.add_argument("--audit-log")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        config = load_chat_runtime_config(args)
    except ValueError as exc:
        parser.error(str(exc))
    issues = validate_chat_runtime(config)
    if issues:
        parser.error(" ".join(issues))
    audit_log = AuditLog(
        config.audit_log_path,
        hash_salt=config.audit_hash_salt,
        required=config.gateway_auth_required,
    )
    try:
        audit_log.preflight()
    except OSError as exc:
        parser.error(f"Audit log is not writable: {type(exc).__name__}")
    service = InstitutionalChatService(config, audit_log=audit_log)
    ready = service.ready()
    if not ready["ready"]:
        parser.error("Prepared read-only NCS database is not ready.")
    httpd = InstitutionalChatHTTPServer((config.host, config.port), service)
    print(
        json.dumps(
            {
                "status": "ready",
                "url": f"http://{config.host}:{httpd.server_address[1]}",
                "auth_mode": config.auth_mode,
                "read_only": True,
                "operator_tools_enabled": False,
                "audit_logging": config.audit_log_path is not None,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
