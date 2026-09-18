from __future__ import annotations

import unittest
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ncs_mcp.search import core
from ncs_mcp.search.semantic_rescue import (
    DEFAULT_RESCUE_MARGIN,
    rescue_index,
    rescue_order,
)


def candidates(count: int) -> list[dict[str, object]]:
    return [{"id": f"unit-{index}", "text": f"name {index}"} for index in range(count)]


class FakeProvider:
    def __init__(self, similarities, *, raises=False, wrong_length=False):
        self.similarities = similarities
        self.raises = raises
        self.wrong_length = wrong_length
        self.calls: list[tuple[str, list[str]]] = []

    def unit_similarities(self, query, unit_codes):
        self.calls.append((query, list(unit_codes)))
        if self.raises:
            raise RuntimeError("provider unavailable")
        if self.wrong_length:
            return self.similarities[:-1]
        return self.similarities


class RescueIndexTests(unittest.TestCase):
    def test_promotes_only_above_the_margin(self) -> None:
        similarities = [0.50, 0.40, 0.30, 0.55]
        self.assertEqual(rescue_index(similarities, margin=0.0), 3)
        self.assertEqual(rescue_index(similarities, margin=0.04), 3)
        self.assertIsNone(rescue_index(similarities, margin=0.05))
        self.assertIsNone(rescue_index(similarities, margin=0.2))

    def test_short_lists_and_ties_keep_lexical_order(self) -> None:
        self.assertIsNone(rescue_index([0.9, 0.8, 0.7]))
        self.assertIsNone(rescue_index([]))
        # An exact tie is not "better than", so nothing is promoted.
        self.assertIsNone(rescue_index([0.5, 0.4, 0.3, 0.5], margin=0.0))

    def test_best_tail_candidate_wins_not_the_first_one(self) -> None:
        self.assertEqual(rescue_index([0.1, 0.1, 0.1, 0.5, 0.9], margin=0.0), 4)


class RescueOrderTests(unittest.TestCase):
    def test_without_a_provider_the_order_is_untouched(self) -> None:
        items = candidates(6)
        ordered, evidence = rescue_order(items, query="q", provider=None)
        self.assertEqual([item["id"] for item in ordered], [item["id"] for item in items])
        self.assertIsNone(evidence)

    def test_promotion_keeps_the_top_two_and_reports_evidence(self) -> None:
        provider = FakeProvider([0.2, 0.1, 0.1, 0.1, 0.9])
        ordered, evidence = rescue_order(candidates(5), query="급여 계산", provider=provider)
        self.assertEqual(
            [item["id"] for item in ordered],
            ["unit-0", "unit-1", "unit-4", "unit-2", "unit-3"],
        )
        self.assertEqual(evidence["promoted_unit_code"], "unit-4")
        self.assertEqual(evidence["promoted_from_rank"], 5)
        self.assertEqual(evidence["margin"], DEFAULT_RESCUE_MARGIN)
        self.assertFalse(evidence["human_review_or_approval_claim"])
        self.assertEqual(provider.calls[0][0], "급여 계산")

    def test_candidates_beyond_the_window_are_preserved_in_place(self) -> None:
        provider = FakeProvider([0.2, 0.1, 0.1, 0.9])
        ordered, evidence = rescue_order(
            candidates(6), query="q", provider=provider, window=4
        )
        self.assertEqual(
            [item["id"] for item in ordered],
            ["unit-0", "unit-1", "unit-3", "unit-2", "unit-4", "unit-5"],
        )
        self.assertEqual(evidence["window"], 4)

    def test_a_failing_or_inconsistent_provider_never_breaks_the_result(self) -> None:
        for provider in (
            FakeProvider([0.2, 0.1, 0.1, 0.9], raises=True),
            FakeProvider([0.2, 0.1, 0.1, 0.9], wrong_length=True),
            FakeProvider(None),
        ):
            with self.subTest(provider=provider):
                items = candidates(4)
                ordered, evidence = rescue_order(items, query="q", provider=provider)
                self.assertEqual(
                    [item["id"] for item in ordered], [item["id"] for item in items]
                )
                self.assertIsNone(evidence)

    def test_blank_unit_codes_are_not_scored(self) -> None:
        items = candidates(4)
        items[2]["id"] = ""
        provider = FakeProvider([0.2, 0.1, 0.1, 0.9])
        ordered, evidence = rescue_order(items, query="q", provider=provider)
        self.assertIsNone(evidence)
        self.assertEqual(provider.calls, [])
        self.assertEqual([item["id"] for item in ordered], [item["id"] for item in items])


class SearchRuntimeDefaultTests(unittest.TestCase):
    def test_serving_default_has_no_semantic_provider(self) -> None:
        # The serving path must stay lexical until a provider is configured on
        # purpose, so no deployment picks up a semantic step by accident.
        self.assertIsNone(core._SEMANTIC_PROVIDER)

    def test_configure_search_runtime_leaves_the_provider_unset_by_default(self) -> None:
        sentinel = object()
        previous = (
            core._OPEN_DB_FACTORY,
            core._CLAMP_LIMIT,
            core._UNIT_PATH,
            core._TIER_PREDICATES,
            core._TIER_EXECUTOR,
            core._TOKEN_EXPANDER,
            core._SEMANTIC_PROVIDER,
        )
        try:
            core.configure_search_runtime(
                open_db_factory=sentinel, clamp_limit=sentinel, unit_path=sentinel
            )
            self.assertIsNone(core._SEMANTIC_PROVIDER)
            provider = FakeProvider([1.0])
            core.configure_search_runtime(
                open_db_factory=sentinel,
                clamp_limit=sentinel,
                unit_path=sentinel,
                semantic_provider=provider,
            )
            self.assertIs(core._SEMANTIC_PROVIDER, provider)
        finally:
            (
                core._OPEN_DB_FACTORY,
                core._CLAMP_LIMIT,
                core._UNIT_PATH,
                core._TIER_PREDICATES,
                core._TIER_EXECUTOR,
                core._TOKEN_EXPANDER,
                core._SEMANTIC_PROVIDER,
            ) = previous


if __name__ == "__main__":
    unittest.main()
