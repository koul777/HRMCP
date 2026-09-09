"""Durable Builder phase history; this does not resume collectors or database writes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuilderSession:
    """Single UI owner journal in ``state/session.json``.

    Version arguments are directory paths (or None). Call ``finish`` only after
    the phase has verified its output. Error messages must be sanitized by the UI.
    A previously running record is incomplete, not proof that its process stopped.
    """

    def __init__(self, state: Path):
        self.path = Path(state) / "session.json"
        self._active_id: str | None = None
        self.data: dict[str, Any] = {
            "schema": "ncs_builder_session_v1",
            "selected_version": None,
            "attempts": [],
        }
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or loaded.get("schema") != self.data["schema"]:
                raise ValueError("지원하지 않는 Builder 세션 기록입니다.")
            if not isinstance(loaded.get("attempts"), list):
                raise ValueError("Builder 실행 이력 형식이 올바르지 않습니다.")
            self.data = loaded
            changed = False
            for attempt in self.data["attempts"]:
                if attempt.get("status") == "running":
                    attempt["status"] = "incomplete"
                    attempt["message"] = "이전 세션의 완료 여부가 확인되지 않았습니다."
                    changed = True
            if changed:
                self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _active(self) -> dict[str, Any] | None:
        return next((item for item in reversed(self.data["attempts"])
                     if item["id"] == self._active_id), None)

    def start(self, phase: str, version: Path | str | None = None) -> str:
        if self._active_id is not None:
            raise RuntimeError("현재 단계가 완료되기 전에 새 단계를 시작할 수 없습니다.")
        attempt_id = uuid4().hex
        self.data["attempts"].append({
            "id": attempt_id,
            "phase": phase,
            "version": str(version) if version is not None else None,
            "status": "running",
            "started_at": _timestamp(),
        })
        self._active_id = attempt_id
        self._save()
        return attempt_id

    def progress(self, event: dict[str, Any]) -> None:
        attempt = self._active()
        if attempt is None:
            return
        # Copy only the public progress contract, never collector request metadata.
        public = {key: event[key] for key in ("stage", "completed", "total", "unit", "detail") if key in event}
        if attempt.get("progress") == public:
            return
        attempt["progress"] = public
        self._save()

    def finish(self, version: Path | str | None = None) -> None:
        attempt = self._active()
        if attempt is None:
            raise RuntimeError("완료할 실행 단계가 없습니다.")
        attempt.update(status="completed", finished_at=_timestamp())
        if version is not None:
            attempt["output_version"] = str(version)
            self.data["selected_version"] = str(version)
        self._save()
        self._active_id = None

    def fail(self, message: str) -> None:
        attempt = self._active()
        if attempt is None:
            return
        attempt.update(status="failed", finished_at=_timestamp(), message=message[:2000])
        self._save()
        self._active_id = None
