"""Local-only Docker credential bridge for the optional Neo4j Gold runtime.

The bridge reads one fixed local Docker container with ``docker inspect``,
keeps its authentication value in process memory, and supplies only the
allowlisted ``NCS_MCP_GOLD_*`` settings to an explicitly launched child.  It
does not write an env file, start or restart Docker, or replace a production
secret manager.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
import json
import logging
import math
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from .gold_mcp import (
    ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD,
    ENV_LOCAL_EMBEDDING_DEVICE,
    ENV_LOCAL_EMBEDDING_ENABLED,
    ENV_LOCAL_EMBEDDING_MODEL,
    GOLD_MCP_ENV_ALLOWLIST,
)
from .gold_runtime import (
    ENV_CONNECT_TIMEOUT_SECONDS,
    ENV_DATABASE,
    ENV_EMBEDDING_DIMENSIONS,
    ENV_ENABLED,
    ENV_PASSWORD,
    ENV_QUERY_TIMEOUT_SECONDS,
    ENV_URI,
    ENV_USERNAME,
    ENV_VECTOR_SEARCH_CAPABILITY,
    GOLD_RUNTIME_ENV_ALLOWLIST,
    create_gold_runtime,
)
from .neo4j_gold import VECTOR_SEARCH_CAPABILITIES, VECTOR_SEARCH_CURRENT


LOCAL_GOLD_STATUS_SCHEMA = "ncs_local_gold_runtime_status_v1"
LOCAL_GOLD_CONTAINER = "ncs-mcp-neo4j-gold"
LOCAL_GOLD_CONTAINER_ALLOWLIST = frozenset({LOCAL_GOLD_CONTAINER})
LOCAL_GOLD_TARGET_ALLOWLIST = frozenset(
    {"status", "health", "probe", "mcp", "builder"}
)
LOCAL_GOLD_TRANSPORT_ALLOWLIST = frozenset({"stdio", "streamable-http"})
LOCAL_GOLD_CHILD_ENV_ALLOWLIST = frozenset(
    set(GOLD_RUNTIME_ENV_ALLOWLIST) | set(GOLD_MCP_ENV_ALLOWLIST)
)
LOCAL_GOLD_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST = frozenset({LOCAL_GOLD_EMBEDDING_MODEL})
LOCAL_GOLD_EMBEDDING_DEVICE_ALLOWLIST = frozenset({"cpu", "cuda", "mps"})

_DOCKER_INPUT_ENV_ALLOWLIST = frozenset(
    {
        "NEO4J_AUTH",
        "NEO4J_initial_dbms_default__database",
        "NEO4J_dbms_default__database",
    }
)
_BOLT_PORT_KEY = "7687/tcp"
_DEFAULT_DATABASE = "neo4j"
_DEFAULT_EMBEDDING_DIMENSIONS = 1024
_DEFAULT_INSPECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_QUERY_TIMEOUT_SECONDS = 2.0
_MAX_TIMEOUT_SECONDS = 60.0
_MAX_EMBEDDING_DIMENSIONS = 4096
_DATABASE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
_USERNAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_PROBE_ROLE_ID = "ncs:internal-role:__local_gold_read_probe__"

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class LocalGoldRuntimeError(RuntimeError):
    """Value-free local runtime failure safe for status output."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"local Gold runtime preflight failed ({code})")


@dataclass(frozen=True, slots=True)
class LocalGoldOptions:
    """Validated, non-secret inputs for the local bridge."""

    container_name: str = LOCAL_GOLD_CONTAINER
    embedding_dimensions: int = _DEFAULT_EMBEDDING_DIMENSIONS
    database: str | None = None
    vector_search_capability: str = VECTOR_SEARCH_CURRENT
    inspect_timeout_seconds: float = _DEFAULT_INSPECT_TIMEOUT_SECONDS
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS
    local_embedding_enabled: bool = True
    local_embedding_model: str = field(
        default=LOCAL_GOLD_EMBEDDING_MODEL, repr=False
    )
    local_embedding_allow_download: bool = False
    local_embedding_device: str | None = field(default="cpu", repr=False)

    def validate(self) -> None:
        if self.container_name not in LOCAL_GOLD_CONTAINER_ALLOWLIST:
            raise LocalGoldRuntimeError("container_not_allowed")
        if (
            isinstance(self.embedding_dimensions, bool)
            or not isinstance(self.embedding_dimensions, int)
            or not 1 <= self.embedding_dimensions <= _MAX_EMBEDDING_DIMENSIONS
        ):
            raise LocalGoldRuntimeError("embedding_dimensions_invalid")
        if self.database is not None and not _valid_database(self.database):
            raise LocalGoldRuntimeError("database_invalid")
        if self.vector_search_capability not in VECTOR_SEARCH_CAPABILITIES:
            raise LocalGoldRuntimeError("vector_search_capability_invalid")
        _validated_timeout(self.inspect_timeout_seconds, "inspect_timeout_invalid")
        _validated_timeout(self.connect_timeout_seconds, "connect_timeout_invalid")
        _validated_timeout(self.query_timeout_seconds, "query_timeout_invalid")
        if not isinstance(self.local_embedding_enabled, bool):
            raise LocalGoldRuntimeError("local_embedding_enabled_invalid")
        if self.local_embedding_model not in LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST:
            raise LocalGoldRuntimeError("local_embedding_model_not_allowed")
        if not isinstance(self.local_embedding_allow_download, bool):
            raise LocalGoldRuntimeError("local_embedding_allow_download_invalid")
        if (
            self.local_embedding_device is not None
            and self.local_embedding_device
            not in LOCAL_GOLD_EMBEDDING_DEVICE_ALLOWLIST
        ):
            raise LocalGoldRuntimeError("local_embedding_device_not_allowed")


def _validated_timeout(value: object, error_code: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not 0 < float(value) <= _MAX_TIMEOUT_SECONDS
    ):
        raise LocalGoldRuntimeError(error_code)
    return float(value)


def _valid_database(value: object) -> bool:
    return isinstance(value, str) and bool(_DATABASE_PATTERN.fullmatch(value))


def _default_runner(argv: Sequence[str], **kwargs: Any) -> Any:
    return subprocess.run(list(argv), **kwargs)


def inspect_local_gold_container(
    options: LocalGoldOptions | None = None,
    *,
    runner: Callable[..., Any] = _default_runner,
) -> Mapping[str, Any]:
    """Return one validated Docker inspect object without logging its payload."""

    resolved = options or LocalGoldOptions()
    resolved.validate()
    command = [
        "docker",
        "inspect",
        "--type",
        "container",
        resolved.container_name,
    ]
    try:
        completed = runner(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=resolved.inspect_timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise LocalGoldRuntimeError("docker_inspect_timeout") from None
    except (OSError, TypeError):
        raise LocalGoldRuntimeError("docker_inspect_unavailable") from None
    if getattr(completed, "returncode", 1) != 0:
        # Docker stderr can contain daemon-provided details.  Do not forward it.
        raise LocalGoldRuntimeError("docker_inspect_failed")
    try:
        payload = json.loads(str(getattr(completed, "stdout", "")))
    except (TypeError, ValueError):
        raise LocalGoldRuntimeError("docker_inspect_invalid_json") from None
    if not isinstance(payload, list) or len(payload) != 1:
        raise LocalGoldRuntimeError("docker_inspect_shape_invalid")
    container = payload[0]
    if not isinstance(container, Mapping):
        raise LocalGoldRuntimeError("docker_inspect_shape_invalid")
    inspected_name = container.get("Name")
    if not isinstance(inspected_name, str) or inspected_name.lstrip("/") != resolved.container_name:
        raise LocalGoldRuntimeError("container_identity_mismatch")
    return container


def _mapping(value: object, error_code: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalGoldRuntimeError(error_code)
    return value


def _container_state(container: Mapping[str, Any]) -> tuple[str, str]:
    state = _mapping(container.get("State"), "container_state_missing")
    status = state.get("Status")
    running = state.get("Running")
    if not isinstance(status, str) or not status:
        raise LocalGoldRuntimeError("container_state_missing")
    health_value = state.get("Health")
    health = "not_configured"
    if health_value is not None:
        health_mapping = _mapping(health_value, "container_health_invalid")
        candidate = health_mapping.get("Status")
        if candidate not in {"none", "starting", "healthy", "unhealthy"}:
            raise LocalGoldRuntimeError("container_health_invalid")
        health = str(candidate)
    if running is not True or status != "running":
        raise LocalGoldRuntimeError("container_not_running")
    if health in {"starting", "unhealthy"}:
        raise LocalGoldRuntimeError(f"container_health_{health}")
    return status, health


def _docker_environment(container: Mapping[str, Any]) -> dict[str, str]:
    config = _mapping(container.get("Config"), "container_config_missing")
    rows = config.get("Env")
    if not isinstance(rows, list) or any(not isinstance(row, str) for row in rows):
        raise LocalGoldRuntimeError("container_environment_invalid")
    values: dict[str, str] = {}
    for row in rows:
        name, separator, value = row.partition("=")
        if separator and name in _DOCKER_INPUT_ENV_ALLOWLIST:
            if name in values and values[name] != value:
                raise LocalGoldRuntimeError("container_environment_conflict")
            values[name] = value
    return values


def _docker_auth(values: Mapping[str, str]) -> tuple[str, str]:
    auth = values.get("NEO4J_AUTH")
    if not isinstance(auth, str) or not auth or auth.casefold() == "none":
        raise LocalGoldRuntimeError("neo4j_auth_missing")
    username, separator, password = auth.partition("/")
    if (
        not separator
        or not _USERNAME_PATTERN.fullmatch(username)
        or not password
        or len(password) > 1024
        or any(character in password for character in ("\x00", "\r", "\n"))
    ):
        raise LocalGoldRuntimeError("neo4j_auth_invalid")
    return username, password


def _docker_database(values: Mapping[str, str], override: str | None) -> str:
    if override is not None:
        if not _valid_database(override):
            raise LocalGoldRuntimeError("database_invalid")
        return override
    candidates = {
        values[name]
        for name in (
            "NEO4J_initial_dbms_default__database",
            "NEO4J_dbms_default__database",
        )
        if values.get(name)
    }
    if len(candidates) > 1:
        raise LocalGoldRuntimeError("neo4j_database_conflict")
    database = next(iter(candidates), _DEFAULT_DATABASE)
    if not _valid_database(database):
        raise LocalGoldRuntimeError("neo4j_database_invalid")
    return database


def _bolt_binding(container: Mapping[str, Any]) -> tuple[int, str]:
    network = _mapping(container.get("NetworkSettings"), "network_settings_missing")
    ports = _mapping(network.get("Ports"), "port_bindings_missing")
    bindings = ports.get(_BOLT_PORT_KEY)
    if not isinstance(bindings, list) or not bindings:
        raise LocalGoldRuntimeError("bolt_port_not_published")
    candidates: list[tuple[int, str]] = []
    allowed_host_ips = {"127.0.0.1", "::1"}
    for binding in bindings:
        if not isinstance(binding, Mapping):
            raise LocalGoldRuntimeError("bolt_port_binding_invalid")
        host_ip = binding.get("HostIp")
        host_port = binding.get("HostPort")
        if not isinstance(host_ip, str) or host_ip not in allowed_host_ips:
            raise LocalGoldRuntimeError("bolt_host_not_local")
        try:
            port = int(host_port)
        except (TypeError, ValueError):
            raise LocalGoldRuntimeError("bolt_port_binding_invalid") from None
        if not 1 <= port <= 65535:
            raise LocalGoldRuntimeError("bolt_port_binding_invalid")
        candidates.append((port, "loopback"))
    candidates.sort(key=lambda item: item[0])
    return candidates[0]


def local_gold_overlay(
    container: Mapping[str, Any],
    options: LocalGoldOptions | None = None,
) -> dict[str, str]:
    """Build the exact secret-bearing Gold overlay in memory."""

    resolved = options or LocalGoldOptions()
    resolved.validate()
    _container_state(container)
    docker_values = _docker_environment(container)
    username, password = _docker_auth(docker_values)
    database = _docker_database(docker_values, resolved.database)
    port, _exposure = _bolt_binding(container)
    overlay = {
        ENV_ENABLED: "true",
        ENV_URI: f"neo4j://127.0.0.1:{port}",
        ENV_USERNAME: username,
        ENV_PASSWORD: password,
        ENV_DATABASE: database,
        ENV_EMBEDDING_DIMENSIONS: str(resolved.embedding_dimensions),
        ENV_VECTOR_SEARCH_CAPABILITY: resolved.vector_search_capability,
        ENV_CONNECT_TIMEOUT_SECONDS: str(resolved.connect_timeout_seconds),
        ENV_QUERY_TIMEOUT_SECONDS: str(resolved.query_timeout_seconds),
        ENV_LOCAL_EMBEDDING_ENABLED: (
            "true" if resolved.local_embedding_enabled else "false"
        ),
        ENV_LOCAL_EMBEDDING_MODEL: resolved.local_embedding_model,
        ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD: (
            "true" if resolved.local_embedding_allow_download else "false"
        ),
    }
    if resolved.local_embedding_device is not None:
        overlay[ENV_LOCAL_EMBEDDING_DEVICE] = resolved.local_embedding_device
    required = set(GOLD_RUNTIME_ENV_ALLOWLIST) | {
        ENV_LOCAL_EMBEDDING_ENABLED,
        ENV_LOCAL_EMBEDDING_MODEL,
        ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD,
    }
    if not required.issubset(overlay) or not set(overlay).issubset(
        LOCAL_GOLD_CHILD_ENV_ALLOWLIST
    ):
        raise LocalGoldRuntimeError("child_environment_contract_invalid")
    return overlay


def build_local_gold_child_environment(
    container: Mapping[str, Any],
    options: LocalGoldOptions | None = None,
    *,
    base_environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a child env with all inherited Gold-prefixed values replaced."""

    base = os.environ if base_environment is None else base_environment
    child = {
        str(name): str(value)
        for name, value in base.items()
        if not str(name).upper().startswith("NCS_MCP_GOLD_")
        and not str(name).upper().startswith("PYTHON")
    }
    child.update(local_gold_overlay(container, options))
    return child


def _base_status(
    *, target: str, options: LocalGoldOptions, container: Mapping[str, Any]
) -> dict[str, Any]:
    status, health = _container_state(container)
    values = _docker_environment(container)
    _docker_auth(values)
    database = _docker_database(values, options.database)
    port, exposure = _bolt_binding(container)
    return {
        "schema": LOCAL_GOLD_STATUS_SCHEMA,
        "ok": True,
        "target": target,
        "mode": "local_docker_process_memory_bridge",
        "container": {
            "name": options.container_name,
            "status": status,
            "health": health,
        },
        "bolt": {
            "published": True,
            "host": "127.0.0.1",
            "host_port": port,
            "binding_exposure": exposure,
            "broad_binding_warning": False,
        },
        "credentials": {
            "username_present": True,
            "password_present": True,
            "database_present": bool(database),
            "values_exposed": False,
        },
        "child_environment": {
            "allowlist": sorted(LOCAL_GOLD_CHILD_ENV_ALLOWLIST),
            "all_required_values_present": True,
            "values_exposed": False,
        },
        "semantic_embedding": {
            "configured": (
                options.local_embedding_enabled
                and options.local_embedding_model
                in LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST
            ),
            "local_files_only": not options.local_embedding_allow_download,
            "device_configured": options.local_embedding_device is not None,
            "values_exposed": False,
        },
        "read_only_probe": {
            "attempted": False,
            "ok": False,
            "row_count": None,
        },
        "dry_run": True,
        "launched": False,
        "secrets_persisted": False,
        "production_secret_manager_replacement": False,
        "issues": [],
    }


def probe_local_gold_read(
    child_environment: Mapping[str, str],
    *,
    gateway_factory: Callable[..., Any] = create_gold_runtime,
) -> dict[str, Any]:
    """Run one fixed, bounded read through the production Gold adapter."""

    environment = {
        name: child_environment[name]
        for name in GOLD_RUNTIME_ENV_ALLOWLIST
        if name in child_environment
    }
    gateway = None
    try:
        gateway = gateway_factory(environ=environment)
        if not bool(getattr(gateway, "available", False)):
            raise LocalGoldRuntimeError("gold_runtime_not_ready")
        client = getattr(gateway, "client", None)
        if client is None:
            raise LocalGoldRuntimeError("gold_runtime_not_ready")
        notification_logger = logging.getLogger("neo4j.notifications")
        previous_disabled = notification_logger.disabled
        notification_logger.disabled = True
        try:
            result = client.internal_role_subgraph(_PROBE_ROLE_ID, limit=1)
        finally:
            notification_logger.disabled = previous_disabled
        if not isinstance(result, Mapping):
            raise LocalGoldRuntimeError("gold_read_probe_invalid")
        rows = result.get("rows")
        if not isinstance(rows, list):
            raise LocalGoldRuntimeError("gold_read_probe_invalid")
        return {
            "attempted": True,
            "ok": True,
            "query_contract": "fixed_internal_role_lookup",
            "row_count": len(rows),
        }
    except LocalGoldRuntimeError:
        raise
    except Exception:
        raise LocalGoldRuntimeError("gold_read_probe_failed") from None
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass


def _launch_command(
    target: str, *, transport: str, mcp_port: int
) -> list[str]:
    if target == "builder":
        return [
            sys.executable,
            "-I",
            str(PROJECT_ROOT / "scripts" / "run_ncs_builder.py"),
        ]
    if target != "mcp":
        raise LocalGoldRuntimeError("launch_target_not_allowed")
    if transport not in LOCAL_GOLD_TRANSPORT_ALLOWLIST:
        raise LocalGoldRuntimeError("mcp_transport_not_allowed")
    if isinstance(mcp_port, bool) or not isinstance(mcp_port, int) or not 1 <= mcp_port <= 65535:
        raise LocalGoldRuntimeError("mcp_port_invalid")
    command = [
        sys.executable,
        "-I",
        str(PROJECT_ROOT / "src" / "ncs_mcp" / "server.py"),
        "--transport",
        transport,
    ]
    if transport == "streamable-http":
        command.extend(["--host", "127.0.0.1", "--port", str(mcp_port)])
    return command


def run_local_gold_target(
    *,
    target: str = "status",
    launch: bool = False,
    transport: str = "stdio",
    mcp_port: int = 8000,
    options: LocalGoldOptions | None = None,
    base_environment: Mapping[str, str] | None = None,
    inspect_runner: Callable[..., Any] = _default_runner,
    launch_runner: Callable[..., Any] = _default_runner,
    gateway_factory: Callable[..., Any] = create_gold_runtime,
) -> dict[str, Any]:
    """Inspect, optionally read-probe, and explicitly launch one fixed target."""

    if target not in LOCAL_GOLD_TARGET_ALLOWLIST:
        raise LocalGoldRuntimeError("target_not_allowed")
    if launch and target not in {"mcp", "builder"}:
        raise LocalGoldRuntimeError("launch_target_not_allowed")
    resolved = options or LocalGoldOptions()
    resolved.validate()
    command = (
        _launch_command(target, transport=transport, mcp_port=mcp_port)
        if target in {"mcp", "builder"}
        else None
    )
    container = inspect_local_gold_container(resolved, runner=inspect_runner)
    report = _base_status(target=target, options=resolved, container=container)
    if target in {"status", "health"}:
        return report

    child_environment = build_local_gold_child_environment(
        container, resolved, base_environment=base_environment
    )
    report["read_only_probe"] = probe_local_gold_read(
        child_environment, gateway_factory=gateway_factory
    )
    if target == "probe":
        report["dry_run"] = False
        return report

    if command is None:  # Defensive assertion after the target allowlist above.
        raise LocalGoldRuntimeError("launch_target_not_allowed")
    report["launch_plan"] = {
        "target": target,
        "argv": [Path(command[0]).name, *command[1:]],
        "working_directory": str(PROJECT_ROOT),
        "gold_environment_values_exposed": False,
    }
    if not launch:
        return report
    try:
        completed = launch_runner(
            command,
            cwd=str(PROJECT_ROOT),
            env=child_environment,
            check=False,
        )
    except (OSError, TypeError):
        raise LocalGoldRuntimeError("child_launch_failed") from None
    return_code = getattr(completed, "returncode", None)
    if isinstance(return_code, bool) or not isinstance(return_code, int):
        raise LocalGoldRuntimeError("child_result_invalid")
    report["dry_run"] = False
    report["launched"] = True
    report["child_return_code"] = return_code
    report["ok"] = return_code == 0
    if return_code != 0:
        report["issues"].append("child_exit_nonzero")
    return report


def failed_status(*, target: str, code: str) -> dict[str, Any]:
    """Build a stable failure document without exception or backend details."""

    return {
        "schema": LOCAL_GOLD_STATUS_SCHEMA,
        "ok": False,
        "target": target if target in LOCAL_GOLD_TARGET_ALLOWLIST else "invalid",
        "mode": "local_docker_process_memory_bridge",
        "dry_run": True,
        "launched": False,
        "secrets_persisted": False,
        "production_secret_manager_replacement": False,
        "issues": [code],
    }


__all__ = [
    "LOCAL_GOLD_CONTAINER",
    "LOCAL_GOLD_CONTAINER_ALLOWLIST",
    "LOCAL_GOLD_CHILD_ENV_ALLOWLIST",
    "LOCAL_GOLD_EMBEDDING_DEVICE_ALLOWLIST",
    "LOCAL_GOLD_EMBEDDING_MODEL",
    "LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST",
    "LOCAL_GOLD_STATUS_SCHEMA",
    "LOCAL_GOLD_TARGET_ALLOWLIST",
    "LOCAL_GOLD_TRANSPORT_ALLOWLIST",
    "LocalGoldOptions",
    "LocalGoldRuntimeError",
    "build_local_gold_child_environment",
    "failed_status",
    "inspect_local_gold_container",
    "local_gold_overlay",
    "probe_local_gold_read",
    "run_local_gold_target",
]
