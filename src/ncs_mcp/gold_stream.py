"""Bounded-memory NDJSON export for the serving-core Gold LPG projection.

The in-memory projection is useful for small fixtures and contract inspection,
but the prepared NCS database is far too large to hold as one Python graph.
This module emits the same *serving-core* node and relationship vocabulary in
deterministic NDJSON batches.  It is deliberately a read-only SQLite exporter:
it does not import a Neo4j driver and cannot load or modify a graph database.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Iterable, Iterator, Mapping

from ncs_mcp.gold_lpg import (
    GOLD_LPG_SCHEMA,
    _edge,
    _is_trusted_definition,
    _link_properties,
    _node,
    _property_safe_json,
    _status,
    _text,
    _value,
    external_id,
)
from ncs_mcp.gold_readiness import (
    GoldProjectionProfile,
    RELEVANT_TABLES,
    SERVING_CORE_PROFILE,
    SQLITE_AUTHORITATIVE_FALLBACK_TABLES,
    serving_core_scope_contract,
)
from ncs_mcp.internal_job_roles import (
    ContractValidationError,
    InternalJobRole,
    RoleAlignmentCandidate,
    validate_alignment_candidate,
    validate_internal_job_role,
)


GOLD_LPG_NDJSON_SCHEMA = "ncs_gold_lpg_ndjson_v1"
_DEFAULT_BATCH_SIZE = 10_000
MAX_STREAM_BATCH_SIZE = 100_000


def _readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def _readonly_snapshot(db_path: str | Path) -> Iterator[tuple[sqlite3.Connection, dict[str, int | bool]]]:
    """Yield one explicit, rollback-only SQLite read snapshot for an export."""

    conn = _readonly_connection(db_path)
    try:
        conn.execute("BEGIN")
        snapshot = {
            "transaction_snapshot": True,
            "schema_version": int(conn.execute("PRAGMA schema_version").fetchone()[0]),
            "data_version": int(conn.execute("PRAGMA data_version").fetchone()[0]),
        }
        yield conn, snapshot
    finally:
        if conn.in_transaction:
            conn.rollback()
        conn.close()


def _columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    available_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # Restrict schema inspection to the Gold scope contract. Optional SQLite
    # virtual tables (notably dbstat) may appear in sqlite_master even when the
    # active Python SQLite build cannot load their module.
    names = sorted(
        (set(RELEVANT_TABLES) | set(SQLITE_AUTHORITATIVE_FALLBACK_TABLES))
        & available_names
    )
    return {
        name: {str(column[1]) for column in conn.execute(f'PRAGMA table_info("{name}")')}
        for name in names
    }


def _has_columns(columns: Mapping[str, set[str]], table: str, *required: str) -> bool:
    return table in columns and set(required).issubset(columns[table])


def _batches(
    conn: sqlite3.Connection,
    sql: str,
    *,
    batch_size: int,
    batch_observer: Callable[[int], None] | None,
) -> Iterator[list[dict[str, Any]]]:
    """Yield bounded SQLite result batches, never an unbounded result list."""

    cursor = conn.execute(sql)
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                return
            if batch_observer is not None:
                batch_observer(len(rows))
            yield [dict(row) for row in rows]
    finally:
        cursor.close()


def _stream_table(
    conn: sqlite3.Connection,
    columns: Mapping[str, set[str]],
    table: str,
    order_by: str,
    *,
    batch_size: int,
    batch_observer: Callable[[int], None] | None,
) -> Iterator[dict[str, Any]]:
    if not _has_columns(columns, table, order_by):
        return
    for batch in _batches(
        conn,
        f'SELECT * FROM "{table}" ORDER BY "{order_by}"',
        batch_size=batch_size,
        batch_observer=batch_observer,
    ):
        yield from batch


def _write_json_line(handle: Any, record: Mapping[str, Any]) -> bytes:
    """Write one canonical NDJSON record and return its exact bytes."""

    encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    handle.write(encoded.decode("utf-8"))
    return encoded


def _classification_levels(row: Mapping[str, Any]) -> list[tuple[str, str, Any]]:
    levels: list[tuple[str, str, Any]] = []
    for level, code_name, name_name in (
        ("major", "major_code", "major_name"),
        ("middle", "middle_code", "middle_name"),
        ("small", "small_code", "small_name"),
        ("sub", "sub_code", "sub_name"),
    ):
        code = _text(row.get(code_name))
        if code is None:
            break
        levels.append((level, code, row.get(name_name)))
    return levels


def _complete_job_code(*codes: Any) -> str | None:
    """Return an NCSJob code only for a complete four-level classification."""

    normalized = [_text(code) for code in codes]
    if len(normalized) != 4 or any(code is None for code in normalized):
        return None
    return "".join(normalized)


def _row_job_code(row: Mapping[str, Any], *, prefix: str = "") -> str | None:
    return _complete_job_code(
        row.get(f"{prefix}major_code"),
        row.get(f"{prefix}middle_code"),
        row.get(f"{prefix}small_code"),
        row.get(f"{prefix}sub_code"),
    )


def _requirement_type(concept_type: Any) -> str:
    return {
        "knowledge": "REQUIRES_KNOWLEDGE",
        "skill": "REQUIRES_SKILL",
        "attitude": "REQUIRES_ATTITUDE",
    }.get((_text(concept_type) or "concept").lower(), "REQUIRES_KSA")


def _record(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"record_type": kind, kind: payload}


def _column_or_null(columns: Mapping[str, set[str]], table: str, alias: str, column: str) -> str:
    """Return a fixed, schema-checked SQL column expression or SQL NULL."""

    if column in columns.get(table, set()):
        return f'{alias}."{column}"'
    return "NULL"


def _criteria_concept_summary_sql(
    columns: Mapping[str, set[str]],
    *,
    scope: str,
) -> str:
    """Return one bounded GROUP BY query for element or NCS-job KSA evidence.

    The CTE deliberately computes distributions and five sorted source-key
    samples in SQL.  Fetching the result still happens through ``fetchmany``;
    no Python dictionary grows with the number of summary pairs.
    """

    link_status = _column_or_null(columns, "criteria_concept_links", "ccl", "link_status")
    review_status = _column_or_null(columns, "criteria_concept_links", "ccl", "review_status")
    link_method = _column_or_null(columns, "criteria_concept_links", "ccl", "link_method")
    relation_type = _column_or_null(columns, "criteria_concept_links", "ccl", "relation_type")
    if scope == "element":
        group_key = 'CAST(pc."element_id" AS TEXT)'
        joins = 'JOIN competency_elements ce ON ce.element_id = pc.element_id'
        where = 'pc."element_id" IS NOT NULL'
        unit_key = "NULL"
    elif scope == "job":
        group_key = "TRIM(c.major_code) || TRIM(c.middle_code) || TRIM(c.small_code) || TRIM(c.sub_code)"
        joins = '''
            JOIN competency_elements ce ON ce.element_id = pc.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN classifications c ON c.classification_id = cu.classification_id
        '''
        where = " AND ".join(
            f"NULLIF(TRIM(c.{column}), '') IS NOT NULL"
            for column in ("major_code", "middle_code", "small_code", "sub_code")
        )
        unit_key = 'CAST(cu.unit_code AS TEXT)'
    else:  # pragma: no cover - fixed internal callers only
        raise ValueError(f"unsupported summary scope: {scope}")
    return f'''
        WITH joined AS (
            SELECT {group_key} AS group_key,
                   CAST(ccl.concept_id AS TEXT) AS concept_key,
                   oc.concept_type AS concept_type,
                   CAST(ccl.link_id AS TEXT) AS link_key,
                   CAST(ccl.criteria_id AS TEXT) AS criteria_key,
                   CAST(pc.element_id AS TEXT) AS element_key,
                   {unit_key} AS unit_key,
                   COALESCE(NULLIF({link_status}, ''), NULLIF({review_status}, ''), 'unknown') AS status_value,
                   COALESCE(NULLIF({link_method}, ''), NULLIF({relation_type}, ''), 'unknown') AS method_value
            FROM criteria_concept_links ccl
            JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
            JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
            {joins}
            WHERE {where} AND ccl.concept_id IS NOT NULL
        ),
        totals AS (
            SELECT group_key, concept_key, concept_type,
                   COUNT(*) AS link_count, COUNT(DISTINCT criteria_key) AS criteria_count
                   ,COUNT(DISTINCT element_key) AS element_count
                   ,COUNT(DISTINCT unit_key) AS unit_count
            FROM joined GROUP BY group_key, concept_key, concept_type
        ),
        status_rows AS (
            SELECT group_key, concept_key, status_value, COUNT(*) AS count_value
            FROM joined GROUP BY group_key, concept_key, status_value
        ),
        status_values AS (
            SELECT group_key, concept_key, group_concat(item, char(30)) AS pairs
            FROM (
                SELECT group_key, concept_key, status_value || char(31) || count_value AS item
                FROM status_rows ORDER BY group_key, concept_key, status_value
            ) GROUP BY group_key, concept_key
        ),
        method_rows AS (
            SELECT group_key, concept_key, method_value, COUNT(*) AS count_value
            FROM joined GROUP BY group_key, concept_key, method_value
        ),
        method_values AS (
            SELECT group_key, concept_key, group_concat(item, char(30)) AS pairs
            FROM (
                SELECT group_key, concept_key, method_value || char(31) || count_value AS item
                FROM method_rows ORDER BY group_key, concept_key, method_value
            ) GROUP BY group_key, concept_key
        ),
        ranked_samples AS (
            SELECT group_key, concept_key, link_key,
                   ROW_NUMBER() OVER (PARTITION BY group_key, concept_key ORDER BY link_key) AS ordinal
            FROM joined
        ),
        sample_values AS (
            SELECT group_key, concept_key, group_concat(link_key, char(30)) AS samples
            FROM (
                SELECT group_key, concept_key, link_key FROM ranked_samples
                WHERE ordinal <= 5 ORDER BY group_key, concept_key, link_key
            ) GROUP BY group_key, concept_key
        )
        SELECT totals.group_key, totals.concept_key, totals.concept_type,
               totals.link_count, totals.criteria_count, totals.element_count, totals.unit_count,
               status_values.pairs AS status_pairs, method_values.pairs AS method_pairs,
               sample_values.samples
        FROM totals
        JOIN status_values USING (group_key, concept_key)
        JOIN method_values USING (group_key, concept_key)
        LEFT JOIN sample_values USING (group_key, concept_key)
        ORDER BY totals.group_key, totals.concept_key
    '''


def _distribution_from_pairs(pairs: Any) -> str:
    values: dict[str, int] = {}
    for item in str(pairs or "").split(chr(30)):
        if not item:
            continue
        name, separator, raw_count = item.partition(chr(31))
        if separator and raw_count.isdigit():
            values[name] = int(raw_count)
    return _property_safe_json(dict(sorted(values.items())))


def _samples_from_pairs(pairs: Any) -> list[str]:
    return [value for value in str(pairs or "").split(chr(30)) if value][:5]


def _validated_role_inputs(
    internal_roles: Iterable[InternalJobRole | Mapping[str, Any]],
    role_alignments: Iterable[RoleAlignmentCandidate | Mapping[str, Any]],
) -> tuple[
    dict[str, InternalJobRole],
    list[RoleAlignmentCandidate],
    dict[str, list[dict[str, Any]]],
]:
    """Materialize the intentionally small organization-owned overlay safely."""

    roles: dict[str, InternalJobRole] = {}
    for raw_role in internal_roles:
        role = validate_internal_job_role(raw_role)
        existing = roles.get(role.gold_id)
        if existing is not None and existing.to_dict() != role.to_dict():
            raise ContractValidationError(
                "duplicate tenant-scoped role identity has conflicting content"
            )
        roles[role.gold_id] = role

    candidates_by_payload: dict[str, RoleAlignmentCandidate] = {}
    for item in role_alignments:
        candidate = validate_alignment_candidate(item)
        if candidate.role_gold_id not in roles:
            raise ContractValidationError(
                "alignment candidate role_gold_id is not a supplied tenant-scoped internal role"
            )
        candidates_by_payload.setdefault(
            _property_safe_json(candidate.to_public_dict()),
            candidate,
        )
    candidates = sorted(
        candidates_by_payload.values(),
        key=lambda item: (
            item.role_gold_id,
            item.ncs_target_type,
            item.ncs_target_key,
            item.status,
            item.method,
            item.model or "",
            item.score,
            _property_safe_json(item.to_public_dict()),
        )
    )
    metadata = {role_gold_id: [] for role_gold_id in roles}
    for candidate in candidates:
        metadata[candidate.role_gold_id].append(candidate.to_public_dict())
    return roles, candidates, metadata


def export_gold_lpg_ndjson(
    db_path: str | Path,
    out_path: str | Path,
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    profile: GoldProjectionProfile = SERVING_CORE_PROFILE,
    internal_roles: Iterable[InternalJobRole | Mapping[str, Any]] = (),
    role_alignments: Iterable[RoleAlignmentCandidate | Mapping[str, Any]] = (),
    _batch_observer: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Atomically write a serving-core Gold LPG NDJSON export.

    Each non-final line has ``record_type`` set to ``node``, ``relationship``,
    or ``diagnostic``.  The final line is a self-contained manifest; its
    ``records_sha256`` covers every preceding line, while the manifest itself
    is intentionally excluded to avoid a self-referential digest.
    """

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= MAX_STREAM_BATCH_SIZE
    ):
        raise ValueError(f"batch_size must be an integer between 1 and {MAX_STREAM_BATCH_SIZE}")
    if not isinstance(profile, GoldProjectionProfile) or profile != SERVING_CORE_PROFILE:
        raise ValueError("streaming export supports SERVING_CORE_PROFILE only")
    validated_roles, validated_alignments, candidate_metadata = _validated_role_inputs(
        internal_roles,
        role_alignments,
    )

    source = Path(db_path).resolve()
    destination = Path(out_path).resolve()
    protected_destinations = {
        os.path.normcase(str(source) + suffix)
        for suffix in ("", "-wal", "-shm", "-journal")
    }
    if os.path.normcase(str(destination)) in protected_destinations:
        raise ValueError("NDJSON output path must not overwrite the source SQLite database or its sidecars")
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(f"NDJSON output path is a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)

    with _readonly_snapshot(db_path) as (conn, snapshot):
        columns = _columns(conn)
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="\n", delete=False,
                dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp",
            ) as handle:
                temp_name = handle.name
                digest = hashlib.sha256()
                node_counts: Counter[str] = Counter()
                edge_counts: Counter[str] = Counter()
                source_tables: set[str] = set()
                record_count = 0
                max_observed_batch_rows = 0
                diagnostics: list[dict[str, Any]] = []

                def observe(size: int) -> None:
                    nonlocal max_observed_batch_rows
                    max_observed_batch_rows = max(max_observed_batch_rows, size)
                    if _batch_observer is not None:
                        _batch_observer(size)

                def emit(kind: str, payload: Mapping[str, Any]) -> None:
                    nonlocal record_count
                    encoded = _write_json_line(handle, _record(kind, payload))
                    digest.update(encoded)
                    record_count += 1
                    provenance = payload.get("provenance")
                    if isinstance(provenance, Mapping) and provenance.get("source_table"):
                        source_tables.add(str(provenance["source_table"]))
                    if kind == "node":
                        properties = payload.get("properties", {})
                        node_counts[str(properties.get("node_type", "unknown"))] += 1
                    elif kind == "relationship":
                        edge_counts[str(payload.get("type", "unknown"))] += 1

                # Nodes: category identities are the only intentionally de-duplicated
                # stream.  Their cardinality is bounded by the NCS classification tree,
                # rather than the much larger task/KSA relation tables.
                seen_categories: set[str] = set()
                seen_jobs: set[str] = set()
                for row in _stream_table(conn, columns, "classifications", "classification_id", batch_size=batch_size, batch_observer=observe):
                    classification_key = _text(row.get("classification_id"))
                    if classification_key is None:
                        continue
                    path: list[str] = []
                    for level, code, name in _classification_levels(row):
                        path.append(code)
                        category_key = f"{level}:{':'.join(path)}"
                        if category_key in seen_categories:
                            continue
                        seen_categories.add(category_key)
                        emit("node", _node("ncs_job_category", category_key, ["NCSJobCategory"], {
                            "classification_id": classification_key, "category_level": level,
                            "code": code, "name": name, "path": ":".join(path),
                            "review_status": _status(row),
                        }, "classifications", row))
                    job_code = _row_job_code(row)
                    if not job_code:
                        diagnostics.append({"code": "ncs_job_code_unresolved", "classification_id": classification_key, "detail": "classification has no stable code path"})
                    elif job_code not in seen_jobs:
                        seen_jobs.add(job_code)
                        job = _node("ncs_job", job_code, ["NCSJob"], {
                            "code": job_code,
                            "name": _value(row, "sub_name", "small_name", "middle_name", "major_name"),
                            "major_code": row.get("major_code"), "middle_code": row.get("middle_code"),
                            "small_code": row.get("small_code"), "sub_code": row.get("sub_code"),
                            "review_status": _status(row),
                        }, "classifications", row)
                        job["provenance"]["classification_id"] = classification_key
                        emit("node", job)

                for row in _stream_table(conn, columns, "competency_units", "unit_code", batch_size=batch_size, batch_observer=observe):
                    key = _text(row.get("unit_code"))
                    if key:
                        emit("node", _node("competency_unit", key, ["CompetencyUnit"], {
                            "unit_code": key, "name": _value(row, "unit_name_refined", "unit_name_raw", "api_unit_name"),
                            "unit_name_raw": row.get("unit_name_raw"), "unit_level": _value(row, "api_unit_level", "unit_level_raw"),
                            "classification_id": row.get("classification_id"), "review_status": _status(row),
                        }, "competency_units", row))

                for row in _stream_table(conn, columns, "competency_elements", "element_id", batch_size=batch_size, batch_observer=observe):
                    key = _text(row.get("element_id"))
                    if key:
                        emit("node", _node("competency_element", key, ["PerformanceElement"], {
                            "element_id": key, "unit_code": row.get("unit_code"), "element_no": row.get("element_no"),
                            "element_code": row.get("element_code_raw"),
                            "name": _value(row, "element_name_refined", "element_name_raw", "api_element_name"),
                            "element_level": _value(row, "api_element_level", "element_level_raw"), "review_status": _status(row),
                        }, "competency_elements", row))

                for row in _stream_table(conn, columns, "performance_criteria", "criteria_id", batch_size=batch_size, batch_observer=observe):
                    key = _text(row.get("criteria_id"))
                    if key:
                        emit("node", _node("performance_criterion", key, ["PerformanceCriterion", "Task"], {
                            "criteria_id": key, "criteria_no": row.get("criteria_no"),
                            "text": _value(row, "criteria_text_refined", "criteria_text_raw"),
                            "criteria_text_raw": row.get("criteria_text_raw"), "element_id": row.get("element_id"),
                            "review_status": _status(row), "task_basis": "performance_criterion",
                        }, "performance_criteria", row))

                for row in _stream_table(conn, columns, "ontology_concepts", "concept_id", batch_size=batch_size, batch_observer=observe):
                    key = _text(row.get("concept_id"))
                    if not key:
                        continue
                    concept_type = (_text(row.get("concept_type")) or "concept").lower()
                    subtype = {"knowledge": "Knowledge", "skill": "Skill", "attitude": "Attitude"}.get(concept_type, "Concept")
                    trusted = _is_trusted_definition(row)
                    emit("node", _node("ontology_concept", key, ["KSAConcept", subtype], {
                        "concept_id": key, "name": row.get("concept_name"), "concept_type": concept_type,
                        "definition": _text(row.get("definition")) if trusted else None,
                        "definition_is_trusted": trusted, "definition_status": row.get("definition_status"),
                        "review_status": _status(row),
                    }, "ontology_concepts", row))

                for row in _stream_table(conn, columns, "ncs_training_courses", "training_course_id", batch_size=batch_size, batch_observer=observe):
                    key = _text(row.get("training_course_id"))
                    if key:
                        emit("node", _node("training_course", key, ["TrainingCourse"], {
                            "training_course_id": key, "ncs_cl_cd": row.get("ncs_cl_cd"),
                            "name": _value(row, "course_name", "compe_unit_name"), "unit_level": row.get("compe_unit_level"),
                            "train_goal": row.get("train_goal"), "train_time": row.get("train_time"),
                            "facility": row.get("fac_name"), "method": row.get("meth_name"),
                        }, "ncs_training_courses", row))

                for row in _stream_table(conn, columns, "training_delivery_relations", "relation_id", batch_size=batch_size, batch_observer=observe):
                    key, course_key = _text(row.get("relation_id")), _text(row.get("training_course_id"))
                    if key and course_key:
                        emit("node", _node("training_delivery", key, ["TrainingDelivery"], {
                            "relation_id": key, "training_course_id": course_key, "relation_type": row.get("relation_type"),
                            "relation_value": row.get("relation_value"), "normalized_value": row.get("normalized_value"),
                            "numeric_value": row.get("numeric_value"), "review_status": _status(row),
                        }, "training_delivery_relations", row))

                # The role overlay is small and organization-owned.  It is validated
                # before the source snapshot opens, then emitted in stable ID order
                # while the stream is still in its node phase.
                for role_gold_id, role in sorted(validated_roles.items()):
                    role_public = role.to_public_dict()
                    metadata = candidate_metadata[role_gold_id]
                    emit("node", _node(
                        "internal_job_role",
                        role_gold_id,
                        ["InternalJobRole"],
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
                            "alignment_candidate_count": len(metadata),
                            "alignment_candidates_json": _property_safe_json(metadata),
                        },
                        "internal_job_roles_input",
                        role_public,
                    ))

                # Relationships are SQL joins, so a streaming record never points at a
                # missing core node.  Detailed task_ksa_concept_relations are not queried.
                for row in _stream_table(conn, columns, "classifications", "classification_id", batch_size=batch_size, batch_observer=observe):
                    classification_key = _text(row.get("classification_id"))
                    if classification_key is None:
                        continue
                    category_ids: list[str] = []
                    path: list[str] = []
                    for level, code, _ in _classification_levels(row):
                        path.append(code)
                        category_ids.append(external_id("ncs_job_category", f"{level}:{':'.join(path)}"))
                    for parent_id, child_id in zip(category_ids, category_ids[1:]):
                        emit("relationship", _edge("HAS_SUB_CATEGORY", parent_id, child_id, "classifications", f"{classification_key}:{parent_id}:{child_id}", row, {"classification_id": classification_key, "review_status": _status(row)}))
                    job_code = _row_job_code(row)
                    if category_ids and job_code:
                        emit("relationship", _edge("HAS_SUB_CATEGORY", category_ids[-1], external_id("ncs_job", job_code), "classifications", f"{classification_key}:sub-category-job", row, {"classification_id": classification_key, "review_status": _status(row), "target_kind": "ncs_job"}))

                if _has_columns(columns, "competency_units", "classification_id", "unit_code") and _has_columns(columns, "classifications", "classification_id", "major_code", "middle_code", "small_code", "sub_code"):
                    for batch in _batches(conn, 'SELECT u.*, c.major_code AS _major_code, c.middle_code AS _middle_code, c.small_code AS _small_code, c.sub_code AS _sub_code FROM competency_units u JOIN classifications c ON c.classification_id = u.classification_id ORDER BY u.unit_code', batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            job_code = _complete_job_code(
                                row.get("_major_code"), row.get("_middle_code"),
                                row.get("_small_code"), row.get("_sub_code"),
                            )
                            unit_key = _text(row.get("unit_code"))
                            if job_code and unit_key:
                                emit("relationship", _edge("REQUIRES_UNIT", external_id("ncs_job", job_code), external_id("competency_unit", unit_key), "competency_units", unit_key, row, {"review_status": _status(row)}))

                if _has_columns(columns, "competency_elements", "element_id", "unit_code") and _has_columns(columns, "competency_units", "unit_code"):
                    sql = 'SELECT e.* FROM competency_elements e JOIN competency_units u ON u.unit_code = e.unit_code ORDER BY e.element_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, unit_key = _text(row.get("element_id")), _text(row.get("unit_code"))
                            if key and unit_key:
                                emit("relationship", _edge("DEFINED_BY", external_id("competency_unit", unit_key), external_id("competency_element", key), "competency_elements", f"{key}:defined-by", row, {"review_status": _status(row), "target_kind": "performance_element"}))

                if _has_columns(columns, "performance_criteria", "criteria_id", "element_id") and _has_columns(columns, "competency_elements", "element_id"):
                    sql = 'SELECT pc.* FROM performance_criteria pc JOIN competency_elements e ON e.element_id = pc.element_id ORDER BY pc.criteria_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, element_key = _text(row.get("criteria_id")), _text(row.get("element_id"))
                            if key and element_key:
                                emit("relationship", _edge("HAS_CRITERION", external_id("competency_element", element_key), external_id("performance_criterion", key), "performance_criteria", key, row, {"review_status": _status(row)}))

                if _has_columns(columns, "criteria_concept_links", "link_id", "criteria_id", "concept_id") and _has_columns(columns, "performance_criteria", "criteria_id") and _has_columns(columns, "ontology_concepts", "concept_id"):
                    sql = 'SELECT ccl.*, oc.concept_type AS _concept_type FROM criteria_concept_links ccl JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id ORDER BY ccl.link_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, criterion_key, concept_key = _text(row.get("link_id")), _text(row.get("criteria_id")), _text(row.get("concept_id"))
                            if key and criterion_key and concept_key:
                                props = _link_properties(row)
                                props.update({"criteria_id": criterion_key, "evidence_kind": "criteria_concept_link"})
                                emit("relationship", _edge(_requirement_type(row.get("_concept_type")), external_id("performance_criterion", criterion_key), external_id("ontology_concept", concept_key), "criteria_concept_links", key, row, props))

                    # Both summaries use SQL GROUP BY and bounded fetchmany batches.
                    # The job summary is the serving path for InternalRole -> NCSJob ->
                    # KSA context; it is still derived only from criterion evidence.
                    summary_count = 0
                    if _has_columns(columns, "performance_criteria", "element_id") and _has_columns(columns, "competency_elements", "element_id"):
                        for batch in _batches(conn, _criteria_concept_summary_sql(columns, scope="element"), batch_size=batch_size, batch_observer=observe):
                            for row in batch:
                                element_key, concept_key = _text(row.get("group_key")), _text(row.get("concept_key"))
                                if element_key and concept_key:
                                    summary_count += 1
                                    props = {
                                        "derived_from": "criteria_concept_links",
                                        "source_link_count": int(row["link_count"]),
                                        "distinct_criteria_count": int(row["criteria_count"]),
                                        "status_distribution_json": _distribution_from_pairs(row.get("status_pairs")),
                                        "method_distribution_json": _distribution_from_pairs(row.get("method_pairs")),
                                        "source_link_key_samples": _samples_from_pairs(row.get("samples")),
                                    }
                                    emit("relationship", _edge(_requirement_type(row.get("concept_type")), external_id("competency_element", element_key), external_id("ontology_concept", concept_key), "criteria_concept_links", f"summary:{element_key}:{concept_key}", {}, props))
                    diagnostics.append({"code": "element_concept_links_summarized", "distinct_edge_count": summary_count})

                    job_summary_count = 0
                    job_scope_possible = _has_columns(columns, "performance_criteria", "element_id") and _has_columns(columns, "competency_elements", "element_id", "unit_code") and _has_columns(columns, "competency_units", "unit_code", "classification_id") and _has_columns(columns, "classifications", "classification_id", "major_code", "middle_code", "small_code", "sub_code")
                    if job_scope_possible:
                        for batch in _batches(conn, _criteria_concept_summary_sql(columns, scope="job"), batch_size=batch_size, batch_observer=observe):
                            for row in batch:
                                job_key, concept_key = _text(row.get("group_key")), _text(row.get("concept_key"))
                                if job_key and concept_key:
                                    job_summary_count += 1
                                    props = {
                                        "derived_from": "criteria_concept_links",
                                        "summary_scope": "ncs_job_to_ksa",
                                        "source_link_count": int(row["link_count"]),
                                        "distinct_unit_count": int(row["unit_count"]),
                                        "distinct_element_count": int(row["element_count"]),
                                        "distinct_criteria_count": int(row["criteria_count"]),
                                        "status_distribution_json": _distribution_from_pairs(row.get("status_pairs")),
                                        "method_distribution_json": _distribution_from_pairs(row.get("method_pairs")),
                                        "source_link_key_samples": _samples_from_pairs(row.get("samples")),
                                    }
                                    job_id = external_id("ncs_job", job_key)
                                    emit("relationship", _edge(_requirement_type(row.get("concept_type")), job_id, external_id("ontology_concept", concept_key), "criteria_concept_links", f"job-summary:{job_id}:{concept_key}", {}, props))
                        diagnostics.append({"code": "job_concept_links_summarized", "distinct_edge_count": job_summary_count})

                if _has_columns(columns, "ncs_training_course_unit_links", "link_id", "training_course_id", "unit_code") and _has_columns(columns, "ncs_training_courses", "training_course_id") and _has_columns(columns, "competency_units", "unit_code"):
                    sql = 'SELECT l.* FROM ncs_training_course_unit_links l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN competency_units u ON u.unit_code = l.unit_code ORDER BY l.link_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, course_key, unit_key = _text(row.get("link_id")), _text(row.get("training_course_id")), _text(row.get("unit_code"))
                            if key and course_key and unit_key:
                                emit("relationship", _edge("COURSE_COVERS_UNIT", external_id("training_course", course_key), external_id("competency_unit", unit_key), "ncs_training_course_unit_links", key, row, _link_properties(row)))

                omitted_inherited = 0
                if _has_columns(columns, "ncs_training_course_concept_links", "link_id", "training_course_id", "concept_id") and _has_columns(columns, "ncs_training_courses", "training_course_id") and _has_columns(columns, "ontology_concepts", "concept_id"):
                    method_column = "l.link_method" if "link_method" in columns["ncs_training_course_concept_links"] else "NULL"
                    sql = f'SELECT l.*, {method_column} AS _link_method FROM ncs_training_course_concept_links l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN ontology_concepts oc ON oc.concept_id = l.concept_id ORDER BY l.link_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            if _text(row.get("_link_method")) == "unit_ksa_concept_inherited":
                                omitted_inherited += 1
                                continue
                            key, course_key, concept_key = _text(row.get("link_id")), _text(row.get("training_course_id")), _text(row.get("concept_id"))
                            if key and course_key and concept_key:
                                emit("relationship", _edge("COURSE_COVERS_CONCEPT", external_id("training_course", course_key), external_id("ontology_concept", concept_key), "ncs_training_course_concept_links", key, row, _link_properties(row, "unit_code", "element_id", "evidence_text")))

                for table, target_table, target_column, target_type, edge_type in (
                    ("ncs_training_course_element_links", "competency_elements", "element_id", "competency_element", "COURSE_COVERS_ELEMENT"),
                    ("training_goal_concept_links", "ontology_concepts", "concept_id", "ontology_concept", "COURSE_GOAL_COVERS_CONCEPT"),
                ):
                    if not _has_columns(columns, table, "link_id", "training_course_id", target_column) or not _has_columns(columns, "ncs_training_courses", "training_course_id") or not _has_columns(columns, target_table, target_column):
                        continue
                    sql = f'SELECT l.* FROM "{table}" l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN "{target_table}" t ON t."{target_column}" = l."{target_column}" ORDER BY l.link_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, course_key, target_key = _text(row.get("link_id")), _text(row.get("training_course_id")), _text(row.get(target_column))
                            if key and course_key and target_key:
                                emit("relationship", _edge(edge_type, external_id("training_course", course_key), external_id(target_type, target_key), table, key, row, _link_properties(row, "unit_code", "element_id", "evidence_text")))

                if _has_columns(columns, "training_delivery_relations", "relation_id", "training_course_id") and _has_columns(columns, "ncs_training_courses", "training_course_id"):
                    sql = 'SELECT d.* FROM training_delivery_relations d JOIN ncs_training_courses c ON c.training_course_id = d.training_course_id ORDER BY d.relation_id'
                    for batch in _batches(conn, sql, batch_size=batch_size, batch_observer=observe):
                        for row in batch:
                            key, course_key = _text(row.get("relation_id")), _text(row.get("training_course_id"))
                            if key and course_key:
                                emit("relationship", _edge("COURSE_HAS_DELIVERY", external_id("training_course", course_key), external_id("training_delivery", key), "training_delivery_relations", key, row, _link_properties(row, "evidence_text")))

                # Only reviewable hypotheses become graph edges.  Ambiguous or
                # unresolved candidates remain visible as role metadata and a
                # diagnostic, while an absent/wrong-kind target can never create a
                # dangling relationship.
                for candidate in validated_alignments:
                    candidate_public = candidate.to_public_dict()
                    if candidate.status not in {"candidate", "review_required"}:
                        diagnostics.append({
                            "code": "role_alignment_not_linked_status",
                            "role_gold_id": candidate.role_gold_id,
                            "status": candidate.status,
                            "ncs_target_type": candidate.ncs_target_type,
                            "ncs_target_key": candidate.ncs_target_key,
                        })
                        continue
                    target_type = candidate.ncs_target_type.strip().casefold().replace("-", "_")
                    if target_type not in {"ncs_job", "ncsjob"} or candidate.ncs_target_key not in seen_jobs:
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
                    emit("relationship", _edge(
                        "ALIGNED_TO",
                        external_id("internal_job_role", candidate.role_gold_id),
                        external_id("ncs_job", candidate.ncs_target_key),
                        "role_alignments_input",
                        candidate_key,
                        candidate_public,
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

                diagnostics.extend([
                    {"code": "task_ksa_concept_relations_omitted", "reason": "serving_core_profile"},
                    {"code": "streaming_export", "batch_size": batch_size},
                ])
                if omitted_inherited:
                    diagnostics.append({"code": "inherited_course_concept_links_omitted", "count": omitted_inherited, "link_method": "unit_ksa_concept_inherited"})

                # Gold is a bounded serving projection, not a second source of
                # truth.  Count-only fallback metadata makes that boundary
                # machine-readable without exposing source rows or mutating SQLite.
                scope_table_counts: dict[str, dict[str, Any]] = {
                    "task_ksa_concept_relations": {
                        "present": "task_ksa_concept_relations" in columns,
                        "row_count": (
                            int(conn.execute('SELECT COUNT(*) FROM "task_ksa_concept_relations"').fetchone()[0])
                            if "task_ksa_concept_relations" in columns
                            else 0
                        ),
                    }
                }
                for table in SQLITE_AUTHORITATIVE_FALLBACK_TABLES:
                    present = table in columns
                    scope_table_counts[table] = {
                        "present": present,
                        "row_count": int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]) if present else 0,
                    }
                scope_contract = serving_core_scope_contract(
                    scope_table_counts,
                    profile=profile,
                    omitted_inherited_training_concept_links=omitted_inherited,
                )
                for fallback in scope_contract["fallback_evidence"]:
                    diagnostics.append(
                        {
                            "code": "sqlite_authoritative_fallback",
                            "table": fallback["table"],
                            "present": fallback["present"],
                            "row_count": fallback["row_count"],
                        }
                    )
                for diagnostic in sorted(diagnostics, key=_property_safe_json):
                    emit("diagnostic", diagnostic)

                manifest = {
                    "schema": GOLD_LPG_NDJSON_SCHEMA,
                    "projection_schema": GOLD_LPG_SCHEMA,
                    "profile": profile.to_dict(),
                    "node_count": sum(node_counts.values()), "edge_count": sum(edge_counts.values()),
                    "node_counts": dict(sorted(node_counts.items())), "edge_counts": dict(sorted(edge_counts.items())),
                    "diagnostic_count": len(diagnostics), "source_tables": sorted(source_tables),
                    "records_before_manifest": record_count,
                    "records_sha256": digest.hexdigest(),
                    "batch_size": batch_size, "max_observed_batch_rows": max_observed_batch_rows,
                    "snapshot": snapshot,
                    "scope_contract": scope_contract,
                    "read_only": True, "db_writes": False, "approval_claim": False,
                }
                _write_json_line(handle, _record("manifest", manifest))
                handle.flush()
                os.fsync(handle.fileno())

            Path(temp_name).replace(destination)
            return manifest
        except Exception:
            if temp_name:
                Path(temp_name).unlink(missing_ok=True)
            raise


__all__ = ["GOLD_LPG_NDJSON_SCHEMA", "MAX_STREAM_BATCH_SIZE", "export_gold_lpg_ndjson"]
