from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path


NOT_FOUND_MARKER = "[NOT_FOUND]"
LOW_CONFIDENCE_MARKER = "[LOW_CONFIDENCE]"
HALLUCINATION_MARKER = "[HALLUCINATION_DETECTED]"
REDACTION_MARKER = "[REDACTED]"
SENSITIVE_ENV_NAMES = {
    "NCS_SERVICE_KEY",
    "NCS_SQF_SERVICE_KEY",
    "NCS_STUDY_MODULE_SERVICE_KEY",
    "NCS_LEARNING_MODULE_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_NCS_CL_CD_JM_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
    "NCS_JOB_BASE_COMPETENCY_SERVICE_KEY",
}
_SENSITIVE_FIELD_RE = re.compile(
    r"(?:auth[_-]?key|service[_-]?key|api[_-]?key|access[_-]?token|secret|password|credential)",
    re.IGNORECASE,
)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?P<prefix>(?:^|[?&\s])"
    r"(?:authKey|auth_key|serviceKey|service_key|apiKey|apikey|api_key|access_token|token|secret|password)"
    r"=)(?P<value>[^&\s]+)",
    re.IGNORECASE,
)
DISCLAIMER = "이 추천은 교육훈련 안내 목적이며 공식 자격 인정이나 법적 적격성 판단이 아닙니다."


def _unique_suggestions(suggestions: list[str] | None) -> list[str]:
    if not suggestions:
        return []
    unique: list[str] = []
    seen: set[str] = set()
    for suggestion in suggestions:
        value = " ".join(str(suggestion).split())
        if not value or value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def _iter_secret_values_from_env_file() -> list[str]:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return []
    values: list[str] = []
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key in SENSITIVE_ENV_NAMES or _SENSITIVE_FIELD_RE.search(key):
            values.append(value)
    return values


def _known_secret_values() -> list[str]:
    values: list[str] = []
    for name in SENSITIVE_ENV_NAMES:
        values.append(os.getenv(name, ""))
    values.extend(_iter_secret_values_from_env_file())
    cleaned = {
        value
        for value in values
        if len(value) >= 6 and value.lower() not in {"none", "null", "false", "true"}
    }
    return sorted(cleaned, key=len, reverse=True)


def mask_sensitive_text(text: str) -> str:
    redacted = _SENSITIVE_QUERY_RE.sub(
        lambda match: f"{match.group('prefix')}{REDACTION_MARKER}",
        str(text),
    )
    for secret_value in _known_secret_values():
        redacted = redacted.replace(secret_value, REDACTION_MARKER)
    return redacted


def mask_sensitive_payload(value):
    if isinstance(value, str):
        return mask_sensitive_text(value)
    if isinstance(value, Mapping):
        masked = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_FIELD_RE.search(key_text):
                masked[key] = item if item is None or item == "" else REDACTION_MARKER
            else:
                masked[key] = mask_sensitive_payload(item)
        return masked
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [mask_sensitive_payload(item) for item in value]
    return value


def not_found_response(message: str, suggestions: list[str] | None = None) -> dict:
    unique_suggestions = _unique_suggestions(suggestions)
    text = f"{NOT_FOUND_MARKER} {message}\nLLM은 추측 또는 생성을 하지 마세요."
    if unique_suggestions:
        text += f"\n시도해볼 질의: {', '.join(unique_suggestions)}"
    return {
        "ok": False,
        "error": {
            "code": "NOT_FOUND",
            "message": message,
            "suggestions": unique_suggestions,
        },
        "data": {"suggestions": unique_suggestions},
        "content": [{"type": "text", "text": text}],
        "audit": {"generated_at": datetime.now(UTC).isoformat()},
    }


def low_confidence_response(result: dict, reason: str) -> dict:
    result.setdefault("warnings", [])
    result["warnings"].append(f"{LOW_CONFIDENCE_MARKER} {reason}")
    return result
