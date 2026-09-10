"""Optional, read-only runtime wiring for the Neo4j Gold projection.

The Gold backend is disabled by default.  This module intentionally depends
only on the standard library and imports the optional ``neo4j`` package only
after an enabled configuration has passed validation.  It is an application
wiring boundary; embedding generation remains the caller's responsibility.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import importlib
import math
import os
from types import ModuleType
from typing import Any

from .neo4j_gold import (
    Neo4jGoldReadClient,
    Neo4jGoldUnavailableError,
    VECTOR_SEARCH_CAPABILITIES,
    VECTOR_SEARCH_CURRENT,
)
from .retrieval import HybridRetriever, Retriever


GOLD_RUNTIME_STATUS_SCHEMA = "ncs_gold_runtime_status_v1"

ENV_ENABLED = "NCS_MCP_GOLD_ENABLED"
ENV_URI = "NCS_MCP_GOLD_URI"
ENV_USERNAME = "NCS_MCP_GOLD_USERNAME"
ENV_PASSWORD = "NCS_MCP_GOLD_PASSWORD"
ENV_DATABASE = "NCS_MCP_GOLD_DATABASE"
ENV_EMBEDDING_DIMENSIONS = "NCS_MCP_GOLD_EMBEDDING_DIMENSIONS"
ENV_VECTOR_SEARCH_CAPABILITY = "NCS_MCP_GOLD_VECTOR_SEARCH_CAPABILITY"
ENV_CONNECT_TIMEOUT_SECONDS = "NCS_MCP_GOLD_CONNECT_TIMEOUT_SECONDS"
ENV_QUERY_TIMEOUT_SECONDS = "NCS_MCP_GOLD_QUERY_TIMEOUT_SECONDS"

# Keep environment access auditable.  No generic Neo4j or embedding-provider
# variables are consulted by this runtime.
GOLD_RUNTIME_ENV_ALLOWLIST = frozenset(
    {
        ENV_ENABLED,
        ENV_URI,
        ENV_USERNAME,
        ENV_PASSWORD,
        ENV_DATABASE,
        ENV_EMBEDDING_DIMENSIONS,
        ENV_VECTOR_SEARCH_CAPABILITY,
        ENV_CONNECT_TIMEOUT_SECONDS,
        ENV_QUERY_TIMEOUT_SECONDS,
    }
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", ""})
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
_DEFAULT_QUERY_TIMEOUT_SECONDS = 1.0
_MAX_TIMEOUT_SECONDS = 300.0
_MAX_EMBEDDING_DIMENSIONS = 4_096


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _parse_enabled(value: object) -> tuple[bool, str | None]:
    text = _text(value)
    if text is None:
        return False, None
    normalized = text.lower()
    if normalized in _TRUE_VALUES:
        return True, None
    if normalized in _FALSE_VALUES:
        return False, None
    # Fail closed: an unrecognised opt-in never activates a network backend.
    return False, "enabled_invalid"


def _parse_positive_int(value: object) -> tuple[int | None, str | None]:
    text = _text(value)
    if text is None:
        return None, None
    try:
        parsed = int(text)
    except ValueError:
        return None, "embedding_dimensions_invalid"
    if not 1 <= parsed <= _MAX_EMBEDDING_DIMENSIONS:
        return None, "embedding_dimensions_invalid"
    return parsed, None


def _parse_timeout(
    value: object,
    *,
    default: float,
    error_code: str,
) -> tuple[float, str | None]:
    text = _text(value)
    if text is None:
        return default, None
    try:
        parsed = float(text)
    except ValueError:
        return default, error_code
    if not math.isfinite(parsed) or not 0 < parsed <= _MAX_TIMEOUT_SECONDS:
        return default, error_code
    return parsed, None


@dataclass(frozen=True, slots=True)
class GoldRuntimeSettings:
    """Validated inputs for the optional Gold runtime.

    Connection fields are excluded from ``repr`` so diagnostics do not leak a
    password or URI user-info.  Use :meth:`readiness` for logging/reporting.
    """

    enabled: bool = False
    uri: str | None = field(default=None, repr=False)
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    embedding_dimensions: int | None = None
    vector_search_capability: str = VECTOR_SEARCH_CURRENT
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS
    parse_errors: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "GoldRuntimeSettings":
        """Load only :data:`GOLD_RUNTIME_ENV_ALLOWLIST` values."""

        source = os.environ if environ is None else environ
        values = {name: source.get(name) for name in GOLD_RUNTIME_ENV_ALLOWLIST}
        enabled, enabled_error = _parse_enabled(values[ENV_ENABLED])
        dimensions, dimensions_error = _parse_positive_int(
            values[ENV_EMBEDDING_DIMENSIONS]
        )
        connect_timeout, connect_error = _parse_timeout(
            values[ENV_CONNECT_TIMEOUT_SECONDS],
            default=_DEFAULT_CONNECT_TIMEOUT_SECONDS,
            error_code="connect_timeout_invalid",
        )
        query_timeout, query_error = _parse_timeout(
            values[ENV_QUERY_TIMEOUT_SECONDS],
            default=_DEFAULT_QUERY_TIMEOUT_SECONDS,
            error_code="query_timeout_invalid",
        )
        errors = tuple(
            error
            for error in (
                enabled_error,
                dimensions_error,
                connect_error,
                query_error,
            )
            if error is not None
        )
        return cls(
            enabled=enabled,
            uri=_text(values[ENV_URI]),
            username=_text(values[ENV_USERNAME]),
            password=_text(values[ENV_PASSWORD]),
            database=_text(values[ENV_DATABASE]),
            embedding_dimensions=dimensions,
            vector_search_capability=(
                _text(values[ENV_VECTOR_SEARCH_CAPABILITY]) or VECTOR_SEARCH_CURRENT
            ),
            connect_timeout_seconds=connect_timeout,
            query_timeout_seconds=query_timeout,
            parse_errors=errors,
        )

    def validation_errors(self) -> tuple[str, ...]:
        """Return stable, value-free validation codes."""

        errors = list(self.parse_errors)
        if not isinstance(self.enabled, bool):
            errors.append("enabled_invalid")
        for name, value in (
            ("uri", self.uri),
            ("username", self.username),
            ("password", self.password),
            ("database", self.database),
        ):
            if _text(value) is None:
                errors.append(f"{name}_missing")
        dimensions = self.embedding_dimensions
        if (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or not 1 <= dimensions <= _MAX_EMBEDDING_DIMENSIONS
        ):
            errors.append("embedding_dimensions_invalid")
        if self.vector_search_capability not in VECTOR_SEARCH_CAPABILITIES:
            errors.append("vector_search_capability_invalid")
        for name, value in (
            ("connect_timeout", self.connect_timeout_seconds),
            ("query_timeout", self.query_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or not 0 < float(value) <= _MAX_TIMEOUT_SECONDS
            ):
                errors.append(f"{name}_invalid")
        # Preserve a deterministic order without repeating loader and runtime
        # validation findings.
        return tuple(dict.fromkeys(errors))

    @property
    def configured(self) -> bool:
        return not self.validation_errors()

    def readiness(self, *, state: str | None = None) -> dict[str, Any]:
        """Return a secret-free readiness report based on presence/validity."""

        errors = self.validation_errors()
        effective_state = state or (
            "disabled" if not self.enabled else "configured" if not errors else "invalid_config"
        )
        return {
            "schema": GOLD_RUNTIME_STATUS_SCHEMA,
            "state": effective_state,
            "enabled": self.enabled,
            "configured": not errors,
            "available": effective_state == "ready",
            "read_only": True,
            "db_writes": False,
            "uri_present": _text(self.uri) is not None,
            "username_present": _text(self.username) is not None,
            "password_present": _text(self.password) is not None,
            "database_present": _text(self.database) is not None,
            "embedding_dimensions_valid": (
                isinstance(self.embedding_dimensions, int)
                and not isinstance(self.embedding_dimensions, bool)
                and 1 <= self.embedding_dimensions <= _MAX_EMBEDDING_DIMENSIONS
            ),
            "vector_search_capability_valid": (
                self.vector_search_capability in VECTOR_SEARCH_CAPABILITIES
            ),
            "connect_timeout_valid": "connect_timeout_invalid" not in errors,
            "query_timeout_valid": "query_timeout_invalid" not in errors,
            "issues": list(errors),
        }


def load_gold_runtime_settings(
    environ: Mapping[str, str] | None = None,
) -> GoldRuntimeSettings:
    """Load the optional Gold settings without importing a driver."""

    return GoldRuntimeSettings.from_env(environ)


class GoldRuntimeGateway:
    """Own a Gold client/driver and provide disabled-safe hybrid wiring."""

    def __init__(
        self,
        settings: GoldRuntimeSettings,
        *,
        client: Neo4jGoldReadClient | None = None,
        driver: Any | None = None,
        state: str | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._driver = driver
        self._closed = False
        self._state = state or ("ready" if client is not None else "disabled")

    @property
    def enabled(self) -> bool:
        return self._state == "ready" and not self._closed

    @property
    def available(self) -> bool:
        return self.enabled

    @property
    def client(self) -> Neo4jGoldReadClient | None:
        return self._client if self.available else None

    @property
    def status(self) -> dict[str, Any]:
        state = "closed" if self._closed else self._state
        return self._settings.readiness(state=state)

    def readiness(self) -> dict[str, Any]:
        return self.status

    def hybrid_retriever(
        self,
        sqlite_retriever: Retriever,
        augmenter: Any | None = None,
        *,
        max_augmented_candidates: int = 20,
    ) -> HybridRetriever:
        """Create a SQLite-authoritative retriever with optional augmentation.

        No embedding adapter is invented here.  A caller may supply one, and it
        is activated only while this Gold runtime is ready.  Disabled, invalid,
        and closed gateways produce the existing baseline-only behavior.
        """

        active_augmenter = augmenter if self.available else None
        query_timeout = self._settings.query_timeout_seconds
        if (
            isinstance(query_timeout, bool)
            or not isinstance(query_timeout, (int, float))
            or not math.isfinite(float(query_timeout))
            or not 0 < float(query_timeout) <= _MAX_TIMEOUT_SECONDS
        ):
            query_timeout = _DEFAULT_QUERY_TIMEOUT_SECONDS
        return HybridRetriever(
            sqlite_retriever,
            active_augmenter,
            timeout_seconds=float(query_timeout),
            max_augmented_candidates=max_augmented_candidates,
        )

    build_hybrid_retriever = hybrid_retriever

    def close(self) -> None:
        """Close the owned driver once; never expose backend exception text."""

        if self._closed:
            return
        self._closed = True
        driver, self._driver = self._driver, None
        self._client = None
        if driver is None:
            return
        try:
            driver.close()
        except Exception:
            raise Neo4jGoldUnavailableError(
                "Gold LPG runtime could not close cleanly"
            ) from None

    def __enter__(self) -> "GoldRuntimeGateway":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()


def _default_importer(name: str) -> ModuleType:
    return importlib.import_module(name)


def create_gold_runtime(
    settings: GoldRuntimeSettings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    importer: Callable[[str], Any] = _default_importer,
) -> GoldRuntimeGateway:
    """Create an owned Gold gateway, or a network-free no-op gateway.

    Disabled and invalid settings intentionally do not raise and do not import
    ``neo4j``.  Import, driver creation, and connectivity failures for a valid
    enabled configuration are normalized to :class:`Neo4jGoldUnavailableError`.
    """

    if settings is not None and environ is not None:
        raise TypeError("pass settings or environ, not both")
    resolved = settings or load_gold_runtime_settings(environ)
    errors = resolved.validation_errors()
    if not resolved.enabled:
        return GoldRuntimeGateway(resolved, state="disabled")
    if errors:
        return GoldRuntimeGateway(resolved, state="invalid_config")

    try:
        neo4j = importer("neo4j")
        graph_database = getattr(neo4j, "GraphDatabase")
        query_factory = getattr(neo4j, "Query")
        driver = graph_database.driver(
            resolved.uri,
            auth=(resolved.username, resolved.password),
            connection_timeout=resolved.connect_timeout_seconds,
        )
    except Exception:
        raise Neo4jGoldUnavailableError(
            "Gold LPG runtime dependency is unavailable"
        ) from None

    try:
        driver.verify_connectivity()

        def execute_query(query: Any, *args: Any, **kwargs: Any) -> Any:
            # Driver.execute_query does not accept a transaction ``timeout_``
            # keyword.  The documented mechanism is a neo4j.Query carrying
            # the timeout; database_ and read-only routing remain untouched.
            timed_query = query_factory(
                query,
                timeout=resolved.query_timeout_seconds,
            )
            return driver.execute_query(timed_query, *args, **kwargs)

        client = Neo4jGoldReadClient(
            execute_query,
            database=resolved.database or "",
            embedding_dimensions=resolved.embedding_dimensions or 0,
            vector_search_capability=resolved.vector_search_capability,
        )
    except Exception:
        try:
            driver.close()
        except Exception:
            pass
        raise Neo4jGoldUnavailableError("Gold LPG read backend is unavailable") from None

    return GoldRuntimeGateway(resolved, client=client, driver=driver, state="ready")


# Friendly aliases for future service integration.
open_gold_runtime = create_gold_runtime
GoldRuntime = GoldRuntimeGateway


__all__ = [
    "ENV_CONNECT_TIMEOUT_SECONDS",
    "ENV_DATABASE",
    "ENV_EMBEDDING_DIMENSIONS",
    "ENV_ENABLED",
    "ENV_PASSWORD",
    "ENV_QUERY_TIMEOUT_SECONDS",
    "ENV_URI",
    "ENV_USERNAME",
    "ENV_VECTOR_SEARCH_CAPABILITY",
    "GOLD_RUNTIME_ENV_ALLOWLIST",
    "GOLD_RUNTIME_STATUS_SCHEMA",
    "GoldRuntime",
    "GoldRuntimeGateway",
    "GoldRuntimeSettings",
    "create_gold_runtime",
    "load_gold_runtime_settings",
    "open_gold_runtime",
]
