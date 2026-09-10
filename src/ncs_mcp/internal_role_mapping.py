"""Read-only, explainable candidate mapping from internal roles to NCS jobs.

This is deliberately a *candidate generator*, not an approval engine.  It
reads the prepared NCS SQLite database across every classification and returns
only the tenant-scoped ``RoleAlignmentCandidate`` contract already used by the
Gold LPG overlay.  It never writes SQLite, Neo4j, or a review status.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import math
import re
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .internal_job_roles import (
    ContractValidationError,
    InternalJobRole,
    RoleAlignmentCandidate,
    normalize_semantic_text,
    validate_internal_job_role,
)


INTERNAL_ROLE_MAPPING_SCHEMA = "ncs_internal_role_mapping_v1"
INTERNAL_ROLE_MAPPING_PACKET_SCHEMA = "ncs_internal_role_mapping_packet_v1"
MAPPING_METHOD = "deterministic_lexical_ncs_job_context_v1"
MAX_CANDIDATES = 25
MAX_CATALOG_ROWS = 100_000
MIN_CANDIDATE_SCORE = 0.16
AMBIGUITY_RATIO = 0.90

_TOKEN_RE = re.compile(r"[0-9a-z가-힣]+", flags=re.IGNORECASE)
_STOP_TOKENS = frozenset({"및", "등", "관련", "업무", "수행", "관리", "운영", "지원", "기획"})


@dataclass(frozen=True, slots=True)
class NCSJobContext:
    """Bounded, derived lexical context for one complete NCS sub-classification."""

    code: str
    name: str
    classification_id: int
    classification_names: tuple[str, ...]
    unit_names: tuple[str, ...]
    element_names: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_code(row: Mapping[str, Any]) -> str | None:
    parts = tuple(str(row[field] or "").strip() for field in (
        "major_code", "middle_code", "small_code", "sub_code",
    ))
    return "".join(parts) if all(parts) else None


def _tokens(value: str) -> tuple[str, ...]:
    """Produce stable Korean/Latin lexical units without mutating source text."""

    seen: set[str] = set()
    tokens: list[str] = []
    for token in _TOKEN_RE.findall(normalize_semantic_text(value)):
        if len(token) < 2 or token in _STOP_TOKENS or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tuple(tokens)


def _chargrams(tokens: Iterable[str]) -> set[str]:
    """Small character n-grams cover Korean compound-word boundary variants."""

    result: set[str] = set()
    for token in tokens:
        if len(token) < 3:
            continue
        for width in (2, 3):
            result.update(token[index:index + width] for index in range(len(token) - width + 1))
    return result


def _bounded_append(bucket: list[str], value: Any, *, maximum: int = 160) -> None:
    text = str(value or "").strip()
    if text and len(bucket) < maximum and text not in bucket:
        bucket.append(text)


@contextmanager
def _readonly_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN")
        yield connection
    finally:
        if connection.in_transaction:
            connection.rollback()
        connection.close()


def _require_tables(connection: sqlite3.Connection) -> None:
    present = {
        str(row[0]) for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    missing = {"classifications", "competency_units", "competency_elements"} - present
    if missing:
        raise ValueError(f"NCS mapping requires tables: {', '.join(sorted(missing))}")


def _fetch_limited(connection: sqlite3.Connection, query: str, *, maximum: int) -> list[sqlite3.Row]:
    rows: list[sqlite3.Row] = []
    cursor = connection.execute(query)
    while len(rows) < maximum:
        batch = cursor.fetchmany(min(2_000, maximum - len(rows)))
        if not batch:
            break
        rows.extend(batch)
    if cursor.fetchone() is not None:
        raise ValueError(f"NCS catalog exceeds bounded source-row limit ({maximum})")
    return rows


def load_ncs_job_contexts(
    db_path: str | Path,
    *,
    max_catalog_rows: int = MAX_CATALOG_ROWS,
) -> tuple[NCSJobContext, ...]:
    """Load a bounded all-NCS lexical catalog from a read-only SQLite snapshot."""

    if isinstance(max_catalog_rows, bool) or not 1 <= int(max_catalog_rows) <= MAX_CATALOG_ROWS:
        raise ValueError(f"max_catalog_rows must be an integer between 1 and {MAX_CATALOG_ROWS}")
    maximum = int(max_catalog_rows)
    with _readonly_connection(db_path) as connection:
        _require_tables(connection)
        classifications = _fetch_limited(
            connection,
            """
            SELECT classification_id, major_code, major_name, middle_code, middle_name,
                   small_code, small_name, sub_code, sub_name
            FROM classifications
            ORDER BY major_code, middle_code, small_code, sub_code, classification_id
            """,
            maximum=maximum,
        )
        units = _fetch_limited(
            connection,
            """
            SELECT classification_id, unit_name_refined, unit_name_raw, api_unit_name
            FROM competency_units
            ORDER BY classification_id, unit_code
            """,
            maximum=maximum,
        )
        elements = _fetch_limited(
            connection,
            """
            SELECT cu.classification_id, ce.element_name_refined, ce.element_name_raw,
                   ce.api_element_name
            FROM competency_elements ce
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            ORDER BY cu.classification_id, ce.element_id
            """,
            maximum=maximum,
        )

    unit_names: dict[int, list[str]] = defaultdict(list)
    for row in units:
        _bounded_append(
            unit_names[int(row["classification_id"])],
            row["unit_name_refined"] or row["unit_name_raw"] or row["api_unit_name"],
        )
    element_names: dict[int, list[str]] = defaultdict(list)
    for row in elements:
        _bounded_append(
            element_names[int(row["classification_id"])],
            row["element_name_refined"] or row["element_name_raw"] or row["api_element_name"],
        )

    contexts: list[NCSJobContext] = []
    for row in classifications:
        code = _job_code(row)
        if code is None:
            continue
        classification_id = int(row["classification_id"])
        names = tuple(
            str(row[field] or "").strip()
            for field in ("major_name", "middle_name", "small_name", "sub_name")
            if str(row[field] or "").strip()
        )
        contexts.append(NCSJobContext(
            code=code,
            name=str(row["sub_name"] or row["small_name"] or row["middle_name"] or row["major_name"]),
            classification_id=classification_id,
            classification_names=names,
            unit_names=tuple(unit_names[classification_id]),
            element_names=tuple(element_names[classification_id]),
        ))
    return tuple(contexts)


def _role_weighted_terms(role: InternalJobRole) -> dict[str, float]:
    weighted: Counter[str] = Counter()
    for text, weight in (
        (role.display_name, 5.0),
        *((alias, 4.0) for alias in role.aliases),
        (role.description, 2.0),
        *((duty, 3.0) for duty in role.duties),
        (role.target_level or "", 1.0),
    ):
        for token in _tokens(text):
            weighted[token] = max(weighted[token], weight)
    return dict(weighted)


def _context_score(
    role_terms: Mapping[str, float],
    context: NCSJobContext,
) -> tuple[float, list[dict[str, Any]]]:
    """Return lexical score and compact, directly reproducible evidence."""

    fields = (
        ("classification_name", context.classification_names, 1.0),
        ("competency_unit", context.unit_names, 0.75),
        ("performance_element", context.element_names, 0.55),
    )
    matched_weight = 0.0
    evidence: list[dict[str, Any]] = []
    all_job_tokens: set[str] = set()
    for field, values, field_weight in fields:
        field_tokens: set[str] = set()
        matches: list[dict[str, Any]] = []
        for value in values:
            tokens = set(_tokens(value))
            field_tokens.update(tokens)
            overlap = sorted(token for token in role_terms if token in tokens)
            if overlap and len(matches) < 5:
                matches.append({"ncs_text": value, "matched_terms": overlap})
        all_job_tokens.update(field_tokens)
        for term, role_weight in role_terms.items():
            if term in field_tokens:
                matched_weight += role_weight * field_weight
        if matches:
            evidence.append({"source": field, "matches": matches})

    denominator = sum(role_terms.values()) or 1.0
    exact_score = min(1.0, matched_weight / denominator)
    role_grams = _chargrams(role_terms)
    job_grams = _chargrams(all_job_tokens)
    gram_score = len(role_grams & job_grams) / len(role_grams) if role_grams else 0.0
    score = min(1.0, 0.85 * exact_score + 0.15 * gram_score)
    return round(score, 6), evidence


def _normalise_semantic_candidates(
    candidates: Iterable[Mapping[str, Any]] | None,
    known_codes: set[str],
) -> set[str]:
    """Select injected semantic IDs for recall only; ignore their score/model."""

    if candidates is None:
        return set()
    selected: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            raise ContractValidationError("semantic candidates must be objects")
        key = candidate.get("ncs_target_key", candidate.get("target_key", candidate.get("code")))
        if not isinstance(key, str) or not key.strip():
            raise ContractValidationError("semantic candidate needs ncs_target_key")
        normalized = key.strip()
        if normalized in known_codes:
            selected.add(normalized)
    return selected


def map_internal_job_role(
    role: InternalJobRole | Mapping[str, Any],
    db_path: str | Path,
    *,
    limit: int = 5,
    semantic_candidates: Iterable[Mapping[str, Any]] | None = None,
    contexts: Sequence[NCSJobContext] | None = None,
    min_candidate_score: float = MIN_CANDIDATE_SCORE,
) -> dict[str, Any]:
    """Generate candidate-only NCS job alignments for one tenant-scoped role."""

    validated_role = validate_internal_job_role(role)
    if isinstance(limit, bool) or not 1 <= int(limit) <= MAX_CANDIDATES:
        raise ValueError(f"limit must be an integer between 1 and {MAX_CANDIDATES}")
    if not isinstance(min_candidate_score, (int, float)) or isinstance(min_candidate_score, bool):
        raise ValueError("min_candidate_score must be a number")
    if not 0.0 <= float(min_candidate_score) <= 1.0:
        raise ValueError("min_candidate_score must be between 0 and 1")
    catalog = tuple(contexts) if contexts is not None else load_ncs_job_contexts(db_path)
    known_codes = {context.code for context in catalog}
    semantic_recall_codes = _normalise_semantic_candidates(semantic_candidates, known_codes)
    role_terms = _role_weighted_terms(validated_role)
    scored: list[tuple[float, NCSJobContext, list[dict[str, Any]]]] = []
    for context in catalog:
        score, evidence = _context_score(role_terms, context)
        # Semantic values alter candidate recall only.  They contribute neither
        # score nor evidence and cannot create a strong alignment by themselves.
        if score > 0 or context.code in semantic_recall_codes:
            scored.append((score, context, evidence))
    scored.sort(key=lambda item: (-item[0], item[1].code))
    scored = scored[:int(limit)]

    viable = [item for item in scored if item[0] >= float(min_candidate_score)]
    if not viable:
        recall_only = [context for score, context, _evidence in scored if context.code in semantic_recall_codes]
        unresolved = [RoleAlignmentCandidate(
            role_gold_id=validated_role.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key=context.code,
            score=0.0,
            method="semantic_recall_only_no_evidence",
            evidence=(),
            status="unresolved",
            provenance={
                "source": "semantic_recall_input",
                "semantic_scores_used_as_evidence": False,
                "requires_lexical_or_human_evidence": True,
            },
        ) for context in recall_only[:int(limit)]]
        return {
            "schema": INTERNAL_ROLE_MAPPING_SCHEMA,
            "role": validated_role.to_public_dict(),
            "status": "unresolved",
            "alignment_candidates": [candidate.to_public_dict() for candidate in unresolved],
            "provenance": {
                "source": "ncs_sqlite_read_only",
                "ncs_scope": "all_classifications",
                "major_code_filter": None,
                "candidate_generation_method": MAPPING_METHOD,
                "semantic_recall_candidate_count": len(semantic_recall_codes),
                "semantic_scores_used_as_evidence": False,
                "catalog_job_count": len(catalog),
                "db_writes": False,
                "neo4j_writes": False,
                "human_approval_claim": False,
                "generated_at": _now(),
            },
        }

    ambiguous = (
        len(viable) > 1
        and viable[1][0] >= viable[0][0] * AMBIGUITY_RATIO
    )
    candidates: list[RoleAlignmentCandidate] = []
    for rank, (score, context, evidence) in enumerate(viable, 1):
        candidates.append(RoleAlignmentCandidate(
            role_gold_id=validated_role.gold_id,
            ncs_target_type="ncs_job",
            ncs_target_key=context.code,
            score=score,
            method=MAPPING_METHOD,
            evidence=tuple(evidence + [{
                "source": "ncs_job_identity",
                "code": context.code,
                "name": context.name,
                "classification_id": context.classification_id,
                "rank": rank,
            }]),
            status="ambiguous" if ambiguous else "candidate",
            provenance={
                "source": "ncs_sqlite_read_only",
                "ncs_scope": "all_classifications",
                "major_code_filter": None,
                "semantic_recall_used": context.code in semantic_recall_codes,
                "semantic_scores_used_as_evidence": False,
                "catalog_job_count": len(catalog),
            },
        ))
    return {
        "schema": INTERNAL_ROLE_MAPPING_SCHEMA,
        "role": validated_role.to_public_dict(),
        "status": "ambiguous" if ambiguous else "candidate",
        "alignment_candidates": [candidate.to_public_dict() for candidate in candidates],
        "provenance": {
            "source": "ncs_sqlite_read_only",
            "ncs_scope": "all_classifications",
            "major_code_filter": None,
            "candidate_generation_method": MAPPING_METHOD,
            "semantic_recall_candidate_count": len(semantic_recall_codes),
            "semantic_scores_used_as_evidence": False,
            "catalog_job_count": len(catalog),
            "db_writes": False,
            "neo4j_writes": False,
            "human_approval_claim": False,
            "generated_at": _now(),
        },
    }


def map_internal_job_roles(
    roles: Iterable[InternalJobRole | Mapping[str, Any]],
    db_path: str | Path,
    *,
    limit: int = 5,
    semantic_candidates_by_role: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Map a batch against one shared immutable NCS catalog snapshot."""

    catalog = load_ncs_job_contexts(db_path)
    results: list[dict[str, Any]] = []
    for raw_role in roles:
        role = validate_internal_job_role(raw_role)
        semantic = (semantic_candidates_by_role or {}).get(role.gold_id)
        results.append(map_internal_job_role(
            role, db_path, limit=limit, semantic_candidates=semantic, contexts=catalog,
        ))
    return {
        "schema": INTERNAL_ROLE_MAPPING_PACKET_SCHEMA,
        "ncs_scope": "all_classifications",
        "major_code_filter": None,
        "role_results": results,
        "provenance": {
            "source": "ncs_sqlite_read_only",
            "db_writes": False,
            "neo4j_writes": False,
            "human_approval_claim": False,
            "generated_at": _now(),
        },
    }


__all__ = [
    "AMBIGUITY_RATIO",
    "INTERNAL_ROLE_MAPPING_PACKET_SCHEMA",
    "INTERNAL_ROLE_MAPPING_SCHEMA",
    "MAPPING_METHOD",
    "MAX_CANDIDATES",
    "MAX_CATALOG_ROWS",
    "MIN_CANDIDATE_SCORE",
    "NCSJobContext",
    "load_ncs_job_contexts",
    "map_internal_job_role",
    "map_internal_job_roles",
]
