"""Vercel Python function entrypoint for the NCS MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from pathlib import Path

# Ensure local source package import works in Vercel function runtime.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.transport_security import TransportSecuritySettings

from .bootstrap_runtime import ensure_bootstrap, is_vercel_read_only_configuration
from .bootstrap_state import get_bootstrap_metrics, merge_bootstrap_metrics

_MODULE_IMPORT_STARTED = time.perf_counter()
_BOOTSTRAP_SNAPSHOT = ensure_bootstrap()
_MCP_BOOTSTRAP_READY = bool(_BOOTSTRAP_SNAPSHOT.get("ready"))
_SERVER_IMPORT_STARTED = time.perf_counter()
from ncs_mcp.server import configure_transport, mcp

merge_bootstrap_metrics(
    {
        "stages_ms": {
            "server_import": round(
                (time.perf_counter() - _SERVER_IMPORT_STARTED) * 1000, 3
            )
        }
    }
)


LOGGER = logging.getLogger(__name__)

_GET_METHOD_NOT_ALLOWED_BODY = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,'
    b'"message":"Method Not Allowed: standalone MCP GET streams are not enabled; use POST"}}'
)

_MCP_DATABASE_UNAVAILABLE_BODY = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32603,'
    b'"message":"Service Unavailable: no verified NCS database snapshot is available"}}'
)

def bootstrap_metrics() -> dict[str, object]:
    """Backward-compatible accessor for process-local bootstrap diagnostics."""

    return get_bootstrap_metrics()


async def _reject_unavailable_mcp(send) -> None:
    """Fail closed before MCP lifespan startup when Vercel has no DB."""

    await send(
        {
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"cache-control", b"no-store"),
                (b"content-type", b"application/json"),
                (
                    b"content-length",
                    str(len(_MCP_DATABASE_UNAVAILABLE_BODY)).encode("ascii"),
                ),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _MCP_DATABASE_UNAVAILABLE_BODY,
            "more_body": False,
        }
    )


async def _reject_standalone_get(send) -> None:
    """End optional Streamable HTTP GET requests before the MCP SDK opens SSE."""
    await send(
        {
            "type": "http.response.start",
            "status": 405,
            "headers": [
                (b"allow", b"POST"),
                (b"cache-control", b"no-store"),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_GET_METHOD_NOT_ALLOWED_BODY)).encode("ascii")),
            ],
        }
    )
    await send(
        {
            "type": "http.response.body",
            "body": _GET_METHOD_NOT_ALLOWED_BODY,
            "more_body": False,
        }
    )


def _parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _prepare_transport_security() -> None:
    # Vercel and other public hosts are not localhost; this is required for a
    # deployed Streamable HTTP transport to accept non-loopback traffic.
    disable_dns_protection = os.getenv("NCS_MCP_DISABLE_DNS_REBINDING_PROTECTION", "1")
    if disable_dns_protection.strip().lower() in {"1", "true", "on", "yes", "y"}:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        return

    allowed_hosts = _parse_csv_list(os.getenv("NCS_MCP_ALLOWED_HOSTS"))
    allowed_origins = _parse_csv_list(os.getenv("NCS_MCP_ALLOWED_ORIGINS"))
    if allowed_hosts or allowed_origins:
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        )


def _app_with_path_prefix_fix() -> object:
    base_app = mcp.streamable_http_app()
    streamable_path = getattr(mcp.settings, "streamable_http_path", "/mcp")
    lifespan_context = base_app.router.lifespan_context(base_app)
    lifespan_started = False
    lifespan_lock = asyncio.Lock()

    async def _ensure_lifespan_ready() -> None:
        nonlocal lifespan_started
        if lifespan_started:
            return
        async with lifespan_lock:
            if lifespan_started:
                return
            await lifespan_context.__aenter__()
            lifespan_started = True

    async def app(scope, receive, send) -> None:
        if scope.get("type") == "http":
            # This stateless service does not emit server-initiated MCP
            # messages.  MCP Streamable HTTP therefore permits an immediate
            # 405 for the optional standalone GET stream.  Rejecting it before
            # lifespan startup also prevents the SDK's unbounded SSE receive
            # loop from occupying a Vercel function until maxDuration.
            if str(scope.get("method", "")).upper() == "GET":
                await _reject_standalone_get(send)
                return
            if is_vercel_read_only_configuration() and not _MCP_BOOTSTRAP_READY:
                await _reject_unavailable_mcp(send)
                return
            await _ensure_lifespan_ready()
            path = scope.get("path", "/") or "/"
            if path == "/" or path == "":
                scope["path"] = streamable_path
            elif path == "/api":
                scope["path"] = streamable_path
            elif path == "/api/mcp":
                scope["path"] = streamable_path
            elif path.startswith("/api/mcp/"):
                scope["path"] = streamable_path
            elif path == "/api/index":
                scope["path"] = streamable_path
        await base_app(scope, receive, send)

    return app


def _configure_for_vercel() -> None:
    started = time.perf_counter()
    host = os.getenv("NCS_MCP_HOST", "127.0.0.1")
    configure_transport(
        transport="streamable-http",
        host=host,
        stateful_http=False,
        allow_remote_bind=True,
    )
    # Keep the MCP path at /mcp (exposed as https://.../api/mcp by Vercel).
    streamable_http_path = os.getenv("NCS_MCP_STREAMABLE_HTTP_PATH", "/mcp")
    mcp.settings.streamable_http_path = streamable_http_path

    _prepare_transport_security()
    merge_bootstrap_metrics(
        {
            "stages_ms": {
                "transport_config": round(
                    (time.perf_counter() - started) * 1000, 3
                )
            },
            "transport": {
                "mode": "streamable-http",
                "endpoint": streamable_http_path,
            },
        }
    )


_configure_for_vercel()

_APP_STARTED = time.perf_counter()
app = _app_with_path_prefix_fix()
merge_bootstrap_metrics(
    {
        "ready": _MCP_BOOTSTRAP_READY,
        "status": "ready" if _MCP_BOOTSTRAP_READY else "not_ready",
        "stages_ms": {
            "app_construction": round((time.perf_counter() - _APP_STARTED) * 1000, 3),
            "module_import_total": round(
                (time.perf_counter() - _MODULE_IMPORT_STARTED) * 1000, 3
            ),
        },
        "mcp_surface_initialized": True,
    }
)
