from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.config import load_settings
from ncs_mcp.db import (
    HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL,
    connect,
    ksa_label_quality_flags,
    normalize_spaces,
    now_utc,
)


DEFAULT_SOURCE_METHODS = ("rule_based_short_label_candidate", "already_short_label")
FORBIDDEN_STATUS_WRITES = {"human_reviewed", "accepted", "reviewed"}
SAFE_RULE_BASED_DANGLING_ENDINGS = (
    "을 위한",
    "를 위한",
    " 위한",
    "할 수 있는",
    "할 수 없는",
    "수 있는",
    "수 없는",
    "에 대한",
    "에 관한",
    "에 따른",
    "을 통한",
    "를 통한",
    "통한",
    "위한",
    "대한",
    "관한",
    "따른",
    "있는",
    "없는",
    "및",
    "또는",
    "으로",
)
SAFE_KSA_LABEL_SUFFIXES = (
    "능력",
    "기술",
    "지식",
    "태도",
    "의지",
    "자세",
    "역량",
    "스킬",
)
SAFE_KSA_TRAILING_PHRASES = (
    "에 대한",
    "에 관한",
    "에 따른",
)
SOURCE_FAITHFUL_ALLOWED_QUALITY_FLAGS = {
    "changed_near_full_length",
    "very_low_label_source_ratio",
    "generic_or_low_specificity",
    "short_acronym_needs_context",
    "digit_heavy",
    "symbol_heavy",
}
REPAIRED_LABEL_SOURCE_METHOD = "llm_repaired_source_faithful_label"
REPAIRED_LABEL_ALLOWED_QUALITY_FLAGS = SOURCE_FAITHFUL_ALLOWED_QUALITY_FLAGS
REPAIRED_LABEL_REMOVE_ENDINGS = (
    "할 수 있는",
    "할 수 없는",
    "수 있는",
    "수 없는",
    "을 위한",
    "를 위한",
)


def _chunks(values: list[int], size: int = 500) -> list[list[int]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _label_ratio(source: str, label: str) -> float:
    return round(len(label) / max(1, len(source)), 6)


def _compact_label_key(value: str) -> str:
    return re.sub(r"\s+", "", normalize_spaces(value)).lower()


def _strip_parenthetical_text(value: str) -> str:
    text = normalize_spaces(value)
    for _ in range(8):
        next_text = re.sub(r"\([^()]*\)", " ", text)
        next_text = re.sub(r"\[[^\[\]]*\]", " ", next_text)
        next_text = re.sub(r"（[^（）]*）", " ", next_text)
        next_text = normalize_spaces(next_text)
        if next_text == text:
            break
        text = next_text
    return text


def _strip_ksa_label_suffixes(value: str) -> str:
    text = normalize_spaces(value)
    for _ in range(8):
        original = text
        for phrase in SAFE_KSA_TRAILING_PHRASES:
            if text.endswith(phrase) and len(text) > len(phrase) + 1:
                text = normalize_spaces(text[: -len(phrase)])
        for suffix in sorted(SAFE_KSA_LABEL_SUFFIXES, key=len, reverse=True):
            if text.endswith(suffix) and len(text) > len(suffix) + 1:
                text = normalize_spaces(text[: -len(suffix)])
                break
        if text == original:
            break
    return text


def _source_text_to_short_label_basis(value: str) -> str:
    return _strip_ksa_label_suffixes(_strip_parenthetical_text(value))


def _repair_source_faithful_label(value: str) -> str:
    text = _source_text_to_short_label_basis(value)
    for _ in range(6):
        original = text
        for ending in REPAIRED_LABEL_REMOVE_ENDINGS:
            if text.endswith(ending) and len(text) > len(ending) + 1:
                text = normalize_spaces(text[: -len(ending)])
        if text == original:
            break
    return normalize_spaces(text.strip(" ,;:/"))


def _repaired_label_skip_reasons(
    source: str,
    label: str,
    concept_type: str,
    *,
    existing_label: str,
    confidence_value: float,
    min_confidence: float,
    max_length: int,
) -> list[str]:
    reasons: list[str] = []
    if confidence_value < min_confidence:
        reasons.append("confidence_too_low")
    if not source:
        reasons.append("missing_source_text")
    if not label:
        reasons.append("missing_repaired_label")
    if len(label) < 4:
        reasons.append("repaired_label_too_short")
    if len(label) > max_length:
        reasons.append("repaired_label_too_long")
    if _compact_label_key(label) == _compact_label_key(source):
        reasons.append("repaired_label_unchanged_from_source")
    if existing_label and _compact_label_key(label) == _compact_label_key(existing_label):
        reasons.append("repaired_label_same_as_existing_label")
    if any(label.endswith(ending) for ending in SAFE_RULE_BASED_DANGLING_ENDINGS):
        reasons.append("dangling_label_ending")
    flags = ksa_label_quality_flags(source, label, concept_type)
    unsupported_flags = sorted(set(flags) - REPAIRED_LABEL_ALLOWED_QUALITY_FLAGS)
    if unsupported_flags:
        reasons.extend(f"quality_flag:{flag}" for flag in unsupported_flags)
    return reasons


def _has_parenthetical_evidence(value: str) -> bool:
    return any(marker in value for marker in ("(", "[", "（"))


def _reason_counts(skipped: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for item in skipped:
        counts.update(str(reason) for reason in item.get("skip_reasons", []))
    return dict(sorted(counts.items()))


def evaluate_candidate(
    row: dict[str, Any],
    *,
    min_ratio: float,
    max_ratio: float,
    min_confidence: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    source = normalize_spaces(row.get("source_text") or "")
    label = normalize_spaces(row.get("label_text") or "")
    concept_type = row.get("concept_type") or "knowledge"
    source_ksa_id = row.get("source_ksa_id")
    source_atomic_id = row.get("source_atomic_id")
    source_scope_key = normalize_spaces(row.get("source_scope_key") or "")
    confidence_score = row.get("confidence_score")
    ratio = _label_ratio(source, label)
    flags = ksa_label_quality_flags(source, label, concept_type)
    reasons: list[str] = []
    if min_confidence is not None:
        try:
            confidence_value = float(confidence_score or 0.0)
        except (TypeError, ValueError):
            confidence_value = 0.0
        if confidence_value < min_confidence:
            reasons.append("confidence_too_low")
    if source_ksa_id is None and source_atomic_id is None:
        reasons.append("missing_source_provenance")
    if not source_scope_key:
        reasons.append("missing_source_scope_key")
    if not source:
        reasons.append("missing_source_text")
    if not label:
        reasons.append("missing_label_text")
    if len(label) < 4:
        reasons.append("label_too_short")
    if ratio < min_ratio:
        reasons.append("ratio_too_low")
    if ratio > max_ratio:
        reasons.append("ratio_too_high")
    if flags:
        reasons.extend(f"quality_flag:{flag}" for flag in flags)
    return not reasons, {
        "source_text": source,
        "label_text": label,
        "concept_type": concept_type,
        "source_ksa_id": source_ksa_id,
        "source_atomic_id": source_atomic_id,
        "source_scope_key": source_scope_key,
        "confidence_score": confidence_score,
        "ratio": ratio,
        "quality_flags": flags,
        "skip_reasons": reasons,
    }


def evaluate_safe_already_short_needs_review(row: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    source = normalize_spaces(row.get("source_text") or "")
    label = normalize_spaces(row.get("label_text") or "")
    concept_type = row.get("concept_type") or "knowledge"
    source_ksa_id = row.get("source_ksa_id")
    source_atomic_id = row.get("source_atomic_id")
    source_scope_key = normalize_spaces(row.get("source_scope_key") or "")
    source_method = row.get("source_method")
    review_status = row.get("review_status")
    ratio = _label_ratio(source, label)
    flags = ksa_label_quality_flags(source, label, concept_type)
    reasons: list[str] = []

    if review_status != "needs_review":
        reasons.append("not_needs_review")
    if source_method != "already_short_label":
        reasons.append("not_already_short_label")
    if source_ksa_id is None and source_atomic_id is None:
        reasons.append("missing_source_provenance")
    if not source_scope_key:
        reasons.append("missing_source_scope_key")
    if not source:
        reasons.append("missing_source_text")
    if not label:
        reasons.append("missing_label_text")
    if len(label) < 4:
        reasons.append("label_too_short")
    if source and label and source != label:
        reasons.append("source_label_mismatch")
    if flags:
        reasons.extend(f"quality_flag:{flag}" for flag in flags)

    return not reasons, {
        "source_text": source,
        "label_text": label,
        "concept_type": concept_type,
        "source_ksa_id": source_ksa_id,
        "source_atomic_id": source_atomic_id,
        "source_scope_key": source_scope_key,
        "source_method": source_method,
        "review_status": review_status,
        "confidence_score": row.get("confidence_score"),
        "ratio": ratio,
        "quality_flags": flags,
        "skip_reasons": reasons,
    }


def evaluate_safe_rule_based_needs_review(
    row: dict[str, Any],
    *,
    min_confidence: float,
    max_ratio: float,
) -> tuple[bool, dict[str, Any]]:
    source = normalize_spaces(row.get("source_text") or "")
    label = normalize_spaces(row.get("label_text") or "")
    concept_type = row.get("concept_type") or "knowledge"
    source_ksa_id = row.get("source_ksa_id")
    source_atomic_id = row.get("source_atomic_id")
    source_scope_key = normalize_spaces(row.get("source_scope_key") or "")
    source_method = row.get("source_method")
    review_status = row.get("review_status")
    confidence_score = row.get("confidence_score")
    try:
        confidence_value = float(confidence_score or 0.0)
    except (TypeError, ValueError):
        confidence_value = 0.0
    ratio = _label_ratio(source, label)
    stripped_source = _strip_parenthetical_text(source)
    flags = ksa_label_quality_flags(source, label, concept_type)
    reasons: list[str] = []

    if review_status != "needs_review":
        reasons.append("not_needs_review")
    if source_method != "rule_based_short_label_candidate":
        reasons.append("not_rule_based_short_label_candidate")
    if confidence_value < min_confidence:
        reasons.append("confidence_too_low")
    if source_ksa_id is None and source_atomic_id is None:
        reasons.append("missing_source_provenance")
    if not source_scope_key:
        reasons.append("missing_source_scope_key")
    if not source:
        reasons.append("missing_source_text")
    if not label:
        reasons.append("missing_label_text")
    if len(label) < 4:
        reasons.append("label_too_short")
    if ratio > max_ratio:
        reasons.append("ratio_too_high_for_parenthetical_short_label")
    if not _has_parenthetical_evidence(source):
        reasons.append("missing_parenthetical_evidence")
    if any(label.endswith(ending) for ending in SAFE_RULE_BASED_DANGLING_ENDINGS):
        reasons.append("dangling_label_ending")
    if _compact_label_key(label) not in _compact_label_key(stripped_source):
        reasons.append("label_not_in_parenthetical_stripped_source")
    if flags:
        reasons.extend(f"quality_flag:{flag}" for flag in flags)

    return not reasons, {
        "source_text": source,
        "label_text": label,
        "concept_type": concept_type,
        "source_ksa_id": source_ksa_id,
        "source_atomic_id": source_atomic_id,
        "source_scope_key": source_scope_key,
        "source_method": source_method,
        "review_status": review_status,
        "confidence_score": confidence_score,
        "ratio": ratio,
        "parenthetical_stripped_source": stripped_source,
        "quality_flags": flags,
        "skip_reasons": reasons,
    }


def evaluate_safe_source_faithful_needs_review(
    row: dict[str, Any],
    *,
    exact_min_confidence: float,
    suffix_min_confidence: float,
) -> tuple[bool, dict[str, Any]]:
    source = normalize_spaces(row.get("source_text") or "")
    label = normalize_spaces(row.get("label_text") or "")
    concept_type = row.get("concept_type") or "knowledge"
    source_ksa_id = row.get("source_ksa_id")
    source_atomic_id = row.get("source_atomic_id")
    source_scope_key = normalize_spaces(row.get("source_scope_key") or "")
    source_method = row.get("source_method")
    review_status = row.get("review_status")
    confidence_score = row.get("confidence_score")
    try:
        confidence_value = float(confidence_score or 0.0)
    except (TypeError, ValueError):
        confidence_value = 0.0
    ratio = _label_ratio(source, label)
    flags = ksa_label_quality_flags(source, label, concept_type)
    normalized_source_key = _compact_label_key(source)
    normalized_label_key = _compact_label_key(label)
    suffix_basis = _source_text_to_short_label_basis(source)
    normalized_suffix_basis_key = _compact_label_key(suffix_basis)
    exact_match = source_method == "already_short_label" and normalized_source_key == normalized_label_key
    suffix_match = (
        source_method == "rule_based_short_label_candidate"
        and normalized_suffix_basis_key == normalized_label_key
        and normalized_source_key != normalized_label_key
    )
    reasons: list[str] = []

    if review_status != "needs_review":
        reasons.append("not_needs_review")
    if not exact_match and not suffix_match:
        reasons.append("not_source_faithful_exact_or_suffix_match")
    if exact_match and confidence_value < exact_min_confidence:
        reasons.append("confidence_too_low_for_exact_source_label")
    if suffix_match and confidence_value < suffix_min_confidence:
        reasons.append("confidence_too_low_for_suffix_label")
    if source_ksa_id is None and source_atomic_id is None:
        reasons.append("missing_source_provenance")
    if not source_scope_key:
        reasons.append("missing_source_scope_key")
    if not source:
        reasons.append("missing_source_text")
    if not label:
        reasons.append("missing_label_text")
    if len(label) < 2:
        reasons.append("label_too_short")
    if suffix_match and len(label) < 4:
        reasons.append("suffix_label_too_short")
    if any(label.endswith(ending) for ending in SAFE_RULE_BASED_DANGLING_ENDINGS):
        reasons.append("dangling_label_ending")
    unsupported_flags = sorted(set(flags) - SOURCE_FAITHFUL_ALLOWED_QUALITY_FLAGS)
    if unsupported_flags:
        reasons.extend(f"quality_flag:{flag}" for flag in unsupported_flags)

    return not reasons, {
        "source_text": source,
        "label_text": label,
        "concept_type": concept_type,
        "source_ksa_id": source_ksa_id,
        "source_atomic_id": source_atomic_id,
        "source_scope_key": source_scope_key,
        "source_method": source_method,
        "review_status": review_status,
        "confidence_score": confidence_score,
        "ratio": ratio,
        "source_faithful_match_type": "exact" if exact_match else "suffix" if suffix_match else None,
        "source_faithful_suffix_basis": suffix_basis,
        "quality_flags": flags,
        "skip_reasons": reasons,
    }


def evaluate_repaired_label_candidate(
    row: dict[str, Any],
    *,
    min_confidence: float,
    max_length: int,
) -> tuple[bool, dict[str, Any]]:
    source = normalize_spaces(row.get("source_text") or "")
    existing_label = normalize_spaces(row.get("label_text") or "")
    concept_type = row.get("concept_type") or "knowledge"
    source_method = row.get("source_method")
    review_status = row.get("review_status")
    confidence_score = row.get("confidence_score")
    try:
        confidence_value = float(confidence_score or 0.0)
    except (TypeError, ValueError):
        confidence_value = 0.0
    repaired_label = _repair_source_faithful_label(source)
    reasons: list[str] = []
    if review_status != "needs_review":
        reasons.append("not_needs_review")
    if source_method != "rule_based_short_label_candidate":
        reasons.append("not_rule_based_short_label_candidate")
    if row.get("source_ksa_id") is None and row.get("source_atomic_id") is None:
        reasons.append("missing_source_provenance")
    if not normalize_spaces(row.get("source_scope_key") or ""):
        reasons.append("missing_source_scope_key")
    reasons.extend(
        _repaired_label_skip_reasons(
            source,
            repaired_label,
            concept_type,
            existing_label=existing_label,
            confidence_value=confidence_value,
            min_confidence=min_confidence,
            max_length=max_length,
        )
    )
    flags = ksa_label_quality_flags(source, repaired_label, concept_type) if repaired_label else []
    return not reasons, {
        "source_text": source,
        "existing_label_text": existing_label,
        "label_text": repaired_label,
        "normalized_label_key": _compact_label_key(repaired_label),
        "concept_type": concept_type,
        "source_ksa_id": row.get("source_ksa_id"),
        "source_atomic_id": row.get("source_atomic_id"),
        "source_scope_key": normalize_spaces(row.get("source_scope_key") or ""),
        "source_method": source_method,
        "review_status": review_status,
        "confidence_score": confidence_score,
        "quality_flags": flags,
        "skip_reasons": reasons,
    }


def build_manual_seedpack_rows(
    conn,
    *,
    limit: int,
    min_confidence: float,
    min_ratio: float,
    max_ratio: float,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
          lc.label_id,
          lc.concept_id,
          lc.label_text,
          lc.source_text,
          lc.confidence_score,
          lc.source_method,
          lc.review_status,
          lc.source_ksa_id,
          lc.source_atomic_id,
          oc.concept_type,
          oc.concept_name
        FROM ontology_concept_label_candidates lc
        JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
        WHERE lc.review_status = 'candidate'
        ORDER BY lc.confidence_score DESC, lc.label_id
        LIMIT ?
        """,
        (max(1, int(limit)),),
    ).fetchall()
    seedpack: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        _, evidence = evaluate_candidate(
            item,
            min_confidence=min_confidence,
            min_ratio=min_ratio,
            max_ratio=max_ratio,
        )
        item.update(evidence)
        item["allowed_decisions"] = ["approve", "needs_revision", "reject"]
        item["status_update_allowed"] = False
        item["trusted_status_write_allowed"] = False
        seedpack.append(item)
    return seedpack


def _manual_seedpack_from_skipped_rows(
    skipped: list[dict[str, Any]],
    *,
    limit: int,
    review_status: str,
) -> list[dict[str, Any]]:
    seedpack: list[dict[str, Any]] = []
    for row in skipped[: max(1, int(limit))]:
        item = dict(row)
        item["review_status"] = review_status
        item["triage_target_review_status"] = "needs_review"
        item["allowed_decisions"] = ["approve", "needs_revision", "reject"]
        item["status_update_allowed"] = False
        item["trusted_status_write_allowed"] = False
        seedpack.append(item)
    return seedpack


def run_codex_judge(
    *,
    db_path: Path,
    out: Path,
    manual_seedpack_out: Path,
    min_confidence: float,
    min_ratio: float,
    max_ratio: float,
    manual_limit: int,
    dry_run: bool,
    mark_skipped_needs_review: bool = False,
    promote_safe_already_short_needs_review: bool = False,
    promote_safe_rule_based_needs_review: bool = False,
    safe_rule_based_min_confidence: float = 0.80,
    safe_rule_based_max_ratio: float = 0.40,
    promote_safe_source_faithful_needs_review: bool = False,
    source_faithful_exact_min_confidence: float = 0.40,
    source_faithful_suffix_min_confidence: float = 0.68,
    create_repaired_label_candidates: bool = False,
    repaired_label_min_confidence: float = 0.68,
    repaired_label_max_length: int = 45,
) -> dict[str, Any]:
    conn = connect(db_path)
    try:
        source_method_placeholders = ",".join("?" for _ in DEFAULT_SOURCE_METHODS)
        confidence_filter_sql = "" if mark_skipped_needs_review else "AND lc.confidence_score >= ?"
        params: list[Any] = []
        if not mark_skipped_needs_review:
            params.append(min_confidence)
        params.extend(DEFAULT_SOURCE_METHODS)
        rows = conn.execute(
            f"""
            SELECT
              lc.label_id,
              lc.label_text,
              lc.confidence_score,
              lc.source_method,
              lc.source_text,
              lc.source_ksa_id,
              lc.source_atomic_id,
              lc.source_scope_key,
              lc.concept_id,
              oc.concept_type,
              oc.concept_name
            FROM ontology_concept_label_candidates lc
            JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
            WHERE lc.review_status = 'candidate'
              {confidence_filter_sql}
              AND lc.source_method IN ({source_method_placeholders})
            ORDER BY lc.confidence_score DESC, lc.label_id
            """,
            params,
        ).fetchall()
        approved: list[int] = []
        skipped: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            ok, evidence = evaluate_candidate(
                item,
                min_confidence=min_confidence,
                min_ratio=min_ratio,
                max_ratio=max_ratio,
            )
            if ok:
                approved.append(int(item["label_id"]))
            else:
                skipped.append({"label_id": int(item["label_id"]), **item, **evidence})
        skipped_reason_counts = _reason_counts(skipped)
        needs_review = [int(item["label_id"]) for item in skipped] if mark_skipped_needs_review else []
        safe_already_short_promoted: list[int] = []
        safe_already_short_skipped: list[dict[str, Any]] = []
        safe_already_short_rows = []
        if promote_safe_already_short_needs_review:
            safe_already_short_rows = conn.execute(
                """
                SELECT
                  lc.label_id,
                  lc.label_text,
                  lc.confidence_score,
                  lc.source_method,
                  lc.source_text,
                  lc.source_ksa_id,
                  lc.source_atomic_id,
                  lc.source_scope_key,
                  lc.review_status,
                  lc.concept_id,
                  oc.concept_type,
                  oc.concept_name
                FROM ontology_concept_label_candidates lc
                JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
                WHERE lc.review_status = 'needs_review'
                  AND lc.source_method = 'already_short_label'
                ORDER BY lc.label_id
                """
            ).fetchall()
            for row in safe_already_short_rows:
                item = dict(row)
                ok, evidence = evaluate_safe_already_short_needs_review(item)
                if ok:
                    safe_already_short_promoted.append(int(item["label_id"]))
                else:
                    safe_already_short_skipped.append(
                        {"label_id": int(item["label_id"]), **item, **evidence}
                    )
        safe_already_short_skipped_reason_counts = _reason_counts(safe_already_short_skipped)
        safe_rule_based_promoted: list[int] = []
        safe_rule_based_skipped: list[dict[str, Any]] = []
        safe_rule_based_rows = []
        if promote_safe_rule_based_needs_review:
            safe_rule_based_rows = conn.execute(
                """
                SELECT
                  lc.label_id,
                  lc.label_text,
                  lc.confidence_score,
                  lc.source_method,
                  lc.source_text,
                  lc.source_ksa_id,
                  lc.source_atomic_id,
                  lc.source_scope_key,
                  lc.review_status,
                  lc.concept_id,
                  oc.concept_type,
                  oc.concept_name
                FROM ontology_concept_label_candidates lc
                JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
                WHERE lc.review_status = 'needs_review'
                  AND lc.source_method = 'rule_based_short_label_candidate'
                ORDER BY lc.label_id
                """
            ).fetchall()
            for row in safe_rule_based_rows:
                item = dict(row)
                ok, evidence = evaluate_safe_rule_based_needs_review(
                    item,
                    min_confidence=safe_rule_based_min_confidence,
                    max_ratio=safe_rule_based_max_ratio,
                )
                if ok:
                    safe_rule_based_promoted.append(int(item["label_id"]))
                else:
                    safe_rule_based_skipped.append(
                        {"label_id": int(item["label_id"]), **item, **evidence}
                    )
        safe_rule_based_skipped_reason_counts = _reason_counts(safe_rule_based_skipped)
        safe_source_faithful_promoted: list[int] = []
        safe_source_faithful_skipped: list[dict[str, Any]] = []
        safe_source_faithful_rows = []
        if promote_safe_source_faithful_needs_review:
            safe_source_faithful_rows = conn.execute(
                """
                SELECT
                  lc.label_id,
                  lc.label_text,
                  lc.confidence_score,
                  lc.source_method,
                  lc.source_text,
                  lc.source_ksa_id,
                  lc.source_atomic_id,
                  lc.source_scope_key,
                  lc.review_status,
                  lc.concept_id,
                  oc.concept_type,
                  oc.concept_name
                FROM ontology_concept_label_candidates lc
                JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
                WHERE lc.review_status = 'needs_review'
                  AND lc.source_method IN ('already_short_label', 'rule_based_short_label_candidate')
                ORDER BY lc.label_id
                """
            ).fetchall()
            for row in safe_source_faithful_rows:
                item = dict(row)
                ok, evidence = evaluate_safe_source_faithful_needs_review(
                    item,
                    exact_min_confidence=source_faithful_exact_min_confidence,
                    suffix_min_confidence=source_faithful_suffix_min_confidence,
                )
                if ok:
                    safe_source_faithful_promoted.append(int(item["label_id"]))
                else:
                    safe_source_faithful_skipped.append(
                        {"label_id": int(item["label_id"]), **item, **evidence}
                    )
        safe_source_faithful_skipped_reason_counts = _reason_counts(safe_source_faithful_skipped)
        repaired_label_candidates: list[dict[str, Any]] = []
        repaired_label_skipped: list[dict[str, Any]] = []
        repaired_label_rows = []
        if create_repaired_label_candidates:
            repaired_label_rows = conn.execute(
                """
                SELECT
                  lc.label_id,
                  lc.concept_id,
                  lc.label_text,
                  lc.confidence_score,
                  lc.source_method,
                  lc.source_text,
                  lc.source_ksa_id,
                  lc.source_atomic_id,
                  lc.source_scope_key,
                  lc.review_status,
                  oc.concept_type,
                  oc.concept_name
                FROM ontology_concept_label_candidates lc
                JOIN ontology_concepts oc ON oc.concept_id = lc.concept_id
                WHERE lc.review_status = 'needs_review'
                  AND lc.source_method = 'rule_based_short_label_candidate'
                ORDER BY lc.label_id
                """
            ).fetchall()
            for row in repaired_label_rows:
                item = dict(row)
                ok, evidence = evaluate_repaired_label_candidate(
                    item,
                    min_confidence=repaired_label_min_confidence,
                    max_length=repaired_label_max_length,
                )
                if ok:
                    repaired_label_candidates.append({"label_id": int(item["label_id"]), **item, **evidence})
                else:
                    repaired_label_skipped.append({"label_id": int(item["label_id"]), **item, **evidence})
        repaired_label_skipped_reason_counts = _reason_counts(repaired_label_skipped)

        timestamp = now_utc()
        if approved and not dry_run:
            for batch in _chunks(approved):
                conn.execute(
                    f"""
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE label_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'candidate'
                    """,
                    (timestamp, *batch),
                )
        if safe_already_short_promoted and not dry_run:
            for batch in _chunks(safe_already_short_promoted):
                conn.execute(
                    f"""
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE label_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'needs_review'
                      AND source_method = 'already_short_label'
                    """,
                    (timestamp, *batch),
                )
        if safe_rule_based_promoted and not dry_run:
            for batch in _chunks(safe_rule_based_promoted):
                conn.execute(
                    f"""
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE label_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'needs_review'
                      AND source_method = 'rule_based_short_label_candidate'
                    """,
                    (timestamp, *batch),
                )
        if safe_source_faithful_promoted and not dry_run:
            for batch in _chunks(safe_source_faithful_promoted):
                conn.execute(
                    f"""
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'llm_reviewed',
                        updated_at = ?
                    WHERE label_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'needs_review'
                      AND source_method IN ('already_short_label', 'rule_based_short_label_candidate')
                    """,
                    (timestamp, *batch),
                )
        repaired_label_inserted = 0
        if repaired_label_candidates and not dry_run:
            for item in repaired_label_candidates:
                result = conn.execute(
                    f"""
                    INSERT INTO ontology_concept_label_candidates(
                        concept_id, source_ksa_id, source_atomic_id, source_scope_key,
                        concept_type, source_text, label_text, normalized_label_key,
                        label_role, source_method, candidate_rank, evidence_text,
                        confidence_score, review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'short_representative_label',
                              ?, 1, ?, ?, 'llm_reviewed', ?, ?)
                    ON CONFLICT(concept_id, source_scope_key, source_method, normalized_label_key)
                    DO UPDATE SET
                        source_ksa_id = excluded.source_ksa_id,
                        source_atomic_id = excluded.source_atomic_id,
                        concept_type = excluded.concept_type,
                        source_text = excluded.source_text,
                        label_text = excluded.label_text,
                        candidate_rank = excluded.candidate_rank,
                        evidence_text = excluded.evidence_text,
                        confidence_score = excluded.confidence_score,
                        review_status = 'llm_reviewed',
                        updated_at = excluded.updated_at
                    WHERE ontology_concept_label_candidates.review_status NOT IN ({HUMAN_TRUSTED_LABEL_REVIEW_STATUS_SQL})
                    """,
                    (
                        item["concept_id"],
                        item.get("source_ksa_id"),
                        item.get("source_atomic_id"),
                        item["source_scope_key"],
                        item["concept_type"],
                        item["source_text"],
                        item["label_text"],
                        item["normalized_label_key"],
                        REPAIRED_LABEL_SOURCE_METHOD,
                        (
                            f"repaired_from_label_id:{item['label_id']} | "
                            f"existing_label:{item.get('existing_label_text', '')} | "
                            f"source_text:{item['source_text']} | "
                            "method:source_parenthetical_suffix_dangling_repair"
                        ),
                        0.82,
                        timestamp,
                        timestamp,
                    ),
                )
                if result.rowcount:
                    repaired_label_inserted += 1
        if needs_review and not dry_run:
            for batch in _chunks(needs_review):
                conn.execute(
                    f"""
                    UPDATE ontology_concept_label_candidates
                    SET review_status = 'needs_review',
                        updated_at = ?
                    WHERE label_id IN ({",".join("?" for _ in batch)})
                      AND review_status = 'candidate'
                    """,
                    (timestamp, *batch),
                )
        if (
            approved
            or needs_review
            or safe_already_short_promoted
            or safe_rule_based_promoted
            or safe_source_faithful_promoted
            or repaired_label_inserted
        ) and not dry_run:
            conn.commit()

        if mark_skipped_needs_review:
            manual_rows = _manual_seedpack_from_skipped_rows(
                skipped,
                limit=manual_limit,
                review_status="candidate" if dry_run else "needs_review",
            )
        else:
            manual_rows = build_manual_seedpack_rows(
                conn,
                limit=manual_limit,
                min_confidence=min_confidence,
                min_ratio=min_ratio,
                max_ratio=max_ratio,
            )
        manual_seedpack_out.parent.mkdir(parents=True, exist_ok=True)
        manual_seedpack_out.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in manual_rows) + ("\n" if manual_rows else ""),
            encoding="utf-8",
        )
        statuses_written: list[str] = []
        if (
            approved
            or safe_already_short_promoted
            or safe_rule_based_promoted
            or safe_source_faithful_promoted
            or repaired_label_inserted
        ) and not dry_run:
            statuses_written.append("llm_reviewed")
        if needs_review and mark_skipped_needs_review and not dry_run:
            statuses_written.append("needs_review")
        forbidden_statuses_written = sorted(
            status for status in statuses_written if status in FORBIDDEN_STATUS_WRITES
        )
        report = {
            "ok": True,
            "schema": "ksa_label_codex_judge_v1",
            "generated_at": timestamp,
            "db_path": str(db_path),
            "dry_run": dry_run,
            "criteria": {
                "review_status": "candidate",
                "min_confidence": min_confidence,
                "confidence_filter_applied": not mark_skipped_needs_review,
                "source_methods": list(DEFAULT_SOURCE_METHODS),
                "min_ratio": min_ratio,
                "max_ratio": max_ratio,
                "min_label_length": 4,
                "source_provenance_required": True,
                "source_scope_key_required": True,
                "quality_flags_required_empty": True,
                "safe_already_short_needs_review": {
                    "enabled": promote_safe_already_short_needs_review,
                    "review_status": "needs_review",
                    "source_method": "already_short_label",
                    "source_equals_label_required": True,
                    "confidence_filter_applied": False,
                },
                "safe_rule_based_needs_review": {
                    "enabled": promote_safe_rule_based_needs_review,
                    "review_status": "needs_review",
                    "source_method": "rule_based_short_label_candidate",
                    "min_confidence": safe_rule_based_min_confidence,
                    "max_ratio": safe_rule_based_max_ratio,
                    "quality_flags_required_empty": True,
                    "parenthetical_evidence_required": True,
                    "label_in_parenthetical_stripped_source_required": True,
                    "dangling_label_endings_blocked": True,
                },
                "safe_source_faithful_needs_review": {
                    "enabled": promote_safe_source_faithful_needs_review,
                    "review_status": "needs_review",
                    "source_methods": ["already_short_label", "rule_based_short_label_candidate"],
                    "exact_min_confidence": source_faithful_exact_min_confidence,
                    "suffix_min_confidence": source_faithful_suffix_min_confidence,
                    "source_equals_label_or_suffix_basis_required": True,
                    "allowed_quality_flags": sorted(SOURCE_FAITHFUL_ALLOWED_QUALITY_FLAGS),
                    "dangling_label_endings_blocked": True,
                },
                "repaired_label_candidates": {
                    "enabled": create_repaired_label_candidates,
                    "review_status_written": "llm_reviewed",
                    "source_method": REPAIRED_LABEL_SOURCE_METHOD,
                    "base_source_method": "rule_based_short_label_candidate",
                    "min_confidence": repaired_label_min_confidence,
                    "max_length": repaired_label_max_length,
                    "human_reviewed_status_written": False,
                },
            },
            "candidate_rows_scanned": len(rows),
            "auto_approved": len(approved),
            "skipped": len(skipped),
            "skipped_by_reason": skipped_reason_counts,
            "mark_skipped_needs_review": mark_skipped_needs_review,
            "promote_safe_already_short_needs_review": promote_safe_already_short_needs_review,
            "safe_already_short_rows_scanned": len(safe_already_short_rows),
            "safe_already_short_promoted": len(safe_already_short_promoted),
            "safe_already_short_promoted_written": len(safe_already_short_promoted)
            if promote_safe_already_short_needs_review and not dry_run
            else 0,
            "safe_already_short_skipped": len(safe_already_short_skipped),
            "safe_already_short_skipped_by_reason": safe_already_short_skipped_reason_counts,
            "safe_rule_based_rows_scanned": len(safe_rule_based_rows),
            "safe_rule_based_promoted": len(safe_rule_based_promoted),
            "safe_rule_based_promoted_written": len(safe_rule_based_promoted)
            if promote_safe_rule_based_needs_review and not dry_run
            else 0,
            "safe_rule_based_skipped": len(safe_rule_based_skipped),
            "safe_rule_based_skipped_by_reason": safe_rule_based_skipped_reason_counts,
            "safe_source_faithful_rows_scanned": len(safe_source_faithful_rows),
            "safe_source_faithful_promoted": len(safe_source_faithful_promoted),
            "safe_source_faithful_promoted_written": len(safe_source_faithful_promoted)
            if promote_safe_source_faithful_needs_review and not dry_run
            else 0,
            "safe_source_faithful_skipped": len(safe_source_faithful_skipped),
            "safe_source_faithful_skipped_by_reason": safe_source_faithful_skipped_reason_counts,
            "repaired_label_rows_scanned": len(repaired_label_rows),
            "repaired_label_candidates": len(repaired_label_candidates),
            "repaired_label_candidates_written": repaired_label_inserted
            if create_repaired_label_candidates and not dry_run
            else 0,
            "repaired_label_skipped": len(repaired_label_skipped),
            "repaired_label_skipped_by_reason": repaired_label_skipped_reason_counts,
            "needs_review": len(needs_review),
            "needs_review_by_reason": skipped_reason_counts if mark_skipped_needs_review else {},
            "needs_review_written": len(needs_review) if mark_skipped_needs_review and not dry_run else 0,
            "manual_seedpack_rows": len(manual_rows),
            "manual_seedpack_path": str(manual_seedpack_out),
            "status_written": ",".join(statuses_written) if statuses_written else None,
            "statuses_written": statuses_written,
            "status_writes": {
                "llm_reviewed": len(approved)
                + len(safe_already_short_promoted)
                + len(safe_rule_based_promoted)
                + len(safe_source_faithful_promoted)
                + repaired_label_inserted
                if not dry_run
                else 0,
                "needs_review": len(needs_review) if mark_skipped_needs_review and not dry_run else 0,
            },
            "manual_seedpack_limit": max(1, int(manual_limit)),
            "manual_seedpack_total_eligible": len(skipped) if mark_skipped_needs_review else None,
            "manual_seedpack_truncated": bool(mark_skipped_needs_review and len(manual_rows) < len(skipped)),
            "manual_seedpack_omitted_rows": max(0, len(skipped) - len(manual_rows))
            if mark_skipped_needs_review
            else 0,
            "manual_seedpack_scope": "needs_review_top_sample"
            if mark_skipped_needs_review
            else "candidate_top_sample",
            "forbidden_statuses_written": forbidden_statuses_written,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return report
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Codex-as-judge auto review for safe KSA label candidates.")
    parser.add_argument("--db-path", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=ROOT / "reports" / f"ksa_label_codex_judge_{datetime.now().strftime('%Y%m%d')}.json")
    parser.add_argument("--manual-seedpack-out", type=Path, default=ROOT / "reports" / f"ksa_label_manual_review_{datetime.now().strftime('%Y%m%d')}.jsonl")
    parser.add_argument("--min-confidence", type=float, default=0.75)
    parser.add_argument("--min-ratio", type=float, default=0.35)
    parser.add_argument("--max-ratio", type=float, default=0.98)
    parser.add_argument("--manual-limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--mark-skipped-needs-review",
        action="store_true",
        help=(
            "Evaluate all candidate rows from allowed source methods regardless "
            "of confidence and mark skipped evaluated rows as needs_review."
        ),
    )
    parser.add_argument(
        "--promote-safe-already-short-needs-review",
        action="store_true",
        help=(
            "Promote needs_review rows produced by already_short_label only when "
            "source_text equals label_text, scoped provenance exists, length is "
            "at least 4, and label quality flags are empty."
        ),
    )
    parser.add_argument(
        "--promote-safe-rule-based-needs-review",
        action="store_true",
        help=(
            "Promote needs_review rule-based short labels only when the source "
            "contains removable parenthetical evidence, confidence is high, "
            "quality flags are empty, and the label remains in the source after "
            "removing parenthetical text."
        ),
    )
    parser.add_argument("--safe-rule-based-min-confidence", type=float, default=0.80)
    parser.add_argument("--safe-rule-based-max-ratio", type=float, default=0.40)
    parser.add_argument(
        "--promote-safe-source-faithful-needs-review",
        action="store_true",
        help=(
            "Promote needs_review labels when the label is exactly the source "
            "text, or equals the source after removing parenthetical evidence "
            "and generic KSA suffixes such as ability, skill, knowledge, or attitude."
        ),
    )
    parser.add_argument("--source-faithful-exact-min-confidence", type=float, default=0.40)
    parser.add_argument("--source-faithful-suffix-min-confidence", type=float, default=0.68)
    parser.add_argument(
        "--create-repaired-label-candidates",
        action="store_true",
        help=(
            "Create additional llm_reviewed label candidates for remaining "
            "needs_review rows when the source text can be repaired by removing "
            "parenthetical evidence, generic KSA suffixes, and dangling verb endings."
        ),
    )
    parser.add_argument("--repaired-label-min-confidence", type=float, default=0.68)
    parser.add_argument("--repaired-label-max-length", type=int, default=45)
    args = parser.parse_args()
    db_path = args.db_path or load_settings().db_path
    report = run_codex_judge(
        db_path=db_path,
        out=args.out,
        manual_seedpack_out=args.manual_seedpack_out,
        min_confidence=args.min_confidence,
        min_ratio=args.min_ratio,
        max_ratio=args.max_ratio,
        manual_limit=args.manual_limit,
        dry_run=args.dry_run,
        mark_skipped_needs_review=args.mark_skipped_needs_review,
        promote_safe_already_short_needs_review=args.promote_safe_already_short_needs_review,
        promote_safe_rule_based_needs_review=args.promote_safe_rule_based_needs_review,
        safe_rule_based_min_confidence=args.safe_rule_based_min_confidence,
        safe_rule_based_max_ratio=args.safe_rule_based_max_ratio,
        promote_safe_source_faithful_needs_review=args.promote_safe_source_faithful_needs_review,
        source_faithful_exact_min_confidence=args.source_faithful_exact_min_confidence,
        source_faithful_suffix_min_confidence=args.source_faithful_suffix_min_confidence,
        create_repaired_label_candidates=args.create_repaired_label_candidates,
        repaired_label_min_confidence=args.repaired_label_min_confidence,
        repaired_label_max_length=args.repaired_label_max_length,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
