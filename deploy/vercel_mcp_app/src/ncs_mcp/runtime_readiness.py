from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from ncs_mcp.config import load_settings


READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)
READINESS_PUBLIC_TOOL_TABLES = (
    "classifications",
    "competency_elements",
    "ncs_training_course_unit_links",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
    "ncs_career_paths",
    "ncs_qualification_items",
    "ncs_unit_qualification_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_unit_job_base_links",
    "ontology_concepts",
    "ontology_concept_aliases",
)
READINESS_CAPABILITY_TABLES = {
    "structure_search": ("classifications", "competency_elements"),
    "training": (
        "ncs_training_courses",
        "ncs_training_course_unit_links",
        "ncs_training_course_concept_links",
        "ncs_training_course_element_links",
        "training_goal_concept_links",
        "training_delivery_relations",
    ),
    "career_path": ("ncs_career_paths",),
    "qualification": ("ncs_qualification_items", "ncs_unit_qualification_links"),
    "job_base": (
        "ncs_job_base_competencies",
        "ncs_job_base_factors",
        "ncs_unit_job_base_links",
    ),
    "ontology": ("ontology_concepts", "ontology_concept_aliases"),
}
READINESS_EXTRA_TABLES_ENV = "NCS_MCP_READINESS_EXTRA_TABLES"
READINESS_MIN_ROWS_ENV = "NCS_MCP_READINESS_MIN_ROWS"
_SQLITE_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERIFIED_READINESS_LOCK = RLock()
_VERIFIED_READINESS_STATE: dict[str, Any] | None = None


def _readiness_required_tables() -> tuple[tuple[str, ...], list[str]]:
    required_tables = list(READINESS_CORE_TABLES)
    seen = {table_name.casefold() for table_name in required_tables}
    invalid_extra_tables: list[str] = []
    invalid_seen: set[str] = set()

    for raw_table_name in os.environ.get(READINESS_EXTRA_TABLES_ENV, "").split(","):
        table_name = raw_table_name.strip()
        if not table_name:
            continue
        if _SQLITE_IDENTIFIER_RE.fullmatch(table_name) is None:
            if table_name not in invalid_seen:
                invalid_extra_tables.append(table_name)
                invalid_seen.add(table_name)
            continue
        normalized_name = table_name.casefold()
        if normalized_name in seen:
            continue
        required_tables.append(table_name)
        seen.add(normalized_name)

    return tuple(required_tables), invalid_extra_tables


def _readiness_minimum_rows_contract() -> dict[str, int] | None:
    raw_value = os.environ.get(READINESS_MIN_ROWS_ENV, "").strip()
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    minimum_rows: dict[str, int] = {}
    for raw_name, raw_count in payload.items():
        table_name = str(raw_name).strip()
        if _SQLITE_IDENTIFIER_RE.fullmatch(table_name) is None or isinstance(raw_count, bool):
            return None
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            return None
        if count < 1:
            return None
        minimum_rows[table_name] = count
    return minimum_rows


def _readiness_file_fingerprint(db_path: Path) -> tuple[Any, ...]:
    resolved = db_path.resolve()
    stat = resolved.stat()
    return (
        os.path.normcase(str(resolved)),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ctime_ns", 0)),
        int(getattr(stat, "st_dev", 0)),
        int(getattr(stat, "st_ino", 0)),
    )


def clear_verified_readiness_counts() -> None:
    """Clear process-local counts derived from a verified bundled snapshot."""

    global _VERIFIED_READINESS_STATE
    with _VERIFIED_READINESS_LOCK:
        _VERIFIED_READINESS_STATE = None


def invalidate_verified_readiness_counts(db_path: Path | str | None = None) -> bool:
    """Invalidate verified counts unconditionally or for their bound DB path."""

    global _VERIFIED_READINESS_STATE
    with _VERIFIED_READINESS_LOCK:
        state = _VERIFIED_READINESS_STATE
        if state is None:
            return False
        if db_path is not None:
            try:
                requested_path = os.path.normcase(str(Path(db_path).resolve()))
            except OSError:
                requested_path = os.path.normcase(str(Path(db_path)))
            if requested_path != state["file_fingerprint"][0]:
                return False
        _VERIFIED_READINESS_STATE = None
        return True


def configure_verified_readiness_counts(
    db_path: Path | str,
    *,
    sqlite_sha256: str,
    sqlite_bytes: int,
    table_counts: Mapping[str, int],
    required_tables: tuple[str, ...],
    minimum_rows: Mapping[str, int] | None = None,
) -> bool:
    """Bind exact manifest counts to one content-verified bundled DB file.

    Callers may configure this state only after compact materialization has
    validated archive SHA, embedded metadata, the external manifest, required
    objects, and minimum rows. Invalid or incomplete inputs leave the ordinary
    read-only SQL readiness scan as the only path.
    """

    global _VERIFIED_READINESS_STATE
    clear_verified_readiness_counts()
    try:
        path = Path(db_path)
        digest = str(sqlite_sha256).strip().lower()
        if _SHA256_RE.fullmatch(digest) is None:
            return False
        if isinstance(sqlite_bytes, bool) or int(sqlite_bytes) <= 0:
            return False
        fingerprint = _readiness_file_fingerprint(path)
        if fingerprint[1] != int(sqlite_bytes):
            return False

        normalized_counts: dict[str, int] = {}
        for raw_name, raw_count in table_counts.items():
            table_name = str(raw_name).strip()
            if _SQLITE_IDENTIFIER_RE.fullmatch(table_name) is None or isinstance(raw_count, bool):
                return False
            count = int(raw_count)
            if count < 0:
                return False
            normalized_counts[table_name] = count

        normalized_required = tuple(str(name).strip() for name in required_tables)
        if not normalized_required or len(set(normalized_required)) != len(normalized_required):
            return False
        if any(_SQLITE_IDENTIFIER_RE.fullmatch(name) is None for name in normalized_required):
            return False

        minima: dict[str, int] = {}
        for raw_name, raw_count in dict(minimum_rows or {}).items():
            table_name = str(raw_name).strip()
            if _SQLITE_IDENTIFIER_RE.fullmatch(table_name) is None or isinstance(raw_count, bool):
                return False
            count = int(raw_count)
            if count < 1:
                return False
            minima[table_name] = count

        current_required, invalid_extra_tables = _readiness_required_tables()
        current_minima = _readiness_minimum_rows_contract()
        if invalid_extra_tables or current_minima is None:
            return False
        if normalized_required != current_required or minima != current_minima:
            return False

        covered_tables = set(normalized_required) | set(READINESS_PUBLIC_TOOL_TABLES) | set(minima)
        if any(table_name not in normalized_counts for table_name in covered_tables):
            return False
        if any(normalized_counts[table_name] < 1 for table_name in normalized_required):
            return False
        if any(normalized_counts[table_name] < minimum for table_name, minimum in minima.items()):
            return False
    except (OSError, TypeError, ValueError):
        return False

    state = {
        "sqlite_sha256": digest,
        "sqlite_bytes": int(sqlite_bytes),
        "file_fingerprint": fingerprint,
        "table_counts": normalized_counts,
        "required_tables": normalized_required,
        "minimum_rows": minima,
    }
    with _VERIFIED_READINESS_LOCK:
        _VERIFIED_READINESS_STATE = state
    return True


def _verified_readiness_counts(
    db_path: Path,
    required_tables: tuple[str, ...],
) -> dict[str, int] | None:
    global _VERIFIED_READINESS_STATE
    with _VERIFIED_READINESS_LOCK:
        state = _VERIFIED_READINESS_STATE
        if state is None:
            return None
        current_minima = _readiness_minimum_rows_contract()
        try:
            fingerprint = _readiness_file_fingerprint(db_path)
        except OSError:
            _VERIFIED_READINESS_STATE = None
            return None
        if (
            state["required_tables"] != required_tables
            or current_minima is None
            or state["minimum_rows"] != current_minima
            or state["file_fingerprint"] != fingerprint
        ):
            _VERIFIED_READINESS_STATE = None
            return None
        return dict(state["table_counts"])


def _apply_verified_readiness_counts(
    result: dict[str, Any],
    db_path: Path,
    required_tables: tuple[str, ...],
) -> bool:
    counts = _verified_readiness_counts(db_path, required_tables)
    if counts is None:
        return False
    result["openable"] = True
    result["readiness_count_source"] = "verified_snapshot_metadata"
    result["core_tables"] = {
        table_name: {"exists": True, "row_count": counts[table_name]}
        for table_name in required_tables
    }
    result["public_tool_tables"] = {
        table_name: {"exists": True, "has_rows": counts[table_name] > 0}
        for table_name in READINESS_PUBLIC_TOOL_TABLES
    }
    core_ready = all(
        item["exists"] and int(item["row_count"] or 0) > 0
        for item in result["core_tables"].values()
    ) and len(result["core_tables"]) == len(required_tables)
    capabilities: dict[str, dict[str, Any]] = {}
    degraded_capabilities: list[str] = []
    for capability, table_names in READINESS_CAPABILITY_TABLES.items():
        missing_tables: list[str] = []
        empty_tables: list[str] = []
        for table_name in table_names:
            table_state = (
                result["core_tables"].get(table_name)
                or result["public_tool_tables"].get(table_name)
                or {}
            )
            if not table_state.get("exists"):
                missing_tables.append(table_name)
                continue
            has_rows = table_state.get("has_rows")
            if has_rows is None:
                has_rows = int(table_state.get("row_count") or 0) > 0
            if not has_rows:
                empty_tables.append(table_name)
        available = not missing_tables and not empty_tables
        capabilities[capability] = {
            "available": available,
            "missing_tables": missing_tables,
            "empty_tables": empty_tables,
        }
        if not available:
            degraded_capabilities.append(capability)
    result["ready"] = core_ready
    result["core_ready"] = core_ready
    result["public_tools_ready"] = not degraded_capabilities
    result["capabilities"] = capabilities
    result["degraded_capabilities"] = degraded_capabilities
    if not result["ready"]:
        result["error"] = {"code": "database_not_ready"}
    return True


def database_readiness_metadata(db_path: Path | str | None) -> dict[str, Any]:
    path = Path(db_path) if db_path else None
    required_tables, invalid_extra_tables = _readiness_required_tables()
    configured = path is not None
    exists = bool(path and path.exists())
    result: dict[str, Any] = {
        "configured": configured,
        "exists": exists,
        "openable": False,
        "ready": False,
        "required_tables": list(required_tables),
        "core_tables": {},
        "public_tool_tables": {},
        "readiness_count_source": "sql_count",
    }
    if invalid_extra_tables:
        result["invalid_extra_tables"] = invalid_extra_tables
    if not configured:
        result["error"] = {"code": "database_not_configured"}
        return result
    if not exists:
        result["error"] = {"code": "database_missing"}
        return result
    if _apply_verified_readiness_counts(result, path, required_tables):
        return result
    try:
        db_uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        conn = sqlite3.connect(db_uri, uri=True)
        try:
            conn.execute("PRAGMA query_only = ON")
            result["openable"] = True
            for table_name in required_tables:
                exists_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if exists_row is None:
                    result["core_tables"][table_name] = {"exists": False, "row_count": None}
                    continue
                row_count = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
                )
                result["core_tables"][table_name] = {"exists": True, "row_count": row_count}
            for table_name in READINESS_PUBLIC_TOOL_TABLES:
                exists_row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if exists_row is None:
                    result["public_tool_tables"][table_name] = {
                        "exists": False,
                        "has_rows": False,
                    }
                    continue
                has_rows = conn.execute(
                    f'SELECT 1 FROM "{table_name}" LIMIT 1'
                ).fetchone() is not None
                result["public_tool_tables"][table_name] = {
                    "exists": True,
                    "has_rows": has_rows,
                }
            minimum_rows = _readiness_minimum_rows_contract()
            core_ready = (
                minimum_rows is not None
                and all(
                    item.get("exists")
                    and int(item.get("row_count") or 0)
                    >= minimum_rows.get(table_name, 1)
                    for table_name, item in result["core_tables"].items()
                )
                and len(result["core_tables"]) == len(required_tables)
            )
            capabilities: dict[str, dict[str, Any]] = {}
            degraded_capabilities: list[str] = []
            for capability, table_names in READINESS_CAPABILITY_TABLES.items():
                missing_tables: list[str] = []
                empty_tables: list[str] = []
                for table_name in table_names:
                    table_state = (
                        result["core_tables"].get(table_name)
                        or result["public_tool_tables"].get(table_name)
                        or {}
                    )
                    if not table_state.get("exists"):
                        missing_tables.append(table_name)
                        continue
                    has_rows = table_state.get("has_rows")
                    if has_rows is None:
                        has_rows = int(table_state.get("row_count") or 0) > 0
                    if not has_rows:
                        empty_tables.append(table_name)
                available = not missing_tables and not empty_tables
                capabilities[capability] = {
                    "available": available,
                    "missing_tables": missing_tables,
                    "empty_tables": empty_tables,
                }
                if not available:
                    degraded_capabilities.append(capability)
            public_tools_ready = not degraded_capabilities
            result["ready"] = core_ready
            result["core_ready"] = core_ready
            result["public_tools_ready"] = public_tools_ready
            result["capabilities"] = capabilities
            result["degraded_capabilities"] = degraded_capabilities
            if not result["ready"]:
                result["error"] = {"code": "database_not_ready"}
        finally:
            conn.close()
    except Exception as exc:  # pragma: no cover - defensive health path
        result["error"] = {"code": "database_unopenable", "type": type(exc).__name__}
    return result


def runtime_health_metadata() -> dict[str, Any]:
    settings = load_settings()
    read_only_mode = bool(getattr(settings, "read_only_mode", False))
    operator_tools_requested = bool(settings.operator_tools_enabled)
    operator_tools_enabled = operator_tools_requested and not read_only_mode
    max_concurrent_recommendations = int(
        getattr(settings, "max_concurrent_recommendations", 2)
    )
    api_keys = {
        "service_key_present": bool(settings.service_key),
        "training_course_service_key_present": bool(settings.training_course_service_key),
        "qualification_service_key_present": bool(settings.qualification_service_key),
        "job_base_service_key_present": bool(settings.job_base_service_key),
        "sqf_service_key_present": bool(settings.sqf_service_key),
        "study_module_service_key_present": bool(settings.study_module_service_key),
    }
    return {
        "database": database_readiness_metadata(settings.db_path),
        "operator_tools_enabled": operator_tools_enabled,
        "operator_tools_requested": operator_tools_requested,
        "operator_tools_blocked_by_read_only": operator_tools_requested and read_only_mode,
        "read_only_mode": read_only_mode,
        "max_concurrent_recommendations": max_concurrent_recommendations,
        "recommendation_queue_timeout_seconds": float(
            getattr(settings, "recommendation_queue_timeout_seconds", 30.0)
        ),
        "api_keys": api_keys,
        "api_key_present_count": sum(1 for present in api_keys.values() if present),
    }
