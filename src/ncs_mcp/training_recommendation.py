from __future__ import annotations

import json
import math
import re
import sqlite3
from typing import Any

from ncs_mcp.career_path import career_paths_for_units
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
from ncs_mcp.job_base_api import job_base_profile_for_units
from ncs_mcp.qualification_api import qualification_profile_for_units


TRUSTED_TRANSITION_REVIEW_STATUSES = ("human_reviewed", "reviewed", "accepted")
TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K = 0.98
REJECTED_REVIEW_STATUSES = {"rejected"}
USABLE_REVIEW_STATUS_WEIGHTS = {
    "human_reviewed": 1.2,
    "reviewed": 1.1,
    "accepted": 1.1,
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


def _split_training_methods(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    methods = []
    for token in ("원격훈련", "집체훈련", "현장견학", "현장실습", "Practice", "Classroom"):
        if token.lower() in text.lower():
            methods.append(token)
    return methods or [text]


def _significant_tokens(value: Any) -> list[str]:
    text = _clean(value)
    tokens = re.findall(r"[0-9A-Za-z가-힣]{2,}", text)
    generic = {"관리", "계획", "기술", "능력", "방법", "분석", "수립", "업무", "운영"}
    return [token for token in tokens if len(token) >= 3 and token not in generic]


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


def _task_concepts(conn: sqlite3.Connection, criteria_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    row = conn.execute(
        """
        SELECT ce.unit_code
        FROM performance_criteria pc
        JOIN competency_elements ce ON ce.element_id = pc.element_id
        WHERE pc.criteria_id = ?
        """,
        (criteria_id,),
    ).fetchone()
    return _concepts_for_units(
        conn,
        {row["unit_code"]} if row else set(),
        criteria_id=criteria_id,
        limit=limit,
    )


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


def _relation_rows(conn: sqlite3.Connection, concept_ids: set[int], *, limit: int = 50) -> list[dict[str, Any]]:
    if not concept_ids:
        return []
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
            and float(candidate.get("score") or 0.0) >= 0.75
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
    row = conn.execute(
        """
        SELECT *
        FROM ncs_query_aliases
        WHERE (normalized_query = ? OR alias_text = ?)
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
        (text, text),
    ).fetchone()
    if not row:
        return query, major_code, middle_code, small_code, sub_code, None
    alias = dict(row)
    return (
        alias.get("normalized_query") or query,
        major_code or alias.get("major_code"),
        middle_code or alias.get("middle_code"),
        small_code or alias.get("small_code"),
        sub_code or alias.get("sub_code"),
        alias,
    )


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
    max_rows = clamp_limit(limit, default=10, maximum=50)
    if not text:
        return {"ok": False, "query": query, "normalized_query": "", "candidates": []}
    query_alias = None
    alias_unit_code = None
    aliased_query, major_code, middle_code, small_code, sub_code, query_alias = _apply_query_alias(
        conn,
        query,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    if query_alias:
        text = _clean(aliased_query) or text
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
        if alias_unit_code and rowd.get("unit_code") == alias_unit_code:
            score = max(score, float(query_alias.get("confidence_score") or 0.0), 0.9)
            match_level = "query_alias_unit"
        if score <= 0:
            continue
        candidates.append(
            {
                "candidate_type": "unit",
                "match_level": match_level,
                "matched_text": rowd.get("unit_name_raw"),
                "unit_name": rowd.get("unit_name_raw"),
                "unit_code": rowd.get("unit_code"),
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
    candidates.sort(
        key=lambda item: (
            -float(item.get("confidence_score") or 0.0),
            order.get(str(item.get("candidate_type")), 9),
            0 if str(item.get("matched_text") or "").lower().startswith(text.lower()) else 1,
            str(item.get("matched_text") or ""),
        )
    )
    return {
        "ok": bool(candidates),
        "query": query,
        "effective_query": text,
        "query_alias": query_alias,
        "normalized_query": normalize_concept_key(text),
        "candidates": candidates[:max_rows],
    }


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
    class_exact = text and source.get("sub_name") == text
    unit_codes: list[str]
    match_level = "source_unit"
    match_text = source.get("unit_name_raw")
    if class_exact:
        rows = conn.execute(
            """
            SELECT cu.unit_code
            FROM competency_units cu
            JOIN classifications c ON c.classification_id = cu.classification_id
            WHERE c.major_code = ? AND c.middle_code = ? AND c.small_code = ? AND c.sub_code = ?
            ORDER BY cu.unit_code
            """,
            (source.get("major_code"), source.get("middle_code"), source.get("small_code"), source.get("sub_code")),
        ).fetchall()
        unit_codes = [row["unit_code"] for row in rows] or [source["unit_code"]]
        match_level = "sub_classification"
        match_text = source.get("sub_name")
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


def _course_payload(conn: sqlite3.Connection, course: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    row = _row_dict(course)
    cid = row["training_course_id"]
    unit_links = rows_to_dicts(
        conn.execute(
            "SELECT * FROM ncs_training_course_unit_links WHERE training_course_id = ? ORDER BY link_id",
            (cid,),
        ).fetchall()
    )
    concept_links = rows_to_dicts(
        conn.execute(
            "SELECT * FROM ncs_training_course_concept_links WHERE training_course_id = ? ORDER BY link_id",
            (cid,),
        ).fetchall()
    )
    element_links = rows_to_dicts(
        conn.execute(
            """
            SELECT l.*, ce.element_name_raw
            FROM ncs_training_course_element_links l
            JOIN competency_elements ce ON ce.element_id = l.element_id
            WHERE l.training_course_id = ?
            ORDER BY l.link_id
            """,
            (cid,),
        ).fetchall()
    )
    goal_links = rows_to_dicts(
        conn.execute(
            """
            SELECT l.*, oc.concept_name, oc.concept_type
            FROM training_goal_concept_links l
            JOIN ontology_concepts oc ON oc.concept_id = l.concept_id
            WHERE l.training_course_id = ?
            ORDER BY l.link_id
            """,
            (cid,),
        ).fetchall()
    )
    delivery = rows_to_dicts(
        conn.execute(
            "SELECT * FROM training_delivery_relations WHERE training_course_id = ? ORDER BY relation_id",
            (cid,),
        ).fetchall()
    )
    return {
        "training_course": row,
        "unit_links": unit_links,
        "concept_links": concept_links,
        "element_links": element_links,
        "goal_concept_links": goal_links,
        "delivery_relations": delivery,
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


def get_training_course(conn: sqlite3.Connection, training_course_id: int) -> dict[str, Any]:
    row = conn.execute(
        "SELECT * FROM ncs_training_courses WHERE training_course_id = ?",
        (training_course_id,),
    ).fetchone()
    if not row:
        return not_found_response(f"훈련과정을 찾을 수 없습니다: {training_course_id}")
    return {"ok": True, **_course_payload(conn, row)}


def build_training_course_ontology_links(conn: sqlite3.Connection) -> dict[str, Any]:
    before_links = int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_concept_links").fetchone()[0])
    before_elements = int(conn.execute("SELECT COUNT(*) FROM ncs_training_course_element_links").fetchone()[0])
    before_goals = int(conn.execute("SELECT COUNT(*) FROM training_goal_concept_links").fetchone()[0])
    before_delivery = int(conn.execute("SELECT COUNT(*) FROM training_delivery_relations").fetchone()[0])
    timestamp = now_utc()
    courses = conn.execute("SELECT * FROM ncs_training_courses ORDER BY training_course_id").fetchall()
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
                ) VALUES (?, ?, 'ncs_cl_cd_exact', 1.0, 'reviewed', ?, ?)
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
    rows = conn.execute(
        f"""
        SELECT tc.*
        FROM ncs_training_courses tc
        {where}
        ORDER BY tc.training_course_id
        LIMIT ?
        """,
        (*params, max_rows),
    ).fetchall()
    return [_course_payload(conn, row) for row in rows]


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
    match = item.get("match") or {}
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
    if gap_job_base_hits:
        components["job_base_score"] += min(0.045, 0.015 + 0.004 * len(gap_job_base_hits))
        reasons.append("gap_job_base_bridge")
    elif target_job_base_hits:
        components["job_base_score"] += min(0.02, 0.004 * len(target_job_base_hits))
        reasons.append("target_job_base_signal")
    if not direct_unit_evidence and not goal_hits:
        return 0.0, {"reasons": reasons, "score_components": components}
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
    query, major_code, middle_code, small_code, sub_code, alias = _apply_query_alias(
        conn,
        query,
        major_code=major_code,
        middle_code=middle_code,
        small_code=small_code,
        sub_code=sub_code,
    )
    alias_unit_code = _clean(alias.get("unit_code")) if alias else None
    if alias and not unit_code:
        unit_code = alias_unit_code or unit_code
    source = resolve_task_criteria(
        conn,
        criteria_id=criteria_id,
        query=None if alias_unit_code else query,
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
    career_path_unit_codes = {_clean(item.get("matched_unit_code")) for item in scope_career_paths if _clean(item.get("matched_unit_code"))}
    scope_qualification_profile = qualification_profile_for_units(conn, scope_unit_codes, limit=500)
    scope_job_base_profile = job_base_profile_for_units(conn, scope_unit_codes, limit=500)
    effective_target_qualification_keys = target_qualification_keys or {_qualification_key(item) for item in scope_qualification_profile if _qualification_key(item)}
    effective_gap_qualification_keys = gap_qualification_keys or set()
    effective_target_job_base_keys = target_job_base_keys or {_job_base_key(item) for item in scope_job_base_profile if _job_base_key(item)}
    effective_gap_job_base_keys = gap_job_base_keys or set()
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
    candidates: list[dict[str, Any]] = []
    for row in course_rows:
        cid = int(row["training_course_id"])
        payload = course_payloads[cid]
        linked_unit_codes = course_unit_codes_by_id[cid]
        qualification_links = _profiles_for_unit_codes(qualification_profiles_by_unit, linked_unit_codes, limit=100)
        job_base_links = _profiles_for_unit_codes(job_base_profiles_by_unit, linked_unit_codes, limit=100)
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
        if extra_support_course_weights and row["compe_unit_name"] in extra_support_course_weights:
            score = min(1.0, score + float(extra_support_course_weights[row["compe_unit_name"]]))
            match.setdefault("reasons", []).append("target_support_course_hint")
            match["support_course_hint_weight"] = extra_support_course_weights[row["compe_unit_name"]]
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
                item for item in scope_career_paths if item.get("matched_unit_code") in {row["ncs_cl_cd"], *[link.get("unit_code") for link in payload["unit_links"]]}
            ][:20],
            "qualification_evidence": candidate["qualification_links"],
            "job_base_evidence": candidate["job_base_links"],
            "supplemental_evidence": supplemental_evidence if supplemental_evidence.get("data_sources") else {},
            "match": match,
            "score_components": match.get("score_components", {}),
            "score_component_highlights": _score_component_highlights(match.get("score_components", {})),
            "confidence_score": round(score, 3),
            "confidence_grade": _confidence_grade(score),
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
                        item for item in scope_career_paths if item.get("matched_unit_code") == row["ncs_cl_cd"]
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


def _label_for_tier(tier: str | None) -> str:
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


def _compact_course_card(raw: dict[str, Any], *, transition: bool = False) -> dict[str, Any]:
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
    card = {
        "rank": item.get("rank"),
        "course_name": course.get("compe_unit_name"),
        "training_course_id": course.get("training_course_id"),
        "confidence_score": item.get("confidence_score"),
        "confidence_grade": item.get("confidence_grade"),
        "tier": tier,
        "tier_label": _label_for_tier(tier),
        "rationale": rationale,
        "evidence_strength_summary": _strength_summary(item.get("evidence_strength")),
        "coverage_counts": coverage_counts,
        "coverage_breakdown": _coverage_breakdown(match),
        "coverage_summary": coverage_summary,
        "evidence_highlights": highlights,
        "fit_summary": _fit_summary(item.get("preference_fit")),
        "delivery": item.get("delivery") or item.get("delivery_evidence") or {},
        "score_component_highlights": item.get("score_component_highlights") or [],
        "why_recommended": why,
    }
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


def _course_delivery_brief(card: dict[str, Any]) -> dict[str, Any]:
    delivery = card.get("delivery") if isinstance(card.get("delivery"), dict) else {}
    relations = delivery.get("relations") or []
    profile = delivery.get("profile") if isinstance(delivery.get("profile"), dict) else {}
    level = None
    hours = None
    for relation in relations:
        if relation.get("relation_type") == "has_level" and level is None:
            level = relation.get("numeric_value") or relation.get("relation_value")
        if relation.get("relation_type") == "requires_time" and hours is None:
            hours = relation.get("numeric_value") or relation.get("relation_value")
    return {
        "level": level,
        "hours": hours,
        "methods": profile.get("methods") or [],
    }


def _compact_alias_interpretation(alias: Any) -> dict[str, Any] | None:
    if not isinstance(alias, dict):
        return None
    return {
        "alias_text": alias.get("alias_text"),
        "normalized_query": alias.get("normalized_query"),
        "unit_code": alias.get("unit_code"),
        "confidence_score": alias.get("confidence_score"),
        "review_status": alias.get("review_status"),
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
    transferability = float(summary.get("transferability_ratio") or 0.0)
    gap_count = int(summary.get("gap_ksa_concept_count") or 0)
    transferable_count = int(summary.get("transferable_ksa_concept_count") or 0)
    target_count = int(summary.get("target_ksa_concept_count") or 0)
    if transferability >= 0.35:
        assessment = "전이 가능한 KSA가 꽤 있어 보완 교육 중심으로 접근할 수 있습니다."
    elif transferability >= 0.1:
        assessment = "일부 KSA는 전이되지만 목표 직무 KSA 보완이 필요합니다."
    else:
        assessment = "공통 KSA가 적어 목표 직무 기초부터 체계적으로 보완하는 편이 안전합니다."

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
    if not groups.get("primary"):
        caveats.append("우선 추천 과정이 없어 결과를 참고 과정 중심으로만 해석해야 합니다.")

    headline = f"{current_query}에서 {target_query}로 전환하려면 {target_label} 관련 교육을 우선 검토하세요."
    if top_cards:
        headline = f"{current_query}에서 {target_query}로 전환하려면 '{top_cards[0].get('course_name')}' 과정을 먼저 검토하세요."

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
            },
            "target": {
                "requested": target_query,
                "resolved_as": target_label,
                "match_level": target_scope.get("match_level"),
                "unit_count": summary.get("target_scope_unit_count"),
                "task_element": target_task.get("element_name"),
                "query_alias": _compact_alias_interpretation(target_alias),
            },
        },
        "transition_assessment": {
            "summary": assessment,
            "transferability_ratio": transferability,
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
    cards = [_compact_course_card(item) for item in items[:max_items]]
    scope = result.get("resolved_scope") or {}
    summary = result.get("recommendation_summary") or {}
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
    cards = [_compact_course_card(item, transition=True) for item in items[:max_items]]
    transition = result.get("transition") or {}
    summary = transition.get("summary") or {}
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
        },
        "scope_interpretation": {
            "current": transition.get("current_scope") or {},
            "target": transition.get("target_scope") or {},
            "transferability_ratio": summary.get("transferability_ratio"),
        },
        "transition_summary": summary,
        "source_recommendation_counts": {key: len(value) for key, value in groups.items()},
        "recommended_courses": cards,
        "input_quality": _input_quality_for_transition(result, groups),
        "audit": _compact_audit(result),
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
    limit: int = DEFAULT_RECOMMENDATIONS,
    save: bool = True,
) -> dict[str, Any]:
    if not _clean(current_query) or not _clean(target_query):
        return {"ok": False, "error": {"code": "missing_transition_query"}}
    requested_current_query = current_query
    requested_target_query = target_query
    effective_current_major = current_major_code or major_code
    effective_target_major = target_major_code or major_code
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
    current_resolution = resolve_ncs_query_scope(
        conn,
        current_query or requested_current_query,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
        limit=12,
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
    current_source = resolve_task_criteria(
        conn,
        query=None if current_unit_code else current_query,
        unit_code=current_unit_code,
        major_code=effective_current_major,
        middle_code=current_middle_code,
        small_code=current_small_code,
        sub_code=current_sub_code,
    )
    target_source = resolve_task_criteria(
        conn,
        query=None if target_unit_code else target_query,
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
        "preferred_max_hours": preferred_max_hours,
        "preferred_methods": preferred_methods or [],
        "current_career_path_count": len(current_career_paths),
        "target_career_path_count": len(target_career_paths),
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
        f"목표 KSA {len(target_ids)}개 중 현재 scope와 공통인 KSA는 {len(transferable_ids)}개, 보완 KSA는 {len(gap_ids)}개입니다.",
    ]
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
        "current_career_path": current_career_paths[:80],
        "target_career_path": target_career_paths[:80],
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


def evaluate_training_transition_scenarios(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
    review_statuses: list[str] | None = None,
) -> dict[str, Any]:
    max_items = clamp_limit(limit, default=DEFAULT_RECOMMENDATIONS, maximum=MAX_RECOMMENDATIONS)
    params: list[Any] = []
    status_clause = "review_status != 'rejected'"
    review_status_filter: list[str] = ["not_rejected"]
    if review_statuses:
        placeholders = ",".join("?" for _ in review_statuses)
        status_clause = f"review_status IN ({placeholders})"
        params.extend(review_statuses)
        review_status_filter = list(review_statuses)
    sql = f"""
        SELECT *
        FROM training_transition_gold_scenarios
        WHERE {status_clause}
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
        recommended_courses = [
            item["training_course"]["compe_unit_name"]
            for item in result.get("recommendations", [])[:max_items]
        ]
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
        "top1_expected_hit_rate": round(top1_expected_hits / ranked_case_count, 4) if ranked_case_count else 0.0,
        "mrr_at_k": round(reciprocal_rank_sum / ranked_case_count, 4) if ranked_case_count else 0.0,
        "map_at_k": round(average_precision_sum / ranked_case_count, 4) if ranked_case_count else None,
        "ndcg_at_k": round(ndcg_sum / ranked_case_count, 4) if ranked_case_count else None,
        "breakdown": breakdown_summary,
        "cases": cases,
    }


def review_training_transition_scenarios(
    conn: sqlite3.Connection,
    *,
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
    source_review_statuses: list[str] | None = None,
    target_review_status: str = "reviewed",
    require_top1_expected_hit: bool = True,
    min_precision_at_k: float = 0.0,
    min_expected_recall_at_k: float = TRANSITION_REVIEW_MIN_EXPECTED_RECALL_AT_K,
    apply: bool = False,
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
    if apply:
        timestamp = now_utc()
        updated_ids: set[int] = set()
        if eligible_ids:
            placeholders = ",".join("?" for _ in eligible_ids)
            status_placeholders = ",".join("?" for _ in statuses)
            cursor = conn.execute(
                f"""
                UPDATE training_transition_gold_scenarios
                SET review_status = ?,
                    updated_at = ?
                WHERE scenario_id IN ({placeholders})
                  AND review_status IN ({status_placeholders})
                """,
                (target_review_status, timestamp, *eligible_ids, *statuses),
            )
            updated_count = cursor.rowcount
            updated_ids = set(eligible_ids)
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
        conn.commit()
    return {
        "ok": True,
        "apply": apply,
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
    limit: int = DEFAULT_RECOMMENDATIONS,
    scenario_limit: int | None = None,
) -> dict[str, Any]:
    if reset_auto:
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
    timestamp = now_utc()
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
                row["compe_