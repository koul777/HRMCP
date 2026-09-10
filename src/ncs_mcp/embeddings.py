"""Provider-neutral embedding inputs and validation helpers.

This module deliberately has no network or vendor dependency.  Embeddings are
disabled unless an application explicitly supplies a provider.  The helpers in
this file are safe to use while building a future cache or vector index: cache
identity is deterministic and untrusted ontology definitions are never added
to semantic text.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable
import unicodedata


EMBEDDING_METADATA_SCHEMA = "ncs_embedding_metadata_v1"
TRUSTED_DEFINITION_REVIEW_STATUSES = frozenset(
    {"human_reviewed", "accepted", "reviewed"}
)

# These are generated definition templates documented by the repository.  A
# concept-name prefix is stripped before comparing, so ``Name: <template>`` is
# rejected too.  English variants protect fixtures and future translated data.
_BOILERPLATE_BODIES = frozenset(
    {
        "업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.",
        "업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력.",
        "업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도.",
        "knowledge of relevant principles, standards, procedures, and cases needed for work judgment and problem solving.",
        "ability to perform tasks using relevant procedures or tools in work situations.",
        "attitude required to maintain quality, collaboration, and responsibility while performing work.",
    }
)


class EmbeddingError(RuntimeError):
    """Base class for provider-neutral embedding failures."""


class EmbeddingDisabledError(EmbeddingError):
    """Raised when embedding is requested without an enabled provider."""


class EmbeddingDimensionError(EmbeddingError, ValueError):
    """Raised when a vector does not match the declared dimension."""


@runtime_checkable
class EmbeddingProvider(Protocol):
    """Minimal interface implemented by optional embedding backends.

    Providers may be local or remote.  Implementations own transport concerns;
    callers validate returned vectors with :func:`validate_embedding_batch`.
    """

    provider_name: str
    model: str
    dimensions: int | None
    enabled: bool

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Return one numeric vector per input text."""


@dataclass(frozen=True, slots=True)
class DisabledEmbeddingProvider:
    """Safe default that performs no I/O and makes opt-in explicit."""

    provider_name: str = "disabled"
    model: str = "disabled"
    dimensions: int | None = None
    enabled: bool = False

    def embed_texts(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        if not texts:
            return ()
        raise EmbeddingDisabledError(
            "Embedding is disabled; configure an explicit provider to enable it."
        )

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        """Compatibility alias for providers exposing ``embed``."""

        return self.embed_texts(texts)


DEFAULT_EMBEDDING_PROVIDER: EmbeddingProvider = DisabledEmbeddingProvider()


@dataclass(frozen=True, slots=True)
class SemanticEmbeddingInput:
    """Canonical semantic text together with reproducible cache metadata."""

    text: str
    content_hash: str
    cache_key: str
    metadata: Mapping[str, Any]


def normalize_semantic_text(value: Any) -> str:
    """Normalize Unicode and whitespace without language-specific stemming."""

    text = unicodedata.normalize("NFC", str(value or ""))
    return " ".join(text.split()).strip()


def content_hash(text: Any) -> str:
    """Return the SHA-256 hash of canonical semantic text."""

    canonical = normalize_semantic_text(text)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_status(value: Any) -> str:
    return normalize_semantic_text(value).casefold()


def is_boilerplate_definition(
    definition: Any,
    *,
    concept_name: Any = "",
) -> bool:
    """Identify known generated KSA definition templates conservatively."""

    text = normalize_semantic_text(definition)
    if not text:
        return False

    name = normalize_semantic_text(concept_name)
    if name:
        prefix_pattern = rf"^{re.escape(name)}\s*[:：-]\s*"
        text = re.sub(prefix_pattern, "", text, count=1, flags=re.IGNORECASE)
    folded = text.casefold()
    if folded in {body.casefold() for body in _BOILERPLATE_BODIES}:
        return True

    # Generated variants occasionally omit punctuation or add a short prefix.
    compact = folded.rstrip(".。 ")
    return any(
        compact == body.casefold().rstrip(".。 ") for body in _BOILERPLATE_BODIES
    )


def is_trusted_definition(
    definition: Any,
    *,
    definition_status: Any,
    review_status: Any,
    concept_name: Any = "",
) -> bool:
    """Return whether a definition is eligible for semantic input.

    A definition must be non-empty, explicitly defined, carry a human-trusted
    review status, and not match a generated boilerplate template.
    """

    return bool(normalize_semantic_text(definition)) and (
        _normalized_status(definition_status) == "defined"
        and _normalized_status(review_status)
        in TRUSTED_DEFINITION_REVIEW_STATUSES
        and not is_boilerplate_definition(definition, concept_name=concept_name)
    )


def build_semantic_text(
    *,
    label: Any = "",
    source_text: Any = "",
    definition: Any = "",
    definition_status: Any = "",
    review_status: Any = "",
    concept_name: Any = "",
    extra_texts: Iterable[Any] = (),
) -> str:
    """Build deterministic text while excluding untrusted definitions.

    ``source_text`` is treated as preserved source evidence, not as an ontology
    definition.  Callers should pass raw/refined task or course text there.
    """

    effective_name = normalize_semantic_text(concept_name) or normalize_semantic_text(label)
    parts: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        normalized = normalize_semantic_text(value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            parts.append(normalized)

    add(label)
    add(source_text)
    if is_trusted_definition(
        definition,
        definition_status=definition_status,
        review_status=review_status,
        concept_name=effective_name,
    ):
        add(definition)
    for value in extra_texts:
        add(value)
    return "\n".join(parts)


def embedding_cache_key(
    *,
    provider_name: Any,
    model: Any,
    dimensions: int | None,
    text: Any | None = None,
    text_content_hash: str | None = None,
) -> str:
    """Return a versioned deterministic key for an embedding cache row."""

    if text_content_hash is None:
        text_content_hash = content_hash(text)
    normalized_hash = normalize_semantic_text(text_content_hash).casefold()
    if not re.fullmatch(r"[0-9a-f]{64}", normalized_hash):
        raise ValueError("text_content_hash must be a SHA-256 hexadecimal digest")
    if dimensions is not None and (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions <= 0
    ):
        raise EmbeddingDimensionError("dimensions must be a positive integer or None")

    identity = {
        "schema": EMBEDDING_METADATA_SCHEMA,
        "provider": normalize_semantic_text(provider_name).casefold(),
        "model": normalize_semantic_text(model),
        "dimensions": dimensions,
        "content_hash": normalized_hash,
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"emb:v1:{hashlib.sha256(encoded).hexdigest()}"


def build_embedding_metadata(
    text: Any,
    *,
    provider_name: Any,
    model: Any,
    dimensions: int | None,
    definition_included: bool | None = None,
) -> dict[str, Any]:
    """Build cache-safe metadata without storing the semantic text itself."""

    digest = content_hash(text)
    metadata: dict[str, Any] = {
        "schema": EMBEDDING_METADATA_SCHEMA,
        "provider": normalize_semantic_text(provider_name).casefold(),
        "model": normalize_semantic_text(model),
        "dimensions": dimensions,
        "content_hash": digest,
        "cache_key": embedding_cache_key(
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            text_content_hash=digest,
        ),
    }
    if definition_included is not None:
        metadata["definition_included"] = bool(definition_included)
    return metadata


def prepare_embedding_input(
    *,
    provider_name: Any,
    model: Any,
    dimensions: int | None,
    label: Any = "",
    source_text: Any = "",
    definition: Any = "",
    definition_status: Any = "",
    review_status: Any = "",
    concept_name: Any = "",
    extra_texts: Iterable[Any] = (),
) -> SemanticEmbeddingInput:
    """Build semantic text plus hash/key metadata in one deterministic call."""

    definition_included = is_trusted_definition(
        definition,
        definition_status=definition_status,
        review_status=review_status,
        concept_name=concept_name or label,
    )
    text = build_semantic_text(
        label=label,
        source_text=source_text,
        definition=definition,
        definition_status=definition_status,
        review_status=review_status,
        concept_name=concept_name,
        extra_texts=extra_texts,
    )
    metadata = build_embedding_metadata(
        text,
        provider_name=provider_name,
        model=model,
        dimensions=dimensions,
        definition_included=definition_included,
    )
    return SemanticEmbeddingInput(
        text=text,
        content_hash=str(metadata["content_hash"]),
        cache_key=str(metadata["cache_key"]),
        metadata=metadata,
    )


def validate_embedding_dimension(
    vector: Sequence[float],
    expected_dimension: int,
) -> tuple[float, ...]:
    """Validate dimension and finite numeric values, returning floats."""

    if (
        isinstance(expected_dimension, bool)
        or not isinstance(expected_dimension, int)
        or expected_dimension <= 0
    ):
        raise EmbeddingDimensionError("expected_dimension must be a positive integer")
    if len(vector) != expected_dimension:
        raise EmbeddingDimensionError(
            f"expected {expected_dimension} dimensions, received {len(vector)}"
        )
    normalized: list[float] = []
    for index, value in enumerate(vector):
        if isinstance(value, bool):
            raise EmbeddingDimensionError(
                f"embedding value at index {index} must be numeric and finite"
            )
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise EmbeddingDimensionError(
                f"embedding value at index {index} must be numeric and finite"
            ) from exc
        if not math.isfinite(numeric):
            raise EmbeddingDimensionError(
                f"embedding value at index {index} must be numeric and finite"
            )
        normalized.append(numeric)
    return tuple(normalized)


def validate_embedding_batch(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
    expected_dimension: int,
) -> tuple[tuple[float, ...], ...]:
    """Validate response cardinality and every vector dimension."""

    if len(vectors) != expected_count:
        raise EmbeddingDimensionError(
            f"expected {expected_count} vectors, received {len(vectors)}"
        )
    return tuple(
        validate_embedding_dimension(vector, expected_dimension) for vector in vectors
    )


# Clear aliases for callers that prefer hash/cache terminology.
semantic_content_hash = content_hash
build_cache_key = embedding_cache_key


def get_default_embedding_provider() -> EmbeddingProvider:
    """Return the explicit disabled-by-default provider."""

    return DEFAULT_EMBEDDING_PROVIDER


__all__ = [
    "DEFAULT_EMBEDDING_PROVIDER",
    "DisabledEmbeddingProvider",
    "EMBEDDING_METADATA_SCHEMA",
    "EmbeddingDimensionError",
    "EmbeddingDisabledError",
    "EmbeddingError",
    "EmbeddingProvider",
    "SemanticEmbeddingInput",
    "TRUSTED_DEFINITION_REVIEW_STATUSES",
    "build_cache_key",
    "build_embedding_metadata",
    "build_semantic_text",
    "content_hash",
    "embedding_cache_key",
    "get_default_embedding_provider",
    "is_boilerplate_definition",
    "is_trusted_definition",
    "normalize_semantic_text",
    "prepare_embedding_input",
    "semantic_content_hash",
    "validate_embedding_batch",
    "validate_embedding_dimension",
]
