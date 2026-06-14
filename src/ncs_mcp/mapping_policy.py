from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any


REVIEWED_STATUSES = {"accepted", "reviewed", "human_reviewed"}
ALWAYS_EXCLUDED_STATUSES = {"rejected"}
LOW_CONFIDENCE_STATUSES = {"low_confidence"}


@dataclass(frozen=True)
class MappingFilter:
    min_score: float = 7.0
    excluded_relations: tuple[str, ...] = ("related",)
    include_candidates: bool = True
    include_low_confidence: bool = False
    include_rejected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "min_score": self.min_score,
            "excluded_relations": list(self.excluded_relations),
            "include_candidates": self.include_candidates,
            "include_low_confidence": self.include_low_confidence,
            "include_rejected": self.include_rejected,
        }


DEFAULT_MAPPING_FILTER = MappingFilter()


def mapping_value(match: dict[str, Any], key: str, default: Any = None) -> Any:
    return (match.get("mapping") or {}).get(key, default)


def mapping_exclusion_reason(
    match: dict[str, Any],
    policy: MappingFilter = DEFAULT_MAPPING_FILTER,
) -> str | None:
    status = str(mapping_value(match, "review_status", "candidate") or "candidate")
    relation = str(mapping_value(match, "relation", "") or "")
    try:
        score = float(mapping_value(match, "score", 0) or 0)
    except (TypeError, ValueError):
        score = 0.0

    if status in ALWAYS_EXCLUDED_STATUSES and not policy.include_rejected:
        return "rejected"
    if status in LOW_CONFIDENCE_STATUSES and not policy.include_low_confidence:
        return "low_confidence_status"
    if status in REVIEWED_STATUSES:
        return None
    if not policy.include_candidates:
        return "candidate_not_allowed"
    if relation in set(policy.excluded_relations):
        return f"relation:{relation}"
    if score < policy.min_score:
        return "score_below_threshold"
    return None


def mapping_sort_key(match: dict[str, Any]) -> tuple[int, float, str]:
    status = str(mapping_value(match, "review_status", "candidate") or "candidate")
    priority = 0 if status in REVIEWED_STATUSES else 1
    try:
        score = float(mapping_value(match, "score", 0) or 0)
    except (TypeError, ValueError):
        score = 0.0
    target_id = str(mapping_value(match, "target_id", "") or match.get("target", {}).get("unit_code", ""))
    return (priority, -score, target_id)


def apply_mapping_filter(
    matches: list[dict[str, Any]],
    policy: MappingFilter = DEFAULT_MAPPING_FILTER,
) -> dict[str, Any]:
    used: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    candidate_based = False

    for match in matches:
        reason = mapping_exclusion_reason(match, policy)
        if reason:
            reasons[reason] += 1
            excluded.append(
                {
                    "target_id": mapping_value(match, "target_id")
                    or match.get("target", {}).get("unit_code"),
                    "relation": mapping_value(match, "relation"),
                    "score": mapping_value(match, "score"),
                    "review_status": mapping_value(match, "review_status"),
                    "reason": reason,
                }
            )
            continue
        used.append(match)
        status = str(mapping_value(match, "review_status", "candidate") or "candidate")
        if status not in REVIEWED_STATUSES:
            candidate_based = True

    used.sort(key=mapping_sort_key)
    return {
        "matches": used,
        "excluded": excluded,
        "metadata": {
            "mapping_filter": policy.as_dict(),
            "used_mapping_count": len(used),
            "excluded_mapping_count": len(excluded),
            "exclusion_reasons": dict(sorted(reasons.items())),
            "candidate_based": candidate_based,
            "caveats": [
                "후보 매핑 기반 분석은 공식 인정 또는 평가 판정이 아니다.",
                "accepted/reviewed가 아닌 candidate는 사람이 검토하기 전의 근거 기반 후보이다.",
            ]
            if candidate_based
            else ["이 결과는 공식 인정 또는 평가 판정이 아니다."],
        },
    }


def merge_filter_metadata(items: list[dict[str, Any]]) -> dict[str, Any]:
    used = sum(int(item.get("used_mapping_count", 0)) for item in items)
    excluded = sum(int(item.get("excluded_mapping_count", 0)) for item in items)
    reasons: Counter[str] = Counter()
    candidate_based = False
    mapping_filter = DEFAULT_MAPPING_FILTER.as_dict()
    caveats: set[str] = set()
    for item in items:
        mapping_filter = item.get("mapping_filter", mapping_filter)
        reasons.update(item.get("exclusion_reasons", {}))
        candidate_based = candidate_based or bool(item.get("candidate_based"))
        caveats.update(item.get("caveats", []))
    return {
        "mapping_filter": mapping_filter,
        "used_mapping_count": used,
        "excluded_mapping_count": excluded,
        "exclusion_reasons": dict(sorted(reasons.items())),
        "candidate_based": candidate_based,
        "caveats": sorted(caveats),
    }
