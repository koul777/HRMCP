"""Read-only SQLite projection for loading the NCS graph into a Gold LPG.

This module intentionally has no Neo4j driver dependency.  It produces a
deterministic, JSON-serialisable node/edge bundle from the prepared SQLite
database, plus Cypher templates that a separately authorised loader may use.
The source database is always opened with ``mode=ro`` and ``query_only``.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from ncs_mcp.internal_job_roles import (
    ContractValidationError,
    InternalJobRole,
    RoleAlignmentCandidate,
    validate_alignment_candidate,
    validate_internal_job_role,
)
from ncs_mcp.gold_readiness import GoldProjectionProfile, SERVING_CORE_PROFILE


GOLD_LPG_SCHEMA = "ncs_gold_lpg_projection_v2"
TRUSTED_REVIEW_STATUSES = frozenset({"human_reviewed", "accepted", "reviewed"})
FULL_FIDELITY_PROFILE = GoldProjectionProfile(
    name="full_fidelity",
    include_task_ksa_detailed_relations=True,
)

# Import statements are generated only from these module-owned allowlists.
# Native dynamic labels/types would require APOC or query-string construction,
# neither of which is appropriate for a portable parameterised loader.
ALLOWED_NODE_IMPORT_GROUPS = {
    "NCSJobCategory": "NCSJobCategory",
    "NCSJob": "NCSJob",
    "CompetencyUnit": "CompetencyUnit",
    "PerformanceElement": "PerformanceElement",
    "PerformanceCriterion": "PerformanceCriterion",
    "PerformanceCriterionTask": "PerformanceCriterion:Task",
    "Task": "Task",
    "KSAConceptKnowledge": "KSAConcept:Knowledge",
    "KSAConceptSkill": "KSAConcept:Skill",
    "KSAConceptAttitude": "KSAConcept:Attitude",
    "KSAConcept": "KSAConcept",
    "TrainingCourse": "TrainingCourse",
    "TrainingDelivery": "TrainingDelivery",
    # The source DB has no internal-role table yet.  The template is exposed
    # for a future authorised source adapter; this projection emits no rows.
    "InternalJobRole": "InternalJobRole",
}
ALLOWED_RELATIONSHIP_TYPES = frozenset({
    "HAS_SUB_CATEGORY", "HAS_NCS_JOB", "REQUIRES_UNIT", "HAS_ELEMENT",
    "HAS_CRITERION", "DEFINED_BY", "REQUIRES_KNOWLEDGE", "REQUIRES_SKILL",
    "REQUIRES_ATTITUDE", "REQUIRES_KSA", "TASK_KSA_CONCEPT_RELATION",
    "COURSE_COVERS_UNIT", "COURSE_COVERS_CONCEPT", "COURSE_COVERS_ELEMENT",
    "COURSE_GOAL_COVERS_CONCEPT", "COURSE_HAS_DELIVERY", "ALIGNED_TO",
    "REPRESENTS_TASK",
})
VECTOR_INDEX_SPECS = {
    "ncs_lpg_performance_criterion_embedding": ("PerformanceCriterion", "embedding"),
    "ncs_lpg_performance_element_embedding": ("PerformanceElement", "embedding"),
    "ncs_lpg_ksa_concept_embedding": ("KSAConcept", "embedding"),
}


def neo4j_vector_index_ddl(dimensions: int) -> tuple[str, ...]:
    """Build fixed-label cosine vector-index DDL without ingesting vectors.

    The caller controls only a validated Neo4j-supported 1--4096 dimensionality;
    index names, labels, properties, and similarity function are all allowlisted.
    """

    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or not 1 <= dimensions <= 4096
    ):
        raise ValueError("dimensions must be an integer between 1 and 4096")
    return tuple(
        "CREATE VECTOR INDEX "
        f"{index_name} IF NOT EXISTS FOR (node:{label}) ON (node.{property_name}) "
        "OPTIONS {indexConfig: {`vector.dimensions`: "
        f"{dimensions}, `vector.similarity_function`: 'cosine'}}}}"
        for index_name, (label, property_name) in VECTOR_INDEX_SPECS.items()
    )


def _node_merge_template(label_expression: str) -> str:
    remove_stale_ksa_subtypes = ""
    if label_expression.startswith("KSAConcept"):
        remove_stale_ksa_subtypes = "REMOVE node:Knowledge:Skill:Attitude\n"
    return f"""
UNWIND $nodes AS row
MERGE (node:LpgNode {{id: row.id}})
SET node = row.properties,
    node.labels = row.labels,
    node.source_table = row.provenance.source_table,
    node.source_key = row.provenance.source_key,
    node.source_review_status = row.provenance.review_status
{remove_stale_ksa_subtypes}SET node:{label_expression}
""".strip()


def _edge_merge_template(relationship_type: str) -> str:
    return f"""
UNWIND $edges AS row
MATCH (source:LpgNode {{id: row.source}})
MATCH (target:LpgNode {{id: row.target}})
MERGE (source)-[edge:{relationship_type} {{id: row.id}}]->(target)
SET edge = row.properties,
    edge.edge_type = row.type,
    edge.source_table = row.provenance.source_table,
    edge.source_key = row.provenance.source_key,
    edge.source_review_status = row.provenance.review_status
""".strip()


NEO4J_DOMAIN_NODE_MERGE_CYPHER = {
    group: _node_merge_template(labels)
    for group, labels in ALLOWED_NODE_IMPORT_GROUPS.items()
}
NEO4J_DOMAIN_EDGE_MERGE_CYPHER = {
    relationship_type: _edge_merge_template(relationship_type)
    for relationship_type in sorted(ALLOWED_RELATIONSHIP_TYPES)
}

NEO4J_SCHEMA_DDL = (
    "CREATE CONSTRAINT ncs_lpg_node_id_unique IF NOT EXISTS "
    "FOR (node:LpgNode) REQUIRE node.id IS UNIQUE",
    "CREATE INDEX ncs_lpg_node_type IF NOT EXISTS "
    "FOR (node:LpgNode) ON (node.node_type)",
) + tuple(
    "CREATE CONSTRAINT ncs_lpg_edge_"
    f"{relationship_type.lower()}_id_unique IF NOT EXISTS "
    f"FOR ()-[edge:{relationship_type}]-() REQUIRE edge.id IS UNIQUE"
    for relationship_type in sorted(ALLOWED_RELATIONSHIP_TYPES)
)

# Singular examples are retained for callers that need one query string; the
# import plan below uses every grouped template and never uses a generic label
# or relationship type.
NEO4J_NODE_MERGE_CYPHER = NEO4J_DOMAIN_NODE_MERGE_CYPHER["NCSJobCategory"]
NEO4J_EDGE_MERGE_CYPHER = NEO4J_DOMAIN_EDGE_MERGE_CYPHER["HAS_SUB_CATEGORY"]


def external_id(entity_type: str, source_key: object) -> str:
    """Return an opaque-safe, deterministic LPG identifier.

    The source key is retained in the identifier (URL escaped) so audit users
    can trace a node without a separate lookup.  Callers should use source
    primary keys rather than display text.
    """

    normalized_type = quote(str(entity_type).strip().lower(), safe="-_")
    normalized_key = quote(str(source_key).strip(), safe="-_.~")
    return f"ncs:{normalized_type}:{normalized_key}"


def _edge_id(edge_type: str, source_table: str, source_key: object) -> str:
    return external_id("edge", f"{edge_type}:{source_table}:{source_key}")


def _text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _value(row: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    return None


def _status(row: Mapping[str, Any]) -> str | None:
    return _text(_value(row, "review_status", "link_status"))


def _source_key(row: Mapping[str, Any], *candidates: str) -> str | None:
    value = _value(row, *candidates)
    return _text(value)


def _complete_ncs_job_code(levels: Iterable[tuple[str, str | None, Any]]) -> str | None:
    """Return a 세분류 job code only for a contiguous four-level NCS path."""

    codes = [_text(code) for _, code, _ in levels]
    if len(codes) != 4 or any(code is None for code in codes):
        return None
    return "".join(code for code in codes if code is not None)


def _provenance(table: str, key: object, row: Mapping[str, Any]) -> dict[str, Any]:
    provenance: dict[str, Any] = {"source_table": table, "source_key": str(key)}
    status = _status(row)
    if status is not None:
        provenance["review_status"] = status
    return provenance


def _is_trusted_definition(row: Mapping[str, Any]) -> bool:
    """Accept only human-trusted, non-boilerplate definitions as semantic text."""

    definition = _text(row.get("definition"))
    if not definition:
        return False
    if _text(row.get("definition_status")) != "defined":
        return False
    if (_text(row.get("review_status")) or "").lower() not in TRUSTED_REVIEW_STATUSES:
        return False
    source = (_text(row.get("definition_source")) or "").lower()
    if "boilerplate" in source:
        return False
    boilerplate_markers = (
        "업무 판단과 문제 해결에 필요한 관련 원리",
        "업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력",
        "업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도",
    )
    return not any(marker in definition for marker in boilerplate_markers)


@contextmanager
def _readonly_connection(db_path: str | Path) -> Iterable[sqlite3.Connection]:
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    names = [
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    ]
    return {
        name: {str(column[1]) for column in conn.execute(f'PRAGMA table_info("{name}")')}
        for name in names
    }


def _rows(
    conn: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    table: str,
    order_by: str,
) -> list[dict[str, Any]]:
    if table not in tables or order_by not in tables[table]:
        return []
    return [
        dict(row)
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY "{order_by}"')
    ]


def _node(
    entity_type: str,
    source_key: object,
    labels: Iterable[str],
    properties: Mapping[str, Any],
    table: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    identifier = external_id(entity_type, source_key)
    cleaned = {key: value for key, value in properties.items() if value is not None}
    cleaned.update({"id": identifier, "node_type": entity_type})
    return {
        "id": identifier,
        "labels": ["LpgNode", *labels],
        "properties": cleaned,
        "provenance": _provenance(table, source_key, row),
    }


def _edge(
    edge_type: str,
    source: str,
    target: str,
    table: str,
    source_key: object,
    row: Mapping[str, Any],
    properties: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    cleaned = {key: value for key, value in (properties or {}).items() if value is not None}
    cleaned.update({"id": _edge_id(edge_type, table, source_key), "edge_type": edge_type})
    return {
        "id": cleaned["id"],
        "type": edge_type,
        "source": source,
        "target": target,
        "properties": cleaned,
        "provenance": _provenance(table, source_key, row),
    }


def _link_properties(row: Mapping[str, Any], *extra_names: str) -> dict[str, Any]:
    names = ("link_method", "relation_type", "confidence_score", "link_status", "review_status", *extra_names)
    return {name: row[name] for name in names if name in row and row[name] is not None}


def _property_safe_json(value: object) -> str:
    """Canonicalise structured evidence for a scalar Neo4j property."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _validated_internal_roles(
    internal_roles: Iterable[InternalJobRole | Mapping[str, Any]],
) -> dict[str, InternalJobRole]:
    roles: dict[str, InternalJobRole] = {}
    for raw_role in internal_roles:
        role = validate_internal_job_role(raw_role)
        existing = roles.get(role.gold_id)
        if existing is not None and existing.to_dict() != role.to_dict():
            raise ContractValidationError("duplicate tenant-scoped role identity has conflicting content")
        roles[role.gold_id] = role
    return roles


def build_gold_lpg_projection(
    db_path: str | Path,
    *,
    internal_roles: Iterable[InternalJobRole | Mapping[str, Any]] = (),
    role_alignments: Iterable[RoleAlignmentCandidate | Mapping[str, Any]] = (),
    profile: GoldProjectionProfile = SERVING_CORE_PROFILE,
) -> dict[str, Any]:
    """Project supported NCS SQLite tables into deterministic LPG records.

    Missing optional tables simply produce no records for that table.  No SQL
    mutation is issued; this function is safe against a source DB mounted
    read-only by the operating system as well as through SQLite URI mode.
    """

    if not isinstance(profile, GoldProjectionProfile):
        raise TypeError("profile must be a GoldProjectionProfile")
    if profile not in {SERVING_CORE_PROFILE, FULL_FIDELITY_PROFILE}:
        raise ValueError("profile must equal SERVING_CORE_PROFILE or FULL_FIDELITY_PROFILE")
    serving_core = profile == SERVING_CORE_PROFILE
    validated_roles = _validated_internal_roles(internal_roles)
    validated_alignments = [validate_alignment_candidate(item) for item in role_alignments]
    for alignment in validated_alignments:
        if alignment.role_gold_id not in validated_roles:
            raise ContractValidationError(
                "alignment candidate role_gold_id is not a supplied tenant-scoped internal role"
            )

    with _readonly_connection(db_path) as conn:
        tables = _table_columns(conn)
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        node_ids: set[str] = set()
        job_ids_by_classification_id: dict[str, str] = {}
        job_ids_by_code: dict[str, str] = {}
        job_ids_by_unit_code: dict[str, str] = {}
        unit_codes_by_element_id: dict[str, str] = {}
        diagnostics: list[dict[str, Any]] = []
        summarized: dict[str, int] = {}

        def add_node(record: dict[str, Any]) -> None:
            if record["id"] not in node_ids:
                node_ids.add(record["id"])
                nodes.append(record)

        classifications = _rows(conn, tables, "classifications", "classification_id")
        for row in classifications:
            key = _source_key(row, "classification_id")
            if key is None:
                continue
            levels = (
                ("major", _source_key(row, "major_code"), _value(row, "major_name")),
                ("middle", _source_key(row, "middle_code"), _value(row, "middle_name")),
                ("small", _source_key(row, "small_code"), _value(row, "small_name")),
                ("sub", _source_key(row, "sub_code"), _value(row, "sub_name")),
            )
            category_ids: list[str] = []
            category_path: list[str] = []
            for level, code, name in levels:
                if code is None:
                    break
                category_path.append(code)
                category_key = f"{level}:{':'.join(category_path)}"
                category_id = external_id("ncs_job_category", category_key)
                category_ids.append(category_id)
                add_node(_node(
                    "ncs_job_category", category_key, ["NCSJobCategory"],
                    {
                        "classification_id": key,
                        "category_level": level,
                        "code": code,
                        "name": name,
                        "path": ":".join(category_path),
                        "review_status": _status(row),
                    }, "classifications", row,
                ))
            for parent_id, child_id in zip(category_ids, category_ids[1:]):
                edges.append(_edge("HAS_SUB_CATEGORY", parent_id, child_id,
                    "classifications", f"{key}:{parent_id}:{child_id}", row,
                    {"classification_id": key, "review_status": _status(row)}))
            job_code = _complete_ncs_job_code(levels)
            if job_code is None:
                diagnostics.append({
                    "code": "ncs_job_code_unresolved",
                    "classification_id": key,
                    "detail": "classification requires a complete major/middle/small/sub code path",
                })
                continue
            job_id = external_id("ncs_job", job_code)
            job_node = _node(
                "ncs_job", job_code, ["NCSJob"],
                {
                    "code": job_code,
                    "name": _value(row, "sub_name", "small_name", "middle_name", "major_name"),
                    "major_code": _value(row, "major_code"),
                    "middle_code": _value(row, "middle_code"),
                    "small_code": _value(row, "small_code"),
                    "sub_code": _value(row, "sub_code"),
                    "review_status": _status(row),
                }, "classifications", row,
            )
            job_node["provenance"]["classification_id"] = key
            add_node(job_node)
            job_ids_by_classification_id[key] = job_id
            job_ids_by_code[job_code] = job_id
            if category_ids:
                edges.append(_edge("HAS_SUB_CATEGORY", category_ids[-1], job_id,
                    "classifications", f"{key}:sub-category-job", row,
                    {"classification_id": key, "review_status": _status(row), "target_kind": "ncs_job"}))
                if not serving_core:
                    edges.append(_edge("HAS_NCS_JOB", category_ids[-1], job_id,
                        "classifications", f"{key}:job", row,
                        {"classification_id": key, "review_status": _status(row)}))

        units = _rows(conn, tables, "competency_units", "unit_code")
        for row in units:
            key = _source_key(row, "unit_code")
            if key is None:
                continue
            unit_id = external_id("competency_unit", key)
            add_node(_node(
                "competency_unit", key, ["CompetencyUnit"],
                {
                    "unit_code": key,
                    "name": _value(row, "unit_name_refined", "unit_name_raw", "api_unit_name"),
                    "unit_name_raw": _value(row, "unit_name_raw"),
                    "unit_level": _value(row, "api_unit_level", "unit_level_raw"),
                    "classification_id": _value(row, "classification_id"),
                    "review_status": _status(row),
                }, "competency_units", row,
            ))
            classification_key = _source_key(row, "classification_id")
            job_id = job_ids_by_classification_id.get(classification_key or "")
            if job_id in node_ids:
                job_ids_by_unit_code[key] = job_id
                edges.append(_edge("REQUIRES_UNIT", job_id, unit_id,
                    "competency_units", key, row, {"review_status": _status(row)}))

        elements = _rows(conn, tables, "competency_elements", "element_id")
        for row in elements:
            key = _source_key(row, "element_id")
            unit_key = _source_key(row, "unit_code")
            if key is None:
                continue
            element_id = external_id("competency_element", key)
            if unit_key is not None:
                unit_codes_by_element_id[key] = unit_key
            add_node(_node(
                "competency_element", key, ["PerformanceElement"],
                {
                    "element_id": key,
                    "unit_code": unit_key,
                    "element_no": _value(row, "element_no"),
                    "element_code": _value(row, "element_code_raw"),
                    "name": _value(row, "element_name_refined", "element_name_raw", "api_element_name"),
                    "element_level": _value(row, "api_element_level", "element_level_raw"),
                    "review_status": _status(row),
                }, "competency_elements", row,
            ))
            unit_id = external_id("competency_unit", unit_key) if unit_key else None
            if unit_id in node_ids:
                if not serving_core:
                    edges.append(_edge("HAS_ELEMENT", unit_id, element_id,
                        "competency_elements", key, row, {"review_status": _status(row)}))
                edges.append(_edge("DEFINED_BY", unit_id, element_id,
                    "competency_elements", f"{key}:defined-by", row,
                    {"review_status": _status(row), "target_kind": "performance_element"}))

        criteria_element_keys: dict[str, str] = {}
        criteria = _rows(conn, tables, "performance_criteria", "criteria_id")
        for row in criteria:
            key = _source_key(row, "criteria_id")
            element_key = _source_key(row, "element_id")
            if key is None:
                continue
            criterion_id = external_id("performance_criterion", key)
            task_id = criterion_id if serving_core else external_id("task", key)
            if element_key is not None:
                criteria_element_keys[key] = element_key
            common_properties = {
                "criteria_id": key,
                "criteria_no": _value(row, "criteria_no"),
                "text": _value(row, "criteria_text_refined", "criteria_text_raw"),
                "criteria_text_raw": _value(row, "criteria_text_raw"),
                "element_id": element_key,
                "review_status": _status(row),
            }
            if serving_core:
                add_node(_node("performance_criterion", key, ["PerformanceCriterion", "Task"],
                    {**common_properties, "task_basis": "performance_criterion"}, "performance_criteria", row))
            else:
                add_node(_node("performance_criterion", key, ["PerformanceCriterion"],
                    common_properties, "performance_criteria", row))
                add_node(_node("task", key, ["Task"],
                    {**common_properties, "task_basis": "performance_criterion"}, "performance_criteria", row))
            element_id = external_id("competency_element", element_key) if element_key else None
            if element_id in node_ids:
                edges.append(_edge("HAS_CRITERION", element_id, criterion_id,
                    "performance_criteria", key, row, {"review_status": _status(row)}))
            if not serving_core:
                edges.append(_edge("REPRESENTS_TASK", criterion_id, task_id,
                    "performance_criteria", key, row, {"review_status": _status(row)}))

        concept_requirement_types: dict[str, str] = {}
        concepts = _rows(conn, tables, "ontology_concepts", "concept_id")
        for row in concepts:
            key = _source_key(row, "concept_id")
            if key is None:
                continue
            concept_type = (_text(_value(row, "concept_type")) or "concept").lower()
            subtype = {"knowledge": "Knowledge", "skill": "Skill", "attitude": "Attitude"}.get(
                concept_type, "Concept"
            )
            concept_requirement_types[key] = {
                "knowledge": "REQUIRES_KNOWLEDGE",
                "skill": "REQUIRES_SKILL",
                "attitude": "REQUIRES_ATTITUDE",
            }.get(concept_type, "REQUIRES_KSA")
            trusted_definition = _is_trusted_definition(row)
            add_node(_node(
                "ontology_concept", key, ["KSAConcept", subtype],
                {
                    "concept_id": key,
                    "name": _value(row, "concept_name"),
                    "concept_type": concept_type,
                    "definition": _text(row.get("definition")) if trusted_definition else None,
                    "definition_is_trusted": trusted_definition,
                    "definition_status": _value(row, "definition_status"),
                    "review_status": _status(row),
                }, "ontology_concepts", row,
            ))

        element_concept_summary: dict[tuple[str, str], dict[str, Any]] = {}
        job_concept_summary: dict[tuple[str, str], dict[str, Any]] = {}
        for row in _rows(conn, tables, "criteria_concept_links", "link_id"):
            key = _source_key(row, "link_id")
            criterion_key = _source_key(row, "criteria_id")
            concept_key = _source_key(row, "concept_id")
            if key is None or criterion_key is None or concept_key is None:
                continue
            criterion_id = external_id("performance_criterion", criterion_key)
            task_id = criterion_id if serving_core else external_id("task", criterion_key)
            concept_id = external_id("ontology_concept", concept_key)
            if criterion_id in node_ids and concept_id in node_ids:
                edge_type = concept_requirement_types.get(concept_key, "REQUIRES_KSA")
                props = _link_properties(row)
                props.update({
                    "criteria_id": criterion_key,
                    "evidence_kind": "criteria_concept_link",
                })
                edges.append(_edge(edge_type, criterion_id, concept_id,
                    "criteria_concept_links", key, row, props))
                if not serving_core and task_id in node_ids:
                    task_props = {**props, "derived_from": "performance_criterion"}
                    edges.append(_edge(edge_type, task_id, concept_id,
                        "criteria_concept_links", f"{key}:task", row, task_props))
                element_key = criteria_element_keys.get(criterion_key)
                element_id = external_id("competency_element", element_key) if element_key else None
                if element_id in node_ids:
                    if serving_core:
                        summary = element_concept_summary.setdefault(
                            (element_key or "", concept_key),
                            {
                                "element_id": element_id,
                                "concept_id": concept_id,
                                "edge_type": edge_type,
                                "criteria_ids": set(),
                                "source_keys": [],
                                "status_counts": Counter(),
                                "method_counts": Counter(),
                            },
                        )
                        summary["criteria_ids"].add(criterion_key)
                        summary["source_keys"].append(key)
                        summary["status_counts"][_text(_value(row, "link_status", "review_status")) or "unknown"] += 1
                        summary["method_counts"][_text(_value(row, "link_method", "relation_type")) or "unknown"] += 1
                        unit_key = unit_codes_by_element_id.get(element_key or "")
                        job_id = job_ids_by_unit_code.get(unit_key or "")
                        if job_id is not None:
                            job_summary = job_concept_summary.setdefault(
                                (job_id, concept_key),
                                {
                                    "job_id": job_id,
                                    "concept_id": concept_id,
                                    "edge_type": edge_type,
                                    "unit_codes": set(),
                                    "element_keys": set(),
                                    "criteria_ids": set(),
                                    "source_keys": [],
                                    "status_counts": Counter(),
                                    "method_counts": Counter(),
                                },
                            )
                            job_summary["unit_codes"].add(unit_key)
                            job_summary["element_keys"].add(element_key)
                            job_summary["criteria_ids"].add(criterion_key)
                            job_summary["source_keys"].append(key)
                            job_summary["status_counts"][_text(_value(row, "link_status", "review_status")) or "unknown"] += 1
                            job_summary["method_counts"][_text(_value(row, "link_method", "relation_type")) or "unknown"] += 1
                    else:
                        element_props = {
                            **props,
                            "derived_from": "criteria_concept_links",
                            "via_criteria_id": criterion_key,
                        }
                        edges.append(_edge(edge_type, element_id, concept_id,
                            "criteria_concept_links", f"{key}:element", row, element_props))

        if serving_core:
            for (element_key, concept_key), summary in sorted(element_concept_summary.items()):
                source_key_samples = sorted(set(summary["source_keys"]))[:5]
                properties = {
                    "derived_from": "criteria_concept_links",
                    "source_link_count": len(summary["source_keys"]),
                    "distinct_criteria_count": len(summary["criteria_ids"]),
                    "status_distribution_json": _property_safe_json(dict(sorted(summary["status_counts"].items()))),
                    "method_distribution_json": _property_safe_json(dict(sorted(summary["method_counts"].items()))),
                    "source_link_key_samples": source_key_samples,
                }
                edges.append(_edge(
                    summary["edge_type"], summary["element_id"], summary["concept_id"],
                    "criteria_concept_links", f"summary:{element_key}:{concept_key}", {}, properties,
                ))
            summarized["element_concept_edges"] = len(element_concept_summary)
            diagnostics.append({
                "code": "element_concept_links_summarized",
                "distinct_edge_count": len(element_concept_summary),
            })
            for (job_id, concept_key), summary in sorted(job_concept_summary.items()):
                source_key_samples = sorted(set(summary["source_keys"]))[:5]
                properties = {
                    "derived_from": "criteria_concept_links",
                    "source_link_count": len(summary["source_keys"]),
                    "distinct_unit_count": len(summary["unit_codes"]),
                    "distinct_element_count": len(summary["element_keys"]),
                    "distinct_criteria_count": len(summary["criteria_ids"]),
                    "status_distribution_json": _property_safe_json(dict(sorted(summary["status_counts"].items()))),
                    "method_distribution_json": _property_safe_json(dict(sorted(summary["method_counts"].items()))),
                    "source_link_key_samples": source_key_samples,
                    "summary_scope": "ncs_job_to_ksa",
                }
                edges.append(_edge(
                    summary["edge_type"], summary["job_id"], summary["concept_id"],
                    "criteria_concept_links", f"job-summary:{job_id}:{concept_key}", {}, properties,
                ))
            summarized["job_concept_edges"] = len(job_concept_summary)
            diagnostics.append({
                "code": "job_concept_links_summarized",
                "distinct_edge_count": len(job_concept_summary),
                "real_db_reference_distinct_pair_count": 389481,
            })

        if not serving_core and profile.include_task_ksa_detailed_relations:
            detailed_task_relation_rows = _rows(conn, tables, "task_ksa_concept_relations", "relation_id")
        else:
            detailed_task_relation_rows = []
            diagnostics.append({
                "code": "task_ksa_concept_relations_omitted",
                "reason": "serving_core_profile",
            })
        for row in detailed_task_relation_rows:
            key = _source_key(row, "relation_id")
            task_key = _source_key(row, "criteria_id")
            source_key = _source_key(row, "source_concept_id")
            target_key = _source_key(row, "target_concept_id")
            if key is None or task_key is None or source_key is None or target_key is None:
                continue
            source_id = external_id("ontology_concept", source_key)
            target_id = external_id("ontology_concept", target_key)
            task_id = external_id("task", task_key)
            props = _link_properties(row, "criteria_id", "element_id", "source_atomic_id", "target_atomic_id", "evidence_text")
            props["task_id"] = task_id
            if source_id in node_ids and target_id in node_ids and task_id in node_ids:
                edges.append(_edge("TASK_KSA_CONCEPT_RELATION", source_id, target_id,
                    "task_ksa_concept_relations", key, row, props))

        courses = _rows(conn, tables, "ncs_training_courses", "training_course_id")
        for row in courses:
            key = _source_key(row, "training_course_id")
            if key is None:
                continue
            add_node(_node(
                "training_course", key, ["TrainingCourse"],
                {
                    "training_course_id": key,
                    "ncs_cl_cd": _value(row, "ncs_cl_cd"),
                    "name": _value(row, "course_name", "compe_unit_name"),
                    "unit_level": _value(row, "compe_unit_level"),
                    "train_goal": _value(row, "train_goal"),
                    "train_time": _value(row, "train_time"),
                    "facility": _value(row, "fac_name"),
                    "method": _value(row, "meth_name"),
                }, "ncs_training_courses", row,
            ))

        for row in _rows(conn, tables, "ncs_training_course_unit_links", "link_id"):
            key = _source_key(row, "link_id")
            course_key = _source_key(row, "training_course_id")
            unit_key = _source_key(row, "unit_code")
            if key is None or course_key is None or unit_key is None:
                continue
            course_id, unit_id = external_id("training_course", course_key), external_id("competency_unit", unit_key)
            if course_id in node_ids and unit_id in node_ids:
                edges.append(_edge("COURSE_COVERS_UNIT", course_id, unit_id,
                    "ncs_training_course_unit_links", key, row, _link_properties(row)))

        omitted_inherited_course_concept_links = 0
        for table, edge_type, target_type, key_name in (
            ("ncs_training_course_concept_links", "COURSE_COVERS_CONCEPT", "ontology_concept", "concept_id"),
            ("ncs_training_course_element_links", "COURSE_COVERS_ELEMENT", "competency_element", "element_id"),
            ("training_goal_concept_links", "COURSE_GOAL_COVERS_CONCEPT", "ontology_concept", "concept_id"),
        ):
            for row in _rows(conn, tables, table, "link_id"):
                key = _source_key(row, "link_id")
                course_key = _source_key(row, "training_course_id")
                target_key = _source_key(row, key_name)
                if key is None or course_key is None or target_key is None:
                    continue
                if (
                    serving_core
                    and table == "ncs_training_course_concept_links"
                    and _text(_value(row, "link_method")) == "unit_ksa_concept_inherited"
                ):
                    omitted_inherited_course_concept_links += 1
                    continue
                course_id = external_id("training_course", course_key)
                target_id = external_id(target_type, target_key)
                if course_id in node_ids and target_id in node_ids:
                    edges.append(_edge(edge_type, course_id, target_id, table, key, row,
                        _link_properties(row, "unit_code", "element_id", "evidence_text")))
        if omitted_inherited_course_concept_links:
            diagnostics.append({
                "code": "inherited_course_concept_links_omitted",
                "count": omitted_inherited_course_concept_links,
                "link_method": "unit_ksa_concept_inherited",
            })

        for row in _rows(conn, tables, "training_delivery_relations", "relation_id"):
            key = _source_key(row, "relation_id")
            course_key = _source_key(row, "training_course_id")
            if key is None or course_key is None:
                continue
            delivery_id = external_id("training_delivery", key)
            add_node(_node(
                "training_delivery", key, ["TrainingDelivery"],
                {
                    "relation_id": key,
                    "training_course_id": course_key,
                    "relation_type": _value(row, "relation_type"),
                    "relation_value": _value(row, "relation_value"),
                    "normalized_value": _value(row, "normalized_value"),
                    "numeric_value": _value(row, "numeric_value"),
                    "review_status": _status(row),
                }, "training_delivery_relations", row,
            ))
            course_id = external_id("training_course", course_key)
            if course_id in node_ids:
                edges.append(_edge("COURSE_HAS_DELIVERY", course_id, delivery_id,
                    "training_delivery_relations", key, row, _link_properties(row, "evidence_text")))

        role_nodes_by_gold_id: dict[str, dict[str, Any]] = {}
        for role_gold_id, role in sorted(validated_roles.items()):
            role_public = role.to_public_dict()
            role_node = _node(
                "internal_job_role", role_gold_id, ["InternalJobRole"],
                {
                    "role_gold_id": role_gold_id,
                    "organization_namespace": role.organization_namespace,
                    "role_id": role.role_id,
                    "display_name": role.display_name,
                    "aliases": list(role.aliases),
                    "description": role.description,
                    "duties": list(role.duties),
                    "target_level": role.target_level,
                    "source": role.source,
                    "effective_date": role.effective_date,
                    "normalized_semantic_text": role.normalized_semantic_text,
                    "provenance_json": _property_safe_json(role_public["provenance"]),
                    "alignment_candidate_count": 0,
                    "alignment_candidates_json": "[]",
                }, "internal_job_roles_input", role_public,
            )
            add_node(role_node)
            role_nodes_by_gold_id[role_gold_id] = role_node

        candidate_metadata: dict[str, list[dict[str, Any]]] = {
            role_gold_id: [] for role_gold_id in role_nodes_by_gold_id
        }
        for candidate in sorted(
            validated_alignments,
            key=lambda item: (
                item.role_gold_id, item.ncs_target_type, item.ncs_target_key,
                item.status, item.method, item.model or "", item.score,
            ),
        ):
            candidate_public = candidate.to_public_dict()
            candidate_metadata[candidate.role_gold_id].append(candidate_public)
            role_node = role_nodes_by_gold_id[candidate.role_gold_id]
            target_type = candidate.ncs_target_type.strip().casefold().replace("-", "_")
            target_id = job_ids_by_code.get(candidate.ncs_target_key)
            if candidate.status in {"ambiguous", "unresolved"}:
                diagnostics.append({
                    "code": "role_alignment_not_linked_status",
                    "role_gold_id": candidate.role_gold_id,
                    "status": candidate.status,
                    "ncs_target_type": candidate.ncs_target_type,
                    "ncs_target_key": candidate.ncs_target_key,
                })
                continue
            if target_type not in {"ncs_job", "ncsjob"} or target_id is None:
                diagnostics.append({
                    "code": "role_alignment_target_unresolved",
                    "role_gold_id": candidate.role_gold_id,
                    "status": candidate.status,
                    "ncs_target_type": candidate.ncs_target_type,
                    "ncs_target_key": candidate.ncs_target_key,
                })
                continue
            candidate_key = hashlib.sha256(
                _property_safe_json(candidate_public).encode("utf-8")
            ).hexdigest()
            edges.append(_edge(
                "ALIGNED_TO", role_node["id"], target_id,
                "role_alignments_input", candidate_key, candidate_public,
                {
                    "role_gold_id": candidate.role_gold_id,
                    "ncs_target_type": candidate.ncs_target_type,
                    "ncs_target_key": candidate.ncs_target_key,
                    "score": float(candidate.score),
                    "method": candidate.method,
                    "model": candidate.model,
                    "status": candidate.status,
                    "evidence_json": _property_safe_json(candidate_public["evidence"]),
                    "alignment_provenance_json": _property_safe_json(candidate_public["provenance"]),
                },
            ))
        for role_gold_id, role_node in role_nodes_by_gold_id.items():
            metadata = candidate_metadata[role_gold_id]
            role_node["properties"]["alignment_candidate_count"] = len(metadata)
            role_node["properties"]["alignment_candidates_json"] = _property_safe_json(metadata)

    nodes.sort(key=lambda record: record["id"])
    edges.sort(key=lambda record: record["id"])
    node_counts = Counter(record["properties"]["node_type"] for record in nodes)
    edge_counts = Counter(record["type"] for record in edges)
    source_tables = sorted({record["provenance"]["source_table"] for record in [*nodes, *edges]})
    diagnostics.sort(key=lambda item: _property_safe_json(item))
    fingerprint_input = {
        "schema": GOLD_LPG_SCHEMA,
        "profile": profile.to_dict(),
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": GOLD_LPG_SCHEMA,
        "profile": profile.to_dict(),
        "fingerprint": fingerprint,
        "projection_fingerprint": fingerprint,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "node_counts": dict(sorted(node_counts.items())),
        "edge_counts": dict(sorted(edge_counts.items())),
        "diagnostic_count": len(diagnostics),
        "omitted_diagnostics": [
            item for item in diagnostics if item["code"].endswith("_omitted")
        ],
        "summarized": dict(sorted(summarized.items())),
        "source_tables": source_tables,
        "read_only": True,
        "db_writes": False,
        "approval_claim": False,
    }
    return {
        "schema": GOLD_LPG_SCHEMA,
        "profile": profile.to_dict(),
        "fingerprint": fingerprint,
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
        "manifest": manifest,
        "read_only": True,
        "db_writes": False,
        "approval_claim": False,
    }


def project_sqlite_to_gold_lpg(db_path: str | Path) -> dict[str, Any]:
    """Compatibility-friendly spelling for :func:`build_gold_lpg_projection`."""

    return build_gold_lpg_projection(db_path)


def _node_import_group(record: Mapping[str, Any]) -> str:
    labels = set(record.get("labels", []))
    if {"PerformanceCriterion", "Task"}.issubset(labels):
        return "PerformanceCriterionTask"
    for label in (
        "NCSJobCategory", "NCSJob", "CompetencyUnit", "PerformanceElement",
        "PerformanceCriterion", "Task", "TrainingCourse", "TrainingDelivery",
        "InternalJobRole",
    ):
        if label in labels:
            return label
    if "KSAConcept" in labels:
        for subtype in ("Knowledge", "Skill", "Attitude"):
            if subtype in labels:
                return f"KSAConcept{subtype}"
        return "KSAConcept"
    raise ValueError(f"Node labels are not in the Gold LPG allowlist: {sorted(labels)}")


def neo4j_import_batches(projection: Mapping[str, Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Group records for fixed-label/type Cypher templates without APOC.

    Empty groups are omitted, including ``InternalJobRole`` until an authorised
    adapter supplies an internal-role source.  The return value contains only
    parameters; this module never connects to Neo4j.
    """

    node_batches: dict[str, list[dict[str, Any]]] = {}
    edge_batches: dict[str, list[dict[str, Any]]] = {}
    for node in projection.get("nodes", []):
        group = _node_import_group(node)
        node_batches.setdefault(group, []).append(dict(node))
    for edge in projection.get("edges", []):
        edge_type = str(edge.get("type", ""))
        if edge_type not in ALLOWED_RELATIONSHIP_TYPES:
            raise ValueError(f"Relationship type is not in the Gold LPG allowlist: {edge_type}")
        edge_batches.setdefault(edge_type, []).append(dict(edge))
    return {
        "nodes": {group: node_batches[group] for group in sorted(node_batches)},
        "edges": {group: edge_batches[group] for group in sorted(edge_batches)},
    }


def neo4j_import_plan(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Return an executable-by-caller, static Cypher import plan.

    Every statement comes from a module-owned allowlist.  A caller can execute
    the DDL and operations later with its own Neo4j connection and credentials.
    """

    batches = neo4j_import_batches(projection)
    internal_role_source_available = bool(batches["nodes"].get("InternalJobRole"))
    return {
        "schema_ddl": list(NEO4J_SCHEMA_DDL),
        "node_operations": [
            {"group": group, "cypher": NEO4J_DOMAIN_NODE_MERGE_CYPHER[group], "parameters": {"nodes": rows}}
            for group, rows in batches["nodes"].items()
        ],
        "edge_operations": [
            {"type": edge_type, "cypher": NEO4J_DOMAIN_EDGE_MERGE_CYPHER[edge_type], "parameters": {"edges": rows}}
            for edge_type, rows in batches["edges"].items()
        ],
        "internal_job_role_source_available": internal_role_source_available,
        "internal_job_role_template": NEO4J_DOMAIN_NODE_MERGE_CYPHER["InternalJobRole"],
    }


def neo4j_import_parameters(projection: Mapping[str, Any]) -> dict[str, Any]:
    """Return only data parameters for the static, parameterised Cypher templates."""

    batches = neo4j_import_batches(projection)
    return {
        "nodes": list(projection.get("nodes", [])),
        "edges": list(projection.get("edges", [])),
        "node_batches": batches["nodes"],
        "edge_batches": batches["edges"],
    }


__all__ = [
    "GOLD_LPG_SCHEMA",
    "FULL_FIDELITY_PROFILE",
    "ALLOWED_NODE_IMPORT_GROUPS",
    "ALLOWED_RELATIONSHIP_TYPES",
    "NEO4J_DOMAIN_EDGE_MERGE_CYPHER",
    "NEO4J_DOMAIN_NODE_MERGE_CYPHER",
    "NEO4J_EDGE_MERGE_CYPHER",
    "NEO4J_NODE_MERGE_CYPHER",
    "NEO4J_SCHEMA_DDL",
    "VECTOR_INDEX_SPECS",
    "build_gold_lpg_projection",
    "external_id",
    "neo4j_import_batches",
    "neo4j_import_plan",
    "neo4j_vector_index_ddl",
    "neo4j_import_parameters",
    "project_sqlite_to_gold_lpg",
]
