from __future__ import annotations

import math
import re
import unicodedata
from typing import Any


_OPEN_DB_FACTORY: Any = None
_CLAMP_LIMIT: Any = None
_UNIT_PATH: Any = None
_TIER_PREDICATES: Any = None
_TIER_EXECUTOR: Any = None
_TOKEN_EXPANDER: Any = None


def configure_search_runtime(
    *,
    open_db_factory: Any,
    clamp_limit: Any,
    unit_path: Any,
    tier_predicates: Any = None,
    tier_executor: Any = None,
    token_expander: Any = None,
) -> None:
    """Inject server-owned runtime helpers without importing the server module."""
    global _OPEN_DB_FACTORY, _CLAMP_LIMIT, _UNIT_PATH
    global _TIER_PREDICATES, _TIER_EXECUTOR, _TOKEN_EXPANDER
    _OPEN_DB_FACTORY = open_db_factory
    _CLAMP_LIMIT = clamp_limit
    _UNIT_PATH = unit_path
    _TIER_PREDICATES = tier_predicates
    _TIER_EXECUTOR = tier_executor
    _TOKEN_EXPANDER = token_expander


def _required_runtime_helper(name: str, helper: Any) -> Any:
    if helper is None:
        raise RuntimeError(f"NCS search runtime helper is not configured: {name}")
    return helper


def _active_tier_predicates() -> Any:
    return _TIER_PREDICATES or _ncs_search_tier_predicates


def _active_tier_executor() -> Any:
    return _TIER_EXECUTOR or _execute_ncs_search_tiers


def _active_token_expander() -> Any:
    return _TOKEN_EXPANDER or _validated_ncs_search_token_expansions


def _ncs_search_markdown(
    query: str,
    results: list[dict[str, Any]],
    *,
    counts_by_type: dict[str, int],
    offset: int,
    next_offset: int | None,
) -> str:
    lines = [f"## NCS 검색 결과: {query}"]
    lines.append(f"- 반환 {len(results)}건 중 최대 5건 미리보기")
    type_summary = ", ".join(
        f"{item_type} {count}건"
        for item_type, count in counts_by_type.items()
        if count > 0
    )
    if type_summary:
        lines.append(f"- 유형별 반환: {type_summary}")
    lines.append(f"- 현재 페이지: `offset={offset}`")
    if next_offset is not None:
        lines.append(
            f"- 다음 페이지: 같은 질의와 범위에 `offset={next_offset}`을 지정하세요."
        )
    for index, item in enumerate(results[:5], start=1):
        item_type = str(item.get("type") or "result")
        item_id = str(item.get("id") or "")
        text = str(item.get("text") or "").strip()
        lines.append(f"{index}. **{text}** (`{item_type}` · `{item_id}`)")
    return "\n".join(lines)


_NCS_SEARCH_TYPES = ("unit", "element", "criteria", "ksa")
_NCS_SEARCH_MATCH_MODES = {
    -1: "intent_alias",
    0: "phrase",
    1: "token_and",
    2: "expanded_token_and",
    3: "token_or",
}
_NCS_SEARCH_LOW_INFORMATION_SUFFIXES = (
    "관리",
    "운영",
    "업무",
    "직무",
    "실무",
)
# These terms are either frequent workflow nouns/verbs in NCS definitions or
# actor/context words that carry little domain-specific intent by themselves.
# They still contribute to fallback ranking, but cannot be the sole fallback hit.
_NCS_SEARCH_GENERIC_TOKENS = frozenset(
    {
        "관리",
        "제도",
        "설계",
        "계획",
        "수립",
        "운영",
        "업무",
        "직원",
        "담당",
    }
)
_NCS_SEARCH_GENERIC_TOKEN_FACTOR = 0.3
# Fallback scoring weighs each token by how few unit names contain it.  A hand
# kept generic list only covers the words someone thought of: 퇴직 names 2 units
# and 처리 names 195, but both scored 1.0, so a lone 처리 hit tied with a lone
# 퇴직 hit and the shorter name won the length tiebreak -- which is how
# 퇴직 정산 서류 처리 returned 심냉처리 and 퀜칭열처리.  Document frequency is
# measured over unit names, the highest weighted field, and normalized to
# (0, 1] so score magnitudes stay in the range the tiers already assume.
_NCS_SEARCH_IDF_FLOOR = 0.05
# Public-search recall equivalences bridge practitioner language to official NCS
# names.  They are candidate-only expansions, not source evidence or DB writes.
_NCS_SEARCH_QUERY_EQUIVALENTS = {
    "성과평가": ("인사평가",),
}
# High-specificity practitioner phrases whose official NCS unit terminology is
# materially different.  Keep these as candidate-only retrieval hints: they do
# not alter source data, review status, or ontology evidence.
_NCS_SEARCH_QUERY_INTENT_EQUIVALENTS = {
    "연봉 협상": ("임금관리",),
    "퇴직금 정산": ("퇴직업무지원", "급여지급"),
    "온보딩": ("인력채용", "교육훈련운영"),
    "승진 심사": ("인력이동관리",),
    "직원 고충": ("노사갈등 해결",),
    "노사관계 성과 평가": ("노사관계 평가",),
    "노사 교육": ("노사관계 교육훈련",),
    "사내 행사": ("행사지원관리",),
    "사무용품": ("비품관리",),
    "법인 차량": ("차량운영관리",),
    "사내 복지": ("복리후생지원",),
    "사옥 보안": ("총무보안관리",),
    "재무제표 작성": ("재무제표작성",),
    "원천세": ("원천징수",),
    "부가세": ("부가가치세 신고",),
}
_NCS_SEARCH_QUERY_INTENT_BLOCKERS = {
    "연봉 협상": ("선수", "스포츠", "프로야구", "프로축구", "구단"),
}


def _normalize_ncs_search_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if re.fullmatch(r"[A-Za-z0-9]+_[A-Za-z0-9]+", text):
        return text
    normalized = [
        " " if character.isspace() or unicodedata.category(character).startswith("P") else character
        for character in text
    ]
    return re.sub(r"\s+", " ", "".join(normalized)).strip()


def _normalize_ncs_search_query(query: str) -> tuple[str, list[str], list[str]]:
    normalized = _normalize_ncs_search_text(query)
    query_tokens = normalized.split()[:4]
    phrase = " ".join(query_tokens)
    fallback_tokens = [token for token in query_tokens if len(token) > 1]
    return phrase, query_tokens, fallback_tokens


def _ncs_search_intent_expansions(phrase: str) -> list[str]:
    """Return deduplicated official terms for strong practitioner-language hints."""
    normalized_phrase = phrase.casefold()
    expansions: list[str] = []
    for trigger, alternatives in _NCS_SEARCH_QUERY_INTENT_EQUIVALENTS.items():
        if trigger.casefold() not in normalized_phrase:
            continue
        blockers = _NCS_SEARCH_QUERY_INTENT_BLOCKERS.get(trigger, ())
        if any(blocker.casefold() in normalized_phrase for blocker in blockers):
            continue
        for alternative in alternatives:
            if alternative not in expansions:
                expansions.append(alternative)
    return expansions


def _escape_ncs_search_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ncs_search_like_any(columns: tuple[str, ...], parameter: str) -> str:
    return "(" + " OR ".join(
        f"COALESCE({column}, '') LIKE :{parameter} ESCAPE '\\'"
        for column in columns
    ) + ")"


def _candidate_ncs_search_expansion_bases(token: str) -> list[str]:
    """Return conservative compound bases that still require alias validation."""
    candidates: list[str] = []
    for suffix in _NCS_SEARCH_LOW_INFORMATION_SUFFIXES:
        if not token.endswith(suffix):
            continue
        base = token[: -len(suffix)].strip()
        if len(base) >= 2 and base not in candidates:
            candidates.append(base)
    return candidates


def _validated_ncs_search_token_expansions(
    conn: Any,
    fallback_tokens: list[str],
) -> dict[str, list[str]]:
    """Return code-reviewed and alias-validated recall-only expansions.

    Query aliases are already part of public-search recall.  Both expansion
    sources remain recall-only: this helper never changes review state or treats
    an expansion as source evidence.
    """
    expansions: dict[str, list[str]] = {}
    for token in fallback_tokens:
        alternatives = list(
            _NCS_SEARCH_QUERY_EQUIVALENTS.get(token.casefold(), ())
        )
        if alternatives:
            expansions[token] = alternatives

    candidate_bases_by_token = {
        token: _candidate_ncs_search_expansion_bases(token)
        for token in fallback_tokens
    }
    candidate_bases = sorted(
        {
            base
            for bases in candidate_bases_by_token.values()
            for base in bases
        }
    )
    if not candidate_bases:
        return expansions

    parameters = {
        f"expansion_base_{index}": base
        for index, base in enumerate(candidate_bases)
    }
    placeholders = ", ".join(f":{name}" for name in parameters)
    rows = conn.execute(
        f"""
        SELECT alias_text, normalized_query
        FROM ncs_query_aliases
        WHERE alias_text COLLATE NOCASE IN ({placeholders})
           OR normalized_query COLLATE NOCASE IN ({placeholders})
        """,
        parameters,
    ).fetchall()
    aliases_by_term: dict[str, list[str]] = {}
    for row in rows:
        alias_text = _normalize_ncs_search_text(row["alias_text"])
        normalized_query = _normalize_ncs_search_text(row["normalized_query"])
        values = [value for value in (alias_text, normalized_query) if len(value) >= 2]
        for value in values:
            aliases_by_term.setdefault(value.casefold(), [])
            for candidate in values:
                if candidate not in aliases_by_term[value.casefold()]:
                    aliases_by_term[value.casefold()].append(candidate)

    for token, bases in candidate_bases_by_token.items():
        alternatives = expansions.setdefault(token, [])
        for base in bases:
            linked_terms = aliases_by_term.get(base.casefold())
            if not linked_terms:
                continue
            for alternative in (base, *linked_terms):
                if alternative != token and alternative not in alternatives:
                    alternatives.append(alternative)
        if not alternatives:
            expansions.pop(token, None)
    return expansions


def _ncs_search_token_idf_weights(
    conn: Any,
    fallback_tokens: list[str],
) -> dict[str, float]:
    """Weight each fallback token by how few unit names contain it."""
    if not fallback_tokens:
        return {}
    total = conn.execute("SELECT COUNT(*) FROM competency_units").fetchone()[0]
    total = int(total or 0)
    if total <= 1:
        return {}
    ceiling = math.log(total)
    weights: dict[str, float] = {}
    for token in fallback_tokens:
        if token in weights:
            continue
        row = conn.execute(
            "SELECT COUNT(*) FROM competency_units "
            "WHERE unit_name_raw LIKE :token ESCAPE '\\'",
            {"token": f"%{_escape_ncs_search_like(token)}%"},
        ).fetchone()
        frequency = max(int(row[0] or 0), 1)
        weights[token] = max(
            _NCS_SEARCH_IDF_FLOOR,
            math.log(total / frequency) / ceiling,
        )
    return weights


def _ncs_search_fallback_ranking(
    weighted_columns: tuple[tuple[str, float], ...],
    fallback_tokens: list[str],
    parameter_groups: list[list[str]],
    search_groups: list[str],
    token_weights: dict[str, float] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Build a parameter-bound score and a non-generic-hit predicate.

    Column expressions and placeholder names come only from fixed server-side
    tuples and integer indexes.  Query text and weights remain bound parameters.
    """
    score_terms: list[str] = []
    meaningful_groups: list[str] = []
    rank_params: dict[str, Any] = {}
    weights = token_weights or {}
    for token_index, token in enumerate(fallback_tokens):
        is_generic = token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
        if not is_generic:
            meaningful_groups.append(search_groups[token_index])
        # Document frequency subsumes the hand kept list for scoring; the list
        # still decides which tokens may be a sole fallback hit.
        token_factor = weights.get(
            token,
            _NCS_SEARCH_GENERIC_TOKEN_FACTOR if is_generic else 1.0,
        )
        for column_index, (column, field_weight) in enumerate(weighted_columns):
            field_matches = "(" + " OR ".join(
                _ncs_search_like_any((column,), parameter)
                for parameter in parameter_groups[token_index]
            ) + ")"
            weight_parameter = f"rank_weight_{token_index}_{column_index}"
            rank_params[weight_parameter] = field_weight * token_factor
            score_terms.append(
                f"CASE WHEN {field_matches} "
                f"THEN :{weight_parameter} ELSE 0 END"
            )
    score_clause = " + ".join(score_terms) or "0"
    meaningful_clause = (
        "(" + " OR ".join(meaningful_groups) + ")"
        if meaningful_groups
        else "0 = 1"
    )
    return score_clause, meaningful_clause, rank_params


def _ncs_search_tier_predicates(
    columns: tuple[str, ...],
    phrase: str,
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None = None,
    weighted_columns: tuple[tuple[str, float], ...] | None = None,
    token_weights: dict[str, float] | None = None,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_ncs_search_like(phrase)}%",
    }
    phrase_clause = _ncs_search_like_any(columns, "phrase_pattern")
    token_clauses: list[str] = []
    parameter_groups: list[list[str]] = []
    for index, token in enumerate(fallback_tokens):
        parameter = f"token_{index}"
        params[parameter] = f"%{_escape_ncs_search_like(token)}%"
        token_clauses.append(_ncs_search_like_any(columns, parameter))
        parameter_groups.append([parameter])
    if not token_clauses:
        return [(0, phrase_clause, params, "", "")]
    token_and = "(" + " AND ".join(token_clauses) + ")"
    expansion_map = token_expansions or {}
    has_expansions = any(
        expansion_map.get(token) for token in fallback_tokens
    )
    if has_expansions:
        for token_index, token in enumerate(fallback_tokens):
            for alternative_index, alternative in enumerate(
                expansion_map.get(token, []),
                start=1,
            ):
                parameter = f"expanded_{token_index}_{alternative_index}"
                params[parameter] = f"%{_escape_ncs_search_like(alternative)}%"
                parameter_groups[token_index].append(parameter)
    search_groups = [
        "(" + " OR ".join(
            _ncs_search_like_any(columns, parameter)
            for parameter in group
        ) + ")"
        for group in parameter_groups
    ]
    token_or = "(" + " OR ".join(search_groups) + ")"
    score_clause, meaningful_clause, rank_params = _ncs_search_fallback_ranking(
        weighted_columns or tuple((column, 1.0) for column in columns),
        fallback_tokens,
        parameter_groups,
        search_groups,
        token_weights,
    )
    params.update(rank_params)
    tiers = [
        (0, phrase_clause, dict(params), "", ""),
        (1, token_and, dict(params), "", ""),
    ]
    if has_expansions:
        tiers.append(
            (
                2,
                "(" + " AND ".join(search_groups) + ")",
                dict(params),
                score_clause,
                meaningful_clause if meaningful_clause == "0 = 1" else "",
            )
        )
    # After generic-only rows are excluded, the OR candidate set is exactly the
    # union of non-generic token groups.  Use that equivalent predicate directly
    # so SQLite does not repeat every generic LIKE check in WHERE.
    token_or_candidates = (
        meaningful_clause if meaningful_clause != "0 = 1" else token_or
    )
    tiers.append(
        (
            3,
            token_or_candidates,
            dict(params),
            score_clause,
            meaningful_clause if meaningful_clause == "0 = 1" else "",
        )
    )
    return tiers


def _prepend_ncs_search_intent_tier(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    *,
    columns: tuple[str, ...],
    weighted_columns: tuple[tuple[str, float], ...],
    phrase: str,
    intent_expansions: list[str],
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    """Prepend a scored unit-only tier for high-confidence official terms."""
    if not intent_expansions:
        return tiers
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_ncs_search_like(phrase)}%",
    }
    parameter_groups: list[list[str]] = []
    search_groups: list[str] = []
    for index, alternative in enumerate(intent_expansions):
        parameter = f"intent_{index}"
        params[parameter] = f"%{_escape_ncs_search_like(alternative)}%"
        parameter_groups.append([parameter])
        search_groups.append(_ncs_search_like_any(columns, parameter))
    score_clause, _, rank_params = _ncs_search_fallback_ranking(
        weighted_columns,
        intent_expansions,
        parameter_groups,
        search_groups,
    )
    params.update(rank_params)
    intent_tier = (
        -1,
        "(" + " OR ".join(search_groups) + ")",
        params,
        score_clause,
        "",
    )
    return [intent_tier, *tiers]


def _execute_ncs_search_tiers(
    conn: Any,
    sql_template: str,
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    base_params: dict[str, Any],
) -> list[Any]:
    """Run a weaker search tier only when the stronger tier has no matches."""
    for match_tier, where_clause, tier_params, score_clause, meaningful_clause in tiers:
        params = dict(tier_params)
        params.update(base_params)
        params["match_tier"] = match_tier
        rows = conn.execute(
            sql_template.format(
                where_clause=where_clause,
                fallback_filter_clause=(
                    f" AND ({meaningful_clause})" if meaningful_clause else ""
                ),
                fallback_order_clause=(
                    f"({score_clause}) DESC," if score_clause else ""
                ),
            ),
            params,
        ).fetchall()
        if rows:
            return rows
    return []


def _ncs_search_match_metadata(
    item: dict[str, Any],
    *,
    query_tokens: list[str],
    phrase: str,
    match_mode: str,
    token_expansions: dict[str, list[str]] | None = None,
    intent_expansions: list[str] | None = None,
) -> None:
    raw_fields = item.pop("_search_fields", {})
    normalized_fields = {
        field_name: _normalize_ncs_search_text(field_value).casefold()
        for field_name, field_value in raw_fields.items()
        if field_value is not None
    }
    active_expansions = (
        token_expansions or {}
        if match_mode in {"expanded_token_and", "token_or"}
        else {}
    )
    matched_tokens: list[str] = []
    matched_expansions: list[dict[str, Any]] = []
    matched_terms: list[str] = []
    if match_mode == "intent_alias":
        for expansion in intent_expansions or []:
            normalized_expansion = expansion.casefold()
            expansion_fields = [
                field_name
                for field_name, value in normalized_fields.items()
                if normalized_expansion and normalized_expansion in value
            ]
            if not expansion_fields:
                continue
            matched_terms.append(normalized_expansion)
            matched_expansions.append(
                {
                    "query": phrase,
                    "matched_as": expansion,
                    "match_fields": expansion_fields,
                }
            )
    for token in query_tokens:
        normalized_token = token.casefold()
        direct_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if normalized_token and normalized_token in value
        ]
        if direct_fields:
            matched_tokens.append(token)
            matched_terms.append(normalized_token)
            continue
        for expansion in active_expansions.get(token, []):
            normalized_expansion = expansion.casefold()
            expansion_fields = [
                field_name
                for field_name, value in normalized_fields.items()
                if normalized_expansion and normalized_expansion in value
            ]
            if not expansion_fields:
                continue
            matched_tokens.append(token)
            matched_terms.append(normalized_expansion)
            matched_expansions.append(
                {
                    "token": token,
                    "matched_as": expansion,
                    "match_fields": expansion_fields,
                }
            )
            break
    normalized_phrase = phrase.casefold()
    if match_mode == "phrase":
        match_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if normalized_phrase and normalized_phrase in value
        ]
    else:
        match_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if any(term in value for term in matched_terms)
        ]
    item.pop("_match_tier", None)
    item["match_mode"] = match_mode
    item["matched_tokens"] = matched_tokens
    item["match_fields"] = match_fields
    item["matched_expansions"] = matched_expansions


def _round_robin_ncs_search_results(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    requested_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index = 0
    while True:
        appended = False
        for item_type in requested_types:
            candidates = candidates_by_type.get(item_type, [])
            if index < len(candidates):
                merged.append(candidates[index])
                appended = True
        if not appended:
            return merged
        index += 1


def search_ncs(
    query: str,
    scope: str = "all",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """Search NCS evidence with phrase, token-AND, and token-OR fallback."""
    max_rows = _required_runtime_helper("clamp_limit", _CLAMP_LIMIT)(limit)
    try:
        applied_offset = min(max(int(offset), 0), 10_000)
    except (TypeError, ValueError):
        applied_offset = 0
    normalized_scope = scope if scope in _NCS_SEARCH_TYPES or scope == "all" else "all"
    requested_types = (
        _NCS_SEARCH_TYPES if normalized_scope == "all" else (normalized_scope,)
    )
    phrase, query_tokens, fallback_tokens = _normalize_ncs_search_query(query)
    intent_expansions = _ncs_search_intent_expansions(phrase)
    empty_counts = {item_type: 0 for item_type in requested_types}
    empty_more = {item_type: False for item_type in requested_types}
    if not phrase:
        return {
            "query": query,
            "normalized_query": phrase,
            "query_tokens": query_tokens,
            "scope": normalized_scope,
            "match_mode": None,
            "query_expansions": {},
            "query_intent_expansions": [],
            "counts_by_type": empty_counts,
            "has_more_by_type": empty_more,
            "returned": 0,
            "offset": applied_offset,
            "next_offset": None,
            "markdown_summary": _ncs_search_markdown(
                query,
                [],
                counts_by_type=empty_counts,
                offset=applied_offset,
                next_offset=None,
            ),
            "results": [],
        }

    candidate_limit = applied_offset + max_rows + 1
    raw_candidates: dict[str, list[dict[str, Any]]] = {
        item_type: [] for item_type in requested_types
    }
    with _required_runtime_helper("open_db", _OPEN_DB_FACTORY)() as conn:
        token_expansions = _active_token_expander()(
            conn,
            fallback_tokens,
        )
        token_weights = _ncs_search_token_idf_weights(conn, fallback_tokens)
        if "unit" in requested_types:
            columns = (
                "cu.unit_code",
                "cu.unit_name_raw",
                "cu.api_definition",
                "c.major_name",
                "c.middle_name",
                "c.small_name",
                "c.sub_name",
                "aliases.alias_search_text",
            )
            weighted_columns = (
                ("cu.unit_name_raw", 3.0),
                ("aliases.alias_search_text", 3.0),
                ("c.sub_name", 2.0),
                ("c.small_name", 2.0),
                ("c.middle_name", 1.0),
                ("c.major_name", 1.0),
                ("cu.api_definition", 1.0),
            )
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=weighted_columns,
                token_weights=token_weights,
            )
            tiers = _prepend_ncs_search_intent_tier(
                tiers,
                columns=columns,
                weighted_columns=weighted_columns,
                phrase=phrase,
                intent_expansions=intent_expansions,
            )
            rows = _active_tier_executor()(
                conn,
                """
                WITH alias_search AS (
                    SELECT unit_code,
                           GROUP_CONCAT(
                               COALESCE(alias_text, '') || ' ' || COALESCE(normalized_query, ''),
                               ' '
                           ) AS alias_search_text
                    FROM ncs_query_aliases
                    WHERE unit_code IS NOT NULL
                    GROUP BY unit_code
                )
                SELECT cu.unit_code, cu.unit_name_raw, cu.api_definition,
                       cu.unit_level_raw,
                       c.major_code, c.major_name,
                       c.middle_code, c.middle_name,
                       c.small_code, c.small_name,
                       c.sub_code, c.sub_name,
                       c.duty_order, aliases.alias_search_text,
                       :match_tier AS match_tier
                FROM competency_units cu
                JOIN classifications c ON c.classification_id = cu.classification_id
                LEFT JOIN alias_search aliases ON aliases.unit_code = cu.unit_code
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier,
                    {fallback_order_clause}
                    CASE
                        WHEN cu.unit_code = :exact THEN 0
                        WHEN TRIM(cu.unit_name_raw) = TRIM(:exact) COLLATE NOCASE THEN 0
                        WHEN cu.unit_name_raw LIKE :prefix_pattern ESCAPE '\\' THEN 1
                        WHEN cu.unit_name_raw LIKE :phrase_pattern ESCAPE '\\' THEN 2
                        WHEN c.major_name LIKE :phrase_pattern ESCAPE '\\'
                          OR c.middle_name LIKE :phrase_pattern ESCAPE '\\'
                          OR c.small_name LIKE :phrase_pattern ESCAPE '\\'
                          OR c.sub_name LIKE :phrase_pattern ESCAPE '\\' THEN 3
                        WHEN cu.api_definition LIKE :phrase_pattern ESCAPE '\\' THEN 4
                        ELSE 5
                    END,
                    LENGTH(cu.unit_name_raw),
                    CASE
                        WHEN SUBSTR(cu.unit_code, 1, 8) =
                             COALESCE(c.major_code, '')
                             || COALESCE(c.middle_code, '')
                             || COALESCE(c.small_code, '')
                             || COALESCE(c.sub_code, '')
                        THEN 0
                        ELSE 1
                    END,
                    cu.unit_code
                LIMIT :candidate_limit
                """,
                tiers,
                {
                    "exact": phrase,
                    "prefix_pattern": f"{_escape_ncs_search_like(phrase)}%",
                    "candidate_limit": candidate_limit,
                },
            )
            for row in rows:
                raw_candidates["unit"].append(
                    {
                        "type": "unit",
                        "id": row["unit_code"],
                        "text": row["unit_name_raw"],
                        "unit_level": row["unit_level_raw"],
                        "path": _required_runtime_helper("unit_path", _UNIT_PATH)(row),
                        "api_definition": row["api_definition"],
                        "_match_tier": int(row["match_tier"]),
                        "_search_fields": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "definition": row["api_definition"],
                            "classification": " ".join(
                                str(row[key] or "")
                                for key in ("major_name", "middle_name", "small_name", "sub_name")
                            ),
                            "alias": row["alias_search_text"],
                        },
                    }
                )

        if "element" in requested_types:
            columns = ("ce.element_name_raw",)
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=(("ce.element_name_raw", 3.0),),
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       :match_tier AS match_tier
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause}
                         LENGTH(ce.element_name_raw), ce.element_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["element"].append(
                    {
                        "type": "element",
                        "id": row["element_id"],
                        "text": row["element_name_raw"],
                        "path": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                        },
                        "_match_tier": int(row["match_tier"]),
                        "_search_fields": {"element_name": row["element_name_raw"]},
                    }
                )

        if "criteria" in requested_types:
            columns = ("pc.criteria_text_raw", "pc.criteria_text_refined")
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=(
                    ("pc.criteria_text_raw", 3.0),
                    ("pc.criteria_text_refined", 3.0),
                ),
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT pc.criteria_id, pc.criteria_text_raw, pc.criteria_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       :match_tier AS match_tier
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause} pc.criteria_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["criteria"].append(
                    {
                        "type": "criteria",
                        "id": row["criteria_id"],
                        "text": row["criteria_text_raw"],
                        "path": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "element_id": row["element_id"],
                            "element_name": row["element_name_raw"],
                        },
                        "_match_tier": int(row["match_tier"]),
                        "_search_fields": {
                            "criteria_text": row["criteria_text_raw"],
                            "criteria_text_refined": row["criteria_text_refined"],
                        },
                    }
                )

        if "ksa" in requested_types:
            columns = ("ki.ksa_text_raw", "ki.ksa_text_refined")
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=(
                    ("ki.ksa_text_raw", 3.0),
                    ("ki.ksa_text_refined", 3.0),
                ),
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.ksa_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       :match_tier AS match_tier
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                WHERE {where_clause}{fallback_filter_clause}
                ORDER BY match_tier, {fallback_order_clause} ki.ksa_id
                LIMIT :candidate_limit
                """,
                tiers,
                {"candidate_limit": candidate_limit},
            )
            for row in rows:
                raw_candidates["ksa"].append(
                    {
                        "type": "ksa",
                        "id": row["ksa_id"],
                        "text": row["ksa_text_raw"],
                        "ksa_type": row["ksa_type_name"],
                        "path": {
                            "unit_code": row["unit_code"],
                            "unit_name": row["unit_name_raw"],
                            "element_id": row["element_id"],
                            "element_name": row["element_name_raw"],
                        },
                        "_match_tier": int(row["match_tier"]),
                        "_search_fields": {
                            "ksa_text": row["ksa_text_raw"],
                            "ksa_text_refined": row["ksa_text_refined"],
                        },
                    }
                )

    selected_tier_by_type = {
        item_type: min(
            (int(item["_match_tier"]) for item in raw_candidates[item_type]),
            default=None,
        )
        for item_type in requested_types
    }
    match_mode_by_type = {
        item_type: (
            _NCS_SEARCH_MATCH_MODES.get(selected_tier)
            if selected_tier is not None
            else None
        )
        for item_type, selected_tier in selected_tier_by_type.items()
    }
    active_match_modes = {
        mode for mode in match_mode_by_type.values() if mode is not None
    }
    applied_token_expansions = (
        token_expansions
        if "expanded_token_and" in active_match_modes
        else {}
    )
    applied_intent_expansions = (
        intent_expansions
        if "intent_alias" in active_match_modes
        else []
    )
    match_mode = (
        next(iter(active_match_modes))
        if len(active_match_modes) == 1
        else "mixed" if active_match_modes else None
    )
    candidates_by_type = {
        item_type: [
            item
            for item in raw_candidates.get(item_type, [])
            if item["_match_tier"] == selected_tier_by_type[item_type]
        ]
        for item_type in requested_types
    }
    merged = _round_robin_ncs_search_results(candidates_by_type, requested_types)
    page_end = applied_offset + max_rows
    page = merged[applied_offset:page_end]
    consumed_by_type = {item_type: 0 for item_type in requested_types}
    for item in merged[:page_end]:
        consumed_by_type[item["type"]] += 1
    has_more_by_type: dict[str, bool] = {}
    for item_type in requested_types:
        selected_candidates = candidates_by_type[item_type]
        fetched = raw_candidates[item_type]
        selected_tier = selected_tier_by_type[item_type]
        may_have_more_selected = bool(
            selected_tier is not None
            and len(fetched) == candidate_limit
            and fetched
            and fetched[-1]["_match_tier"] == selected_tier
        )
        has_more_by_type[item_type] = (
            len(selected_candidates) > consumed_by_type[item_type]
            or may_have_more_selected
        )
    counts_by_type = {item_type: 0 for item_type in requested_types}
    for item in page:
        counts_by_type[item["type"]] += 1
        _ncs_search_match_metadata(
            item,
            query_tokens=query_tokens,
            phrase=phrase,
            match_mode=str(match_mode_by_type[item["type"]]),
            token_expansions=token_expansions,
            intent_expansions=intent_expansions,
        )
    next_offset = page_end if page and any(has_more_by_type.values()) else None
    result = {
        "query": query,
        "normalized_query": phrase,
        "query_tokens": query_tokens,
        "scope": normalized_scope,
        "match_mode": match_mode,
        "match_mode_by_type": match_mode_by_type,
        "query_expansions": applied_token_expansions,
        "query_intent_expansions": applied_intent_expansions,
        "counts_by_type": counts_by_type,
        "has_more_by_type": has_more_by_type,
        "returned": len(page),
        "offset": applied_offset,
        "next_offset": next_offset,
        "results": page,
    }
    result["markdown_summary"] = _ncs_search_markdown(
        query,
        page,
        counts_by_type=counts_by_type,
        offset=applied_offset,
        next_offset=next_offset,
    )
    return result
