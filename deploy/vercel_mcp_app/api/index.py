"""Vercel function entrypoint for API multiplexing."""

from __future__ import annotations


_GET_METHOD_NOT_ALLOWED_BODY = (
    b'{"jsonrpc":"2.0","id":"server-error","error":{"code":-32600,'
    b'"message":"Method Not Allowed: standalone MCP GET streams are not enabled; use POST"}}'
)


async def _reject_standalone_mcp_get(send) -> None:
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


async def app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        return

    path = scope.get("path", "/") or "/"
    if (
        str(scope.get("method", "")).upper() == "GET"
        and (path in {"/api/mcp", "/api/mcp/"} or path.startswith("/api/mcp/"))
    ):
        # Keep unsupported standalone GET traffic out of the MCP SDK and out
        # of the database bootstrap path.  POST imports the full app lazily.
        await _reject_standalone_mcp_get(send)
        return
    if path in {"/api/health", "/api/health/"}:
        from api.health import app as _health_app

        await _health_app(scope, receive, send)
        return
    if path in {"/api/ready", "/api/ready/"}:
        from api.ready import app as _ready_app

        await _ready_app(scope, receive, send)
        return

    from api.mcp import app as _mcp_app

    await _mcp_app(scope, receive, send)
