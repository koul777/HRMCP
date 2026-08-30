"""Vercel Python function entrypoint for the NCS MCP server."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
import urllib.request
from pathlib import Path

# Ensure local source package import works in Vercel function runtime.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.transport_security import TransportSecuritySettings

from ncs_mcp.server import configure_transport, mcp
from ncs_mcp.vercel_snapshot import (
    COMPACT_ARCHIVE_NAME,
    COMPACT_MANIFEST_NAME,
    COMPACT_SNAPSHOT_NAME,
    external_db_override_allowed,
    materialize_compact_snapshot,
    readiness_required_min_rows,
    readiness_required_tables,
    sqlite_snapshot_is_usable,
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

_MCP_BOOTSTRAP_READY = True
_MCP_BOOTSTRAP_METRICS: dict[str, object] = {
    "schema": "ncs_vercel_bootstrap_metrics_v1",
    "ready": None,
}


def _is_vercel_read_only_configuration() -> bool:
    """Return whether the deployed, read-only Vercel contract is active."""

    truthy = {"1", "true", "on", "yes", "y"}
    return (
        os.getenv("VERCEL", "").strip().lower() in truthy
        and os.getenv("NCS_MCP_READ_ONLY", "").strip().lower() in truthy
    )


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


def _bootstrap_db_from_url(
    *,
    required_tables: tuple[str, ...],
    minimum_rows: dict[str, int],
) -> bool:
    # A remote database can replace the bundled release only when the operator
    # explicitly enables that behavior.  The default deployment never performs
    # a DB download and therefore has no network/bootstrap dependency.
    if not external_db_override_allowed():
        return False
    download_url = os.getenv("NCS_DB_URL")
    if not download_url:
        return False

    db_path = Path(os.getenv("NCS_DB_PATH", "/tmp/ncs_interview_serving.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if sqlite_snapshot_is_usable(
        db_path,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    ):
        os.environ["NCS_DB_PATH"] = str(db_path)
        return True

    tmp_path = db_path.with_suffix(db_path.suffix + ".download")
    try:
        with urllib.request.urlopen(download_url, timeout=120) as response:
            with tmp_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        if not sqlite_snapshot_is_usable(
            tmp_path,
            required_tables=required_tables,
            minimum_rows=minimum_rows,
        ):
            LOGGER.error("Explicit remote Vercel DB override failed validation")
            return False
        tmp_path.replace(db_path)
        os.environ["NCS_DB_PATH"] = str(db_path)
        return True
    except OSError:
        LOGGER.exception("Unable to download the explicitly enabled remote Vercel DB")
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def _bootstrap_db_from_explicit_path(
    *,
    required_tables: tuple[str, ...],
    minimum_rows: dict[str, int],
) -> bool:
    if not external_db_override_allowed():
        return False
    raw_path = os.getenv("NCS_DB_PATH", "").strip()
    if not raw_path:
        return False
    db_path = Path(raw_path)
    return sqlite_snapshot_is_usable(
        db_path,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    )


def _bootstrap_db_from_local_snapshot(
    *,
    required_tables: tuple[str, ...],
    minimum_rows: dict[str, int],
) -> bool:
    # The standard function bundles only a compressed snapshot and its signed-
    # by-content sidecar.  Materialize once per warm instance into /tmp; never
    # place the raw database in the function package.
    archive_path = _ROOT / "api" / COMPACT_ARCHIVE_NAME
    manifest_path = _ROOT / "api" / COMPACT_MANIFEST_NAME
    runtime_db = Path("/tmp") / COMPACT_SNAPSHOT_NAME
    if materialize_compact_snapshot(
        archive_path,
        manifest_path,
        runtime_db,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    ):
        os.environ["NCS_DB_PATH"] = str(runtime_db)
        return True

    LOGGER.error(
        "Bundled compact ontology snapshot is missing or failed validation: %s",
        archive_path,
    )
    os.environ["NCS_DB_PATH"] = str(runtime_db)
    return False


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
            if _is_vercel_read_only_configuration() and not _MCP_BOOTSTRAP_READY:
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
    required_tables = readiness_required_tables()
    minimum_rows = readiness_required_min_rows()
    source = "local_snapshot"
    ready = False
    if _bootstrap_db_from_url(
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    ):
        source = "url_override"
        ready = True
    elif _bootstrap_db_from_explicit_path(
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    ):
        source = "explicit_path_override"
        ready = True
    else:
        global _MCP_BOOTSTRAP_READY
        _MCP_BOOTSTRAP_READY = _bootstrap_db_from_local_snapshot(
            required_tables=required_tables,
            minimum_rows=minimum_rows,
        )
        ready = _MCP_BOOTSTRAP_READY
    global _MCP_BOOTSTRAP_METRICS
    _MCP_BOOTSTRAP_METRICS = {
        "schema": "ncs_vercel_bootstrap_metrics_v1",
        "source": source,
        "ready": ready,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "required_tables": list(required_tables),
        "minimum_rows": minimum_rows,
        "read_only_configuration": _is_vercel_read_only_configuration(),
    }


_configure_for_vercel()

app = _app_with_path_prefix_fix()
