"""Safe MCP-facing facade for the optional Neo4j Gold LPG projection.

SQLite remains the system of record.  This module only returns bounded Gold
results as *candidate/context* material and is intentionally inert until a
read operation is requested.  In particular, importing it or calling
``status`` neither imports the Neo4j driver nor loads an embedding model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import math
import os
from typing import Any, Protocol

from .embeddings import EmbeddingProvider
from .gold_runtime import (
    ENV_EMBEDDING_DIMENSIONS,
    GoldRuntimeGateway,
    GoldRuntimeSettings,
    create_gold_runtime,
    load_gold_runtime_settings,
)
from .local_embeddings import SentenceTransformerEmbeddingProvider
from .neo4j_gold import (
    MAX_JOB_KSA_SUMMARY_LIMIT,
    MAX_RESULT_LIMIT,
    MAX_TOP_K,
    VECTOR_INDEX_ALLOWLIST,
)


GOLD_MCP_STATUS_SCHEMA = "ncs_gold_mcp_context_v1"

# The facade has a separate, deliberately small local-provider allowlist.
# ``ALLOW_DOWNLOAD`` is opt-in: no model download occurs with the default.
ENV_LOCAL_EMBEDDING_ENABLED = "NCS_MCP_GOLD_LOCAL_EMBEDDING_ENABLED"
ENV_LOCAL_EMBEDDING_MODEL = "NCS_MCP_GOLD_LOCAL_EMBEDDING_MODEL"
ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD = "NCS_MCP_GOLD_LOCAL_EMBEDDING_ALLOW_DOWNLOAD"
ENV_LOCAL_EMBEDDING_DEVICE = "NCS_MCP_GOLD_LOCAL_EMBEDDING_DEVICE"
GOLD_MCP_ENV_ALLOWLIST = frozenset(
    {
        ENV_EMBEDDING_DIMENSIONS,
        ENV_LOCAL_EMBEDDING_ENABLED,
        ENV_LOCAL_EMBEDDING_MODEL,
        ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD,
        ENV_LOCAL_EMBEDDING_DEVICE,
    }
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "y", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "n", "off", ""})
_MAX_QUERY_LENGTH = 4_000


class _Client(Protocol):
    def internal_role_subgraph(self, internal_role_id: str, *, limit: int) -> dict[str, Any]: ...

    def internal_role_job_ksa_summary(
        self, internal_role_id: str, *, limit: int
    ) -> dict[str, Any]: ...

    def vector_graph_expansion(
        self,
        embedding: Sequence[float],
        *,
        entity_kind: str,
        top_k: int,
        limit: int,
    ) -> dict[str, Any]: ...


def _text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _flag(value: object) -> tuple[bool, str | None]:
    text = _text(value)
    if text is None:
        return False, None
    normalized = text.casefold()
    if normalized in _TRUE_VALUES:
        return True, None
    if normalized in _FALSE_VALUES:
        return False, None
    return False, "local_embedding_enabled_invalid"


def _positive_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _context_envelope(operation: str, status: str, **extra: Any) -> dict[str, Any]:
    """Return a stable context-only response without backend configuration."""

    response: dict[str, Any] = {
        "schema": GOLD_MCP_STATUS_SCHEMA,
        "operation": operation,
        "status": status,
        "authority": {
            "sqlite_authoritative": True,
            "gold_result_role": "candidate_context",
            "not_an_approval_or_human_review_status": True,
        },
        "audit": {"read_only": True, "db_writes": False},
    }
    response.update(extra)
    return response


class GoldMCPFacade:
    """Lazy, fail-closed access to the optional Gold read projection.

    ``gateway_factory`` and ``local_provider_factory`` are dependency-injection
    seams for the MCP server and tests.  A facade owns a created runtime and
    closes it when callers invoke :meth:`close`.
    """

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
        settings: GoldRuntimeSettings | None = None,
        gateway_factory: Callable[[GoldRuntimeSettings], GoldRuntimeGateway] | None = None,
        embedding_provider: EmbeddingProvider | None = None,
        local_provider_factory: Callable[..., EmbeddingProvider] = SentenceTransformerEmbeddingProvider,
    ) -> None:
        if settings is not None and environ is not None:
            raise TypeError("pass settings or environ, not both")
        self._environ = environ
        self._settings = settings
        self._gateway_factory = gateway_factory or (lambda value: create_gold_runtime(value))
        self._embedding_provider = embedding_provider
        self._local_provider_factory = local_provider_factory
        self._gateway: GoldRuntimeGateway | None = None

    def _resolved_settings(self) -> GoldRuntimeSettings:
        return self._settings or load_gold_runtime_settings(self._environ)

    def _env_value(self, name: str) -> str | None:
        source = os.environ if self._environ is None else self._environ
        # Never inspect unrelated process environment values.
        return source.get(name) if name in GOLD_MCP_ENV_ALLOWLIST else None

    def status(self) -> dict[str, Any]:
        """Return sanitized configuration state without backend/model loading."""

        settings = self._resolved_settings()
        local_enabled, local_error = _flag(self._env_value(ENV_LOCAL_EMBEDDING_ENABLED))
        local_model = _text(self._env_value(ENV_LOCAL_EMBEDDING_MODEL))
        allow_download, download_error = _flag(
            self._env_value(ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD)
        )
        errors = [error for error in (local_error, download_error) if error]
        if local_enabled and local_model is None:
            errors.append("local_embedding_model_missing")
        provider = self._embedding_provider
        embedding: dict[str, Any] = {
            "configured": provider is not None or (local_enabled and local_model is not None),
            "provider_mode": "injected" if provider is not None else "local" if local_enabled else "disabled",
            "local_files_only": not allow_download,
            "model_loaded": False,
            "issues": errors,
        }
        if provider is not None:
            embedding["provider"] = _safe_provider_name(provider)
            embedding["dimensions_known"] = _provider_dimensions(provider) is not None
        elif local_model is not None:
            # A model identifier is configuration, not credentials; keep it only
            # for reproducible local Builder/MCP evidence.
            embedding["model"] = local_model
        if self._gateway is not None:
            embedding["gateway_created"] = True

        return _context_envelope(
            "status",
            "ready" if self._gateway is not None and self._gateway.available else settings.readiness()["state"],
            runtime=settings.readiness(),
            embedding=embedding,
        )

    def internal_role_context(
        self, role_id: str, *, summary: bool = True, limit: int = 50
    ) -> dict[str, Any]:
        role = _text(role_id)
        if role is None:
            return self._invalid("internal_role_context", "role_id_invalid")
        try:
            bounded_limit = _positive_int(
                limit,
                name="limit",
                maximum=MAX_JOB_KSA_SUMMARY_LIMIT if summary else MAX_RESULT_LIMIT,
            )
        except ValueError:
            return self._invalid("internal_role_context", "limit_invalid")
        client, unavailable = self._client("internal_role_context")
        if unavailable is not None:
            return unavailable
        assert client is not None
        try:
            result = (
                client.internal_role_job_ksa_summary(role, limit=bounded_limit)
                if summary
                else client.internal_role_subgraph(role, limit=bounded_limit)
            )
        except Exception:
            return self._unavailable("internal_role_context")
        return self._success(
            "internal_role_context",
            result,
            parameters={"summary": bool(summary), "limit": bounded_limit},
        )

    def semantic_context(
        self,
        query: str,
        *,
        entity_kind: str = "performance_criterion",
        top_k: int = 5,
        limit: int = 50,
    ) -> dict[str, Any]:
        text = _text(query)
        if text is None or len(text) > _MAX_QUERY_LENGTH:
            return self._invalid("semantic_context", "query_invalid")
        if entity_kind not in VECTOR_INDEX_ALLOWLIST:
            return self._invalid("semantic_context", "entity_kind_invalid")
        try:
            bounded_top_k = _positive_int(top_k, name="top_k", maximum=MAX_TOP_K)
            bounded_limit = _positive_int(limit, name="limit", maximum=MAX_RESULT_LIMIT)
        except ValueError:
            return self._invalid("semantic_context", "limit_invalid")
        client, unavailable = self._client("semantic_context")
        if unavailable is not None:
            return unavailable
        assert client is not None
        provider = self._resolve_embedding_provider()
        if provider is None:
            return _context_envelope(
                "semantic_context",
                "unavailable",
                error="embedding_provider_unavailable",
                context={"rows": []},
            )
        try:
            vectors = _embed_one(provider, text)
            result = client.vector_graph_expansion(
                vectors,
                entity_kind=entity_kind,
                top_k=bounded_top_k,
                limit=bounded_limit,
            )
        except Exception:
            # Provider and backend error strings may contain filesystem paths,
            # endpoint details, or secrets.  Never surface them through MCP.
            return self._unavailable("semantic_context")
        return self._success(
            "semantic_context",
            result,
            parameters={
                "entity_kind": entity_kind,
                "top_k": bounded_top_k,
                "limit": bounded_limit,
                "embedding_provider": _safe_provider_name(provider),
            },
        )

    def close(self) -> None:
        gateway, self._gateway = self._gateway, None
        if gateway is None:
            return
        try:
            gateway.close()
        except Exception:
            # Closing an optional context backend must not turn an MCP response
            # into an exception and must not reveal backend diagnostics.
            return

    def _client(self, operation: str) -> tuple[_Client | None, dict[str, Any] | None]:
        settings = self._resolved_settings()
        if not settings.enabled:
            return None, _context_envelope(
                operation, "disabled", error="gold_disabled", context={"rows": []}
            )
        if not settings.configured:
            return None, _context_envelope(
                operation, "unavailable", error="gold_configuration_invalid", context={"rows": []}
            )
        try:
            if self._gateway is None:
                self._gateway = self._gateway_factory(settings)
            client = self._gateway.client
        except Exception:
            return None, self._unavailable(operation)
        if client is None:
            return None, self._unavailable(operation)
        return client, None

    def _resolve_embedding_provider(self) -> EmbeddingProvider | None:
        if self._embedding_provider is not None:
            return self._embedding_provider
        enabled, issue = _flag(self._env_value(ENV_LOCAL_EMBEDDING_ENABLED))
        model = _text(self._env_value(ENV_LOCAL_EMBEDDING_MODEL))
        if issue is not None or not enabled or model is None:
            return None
        allow_download, download_issue = _flag(
            self._env_value(ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD)
        )
        if download_issue is not None:
            return None
        dimensions = self._resolved_settings().embedding_dimensions
        device = _text(self._env_value(ENV_LOCAL_EMBEDDING_DEVICE))
        try:
            self._embedding_provider = self._local_provider_factory(
                model,
                dimensions=dimensions,
                device=device,
                local_files_only=not allow_download,
                # Retrieval models such as Qwen3 distinguish query encoding
                # from the unprompted document encoding used for the Gold
                # index.  This provider instance is query-only.
                prompt_name="query",
            )
        except Exception:
            return None
        return self._embedding_provider

    @staticmethod
    def _invalid(operation: str, error: str) -> dict[str, Any]:
        return _context_envelope(
            operation, "invalid_input", error=error, context={"rows": []}
        )

    @staticmethod
    def _unavailable(operation: str) -> dict[str, Any]:
        return _context_envelope(
            operation, "unavailable", error="gold_backend_unavailable", context={"rows": []}
        )

    @staticmethod
    def _success(
        operation: str,
        result: object,
        *,
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping):
            return GoldMCPFacade._unavailable(operation)
        rows = result.get("rows")
        audit = result.get("audit")
        if not isinstance(rows, list) or not isinstance(audit, Mapping):
            return GoldMCPFacade._unavailable(operation)
        # The adapter already emits an allowlisted projection.  Do not accept
        # arbitrary top-level fields from a driver or an injected fake.
        return _context_envelope(
            operation,
            "ok",
            context={"rows": rows, "audit": dict(audit)},
            request=dict(parameters),
        )


def _safe_provider_name(provider: object) -> str:
    name = getattr(provider, "provider_name", None)
    return name.strip() if isinstance(name, str) and name.strip() else "configured_provider"


def _provider_dimensions(provider: object) -> int | None:
    value = getattr(provider, "dimensions", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _embed_one(provider: EmbeddingProvider, text: str) -> list[float]:
    embedder = getattr(provider, "embed_texts", None) or getattr(provider, "embed", None)
    if not callable(embedder):
        raise TypeError("embedding provider does not expose an embed method")
    vectors = embedder([text])
    if not isinstance(vectors, Sequence) or len(vectors) != 1:
        raise ValueError("embedding provider returned an invalid batch")
    vector = vectors[0]
    if isinstance(vector, (str, bytes)) or not isinstance(vector, Sequence):
        raise ValueError("embedding provider returned an invalid vector")
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("embedding provider returned a non-finite vector")
        values.append(float(value))
    if not values:
        raise ValueError("embedding provider returned an empty vector")
    return values


__all__ = [
    "ENV_LOCAL_EMBEDDING_ALLOW_DOWNLOAD",
    "ENV_LOCAL_EMBEDDING_DEVICE",
    "ENV_LOCAL_EMBEDDING_ENABLED",
    "ENV_LOCAL_EMBEDDING_MODEL",
    "GOLD_MCP_ENV_ALLOWLIST",
    "GOLD_MCP_STATUS_SCHEMA",
    "GoldMCPFacade",
]
