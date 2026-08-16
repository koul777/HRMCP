"""Vercel Python function entrypoint for the NCS MCP server."""

from __future__ import annotations

import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# Ensure local source package import works in Vercel function runtime.
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mcp.server.transport_security import TransportSecuritySettings

from ncs_mcp.server import configure_transport, mcp


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


def _bootstrap_db_from_url() -> None:
    # If deployment wants a prebuilt serving DB directly in Vercel, set
    # NCS_DB_URL and (optionally) NCS_DB_PATH.
    download_url = os.getenv("NCS_DB_URL")
    if not download_url:
        return

    db_path = Path(os.getenv("NCS_DB_PATH", "/tmp/ncs_interview_serving.db"))
    if not os.getenv("NCS_DB_PATH"):
        os.environ["NCS_DB_PATH"] = str(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists() and db_path.stat().st_size > 0:
        return

    tmp_path = db_path.with_suffix(db_path.suffix + ".download")
    with urllib.request.urlopen(download_url, timeout=120) as response:
        with tmp_path.open("wb") as output:
            shutil.copyfileobj(response, output)
    tmp_path.replace(db_path)


def _bootstrap_db_from_local_snapshot() -> None:
    # Optional local fallback if a prebuilt file is shipped in repo (eg.
    # tmp/ncs_interview_serving_release.db) but not yet copied to NCS_DB_PATH.
    env_path = os.getenv("NCS_DB_PATH")
    if env_path:
        db_path = Path(env_path)
    else:
        packaged_candidates = [
            _ROOT / "api" / "ncs_interview_db.zip",
            _ROOT / "api" / "ncs_interview_serving_release.db",
            _ROOT / "tmp" / "ncs_interview_serving_release.db",
            _ROOT / "tmp" / "ncs_interview_serving_test.db",
            _ROOT / "data" / "processed" / "ncs.db",
            _ROOT / "data" / "processed" / "ci-smoke" / "ncs.db",
        ]
        for candidate in packaged_candidates:
            if candidate.exists() and candidate.stat().st_size > 0:
                if candidate.suffix == ".zip":
                    extract_to = Path("/tmp")
                    preferred_path = extract_to / "ncs_interview_serving_release.db"
                    if preferred_path.exists() and preferred_path.stat().st_size > 0:
                        os.environ["NCS_DB_PATH"] = str(preferred_path)
                        return
                    with zipfile.ZipFile(candidate, "r") as zf:
                        try:
                            target_name = next(
                                (
                                    name
                                    for name in zf.namelist()
                                    if name.lower().endswith(".db")
                                ),
                                None,
                            )
                            if target_name is None:
                                target_name = zf.namelist()[0]
                            zf.extract(target_name, extract_to)
                            extracted_path = extract_to / target_name
                            if extracted_path.suffix.lower() != ".db":
                                extracted_path = extracted_path.with_suffix(".db")
                            # move/rename nested path to a stable location
                            if extracted_path != preferred_path:
                                try:
                                    preferred_path.parent.mkdir(parents=True, exist_ok=True)
                                    if preferred_path.exists():
                                        preferred_path.unlink()
                                    extracted_path.rename(preferred_path)
                                except Exception:
                                    preferred_path = extracted_path
                        except Exception:
                            continue
                    if preferred_path.exists() and preferred_path.stat().st_size > 0:
                        os.environ["NCS_DB_PATH"] = str(preferred_path)
                        return
                    continue
                db_path = candidate
                os.environ["NCS_DB_PATH"] = str(db_path)
                return

        db_path = Path("/tmp/ncs_interview_serving.db")
        os.environ["NCS_DB_PATH"] = str(db_path)

    if db_path.exists() and db_path.stat().st_size > 0:
        return

    snapshot = _ROOT / "tmp" / "ncs_interview_serving_release.db"
    if snapshot.exists() and snapshot.stat().st_size > 0:
        try:
            shutil.copy2(snapshot, db_path)
        except Exception:
            pass


def _app_with_path_prefix_fix() -> object:
    base_app = mcp.streamable_http_app()
    streamable_path = getattr(mcp.settings, "streamable_http_path", "/mcp")

    async def app(scope, receive, send) -> None:
        if scope.get("type") == "http":
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
    # Order: first local fallback (for repository-packaged serving DB), then
    # optional remote override.
    _bootstrap_db_from_local_snapshot()
    _bootstrap_db_from_url()


_configure_for_vercel()

app = _app_with_path_prefix_fix()
