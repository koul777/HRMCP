"""Promote one semantically strong NCS unit from rank 4+ into third place.

Lexical ranking scores a unit by where a query token appears, so a unit whose
name literally contains the query words outranks the unit that performs the
work. Measured on the 90-query development set, the correct unit sits at rank
4-9 in those cases while carrying the query terms in its definition or
performance criteria.

This module only reorders candidates the lexical search already returned. It
never generates candidates, never scans the corpus, and leaves the lexical top
two in place so an alias-driven answer cannot be displaced. Measured end to end
with precomputed unit vectors, the 90-query development set rises from Hit@3
0.800 to 0.833 with three cases fixed and none broken, while the alias-tuned
40-query regression set stays at 0.875.

No embedding provider ships with the serving package yet. Until one is
configured the search path calls :func:`rescue_order` with no provider and gets
the lexical order back unchanged.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence, runtime_checkable


# Below this margin a rank-4+ candidate may not take third place. 0.0 costs the
# regression set a case on every model measured, and 0.05 gives back half the
# gain. Both 0.01 and 0.02 reach 0.833, so take the higher one: it promotes on
# 7 of 130 queries instead of 10 for the same result, and a rule that intervenes
# less is easier to reason about when it is wrong.
DEFAULT_RESCUE_MARGIN = 0.02
# Only the returned page is inspected; a wider window would add cost without
# evidence, since no measured case had its answer below rank 10.
DEFAULT_RESCUE_WINDOW = 10
RESCUE_SCHEMA = "ncs_search_semantic_rescue_v1"


@runtime_checkable
class SemanticSimilarityProvider(Protocol):
    """Return one similarity per unit code, aligned with *unit_codes*."""

    def unit_similarities(
        self, query: str, unit_codes: Sequence[str]
    ) -> Sequence[float] | None:
        ...


def rescue_index(
    similarities: Sequence[float],
    *,
    margin: float = DEFAULT_RESCUE_MARGIN,
) -> int | None:
    """Return the index that earns third place, or None to keep lexical order."""
    if len(similarities) <= 3:
        return None
    head = max(similarities[:3])
    tail = range(3, len(similarities))
    best = max(tail, key=lambda index: similarities[index])
    if similarities[best] > head + abs(margin):
        return best
    return None


def rescue_order(
    candidates: Sequence[dict[str, Any]],
    *,
    query: str,
    provider: SemanticSimilarityProvider | None,
    margin: float = DEFAULT_RESCUE_MARGIN,
    window: int = DEFAULT_RESCUE_WINDOW,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Reorder *candidates* once, keeping the lexical top two fixed.

    Returns the candidates and, when a promotion happened, evidence describing
    it. A provider that returns nothing, raises, or answers with a mismatched
    length leaves the order untouched: a semantic hint must never turn a
    working lexical result into an error.
    """
    items = list(candidates)
    if provider is None or len(items) <= 3:
        return items, None
    head = items[:window]
    codes = [str(item.get("id") or "") for item in head]
    if not all(codes):
        return items, None
    try:
        similarities = provider.unit_similarities(query, codes)
    except Exception:
        return items, None
    if similarities is None or len(similarities) != len(head):
        return items, None
    scores = [float(value) for value in similarities]
    promoted = rescue_index(scores, margin=margin)
    if promoted is None:
        return items, None
    reordered = [head[0], head[1], head[promoted]]
    reordered += [item for index, item in enumerate(head) if index not in (0, 1, promoted)]
    evidence = {
        "schema": RESCUE_SCHEMA,
        "applied": True,
        "margin": margin,
        "window": len(head),
        "promoted_unit_code": codes[promoted],
        "promoted_from_rank": promoted + 1,
        "similarity": round(scores[promoted], 6),
        "displaced_top3_similarity": round(max(scores[:3]), 6),
        "basis": "unit_name_and_definition_embedding",
        "human_review_or_approval_claim": False,
    }
    return reordered + items[window:], evidence
