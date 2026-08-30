from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


SCHEMA = "ncs_search_precision_risk_audit_v1"
DEFAULT_INPUT = ROOT / "reports" / "ncs_search_eval_candidates_20260830.json"
DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_OUT = ROOT / "reports" / "ncs_search_precision_risk_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "ncs_search_precision_risk_20260830.md"
RESULT_TYPES = ("unit", "element", "criteria", "ksa")
NEAR_DUPLICATE_THRESHOLD = 0.92

SearchFunction = Callable[[str, str, int], dict[str, Any]]


def generated_at() -> str:
    return datetime.now(UTC).isoformat()


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = re.sub(r"[^0-9a-zA-Z\uac00-\ud7a3]+", " ", text)
    return " ".join(text.split())


def compact_text(value: Any) -> str:
    return normalize_text(value).replace(" ", "")


def safe_rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _median(values: Iterable[float | int | None]) -> float | None:
    usable = [float(value) for value in values if isinstance(value, (int, float))]
    return round(float(median(usable)), 4) if usable else None


def _walk_case_objects(value: Any, path: str = "$") -> Iterable[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        if value.get("case_id") is not None and value.get("query") is not None:
            yield path, value
        for key, child in value.items():
            yield from _walk_case_objects(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk_case_objects(child, f"{path}[{index}]")


def _is_simple_metadata(value: Any) -> bool:
    if value is None or isinstance(value, (str, int, float, bool)):
        return True
    if isinstance(value, list):
        return all(item is None or isinstance(item, (str, int, float, bool)) for item in value)
    return False


def extract_candidate_cases(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Merge candidate definitions and measured case rows without using relevance labels."""
    grouped: dict[str, dict[str, Any]] = {}
    source_paths: dict[str, list[str]] = defaultdict(list)
    for path, raw_case in _walk_case_objects(payload):
        case_id = str(raw_case["case_id"])
        target = grouped.setdefault(case_id, {"case_id": case_id})
        source_paths[case_id].append(path)
        definition_like = "preview" not in raw_case and "result_count" not in raw_case
        for key, value in raw_case.items():
            if key in {"preview", "results"} or not _is_simple_metadata(value):
                continue
            if key not in target or definition_like or target[key] in (None, "", []):
                target[key] = value

    cases: list[dict[str, Any]] = []
    for case_id in sorted(grouped):
        case = grouped[case_id]
        query = str(case.get("query") or "").strip()
        if not query:
            continue
        scope = str(case.get("measurement_scope") or case.get("scope") or "all").lower()
        if scope not in {*RESULT_TYPES, "all"}:
            scope = "all"
        case["query"] = query
        case["measurement_scope"] = scope
        case["source_paths"] = sorted(source_paths[case_id])
        case["off_scope_candidate"] = is_off_scope_candidate(case)
        cases.append(case)
    return cases


def is_off_scope_candidate(case: dict[str, Any]) -> bool:
    explicit = case.get("off_scope_candidate")
    if isinstance(explicit, bool):
        return explicit
    signals: list[str] = []
    for key, value in case.items():
        normalized_key = normalize_text(key).replace(" ", "_")
        if "off_scope" in normalized_key or "out_of_scope" in normalized_key:
            if value not in (False, None, "", 0, []):
                return True
        if key.lower() not in {
            "category",
            "case",
            "case_family",
            "candidate_kind",
            "candidate_type",
            "expected_behavior",
            "intent",
            "intent_class",
            "notes",
            "rationale",
            "risk_tags",
            "tags",
            "query_family",
        }:
            continue
        if isinstance(value, list):
            signals.extend(str(item) for item in value)
        elif value is not None:
            signals.append(str(value))
    joined = " ".join(signals).casefold()
    return bool(
        re.search(r"off[ _-]?scope|out[ _-]?of[ _-]?scope|non[ _-]?ncs", joined)
        or re.search(r"\ube44\ubc94\uc704|\ubc94\uc704\s*\ubc16", joined)
    )


def load_runtime_search(db_path: Path) -> SearchFunction:
    os.environ["NCS_DB_PATH"] = str(db_path.resolve())
    os.environ["NCS_MCP_READ_ONLY_MODE"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    from ncs_mcp.server import search_ncs

    return search_ncs


def _entropy(counter: Counter[str], *, domain_size: int | None = None) -> float | None:
    total = sum(counter.values())
    if total <= 0:
        return None
    size = domain_size or len(counter)
    if size <= 1:
        return 0.0
    entropy = -sum((count / total) * math.log(count / total) for count in counter.values())
    return round(entropy / math.log(size), 4)


def _result_unit_key(result: dict[str, Any]) -> str | None:
    path = result.get("path") if isinstance(result.get("path"), dict) else {}
    unit_code = path.get("unit_code")
    if unit_code:
        return str(unit_code)
    if result.get("type") == "unit" and result.get("id") is not None:
        return str(result["id"])
    return None


def _result_major_key(result: dict[str, Any]) -> str | None:
    path = result.get("path") if isinstance(result.get("path"), dict) else {}
    major_code = path.get("major_code")
    if major_code:
        return str(major_code)
    unit_key = _result_unit_key(result)
    if unit_key and len(unit_key) >= 2 and unit_key[:2].isdigit():
        return unit_key[:2]
    return None


def _preview_duplicate_summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    preview = results[:5]
    pairs: list[dict[str, Any]] = []
    for left_index in range(len(preview)):
        left = preview[left_index]
        left_label = str(left.get("text") or left.get("label") or "")
        left_compact = compact_text(left_label)
        if not left_compact:
            continue
        for right_index in range(left_index + 1, len(preview)):
            right = preview[right_index]
            right_label = str(right.get("text") or right.get("label") or "")
            right_compact = compact_text(right_label)
            if not right_compact:
                continue
            exact = left_compact == right_compact
            ratio = SequenceMatcher(None, left_compact, right_compact).ratio()
            if not exact and (min(len(left_compact), len(right_compact)) < 4 or ratio < NEAR_DUPLICATE_THRESHOLD):
                continue
            pairs.append(
                {
                    "left_rank": left_index + 1,
                    "right_rank": right_index + 1,
                    "kind": "exact" if exact else "near",
                    "similarity": round(ratio, 4),
                    "left_stable_id": f"{left.get('type')}:{left.get('id')}",
                    "right_stable_id": f"{right.get('type')}:{right.get('id')}",
                    "left_label": left_label,
                    "right_label": right_label,
                }
            )
    return {
        "preview_size": len(preview),
        "has_duplicate_or_near_duplicate": bool(pairs),
        "pair_count": len(pairs),
        "exact_pair_count": sum(pair["kind"] == "exact" for pair in pairs),
        "near_pair_count": sum(pair["kind"] == "near" for pair in pairs),
        "threshold": NEAR_DUPLICATE_THRESHOLD,
        "pairs": pairs,
    }


def _type_imbalance(results: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    counts = Counter(str(result.get("type") or "unknown") for result in results)
    total = len(results)
    if scope != "all" or total <= 0:
        return {
            "applicable": False,
            "counts_by_type": dict(sorted(counts.items())),
            "total_variation_from_equal": None,
            "max_type_share": None,
            "severe": False,
        }
    shares = {result_type: counts.get(result_type, 0) / total for result_type in RESULT_TYPES}
    total_variation = 0.5 * sum(abs(share - 0.25) for share in shares.values())
    return {
        "applicable": True,
        "counts_by_type": {result_type: counts.get(result_type, 0) for result_type in RESULT_TYPES},
        "total_variation_from_equal": round(total_variation, 4),
        "max_type_share": round(max(shares.values()), 4),
        "severe": total_variation > 0.25 or max(shares.values()) > 0.6,
        "severe_thresholds": {
            "total_variation_gt": 0.25,
            "max_type_share_gt": 0.6,
        },
    }


def _scope_diversity(results: list[dict[str, Any]], scope: str) -> dict[str, Any]:
    type_counts = Counter(str(result.get("type") or "unknown") for result in results)
    major_counts = Counter(key for result in results if (key := _result_major_key(result)))
    unit_counts = Counter(key for result in results if (key := _result_unit_key(result)))
    return {
        "result_type_count": len(type_counts),
        "result_type_entropy": _entropy(type_counts, domain_size=4 if scope == "all" else None),
        "distinct_major_count": len(major_counts),
        "major_entropy": _entropy(major_counts),
        "major_code_coverage": safe_rate(sum(major_counts.values()), len(results)),
        "distinct_unit_count": len(unit_counts),
        "unit_code_coverage": safe_rate(sum(unit_counts.values()), len(results)),
    }


def _metadata_values(case: dict[str, Any]) -> dict[str, Any]:
    excluded = {"query", "source_paths", "off_scope_candidate"}
    return {key: value for key, value in case.items() if key not in excluded and _is_simple_metadata(value)}


def _case_risk_score(case: dict[str, Any]) -> float:
    score = 0.0
    if case["off_scope_candidate"] and case["result_count"] > 0:
        score += 4.0
    if case["or_tier"]["or_only"]:
        score += 2.0
    score += 2.0 * float(case["single_token_common_word"]["occupancy_rate"] or 0.0)
    if case["type_imbalance"]["severe"]:
        score += 1.5
    if case["preview_duplicates"]["has_duplicate_or_near_duplicate"]:
        score += 1.0
    if case["result_count"] >= 5 and case["scope_diversity"]["distinct_major_count"] <= 1:
        score += 1.0
    return round(score, 4)


def audit_cases(
    cases: list[dict[str, Any]],
    search_fn: SearchFunction,
    *,
    limit: int,
) -> dict[str, Any]:
    raw_runs: list[dict[str, Any]] = []
    query_token_document_frequency: Counter[str] = Counter()
    for case in cases:
        try:
            payload = search_fn(case["query"], case["measurement_scope"], limit)
            if not isinstance(payload, dict):
                raise TypeError("search response must be a dict")
            results = payload.get("results")
            if not isinstance(results, list):
                results = []
            query_tokens = {
                normalize_text(token)
                for token in payload.get("query_tokens", [])
                if normalize_text(token)
            }
            query_token_document_frequency.update(query_tokens)
            raw_runs.append({"case": case, "payload": payload, "results": results, "error": None})
        except Exception as exc:  # Audit should preserve all case failures in evidence.
            raw_runs.append(
                {
                    "case": case,
                    "payload": {},
                    "results": [],
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                }
            )

    common_token_min_case_count = max(3, math.ceil(len(cases) * 0.10))
    common_tokens = {
        token
        for token, count in query_token_document_frequency.items()
        if count >= common_token_min_case_count
    }

    audited_cases: list[dict[str, Any]] = []
    aggregate_tiers: Counter[str] = Counter()
    cross_case_ids: dict[str, set[str]] = defaultdict(set)
    cross_case_labels: dict[str, set[str]] = defaultdict(set)
    for run in raw_runs:
        case = run["case"]
        payload = run["payload"]
        results = run["results"]
        tier_counts = Counter(
            str(result.get("match_mode") or "missing").lower() for result in results
        )
        aggregate_tiers.update(tier_counts)
        or_result_count = tier_counts.get("or", 0)
        matched_token_counts: Counter[str] = Counter()
        single_token_count = 0
        common_single_token_count = 0
        missing_match_metadata_count = 0
        for result in results:
            matched_tokens = {
                normalize_text(token)
                for token in result.get("matched_tokens", [])
                if normalize_text(token)
            }
            if not result.get("match_mode") or not isinstance(result.get("matched_tokens"), list):
                missing_match_metadata_count += 1
            if len(matched_tokens) == 1:
                single_token_count += 1
                token = next(iter(matched_tokens))
                matched_token_counts[token] += 1
                if token in common_tokens:
                    common_single_token_count += 1
            stable_id = f"{result.get('type')}:{result.get('id')}"
            cross_case_ids[stable_id].add(case["case_id"])
            label_key = compact_text(result.get("text") or result.get("label"))
            if label_key:
                cross_case_labels[label_key].add(case["case_id"])

        case_record = {
            "case_id": case["case_id"],
            "query": case["query"],
            "measurement_scope": case["measurement_scope"],
            "input_metadata": _metadata_values(case),
            "off_scope_candidate": bool(case["off_scope_candidate"]),
            "result_count": len(results),
            "error": run["error"],
            "response_match_mode": payload.get("match_mode"),
            "match_mode_by_type": payload.get("match_mode_by_type", {}),
            "match_tier_distribution": dict(sorted(tier_counts.items())),
            "or_tier": {
                "or_result_count": or_result_count,
                "or_result_rate": safe_rate(or_result_count, len(results)),
                "or_only": bool(results) and set(tier_counts) == {"or"},
            },
            "single_token_common_word": {
                "single_token_match_count": single_token_count,
                "single_token_match_rate": safe_rate(single_token_count, len(results)),
                "common_single_token_match_count": common_single_token_count,
                "occupancy_rate": safe_rate(common_single_token_count, len(results)),
                "dominant_single_tokens": [
                    {"token": token, "count": count, "share": safe_rate(count, len(results))}
                    for token, count in matched_token_counts.most_common(5)
                ],
            },
            "type_imbalance": _type_imbalance(results, case["measurement_scope"]),
            "preview_duplicates": _preview_duplicate_summary(results),
            "scope_diversity": _scope_diversity(results, case["measurement_scope"]),
            "missing_match_metadata_count": missing_match_metadata_count,
            "preview": [
                {
                    "rank": index + 1,
                    "type": result.get("type"),
                    "id": result.get("id"),
                    "label": result.get("text") or result.get("label"),
                    "match_mode": result.get("match_mode"),
                    "matched_tokens": result.get("matched_tokens", []),
                }
                for index, result in enumerate(results[:5])
            ],
        }
        case_record["risk_score"] = _case_risk_score(case_record)
        audited_cases.append(case_record)

    returned_cases = [case for case in audited_cases if case["result_count"] > 0]
    off_scope_cases = [case for case in audited_cases if case["off_scope_candidate"]]
    off_scope_hits = [case for case in off_scope_cases if case["result_count"] > 0]
    all_scope_cases = [
        case for case in audited_cases if case["type_imbalance"]["applicable"]
    ]
    severe_imbalance_cases = [
        case for case in all_scope_cases if case["type_imbalance"]["severe"]
    ]
    duplicate_cases = [
        case
        for case in audited_cases
        if case["preview_duplicates"]["has_duplicate_or_near_duplicate"]
    ]
    low_major_diversity_cases = [
        case
        for case in audited_cases
        if case["result_count"] >= 5 and case["scope_diversity"]["distinct_major_count"] <= 1
    ]
    total_results = sum(case["result_count"] for case in audited_cases)
    common_single_results = sum(
        case["single_token_common_word"]["common_single_token_match_count"]
        for case in audited_cases
    )
    single_token_results = sum(
        case["single_token_common_word"]["single_token_match_count"]
        for case in audited_cases
    )
    or_only_cases = [case for case in returned_cases if case["or_tier"]["or_only"]]
    repeated_ids = {
        stable_id: sorted(case_ids)
        for stable_id, case_ids in cross_case_ids.items()
        if len(case_ids) >= 2
    }
    repeated_labels = {
        label: sorted(case_ids)
        for label, case_ids in cross_case_labels.items()
        if len(case_ids) >= 2
    }

    aggregate = {
        "case_count": len(audited_cases),
        "returned_case_count": len(returned_cases),
        "search_error_count": sum(case["error"] is not None for case in audited_cases),
        "total_result_count": total_results,
        "off_scope_candidate": {
            "case_count": len(off_scope_cases),
            "hit_count": len(off_scope_hits),
            "hit_rate": safe_rate(len(off_scope_hits), len(off_scope_cases)),
            "hit_case_ids": [case["case_id"] for case in off_scope_hits],
            "available": bool(off_scope_cases),
        },
        "or_tier": {
            "or_only_case_count": len(or_only_cases),
            "or_only_case_rate": safe_rate(len(or_only_cases), len(returned_cases)),
            "or_only_case_ids": [case["case_id"] for case in or_only_cases],
            "or_result_count": aggregate_tiers.get("or", 0),
            "or_result_rate": safe_rate(aggregate_tiers.get("or", 0), total_results),
        },
        "single_token_common_word": {
            "derivation": "query token document frequency across candidate cases",
            "minimum_case_count": common_token_min_case_count,
            "common_tokens": [
                {"token": token, "case_count": query_token_document_frequency[token]}
                for token in sorted(common_tokens)
            ],
            "single_token_result_count": single_token_results,
            "single_token_result_rate": safe_rate(single_token_results, total_results),
            "common_single_token_result_count": common_single_results,
            "common_single_token_result_occupancy_rate": safe_rate(common_single_results, total_results),
        },
        "type_imbalance": {
            "all_scope_case_count": len(all_scope_cases),
            "severe_case_count": len(severe_imbalance_cases),
            "severe_case_rate": safe_rate(len(severe_imbalance_cases), len(all_scope_cases)),
            "severe_case_ids": [case["case_id"] for case in severe_imbalance_cases],
            "median_total_variation_from_equal": _median(
                case["type_imbalance"]["total_variation_from_equal"] for case in all_scope_cases
            ),
            "max_total_variation_from_equal": max(
                (case["type_imbalance"]["total_variation_from_equal"] for case in all_scope_cases),
                default=None,
            ),
        },
        "preview_duplicates": {
            "case_count_with_duplicate_or_near_duplicate": len(duplicate_cases),
            "case_rate": safe_rate(len(duplicate_cases), len(audited_cases)),
            "case_ids": [case["case_id"] for case in duplicate_cases],
            "exact_pair_count": sum(case["preview_duplicates"]["exact_pair_count"] for case in audited_cases),
            "near_pair_count": sum(case["preview_duplicates"]["near_pair_count"] for case in audited_cases),
            "cross_case_repeated_stable_id_count": len(repeated_ids),
            "cross_case_repeated_normalized_label_count": len(repeated_labels),
            "cross_case_repeated_stable_ids_preview": dict(list(sorted(repeated_ids.items()))[:20]),
            "cross_case_repeated_labels_preview": dict(list(sorted(repeated_labels.items()))[:20]),
        },
        "match_tier_distribution": dict(sorted(aggregate_tiers.items())),
        "scope_diversity": {
            "median_distinct_major_count": _median(
                case["scope_diversity"]["distinct_major_count"] for case in audited_cases
            ),
            "median_distinct_unit_count": _median(
                case["scope_diversity"]["distinct_unit_count"] for case in audited_cases
            ),
            "median_result_type_entropy": _median(
                case["scope_diversity"]["result_type_entropy"] for case in all_scope_cases
            ),
            "low_major_diversity_case_count": len(low_major_diversity_cases),
            "low_major_diversity_case_rate": safe_rate(len(low_major_diversity_cases), len(audited_cases)),
            "low_major_diversity_case_ids": [case["case_id"] for case in low_major_diversity_cases],
        },
        "match_metadata": {
            "missing_result_count": sum(case["missing_match_metadata_count"] for case in audited_cases),
            "complete": all(case["missing_match_metadata_count"] == 0 for case in audited_cases),
        },
    }
    return {
        "query_token_document_frequency": dict(sorted(query_token_document_frequency.items())),
        "aggregate": aggregate,
        "cases": sorted(audited_cases, key=lambda case: case["case_id"]),
        "highest_risk_cases": [
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "risk_score": case["risk_score"],
                "off_scope_hit": case["off_scope_candidate"] and case["result_count"] > 0,
                "or_only": case["or_tier"]["or_only"],
                "common_single_token_occupancy_rate": case["single_token_common_word"]["occupancy_rate"],
                "severe_type_imbalance": case["type_imbalance"]["severe"],
                "preview_duplicate": case["preview_duplicates"]["has_duplicate_or_near_duplicate"],
                "distinct_major_count": case["scope_diversity"]["distinct_major_count"],
            }
            for case in sorted(audited_cases, key=lambda case: (-case["risk_score"], case["case_id"]))[:15]
        ],
    }


def proposed_veto_policy() -> dict[str, Any]:
    return {
        "status": "proposed_for_strategy_comparison",
        "baseline_required": True,
        "candidate_must_use_same_eval_pack_and_limit": True,
        "rules": [
            {
                "metric": "off_scope_candidate.hit_rate",
                "veto_when": "candidate_rate > baseline_rate or any new off-scope case becomes a hit",
                "allowed_delta": 0.0,
                "reason": "Recall relaxation must not add candidate hits to explicitly off-scope probes.",
            },
            {
                "metric": "or_tier.or_only_case_rate",
                "veto_when": "candidate_rate - baseline_rate > 0.05",
                "allowed_delta": 0.05,
                "reason": "A faster strategy must not materially increase dependence on OR-only retrieval.",
            },
            {
                "metric": "single_token_common_word.common_single_token_result_occupancy_rate",
                "veto_when": "candidate_rate - baseline_rate > 0.05 or candidate_rate > 0.35",
                "allowed_delta": 0.05,
                "absolute_warning_threshold": 0.35,
                "reason": "Common single-token matches can crowd out multi-token intent evidence.",
            },
            {
                "metric": "type_imbalance.severe_case_rate",
                "veto_when": "candidate_rate - baseline_rate > 0.05",
                "allowed_delta": 0.05,
                "reason": "The all-scope surface must not become dominated by one result type.",
            },
            {
                "metric": "preview_duplicates.case_rate",
                "veto_when": "candidate_rate - baseline_rate > 0.05",
                "allowed_delta": 0.05,
                "reason": "Top-five evidence variety must not regress.",
            },
            {
                "metric": "scope_diversity.median_distinct_major_count",
                "veto_when": "candidate_median < baseline_median - 1",
                "allowed_drop": 1,
                "reason": "Broad OR results should not collapse onto fewer NCS major scopes without labels proving relevance.",
            },
            {
                "metric": "match_metadata.missing_result_count",
                "veto_when": "candidate_count > 0",
                "required_value": 0,
                "reason": "Every result must remain auditable by match tier and token evidence.",
            },
        ],
        "mandatory_human_labeled_gate": {
            "required": True,
            "metrics": ["Precision@5", "Recall@5", "Recall@10", "MRR@10", "nDCG@10", "off-scope relevance"],
            "statement": (
                "Proxy vetoes can reject obvious risk regressions, but cannot approve relevance. "
                "Promotion still requires explicit human judgments on the candidate evaluation pack."
            ),
        },
    }


def build_report(
    *,
    input_path: Path,
    db_path: Path,
    limit: int,
    search_fn: SearchFunction | None = None,
) -> dict[str, Any]:
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    cases = extract_candidate_cases(payload)
    if not cases:
        raise ValueError("no candidate evaluation cases found")
    runtime_search = search_fn or load_runtime_search(db_path)
    audit = audit_cases(cases, runtime_search, limit=limit)
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at(),
        "mode": "read_only_precision_risk_proxy_audit",
        "source": {
            "eval_pack_path": str(input_path),
            "eval_pack_schema": payload.get("schema"),
            "database_path": str(db_path),
            "search_contract": "ncs_mcp.server.search_ncs",
            "case_count": len(cases),
            "expected_case_count": 50,
            "expected_case_count_met": len(cases) == 50,
            "limit": limit,
        },
        "interpretation_contract": {
            "proxy_only": True,
            "human_relevance_labels_used": False,
            "precision_measured": False,
            "recall_measured": False,
            "mrr_measured": False,
            "ndcg_measured": False,
            "not_a_substitute_for": ["Precision@K", "Recall@K", "MRR", "MAP", "nDCG", "human relevance review"],
            "allowed_claim": "Automated structural risk proxies observed on current search responses.",
            "prohibited_claim": "Search relevance or ranking quality is proven by this audit.",
        },
        "method": {
            "off_scope_candidate": "Input metadata explicitly tags a case as off-scope; any returned row is counted as a candidate hit, not a proven false positive.",
            "or_tier_only": "A returned case is OR-only when every returned row has match_mode=or.",
            "single_token_common_word": "A common token appears in at least max(3, ceil(10% of cases)) candidate queries; occupancy counts rows matched by exactly one such token.",
            "type_imbalance": "For scope=all, total-variation distance from an equal 25% share across unit/element/criteria/ksa.",
            "duplicates": f"Exact normalized labels or SequenceMatcher ratio >= {NEAR_DUPLICATE_THRESHOLD} within the top-five preview.",
            "scope_diversity": "Distinct NCS major/unit keys and normalized entropy derived from result paths; diversity is not relevance.",
        },
        "audit": audit,
        "strategy_promotion_veto_policy": proposed_veto_policy(),
        "safety": {
            "database_open_mode": "read_only_runtime_contract",
            "database_writes": False,
            "raw_ksa_mutation": False,
            "status_updates": False,
            "human_reviewed_written": False,
            "accepted_written": False,
            "reviewed_written": False,
        },
        "commands": {
            "reproduce": (
                f'python scripts/audit_ncs_search_precision.py --input "{input_path}" '
                f'--db "{db_path}" --limit {limit}'
            ),
            "focused_tests": "python -m unittest tests.test_audit_ncs_search_precision -v",
        },
    }


def _fmt_rate(value: Any) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"


def render_markdown(report: dict[str, Any]) -> str:
    aggregate = report["audit"]["aggregate"]
    lines = [
        "# NCS Search Precision Risk Proxy Audit",
        "",
        f"- Schema: `{report['schema']}`",
        f"- Candidate cases: {report['source']['case_count']}",
        f"- Result limit: {report['source']['limit']}",
        "- Mode: read-only current-response audit",
        "",
        "## Verdict",
        "",
        "This report measures structural risk proxies only. It does not measure or replace Precision@K, Recall@K, MRR, MAP, nDCG, or human relevance review.",
        "A strategy may be vetoed by these proxies, but it cannot be approved for relevance without explicit human labels.",
        "",
        "## Metrics Snapshot",
        "",
        "| Proxy | Value |",
        "| --- | ---: |",
        f"| Off-scope candidate hit rate | {_fmt_rate(aggregate['off_scope_candidate']['hit_rate'])} |",
        f"| OR-tier-only returned-case rate | {_fmt_rate(aggregate['or_tier']['or_only_case_rate'])} |",
        f"| OR-tier result rate | {_fmt_rate(aggregate['or_tier']['or_result_rate'])} |",
        f"| Common single-token result occupancy | {_fmt_rate(aggregate['single_token_common_word']['common_single_token_result_occupancy_rate'])} |",
        f"| Severe all-scope type-imbalance case rate | {_fmt_rate(aggregate['type_imbalance']['severe_case_rate'])} |",
        f"| Top-five duplicate/near-duplicate case rate | {_fmt_rate(aggregate['preview_duplicates']['case_rate'])} |",
        f"| Median distinct major count | {aggregate['scope_diversity']['median_distinct_major_count']} |",
        f"| Missing result match metadata | {aggregate['match_metadata']['missing_result_count']} |",
        "",
        "## Match Tier Distribution",
        "",
    ]
    tier_distribution = aggregate["match_tier_distribution"]
    if tier_distribution:
        for tier, count in tier_distribution.items():
            lines.append(f"- `{tier}`: {count}")
    else:
        lines.append("- No returned results.")

    lines.extend(["", "## Corpus-Derived Common Tokens", ""])
    common_tokens = aggregate["single_token_common_word"]["common_tokens"]
    if common_tokens:
        for item in common_tokens:
            lines.append(f"- `{item['token']}`: {item['case_count']} cases")
    else:
        lines.append("- None at the configured document-frequency threshold.")

    lines.extend(["", "## Highest-Risk Cases", "", "| Case | Query | Score | Off-scope hit | OR-only | Common-token occupancy | Type imbalance | Preview duplicate | Major scopes |", "| --- | --- | ---: | --- | --- | ---: | --- | --- | ---: |"])
    for item in report["audit"]["highest_risk_cases"]:
        query = str(item["query"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {item['case_id']} | {query} | {item['risk_score']} | "
            f"{str(item['off_scope_hit']).lower()} | {str(item['or_only']).lower()} | "
            f"{_fmt_rate(item['common_single_token_occupancy_rate'])} | "
            f"{str(item['severe_type_imbalance']).lower()} | "
            f"{str(item['preview_duplicate']).lower()} | {item['distinct_major_count']} |"
        )

    lines.extend(["", "## Proposed Strategy-Promotion Vetoes", ""])
    for rule in report["strategy_promotion_veto_policy"]["rules"]:
        lines.append(f"- `{rule['metric']}`: {rule['veto_when']}")
    lines.extend(
        [
            "",
            "## Required Human-Labeled Gate",
            "",
            "Before lazy-tier or another strategy is promoted, compare the same candidate pack and limit, then obtain explicit human relevance judgments for Precision@5, Recall@5/10, MRR@10, nDCG@10, and off-scope relevance.",
            "No automated status or approval field was written by this audit.",
            "",
            "## Reproduce",
            "",
            f"`{report['commands']['reproduce']}`",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit NCS search precision-risk proxies.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.limit <= 0:
        raise ValueError("limit must be positive")
    report = build_report(
        input_path=args.input.resolve(),
        db_path=args.db.resolve(),
        limit=args.limit,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "cases": report["source"]["case_count"],
                "search_errors": report["audit"]["aggregate"]["search_error_count"],
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
