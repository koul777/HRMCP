"""Bounded second-stage ontology evidence lookup for Gold candidates.

This private runtime helper reads candidate ontology relations from the
authoritative SQLite database after another backend has selected a small set
of concept identifiers.  It does not promote relations, make approval claims,
or write to the source database.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import closing
import math
import os
from pathlib import Path
import sqlite3
from typing import Any


GOLD_DEEP_EVIDENCE_SCHEMA = "ncs_gold_deep_evidence_v1"
GOLD_TASK_DEEP_EVIDENCE_SCHEMA = "ncs_gold_task_deep_evidence_v1"
MAX_DEEP_EVIDENCE_CONCEPT_IDS = 25
MAX_DEEP_EVIDENCE_CRITERIA_IDS = 25
MAX_DEEP_EVIDENCE_RELATIONS = 200
DEFAULT_DEEP_EVIDENCE_LIMIT = 50

ALLOWED_RELATION_TYPES = frozenset(
    {
        "knowledge_enables_skill",
        "attitude_supports_skill",
        "knowledge_informs_attitude",
        "co_required_in_element",
    }
)
ALLOWED_DIRECTIONS = frozenset({"incoming", "outgoing", "both"})

_DIRECTION_PREDICATES = {
    "outgoing": "rel.source_concept_id IN ({ids})",
    "incoming": "rel.target_concept_id IN ({ids})",
    "both": (
        "(rel.source_concept_id IN ({ids}) "
        "OR rel.target_concept_id IN ({ids_second}))"
    ),
}


class GoldDeepEvidenceError(RuntimeError):
    """Base error for the private second-stage evidence reader."""


class GoldDeepEvidenceValidationError(GoldDeepEvidenceError, ValueError):
    """Raised before opening SQLite when an input exceeds the safe contract."""


class GoldDeepEvidenceUnavailableError(GoldDeepEvidenceError):
    """Raised when the read-only SQLite evidence source cannot be queried."""


def _validated_concept_ids(concept_ids: object) -> tuple[int, ...]:
    if (
        isinstance(concept_ids, (str, bytes))
        or not isinstance(concept_ids, Sequence)
    ):
        raise GoldDeepEvidenceValidationError(
            "concept_ids must be a non-empty sequence of positive integers"
        )
    if not concept_ids:
        raise GoldDeepEvidenceValidationError("concept_ids must not be empty")
    if len(concept_ids) > MAX_DEEP_EVIDENCE_CONCEPT_IDS:
        raise GoldDeepEvidenceValidationError(
            f"concept_ids may contain at most {MAX_DEEP_EVIDENCE_CONCEPT_IDS} items"
        )
    validated: list[int] = []
    for value in concept_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GoldDeepEvidenceValidationError(
                "concept_ids must contain only positive integers"
            )
        validated.append(value)
    # Query and output order must not depend on Gold backend return order.
    return tuple(sorted(set(validated)))


def _validated_optional_concept_ids(
    concept_ids: object,
) -> tuple[int, ...] | None:
    if concept_ids is None:
        return None
    return _validated_concept_ids(concept_ids)


def _validated_criteria_ids(criteria_ids: object) -> tuple[int, ...]:
    if (
        isinstance(criteria_ids, (str, bytes))
        or not isinstance(criteria_ids, Sequence)
    ):
        raise GoldDeepEvidenceValidationError(
            "criteria_ids must be a non-empty sequence of positive integers"
        )
    if not criteria_ids:
        raise GoldDeepEvidenceValidationError("criteria_ids must not be empty")
    if len(criteria_ids) > MAX_DEEP_EVIDENCE_CRITERIA_IDS:
        raise GoldDeepEvidenceValidationError(
            f"criteria_ids may contain at most {MAX_DEEP_EVIDENCE_CRITERIA_IDS} items"
        )
    validated: list[int] = []
    for value in criteria_ids:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise GoldDeepEvidenceValidationError(
                "criteria_ids must contain only positive integers"
            )
        validated.append(value)
    return tuple(sorted(set(validated)))


def _validated_relation_types(relation_types: object) -> tuple[str, ...]:
    if relation_types is None:
        return tuple(sorted(ALLOWED_RELATION_TYPES))
    if (
        isinstance(relation_types, (str, bytes))
        or not isinstance(relation_types, Sequence)
        or not relation_types
    ):
        raise GoldDeepEvidenceValidationError(
            "relation_types must be a non-empty sequence"
        )
    values: list[str] = []
    for value in relation_types:
        if not isinstance(value, str) or value not in ALLOWED_RELATION_TYPES:
            raise GoldDeepEvidenceValidationError(
                "relation_types contains a relation outside the serving allowlist"
            )
        values.append(value)
    return tuple(sorted(set(values)))


def _validated_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_DEEP_EVIDENCE_RELATIONS
    ):
        raise GoldDeepEvidenceValidationError(
            f"limit must be an integer between 1 and {MAX_DEEP_EVIDENCE_RELATIONS}"
        )
    return limit


def _validated_timeout(timeout_seconds: object) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(float(timeout_seconds))
        or not 0 < float(timeout_seconds) <= 30.0
    ):
        raise GoldDeepEvidenceValidationError(
            "timeout_seconds must be a finite number between 0 and 30"
        )
    return float(timeout_seconds)


def _read_only_uri(db_path: str | os.PathLike[str]) -> str:
    try:
        path = Path(db_path).resolve(strict=True)
    except (OSError, TypeError, ValueError):
        raise GoldDeepEvidenceUnavailableError(
            "SQLite deep-evidence source is unavailable"
        ) from None
    if not path.is_file():
        raise GoldDeepEvidenceUnavailableError(
            "SQLite deep-evidence source is unavailable"
        )
    # Path.as_uri percent-encodes query separators that may occur in a file
    # name, so only this module-owned mode parameter affects sqlite3.connect.
    return f"{path.as_uri()}?mode=ro"


class SQLiteGoldDeepEvidenceReader:
    """Read candidate ontology relations through a read-only SQLite handle."""

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        timeout_seconds: float = 1.0,
    ) -> None:
        self._db_uri = _read_only_uri(db_path)
        self._timeout_seconds = _validated_timeout(timeout_seconds)

    def read(
        self,
        concept_ids: Sequence[int],
        *,
        direction: str = "both",
        relation_types: Sequence[str] | None = None,
        limit: int = DEFAULT_DEEP_EVIDENCE_LIMIT,
    ) -> dict[str, Any]:
        """Return only candidate-state, allowlisted relations.

        Dynamic values are bound SQLite parameters.  The only SQL fragments
        selected dynamically are module-owned direction predicates and the
        number of ``?`` placeholders derived from already bounded sequences.
        """

        ids = _validated_concept_ids(concept_ids)
        if direction not in ALLOWED_DIRECTIONS:
            raise GoldDeepEvidenceValidationError(
                "direction must be one of: both, incoming, outgoing"
            )
        types = _validated_relation_types(relation_types)
        bounded_limit = _validated_limit(limit)

        id_placeholders = ",".join("?" for _ in ids)
        type_placeholders = ",".join("?" for _ in types)
        predicate = _DIRECTION_PREDICATES[direction].format(
            ids=id_placeholders,
            ids_second=id_placeholders,
        )
        id_parameters: tuple[object, ...] = tuple(ids)
        if direction == "both":
            id_parameters = (*id_parameters, *ids)
        parameters = (
            *id_parameters,
            *types,
            "candidate",
            bounded_limit + 1,
        )
        sql = f"""
            SELECT
                rel.relation_id,
                rel.source_concept_id,
                source.concept_name AS source_concept_name,
                source.concept_type AS source_concept_type,
                rel.relation_type,
                rel.target_concept_id,
                target.concept_name AS target_concept_name,
                target.concept_type AS target_concept_type,
                rel.relation_label,
                rel.review_status
            FROM ontology_concept_relations AS rel
            JOIN ontology_concepts AS source
              ON source.concept_id = rel.source_concept_id
            JOIN ontology_concepts AS target
              ON target.concept_id = rel.target_concept_id
            WHERE {predicate}
              AND rel.relation_type IN ({type_placeholders})
              AND rel.review_status = ?
            ORDER BY
                rel.relation_type ASC,
                rel.source_concept_id ASC,
                rel.target_concept_id ASC,
                rel.relation_id ASC
            LIMIT ?
        """

        try:
            with closing(
                sqlite3.connect(
                    self._db_uri,
                    uri=True,
                    timeout=self._timeout_seconds,
                )
            ) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                raw_rows = conn.execute(sql, parameters).fetchall()
        except sqlite3.Error:
            raise GoldDeepEvidenceUnavailableError(
                "SQLite deep-evidence source is unavailable"
            ) from None

        truncated = len(raw_rows) > bounded_limit
        selected_ids = set(ids)
        relations: list[dict[str, Any]] = []
        for raw in raw_rows[:bounded_limit]:
            row = dict(raw)
            source_id = int(row["source_concept_id"])
            target_id = int(row["target_concept_id"])
            incoming_match = target_id in selected_ids
            outgoing_match = source_id in selected_ids
            matched_direction = (
                "both"
                if incoming_match and outgoing_match
                else "incoming" if incoming_match else "outgoing"
            )
            relations.append(
                {
                    **row,
                    "matched_direction": matched_direction,
                    "approval_claim": False,
                }
            )

        return {
            "schema": GOLD_DEEP_EVIDENCE_SCHEMA,
            "concept_ids": list(ids),
            "direction": direction,
            "relation_types": list(types),
            "relations": relations,
            "read_only": True,
            "db_writes": False,
            "approval_claim": False,
            "audit": {
                "read_only": True,
                "query_only": True,
                "db_writes": False,
                "approval_claim": False,
                "review_status_filter": "candidate",
                "row_count": len(relations),
                "limit": bounded_limit,
                "truncated": truncated,
            },
        }

    retrieve = read

    def read_task_relations(
        self,
        criteria_ids: Sequence[int],
        *,
        concept_ids: Sequence[int] | None = None,
        relation_types: Sequence[str] | None = None,
        limit: int = DEFAULT_DEEP_EVIDENCE_LIMIT,
    ) -> dict[str, Any]:
        """Hydrate bounded task/KSA evidence through the criteria index.

        ``criteria_ids`` is intentionally mandatory.  Optional concept IDs can
        narrow rows already selected through ``idx_task_ksa_rel_criteria`` but
        can never become a concept-only scan of the large relation table.
        """

        criteria = _validated_criteria_ids(criteria_ids)
        concepts = _validated_optional_concept_ids(concept_ids)
        types = _validated_relation_types(relation_types)
        bounded_limit = _validated_limit(limit)

        criteria_placeholders = ",".join("?" for _ in criteria)
        type_placeholders = ",".join("?" for _ in types)
        concept_clause = ""
        concept_parameters: tuple[object, ...] = ()
        if concepts is not None:
            concept_placeholders = ",".join("?" for _ in concepts)
            concept_clause = (
                f" AND (rel.source_concept_id IN ({concept_placeholders})"
                f" OR rel.target_concept_id IN ({concept_placeholders}))"
            )
            concept_parameters = (*concepts, *concepts)

        # INDEXED BY makes failure preferable to accidentally scanning the
        # multi-million-row task relation table when a serving snapshot lacks
        # its required criteria index.
        sql = f"""
            SELECT
                rel.relation_id,
                rel.relation_type,
                rel.criteria_id,
                rel.element_id,
                rel.source_concept_id,
                rel.target_concept_id,
                rel.source_atomic_id,
                rel.target_atomic_id,
                rel.confidence_score,
                rel.evidence_text,
                rel.review_status
            FROM task_ksa_concept_relations AS rel
                 INDEXED BY idx_task_ksa_rel_criteria
            WHERE rel.criteria_id IN ({criteria_placeholders})
              AND rel.relation_type IN ({type_placeholders})
              AND rel.review_status != ?
              {concept_clause}
            ORDER BY
                rel.criteria_id ASC,
                rel.relation_type ASC,
                rel.source_concept_id ASC,
                rel.target_concept_id ASC,
                rel.source_atomic_id ASC,
                rel.target_atomic_id ASC,
                rel.relation_id ASC
            LIMIT ?
        """
        parameters = (
            *criteria,
            *types,
            "rejected",
            *concept_parameters,
            bounded_limit + 1,
        )

        try:
            with closing(
                sqlite3.connect(
                    self._db_uri,
                    uri=True,
                    timeout=self._timeout_seconds,
                )
            ) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only=ON")
                raw_rows = conn.execute(sql, parameters).fetchall()
        except sqlite3.Error:
            raise GoldDeepEvidenceUnavailableError(
                "SQLite task deep-evidence source is unavailable"
            ) from None

        truncated = len(raw_rows) > bounded_limit
        relations = [
            {**dict(row), "approval_claim": False}
            for row in raw_rows[:bounded_limit]
        ]
        return {
            "schema": GOLD_TASK_DEEP_EVIDENCE_SCHEMA,
            "criteria_ids": list(criteria),
            "concept_ids": list(concepts) if concepts is not None else None,
            "relation_types": list(types),
            "relations": relations,
            "read_only": True,
            "db_writes": False,
            "approval_claim": False,
            "audit": {
                "read_only": True,
                "query_only": True,
                "db_writes": False,
                "approval_claim": False,
                "mandatory_index": "idx_task_ksa_rel_criteria",
                "criteria_filter_required": True,
                "concept_only_scan_allowed": False,
                "rejected_relations_included": False,
                "row_count": len(relations),
                "limit": bounded_limit,
                "truncated": truncated,
            },
        }

    hydrate_task_relations = read_task_relations


def read_gold_deep_evidence(
    db_path: str | os.PathLike[str],
    concept_ids: Sequence[int],
    **kwargs: Any,
) -> dict[str, Any]:
    """One-shot convenience wrapper around :class:`SQLiteGoldDeepEvidenceReader`."""

    return SQLiteGoldDeepEvidenceReader(db_path).read(concept_ids, **kwargs)


def read_gold_task_deep_evidence(
    db_path: str | os.PathLike[str],
    criteria_ids: Sequence[int],
    **kwargs: Any,
) -> dict[str, Any]:
    """One-shot, criteria-indexed task/KSA evidence hydration."""

    return SQLiteGoldDeepEvidenceReader(db_path).read_task_relations(
        criteria_ids,
        **kwargs,
    )


# Concise alias for future internal orchestration code.
GoldDeepEvidenceReader = SQLiteGoldDeepEvidenceReader


__all__ = [
    "ALLOWED_DIRECTIONS",
    "ALLOWED_RELATION_TYPES",
    "DEFAULT_DEEP_EVIDENCE_LIMIT",
    "GOLD_DEEP_EVIDENCE_SCHEMA",
    "GOLD_TASK_DEEP_EVIDENCE_SCHEMA",
    "GoldDeepEvidenceError",
    "GoldDeepEvidenceReader",
    "GoldDeepEvidenceUnavailableError",
    "GoldDeepEvidenceValidationError",
    "MAX_DEEP_EVIDENCE_CONCEPT_IDS",
    "MAX_DEEP_EVIDENCE_CRITERIA_IDS",
    "MAX_DEEP_EVIDENCE_RELATIONS",
    "SQLiteGoldDeepEvidenceReader",
    "read_gold_deep_evidence",
    "read_gold_task_deep_evidence",
]
