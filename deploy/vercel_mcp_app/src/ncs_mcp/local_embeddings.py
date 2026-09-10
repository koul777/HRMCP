"""Opt-in local sentence-transformer embeddings for the Gold projection.

The project keeps embeddings provider-neutral.  This adapter is deliberately
lazy: importing :mod:`ncs_mcp` never imports torch or transformers, and model
loading is local-only unless a caller explicitly permits downloads.
"""

from __future__ import annotations

import math
from threading import Lock
from typing import Any, Callable, Sequence

from .embeddings import EmbeddingDimensionError, EmbeddingError


LOCAL_EMBEDDING_PROVIDER = "sentence_transformers_local"


class LocalEmbeddingUnavailableError(EmbeddingError):
    """Raised when the optional local model runtime cannot be initialized."""


class SentenceTransformerEmbeddingProvider:
    """Lazy, bounded adapter around ``sentence_transformers``.

    Parameters are explicit so the Builder can persist the exact model and
    dimension in its Gold evidence.  ``local_files_only`` defaults to true to
    prevent an implicit network/model download during a data build.
    """

    provider_name = LOCAL_EMBEDDING_PROVIDER
    enabled = True

    def __init__(
        self,
        model: str,
        *,
        dimensions: int | None = None,
        device: str | None = None,
        local_files_only: bool = True,
        normalize_embeddings: bool = True,
        prompt_name: str | None = None,
        loader: Callable[..., Any] | None = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        if dimensions is not None and (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or not 1 <= dimensions <= 8_192
        ):
            raise EmbeddingDimensionError(
                "dimensions must be an integer between 1 and 8192 or null"
            )
        self.model = model.strip()
        self.dimensions = dimensions
        self.device = device.strip() if isinstance(device, str) and device.strip() else None
        self.local_files_only = bool(local_files_only)
        self.normalize_embeddings = bool(normalize_embeddings)
        self.prompt_name = (
            prompt_name.strip()
            if isinstance(prompt_name, str) and prompt_name.strip()
            else None
        )
        self._loader = loader
        self._runtime: Any | None = None
        self._lock = Lock()

    def _load(self) -> Any:
        if self._runtime is not None:
            return self._runtime
        with self._lock:
            if self._runtime is not None:
                return self._runtime
            try:
                loader = self._loader
                if loader is None:
                    from sentence_transformers import SentenceTransformer

                    loader = SentenceTransformer
                kwargs: dict[str, Any] = {
                    "local_files_only": self.local_files_only,
                }
                if self.device:
                    kwargs["device"] = self.device
                runtime = loader(self.model, **kwargs)
                dimension_getter = getattr(runtime, "get_embedding_dimension", None)
                if not callable(dimension_getter):
                    dimension_getter = getattr(
                        runtime, "get_sentence_embedding_dimension", None
                    )
                if not callable(dimension_getter):
                    raise TypeError("embedding dimension getter is unavailable")
                model_dimension = dimension_getter()
            except Exception as exc:
                raise LocalEmbeddingUnavailableError(
                    "Local embedding runtime is unavailable; verify the optional "
                    "dependencies and local model cache."
                ) from exc
            if isinstance(model_dimension, bool) or not isinstance(model_dimension, int):
                raise LocalEmbeddingUnavailableError(
                    "Local embedding model did not report a valid dimension."
                )
            if self.dimensions is not None and model_dimension != self.dimensions:
                raise EmbeddingDimensionError(
                    f"configured dimension {self.dimensions} does not match model dimension "
                    f"{model_dimension}"
                )
            self.dimensions = model_dimension
            self._runtime = runtime
            return runtime

    def resolve_dimensions(self) -> int:
        """Load the local model, if needed, and return its output dimension."""

        self._load()
        assert self.dimensions is not None
        return self.dimensions

    def embed_texts(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if isinstance(texts, (str, bytes)):
            raise TypeError("texts must be a sequence of strings")
        materialized = tuple(texts)
        if not materialized:
            return ()
        if any(not isinstance(text, str) or not text.strip() for text in materialized):
            raise ValueError("texts must contain only non-empty strings")
        runtime = self._load()
        try:
            encode_options: dict[str, Any] = {
                "batch_size": len(materialized),
                "show_progress_bar": False,
                "convert_to_numpy": False,
                "normalize_embeddings": self.normalize_embeddings,
            }
            if self.prompt_name is not None:
                encode_options["prompt_name"] = self.prompt_name
            raw_vectors = runtime.encode(list(materialized), **encode_options)
        except Exception as exc:
            raise LocalEmbeddingUnavailableError(
                "Local embedding inference failed."
            ) from exc

        vectors: list[tuple[float, ...]] = []
        expected = self.resolve_dimensions()
        for raw in raw_vectors:
            if hasattr(raw, "tolist"):
                raw = raw.tolist()
            vector = tuple(float(value) for value in raw)
            if len(vector) != expected or any(not math.isfinite(value) for value in vector):
                raise EmbeddingDimensionError(
                    "Local embedding output has an invalid dimension or non-finite value."
                )
            vectors.append(vector)
        if len(vectors) != len(materialized):
            raise EmbeddingDimensionError(
                f"expected {len(materialized)} vectors, received {len(vectors)}"
            )
        return tuple(vectors)

    embed = embed_texts

    def status(self) -> dict[str, Any]:
        """Return value-free operational metadata suitable for reports."""

        return {
            "provider": self.provider_name,
            "model": self.model,
            "dimensions": self.dimensions,
            "enabled": True,
            "local_files_only": self.local_files_only,
            "normalize_embeddings": self.normalize_embeddings,
            "prompt_name": self.prompt_name,
            "loaded": self._runtime is not None,
        }


__all__ = [
    "LOCAL_EMBEDDING_PROVIDER",
    "LocalEmbeddingUnavailableError",
    "SentenceTransformerEmbeddingProvider",
]
