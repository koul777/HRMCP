"""Read-only, dependency-injected queries for the Gold LPG Neo4j projection.

This module deliberately does not import the Neo4j driver or create a network
connection.  An application supplies the driver's ``execute_query`` method
(or a compatible callable) after it has configured credentials and a read-only
routing policy.  The public methods use only fixed, reviewed Cypher templates
and return a deliberately small public-safe projection of result records.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import math
from typing import Any


MAX_TOP_K = 100
MAX_RESULT_LIMIT = 250
MAX_GRAPH_HOPS = 4
MAX_JOB_KSA_SUMMARY_HOPS = 2
# The compact role->job->KSA surface is intentionally bounded, but should
# retain the strongest aggregated evidence before deterministic tie-breakers.
MAX_JOB_KSA_SUMMARY_LIMIT = 250
VECTOR_SEARCH_CURRENT = "search_clause"
VECTOR_SEARCH_LEGACY = "legacy_procedure"
VECTOR_SEARCH_CAPABILITIES = frozenset(
    {VECTOR_SEARCH_CURRENT, VECTOR_SEARCH_LEGACY}
)

# These identifiers are intentionally duplicated as a serving allowlist rather
# than accepted from callers.  SEARCH requires an identifier (not a parameter),
# while the legacy procedure accepts the corresponding fixed parameter value.
VECTOR_INDEX_ALLOWLIST = {
    "performance_criterion": "ncs_lpg_performance_criterion_embedding",
    "performance_element": "ncs_lpg_performance_element_embedding",
    "ksa_concept": "ncs_lpg_ksa_concept_embedding",
}
VECTOR_RETRIEVAL_METHODS = frozenset(
    f"vector_{capability}_{entity_kind}_graph_expansion"
    for capability in VECTOR_SEARCH_CAPABILITIES
    for entity_kind in VECTOR_INDEX_ALLOWLIST
)


class Neo4jGoldError(RuntimeError):
    """Base error for the optional Gold LPG serving adapter."""


class Neo4jGoldValidationError(Neo4jGoldError, ValueError):
    """Raised before I/O when a caller provides an unsafe query input."""


class Neo4jGoldUnavailableError(Neo4jGoldError):
    """A backend failure that callers can handle by falling back to SQLite."""


# InternalJobRole is a future authorised source in the Gold LPG.  Querying it
# is nevertheless safe now: an empty graph simply produces no rows.
INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER = """
MATCH (role:LpgNode:InternalJobRole {id: $internal_role_id})
      -[:ALIGNED_TO]->(job:LpgNode:NCSJob)
      -[:REQUIRES_UNIT]->(unit:LpgNode:CompetencyUnit)
      -[:DEFINED_BY]->(element:LpgNode:PerformanceElement)
OPTIONAL MATCH (element)-[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]
      ->(ksa:LpgNode:KSAConcept)
RETURN role.id AS internal_job_role_id,
       role.display_name AS internal_job_role_name,
       job.id AS ncs_job_id,
       job.name AS ncs_job_name,
       unit.unit_code AS competency_unit_code,
       unit.name AS competency_unit_name,
       element.element_id AS performance_element_id,
       element.name AS performance_element_name,
       ksa.concept_id AS ksa_concept_id,
       ksa.name AS ksa_concept_name,
       ksa.concept_type AS ksa_concept_type,
       coalesce(element.projection_fingerprint, unit.projection_fingerprint) AS projection_fingerprint,
       coalesce(element.projection_freshness, unit.projection_freshness) AS projection_freshness,
       coalesce(element.projection_version, unit.projection_version) AS projection_version
ORDER BY ncs_job_id ASC, competency_unit_code ASC, performance_element_id ASC,
         ksa_concept_id ASC
LIMIT $limit
""".strip()


INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER = """
MATCH (role:LpgNode:InternalJobRole {id: $internal_role_id})
      -[:ALIGNED_TO]->(job:LpgNode:NCSJob)
      -[summary:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]
      ->(ksa:LpgNode:KSAConcept)
WHERE summary.summary_scope = 'ncs_job_to_ksa'
RETURN role.id AS internal_job_role_id,
       role.display_name AS internal_job_role_name,
       job.id AS ncs_job_id,
       job.name AS ncs_job_name,
       ksa.concept_id AS ksa_concept_id,
       ksa.name AS ksa_concept_name,
       ksa.concept_type AS ksa_concept_type,
       summary.source_link_count AS source_link_count,
       summary.distinct_unit_count AS distinct_unit_count,
       summary.distinct_element_count AS distinct_element_count,
       summary.distinct_criteria_count AS distinct_criteria_count,
       summary.status_distribution_json AS status_distribution_json,
       summary.method_distribution_json AS method_distribution_json,
       coalesce(job.projection_fingerprint, ksa.projection_fingerprint) AS projection_fingerprint,
       coalesce(job.projection_freshness, ksa.projection_freshness) AS projection_freshness,
       coalesce(job.projection_version, ksa.projection_version) AS projection_version
ORDER BY source_link_count DESC, distinct_criteria_count DESC,
         ncs_job_id ASC, ksa_concept_id ASC
LIMIT $limit
""".strip()


def _vector_expansion_tail(seed_return_fields: str, *, seed_is_element: bool) -> str:
    """Build a static query tail from a module-owned label/return fragment."""

    element_binding = (
        "WITH seed, seed AS element, score"
        if seed_is_element
        else "OPTIONAL MATCH (element:LpgNode:PerformanceElement)-[:HAS_CRITERION]->(seed)"
    )
    ksa_source = "element" if seed_is_element else "seed"
    return f"""
{element_binding}
OPTIONAL MATCH (unit:LpgNode:CompetencyUnit)-[:DEFINED_BY]->(element)
OPTIONAL MATCH (job:LpgNode:NCSJob)-[:REQUIRES_UNIT]->(unit)
OPTIONAL MATCH (role:LpgNode:InternalJobRole)-[:ALIGNED_TO]->(job)
OPTIONAL MATCH ({ksa_source})-[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]
      ->(ksa:LpgNode:KSAConcept)
WITH seed, element, unit, job, role, score,
     collect(DISTINCT CASE WHEN ksa.concept_type = 'knowledge' THEN ksa.name END)
       AS knowledge_values,
     collect(DISTINCT CASE WHEN ksa.concept_type = 'skill' THEN ksa.name END)
       AS skill_values,
     collect(DISTINCT CASE WHEN ksa.concept_type = 'attitude' THEN ksa.name END)
       AS attitude_values,
     count(DISTINCT ksa) AS ksa_concept_count
WITH seed, element, unit, job, role, score,
     knowledge_values[..8] AS required_knowledge,
     skill_values[..8] AS required_skills,
     attitude_values[..8] AS required_attitudes,
     size(knowledge_values) AS knowledge_count,
     size(skill_values) AS skill_count,
     size(attitude_values) AS attitude_count,
     ksa_concept_count,
     (size(knowledge_values) > 8 OR size(skill_values) > 8 OR size(attitude_values) > 8)
       AS ksa_lists_truncated
RETURN {seed_return_fields},
       role.id AS internal_job_role_id,
       role.display_name AS internal_job_role_name,
       job.id AS ncs_job_id,
       job.name AS ncs_job_name,
       unit.unit_code AS competency_unit_code,
       unit.name AS competency_unit_name,
       element.element_id AS performance_element_id,
       element.name AS performance_element_name,
       required_knowledge,
       required_skills,
       required_attitudes,
       knowledge_count,
       skill_count,
       attitude_count,
       ksa_concept_count,
       ksa_lists_truncated,
       coalesce(seed.projection_fingerprint, element.projection_fingerprint, unit.projection_fingerprint) AS projection_fingerprint,
       coalesce(seed.projection_freshness, element.projection_freshness, unit.projection_freshness) AS projection_freshness,
       coalesce(seed.projection_version, element.projection_version, unit.projection_version) AS projection_version
ORDER BY score DESC, ncs_job_id ASC, competency_unit_code ASC,
         performance_element_id ASC, performance_criterion_id ASC
LIMIT $limit
""".strip()


def _ksa_vector_expansion_tail() -> str:
    """Expand one KSA seed back to its bounded task, unit, job, and role context."""

    return """
OPTIONAL MATCH (element:LpgNode:PerformanceElement)
      -[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]->(seed)
OPTIONAL MATCH (unit:LpgNode:CompetencyUnit)-[:DEFINED_BY]->(element)
OPTIONAL MATCH (job_from_element:LpgNode:NCSJob)-[:REQUIRES_UNIT]->(unit)
OPTIONAL MATCH (job_direct:LpgNode:NCSJob)
      -[:REQUIRES_KNOWLEDGE|REQUIRES_SKILL|REQUIRES_ATTITUDE|REQUIRES_KSA]->(seed)
WITH seed, score, element, unit, coalesce(job_from_element, job_direct) AS job
OPTIONAL MATCH (role:LpgNode:InternalJobRole)-[:ALIGNED_TO]->(job)
RETURN NULL AS performance_criterion_id,
       NULL AS performance_criterion_text,
       score,
       role.id AS internal_job_role_id,
       role.display_name AS internal_job_role_name,
       job.id AS ncs_job_id,
       job.name AS ncs_job_name,
       unit.unit_code AS competency_unit_code,
       unit.name AS competency_unit_name,
       element.element_id AS performance_element_id,
       element.name AS performance_element_name,
       seed.concept_id AS ksa_concept_id,
       seed.name AS ksa_concept_name,
       seed.concept_type AS ksa_concept_type,
       coalesce(seed.projection_fingerprint, element.projection_fingerprint, unit.projection_fingerprint) AS projection_fingerprint,
       coalesce(seed.projection_freshness, element.projection_freshness, unit.projection_freshness) AS projection_freshness,
       coalesce(seed.projection_version, element.projection_version, unit.projection_version) AS projection_version
ORDER BY score DESC, ncs_job_id ASC, competency_unit_code ASC,
         performance_element_id ASC, ksa_concept_id ASC
LIMIT $limit
""".strip()


# Cypher 25 / Neo4j 2026.01+ SEARCH templates.  Index identifiers cannot be
# Cypher parameters, so each allowlisted index has its own precompiled query.
CURRENT_VECTOR_PERFORMANCE_CRITERION_CYPHER = (
    """
MATCH (seed:LpgNode:PerformanceCriterion)
SEARCH seed IN (
  VECTOR INDEX ncs_lpg_performance_criterion_embedding
  FOR $embedding
  LIMIT $top_k
) SCORE AS score
""".strip()
    + "\n"
    + _vector_expansion_tail(
        "seed.criteria_id AS performance_criterion_id, seed.text AS performance_criterion_text, score",
        seed_is_element=False,
    )
)
CURRENT_VECTOR_PERFORMANCE_ELEMENT_CYPHER = (
    """
MATCH (seed:LpgNode:PerformanceElement)
SEARCH seed IN (
  VECTOR INDEX ncs_lpg_performance_element_embedding
  FOR $embedding
  LIMIT $top_k
) SCORE AS score
""".strip()
    + "\n"
    + _vector_expansion_tail(
        "NULL AS performance_criterion_id, NULL AS performance_criterion_text, score",
        seed_is_element=True,
    )
)
CURRENT_VECTOR_KSA_CONCEPT_CYPHER = (
    """
MATCH (seed:LpgNode:KSAConcept)
SEARCH seed IN (
  VECTOR INDEX ncs_lpg_ksa_concept_embedding
  FOR $embedding
  LIMIT $top_k
) SCORE AS score
""".strip()
    + "\n"
    + _ksa_vector_expansion_tail()
)

# Works with Neo4j versions predating the SEARCH clause.  The index-name value
# is still selected by ``VECTOR_INDEX_ALLOWLIST`` before execution.
LEGACY_VECTOR_PERFORMANCE_CRITERION_CYPHER = (
    """
CALL db.index.vector.queryNodes($index_name, $top_k, $embedding)
YIELD node AS seed, score
WITH seed, score
WHERE seed:LpgNode AND seed:PerformanceCriterion
""".strip()
    + "\n"
    + _vector_expansion_tail(
        "seed.criteria_id AS performance_criterion_id, seed.text AS performance_criterion_text, score",
        seed_is_element=False,
    )
)
LEGACY_VECTOR_PERFORMANCE_ELEMENT_CYPHER = (
    """
CALL db.index.vector.queryNodes($index_name, $top_k, $embedding)
YIELD node AS seed, score
WITH seed, score
WHERE seed:LpgNode AND seed:PerformanceElement
""".strip()
    + "\n"
    + _vector_expansion_tail(
        "NULL AS performance_criterion_id, NULL AS performance_criterion_text, score",
        seed_is_element=True,
    )
)
LEGACY_VECTOR_KSA_CONCEPT_CYPHER = (
    """
CALL db.index.vector.queryNodes($index_name, $top_k, $embedding)
YIELD node AS seed, score
WITH seed, score
WHERE seed:LpgNode AND seed:KSAConcept
""".strip()
    + "\n"
    + _ksa_vector_expansion_tail()
)

CURRENT_VECTOR_SEARCH_CYPHER = {
    "performance_criterion": CURRENT_VECTOR_PERFORMANCE_CRITERION_CYPHER,
    "performance_element": CURRENT_VECTOR_PERFORMANCE_ELEMENT_CYPHER,
    "ksa_concept": CURRENT_VECTOR_KSA_CONCEPT_CYPHER,
}
LEGACY_VECTOR_SEARCH_CYPHER = {
    "performance_criterion": LEGACY_VECTOR_PERFORMANCE_CRITERION_CYPHER,
    "performance_element": LEGACY_VECTOR_PERFORMANCE_ELEMENT_CYPHER,
    "ksa_concept": LEGACY_VECTOR_KSA_CONCEPT_CYPHER,
}

# Aliases make the two user-facing query patterns obvious to integration code.
ROLE_ALIGNMENT_SUBGRAPH_CYPHER = INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER
ROLE_ALIGNMENT_JOB_KSA_SUMMARY_CYPHER = INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER
VECTOR_GRAPH_EXPANSION_CYPHER = CURRENT_VECTOR_SEARCH_CYPHER


_PUBLIC_ROW_FIELDS = (
    "internal_job_role_id", "internal_job_role_name", "ncs_job_id",
    "ncs_job_name", "competency_unit_code", "competency_unit_name",
    "performance_element_id", "performance_element_name",
    "performance_criterion_id", "performance_criterion_text",
    "ksa_concept_id", "ksa_concept_name", "ksa_concept_type",
    "required_knowledge", "required_skills", "required_attitudes",
    "knowledge_count", "skill_count", "attitude_count", "ksa_concept_count",
    "ksa_lists_truncated",
    "source_link_count", "distinct_unit_count", "distinct_element_count",
    "distinct_criteria_count", "status_distribution_json", "method_distribution_json",
    "projection_fingerprint", "projection_freshness", "projection_version",
)


def _positive_int(value: object, *, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise Neo4jGoldValidationError(f"{name} must be an integer between 1 and {maximum}")
    return value


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Neo4jGoldValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _safe_public_value(
    value: object,
) -> str | int | float | bool | None | list[str | int | float | bool | None]:
    """Keep bounded JSON values from the intentionally narrow RETURN list."""

    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (list, tuple)):
        cleaned: list[str | int | float | bool | None] = []
        for item in value[:20]:
            if item is None or isinstance(item, (str, int, bool)):
                cleaned.append(item)
            elif isinstance(item, float):
                cleaned.append(item if math.isfinite(item) else None)
            else:
                cleaned.append(str(item))
        return cleaned
    return str(value)


def _sort_count(value: object) -> float:
    """Return a safe evidence count for descending deterministic ordering."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value) if math.isfinite(value) else 0.0


def _record_mapping(record: object) -> Mapping[str, Any]:
    try:
        data = getattr(record, "data", None)
        # ``neo4j.Record`` advertises the Mapping protocol, but membership
        # checks use tuple/value semantics (``"alias" in record`` is false).
        # Always materialise driver records through ``data()`` so the public
        # allowlist below sees the Cypher aliases instead of dropping every
        # field except the separately-read score.
        if callable(data):
            candidate = data()
        elif isinstance(record, Mapping):
            candidate = dict(record)
        else:
            candidate = dict(record)  # type: ignore[arg-type]
    except Exception:
        raise Neo4jGoldUnavailableError("Neo4j returned an unsupported record shape") from None
    if not isinstance(candidate, Mapping):
        raise Neo4jGoldUnavailableError("Neo4j returned an unsupported record shape") from None
    return dict(candidate)


def _records_from_execute_result(result: object) -> Sequence[object]:
    # neo4j.Driver.execute_query returns (records, summary, keys).  Supporting
    # a bare sequence also keeps this adapter simple to test without a driver.
    records = getattr(result, "records", None)
    if records is not None:
        result = records
    elif isinstance(result, tuple) and result:
        result = result[0]
    if isinstance(result, (str, bytes)) or not isinstance(result, Sequence):
        raise Neo4jGoldUnavailableError("Neo4j returned an unsupported result shape")
    return result


class Neo4jGoldReadClient:
    """Execute only the fixed Gold LPG read contracts through an injected callable."""

    def __init__(
        self,
        execute_query: Callable[..., Any],
        *,
        database: str,
        embedding_dimensions: int,
        vector_search_capability: str = VECTOR_SEARCH_CURRENT,
    ) -> None:
        if not callable(execute_query):
            raise Neo4jGoldValidationError("execute_query must be callable")
        self._database = _required_text(database, name="database")
        self._embedding_dimensions = _positive_int(
            embedding_dimensions, name="embedding_dimensions", maximum=4_096
        )
        if vector_search_capability not in VECTOR_SEARCH_CAPABILITIES:
            allowed = ", ".join(sorted(VECTOR_SEARCH_CAPABILITIES))
            raise Neo4jGoldValidationError(
                f"vector_search_capability must be one of: {allowed}"
            )
        self._execute_query = execute_query
        self._vector_search_capability = vector_search_capability

    @property
    def database(self) -> str:
        return self._database

    @property
    def embedding_dimensions(self) -> int:
        return self._embedding_dimensions

    def internal_role_subgraph(
        self, internal_role_id: str, *, limit: int = 100, max_hops: int = MAX_GRAPH_HOPS
    ) -> dict[str, Any]:
        role_id = _required_text(internal_role_id, name="internal_role_id")
        return self._run(
            INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER,
            {"internal_role_id": role_id, "limit": _positive_int(limit, name="limit", maximum=MAX_RESULT_LIMIT)},
            retrieval_method="role_alignment_subgraph",
            limit=limit,
            max_hops=max_hops,
        )

    def internal_role_job_ksa_summary(
        self,
        internal_role_id: str,
        *,
        limit: int = 50,
        max_hops: int = MAX_JOB_KSA_SUMMARY_HOPS,
    ) -> dict[str, Any]:
        """Return the bounded two-hop InternalJobRole -> NCSJob -> KSA summary."""

        role_id = _required_text(internal_role_id, name="internal_role_id")
        bounded_limit = _positive_int(limit, name="limit", maximum=MAX_JOB_KSA_SUMMARY_LIMIT)
        return self._run(
            INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER,
            {"internal_role_id": role_id, "limit": bounded_limit},
            retrieval_method="role_alignment_job_ksa_summary",
            limit=bounded_limit,
            max_hops=max_hops,
            graph_depth=MAX_JOB_KSA_SUMMARY_HOPS,
        )

    def vector_graph_expansion(
        self,
        embedding: Sequence[float],
        *,
        entity_kind: str = "performance_criterion",
        top_k: int = 10,
        limit: int = 50,
        max_hops: int = MAX_GRAPH_HOPS,
    ) -> dict[str, Any]:
        if entity_kind not in VECTOR_INDEX_ALLOWLIST:
            raise Neo4jGoldValidationError(
                "entity_kind must be one of: " + ", ".join(sorted(VECTOR_INDEX_ALLOWLIST))
            )
        validated_embedding = self._validate_embedding(embedding)
        bounded_top_k = _positive_int(top_k, name="top_k", maximum=MAX_TOP_K)
        bounded_limit = _positive_int(limit, name="limit", maximum=MAX_RESULT_LIMIT)
        self._validate_hops(max_hops)
        templates = (
            CURRENT_VECTOR_SEARCH_CYPHER
            if self._vector_search_capability == VECTOR_SEARCH_CURRENT
            else LEGACY_VECTOR_SEARCH_CYPHER
        )
        parameters: dict[str, Any] = {
            "embedding": validated_embedding,
            "top_k": bounded_top_k,
            "limit": bounded_limit,
        }
        if self._vector_search_capability == VECTOR_SEARCH_LEGACY:
            parameters["index_name"] = VECTOR_INDEX_ALLOWLIST[entity_kind]
        return self._run(
            templates[entity_kind],
            parameters,
            retrieval_method=(
                f"vector_{self._vector_search_capability}_{entity_kind}_graph_expansion"
            ),
            limit=bounded_limit,
            max_hops=max_hops,
            top_k=bounded_top_k,
        )

    # Focused convenience names are friendlier for callers that know the seed type.
    def search_performance_criteria(self, embedding: Sequence[float], **kwargs: Any) -> dict[str, Any]:
        return self.vector_graph_expansion(embedding, entity_kind="performance_criterion", **kwargs)

    def search_performance_elements(self, embedding: Sequence[float], **kwargs: Any) -> dict[str, Any]:
        return self.vector_graph_expansion(embedding, entity_kind="performance_element", **kwargs)

    def search_ksa_concepts(self, embedding: Sequence[float], **kwargs: Any) -> dict[str, Any]:
        return self.vector_graph_expansion(embedding, entity_kind="ksa_concept", **kwargs)

    # Explicit retrieval aliases keep orchestration code readable without
    # widening the query surface beyond the methods above.
    retrieve_internal_role_subgraph = internal_role_subgraph
    retrieve_internal_role_job_ksa_summary = internal_role_job_ksa_summary
    search_vector = vector_graph_expansion

    def _validate_embedding(self, embedding: Sequence[float]) -> list[float]:
        if isinstance(embedding, (str, bytes)) or not isinstance(embedding, Sequence):
            raise Neo4jGoldValidationError("embedding must be a sequence of finite numbers")
        if len(embedding) != self._embedding_dimensions:
            raise Neo4jGoldValidationError(
                f"embedding must have {self._embedding_dimensions} dimensions, got {len(embedding)}"
            )
        values: list[float] = []
        for index, value in enumerate(embedding):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise Neo4jGoldValidationError(f"embedding[{index}] must be a finite number")
            values.append(float(value))
        return values

    @staticmethod
    def _validate_hops(max_hops: int, *, expected: int = MAX_GRAPH_HOPS) -> None:
        if max_hops != expected:
            raise Neo4jGoldValidationError(
                f"max_hops is fixed at {expected} for the reviewed graph path"
            )

    def _run(
        self,
        cypher: str,
        parameters: Mapping[str, Any],
        *,
        retrieval_method: str,
        limit: int,
        max_hops: int,
        top_k: int | None = None,
        graph_depth: int = MAX_GRAPH_HOPS,
    ) -> dict[str, Any]:
        self._validate_hops(max_hops, expected=graph_depth)
        try:
            # These are the Neo4j driver's keyword spellings.  ``"r"`` is the
            # dependency-free read-routing value accepted by the official driver.
            result = self._execute_query(
                cypher,
                parameters_=dict(parameters),
                database_=self._database,
                routing_="r",
            )
        except Neo4jGoldError:
            raise
        except Exception:  # Backend specifics must not leak to callers.
            raise Neo4jGoldUnavailableError("Gold LPG read backend is unavailable") from None

        normalized_rows = [
            self._normalize_row(_record_mapping(record), retrieval_method)
            for record in _records_from_execute_result(result)
        ]
        self._sort_rows(normalized_rows, retrieval_method)
        audit: dict[str, Any] = {
            "retrieval_method": retrieval_method,
            "row_count": len(normalized_rows),
            "read_only": True,
            "db_writes": False,
            "graph_depth": graph_depth,
            "limit": limit,
        }
        if top_k is not None:
            audit["top_k"] = top_k
        for key in ("projection_fingerprint", "projection_freshness", "projection_version"):
            values = sorted({str(row[key]) for row in normalized_rows if row.get(key) is not None})
            if values:
                audit[key] = values[0] if len(values) == 1 else values
        return {"rows": normalized_rows, "audit": audit}

    @staticmethod
    def _sort_rows(rows: list[dict[str, Any]], retrieval_method: str) -> None:
        """Apply the fixed query contract's deterministic public row order."""

        def stable_ids(row: Mapping[str, Any]) -> tuple[str, str, str, str, str, str]:
            return (
                str(row.get("internal_job_role_id") or ""),
                str(row.get("ncs_job_id") or ""),
                str(row.get("competency_unit_code") or ""),
                str(row.get("performance_element_id") or ""),
                str(row.get("performance_criterion_id") or ""),
                str(row.get("ksa_concept_id") or ""),
            )

        if retrieval_method in VECTOR_RETRIEVAL_METHODS:
            rows.sort(key=lambda row: (-float(row["score"]), *stable_ids(row)))
        elif retrieval_method == "role_alignment_job_ksa_summary":
            rows.sort(key=lambda row: (
                -_sort_count(row.get("source_link_count")),
                -_sort_count(row.get("distinct_criteria_count")),
                *stable_ids(row),
            ))
        else:
            rows.sort(key=stable_ids)

    @staticmethod
    def _normalize_row(record: Mapping[str, Any], retrieval_method: str) -> dict[str, Any]:
        row = {
            key: _safe_public_value(record[key])
            for key in _PUBLIC_ROW_FIELDS
            if key in record
        }
        score = record.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            score = 0.0
        row["score"] = float(score)
        row["retrieval_method"] = retrieval_method
        return row


# Short alias for applications that describe this layer as an adapter.
Neo4jGoldAdapter = Neo4jGoldReadClient


__all__ = [
    "CURRENT_VECTOR_PERFORMANCE_CRITERION_CYPHER",
    "CURRENT_VECTOR_PERFORMANCE_ELEMENT_CYPHER",
    "CURRENT_VECTOR_KSA_CONCEPT_CYPHER",
    "CURRENT_VECTOR_SEARCH_CYPHER",
    "INTERNAL_ROLE_NCS_JOB_KSA_SUMMARY_CYPHER",
    "INTERNAL_ROLE_NCS_KSA_SUBGRAPH_CYPHER",
    "LEGACY_VECTOR_PERFORMANCE_CRITERION_CYPHER",
    "LEGACY_VECTOR_PERFORMANCE_ELEMENT_CYPHER",
    "LEGACY_VECTOR_KSA_CONCEPT_CYPHER",
    "LEGACY_VECTOR_SEARCH_CYPHER",
    "MAX_GRAPH_HOPS",
    "MAX_JOB_KSA_SUMMARY_HOPS",
    "MAX_JOB_KSA_SUMMARY_LIMIT",
    "MAX_RESULT_LIMIT",
    "MAX_TOP_K",
    "Neo4jGoldAdapter",
    "Neo4jGoldError",
    "Neo4jGoldReadClient",
    "Neo4jGoldUnavailableError",
    "Neo4jGoldValidationError",
    "ROLE_ALIGNMENT_SUBGRAPH_CYPHER",
    "ROLE_ALIGNMENT_JOB_KSA_SUMMARY_CYPHER",
    "VECTOR_GRAPH_EXPANSION_CYPHER",
    "VECTOR_INDEX_ALLOWLIST",
    "VECTOR_SEARCH_CAPABILITIES",
    "VECTOR_SEARCH_CURRENT",
    "VECTOR_SEARCH_LEGACY",
]
