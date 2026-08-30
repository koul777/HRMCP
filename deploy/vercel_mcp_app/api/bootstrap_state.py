"""Side-effect-free process-local Vercel bootstrap diagnostics state."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock
from typing import Any, Mapping


_SCHEMA = "ncs_vercel_bootstrap_metrics_v2"
_NOT_INITIALIZED: dict[str, object] = {
    "schema": _SCHEMA,
    "status": "not_initialized",
    "source": None,
    "ready": None,
}
_LOCK = RLock()
_bootstrap_metrics: dict[str, object] = deepcopy(_NOT_INITIALIZED)


def _deep_merge(base: dict[str, Any], patch: Mapping[str, object]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in patch.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = deepcopy(value)
    return merged


def record_bootstrap_metrics(metrics: Mapping[str, object]) -> dict[str, object]:
    """Atomically replace bootstrap diagnostics and return an isolated copy."""

    value = dict(metrics)
    value.setdefault("schema", _SCHEMA)
    with _LOCK:
        global _bootstrap_metrics
        _bootstrap_metrics = deepcopy(value)
        return deepcopy(_bootstrap_metrics)


def merge_bootstrap_metrics(metrics: Mapping[str, object]) -> dict[str, object]:
    """Atomically merge bootstrap diagnostics and return an isolated copy."""

    with _LOCK:
        global _bootstrap_metrics
        _bootstrap_metrics = _deep_merge(_bootstrap_metrics, metrics)
        return deepcopy(_bootstrap_metrics)


def get_bootstrap_metrics() -> dict[str, object]:
    """Return an isolated snapshot without importing the MCP application."""

    with _LOCK:
        return deepcopy(_bootstrap_metrics)
