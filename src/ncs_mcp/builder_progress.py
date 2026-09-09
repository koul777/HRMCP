"""Measured work progress, separate from wall-clock estimates and workflow completion."""
from __future__ import annotations

import math


def describe_progress(event: str | dict) -> tuple[str, float | None]:
    if isinstance(event, str):
        return event + " · 처리량 계산 중", None
    stage = str(event.get("stage", "처리 중"))
    completed, total = event.get("completed"), event.get("total")
    valid_number = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
    if not valid_number(completed) or completed < 0:
        return stage + " · 처리량 계산 중", None
    unit = str(event.get("unit", "건"))
    suffix = f" · {event['detail']}" if event.get("detail") else ""
    if not valid_number(total) or total <= 0 or completed > total:
        return f"{stage} · {completed:,}{unit} 처리 · 전체량 확인 중{suffix}", None
    percent = completed / total * 100
    return f"{stage} · {percent:.1f}% ({completed:,}/{total:,}{unit}){suffix}", percent


def workflow_percent(states: dict[int, str]) -> tuple[float, int]:
    done = sum(states.get(number) == "done" for number in range(1, 5))
    return done * 25.0, done
