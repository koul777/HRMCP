from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from typing import Any

from ncs_mcp.query_router import (
    NCS_SEARCH_CONTEXT_RESOLVER_VERSION,
    NCS_SEARCH_CONTEXT_SCHEMA,
    normalize_search_context_inputs,
    search_context_request_contract,
)

from .normalization import (
    SEARCH_NORMALIZATION_FIELDS,
    SEARCH_NORMALIZATION_REQUIRED_MANIFEST,
    SEARCH_NORMALIZATION_SOURCE_FIELDS,
    SEARCH_NORMALIZATION_V2_FIELDS,
    SEARCH_NORMALIZATION_V2_OVERRIDES,
    SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST,
    normalize_search_text,
)


_OPEN_DB_FACTORY: Any = None
_CLAMP_LIMIT: Any = None
_UNIT_PATH: Any = None
_TIER_PREDICATES: Any = None
_TIER_EXECUTOR: Any = None
_TOKEN_EXPANDER: Any = None

_NCS_CLASSIFICATION_FILTER_FIELDS = (
    "major_code",
    "middle_code",
    "small_code",
    "sub_code",
    "major_name",
    "middle_name",
    "small_name",
    "sub_name",
)
_NCS_IGNORED_FILTER_KEY_PREVIEW_LIMIT = 8
_NCS_IGNORED_FILTER_KEY_MAX_LENGTH = 64


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
# Definitions describe the work performed by a unit.  Give them enough weight
# to beat a name-only candidate when the query contains concrete task terms,
# while keeping the unit name as the strongest single field.
_NCS_SEARCH_DEFINITION_WEIGHT = 2.0
# Task/KSA evidence is a supporting signal for the weakest lexical fallback.
# It is deliberately below the unit-name/definition weights so that broad
# evidence cannot override an exact or token-AND match.
_NCS_SEARCH_TASK_KSA_WEIGHT = 0.5
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


def _ncs_search_boundary_match(value: Any, needle: Any) -> int:
    """Return 1 when ``needle`` starts at a lexical boundary in ``value``.

    NCS names are Korean compounds, so a right-hand boundary would reject
    useful prefix matches such as ``데이터분석`` -> ``데이터분석 실무``.  The
    important false-positive case is a query token occurring *inside* another
    word (for example ``차량`` in ``철도차량``), which is rejected by requiring
    a non-word character on the left unless the match starts at position 0.
    The helper is deliberately small and deterministic so it can be registered
    as a SQLite UDF for every search connection and reused by Python metadata.
    """
    normalized_value = _normalize_ncs_search_text(value).casefold()
    normalized_needle = _normalize_ncs_search_text(needle).casefold()
    return _ncs_search_boundary_match_normalized(normalized_value, normalized_needle)


def _ncs_search_boundary_match_normalized(value: Any, needle: Any) -> int:
    """Check already-normalized fields without repeating Unicode conversion."""
    normalized_value = str(value or "")
    normalized_needle = str(needle or "")
    if not normalized_value or not normalized_needle:
        return 0
    start = 0
    while True:
        index = normalized_value.find(normalized_needle, start)
        if index < 0:
            return 0
        if index == 0 or not _ncs_search_word_character(normalized_value[index - 1]):
            return 1
        start = index + 1


def _ncs_search_word_character(character: str) -> bool:
    """Whether a character belongs to a lexical token for boundary checks."""
    if not character:
        return False
    category = unicodedata.category(character)
    return character == "_" or category[0] in {"L", "N", "M"}


def _register_ncs_search_udfs(conn: Any) -> None:
    """Install search UDFs on a connection before executing tier SQL."""
    conn.create_function("ncs_search_match", 2, _ncs_search_boundary_match)
    conn.create_function(
        "ncs_search_match_normalized", 2, _ncs_search_boundary_match_normalized
    )


def _has_normalized_search_columns(conn: Any) -> bool:
    """Trust only a complete, Builder-attested normalized projection."""
    return bool(_normalized_search_storage(conn))


def _normalized_search_storage(conn: Any) -> str | bool:
    """Select an entire attested storage contract, or the all-legacy path.

    Manifest identity is checked before schema so a partial v2 cannot silently
    borrow v1 columns, and contradictory/duplicate attestations fail closed.
    """
    manifest_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(serving_snapshot_manifest)")
    }
    if not {"manifest_key", "manifest_value"}.issubset(manifest_columns):
        return False
    placeholders = ", ".join(
        "?" for _ in SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST
    )
    rows = conn.execute(
        "SELECT manifest_key, manifest_value "
        "FROM serving_snapshot_manifest "
        f"WHERE manifest_key IN ({placeholders})",
        tuple(SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST),
    ).fetchall()
    values = dict(rows)
    if len(values) != len(rows):
        return False
    if values.get("search_normalization_schema") == (
        SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST["search_normalization_schema"]
    ):
        mode = "v2"
        fields = SEARCH_NORMALIZATION_V2_FIELDS
        manifest = SEARCH_NORMALIZATION_V2_REQUIRED_MANIFEST
    else:
        mode = "v1"
        fields = SEARCH_NORMALIZATION_FIELDS
        manifest = SEARCH_NORMALIZATION_REQUIRED_MANIFEST
        if "search_normalization_storage" in values:
            return False
    if any(values.get(key) != expected for key, expected in manifest.items()):
        return False
    for table, mapping in fields.items():
        columns = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
        required = set(mapping.values()) | set(SEARCH_NORMALIZATION_SOURCE_FIELDS[table])
        if not required.issubset(columns):
            return False
        if mode == "v2":
            overrides = set(SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {}).values())
            for derived in mapping.values():
                info = columns[derived]
                if str(info[2]).upper() != "TEXT" or bool(info[3]) != (derived not in overrides):
                    return False
    return mode


def _ncs_search_column(column: str, normalized: bool | str) -> str:
    if not normalized:
        return column
    alias, field = column.split(".", 1)
    tables = {
        "cu": "competency_units", "ce": "competency_elements",
        "pc": "performance_criteria", "ki": "ksa_items",
        "c": "classifications", "aliases": "ncs_query_aliases",
    }
    table = tables.get(alias, "")
    if normalized == "v2":
        override = SEARCH_NORMALIZATION_V2_OVERRIDES.get(table, {}).get(field)
        if override:
            return f"COALESCE({alias}.{override}, {column}, '')"
    fields = (
        SEARCH_NORMALIZATION_V2_FIELDS
        if normalized == "v2" else SEARCH_NORMALIZATION_FIELDS
    )
    derived = fields.get(table, {}).get(field)
    return f"{alias}.{derived}" if derived else column


def _normalize_ncs_classification_filter(
    classification_filter: dict[str, Any] | None,
) -> dict[str, str]:
    """Keep only explicit, parameter-bound classification constraints."""
    if not isinstance(classification_filter, dict):
        return {}
    normalized: dict[str, str] = {}
    for field in _NCS_CLASSIFICATION_FILTER_FIELDS:
        value = classification_filter.get(field)
        if value is None:
            continue
        text = _normalize_ncs_search_text(value)
        if text:
            normalized[field] = text
    return normalized


def _ignored_ncs_classification_filter_keys(
    classification_filter: dict[str, Any] | None,
) -> tuple[list[str], int]:
    if not isinstance(classification_filter, dict):
        return [], 0

    def bounded_key(key: Any) -> str:
        text = str(key)
        if len(text) <= _NCS_IGNORED_FILTER_KEY_MAX_LENGTH:
            return text
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        prefix_length = _NCS_IGNORED_FILTER_KEY_MAX_LENGTH - len(digest) - 1
        return f"{text[:prefix_length]}#{digest}"

    ignored = sorted(
        bounded_key(key)
        for key in classification_filter
        if str(key) not in _NCS_CLASSIFICATION_FILTER_FIELDS
    )
    preview = ignored[:_NCS_IGNORED_FILTER_KEY_PREVIEW_LIMIT]
    return preview, max(0, len(ignored) - len(preview))


def _ncs_context_candidate_public(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "major_code",
            "middle_code",
            "small_code",
            "sub_code",
            "path_label",
            "confidence",
            "match_basis",
        )
    }


def _ncs_context_candidate_compatible(
    candidate: dict[str, Any],
    classification_filter: dict[str, str],
) -> bool:
    if not classification_filter:
        return True
    members = candidate.get("_members") or []
    for member in members:
        compatible = True
        for field, expected in classification_filter.items():
            actual = str(member.get(field) or "")
            if field.endswith("_code"):
                if actual.casefold() != str(expected).casefold():
                    compatible = False
                    break
            elif not _ncs_search_boundary_match(actual, expected):
                compatible = False
                break
        if compatible:
            return True
    return False


def resolve_ncs_search_context(
    conn: Any,
    *,
    context_text: Any = None,
    job_scope: Any = None,
    classification_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve caller-supplied HR context against source-backed NCS paths.

    The search query is intentionally absent from this API.  Resolution is
    read-only and produces a shadow prior only; it never becomes a SQL filter
    or changes the public result order during rollout phase 2.
    """
    normalized_context, normalized_job_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    normalized_filter = _normalize_ncs_classification_filter(classification_filter)
    ignored_filter_keys, ignored_filter_key_omitted_count = (
        _ignored_ncs_classification_filter_keys(classification_filter)
    )
    requested = search_context_request_contract(
        context_text=normalized_context,
        job_scope=normalized_job_scope,
        classification_filter=normalized_filter,
    )
    policy = {
        "query_inference_allowed": False,
        "soft_prior_source": (
            "caller_supplied_context"
            if normalized_context or normalized_job_scope
            else None
        ),
        "hard_filter_source": "caller_supplied" if normalized_filter else None,
        "lexical_tier_preserved": True,
        "rollout_phase": "shadow",
    }
    warnings = [
        f"ignored_classification_filter_key:{key}"
        for key in ignored_filter_keys
    ]
    if ignored_filter_key_omitted_count:
        warnings.append(
            "ignored_classification_filter_keys_omitted:"
            f"{ignored_filter_key_omitted_count}"
        )
    base = {
        "schema": NCS_SEARCH_CONTEXT_SCHEMA,
        "resolver_version": NCS_SEARCH_CONTEXT_RESOLVER_VERSION,
        "requested": requested,
        "policy": policy,
        "selected_candidate": None,
        "alternative_candidates": [],
        "alternative_count": 0,
        "prior_applied": False,
        "shadow_mode": True,
        "shadow_ranking_computed": False,
        "hard_filter_applied": bool(normalized_filter),
        "needs_context": False,
        "warnings": warnings,
    }
    if not normalized_context and not normalized_job_scope:
        base["status"] = "filtered" if normalized_filter else "not_provided"
        return base

    rows = conn.execute(
        """
        SELECT c.classification_id,
               c.major_code, c.major_name,
               c.middle_code, c.middle_name,
               c.small_code, c.small_name,
               c.sub_code, c.sub_name,
               GROUP_CONCAT(COALESCE(cu.unit_name_raw, ''), ' ') AS unit_names
        FROM classifications c
        LEFT JOIN competency_units cu
          ON cu.classification_id = c.classification_id
        GROUP BY c.classification_id,
                 c.major_code, c.major_name,
                 c.middle_code, c.middle_name,
                 c.small_code, c.small_name,
                 c.sub_code, c.sub_name
        ORDER BY c.major_code, c.middle_code, c.small_code, c.sub_code,
                 c.classification_id
        """
    ).fetchall()
    if not rows:
        base.update(status="unresolved", needs_context=True)
        base["warnings"].append("classification_corpus_empty")
        return base

    levels = ("major", "middle", "small", "sub")
    context_tokens = list(
        dict.fromkeys(
            token
            for token in normalize_search_text(normalized_context).split()
            if len(token) >= 2
        )
    )[:12]
    row_documents: list[str] = []
    for row in rows:
        row_documents.append(
            " ".join(
                normalize_search_text(row[f"{level}_name"])
                for level in levels
                if row[f"{level}_name"]
            )
            + " "
            + normalize_search_text(row["unit_names"])
        )
    context_weights: dict[str, float] = {}
    for token in context_tokens:
        frequency = sum(
            1 for document in row_documents
            if _ncs_search_boundary_match_normalized(document, token)
        )
        context_weights[token] = math.log((len(rows) + 1) / (frequency + 1)) + 1.0
    total_context_weight = sum(context_weights.values())

    candidates: dict[tuple[str | None, ...], dict[str, Any]] = {}

    def add_candidate(
        row: Any,
        *,
        depth: int,
        job_basis: float,
        job_match_name: str | None,
        job_match_basis: str | None,
        matched_context_tokens: list[str],
    ) -> None:
        codes = tuple(
            str(row[f"{level}_code"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        names = tuple(
            str(row[f"{level}_name"] or "") or None
            if index <= depth else None
            for index, level in enumerate(levels)
        )
        matched_weight = sum(
            context_weights.get(token, 0.0) for token in matched_context_tokens
        )
        coverage = (
            matched_weight / total_context_weight if total_context_weight else 0.0
        )
        if normalized_job_scope and normalized_context:
            score = min(1.0, 0.8 * job_basis + 0.2 * coverage)
        elif normalized_job_scope:
            score = job_basis
        else:
            score = min(0.6, 0.6 * coverage)
        if score <= 0:
            return
        key = codes
        path_label = " > ".join(name for name in names if name)
        candidate = candidates.setdefault(
            key,
            {
                **{f"{level}_code": codes[index] for index, level in enumerate(levels)},
                **{f"{level}_name": names[index] for index, level in enumerate(levels)},
                "path_label": path_label,
                "confidence": 0.0,
                "match_basis": [],
                "_depth": depth,
                "_job_basis": 0.0,
                "_job_match_name": job_match_name,
                "_context_token_count": 0,
                "_members": [],
            },
        )
        candidate["_members"].append({key: row[key] for key in row.keys()})
        candidate["_job_basis"] = max(candidate["_job_basis"], job_basis)
        if job_match_name:
            candidate["_job_match_name"] = job_match_name
        candidate["_context_token_count"] = max(
            candidate["_context_token_count"], len(matched_context_tokens)
        )
        candidate["confidence"] = max(candidate["confidence"], round(score, 4))
        bases = candidate["match_basis"]
        if job_match_basis and job_match_basis not in bases:
            bases.append(job_match_basis)
        if matched_context_tokens:
            # Context text is sensitive caller input.  The response may expose
            # that token overlap contributed, but never the matching tokens.
            basis = "context_text_token_overlap"
            if basis not in bases:
                bases.append(basis)

    normalized_job = normalize_search_text(normalized_job_scope)
    for row, document in zip(rows, row_documents):
        matched_context = [
            token
            for token in context_tokens
            if _ncs_search_boundary_match_normalized(document, token)
        ]
        job_depth = 3
        job_basis = 0.0
        job_match_name: str | None = None
        job_match_basis: str | None = None
        if normalized_job:
            exact_matches: list[tuple[int, str]] = []
            boundary_matches: list[tuple[int, str]] = []
            for depth, level in enumerate(levels):
                name = normalize_search_text(row[f"{level}_name"])
                if not name:
                    continue
                if name == normalized_job:
                    exact_matches.append((depth, name))
                elif (
                    name.startswith(normalized_job)
                    or normalized_job.startswith(name)
                    or _ncs_search_boundary_match_normalized(name, normalized_job)
                ):
                    boundary_matches.append((depth, name))
            if exact_matches:
                job_depth, job_match_name = max(exact_matches)
                job_basis = 1.0
                job_match_basis = f"job_scope_exact_{levels[job_depth]}_name"
            elif boundary_matches:
                job_depth, job_match_name = max(boundary_matches)
                job_basis = 0.9
                job_match_basis = f"job_scope_boundary_{levels[job_depth]}_name"
            else:
                unit_names = normalize_search_text(row["unit_names"])
                if unit_names and _ncs_search_boundary_match_normalized(
                    unit_names, normalized_job
                ):
                    job_basis = 0.75
                    job_match_name = normalized_job
                    job_match_basis = "job_scope_official_unit_name"
        if normalized_job and not job_basis and not matched_context:
            continue
        if not normalized_job and not matched_context:
            continue
        add_candidate(
            row,
            depth=job_depth if job_basis else 3,
            job_basis=job_basis,
            job_match_name=job_match_name,
            job_match_basis=job_match_basis,
            matched_context_tokens=matched_context,
        )

    candidate_values = list(candidates.values())
    if normalized_job and any(
        float(item["_job_basis"]) >= 1.0 for item in candidate_values
    ):
        # An exact source-backed job-scope name is stronger than context-text
        # coverage on a broader boundary match.  Context may disambiguate two
        # exact names, but cannot demote the only exact hierarchy node.
        candidate_values = [
            item for item in candidate_values if float(item["_job_basis"]) >= 1.0
        ]
    ranked = sorted(
        candidate_values,
        key=lambda item: (
            -float(item["confidence"]),
            -int(item["_depth"]),
            str(item.get("major_code") or ""),
            str(item.get("middle_code") or ""),
            str(item.get("small_code") or ""),
            str(item.get("sub_code") or ""),
        ),
    )
    # When the same exact name appears on an ancestor and its descendant in the
    # same path (for example 총무), keep the most specific source-backed node.
    pruned: list[dict[str, Any]] = []
    for candidate in ranked:
        is_ancestor_duplicate = any(
            int(candidate["_depth"]) < int(kept["_depth"])
            and float(candidate["confidence"]) <= float(kept["confidence"])
            and all(
                candidate.get(f"{levels[index]}_code")
                == kept.get(f"{levels[index]}_code")
                for index in range(int(candidate["_depth"]) + 1)
            )
            for kept in pruned
        )
        if not is_ancestor_duplicate:
            pruned.append(candidate)
    ranked = pruned
    if not ranked:
        base.update(status="unresolved", needs_context=True)
        return base

    top = ranked[0]
    second_score = float(ranked[1]["confidence"]) if len(ranked) > 1 else 0.0
    margin = round(float(top["confidence"]) - second_score, 4)
    if normalized_job_scope:
        threshold_ok = float(top["confidence"]) >= 0.8
        margin_ok = margin >= 0.15
    else:
        threshold_ok = (
            float(top["confidence"]) >= 0.5
            and int(top["_context_token_count"]) >= 2
        )
        margin_ok = margin >= 0.2
    base["resolution_margin"] = margin
    if not threshold_ok:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in ranked[:3]
        ]
        base["alternative_count"] = len(ranked)
        base.update(status="unresolved", needs_context=True)
        return base
    if not margin_ok:
        base["alternative_candidates"] = [
            _ncs_context_candidate_public(item) for item in ranked[:3]
        ]
        base["alternative_count"] = len(ranked)
        base.update(status="ambiguous", needs_context=True)
        return base
    selected = _ncs_context_candidate_public(top)
    base["selected_candidate"] = selected
    base["alternative_candidates"] = [
        _ncs_context_candidate_public(item) for item in ranked[1:4]
    ]
    base["alternative_count"] = max(0, len(ranked) - 1)
    if normalized_filter and not _ncs_context_candidate_compatible(
        top, normalized_filter
    ):
        base.update(status="conflict", needs_context=True)
        base["warnings"].append("context_conflicts_with_hard_filter")
        return base
    base.update(status="resolved", needs_context=False)
    return base


def _ncs_context_affinity(
    classification_codes: dict[str, Any],
    selected_candidate: dict[str, Any] | None,
) -> tuple[float, str | None]:
    if not selected_candidate:
        return 0.0, None
    weights = {"major": 0.4, "middle": 0.6, "small": 0.8, "sub": 1.0}
    matched_level: str | None = None
    for level in ("major", "middle", "small", "sub"):
        selected = selected_candidate.get(f"{level}_code")
        if selected is None:
            break
        if str(classification_codes.get(f"{level}_code") or "") != str(selected):
            return 0.0, None
        matched_level = level
    return (weights.get(matched_level, 0.0), matched_level)


def _annotate_ncs_search_shadow(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    requested_types: tuple[str, ...],
    search_context: dict[str, Any],
) -> None:
    selected = (
        search_context.get("selected_candidate")
        if search_context.get("status") == "resolved"
        else None
    )
    for item_type in requested_types:
        candidates = candidates_by_type.get(item_type, [])
        annotated: list[tuple[int, float]] = []
        for baseline_rank, item in enumerate(candidates, start=1):
            affinity, level = _ncs_context_affinity(
                item.get("_classification_codes") or {}, selected
            )
            item["context_affinity"] = affinity
            item["context_match"] = {
                "level": level,
                "matched": bool(affinity),
                "prior_applied": False,
            }
            annotated.append((baseline_rank, affinity))
        shadow_order = sorted(annotated, key=lambda pair: (-pair[1], pair[0]))
        shadow_rank = {
            baseline_rank: rank
            for rank, (baseline_rank, _) in enumerate(shadow_order, start=1)
        }
        for baseline_rank, item in enumerate(candidates, start=1):
            item["shadow_rank"] = shadow_rank[baseline_rank]
    search_context["shadow_ranking_computed"] = bool(
        selected and any(candidates_by_type.get(item_type) for item_type in requested_types)
    )


def _ncs_search_needs_context(
    candidates_by_type: dict[str, list[dict[str, Any]]],
    selected_tier_by_type: dict[str, int | None],
) -> bool:
    if selected_tier_by_type.get("unit") != 3:
        return False
    candidates = candidates_by_type.get("unit", [])[:5]
    if not candidates:
        return False
    scope_counts: dict[tuple[str, str, str, str], int] = {}
    for item in candidates:
        codes = item.get("_classification_codes") or {}
        key = tuple(
            str(codes.get(f"{level}_code") or "")
            for level in ("major", "middle", "small", "sub")
        )
        scope_counts[key] = scope_counts.get(key, 0) + 1
    return bool(
        len(scope_counts) >= 2
        and max(scope_counts.values()) / len(candidates) < 0.6
    )


def _ncs_classification_filter_sql(
    classification_filter: dict[str, str],
    *,
    alias: str = "c",
    normalized: bool | str = False,
) -> tuple[str, dict[str, str]]:
    """Build exact-code/boundary-name predicates for a classification alias."""
    clauses: list[str] = []
    params: dict[str, str] = {}
    for field in _NCS_CLASSIFICATION_FILTER_FIELDS:
        value = classification_filter.get(field)
        if not value:
            continue
        parameter = f"class_filter_{field}"
        params[parameter] = value
        if field.endswith("_code"):
            clauses.append(
                f"TRIM(COALESCE({alias}.{field}, '')) = :{parameter} COLLATE NOCASE"
            )
        else:
            column = _ncs_search_column(f"{alias}.{field}", normalized)
            function = "ncs_search_match_normalized" if normalized else "ncs_search_match"
            if normalized:
                params[parameter] = normalize_search_text(value)
            clauses.append(
                f"{function}(COALESCE({column}, ''), :{parameter}) = 1"
            )
    if not clauses:
        return "", {}
    return "(" + " AND ".join(clauses) + ")", params


def _apply_ncs_classification_filter_to_tiers(
    tiers: list[tuple[int, str, dict[str, Any], str, str]],
    classification_filter: dict[str, str],
    *,
    normalized: bool | str = False,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    clause, filter_params = _ncs_classification_filter_sql(
        classification_filter, normalized=normalized
    )
    if not clause:
        return tiers
    filtered: list[tuple[int, str, dict[str, Any], str, str]] = []
    for match_tier, where_clause, params, score_clause, meaningful_clause in tiers:
        tier_params = dict(params)
        tier_params.update(filter_params)
        filtered.append(
            (
                match_tier,
                f"({where_clause}) AND {clause}",
                tier_params,
                score_clause,
                meaningful_clause,
            )
        )
    return filtered


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


def _ncs_search_boundary_any(
    columns: tuple[str, ...], parameter: str, *, normalized: bool | str = False
) -> str:
    """Build a parameter-bound lexical-boundary predicate for SQL search."""
    predicates = []
    for raw_column in columns:
        column = _ncs_search_column(raw_column, normalized)
        derived = normalized and column != raw_column
        function = "ncs_search_match_normalized" if derived else "ncs_search_match"
        bind = f"{parameter}_raw" if normalized and not derived else parameter
        # Keep the cheap SQLite LIKE prefilter ahead of the Python UDF.  The
        # UDF preserves lexical-boundary semantics, while LIKE avoids calling
        # it for the vast majority of rows in the large criteria/KSA tables.
        predicates.append(
            f"(COALESCE({column}, '') LIKE '%' || :{bind} || '%' "
            f"AND {function}(COALESCE({column}, ''), :{bind}) = 1)"
        )
    return "(" + " OR ".join(predicates) + ")"


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
    classification_filter: dict[str, str] | None = None,
    *,
    normalized: bool | str = False,
) -> dict[str, float]:
    """Weight tokens by document frequency in the active search corpus.

    When callers provide a classification filter, the IDF corpus must be the
    same filtered unit set used by the search tiers.  Otherwise a token can be
    common in unrelated NCS majors and be underweighted inside the requested
    scope.  The no-filter path intentionally keeps the original whole-corpus
    query shape and behavior.
    """
    tokens = list(dict.fromkeys(fallback_tokens))
    if not tokens:
        return {}
    # Count the corpus and every token in one pass.  The compact serving
    # profile drops the unit_name_raw index, so each LIKE reads the whole
    # table; a COUNT per token would re-scan it once per token.  Column
    # expressions and placeholder names come from fixed server-side text and
    # integer indexes, and query text stays bound.
    name_column = "unit_name_search_norm" if normalized else "unit_name_raw"
    frequency_terms = ", ".join(
        f"SUM(CASE WHEN {name_column} LIKE :idf_token_{index} ESCAPE '\\' "
        "THEN 1 ELSE 0 END)"
        for index in range(len(tokens))
    )
    params = {
        f"idf_token_{index}": f"%{_escape_ncs_search_like(normalize_search_text(token) if normalized else token)}%"
        for index, token in enumerate(tokens)
    }
    normalized_filter = classification_filter or {}
    scope_clause, scope_params = _ncs_classification_filter_sql(
        normalized_filter,
        alias="c",
        normalized=normalized,
    )
    if scope_clause:
        from_clause = (
            "competency_units cu "
            "JOIN classifications c ON c.classification_id = cu.classification_id"
        )
        sql = f"SELECT COUNT(*), {frequency_terms} FROM {from_clause} WHERE {scope_clause}"
        params = {**params, **scope_params}
    else:
        sql = f"SELECT COUNT(*), {frequency_terms} FROM competency_units"
    row = conn.execute(sql, params).fetchone()
    total = int(row[0] or 0)
    if total <= 1:
        return {}
    ceiling = math.log(total)
    weights: dict[str, float] = {}
    for index, token in enumerate(tokens):
        frequency = max(int(row[index + 1] or 0), 1)
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
    *,
    normalized: bool | str = False,
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
                _ncs_search_boundary_any((column,), parameter, normalized=normalized)
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


def _ncs_search_unit_task_ksa_scores(
    conn: Any,
    unit_codes: list[str],
    fallback_tokens: list[str],
    token_weights: dict[str, float] | None = None,
    *,
    normalized: bool | str | None = None,
) -> dict[str, float]:
    """Score task/KSA evidence for an already retrieved unit candidate set.

    This is intentionally a second-stage lookup.  It never scans the full
    criteria/KSA corpus for every query: only unit codes already returned by
    the lexical fallback are fetched through the element indexes.  Evidence
    contributes once per matched query token, avoiding a verbosity bias toward
    units with more criteria rows.
    """
    candidates = list(dict.fromkeys(str(code) for code in unit_codes if code))
    tokens = list(dict.fromkeys(token for token in fallback_tokens if token))
    if not candidates or not tokens:
        return {}
    if normalized is None:
        normalized = _normalized_search_storage(conn)
    parameters = {
        f"task_ksa_unit_{index}": code
        for index, code in enumerate(candidates)
    }
    # Only Builder-normalized columns can safely prefilter the Unicode-aware
    # boundary check. Legacy raw LIKE would discard fullwidth/decomposed/casefold
    # matches before Python sees them, so legacy snapshots fetch the candidate
    # units' evidence without this additional text prefilter.
    for index, token in enumerate(tokens):
        parameters[f"task_ksa_like_{index}"] = (
            f"%{_escape_ncs_search_like(normalize_search_text(token) if normalized else token)}%"
        )
    unit_placeholders = ", ".join(
        f":task_ksa_unit_{index}" for index in range(len(candidates))
    )

    def evidence_filter(column: str) -> str:
        if not normalized:
            return "1 = 1"
        column = _ncs_search_column(column, normalized)
        return "(" + " OR ".join(
            f"COALESCE({column}, '') LIKE :task_ksa_like_{index} ESCAPE '\\'"
            for index in range(len(tokens))
        ) + ")"

    def evidence_projection(column: str) -> str:
        return (
            f", {_ncs_search_column(column, normalized)} AS evidence_match_text"
            if normalized else ""
        )

    rows = conn.execute(
        f"""
        SELECT ce.unit_code, pc.criteria_text_raw AS evidence_text
               {evidence_projection('pc.criteria_text_raw')}
        FROM competency_elements ce
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND pc.criteria_text_raw IS NOT NULL
          AND {evidence_filter('pc.criteria_text_raw')}
        UNION ALL
        SELECT ce.unit_code, pc.criteria_text_refined AS evidence_text
               {evidence_projection('pc.criteria_text_refined')}
        FROM competency_elements ce
        JOIN performance_criteria pc ON pc.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND pc.criteria_text_refined IS NOT NULL
          AND {evidence_filter('pc.criteria_text_refined')}
        UNION ALL
        SELECT ce.unit_code, ki.ksa_text_raw AS evidence_text
               {evidence_projection('ki.ksa_text_raw')}
        FROM competency_elements ce
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND ki.ksa_text_raw IS NOT NULL
          AND {evidence_filter('ki.ksa_text_raw')}
        UNION ALL
        SELECT ce.unit_code, ki.ksa_text_refined AS evidence_text
               {evidence_projection('ki.ksa_text_refined')}
        FROM competency_elements ce
        JOIN ksa_items ki ON ki.element_id = ce.element_id
        WHERE ce.unit_code IN ({unit_placeholders})
          AND ki.ksa_text_refined IS NOT NULL
          AND {evidence_filter('ki.ksa_text_refined')}
        """,
        parameters,
    ).fetchall()
    evidence_by_unit: dict[str, list[str]] = {code: [] for code in candidates}
    for row in rows:
        evidence_by_unit.setdefault(str(row["unit_code"]), []).append(
            str(row["evidence_match_text" if normalized else "evidence_text"] or "")
        )
    weights = token_weights or {}
    scores: dict[str, float] = {}
    for code, evidence_rows in evidence_by_unit.items():
        score = 0.0
        matched_token_count = 0
        for token in tokens:
            if any(
                (
                    _ncs_search_boundary_match_normalized(
                        evidence, normalize_search_text(token)
                    ) if normalized else _ncs_search_boundary_match(evidence, token)
                ) == 1
                for evidence in evidence_rows
            ):
                matched_token_count += 1
                token_factor = weights.get(
                    token,
                    _NCS_SEARCH_GENERIC_TOKEN_FACTOR
                    if token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
                    else 1.0,
                )
                score += _NCS_SEARCH_TASK_KSA_WEIGHT * token_factor
        # One generic task/KSA word is too weak to overturn a lexical result;
        # require two independent query tokens before enabling the boost.
        scores[code] = score if matched_token_count >= 2 else 0.0
    return scores


def _ncs_search_unit_fallback_score(
    item: dict[str, Any],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    token_weights: dict[str, float] | None,
    *,
    normalized: bool | str = False,
) -> float:
    """Reconstruct the lexical fallback score for stable second-stage sorting."""
    fields = item.get("_search_fields") or {}
    weighted_fields = (
        ("unit_name", 3.0),
        ("alias", 3.0),
        ("classification", 1.5),
        ("definition", _NCS_SEARCH_DEFINITION_WEIGHT),
    )
    expansions = token_expansions or {}
    weights = token_weights or {}
    score = 0.0
    for token in fallback_tokens:
        token_factor = weights.get(
            token,
            _NCS_SEARCH_GENERIC_TOKEN_FACTOR
            if token.casefold() in _NCS_SEARCH_GENERIC_TOKENS
            else 1.0,
        )
        terms = [token, *expansions.get(token, [])]
        for field_name, field_weight in weighted_fields:
            field_value = fields.get(field_name)
            if any(
                (
                    _ncs_search_boundary_match_normalized(
                        normalize_search_text(field_value), normalize_search_text(term)
                    ) if normalized and field_name != "unit_code"
                    else _ncs_search_boundary_match(field_value, term)
                ) == 1
                for term in terms
            ):
                score += field_weight * token_factor
    return score


def _rerank_ncs_unit_task_ksa_candidates(
    candidates: list[dict[str, Any]],
    task_ksa_scores: dict[str, float],
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None,
    token_weights: dict[str, float] | None,
    *,
    normalized: bool | str = False,
) -> list[dict[str, Any]]:
    """Apply supporting task/KSA evidence only within the OR fallback tier."""
    if not candidates or not task_ksa_scores:
        return candidates
    scored = []
    for index, item in enumerate(candidates):
        code = str(item.get("id") or "")
        lexical_score = _ncs_search_unit_fallback_score(
            item,
            fallback_tokens,
            token_expansions,
            token_weights,
            normalized=normalized,
        )
        scored.append(
            (
                lexical_score + task_ksa_scores.get(code, 0.0),
                -index,
                item,
            )
        )
    return [
        item
        for _, _, item in sorted(scored, key=lambda row: (-row[0], -row[1]))
    ]


def _normalized_ncs_search_params(params: dict[str, Any]) -> dict[str, Any]:
    """Keep original code binds while normalizing text binds once per tier."""
    result = dict(params)
    for key, value in params.items():
        if key == "phrase_term" or key.startswith(("token_", "expanded_", "intent_")):
            result[f"{key}_raw"] = value
            result[key] = normalize_search_text(value)
    # phrase_pattern is the legacy unit-order tiebreak, not a text prefilter.
    return result


def _ncs_search_tier_predicates(
    columns: tuple[str, ...],
    phrase: str,
    fallback_tokens: list[str],
    token_expansions: dict[str, list[str]] | None = None,
    weighted_columns: tuple[tuple[str, float], ...] | None = None,
    token_weights: dict[str, float] | None = None,
    *,
    normalized: bool | str = False,
) -> list[tuple[int, str, dict[str, Any], str, str]]:
    params: dict[str, Any] = {
        "phrase_pattern": f"%{_escape_ncs_search_like(phrase)}%",
        "phrase_term": phrase,
    }
    phrase_clause = _ncs_search_boundary_any(columns, "phrase_term", normalized=normalized)
    token_clauses: list[str] = []
    parameter_groups: list[list[str]] = []
    for index, token in enumerate(fallback_tokens):
        parameter = f"token_{index}"
        params[parameter] = token
        token_clauses.append(_ncs_search_boundary_any(columns, parameter, normalized=normalized))
        parameter_groups.append([parameter])
    if not token_clauses:
        if normalized:
            params = _normalized_ncs_search_params(params)
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
                params[parameter] = alternative
                parameter_groups[token_index].append(parameter)
    search_groups = [
        "(" + " OR ".join(
            _ncs_search_boundary_any(columns, parameter, normalized=normalized)
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
        normalized=normalized,
    )
    params.update(rank_params)
    if normalized:
        params = _normalized_ncs_search_params(params)
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
    normalized: bool | str = False,
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
        params[parameter] = alternative
        parameter_groups.append([parameter])
        search_groups.append(_ncs_search_boundary_any(columns, parameter, normalized=normalized))
    score_clause, _, rank_params = _ncs_search_fallback_ranking(
        weighted_columns,
        intent_expansions,
        parameter_groups,
        search_groups,
        normalized=normalized,
    )
    params.update(rank_params)
    if normalized:
        params = _normalized_ncs_search_params(params)
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
    normalized: bool | str = False,
) -> None:
    raw_fields = item.pop("_search_fields", {})
    normalized_fields = {
        field_name: (
            normalize_search_text(field_value)
            if normalized and field_name != "unit_code"
            else _normalize_ncs_search_text(field_value).casefold()
        )
        for field_name, field_value in raw_fields.items()
        if field_value is not None
    }

    def matches(field_name: str, value: str, term: str) -> int:
        if normalized and field_name != "unit_code":
            return _ncs_search_boundary_match_normalized(value, normalize_search_text(term))
        return _ncs_search_boundary_match(value, term)

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
                if normalized_expansion
                and matches(field_name, value, normalized_expansion)
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
            if normalized_token and matches(field_name, value, normalized_token)
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
                if normalized_expansion
                and matches(field_name, value, normalized_expansion)
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
            if normalized_phrase and matches(field_name, value, normalized_phrase)
        ]
    else:
        match_fields = [
            field_name
            for field_name, value in normalized_fields.items()
            if any(matches(field_name, value, term) for term in matched_terms)
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
    classification_filter: dict[str, Any] | None = None,
    context_text: str | None = None,
    job_scope: str | None = None,
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
    normalized_classification_filter = _normalize_ncs_classification_filter(
        classification_filter
    )
    # Validate independently supplied context before touching the DB.  The
    # normalized free text is never included in the response.
    normalized_context_text, normalized_job_scope = normalize_search_context_inputs(
        context_text=context_text,
        job_scope=job_scope,
    )
    intent_expansions = _ncs_search_intent_expansions(phrase)
    empty_counts = {item_type: 0 for item_type in requested_types}
    empty_more = {item_type: False for item_type in requested_types}
    if not phrase:
        with _required_runtime_helper("open_db", _OPEN_DB_FACTORY)() as conn:
            search_context = resolve_ncs_search_context(
                conn,
                context_text=context_text,
                job_scope=job_scope,
                classification_filter=classification_filter,
            )
        return {
            "query": query,
            "normalized_query": phrase,
            "query_tokens": query_tokens,
            "scope": normalized_scope,
            "classification_filter": normalized_classification_filter,
            "classification_filter_applied": bool(normalized_classification_filter),
            "match_mode": None,
            "query_expansions": {},
            "query_intent_expansions": [],
            "counts_by_type": empty_counts,
            "has_more_by_type": empty_more,
            "returned": 0,
            "offset": applied_offset,
            "next_offset": None,
            "search_context": search_context,
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
    unit_task_ksa_scores: dict[str, float] = {}
    search_context: dict[str, Any]
    with _required_runtime_helper("open_db", _OPEN_DB_FACTORY)() as conn:
        _register_ncs_search_udfs(conn)
        search_context = resolve_ncs_search_context(
            conn,
            context_text=context_text,
            job_scope=job_scope,
            classification_filter=classification_filter,
        )
        normalized_search = _normalized_search_storage(conn)
        tier_options = {"normalized": normalized_search} if normalized_search else {}
        token_expansions = _active_token_expander()(
            conn,
            fallback_tokens,
        )
        token_weights = _ncs_search_token_idf_weights(
            conn,
            fallback_tokens,
            normalized_classification_filter,
            normalized=normalized_search,
        )
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
                ("cu.api_definition", _NCS_SEARCH_DEFINITION_WEIGHT),
            )
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=weighted_columns,
                token_weights=token_weights,
                **tier_options,
            )
            tiers = _prepend_ncs_search_intent_tier(
                tiers,
                columns=columns,
                weighted_columns=weighted_columns,
                phrase=phrase,
                intent_expansions=intent_expansions,
                normalized=normalized_search,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            unit_order_name = _ncs_search_column(
                "cu.unit_name_raw", normalized_search
            )
            unit_order_definition = _ncs_search_column(
                "cu.api_definition", normalized_search
            )
            unit_order_classification = tuple(
                _ncs_search_column(f"c.{field}", normalized_search)
                for field in ("major_name", "middle_name", "small_name", "sub_name")
            )
            order_phrase = (
                normalize_search_text(phrase) if normalized_search else phrase
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
                """ + (
                    ", GROUP_CONCAT(alias_search_norm, ' ') AS alias_search_norm"
                    if normalized_search else ""
                ) + f"""
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
                WHERE {{where_clause}}{{fallback_filter_clause}}
                ORDER BY match_tier,
                    {{fallback_order_clause}}
                    CASE
                        WHEN cu.unit_code = :exact_code THEN 0
                        WHEN TRIM({unit_order_name}) = TRIM(:order_exact) COLLATE NOCASE THEN 0
                        WHEN {unit_order_name} LIKE :order_prefix_pattern ESCAPE '\\' THEN 1
                        WHEN {unit_order_name} LIKE :order_phrase_pattern ESCAPE '\\' THEN 2
                        WHEN {unit_order_classification[0]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[1]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[2]} LIKE :order_phrase_pattern ESCAPE '\\'
                          OR {unit_order_classification[3]} LIKE :order_phrase_pattern ESCAPE '\\' THEN 3
                        WHEN {unit_order_definition} LIKE :order_phrase_pattern ESCAPE '\\' THEN 4
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
                    "exact_code": phrase,
                    "order_exact": order_phrase,
                    "order_prefix_pattern": f"{_escape_ncs_search_like(order_phrase)}%",
                    "order_phrase_pattern": f"%{_escape_ncs_search_like(order_phrase)}%",
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
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
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
            selected_unit_tier = min(
                (item["_match_tier"] for item in raw_candidates["unit"]),
                default=None,
            )
            if selected_unit_tier == 3:
                unit_task_ksa_scores = _ncs_search_unit_task_ksa_scores(
                    conn,
                    [item["id"] for item in raw_candidates["unit"]],
                    fallback_tokens,
                    token_weights,
                    normalized=normalized_search,
                )

        if "element" in requested_types:
            columns = ("ce.element_name_raw",)
            tiers = _active_tier_predicates()(
                columns,
                phrase,
                fallback_tokens,
                token_expansions,
                weighted_columns=(("ce.element_name_raw", 3.0),),
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.middle_code, c.small_code, c.sub_code,
                       :match_tier AS match_tier
                FROM competency_elements ce
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
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
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
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
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT pc.criteria_id, pc.criteria_text_raw, pc.criteria_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.middle_code, c.small_code, c.sub_code,
                       :match_tier AS match_tier
                FROM performance_criteria pc
                JOIN competency_elements ce ON ce.element_id = pc.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
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
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
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
                **tier_options,
            )
            tiers = _apply_ncs_classification_filter_to_tiers(
                tiers,
                normalized_classification_filter,
                normalized=normalized_search,
            )
            rows = _active_tier_executor()(
                conn,
                """
                SELECT ki.ksa_id, ki.ksa_type_name, ki.ksa_text_raw, ki.ksa_text_refined,
                       ce.element_id, ce.element_name_raw, ce.unit_code, cu.unit_name_raw,
                       c.major_code, c.middle_code, c.small_code, c.sub_code,
                       :match_tier AS match_tier
                FROM ksa_items ki
                JOIN competency_elements ce ON ce.element_id = ki.element_id
                JOIN competency_units cu ON cu.unit_code = ce.unit_code
                JOIN classifications c ON c.classification_id = cu.classification_id
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
                        "_classification_codes": {
                            f"{level}_code": row[f"{level}_code"]
                            for level in ("major", "middle", "small", "sub")
                        },
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
    if selected_tier_by_type.get("unit") == 3 and unit_task_ksa_scores:
        candidates_by_type["unit"] = _rerank_ncs_unit_task_ksa_candidates(
            candidates_by_type["unit"],
            unit_task_ksa_scores,
            fallback_tokens,
            token_expansions,
            token_weights,
            normalized=normalized_search,
        )
    if search_context.get("status") == "not_provided":
        search_context["needs_context"] = _ncs_search_needs_context(
            candidates_by_type,
            selected_tier_by_type,
        )
    if normalized_context_text or normalized_job_scope:
        _annotate_ncs_search_shadow(
            candidates_by_type,
            requested_types,
            search_context,
        )
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
        item.pop("_classification_codes", None)
        _ncs_search_match_metadata(
            item,
            query_tokens=query_tokens,
            phrase=phrase,
            match_mode=str(match_mode_by_type[item["type"]]),
            token_expansions=token_expansions,
            intent_expansions=intent_expansions,
            normalized=normalized_search,
        )
    next_offset = page_end if page and any(has_more_by_type.values()) else None
    result = {
        "query": query,
        "normalized_query": phrase,
        "query_tokens": query_tokens,
        "scope": normalized_scope,
        "classification_filter": normalized_classification_filter,
        "classification_filter_applied": bool(normalized_classification_filter),
        "match_mode": match_mode,
        "match_mode_by_type": match_mode_by_type,
        "query_expansions": applied_token_expansions,
        "query_intent_expansions": applied_intent_expansions,
        "counts_by_type": counts_by_type,
        "has_more_by_type": has_more_by_type,
        "returned": len(page),
        "offset": applied_offset,
        "next_offset": next_offset,
        "search_context": search_context,
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
