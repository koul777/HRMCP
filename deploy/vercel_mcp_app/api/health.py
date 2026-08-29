"""Public health endpoint for Vercel deployment."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure local source package import works in Vercel function runtime.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ncs_mcp.server import runtime_health_metadata
from starlette.responses import JSONResponse


async def app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        return
    runtime = runtime_health_metadata()
    response = JSONResponse(
        {
            "status": (
                "ok"
                if runtime["database"]["ready"]
                and runtime["database"].get("public_tools_ready", False)
                else "degraded"
            ),
            "name": "ncs-mcp",
            "transport": "streamable-http",
            "endpoint": "/mcp",
            "runtime": runtime,
        }
    )
    await response(scope, receive, send)
