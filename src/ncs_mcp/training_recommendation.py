from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from typing import Any

from ncs_mcp.career_path import career_paths_for_units
from ncs_mcp.compact_postings import (
    criteria_concept_ids,
    has_compact_criteria_postings,
    has_compact_ontology_postings,
    ontology_relation_rows,
    sqlite_object_exists,
)
from ncs_mcp.contracts import (
    AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
    AIHR_TRAINING_SYSTEM_GUIDE_TRACE_REQUIRED_CHECKS,
    AIHR_TRAINING_SYSTEM_GUIDE_WORKFLOW_REQUIRED_STAGES,
)
from ncs_mcp.constants import (
    DEFAULT_RECOMMENDATIONS,
    GENERIC_KSA_UNIT_THRESHOLD,
    MATCH_BASIS_WEIGHTS,
    MAX_RECOMMENDATIONS,
    MAX_SAME_SUB_CODE_IN_TOP_K,
    SCORE_WEIGHTS,
)
from ncs_mcp.db import (
    clamp_limit,
    normalize_concept_key,
    normalize_spaces,
    now_utc,
    resolve_task_criteria,
    row_to_dict,
    rows_to_dicts,
)
from ncs_mcp.helpers import DISCLAIMER, not_found_response
from ncs_mcp.hrd_guide_reference import (
    hrd_guide_reference_metadata,
    hrd_guide_trace_check_codes,
    hrd_guide_workflow_stage_codes,
    load_hrd_guide_reference_index,
)
from ncs_mcp.job_base_api import job_base_profile_for_units
from ncs_mcp.qualification_api import qualification_profile_for_units
from ncs_mcp.review_safety import REVIEW_PACKET_EXTENSIONS, is_portable_reports_packet_ref


TRUSTED_TRANSITION_REVIEW_STATUSES = ("human_reviewed", "reviewed", "accepted")
TRUSTED_CAREER_PATH_REVIEW_STATUSES = set(TRUSTED_TRANSITION_REVIEW_STATUSES)
REVIEW_AUDIT_PACKET_EXTENSIONS = REVIEW_PACKET_EXTENSIONS
DEFAULT_COURSE_LINK_LIMIT = 10
MAX_COURSE_LINK_LIMIT = 100
PUBLIC_TRAINING_COURSE_FIELDS = (
    "training_course_id",
    "ncs_cl_cd",
    "compe_unit_name",
    "compe_unit_level",
    "ncs_lclas_cd",
    "ncs_lclas_cdnm",
    "ncs_mclas_cd",
    "ncs_mclas_cdnm",
    "ncs_sclas_cd",
    "ncs_sclas_cdnm",
    "ncs_subd_cd",
    "ncs_subd_cdnm",
    "train_goal",
    "train_time",
    "fac_name",
    "meth_name",
)
SUMMARY_TRAINING_COURSE_FIELDS = (
    "training_course_id",
    "ncs_cl_cd",
    "compe_unit_name",
    "compe_unit_level",
    "train_goal",
    "train_time",
)
DEFINITION_TRUST_WEIGHT = {
    "human_reviewed": 1.0,
    "auto_promoted": 0.85,
    "llm_reviewed": 0.7,
    "candidate": 0.5,
    "missing": 0.3,
}
SHORT_KSA_PENALTY = 0.6
DUPLICATE_KSA_PENALTY = 0.6
BROAD_GENERIC_KSA_PENALTY = 0.8
BROAD_GENERIC_KSA_MIN_UNIT_COUNT = 50
BROAD_GENERIC_KSA_MIN_MAJOR_COUNT = 5
DISTANT_SCOPE_QUALITY_PENALTY_STACK = 0.85
DISTANT_SCOPE_RELATIONS_FOR_QUALITY_STACK = frozenset(
    {
        "different_classification",
        "same_major_classification",
        "course_scope_unknown",
    }
)
JOB_BASE_EVIDENCE_ROLE = "auxiliary_tie_breaker"
GENERIC_JOB_QUERY_SUFFIXES = ("\uc5c5\ubb34", "\uc9c1\ubb34", "\ubd84\uc57c")
GENERIC_JOB_QUERY_MIN_BASE_CHARS = 2
AUTOMATED_REVIEWER_IDS = {
    "",
    "automated_eval_gate",
    "automation",
    "mcp",
    "system",
}
TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K = 0.98
REJECTED_REVIEW_STATUSES = {"rejected"}
USABLE_REVIEW_STATUS_WEIGHTS = {
    "human_reviewed": 1.2,
    "reviewed": 1.1,
    "accepted": 1.1,
    "llm_reviewed": 1.05,
    "model_preprocessed": 0.95,
    "candidate": 0.88,
    "candidate_auto": 0.84,
    "auto_linked": 0.8,
    "raw": 0.75,
    "review_required": 0.72,
}
METHOD_GROUP_ALIASES = {
    "practice": {
        "practice",
        "hands-on",
        "handson",
        "field practice",
        "field",
        "lab",
        "실습",
        "현장실습",
        "집체훈련현장실습",
    },
    "classroom": {"classroom", "lecture", "offline", "집체훈련", "강의"},
    "remote": {"remote", "online", "distance", "원격훈련", "온라인"},
}


def _json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _clean(value: Any) -> str:
    return normalize_spaces("" if value is None else str(value))


def _with_korean_direction_particle(value: str) -> str:
    """Append the Korean directional particle `로` or `으로`."""

    normalized = _clean(value)
    for char in reversed(normalized):
        codepoint = ord(char)
        if 0xAC00 <= codepoint <= 0xD7A3:
            final_consonant = (codepoint - 0xAC00) % 28
            particle = "으로" if final_consonant not in {0, 8} else "로"
            return f"{normalized}{particle}"
    return f"{normalized}로"


def _generic_job_query_normalization(query: str | None) -> tuple[str | None, dict[str, Any] | None]:
    text = _clean(query)
    if not text:
        return query, None
    variants = [(text, False)]
    compact = re.sub(r"\s+", "", text)
    if compact and compact != text:
        variants.append((compact, True))
    for candidate, spacing_normalized in variants:
        for suffix in GENERIC_JOB_QUERY_SUFFIXES:
            if not candidate.endswith(suffix):
                continue
            base = candidate[: -len(suffix)].strip()
            if len(normalize_concept_key(base)) < GENERIC_JOB_QUERY_MIN_BASE_CHARS:
                continue
            return base, {
                "method": "generic_suffix_strip",
                "source_query": text,
                "effective_query": base,
                "stripped_suffix": suffix,
                "spacing_normalized": spacing_normalized,
            }
    return text, None


def _trusted_career_paths(paths: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in paths
        if _clean(item.get("review_status")) in TRUSTED_CAREER_PATH_REVIEW_STATUSES
    ]


def _review_status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(_clean(item.get("review_status")) or "unknown" for item in items)
    return dict(sorted(counts.items()))


def _row_dict(row: sqlite3.Row | dict[str, Any] | None) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return dict(row)
    return dict(row)


def _contains(haystack: Any, needle: Any) -> bool:
    text = _clean(haystack).lower()
    target = _clean(needle).lower()
    return bool(target) and target in text


def _parse_number(value: Any) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", _clean(value).replace(",", ""))
    return float(match.group(0)) if match else None


def _split_list_value(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return [item.strip() for item in re.split(r"[,;/]+", text) if item.strip()]


def _normalize_text_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items: list[str] = []
        for item in value:
            items.extend(_normalize_text_list(item))
        return list(dict.fromkeys(items))
    return list(dict.fromkeys(_split_list_value(value)))


def _split_training_methods(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    methods = []
    for token in ("원격훈련", "집체훈련", "현장견학", "현장실습", "Practice", "Classroom"):
        if token.lower() in text.lower():
            methods.append(token)
    return methods or [text]


def _split_training_methods(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    known_tokens = ("?먭꺽?덈젴", "吏묒껜?덈젴", "?꾩옣寃ы븰", "?꾩옣?ㅼ뒿", "Practice", "Classroom")
    methods: list[str] = []
    parts = _split_list_value(text) or [text]
    for part in parts:
        matches = [token for token in known_tokens if token.lower() in part.lower()]
        methods.extend(matches or [part])
    return list(dict.fromkeys(methods))


def _significant_tokens(value: Any) -> list[str]:
    text = _clean(value)
    raw_tokens = re.findall(r"[0-9A-Za-z가-힣]+", text)
    generic = {
        "관리",
        "계획",
        "기술",
        "능력",
        "방법",
        "분석",
        "수립",
        "업무",
        "학습",
        "역량",
        "practice",
        "training",
    }
    korean_suffixes = (
        "으로",
        "에서",
        "부터",
        "까지",
        "에게",
        "와",
        "과",
        "을",
        "를",
        "은",
        "는",
        "이",
        "가",
        "의",
        "에",
        "로",
        "도",
        "만",
    )
    tokens: list[str] = []
    seen: set[str] = set()
    for raw_token in raw_tokens:
        token = raw_token.lower()
        variants = [token]
        if re.search(r"[가-힣]", token):
            for suffix in korean_suffixes:
                if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                    variants.append(token[: -len(suffix)])
                    break
        for variant in variants:
            if len(variant) < 2 or variant in generic or variant in seen:
                continue
            seen.add(variant)
            tokens.append(variant)
    return tokens


def _confidence_grade(score: float) -> str:
    if score >= 0.75:
        return "high"
    if score >= 0.5:
        return "medium"
    if score > 0:
        return "low"
    return "insufficient"


def _link_is_usable(row: sqlite3.Row | dict[str, Any]) -> bool:
    status = _clean(_row_dict(row).get("review_status"))
    return status not in REJECTED_REVIEW_STATUSES


def _review_weight(row: sqlite3.Row | dict[str, Any]) -> float:
    if not _link_is_usable(row):
        return 0.0
    return USABLE_REVIEW_STATUS_WEIGHTS.get(_clean(_row_dict(row).get("review_status")), 0.8)


def _definition_trust_status(concept: dict[str, Any]) -> str:
    review_status = _clean(concept.get("review_status"))
    if review_status in DEFINITION_TRUST_WEIGHT:
        return review_status
    definition_status = _clean(concept.get("definition_status"))
    if definition_status in DEFINITION_TRUST_WEIGHT:
        return definition_status
    return "missing"


def _definition_trust_profile(concepts: list[dict[str, Any]]) -> dict[str, Any]:
    weighted_concepts = [concept for concept in concepts if int(concept.get("concept_id") or 0)]
    if not weighted_concepts:
        return {
            "weight": 1.0,
            "status_counts": {},
            "applied": False,
        }
    statuses = [_definition_trust_status(concept) for concept in weighted_concepts]
    status_counts = Counter(statuses)
    weight = sum(
        DEFINITION_TRUST_WEIGHT.get(status, DEFINITION_TRUST_WEIGHT["missing"])
        for status in statuses
    ) / len(statuses)
    return {
        "weight": round(weight, 4),
        "status_counts": dict(sorted(status_counts.items())),
        "applied": True,
    }


def _concept_quality_issue_penalty_map(
    conn: sqlite3.Connection,
    concept_ids: set[int],
) -> dict[int, list[dict[str, Any]]]:
    concept_ids = {int(concept_id) for concept_id in concept_ids if int(concept_id or 0)}
    if not concept_ids:
        return {}
    penalty_by_concept: dict[int, list[dict[str, Any]]] = {}

    def add_penalty(concept_id: int, issue_type: str, multiplier: float) -> None:
        if multiplier >= 1.0:
            return
        bucket = penalty_by_concept.setdefault(concept_id, [])
        for row in bucket:
            if row["issue_type"] == issue_type:
                row["penalty_multiplier"] = min(float(row["penalty_multiplier"]), multiplier)
                return
        bucket.append(
            {
                "concept_id": concept_id,
                "issue_type": issue_type,
                "penalty_multiplier": multiplier,
            }
        )

    def chunks(values: list[Any], size: int = 900) -> list[list[Any]]:
        return [values[index : index + size] for index in range(0, len(values), size)]

    duplicate_noncanonical_ids: set[int] = set()
    sorted_concept_ids = sorted(concept_ids)
    for batch in chunks(sorted_concept_ids):
        if has_compact_ontology_postings(conn):
            rows = ontology_relation_rows(conn, source_ids=batch)
            duplicate_noncanonical_ids.update(
                int(row["source_concept_id"])
                for row in rows
                if row["relation_type"] == "same_as"
            )
        elif sqlite_object_exists(conn, "ontology_concept_relations"):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT DISTINCT source_concept_id AS concept_id
                FROM ontology_concept_relations
                WHERE source_concept_id IN ({placeholders})
                  AND relation_type = 'same_as'
                  AND review_status != 'rejected'
                """,
                batch,
            ).fetchall()
            duplicate_noncanonical_ids.update(
                int(row["concept_id"]) for row in rows
            )
    for concept_id in duplicate_noncanonical_ids:
        add_penalty(concept_id, "duplicate_text", DUPLICATE_KSA_PENALTY)

    ksa_to_concept_ids: dict[int, set[int]] = {}
    for batch in chunks(sorted_concept_ids):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            f"""
            SELECT concept_id, ksa_id
            FROM ksa_concept_links
            WHERE concept_id IN ({placeholders})
            """,
            batch,
        ).fetchall():
            ksa_to_concept_ids.setdefault(int(row["ksa_id"]), set()).add(int(row["concept_id"]))
        for row in conn.execute(
            f"""
            SELECT kacl.concept_id, kai.ksa_id
            FROM ksa_atomic_concept_links kacl
            JOIN ksa_atomic_items kai ON kai.atomic_id = kacl.atomic_id
            WHERE kacl.concept_id IN ({placeholders})
            """,
            batch,
            ).fetchall():
            ksa_to_concept_ids.setdefault(int(row["ksa_id"]), set()).add(int(row["concept_id"]))

    duplicate_issue_ksa_to_concept_ids: dict[int, set[int]] = {}
    sorted_ksa_ids = sorted(ksa_to_concept_ids)
    has_quality_issues = sqlite_object_exists(conn, "quality_issues")
    if has_quality_issues:
        for batch in chunks([str(ksa_id) for ksa_id in sorted_ksa_ids]):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT target_id, issue_type
                FROM quality_issues
                WHERE target_type = 'ksa'
                  AND target_id IN ({placeholders})
                  AND issue_type IN ('short_ksa', 'duplicate_text')
                  AND resolved_at IS NULL
                """,
                batch,
            ).fetchall()
            for row in rows:
                issue_type = row["issue_type"]
                concept_ids_for_ksa = ksa_to_concept_ids.get(
                    int(row["target_id"]), set()
                )
                for concept_id in concept_ids_for_ksa:
                    if issue_type == "short_ksa":
                        add_penalty(concept_id, issue_type, SHORT_KSA_PENALTY)
                    elif issue_type == "duplicate_text":
                        duplicate_issue_ksa_to_concept_ids.setdefault(
                            int(row["target_id"]), set()
                        ).add(concept_id)
                        if concept_id in duplicate_noncanonical_ids:
                            add_penalty(
                                concept_id,
                                issue_type,
                                DUPLICATE_KSA_PENALTY,
                            )

    if duplicate_issue_ksa_to_concept_ids:
        concept_scope: dict[int, dict[str, set[str]]] = {}
        for batch in chunks(sorted(duplicate_issue_ksa_to_concept_ids)):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT
                    ki.ksa_id,
                    cu.unit_code,
                    c.major_code
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
                WHERE ki.ksa_id IN ({placeholders})
                """,
                batch,
            ).fetchall()
            for row in rows:
                ksa_id = int(row["ksa_id"])
                for concept_id in duplicate_issue_ksa_to_concept_ids.get(ksa_id, set()):
                    bucket = concept_scope.setdefault(
                        concept_id,
                        {"unit_codes": set(), "major_codes": set()},
                    )
                    if row["unit_code"]:
                        bucket["unit_codes"].add(str(row["unit_code"]))
                    if row["major_code"]:
                        bucket["major_codes"].add(str(row["major_code"]))
        for concept_id, scope in concept_scope.items():
            unit_count = len(scope["unit_codes"])
            major_count = len(scope["major_codes"])
            if (
                unit_count >= BROAD_GENERIC_KSA_MIN_UNIT_COUNT
                or major_count >= BROAD_GENERIC_KSA_MIN_MAJOR_COUNT
            ):
                add_penalty(concept_id, "broad_generic_ksa", BROAD_GENERIC_KSA_PENALTY)

    if has_quality_issues:
        for batch in chunks([str(concept_id) for concept_id in sorted_concept_ids]):
            placeholders = ",".join("?" for _ in batch)
            rows = conn.execute(
                f"""
                SELECT target_id, issue_type
                FROM quality_issues
                WHERE target_type IN ('concept', 'ontology_concept')
                  AND target_id IN ({placeholders})
                  AND issue_type IN ('short_ksa', 'duplicate_text')
                  AND resolved_at IS NULL
                """,
                batch,
            ).fetchall()
            for row in rows:
                concept_id = int(row["target_id"])
                issue_type = row["issue_type"]
                if issue_type == "short_ksa":
                    add_penalty(concept_id, issue_type, SHORT_KSA_PENALTY)
                elif concept_id in duplicate_noncanonical_ids:
                    add_penalty(concept_id, issue_type, DUPLICATE_KSA_PENALTY)
    return penalty_by_concept


def _concept_quality_issue_penalty_profile_from_map(
    concept_ids: set[int],
    penalty_by_concept: dict[int, list[dict[str, Any]]],
) -> dict[str, Any]:
    concept_ids = {int(concept_id) for concept_id in concept_ids if int(concept_id or 0)}
    penalty_rows = [
        row
        for concept_id in concept_ids
        for row in penalty_by_concept.get(concept_id, [])
    ]
    if not penalty_rows:
        return {
            "multiplier": 1.0,
            "applied": False,
            "issue_types": [],
            "concept_ids": [],
            "concept_issue_types": {},
        }
    multiplier = min(row["penalty_multiplier"] for row in penalty_rows)
    concept_issue_types: dict[str, set[str]] = {}
    for row in penalty_rows:
        concept_issue_types.setdefault(str(int(row["concept_id"])), set()).add(str(row["issue_type"]))
    return {
        "multiplier": round(multiplier, 4),
        "applied": True,
        "issue_types": sorted({row["issue_type"] for row in penalty_rows}),
        "concept_ids": sorted({row["concept_id"] for row in penalty_rows}),
        "concept_issue_types": {
            concept_id: sorted(issue_types)
            for concept_id, issue_types in sorted(
                concept_issue_types.items(),
                key=lambda item: int(item[0]),
            )
        },
    }


def _concept_quality_issue_penalty_profile(
    conn: sqlite3.Connection,
    concept_ids: set[int],
) -> dict[str, Any]:
    return _concept_quality_issue_penalty_profile_from_map(
        concept_ids,
        _concept_quality_issue_penalty_map(conn, concept_ids),
    )


def _concept_key(item: dict[str, Any]) -> int:
    return int(item.get("concept_id") or 0)


def _dedupe_dicts(items: list[dict[str, Any]], key: str, *, limit: int | None = None) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        value = item.get(key)
        if value in seen:
            continue
        seen.add(value)
        result.append(item)
        if limit is not None and len(result) >= limit:
            break
    return result


def _concepts_for_units(
    conn: sqlite3.Connection,
    unit_codes: set[str],
    *,
    criteria_id: int | None = None,
    limit: int = 300,
) -> list[dict[str, Any]]:
    if not unit_codes and criteria_id is None:
        return []
    params: list[Any] = []
    unit_clause = ""
    if unit_codes:
        placeholders = ",".join("?" for _ in unit_codes)
        unit_clause = f"ce.unit_code IN ({placeholders})"
        params.extend(sorted(unit_codes))
    criteria_clause = ""
    if criteria_id is not None:
        criteria_clause = "pc.criteria_id = ?"
        params.append(criteria_id)
    where = " OR ".join(f"({clause})" for clause in [unit_clause, criteria_clause] if clause)
    rows = conn.execute(
        f"""
        SELECT DISTINCT
            oc.concept_id, oc.concept_name, oc.concept_type,
            oc.definition, oc.definition_source, oc.definition_status, oc.review_status,
            COUNT(DISTINCT ce.unit_code) AS scope_unit_count,
            COUNT(DISTINCT ce.element_id) AS scope_element_count,
            COUNT(DISTINCT pc.criteria_id) AS scope_criteria_count
        FROM competency_elements ce
        LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        JOIN ksa_concept_links kcl ON kcl.ksa_id = ki.ksa_id
        JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
        WHERE {where}
        GROUP BY
            oc.concept_id, oc.concept_name, oc.concept_type,
            oc.definition, oc.definition_source, oc.definition_status, oc.review_status
        ORDER BY scope_criteria_count DESC, oc.concept_type, oc.concept_name
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    concepts = rows_to_dicts(rows)
    if len(concepts) < limit:
        rows = conn.execute(
            f"""
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status,
                COUNT(DISTINCT ce.unit_code) AS scope_unit_count,
                COUNT(DISTINCT ce.element_id) AS scope_element_count,
                COUNT(DISTINCT pc.criteria_id) AS scope_criteria_count
            FROM competency_elements ce
            LEFT JOIN performance_criteria pc ON pc.element_id = ce.element_id
            JOIN ksa_atomic_items kai ON kai.element_id = ce.element_id
            JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
            JOIN ontology_concepts oc ON oc.concept_id = kacl.concept_id
            WHERE {where}
            GROUP BY
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status
            ORDER BY scope_criteria_count DESC, oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
        concepts = _dedupe_dicts(concepts + rows_to_dicts(rows), "concept_id", limit=limit)
    return concepts


def _concepts_for_criteria(conn: sqlite3.Connection, criteria_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    if has_compact_criteria_postings(conn):
        linked_ids = criteria_concept_ids(conn, [criteria_id]).get(criteria_id, [])
        if not linked_ids:
            return []
        placeholders = ",".join("?" for _ in linked_ids)
        rows = conn.execute(
            f"""
            SELECT
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status,
                oc.review_status,
                1 AS scope_unit_count,
                1 AS scope_element_count,
                1 AS scope_criteria_count
            FROM ontology_concepts AS oc
            WHERE oc.concept_id IN ({placeholders})
            ORDER BY oc.concept_type, oc.concept_name, oc.concept_id
            LIMIT ?
            """,
            (*linked_ids, limit),
        ).fetchall()
        return rows_to_dicts(rows)
    rows = conn.execute(
        """
        SELECT DISTINCT
            oc.concept_id, oc.concept_name, oc.concept_type,
            oc.definition, oc.definition_source, oc.definition_status, oc.review_status,
            COUNT(DISTINCT ce.unit_code) AS scope_unit_count,
            COUNT(DISTINCT ce.element_id) AS scope_element_count,
            COUNT(DISTINCT pc.criteria_id) AS scope_criteria_count
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        JOIN criteria_concept_links ccl ON ccl.criteria_id = pc.criteria_id
        JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
        WHERE pc.criteria_id = ?
        GROUP BY
            oc.concept_id, oc.concept_name, oc.concept_type,
            oc.definition, oc.definition_source, oc.definition_status, oc.review_status
        ORDER BY scope_criteria_count DESC, oc.concept_type, oc.concept_name
        LIMIT ?
        """,
        (criteria_id, limit),
    ).fetchall()
    concepts = rows_to_dicts(rows)
    if len(concepts) < limit:
        rows = conn.execute(
            """
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status,
                COUNT(DISTINCT ce.unit_code) AS scope_unit_count,
                COUNT(DISTINCT ce.element_id) AS scope_element_count,
                COUNT(DISTINCT pc.criteria_id) AS scope_criteria_count
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN element_criteria_ksa_links eckl ON eckl.criteria_id = pc.criteria_id
            JOIN ksa_concept_links kcl ON kcl.ksa_id = eckl.ksa_id
            JOIN ontology_concepts oc ON oc.concept_id = kcl.concept_id
            WHERE pc.criteria_id = ?
            GROUP BY
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status
            ORDER BY scope_criteria_count DESC, oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (criteria_id, limit),
        ).fetchall()
        concepts = _dedupe_dicts(concepts + rows_to_dicts(rows), "concept_id", limit=limit)
    if len(concepts) < limit:
        rows = conn.execute(
            """
            SELECT DISTINCT
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status,
                COUNT(DISTINCT ce.unit_code) AS scope_unit_count,
                COUNT(DISTINCT ce.element_id) AS scope_element_count,
                COUNT(DISTINCT pc.criteria_id) AS scope_criteria_count
            FROM performance_criteria pc
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN element_criteria_ksa_links eckl ON eckl.criteria_id = pc.criteria_id
            JOIN ksa_atomic_items kai ON kai.ksa_id = eckl.ksa_id
            JOIN ksa_atomic_concept_links kacl ON kacl.atomic_id = kai.atomic_id
            JOIN ontology_concepts oc ON oc.concept_id = kacl.concept_id
            WHERE pc.criteria_id = ?
            GROUP BY
                oc.concept_id, oc.concept_name, oc.concept_type,
                oc.definition, oc.definition_source, oc.definition_status, oc.review_status
            ORDER BY scope_criteria_count DESC, oc.concept_type, oc.concept_name
            LIMIT ?
            """,
            (criteria_id, limit),
        ).fetchall()
        concepts = _dedupe_dicts(concepts + rows_to_dicts(rows), "concept_id", limit=limit)
    return concepts


def _task_concepts(conn: sqlite3.Connection, criteria_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    return _concepts_for_criteria(conn, criteria_id, limit=limit)


def _scope_concepts(conn: sqlite3.Connection, unit_codes: set[str], *, limit: int = 300) -> list[dict[str, Any]]:
    return _concepts_for_units(conn, unit_codes, limit=limit)


def _filter_concepts(profile: list[dict[str, Any]], concept_ids: set[int], *, limit: int) -> list[dict[str, Any]]:
    return [item for item in profile if int(item.get("concept_id") or 0) in concept_ids][:limit]


def _concept_names(items: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = _clean(item.get("concept_name"))
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _role_overlay_profile(
    *,
    requested_query: str | None,
    effective_query: str | None,
    alias: dict[str, Any] | None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    text_parts = [
        requested_query,
        effective_query,
        alias.get("alias_text") if isinstance(alias, dict) else None,
        alias.get("normalized_query") if isinstance(alias, dict) else None,
        scope.get("match_text") if isinstance(scope, dict) else None,
    ]
    text = " ".join(_clean(part).lower() for part in text_parts if _clean(part))
    team_lead_terms = (
        "팀장",
        "부서장",
        "실장",
        "총괄",
        "manager",
        "lead",
        "head",
    )
    hr_terms = (
        "인사",
        "hr",
        "human resources",
        "people",
        "personnel",
    )
    if not any(term in text for term in team_lead_terms):
        return None
    if not any(term in text for term in hr_terms) and not (
        isinstance(scope, dict)
        and scope.get("major_code") == "02"
        and scope.get("middle_code") == "02"
        and scope.get("small_code") == "02"
    ):
        return None
    return {
        "code": "hr_team_lead",
        "label": "인사 직무군 팀장 역할",
        "scope_strategy": "ncs_hr_subclassification_plus_team_lead_overlay",
        "source": "query_alias_or_role_term",
    }


def _same_scope(scope: dict[str, Any], other: dict[str, Any], fields: tuple[str, ...]) -> bool:
    return all(_clean(scope.get(field)) and _clean(scope.get(field)) == _clean(other.get(field)) for field in fields)


def _classification_scope_fit(current_scope: dict[str, Any], target_scope: dict[str, Any]) -> dict[str, Any]:
    levels = [
        (("major_code", "middle_code", "small_code", "sub_code"), "same_sub_classification", 0.24),
        (("major_code", "middle_code", "small_code"), "same_small_classification", 0.18),
        (("major_code", "middle_code"), "same_middle_classification", 0.1),
        (("major_code",), "same_major_classification", 0.04),
    ]
    for fields, relation, score in levels:
        if _same_scope(current_scope, target_scope, fields):
            return {"relation": relation, "score": score, "fields": list(fields)}
    return {"relation": "different_classification", "score": 0.0, "fields": []}


COURSE_SCOPE_NEAR_RELATIONS = {"direct_scope_unit", "same_sub_classification", "same_small_classification"}

COURSE_SCOPE_RELATION_RANK = {
    "direct_scope_unit": 0,
    "same_sub_classification": 1,
    "same_small_classification": 2,
    "same_middle_classification": 3,
    "same_major_classification": 4,
}

DIRECT_UNIT_DIVERSITY_BYPASS_SCORE = 0.45


COURSE_SCOPE_RELATION_LABELS = {
    "direct_scope_unit": "Direct NCS unit link",
    "same_sub_classification": "Same NCS sub classification",
    "same_small_classification": "Same NCS small classification",
    "same_middle_classification": "Same NCS middle classification",
    "same_major_classification": "Same NCS major classification",
    "different_classification": "Different NCS classification",
    "course_scope_unknown": "Course scope unknown",
    "target_scope_unknown": "Target scope unknown",
    "unknown": "Scope fit unknown",
}


def _scope_alignment_from_relation(relation: str) -> str:
    if relation == "direct_scope_unit":
        return "direct"
    if relation in {"same_sub_classification", "same_small_classification"}:
        return "near"
    if relation in {"same_middle_classification", "same_major_classification"}:
        return "adjacent"
    if relation == "different_classification":
        return "distant"
    return "unknown"


def _public_course_scope_fit(scope_fit: Any) -> dict[str, Any]:
    fit = scope_fit if isinstance(scope_fit, dict) else {}
    relation = _clean(fit.get("relation")) or "unknown"
    alignment = _scope_alignment_from_relation(relation)
    fields = [str(field) for field in fit.get("fields") or [] if str(field).strip()]
    target_scope = fit.get("target_scope") if isinstance(fit.get("target_scope"), dict) else {}
    course_scope = fit.get("course_scope") if isinstance(fit.get("course_scope"), dict) else {}
    direct_unit_codes = [str(code) for code in fit.get("direct_unit_codes") or [] if str(code).strip()]
    return {
        "relation": relation,
        "label": COURSE_SCOPE_RELATION_LABELS.get(relation, relation),
        "alignment": alignment,
        "fields": fields,
        "target_scope": {
            key: _clean(target_scope.get(key))
            for key in ("major_code", "middle_code", "small_code", "sub_code")
            if _clean(target_scope.get(key))
        },
        "course_scope": {
            key: _clean(course_scope.get(key))
            for key in ("major_code", "middle_code", "small_code", "sub_code")
            if _clean(course_scope.get(key))
        },
        "direct_unit_codes": direct_unit_codes[:10],
        "is_direct_or_near_scope": relation in COURSE_SCOPE_NEAR_RELATIONS,
        "requires_scope_review": alignment in {"adjacent", "distant", "unknown"},
    }


def _course_scope_fit(
    row: sqlite3.Row | dict[str, Any],
    target_scope: dict[str, Any],
    *,
    linked_unit_codes: set[str],
    scope_unit_codes: set[str],
) -> dict[str, Any]:
    rowd = _row_dict(row)
    course_scope = {
        "major_code": _clean(rowd.get("ncs_lclas_cd")),
        "middle_code": _clean(rowd.get("ncs_mclas_cd")),
        "small_code": _clean(rowd.get("ncs_sclas_cd")),
        "sub_code": _clean(rowd.get("ncs_subd_cd")),
    }
    target = {
        "major_code": _clean(target_scope.get("major_code")),
        "middle_code": _clean(target_scope.get("middle_code")),
        "small_code": _clean(target_scope.get("small_code")),
        "sub_code": _clean(target_scope.get("sub_code")),
    }
    direct_units = sorted(scope_unit_codes & linked_unit_codes)
    if direct_units:
        return {
            "relation": "direct_scope_unit",
            "fields": ["unit_code"],
            "target_scope": target,
            "course_scope": course_scope,
            "direct_unit_codes": direct_units,
        }
    for fields, relation in [
        (("major_code", "middle_code", "small_code", "sub_code"), "same_sub_classification"),
        (("major_code", "middle_code", "small_code"), "same_small_classification"),
        (("major_code", "middle_code"), "same_middle_classification"),
        (("major_code",), "same_major_classification"),
    ]:
        if all(target.get(field) and course_scope.get(field) and target[field] == course_scope[field] for field in fields):
            return {
                "relation": relation,
                "fields": list(fields),
                "target_scope": target,
                "course_scope": course_scope,
                "direct_unit_codes": [],
            }
    relation = "different_classification" if any(course_scope.values()) else "course_scope_unknown"
    if not any(target.values()):
        relation = "target_scope_unknown"
    return {
        "relation": relation,
        "fields": [],
        "target_scope": target,
        "course_scope": course_scope,
        "direct_unit_codes": [],
    }


def _is_distant_scope_concept_only_candidate(match: dict[str, Any]) -> bool:
    if match.get("direct_unit_evidence") or match.get("source_element_covered") or match.get("support_course_hint_weight"):
        return False
    if int(match.get("goal_concept_hits") or 0) <= 0:
        return False
    relation = _clean((match.get("course_scope_fit") or {}).get("relation"))
    return relation not in COURSE_SCOPE_NEAR_RELATIONS


def _course_candidate_sort_key(item: dict[str, Any]) -> tuple[int, int, float, str]:
    match = item.get("match") or {}
    relation = _clean((match.get("course_scope_fit") or {}).get("relation"))
    row = _row_dict(item.get("row"))
    return (
        1 if _is_distant_scope_concept_only_candidate(match) else 0,
        COURSE_SCOPE_RELATION_RANK.get(relation, 9),
        -float(item["score"]),
        _clean(row.get("compe_unit_name")),
    )


def _ontology_related_transition_profile(
    conn: sqlite3.Connection,
    *,
    current_ids: set[int],
    target_ids: set[int],
    exact_ids: set[int],
    limit: int = 1000,
) -> dict[str, Any]:
    if not current_ids or not target_ids:
        return {"related_target_ids": [], "related_target_count": 0, "evidence": []}
    if has_compact_ontology_postings(conn):
        candidate_ids = current_ids | target_ids
        compact_rows = ontology_relation_rows(
            conn,
            source_ids=candidate_ids,
            target_ids=candidate_ids,
        )
        compact_rows = [
            row
            for row in compact_rows
            if (
                row["source_concept_id"] in current_ids
                and row["target_concept_id"] in target_ids
            )
            or (
                row["source_concept_id"] in target_ids
                and row["target_concept_id"] in current_ids
            )
        ][:limit]
        placeholders = ",".join("?" for _ in candidate_ids)
        names = {
            int(row["concept_id"]): row["concept_name"]
            for row in conn.execute(
                f"""
                SELECT concept_id, concept_name
                FROM ontology_concepts
                WHERE concept_id IN ({placeholders})
                """,
                sorted(candidate_ids),
            ).fetchall()
        }
        rows = [
            {
                **row,
                "source_concept_name": names.get(int(row["source_concept_id"])),
                "target_concept_name": names.get(int(row["target_concept_id"])),
            }
            for row in compact_rows
        ]
    else:
        current_placeholders = ",".join("?" for _ in current_ids)
        target_placeholders = ",".join("?" for _ in target_ids)
        rows = rows_to_dicts(
            conn.execute(
                f"""
                SELECT DISTINCT
                    rel.relation_id, rel.source_concept_id, rel.target_concept_id,
                    rel.relation_type, rel.relation_label, rel.review_status,
                    source.concept_name AS source_concept_name,
                    target.concept_name AS target_concept_name
                FROM ontology_concept_relations rel
                JOIN ontology_concepts source ON source.concept_id = rel.source_concept_id
                JOIN ontology_concepts target ON target.concept_id = rel.target_concept_id
                WHERE rel.review_status != 'rejected'
                  AND (
                        (
                            rel.source_concept_id IN ({current_placeholders})
                            AND rel.target_concept_id IN ({target_placeholders})
                        )
                        OR (
                            rel.source_concept_id IN ({target_placeholders})
                            AND rel.target_concept_id IN ({current_placeholders})
                        )
                  )
                ORDER BY rel.relation_id
                LIMIT ?
                """,
                (
                    *sorted(current_ids),
                    *sorted(target_ids),
                    *sorted(target_ids),
                    *sorted(current_ids),
                    limit,
                ),
            ).fetchall()
        )
    related_target_ids: set[int] = set()
    evidence: list[dict[str, Any]] = []
    for row in rows:
        source_id = int(row.get("source_concept_id") or 0)
        target_id = int(row.get("target_concept_id") or 0)
        if source_id in current_ids and target_id in target_ids:
            related_id = target_id
            current_name = row.get("source_concept_name")
            target_name = row.get("target_concept_name")
        elif target_id in current_ids and source_id in target_ids:
            related_id = source_id
            current_name = row.get("target_concept_name")
            target_name = row.get("source_concept_name")
        else:
            continue
        if related_id in exact_ids:
            continue
        related_target_ids.add(related_id)
        if len(evidence) < 12:
            evidence.append(
                {
                    "current_concept": current_name,
                    "target_concept": target_name,
                    "relation_type": row.get("relation_type"),
                    "review_status": row.get("review_status"),
                }
            )
    return {
        "related_target_ids": sorted(related_target_ids),
        "related_target_count": len(related_target_ids),
        "evidence": evidence,
    }


def _task_similarity_transition_profile(
    conn: sqlite3.Connection,
    *,
    current_unit_codes: set[str],
    target_unit_codes: set[str],
    limit: int = 12,
) -> dict[str, Any]:
    current_codes = sorted(code for code in current_unit_codes if code)
    target_codes = sorted(code for code in target_unit_codes if code)
    if not current_codes or not target_codes:
        return {"max_score": 0.0, "link_count": 0, "evidence": []}
    if (
        not sqlite_object_exists(conn, "task_similarity_links")
        and has_compact_criteria_postings(conn)
    ):
        all_codes = sorted(set(current_codes) | set(target_codes))
        placeholders = ",".join("?" for _ in all_codes)
        criteria_rows = conn.execute(
            f"""
            SELECT pc.criteria_id, ce.unit_code
            FROM performance_criteria AS pc
            JOIN competency_elements AS ce ON ce.element_id = pc.element_id
            WHERE ce.unit_code IN ({placeholders})
            ORDER BY ce.unit_code, pc.criteria_id
            """,
            all_codes,
        ).fetchall()
        criteria_to_unit = {
            int(row["criteria_id"]): str(row["unit_code"])
            for row in criteria_rows
        }
        concepts_by_criteria = {
            criteria_id: set(concept_ids_for_task)
            for criteria_id, concept_ids_for_task in criteria_concept_ids(
                conn,
                criteria_to_unit,
            ).items()
        }
        current_criteria = [
            criteria_id
            for criteria_id, code in criteria_to_unit.items()
            if code in current_unit_codes
        ]
        target_criteria = [
            criteria_id
            for criteria_id, code in criteria_to_unit.items()
            if code in target_unit_codes
        ]
        derived_rows: list[dict[str, Any]] = []
        for source_criteria_id in current_criteria:
            source_concepts = concepts_by_criteria.get(source_criteria_id, set())
            if not source_concepts:
                continue
            for target_criteria_id in target_criteria:
                target_concepts = concepts_by_criteria.get(
                    target_criteria_id,
                    set(),
                )
                if not target_concepts:
                    continue
                shared = source_concepts & target_concepts
                if not shared:
                    continue
                union = source_concepts | target_concepts
                derived_rows.append(
                    {
                        "source_unit_code": criteria_to_unit[source_criteria_id],
                        "target_unit_code": criteria_to_unit[target_criteria_id],
                        "relation_type": "derived_criteria_concept_overlap",
                        "similarity_score": round(len(shared) / len(union), 6),
                        "shared_concept_count": len(shared),
                        "source_concept_count": len(source_concepts),
                        "target_concept_count": len(target_concepts),
                    }
                )
        derived_rows.sort(
            key=lambda row: (
                -float(row["similarity_score"]),
                -int(row["shared_concept_count"]),
                str(row["source_unit_code"]),
                str(row["target_unit_code"]),
            )
        )
        rows = derived_rows[:limit]
        max_score = max(
            (float(row.get("similarity_score") or 0.0) for row in rows),
            default=0.0,
        )
        return {
            "max_score": round(max_score, 4),
            "link_count": len(rows),
            "evidence": rows[:5],
        }
    current_placeholders = ",".join("?" for _ in current_codes)
    target_placeholders = ",".join("?" for _ in target_codes)
    rows = rows_to_dicts(
        conn.execute(
            f"""
            SELECT
                source_unit_code, target_unit_code, relation_type,
                similarity_score, shared_concept_count,
                source_concept_count, target_concept_count
            FROM task_similarity_links
            WHERE review_status != 'rejected'
              AND (
                    (
                        source_unit_code IN ({current_placeholders})
                        AND target_unit_code IN ({target_placeholders})
                    )
                    OR (
                        source_unit_code IN ({target_placeholders})
                        AND target_unit_code IN ({current_placeholders})
                    )
              )
            ORDER BY similarity_score DESC, shared_concept_count DESC
            LIMIT ?
            """,
            (*current_codes, *target_codes, *target_codes, *current_codes, limit),
        ).fetchall()
    )
    max_score = max((float(row.get("similarity_score") or 0.0) for row in rows), default=0.0)
    return {
        "max_score": round(max_score, 4),
        "link_count": len(rows),
        "evidence": rows[:5],
    }


def _transition_semantic_fit(
    conn: sqlite3.Connection,
    *,
    current_scope: dict[str, Any],
    target_scope: dict[str, Any],
    current_ids: set[int],
    target_ids: set[int],
    exact_ids: set[int],
    current_job_base_keys: set[str],
    target_job_base_keys: set[str],
    current_qualification_keys: set[str],
    target_qualification_keys: set[str],
    target_role_overlay: dict[str, Any] | None,
) -> dict[str, Any]:
    exact_ratio = round(len(exact_ids) / len(target_ids), 4) if target_ids else 0.0
    current_units = set(current_scope.get("unit_codes") or [])
    target_units = set(target_scope.get("unit_codes") or [])
    current_scope_subset = bool(current_units and target_units and current_units < target_units)
    current_target_unit_overlap = current_units & target_units
    containment_score = 0.22 if current_scope_subset else (0.12 if current_target_unit_overlap else 0.0)
    classification_fit = _classification_scope_fit(current_scope, target_scope)
    related_profile = _ontology_related_transition_profile(
        conn,
        current_ids=current_ids,
        target_ids=target_ids,
        exact_ids=exact_ids,
    )
    related_ratio = (
        related_profile["related_target_count"] / len(target_ids)
        if target_ids
        else 0.0
    )
    ontology_related_score = min(0.14, 0.35 * related_ratio)
    task_similarity = _task_similarity_transition_profile(
        conn,
        current_unit_codes=current_units,
        target_unit_codes=target_units,
    )
    task_similarity_score = min(0.1, 0.2 * float(task_similarity.get("max_score") or 0.0))
    job_base_ratio = (
        len(current_job_base_keys & target_job_base_keys) / len(target_job_base_keys)
        if target_job_base_keys
        else 0.0
    )
    qualification_ratio = (
        len(current_qualification_keys & target_qualification_keys) / len(target_qualification_keys)
        if target_qualification_keys
        else 0.0
    )
    job_base_score = min(0.05, 0.12 * job_base_ratio)
    qualification_score = min(0.03, 0.08 * qualification_ratio)
    role_overlay_score = 0.0
    if target_role_overlay:
        if current_scope_subset or current_target_unit_overlap or classification_fit["score"] >= 0.28:
            role_overlay_score = 0.08
        elif classification_fit["score"] > 0:
            role_overlay_score = 0.04
    components = {
        "exact_ksa_overlap_ratio": exact_ratio,
        "scope_containment_score": round(containment_score, 4),
        "classification_scope_score": round(float(classification_fit["score"]), 4),
        "ontology_related_score": round(ontology_related_score, 4),
        "task_similarity_score": round(task_similarity_score, 4),
        "job_base_score": round(job_base_score, 4),
        "qualification_score": round(qualification_score, 4),
        "role_overlay_score": round(role_overlay_score, 4),
    }
    adjusted = min(1.0, sum(float(value) for value in components.values()))
    return {
        "ratio": round(max(exact_ratio, adjusted), 4),
        "method": "exact_ksa_overlap_plus_ncs_scope_ontology_task_similarity_role_overlay",
        "components": components,
        "scope_relation": classification_fit["relation"],
        "current_scope_subset_of_target": current_scope_subset,
        "shared_unit_count": len(current_target_unit_overlap),
        "ontology_related_ksa_count": related_profile["related_target_count"],
        "ontology_related_evidence": related_profile["evidence"],
        "task_similarity": task_similarity,
        "role_overlay": target_role_overlay,
    }


def _relation_rows(conn: sqlite3.Connection, concept_ids: set[int], *, limit: int = 50) -> list[dict[str, Any]]:
    if not concept_ids:
        return []
    if has_compact_ontology_postings(conn):
        rows = ontology_relation_rows(
            conn,
            incident_ids=concept_ids,
            limit=limit,
        )
        endpoint_ids = {
            int(row[key])
            for row in rows
            for key in ("source_concept_id", "target_concept_id")
        }
        if not endpoint_ids:
            return []
        placeholders = ",".join("?" for _ in endpoint_ids)
        names = {
            int(row["concept_id"]): row["concept_name"]
            for row in conn.execute(
                f"""
                SELECT concept_id, concept_name
                FROM ontology_concepts
                WHERE concept_id IN ({placeholders})
                """,
                sorted(endpoint_ids),
            ).fetchall()
        }
        return [
            {
                **row,
                "source_concept_name": names.get(int(row["source_concept_id"])),
                "target_concept_name": names.get(int(row["target_concept_id"])),
            }
            for row in rows
        ]
    placeholders = ",".join("?" for _ in concept_ids)
    rows = conn.execute(
        f"""
        SELECT
            rel.relation_id, rel.source_concept_id, rel.target_concept_id,
            rel.relation_type, rel.relation_label, rel.review_status,
            source.concept_name AS source_concept_name,
            target.concept_name AS target_concept_name
        FROM ontology_concept_relations rel
        JOIN ontology_concepts source ON source.concept_id = rel.source_concept_id
        JOIN ontology_concepts target ON target.concept_id = rel.target_concept_id
        WHERE rel.source_concept_id IN ({placeholders})
           OR rel.target_concept_id IN ({placeholders})
        ORDER BY rel.relation_id
        LIMIT ?
        """,
        (*concept_ids, *concept_ids, limit),
    ).fetchall()
    return rows_to_dicts(rows)


def _course_delivery_relations(course: sqlite3.Row | dict[str, Any]) -> list[dict[str, Any]]:
    row = _row_dict(course)
    relations: list[dict[str, Any]] = []
    level = _clean(row.get("compe_unit_level"))
    if level:
        relations.append(
            {
                "relation_type": "has_level",
                "relation_value": level,
                "normalized_value": normalize_concept_key(level),
                "numeric_value": _parse_number(level),
                "evidence_text": f"능력단위수준 {level}",
                "confidence_score": 1.0,
            }
        )
    train_time = _clean(row.get("train_time"))
    if train_time:
        relations.append(
            {
                "relation_type": "requires_time",
                "relation_value": train_time,
                "normalized_value": normalize_concept_key(train_time),
                "numeric_value": _parse_number(train_time),
                "evidence_text": f"훈련시간 {train_time}",
                "confidence_score": 1.0,
            }
        )
    for facility in _split_list_value(row.get("fac_name")):
        relations.append(
            {
                "relation_type": "uses_facility",
                "relation_value": facility,
                "normalized_value": normalize_concept_key(facility),
                "numeric_value": None,
                "evidence_text": _clean(row.get("fac_name")),
                "confidence_score": 0.95,
            }
        )
    for method in _split_training_methods(row.get("meth_name")):
        relations.append(
            {
                "relation_type": "delivered_by",
                "relation_value": method,
                "normalized_value": normalize_concept_key(method),
                "numeric_value": None,
                "evidence_text": _clean(row.get("meth_name")),
                "confidence_score": 0.95,
            }
        )
    return relations


def _delivery_mode_profile(delivery_relations: list[sqlite3.Row] | list[dict[str, Any]]) -> dict[str, Any]:
    values = [_clean(_row_dict(row).get("relation_value")) for row in delivery_relations]
    methods = [
        value
        for value in values
        if _row_dict({"relation_value": value})
        and any(value.lower() in aliases or alias in value.lower() for aliases in METHOD_GROUP_ALIASES.values() for alias in aliases)
    ]
    facilities = [value for value in values if value and value not in methods and not re.fullmatch(r"\d+(?:\.\d+)?", value)]
    text = " ".join(values).lower()
    return {
        "methods": sorted(set(methods)),
        "facilities": sorted(set(facilities)),
        "practical_method": any(token in text for token in ("practice", "실습", "현장")),
        "practical_facility": any(token in text for token in ("lab", "center", "실습", "센터")),
        "remote_only": bool(values) and all("원격" in value or "online" in value.lower() for value in values),
    }


def _preference_time_adjustment(hours: float | None, preferred_max_hours: float | None) -> tuple[float, str, float | None]:
    if hours is None or preferred_max_hours is None or preferred_max_hours <= 0:
        return 0.0, "unspecified", None
    if hours <= preferred_max_hours:
        return 0.02, "fit", 0.0
    over_ratio = round((hours - preferred_max_hours) / preferred_max_hours, 4)
    return -round(min(0.15, 0.09 * over_ratio + 0.015), 4), "over", over_ratio


def _method_groups(values: list[str] | None) -> set[str]:
    groups: set[str] = set()
    for raw in values or []:
        value = _clean(raw).lower()
        normalized = value.replace(" ", "").replace("-", "")
        for group, aliases in METHOD_GROUP_ALIASES.items():
            if any(alias.lower().replace(" ", "").replace("-", "") in normalized for alias in aliases):
                groups.add(group)
        if value and not any(value in aliases for aliases in METHOD_GROUP_ALIASES.values()):
            groups.add(value)
    return groups


def _preference_fit_profile(
    *,
    delivery_methods: list[str] | None,
    requested_methods: list[str] | None,
    hours: float | None,
    preferred_max_hours: float | None,
) -> dict[str, Any]:
    delivery_groups = _method_groups(delivery_methods)
    requested_groups = _method_groups(requested_methods)
    matched_groups = sorted(delivery_groups & requested_groups)
    time_adjustment, time_fit, over_ratio = _preference_time_adjustment(hours, preferred_max_hours)
    return {
        "preferred_max_hours": preferred_max_hours,
        "actual_hours": hours,
        "time_fit": time_fit,
        "time_over_ratio": over_ratio,
        "time_score_adjustment": time_adjustment,
        "requested_methods": requested_methods or [],
        "delivery_methods": delivery_methods or [],
        "matched_method_groups": matched_groups,
        "method_fit": bool(requested_groups and matched_groups) or not requested_groups,
    }


def _recommendation_tier(score: float, match: dict[str, Any]) -> dict[str, str]:
    direct = bool(match.get("direct_unit_evidence"))
    source_element = bool(match.get("source_element_covered"))
    goal_direct_or_token = bool(
        int(match.get("goal_direct_concept_hits") or 0)
        + int(match.get("goal_token_concept_hits") or 0)
    )
    if match.get("current_scope_already_covered"):
        return {
            "tier": "supplemental",
            "label": "기반 유지",
            "rationale": "현재 직무에서 이미 직접 다루는 능력단위이므로 추가 보완이 아니라 기반 확인 과정",
        }
    if direct and (score >= 0.75 or source_element or (score >= 0.5 and goal_direct_or_token)):
        return {
            "tier": "primary",
            "label": "주추천",
            "rationale": "목표 NCS 능력단위, 수행요소, 또는 훈련목표 KSA와 직접 연결된 과정",
        }
    if (
        direct
        or score >= 0.5
        or match.get("support_course_hint_weight")
        or match.get("sibling_scope_evidence")
    ):
        return {
            "tier": "supplemental",
            "label": "보조추천",
            "rationale": "전환 경로의 부족 역량이나 주변 직무 역량을 보완하는 과정",
        }
    return {
        "tier": "adjacent",
        "label": "인접추천",
        "rationale": "목표와 인접한 NCS 범위에서 참고할 수 있는 낮은 확신의 과정",
    }


def _diversify_top_k_candidates(
    candidates: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    deferred: list[dict[str, Any]] = []
    sub_code_counts: dict[str, int] = {}
    for candidate in candidates:
        row = _row_dict(candidate.get("row"))
        sub_code = _clean(row.get("ncs_subd_cd")) or "__missing__"
        strong_direct_evidence = (
            bool((candidate.get("match") or {}).get("direct_unit_evidence"))
            and float(candidate.get("score") or 0.0) >= DIRECT_UNIT_DIVERSITY_BYPASS_SCORE
        )
        if (
            sub_code_counts.get(sub_code, 0) >= MAX_SAME_SUB_CODE_IN_TOP_K
            and len(selected) < max_items
            and not strong_direct_evidence
        ):
            deferred.append(candidate)
            continue
        selected.append(candidate)
        sub_code_counts[sub_code] = sub_code_counts.get(sub_code, 0) + 1
        if len(selected) >= max_items:
            break
    if len(selected) >= max_items:
        return selected
    for candidate in deferred:
        row = _row_dict(candidate.get("row"))
        sub_code = _clean(row.get("ncs_subd_cd")) or "__missing__"
        if sub_code_counts.get(sub_code, 0) >= MAX_SAME_SUB_CODE_IN_TOP_K:
            penalty = 0.03
            candidate["score"] = max(0.0, float(candidate.get("score") or 0.0) - penalty)
            candidate.setdefault("match", {}).setdefault("reasons", []).append("diversity_penalty")
            candidate["match"]["diversity_penalty"] = penalty
            components = candidate["match"].setdefault("score_components", {})
            components["penalty_score"] = round(float(components.get("penalty_score", 0.0)) - penalty, 4)
            components["final_score"] = round(candidate["score"], 4)
        selected.append(candidate)
        sub_code_counts[sub_code] = sub_code_counts.get(sub_code, 0) + 1
        if len(selected) >= max_items:
            break
    return selected


def available_major_codes(conn: sqlite3.Connection) -> list[str]:
    return [
        row["major_code"]
        for row in conn.execute(
            """
            SELECT DISTINCT major_code
            FROM classifications
            WHERE TRIM(COALESCE(major_code, '')) <> ''
            ORDER BY major_code
            """
        ).fetchall()
    ]


def _apply_query_alias(
    conn: sqlite3.Connection,
    query: str | None,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, dict[str, Any] | None]:
    text = _clean(query)
    if not text:
        return query, major_code, middle_code, small_code, sub_code, None
    normalized_query, normalization = _generic_job_query_normalization(text)
    lookup_terms: list[tuple[str, dict[str, Any] | None]] = [(text, None)]
    if normalization and normalized_query and normalized_query != text:
        lookup_terms.append((normalized_query, normalization))
    for lookup_text, lookup_normalization in lookup_terms:
        if lookup_normalization:
            row = conn.execute(
                """
                SELECT *
                FROM ncs_query_aliases
                WHERE alias_text = ?
                  AND review_status != 'rejected'
                ORDER BY
                    CASE review_status
                        WHEN 'human_reviewed' THEN 0
                        WHEN 'reviewed' THEN 1
                        WHEN 'accepted' THEN 2
                        WHEN 'candidate' THEN 3
                        ELSE 4
                    END,
                    confidence_score DESC,
                    alias_id
                LIMIT 1
                """,
                (lookup_text,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT *
                FROM ncs_query_aliases
                WHERE (normalized_query = ? OR alias_text = ?)
                  AND review_status != 'rejected'
                ORDER BY
                    CASE WHEN alias_text = ? THEN 0 ELSE 1 END,
                    CASE review_status
                        WHEN 'human_reviewed' THEN 0
                        WHEN 'reviewed' THEN 1
                        WHEN 'accepted' THEN 2
                        WHEN 'candidate' THEN 3
                        ELSE 4
                    END,
                    confidence_score DESC,
                    alias_id
                LIMIT 1
                """,
                (lookup_text, lookup_text, lookup_text),
            ).fetchone()
        if not row:
            continue
        alias = dict(row)
        if lookup_normalization:
            alias["query_normalization"] = lookup_normalization
        return (
            alias.get("normalized_query") or lookup_text,
            major_code or alias.get("major_code"),
            middle_code or alias.get("middle_code"),
            small_code or alias.get("small_code"),
            sub_code or alias.get("sub_code"),
            alias,
        )
    if normalization:
        return normalized_query, major_code, middle_code, small_code, sub_code, None
    return query, major_code, middle_code, small_code, sub_code, None


def _exact_unit_name_match(
    conn: sqlite3.Connection,
    query: str | None,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, Any] | None:
    query_key = normalize_concept_key(query or "")
    if not query_key:
        return None
    rows = conn.execute(
        """
        SELECT cu.*, c.major_code, c.major_name, c.middle_code, c.middle_name,
               c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE (? IS NULL OR c.major_code = ?)
          AND (? IS NULL OR c.middle_code = ?)
          AND (? IS NULL OR c.small_code = ?)
          AND (? IS NULL OR c.sub_code = ?)
        ORDER BY
            CASE WHEN c.major_code = '02' THEN 0 ELSE 1 END,
            c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        """,
        (major_code, major_code, middle_code, middle_code, small_code, small_code, sub_code, sub_code),
    ).fetchall()
    for row in rows:
        rowd = dict(row)
        if normalize_concept_key(rowd.get("unit_name_raw") or "") == query_key:
            return rowd
    return None


def _alias_unit_conflicts_with_exact_unit(
    conn: sqlite3.Connection,
    *,
    requested_query: str,
    alias: dict[str, Any] | None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, Any] | None:
    if not alias or alias.get("review_status") in TRUSTED_TRANSITION_REVIEW_STATUSES:
        return None
    alias_unit_code = _clean(alias.get("unit_code"))
    if not alias_unit_code:
        return None
    exact_unit = _exact_unit_name_match(
        conn,
        requested_query,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if not exact_unit:
        return None
    exact_unit_code = _clean(exact_unit.get("unit_code"))
    if exact_unit_code and exact_unit_code != alias_unit_code:
        return {
            "reason": "candidate_alias_conflicts_with_exact_unit_name",
            "alias_unit_code": alias_unit_code,
            "exact_unit_code": exact_unit_code,
            "exact_unit_name": exact_unit.get("unit_name_raw"),
        }
    return None


def _candidate_score(text: str, query: str, *, exact_bonus: float = 0.0) -> float:
    text_key = normalize_concept_key(text)
    query_key = normalize_concept_key(query)
    if not query_key:
        return 0.0
    if text_key == query_key:
        return 1.0 + exact_bonus
    if text_key.startswith(query_key):
        return 0.82 + exact_bonus
    if query_key in text_key:
        return 0.64 + exact_bonus
    if not _candidate_allows_edit_distance(text_key, query_key):
        return 0.0
    distance = _levenshtein(text_key, query_key)
    if distance <= 2 and min(len(text_key), len(query_key)) >= 5:
        return max(0.55, 0.8 - distance * 0.1)
    return 0.0


def _candidate_allows_edit_distance(text_key: str, query_key: str) -> bool:
    if min(len(text_key), len(query_key)) < 5:
        return False
    if abs(len(text_key) - len(query_key)) > 2:
        return False
    if text_key[:1] == query_key[:1]:
        return True
    query_bigrams = {query_key[index : index + 2] for index in range(len(query_key) - 1)}
    if not query_bigrams:
        return False
    return any(text_key[index : index + 2] in query_bigrams for index in range(len(text_key) - 1))


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    prev = list(range(len(right) + 1))
    for i, c1 in enumerate(left, start=1):
        curr = [i]
        for j, c2 in enumerate(right, start=1):
            curr.append(min(curr[-1] + 1, prev[j] + 1, prev[j - 1] + (0 if c1 == c2 else 1)))
        prev = curr
    return prev[-1]


def resolve_ncs_query_scope(
    conn: sqlite3.Connection,
    query: str,
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    text = _clean(query)
    requested_text = text
    requested_filters = {
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "sub_code": sub_code,
    }
    max_rows = clamp_limit(limit, default=10, maximum=50)
    if not text:
        return {"ok": False, "query": query, "normalized_query": "", "candidates": []}
    query_alias = None
    alias_unit_code = None
    query_normalization: dict[str, Any] | None = None
    aliased_query, major_code, middle_code, small_code, sub_code, query_alias = _apply_query_alias(
        conn,
        query,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if query_alias:
        query_normalization = query_alias.get("query_normalization")
    if query_normalization is None:
        normalized_query, normalization = _generic_job_query_normalization(requested_text)
        if normalization and _clean(normalized_query) == _clean(aliased_query):
            query_normalization = normalization
    alias_guard = _alias_unit_conflicts_with_exact_unit(
        conn,
        requested_query=requested_text,
        alias=query_alias,
        **requested_filters,
    )
    if alias_guard:
        alias_unit_code = None
        if query_alias:
            query_alias = {**query_alias, "ignored_unit_code": _clean(query_alias.get("unit_code")), "ignore_guard": alias_guard}
        aliased_query = requested_text
        major_code = requested_filters["major_code"]
        middle_code = requested_filters["middle_code"]
        small_code = requested_filters["small_code"]
        sub_code = requested_filters["sub_code"]
    if aliased_query:
        text = _clean(aliased_query) or text
    if query_alias and not alias_guard:
        alias_unit_code = _clean(query_alias.get("unit_code"))
    candidates: list[dict[str, Any]] = []
    class_rows = conn.execute(
        """
        SELECT *
        FROM classifications
        WHERE (? IS NULL OR major_code = ?)
          AND (? IS NULL OR middle_code = ?)
          AND (? IS NULL OR small_code = ?)
          AND (? IS NULL OR sub_code = ?)
        ORDER BY major_code, middle_code, small_code, sub_code
        """,
        (major_code, major_code, middle_code, middle_code, small_code, small_code, sub_code, sub_code),
    ).fetchall()
    for row in class_rows:
        rowd = dict(row)
        levels = [
            ("major_classification", "major_name"),
            ("middle_classification", "middle_name"),
            ("small_classification", "small_name"),
            ("sub_classification", "sub_name"),
        ]
        for match_level, field in levels:
            score = _candidate_score(rowd.get(field) or "", text, exact_bonus=0.08 if rowd.get("major_code") == "02" else 0.0)
            if score <= 0:
                continue
            candidates.append(
                {
                    "candidate_type": "classification",
                    "match_level": match_level,
                    "matched_text": rowd.get(field),
                    "confidence_score": round(min(score, 1.0), 4),
                    **rowd,
                }
            )
    unit_rows = conn.execute(
        """
        SELECT cu.*, c.major_code, c.major_name, c.middle_code, c.middle_name,
               c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM competency_units cu
        JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE (? IS NULL OR c.major_code = ?)
          AND (? IS NULL OR c.middle_code = ?)
          AND (? IS NULL OR c.small_code = ?)
          AND (? IS NULL OR c.sub_code = ?)
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code, cu.unit_code
        """,
        (major_code, major_code, middle_code, middle_code, small_code, small_code, sub_code, sub_code),
    ).fetchall()
    for row in unit_rows:
        rowd = dict(row)
        score = _candidate_score(rowd.get("unit_name_raw") or "", text, exact_bonus=0.05 if rowd.get("major_code") == "02" else 0.0)
        match_level = "competency_unit"
        query_alias_review_status = None
        if alias_unit_code and rowd.get("unit_code") == alias_unit_code:
            score = max(score, float(query_alias.get("confidence_score") or 0.0), 0.9)
            match_level = "query_alias_unit"
            query_alias_review_status = query_alias.get("review_status")
        if score <= 0:
            continue
        candidates.append(
            {
                "candidate_type": "unit",
                "match_level": match_level,
                "matched_text": rowd.get("unit_name_raw"),
                "unit_name": rowd.get("unit_name_raw"),
                "unit_code": rowd.get("unit_code"),
                "query_alias_review_status": query_alias_review_status,
                "confidence_score": round(min(score, 1.0), 4),
                **rowd,
            }
        )
    element_rows = conn.execute(
        """
        SELECT ce.*, cu.unit_name_raw, c.major_code, c.major_name, c.middle_code, c.middle_name,
               c.small_code, c.small_name, c.sub_code, c.sub_name
        FROM competency_elements ce
        JOIN competency_units cu ON cu.unit_code = ce.unit_code
        JOIN classifications c ON c.classification_id = cu.classification_id
        ORDER BY ce.element_id
        """
    ).fetchall()
    for row in element_rows:
        rowd = dict(row)
        if major_code and rowd.get("major_code") != major_code:
            continue
        score = _candidate_score(rowd.get("element_name_raw") or "", text)
        if score <= 0:
            continue
        candidates.append(
            {
                "candidate_type": "element",
                "match_level": "competency_element",
                "matched_text": rowd.get("element_name_raw"),
                "confidence_score": round(min(score, 1.0), 4),
                **rowd,
            }
        )
    query_key = normalize_concept_key(text)
    concept_rows = conn.execute(
        """
        SELECT concept_id, concept_name, concept_type, definition_status, review_status
        FROM ontology_concepts
        WHERE normalized_key = ?
           OR normalized_key LIKE ?
           OR normalized_key LIKE ?
           OR concept_name LIKE ?
        ORDER BY concept_id
        LIMIT 2000
        """,
        (query_key, f"{query_key}%", f"%{query_key}%", f"%{text}%"),
    ).fetchall()
    for row in concept_rows:
        rowd = dict(row)
        score = _candidate_score(rowd.get("concept_name") or "", text)
        if score <= 0:
            continue
        candidates.append(
            {
                "candidate_type": "concept",
                "match_level": "ontology_concept",
                "matched_text": rowd.get("concept_name"),
                "confidence_score": round(min(score, 1.0), 4),
                **rowd,
            }
        )
    order = {
        "classification": 0,
        "unit": 1,
        "element": 2,
        "concept": 3,
    }
    def candidate_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
        exact_text = _clean(item.get("matched_text")).lower() == text.lower()
        candidate_type = str(item.get("candidate_type"))
        match_level = str(item.get("match_level"))
        trusted_alias = (
            match_level == "query_alias_unit"
            and _clean(item.get("query_alias_review_status")) in TRUSTED_TRANSITION_REVIEW_STATUSES
        )
        untrusted_alias = match_level == "query_alias_unit" and not trusted_alias
        return (
            0 if exact_text else 1,
            0 if exact_text and candidate_type == "unit" and match_level == "competency_unit" else 1,
            0 if trusted_alias else 1,
            0 if match_level == "query_alias_unit" else 1,
            1 if untrusted_alias else 0,
            -float(item.get("confidence_score") or 0.0),
            order.get(candidate_type, 9),
            0 if str(item.get("matched_text") or "").lower().startswith(text.lower()) else 1,
            str(item.get("matched_text") or ""),
        )

    candidates.sort(
        key=candidate_sort_key
    )
    return {
        "ok": bool(candidates),
        "query": query,
        "effective_query": text,
        "query_alias": query_alias,
        "query_normalization": query_normalization,
        "normalized_query": normalize_concept_key(text),
        "candidates": candidates[:max_rows],
    }


def _resolution_classification_filters(
    resolution: dict[str, Any],
    *,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, str | None]:
    filters = {
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "sub_code": sub_code,
    }
    top = _resolution_classification_scope_candidate(resolution)
    if top is None:
        return filters
    fields_by_level = {
        "major_classification": ("major_code",),
        "middle_classification": ("major_code", "middle_code"),
        "small_classification": ("major_code", "middle_code", "small_code"),
        "sub_classification": ("major_code", "middle_code", "small_code", "sub_code"),
    }
    for field in fields_by_level.get(str(top.get("match_level")), ()):
        if not filters.get(field):
            filters[field] = _clean(top.get(field)) or None
    return filters


def _resolution_classification_scope_candidate(resolution: dict[str, Any]) -> dict[str, Any] | None:
    candidates = resolution.get("candidates") if isinstance(resolution, dict) else []
    top = candidates[0] if isinstance(candidates, list) and candidates else None
    if not isinstance(top, dict) or top.get("candidate_type") != "classification":
        return None
    normalization = resolution.get("query_normalization")
    if not isinstance(normalization, dict) or normalization.get("method") != "generic_suffix_strip":
        return None
    effective_query = _clean(resolution.get("effective_query"))
    if not effective_query or _clean(top.get("matched_text")) != effective_query:
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("candidate_type") == "unit" and _clean(candidate.get("matched_text")) == effective_query:
            return None
    return top


def _attach_query_normalization(
    resolution: dict[str, Any],
    normalization: dict[str, Any] | None,
    normalized_query: str | None,
) -> dict[str, Any]:
    if not normalization or not isinstance(resolution, dict) or resolution.get("query_normalization"):
        return resolution
    if _clean(normalized_query) != _clean(resolution.get("effective_query")):
        return resolution
    return {**resolution, "query_normalization": normalization}


def _resolve_query_scope_units(
    conn: sqlite3.Connection,
    *,
    query: str | None,
    source: dict[str, Any],
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
) -> dict[str, Any]:
    text = _clean(query)
    unit_codes: list[str]
    match_level = "source_unit"
    match_text = source.get("unit_name_raw")
    class_matches = [
        ("sub_classification", "sub_name", ("major_code", "middle_code", "small_code", "sub_code")),
        ("small_classification", "small_name", ("major_code", "middle_code", "small_code")),
        ("middle_classification", "middle_name", ("major_code", "middle_code")),
        ("major_classification", "major_name", ("major_code",)),
    ]
    matched_fields: tuple[str, ...] | None = None
    for candidate_level, name_field, code_fields in class_matches:
        if text and _clean(source.get(name_field)) == text:
            match_level = candidate_level
            match_text = source.get(name_field)
            matched_fields = code_fields
            break
    if matched_fields:
        where = " AND ".join(f"c.{field} = ?" for field in matched_fields)
        rows = conn.execute(
            f"""
            SELECT cu.unit_code
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE {where}
            ORDER BY cu.unit_code
            """,
            tuple(source.get(field) for field in matched_fields),
        ).fetchall()
        unit_codes = [row["unit_code"] for row in rows] or [source["unit_code"]]
    else:
        unit_codes = [source["unit_code"]]
    return {
        "match_level": match_level,
        "match_text": match_text,
        "unit_codes": unit_codes,
        "major_code": major_code or source.get("major_code"),
        "middle_code": middle_code or source.get("middle_code"),
        "small_code": small_code or source.get("small_code"),
        "sub_code": sub_code or source.get("sub_code"),
    }


def _project_fields(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


def _bounded_link_rows(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[Any, ...],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows = rows_to_dicts(conn.execute(sql, (*params, limit)).fetchall())
    total_count = int(rows[0].pop("_total_count")) if rows else 0
    for row in rows[1:]:
        row.pop("_total_count", None)
    returned_count = len(rows)
    return rows, {
        "total_count": total_count,
        "returned_count": returned_count,
        "truncated": total_count > returned_count,
    }


def _course_payload(
    conn: sqlite3.Connection,
    course: sqlite3.Row | dict[str, Any],
    *,
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
) -> dict[str, Any]:
    row = _row_dict(course)
    cid = int(row["training_course_id"])
    max_links = clamp_limit(
        link_limit,
        default=DEFAULT_COURSE_LINK_LIMIT,
        maximum=MAX_COURSE_LINK_LIMIT,
    )
    unit_links, unit_meta = _bounded_link_rows(
        conn,
        """
        SELECT unit_code, link_method, confidence_score, review_status,
               COUNT(*) OVER () AS _total_count
        FROM ncs_training_course_unit_links
        WHERE training_course_id = ?
        ORDER BY link_id
        LIMIT ?
        """,
        (cid,),
        limit=max_links,
    )
    concept_links, concept_meta = _bounded_link_rows(
        conn,
        """
        SELECT l.unit_code, l.concept_id, oc.concept_name, oc.concept_type,
               l.link_method, l.confidence_score, l.review_status,
               COUNT(*) OVER () AS _total_count
        FROM ncs_training_course_concept_links l
        JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
        WHERE l.training_course_id = ?
        ORDER BY l.link_id
        LIMIT ?
        """,
        (cid,),
        limit=max_links,
    )
    element_links, element_meta = _bounded_link_rows(
        conn,
        """
        SELECT l.unit_code, l.element_id, ce.element_name_raw AS element_name,
               l.link_method, l.confidence_score, l.review_status,
               COUNT(*) OVER () AS _total_count
        FROM ncs_training_course_element_links l
        JOIN competency_elements ce ON ce.element_id = l.element_id
        WHERE l.training_course_id = ?
        ORDER BY l.link_id
        LIMIT ?
        """,
        (cid,),
        limit=max_links,
    )
    goal_links, goal_meta = _bounded_link_rows(
        conn,
        """
        SELECT l.unit_code, l.element_id, l.concept_id,
               oc.concept_name, oc.concept_type,
               l.link_method, l.confidence_score, l.review_status,
               COUNT(*) OVER () AS _total_count
        FROM training_goal_concept_links l
        JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
        WHERE l.training_course_id = ?
        ORDER BY l.link_id
        LIMIT ?
        """,
        (cid,),
        limit=max_links,
    )
    delivery, delivery_meta = _bounded_link_rows(
        conn,
        """
        SELECT relation_type, relation_value, normalized_value, numeric_value,
               confidence_score, review_status,
               COUNT(*) OVER () AS _total_count
        FROM training_delivery_relations
        WHERE training_course_id = ?
        ORDER BY relation_id
        LIMIT ?
        """,
        (cid,),
        limit=max_links,
    )
    return {
        "training_course": _project_fields(row, PUBLIC_TRAINING_COURSE_FIELDS),
        "unit_links": unit_links,
        "concept_links": concept_links,
        "element_links": element_links,
        "goal_concept_links": goal_links,
        "delivery_relations": delivery,
        "link_meta": {
            "unit_links": unit_meta,
            "concept_links": concept_meta,
            "element_links": element_meta,
            "goal_concept_links": goal_meta,
            "delivery_relations": delivery_meta,
        },
    }


def _course_summary_payload(course: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    row = _row_dict(course)
    summary = _project_fields(row, SUMMARY_TRAINING_COURSE_FIELDS)
    goal = _clean(summary.get("train_goal"))
    if len(goal) > 100:
        goal = f"{goal[:97]}..."
    summary["train_goal"] = goal or None
    return {
        "training_course": summary,
        "link_counts": {
            "unit_links": int(row.get("unit_link_count") or 0),
            "concept_links": int(row.get("concept_link_count") or 0),
            "element_links": int(row.get("element_link_count") or 0),
            "goal_concept_links": int(row.get("goal_link_count") or 0),
            "delivery_relations": int(row.get("delivery_relation_count") or 0),
        },
    }


def _rows_by_course_id(rows: list[sqlite3.Row]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        grouped.setdefault(int(item["training_course_id"]), []).append(item)
    return grouped


def _course_payloads_by_id(
    conn: sqlite3.Connection,
    course_rows: list[sqlite3.Row],
) -> dict[int, dict[str, Any]]:
    course_ids = [int(row["training_course_id"]) for row in course_rows]
    if not course_ids:
        return {}
    course_id_json = json.dumps(course_ids)
    unit_links = _rows_by_course_id(
        conn.execute(
            """
            SELECT *
            FROM ncs_training_course_unit_links
            WHERE training_course_id IN (SELECT value FROM json_each(?))
            ORDER BY training_course_id, link_id
            """,
            (course_id_json,),
        ).fetchall()
    )
    concept_links = _rows_by_course_id(
        conn.execute(
            """
            SELECT *
            FROM ncs_training_course_concept_links
            WHERE training_course_id IN (SELECT value FROM json_each(?))
            ORDER BY training_course_id, link_id
            """,
            (course_id_json,),
        ).fetchall()
    )
    element_links = _rows_by_course_id(
        conn.execute(
            """
            SELECT l.*, ce.element_name_raw
            FROM ncs_training_course_element_links l
            JOIN competency_elements ce ON ce.element_id = l.element_id
            WHERE l.training_course_id IN (SELECT value FROM json_each(?))
            ORDER BY l.training_course_id, l.link_id
            """,
            (course_id_json,),
        ).fetchall()
    )
    goal_links = _rows_by_course_id(
        conn.execute(
            """
            SELECT l.*, oc.concept_name, oc.concept_type
            FROM training_goal_concept_links l
            JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
            WHERE l.training_course_id IN (SELECT value FROM json_each(?))
            ORDER BY l.training_course_id, l.link_id
            """,
            (course_id_json,),
        ).fetchall()
    )
    delivery = _rows_by_course_id(
        conn.execute(
            """
            SELECT *
            FROM training_delivery_relations
            WHERE training_course_id IN (SELECT value FROM json_each(?))
            ORDER BY training_course_id, relation_id
            """,
            (course_id_json,),
        ).fetchall()
    )
    payloads: dict[int, dict[str, Any]] = {}
    for course in course_rows:
        cid = int(course["training_course_id"])
        payloads[cid] = {
            "training_course": dict(course),
            "unit_links": unit_links.get(cid, []),
            "concept_links": concept_links.get(cid, []),
            "element_links": element_links.get(cid, []),
            "goal_concept_links": goal_links.get(cid, []),
            "delivery_relations": delivery.get(cid, []),
        }
    return payloads


def _candidate_training_course_rows(
    conn: sqlite3.Connection,
    *,
    scope_unit_codes: set[str],
    target_concept_ids: set[int],
    support_course_names: set[str],
) -> list[sqlite3.Row]:
    unit_json = json.dumps(sorted(code for code in scope_unit_codes if code))
    concept_json = json.dumps(sorted(int(cid) for cid in target_concept_ids if cid))
    name_json = json.dumps(sorted(name for name in support_course_names if name))
    return conn.execute(
        """
        SELECT DISTINCT tc.*
        FROM ncs_training_courses tc
        WHERE tc.ncs_cl_cd IN (SELECT value FROM json_each(?))
           OR tc.compe_unit_name IN (SELECT value FROM json_each(?))
           OR EXISTS (
                SELECT 1
                FROM ncs_training_course_unit_links l
                WHERE l.training_course_id = tc.training_course_id
                  AND l.unit_code IN (SELECT value FROM json_each(?))
           )
           OR EXISTS (
                SELECT 1
                FROM ncs_training_course_element_links l
                WHERE l.training_course_id = tc.training_course_id
                  AND l.unit_code IN (SELECT value FROM json_each(?))
           )
           OR EXISTS (
                SELECT 1
                FROM ncs_training_course_concept_links l
                WHERE l.training_course_id = tc.training_course_id
                  AND l.unit_code IN (SELECT value FROM json_each(?))
           )
           OR EXISTS (
                SELECT 1
                FROM training_goal_concept_links l
                WHERE l.training_course_id = tc.training_course_id
                  AND l.concept_id IN (SELECT value FROM json_each(?))
           )
        ORDER BY tc.training_course_id
        """,
        (unit_json, name_json, unit_json, unit_json, unit_json, concept_json),
    ).fetchall()


def _profiles_by_unit(rows: list[sqlite3.Row]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        unit_code = _clean(item.get("unit_code"))
        if unit_code:
            grouped.setdefault(unit_code, []).append(item)
    return grouped


def _qualification_profiles_by_unit(
    conn: sqlite3.Connection,
    unit_codes: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not unit_codes:
        return {}
    rows = conn.execute(
        """
        SELECT
            l.*, qi.jm_nm, qi.exam_insti_nm,
            cu.unit_name_raw AS unit_name,
            c.major_code, c.major_name,
            c.middle_code, c.middle_name,
            c.small_code, c.small_name,
            c.sub_code, c.sub_name
        FROM ncs_unit_qualification_links l
        JOIN ncs_qualification_items qi ON qi.jm_cd = l.jm_cd
        LEFT JOIN competency_units cu ON cu.unit_code = l.unit_code
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE l.unit_code IN (SELECT value FROM json_each(?))
        ORDER BY
            CASE WHEN l.ablt_unit_typ_cd = 'MAND' THEN 0 ELSE 1 END,
            qi.jm_nm,
            l.unit_code
        """,
        (json.dumps(sorted(unit_codes)),),
    ).fetchall()
    return _profiles_by_unit(rows)


def _job_base_profiles_by_unit(
    conn: sqlite3.Connection,
    unit_codes: set[str],
) -> dict[str, list[dict[str, Any]]]:
    if not unit_codes:
        return {}
    rows = conn.execute(
        """
        SELECT
            l.link_id, l.unit_code, l.job_base_competency_id, l.job_base_factor_id,
            c.competency_name, f.factor_name,
            COUNT(*) OVER (PARTITION BY l.job_base_competency_id, l.job_base_factor_id) AS scope_frequency
        FROM ncs_unit_job_base_links l
        JOIN ncs_job_base_competencies c
          ON c.job_base_competency_id = l.job_base_competency_id
        LEFT JOIN ncs_job_base_factors f
          ON f.job_base_factor_id = l.job_base_factor_id
        WHERE l.unit_code IN (SELECT value FROM json_each(?))
        ORDER BY c.competency_name, f.factor_name, l.unit_code
        """,
        (json.dumps(sorted(unit_codes)),),
    ).fetchall()
    return _profiles_by_unit(rows)


def _profiles_for_unit_codes(
    profiles_by_unit: dict[str, list[dict[str, Any]]],
    unit_codes: set[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for unit_code in sorted(code for code in unit_codes if code):
        for item in profiles_by_unit.get(unit_code, []):
            key = (unit_code, str(item.get("link_id") or item.get("jm_cd") or item.get("job_base_competency_id") or ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
            if len(rows) >= limit:
                return rows
    return rows


def get_training_course(
    conn: sqlite3.Connection,
    training_course_id: int,
    *,
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
) -> dict[str, Any]:
    course_columns = ", ".join(PUBLIC_TRAINING_COURSE_FIELDS)
    row = conn.execute(
        f"SELECT {course_columns} FROM ncs_training_courses WHERE training_course_id = ?",
        (training_course_id,),
    ).fetchone()
    if not row:
        return not_found_response(f"훈련과정을 찾을 수 없습니다: {training_course_id}")
    return {"ok": True, **_course_payload(conn, row, link_limit=link_limit)}


def build_training_course_ontology_links(
    conn: sqlite3.Connection,
    *,
    major_code: str | None = None,
    reset: bool = False,
) -> dict[str, Any]:
    params: list[Any] = []
    scope_clause = ""
    if major_code:
        scope_clause = " WHERE ncs_lclas_cd = ?"
        params.append(major_code)
    if reset:
        if major_code:
            scoped_course_ids = [
                int(row["training_course_id"])
                for row in conn.execute(
                    "SELECT training_course_id FROM ncs_training_courses WHERE ncs_lclas_cd = ?",
                    (major_code,),
                ).fetchall()
            ]
            if scoped_course_ids:
                placeholders = ",".join("?" for _ in scoped_course_ids)
                for table in (
                    "ncs_training_course_concept_links",
                    "ncs_training_course_element_links",
                    "training_goal_concept_links",
                    "training_delivery_relations",
                    "ncs_training_course_unit_links",
                ):
                    conn.execute(
                        f"DELETE FROM {table} WHERE training_course_id IN ({placeholders})",
                        scoped_course_ids,
                    )
        else:
            for table in (
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
                "ncs_training_course_unit_links",
            ):
                conn.execute(f"DELETE FROM {table}")
    before_links = int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_concept_links").fetchone()[0])
    before_elements = int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_element_links").fetchone()[0])
    before_goals = int(conn.execute("SELECT COUNT(*) FROM training_goal_concept_links").fetchone()[0])
    before_delivery = int(conn.execute("SELECT COUNT(*) FROM training_delivery_relations").fetchone()[0])
    timestamp = now_utc()
    courses = conn.execute(
        f"SELECT * FROM ncs_training_courses{scope_clause} ORDER BY training_course_id",
        params,
    ).fetchall()
    for course in courses:
        course_id = course["training_course_id"]
        unit_code = _clean(course["ncs_cl_cd"])
        unit_exists = conn.execute("SELECT 1 FROM competency_units WHERE unit_code = ?", (unit_code,)).fetchone()
        if unit_exists:
            conn.execute(
                """
                INSERT OR IGNORE INTO ncs_training_course_unit_links(
                    training_course_id, unit_code, link_method, confidence_score,
                    review_status, created_at, updated_at
                ) VALUES (?, ?, 'ncs_cl_cd_exact', 1.0, 'auto_linked', ?, ?)
                """,
                (course_id, unit_code, timestamp, timestamp),
            )
            element_rows = conn.execute(
                "SELECT element_id, element_name_raw FROM competency_elements WHERE unit_code = ?",
                (unit_code,),
            ).fetchall()
            for element in element_rows:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ncs_training_course_element_links(
                        training_course_id, unit_code, element_id, link_method,
                        confidence_score, evidence_text, review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'unit_element_coverage', 1.0, ?, 'auto_linked', ?, ?)
                    """,
                    (course_id, unit_code, element["element_id"], element["element_name_raw"], timestamp, timestamp),
                )
            concepts = _scope_concepts(conn, {unit_code}, limit=500)
            goal = _clean(course["train_goal"])
            for concept in concepts:
                concept_id = concept["concept_id"]
                concept_name = _clean(concept["concept_name"])
                conn.execute(
                    """
                    INSERT OR IGNORE INTO ncs_training_course_concept_links(
                        training_course_id, unit_code, concept_id, link_method,
                        confidence_score, evidence_text, review_status, created_at, updated_at
                    ) VALUES (?, ?, ?, 'unit_ksa_concept_inherited', ?, ?, 'auto_linked', ?, ?)
                    """,
                    (
                        course_id,
                        unit_code,
                        concept_id,
                        MATCH_BASIS_WEIGHTS["unit_ksa_concept_inherited"],
                        concept_name,
                        timestamp,
                        timestamp,
                    ),
                )
                exact = _contains(goal, concept_name)
                token_hits = [token for token in _significant_tokens(concept_name) if token.lower() in goal.lower()]
                if exact:
                    method = "training_goal_concept_text"
                    confidence = MATCH_BASIS_WEIGHTS["training_goal_concept_text"]
                elif token_hits:
                    method = "training_goal_concept_token"
                    confidence = MATCH_BASIS_WEIGHTS["training_goal_concept_token"]
                else:
                    continue
                conn.execute(
                    """
                    INSERT OR IGNORE INTO training_goal_concept_links(
                        training_course_id, unit_code, element_id, concept_id, link_method,
                        confidence_score, evidence_text, review_status, created_at, updated_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, 'auto_linked', ?, ?)
                    """,
                    (course_id, unit_code, concept_id, method, confidence, goal or concept_name, timestamp, timestamp),
                )
        for relation in _course_delivery_relations(course):
            conn.execute(
                """
                INSERT OR IGNORE INTO training_delivery_relations(
                    training_course_id, relation_type, relation_value, normalized_value,
                    numeric_value, evidence_text, confidence_score, review_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'auto_linked', ?, ?)
                """,
                (
                    course_id,
                    relation["relation_type"],
                    relation["relation_value"],
                    relation["normalized_value"],
                    relation["numeric_value"],
                    relation["evidence_text"],
                    relation["confidence_score"],
                    timestamp,
                    timestamp,
                ),
            )
    conn.commit()
    return {
        "ok": True,
        "scope": {"major_code": major_code},
        "reset": reset,
        "courses_processed": len(courses),
        "links_before": before_links,
        "links_after": int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_concept_links").fetchone()[0]),
        "element_links_before": before_elements,
        "element_links_after": int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_element_links").fetchone()[0]),
        "goal_concept_links_before": before_goals,
        "goal_concept_links_after": int(conn.execute("SELECT COUNT(*) FROM training_goal_concept_links").fetchone()[0]),
        "delivery_relations_before": before_delivery,
        "delivery_relations_after": int(conn.execute("SELECT COUNT(*) FROM training_delivery_relations").fetchone()[0]),
    }


def search_training_courses(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    major_code: str | None = None,
    unit_code: str | None = None,
    concept_query: str | None = None,
    limit: int = 20,
    link_limit: int = DEFAULT_COURSE_LINK_LIMIT,
    compact: bool = False,
) -> list[dict[str, Any]]:
    max_rows = clamp_limit(limit, default=20, maximum=100)
    clauses: list[str] = []
    params: list[Any] = []
    if query:
        clauses.append("(tc.compe_unit_name LIKE ? OR tc.train_goal LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if major_code:
        clauses.append("tc.ncs_lclas_cd = ?")
        params.append(major_code)
    if unit_code:
        clauses.append("tc.ncs_cl_cd = ?")
        params.append(unit_code)
    if concept_query:
        clauses.append(
            """
            EXISTS (
                SELECT 1
                FROM ncs_training_course_concept_links l
                JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
                WHERE l.training_course_id = tc.training_course_id
                  AND oc.concept_name LIKE ?
            )
            """
        )
        params.append(f"%{concept_query}%")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    if compact:
        course_columns = ", ".join(f"tc.{field}" for field in SUMMARY_TRAINING_COURSE_FIELDS)
        rows = conn.execute(
            f"""
            SELECT {course_columns},
                   (SELECT COUNT(*) FROM ncs_training_course_unit_links l
                    WHERE l.training_course_id = tc.training_course_id) AS unit_link_count,
                   (SELECT COUNT(*) FROM ncs_training_course_concept_links l
                    WHERE l.training_course_id = tc.training_course_id) AS concept_link_count,
                   (SELECT COUNT(*) FROM ncs_training_course_element_links l
                    WHERE l.training_course_id = tc.training_course_id) AS element_link_count,
                   (SELECT COUNT(*) FROM training_goal_concept_links l
                    WHERE l.training_course_id = tc.training_course_id) AS goal_link_count,
                   (SELECT COUNT(*) FROM training_delivery_relations l
                    WHERE l.training_course_id = tc.training_course_id) AS delivery_relation_count
            FROM ncs_training_courses tc
            {where}
            ORDER BY tc.training_course_id
            LIMIT ?
            """,
            (*params, max_rows),
        ).fetchall()
        return [_course_summary_payload(row) for row in rows]

    course_columns = ", ".join(f"tc.{field}" for field in PUBLIC_TRAINING_COURSE_FIELDS)
    rows = conn.execute(
        f"""
        SELECT {course_columns}
        FROM ncs_training_courses tc
        {where}
        ORDER BY tc.training_course_id
        LIMIT ?
        """,
        (*params, max_rows),
    ).fetchall()
    return [_course_payload(conn, row, link_limit=link_limit) for row in rows]


def _qualification_key(item: dict[str, Any]) -> str:
    return ":".join(
        _clean(item.get(key))
        for key in ("jm_cd", "organ_std_ver_cd", "ablt_unit_typ_cd", "min_edu_trng_tm")
        if _clean(item.get(key))
    )


def _job_base_key(item: dict[str, Any]) -> str:
    return f"{item.get('job_base_competency_id')}:{item.get('job_base_factor_id')}"


def _qualification_label(item: dict[str, Any]) -> str:
    name = _clean(item.get("jm_nm")) or _clean(item.get("jm_cd"))
    typ = _clean(item.get("ablt_unit_typ_nm"))
    return f"{name}({typ})" if typ else name


def _job_base_label(item: dict[str, Any]) -> str:
    comp = _clean(item.get("competency_name"))
    factor = _clean(item.get("factor_name"))
    return f"{comp}:{factor}" if factor else comp


def _filter_qualifications(profile: list[dict[str, Any]], keys: set[str], *, limit: int) -> list[dict[str, Any]]:
    return [item for item in profile if _qualification_key(item) in keys][:limit]


def _filter_job_base(profile: list[dict[str, Any]], keys: set[str], *, limit: int) -> list[dict[str, Any]]:
    return [item for item in profile if _job_base_key(item) in keys][:limit]


def _job_base_hit_labels(
    job_base_links: list[dict[str, Any]],
    keys: set[str],
    *,
    limit: int = 5,
) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for item in job_base_links:
        key = _job_base_key(item)
        if key not in keys:
            continue
        label = _job_base_label(item)
        if not label or label in seen:
            continue
        labels.append(label)
        seen.add(label)
        if len(labels) >= limit:
            break
    return labels


def _job_base_transition_profile(result: dict[str, Any], *, limit: int = 8) -> dict[str, Any]:
    transition = result.get("transition") if isinstance(result.get("transition"), dict) else {}
    current_profile = [
        item
        for item in result.get("current_job_base_profile")
        or transition.get("current_job_base_profile")
        or []
        if isinstance(item, dict)
    ]
    target_profile = [
        item
        for item in result.get("target_job_base_profile")
        or transition.get("target_job_base_profile")
        or []
        if isinstance(item, dict)
    ]
    current_keys = {_job_base_key(item) for item in current_profile if _job_base_key(item)}
    target_keys = {_job_base_key(item) for item in target_profile if _job_base_key(item)}
    shared_keys = current_keys & target_keys
    gap_keys = target_keys - current_keys
    summary = transition.get("summary") or {}
    current_count = summary.get("current_job_base_count")
    target_count = summary.get("target_job_base_count")
    transferable_count = summary.get("transferable_job_base_count")
    gap_count = summary.get("gap_job_base_count")
    current_count = len(current_keys) if current_count is None else int(current_count)
    target_count = len(target_keys) if target_count is None else int(target_count)
    transferable_count = len(shared_keys) if transferable_count is None else int(transferable_count)
    gap_count = len(gap_keys) if gap_count is None else int(gap_count)
    if gap_count <= 0:
        gap_keys = set()
    gap_labels = _job_base_hit_labels(target_profile, gap_keys, limit=limit)
    gap_labels_unavailable = gap_count > 0 and not gap_labels
    if gap_labels:
        gap_label_status = "available"
    elif gap_labels_unavailable and not target_profile:
        gap_label_status = "summary_only_labels_unavailable"
    elif gap_labels_unavailable:
        gap_label_status = "gap_labels_unavailable"
    else:
        gap_label_status = "not_applicable"
    return {
        "schema": "ncs_job_base_transition_profile_v1",
        "evidence_role": "supporting_gap_context",
        "scoring_role": "auxiliary_tie_breaker_not_primary_evidence",
        "profile_source": "profile_rows" if current_profile or target_profile else "summary_only",
        "current_count": current_count,
        "target_count": target_count,
        "transferable_count": transferable_count,
        "gap_count": gap_count,
        "transferable": _job_base_hit_labels(target_profile, shared_keys, limit=limit),
        "gaps": gap_labels,
        "gap_label_status": gap_label_status,
        "labels_unavailable": gap_labels_unavailable,
        "review_required": gap_count > 0,
        "db_writes": False,
    }


def _job_base_signal(
    job_base_links: list[dict[str, Any]],
    *,
    target_job_base_keys: set[str],
    gap_job_base_keys: set[str],
    target_job_base_hits: set[str],
    gap_job_base_hits: set[str],
) -> dict[str, Any]:
    target_signal_count = len(target_job_base_keys)
    gap_signal_count = len(gap_job_base_keys)
    status = "not_available"
    score_cap = 0.0
    if gap_job_base_hits:
        status = "gap_bridge"
        score_cap = 0.045
    elif target_job_base_hits:
        status = "target_scope_signal"
        score_cap = 0.02
    elif target_signal_count or gap_signal_count:
        status = "no_course_match"
    return {
        "status": status,
        "evidence_role": JOB_BASE_EVIDENCE_ROLE,
        "target_hit_count": len(target_job_base_hits),
        "gap_hit_count": len(gap_job_base_hits),
        "target_signal_count": target_signal_count,
        "gap_signal_count": gap_signal_count,
        "target_hit_ratio": round(len(target_job_base_hits) / target_signal_count, 4)
        if target_signal_count
        else 0.0,
        "gap_hit_ratio": round(len(gap_job_base_hits) / gap_signal_count, 4)
        if gap_signal_count
        else 0.0,
        "score_cap": score_cap,
        "matched_target_labels": _job_base_hit_labels(job_base_links, target_job_base_hits),
        "matched_gap_labels": _job_base_hit_labels(job_base_links, gap_job_base_hits),
    }


def _score_component_highlights(components: dict[str, Any]) -> list[dict[str, Any]]:
    labels = {
        "unit_score": "능력단위",
        "element_score": "능력단위요소",
        "training_goal_ksa_score": "훈련목표 KSA",
        "source_ksa_score": "목표 범위 KSA",
        "gap_ksa_score": "보완 KSA",
        "career_path_score": "경력경로",
        "time_score": "훈련시간",
        "preference_score": "사용자 선호",
        "qualification_score": "자격 신호",
        "job_base_score": "직업기초능력",
    }
    highlights = [
        {"component": key, "label": label, "score": round(float(components.get(key) or 0.0), 4)}
        for key, label in labels.items()
        if float(components.get(key) or 0.0) > 0
    ]
    return sorted(highlights, key=lambda item: item["score"], reverse=True)[:5]


def _evidence_strength(match: dict[str, Any], score: float) -> dict[str, str]:
    if score >= 0.75 and (match.get("direct_unit_evidence") or match.get("goal_concept_hits")):
        return {"grade": "high", "label": "strong_direct_evidence"}
    if score >= 0.5:
        return {"grade": "medium", "label": "usable_supporting_evidence"}
    return {"grade": "low", "label": "weak_evidence"}


def _training_sequence_hint(
    *,
    course_name: str,
    target_label: str | None,
    match: dict[str, Any],
    course_level: Any,
) -> dict[str, Any]:
    if match.get("current_scope_already_covered"):
        return {"stage": 3, "role": "current_foundation_refresh", "rationale": "현재 직무 기반 역량 확인"}
    if match.get("direct_unit_evidence"):
        return {"stage": 1, "role": "direct_target_coverage", "rationale": "목표 능력단위 직접 보완"}
    if match.get("gap_concept_hits"):
        return {"stage": 2, "role": "gap_bridge", "rationale": "부족 KSA 보완"}
    return {"stage": 3, "role": "adjacent_reference", "rationale": "인접 과정 참고"}


def _training_sequence_plan(recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stages: dict[int, dict[str, Any]] = {}
    for item in recommendations:
        seq = item.get("training_sequence") or {}
        stage = int(seq.get("stage") or 3)
        stages.setdefault(
            stage,
            {
                "stage": stage,
                "role": seq.get("role"),
                "courses": [],
            },
        )
        stages[stage]["courses"].append(item["training_course"]["compe_unit_name"])
    return [stages[key] for key in sorted(stages)]


def _group_card(item: dict[str, Any]) -> dict[str, Any]:
    course = item.get("training_course") or {}
    match = dict(item.get("match") or {})
    quality_penalty = _quality_issue_penalty_summary(match)
    if quality_penalty:
        quality_penalty["affected_concepts"] = _quality_issue_affected_concepts(item, quality_penalty)
        match["quality_issue_penalty"] = {
            **dict(match.get("quality_issue_penalty") or {}),
            "affected_concepts": quality_penalty["affected_concepts"],
        }
    tier = item.get("recommendation_tier") or {}
    card = {
        "rank": item.get("rank"),
        "course_name": course.get("compe_unit_name"),
        "training_course_id": course.get("training_course_id"),
        "confidence_score": item.get("confidence_score"),
        "confidence_grade": item.get("confidence_grade"),
        "tier": tier.get("tier"),
        "label": tier.get("label"),
        "rationale": tier.get("rationale"),
        "display_tier": item.get("display_tier"),
        "evidence_strength": item.get("evidence_strength"),
        "preference_fit": item.get("preference_fit"),
        "delivery": item.get("delivery_evidence"),
        "coverage_counts": item.get("coverage_counts"),
        "score_component_highlights": item.get("score_component_highlights"),
        "evidence_highlights": item.get("evidence_highlights"),
        "job_base_signal": item.get("job_base_signal"),
        "training_sequence_role": (item.get("training_sequence") or {}).get("role"),
        "supplemental_evidence": item.get("supplemental_evidence") or {},
        "match": match,
    }
    return card


def _recommendation_groups(recommendations: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups = {"primary": [], "supplemental": [], "adjacent": []}
    for item in recommendations:
        key = (item.get("recommendation_tier") or {}).get("tier")
        if key not in groups:
            key = "adjacent"
        groups[key].append(_group_card(item))
    return groups


def _score_course(
    row: sqlite3.Row,
    *,
    scope_unit_codes: set[str],
    source_element_id: int | None,
    source_concept_ids: set[int],
    gap_concept_ids: set[int],
    concept_links: list[dict[str, Any]],
    element_links: list[dict[str, Any]],
    goal_concept_links: list[dict[str, Any]],
    delivery_relations: list[dict[str, Any]],
    career_path_unit_codes: set[str],
    qualification_links: list[dict[str, Any]],
    job_base_links: list[dict[str, Any]],
    target_qualification_keys: set[str],
    gap_qualification_keys: set[str],
    target_job_base_keys: set[str],
    gap_job_base_keys: set[str],
    preferred_max_hours: float | None,
    preferred_methods: list[str] | None,
) -> tuple[float, dict[str, Any]]:
    linked_unit_codes = {link.get("unit_code") for link in concept_links + element_links if link.get("unit_code")}
    linked_unit_codes.update(link.get("unit_code") for link in qualification_links if link.get("unit_code"))
    if row["ncs_cl_cd"]:
        linked_unit_codes.add(row["ncs_cl_cd"])
    direct_unit_evidence = bool(scope_unit_codes & set(linked_unit_codes))
    source_element_covered = bool(
        source_element_id
        and any(int(link.get("element_id") or 0) == source_element_id for link in element_links if _link_is_usable(link))
    )
    usable_concept_links = [link for link in concept_links if _link_is_usable(link)]
    usable_goal_links = [link for link in goal_concept_links if _link_is_usable(link)]
    linked_concept_ids = {int(link.get("concept_id") or 0) for link in usable_concept_links}
    goal_concept_ids = {int(link.get("concept_id") or 0) for link in usable_goal_links}
    source_hits = linked_concept_ids & source_concept_ids
    gap_hits = linked_concept_ids & gap_concept_ids
    goal_hits = goal_concept_ids & (source_concept_ids | gap_concept_ids)
    goal_direct_hits = {
        int(link.get("concept_id") or 0)
        for link in usable_goal_links
        if link.get("link_method") == "training_goal_concept_text"
    } & (source_concept_ids | gap_concept_ids)
    goal_token_hits = {
        int(link.get("concept_id") or 0)
        for link in usable_goal_links
        if link.get("link_method") == "training_goal_concept_token"
    } & (source_concept_ids | gap_concept_ids)
    components = {
        "unit_score": 0.0,
        "element_score": 0.0,
        "training_goal_ksa_score": 0.0,
        "source_ksa_score": 0.0,
        "gap_ksa_score": 0.0,
        "career_path_score": 0.0,
        "time_score": 0.0,
        "preference_score": 0.0,
        "qualification_score": 0.0,
        "job_base_score": 0.0,
        "penalty_score": 0.0,
    }
    reasons: list[str] = []
    if direct_unit_evidence:
        components["unit_score"] += 0.38
        reasons.extend(["resolved_scope_unit_link", "transition_unit_link"])
    if source_element_covered:
        components["element_score"] += 0.16
        reasons.append("source_element_coverage")
    if goal_hits:
        weighted = 0.0
        for link in usable_goal_links:
            cid = int(link.get("concept_id") or 0)
            if cid not in goal_hits:
                continue
            method = link.get("link_method") or ""
            weighted += float(link.get("confidence_score") or MATCH_BASIS_WEIGHTS.get(method, 0.4)) * _review_weight(link)
        components["training_goal_ksa_score"] += min(0.32, 0.09 * weighted)
        reasons.append("training_goal_ksa_coverage")
        if goal_direct_hits:
            reasons.append("training_goal_direct_ksa_coverage")
        if goal_token_hits:
            reasons.append("training_goal_token_ksa_coverage")
        if any(
            link.get("review_status") in {"human_reviewed", "reviewed"}
            and int(link.get("concept_id") or 0) in goal_hits
            for link in usable_goal_links
        ):
            reasons.append("reviewed_training_goal_ksa_coverage")
    if source_hits:
        components["source_ksa_score"] += min(0.08, 0.025 * len(source_hits))
        reasons.append("source_ksa_concept_overlap")
    if gap_hits:
        components["gap_ksa_score"] += min(0.09, 0.035 * len(gap_hits))
        reasons.append("gap_ksa_concept_coverage")
    if row["ncs_cl_cd"] in career_path_unit_codes:
        components["career_path_score"] += 0.05
        reasons.append("career_path_unit_link")
    hours = _parse_number(row["train_time"])
    delivery_methods = [item.get("relation_value") for item in delivery_relations if item.get("relation_type") == "delivered_by"]
    preference_fit = _preference_fit_profile(
        delivery_methods=delivery_methods,
        requested_methods=preferred_methods,
        hours=hours,
        preferred_max_hours=preferred_max_hours,
    )
    if preference_fit["time_fit"] == "fit":
        components["time_score"] += 0.04
        reasons.append("preferred_time_fit")
    elif preference_fit["time_fit"] == "over":
        components["penalty_score"] += preference_fit["time_score_adjustment"]
        reasons.append("preferred_time_over")
    if preferred_methods and preference_fit["method_fit"]:
        components["preference_score"] += 0.03
        reasons.append("preferred_method_fit")
    qualification_keys = {_qualification_key(item) for item in qualification_links if _qualification_key(item)}
    target_qualification_hits = qualification_keys & target_qualification_keys
    gap_qualification_hits = qualification_keys & gap_qualification_keys
    if gap_qualification_hits:
        components["qualification_score"] += min(0.06, 0.03 + 0.01 * len(gap_qualification_hits))
        reasons.append("gap_qualification_bridge")
    elif target_qualification_hits:
        components["qualification_score"] += min(0.03, 0.01 + 0.005 * len(target_qualification_hits))
        reasons.append("target_qualification_signal")
    job_base_keys = {_job_base_key(item) for item in job_base_links if _job_base_key(item)}
    target_job_base_hits = job_base_keys & target_job_base_keys
    gap_job_base_hits = job_base_keys & gap_job_base_keys
    job_base_signal = _job_base_signal(
        job_base_links,
        target_job_base_keys=target_job_base_keys,
        gap_job_base_keys=gap_job_base_keys,
        target_job_base_hits=target_job_base_hits,
        gap_job_base_hits=gap_job_base_hits,
    )
    if gap_job_base_hits:
        components["job_base_score"] += min(0.045, 0.015 + 0.004 * len(gap_job_base_hits))
        reasons.append("gap_job_base_bridge")
    elif target_job_base_hits:
        components["job_base_score"] += min(0.02, 0.004 * len(target_job_base_hits))
        reasons.append("target_job_base_signal")
    if not direct_unit_evidence and not goal_hits:
        return 0.0, {"reasons": reasons, "score_components": components, "job_base_signal": job_base_signal}
    final_score = max(0.0, min(1.0, sum(components.values())))
    components["final_score"] = round(final_score, 4)
    return final_score, {
        "reasons": sorted(set(reasons)),
        "direct_unit_evidence": direct_unit_evidence,
        "source_element_covered": source_element_covered,
        "source_concept_hits": len(source_hits),
        "gap_concept_hits": len(gap_hits),
        "goal_concept_hits": len(goal_hits),
        "goal_direct_concept_hits": len(goal_direct_hits),
        "goal_token_concept_hits": len(goal_token_hits),
        "goal_review_counts": {
            "human_reviewed": sum(1 for link in usable_goal_links if link.get("review_status") == "human_reviewed" and int(link.get("concept_id") or 0) in goal_hits),
            "reviewed": sum(1 for link in usable_goal_links if link.get("review_status") == "reviewed" and int(link.get("concept_id") or 0) in goal_hits),
        },
        "weighted_goal_score": round(sum(float(link.get("confidence_score") or 0.0) for link in usable_goal_links if int(link.get("concept_id") or 0) in goal_hits), 4),
        "career_path_unit_hits": sorted(career_path_unit_codes & set(linked_unit_codes)),
        "qualification_hits": sorted(target_qualification_hits),
        "gap_qualification_hits": sorted(gap_qualification_hits),
        "job_base_hits": sorted(target_job_base_hits),
        "gap_job_base_hits": sorted(gap_job_base_hits),
        "job_base_signal": job_base_signal,
        "score_components": components,
        "preference_fit": preference_fit,
        "source_hit_ids": sorted(source_hits),
        "gap_hit_ids": sorted(gap_hits),
        "goal_hit_ids": sorted(goal_hits),
    }


def _save_recommendation(
    conn: sqlite3.Connection,
    *,
    query: str,
    source: dict[str, Any],
    summary: dict[str, Any],
    audit: dict[str, Any],
    recommendations: list[dict[str, Any]],
) -> int | None:
    timestamp = now_utc()
    conn.execute(
        """
        INSERT INTO education_recommendation_runs(
            query, target_source_key, request_payload, target_payload,
            summary_payload, audit_payload, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            query or "",
            str(source.get("criteria_id") or source.get("unit_code") or ""),
            _json({"query": query}),
            _json(source),
            _json(summary),
            _json(audit),
            timestamp,
        ),
    )
    run_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    for item in recommendations:
        course = item["training_course"]
        conn.execute(
            """
            INSERT INTO education_recommendation_items(
                run_id, rank, learn_module_seq, learn_module_name,
                recommendation_payload, confidence_score, confidence_grade, created_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item["rank"],
                course.get("compe_unit_name"),
                _json(item),
                item.get("confidence_score") or 0.0,
                item.get("confidence_grade") or "insufficient",
                timestamp,
            ),
        )
        item_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO education_recommendation_evidence(
                run_id, item_id, evidence_type, source_table, source_id,
                unit_code, evidence_text, evidence_summary, confidence_score, created_at
            ) VALUES (?, ?, 'training_course', 'ncs_training_courses', ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                item_id,
                str(course.get("training_course_id")),
                course.get("ncs_cl_cd"),
                course.get("train_goal"),
                course.get("compe_unit_name"),
                item.get("confidence_score"),
                timestamp,
            ),
        )
        for qual in item.get("qualification_evidence", []):
            conn.execute(
                """
                INSERT INTO education_recommendation_evidence(
                    run_id, item_id, evidence_type, source_table, source_id,
                    unit_code, evidence_text, evidence_summary, confidence_score, created_at
                ) VALUES (?, ?, 'related_qualification', 'ncs_unit_qualification_links', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item_id,
                    str(qual.get("link_id") or qual.get("jm_cd") or ""),
                    qual.get("unit_code"),
                    _qualification_label(qual),
                    qual.get("jm_nm"),
                    qual.get("confidence_score"),
                    timestamp,
                ),
            )
        for job in item.get("job_base_evidence", []):
            conn.execute(
                """
                INSERT INTO education_recommendation_evidence(
                    run_id, item_id, evidence_type, source_table, source_id,
                    unit_code, evidence_text, evidence_summary, confidence_score, created_at
                ) VALUES (?, ?, 'job_base_competency', 'ncs_unit_job_base_links', ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item_id,
                    str(job.get("link_id") or ""),
                    job.get("unit_code"),
                    _job_base_label(job),
                    job.get("competency_name"),
                    job.get("confidence_score"),
                    timestamp,
                ),
            )
    conn.commit()
    return run_id


def recommend_training_for_task(
    conn: sqlite3.Connection,
    *,
    criteria_id: int | None = None,
    unit_code: str | None = None,
    query: str | None = None,
    major_code: str | None = None,
    middle_code: str | None = None,
    small_code: str | None = None,
    sub_code: str | None = None,
    mode: str = "all",
    current_concepts: list[str] | None = None,
    extra_support_course_weights: dict[str, float] | None = None,
    sequence_target_label: str | None = None,
    target_qualification_keys: set[str] | None = None,
    gap_qualification_keys: set[str] | None = None,
    target_job_base_keys: set[str] | None = None,
    gap_job_base_keys: set[str] | None = None,
    already_covered_unit_codes: set[str] | None = None,
    preferred_max_hours: float | None = None,
    preferred_methods: list[str] | None = None,
    precomputed_query_resolution: dict[str, Any] | None = None,
    limit: int = DEFAULT_RECOMMENDATIONS,
    save: bool = True,
) -> dict[str, Any]:
    requested_query = query
    query = _clean(query) or None
    unit_code = _clean(unit_code) or None
    if criteria_id is None and not unit_code and not query:
        return {
            "ok": False,
            "error": {"code": "missing_task_locator", "message": "criteria_id, unit_code, or query is required."},
        }
    query_resolution = precomputed_query_resolution or resolve_ncs_query_scope(
        conn,
        query or unit_code or str(criteria_id or ""),
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
        limit=12,
    )
    if query is not None and len(query) < 2:
        return {
            "ok": False,
            "requested_query": requested_query,
            "query_resolution": query_resolution,
            "input_quality": {
                "ok": False,
                "warnings": [{"code": "short_query", "field": "query", "message": "질의가 너무 짧습니다."}],
                "suggestions": _candidate_suggestions(query_resolution, field="query"),
                "candidate_queries": {"query": _candidate_query_items(query_resolution)},
            },
                "error": {"code": "low_quality_query"},
        }
    requested_filters = {
        "major_code": major_code,
        "middle_code": middle_code,
        "small_code": small_code,
        "sub_code": sub_code,
    }
    query, major_code, middle_code, small_code, sub_code, alias = _apply_query_alias(
        conn,
        query,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    alias_unit_code = _clean(alias.get("unit_code")) if alias else None
    alias_guard = _alias_unit_conflicts_with_exact_unit(
        conn,
        requested_query=_clean(requested_query),
        alias=alias,
        **requested_filters,
    )
    if alias_guard:
        if alias:
            alias = {**alias, "ignored_unit_code": alias_unit_code, "ignore_guard": alias_guard}
        alias_unit_code = None
        query = _clean(requested_query) or query
        major_code = requested_filters["major_code"]
        middle_code = requested_filters["middle_code"]
        small_code = requested_filters["small_code"]
        sub_code = requested_filters["sub_code"]
    if alias and not unit_code:
        unit_code = alias_unit_code or unit_code
    query_filters = _resolution_classification_filters(
        query_resolution,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    major_code = query_filters["major_code"]
    middle_code = query_filters["middle_code"]
    small_code = query_filters["small_code"]
    sub_code = query_filters["sub_code"]
    source_query = None if _resolution_classification_scope_candidate(query_resolution) else query
    source = resolve_task_criteria(
        conn,
        criteria_id=criteria_id,
        query=None if alias_unit_code else source_query,
        unit_code=unit_code,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if source is None:
        suggestions = [item["query"] for item in _candidate_query_items(query_resolution)]
        response = not_found_response("NCS 과업 범위를 찾을 수 없습니다.", suggestions=suggestions)
        response["requested_query"] = requested_query
        response["query_resolution"] = query_resolution
        return response
    max_items = clamp_limit(limit, default=DEFAULT_RECOMMENDATIONS, maximum=MAX_RECOMMENDATIONS)
    resolved_scope = _resolve_query_scope_units(
        conn,
        query=query,
        source=source,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    scope_unit_codes = set(resolved_scope["unit_codes"])
    source_task_concepts = _task_concepts(conn, int(source["criteria_id"]), limit=200)
    scope_profile_concepts = _scope_concepts(conn, scope_unit_codes, limit=300)
    source_concepts = _dedupe_dicts(source_task_concepts + scope_profile_concepts, "concept_id", limit=300)
    current_keys = {normalize_concept_key(item) for item in (current_concepts or [])}
    gap_concepts = [
        concept
        for concept in source_concepts
        if current_keys and normalize_concept_key(concept.get("concept_name")) not in current_keys
    ]
    source_concept_ids = {_concept_key(item) for item in source_concepts}
    gap_concept_ids = {_concept_key(item) for item in gap_concepts}
    scope_career_paths = career_paths_for_units(conn, scope_unit_codes, limit=300)
    trusted_scope_career_paths = career_paths_for_units(
        conn,
        scope_unit_codes,
        limit=300,
        review_statuses=TRUSTED_CAREER_PATH_REVIEW_STATUSES,
    )
    career_path_unit_codes = {
        _clean(item.get("matched_unit_code"))
        for item in trusted_scope_career_paths
        if _clean(item.get("matched_unit_code"))
    }
    scope_qualification_profile = qualification_profile_for_units(conn, scope_unit_codes, limit=500)
    scope_job_base_profile = job_base_profile_for_units(conn, scope_unit_codes, limit=500)
    effective_target_qualification_keys = target_qualification_keys or {_qualification_key(item) for item in scope_qualification_profile if _qualification_key(item)}
    effective_gap_qualification_keys = gap_qualification_keys or set()
    effective_target_job_base_keys = target_job_base_keys or {_job_base_key(item) for item in scope_job_base_profile if _job_base_key(item)}
    effective_gap_job_base_keys = gap_job_base_keys or set()
    covered_unit_codes = {code for code in (already_covered_unit_codes or set()) if code}
    course_rows = _candidate_training_course_rows(
        conn,
        scope_unit_codes=scope_unit_codes,
        target_concept_ids=source_concept_ids | gap_concept_ids,
        support_course_names=set((extra_support_course_weights or {}).keys()),
    )
    course_payloads = _course_payloads_by_id(conn, course_rows)
    course_unit_codes_by_id: dict[int, set[str]] = {}
    candidate_unit_codes: set[str] = set()
    for row in course_rows:
        cid = int(row["training_course_id"])
        payload = course_payloads.get(cid, {})
        linked_unit_codes = {
            _clean(row["ncs_cl_cd"]),
            *[
                _clean(link.get("unit_code"))
                for link in payload.get("unit_links", [])
                if _clean(link.get("unit_code"))
            ],
        }
        linked_unit_codes.update(
            _clean(link.get("unit_code"))
            for link in payload.get("concept_links", []) + payload.get("element_links", [])
            if _clean(link.get("unit_code"))
        )
        course_unit_codes_by_id[cid] = {code for code in linked_unit_codes if code}
        candidate_unit_codes.update(course_unit_codes_by_id[cid])
    qualification_profiles_by_unit = _qualification_profiles_by_unit(conn, candidate_unit_codes)
    job_base_profiles_by_unit = _job_base_profiles_by_unit(conn, candidate_unit_codes)
    quality_penalty_by_concept = _concept_quality_issue_penalty_map(
        conn,
        source_concept_ids | gap_concept_ids,
    )
    candidates: list[dict[str, Any]] = []
    for row in course_rows:
        cid = int(row["training_course_id"])
        payload = course_payloads[cid]
        linked_unit_codes = course_unit_codes_by_id[cid]
        qualification_links = _profiles_for_unit_codes(qualification_profiles_by_unit, linked_unit_codes, limit=100)
        job_base_links = _profiles_for_unit_codes(job_base_profiles_by_unit, linked_unit_codes, limit=100)
        course_scope_fit = _course_scope_fit(
            row,
            resolved_scope,
            linked_unit_codes=linked_unit_codes,
            scope_unit_codes=scope_unit_codes,
        )
        score, match = _score_course(
            row,
            scope_unit_codes=scope_unit_codes,
            source_element_id=source.get("element_id"),
            source_concept_ids=source_concept_ids,
            gap_concept_ids=gap_concept_ids,
            concept_links=payload["concept_links"],
            element_links=payload["element_links"],
            goal_concept_links=payload["goal_concept_links"],
            delivery_relations=payload["delivery_relations"],
            career_path_unit_codes=career_path_unit_codes,
            qualification_links=qualification_links,
            job_base_links=job_base_links,
            target_qualification_keys=effective_target_qualification_keys,
            gap_qualification_keys=effective_gap_qualification_keys,
            target_job_base_keys=effective_target_job_base_keys,
            gap_job_base_keys=effective_gap_job_base_keys,
            preferred_max_hours=preferred_max_hours,
            preferred_methods=preferred_methods,
        )
        match["course_scope_fit"] = course_scope_fit
        scope_relation = course_scope_fit.get("relation")
        if scope_relation == "direct_scope_unit":
            match.setdefault("reasons", []).append("course_scope_direct_unit")
        elif scope_relation in {"same_sub_classification", "same_small_classification"}:
            match.setdefault("reasons", []).append("course_scope_near_classification")
        elif scope_relation in {"same_middle_classification", "same_major_classification"}:
            match.setdefault("reasons", []).append("course_scope_adjacent_classification")
        else:
            match.setdefault("reasons", []).append("course_scope_distant_classification")
        if _is_distant_scope_concept_only_candidate(match):
            penalty = {
                "same_middle_classification": 0.06,
                "same_major_classification": 0.12,
                "different_classification": 0.24,
                "course_scope_unknown": 0.18,
                "target_scope_unknown": 0.12,
            }.get(scope_relation, 0.1)
            score = max(0.0, score - penalty)
            match.setdefault("reasons", []).append("distant_scope_concept_only_penalty")
            match["distant_scope_concept_only_penalty"] = penalty
            components = match.setdefault("score_components", {})
            components["penalty_score"] = round(float(components.get("penalty_score", 0.0)) - penalty, 4)
            components["distant_scope_concept_only_penalty"] = -penalty
            components["final_score"] = round(score, 4)
        if extra_support_course_weights and row["compe_unit_name"] in extra_support_course_weights:
            score = min(1.0, score + float(extra_support_course_weights[row["compe_unit_name"]]))
            match.setdefault("reasons", []).append("target_support_course_hint")
            match["support_course_hint_weight"] = extra_support_course_weights[row["compe_unit_name"]]
        already_covered_hits = linked_unit_codes & covered_unit_codes
        has_gap_bridge = bool(
            int(match.get("gap_concept_hits") or 0)
            or match.get("gap_qualification_hits")
            or match.get("gap_job_base_hits")
        )
        if already_covered_hits and not has_gap_bridge:
            penalty = 0.22
            score = max(0.0, score - penalty)
            match.setdefault("reasons", []).append("current_scope_already_covered")
            match["already_covered_unit_codes"] = sorted(already_covered_hits)
            components = match.setdefault("score_components", {})
            components["penalty_score"] = round(float(components.get("penalty_score", 0.0)) - penalty, 4)
            components["already_covered_penalty"] = -penalty
            components["final_score"] = round(score, 4)
        quality_penalty = _concept_quality_issue_penalty_profile_from_map(
            set(match.get("source_hit_ids") or [])
            | set(match.get("gap_hit_ids") or [])
            | set(match.get("goal_hit_ids") or []),
            quality_penalty_by_concept,
        )
        if quality_penalty.get("applied"):
            multiplier = float(quality_penalty.get("multiplier") or 1.0)
            penalty_amount = max(0.0, score - (score * multiplier))
            score = max(0.0, score * multiplier)
            match.setdefault("reasons", []).append("quality_issue_ksa_penalty")
            match["quality_issue_penalty"] = quality_penalty
            components = match.setdefault("score_components", {})
            components["penalty_score"] = round(float(components.get("penalty_score", 0.0)) - penalty_amount, 4)
            components["quality_issue_penalty_score"] = -round(penalty_amount, 4)
            components["quality_issue_penalty_multiplier"] = round(multiplier, 4)
            components["final_score"] = round(score, 4)
        if (
            quality_penalty.get("applied")
            and scope_relation in DISTANT_SCOPE_RELATIONS_FOR_QUALITY_STACK
        ):
            stack_multiplier = DISTANT_SCOPE_QUALITY_PENALTY_STACK
            stacked_amount = max(0.0, score - (score * stack_multiplier))
            score = max(0.0, score * stack_multiplier)
            match.setdefault("reasons", []).append("distant_scope_quality_penalty_stack")
            components = match.setdefault("score_components", {})
            components["penalty_score"] = round(float(components.get("penalty_score", 0.0)) - stacked_amount, 4)
            components["distant_scope_quality_penalty_stack"] = -round(stacked_amount, 4)
            components["distant_scope_quality_penalty_multiplier"] = round(stack_multiplier, 4)
            components["final_score"] = round(score, 4)
        if score <= 0:
            continue
        candidates.append(
            {
                "row": row,
                "score": score,
                "match": match,
                "payload": payload,
                "qualification_links": qualification_links,
                "job_base_links": job_base_links,
            }
        )
    near_candidates = [
        item
        for item in candidates
        if ((item.get("match") or {}).get("course_scope_fit") or {}).get("relation") in COURSE_SCOPE_NEAR_RELATIONS
    ]
    if near_candidates:
        candidates.sort(
            key=_course_candidate_sort_key
        )
    else:
        candidates.sort(key=lambda item: (-float(item["score"]), _clean(item["row"]["compe_unit_name"])))
    selected = _diversify_top_k_candidates(candidates, max_items=max_items)
    recommendations: list[dict[str, Any]] = []
    for rank, candidate in enumerate(selected, start=1):
        row = candidate["row"]
        payload = candidate["payload"]
        match = candidate["match"]
        score = round(float(candidate["score"]), 4)
        hit_source = _filter_concepts(source_concepts, set(match.get("source_hit_ids") or []), limit=20)
        hit_gap = _filter_concepts(source_concepts, set(match.get("gap_hit_ids") or []), limit=20)
        hit_goal = _filter_concepts(source_concepts, set(match.get("goal_hit_ids") or []), limit=20)
        coverage_counts = {
            "source_ksa": len(hit_source),
            "gap_ksa": len(hit_gap),
            "goal_ksa": len(hit_goal),
            "elements": len(payload["element_links"]),
        }
        definition_trust = _definition_trust_profile(hit_source + hit_gap + hit_goal)
        match["definition_trust"] = definition_trust
        definition_trust_weight = float(definition_trust.get("weight") or 1.0)
        confidence_score = round(max(0.0, min(1.0, score * definition_trust_weight)), 3)
        preference_fit = match.get("preference_fit") or {}
        delivery_evidence = {
            "relations": payload["delivery_relations"],
            "profile": _delivery_mode_profile(payload["delivery_relations"]),
        }
        linked_unit_codes = {
            row["ncs_cl_cd"],
            *[link.get("unit_code") for link in payload["unit_links"] if link.get("unit_code")],
        }
        supplemental_evidence = _supplemental_reference_evidence(
            conn,
            row,
            {code for code in linked_unit_codes if code},
        )
        tier = _recommendation_tier(score, match)
        evidence_strength = _evidence_strength(match, score)
        item = {
            "rank": rank,
            "recommendation_source_type": "training_course",
            "training_course": dict(row),
            "unit_links": payload["unit_links"],
            "element_links": payload["element_links"],
            "concept_links": payload["concept_links"],
            "goal_coverage": hit_goal or [
                {
                    "concept_id": link.get("concept_id"),
                    "concept_name": link.get("concept_name"),
                    "concept_type": link.get("concept_type"),
                }
                for link in payload["goal_concept_links"]
                if _link_is_usable(link)
            ][:10],
            "covered_elements": payload["element_links"],
            "source_task_ksa_concepts": source_task_concepts,
            "scope_ksa_profile": scope_profile_concepts,
            "source_ksa_concepts": source_concepts,
            "gap_ksa_concepts": gap_concepts,
            "matched_source_ksa_concepts": hit_source,
            "matched_gap_ksa_concepts": hit_gap,
            "ontology_relations": _relation_rows(conn, source_concept_ids, limit=20),
            "delivery_evidence": delivery_evidence,
            "career_path_evidence": [
                item
                for item in trusted_scope_career_paths
                if item.get("matched_unit_code")
                in {row["ncs_cl_cd"], *[link.get("unit_code") for link in payload["unit_links"]]}
            ][:20],
            "qualification_evidence": candidate["qualification_links"],
            "job_base_evidence": candidate["job_base_links"],
            "job_base_signal": match.get("job_base_signal", {}),
            "supplemental_evidence": supplemental_evidence if supplemental_evidence.get("data_sources") else {},
            "match": match,
            "score_components": match.get("score_components", {}),
            "score_component_highlights": _score_component_highlights(match.get("score_components", {})),
            "confidence_score": confidence_score,
            "confidence_grade": _confidence_grade(confidence_score),
            "recommendation_tier": tier,
            "display_tier": tier["tier"],
            "evidence_strength": evidence_strength,
            "preference_fit": preference_fit,
            "coverage_counts": coverage_counts,
            "evidence_highlights": _evidence_highlights(
                {
                    "matched_source_ksa_concepts": hit_source,
                    "matched_gap_ksa_concepts": hit_gap,
                    "goal_coverage": hit_goal,
                    "covered_elements": payload["element_links"],
                    "career_path_evidence": [
                        item
                        for item in trusted_scope_career_paths
                        if item.get("matched_unit_code") == row["ncs_cl_cd"]
                    ],
                    "qualification_evidence": candidate["qualification_links"],
                    "job_base_evidence": candidate["job_base_links"],
                    "match": match,
                }
            ),
            "training_sequence": _training_sequence_hint(
                course_name=row["compe_unit_name"],
                target_label=sequence_target_label or resolved_scope.get("match_text") or query,
                match=match,
                course_level=row["compe_unit_level"],
            ),
            "explanation": _course_explanation(row, match, hit_source, hit_gap, hit_goal),
        }
        recommendations.append(item)
    if not recommendations:
        response = not_found_response("조건에 맞는 NCS 훈련과정 추천 결과가 없습니다.", suggestions=[source["unit_name_raw"]])
        response["requested_query"] = requested_query
        response["query_resolution"] = query_resolution
        return response
    sequence_plan = _training_sequence_plan(recommendations)
    groups = _recommendation_groups(recommendations)
    qualification_count = sum(len(item.get("qualification_evidence") or []) for item in recommendations)
    job_base_count = sum(len(item.get("job_base_evidence") or []) for item in recommendations)
    supplemental_data_sources = sorted(
        {
            source
            for item in recommendations
            for source in ((item.get("supplemental_evidence") or {}).get("data_sources") or [])
        }
    )
    summary = {
        "recommended_training_courses_count": len(recommendations),
        "primary_recommendation_count": len(groups["primary"]),
        "supplemental_recommendation_count": len(groups["supplemental"]),
        "adjacent_recommendation_count": len(groups["adjacent"]),
        "display_recommendation_counts": {key: len(value) for key, value in groups.items()},
        "source_task_ksa_concepts_used": len(source_task_concepts),
        "scope_ksa_concepts_used": len(scope_profile_concepts),
        "source_ksa_concepts_used": len(source_concepts),
        "gap_ksa_concepts_used": len(gap_concepts),
        "career_path_rows_seen": len(scope_career_paths),
        "trusted_career_path_rows_used": len(trusted_scope_career_paths),
        "career_path_review_status_counts": _review_status_counts(scope_career_paths),
        "career_path_units_used": len(career_path_unit_codes),
        "training_sequence_stage_count": len(sequence_plan),
        "qualification_evidence_count": qualification_count,
        "job_base_evidence_count": job_base_count,
        "target_qualification_signal_count": len(effective_target_qualification_keys),
        "gap_qualification_signal_count": len(effective_gap_qualification_keys),
        "target_job_base_signal_count": len(effective_target_job_base_keys),
        "gap_job_base_signal_count": len(effective_gap_job_base_keys),
        "supplemental_data_sources_used": supplemental_data_sources,
        "preferred_max_hours": preferred_max_hours,
        "preferred_methods": preferred_methods or [],
    }
    audit = {
        "generated_at": now_utc(),
        "sqf_used": False,
        "learning_modules_used": False,
        "data_sources": sorted(
            {
                "ncs_training_courses",
                "ncs_training_course_unit_links",
                "ncs_training_course_concept_links",
                "ncs_training_course_element_links",
                "training_goal_concept_links",
                "training_delivery_relations",
                "ontology_concepts",
                "ontology_concept_relations",
                "ncs_career_paths",
                "ncs_qualification_items",
                "ncs_unit_qualification_links",
                "ncs_job_base_competencies",
                "ncs_job_base_factors",
                "ncs_unit_job_base_links",
            }
            | set(supplemental_data_sources)
        ),
        "score_policy": {
            "score_weights": SCORE_WEIGHTS,
            "match_basis_weights": MATCH_BASIS_WEIGHTS,
            "definition_trust_weight": DEFINITION_TRUST_WEIGHT,
            "generic_ksa_unit_threshold": GENERIC_KSA_UNIT_THRESHOLD,
            "max_same_sub_code_in_top_k": MAX_SAME_SUB_CODE_IN_TOP_K,
        },
    }
    run_id = _save_recommendation(
        conn,
        query=query or unit_code or str(criteria_id or ""),
        source=source,
        summary=summary,
        audit=audit,
        recommendations=recommendations,
    ) if save else None
    return {
        "ok": True,
        "disclaimer": DISCLAIMER,
        "recommendation_run_id": run_id,
        "requested_query": requested_query,
        "requested_criteria_id": criteria_id,
        "requested_unit_code": unit_code,
        "query_alias": alias,
        "query_resolution": query_resolution,
        "source_task": {
            "criteria_id": source["criteria_id"],
            "criteria_text": source["criteria_text_raw"],
            "unit_code": source["unit_code"],
            "unit_name": source["unit_name_raw"],
            "element_id": source["element_id"],
            "element_name": source["element_name_raw"],
            "classification": {
                "major_code": source["major_code"],
                "major_name": source["major_name"],
                "middle_code": source["middle_code"],
                "middle_name": source["middle_name"],
                "small_code": source["small_code"],
                "small_name": source["small_name"],
                "sub_code": source["sub_code"],
                "sub_name": source["sub_name"],
            },
        },
        "resolved_scope": resolved_scope,
        "recommendation_summary": summary,
        "recommendations": recommendations,
        "recommendation_groups": groups,
        "training_sequence_plan": sequence_plan,
        "gaps": {"missing_concepts": gap_concepts[:30]},
        "audit": audit,
    }


def _course_explanation(
    row: sqlite3.Row,
    match: dict[str, Any],
    source_hits: list[dict[str, Any]],
    gap_hits: list[dict[str, Any]],
    goal_hits: list[dict[str, Any]],
) -> list[str]:
    lines = [f"{row['compe_unit_name']} 과정은 NCS 훈련과정 DB의 능력단위 {row['ncs_cl_cd']}와 연결됩니다."]
    if source_hits:
        lines.append("목표 범위 KSA: " + ", ".join(_concept_names(source_hits[:5])))
    if gap_hits:
        lines.append("보완 KSA: " + ", ".join(_concept_names(gap_hits[:5])))
    if goal_hits:
        lines.append("훈련목표 KSA: " + ", ".join(_concept_names(goal_hits[:5])))
    definition_trust = match.get("definition_trust") or {}
    if definition_trust.get("applied"):
        lines.append(
            "KSA 정의 신뢰 가중치: "
            f"{float(definition_trust.get('weight') or 0.0):.2f} "
            f"({definition_trust.get('status_counts')})"
        )
    if match.get("reasons"):
        lines.append("근거 방식: " + ", ".join(match["reasons"][:8]))
    return lines


def _course_scope_codes(course: sqlite3.Row | dict[str, Any]) -> dict[str, str]:
    row = _row_dict(course)
    major = _clean(row.get("ncs_lclas_cd")).zfill(2) if _clean(row.get("ncs_lclas_cd")) else ""
    middle_part = _clean(row.get("ncs_mclas_cd")).zfill(2) if _clean(row.get("ncs_mclas_cd")) else ""
    small_part = _clean(row.get("ncs_sclas_cd")).zfill(2) if _clean(row.get("ncs_sclas_cd")) else ""
    sub_part = _clean(row.get("ncs_subd_cd")).zfill(2) if _clean(row.get("ncs_subd_cd")) else ""
    middle = major + middle_part if major and middle_part else ""
    small = middle + small_part if middle and small_part else ""
    sub = small + sub_part if small and sub_part else ""
    return {
        "major": major,
        "middle": middle,
        "small": small,
        "sub": sub,
    }


def _scoped_supplemental_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    scope_codes: dict[str, str],
    select_columns: str,
    limit: int,
) -> tuple[list[dict[str, Any]], int, str | None]:
    # External catalogs and occupation mappings are optional support tables in
    # compact serving databases.  Check presence explicitly so an omitted
    # table means "no supplemental evidence" while SQL errors for an existing
    # table continue to propagate for diagnosis.
    table_row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    if table_row is None:
        return [], 0, None

    ordered_scopes = [
        (level, scope_codes.get(level))
        for level in ("sub", "small", "middle", "major")
        if scope_codes.get(level)
    ]
    if not ordered_scopes:
        return [], 0, None
    clauses: list[str] = []
    params: list[Any] = []
    for level, code in ordered_scopes:
        clauses.append("(ncs_code_normalized = ? AND ncs_code_level = ?)")
        params.extend([code, level])
    counts = rows_to_dicts(
        conn.execute(
            f"""
            SELECT ncs_code_normalized, ncs_code_level, COUNT(*) AS row_count
            FROM {table}
            WHERE {" OR ".join(clauses)}
            GROUP BY ncs_code_normalized, ncs_code_level
            """,
            params,
        ).fetchall()
    )
    priority = {(code, level): index for index, (level, code) in enumerate(ordered_scopes)}
    if counts:
        selected = min(
            counts,
            key=lambda item: priority.get((item.get("ncs_code_normalized"), item.get("ncs_code_level")), 999),
        )
        code = _clean(selected.get("ncs_code_normalized"))
        level = _clean(selected.get("ncs_code_level"))
        rows = conn.execute(
            f"""
            SELECT {select_columns}
            FROM {table}
            WHERE ncs_code_normalized = ?
              AND ncs_code_level = ?
            ORDER BY source_row_number
            LIMIT ?
            """,
            (code, level, limit),
        ).fetchall()
        return rows_to_dicts(rows), int(selected.get("row_count") or 0), level
    code_priority = {code: index for index, (_level, code) in enumerate(ordered_scopes)}
    placeholders = ",".join("?" for _level, code in ordered_scopes)
    fallback_counts = rows_to_dicts(
        conn.execute(
            f"""
            SELECT ncs_code_normalized, ncs_code_level, COUNT(*) AS row_count
            FROM {table}
            WHERE ncs_code_normalized IN ({placeholders})
            GROUP BY ncs_code_normalized, ncs_code_level
            """,
            [code for _level, code in ordered_scopes],
        ).fetchall()
    )
    if fallback_counts:
        selected = min(
            fallback_counts,
            key=lambda item: code_priority.get(item.get("ncs_code_normalized"), 999),
        )
        code = _clean(selected.get("ncs_code_normalized"))
        level = _clean(selected.get("ncs_code_level"))
        rows = conn.execute(
            f"""
            SELECT {select_columns}
            FROM {table}
            WHERE ncs_code_normalized = ?
              AND ncs_code_level = ?
            ORDER BY source_row_number
            LIMIT ?
            """,
            (code, level, limit),
        ).fetchall()
        return rows_to_dicts(rows), int(selected.get("row_count") or 0), level
    return [], 0, None


def _standard_time_alignment(course_hours: float | None, standard_hours: float | None) -> str:
    if course_hours is None or standard_hours is None:
        return "unknown"
    if abs(course_hours - standard_hours) <= 0.01:
        return "matches_standard"
    if course_hours < standard_hours:
        return "shorter_than_standard"
    return "longer_than_standard"


def _supplemental_reference_evidence(
    conn: sqlite3.Connection,
    course: sqlite3.Row | dict[str, Any],
    unit_codes: set[str],
) -> dict[str, Any]:
    row = _row_dict(course)
    evidence: dict[str, Any] = {
        "scoring_role": "context_only",
        "used_for_scoring": False,
        "policy_note": "Supplemental CSV rows are shown as reference context and are not used as primary ranking evidence.",
        "data_sources": [],
    }
    source_tables: set[str] = set()
    clean_unit_codes = sorted({_clean(code) for code in unit_codes if _clean(code)})
    if clean_unit_codes:
        placeholders = ",".join("?" for _ in clean_unit_codes)
        standard_row = conn.execute(
            f"""
            SELECT
                unit_code_raw, unit_name, unit_level, standard_training_hours,
                matched_unit_code, match_status
            FROM ncs_unit_standard_training
            WHERE matched_unit_code IN ({placeholders})
              AND match_status = 'matched_unit_exact'
            ORDER BY updated_at DESC, unit_standard_id DESC
            LIMIT 1
            """,
            clean_unit_codes,
        ).fetchone()
        if standard_row:
            source_tables.add("ncs_unit_standard_training")
            standard = row_to_dict(standard_row)
            course_hours = _parse_number(row.get("train_time"))
            standard_hours = _parse_number(standard.get("standard_training_hours"))
            hours_delta = (
                round(course_hours - standard_hours, 4)
                if course_hours is not None and standard_hours is not None
                else None
            )
            hours_ratio = (
                round(course_hours / standard_hours, 4)
                if course_hours is not None and standard_hours not in {None, 0}
                else None
            )
            evidence["standard_training"] = {
                "matched_unit_code": standard.get("matched_unit_code"),
                "unit_name": standard.get("unit_name"),
                "unit_level": standard.get("unit_level"),
                "standard_training_hours": standard_hours,
                "course_training_hours": course_hours,
                "hours_delta": hours_delta,
                "hours_ratio": hours_ratio,
                "time_alignment": _standard_time_alignment(course_hours, standard_hours),
                "match_status": "exact",
                "used_for_scoring": False,
            }
    scope_codes = _course_scope_codes(row)
    external_rows, external_count, external_scope = _scoped_supplemental_rows(
        conn,
        table="ncs_external_training_zip_courses",
        scope_codes=scope_codes,
        select_columns="""
            external_training_id, course_name, business_type, institution_name,
            ncs_code_normalized, ncs_code_level, training_method, training_hours
        """,
        limit=5,
    )
    if external_count:
        source_tables.add("ncs_external_training_zip_courses")
        evidence["external_training_catalog"] = {
            "match_scope": external_scope,
            "match_status": "matched",
            "matched_course_count": external_count,
            "used_for_scoring": False,
            "sample_courses": [
                {
                    "course_name": item.get("course_name"),
                    "business_type": item.get("business_type"),
                    "training_method": item.get("training_method"),
                    "training_hours": item.get("training_hours"),
                    "ncs_code_level": item.get("ncs_code_level"),
                }
                for item in external_rows
            ],
        }
    mapping_rows, mapping_count, mapping_scope = _scoped_supplemental_rows(
        conn,
        table="ncs_occupation_code_mappings",
        scope_codes=scope_codes,
        select_columns="""
            mapping_id, ncs_code_normalized, ncs_code_level, ncs_code_name,
            national_job_code, national_job_name, keco_code, keco_name
        """,
        limit=5,
    )
    if mapping_count:
        source_tables.add("ncs_occupation_code_mappings")
        evidence["occupation_code_mappings"] = {
            "match_scope": mapping_scope,
            "match_status": "matched",
            "mapping_count": mapping_count,
            "used_for_scoring": False,
            "sample_mappings": [
                {
                    "ncs_code_name": item.get("ncs_code_name"),
                    "national_job_code": item.get("national_job_code"),
                    "national_job_name": item.get("national_job_name"),
                    "keco_code": item.get("keco_code"),
                    "keco_name": item.get("keco_name"),
                    "ncs_code_level": item.get("ncs_code_level"),
                }
                for item in mapping_rows
            ],
        }
    evidence["data_sources"] = sorted(source_tables)
    return evidence


def _candidate_query_items(resolution: dict[str, Any] | None, *, limit: int = 5) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in (resolution or {}).get("candidates") or []:
        if candidate.get("candidate_type") == "concept":
            continue
        query = _clean(candidate.get("matched_text") or candidate.get("unit_name") or candidate.get("sub_name"))
        if not query or query in seen:
            continue
        original = _clean((resolution or {}).get("query"))
        if original and len(original) <= 1 and not query.startswith(original):
            continue
        seen.add(query)
        items.append(
            {
                "query": query,
                "candidate_type": candidate.get("candidate_type"),
                "match_level": candidate.get("match_level"),
                "unit_code": candidate.get("unit_code"),
                "confidence_score": candidate.get("confidence_score"),
            }
        )
        if len(items) >= limit:
            break
    return items


def _candidate_suggestions(resolution: dict[str, Any] | None, *, field: str) -> list[str]:
    return [f"{field}: {item['query']}" for item in _candidate_query_items(resolution)]


def _input_quality_for_task(result: dict[str, Any]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    requested_query = _clean(result.get("requested_query"))
    if requested_query and len(requested_query) < 2:
        warnings.append({"code": "short_query", "field": "query", "message": "질의가 너무 짧습니다."})
    candidates = _candidate_query_items(result.get("query_resolution"))
    suggestions = [f"query: {item['query']}" for item in candidates]
    return {
        "ok": not warnings,
        "warnings": warnings,
        "suggestions": suggestions,
        "candidate_queries": {"query": candidates} if candidates else {},
    }


def _input_quality_for_transition(result: dict[str, Any], groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    warnings: list[dict[str, Any]] = []
    transition = result.get("transition") or {}
    summary = transition.get("summary") or {}
    for field, value in (
        ("current_query", summary.get("requested_current_query")),
        ("target_query", summary.get("requested_target_query")),
    ):
        if _clean(value) and len(_clean(value)) < 2:
            warnings.append({"code": "short_query", "field": field, "message": "질의가 너무 짧습니다."})
    for field, scope, count_key in (
        ("current_query", transition.get("current_scope") or {}, "current_scope_unit_count"),
        ("target_query", transition.get("target_scope") or {}, "target_scope_unit_count"),
    ):
        if (summary.get(count_key) or 0) >= 20 or scope.get("match_level") in {"major", "middle_classification", "major_classification"}:
            warnings.append({"code": "broad_scope", "field": field, "message": "범위가 넓어 근거 품질이 낮아질 수 있습니다."})
    if not groups.get("primary"):
        warnings.append({"code": "zero_primary_recommendations", "field": "recommendations", "message": "우선 추천이 없습니다."})
    visible_has_non_reference = any((item.get("confidence_score") or 0) >= 0.35 for item in groups.get("primary", []) + groups.get("supplemental", []))
    if not groups.get("primary") and not visible_has_non_reference:
        warnings.append({"code": "adjacent_reference_only", "field": "recommendations", "message": "참고 과정 중심의 결과입니다."})
    current_resolution = result.get("current_query_resolution") or transition.get("current_query_resolution")
    target_resolution = result.get("target_query_resolution") or transition.get("target_query_resolution")
    candidates: dict[str, list[dict[str, Any]]] = {}
    current_candidates = _candidate_query_items(current_resolution)
    target_candidates = _candidate_query_items(target_resolution)
    if current_candidates:
        candidates["current_query"] = current_candidates
    if target_candidates:
        candidates["target_query"] = target_candidates
    suggestions = [f"{field}: {item['query']}" for field, values in candidates.items() for item in values]
    return {
        "ok": not warnings,
        "warnings": warnings,
        "suggestions": suggestions,
        "candidate_queries": candidates,
    }


def _fit_summary(preference_fit: dict[str, Any] | None) -> list[str]:
    fit = preference_fit or {}
    lines: list[str] = []
    if fit.get("time_fit") == "over":
        lines.append(f"시간 조건 초과: {fit.get('actual_hours')}h > {fit.get('preferred_max_hours')}h")
    elif fit.get("time_fit") == "fit":
        lines.append(f"시간 조건 적합: {fit.get('actual_hours')}h")
    requested = [item for item in (fit.get("requested_methods") or []) if item]
    matched_groups = set(fit.get("matched_method_groups") or [])
    delivery = [item for item in (fit.get("delivery_methods") or []) if item]
    if requested:
        if fit.get("method_fit"):
            display = next((item for item in requested if _method_groups([item]) & matched_groups), requested[0])
            lines.append(f"훈련방식 일치: {display}")
        else:
            lines.append("훈련방식 불일치: 요청 " + ", ".join(requested) + " / 과정 " + ", ".join(delivery))
    return lines


QUALITY_ISSUE_PENALTY_LABELS = {
    "short_ksa": "짧은 KSA 용어 검토 필요",
    "duplicate_text": "중복 KSA 용어 검토 필요",
    "broad_generic_ksa": "범용 KSA 과잉 연결 감점",
}

QUALITY_ISSUE_PENALTY_EXPLANATIONS = {
    "short_ksa": (
        "짧은 KSA 문구는 약어인지 범용어인지 확인되기 전까지 추천 근거 가중치를 낮춥니다."
    ),
    "duplicate_text": (
        "여러 위치에 반복된 KSA 문구는 같은 의미인지 범위별 의미가 다른지 확인되기 전까지 감점합니다."
    ),
    "broad_generic_ksa": (
        "여러 능력단위나 분류에 넓게 반복된 KSA는 특정 직무 추천 근거로 과대평가하지 않도록 낮은 가중치만 적용합니다."
    ),
}

QUALITY_ISSUE_PENALTY_OPERATOR_ACTIONS = {
    "short_ksa": "sample_task_evidence_before_definition_or_acronym_decision",
    "duplicate_text": "compare_scope_samples_before_merge_or_split_decision",
    "broad_generic_ksa": "review_for_downweight_or_scope_split",
}


def _quality_issue_penalty_summary(match: dict[str, Any]) -> dict[str, Any]:
    penalty = match.get("quality_issue_penalty") if isinstance(match, dict) else None
    if not isinstance(penalty, dict) or not penalty.get("applied"):
        return {}
    issue_types = [str(issue_type) for issue_type in penalty.get("issue_types") or []]
    labels = [
        QUALITY_ISSUE_PENALTY_LABELS.get(issue_type, issue_type)
        for issue_type in issue_types
    ]
    explanations = [
        QUALITY_ISSUE_PENALTY_EXPLANATIONS.get(issue_type, issue_type)
        for issue_type in issue_types
    ]
    operator_actions = [
        QUALITY_ISSUE_PENALTY_OPERATOR_ACTIONS.get(issue_type, "inspect_quality_issue_samples")
        for issue_type in issue_types
    ]
    review_assist: dict[str, Any] = {
        "schema": "ncs_recommendation_quality_issue_penalty_review_assist_v1",
        "operator_actions": list(dict.fromkeys(operator_actions)),
        "human_review_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }
    if "broad_generic_ksa" in issue_types:
        review_assist["genericity_signal"] = {
            "schema": "ncs_recommendation_ksa_genericity_signal_v1",
            "level": "high",
            "reasons": ["broad_generic_ksa_issue"],
            "operator_action": "review_for_downweight_or_scope_split",
            "scoring_role": "recommendation_downweight_only",
            "status_update_allowed": False,
            "db_writes": False,
            "approval_claim": False,
        }
    return {
        "applied": True,
        "issue_types": issue_types,
        "labels": labels,
        "explanations": explanations,
        "multiplier": round(float(penalty.get("multiplier") or 1.0), 4),
        "concept_ids": [int(concept_id) for concept_id in penalty.get("concept_ids") or []],
        "concept_issue_types": dict(penalty.get("concept_issue_types") or {}),
        "affected_concepts": list(penalty.get("affected_concepts") or []),
        "scoring_role": "downweight_only",
        "review_required": True,
        "review_assist": review_assist,
    }


def _quality_issue_affected_concepts(
    item: dict[str, Any],
    quality_penalty: dict[str, Any],
    *,
    limit: int = 8,
) -> list[dict[str, Any]]:
    affected_ids = {
        int(concept_id)
        for concept_id in quality_penalty.get("concept_ids") or []
        if int(concept_id or 0)
    }
    if not affected_ids:
        return []
    concept_issue_types = {
        str(concept_id): [str(issue_type) for issue_type in issue_types or []]
        for concept_id, issue_types in (quality_penalty.get("concept_issue_types") or {}).items()
    }
    concept_rows: list[dict[str, Any]] = []
    for field in ("matched_gap_ksa_concepts", "goal_coverage", "matched_source_ksa_concepts"):
        concept_rows.extend(row for row in item.get(field) or [] if isinstance(row, dict))
    by_id: dict[int, dict[str, Any]] = {}
    for concept in concept_rows:
        concept_id = int(concept.get("concept_id") or 0)
        if concept_id and concept_id in affected_ids and concept_id not in by_id:
            by_id[concept_id] = concept
    affected: list[dict[str, Any]] = []
    for concept_id in sorted(affected_ids):
        concept = by_id.get(concept_id, {})
        issue_types = concept_issue_types.get(str(concept_id)) or list(quality_penalty.get("issue_types") or [])
        affected.append(
            {
                "concept_id": concept_id,
                "concept_name": _clean(concept.get("concept_name")),
                "concept_type": _clean(concept.get("concept_type")),
                "issue_types": issue_types,
            }
        )
        if len(affected) >= limit:
            break
    return affected


def _label_for_tier(
    tier: str | None,
    *,
    confidence_grade: str | None = None,
    confidence_score: float | None = None,
) -> str:
    if tier == "primary" and (
        str(confidence_grade or "").lower() == "low"
        or (confidence_score is not None and confidence_score < 0.35)
    ):
        return "우선 검토"
    return {
        "primary": "우선 추천",
        "supplemental": "보조 추천",
        "adjacent": "참고 과정",
        "adjacent_reference": "참고 과정",
    }.get(tier or "", "참고 과정")


def _strength_summary(evidence_strength: dict[str, Any] | None) -> dict[str, Any]:
    strength = dict(evidence_strength or {})
    label_map = {
        "strong_direct_evidence": "강한 직접 근거",
        "usable_supporting_evidence": "활용 가능한 근거",
        "transition_supporting_evidence": "전환 보조 근거",
        "weak_evidence": "약한 근거",
    }
    strength["label"] = label_map.get(strength.get("label"), label_map.get(strength.get("grade"), strength.get("label") or "근거"))
    return strength


def _evidence_highlights(item: dict[str, Any]) -> dict[str, list[str]]:
    match = item.get("match") or {}
    highlights: dict[str, list[str]] = {}
    if int(match.get("source_concept_hits") or 0) > 0 or item.get("matched_source_ksa_concepts"):
        names = _concept_names(item.get("matched_source_ksa_concepts") or [])
        if names:
            highlights["source_ksa"] = names[:5]
    if int(match.get("gap_concept_hits") or 0) > 0 or item.get("matched_gap_ksa_concepts"):
        names = _concept_names(item.get("matched_gap_ksa_concepts") or [])
        if names:
            highlights["gap_ksa"] = names[:5]
    if int(match.get("goal_concept_hits") or 0) > 0 or item.get("goal_coverage"):
        names = _concept_names(item.get("goal_coverage") or [])
        if names:
            highlights["goal_ksa"] = names[:5]
    elements = [
        _clean(element.get("element_name_raw") or element.get("element_name"))
        for element in item.get("covered_elements") or []
        if _clean(element.get("element_name_raw") or element.get("element_name"))
    ]
    if elements:
        highlights["covered_elements"] = list(dict.fromkeys(elements))[:5]
    if highlights.get("source_ksa") or highlights.get("gap_ksa") or highlights.get("goal_ksa"):
        career = [
            f"{_clean(row.get('competency_name'))}({_clean(row.get('position_name') or row.get('position_level_raw'))})"
            for row in item.get("career_path_evidence") or []
            if _clean(row.get("competency_name"))
        ]
        if career:
            highlights["career_path"] = list(dict.fromkeys(career))[:5]
        qualifications = [_qualification_label(row) for row in item.get("qualification_evidence") or [] if _qualification_label(row)]
        if qualifications:
            highlights["qualifications"] = list(dict.fromkeys(qualifications))[:5]
        job_base = [_job_base_label(row) for row in item.get("job_base_evidence") or [] if _job_base_label(row)]
        if job_base:
            highlights["job_base"] = list(dict.fromkeys(job_base))[:5]
    return highlights


def _career_path_review_basis(item: dict[str, Any], highlights: dict[str, list[str]]) -> dict[str, Any]:
    raw_rows = item.get("career_path_evidence") if isinstance(item.get("career_path_evidence"), list) else []
    trusted_rows = [
        row
        for row in raw_rows
        if isinstance(row, dict)
        and _clean(row.get("review_status")) in TRUSTED_CAREER_PATH_REVIEW_STATUSES
    ]
    display_refs = list(dict.fromkeys(highlights.get("career_path") or []))[:5]
    if not trusted_rows and not display_refs:
        return {
        "schema": "aihr_career_path_review_basis_v1",
        "status": "not_available",
        "trusted_reviewed_count": 0,
        "trusted_review_state_counts": {},
        "display_refs": [],
        "basis": [],
            "approval_claim": False,
            "db_writes": False,
        }
    status_counts: dict[str, int] = {}
    basis: list[dict[str, Any]] = []
    for row in trusted_rows[:8]:
        status = _clean(row.get("review_status")) or "unknown"
        status_counts[status] = status_counts.get(status, 0) + 1
        basis.append(
            {
                "career_path_id": row.get("career_path_id"),
                "job_name": row.get("job_name"),
                "competency_name": row.get("competency_name"),
                "matched_unit_code": row.get("matched_unit_code"),
                "position_name": row.get("position_name"),
                "position_level": row.get("position_level_raw"),
                "trusted_review_state": status,
            }
        )
    return {
        "schema": "aihr_career_path_review_basis_v1",
        "status": "trusted_evidence_visible" if trusted_rows else "highlight_only",
        "trusted_reviewed_count": len(trusted_rows),
        "trusted_review_state_counts": status_counts,
        "display_refs": display_refs,
        "basis": basis,
        "basis_source": "ncs_career_paths",
        "approval_claim": False,
        "db_writes": False,
    }


def _coverage_breakdown(match: dict[str, Any]) -> dict[str, int]:
    goal_review = match.get("goal_review_counts") or {}
    return {
        "source_ksa": int(match.get("source_concept_hits") or 0),
        "gap_ksa": int(match.get("gap_concept_hits") or 0),
        "goal_ksa": int(match.get("goal_concept_hits") or 0),
        "goal_direct": int(match.get("goal_direct_concept_hits") or 0),
        "goal_token": int(match.get("goal_token_concept_hits") or 0),
        "reviewed_goal_links": int(goal_review.get("reviewed") or 0) + int(goal_review.get("human_reviewed") or 0),
    }


def _training_need_classification(
    *,
    tier: str | None,
    match: dict[str, Any],
    coverage_counts: dict[str, Any],
) -> dict[str, str]:
    has_task_ksa_or_goal_evidence = bool(
        match.get("source_element_covered")
        or match.get("goal_direct_concept_hits")
        or match.get("goal_token_concept_hits")
        or int(coverage_counts.get("gap_ksa") or 0)
        or int(coverage_counts.get("goal_ksa") or 0)
    )
    if tier == "primary":
        code = "required"
    elif tier == "supplemental":
        if int(coverage_counts.get("gap_ksa") or 0) or int(coverage_counts.get("goal_ksa") or 0):
            code = "supporting"
        else:
            code = "optional"
    else:
        code = "adjacent_reference"
    labels = {
        "required": "필수 검토",
        "supporting": "보완 추천",
        "optional": "선택 후보",
        "adjacent_reference": "인접 참고",
    }
    reasons = {
        "required": "목표 범위, 과업, KSA, 훈련목표 근거가 강한 우선 과정입니다.",
        "supporting": "부족 KSA나 훈련목표 근거를 보완하는 과정입니다.",
        "optional": "직무 연계성은 있으나 필수 편성 전 추가 검토가 필요한 과정입니다.",
        "adjacent_reference": "직접 근거가 약한 참고 과정입니다.",
    }
    if code == "required" and not has_task_ksa_or_goal_evidence:
        code = "supporting"
    return {"code": code, "label": labels[code], "rationale": reasons[code]}


def _evidence_directness(match: dict[str, Any], coverage_counts: dict[str, Any]) -> dict[str, str]:
    if int(match.get("goal_direct_concept_hits") or 0):
        code = "training_goal_direct"
    elif int(match.get("goal_token_concept_hits") or 0):
        code = "training_goal_token"
    elif match.get("source_element_covered"):
        code = "element_coverage"
    elif match.get("direct_unit_evidence"):
        code = "unit_scope"
    elif int(coverage_counts.get("gap_ksa") or 0) or int(coverage_counts.get("goal_ksa") or 0):
        code = "ksa_overlap"
    else:
        code = "weak"
    labels = {
        "training_goal_direct": "훈련목표 직접 KSA 근거",
        "training_goal_token": "훈련목표 토큰 KSA 근거",
        "element_coverage": "능력단위요소 근거",
        "unit_scope": "능력단위 범위 근거",
        "ksa_overlap": "KSA 중첩 근거",
        "weak": "약한 근거",
    }
    return {"code": code, "label": labels[code]}


def _training_system_fit(
    *,
    card: dict[str, Any],
    match: dict[str, Any],
    coverage_counts: dict[str, Any],
    highlights: dict[str, list[str]],
    tier: str | None,
) -> dict[str, Any]:
    need = _training_need_classification(tier=tier, match=match, coverage_counts=coverage_counts)
    delivery = _course_delivery_brief(card)
    basis = []
    if highlights.get("target_scope_ksa"):
        basis.append("target_scope_ksa")
    if highlights.get("source_ksa"):
        basis.append("source_ksa")
    if highlights.get("gap_ksa"):
        basis.append("gap_ksa")
    if highlights.get("goal_ksa"):
        basis.append("training_goal_ksa")
    if highlights.get("covered_elements"):
        basis.append("competency_element")
    flags: list[str] = []
    directness = _evidence_directness(match, coverage_counts)
    if directness["code"] == "weak":
        flags.append("weak_direct_evidence")
    if tier == "primary" and need.get("code") != "required":
        flags.append("primary_demoted_without_direct_task_ksa_or_goal")
    if directness["code"] == "unit_scope" and need.get("code") != "required":
        flags.append("unit_scope_without_task_ksa_or_goal")
    if not basis:
        flags.append("missing_named_task_ksa_evidence")
    if need.get("code") == "adjacent_reference":
        flags.append("adjacent_reference_only")
        if directness["code"] == "ksa_overlap":
            flags.append("adjacent_ksa_overlap_requires_review")
    course_scope_fit = _public_course_scope_fit(match.get("course_scope_fit"))
    scope_alignment = course_scope_fit.get("alignment")
    if scope_alignment == "adjacent":
        flags.append("course_scope_adjacent_requires_review")
    elif scope_alignment == "distant":
        flags.append("course_scope_distant_requires_review")
    elif scope_alignment == "unknown":
        flags.append("course_scope_unknown_requires_review")
    flags = list(dict.fromkeys(flags))
    return {
        "rubric_source": "2026_hr_ncs_training_system_guide",
        "rubric_role": "framework_reference_not_scoring_source",
        "mapping_chain": [
            "job_or_ncs_scope",
            "duty_or_task",
            "performance_criterion",
            "ksa",
            "training_course",
        ],
        "need_classification": need,
        "evidence_directness": directness,
        "task_ksa_basis": {
            "basis_types": basis,
            "target_scope_ksa": highlights.get("target_scope_ksa") or highlights.get("source_ksa") or [],
            "gap_ksa": highlights.get("gap_ksa") or [],
            "training_goal_ksa": highlights.get("goal_ksa") or [],
            "covered_elements": highlights.get("covered_elements") or [],
        },
        "course_fit": {
            "level": delivery.get("level"),
            "hours": delivery.get("hours"),
            "methods": delivery.get("methods"),
            "facilities": delivery.get("facilities"),
        },
        "course_scope_fit": course_scope_fit,
        "review_flags": flags,
        "human_review_prompt": "이 과정이 어떤 직무/과업/KSA 필요를 충족하는지 확인한 뒤 필수/선택/보조/참고로 확정하세요.",
    }


def _compact_course_card(
    raw: dict[str, Any],
    *,
    transition: bool = False,
    preferred_facilities: list[Any] | None = None,
) -> dict[str, Any]:
    item = dict(raw)
    course = item.get("training_course") or {}
    if not course and item.get("course_name"):
        course = {
            "training_course_id": item.get("training_course_id"),
            "compe_unit_name": item.get("course_name"),
        }
    match = item.get("match") or {}
    tier = item.get("tier") or (item.get("recommendation_tier") or {}).get("tier") or item.get("display_tier")
    score = float(item.get("confidence_score") or 0.0)
    if tier == "supplemental" and score < 0.35:
        tier = "adjacent_reference"
    rationale = item.get("rationale") or (item.get("recommendation_tier") or {}).get("rationale") or ""
    if tier == "adjacent_reference":
        rationale = "참고 과정입니다. 직접 근거가 약하므로 검토용으로만 사용하세요."
    highlights = dict(item.get("evidence_highlights") or _evidence_highlights(item))
    coverage_counts = {
        "source_ksa": 0,
        "gap_ksa": 0,
        "goal_ksa": 0,
        **dict(item.get("coverage_counts") or {}),
    }
    if transition:
        if "source_ksa" in coverage_counts:
            coverage_counts["target_scope_ksa"] = coverage_counts.pop("source_ksa")
        if "source_ksa" in highlights:
            highlights["target_scope_ksa"] = highlights.pop("source_ksa")
    coverage_summary: list[str] = []
    if transition and int(coverage_counts.get("target_scope_ksa") or 0):
        coverage_summary.append(f"목표 범위 KSA 근거 {coverage_counts['target_scope_ksa']}개")
    if int(coverage_counts.get("gap_ksa") or 0):
        coverage_summary.append(f"보완 KSA 근거 {coverage_counts['gap_ksa']}개")
    if int(coverage_counts.get("goal_ksa") or 0):
        coverage_summary.append(f"훈련목표 KSA 근거 {coverage_counts['goal_ksa']}개")
    why: list[str] = []
    if highlights.get("target_scope_ksa"):
        why.append("목표 범위 KSA: " + ", ".join(highlights["target_scope_ksa"]))
    if highlights.get("source_ksa"):
        why.append("목표 범위 KSA: " + ", ".join(highlights["source_ksa"]))
    if highlights.get("gap_ksa"):
        why.append("보완 KSA: " + ", ".join(highlights["gap_ksa"]))
    if highlights.get("goal_ksa"):
        why.append("훈련목표 KSA: " + ", ".join(highlights["goal_ksa"]))
    if match.get("reasons"):
        why.append("근거 방식: " + ", ".join(match.get("reasons")[:6]))
    elif item.get("evidence_strength") or match:
        basis = []
        if match.get("goal_direct_concept_hits"):
            basis.append("training_goal_concept_text")
        if match.get("goal_token_concept_hits"):
            basis.append("training_goal_concept_token")
        if not basis and item.get("evidence_strength"):
            basis.append(str((item.get("evidence_strength") or {}).get("label") or "evidence"))
        why.append("근거 방식: " + ", ".join(basis))
    quality_penalty = _quality_issue_penalty_summary(match)
    if quality_penalty:
        if not quality_penalty.get("affected_concepts"):
            quality_penalty["affected_concepts"] = _quality_issue_affected_concepts(item, quality_penalty)
        why.append("KSA 품질 감점: " + ", ".join(quality_penalty.get("labels") or []))
    delivery = _course_delivery_brief({"delivery": item.get("delivery") or item.get("delivery_evidence") or {}})
    career_path_review_basis = item.get("career_path_review_basis")
    if not isinstance(career_path_review_basis, dict):
        career_path_review_basis = _career_path_review_basis(item, highlights)
    card = {
        "rank": item.get("rank"),
        "course_name": course.get("compe_unit_name"),
        "course_goal": _clean(course.get("train_goal") or course.get("course_goal")),
        "training_course_id": course.get("training_course_id"),
        "confidence_score": item.get("confidence_score"),
        "confidence_grade": item.get("confidence_grade"),
        "tier": tier,
        "tier_label": _label_for_tier(
            tier,
            confidence_grade=item.get("confidence_grade"),
            confidence_score=score,
        ),
        "rationale": rationale,
        "evidence_strength_summary": _strength_summary(item.get("evidence_strength")),
        "coverage_counts": coverage_counts,
        "coverage_breakdown": _coverage_breakdown(match),
        "coverage_summary": coverage_summary,
        "evidence_highlights": highlights,
        "career_path_review_basis": career_path_review_basis,
        "fit_summary": _fit_summary(item.get("preference_fit")),
        "delivery": delivery,
        "score_component_highlights": item.get("score_component_highlights") or [],
        "quality_issue_penalty": quality_penalty,
        "job_base_signal": item.get("job_base_signal") or match.get("job_base_signal") or {},
        "why_recommended": why,
    }
    card["training_system_fit"] = _training_system_fit(
        card=card,
        match=match,
        coverage_counts=coverage_counts,
        highlights=highlights,
        tier=tier,
    )
    facility_fit = _facility_constraint_fit(
        card["training_system_fit"].get("course_fit") or {},
        preferred_facilities,
    )
    review_flags = list(card["training_system_fit"].get("review_flags") or [])
    facility_status = str(facility_fit.get("status") or "")
    if facility_status in {"unknown", "partial", "mismatch"}:
        review_flags.append(f"delivery:facility_{facility_status}")
        review_flags.append(f"facility_constraint_{facility_status}")
    review_flags = list(dict.fromkeys(review_flags))
    card["training_system_fit"]["review_flags"] = review_flags
    human_review = {
        "severity": "needs_review" if review_flags else "ready",
        "prompt": card["training_system_fit"].get("human_review_prompt")
        or "Confirm job/task/KSA fit and classify this course as required, optional, supporting, or reference.",
        "action": "review_training_course_card",
        "flags": review_flags,
        "status_update_allowed": False,
        "db_writes": False,
        "approval_claim": False,
    }
    card["facility_constraint_fit"] = facility_fit
    card["human_review"] = human_review
    card["training_system_fit"]["facility_constraint_fit"] = facility_fit
    card["training_system_fit"]["human_review"] = human_review
    card["course_scope_fit"] = card["training_system_fit"]["course_scope_fit"]
    supplemental_evidence = item.get("supplemental_evidence") or {}
    if supplemental_evidence.get("data_sources"):
        card["supplemental_evidence"] = {
            key: value
            for key, value in supplemental_evidence.items()
            if key in {
                "scoring_role",
                "used_for_scoring",
                "policy_note",
                "standard_training",
                "external_training_catalog",
                "occupation_code_mappings",
                "data_sources",
            }
        }
    return card


def _items_from_groups_or_recommendations(result: dict[str, Any]) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    groups = result.get("recommendation_groups")
    if isinstance(groups, dict):
        normalized: dict[str, list[dict[str, Any]]] = {"primary": [], "supplemental": [], "adjacent": []}
        all_items: list[dict[str, Any]] = []
        for key in ("primary", "supplemental", "adjacent"):
            for raw in groups.get(key) or []:
                item = dict(raw)
                item.setdefault("tier", key)
                item.setdefault("display_tier", key)
                if "recommendation_tier" not in item:
                    item["recommendation_tier"] = {
                        "tier": key,
                        "label": _label_for_tier(key),
                        "rationale": item.get("rationale") or "",
                    }
                normalized[key].append(item)
                all_items.append(item)
        all_items.sort(key=lambda item: (int(item.get("rank") or 999999), {"primary": 0, "supplemental": 1, "adjacent": 2}.get(str(item.get("tier")), 9)))
        return normalized, all_items
    recommendations = list(result.get("recommendations") or [])
    groups = _recommendation_groups(recommendations)
    return groups, recommendations


def _compact_audit(result: dict[str, Any]) -> dict[str, Any]:
    audit = result.get("audit") if isinstance(result.get("audit"), dict) else {}
    data_sources = sorted({_clean(source) for source in audit.get("data_sources") or [] if _clean(source)})
    sqf_used = bool(audit.get("sqf_used", False))
    learning_modules_used = bool(audit.get("learning_modules_used", False))
    excluded_legacy_sources: list[str] = []
    if not sqf_used:
        excluded_legacy_sources.append("SQF")
    if not learning_modules_used:
        excluded_legacy_sources.append("ncs_learning_modules")
    return {
        "generated_at": audit.get("generated_at"),
        "sqf_used": sqf_used,
        "learning_modules_used": learning_modules_used,
        "data_sources": data_sources,
        "excluded_legacy_sources": excluded_legacy_sources,
    }


PUBLIC_OPERATIONAL_METADATA_KEYS = {
    "data_sources",
    "delivery_evidence",
    "relation_id",
    "created_at",
    "updated_at",
    "review_status",
    "source_payload",
    "source_rows",
    "source_json",
    "raw_payload",
    "raw_response",
}


def _strip_public_operational_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_public_operational_metadata(child)
            for key, child in value.items()
            if key not in PUBLIC_OPERATIONAL_METADATA_KEYS
        }
    if isinstance(value, list):
        return [_strip_public_operational_metadata(item) for item in value]
    return value


def _public_plan_audit(result: dict[str, Any]) -> dict[str, Any]:
    return _strip_public_operational_metadata(_compact_audit(result))


def _course_delivery_brief(card: dict[str, Any]) -> dict[str, Any]:
    delivery = card.get("delivery") if isinstance(card.get("delivery"), dict) else {}
    relations = delivery.get("relations") or []
    profile = delivery.get("profile") if isinstance(delivery.get("profile"), dict) else {}
    level = delivery.get("level")
    hours = delivery.get("hours")
    methods = _normalize_text_list([delivery.get("methods"), profile.get("methods")])
    facilities: list[Any] = _normalize_text_list([delivery.get("facilities"), profile.get("facilities")])
    for relation in relations:
        if relation.get("relation_type") == "has_level" and level is None:
            level = relation.get("numeric_value") or relation.get("relation_value")
        if relation.get("relation_type") == "requires_time" and hours is None:
            hours = relation.get("numeric_value") or relation.get("relation_value")
        if relation.get("relation_type") == "uses_facility":
            facility = relation.get("relation_value")
            if facility:
                facilities.append(facility)
        if relation.get("relation_type") == "delivered_by":
            method = relation.get("relation_value")
            if method:
                methods.extend(_normalize_text_list(method))
    return {
        "level": level,
        "hours": hours,
        "methods": methods,
        "facilities": list(dict.fromkeys(facilities)),
    }


def _public_course_card(card: dict[str, Any]) -> dict[str, Any]:
    public_card = dict(card)
    delivery = _course_delivery_brief(public_card)
    for key in PUBLIC_OPERATIONAL_METADATA_KEYS:
        public_card.pop(key, None)
    public_card["delivery"] = delivery
    fit = public_card.get("training_system_fit")
    if isinstance(fit, dict):
        public_fit = dict(fit)
        public_fit["course_fit"] = _course_delivery_brief({"delivery": public_fit.get("course_fit") or delivery})
        public_card["training_system_fit"] = public_fit
    return _strip_public_operational_metadata(public_card)


def _compact_alias_interpretation(alias: Any) -> dict[str, Any] | None:
    if not isinstance(alias, dict):
        return None
    review_status = _clean(alias.get("review_status"))
    return {
        "alias_text": alias.get("alias_text"),
        "normalized_query": alias.get("normalized_query"),
        "unit_code": alias.get("unit_code"),
        "confidence_score": alias.get("confidence_score"),
        "needs_human_review": bool(review_status and review_status not in TRUSTED_TRANSITION_REVIEW_STATUSES),
    }


def _scope_label_with_alias(
    scope: dict[str, Any],
    task: dict[str, Any],
    alias: Any,
    fallback: Any,
) -> Any:
    label = scope.get("match_text") or task.get("unit_name") or fallback
    if not isinstance(alias, dict):
        return label
    normalized = _clean(alias.get("normalized_query"))
    if normalized and normalized != _clean(label):
        return f"{normalized} ({label})" if label else normalized
    return label


def _compact_interpretation_alternatives(
    resolution: dict[str, Any] | None,
    *,
    selected_values: list[Any],
    limit: int = 4,
) -> list[dict[str, Any]]:
    selected = {_clean(value) for value in selected_values if _clean(value)}
    alternatives: list[dict[str, Any]] = []
    for item in _candidate_query_items(resolution, limit=limit + len(selected) + 3):
        query = _clean(item.get("query"))
        if not query or query in selected:
            continue
        alternatives.append(item)
        if len(alternatives) >= limit:
            break
    return alternatives


def _compact_transition_answer_summary(
    result: dict[str, Any],
    cards: list[dict[str, Any]],
    groups: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    transition = result.get("transition") if isinstance(result.get("transition"), dict) else {}
    summary = transition.get("summary") if isinstance(transition.get("summary"), dict) else {}
    current_scope = transition.get("current_scope") if isinstance(transition.get("current_scope"), dict) else {}
    target_scope = transition.get("target_scope") if isinstance(transition.get("target_scope"), dict) else {}
    current_task = transition.get("current_task") if isinstance(transition.get("current_task"), dict) else {}
    target_task = transition.get("target_task") if isinstance(transition.get("target_task"), dict) else {}
    current_query = summary.get("requested_current_query") or result.get("current_query")
    target_query = summary.get("requested_target_query") or result.get("target_query")
    current_alias = transition.get("current_query_alias")
    target_alias = transition.get("target_query_alias")
    current_label = _scope_label_with_alias(current_scope, current_task, current_alias, current_query)
    target_label = _scope_label_with_alias(target_scope, target_task, target_alias, target_query)
    current_resolution = transition.get("current_query_resolution")
    target_resolution = transition.get("target_query_resolution")
    current_alternatives = _compact_interpretation_alternatives(
        current_resolution,
        selected_values=[
            current_label,
            current_scope.get("match_text"),
            current_task.get("unit_name"),
            current_query,
            current_alias.get("normalized_query") if isinstance(current_alias, dict) else None,
        ],
    )
    target_alternatives = _compact_interpretation_alternatives(
        target_resolution,
        selected_values=[
            target_label,
            target_scope.get("match_text"),
            target_task.get("unit_name"),
            target_query,
            target_alias.get("normalized_query") if isinstance(target_alias, dict) else None,
        ],
    )
    exact_transferability = float(summary.get("exact_ksa_overlap_ratio") or summary.get("transferability_ratio") or 0.0)
    transferability = float(summary.get("ontology_adjusted_transferability_ratio") or exact_transferability)
    gap_count = int(summary.get("gap_ksa_concept_count") or 0)
    transferable_count = int(summary.get("transferable_ksa_concept_count") or 0)
    target_count = int(summary.get("target_ksa_concept_count") or 0)
    if transferability >= 0.35:
        assessment = "NCS 범위와 온톨로지 근접도가 높아 보완 교육 중심으로 접근할 수 있습니다."
    elif transferability >= 0.1:
        assessment = "일부 KSA와 인접 과업은 전이되지만 목표 직무 KSA 보완이 필요합니다."
    else:
        assessment = "동일 KSA와 인접 근거가 적어 목표 직무 기초부터 체계적으로 보완하는 편이 안전합니다."

    top_cards = cards[:3]
    recommended_path = []
    for card in top_cards:
        delivery = _course_delivery_brief(card)
        recommended_path.append(
            {
                "rank": card.get("rank"),
                "course_name": card.get("course_name"),
                "tier_label": card.get("tier_label"),
                "confidence_grade": card.get("confidence_grade"),
                "level": delivery.get("level"),
                "hours": delivery.get("hours"),
                "methods": delivery.get("methods"),
                "why": (card.get("why_recommended") or [])[:3],
            }
        )

    gap_ksa: list[str] = []
    for card in cards:
        highlights = card.get("evidence_highlights") if isinstance(card.get("evidence_highlights"), dict) else {}
        for item in highlights.get("gap_ksa") or []:
            if item and item not in gap_ksa:
                gap_ksa.append(item)
            if len(gap_ksa) >= 8:
                break
        if len(gap_ksa) >= 8:
            break

    caveats = [DISCLAIMER]
    current_alias = transition.get("current_query_alias")
    if isinstance(current_alias, dict) and current_alias.get("unit_code"):
        caveats.append(
            f"입력 '{current_query}'는 공식 NCS 분류명과 완전히 일치하지 않아 "
            f"'{current_alias.get('normalized_query')}' 기준 능력단위로 해석했습니다."
        )
    target_alias = transition.get("target_query_alias")
    if isinstance(target_alias, dict) and target_alias.get("unit_code"):
        caveats.append(
            f"목표 '{target_query}'는 '{target_alias.get('normalized_query')}' 능력단위로 해석했습니다."
        )
    if isinstance(current_alias, dict) and current_alias.get("review_status") == "candidate":
        caveats.append(f"입력 '{current_query}'의 별칭 해석은 candidate 상태라 사람 검토 전입니다.")
    if isinstance(target_alias, dict) and target_alias.get("review_status") == "candidate":
        caveats.append(f"목표 '{target_query}'의 별칭 해석은 candidate 상태라 사람 검토 전입니다.")
    if not groups.get("primary"):
        caveats.append("우선 추천 과정이 없어 결과를 참고 과정 중심으로만 해석해야 합니다.")

    target_direction = _with_korean_direction_particle(target_query)
    headline = f"{current_query}에서 {target_direction} 전환하려면 {target_label} 관련 교육을 우선 검토하세요."
    if top_cards:
        headline = f"{current_query}에서 {target_direction} 전환하려면 '{top_cards[0].get('course_name')}' 과정을 먼저 검토하세요."

    return {
        "headline": headline,
        "interpretation": {
            "current": {
                "requested": current_query,
                "resolved_as": current_label,
                "match_level": current_scope.get("match_level"),
                "unit_count": summary.get("current_scope_unit_count"),
                "task_element": current_task.get("element_name"),
                "query_alias": _compact_alias_interpretation(current_alias),
                "alternatives": current_alternatives,
            },
            "target": {
                "requested": target_query,
                "resolved_as": target_label,
                "match_level": target_scope.get("match_level"),
                "unit_count": summary.get("target_scope_unit_count"),
                "task_element": target_task.get("element_name"),
                "query_alias": _compact_alias_interpretation(target_alias),
                "alternatives": target_alternatives,
            },
        },
        "transition_assessment": {
            "summary": assessment,
            "transferability_ratio": transferability,
            "exact_ksa_overlap_ratio": exact_transferability,
            "ontology_adjusted_transferability_ratio": transferability,
            "transferability_method": summary.get("transferability_method"),
            "adjusted_transferability_components": summary.get("adjusted_transferability_components") or {},
            "ncs_scope_relation": summary.get("ncs_scope_relation"),
            "current_scope_subset_of_target": bool(summary.get("current_scope_subset_of_target")),
            "target_role_overlay": summary.get("target_role_overlay"),
            "transferable_ksa_count": transferable_count,
            "target_ksa_count": target_count,
            "gap_ksa_count": gap_count,
        },
        "key_gap_ksa": gap_ksa,
        "recommended_path": recommended_path,
        "caveats": caveats,
    }


def compact_training_task_response(result: dict[str, Any], *, recommendation_limit: int = DEFAULT_RECOMMENDATIONS) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    max_items = clamp_limit(recommendation_limit, default=DEFAULT_RECOMMENDATIONS, maximum=MAX_RECOMMENDATIONS)
    groups, items = _items_from_groups_or_recommendations(result)
    scope = result.get("resolved_scope") or {}
    summary = result.get("recommendation_summary") or {}
    cards = [
        _compact_course_card(
            item,
            preferred_facilities=summary.get("preferred_facilities") or [],
        )
        for item in items[:max_items]
    ]
    return {
        "ok": True,
        "view": "compact_training_task",
        "disclaimer": result.get("disclaimer") or DISCLAIMER,
        "requested": {
            "query": result.get("requested_query"),
            "criteria_id": result.get("requested_criteria_id"),
            "unit_code": result.get("requested_unit_code"),
            "preferred_max_hours": summary.get("preferred_max_hours"),
            "preferred_methods": summary.get("preferred_methods") or [],
        },
        "scope_interpretation": {
            "match_text": scope.get("match_text"),
            "match_level": scope.get("match_level"),
            "unit_count": len(scope.get("unit_codes") or []),
        },
        "source_task": result.get("source_task") or {},
        "recommendation_summary": summary,
        "source_recommendation_counts": {key: len(value) for key, value in groups.items()},
        "source_task": result.get("source_task") or {},
        "current_task": result.get("current_task") or {},
        "target_task": result.get("target_task") or result.get("source_task") or {},
        "recommended_courses": cards,
        "gaps": result.get("gaps") or {},
        "input_quality": _input_quality_for_task(result),
        "audit": _compact_audit(result),
    }


def compact_training_transition_response(result: dict[str, Any], *, recommendation_limit: int = DEFAULT_RECOMMENDATIONS) -> dict[str, Any]:
    if not result.get("ok"):
        return result
    max_items = clamp_limit(recommendation_limit, default=DEFAULT_RECOMMENDATIONS, maximum=MAX_RECOMMENDATIONS)
    groups, items = _items_from_groups_or_recommendations(result)
    transition = result.get("transition") or {}
    summary = transition.get("summary") or {}
    cards = [
        _compact_course_card(
            item,
            transition=True,
            preferred_facilities=summary.get("preferred_facilities") or [],
        )
        for item in items[:max_items]
    ]
    answer_summary = _compact_transition_answer_summary(result, cards, groups)
    return {
        "ok": True,
        "view": "compact_training_transition",
        "disclaimer": result.get("disclaimer") or DISCLAIMER,
        "answer_summary": answer_summary,
        "requested": {
            "current_query": summary.get("requested_current_query") or result.get("current_query"),
            "target_query": summary.get("requested_target_query") or result.get("target_query"),
            "preferred_max_hours": summary.get("preferred_max_hours"),
            "preferred_methods": summary.get("preferred_methods") or [],
            "preferred_facilities": summary.get("preferred_facilities") or [],
        },
        "scope_interpretation": {
            "current": transition.get("current_scope") or {},
            "target": transition.get("target_scope") or {},
            "transferability_ratio": summary.get("transferability_ratio"),
            "exact_ksa_overlap_ratio": summary.get("exact_ksa_overlap_ratio"),
            "ontology_adjusted_transferability_ratio": summary.get("ontology_adjusted_transferability_ratio"),
            "transferability_method": summary.get("transferability_method"),
        },
        "transition_summary": summary,
        "job_base_transition_profile": _job_base_transition_profile(result),
        "source_recommendation_counts": {key: len(value) for key, value in groups.items()},
        "recommended_courses": cards,
        "input_quality": _input_quality_for_transition(result, groups),
        "audit": _compact_audit(result),
    }


def _planning_course_brief(card: dict[str, Any]) -> dict[str, Any]:
    delivery = _course_delivery_brief(card)
    return {
        "rank": card.get("rank"),
        "course_name": card.get("course_name"),
        "training_course_id": card.get("training_course_id"),
        "tier": card.get("tier"),
        "tier_label": card.get("tier_label"),
        "confidence_grade": card.get("confidence_grade"),
        "confidence_score": card.get("confidence_score"),
        "level": delivery.get("level"),
        "hours": delivery.get("hours"),
        "methods": delivery.get("methods"),
        "facilities": delivery.get("facilities"),
        "coverage_summary": card.get("coverage_summary") or [],
        "evidence_highlights": card.get("evidence_highlights") or {},
        "career_path_review_basis": card.get("career_path_review_basis") or {},
        "quality_issue_penalty": card.get("quality_issue_penalty") or {},
        "why_recommended": (card.get("why_recommended") or [])[:3],
        "fit_summary": card.get("fit_summary") or [],
        "training_system_fit": card.get("training_system_fit") or {},
    }


def _planning_stage_courses(cards: list[dict[str, Any]], tiers: set[str], *, limit: int) -> list[dict[str, Any]]:
    courses = [
        _planning_course_brief(card)
        for card in cards
        if card.get("tier") in tiers
    ]
    return courses[:limit]


def _transition_review_basis(transition_summary: dict[str, Any]) -> dict[str, Any]:
    current_trusted = int(transition_summary.get("current_trusted_career_path_count") or 0)
    target_trusted = int(transition_summary.get("target_trusted_career_path_count") or 0)
    current_counts = transition_summary.get("current_career_path_review_status_counts")
    target_counts = transition_summary.get("target_career_path_review_status_counts")
    if not isinstance(current_counts, dict):
        current_counts = {}
    if not isinstance(target_counts, dict):
        target_counts = {}
    return {
        "schema": "aihr_transition_review_basis_v1",
        "status": "trusted_career_path_visible"
        if current_trusted or target_trusted
        else "review_required",
        "current_trusted_career_path_count": current_trusted,
        "target_trusted_career_path_count": target_trusted,
        "current_career_path_review_state_counts": current_counts,
        "target_career_path_review_state_counts": target_counts,
        "basis_sources": ["ncs_career_paths", "2026_hr_ncs_training_system_guide"],
        "basis_role": "movement_scope_and_level_review_not_course_or_ksa_approval",
        "approval_claim": False,
        "db_writes": False,
        "human_review_required_for_approval": True,
    }


def _fallback_training_system_fit(card: dict[str, Any]) -> dict[str, Any]:
    tier = card.get("tier")
    highlights = card.get("evidence_highlights") if isinstance(card.get("evidence_highlights"), dict) else {}
    basis_types: list[str] = []
    if highlights.get("target_scope_ksa") or highlights.get("source_ksa"):
        basis_types.append("target_scope_ksa")
    if highlights.get("gap_ksa"):
        basis_types.append("gap_ksa")
    if highlights.get("goal_ksa"):
        basis_types.append("training_goal_ksa")
    if highlights.get("covered_elements"):
        basis_types.append("competency_element")
    has_task_ksa_or_goal_evidence = bool(basis_types)
    need_code = {
        "primary": "required",
        "supplemental": "supporting",
        "adjacent": "adjacent_reference",
        "adjacent_reference": "adjacent_reference",
    }.get(str(tier), "optional")
    if need_code == "required" and not has_task_ksa_or_goal_evidence:
        need_code = "supporting"
    need_label = {
        "required": "필수 검토",
        "supporting": "보완 추천",
        "optional": "선택 후보",
        "adjacent_reference": "인접 참고",
    }[need_code]
    review_flags = ["fallback_from_compact_card"]
    if tier == "primary" and need_code != "required":
        review_flags.append("primary_demoted_without_direct_task_ksa_or_goal")
    if need_code == "adjacent_reference":
        review_flags.append("adjacent_reference_only")
    course_scope_fit = _public_course_scope_fit(card.get("course_scope_fit"))
    if course_scope_fit.get("requires_scope_review"):
        review_flags.append(f"course_scope_{course_scope_fit.get('alignment')}_requires_review")
    review_flags = list(dict.fromkeys(review_flags))
    return {
        "rubric_source": "2026_hr_ncs_training_system_guide",
        "rubric_role": "framework_reference_not_scoring_source",
        "need_classification": {
            "code": need_code,
            "label": need_label,
            "rationale": "컴팩트 카드의 추천 그룹을 교육체계도 초안 분류로 변환했습니다.",
        },
        "evidence_directness": {"code": "compact_card", "label": "컴팩트 카드 근거"},
        "task_ksa_basis": {
            "basis_types": basis_types,
            "target_scope_ksa": highlights.get("target_scope_ksa") or highlights.get("source_ksa") or [],
            "gap_ksa": highlights.get("gap_ksa") or [],
            "training_goal_ksa": highlights.get("goal_ksa") or [],
            "covered_elements": highlights.get("covered_elements") or [],
        },
        "course_fit": _course_delivery_brief(card),
        "course_scope_fit": course_scope_fit,
        "review_flags": review_flags,
    }


def _level_band(level: Any) -> dict[str, Any]:
    if level is None or level == "":
        return {
            "code": "unknown",
            "label": "unknown",
            "level": None,
            "rationale": "Course level is not available in the training-course evidence.",
        }
    try:
        numeric = float(level)
    except (TypeError, ValueError):
        return {
            "code": "unknown",
            "label": "unknown",
            "level": level,
            "rationale": "Course level is present but not numeric.",
        }
    if numeric <= 2:
        code = "level_1_2"
    elif numeric <= 4:
        code = "level_3_4"
    elif numeric <= 6:
        code = "level_5_6"
    else:
        code = "level_7_plus"
    return {
        "code": code,
        "label": code.replace("_", " "),
        "level": level,
        "rationale": "Band derived from the course ability-unit level for planner grouping.",
    }


def _education_type_from_delivery(course_fit: dict[str, Any]) -> dict[str, Any]:
    methods = _normalize_text_list(course_fit.get("methods") or [])
    facilities = _normalize_text_list(course_fit.get("facilities") or [])
    method_groups = _method_groups(methods)
    facility_groups = _method_groups(facilities)
    hours = _parse_number(course_fit.get("hours"))
    code = "unknown"
    label = "unknown"
    evidence_source = "none"
    if "practice" in method_groups and ("classroom" in method_groups or "remote" in method_groups):
        code = "blended_practice"
        label = "blended practice"
        evidence_source = "method"
    elif "practice" in method_groups:
        code = "practice_or_field"
        label = "practice or field"
        evidence_source = "method"
    elif "remote" in method_groups:
        code = "remote_or_online"
        label = "remote or online"
        evidence_source = "method"
    elif "classroom" in method_groups:
        code = "classroom_or_lecture"
        label = "classroom or lecture"
        evidence_source = "method"
    elif methods:
        code = "method_specified"
        label = "method specified"
        evidence_source = "method"
    elif facilities:
        code = "facility_specified"
        label = "facility specified"
        evidence_source = "facility"
    elif hours is not None:
        code = "time_only"
        label = "time evidence only"
        evidence_source = "time"
    return {
        "code": code,
        "label": label,
        "evidence_source": evidence_source,
        "rationale": (
            "Derived only from training method, facility, and hour evidence for C2-1 grouping; "
            "course title text is not used and this does not change recommendation scoring."
        ),
        "evidence_basis": {
            "methods": methods,
            "facilities": facilities,
            "hours": course_fit.get("hours"),
            "method_groups": sorted(method_groups),
            "facility_groups": sorted(facility_groups),
        },
    }


def _method_constraint_fit(course_fit: dict[str, Any], preferred_methods: list[Any] | None) -> dict[str, Any]:
    requested = _normalize_text_list(preferred_methods)
    available = _normalize_text_list(course_fit.get("methods") or [])
    requested_groups = _method_groups(requested)
    available_groups = _method_groups(available)
    if not requested:
        status = "not_requested"
        matched_groups: list[str] = []
        missing_groups: list[str] = []
        rationale = "No training-method constraint was requested."
    elif not available:
        status = "unknown"
        matched_groups = []
        missing_groups = sorted(requested_groups) or requested
        rationale = "Training-method evidence is not available for this course."
    else:
        matched_groups = sorted(requested_groups & available_groups)
        missing_groups = sorted(requested_groups - available_groups)
        status = "fit" if requested_groups and not missing_groups else "partial" if matched_groups else "mismatch"
        rationale = "Compared requested methods with course method evidence using normalized method groups."
    return {
        "status": status,
        "requested": requested,
        "available": available,
        "requested_method_groups": sorted(requested_groups),
        "available_method_groups": sorted(available_groups),
        "matched_method_groups": matched_groups,
        "missing_method_groups": missing_groups,
        "rationale": rationale,
    }


def _time_constraint_fit(course_fit: dict[str, Any], preferred_max_hours: Any) -> dict[str, Any]:
    preferred = _parse_number(preferred_max_hours)
    actual = _parse_number(course_fit.get("hours"))
    if preferred is None:
        status = "not_requested"
        over_ratio = None
        rationale = "No maximum-hours constraint was requested."
    elif actual is None:
        status = "unknown"
        over_ratio = None
        rationale = "Training-hour evidence is not available for this course."
    elif actual <= preferred:
        status = "fit"
        over_ratio = 0.0
        rationale = "Course hours fit within the requested maximum hours."
    else:
        status = "over"
        over_ratio = round((actual - preferred) / preferred, 4) if preferred else None
        rationale = "Course hours exceed the requested maximum hours."
    return {
        "status": status,
        "preferred_max_hours": preferred,
        "actual_hours": actual,
        "time_over_ratio": over_ratio,
        "rationale": rationale,
    }


def _delivery_operation(
    course_fit: dict[str, Any],
    *,
    preferred_methods: list[Any] | None = None,
    preferred_max_hours: Any = None,
) -> dict[str, Any]:
    methods = [str(item) for item in course_fit.get("methods") or [] if str(item).strip()]
    facilities = [str(item) for item in course_fit.get("facilities") or [] if str(item).strip()]
    if methods and facilities:
        code = "method_and_facility_specified"
    elif methods:
        code = "method_specified"
    elif facilities:
        code = "facility_specified"
    else:
        code = "unknown"
    return {
        "code": code,
        "methods": methods,
        "facilities": facilities,
        "hours": course_fit.get("hours"),
        "method_constraint_fit": _method_constraint_fit(course_fit, preferred_methods),
        "time_constraint_fit": _time_constraint_fit(course_fit, preferred_max_hours),
        "rationale": "Delivery operation is summarized from training method, facility, and hours evidence.",
    }


def _facility_constraint_fit(course_fit: dict[str, Any], preferred_facilities: list[Any] | None) -> dict[str, Any]:
    requested = _normalize_text_list(preferred_facilities)
    available = _normalize_text_list(course_fit.get("facilities") or [])
    if not requested:
        status = "not_requested"
        matched: list[str] = []
        missing: list[str] = []
        rationale = "No facility constraint was requested."
    elif not available:
        status = "unknown"
        matched = []
        missing = requested
        rationale = "Facility evidence is not available for this course."
    else:
        available_keys = {normalize_concept_key(item): item for item in available}
        matched = []
        missing = []
        for item in requested:
            key = normalize_concept_key(item)
            if key in available_keys:
                matched.append(available_keys[key])
            else:
                missing.append(item)
        status = "fit" if matched and not missing else "partial" if matched else "mismatch"
        rationale = "Compared requested facilities with course facility evidence."
    return {
        "status": status,
        "requested": requested,
        "available": available,
        "matched": matched,
        "missing": missing,
        "rationale": rationale,
    }


def _delivery_constraint_fit_summary(
    *,
    method_fit: dict[str, Any],
    time_fit: dict[str, Any],
    facility_fit: dict[str, Any],
) -> dict[str, Any]:
    statuses = {
        "method": str(method_fit.get("status") or "unknown"),
        "time": str(time_fit.get("status") or "unknown"),
        "facility": str(facility_fit.get("status") or "unknown"),
    }
    requested = {
        key: value
        for key, value in statuses.items()
        if value not in {"not_requested", ""}
    }
    if not requested:
        status = "not_requested"
    elif all(value == "fit" for value in requested.values()):
        status = "fit"
    elif any(value in {"mismatch", "over"} for value in requested.values()):
        status = "mismatch"
    else:
        status = "needs_review"
    return {
        "status": status,
        "dimensions": statuses,
        "rationale": "Aggregates requested method, time, and facility constraints for C2-2 delivery planning.",
    }


def _delivery_review_flags(
    *,
    method_fit: dict[str, Any],
    time_fit: dict[str, Any],
    facility_fit: dict[str, Any],
) -> list[str]:
    flags: list[str] = []
    for prefix, fit in (
        ("method", method_fit),
        ("time", time_fit),
        ("facility", facility_fit),
    ):
        status = str(fit.get("status") or "")
        if status in {"not_requested", "fit", ""}:
            continue
        flags.append(f"delivery:{prefix}_{status}")
        if prefix == "facility":
            flags.append(f"facility_constraint_{status}")
    return flags


def _planner_grouping(
    *,
    current_label: Any,
    target_label: Any,
    need: dict[str, Any],
    level_band: dict[str, Any],
    education_type: dict[str, Any],
    delivery_operation: dict[str, Any],
    course_scope_fit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    delivery_methods = delivery_operation.get("methods") or []
    scope_fit = course_scope_fit if isinstance(course_scope_fit, dict) else {}
    return {
        "job_scope": f"{current_label} -> {target_label}",
        "target_level_band": level_band.get("code") or "unknown",
        "education_type": education_type.get("code") or "unknown",
        "required_optional": need.get("code") or "unknown",
        "delivery_method": delivery_methods[0] if delivery_methods else "unknown",
        "course_scope_relation": scope_fit.get("relation") or "unknown",
    }


def _task_ksa_basis_contract(fit: dict[str, Any]) -> dict[str, Any]:
    basis = fit.get("task_ksa_basis") if isinstance(fit.get("task_ksa_basis"), dict) else {}
    target_scope_ksa = basis.get("target_scope_ksa") or []
    gap_ksa = basis.get("gap_ksa") or []
    training_goal_ksa = basis.get("training_goal_ksa") or []
    covered_elements = basis.get("covered_elements") or []
    return {
        **basis,
        "basis_types": basis.get("basis_types") or [],
        "target_scope_ksa": target_scope_ksa,
        "gap_ksa": gap_ksa,
        "training_goal_ksa": training_goal_ksa,
        "covered_elements": covered_elements,
        "target_scope_ksa_count": len(target_scope_ksa),
        "gap_ksa_count": len(gap_ksa),
        "training_goal_ksa_count": len(training_goal_ksa),
        "covered_element_count": len(covered_elements),
    }


def _mapping_strength_contract(
    *,
    need: dict[str, Any],
    directness: dict[str, Any],
    task_ksa_basis: dict[str, Any],
    course_scope_fit: dict[str, Any],
) -> dict[str, Any]:
    return {
        "target_scope_ksa_count": int(task_ksa_basis.get("target_scope_ksa_count") or 0),
        "gap_ksa_count": int(task_ksa_basis.get("gap_ksa_count") or 0),
        "training_goal_ksa_count": int(task_ksa_basis.get("training_goal_ksa_count") or 0),
        "covered_element_count": int(task_ksa_basis.get("covered_element_count") or 0),
        "course_scope_relation": course_scope_fit.get("relation") or "unknown",
        "course_scope_alignment": course_scope_fit.get("alignment") or "unknown",
        "evidence_directness": directness.get("code") or "unknown",
        "required_optional": need.get("code") or "unknown",
        "review_required": bool(course_scope_fit.get("requires_scope_review")),
    }


def _mapping_strength_warning(mapping_strength: dict[str, Any]) -> dict[str, Any]:
    target_count = int(mapping_strength.get("target_scope_ksa_count") or 0)
    gap_count = int(mapping_strength.get("gap_ksa_count") or 0)
    goal_count = int(mapping_strength.get("training_goal_ksa_count") or 0)
    element_count = int(mapping_strength.get("covered_element_count") or 0)
    directness = _clean(mapping_strength.get("evidence_directness"))
    required_optional = _clean(mapping_strength.get("required_optional"))
    scope_relation = _clean(mapping_strength.get("course_scope_relation"))
    scope_alignment = _clean(mapping_strength.get("course_scope_alignment"))
    codes: list[str] = []
    if not any([target_count, gap_count, goal_count, element_count]):
        codes.append("no_task_ksa_or_element_evidence")
    if target_count == 0 and element_count == 0:
        codes.append("missing_target_ksa_and_task_element")
    if required_optional == "required" and gap_count == 0 and goal_count == 0:
        codes.append("required_without_gap_or_goal_ksa")
    if directness in {"weak", "compact_card", "unknown", ""}:
        codes.append("weak_evidence_directness")
    if scope_relation in {"unknown", "broad_scope", "outside_scope"} or scope_alignment in {"unknown", "broad", "outside"}:
        codes.append("weak_course_scope_alignment")
    if mapping_strength.get("review_required"):
        codes.append("course_scope_review_required")
    codes = list(dict.fromkeys(codes))
    return {
        "status": "warning" if codes else "clear",
        "codes": codes,
        "basis": {
            "target_scope_ksa_count": target_count,
            "gap_ksa_count": gap_count,
            "training_goal_ksa_count": goal_count,
            "covered_element_count": element_count,
            "course_scope_relation": scope_relation or "unknown",
            "course_scope_alignment": scope_alignment or "unknown",
            "evidence_directness": directness or "unknown",
            "required_optional": required_optional or "unknown",
        },
        "message": (
            "Review mapping strength before treating this course as a core education-system row."
            if codes
            else "Mapping-strength evidence risk was not detected."
        ),
    }


def _decision_state_contract(
    *,
    need: dict[str, Any],
    review_flags: list[str],
    mapping_strength_warning: dict[str, Any],
) -> dict[str, Any]:
    suggested = need.get("code") or "unknown"
    warning_codes = [
        str(code)
        for code in mapping_strength_warning.get("codes") or []
        if str(code).strip()
    ]
    requires_attention = bool(review_flags or warning_codes or suggested in {"unknown", "adjacent_reference"})
    return {
        "schema": "aihr_training_row_decision_state_v1",
        "status": "pending_human_decision",
        "decision_required": True,
        "system_suggestion": suggested,
        "allowed_decisions": [
            "required",
            "optional",
            "supporting",
            "adjacent_reference",
            "defer",
            "reject",
        ],
        "approval_claim": False,
        "evidence_attention_required": requires_attention,
        "basis": {
            "review_flag_count": len(review_flags),
            "mapping_strength_warning_codes": warning_codes,
            "need_classification": suggested,
        },
        "message": (
            "No row is approved by automation; confirm the final required/optional/supporting/reference decision with a human reviewer."
        ),
    }


def _evidence_chain_contract(
    *,
    current_label: Any,
    target_label: Any,
    card: dict[str, Any],
    fit: dict[str, Any],
    task_ksa_basis: dict[str, Any],
    target_task: dict[str, Any] | None = None,
    target_task_element: Any = None,
) -> dict[str, Any]:
    task = target_task if isinstance(target_task, dict) else {}
    criteria_text = _clean(task.get("criteria_text") or task.get("criteria_text_raw"))
    element_name = _clean(task.get("element_name") or task.get("element_name_raw") or target_task_element)
    unit_name = _clean(task.get("unit_name") or task.get("unit_name_raw") or target_label)
    ksa_samples = list(
        dict.fromkeys(
            [
                *_normalize_text_list(task_ksa_basis.get("gap_ksa") or []),
                *_normalize_text_list(task_ksa_basis.get("training_goal_ksa") or []),
                *_normalize_text_list(task_ksa_basis.get("target_scope_ksa") or []),
            ]
        )
    )
    covered_elements = _normalize_text_list(task_ksa_basis.get("covered_elements") or [])
    task_sample = criteria_text or (covered_elements[0] if covered_elements else element_name)
    links = [
        {
            "stage": "job_scope",
            "label": "Job or NCS scope",
            "value": f"{current_label} -> {target_label}",
            "evidence_source": "scope_baseline",
        },
        {
            "stage": "duty_task",
            "label": "Duty/task or competency element",
            "value": element_name or unit_name or "unknown",
            "evidence_source": "target_task.element_name_or_scope_task_element",
        },
        {
            "stage": "performance_criterion",
            "label": "Performance criterion",
            "value": task_sample or "not_visible",
            "evidence_source": "target_task.criteria_text_or_covered_elements",
        },
        {
            "stage": "ksa",
            "label": "KSA evidence",
            "value": ksa_samples[:5],
            "evidence_source": "task_ksa_basis",
        },
        {
            "stage": "training_course",
            "label": "Training course",
            "value": card.get("course_name") or "unknown",
            "evidence_source": "ncs_training_courses",
        },
    ]
    missing = [
        item["stage"]
        for item in links
        if item["value"] in (None, "", "unknown", "not_visible", [])
    ]
    return {
        "schema": "aihr_course_evidence_chain_v1",
        "chain_order": [item["stage"] for item in links],
        "links": links,
        "basis_types": task_ksa_basis.get("basis_types") or [],
        "covered_elements": covered_elements[:5],
        "course_scope_relation": (fit.get("course_scope_fit") or {}).get("relation"),
        "completeness": {
            "status": "complete" if not missing else "partial",
            "missing_stages": missing,
        },
        "message": "Evidence chain follows the NCS guide order: job scope -> duty/task -> performance criterion -> KSA -> training course.",
    }


def _course_link_contract(
    card: dict[str, Any],
    fit: dict[str, Any],
    task_ksa_basis: dict[str, Any],
) -> dict[str, Any]:
    directness = fit.get("evidence_directness") if isinstance(fit.get("evidence_directness"), dict) else {}
    need = fit.get("need_classification") if isinstance(fit.get("need_classification"), dict) else {}
    course_scope_fit = _public_course_scope_fit(fit.get("course_scope_fit") or card.get("course_scope_fit"))
    return {
        "course_name": card.get("course_name"),
        "training_course_id": card.get("training_course_id"),
        "mapping_chain": fit.get("mapping_chain")
        or [
            "job_or_ncs_scope",
            "duty_or_task",
            "performance_criterion",
            "ksa",
            "training_course",
        ],
        "evidence_directness": {
            "code": directness.get("code") or "unknown",
            "label": directness.get("label") or "unknown",
        },
        "need_classification": {
            "code": need.get("code") or "unknown",
            "label": need.get("label") or "unknown",
        },
        "basis_types": task_ksa_basis.get("basis_types") or [],
        "target_scope_ksa": (task_ksa_basis.get("target_scope_ksa") or [])[:5],
        "gap_ksa": (task_ksa_basis.get("gap_ksa") or [])[:5],
        "training_goal_ksa": (task_ksa_basis.get("training_goal_ksa") or [])[:5],
        "covered_elements": (task_ksa_basis.get("covered_elements") or [])[:5],
        "mapping_strength": _mapping_strength_contract(
            need=need,
            directness=directness,
            task_ksa_basis=task_ksa_basis,
            course_scope_fit=course_scope_fit,
        ),
        "course_scope_fit": course_scope_fit,
        "why_recommended": (card.get("why_recommended") or [])[:3],
    }


def _specificity_warning(
    fit: dict[str, Any],
    task_ksa_basis: dict[str, Any],
) -> dict[str, Any]:
    directness = fit.get("evidence_directness") if isinstance(fit.get("evidence_directness"), dict) else {}
    need = fit.get("need_classification") if isinstance(fit.get("need_classification"), dict) else {}
    directness_code = _clean(directness.get("code"))
    need_code = _clean(need.get("code"))
    basis_types = [str(item) for item in task_ksa_basis.get("basis_types") or [] if str(item).strip()]
    codes: list[str] = []
    if directness_code in {"weak", "compact_card", "unknown", ""}:
        codes.append("weak_or_missing_direct_evidence")
    if directness_code == "unit_scope":
        codes.append("unit_scope_only")
    if not basis_types:
        codes.append("missing_task_ksa_basis")
    if need_code == "adjacent_reference":
        codes.append("adjacent_reference_only")
    codes = list(dict.fromkeys(codes))
    return {
        "status": "warning" if codes else "clear",
        "codes": codes,
        "basis": {
            "evidence_directness": directness_code or "unknown",
            "required_optional": need_code or "unknown",
            "basis_types": basis_types,
        },
        "message": (
            "Review specificity before using this course as core training."
            if codes
            else "Task/KSA/course-link specificity risk was not detected."
        ),
    }


def _duplicate_or_generic_warning(
    card: dict[str, Any],
    *,
    duplicate_count: int,
) -> dict[str, Any]:
    course_name = _clean(card.get("course_name"))
    tokens = _significant_tokens(course_name)
    codes: list[str] = []
    if duplicate_count > 1:
        codes.append("duplicate_course_name_in_plan")
    if course_name and len(tokens) <= 1:
        codes.append("generic_or_low_specificity_course_name")
    if not course_name:
        codes.append("missing_course_name")
    codes = list(dict.fromkeys(codes))
    return {
        "status": "warning" if codes else "clear",
        "codes": codes,
        "basis": {
            "course_name": course_name,
            "duplicate_count": duplicate_count,
            "significant_tokens": tokens[:5],
        },
        "message": (
            "Review duplicate or generic course risk before finalizing the education plan."
            if codes
            else "Duplicate/generic course risk was not detected."
        ),
    }


def _training_system_matrix(
    cards: list[dict[str, Any]],
    *,
    current_label: Any,
    target_label: Any,
    target_task: dict[str, Any] | None = None,
    target_task_element: Any = None,
    preferred_methods: list[Any] | None = None,
    preferred_max_hours: Any = None,
    preferred_facilities: list[Any] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    scoped_cards = cards[:limit]
    course_name_counts: dict[str, int] = {}
    for card in scoped_cards:
        course_name = _clean(card.get("course_name"))
        if course_name:
            course_name_counts[course_name] = course_name_counts.get(course_name, 0) + 1
    for card in scoped_cards:
        fit = card.get("training_system_fit") or _fallback_training_system_fit(card)
        course_fit = fit.get("course_fit") or _course_delivery_brief(card)
        need = fit.get("need_classification") or {}
        education_type = _education_type_from_delivery(course_fit)
        level_band = _level_band(course_fit.get("level"))
        delivery_operation = _delivery_operation(
            course_fit,
            preferred_methods=preferred_methods,
            preferred_max_hours=preferred_max_hours,
        )
        facility_fit = _facility_constraint_fit(course_fit, preferred_facilities)
        task_ksa_basis = _task_ksa_basis_contract(fit)
        delivery_operation["facility_constraint_fit"] = facility_fit
        delivery_operation["constraint_fit"] = _delivery_constraint_fit_summary(
            method_fit=delivery_operation["method_constraint_fit"],
            time_fit=delivery_operation["time_constraint_fit"],
            facility_fit=facility_fit,
        )
        specificity_warning = _specificity_warning(fit, task_ksa_basis)
        duplicate_or_generic_warning = _duplicate_or_generic_warning(
            card,
            duplicate_count=course_name_counts.get(_clean(card.get("course_name")), 0),
        )
        warning_flags = [
            f"specificity:{code}" for code in specificity_warning.get("codes") or []
        ] + [
            f"duplicate_or_generic:{code}"
            for code in duplicate_or_generic_warning.get("codes") or []
        ]
        delivery_flags = _delivery_review_flags(
            method_fit=delivery_operation["method_constraint_fit"],
            time_fit=delivery_operation["time_constraint_fit"],
            facility_fit=facility_fit,
        )
        course_link = _course_link_contract(card, fit, task_ksa_basis)
        course_scope_fit = _public_course_scope_fit(fit.get("course_scope_fit") or card.get("course_scope_fit"))
        mapping_strength = _mapping_strength_contract(
            need=need,
            directness=fit.get("evidence_directness") or {},
            task_ksa_basis=task_ksa_basis,
            course_scope_fit=course_scope_fit,
        )
        mapping_strength_warning = _mapping_strength_warning(mapping_strength)
        warning_flags.extend(
            f"mapping_strength:{code}" for code in mapping_strength_warning.get("codes") or []
        )
        quality_penalty = (
            card.get("quality_issue_penalty")
            if isinstance(card.get("quality_issue_penalty"), dict)
            else {}
        )
        quality_flags = [
            f"quality_issue:{issue_type}"
            for issue_type in quality_penalty.get("issue_types") or []
            if str(issue_type).strip()
        ]
        review_flags = list(
            dict.fromkeys([*(fit.get("review_flags") or []), *warning_flags, *delivery_flags, *quality_flags])
        )
        decision_state = _decision_state_contract(
            need=need,
            review_flags=review_flags,
            mapping_strength_warning=mapping_strength_warning,
        )
        if decision_state.get("status") == "pending_human_decision":
            review_flags = list(dict.fromkeys([*review_flags, "pending_human_decision"]))
        review_severity = "needs_review" if review_flags else "ready"
        evidence_chain = _evidence_chain_contract(
            current_label=current_label,
            target_label=target_label,
            card=card,
            fit=fit,
            task_ksa_basis=task_ksa_basis,
            target_task=target_task,
            target_task_element=target_task_element,
        )
        rows.append(
            {
                "rank": card.get("rank"),
                "course_name": card.get("course_name"),
                "course_goal": card.get("course_goal") or "",
                "training_course_id": card.get("training_course_id"),
                "current_scope": current_label,
                "target_scope": target_label,
                "job_scope": {
                    "current": current_label,
                    "target": target_label,
                    "transition": f"{current_label} -> {target_label}",
                },
                "target_level_band": level_band,
                "education_type": education_type,
                "required_optional": need.get("code") or "unknown",
                "required_optional_basis": {
                    "code": need.get("code") or "unknown",
                    "label": need.get("label") or "unknown",
                    "rationale": need.get("rationale") or "",
                },
                "delivery_operation": delivery_operation,
                "planner_grouping": _planner_grouping(
                    current_label=current_label,
                    target_label=target_label,
                    need=need,
                    level_band=level_band,
                    education_type=education_type,
                    delivery_operation=delivery_operation,
                    course_scope_fit=course_scope_fit,
                ),
                "need_classification": need,
                "evidence_directness": fit.get("evidence_directness") or {},
                "task_ksa_basis": task_ksa_basis,
                "course_link": course_link,
                "career_path_review_basis": card.get("career_path_review_basis") or {},
                "quality_issue_penalty": quality_penalty,
                "course_scope_fit": course_scope_fit,
                "mapping_strength": mapping_strength,
                "mapping_strength_warning": mapping_strength_warning,
                "decision_state": decision_state,
                "evidence_chain": evidence_chain,
                "course_fit": course_fit,
                "facility_constraint_fit": facility_fit,
                "specificity_warning": specificity_warning,
                "duplicate_or_generic_warning": duplicate_or_generic_warning,
                "review_flags": review_flags,
                "human_review": {
                    "severity": review_severity,
                    "prompt": fit.get("human_review_prompt")
                    or "Confirm job/task/KSA fit and classify this course as required, optional, supporting, or reference.",
                    "action": "review_training_system_row",
                    "review_board_hint": "Use the review board for rows with weak, adjacent, or incomplete evidence.",
                    "flags": review_flags,
                },
                "why_recommended": (card.get("why_recommended") or [])[:3],
            }
        )
    return rows


def _training_system_summary(matrix: list[dict[str, Any]]) -> dict[str, Any]:
    need_counts: dict[str, int] = {}
    directness_counts: dict[str, int] = {}
    review_flag_counts: dict[str, int] = {}
    specificity_warning_counts: dict[str, int] = {}
    duplicate_or_generic_warning_counts: dict[str, int] = {}
    mapping_strength_warning_counts: dict[str, int] = {}
    quality_issue_penalty_counts: dict[str, int] = {}
    decision_state_counts: dict[str, int] = {}
    evidence_chain_status_counts: dict[str, int] = {}
    level_band_counts: dict[str, int] = {}
    education_type_counts: dict[str, int] = {}
    education_type_evidence_source_counts: dict[str, int] = {}
    delivery_method_counts: dict[str, int] = {}
    delivery_operation_counts: dict[str, int] = {}
    course_scope_relation_counts: dict[str, int] = {}
    course_scope_alignment_counts: dict[str, int] = {}
    delivery_constraint_fit_counts: dict[str, dict[str, int]] = {
        "overall": {},
        "method": {},
        "time": {},
        "facility": {},
    }
    job_scope_counts: dict[str, int] = {}
    required_course_names: list[str] = []
    review_required_course_names: list[str] = []
    quality_issue_penalty_course_names: list[str] = []
    quality_issue_penalty_review_required_count = 0
    for row in matrix:
        need_code = str((row.get("need_classification") or {}).get("code") or "unknown")
        directness_code = str((row.get("evidence_directness") or {}).get("code") or "unknown")
        grouping = row.get("planner_grouping") if isinstance(row.get("planner_grouping"), dict) else {}
        level_band = str(grouping.get("target_level_band") or "unknown")
        education_type = str(grouping.get("education_type") or "unknown")
        delivery_method = str(grouping.get("delivery_method") or "unknown")
        job_scope = str(grouping.get("job_scope") or "unknown")
        delivery_operation = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
        delivery_operation_code = str(delivery_operation.get("code") or "unknown")
        course_scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        course_scope_relation = str(course_scope_fit.get("relation") or "unknown")
        course_scope_alignment = str(course_scope_fit.get("alignment") or "unknown")
        education_type_payload = row.get("education_type") if isinstance(row.get("education_type"), dict) else {}
        education_type_source = str(education_type_payload.get("evidence_source") or "unknown")
        constraint_fit = (
            delivery_operation.get("constraint_fit")
            if isinstance(delivery_operation.get("constraint_fit"), dict)
            else {}
        )
        constraint_dimensions = (
            constraint_fit.get("dimensions")
            if isinstance(constraint_fit.get("dimensions"), dict)
            else {}
        )
        overall_constraint_status = str(constraint_fit.get("status") or "unknown")
        delivery_constraint_fit_counts["overall"][overall_constraint_status] = (
            delivery_constraint_fit_counts["overall"].get(overall_constraint_status, 0) + 1
        )
        for dimension in ("method", "time", "facility"):
            status = str(constraint_dimensions.get(dimension) or "unknown")
            delivery_constraint_fit_counts[dimension][status] = (
                delivery_constraint_fit_counts[dimension].get(status, 0) + 1
            )
        need_counts[need_code] = need_counts.get(need_code, 0) + 1
        directness_counts[directness_code] = directness_counts.get(directness_code, 0) + 1
        level_band_counts[level_band] = level_band_counts.get(level_band, 0) + 1
        education_type_counts[education_type] = education_type_counts.get(education_type, 0) + 1
        education_type_evidence_source_counts[education_type_source] = (
            education_type_evidence_source_counts.get(education_type_source, 0) + 1
        )
        delivery_method_counts[delivery_method] = delivery_method_counts.get(delivery_method, 0) + 1
        delivery_operation_counts[delivery_operation_code] = delivery_operation_counts.get(delivery_operation_code, 0) + 1
        course_scope_relation_counts[course_scope_relation] = (
            course_scope_relation_counts.get(course_scope_relation, 0) + 1
        )
        course_scope_alignment_counts[course_scope_alignment] = (
            course_scope_alignment_counts.get(course_scope_alignment, 0) + 1
        )
        job_scope_counts[job_scope] = job_scope_counts.get(job_scope, 0) + 1
        course_name = row.get("course_name")
        if need_code == "required" and course_name:
            required_course_names.append(str(course_name))
        flags = [str(flag) for flag in row.get("review_flags") or []]
        for flag in flags:
            review_flag_counts[flag] = review_flag_counts.get(flag, 0) + 1
        specificity_warning = row.get("specificity_warning") if isinstance(row.get("specificity_warning"), dict) else {}
        for code in specificity_warning.get("codes") or []:
            code = str(code)
            specificity_warning_counts[code] = specificity_warning_counts.get(code, 0) + 1
        duplicate_or_generic_warning = (
            row.get("duplicate_or_generic_warning")
            if isinstance(row.get("duplicate_or_generic_warning"), dict)
            else {}
        )
        for code in duplicate_or_generic_warning.get("codes") or []:
            code = str(code)
            duplicate_or_generic_warning_counts[code] = (
                duplicate_or_generic_warning_counts.get(code, 0) + 1
            )
        mapping_strength_warning = (
            row.get("mapping_strength_warning")
            if isinstance(row.get("mapping_strength_warning"), dict)
            else {}
        )
        for code in mapping_strength_warning.get("codes") or []:
            code = str(code)
            mapping_strength_warning_counts[code] = (
                mapping_strength_warning_counts.get(code, 0) + 1
            )
        quality_penalty = (
            row.get("quality_issue_penalty")
            if isinstance(row.get("quality_issue_penalty"), dict)
            else {}
        )
        if quality_penalty.get("applied"):
            if course_name:
                quality_issue_penalty_course_names.append(str(course_name))
            if quality_penalty.get("review_required"):
                quality_issue_penalty_review_required_count += 1
            for issue_type in quality_penalty.get("issue_types") or []:
                issue_type = str(issue_type)
                quality_issue_penalty_counts[issue_type] = (
                    quality_issue_penalty_counts.get(issue_type, 0) + 1
                )
        decision_state = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        decision_status = str(decision_state.get("status") or "unknown")
        decision_state_counts[decision_status] = decision_state_counts.get(decision_status, 0) + 1
        evidence_chain = row.get("evidence_chain") if isinstance(row.get("evidence_chain"), dict) else {}
        completeness = (
            evidence_chain.get("completeness")
            if isinstance(evidence_chain.get("completeness"), dict)
            else {}
        )
        chain_status = str(completeness.get("status") or "unknown")
        evidence_chain_status_counts[chain_status] = evidence_chain_status_counts.get(chain_status, 0) + 1
        if course_name and (
            flags
            or need_code == "adjacent_reference"
            or directness_code in {"weak", "compact_card", "unknown"}
        ):
            review_required_course_names.append(str(course_name))
    return {
        "rubric_source": "2026_hr_ncs_training_system_guide",
        "rubric_role": "framework_reference_not_scoring_source",
        "course_count": len(matrix),
        "need_classification_counts": need_counts,
        "evidence_directness_counts": directness_counts,
        "planner_group_counts": {
            "job_scope": job_scope_counts,
            "target_level_band": level_band_counts,
            "education_type": education_type_counts,
            "required_optional": need_counts,
            "delivery_method": delivery_method_counts,
            "course_scope_relation": course_scope_relation_counts,
        },
        "education_type_evidence_source_counts": education_type_evidence_source_counts,
        "delivery_operation_counts": delivery_operation_counts,
        "course_scope_relation_counts": course_scope_relation_counts,
        "course_scope_alignment_counts": course_scope_alignment_counts,
        "groupable_fields": [
            "job_scope",
            "target_level_band",
            "education_type",
            "required_optional",
            "delivery_method",
            "course_scope_relation",
        ],
        "review_flag_counts": review_flag_counts,
        "specificity_warning_counts": specificity_warning_counts,
        "duplicate_or_generic_warning_counts": duplicate_or_generic_warning_counts,
        "mapping_strength_warning_counts": mapping_strength_warning_counts,
        "quality_issue_penalty_counts": quality_issue_penalty_counts,
        "quality_issue_penalty_review_required_count": quality_issue_penalty_review_required_count,
        "quality_issue_penalty_course_names": list(dict.fromkeys(quality_issue_penalty_course_names))[:5],
        "decision_state_counts": decision_state_counts,
        "evidence_chain_status_counts": evidence_chain_status_counts,
        "delivery_constraint_fit_counts": delivery_constraint_fit_counts,
        "required_course_names": required_course_names[:5],
        "review_required_course_names": list(dict.fromkeys(review_required_course_names))[:5],
    }


def _annual_operation_window(need_code: str, rank: int) -> tuple[str, str, str]:
    if need_code == "required":
        return (
            "Q1",
            "core_gap_training",
            "Run after scope confirmation because this row is closest to the target task/KSA gap.",
        )
    if need_code == "supporting":
        return (
            "Q2",
            "supporting_training",
            "Run after core gap courses or in parallel when delivery constraints allow it.",
        )
    if need_code == "optional":
        return (
            "Q3",
            "optional_training",
            "Keep as a selectable course after required and supporting needs are confirmed.",
        )
    if need_code == "adjacent_reference":
        return (
            "Q4",
            "adjacent_reference_review",
            "Use as a review-only adjacent reference unless a human reviewer promotes it.",
        )
    if rank <= 1:
        return ("Q1", "scope_confirmation", "Confirm scope and evidence before scheduling.")
    return ("Q4", "review_backlog", "Hold until the row has enough evidence for scheduling.")


def _annual_operation_plan_seed(
    matrix: list[dict[str, Any]],
    *,
    target_population: str,
    requested_constraints: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    total_hours = 0.0
    hours_known_count = 0
    pending_decisions = 0
    review_required = 0
    for index, row in enumerate(matrix, start=1):
        need = row.get("need_classification") if isinstance(row.get("need_classification"), dict) else {}
        need_code = str(need.get("code") or "unknown")
        quarter, phase, rationale = _annual_operation_window(need_code, index)
        course_fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
        delivery_operation = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
        decision_state = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        human_review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
        evidence_chain = row.get("evidence_chain") if isinstance(row.get("evidence_chain"), dict) else {}
        completeness = (
            evidence_chain.get("completeness")
            if isinstance(evidence_chain.get("completeness"), dict)
            else {}
        )
        constraint_fit = (
            delivery_operation.get("constraint_fit")
            if isinstance(delivery_operation.get("constraint_fit"), dict)
            else {}
        )
        method_fit = (
            delivery_operation.get("method_constraint_fit")
            if isinstance(delivery_operation.get("method_constraint_fit"), dict)
            else {}
        )
        time_fit = (
            delivery_operation.get("time_constraint_fit")
            if isinstance(delivery_operation.get("time_constraint_fit"), dict)
            else {}
        )
        facility_fit = row.get("facility_constraint_fit") if isinstance(row.get("facility_constraint_fit"), dict) else {}
        if not facility_fit and isinstance(delivery_operation.get("facility_constraint_fit"), dict):
            facility_fit = delivery_operation.get("facility_constraint_fit") or {}
        hours = _parse_number(course_fit.get("hours"))
        if hours is not None:
            total_hours += hours
            hours_known_count += 1
        if decision_state.get("status") == "pending_human_decision":
            pending_decisions += 1
        if str(human_review.get("severity") or "") != "ready" or row.get("review_flags"):
            review_required += 1
        rows.append(
            {
                "sequence": index,
                "recommended_window": quarter,
                "phase": phase,
                "course_name": row.get("course_name"),
                "training_course_id": row.get("training_course_id"),
                "target_population": target_population,
                "need_classification": need_code,
                "system_suggestion": decision_state.get("system_suggestion") or need_code,
                "decision_status": decision_state.get("status") or "pending_human_decision",
                "human_review_severity": human_review.get("severity") or "ready",
                "evidence_chain_status": completeness.get("status") or "unknown",
                "hours": hours,
                "methods": course_fit.get("methods") if isinstance(course_fit.get("methods"), list) else [],
                "facilities": course_fit.get("facilities") if isinstance(course_fit.get("facilities"), list) else [],
                "constraint_status": constraint_fit.get("status") or "unknown",
                "method_status": method_fit.get("status") or "not_requested",
                "time_status": time_fit.get("status") or "not_requested",
                "facility_status": facility_fit.get("status") or "not_requested",
                "review_flags": [str(flag) for flag in row.get("review_flags") or []],
                "scheduling_rationale": rationale,
            }
        )
    status = "needs_human_review" if pending_decisions or review_required else "ready_for_operator_scheduling"
    return {
        "schema": "aihr_annual_operation_plan_seed_v1",
        "guide_stage": "C2-2",
        "status": status,
        "purpose": "Draft annual operation-plan seed generated from the training-system matrix; it is not an approved annual plan.",
        "target_population": target_population,
        "requested_constraints": requested_constraints,
        "summary": {
            "row_count": len(rows),
            "estimated_total_hours": round(total_hours, 2) if hours_known_count else None,
            "hours_known_count": hours_known_count,
            "pending_human_decision_rows": pending_decisions,
            "review_required_rows": review_required,
        },
        "rows": rows,
        "review_gate": {
            "status": "blocked_until_human_decision" if pending_decisions else "operator_review_required",
            "approval_claim": False,
            "message": "Use this as an operation-planning seed only. Required/optional decisions remain pending until a human confirms them.",
        },
        "export_fields": [
            "recommended_window",
            "phase",
            "target_population",
            "course_name",
            "need_classification",
            "hours",
            "methods",
            "facilities",
            "constraint_status",
            "decision_status",
            "human_review_severity",
        ],
    }


def _scope_baseline_entry(
    *,
    role: str,
    requested_query: Any,
    scope: dict[str, Any],
    unit_count: Any,
) -> dict[str, Any]:
    alias = scope.get("query_alias") if isinstance(scope.get("query_alias"), dict) else {}
    alternatives = scope.get("alternatives") if isinstance(scope.get("alternatives"), list) else []
    return {
        "role": role,
        "requested_query": requested_query,
        "resolved_scope": scope.get("resolved_as") or requested_query,
        "match_level": scope.get("match_level") or "unknown",
        "unit_count": scope.get("unit_count") if scope.get("unit_count") is not None else unit_count,
        "task_element": scope.get("task_element"),
        "query_alias": {
            "alias_text": alias.get("alias_text"),
            "normalized_query": alias.get("normalized_query"),
            "unit_code": alias.get("unit_code"),
            "confidence_score": alias.get("confidence_score"),
            "needs_human_review": bool(alias.get("needs_human_review")),
        }
        if alias
        else None,
        "alternative_count": len(alternatives),
        "scope_resolution_basis": [
            item
            for item in (
                scope.get("match_level"),
                "query_alias" if alias else None,
                "alternatives" if alternatives else None,
            )
            if item
        ],
    }


def _scope_baseline_contract(
    *,
    requested: dict[str, Any],
    current_scope: dict[str, Any],
    target_scope: dict[str, Any],
    transition_assessment: dict[str, Any],
    transition_summary: dict[str, Any],
) -> dict[str, Any]:
    current = _scope_baseline_entry(
        role="current",
        requested_query=requested.get("current_query"),
        scope=current_scope,
        unit_count=transition_summary.get("current_scope_unit_count"),
    )
    target = _scope_baseline_entry(
        role="target",
        requested_query=requested.get("target_query"),
        scope=target_scope,
        unit_count=transition_summary.get("target_scope_unit_count"),
    )
    review_flags: list[str] = []
    for entry in (current, target):
        alias = entry.get("query_alias") if isinstance(entry.get("query_alias"), dict) else {}
        if alias.get("needs_human_review"):
            review_flags.append(f"{entry['role']}_alias_candidate")
        if entry.get("match_level") in {"unknown", "fallback"}:
            review_flags.append(f"{entry['role']}_scope_low_confidence")
    target_role_overlay = transition_assessment.get("target_role_overlay")
    if target_role_overlay:
        review_flags.append("target_role_overlay_applied")
    review_flags = list(dict.fromkeys(review_flags))
    return {
        "schema": "aihr_scope_baseline_v1",
        "guide_stage": "C1-1",
        "purpose": "Record the job/NCS scope resolution used before task/KSA/course mapping.",
        "current": current,
        "target": target,
        "ncs_scope_relation": transition_assessment.get("ncs_scope_relation"),
        "current_scope_subset_of_target": bool(transition_assessment.get("current_scope_subset_of_target")),
        "exact_ksa_overlap_ratio": transition_assessment.get("exact_ksa_overlap_ratio"),
        "ontology_adjusted_transferability_ratio": transition_assessment.get(
            "ontology_adjusted_transferability_ratio"
        ),
        "adjusted_transferability_components": transition_assessment.get("adjusted_transferability_components")
        or {},
        "target_role_overlay": target_role_overlay,
        "human_review": {
            "status": "needs_review" if review_flags else "ready",
            "flags": review_flags,
            "prompt": "Confirm that the resolved NCS scopes match the intended current and target jobs before finalizing the education plan.",
        },
    }


def _course_intake_requirements(
    matrix: list[dict[str, Any]],
    *,
    current_label: Any,
    target_label: Any,
    target_population: str,
    requested_constraints: dict[str, Any],
) -> dict[str, Any]:
    course_names = [
        str(row.get("course_name"))
        for row in matrix
        if isinstance(row, dict) and _clean(row.get("course_name"))
    ]
    unique_course_names = list(dict.fromkeys(course_names))
    missing_hours = 0
    missing_methods = 0
    missing_facilities = 0
    for row in matrix:
        if not isinstance(row, dict):
            continue
        fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
        if fit.get("hours") in (None, ""):
            missing_hours += 1
        methods = fit.get("methods") if isinstance(fit.get("methods"), list) else []
        if not [item for item in methods if str(item).strip()]:
            missing_methods += 1
        facilities = fit.get("facilities") if isinstance(fit.get("facilities"), list) else []
        facility_fit = row.get("facility_constraint_fit") if isinstance(row.get("facility_constraint_fit"), dict) else {}
        facility_status = str(facility_fit.get("status") or "")
        if (
            not [item for item in facilities if str(item).strip()]
            and facility_status not in {"unknown", "not_requested"}
        ):
            missing_facilities += 1
    required_fields = [
        {
            "field": "course_name",
            "purpose": "Identify the course, but never use the name as sufficient mapping evidence.",
            "maps_to": ["training_course.course_name"],
        },
        {
            "field": "course_goal",
            "purpose": "Capture what performance, task, or KSA the course is intended to improve.",
            "maps_to": ["training_goal_concept_links", "course_link", "evidence_chain.training_course"],
        },
        {
            "field": "target_learners",
            "purpose": "Confirm whether the course fits the requested job, role, and level band.",
            "maps_to": ["target_population", "target_level_band", "job_scope"],
        },
        {
            "field": "content_outline",
            "purpose": "Provide visible evidence beyond title similarity.",
            "maps_to": ["task_ksa_basis", "performance_criteria", "ksa_evidence"],
        },
        {
            "field": "ncs_scope_or_unit",
            "purpose": "Anchor the course to an NCS classification, competency unit, element, or task.",
            "maps_to": ["course_scope_fit", "course_link.mapping_chain"],
        },
        {
            "field": "performance_criteria_or_task",
            "purpose": "Link course content to duty, task, or performance-criterion evidence.",
            "maps_to": ["evidence_chain.duty_task", "evidence_chain.performance_criterion"],
        },
        {
            "field": "ksa_evidence",
            "purpose": "State the knowledge, skill, and attitude evidence covered by the course.",
            "maps_to": ["task_ksa_basis", "evidence_chain.ksa"],
        },
        {
            "field": "level",
            "purpose": "Check level fit against the target NCS role and learner group.",
            "maps_to": ["course_fit.level", "target_level_band"],
        },
        {
            "field": "hours",
            "purpose": "Support feasibility, annual scheduling, and time-constraint fit.",
            "maps_to": ["course_fit.hours", "annual_operation_plan.rows.hours"],
        },
        {
            "field": "methods",
            "purpose": "Record delivery method such as classroom, practice, online, or blended.",
            "maps_to": ["course_fit.methods", "delivery_operation.method_constraint_fit"],
        },
        {
            "field": "facilities",
            "purpose": "Record facility or equipment requirements and facility-constraint fit.",
            "maps_to": ["course_fit.facilities", "facility_constraint_fit"],
        },
        {
            "field": "assessment_method",
            "purpose": "Preserve how learning completion or performance contribution will be checked.",
            "maps_to": ["human_review", "annual_operation_plan.review_gate"],
        },
    ]
    optional_fields = [
        "provider",
        "source_url_or_document",
        "education_type",
        "legal_or_mandatory_basis",
        "operation_constraints",
        "cost_or_budget_note",
        "reviewer_notes",
    ]
    return {
        "schema": "aihr_course_intake_requirements_v1",
        "guide_stage": "C1-1",
        "status": "needs_collection_or_review",
        "purpose": (
            "Minimum course-investigation fields required before mapping internal or external "
            "courses to job, task, performance-criterion, KSA, and delivery evidence."
        ),
        "current_scope": str(current_label or ""),
        "target_scope": str(target_label or ""),
        "target_population": target_population,
        "requested_constraints": requested_constraints,
        "required_fields": required_fields,
        "optional_fields": optional_fields,
        "mapping_policy": {
            "title_only_mapping_allowed": False,
            "n_to_n_job_course_mapping_allowed": True,
            "generic_course_requires_warning": True,
            "framework_reference_is_not_scoring_source": True,
            "human_review_required_before_approval": True,
        },
        "prefill_from_recommendations": {
            "matrix_rows": len(matrix),
            "course_count": len(unique_course_names),
            "course_names": unique_course_names[:10],
            "missing_hours_rows": missing_hours,
            "missing_methods_rows": missing_methods,
            "missing_facilities_rows": missing_facilities,
        },
        "review_gate": {
            "status": "intake_template_only",
            "approval_claim": False,
            "message": (
                "This object defines course-intake evidence requirements only; it does not approve "
                "or score any course without NCS task/KSA links and human review where needed."
            ),
        },
    }


def _training_course_inventory_template(
    matrix: list[dict[str, Any]],
    *,
    target_population: str,
    requested_constraints: dict[str, Any],
) -> dict[str, Any]:
    columns = [
        {
            "column": "source_type",
            "required": True,
            "purpose": "Distinguish internal, external, NCS API, or supplemental course sources.",
            "maps_to": ["course_inventory.source_type", "source_policy"],
            "validation": "internal|external|ncs_training_api|supplemental|unknown",
        },
        {
            "column": "course_name",
            "required": True,
            "purpose": "Identify the course without treating the title as sufficient evidence.",
            "maps_to": ["training_course.course_name"],
            "validation": "non_empty_text",
        },
        {
            "column": "provider",
            "required": False,
            "purpose": "Record the institution, platform, department, or vendor that provides the course.",
            "maps_to": ["course_inventory.provider"],
            "validation": "text_or_unknown",
        },
        {
            "column": "source_url_or_document",
            "required": False,
            "purpose": "Preserve where the course facts came from for later audit.",
            "maps_to": ["course_inventory.source_reference"],
            "validation": "url_path_or_document_note",
        },
        {
            "column": "course_goal",
            "required": True,
            "purpose": "Capture source course-objective evidence used for task/KSA mapping.",
            "maps_to": ["training_goal_concept_links", "evidence_chain.training_course"],
            "validation": "non_empty_text",
        },
        {
            "column": "target_learners",
            "required": True,
            "purpose": "Check learner group, job scope, and target level fit.",
            "maps_to": ["target_population", "target_level_band"],
            "validation": "text_or_role_group",
        },
        {
            "column": "content_outline",
            "required": True,
            "purpose": "Expose evidence beyond title similarity and support duplicate review.",
            "maps_to": ["task_ksa_basis", "duplicate_or_generic_warning"],
            "validation": "non_empty_text",
        },
        {
            "column": "ncs_scope_or_unit",
            "required": True,
            "purpose": "Anchor the course to NCS classification, unit, element, or task where possible.",
            "maps_to": ["course_scope_fit", "course_link.mapping_chain"],
            "validation": "ncs_code_or_scope_text_or_review_needed",
        },
        {
            "column": "performance_criteria_or_task",
            "required": True,
            "purpose": "Connect course content to duty, task, and performance-criterion evidence.",
            "maps_to": ["evidence_chain.duty_task", "evidence_chain.performance_criterion"],
            "validation": "text_or_review_needed",
        },
        {
            "column": "ksa_evidence",
            "required": True,
            "purpose": "List knowledge, skill, and attitude evidence covered by the course.",
            "maps_to": ["task_ksa_basis", "evidence_chain.ksa"],
            "validation": "list_or_text",
        },
        {
            "column": "level",
            "required": True,
            "purpose": "Support level fit and target level band grouping.",
            "maps_to": ["course_fit.level", "target_level_band"],
            "validation": "number_or_level_band_or_review_needed",
        },
        {
            "column": "hours",
            "required": True,
            "purpose": "Support feasibility, annual operation planning, and time constraints.",
            "maps_to": ["course_fit.hours", "annual_operation_plan.rows.hours"],
            "validation": "positive_number_or_review_needed",
        },
        {
            "column": "methods",
            "required": True,
            "purpose": "Record delivery method such as classroom, practice, remote, or blended.",
            "maps_to": ["course_fit.methods", "delivery_operation.method_constraint_fit"],
            "validation": "list_or_text",
        },
        {
            "column": "facilities",
            "required": True,
            "purpose": "Record facility and equipment needs for delivery constraint fit.",
            "maps_to": ["course_fit.facilities", "facility_constraint_fit"],
            "validation": "list_or_text_or_not_requested",
        },
        {
            "column": "education_type",
            "required": True,
            "purpose": "Group courses into basic, job, leadership, statutory, reskill, or support types.",
            "maps_to": ["education_type", "planner_grouping.education_type"],
            "validation": "controlled_text_or_review_needed",
        },
        {
            "column": "required_optional_basis",
            "required": True,
            "purpose": "Keep the reason for required, optional, supporting, or adjacent classification visible.",
            "maps_to": ["required_optional_basis", "decision_state"],
            "validation": "controlled_text_with_rationale",
        },
        {
            "column": "assessment_method",
            "required": True,
            "purpose": "Record completion, performance, or learning assessment evidence.",
            "maps_to": ["human_review", "annual_operation_plan.review_gate"],
            "validation": "text_or_not_collected",
        },
        {
            "column": "duplicate_or_generic_risk",
            "required": True,
            "purpose": "Surface courses that are broad, duplicate, or weakly mapped.",
            "maps_to": ["duplicate_or_generic_warning", "specificity_warning", "mapping_strength_warning"],
            "validation": "clear|warning|needs_review",
        },
        {
            "column": "review_state",
            "required": True,
            "purpose": "Preserve review status without claiming approval.",
            "maps_to": ["human_review", "decision_state"],
            "validation": "pending_human_decision|needs_review|ready",
        },
    ]
    required_columns = [item["column"] for item in columns if item.get("required")]
    prefill_rows: list[dict[str, Any]] = []
    for row in matrix[:10]:
        if not isinstance(row, dict):
            continue
        fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
        need = row.get("need_classification") if isinstance(row.get("need_classification"), dict) else {}
        education_type = row.get("education_type") if isinstance(row.get("education_type"), dict) else {}
        basis = row.get("task_ksa_basis") if isinstance(row.get("task_ksa_basis"), dict) else {}
        scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        direct_units = scope_fit.get("direct_unit_codes") if isinstance(scope_fit.get("direct_unit_codes"), list) else []
        evidence_chain = row.get("evidence_chain") if isinstance(row.get("evidence_chain"), dict) else {}
        completeness = evidence_chain.get("completeness") if isinstance(evidence_chain.get("completeness"), dict) else {}
        duplicate = row.get("duplicate_or_generic_warning") if isinstance(row.get("duplicate_or_generic_warning"), dict) else {}
        specificity = row.get("specificity_warning") if isinstance(row.get("specificity_warning"), dict) else {}
        mapping_warning = row.get("mapping_strength_warning") if isinstance(row.get("mapping_strength_warning"), dict) else {}
        decision = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        human_review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
        prefill_rows.append(
            {
                "source_type": "ncs_training_api",
                "course_name": row.get("course_name"),
                "training_course_id": row.get("training_course_id"),
                "provider": "unknown",
                "source_url_or_document": "ncs_training_courses",
                "course_goal": _clean(row.get("course_goal")) or "review_needed",
                "target_learners": target_population,
                "content_outline": "; ".join(_normalize_text_list(basis.get("covered_elements") or []))
                or "review_needed",
                "ncs_scope_or_unit": direct_units or scope_fit.get("relation") or "review_needed",
                "performance_criteria_or_task": basis.get("covered_elements") or "review_needed",
                "ksa_evidence": (
                    basis.get("gap_ksa")
                    or basis.get("training_goal_ksa")
                    or basis.get("target_scope_ksa")
                    or []
                ),
                "level": fit.get("level"),
                "hours": fit.get("hours"),
                "methods": fit.get("methods") if isinstance(fit.get("methods"), list) else [],
                "facilities": fit.get("facilities") if isinstance(fit.get("facilities"), list) else [],
                "education_type": education_type.get("code") or "unknown",
                "required_optional_basis": need.get("code") or "unknown",
                "assessment_method": "not_collected",
                "duplicate_or_generic_risk": duplicate.get("status") or specificity.get("status") or mapping_warning.get("status") or "unknown",
                "review_state": decision.get("status") or human_review.get("severity") or "pending_human_decision",
                "evidence_chain_status": completeness.get("status") or "unknown",
            }
        )
    return {
        "schema": "aihr_training_course_inventory_template_v1",
        "guide_stage": "C1-1",
        "status": "template_with_prefill",
        "purpose": (
            "Course inventory table contract for organizing investigated internal and external "
            "training courses before C1-2 necessity review and C2-1 matrix grouping."
        ),
        "target_population": target_population,
        "requested_constraints": requested_constraints,
        "columns": columns,
        "required_columns": required_columns,
        "row_template": {column: "" for column in required_columns},
        "prefill_rows": prefill_rows,
        "validation_rules": [
            "Do not classify a course as required from course_name alone.",
            "Rows without performance_criteria_or_task or ksa_evidence must remain review gated.",
            "Broad or duplicate courses must carry duplicate_or_generic_risk before ranking or grouping.",
            "Internal and external rows can share one course, so N:N job-course mapping is allowed.",
        ],
        "review_gate": {
            "status": "inventory_template_only",
            "approval_claim": False,
            "message": (
                "Inventory rows are collection scaffolds. They are not approved course mappings "
                "until task/KSA evidence and human decisions are supplied where required."
            ),
        },
    }


def _count_sequence(value: Any) -> int:
    if isinstance(value, list):
        return len(value)
    if value in (None, ""):
        return 0
    return 1


def _status_counts(rows: list[dict[str, Any]], field_path: tuple[str, ...]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value: Any = row
        for field in field_path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(field)
        key = str(value or "unknown")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _training_necessity_review(
    matrix: list[dict[str, Any]],
    *,
    target_population: str,
    requested_constraints: dict[str, Any],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    review_required_rows = 0
    approval_blocked_rows = 0
    for index, row in enumerate(matrix, start=1):
        if not isinstance(row, dict):
            continue
        basis = row.get("task_ksa_basis") if isinstance(row.get("task_ksa_basis"), dict) else {}
        scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        directness = row.get("evidence_directness") if isinstance(row.get("evidence_directness"), dict) else {}
        fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
        level_band = row.get("target_level_band") if isinstance(row.get("target_level_band"), dict) else {}
        delivery_operation = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
        constraint_fit = (
            delivery_operation.get("constraint_fit")
            if isinstance(delivery_operation.get("constraint_fit"), dict)
            else {}
        )
        duplicate = (
            row.get("duplicate_or_generic_warning")
            if isinstance(row.get("duplicate_or_generic_warning"), dict)
            else {}
        )
        specificity = row.get("specificity_warning") if isinstance(row.get("specificity_warning"), dict) else {}
        mapping_warning = (
            row.get("mapping_strength_warning")
            if isinstance(row.get("mapping_strength_warning"), dict)
            else {}
        )
        decision = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        human_review = row.get("human_review") if isinstance(row.get("human_review"), dict) else {}
        need = row.get("required_optional_basis") if isinstance(row.get("required_optional_basis"), dict) else {}
        if not need:
            need = row.get("need_classification") if isinstance(row.get("need_classification"), dict) else {}
        evidence_chain = row.get("evidence_chain") if isinstance(row.get("evidence_chain"), dict) else {}
        completeness = (
            evidence_chain.get("completeness")
            if isinstance(evidence_chain.get("completeness"), dict)
            else {}
        )
        basis_counts = {
            "target_scope_ksa": _count_sequence(basis.get("target_scope_ksa")),
            "gap_ksa": _count_sequence(basis.get("gap_ksa")),
            "training_goal_ksa": _count_sequence(basis.get("training_goal_ksa")),
            "covered_elements": _count_sequence(basis.get("covered_elements")),
        }
        task_or_ksa_visible = any(basis_counts.values())
        weak_scope_relations = {
            "course_scope_unknown",
            "target_scope_unknown",
            "different_classification",
            "unknown",
            "",
        }
        scope_relation = str(scope_fit.get("relation") or "unknown")
        directness_code = str(directness.get("code") or "unknown")
        weak_directness = directness_code in {"weak", "compact_card", "unknown", ""}
        job_linkage_status = (
            "evidence_visible"
            if task_or_ksa_visible and scope_relation not in weak_scope_relations and not weak_directness
            else "needs_review"
        )
        level_visible = fit.get("level") not in (None, "") and bool(level_band.get("code"))
        level_fit_status = "evidence_visible" if level_visible else "needs_review"
        constraint_status = str(constraint_fit.get("status") or "unknown")
        delivery_status = (
            "fit"
            if constraint_status in {"fit", "not_requested"}
            else "needs_review"
        )
        risk_codes = list(dict.fromkeys(
            [
                *(duplicate.get("codes") or []),
                *(specificity.get("codes") or []),
                *(mapping_warning.get("codes") or []),
            ]
        ))
        risk_status = (
            "warning"
            if risk_codes
            else str(duplicate.get("status") or specificity.get("status") or mapping_warning.get("status") or "clear")
        )
        performance_status = "evidence_visible" if task_or_ksa_visible else "needs_review"
        review_flags = [str(item) for item in row.get("review_flags") or [] if str(item).strip()]
        decision_status = str(decision.get("status") or "pending_human_decision")
        needs_review = (
            bool(review_flags)
            or decision_status != "ready"
            or job_linkage_status != "evidence_visible"
            or level_fit_status != "evidence_visible"
            or delivery_status == "needs_review"
            or risk_status != "clear"
            or performance_status != "evidence_visible"
        )
        if needs_review:
            review_required_rows += 1
        if decision.get("approval_claim") is not False:
            approval_blocked_rows += 1
        elif decision_status == "pending_human_decision":
            approval_blocked_rows += 1
        rows.append(
            {
                "sequence": index,
                "course_name": row.get("course_name"),
                "training_course_id": row.get("training_course_id"),
                "job_linkage": {
                    "status": job_linkage_status,
                    "course_scope_relation": scope_relation,
                    "course_scope_alignment": scope_fit.get("alignment") or "unknown",
                    "evidence_directness": directness_code,
                    "task_ksa_basis_counts": basis_counts,
                    "review_reason": (
                        "Task/KSA and scope evidence are visible."
                        if job_linkage_status == "evidence_visible"
                        else "Confirm job, task, performance-criterion, and KSA linkage before finalizing."
                    ),
                },
                "level_fit": {
                    "status": level_fit_status,
                    "target_level_band": level_band,
                    "course_level": fit.get("level"),
                    "review_reason": (
                        "Course level and target level band are visible."
                        if level_fit_status == "evidence_visible"
                        else "Confirm course level and target learner level."
                    ),
                },
                "required_optional_review": {
                    "code": need.get("code") or "unknown",
                    "label": need.get("label") or "unknown",
                    "rationale": need.get("rationale") or "",
                    "statutory_or_mandatory_basis": "not_supplied",
                    "approval_claim": False,
                },
                "duplicate_or_generic_review": {
                    "status": risk_status,
                    "codes": risk_codes,
                    "duplicate_or_generic_warning": duplicate,
                    "specificity_warning": specificity,
                    "mapping_strength_warning": mapping_warning,
                },
                "delivery_feasibility": {
                    "status": delivery_status,
                    "constraint_status": constraint_status,
                    "hours": fit.get("hours"),
                    "methods": fit.get("methods") if isinstance(fit.get("methods"), list) else [],
                    "facilities": fit.get("facilities") if isinstance(fit.get("facilities"), list) else [],
                    "requested_constraints": requested_constraints,
                    "constraint_fit": constraint_fit,
                },
                "performance_contribution": {
                    "status": performance_status,
                    "evidence_chain_status": completeness.get("status") or "unknown",
                    "gap_ksa": _normalize_text_list(basis.get("gap_ksa") or [])[:5],
                    "training_goal_ksa": _normalize_text_list(basis.get("training_goal_ksa") or [])[:5],
                    "covered_elements": _normalize_text_list(basis.get("covered_elements") or [])[:5],
                    "review_reason": (
                        "KSA or covered element evidence is visible."
                        if performance_status == "evidence_visible"
                        else "Confirm contribution to task performance or business outcome."
                    ),
                },
                "decision_state": decision,
                "human_review": human_review,
                "review_flags": review_flags,
                "recommended_review_action": (
                    "human_confirm_before_use" if needs_review else "human_spotcheck_before_use"
                ),
            }
        )
    summary = {
        "row_count": len(rows),
        "review_required_rows": review_required_rows,
        "approval_blocked_rows": approval_blocked_rows,
        "job_linkage_status_counts": _status_counts(rows, ("job_linkage", "status")),
        "level_fit_status_counts": _status_counts(rows, ("level_fit", "status")),
        "delivery_feasibility_status_counts": _status_counts(rows, ("delivery_feasibility", "status")),
        "duplicate_or_generic_status_counts": _status_counts(rows, ("duplicate_or_generic_review", "status")),
        "performance_contribution_status_counts": _status_counts(rows, ("performance_contribution", "status")),
        "required_optional_counts": _status_counts(rows, ("required_optional_review", "code")),
        "decision_state_counts": _status_counts(rows, ("decision_state", "status")),
    }
    return {
        "schema": "aihr_training_necessity_review_v1",
        "guide_stage": "C1-2",
        "status": "review_evidence_prepared",
        "purpose": (
            "C1-2 necessity-review contract for checking job relevance, task/KSA evidence, "
            "level fit, required/optional basis, duplication risk, delivery feasibility, and "
            "performance contribution before confirming the education-course list."
        ),
        "target_population": target_population,
        "requested_constraints": requested_constraints,
        "review_dimensions": [
            "job_linkage",
            "level_fit",
            "required_optional_review",
            "duplicate_or_generic_review",
            "delivery_feasibility",
            "performance_contribution",
            "human_review",
        ],
        "summary": summary,
        "rows": rows,
        "validation_rules": [
            "Do not confirm a course from course_name alone.",
            "Rows with weak task/KSA, level, delivery, duplicate, or mapping evidence remain review gated.",
            "Automation prepares necessity-review evidence only and does not approve the course list.",
            "Statutory or mandatory status requires supplied organization or legal-policy evidence.",
        ],
        "review_gate": {
            "status": "pending_human_confirmation",
            "approval_claim": False,
            "message": (
                "This object is C1-2 review evidence. It must not be treated as a final approved "
                "course list until a human reviewer confirms the rows."
            ),
        },
    }


def _training_system_guide_trace(
    matrix: list[dict[str, Any]],
    *,
    current_label: Any,
    target_label: Any,
    priority_gaps: list[Any],
    summary: dict[str, Any],
) -> dict[str, Any]:
    guide_index = load_hrd_guide_reference_index()
    guide_reference = hrd_guide_reference_metadata(guide_index)
    guide_check_codes = hrd_guide_trace_check_codes(guide_index) or list(
        AIHR_TRAINING_SYSTEM_GUIDE_TRACE_REQUIRED_CHECKS
    )
    guide_stage_codes = hrd_guide_workflow_stage_codes(guide_index) or list(
        AIHR_TRAINING_SYSTEM_GUIDE_WORKFLOW_REQUIRED_STAGES
    )
    guide_workflow = guide_index.get("guide_workflow") if isinstance(guide_index.get("guide_workflow"), list) else []
    guide_stage_labels = {
        str(item.get("code")): str(item.get("korean_name") or item.get("name") or item.get("code"))
        for item in guide_workflow
        if isinstance(item, dict) and item.get("code")
    }
    review_flag_counts = summary.get("review_flag_counts") if isinstance(summary.get("review_flag_counts"), dict) else {}
    review_courses = summary.get("review_required_course_names") if isinstance(summary.get("review_required_course_names"), list) else []
    groupable_fields = summary.get("groupable_fields") if isinstance(summary.get("groupable_fields"), list) else []
    delivery_constraint_fit_counts = (
        summary.get("delivery_constraint_fit_counts")
        if isinstance(summary.get("delivery_constraint_fit_counts"), dict)
        else {}
    )
    course_scope_relation_counts = (
        summary.get("course_scope_relation_counts")
        if isinstance(summary.get("course_scope_relation_counts"), dict)
        else {}
    )
    course_scope_review_count = sum(
        int(course_scope_relation_counts.get(relation) or 0)
        for relation in (
            "same_middle_classification",
            "same_major_classification",
            "different_classification",
            "course_scope_unknown",
            "target_scope_unknown",
            "unknown",
        )
    )
    rows_with_task_ksa = 0
    rows_with_course_scope_fit = 0
    rows_with_required_optional = 0
    rows_with_decision_state = 0
    rows_with_delivery = 0
    delivery_constraint_review_rows = 0
    missing_delivery: list[str] = []
    for index, row in enumerate(matrix, start=1):
        if not isinstance(row, dict):
            continue
        basis = row.get("task_ksa_basis") if isinstance(row.get("task_ksa_basis"), dict) else {}
        basis_types = basis.get("basis_types") if isinstance(basis.get("basis_types"), list) else []
        if basis_types or basis.get("gap_ksa") or basis.get("training_goal_ksa") or basis.get("target_scope_ksa"):
            rows_with_task_ksa += 1
        course_scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        if course_scope_fit.get("relation") and course_scope_fit.get("relation") not in {
            "course_scope_unknown",
            "target_scope_unknown",
            "unknown",
        }:
            rows_with_course_scope_fit += 1
        need = row.get("need_classification") if isinstance(row.get("need_classification"), dict) else {}
        required_optional = row.get("required_optional_basis") if isinstance(row.get("required_optional_basis"), dict) else {}
        if need.get("code") and required_optional.get("code"):
            rows_with_required_optional += 1
        decision_state = row.get("decision_state") if isinstance(row.get("decision_state"), dict) else {}
        if (
            decision_state.get("schema") == "aihr_training_row_decision_state_v1"
            and decision_state.get("status") == "pending_human_decision"
        ):
            rows_with_decision_state += 1
        fit = row.get("course_fit") if isinstance(row.get("course_fit"), dict) else {}
        delivery_operation = row.get("delivery_operation") if isinstance(row.get("delivery_operation"), dict) else {}
        missing: list[str] = []
        facility_fit = row.get("facility_constraint_fit") if isinstance(row.get("facility_constraint_fit"), dict) else {}
        if not facility_fit and isinstance(delivery_operation.get("facility_constraint_fit"), dict):
            facility_fit = delivery_operation.get("facility_constraint_fit") or {}
        facility_status = str(facility_fit.get("status") or "")
        allows_sparse_delivery = facility_status in {"unknown", "not_requested"}
        for field in ("level", "hours", "methods", "facilities"):
            if field not in fit:
                missing.append(field)
                continue
            value = fit.get(field)
            if field in {"methods", "facilities"}:
                if not isinstance(value, list):
                    missing.append(field)
                elif not [item for item in value if str(item).strip()] and not allows_sparse_delivery:
                    missing.append(field)
            elif value is None or value == "":
                missing.append(field)
        sparse_review_only = bool(missing) and set(missing).issubset({"methods", "facilities"}) and allows_sparse_delivery
        if (not missing or sparse_review_only) and delivery_operation.get("code"):
            rows_with_delivery += 1
        constraint_fit = (
            delivery_operation.get("constraint_fit")
            if isinstance(delivery_operation.get("constraint_fit"), dict)
            else {}
        )
        if str(constraint_fit.get("status") or "") not in {"", "fit", "not_requested"}:
            delivery_constraint_review_rows += 1
        if missing:
            label = str(row.get("course_name") or row.get("rank") or index)
            missing_delivery.append(f"{label}: {', '.join(missing)}")

    def check(code: str, label: str, ok: bool, evidence: str, *, needs_review: bool = False) -> dict[str, Any]:
        if ok and not needs_review:
            status = "ready"
        elif ok and needs_review:
            status = "needs_review"
        else:
            status = "needs_review"
        return {
            "check": code,
            "code": code,
            "label": label,
            "status": status,
            "evidence": evidence,
        }

    checks = [
        check(
            "job_scope",
            "job/NCS scope",
            bool(current_label and target_label),
            (
                f"{current_label} -> {target_label}; "
                f"course_scope_fit_rows={rows_with_course_scope_fit}/{len(matrix)}; "
                f"scope_review_rows={course_scope_review_count}"
            ),
            needs_review=course_scope_review_count > 0 or rows_with_course_scope_fit < len(matrix),
        ),
        check(
            "task_ksa",
            "task and KSA evidence",
            rows_with_task_ksa > 0 or bool(priority_gaps),
            f"matrix_rows_with_task_ksa={rows_with_task_ksa}, priority_gaps={len(priority_gaps)}",
            needs_review=rows_with_task_ksa < len(matrix),
        ),
        check(
            "course_link",
            "training course linkage",
            bool(matrix),
            f"training_system_matrix_rows={len(matrix)}",
        ),
        check(
            "required_optional",
            "required/optional classification",
            bool(matrix) and rows_with_required_optional == len(matrix),
            (
                f"classified_rows={rows_with_required_optional}/{len(matrix)}; "
                f"decision_state_rows={rows_with_decision_state}/{len(matrix)}"
            ),
            needs_review=rows_with_decision_state > 0,
        ),
        check(
            "level_delivery",
            "level, hours, method, facility",
            bool(matrix) and rows_with_delivery == len(matrix),
            f"delivery_rows={rows_with_delivery}/{len(matrix)}",
            needs_review=bool(missing_delivery or delivery_constraint_review_rows),
        ),
        check(
            "human_review",
            "human review gate",
            True,
            (
                f"review_courses={len(review_courses)}, review_flag_types={len(review_flag_counts)}, "
                f"pending_decision_rows={rows_with_decision_state}/{len(matrix)}"
            ),
            needs_review=bool(review_courses or review_flag_counts or rows_with_decision_state > 0),
        ),
    ]
    status_counts: dict[str, int] = {}
    for item in checks:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    required_groupable_fields = {
        "job_scope",
        "target_level_band",
        "education_type",
        "required_optional",
        "delivery_method",
    }
    present_groupable_fields = {str(field) for field in groupable_fields}

    def workflow_stage(
        code: str,
        title: str,
        status: str,
        evidence: str,
        *,
        related_checks: list[str],
        output_fields: list[str],
    ) -> dict[str, Any]:
        return {
            "code": code,
            "title": guide_stage_labels.get(code, title),
            "status": status,
            "evidence": evidence,
            "related_checks": related_checks,
            "output_fields": output_fields,
        }

    guide_workflow_stages = [
        workflow_stage(
            "C1-1",
            "교육과정 조사 및 직무/과업/KSA 매핑",
            "ready" if matrix and rows_with_task_ksa == len(matrix) else "needs_review",
            (
                f"current_target={current_label} -> {target_label}; "
                f"mapped_rows={rows_with_task_ksa}/{len(matrix)}; "
                f"course_scope_fit_rows={rows_with_course_scope_fit}/{len(matrix)}"
            ),
            related_checks=["job_scope", "task_ksa", "course_link"],
            output_fields=[
                "course_intake_requirements",
                "training_course_inventory_template",
                "training_system_matrix",
                "evidence_chain",
                "task_ksa_basis",
                "course_scope_fit",
                "course_fit",
            ],
        ),
        workflow_stage(
            "C1-2",
            "교육훈련 필요성 검토 및 교육과정 리스트 확정",
            (
                "needs_review"
                if review_courses or review_flag_counts or rows_with_decision_state > 0
                else ("ready" if matrix and rows_with_required_optional == len(matrix) else "needs_review")
            ),
            (
                f"classified_rows={rows_with_required_optional}/{len(matrix)}; "
                f"decision_state_rows={rows_with_decision_state}/{len(matrix)}; "
                f"review_courses={len(review_courses)}; review_flag_types={len(review_flag_counts)}"
            ),
            related_checks=["required_optional", "human_review"],
            output_fields=[
                "training_necessity_review",
                "need_classification",
                "required_optional_basis",
                "decision_state",
                "human_review",
            ],
        ),
        workflow_stage(
            "C2-1",
            "교육유형/수준 정의 및 교육훈련체계도 설계",
            (
                "ready"
                if matrix and required_groupable_fields.issubset(present_groupable_fields)
                else "needs_review"
            ),
            (
                "groupable_fields="
                + ",".join(field for field in groupable_fields)
                + f"; matrix_rows={len(matrix)}"
            ),
            related_checks=["job_scope", "required_optional", "level_delivery"],
            output_fields=["training_system_summary", "training_system_matrix", "planner_grouping"],
        ),
        workflow_stage(
            "C2-2",
            "연간 교육운영계획 및 관리체계 수립",
            (
                "needs_review"
                if missing_delivery or delivery_constraint_review_rows
                else ("ready" if matrix and rows_with_delivery == len(matrix) else "needs_review")
            ),
            (
                f"delivery_rows={rows_with_delivery}/{len(matrix)}; "
                f"missing_delivery={len(missing_delivery)}; "
                f"delivery_constraint_review_rows={delivery_constraint_review_rows}; "
                f"delivery_constraint_fit_counts={delivery_constraint_fit_counts}"
            ),
            related_checks=["level_delivery", "human_review"],
            output_fields=[
                "annual_operation_plan",
                "course_fit.hours",
                "course_fit.methods",
                "course_fit.facilities",
                "delivery_operation",
                "delivery_operation.method_constraint_fit",
                "delivery_operation.time_constraint_fit",
                "delivery_operation.constraint_fit",
                "facility_constraint_fit",
                "human_review",
            ],
        ),
    ]
    for item in guide_workflow_stages:
        status = str(item.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    return {
        "schema": AIHR_TRAINING_SYSTEM_GUIDE_TRACE_SCHEMA,
        "rubric_source": "2026_hr_ncs_training_system_guide",
        "rubric_role": "framework_reference_not_scoring_source",
        "non_source_data_policy": "guide_used_as_rubric_only",
        "guide_reference_schema": guide_reference.get("schema"),
        "rubric_source_path": guide_reference.get("project_copy_path"),
        "rubric_source_hash": guide_reference.get("source_hash_sha256"),
        "reference_page_count": guide_reference.get("page_count"),
        "flow": [
            "job_scope",
            "task_ksa",
            "training_course",
            "required_optional",
            "level_delivery_operation",
            "human_review",
        ],
        "guide_workflow_stage_codes": guide_stage_codes,
        "guide_workflow_stages": guide_workflow_stages,
        "guide_workflow": {
            "schema": "aihr_guide_workflow_v1",
            "steps": guide_workflow_stages,
            "missing_codes": [
                code
                for code in guide_stage_codes
                if code not in {str(item.get("code")) for item in guide_workflow_stages}
            ],
        },
        "required_check_codes": guide_check_codes,
        "checks": checks,
        "status_counts": status_counts,
        "matrix_reconstruction_fields": groupable_fields
        or [
            "job_scope",
            "target_level_band",
            "education_type",
            "required_optional",
            "delivery_method",
        ],
        "evidence_contract": {
            "matrix_rows": len(matrix),
            "rows_with_task_ksa": rows_with_task_ksa,
            "rows_with_required_optional": rows_with_required_optional,
            "rows_with_delivery": rows_with_delivery,
            "missing_delivery": missing_delivery[:10],
            "delivery_constraint_fit_counts": delivery_constraint_fit_counts,
            "delivery_constraint_review_rows": delivery_constraint_review_rows,
            "evidence_chain_status_counts": summary.get("evidence_chain_status_counts") or {},
        },
    }


EDUCATION_PLAN_SCENARIOS: dict[str, dict[str, Any]] = {
    "task_transition": {
        "title": "과업/직무 전환 교육체계",
        "aliases": ["task_transition", "transition", "career_transition", "직무전환", "과업이동", "전환"],
        "purpose": "현재 NCS 범위에서 목표 NCS 범위로 이동할 때 필요한 보완 KSA와 우선 훈련을 정리한다.",
    },
    "new_role_onboarding": {
        "title": "신규 직무 온보딩 교육체계",
        "aliases": ["new_role_onboarding", "onboarding", "new hire", "신규", "온보딩", "신입"],
        "purpose": "목표 직무 수행에 필요한 기초 범위 확인, 핵심 KSA 보완, 현장 적용 순서로 교육을 구성한다.",
    },
    "upskilling": {
        "title": "현 직무 고도화 교육체계",
        "aliases": ["upskilling", "skill up", "고도화", "업스킬링", "숙련향상"],
        "purpose": "현재 직무와 목표 직무의 공통 KSA를 활용하면서 부족 KSA를 집중 보완한다.",
    },
    "reskilling": {
        "title": "리스킬링 교육체계",
        "aliases": ["reskilling", "reskill", "재교육", "리스킬링", "전직"],
        "purpose": "공통 KSA가 낮은 전환에서 목표 직무 기초와 핵심 과업을 단계적으로 보완한다.",
    },
    "qualification_bridge": {
        "title": "자격 연계 보조 교육체계",
        "aliases": ["qualification", "certificate", "자격", "자격연계"],
        "purpose": "관련 자격 종목은 공식 인정 판단이 아니라 교육 우선순위 보조 근거로만 활용한다.",
    },
    "job_base_gap": {
        "title": "직업기초능력 보완 교육체계",
        "aliases": ["job_base", "basic competency", "직업기초능력", "기초역량"],
        "purpose": "전환 과정에서 반복적으로 요구되는 직업기초능력 공통점과 부족점을 확인한다.",
    },
    "delivery_fit": {
        "title": "훈련 운영 제약 맞춤 교육체계",
        "aliases": ["delivery", "hours", "method", "시간", "훈련방법", "운영"],
        "purpose": "훈련시간, 수준, 시설, 방법을 운영 제약과 맞춰 과정 배치를 조정한다.",
    },
}


def _available_education_plan_scenarios() -> list[dict[str, Any]]:
    return [
        {
            "scenario": key,
            "title": value["title"],
            "purpose": value["purpose"],
        }
        for key, value in EDUCATION_PLAN_SCENARIOS.items()
    ]


def _normalize_education_plan_scenario(scenario: str | None, *, transferability_ratio: float) -> tuple[str, str]:
    requested = _clean(scenario) or "auto"
    normalized = requested.lower().replace("-", "_").replace(" ", "_")
    if normalized in {"auto", "자동"}:
        if transferability_ratio < 0.1:
            return "reskilling", requested
        if transferability_ratio >= 0.35:
            return "upskilling", requested
        return "task_transition", requested
    for key, profile in EDUCATION_PLAN_SCENARIOS.items():
        aliases = [str(item).lower().replace("-", "_").replace(" ", "_") for item in profile.get("aliases") or []]
        if normalized == key or normalized in aliases:
            return key, requested
    return "task_transition", requested


def _collect_plan_highlights(cards: list[dict[str, Any]], key: str, *, limit: int = 8) -> list[str]:
    values: list[str] = []
    for card in cards:
        highlights = card.get("evidence_highlights") if isinstance(card.get("evidence_highlights"), dict) else {}
        for item in highlights.get(key) or []:
            text = _clean(item)
            if text and text not in values:
                values.append(text)
            if len(values) >= limit:
                return values
    return values


def _scenario_extension(
    scenario: str,
    *,
    cards: list[dict[str, Any]],
    primary_courses: list[dict[str, Any]],
    supplemental_courses: list[dict[str, Any]],
    adjacent_courses: list[dict[str, Any]],
    transferability_ratio: float,
    preferred_max_hours: Any,
    preferred_methods: list[Any],
    preferred_facilities: list[Any],
) -> dict[str, Any]:
    profile = EDUCATION_PLAN_SCENARIOS[scenario]
    gap_ksa = _collect_plan_highlights(cards, "gap_ksa")
    target_ksa = _collect_plan_highlights(cards, "target_scope_ksa")
    goal_ksa = _collect_plan_highlights(cards, "goal_ksa")
    career_path = _collect_plan_highlights(cards, "career_path")
    qualifications = _collect_plan_highlights(cards, "qualifications")
    job_base = _collect_plan_highlights(cards, "job_base")
    if scenario == "new_role_onboarding":
        actions = [
            "목표 직무의 NCS 범위와 수행요소를 신규 담당자 기준으로 다시 확인한다.",
            "목표 범위 KSA와 훈련목표 KSA가 동시에 보이는 과정을 먼저 배치한다.",
            "참고 과정은 현장 적용 과제나 멘토링 보조 자료로만 둔다.",
        ]
        focus_evidence = target_ksa + goal_ksa
    elif scenario == "upskilling":
        actions = [
            "공통 KSA는 선수학습으로 인정하고 보완 KSA 중심으로 교육시간을 배분한다.",
            "우선 추천 과정 뒤에 보조 과정을 붙여 심화 과업 커버리지를 넓힌다.",
            "시간 제약이 있으면 fit_summary가 적합한 과정부터 배치한다.",
        ]
        focus_evidence = gap_ksa + target_ksa
    elif scenario == "reskilling":
        actions = [
            "공통 KSA가 낮은 전환으로 보고 목표 직무 기초 KSA부터 배치한다.",
            "낮은 근거의 인접 과정은 참고로만 표시하고 핵심 과정 대체로 사용하지 않는다.",
            "사람 검토 전 별칭/자동 링크가 많으면 교육체계 확정 전 검토 큐에 올린다.",
        ]
        focus_evidence = gap_ksa + goal_ksa
    elif scenario == "qualification_bridge":
        actions = [
            "자격 종목은 교육 추천 보조 근거로만 사용하고 공식 적격성 판단과 분리한다.",
            "자격 근거가 있는 과정은 목표 KSA 직접 근거와 함께 있을 때 우선 검토한다.",
            "자격 coverage가 부족하면 자격 근거 없는 추천을 낮은 신뢰로 설명한다.",
        ]
        focus_evidence = qualifications
    elif scenario == "job_base_gap":
        actions = [
            "목표 직무에서 반복되는 직업기초능력 요인을 공통/부족 역량 설명에 반영한다.",
            "직업기초능력은 KSA 직접 근거를 대체하지 않고 전환 난이도 설명에 사용한다.",
            "기초역량 보완 과정은 핵심 NCS 과정 이후 보조 트랙으로 둔다.",
        ]
        focus_evidence = job_base
    elif scenario == "delivery_fit":
        actions = [
            "훈련시간, 능력단위 수준, 훈련방법, 시설을 운영 가능 조건과 비교한다.",
            "시간 초과 또는 방식 불일치가 있으면 과정 순서와 대체 후보를 조정한다.",
            "원격/집체/현장실습 요구가 명확하면 preferred_methods와 fit_summary를 함께 확인한다.",
        ]
        focus_evidence = [
            str(item)
            for item in [
                f"preferred_max_hours={preferred_max_hours}" if preferred_max_hours else "",
                "preferred_methods=" + ", ".join(str(method) for method in preferred_methods) if preferred_methods else "",
                "preferred_facilities=" + ", ".join(str(facility) for facility in preferred_facilities) if preferred_facilities else "",
            ]
            if item
        ]
    else:
        actions = [
            "현재 범위와 목표 범위 해석을 먼저 확정한다.",
            "보완 KSA와 훈련목표 KSA가 직접 연결된 우선 과정을 배치한다.",
            "보조/인접 과정은 근거 강도를 낮게 표시하고 검토 후보로 분리한다.",
        ]
        focus_evidence = gap_ksa + target_ksa + goal_ksa
    return {
        "scenario": scenario,
        "title": profile["title"],
        "purpose": profile["purpose"],
        "transferability_ratio": transferability_ratio,
        "focus_evidence": list(dict.fromkeys(focus_evidence))[:8],
        "course_mix": {
            "primary": len(primary_courses),
            "supplemental": len(supplemental_courses),
            "adjacent_reference": len(adjacent_courses),
        },
        "actions": actions,
        "caveats": [
            "이 시나리오는 교육체계 설계 보조 관점이며 공식 자격 인정 판단이 아닙니다.",
            "근거가 약한 과정은 우선 교육과정이 아니라 검토 후보로만 해석해야 합니다.",
        ],
    }


def _plan_scenario_extensions(
    selected_scenario: str,
    *,
    cards: list[dict[str, Any]],
    primary_courses: list[dict[str, Any]],
    supplemental_courses: list[dict[str, Any]],
    adjacent_courses: list[dict[str, Any]],
    transferability_ratio: float,
    preferred_max_hours: Any,
    preferred_methods: list[Any],
    preferred_facilities: list[Any],
) -> list[dict[str, Any]]:
    scenarios = [selected_scenario]
    if _collect_plan_highlights(cards, "qualifications") and "qualification_bridge" not in scenarios:
        scenarios.append("qualification_bridge")
    if _collect_plan_highlights(cards, "job_base") and "job_base_gap" not in scenarios:
        scenarios.append("job_base_gap")
    if (preferred_max_hours or preferred_methods or preferred_facilities) and "delivery_fit" not in scenarios:
        scenarios.append("delivery_fit")
    return [
        _scenario_extension(
            scenario,
            cards=cards,
            primary_courses=primary_courses,
            supplemental_courses=supplemental_courses,
            adjacent_courses=adjacent_courses,
            transferability_ratio=transferability_ratio,
            preferred_max_hours=preferred_max_hours,
            preferred_methods=preferred_methods,
            preferred_facilities=preferred_facilities,
        )
        for scenario in scenarios[:4]
    ]


def compact_ncs_education_plan_response(
    transition_result: dict[str, Any],
    *,
    plan_objective: str | None = None,
    target_population: str | None = None,
    scenario: str | None = None,
    recommendation_limit: int = DEFAULT_RECOMMENDATIONS,
) -> dict[str, Any]:
    if not transition_result.get("ok"):
        return transition_result
    if transition_result.get("view") != "compact_training_transition":
        transition_result = compact_training_transition_response(
            transition_result,
            recommendation_limit=recommendation_limit,
        )
    answer = transition_result.get("answer_summary") or {}
    interpretation = answer.get("interpretation") or {}
    current_scope = interpretation.get("current") or {}
    target_scope = interpretation.get("target") or {}
    requested = transition_result.get("requested") or {}
    current_label = current_scope.get("resolved_as") or requested.get("current_query")
    target_label = target_scope.get("resolved_as") or requested.get("target_query")
    transition_context = (
        transition_result.get("transition")
        if isinstance(transition_result.get("transition"), dict)
        else {}
    )
    target_task = (
        transition_result.get("target_task")
        if isinstance(transition_result.get("target_task"), dict)
        else transition_context.get("target_task")
        if isinstance(transition_context.get("target_task"), dict)
        else transition_result.get("source_task")
        if isinstance(transition_result.get("source_task"), dict)
        else {}
    )
    cards = [
        _public_course_card(card)
        for card in transition_result.get("recommended_courses") or []
        if isinstance(card, dict)
    ]
    primary_courses = _planning_stage_courses(cards, {"primary"}, limit=3)
    supplemental_courses = _planning_stage_courses(cards, {"supplemental"}, limit=3)
    adjacent_courses = _planning_stage_courses(cards, {"adjacent", "adjacent_reference"}, limit=3)
    transition_assessment = answer.get("transition_assessment") or {}
    transition_summary = transition_result.get("transition_summary") or {}
    job_base_transition = transition_result.get("job_base_transition_profile") or {}
    transition_review_basis = _transition_review_basis(transition_summary)
    scope_baseline = _scope_baseline_contract(
        requested=requested,
        current_scope=current_scope,
        target_scope=target_scope,
        transition_assessment=transition_assessment,
        transition_summary=transition_summary,
    )
    transferability_ratio = float(transition_assessment.get("transferability_ratio") or 0.0)
    selected_scenario, requested_scenario = _normalize_education_plan_scenario(
        scenario,
        transferability_ratio=transferability_ratio,
    )
    input_quality = transition_result.get("input_quality") or {}
    warnings = input_quality.get("warnings") if isinstance(input_quality, dict) else []
    caveats = list(answer.get("caveats") or [])
    for warning in warnings or []:
        code = warning.get("code")
        message = warning.get("message")
        field = warning.get("field")
        if code and message:
            caveats.append(f"{field or 'input'}: {message} ({code})")
    if not primary_courses:
        caveats.append("직접 근거가 강한 우선 추천 과정이 없어 보조/참고 과정을 교육체계 초안의 검토 후보로만 사용해야 합니다.")
    objective = _clean(plan_objective) or f"{current_label}에서 {target_label}로 이동하기 위한 NCS 기반 교육체계 초안"
    population = _clean(target_population) or "not_specified"
    preferred_methods = requested.get("preferred_methods") or []
    preferred_facilities = _normalize_text_list(requested.get("preferred_facilities") or [])
    preferred_max_hours = requested.get("preferred_max_hours")
    requested_constraints = {
        "current_query": requested.get("current_query"),
        "target_query": requested.get("target_query"),
        "preferred_max_hours": preferred_max_hours,
        "preferred_methods": preferred_methods,
        "preferred_facilities": preferred_facilities,
    }
    path: list[dict[str, Any]] = [
        {
            "stage": 1,
            "role": "scope_confirmation",
            "guide_stage": "C1-1",
            "title": "NCS 범위와 전환 목표 확정",
            "actions": [
                f"현재 범위를 '{current_label}'로 해석한 것이 맞는지 확인한다.",
                f"목표 범위를 '{target_label}'로 해석한 것이 맞는지 확인한다.",
            ],
            "evidence_basis": [
                "scope_interpretation.current",
                "scope_interpretation.target",
                "answer_summary.interpretation",
            ],
            "outputs": [
                "current_scope",
                "target_scope",
                "scope_baseline",
                "transition_assessment",
            ],
        },
        {
            "stage": 2,
            "role": "core_gap_training",
            "guide_stage": "C1-2",
            "title": "목표 과업 수행에 필요한 핵심 KSA 보완",
            "priority_gaps": (answer.get("key_gap_ksa") or [])[:8],
            "courses": primary_courses,
            "selection_rule": "목표 NCS 범위, 보완 KSA, 훈련목표 KSA에 직접 근거가 있는 과정을 우선 배치한다.",
        },
        {
            "stage": 3,
            "role": "supporting_or_adjacent_training",
            "guide_stage": "C2-1",
            "title": "보조 과정과 인접 참고 과정 검토",
            "job_base_gaps": (job_base_transition.get("gaps") or [])[:8],
            "job_base_gap_context": {
                "gap_count": int(job_base_transition.get("gap_count") or 0),
                "gap_label_status": job_base_transition.get("gap_label_status") or "unknown",
                "labels_unavailable": bool(job_base_transition.get("labels_unavailable")),
                "review_required": bool(job_base_transition.get("review_required")),
                "evidence_role": job_base_transition.get("evidence_role"),
                "scoring_role": job_base_transition.get("scoring_role"),
            },
            "courses": supplemental_courses + adjacent_courses,
            "selection_rule": "보조/인접 과정은 직접 근거가 약할 수 있으므로 우선 과정의 대체가 아니라 보완 후보로 검토한다.",
        },
    ]
    path.append(
        {
            "stage": 4,
            "role": "delivery_fit_review",
            "guide_stage": "C2-2",
            "title": "연간 운영계획과 훈련방법 적합성 검토",
            "constraints": {
                "preferred_max_hours": preferred_max_hours,
                "preferred_methods": preferred_methods,
                "preferred_facilities": preferred_facilities,
            },
            "actions": [
                "각 과정의 훈련시간, 수준, 훈련방법, 시설을 운영 가능 시간표와 비교한다.",
                "선호 조건이 없더라도 연간 교육운영계획에 필요한 기간, 대상, 방법, 검토 상태를 보존한다.",
                "fit_summary에 시간 초과나 방식 불일치가 있으면 과정 순서나 대체 과정을 조정한다.",
            ],
        }
    )
    for item in path:
        item["transition_review_basis"] = transition_review_basis
    training_system_matrix = _training_system_matrix(
        cards,
        current_label=current_label,
        target_label=target_label,
        target_task=target_task,
        target_task_element=target_scope.get("task_element"),
        preferred_methods=preferred_methods,
        preferred_max_hours=preferred_max_hours,
        preferred_facilities=preferred_facilities,
    )
    for row in training_system_matrix:
        row["transition_review_basis"] = transition_review_basis
    course_intake_requirements = _course_intake_requirements(
        training_system_matrix,
        current_label=current_label,
        target_label=target_label,
        target_population=population,
        requested_constraints=requested_constraints,
    )
    training_course_inventory_template = _training_course_inventory_template(
        training_system_matrix,
        target_population=population,
        requested_constraints=requested_constraints,
    )
    training_necessity_review = _training_necessity_review(
        training_system_matrix,
        target_population=population,
        requested_constraints=requested_constraints,
    )
    training_system_summary = _training_system_summary(training_system_matrix)
    annual_operation_plan = _annual_operation_plan_seed(
        training_system_matrix,
        target_population=population,
        requested_constraints=requested_constraints,
    )
    training_system_guide_trace = _training_system_guide_trace(
        training_system_matrix,
        current_label=current_label,
        target_label=target_label,
        priority_gaps=(answer.get("key_gap_ksa") or [])[:8],
        summary=training_system_summary,
    )
    stage_evidence_by_code = {
        item.get("code"): item
        for item in training_system_guide_trace.get("guide_workflow_stages", [])
        if isinstance(item, dict) and item.get("code")
    }
    for item in path:
        guide_stage = item.get("guide_stage")
        stage_evidence = stage_evidence_by_code.get(guide_stage)
        if not isinstance(stage_evidence, dict):
            continue
        item["guide_stage_status"] = stage_evidence.get("status")
        item["guide_stage_evidence"] = {
            "title": stage_evidence.get("title"),
            "evidence": stage_evidence.get("evidence"),
            "output_fields": stage_evidence.get("output_fields") or [],
        }
    return {
        "ok": True,
        "view": "ncs_education_plan",
        "disclaimer": transition_result.get("disclaimer") or DISCLAIMER,
        "requested": requested_constraints,
        "plan_objective": objective,
        "target_population": population,
        "current_scope": current_scope,
        "target_scope": target_scope,
        "scope_baseline": scope_baseline,
        "transition_assessment": transition_assessment,
        "job_base_transition_profile": job_base_transition,
        "transition_review_basis": transition_review_basis,
        "scenario": {
            "requested": requested_scenario,
            "selected": selected_scenario,
            "title": EDUCATION_PLAN_SCENARIOS[selected_scenario]["title"],
            "available": _available_education_plan_scenarios(),
        },
        "scenario_extensions": _plan_scenario_extensions(
            selected_scenario,
            cards=cards,
            primary_courses=primary_courses,
            supplemental_courses=supplemental_courses,
            adjacent_courses=adjacent_courses,
            transferability_ratio=transferability_ratio,
            preferred_max_hours=preferred_max_hours,
            preferred_methods=preferred_methods,
            preferred_facilities=preferred_facilities,
        ),
        "priority_gaps": (answer.get("key_gap_ksa") or [])[:8],
        "recommended_path": path,
        "course_intake_requirements": course_intake_requirements,
        "training_course_inventory_template": training_course_inventory_template,
        "training_necessity_review": training_necessity_review,
        "training_system_summary": training_system_summary,
        "annual_operation_plan": annual_operation_plan,
        "training_system_guide_trace": training_system_guide_trace,
        "training_system_matrix": training_system_matrix,
        "recommended_courses": cards,
        "evidence_basis": {
            "source_recommendation_counts": transition_result.get("source_recommendation_counts") or {},
            "transition_summary": transition_summary,
            "course_evidence_fields": [
                "evidence_highlights",
                "why_recommended",
                "coverage_summary",
                "score_component_highlights",
                "quality_issue_penalty",
                "delivery",
                "scope_baseline",
                "course_scope_fit",
                "training_system_fit",
                "training_system_summary",
                "course_intake_requirements",
                "training_course_inventory_template",
                "training_necessity_review",
                "annual_operation_plan",
                "training_system_guide_trace",
                "training_system_matrix",
                "career_path_review_basis",
                "transition_review_basis",
                "job_base_transition_profile",
            ],
        },
        "assumptions": [
            "질의 해석은 NCS 능력단위/분류/수행준거 resolver와 등록된 별칭을 기준으로 한다.",
            "과정 우선순위는 목표 범위 KSA, 보완 KSA, 훈련목표 KSA, 경력경로, 자격, 직업기초능력 근거를 함께 본다.",
            "후보 과정은 현재 캐시된 SQLite 데이터와 전처리된 온톨로지 링크 범위 안에서 산출한다.",
        ],
        "non_goals": [
            "공식 자격 인정, 법적 적격성, 채용 적합성 판단이 아니다.",
            "SQF와 NCS 학습모듈은 활성 추천 근거로 사용하지 않는다.",
            "사람 검토 전 candidate 별칭이나 자동 링크를 확정 사실로 간주하지 않는다.",
        ],
        "caveats": list(dict.fromkeys(caveats)),
        "input_quality": input_quality,
        "audit": _public_plan_audit(transition_result),
    }


def recommend_training_transition(
    conn: sqlite3.Connection,
    *,
    current_query: str,
    target_query: str,
    major_code: str | None = None,
    current_major_code: str | None = None,
    target_major_code: str | None = None,
    current_middle_code: str | None = None,
    target_middle_code: str | None = None,
    current_small_code: str | None = None,
    target_small_code: str | None = None,
    current_sub_code: str | None = None,
    target_sub_code: str | None = None,
    mode: str = "all",
    preferred_max_hours: float | None = None,
    preferred_methods: list[str] | None = None,
    preferred_facilities: list[str] | None = None,
    limit: int = DEFAULT_RECOMMENDATIONS,
    save: bool = True,
) -> dict[str, Any]:
    if not _clean(current_query) or not _clean(target_query):
        return {"ok": False, "error": {"code": "missing_transition_query"}}
    requested_current_query = current_query
    requested_target_query = target_query
    effective_current_major = current_major_code or major_code
    effective_target_major = target_major_code or major_code
    requested_current_filters = {
        "major_code": effective_current_major,
        "middle_code": current_middle_code,
        "small_code": current_small_code,
        "sub_code": current_sub_code,
    }
    requested_target_filters = {
        "major_code": effective_target_major,
        "middle_code": target_middle_code,
        "small_code": target_small_code,
        "sub_code": target_sub_code,
    }
    normalized_current_query, current_query_normalization = _generic_job_query_normalization(requested_current_query)
    normalized_target_query, target_query_normalization = _generic_job_query_normalization(requested_target_query)
    current_query, effective_current_major, current_middle_code, current_small_code, current_sub_code, current_alias = _apply_query_alias(
        conn,
        current_query,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
    )
    target_query, effective_target_major, target_middle_code, target_small_code, target_sub_code, target_alias = _apply_query_alias(
        conn,
        target_query,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
    )
    current_unit_code = _clean(current_alias.get("unit_code")) if current_alias else None
    target_unit_code = _clean(target_alias.get("unit_code")) if target_alias else None
    current_alias_guard = _alias_unit_conflicts_with_exact_unit(
        conn,
        requested_query=requested_current_query,
        alias=current_alias,
        **requested_current_filters,
    )
    if current_alias_guard:
        if current_alias:
            current_alias = {**current_alias, "ignored_unit_code": current_unit_code, "ignore_guard": current_alias_guard}
        current_unit_code = None
        current_query = requested_current_query
        effective_current_major = requested_current_filters["major_code"]
        current_middle_code = requested_current_filters["middle_code"]
        current_small_code = requested_current_filters["small_code"]
        current_sub_code = requested_current_filters["sub_code"]
    target_alias_guard = _alias_unit_conflicts_with_exact_unit(
        conn,
        requested_query=requested_target_query,
        alias=target_alias,
        **requested_target_filters,
    )
    if target_alias_guard:
        if target_alias:
            target_alias = {**target_alias, "ignored_unit_code": target_unit_code, "ignore_guard": target_alias_guard}
        target_unit_code = None
        target_query = requested_target_query
        effective_target_major = requested_target_filters["major_code"]
        target_middle_code = requested_target_filters["middle_code"]
        target_small_code = requested_target_filters["small_code"]
        target_sub_code = requested_target_filters["sub_code"]
    current_resolution = resolve_ncs_query_scope(
        conn,
        current_query or requested_current_query,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
        limit=12,
    )
    current_resolution = _attach_query_normalization(
        current_resolution,
        current_query_normalization,
        normalized_current_query,
    )
    target_resolution = resolve_ncs_query_scope(
        conn,
        target_query or requested_target_query,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
        limit=12,
    )
    target_resolution = _attach_query_normalization(
        target_resolution,
        target_query_normalization,
        normalized_target_query,
    )
    current_filters = _resolution_classification_filters(
        current_resolution,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
    )
    target_filters = _resolution_classification_filters(
        target_resolution,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
    )
    effective_current_major = current_filters["major_code"]
    current_middle_code = current_filters["middle_code"]
    current_small_code = current_filters["small_code"]
    current_sub_code = current_filters["sub_code"]
    effective_target_major = target_filters["major_code"]
    target_middle_code = target_filters["middle_code"]
    target_small_code = target_filters["small_code"]
    target_sub_code = target_filters["sub_code"]
    current_source_query = None if _resolution_classification_scope_candidate(current_resolution) else current_query
    target_source_query = None if _resolution_classification_scope_candidate(target_resolution) else target_query
    current_source = resolve_task_criteria(
        conn,
        query=None if current_unit_code else current_source_query,
        unit_code=current_unit_code,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
    )
    target_source = resolve_task_criteria(
        conn,
        query=None if target_unit_code else target_source_query,
        unit_code=target_unit_code,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
    )
    if current_source is None:
        response = not_found_response("현재 직무 범위를 찾을 수 없습니다.", suggestions=[item["query"] for item in _candidate_query_items(current_resolution)])
        response["input_quality"] = {
            "ok": False,
            "warnings": [{"code": "scope_not_found", "field": "current_query", "message": "현재 직무 범위를 찾을 수 없습니다."}],
            "suggestions": response["error"]["suggestions"],
            "candidate_queries": {"current_query": _candidate_query_items(current_resolution)},
        }
        return response
    if target_source is None:
        response = not_found_response("목표 직무 범위를 찾을 수 없습니다.", suggestions=[item["query"] for item in _candidate_query_items(target_resolution)])
        response["input_quality"] = {
            "ok": False,
            "warnings": [{"code": "scope_not_found", "field": "target_query", "message": "목표 직무 범위를 찾을 수 없습니다."}],
            "suggestions": response["error"]["suggestions"],
            "candidate_queries": {"target_query": _candidate_query_items(target_resolution)},
        }
        return response
    current_scope = _resolve_query_scope_units(
        conn,
        query=current_query,
        source=current_source,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
    )
    target_scope = _resolve_query_scope_units(
        conn,
        query=target_query,
        source=target_source,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
    )
    if current_unit_code and current_alias:
        current_scope["match_level"] = "query_alias_unit"
        current_scope["alias_text"] = current_alias.get("alias_text")
        current_scope["alias_normalized_query"] = current_alias.get("normalized_query")
        current_scope["alias_confidence_score"] = current_alias.get("confidence_score")
    if target_unit_code and target_alias:
        target_scope["match_level"] = "query_alias_unit"
        target_scope["alias_text"] = target_alias.get("alias_text")
        target_scope["alias_normalized_query"] = target_alias.get("normalized_query")
        target_scope["alias_confidence_score"] = target_alias.get("confidence_score")
    current_role_overlay = _role_overlay_profile(
        requested_query=requested_current_query,
        effective_query=current_query,
        alias=current_alias,
        scope=current_scope,
    )
    target_role_overlay = _role_overlay_profile(
        requested_query=requested_target_query,
        effective_query=target_query,
        alias=target_alias,
        scope=target_scope,
    )
    if current_role_overlay:
        current_scope["role_overlay"] = current_role_overlay
    if target_role_overlay:
        target_scope["role_overlay"] = target_role_overlay
    current_profile = _scope_concepts(conn, set(current_scope["unit_codes"]), limit=300)
    target_profile = _scope_concepts(conn, set(target_scope["unit_codes"]), limit=300)
    current_ids = {_concept_key(item) for item in current_profile}
    target_ids = {_concept_key(item) for item in target_profile}
    transferable_ids = current_ids & target_ids
    gap_ids = target_ids - current_ids
    transferable = _filter_concepts(target_profile, transferable_ids, limit=50)
    gaps = _filter_concepts(target_profile, gap_ids, limit=80)
    current_job_base_profile = job_base_profile_for_units(conn, set(current_scope["unit_codes"]), limit=500)
    target_job_base_profile = job_base_profile_for_units(conn, set(target_scope["unit_codes"]), limit=500)
    current_job_base_keys = {_job_base_key(item) for item in current_job_base_profile}
    target_job_base_keys = {_job_base_key(item) for item in target_job_base_profile}
    current_qualification_profile = qualification_profile_for_units(conn, set(current_scope["unit_codes"]), limit=300)
    target_qualification_profile = qualification_profile_for_units(conn, set(target_scope["unit_codes"]), limit=300)
    current_qualification_keys = {_qualification_key(item) for item in current_qualification_profile if _qualification_key(item)}
    target_qualification_keys = {_qualification_key(item) for item in target_qualification_profile if _qualification_key(item)}
    semantic_fit = _transition_semantic_fit(
        conn,
        current_scope=current_scope,
        target_scope=target_scope,
        current_ids=current_ids,
        target_ids=target_ids,
        exact_ids=transferable_ids,
        current_job_base_keys=current_job_base_keys,
        target_job_base_keys=target_job_base_keys,
        current_qualification_keys=current_qualification_keys,
        target_qualification_keys=target_qualification_keys,
        target_role_overlay=target_role_overlay,
    )
    recommendation = recommend_training_for_task(
        conn,
        query=target_query,
        major_code=effective_target_major,
        middle_code=target_middle_code,
        small_code=target_small_code,
        sub_code=target_sub_code,
        mode=mode,
        current_concepts=_concept_names(current_profile),
        sequence_target_label=target_scope.get("match_text") or target_query,
        target_qualification_keys=target_qualification_keys,
        gap_qualification_keys=target_qualification_keys - current_qualification_keys,
        target_job_base_keys=target_job_base_keys,
        gap_job_base_keys=target_job_base_keys - current_job_base_keys,
        already_covered_unit_codes=set(current_scope["unit_codes"]),
        preferred_max_hours=preferred_max_hours,
        preferred_methods=preferred_methods,
        precomputed_query_resolution=target_resolution,
        limit=limit,
        save=save,
    )
    if not recommendation.get("ok"):
        return recommendation
    current_career_paths = career_paths_for_units(conn, set(current_scope["unit_codes"]), limit=300)
    target_career_paths = career_paths_for_units(conn, set(target_scope["unit_codes"]), limit=300)
    current_trusted_career_paths = career_paths_for_units(
        conn,
        set(current_scope["unit_codes"]),
        limit=300,
        review_statuses=TRUSTED_CAREER_PATH_REVIEW_STATUSES,
    )
    target_trusted_career_paths = career_paths_for_units(
        conn,
        set(target_scope["unit_codes"]),
        limit=300,
        review_statuses=TRUSTED_CAREER_PATH_REVIEW_STATUSES,
    )
    transition_summary = {
        "current_query": current_query,
        "target_query": target_query,
        "requested_current_query": requested_current_query,
        "requested_target_query": requested_target_query,
        "current_scope_unit_count": len(set(current_scope["unit_codes"])),
        "target_scope_unit_count": len(set(target_scope["unit_codes"])),
        "current_ksa_concept_count": len(current_profile),
        "target_ksa_concept_count": len(target_profile),
        "transferable_ksa_concept_count": len(transferable_ids),
        "gap_ksa_concept_count": len(gap_ids),
        "transferability_ratio": round(len(transferable_ids) / len(target_ids), 4) if target_ids else 0.0,
        "exact_ksa_overlap_ratio": semantic_fit["components"]["exact_ksa_overlap_ratio"],
        "ontology_adjusted_transferability_ratio": semantic_fit["ratio"],
        "transferability_method": semantic_fit["method"],
        "adjusted_transferability_components": semantic_fit["components"],
        "ncs_scope_relation": semantic_fit["scope_relation"],
        "current_scope_subset_of_target": semantic_fit["current_scope_subset_of_target"],
        "shared_unit_count": semantic_fit["shared_unit_count"],
        "ontology_related_ksa_count": semantic_fit["ontology_related_ksa_count"],
        "task_similarity_max_score": semantic_fit["task_similarity"]["max_score"],
        "task_similarity_link_count": semantic_fit["task_similarity"]["link_count"],
        "target_role_overlay": target_role_overlay,
        "preferred_max_hours": preferred_max_hours,
        "preferred_methods": preferred_methods or [],
        "preferred_facilities": preferred_facilities or [],
        "current_career_path_count": len(current_career_paths),
        "target_career_path_count": len(target_career_paths),
        "current_trusted_career_path_count": len(current_trusted_career_paths),
        "target_trusted_career_path_count": len(target_trusted_career_paths),
        "current_career_path_review_status_counts": _review_status_counts(current_career_paths),
        "target_career_path_review_status_counts": _review_status_counts(target_career_paths),
        "current_job_base_count": len(current_job_base_keys),
        "target_job_base_count": len(target_job_base_keys),
        "transferable_job_base_count": len(current_job_base_keys & target_job_base_keys),
        "gap_job_base_count": len(target_job_base_keys - current_job_base_keys),
        "current_qualification_count": len(current_qualification_keys),
        "target_qualification_count": len(target_qualification_keys),
        "transferable_qualification_count": len(current_qualification_keys & target_qualification_keys),
        "gap_qualification_count": len(target_qualification_keys - current_qualification_keys),
    }
    explanation = [
        f"{requested_current_query}은(는) NCS '{current_scope['match_text']}' 범위로, {requested_target_query}은(는) NCS '{target_scope['match_text']}' 범위로 해석했습니다.",
        f"동일 KSA concept_id 기준 공통 KSA는 목표 {len(target_ids)}개 중 {len(transferable_ids)}개입니다.",
        (
            "NCS scope/온톨로지/과업유사도/직위 overlay를 반영한 보정 전이율은 "
            f"{semantic_fit['ratio']:.1%}입니다."
        ),
    ]
    if target_role_overlay:
        explanation.append(
            f"목표 '{requested_target_query}'는 공식 NCS 능력단위가 아니라 "
            f"{target_role_overlay['label']}로 해석해 인사 직무군 범위를 함께 봅니다."
        )
    if semantic_fit["current_scope_subset_of_target"]:
        explanation.append("현재 NCS 범위가 목표 직위형 범위 안에 포함되어 있어 직무 기반성은 높게 평가합니다.")
    if transferable:
        explanation.append("전이 가능한 대표 KSA: " + ", ".join(_concept_names(transferable[:6])))
    if gaps:
        explanation.append("우선 보완할 대표 KSA: " + ", ".join(_concept_names(gaps[:8])))
    recommendation["transition"] = {
        "summary": transition_summary,
        "explanation": explanation,
        "current_query_resolution": current_resolution,
        "target_query_resolution": target_resolution,
        "current_query_alias": current_alias,
        "target_query_alias": target_alias,
        "current_scope": current_scope,
        "target_scope": target_scope,
        "current_task": {
            "criteria_id": current_source["criteria_id"],
            "criteria_text": current_source["criteria_text_raw"],
            "unit_code": current_source["unit_code"],
            "unit_name": current_source["unit_name_raw"],
            "element_id": current_source["element_id"],
            "element_name": current_source["element_name_raw"],
            "classification": {
                "major_code": current_source["major_code"],
                "major_name": current_source["major_name"],
                "middle_code": current_source["middle_code"],
                "middle_name": current_source["middle_name"],
                "small_code": current_source["small_code"],
                "small_name": current_source["small_name"],
                "sub_code": current_source["sub_code"],
                "sub_name": current_source["sub_name"],
            },
        },
        "target_task": recommendation["source_task"],
        "current_ksa_profile": current_profile[:80],
        "target_ksa_profile": target_profile[:80],
        "transferable_ksa": transferable,
        "gap_ksa": gaps,
        "ontology_adjusted_transferability": {
            "ratio": semantic_fit["ratio"],
            "method": semantic_fit["method"],
            "components": semantic_fit["components"],
            "scope_relation": semantic_fit["scope_relation"],
            "current_scope_subset_of_target": semantic_fit["current_scope_subset_of_target"],
            "shared_unit_count": semantic_fit["shared_unit_count"],
            "ontology_related_ksa_count": semantic_fit["ontology_related_ksa_count"],
            "ontology_related_evidence": semantic_fit["ontology_related_evidence"],
            "task_similarity": semantic_fit["task_similarity"],
            "role_overlay": target_role_overlay,
        },
        "current_career_path": current_trusted_career_paths[:80],
        "target_career_path": target_trusted_career_paths[:80],
        "current_candidate_career_path_review_status_counts": _review_status_counts(current_career_paths),
        "target_candidate_career_path_review_status_counts": _review_status_counts(target_career_paths),
        "current_job_base_profile": current_job_base_profile[:80],
        "target_job_base_profile": target_job_base_profile[:80],
        "current_qualification_profile": current_qualification_profile[:80],
        "target_qualification_profile": target_qualification_profile[:80],
    }
    recommendation["transition_request"] = {
        "current_query": requested_current_query,
        "target_query": requested_target_query,
    }
    recommendation["source_task_role"] = "target_task"
    recommendation["current_task"] = recommendation["transition"]["current_task"]
    recommendation["target_task"] = recommendation["source_task"]
    recommendation["current_query_resolution"] = current_resolution
    recommendation["target_query_resolution"] = target_resolution
    recommendation["recommendation_summary"]["transition"] = transition_summary
    recommendation["audit"]["data_sources"] = sorted(
        set(recommendation["audit"].get("data_sources") or [])
        | {
            "transition_current_scope_profile",
            "transition_target_scope_profile",
            "ncs_career_paths",
            "ncs_job_base_competencies",
            "ncs_job_base_factors",
            "ncs_unit_job_base_links",
            "ncs_qualification_items",
            "ncs_unit_qualification_links",
            "task_similarity_links",
        }
    )
    return recommendation


def _ranking_metrics(expected_courses: list[str], recommended_courses: list[str]) -> dict[str, Any]:
    expected = list(dict.fromkeys(expected_courses))
    expected_set = set(expected)
    hits_by_rank = [1 if course in expected_set else 0 for course in recommended_courses]
    hit_count = sum(hits_by_rank)
    possible_hit_count = min(len(expected), len(recommended_courses))
    first_rank = None
    for index, hit in enumerate(hits_by_rank, start=1):
        if hit:
            first_rank = index
            break
    precision = hit_count / len(recommended_courses) if recommended_courses else 0.0
    precision_upper_bound = possible_hit_count / len(recommended_courses) if recommended_courses else 0.0
    precision_relative_to_upper_bound = (
        hit_count / possible_hit_count if possible_hit_count else None
    )
    recall = hit_count / len(expected) if expected else None
    reciprocal_rank = 1.0 / first_rank if first_rank else 0.0
    precision_sum = 0.0
    seen_hits = 0
    for index, hit in enumerate(hits_by_rank, start=1):
        if hit:
            seen_hits += 1
            precision_sum += seen_hits / index
    average_precision = precision_sum / len(expected) if expected else None
    dcg = sum(hit / math.log2(index + 1) for index, hit in enumerate(hits_by_rank, start=1))
    ideal_len = min(len(expected), len(recommended_courses))
    ideal_dcg = sum(1 / math.log2(index + 1) for index in range(1, ideal_len + 1))
    ndcg = dcg / ideal_dcg if ideal_dcg else None
    return {
        "hit_count": hit_count,
        "possible_hit_count": possible_hit_count,
        "precision_at_k": round(precision, 4),
        "precision_at_k_upper_bound": round(precision_upper_bound, 4),
        "precision_at_k_relative_to_upper_bound": round(precision_relative_to_upper_bound, 4)
        if precision_relative_to_upper_bound is not None
        else None,
        "expected_recall_at_k": round(recall, 4) if recall is not None else None,
        "top1_expected_hit": bool(hits_by_rank[:1] and hits_by_rank[0]),
        "first_expected_rank": first_rank,
        "reciprocal_rank": round(reciprocal_rank, 4),
        "average_precision_at_k": round(average_precision, 4) if average_precision is not None else None,
        "ndcg_at_k": round(ndcg, 4) if ndcg is not None else None,
    }


def _transition_case_course_evidence(
    recommendations: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in recommendations[: max(0, limit)]:
        if not isinstance(raw, dict):
            continue
        card = _compact_course_card(raw, transition=True)
        fit = card.get("training_system_fit") if isinstance(card.get("training_system_fit"), dict) else {}
        scope_fit = _public_course_scope_fit(card.get("course_scope_fit") or fit.get("course_scope_fit"))
        review_flags = [str(flag) for flag in fit.get("review_flags") or [] if str(flag).strip()]
        quality_penalty = (
            card.get("quality_issue_penalty")
            if isinstance(card.get("quality_issue_penalty"), dict)
            else {}
        )
        review_flags = list(
            dict.fromkeys(
                [
                    *review_flags,
                    *[
                        f"quality_issue:{issue_type}"
                        for issue_type in quality_penalty.get("issue_types") or []
                        if str(issue_type).strip()
                    ],
                ]
            )
        )
        rows.append(
            {
                "rank": card.get("rank"),
                "training_course_id": card.get("training_course_id"),
                "course_name": card.get("course_name"),
                "tier": card.get("tier"),
                "confidence_score": card.get("confidence_score"),
                "confidence_grade": card.get("confidence_grade"),
                "course_scope_fit": scope_fit,
                "need_classification": fit.get("need_classification") or {},
                "evidence_directness": fit.get("evidence_directness") or {},
                "task_ksa_basis": fit.get("task_ksa_basis") or {},
                "course_fit": fit.get("course_fit") or {},
                "quality_issue_penalty": quality_penalty,
                "job_base_signal": card.get("job_base_signal") or {},
                "review_flags": review_flags,
                "human_review": {
                    "severity": "needs_review" if review_flags else "ready",
                    "flags": review_flags,
                    "prompt": fit.get("human_review_prompt") or "",
                },
            }
        )
    return rows


def _course_scope_summary_from_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    relation_counts: Counter[str] = Counter()
    alignment_counts: Counter[str] = Counter()
    review_flag_counts: Counter[str] = Counter()
    direct_or_near_count = 0
    requires_scope_review_count = 0
    for row in rows:
        scope_fit = row.get("course_scope_fit") if isinstance(row.get("course_scope_fit"), dict) else {}
        relation = str(scope_fit.get("relation") or "unknown")
        alignment = str(scope_fit.get("alignment") or "unknown")
        relation_counts[relation] += 1
        alignment_counts[alignment] += 1
        if alignment in {"direct", "near"}:
            direct_or_near_count += 1
        if scope_fit.get("requires_scope_review") or alignment in {"adjacent", "distant", "unknown"}:
            requires_scope_review_count += 1
        for flag in row.get("review_flags") or []:
            if str(flag).strip():
                review_flag_counts[str(flag)] += 1
    return {
        "course_count": len(rows),
        "relation_counts": dict(sorted(relation_counts.items())),
        "alignment_counts": dict(sorted(alignment_counts.items())),
        "direct_or_near_count": direct_or_near_count,
        "requires_scope_review_count": requires_scope_review_count,
        "review_flag_counts": dict(sorted(review_flag_counts.items())),
    }


def evaluate_training_transition_scenarios(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
    review_statuses: list[str] | None = None,
    scenario_ids: list[int] | None = None,
) -> dict[str, Any]:
    max_items = clamp_limit(limit, default=DEFAULT_RECOMMENDATIONS, maximum=MAX_RECOMMENDATIONS)
    params: list[Any] = []
    where_clauses = ["review_status != 'rejected'"]
    review_status_filter: list[str] = ["not_rejected"]
    if review_statuses:
        placeholders = ",".join("?" for _ in review_statuses)
        where_clauses = [f"review_status IN ({placeholders})"]
        params.extend(review_statuses)
        review_status_filter = list(review_statuses)
    scenario_id_filter: list[int] = []
    if scenario_ids:
        scenario_id_filter = [int(item) for item in scenario_ids]
        placeholders = ",".join("?" for _ in scenario_id_filter)
        where_clauses.append(f"scenario_id IN ({placeholders})")
        params.extend(scenario_id_filter)
    where_clause = " AND ".join(where_clauses)
    sql = f"""
        SELECT *
        FROM training_transition_gold_scenarios
        WHERE {where_clause}
        ORDER BY scenario_id
    """
    if scenario_limit is not None:
        sql += " LIMIT ?"
        params.append(clamp_limit(scenario_limit, default=20, maximum=1000))
    rows = conn.execute(sql, params).fetchall()
    cases: list[dict[str, Any]] = []
    current_scope_hits = 0
    target_scope_hits = 0
    expected_course_hit_count = 0
    possible_expected_course_hit_count = 0
    total_expected = 0
    recommended_course_total = 0
    top1_expected_hits = 0
    reciprocal_rank_sum = 0.0
    average_precision_sum = 0.0
    ndcg_sum = 0.0
    ranked_case_count = 0
    course_scope_relation_counts: Counter[str] = Counter()
    course_scope_alignment_counts: Counter[str] = Counter()
    course_scope_review_required_count = 0
    breakdown: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            expected_courses = json.loads(row["expected_course_names_json"] or "[]")
        except json.JSONDecodeError:
            expected_courses = []
        total_expected += len(expected_courses)
        major_key = row["major_code"] or "unknown"
        review_key = row["review_status"] or "unknown"
        for key in (f"major:{major_key}", f"review_status:{review_key}"):
            breakdown.setdefault(
                key,
                {
                    "scenario_count": 0,
                    "current_scope_hits": 0,
                    "target_scope_hits": 0,
                    "expected_course_hit_count": 0,
                    "possible_expected_course_hit_count": 0,
                    "expected_course_total": 0,
                    "recommended_course_total": 0,
                    "top1_expected_hits": 0,
                    "reciprocal_rank_sum": 0.0,
                    "average_precision_sum": 0.0,
                    "ndcg_sum": 0.0,
                    "ranked_case_count": 0,
                },
            )
            breakdown[key]["scenario_count"] += 1
            breakdown[key]["expected_course_total"] += len(expected_courses)
        result = recommend_training_transition(
            conn,
            current_query=row["current_query"],
            target_query=row["target_query"],
            major_code=row["major_code"],
            limit=max_items,
            save=False,
        )
        if not result.get("ok"):
            cases.append(
                {
                    "scenario_id": row["scenario_id"],
                    "scenario_name": row["scenario_name"],
                    "review_status": row["review_status"],
                    "ok": False,
                    "error": result.get("error"),
                    "expected_courses": expected_courses,
                }
            )
            continue
        current_match = result["transition"]["current_scope"]["match_text"]
        target_match = result["transition"]["target_scope"]["match_text"]
        current_hit = not row["expected_current_match_text"] or current_match == row["expected_current_match_text"]
        target_hit = not row["expected_target_match_text"] or target_match == row["expected_target_match_text"]
        if current_hit:
            current_scope_hits += 1
        if target_hit:
            target_scope_hits += 1
        raw_recommendations = [
            item
            for item in result.get("recommendations", [])[:max_items]
            if isinstance(item, dict)
        ]
        recommended_courses = [
            (item.get("training_course") or {}).get("compe_unit_name")
            for item in raw_recommendations
            if (item.get("training_course") or {}).get("compe_unit_name")
        ]
        recommended_course_evidence = _transition_case_course_evidence(
            raw_recommendations,
            limit=max_items,
        )
        recommended_course_scope_summary = _course_scope_summary_from_evidence(
            recommended_course_evidence,
        )
        course_scope_relation_counts.update(recommended_course_scope_summary["relation_counts"])
        course_scope_alignment_counts.update(recommended_course_scope_summary["alignment_counts"])
        course_scope_review_required_count += int(
            recommended_course_scope_summary.get("requires_scope_review_count") or 0
        )
        course_hits = [expected for expected in expected_courses if expected in recommended_courses]
        ranking = _ranking_metrics(expected_courses, recommended_courses)
        expected_course_hit_count += len(course_hits)
        possible_expected_course_hit_count += int(ranking["possible_hit_count"])
        recommended_course_total += len(recommended_courses)
        top1_expected_hits += 1 if ranking["top1_expected_hit"] else 0
        reciprocal_rank_sum += float(ranking["reciprocal_rank"] or 0.0)
        if ranking["average_precision_at_k"] is not None:
            average_precision_sum += float(ranking["average_precision_at_k"])
        if ranking["ndcg_at_k"] is not None:
            ndcg_sum += float(ranking["ndcg_at_k"])
        ranked_case_count += 1
        for key in (f"major:{major_key}", f"review_status:{review_key}"):
            if current_hit:
                breakdown[key]["current_scope_hits"] += 1
            if target_hit:
                breakdown[key]["target_scope_hits"] += 1
            breakdown[key]["expected_course_hit_count"] += len(course_hits)
            breakdown[key]["possible_expected_course_hit_count"] += int(ranking["possible_hit_count"])
            breakdown[key]["recommended_course_total"] += len(recommended_courses)
            breakdown[key]["top1_expected_hits"] += 1 if ranking["top1_expected_hit"] else 0
            breakdown[key]["reciprocal_rank_sum"] += float(ranking["reciprocal_rank"] or 0.0)
            if ranking["average_precision_at_k"] is not None:
                breakdown[key]["average_precision_sum"] += float(ranking["average_precision_at_k"])
            if ranking["ndcg_at_k"] is not None:
                breakdown[key]["ndcg_sum"] += float(ranking["ndcg_at_k"])
            breakdown[key]["ranked_case_count"] += 1
        cases.append(
            {
                "scenario_id": row["scenario_id"],
                "scenario_name": row["scenario_name"],
                "review_status": row["review_status"],
                "ok": True,
                "current_match": current_match,
                "target_match": target_match,
                "current_scope_hit": current_hit,
                "target_scope_hit": target_hit,
                "expected_courses": expected_courses,
                "recommended_courses": recommended_courses,
                "recommended_course_evidence": recommended_course_evidence,
                "recommended_course_scope_summary": recommended_course_scope_summary,
                "expected_course_hits": course_hits,
                **ranking,
                "transferability_ratio": result["transition"]["summary"]["transferability_ratio"],
            }
        )
    scenario_count = len(rows)
    breakdown_summary: dict[str, Any] = {}
    for key, item in sorted(breakdown.items()):
        scenario_total = item["scenario_count"]
        expected_total = item["expected_course_total"]
        recommended_total = item["recommended_course_total"]
        possible_hit_total = item["possible_expected_course_hit_count"]
        ranked_total = item["ranked_case_count"]
        breakdown_summary[key] = {
            **item,
            "current_scope_accuracy": round(item["current_scope_hits"] / scenario_total, 4) if scenario_total else 0.0,
            "target_scope_accuracy": round(item["target_scope_hits"] / scenario_total, 4) if scenario_total else 0.0,
            "expected_course_recall_at_k": round(item["expected_course_hit_count"] / expected_total, 4) if expected_total else None,
            "precision_at_k": round(item["expected_course_hit_count"] / recommended_total, 4) if recommended_total else 0.0,
            "precision_at_k_upper_bound": round(possible_hit_total / recommended_total, 4) if recommended_total else 0.0,
            "precision_at_k_relative_to_upper_bound": round(item["expected_course_hit_count"] / possible_hit_total, 4)
            if possible_hit_total
            else None,
            "top1_expected_hit_rate": round(item["top1_expected_hits"] / ranked_total, 4) if ranked_total else 0.0,
            "mrr_at_k": round(item["reciprocal_rank_sum"] / ranked_total, 4) if ranked_total else 0.0,
            "map_at_k": round(item["average_precision_sum"] / ranked_total, 4) if ranked_total else None,
            "ndcg_at_k": round(item["ndcg_sum"] / ranked_total, 4) if ranked_total else None,
        }
    return {
        "ok": True,
        "scenario_limit": scenario_limit,
        "scenario_id_filter": scenario_id_filter,
        "review_status_filter": review_status_filter,
        "scenario_count": scenario_count,
        "current_scope_accuracy": round(current_scope_hits / scenario_count, 4) if scenario_count else 0.0,
        "target_scope_accuracy": round(target_scope_hits / scenario_count, 4) if scenario_count else 0.0,
        "expected_course_hit_count": expected_course_hit_count,
        "possible_expected_course_hit_count": possible_expected_course_hit_count,
        "expected_course_total": total_expected,
        "expected_course_recall_at_k": round(expected_course_hit_count / total_expected, 4) if total_expected else None,
        "recommended_course_total": recommended_course_total,
        "precision_at_k": round(expected_course_hit_count / recommended_course_total, 4) if recommended_course_total else 0.0,
        "precision_at_k_upper_bound": round(possible_expected_course_hit_count / recommended_course_total, 4)
        if recommended_course_total
        else 0.0,
        "precision_at_k_relative_to_upper_bound": round(
            expected_course_hit_count / possible_expected_course_hit_count,
            4,
        )
        if possible_expected_course_hit_count
        else None,
        "course_scope_relation_counts": dict(sorted(course_scope_relation_counts.items())),
        "course_scope_alignment_counts": dict(sorted(course_scope_alignment_counts.items())),
        "course_scope_review_required_count": course_scope_review_required_count,
        "top1_expected_hit_rate": round(top1_expected_hits / ranked_case_count, 4) if ranked_case_count else 0.0,
        "mrr_at_k": round(reciprocal_rank_sum / ranked_case_count, 4) if ranked_case_count else 0.0,
        "map_at_k": round(average_precision_sum / ranked_case_count, 4) if ranked_case_count else None,
        "ndcg_at_k": round(ndcg_sum / ranked_case_count, 4) if ranked_case_count else None,
        "breakdown": breakdown_summary,
        "cases": cases,
    }


def _is_portable_reports_packet_ref(source_decision_packet: str | None) -> bool:
    return is_portable_reports_packet_ref(
        source_decision_packet,
        extensions=REVIEW_AUDIT_PACKET_EXTENSIONS,
    )


def review_training_transition_scenarios(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
    source_review_statuses: list[str] | None = None,
    target_review_status: str = "candidate_auto",
    require_top1_expected_hit: bool = True,
    min_precision_at_k: float = 0.0,
    min_expected_recall_at_k: float = TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
    apply: bool = False,
    reviewer_id: str | None = None,
    notes: str | None = None,
    source_decision_packet: str | None = None,
    source_artifact_hash: str | None = None,
    rationale: str | None = None,
    evidence_refs: list[Any] | None = None,
    run_artifact: str | None = None,
    allow_automated_status_write: bool = False,
) -> dict[str, Any]:
    statuses = source_review_statuses or ["candidate"]
    evaluation = evaluate_training_transition_scenarios(
        conn,
        limit=limit,
        scenario_limit=scenario_limit,
        review_statuses=statuses,
    )
    eligible_ids: list[int] = []
    reviewed_cases: list[dict[str, Any]] = []
    trusted_target = target_review_status in TRUSTED_TRANSITION_REVIEW_STATUSES
    effective_require_top1_expected_hit = require_top1_expected_hit or trusted_target
    precision_floor = max(0.0, min(float(min_precision_at_k), 1.0))
    recall_floor = max(0.0, min(float(min_expected_recall_at_k), 1.0))
    criteria = {
        "require_current_scope_hit": True,
        "require_target_scope_hit": True,
        "require_top1_expected_hit": effective_require_top1_expected_hit,
        "require_any_expected_course_hit": True,
        "min_precision_at_k": precision_floor,
        "min_expected_recall_at_k": recall_floor,
        "trusted_target_status": trusted_target,
    }
    for case in evaluation.get("cases") or []:
        blockers: list[str] = []
        if not case.get("ok"):
            blockers.append("evaluation_failed")
        if not case.get("current_scope_hit"):
            blockers.append("current_scope_mismatch")
        if not case.get("target_scope_hit"):
            blockers.append("target_scope_mismatch")
        if effective_require_top1_expected_hit and not case.get("top1_expected_hit"):
            blockers.append("top1_expected_course_miss")
        if not case.get("expected_course_hits"):
            blockers.append("no_expected_course_hit")
        if float(case.get("precision_at_k") or 0.0) < precision_floor:
            blockers.append("precision_below_threshold")
        expected_recall = case.get("expected_recall_at_k")
        if expected_recall is None or float(expected_recall) < recall_floor:
            blockers.append("expected_recall_below_threshold")
        eligible = not blockers
        scenario_id = int(case.get("scenario_id") or 0)
        if eligible and scenario_id:
            eligible_ids.append(scenario_id)
        reviewed_cases.append(
            {
                "scenario_id": scenario_id,
                "scenario_name": case.get("scenario_name"),
                "source_review_status": case.get("review_status"),
                "eligible": eligible,
                "blockers": blockers,
                "current_scope_hit": bool(case.get("current_scope_hit")),
                "target_scope_hit": bool(case.get("target_scope_hit")),
                "top1_expected_hit": bool(case.get("top1_expected_hit")),
                "precision_at_k": case.get("precision_at_k"),
                "expected_recall_at_k": case.get("expected_recall_at_k"),
                "first_expected_rank": case.get("first_expected_rank"),
                "expected_course_hits": case.get("expected_course_hits") or [],
                "recommended_courses": case.get("recommended_courses") or [],
            }
        )
    updated_count = 0
    provenance_blockers: list[str] = []
    audit_reviewer_id = (reviewer_id or "").strip()
    audit_rationale = (rationale or "").strip()
    audit_source_packet = (source_decision_packet or "").strip()
    if apply:
        if audit_source_packet and not _is_portable_reports_packet_ref(audit_source_packet):
            provenance_blockers.append("source_decision_packet_must_be_portable_reports_ref")
        if trusted_target:
            provenance_blockers.append("trusted_status_updates_require_human_decision_import")
            if audit_reviewer_id.lower() in AUTOMATED_REVIEWER_IDS:
                provenance_blockers.append("trusted_status_requires_explicit_human_reviewer_id")
            if not audit_source_packet:
                provenance_blockers.append("trusted_status_requires_source_decision_packet")
            if not audit_rationale:
                provenance_blockers.append("trusted_status_requires_rationale")
        elif not allow_automated_status_write:
            provenance_blockers.append("automated_status_updates_require_explicit_opt_in")
    if provenance_blockers:
        return {
            "ok": False,
            "apply": apply,
            "status_update_allowed": False,
            "db_writes": False,
            "review_method": "automated_eval_gate",
            "source_review_statuses": statuses,
            "target_review_status": target_review_status,
            "scenario_limit": scenario_limit,
            "limit": limit,
            "criteria": criteria,
            "evaluated_count": len(reviewed_cases),
            "eligible_count": len(eligible_ids),
            "updated_count": 0,
            "provenance_blockers": provenance_blockers,
            "message": (
                "Review-status updates require explicit operator intent. Trusted statuses require "
                "a validated human decision import path; automated candidate statuses require "
                "a separate opt-in flag."
            ),
            "evaluation_summary": {
                key: value for key, value in evaluation.items() if key not in {"cases", "breakdown"}
            },
            "cases": reviewed_cases,
        }
    if apply:
        timestamp = now_utc()
        updated_ids: set[int] = set()
        updated_count = 0
        if eligible_ids:
            placeholders = ",".join("?" for _ in eligible_ids)
            status_placeholders = ",".join("?" for _ in statuses)
            rows_to_update = list(
                conn.execute(
                    f"""
                    SELECT scenario_id
                    FROM training_transition_gold_scenarios
                    WHERE scenario_id IN ({placeholders})
                      AND review_status IN ({status_placeholders})
                      AND COALESCE(review_status, '') <> ?
                    """,
                    (*eligible_ids, *statuses, target_review_status),
                )
            )
            update_ids = [int(row["scenario_id"]) for row in rows_to_update]
        else:
            update_ids = []
        if update_ids:
            update_placeholders = ",".join("?" for _ in update_ids)
            status_placeholders = ",".join("?" for _ in statuses)
            cursor = conn.execute(
                f"""
                UPDATE training_transition_gold_scenarios
                SET review_status = ?,
                    updated_at = ?
                WHERE scenario_id IN ({update_placeholders})
                  AND review_status IN ({status_placeholders})
                  AND COALESCE(review_status, '') <> ?
                """,
                (target_review_status, timestamp, *update_ids, *statuses, target_review_status),
            )
            updated_count = cursor.rowcount
            updated_ids = set(update_ids)
        if reviewed_cases:
            conn.executemany(
                """
                INSERT INTO training_transition_scenario_reviews(
                    scenario_id, review_method, source_review_status, target_review_status,
                    applied, eligible, status_updated, blockers_json, criteria_json,
                    metrics_json, expected_course_hits_json, recommended_courses_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        case["scenario_id"],
                        "automated_eval_gate",
                        case.get("source_review_status"),
                        target_review_status,
                        1,
                        1 if case.get("eligible") else 0,
                        1 if case.get("scenario_id") in updated_ids else 0,
                        json.dumps(case.get("blockers") or [], ensure_ascii=False),
                        json.dumps(criteria, ensure_ascii=False),
                        json.dumps(
                            {
                                "current_scope_hit": case.get("current_scope_hit"),
                                "target_scope_hit": case.get("target_scope_hit"),
                                "top1_expected_hit": case.get("top1_expected_hit"),
                                "precision_at_k": case.get("precision_at_k"),
                                "expected_recall_at_k": case.get("expected_recall_at_k"),
                                "first_expected_rank": case.get("first_expected_rank"),
                            },
                            ensure_ascii=False,
                        ),
                        json.dumps(case.get("expected_course_hits") or [], ensure_ascii=False),
                        json.dumps(case.get("recommended_courses") or [], ensure_ascii=False),
                        timestamp,
                    )
                    for case in reviewed_cases
                    if case.get("scenario_id")
                ],
            )
        changed_cases = [case for case in reviewed_cases if case.get("scenario_id") in updated_ids]
        if changed_cases:
            effective_reviewer_id = audit_reviewer_id or "automated_eval_gate"
            audit_notes = (
                notes
                or "Automated transition evaluation gate applied; source decision provenance should be confirmed separately for human-trusted use."
            )
            effective_rationale = audit_rationale or audit_notes
            evidence_refs_json = json.dumps(evidence_refs or [], ensure_ascii=False)
            conn.executemany(
                """
                INSERT INTO review_audit_log(
                    entity_type, entity_id, action, previous_status, new_status,
                    reviewer_id, notes, source_decision_packet, source_artifact_hash,
                    rationale, evidence_refs_json, created_by_tool, run_artifact, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "training_transition_gold_scenario",
                        str(case["scenario_id"]),
                        "review_training_transition_scenarios",
                        case.get("source_review_status"),
                        target_review_status,
                        effective_reviewer_id,
                        audit_notes,
                        audit_source_packet or None,
                        source_artifact_hash,
                        effective_rationale,
                        evidence_refs_json,
                        "ncs_harness.review-training-transition-scenarios",
                        run_artifact,
                        timestamp,
                    )
                    for case in changed_cases
                ],
            )
        conn.commit()
    return {
        "ok": True,
        "apply": apply,
        "status_update_allowed": bool(apply and updated_count > 0),
        "db_writes": bool(apply and (updated_count > 0 or reviewed_cases)),
        "review_method": "automated_eval_gate",
        "source_review_statuses": statuses,
        "target_review_status": target_review_status,
        "scenario_limit": scenario_limit,
        "limit": limit,
        "criteria": criteria,
        "evaluated_count": len(reviewed_cases),
        "eligible_count": len(eligible_ids),
        "updated_count": updated_count,
        "evaluation_summary": {
            key: value for key, value in evaluation.items() if key not in {"cases", "breakdown"}
        },
        "cases": reviewed_cases,
    }


def generate_training_transition_eval_scenarios(
    conn: sqlite3.Connection,
    *,
    target_non_hr_count: int = 70,
    per_major_limit: int = 8,
    per_classification_limit: int = 3,
    reset_auto: bool = False,
    apply: bool = False,
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
) -> dict[str, Any]:
    mutation_timestamp = now_utc() if apply else None
    reset_auto_deleted_rows: list[sqlite3.Row] = []
    if reset_auto and apply:
        reset_auto_deleted_rows = list(
            conn.execute(
                """
                SELECT scenario_id, review_status
                FROM training_transition_gold_scenarios
                WHERE review_status = 'candidate_auto'
                """
            )
        )
        if reset_auto_deleted_rows:
            conn.executemany(
                """
                INSERT INTO review_audit_log(
                    entity_type, entity_id, action, previous_status, new_status,
                    reviewer_id, notes, source_decision_packet, source_artifact_hash,
                    rationale, evidence_refs_json, created_by_tool, run_artifact, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "training_transition_gold_scenario",
                        str(row["scenario_id"]),
                        "generate_training_transition_eval_set_reset_auto",
                        row["review_status"],
                        None,
                        "automated_eval_set",
                        "Candidate_auto scenario removed by reset-auto before regeneration.",
                        None,
                        None,
                        "Reset-auto delete for generated transition evaluation scenarios.",
                        "[]",
                        "ncs_harness.generate-training-transition-eval-set",
                        None,
                        mutation_timestamp,
                    )
                    for row in reset_auto_deleted_rows
                ],
            )
        conn.execute("DELETE FROM training_transition_gold_scenarios WHERE review_status = 'candidate_auto'")
    rows = conn.execute(
        """
        SELECT tc.*, c.major_code, c.major_name, c.middle_code, c.small_code, c.sub_code, c.sub_name
        FROM ncs_training_courses tc
        LEFT JOIN competency_units cu ON cu.unit_code = tc.ncs_cl_cd
        LEFT JOIN classifications c ON c.classification_id = cu.classification_id
        WHERE COALESCE(tc.ncs_lclas_cd, c.major_code, '') <> '02'
        ORDER BY tc.ncs_lclas_cd, tc.ncs_cl_cd, tc.training_course_id
        """
    ).fetchall()
    max_count = min(target_non_hr_count, scenario_limit or target_non_hr_count)
    selected: list[sqlite3.Row] = []
    major_counts: dict[str, int] = {}
    classification_counts: dict[str, int] = {}
    for row in rows:
        major = row["ncs_lclas_cd"] or row["major_code"] or "unknown"
        classification = row["ncs_cl_cd"]
        if major_counts.get(major, 0) >= per_major_limit:
            continue
        if classification_counts.get(classification, 0) >= per_classification_limit:
            continue
        selected.append(row)
        major_counts[major] = major_counts.get(major, 0) + 1
        classification_counts[classification] = classification_counts.get(classification, 0) + 1
        if len(selected) >= max_count:
            break
    planned_scenarios = [
        {
            "scenario_name": f"auto_non_hr_{row['ncs_cl_cd']}_{index}",
            "current_query": "총무",
            "target_query": row["compe_unit_name"] or row["ncs_subd_cdnm"] or row["ncs_cl_cd"],
            "major_code": row["ncs_lclas_cd"] or row["major_code"],
            "expected_target_match_text": row["compe_unit_name"],
            "expected_course_names": [row["compe_unit_name"]],
            "review_status": "candidate_auto",
        }
        for index, row in enumerate(selected, start=1)
    ]
    if not apply:
        return {
            "ok": True,
            "apply": False,
            "report_only": True,
            "status_update_allowed": False,
            "db_writes": False,
            "selected_count": len(selected),
            "auto_non_hr_scenario_count": len(selected),
            "major_counts": major_counts,
            "planned_scenarios": planned_scenarios,
            "evaluation": {
                "ok": True,
                "report_only": True,
                "scenario_count": len(planned_scenarios),
                "review_status_filter": ["candidate_auto"],
                "cases": [],
                "message": "Dry run only; no candidate_auto rows were inserted or updated.",
            },
        }
    timestamp = mutation_timestamp or now_utc()
    for index, row in enumerate(selected, start=1):
        scenario_name = f"auto_non_hr_{row['ncs_cl_cd']}_{index}"
        conn.execute(
            """
            INSERT INTO training_transition_gold_scenarios(
                scenario_name, current_query, target_query, major_code,
                expected_current_match_text, expected_target_match_text,
                expected_course_names_json, review_status, created_at, updated_at
            ) VALUES (?, '총무', ?, ?, NULL, ?, ?, 'candidate_auto', ?, ?)
            ON CONFLICT(scenario_name) DO UPDATE SET
                current_query = excluded.current_query,
                target_query = excluded.target_query,
                major_code = excluded.major_code,
                expected_target_match_text = excluded.expected_target_match_text,
                expected_course_names_json = excluded.expected_course_names_json,
                review_status = excluded.review_status,
                updated_at = excluded.updated_at
            """,
            (
                scenario_name,
                row["compe_unit_name"] or row["ncs_subd_cdnm"] or row["ncs_cl_cd"],
                row["ncs_lclas_cd"] or row["major_code"],
                row["compe_unit_name"],
                _json([row["compe_unit_name"]]),
                timestamp,
                timestamp,
            ),
        )
    conn.commit()
    evaluation = evaluate_training_transition_scenarios(
        conn,
        limit=limit,
        scenario_limit=scenario_limit,
        review_statuses=["candidate_auto"],
    )
    return {
        "ok": True,
        "apply": True,
        "report_only": False,
        "status_update_allowed": bool(selected or reset_auto_deleted_rows),
        "db_writes": bool(selected or reset_auto_deleted_rows),
        "reset_auto_deleted_count": len(reset_auto_deleted_rows),
        "reset_auto_audit_log_count": len(reset_auto_deleted_rows),
        "selected_count": len(selected),
        "auto_non_hr_scenario_count": len(selected),
        "major_counts": major_counts,
        "planned_scenarios": planned_scenarios,
        "evaluation": evaluation,
    }
