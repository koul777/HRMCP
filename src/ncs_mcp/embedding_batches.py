"""Memory-bounded, provider-neutral embedding work batches.

The SQLite database remains the source of truth.  This module only reads
semantic inputs and validates provider responses; it never writes SQLite or
Neo4j.  Callers may persist the returned patch records through a separately
guarded serving-layer adapter.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .embeddings import (
    EmbeddingDimensionError,
    SemanticEmbeddingInput,
    build_embedding_metadata,
    is_trusted_definition,
    normalize_semantic_text,
    prepare_embedding_input,
    validate_embedding_batch,
)
from .gold_lpg import external_id


EMBEDDING_BATCH_INPUT_SCHEMA = "ncs_embedding_batch_input_v1"
EMBEDDING_NODE_PATCH_SCHEMA = "ncs_embedding_node_patch_v1"

PERFORMANCE_CRITERION = "PerformanceCriterion"
PERFORMANCE_ELEMENT = "PerformanceElement"
KSA_CONCEPT = "KSAConcept"
SUPPORTED_ENTITY_TYPES = (
    PERFORMANCE_CRITERION,
    PERFORMANCE_ELEMENT,
    KSA_CONCEPT,
)

DEFAULT_BATCH_SIZE = 64
DEFAULT_FETCH_SIZE = 128
DEFAULT_MAX_TEXT_CHARS = 8_192
MAX_BATCH_SIZE = 256
MAX_FETCH_SIZE = 512
MAX_TEXT_CHARS = 32_768
MAX_EMBEDDING_DIMENSIONS = 8_192
DEFINITION_POLICY_SCAN_CHARS = 2_048

_KOREAN_BOILERPLATE_DEFINITIONS = frozenset(
    {
        "업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.",
        "업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력.",
        "업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도.",
    }
)
_UNTRUSTED_DEFINITION_SOURCE_MARKERS = (
    "auto",
    "boilerplate",
    "candidate",
    "generated",
    "ksa_meaning",
    "llm",
    "model",
    "raw",
    "template",
)


@dataclass(frozen=True, slots=True)
class EmbeddingNodeInput:
    """One bounded semantic input targeting a stable Gold node id."""

    entity_type: str
    entity_id: str
    source_table: str
    source_key: str
    text: str
    content_hash: str
    cache_key: str
    metadata: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class EmbeddingNodePatch:
    """Validated embedding properties ready for a guarded serving write."""

    entity_type: str
    entity_id: str
    embedding: tuple[float, ...]
    content_hash: str
    cache_key: str
    provider: str
    model: str
    dimensions: int
    metadata: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation without semantic text."""

        return {
            "schema": EMBEDDING_NODE_PATCH_SCHEMA,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "embedding": list(self.embedding),
            "content_hash": self.content_hash,
            "cache_key": self.cache_key,
            "provider": self.provider,
            "model": self.model,
            "dimensions": self.dimensions,
            "metadata": dict(self.metadata),
        }


class _BoundedTextAccumulator:
    """Accumulate normalized fragments without growing beyond ``limit``."""

    __slots__ = ("_parts", "_length", "limit", "truncated")

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._parts: list[str] = []
        self._length = 0
        self.truncated = False

    def add(self, value: Any) -> bool:
        normalized = normalize_semantic_text(value)
        if not normalized:
            return False
        separator_length = 1 if self._parts else 0
        available = self.limit - self._length - separator_length
        if available <= 0:
            self.truncated = True
            return False
        fragment = normalized[:available]
        if len(fragment) < len(normalized):
            self.truncated = True
        self._parts.append(fragment)
        self._length += separator_length + len(fragment)
        return bool(fragment)

    @property
    def text(self) -> str:
        return "\n".join(self._parts)


def _bounded_int(name: str, value: Any, *, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 1 or value > upper:
        raise ValueError(f"{name} must be between 1 and {upper}")
    return value


@contextmanager
def _readonly_connection(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if table not in {
        "performance_criteria",
        "competency_elements",
        "ontology_concepts",
    }:
        return set()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    if exists is None:
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _iter_cursor_rows(cursor: Any, fetch_size: int) -> Iterator[Any]:
    """Iterate solely through bounded ``fetchmany`` calls."""

    while True:
        rows = cursor.fetchmany(fetch_size)
        if not rows:
            return
        yield from rows


def _text_expression(columns: set[str], refined: str, raw: str) -> str | None:
    if refined in columns and raw in columns:
        return f"COALESCE(NULLIF(TRIM({refined}), ''), {raw}, '')"
    if refined in columns:
        return f"COALESCE({refined}, '')"
    if raw in columns:
        return f"COALESCE({raw}, '')"
    return None


def _keyset_bounds(
    *, source_key_min: int | None, source_key_max: int | None, column: str
) -> tuple[str, tuple[int, ...]]:
    """Return a parameterized inclusive keyset range; never use OFFSET.

    All embedding source identifiers are integer primary keys.  The optional
    range is deliberately applied in SQL, rather than filtering emitted
    records, so a shard remains bounded even when a source table is large.
    """

    for name, value in (
        ("source_key_min", source_key_min),
        ("source_key_max", source_key_max),
    ):
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise ValueError(f"{name} must be an integer or None")
    if (
        source_key_min is not None
        and source_key_max is not None
        and source_key_min > source_key_max
    ):
        raise ValueError("source_key_min must not exceed source_key_max")
    clauses: list[str] = []
    params: list[int] = []
    if source_key_min is not None:
        clauses.append(f"{column} >= ?")
        params.append(source_key_min)
    if source_key_max is not None:
        clauses.append(f"{column} <= ?")
        params.append(source_key_max)
    return (f" WHERE {' AND '.join(clauses)}" if clauses else "", tuple(params))


def _record_from_semantic_input(
    prepared: SemanticEmbeddingInput,
    *,
    entity_type: str,
    entity_id: str,
    source_table: str,
    source_key: str,
    max_text_chars: int,
    audit: Mapping[str, Any],
) -> EmbeddingNodeInput | None:
    text = prepared.text
    hard_truncated = len(text) > max_text_chars
    if hard_truncated:
        text = text[:max_text_chars].rstrip()
    if not text:
        return None

    metadata = dict(prepared.metadata)
    if hard_truncated:
        metadata = build_embedding_metadata(
            text,
            provider_name=metadata.get("provider", ""),
            model=metadata.get("model", ""),
            dimensions=metadata.get("dimensions"),
            definition_included=bool(metadata.get("definition_included")),
        )
    metadata.update(
        {
            "batch_schema": EMBEDDING_BATCH_INPUT_SCHEMA,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "source_table": source_table,
            "source_key": source_key,
            "text_char_count": len(text),
            "max_text_chars": max_text_chars,
            **dict(audit),
        }
    )
    metadata["text_truncated"] = bool(hard_truncated or metadata.get("text_truncated"))
    return EmbeddingNodeInput(
        entity_type=entity_type,
        entity_id=entity_id,
        source_table=source_table,
        source_key=source_key,
        text=text,
        content_hash=str(metadata["content_hash"]),
        cache_key=str(metadata["cache_key"]),
        metadata=metadata,
    )


def _iter_criterion_inputs(
    conn: sqlite3.Connection,
    *,
    provider_name: str,
    model: str,
    dimensions: int,
    max_text_chars: int,
    fetch_size: int,
    source_key_min: int | None = None,
    source_key_max: int | None = None,
) -> Iterator[EmbeddingNodeInput]:
    columns = _table_columns(conn, "performance_criteria")
    if "criteria_id" not in columns:
        return
    text_expr = _text_expression(columns, "criteria_text_refined", "criteria_text_raw")
    if text_expr is None:
        return
    where, bounds = _keyset_bounds(
        source_key_min=source_key_min,
        source_key_max=source_key_max,
        column="criteria_id",
    )
    cursor = conn.execute(
        f"""
        SELECT CAST(criteria_id AS TEXT) AS source_key,
               SUBSTR({text_expr}, 1, ?) AS semantic_text,
               LENGTH({text_expr}) AS source_char_count
        FROM performance_criteria
        {where}
        ORDER BY criteria_id
        """,
        (max_text_chars + 1, *bounds),
    )
    for row in _iter_cursor_rows(cursor, fetch_size):
        source_key = str(row["source_key"])
        source_char_count = int(row["source_char_count"] or 0)
        prepared = prepare_embedding_input(
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            source_text=row["semantic_text"],
        )
        record = _record_from_semantic_input(
            prepared,
            entity_type=PERFORMANCE_CRITERION,
            entity_id=external_id("performance_criterion", source_key),
            source_table="performance_criteria",
            source_key=source_key,
            max_text_chars=max_text_chars,
            audit={
                "source_char_count": source_char_count,
                "text_truncated": source_char_count > max_text_chars,
                "definition_included": False,
            },
        )
        if record is not None:
            yield record


def _iter_element_inputs(
    conn: sqlite3.Connection,
    *,
    provider_name: str,
    model: str,
    dimensions: int,
    max_text_chars: int,
    fetch_size: int,
    source_key_min: int | None = None,
    source_key_max: int | None = None,
) -> Iterator[EmbeddingNodeInput]:
    element_columns = _table_columns(conn, "competency_elements")
    if "element_id" not in element_columns:
        return
    name_expr = _text_expression(
        element_columns, "element_name_refined", "element_name_raw"
    )
    if name_expr is None:
        name_expr = "''"

    criteria_columns = _table_columns(conn, "performance_criteria")
    criteria_text_expr = _text_expression(
        criteria_columns, "criteria_text_refined", "criteria_text_raw"
    )
    criteria_available = {
        "criteria_id",
        "element_id",
    }.issubset(criteria_columns) and criteria_text_expr is not None

    where, bounds = _keyset_bounds(
        source_key_min=source_key_min,
        source_key_max=source_key_max,
        column="e.element_id",
    )
    if criteria_available:
        cursor = conn.execute(
            f"""
            SELECT CAST(e.element_id AS TEXT) AS source_key,
                   SUBSTR({name_expr}, 1, ?) AS element_name,
                   LENGTH({name_expr}) AS element_name_chars,
                   CAST(pc.criteria_id AS TEXT) AS criteria_key,
                   SUBSTR({criteria_text_expr}, 1, ?) AS criteria_text,
                   LENGTH({criteria_text_expr}) AS criteria_text_chars
            FROM competency_elements AS e
            LEFT JOIN performance_criteria AS pc
              ON pc.element_id = e.element_id
            {where}
            ORDER BY e.element_id, pc.criteria_id
            """,
            (max_text_chars + 1, max_text_chars + 1, *bounds),
        )
    else:
        cursor = conn.execute(
            f"""
            SELECT CAST(e.element_id AS TEXT) AS source_key,
                   SUBSTR({name_expr}, 1, ?) AS element_name,
                   LENGTH({name_expr}) AS element_name_chars,
                   NULL AS criteria_key,
                   NULL AS criteria_text,
                   0 AS criteria_text_chars
            FROM competency_elements AS e
            {where}
            ORDER BY e.element_id
            """,
            (max_text_chars + 1, *bounds),
        )

    current_key: str | None = None
    accumulator: _BoundedTextAccumulator | None = None
    source_char_count = 0
    criteria_seen = 0
    criteria_included = 0

    def finish() -> EmbeddingNodeInput | None:
        if current_key is None or accumulator is None:
            return None
        prepared = prepare_embedding_input(
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            label=accumulator.text,
        )
        return _record_from_semantic_input(
            prepared,
            entity_type=PERFORMANCE_ELEMENT,
            entity_id=external_id("competency_element", current_key),
            source_table="competency_elements",
            source_key=current_key,
            max_text_chars=max_text_chars,
            audit={
                "source_char_count": source_char_count,
                "criteria_seen": criteria_seen,
                "criteria_included": criteria_included,
                "text_truncated": accumulator.truncated,
                "definition_included": False,
            },
        )

    for row in _iter_cursor_rows(cursor, fetch_size):
        row_key = str(row["source_key"])
        if row_key != current_key:
            completed = finish()
            if completed is not None:
                yield completed
            current_key = row_key
            accumulator = _BoundedTextAccumulator(max_text_chars)
            source_char_count = int(row["element_name_chars"] or 0)
            criteria_seen = 0
            criteria_included = 0
            accumulator.add(row["element_name"])

        criteria_text = normalize_semantic_text(row["criteria_text"])
        if row["criteria_key"] is not None and criteria_text:
            criteria_seen += 1
            source_char_count += int(row["criteria_text_chars"] or 0) + 1
            if accumulator is not None and accumulator.add(criteria_text):
                criteria_included += 1

    completed = finish()
    if completed is not None:
        yield completed


def _is_known_korean_boilerplate(definition: Any, concept_name: Any) -> bool:
    text = normalize_semantic_text(definition)
    name = normalize_semantic_text(concept_name)
    if name and text.startswith(name):
        remainder = text[len(name) :].lstrip()
        for separator in (":", "：", "->", "→"):
            if remainder.startswith(separator):
                text = remainder[len(separator) :].strip()
                break
    compact = text.rstrip(".。!? ")
    return any(
        compact == template.rstrip(".。!? ")
        for template in _KOREAN_BOILERPLATE_DEFINITIONS
    )


def _definition_source_is_eligible(source: Any) -> bool:
    normalized = normalize_semantic_text(source).casefold()
    if not normalized:
        return False
    return not any(
        marker in normalized for marker in _UNTRUSTED_DEFINITION_SOURCE_MARKERS
    )


def _iter_concept_inputs(
    conn: sqlite3.Connection,
    *,
    provider_name: str,
    model: str,
    dimensions: int,
    max_text_chars: int,
    fetch_size: int,
    source_key_min: int | None = None,
    source_key_max: int | None = None,
) -> Iterator[EmbeddingNodeInput]:
    columns = _table_columns(conn, "ontology_concepts")
    if not {"concept_id", "concept_name"}.issubset(columns):
        return

    optional = {
        "definition": "definition",
        "definition_source": "definition_source",
        "definition_status": "definition_status",
        "review_status": "review_status",
    }
    expressions = {
        name: (column if column in columns else "NULL")
        for name, column in optional.items()
    }
    where, bounds = _keyset_bounds(
        source_key_min=source_key_min,
        source_key_max=source_key_max,
        column="concept_id",
    )
    cursor = conn.execute(
        f"""
        SELECT CAST(concept_id AS TEXT) AS source_key,
               SUBSTR(COALESCE(concept_name, ''), 1, ?) AS concept_name,
               LENGTH(COALESCE(concept_name, '')) AS concept_name_chars,
               SUBSTR(COALESCE({expressions["definition"]}, ''), 1, ?)
                 AS definition_preview,
               LENGTH(COALESCE({expressions["definition"]}, ''))
                 AS definition_chars,
               {expressions["definition_source"]} AS definition_source,
               {expressions["definition_status"]} AS definition_status,
               {expressions["review_status"]} AS review_status
        FROM ontology_concepts
        {where}
        ORDER BY concept_id
        """,
        (max_text_chars + 1, DEFINITION_POLICY_SCAN_CHARS + 1, *bounds),
    )
    for row in _iter_cursor_rows(cursor, fetch_size):
        source_key = str(row["source_key"])
        name = row["concept_name"]
        definition = row["definition_preview"]
        source_eligible = _definition_source_is_eligible(row["definition_source"])
        definition_eligible = source_eligible and is_trusted_definition(
            definition,
            definition_status=row["definition_status"],
            review_status=row["review_status"],
            concept_name=name,
        )
        if definition_eligible and _is_known_korean_boilerplate(definition, name):
            definition_eligible = False

        prepared = prepare_embedding_input(
            provider_name=provider_name,
            model=model,
            dimensions=dimensions,
            label=name,
            concept_name=name,
            definition=definition if definition_eligible else "",
            definition_status="defined" if definition_eligible else "missing",
            review_status=row["review_status"] if definition_eligible else "raw",
        )
        definition_chars = int(row["definition_chars"] or 0)
        record = _record_from_semantic_input(
            prepared,
            entity_type=KSA_CONCEPT,
            entity_id=external_id("ontology_concept", source_key),
            source_table="ontology_concepts",
            source_key=source_key,
            max_text_chars=max_text_chars,
            audit={
                "source_char_count": int(row["concept_name_chars"] or 0)
                + (definition_chars + 1 if definition_eligible else 0),
                "definition_included": definition_eligible,
                "definition_source_eligible": source_eligible,
                "definition_source": normalize_semantic_text(row["definition_source"]),
                "text_truncated": bool(
                    int(row["concept_name_chars"] or 0) > max_text_chars
                    or (
                        definition_eligible
                        and definition_chars > DEFINITION_POLICY_SCAN_CHARS
                    )
                ),
            },
        )
        if record is not None:
            yield record


def iter_embedding_input_batches(
    db_path: str | Path,
    *,
    provider_name: str,
    model: str,
    dimensions: int,
    entity_types: Iterable[str] = SUPPORTED_ENTITY_TYPES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fetch_size: int = DEFAULT_FETCH_SIZE,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    source_key_min: int | None = None,
    source_key_max: int | None = None,
) -> Iterator[tuple[EmbeddingNodeInput, ...]]:
    """Yield deterministic semantic inputs in hard-bounded batches.

    The source connection is opened with ``mode=ro`` and ``query_only``.  A
    missing optional source table simply contributes no records.
    """

    batch_size = _bounded_int("batch_size", batch_size, upper=MAX_BATCH_SIZE)
    fetch_size = _bounded_int("fetch_size", fetch_size, upper=MAX_FETCH_SIZE)
    max_text_chars = _bounded_int(
        "max_text_chars", max_text_chars, upper=MAX_TEXT_CHARS
    )
    dimensions = _bounded_int("dimensions", dimensions, upper=MAX_EMBEDDING_DIMENSIONS)
    normalized_provider = normalize_semantic_text(provider_name)
    normalized_model = normalize_semantic_text(model)
    if not normalized_provider or not normalized_model:
        raise ValueError("provider_name and model must be non-empty")

    if isinstance(entity_types, str):
        requested = (entity_types,)
    else:
        requested = tuple(entity_types)
    requested_set = set(requested)
    unsupported = requested_set.difference(SUPPORTED_ENTITY_TYPES)
    if unsupported:
        raise ValueError(f"unsupported entity types: {sorted(unsupported)}")

    iterators = {
        PERFORMANCE_CRITERION: _iter_criterion_inputs,
        PERFORMANCE_ELEMENT: _iter_element_inputs,
        KSA_CONCEPT: _iter_concept_inputs,
    }
    batch: list[EmbeddingNodeInput] = []
    with _readonly_connection(db_path) as conn:
        for entity_type in SUPPORTED_ENTITY_TYPES:
            if entity_type not in requested_set:
                continue
            for record in iterators[entity_type](
                conn,
                provider_name=normalized_provider,
                model=normalized_model,
                dimensions=dimensions,
                max_text_chars=max_text_chars,
                fetch_size=fetch_size,
                source_key_min=source_key_min,
                source_key_max=source_key_max,
            ):
                batch.append(record)
                if len(batch) == batch_size:
                    yield tuple(batch)
                    batch.clear()
    if batch:
        yield tuple(batch)


def build_embedding_patch_records(
    inputs: Sequence[EmbeddingNodeInput],
    vectors: Sequence[Sequence[float]],
    *,
    expected_dimension: int | None = None,
) -> tuple[EmbeddingNodePatch, ...]:
    """Validate a provider response and create write-free node patch records."""

    if len(inputs) > MAX_BATCH_SIZE:
        raise ValueError(f"input batch exceeds maximum size {MAX_BATCH_SIZE}")
    if not inputs:
        if len(vectors) != 0:
            raise EmbeddingDimensionError(
                f"expected 0 vectors, received {len(vectors)}"
            )
        return ()

    metadata_dimensions = {item.metadata.get("dimensions") for item in inputs}
    if expected_dimension is None:
        if len(metadata_dimensions) != 1:
            raise EmbeddingDimensionError(
                "input metadata must declare one consistent dimension"
            )
        expected_dimension = next(iter(metadata_dimensions))
    expected_dimension = _bounded_int(
        "expected_dimension",
        expected_dimension,
        upper=MAX_EMBEDDING_DIMENSIONS,
    )
    if any(item.metadata.get("dimensions") != expected_dimension for item in inputs):
        raise EmbeddingDimensionError(
            "input metadata dimensions do not match expected_dimension"
        )

    validated = validate_embedding_batch(
        vectors,
        expected_count=len(inputs),
        expected_dimension=expected_dimension,
    )
    patches: list[EmbeddingNodePatch] = []
    for item, vector in zip(inputs, validated):
        metadata = {
            "schema": EMBEDDING_NODE_PATCH_SCHEMA,
            "source_table": item.source_table,
            "source_key": item.source_key,
            "content_hash": item.content_hash,
            "cache_key": item.cache_key,
            "provider": item.metadata.get("provider"),
            "model": item.metadata.get("model"),
            "dimensions": expected_dimension,
            "text_char_count": item.metadata.get("text_char_count"),
            "text_truncated": bool(item.metadata.get("text_truncated")),
            "definition_included": bool(item.metadata.get("definition_included")),
            "db_writes": False,
            "neo4j_writes": False,
        }
        patches.append(
            EmbeddingNodePatch(
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                embedding=vector,
                content_hash=item.content_hash,
                cache_key=item.cache_key,
                provider=str(item.metadata.get("provider") or ""),
                model=str(item.metadata.get("model") or ""),
                dimensions=expected_dimension,
                metadata=metadata,
            )
        )
    return tuple(patches)


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_FETCH_SIZE",
    "DEFAULT_MAX_TEXT_CHARS",
    "EMBEDDING_BATCH_INPUT_SCHEMA",
    "EMBEDDING_NODE_PATCH_SCHEMA",
    "EmbeddingNodeInput",
    "EmbeddingNodePatch",
    "KSA_CONCEPT",
    "MAX_BATCH_SIZE",
    "MAX_EMBEDDING_DIMENSIONS",
    "MAX_FETCH_SIZE",
    "MAX_TEXT_CHARS",
    "PERFORMANCE_CRITERION",
    "PERFORMANCE_ELEMENT",
    "SUPPORTED_ENTITY_TYPES",
    "build_embedding_patch_records",
    "iter_embedding_input_batches",
]
