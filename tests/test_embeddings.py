from __future__ import annotations

import math
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.embeddings import (
    DEFAULT_EMBEDDING_PROVIDER,
    DisabledEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingDisabledError,
    build_embedding_metadata,
    build_semantic_text,
    content_hash,
    embedding_cache_key,
    is_boilerplate_definition,
    prepare_embedding_input,
    validate_embedding_batch,
    validate_embedding_dimension,
)


class EmbeddingFoundationTests(unittest.TestCase):
    def test_hash_and_cache_key_are_canonical_and_deterministic(self) -> None:
        self.assertEqual(content_hash(" 인사\n 기획 "), content_hash("인사 기획"))

        first = build_embedding_metadata(
            " 인사\n 기획 ",
            provider_name="LOCAL",
            model="model-a",
            dimensions=3,
        )
        second = build_embedding_metadata(
            "인사 기획",
            provider_name="local",
            model="model-a",
            dimensions=3,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first["content_hash"]), 64)
        self.assertTrue(first["cache_key"].startswith("emb:v1:"))
        self.assertNotEqual(
            first["cache_key"],
            embedding_cache_key(
                provider_name="local",
                model="model-b",
                dimensions=3,
                text="인사 기획",
            ),
        )

    def test_disabled_provider_is_safe_default(self) -> None:
        self.assertIsInstance(DEFAULT_EMBEDDING_PROVIDER, DisabledEmbeddingProvider)
        self.assertFalse(DEFAULT_EMBEDDING_PROVIDER.enabled)
        self.assertEqual(DEFAULT_EMBEDDING_PROVIDER.embed_texts([]), ())
        with self.assertRaises(EmbeddingDisabledError):
            DEFAULT_EMBEDDING_PROVIDER.embed_texts(["text"])

    def test_dimension_validation_checks_shape_and_values(self) -> None:
        self.assertEqual(validate_embedding_dimension([1, 2.5], 2), (1.0, 2.5))
        self.assertEqual(
            validate_embedding_batch(
                [[1, 2], [3, 4]],
                expected_count=2,
                expected_dimension=2,
            ),
            ((1.0, 2.0), (3.0, 4.0)),
        )
        with self.assertRaises(EmbeddingDimensionError):
            validate_embedding_dimension([1], 2)
        with self.assertRaises(EmbeddingDimensionError):
            validate_embedding_dimension([1, 2], 2.0)  # type: ignore[arg-type]
        with self.assertRaises(EmbeddingDimensionError):
            validate_embedding_dimension([1, math.inf], 2)
        with self.assertRaises(EmbeddingDimensionError):
            validate_embedding_batch(
                [[1, 2]],
                expected_count=2,
                expected_dimension=2,
            )

    def test_untrusted_and_boilerplate_definitions_are_never_semantic_text(self) -> None:
        boilerplate = (
            "인사기획: 업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, "
            "사례에 대한 지식."
        )
        self.assertTrue(
            is_boilerplate_definition(boilerplate, concept_name="인사기획")
        )
        semantic = build_semantic_text(
            label="인사기획",
            source_text="인사 전략을 수립한다.",
            definition="검토되지 않은 초안 정의",
            definition_status="defined",
            review_status="llm_reviewed",
        )
        self.assertNotIn("초안 정의", semantic)

        even_trusted_boilerplate = build_semantic_text(
            label="인사기획",
            definition=boilerplate,
            definition_status="defined",
            review_status="human_reviewed",
        )
        self.assertEqual(even_trusted_boilerplate, "인사기획")

        trusted = prepare_embedding_input(
            provider_name="local",
            model="fixture",
            dimensions=2,
            label="인사기획",
            definition="조직의 인사 방향과 실행 계획을 설계하는 지식.",
            definition_status="defined",
            review_status="human_reviewed",
        )
        self.assertIn("실행 계획", trusted.text)
        self.assertTrue(trusted.metadata["definition_included"])


if __name__ == "__main__":
    unittest.main()
