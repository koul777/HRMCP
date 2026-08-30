"""Shared Vercel bootstrap logic for MCP and public probe routes."""

from __future__ import annotations

import logging
import os
import shutil
import time
import urllib.request
from pathlib import Path
from threading import RLock
from typing import Any

from .bootstrap_state import get_bootstrap_metrics, record_bootstrap_metrics
from ncs_mcp.runtime_readiness import (
    clear_verified_readiness_counts,
    configure_verified_readiness_counts,
)
from ncs_mcp.vercel_snapshot import (
    COMPACT_ARCHIVE_NAME,
    COMPACT_MANIFEST_NAME,
    COMPACT_SNAPSHOT_NAME,
    external_db_override_allowed,
    load_compact_manifest,
    materialize_compact_snapshot,
    readiness_required_min_rows,
    readiness_required_tables,
    sqlite_snapshot_is_usable,
)


LOGGER = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[1]
_BOOTSTRAP_LOCK = RLock()


def _stage(stages: dict[str, float], name: str, started: float) -> None:
    stages[name] = round((time.perf_counter() - started) * 1000, 3)


def is_vercel_read_only_configuration() -> bool:
    truthy = {"1", "true", "on", "yes", "y"}
    return (
        os.getenv("VERCEL", "").strip().lower() in truthy
        and os.getenv("NCS_MCP_READ_ONLY", "").strip().lower() in truthy
    )


def _bootstrap_db_from_url(
    *,
    required_tables: tuple[str, ...],
    minimum_rows: dict[str, int],
    stages_ms: dict[str, float],
) -> bool:
    if not external_db_override_allowed():
        return False
    download_url = os.getenv("NCS_DB_URL")
    if not download_url:
        return False

    db_path = Path(os.getenv("NCS_DB_PATH", "/tmp/ncs_interview_serving.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    validate_started = time.perf_counter()
    if sqlite_snapshot_is_usable(
        db_path,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    ):
        _stage(stages_ms, "url_override_cached_validate", validate_started)
        os.environ["NCS_DB_PATH"] = str(db_path)
        return True
    _stage(stages_ms, "url_override_cached_validate", validate_started)

    tmp_path = db_path.with_suffix(db_path.suffix + ".download")
    try:
        download_started = time.perf_counter()
        with urllib.request.urlopen(download_url, timeout=120) as response:
            with tmp_path.open("wb") as output:
                shutil.copyfileobj(response, output)
        _stage(stages_ms, "url_override_download", download_started)
        validate_started = time.perf_counter()
        if not sqlite_snapshot_is_usable(
            tmp_path,
            required_tables=required_tables,
            minimum_rows=minimum_rows,
        ):
            _stage(stages_ms, "url_override_download_validate", validate_started)
            LOGGER.error("Explicit remote Vercel DB override failed validation")
            return False
        _stage(stages_ms, "url_override_download_validate", validate_started)
        publish_started = time.perf_counter()
        tmp_path.replace(db_path)
        _stage(stages_ms, "url_override_publish", publish_started)
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
    stages_ms: dict[str, float],
) -> bool:
    if not external_db_override_allowed():
        return False
    raw_path = os.getenv("NCS_DB_PATH", "").strip()
    if not raw_path:
        return False
    validate_started = time.perf_counter()
    ready = sqlite_snapshot_is_usable(
        Path(raw_path),
        required_tables=required_tables,
        minimum_rows=minimum_rows,
    )
    _stage(stages_ms, "explicit_path_override_validate", validate_started)
    return ready


def _bootstrap_db_from_local_snapshot(
    *,
    required_tables: tuple[str, ...],
    minimum_rows: dict[str, int],
) -> tuple[bool, dict[str, Any]]:
    archive_path = _ROOT / "api" / COMPACT_ARCHIVE_NAME
    manifest_path = _ROOT / "api" / COMPACT_MANIFEST_NAME
    runtime_db = Path("/tmp") / COMPACT_SNAPSHOT_NAME
    metrics: dict[str, Any] = {}
    clear_verified_readiness_counts()
    ready = materialize_compact_snapshot(
        archive_path,
        manifest_path,
        runtime_db,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
        metrics=metrics,
    )
    os.environ["NCS_DB_PATH"] = str(runtime_db)
    fast_path_configured = False
    if ready:
        try:
            manifest = load_compact_manifest(manifest_path)
            table_counts: dict[str, int] = {}
            # Only counts tied to a physical table or canonical serving object
            # are eligible for readiness. Logical aggregates alone do not
            # prove that the runtime object exists with the advertised rows.
            for count_kind in ("physical_counts", "servable_counts"):
                table_counts.update(
                    {
                        str(table_name): int(row_count)
                        for table_name, row_count in manifest[count_kind].items()
                    }
                )
            fast_path_configured = configure_verified_readiness_counts(
                runtime_db,
                sqlite_sha256=str(manifest["sqlite_sha256"]),
                sqlite_bytes=int(manifest["sqlite_bytes"]),
                table_counts=table_counts,
                required_tables=required_tables,
                minimum_rows=minimum_rows,
            )
            if not fast_path_configured:
                LOGGER.warning(
                    "Verified compact counts were not eligible for readiness fast path; using SQL fallback"
                )
        except (KeyError, OSError, TypeError, ValueError):
            clear_verified_readiness_counts()
            LOGGER.exception(
                "Unable to configure verified compact readiness metadata; using SQL fallback"
            )
    metrics["readiness_fast_path_configured"] = fast_path_configured
    if not ready:
        LOGGER.error(
            "Bundled compact ontology snapshot is missing or failed validation: %s",
            archive_path,
        )
    return ready, metrics


def ensure_bootstrap() -> dict[str, object]:
    with _BOOTSTRAP_LOCK:
        current = get_bootstrap_metrics()
        if current.get("status") in {"ready", "not_ready"}:
            return current

        clear_verified_readiness_counts()
        started = time.perf_counter()
        stages_ms: dict[str, float] = {}
        try:
            required_tables = readiness_required_tables()
            minimum_rows = readiness_required_min_rows()
        except ValueError as exc:
            return record_bootstrap_metrics(
                {
                    "schema": "ncs_vercel_bootstrap_metrics_v2",
                    "status": "not_ready",
                    "source": None,
                    "ready": False,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "stages_ms": stages_ms,
                    "required_tables": [],
                    "minimum_rows": {},
                    "read_only_configuration": is_vercel_read_only_configuration(),
                    "process_level_metrics": True,
                    "request_level_metrics": False,
                    "error": {
                        "code": "invalid_readiness_configuration",
                        "type": type(exc).__name__,
                    },
                }
            )

        source = "local_snapshot"
        ready = False
        snapshot_metrics: dict[str, Any] = {}
        if _bootstrap_db_from_url(
            required_tables=required_tables,
            minimum_rows=minimum_rows,
            stages_ms=stages_ms,
        ):
            source = "url_override"
            ready = True
        elif _bootstrap_db_from_explicit_path(
            required_tables=required_tables,
            minimum_rows=minimum_rows,
            stages_ms=stages_ms,
        ):
            source = "explicit_path_override"
            ready = True
        else:
            ready, snapshot_metrics = _bootstrap_db_from_local_snapshot(
                required_tables=required_tables,
                minimum_rows=minimum_rows,
            )
            stages_ms.update(
                {
                    key: value
                    for key, value in (snapshot_metrics.get("stages_ms") or {}).items()
                    if isinstance(value, (int, float))
                }
            )

        payload: dict[str, Any] = {
            "schema": "ncs_vercel_bootstrap_metrics_v2",
            "status": "ready" if ready else "not_ready",
            "source": source,
            "ready": ready,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            "stages_ms": stages_ms,
            "required_tables": list(required_tables),
            "minimum_rows": minimum_rows,
            "read_only_configuration": is_vercel_read_only_configuration(),
            "process_level_metrics": True,
            "request_level_metrics": False,
        }
        if snapshot_metrics:
            payload["local_snapshot"] = {
                key: value
                for key, value in snapshot_metrics.items()
                if key != "stages_ms"
            }
        if not ready:
            payload["error"] = {"code": "no_verified_snapshot"}
        return record_bootstrap_metrics(payload)
