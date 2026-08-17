"""Vercel function entrypoint for API multiplexing."""

from __future__ import annotations

from api.health import app as _health_app
from api.mcp import app as _mcp_app
from api.ready import app as _ready_app


async def app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        return

    path = scope.get("path", "/") or "/"
    if path in {"/api/health", "/api/health/"}:
        await _health_app(scope, receive, send)
        return
    if path in {"/api/ready", "/api/ready/"}:
        await _ready_app(scope, receive, send)
        return

    await _mcp_app(scope, receive, send)
