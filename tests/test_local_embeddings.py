from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.embeddings import EmbeddingDimensionError  # noqa: E402
from ncs_mcp.local_embeddings import (
    LocalEmbeddingUnavailableError,
    SentenceTransformerEmbeddingProvider,
)  # noqa: E402


class _FakeVector:
    def __init__(self, values):
        self.values = values

    def tolist(self):
        return self.values


class _FakeModel:
    def __init__(self, dimension=3, vectors=None):
        self.dimension = dimension
        self.vectors = vectors
        self.calls = []

    def get_sentence_embedding_dimension(self):
        return self.dimension

    def encode(self, texts, **kwargs):
        self.calls.append((texts, kwargs))
        if self.vectors is not None:
            return self.vectors
        return [_FakeVector([float(index + 1)] * self.dimension) for index, _ in enumerate(texts)]


class LocalEmbeddingProviderTests(unittest.TestCase):
    def test_lazy_local_only_load_and_validated_vectors(self):
        model = _FakeModel()
        calls = []

        def loader(name, **kwargs):
            calls.append((name, kwargs))
            return model

        provider = SentenceTransformerEmbeddingProvider("cached/model", loader=loader)
        self.assertFalse(provider.status()["loaded"])
        vectors = provider.embed_texts(["one", "two"])
        self.assertEqual(vectors, ((1.0, 1.0, 1.0), (2.0, 2.0, 2.0)))
        self.assertEqual(calls, [("cached/model", {"local_files_only": True})])
        self.assertEqual(provider.dimensions, 3)
        self.assertTrue(model.calls[0][1]["normalize_embeddings"])

    def test_configured_dimension_must_match_model(self):
        provider = SentenceTransformerEmbeddingProvider(
            "cached/model", dimensions=2, loader=lambda *_args, **_kwargs: _FakeModel(3)
        )
        with self.assertRaises(EmbeddingDimensionError):
            provider.resolve_dimensions()

    def test_optional_query_prompt_is_forwarded_without_changing_default(self):
        default_model = _FakeModel()
        default_provider = SentenceTransformerEmbeddingProvider(
            "cached/model", loader=lambda *_args, **_kwargs: default_model
        )
        default_provider.embed_texts(["document"])
        self.assertNotIn("prompt_name", default_model.calls[0][1])

        query_model = _FakeModel()
        query_provider = SentenceTransformerEmbeddingProvider(
            "cached/model", prompt_name="query",
            loader=lambda *_args, **_kwargs: query_model,
        )
        query_provider.embed_texts(["query"])
        self.assertEqual(query_model.calls[0][1]["prompt_name"], "query")
        self.assertEqual(query_provider.status()["prompt_name"], "query")

    def test_invalid_outputs_fail_closed(self):
        provider = SentenceTransformerEmbeddingProvider(
            "cached/model",
            loader=lambda *_args, **_kwargs: _FakeModel(
                2, vectors=[_FakeVector([1.0, math.inf])]
            ),
        )
        with self.assertRaises(EmbeddingDimensionError):
            provider.embed_texts(["one"])

    def test_dependency_failure_is_normalized(self):
        def fail(*_args, **_kwargs):
            raise RuntimeError("secret backend detail")

        provider = SentenceTransformerEmbeddingProvider("cached/model", loader=fail)
        with self.assertRaisesRegex(LocalEmbeddingUnavailableError, "runtime is unavailable") as ctx:
            provider.resolve_dimensions()
        self.assertNotIn("secret backend detail", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
