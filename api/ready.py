"""Public readiness endpoint for Vercel deployment."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure local source package import works in Vercel function runtime.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from api.mcp import bootstrap_metrics
from ncs_mcp.server import runtime_health_metadata
from starlette.responses import JSONResponse


async def app(scope, receive, send) -> None:
    if scope.get("type") != "http":
        return
    runtime = runtime_health_metadata()
    ready = bool(runtime["database"]["ready"])
    response = JSONResponse(
        {
            "status": "ready" if ready else "not_ready",
            "name": "ncs-mcp",
            "bootstrap": bootstrap_metrics(),
            "runtime": runtime,
        },
        status_code=200 if ready else 503,
    )
    await response(scope, receive, send)
