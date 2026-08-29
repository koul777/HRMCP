"""Export the read-only NCS graph used by hosted MCP deployments.

The canonical NCS_MCP database contains recommendation, ontology, training, and
audit tables and is intentionally large.  This utility creates a controlled
derived snapshot for interview/MCP serving use cases.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.compact_postings import (  # noqa: E402
    COMPACT_POSTING_CODEC,
    CRITERIA_CONCEPT_FORWARD_TABLE,
    CRITERIA_CONCEPT_INVERSE_TABLE,
    ONTOLOGY_RELATION_INCOMING_TABLE,
    ONTOLOGY_RELATION_OUTGOING_TABLE,
    ONTOLOGY_RELATION_TYPE_TABLE,
    encode_posting_ids,
)
from ncs_mcp.compact_storage import (  # noqa: E402
    ATOMIC_COMPACT_TABLE,
    JOB_BASE_COMPACT_TABLE,
    TRAINING_CONCEPT_COMPACT_TABLE,
    TRAINING_DELIVERY_COMPACT_TABLE,
    TRAINING_ELEMENT_COMPACT_TABLE,
    TRAINING_GOAL_COMPACT_TABLE,
    compact_canonical_objects,
    compact_physical_tables,
    create_atomic_storage,
    create_job_base_storage,
    create_training_storage,
)


CORE_TABLES = (
    "classifications",
    "competency_units",
    "competency_elements",
    "performance_criteria",
    "ksa_items",
    # The MCP readiness check expects this table.  It is small compared with
    # the ontology/recommendation graph and keeps the serving endpoint healthy.
    "ncs_training_courses",
)

TRAINING_LINK_TABLES = (
    "ncs_training_course_unit_links",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
)

ONTOLOGY_CORE_TABLES = (
    "ontology_concepts",
    "ontology_concept_aliases",
    "ontology_concept_relations",
    "ontology_concept_label_candidates",
    "ksa_concept_links",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "criteria_concept_links",
)

ONTOLOGY_TASK_TABLES = (
    "task_ksa_concept_relations",
    "task_similarity_links",
    "ncs_unit_job_base_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_unit_qualification_links",
    "ncs_qualification_items",
    "training_transition_gold_scenarios",
    "training_transition_scenario_reviews",
)

PROFILE_DEFAULT = "default"
PROFILE_VERCEL_ONTOLOGY_LIGHT = "vercel-ontology-light"
PROFILE_VERCEL_ONTOLOGY_COMPLETE = "vercel-ontology-complete"
PROFILE_VERCEL_ONTOLOGY_COMPACT = "vercel-ontology-compact"
SUPPORTED_PROFILES = (
    PROFILE_DEFAULT,
    PROFILE_VERCEL_ONTOLOGY_LIGHT,
    PROFILE_VERCEL_ONTOLOGY_COMPLETE,
    PROFILE_VERCEL_ONTOLOGY_COMPACT,
)

VERCEL_COMPACT_MAX_BYTES = 480_000_000
VERCEL_COMPACT_SCHEMA = "ncs_vercel_ontology_compact_v2"

VERCEL_ONTOLOGY_COMPACT_DIRECT_TABLES = (
    *CORE_TABLES,
    # Unit links are small and remain directly materialized. The four large
    # generated training relation tables are numeric physical tables with
    # canonical compatibility views.
    "ncs_training_course_unit_links",
    "ksa_concept_links",
    "ncs_career_paths",
    "ncs_qualification_items",
    "ncs_unit_qualification_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_unit_standard_training",
    "training_transition_gold_scenarios",
    "training_transition_scenario_reviews",
)

VERCEL_ONTOLOGY_COMPACT_REPLACED_TABLES = (
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
    "ncs_unit_job_base_links",
)

VERCEL_ONTOLOGY_COMPACT_NULL_COLUMNS = {
    "created_at",
    "updated_at",
    "api_fetched_at",
    "source_payload",
    "evidence_text",
}

VERCEL_ONTOLOGY_LIGHT_DIRECT_TABLES = (
    "ncs_training_course_unit_links",
    "ncs_training_course_element_links",
    "training_delivery_relations",
    "ncs_career_paths",
    "ncs_qualification_items",
    "ncs_unit_qualification_links",
    "ncs_unit_standard_training",
)

VERCEL_ONTOLOGY_LIGHT_EMPTY_COMPATIBILITY_TABLES = (
    "ontology_concept_relations",
    "criteria_concept_links",
    "learning_module_concept_links",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "element_criteria_ksa_links",
    "ncs_unit_job_base_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_external_training_zip_courses",
    "ncs_occupation_code_mappings",
    "quality_issues",
)

# The complete profile preserves the evidence graph used by NCS task/KSA and
# training recommendations.  ``task_ksa_concept_relations`` is intentionally
# handled separately: its repeated text and derivable identifiers dominate the
# canonical DB size, so the serving snapshot stores a lossless compact relation
# key and exposes the legacy shape through a compatibility view.
VERCEL_ONTOLOGY_COMPLETE_DIRECT_TABLES = (
    *CORE_TABLES,
    *TRAINING_LINK_TABLES,
    "ontology_concepts",
    "ontology_concept_aliases",
    "ontology_concept_relations",
    "ontology_concept_label_candidates",
    "ksa_concept_links",
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
    "criteria_concept_links",
    "task_similarity_links",
    "element_criteria_ksa_links",
    "quality_issues",
    "ncs_unit_job_base_links",
    "ncs_job_base_competencies",
    "ncs_job_base_factors",
    "ncs_career_paths",
    "ncs_unit_qualification_links",
    "ncs_qualification_items",
    "ncs_unit_standard_training",
    "ncs_external_training_zip_courses",
    "ncs_occupation_code_mappings",
    "training_transition_gold_scenarios",
    "training_transition_scenario_reviews",
)

# Public ontology search preserves the historical ``learning_module_count``
# output field even though learning modules are outside the active NCS product
# scope.  Keep only the source schema, with zero rows, so that the stable query
# contract works without shipping legacy learning-module data.
VERCEL_ONTOLOGY_COMPLETE_EMPTY_COMPATIBILITY_TABLES = (
    "learning_module_concept_links",
)

VERCEL_ONTOLOGY_COMPLETE_NULL_COLUMNS = {
    # Reproducible serving snapshots do not need source/write timestamps.
    "created_at",
    "updated_at",
    "detected_at",
    "api_fetched_at",
    # These payload/evidence fields duplicate rows already preserved elsewhere
    # in the graph.  Review and link status columns are deliberately retained.
    "source_payload",
}

TASK_KSA_RELATION_SOURCE_TABLE = "task_ksa_concept_relations"
TASK_KSA_RELATION_COMPACT_TABLE = "task_ksa_relations_compact"
TASK_KSA_RELATION_TYPE_TABLE = "task_ksa_relation_types"
TASK_KSA_REVIEW_STATUS_TABLE = "task_ksa_review_statuses"


def _resolve_tables(
    *,
    include_training_links: bool,
    include_ontology: bool,
    include_task_ontology: bool,
) -> tuple[str, ...]:
    selected = list(CORE_TABLES)
    if include_training_links:
        selected.extend(TRAINING_LINK_TABLES)
    if include_ontology:
        selected.extend(ONTOLOGY_CORE_TABLES)
    if include_task_ontology:
        selected.extend(ONTOLOGY_TASK_TABLES)
    return tuple(selected)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
    )


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_columns(conn: sqlite3.Connection, name: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_quote(name)})").fetchall()
    )


def _source_table_names(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }


def _require_columns(
    conn: sqlite3.Connection,
    table: str,
    required: set[str],
) -> tuple[str, ...]:
    columns = _table_columns(conn, table)
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError(
            f"source table has incompatible schema: {table}; missing columns: {missing}"
        )
    return columns


def _projection(
    columns: tuple[str, ...],
    *,
    alias: str,
    null_columns: set[str] | None = None,
) -> str:
    nulls = null_columns or set()
    return ", ".join(
        (
            f"NULL AS {_quote(column)}"
            if column in nulls
            else f"{alias}.{_quote(column)}"
        )
        for column in columns
    )


def _copy_source_table(
    dst: sqlite3.Connection,
    table: str,
    *,
    where: str | None = None,
) -> None:
    where_clause = f" WHERE {where}" if where else ""
    dst.execute(
        f"CREATE TABLE {_quote(table)} AS "
        f"SELECT * FROM source.{_quote(table)}{where_clause}"
    )


def _copy_projected_source_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    *,
    null_columns: set[str],
) -> None:
    columns = _require_columns(src, table, null_columns)
    select_columns = _projection(columns, alias="item", null_columns=null_columns)
    dst.execute(
        f"CREATE TABLE {_quote(table)} AS "
        f"SELECT {select_columns} FROM source.{_quote(table)} AS item"
    )


def _copy_compacted_source_table(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    table: str,
    *,
    null_columns: set[str] | None = None,
) -> None:
    """Copy a table while nulling only columns that exist in its source schema."""
    columns = _table_columns(src, table)
    selected_nulls = set(null_columns or ()) & set(columns)
    select_columns = _projection(
        columns,
        alias="item",
        null_columns=selected_nulls,
    )
    dst.execute(
        f"CREATE TABLE {_quote(table)} AS "
        f"SELECT {select_columns} FROM source.{_quote(table)} AS item"
    )


def _copy_complete_concepts(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> None:
    columns = _require_columns(
        src,
        "ontology_concepts",
        {
            "definition",
            "definition_source",
            "definition_status",
            "review_status",
        },
    )
    boilerplate = """
        (
            LOWER(TRIM(COALESCE(item.review_status, ''))) <> 'human_reviewed'
            AND LOWER(TRIM(COALESCE(item.definition_status, ''))) <> 'defined'
            AND (
                LOWER(TRIM(COALESCE(item.definition_source, ''))) IN (
                    'boilerplate',
                    'ksa_meaning_candidates.term_definition_template',
                    'ksa_meaning_candidate_promotion'
                )
                OR COALESCE(item.definition, '') LIKE
                   '%업무 판단과 문제 해결에 필요한 관련 원리, 기준, 절차, 사례에 대한 지식.'
                OR COALESCE(item.definition, '') LIKE
                   '%업무 상황에서 관련 절차나 도구를 활용해 과업을 수행하는 능력.'
                OR COALESCE(item.definition, '') LIKE
                   '%업무 수행 과정에서 품질, 협업, 책임성을 유지하기 위한 태도.'
            )
        )
    """
    expressions: list[str] = []
    for column in columns:
        if column == "definition":
            expression = f"CASE WHEN {boilerplate} THEN NULL ELSE item.definition END"
        elif column == "definition_source":
            expression = (
                f"CASE WHEN {boilerplate} THEN NULL ELSE item.definition_source END"
            )
        elif column in {"created_at", "updated_at"}:
            expression = "NULL"
        else:
            expression = f"item.{_quote(column)}"
        expressions.append(f"{expression} AS {_quote(column)}")
    dst.execute(
        "CREATE TABLE ontology_concepts AS "
        f"SELECT {', '.join(expressions)} "
        "FROM source.ontology_concepts AS item"
    )


def _copy_complete_aliases_and_merge_reviewed_labels(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> int:
    alias_columns = _require_columns(
        src,
        "ontology_concept_aliases",
        {
            "alias_id",
            "concept_id",
            "alias_text",
            "normalized_alias_key",
            "alias_source",
        },
    )
    _require_columns(
        src,
        "ontology_concept_label_candidates",
        {
            "label_id",
            "concept_id",
            "label_text",
            "normalized_label_key",
            "review_status",
        },
    )
    select_columns = _projection(
        alias_columns,
        alias="alias",
        null_columns={"created_at"},
    )
    dst.execute(
        "CREATE TABLE ontology_concept_aliases AS "
        f"SELECT {select_columns} "
        "FROM source.ontology_concept_aliases AS alias"
    )

    value_by_column = {
        "alias_id": "new_alias_id",
        "concept_id": "concept_id",
        "alias_text": "label_text",
        "normalized_alias_key": "normalized_label_key",
        "alias_source": "'ontology_label_human_reviewed'",
        "created_at": "NULL",
    }
    insert_columns = ", ".join(_quote(column) for column in alias_columns)
    insert_values = ", ".join(
        f"{value_by_column.get(column, 'NULL')} AS {_quote(column)}"
        for column in alias_columns
    )
    dst.execute(
        f"""
        WITH reviewed AS (
            SELECT
                candidate.label_id,
                candidate.concept_id,
                candidate.label_text,
                candidate.normalized_label_key,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        candidate.concept_id,
                        LOWER(TRIM(candidate.normalized_label_key)),
                        LOWER(TRIM(candidate.label_text))
                    ORDER BY candidate.label_id
                ) AS duplicate_rank
            FROM source.ontology_concept_label_candidates AS candidate
            WHERE candidate.review_status = 'human_reviewed'
        ),
        missing AS (
            SELECT reviewed.*
            FROM reviewed
            WHERE reviewed.duplicate_rank = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM ontology_concept_aliases AS alias
                  WHERE alias.concept_id = reviewed.concept_id
                    AND (
                        LOWER(TRIM(alias.normalized_alias_key)) =
                            LOWER(TRIM(reviewed.normalized_label_key))
                        OR LOWER(TRIM(alias.alias_text)) =
                            LOWER(TRIM(reviewed.label_text))
                    )
              )
        ),
        numbered AS (
            SELECT
                COALESCE((SELECT MAX(alias_id) FROM ontology_concept_aliases), 0)
                    + ROW_NUMBER() OVER (
                        ORDER BY concept_id, normalized_label_key, label_text, label_id
                    ) AS new_alias_id,
                concept_id,
                label_text,
                normalized_label_key
            FROM missing
        )
        INSERT INTO ontology_concept_aliases ({insert_columns})
        SELECT {insert_values}
        FROM numbered
        """
    )
    return int(dst.execute("SELECT changes()").fetchone()[0])


def _merge_reviewed_labels_into_aliases(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> int:
    """Add only missing human-reviewed labels to an existing alias table."""
    alias_columns = _require_columns(
        src,
        "ontology_concept_aliases",
        {
            "alias_id",
            "concept_id",
            "alias_text",
            "normalized_alias_key",
            "alias_source",
        },
    )
    _require_columns(
        src,
        "ontology_concept_label_candidates",
        {
            "label_id",
            "concept_id",
            "label_text",
            "normalized_label_key",
            "review_status",
        },
    )
    value_by_column = {
        "alias_id": "new_alias_id",
        "concept_id": "concept_id",
        "alias_text": "label_text",
        "normalized_alias_key": "normalized_label_key",
        "alias_source": "'ontology_label_human_reviewed'",
        "created_at": "NULL",
    }
    insert_columns = ", ".join(_quote(column) for column in alias_columns)
    insert_values = ", ".join(
        f"{value_by_column.get(column, 'NULL')} AS {_quote(column)}"
        for column in alias_columns
    )
    dst.execute(
        f"""
        WITH reviewed AS (
            SELECT
                candidate.label_id,
                candidate.concept_id,
                candidate.label_text,
                candidate.normalized_label_key,
                ROW_NUMBER() OVER (
                    PARTITION BY
                        candidate.concept_id,
                        LOWER(TRIM(candidate.normalized_label_key)),
                        LOWER(TRIM(candidate.label_text))
                    ORDER BY candidate.label_id
                ) AS duplicate_rank
            FROM source.ontology_concept_label_candidates AS candidate
            WHERE LOWER(TRIM(candidate.review_status)) = 'human_reviewed'
        ),
        missing AS (
            SELECT reviewed.*
            FROM reviewed
            WHERE reviewed.duplicate_rank = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM ontology_concept_aliases AS alias
                  WHERE alias.concept_id = reviewed.concept_id
                    AND (
                        LOWER(TRIM(alias.normalized_alias_key)) =
                            LOWER(TRIM(reviewed.normalized_label_key))
                        OR LOWER(TRIM(alias.alias_text)) =
                            LOWER(TRIM(reviewed.label_text))
                    )
              )
        ),
        numbered AS (
            SELECT
                COALESCE((SELECT MAX(alias_id) FROM ontology_concept_aliases), 0)
                    + ROW_NUMBER() OVER (
                        ORDER BY concept_id, normalized_label_key, label_text, label_id
                    ) AS new_alias_id,
                concept_id,
                label_text,
                normalized_label_key
            FROM missing
        )
        INSERT INTO ontology_concept_aliases ({insert_columns})
        SELECT {insert_values}
        FROM numbered
        """
    )
    return int(dst.execute("SELECT changes()").fetchone()[0])


def _copy_human_reviewed_label_candidates(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> int:
    columns = _require_columns(
        src,
        "ontology_concept_label_candidates",
        {"label_id", "concept_id", "label_text", "review_status"},
    )
    selected_nulls = {
        "source_text",
        "evidence_text",
        "created_at",
        "updated_at",
    } & set(columns)
    select_columns = _projection(
        columns,
        alias="candidate",
        null_columns=selected_nulls,
    )
    dst.execute(
        "CREATE TABLE ontology_concept_label_candidates AS "
        f"SELECT {select_columns} "
        "FROM source.ontology_concept_label_candidates AS candidate "
        "WHERE LOWER(TRIM(candidate.review_status)) = 'human_reviewed'"
    )
    return int(
        dst.execute(
            "SELECT COUNT(*) FROM ontology_concept_label_candidates"
        ).fetchone()[0]
    )


def _flush_posting_batch(
    dst: sqlite3.Connection,
    insert_sql: str,
    batch: list[tuple[object, ...]],
) -> None:
    if batch:
        dst.executemany(insert_sql, batch)
        batch.clear()


def _write_two_key_postings(
    dst: sqlite3.Connection,
    *,
    select_sql: str,
    insert_sql: str,
) -> tuple[int, int]:
    current_key: tuple[int, int] | None = None
    values: list[int] = []
    posting_rows = 0
    logical_rows = 0
    batch: list[tuple[object, ...]] = []
    for raw_key_1, raw_key_2, raw_value in dst.execute(select_sql):
        key = (int(raw_key_1), int(raw_key_2))
        value = int(raw_value)
        if current_key is not None and key != current_key:
            payload = encode_posting_ids(values)
            batch.append((*current_key, len(values), payload))
            posting_rows += 1
            logical_rows += len(values)
            if len(batch) >= 5_000:
                _flush_posting_batch(dst, insert_sql, batch)
            values = []
        current_key = key
        values.append(value)
    if current_key is not None:
        payload = encode_posting_ids(values)
        batch.append((*current_key, len(values), payload))
        posting_rows += 1
        logical_rows += len(values)
    _flush_posting_batch(dst, insert_sql, batch)
    return posting_rows, logical_rows


def _write_one_key_postings(
    dst: sqlite3.Connection,
    *,
    select_sql: str,
    insert_sql: str,
) -> tuple[int, int]:
    current_key: int | None = None
    values: list[int] = []
    posting_rows = 0
    logical_rows = 0
    batch: list[tuple[object, ...]] = []
    for raw_key, raw_value in dst.execute(select_sql):
        key = int(raw_key)
        value = int(raw_value)
        if current_key is not None and key != current_key:
            payload = encode_posting_ids(values)
            batch.append((current_key, len(values), payload))
            posting_rows += 1
            logical_rows += len(values)
            if len(batch) >= 5_000:
                _flush_posting_batch(dst, insert_sql, batch)
            values = []
        current_key = key
        values.append(value)
    if current_key is not None:
        payload = encode_posting_ids(values)
        batch.append((current_key, len(values), payload))
        posting_rows += 1
        logical_rows += len(values)
    _flush_posting_batch(dst, insert_sql, batch)
    return posting_rows, logical_rows


def _create_ontology_relation_postings(
    dst: sqlite3.Connection,
) -> tuple[dict[str, int], int]:
    relation_statuses = {
        str(row[0] or "").strip().lower()
        for row in dst.execute(
            "SELECT DISTINCT review_status FROM source.ontology_concept_relations"
        ).fetchall()
    }
    if relation_statuses != {"candidate"}:
        raise RuntimeError(
            "compact ontology postings require a candidate-only source graph; "
            f"found review statuses: {sorted(relation_statuses)}"
        )
    labels_by_type: dict[str, set[object]] = {}
    for relation_type, relation_label in dst.execute(
        """
        SELECT relation_type, relation_label
        FROM source.ontology_concept_relations
        ORDER BY relation_type, relation_label
        """
    ).fetchall():
        labels_by_type.setdefault(str(relation_type), set()).add(relation_label)
    ambiguous_labels = [
        relation_type
        for relation_type, labels in labels_by_type.items()
        if len(labels) != 1 or None in labels
    ]
    if ambiguous_labels:
        raise RuntimeError(
            "compact ontology postings require one relation_label per "
            "relation_type; ambiguous relation types: "
            f"{ambiguous_labels}"
        )
    dst.executescript(
        f"""
        CREATE TABLE {ONTOLOGY_RELATION_TYPE_TABLE} (
            relation_type_code INTEGER PRIMARY KEY,
            relation_type TEXT NOT NULL UNIQUE,
            relation_label TEXT NOT NULL
        );
        INSERT INTO {ONTOLOGY_RELATION_TYPE_TABLE}(relation_type, relation_label)
        SELECT relation_type, MIN(relation_label)
        FROM source.ontology_concept_relations
        GROUP BY relation_type
        ORDER BY relation_type;

        CREATE TABLE {ONTOLOGY_RELATION_OUTGOING_TABLE} (
            source_concept_id INTEGER NOT NULL,
            relation_type_code INTEGER NOT NULL,
            target_count INTEGER NOT NULL,
            target_ids BLOB NOT NULL,
            PRIMARY KEY(source_concept_id, relation_type_code)
        ) WITHOUT ROWID;
        CREATE TABLE {ONTOLOGY_RELATION_INCOMING_TABLE} (
            target_concept_id INTEGER NOT NULL,
            relation_type_code INTEGER NOT NULL,
            source_count INTEGER NOT NULL,
            source_ids BLOB NOT NULL,
            PRIMARY KEY(target_concept_id, relation_type_code)
        ) WITHOUT ROWID;
        """
    )
    outgoing_rows, outgoing_edges = _write_two_key_postings(
        dst,
        select_sql=f"""
            SELECT relation.source_concept_id, relation_type.relation_type_code,
                   relation.target_concept_id
            FROM source.ontology_concept_relations AS relation
            JOIN main.{ONTOLOGY_RELATION_TYPE_TABLE} AS relation_type
              ON relation_type.relation_type = relation.relation_type
            WHERE LOWER(COALESCE(relation.review_status, '')) <> 'rejected'
            ORDER BY relation.source_concept_id,
                     relation_type.relation_type_code,
                     relation.target_concept_id
        """,
        insert_sql=f"""
            INSERT INTO {ONTOLOGY_RELATION_OUTGOING_TABLE}
            (source_concept_id, relation_type_code, target_count, target_ids)
            VALUES (?, ?, ?, ?)
        """,
    )
    incoming_rows, incoming_edges = _write_two_key_postings(
        dst,
        select_sql=f"""
            SELECT relation.target_concept_id, relation_type.relation_type_code,
                   relation.source_concept_id
            FROM source.ontology_concept_relations AS relation
            JOIN main.{ONTOLOGY_RELATION_TYPE_TABLE} AS relation_type
              ON relation_type.relation_type = relation.relation_type
            WHERE LOWER(COALESCE(relation.review_status, '')) <> 'rejected'
            ORDER BY relation.target_concept_id,
                     relation_type.relation_type_code,
                     relation.source_concept_id
        """,
        insert_sql=f"""
            INSERT INTO {ONTOLOGY_RELATION_INCOMING_TABLE}
            (target_concept_id, relation_type_code, source_count, source_ids)
            VALUES (?, ?, ?, ?)
        """,
    )
    if outgoing_edges != incoming_edges:
        raise RuntimeError(
            "ontology relation posting parity failed: "
            f"outgoing={outgoing_edges}, incoming={incoming_edges}"
        )
    return {
        ONTOLOGY_RELATION_OUTGOING_TABLE: outgoing_rows,
        ONTOLOGY_RELATION_INCOMING_TABLE: incoming_rows,
    }, outgoing_edges


def _create_criteria_concept_postings(
    dst: sqlite3.Connection,
) -> tuple[dict[str, int], int]:
    expected_statuses = (
        ("criteria_concept_links", "link_status", {"raw"}),
        ("ksa_atomic_items", "review_status", {"raw"}),
        ("ksa_atomic_concept_links", "link_status", {"raw"}),
    )
    for table, column, expected in expected_statuses:
        actual = {
            str(row[0] or "").strip().lower()
            for row in dst.execute(
                f"SELECT DISTINCT {_quote(column)} FROM source.{_quote(table)}"
            ).fetchall()
        }
        if actual != expected:
            raise RuntimeError(
                "compact criterion postings refuse to collapse review states: "
                f"{table}.{column}={sorted(actual)}; expected {sorted(expected)}"
            )
    dst.executescript(
        f"""
        CREATE TEMP TABLE criteria_concept_edge_stage (
            criteria_id INTEGER NOT NULL,
            concept_id INTEGER NOT NULL,
            PRIMARY KEY(criteria_id, concept_id)
        ) WITHOUT ROWID;
        INSERT OR IGNORE INTO criteria_concept_edge_stage(criteria_id, concept_id)
        SELECT criteria_id, concept_id
        FROM source.criteria_concept_links
        WHERE LOWER(COALESCE(link_status, '')) <> 'rejected';
        INSERT OR IGNORE INTO criteria_concept_edge_stage(criteria_id, concept_id)
        SELECT source_link.criteria_id, atomic_link.concept_id
        FROM source.element_criteria_ksa_links AS source_link
        JOIN source.ksa_atomic_items AS atomic
          ON atomic.ksa_id = source_link.ksa_id
        JOIN source.ksa_atomic_concept_links AS atomic_link
          ON atomic_link.atomic_id = atomic.atomic_id
        WHERE LOWER(COALESCE(atomic.review_status, '')) <> 'rejected'
          AND LOWER(COALESCE(atomic_link.link_status, '')) <> 'rejected';
        CREATE INDEX temp.idx_criteria_concept_stage_inverse
        ON criteria_concept_edge_stage(concept_id, criteria_id);

        CREATE TABLE {CRITERIA_CONCEPT_FORWARD_TABLE} (
            criteria_id INTEGER PRIMARY KEY,
            concept_count INTEGER NOT NULL,
            concept_ids BLOB NOT NULL
        ) WITHOUT ROWID;
        CREATE TABLE {CRITERIA_CONCEPT_INVERSE_TABLE} (
            concept_id INTEGER PRIMARY KEY,
            criteria_count INTEGER NOT NULL,
            criteria_ids BLOB NOT NULL
        ) WITHOUT ROWID;
        """
    )
    forward_rows, forward_edges = _write_one_key_postings(
        dst,
        select_sql="""
            SELECT criteria_id, concept_id
            FROM criteria_concept_edge_stage
            ORDER BY criteria_id, concept_id
        """,
        insert_sql=f"""
            INSERT INTO {CRITERIA_CONCEPT_FORWARD_TABLE}
            (criteria_id, concept_count, concept_ids)
            VALUES (?, ?, ?)
        """,
    )
    inverse_rows, inverse_edges = _write_one_key_postings(
        dst,
        select_sql="""
            SELECT concept_id, criteria_id
            FROM criteria_concept_edge_stage
            ORDER BY concept_id, criteria_id
        """,
        insert_sql=f"""
            INSERT INTO {CRITERIA_CONCEPT_INVERSE_TABLE}
            (concept_id, criteria_count, criteria_ids)
            VALUES (?, ?, ?)
        """,
    )
    if forward_edges != inverse_edges:
        raise RuntimeError(
            "criteria concept posting parity failed: "
            f"forward={forward_edges}, inverse={inverse_edges}"
        )
    dst.execute("DROP TABLE criteria_concept_edge_stage")
    return {
        CRITERIA_CONCEPT_FORWARD_TABLE: forward_rows,
        CRITERIA_CONCEPT_INVERSE_TABLE: inverse_rows,
    }, forward_edges


def _stable_ksa_sha256(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for ksa_id, text in conn.execute(
        "SELECT ksa_id, ksa_text_raw FROM ksa_items ORDER BY ksa_id"
    ):
        encoded = str(text or "").encode("utf-8")
        digest.update(int(ksa_id).to_bytes(8, "big", signed=False))
        digest.update(len(encoded).to_bytes(4, "big", signed=False))
        digest.update(encoded)
    return digest.hexdigest()


def _copy_compact_task_ksa_relations(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> None:
    _require_columns(
        src,
        TASK_KSA_RELATION_SOURCE_TABLE,
        {
            "relation_id",
            "criteria_id",
            "source_atomic_id",
            "target_atomic_id",
            "relation_type",
            "confidence_score",
            "review_status",
        },
    )
    dst.executescript(
        f"""
        CREATE TABLE {TASK_KSA_RELATION_TYPE_TABLE} (
            relation_type_code INTEGER PRIMARY KEY,
            relation_type TEXT NOT NULL UNIQUE
        );
        INSERT INTO {TASK_KSA_RELATION_TYPE_TABLE} (relation_type)
        SELECT DISTINCT relation_type
        FROM source.{TASK_KSA_RELATION_SOURCE_TABLE}
        ORDER BY relation_type;

        CREATE TABLE {TASK_KSA_REVIEW_STATUS_TABLE} (
            review_status_code INTEGER PRIMARY KEY,
            review_status TEXT NOT NULL UNIQUE
        );
        INSERT INTO {TASK_KSA_REVIEW_STATUS_TABLE} (review_status)
        SELECT DISTINCT review_status
        FROM source.{TASK_KSA_RELATION_SOURCE_TABLE}
        ORDER BY review_status;

        CREATE TABLE {TASK_KSA_RELATION_COMPACT_TABLE} (
            relation_id INTEGER PRIMARY KEY,
            criteria_id INTEGER NOT NULL,
            source_atomic_id INTEGER NOT NULL,
            target_atomic_id INTEGER NOT NULL,
            relation_type_code INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            review_status_code INTEGER NOT NULL
        );
        INSERT INTO {TASK_KSA_RELATION_COMPACT_TABLE} (
            relation_id,
            criteria_id,
            source_atomic_id,
            target_atomic_id,
            relation_type_code,
            confidence_score,
            review_status_code
        )
        SELECT
            relation.relation_id,
            relation.criteria_id,
            relation.source_atomic_id,
            relation.target_atomic_id,
            relation_type.relation_type_code,
            relation.confidence_score,
            review_status.review_status_code
        FROM source.{TASK_KSA_RELATION_SOURCE_TABLE} AS relation
        JOIN {TASK_KSA_RELATION_TYPE_TABLE} AS relation_type
          ON relation_type.relation_type = relation.relation_type
        JOIN {TASK_KSA_REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = relation.review_status
        ORDER BY relation.relation_id;

        CREATE VIEW {TASK_KSA_RELATION_SOURCE_TABLE} AS
        SELECT
            relation.relation_id AS relation_id,
            relation.criteria_id AS criteria_id,
            criteria.element_id AS element_id,
            (
                SELECT link.concept_id
                FROM ksa_atomic_concept_links AS link
                WHERE link.atomic_id = relation.source_atomic_id
                ORDER BY link.link_id
                LIMIT 1
            ) AS source_concept_id,
            relation_type.relation_type AS relation_type,
            (
                SELECT link.concept_id
                FROM ksa_atomic_concept_links AS link
                WHERE link.atomic_id = relation.target_atomic_id
                ORDER BY link.link_id
                LIMIT 1
            ) AS target_concept_id,
            relation.source_atomic_id AS source_atomic_id,
            relation.target_atomic_id AS target_atomic_id,
            NULL AS evidence_text,
            relation.confidence_score AS confidence_score,
            review_status.review_status AS review_status,
            NULL AS created_at
        FROM {TASK_KSA_RELATION_COMPACT_TABLE} AS relation
        JOIN {TASK_KSA_RELATION_TYPE_TABLE} AS relation_type
          ON relation_type.relation_type_code = relation.relation_type_code
        JOIN {TASK_KSA_REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = relation.review_status_code
        LEFT JOIN performance_criteria AS criteria
          ON criteria.criteria_id = relation.criteria_id;
        """
    )


def _copy_lightweight_aliases(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> None:
    required_alias_columns = {
        "concept_id",
        "alias_text",
        "normalized_alias_key",
    }
    columns = _require_columns(
        src,
        "ontology_concept_aliases",
        required_alias_columns,
    )
    _require_columns(
        src,
        "ontology_concepts",
        {"concept_id", "concept_name", "normalized_key"},
    )
    select_columns = _projection(columns, alias="alias")
    dst.execute(
        f"""
        CREATE TABLE ontology_concept_aliases AS
        SELECT {select_columns}
        FROM source.ontology_concept_aliases AS alias
        JOIN source.ontology_concepts AS concept
          ON concept.concept_id = alias.concept_id
        WHERE LOWER(TRIM(COALESCE(alias.normalized_alias_key, '')))
              <> LOWER(TRIM(COALESCE(concept.normalized_key, '')))
           OR LOWER(TRIM(COALESCE(alias.alias_text, '')))
              <> LOWER(TRIM(COALESCE(concept.concept_name, '')))
        """
    )


def _copy_query_aliases(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> bool:
    """Copy query aliases and return True when a compatibility table was created."""
    if _table_exists(src, "ncs_query_aliases"):
        _copy_source_table(dst, "ncs_query_aliases")
        return False
    dst.execute(
        """
        CREATE TABLE ncs_query_aliases (
            alias_id INTEGER PRIMARY KEY,
            unit_code TEXT,
            alias_text TEXT,
            normalized_query TEXT
        )
        """
    )
    return True


def _execute_indexes(
    dst: sqlite3.Connection,
    statements: tuple[str, ...],
    *,
    strict: bool = False,
) -> None:
    for statement in statements:
        try:
            dst.execute(statement)
        except sqlite3.OperationalError:
            if strict:
                raise
            # Preserve the existing exporter behavior for old-but-readable source
            # schemas whose optional columns do not support every serving index.
            continue


def _validate_profile_selection(
    *,
    profile: str,
    include_training_links: bool,
    include_ontology: bool,
    include_task_ontology: bool,
) -> None:
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"unsupported profile: {profile}; expected one of {SUPPORTED_PROFILES}"
        )
    if profile in {
        PROFILE_VERCEL_ONTOLOGY_LIGHT,
        PROFILE_VERCEL_ONTOLOGY_COMPLETE,
        PROFILE_VERCEL_ONTOLOGY_COMPACT,
    } and any(
        (include_training_links, include_ontology, include_task_ontology)
    ):
        raise ValueError(
            f"--profile {profile} already selects its complete table set; "
            "do not combine it with --include-training-links, --include-ontology, "
            "or --include-task-ontology"
        )


def _export_vercel_ontology_light(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> tuple[list[str], list[str]]:
    for table in CORE_TABLES:
        if not _table_exists(src, table):
            raise RuntimeError(f"source table is missing: {table}")
        _copy_source_table(dst, table)

    required_tables = (
        "ontology_concepts",
        "ontology_concept_aliases",
        "ksa_concept_links",
        "ncs_training_course_concept_links",
        "training_goal_concept_links",
        *VERCEL_ONTOLOGY_LIGHT_DIRECT_TABLES,
    )
    for table in required_tables:
        if not _table_exists(src, table):
            raise RuntimeError(f"source table is missing: {table}")

    _copy_projected_source_table(
        src,
        dst,
        "ontology_concepts",
        null_columns={"definition", "definition_source", "created_at", "updated_at"},
    )
    _copy_lightweight_aliases(src, dst)
    _copy_projected_source_table(
        src,
        dst,
        "ksa_concept_links",
        null_columns={"created_at"},
    )

    for table in VERCEL_ONTOLOGY_LIGHT_DIRECT_TABLES:
        _copy_source_table(dst, table)
    _require_columns(
        src,
        "ncs_training_course_concept_links",
        {"link_method"},
    )
    _copy_source_table(
        dst,
        "ncs_training_course_concept_links",
        where="link_method <> 'unit_ksa_concept_inherited'",
    )
    _require_columns(src, "training_goal_concept_links", {"link_method"})
    _copy_source_table(
        dst,
        "training_goal_concept_links",
        where="link_method <> 'training_goal_element_implied_concept'",
    )

    empty_compatibility_tables: list[str] = []
    missing_compatibility_tables: list[str] = []
    for table in VERCEL_ONTOLOGY_LIGHT_EMPTY_COMPATIBILITY_TABLES:
        if _table_exists(src, table):
            _copy_source_table(dst, table, where="0")
            empty_compatibility_tables.append(table)
        else:
            missing_compatibility_tables.append(table)
    return empty_compatibility_tables, missing_compatibility_tables


def _export_vercel_ontology_complete(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> tuple[int, list[str], list[str]]:
    required_tables = (
        *VERCEL_ONTOLOGY_COMPLETE_DIRECT_TABLES,
        TASK_KSA_RELATION_SOURCE_TABLE,
    )
    for table in required_tables:
        if not _table_exists(src, table):
            raise RuntimeError(f"source table is missing: {table}")

    _copy_complete_concepts(src, dst)
    merged_reviewed_aliases = _copy_complete_aliases_and_merge_reviewed_labels(
        src,
        dst,
    )

    specially_copied = {
        "ontology_concepts",
        "ontology_concept_aliases",
    }
    for table in VERCEL_ONTOLOGY_COMPLETE_DIRECT_TABLES:
        if table in specially_copied:
            continue
        null_columns = set(VERCEL_ONTOLOGY_COMPLETE_NULL_COLUMNS)
        if table == "ontology_concept_label_candidates":
            null_columns.update(
                {"source_text", "evidence_text", "created_at", "updated_at"}
            )
        elif table in {
            "ncs_training_course_concept_links",
            "training_goal_concept_links",
        }:
            null_columns.add("evidence_text")
        elif table == "task_similarity_links":
            null_columns.add("evidence_json")
        _copy_compacted_source_table(
            src,
            dst,
            table,
            null_columns=null_columns,
        )

    empty_compatibility_tables: list[str] = []
    missing_compatibility_tables: list[str] = []
    for table in VERCEL_ONTOLOGY_COMPLETE_EMPTY_COMPATIBILITY_TABLES:
        if _table_exists(src, table):
            _copy_source_table(dst, table, where="0")
            empty_compatibility_tables.append(table)
        else:
            missing_compatibility_tables.append(table)

    _copy_compact_task_ksa_relations(src, dst)
    dst.execute(
        """
        CREATE TABLE serving_snapshot_manifest (
            manifest_key TEXT PRIMARY KEY,
            manifest_value TEXT NOT NULL
        )
        """
    )
    dst.executemany(
        "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
        (
            ("profile", PROFILE_VERCEL_ONTOLOGY_COMPLETE),
            ("schema", "ncs_vercel_ontology_complete_v1"),
            ("task_ksa_storage", TASK_KSA_RELATION_COMPACT_TABLE),
            ("task_ksa_compatibility_view", TASK_KSA_RELATION_SOURCE_TABLE),
            ("label_candidate_scope", "all_statuses"),
            ("source_access", "read_only_immutable"),
        ),
    )
    return (
        merged_reviewed_aliases,
        empty_compatibility_tables,
        missing_compatibility_tables,
    )


def _export_vercel_ontology_compact(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> tuple[int, list[str], list[str], dict[str, int], dict[str, object]]:
    required_tables = (
        *VERCEL_ONTOLOGY_COMPACT_DIRECT_TABLES,
        "ontology_concepts",
        "ontology_concept_aliases",
        "ontology_concept_label_candidates",
        "ontology_concept_relations",
        "criteria_concept_links",
        "element_criteria_ksa_links",
    )
    for table in required_tables:
        if not _table_exists(src, table):
            raise RuntimeError(f"source table is missing: {table}")

    for table in VERCEL_ONTOLOGY_COMPACT_DIRECT_TABLES:
        _copy_compacted_source_table(
            src,
            dst,
            table,
            null_columns=set(VERCEL_ONTOLOGY_COMPACT_NULL_COLUMNS),
        )

    _copy_complete_concepts(src, dst)
    atomic_metrics = create_atomic_storage(src, dst)
    training_metrics = create_training_storage(src, dst)
    job_base_metrics = create_job_base_storage(src, dst)
    _copy_lightweight_aliases(src, dst)
    merged_reviewed_aliases = _merge_reviewed_labels_into_aliases(src, dst)
    reviewed_label_count = _copy_human_reviewed_label_candidates(src, dst)

    empty_compatibility_tables: list[str] = []
    missing_compatibility_tables: list[str] = []
    if _table_exists(src, "learning_module_concept_links"):
        _copy_source_table(dst, "learning_module_concept_links", where="0")
        empty_compatibility_tables.append("learning_module_concept_links")
    else:
        missing_compatibility_tables.append("learning_module_concept_links")

    relation_posting_counts, relation_edge_count = (
        _create_ontology_relation_postings(dst)
    )
    criteria_posting_counts, criteria_edge_count = (
        _create_criteria_concept_postings(dst)
    )
    logical_counts = {
        "ontology_concept_relations": relation_edge_count,
        "criteria_concept_links_enriched": criteria_edge_count,
    }
    servable_counts: dict[str, int] = {}
    for metrics in (atomic_metrics, training_metrics, job_base_metrics):
        servable_counts.update(
            {
                str(name): int(count)
                for name, count in dict(metrics["servable_counts"]).items()
            }
        )

    source_ksa_hash = _stable_ksa_sha256(src)
    destination_ksa_hash = _stable_ksa_sha256(dst)
    if source_ksa_hash != destination_ksa_hash:
        raise RuntimeError(
            "raw KSA parity failed: source and compact snapshot hashes differ"
        )

    dst.execute(
        """
        CREATE TABLE serving_snapshot_manifest (
            manifest_key TEXT PRIMARY KEY,
            manifest_value TEXT NOT NULL
        ) WITHOUT ROWID
        """
    )
    dst.executemany(
        "INSERT INTO serving_snapshot_manifest VALUES (?, ?)",
        (
            ("profile", PROFILE_VERCEL_ONTOLOGY_COMPACT),
            ("schema", VERCEL_COMPACT_SCHEMA),
            ("posting_codec", COMPACT_POSTING_CODEC),
            ("source_access", "read_only_immutable"),
            ("criteria_storage", "forward_inverse_postings"),
            ("ontology_relation_storage", "outgoing_incoming_postings"),
            ("atomic_storage", ATOMIC_COMPACT_TABLE),
            ("atomic_compatibility_views", "ksa_atomic_items,ksa_atomic_concept_links"),
            (
                "training_storage",
                ",".join(
                    (
                        TRAINING_CONCEPT_COMPACT_TABLE,
                        TRAINING_ELEMENT_COMPACT_TABLE,
                        TRAINING_GOAL_COMPACT_TABLE,
                        TRAINING_DELIVERY_COMPACT_TABLE,
                    )
                ),
            ),
            (
                "training_compatibility_views",
                "ncs_training_course_concept_links,"
                "ncs_training_course_element_links,"
                "training_goal_concept_links,training_delivery_relations",
            ),
            ("job_base_storage", JOB_BASE_COMPACT_TABLE),
            ("job_base_compatibility_view", "ncs_unit_job_base_links"),
            (
                "job_base_omitted_internal_columns",
                "source_payload,api_fetched_at,created_at,updated_at",
            ),
            ("task_ksa_storage", "derived_not_materialized"),
            ("task_similarity_storage", "derived_not_materialized"),
            ("label_candidate_scope", "human_reviewed_only"),
            ("raw_ksa_sha256", source_ksa_hash),
        ),
    )

    if _copy_query_aliases(src, dst):
        empty_compatibility_tables.append("ncs_query_aliases")

    payload_tables = sorted(
        str(row[0])
        for row in dst.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
              AND name NOT IN (
                  'serving_snapshot_manifest',
                  'serving_snapshot_table_counts'
              )
            ORDER BY name
            """
        ).fetchall()
    )
    physical_counts = {
        table: int(
            dst.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
        )
        for table in payload_tables
    }
    missing_compact_physical = sorted(
        set(compact_physical_tables()) - set(physical_counts)
    )
    if missing_compact_physical:
        raise RuntimeError(
            "compact physical count manifest is missing implementation tables: "
            f"{missing_compact_physical}"
        )
    for table, expected in {
        **relation_posting_counts,
        **criteria_posting_counts,
    }.items():
        if physical_counts.get(table) != expected:
            raise RuntimeError(
                f"compact posting row-count mismatch for {table}: "
                f"expected={expected}, actual={physical_counts.get(table)}"
            )

    dst.execute(
        """
        CREATE TABLE serving_snapshot_table_counts (
            object_name TEXT NOT NULL,
            row_count INTEGER NOT NULL,
            count_kind TEXT NOT NULL
                CHECK(count_kind IN ('physical', 'logical', 'servable')),
            PRIMARY KEY(object_name, count_kind)
        ) WITHOUT ROWID
        """
    )
    dst.executemany(
        "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'physical')",
        sorted(physical_counts.items()),
    )
    dst.executemany(
        "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'logical')",
        sorted(logical_counts.items()),
    )
    public_canonical_objects = tuple(
        dict.fromkeys(
            (
                *VERCEL_ONTOLOGY_COMPACT_DIRECT_TABLES,
                "ontology_concepts",
                "ontology_concept_aliases",
                "ontology_concept_label_candidates",
                *compact_canonical_objects(),
                "learning_module_concept_links",
                "ncs_query_aliases",
            )
        )
    )
    for object_name in public_canonical_objects:
        object_row = dst.execute(
            """
            SELECT type
            FROM sqlite_master
            WHERE name = ? AND type IN ('table', 'view')
            """,
            (object_name,),
        ).fetchone()
        if object_row is None:
            continue
        servable_counts[object_name] = int(
            dst.execute(
                f"SELECT COUNT(*) FROM {_quote(object_name)}"
            ).fetchone()[0]
        )
    dst.executemany(
        "INSERT INTO serving_snapshot_table_counts VALUES (?, ?, 'servable')",
        sorted(servable_counts.items()),
    )
    return (
        merged_reviewed_aliases,
        empty_compatibility_tables,
        missing_compatibility_tables,
        logical_counts,
        {
            "physical_counts": physical_counts,
            "servable_counts": servable_counts,
            "raw_ksa_sha256": source_ksa_hash,
            "human_reviewed_label_count": reviewed_label_count,
            "atomic_compaction": atomic_metrics,
            "training_compaction": training_metrics,
            "job_base_compaction": job_base_metrics,
        },
    )


def _base_indexes(*, compact: bool = False) -> tuple[str, ...]:
    indexes = (
        "CREATE UNIQUE INDEX idx_serving_classifications_code ON classifications(major_code, middle_code, small_code, sub_code)",
        "CREATE INDEX idx_serving_units_classification ON competency_units(classification_id)",
        "CREATE INDEX idx_serving_units_name ON competency_units(unit_name_raw)",
        "CREATE INDEX idx_serving_units_code ON competency_units(unit_code)",
        "CREATE INDEX idx_serving_elements_unit ON competency_elements(unit_code)",
        "CREATE INDEX idx_serving_criteria_element ON performance_criteria(element_id)",
        "CREATE INDEX idx_serving_ksa_element ON ksa_items(element_id)",
        "CREATE INDEX idx_serving_ksa_type ON ksa_items(ksa_type_name)",
        "CREATE INDEX idx_serving_alias_unit ON ncs_query_aliases(unit_code)",
        "CREATE INDEX idx_serving_alias_text ON ncs_query_aliases(alias_text)",
    )
    if not compact:
        return indexes
    omitted_names = {
        "idx_serving_ksa_type",
        "idx_serving_units_name",
        "idx_serving_alias_unit",
    }
    return tuple(
        statement
        for statement in indexes
        if statement.split()[2] not in omitted_names
    )


def _vercel_ontology_light_indexes() -> tuple[str, ...]:
    return (
        "CREATE UNIQUE INDEX idx_serving_elements_id ON competency_elements(element_id)",
        "CREATE UNIQUE INDEX idx_serving_criteria_id ON performance_criteria(criteria_id)",
        "CREATE UNIQUE INDEX idx_serving_ksa_id ON ksa_items(ksa_id)",
        "CREATE UNIQUE INDEX idx_serving_training_course_id ON ncs_training_courses(training_course_id)",
        "CREATE UNIQUE INDEX idx_serving_ont_concepts_id ON ontology_concepts(concept_id)",
        "CREATE INDEX idx_serving_ont_concepts_key ON ontology_concepts(normalized_key)",
        "CREATE INDEX idx_serving_ont_concepts_type ON ontology_concepts(concept_type)",
        "CREATE INDEX idx_serving_ont_alias_concept ON ontology_concept_aliases(concept_id)",
        "CREATE INDEX idx_serving_ont_alias_key ON ontology_concept_aliases(normalized_alias_key)",
        "CREATE INDEX idx_serving_ksa_concept_ksa ON ksa_concept_links(ksa_id)",
        "CREATE INDEX idx_serving_ksa_concept_concept ON ksa_concept_links(concept_id)",
        "CREATE INDEX idx_serving_course_unit_course ON ncs_training_course_unit_links(training_course_id)",
        "CREATE INDEX idx_serving_course_unit_unit ON ncs_training_course_unit_links(unit_code)",
        "CREATE INDEX idx_serving_course_concept_course ON ncs_training_course_concept_links(training_course_id)",
        "CREATE INDEX idx_serving_course_concept_unit ON ncs_training_course_concept_links(unit_code)",
        "CREATE INDEX idx_serving_course_concept_linked ON ncs_training_course_concept_links(concept_id)",
        "CREATE INDEX idx_serving_course_element_course ON ncs_training_course_element_links(training_course_id)",
        "CREATE INDEX idx_serving_course_element_unit ON ncs_training_course_element_links(unit_code)",
        "CREATE INDEX idx_serving_course_element_element ON ncs_training_course_element_links(element_id)",
        "CREATE INDEX idx_serving_course_goal_course ON training_goal_concept_links(training_course_id)",
        "CREATE INDEX idx_serving_course_goal_concept ON training_goal_concept_links(concept_id)",
        "CREATE INDEX idx_serving_delivery_course ON training_delivery_relations(training_course_id)",
        "CREATE INDEX idx_serving_career_scope ON ncs_career_paths(major_code, middle_code, small_code, sub_code)",
        "CREATE INDEX idx_serving_career_unit ON ncs_career_paths(matched_unit_code)",
        "CREATE INDEX idx_serving_qualification_name ON ncs_qualification_items(jm_nm)",
        "CREATE INDEX idx_serving_unit_qualification_unit ON ncs_unit_qualification_links(unit_code)",
        "CREATE INDEX idx_serving_unit_qualification_jm ON ncs_unit_qualification_links(jm_cd)",
        "CREATE INDEX idx_serving_unit_standard_matched ON ncs_unit_standard_training(matched_unit_code)",
    )


def _vercel_ontology_complete_indexes() -> tuple[str, ...]:
    return (
        *_vercel_ontology_light_indexes(),
        "CREATE INDEX idx_serving_ont_rel_source ON ontology_concept_relations(source_concept_id)",
        "CREATE INDEX idx_serving_ont_rel_target ON ontology_concept_relations(target_concept_id)",
        "CREATE INDEX idx_serving_label_candidate_concept ON ontology_concept_label_candidates(concept_id)",
        "CREATE INDEX idx_serving_label_candidate_status ON ontology_concept_label_candidates(review_status)",
        "CREATE INDEX idx_serving_atomic_ksa ON ksa_atomic_items(ksa_id)",
        "CREATE UNIQUE INDEX idx_serving_atomic_id ON ksa_atomic_items(atomic_id)",
        "CREATE INDEX idx_serving_atomic_concept_atomic ON ksa_atomic_concept_links(atomic_id)",
        "CREATE INDEX idx_serving_atomic_concept_concept ON ksa_atomic_concept_links(concept_id)",
        "CREATE INDEX idx_serving_criteria_concept_criteria ON criteria_concept_links(criteria_id)",
        "CREATE INDEX idx_serving_criteria_concept_concept ON criteria_concept_links(concept_id)",
        f"CREATE INDEX idx_serving_task_ksa_criteria ON {TASK_KSA_RELATION_COMPACT_TABLE}(criteria_id)",
        "CREATE INDEX idx_serving_task_sim_source ON task_similarity_links(source_criteria_id)",
        "CREATE INDEX idx_serving_task_sim_target ON task_similarity_links(target_criteria_id)",
        "CREATE INDEX idx_serving_task_sim_score ON task_similarity_links(similarity_score)",
        "CREATE INDEX idx_serving_element_criteria_ksa_element ON element_criteria_ksa_links(element_id)",
        "CREATE INDEX idx_serving_element_criteria_ksa_criteria ON element_criteria_ksa_links(criteria_id)",
        "CREATE INDEX idx_serving_element_criteria_ksa_ksa ON element_criteria_ksa_links(ksa_id)",
        "CREATE INDEX idx_serving_quality_target ON quality_issues(target_type, target_id)",
        "CREATE INDEX idx_serving_job_base_name ON ncs_job_base_competencies(normalized_key)",
        "CREATE INDEX idx_serving_job_base_factor_name ON ncs_job_base_factors(normalized_key)",
        "CREATE INDEX idx_serving_unit_job_base_unit ON ncs_unit_job_base_links(unit_code)",
        "CREATE INDEX idx_serving_gold_status ON training_transition_gold_scenarios(review_status)",
        "CREATE INDEX idx_serving_gold_reviews_scenario ON training_transition_scenario_reviews(scenario_id)",
    )


def _vercel_ontology_compact_indexes() -> tuple[str, ...]:
    return (
        # Directly materialized public tables (views are intentionally absent).
        "CREATE UNIQUE INDEX idx_serving_elements_id ON competency_elements(element_id)",
        "CREATE UNIQUE INDEX idx_serving_criteria_id ON performance_criteria(criteria_id)",
        "CREATE UNIQUE INDEX idx_serving_ksa_id ON ksa_items(ksa_id)",
        "CREATE UNIQUE INDEX idx_serving_training_course_id ON ncs_training_courses(training_course_id)",
        "CREATE UNIQUE INDEX idx_serving_ont_concepts_id ON ontology_concepts(concept_id)",
        "CREATE INDEX idx_serving_ont_concepts_key ON ontology_concepts(normalized_key)",
        "CREATE INDEX idx_serving_ont_concepts_type ON ontology_concepts(concept_type)",
        "CREATE INDEX idx_serving_ont_alias_concept ON ontology_concept_aliases(concept_id)",
        "CREATE INDEX idx_serving_ont_alias_key ON ontology_concept_aliases(normalized_alias_key)",
        "CREATE INDEX idx_serving_ksa_concept_ksa ON ksa_concept_links(ksa_id)",
        "CREATE INDEX idx_serving_ksa_concept_concept ON ksa_concept_links(concept_id)",
        "CREATE INDEX idx_serving_course_unit_course ON ncs_training_course_unit_links(training_course_id)",
        "CREATE INDEX idx_serving_course_unit_unit ON ncs_training_course_unit_links(unit_code)",
        "CREATE INDEX idx_serving_career_scope ON ncs_career_paths(major_code, middle_code, small_code, sub_code)",
        "CREATE INDEX idx_serving_career_unit ON ncs_career_paths(matched_unit_code)",
        "CREATE INDEX idx_serving_qualification_name ON ncs_qualification_items(jm_nm)",
        "CREATE INDEX idx_serving_unit_qualification_unit ON ncs_unit_qualification_links(unit_code)",
        "CREATE INDEX idx_serving_unit_qualification_jm ON ncs_unit_qualification_links(jm_cd)",
        "CREATE INDEX idx_serving_unit_standard_matched ON ncs_unit_standard_training(matched_unit_code)",
        "CREATE INDEX idx_serving_label_candidate_concept ON ontology_concept_label_candidates(concept_id)",
        # Compact physical relation tables. INTEGER PRIMARY KEY columns already
        # provide rowid indexes, so do not add redundant unique indexes.
        f"CREATE INDEX idx_serving_atomic_ksa ON {ATOMIC_COMPACT_TABLE}(ksa_id)",
        f"CREATE INDEX idx_serving_atomic_concept_concept ON {ATOMIC_COMPACT_TABLE}(concept_id)",
        f"CREATE INDEX idx_serving_course_concept_course ON {TRAINING_CONCEPT_COMPACT_TABLE}(training_course_id)",
        f"CREATE INDEX idx_serving_course_concept_unit ON {TRAINING_CONCEPT_COMPACT_TABLE}(unit_code_code)",
        f"CREATE INDEX idx_serving_course_concept_linked ON {TRAINING_CONCEPT_COMPACT_TABLE}(concept_id)",
        f"CREATE INDEX idx_serving_course_element_course ON {TRAINING_ELEMENT_COMPACT_TABLE}(training_course_id)",
        f"CREATE INDEX idx_serving_course_element_unit ON {TRAINING_ELEMENT_COMPACT_TABLE}(unit_code_code)",
        f"CREATE INDEX idx_serving_course_element_element ON {TRAINING_ELEMENT_COMPACT_TABLE}(element_id)",
        f"CREATE INDEX idx_serving_course_goal_course ON {TRAINING_GOAL_COMPACT_TABLE}(training_course_id)",
        f"CREATE INDEX idx_serving_course_goal_concept ON {TRAINING_GOAL_COMPACT_TABLE}(concept_id)",
        f"CREATE INDEX idx_serving_delivery_course ON {TRAINING_DELIVERY_COMPACT_TABLE}(training_course_id)",
        "CREATE INDEX idx_serving_job_base_name ON ncs_job_base_competencies(normalized_key)",
        "CREATE INDEX idx_serving_job_base_factor_name ON ncs_job_base_factors(normalized_key)",
        f"CREATE INDEX idx_serving_unit_job_base_unit ON {JOB_BASE_COMPACT_TABLE}(unit_code_code)",
        f"CREATE INDEX idx_serving_unit_job_base_competency ON {JOB_BASE_COMPACT_TABLE}(job_base_competency_id)",
        f"CREATE INDEX idx_serving_unit_job_base_factor ON {JOB_BASE_COMPACT_TABLE}(job_base_factor_id)",
        "CREATE INDEX idx_serving_gold_status ON training_transition_gold_scenarios(review_status)",
        "CREATE INDEX idx_serving_gold_reviews_scenario ON training_transition_scenario_reviews(scenario_id)",
    )


def export_serving_db(
    source: Path,
    destination: Path,
    *,
    profile: str = PROFILE_DEFAULT,
    include_training_links: bool = False,
    include_ontology: bool = False,
    include_task_ontology: bool = False,
    include_indexes: bool = True,
) -> dict[str, object]:
    _validate_profile_selection(
        profile=profile,
        include_training_links=include_training_links,
        include_ontology=include_ontology,
        include_task_ontology=include_task_ontology,
    )
    if source.resolve() == destination.resolve():
        raise ValueError("destination must be different from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    source_uri = (
        f"file:{quote(source.resolve().as_posix(), safe='/:')}"
        "?mode=ro&immutable=1"
    )
    with closing(sqlite3.connect(source_uri, uri=True)) as src, closing(
        sqlite3.connect(destination, uri=True)
    ) as dst:
        src.execute("PRAGMA query_only = ON")
        # Use the same immutable URI for the connection doing the bulk copy.
        # Attaching the plain path would silently reopen the canonical source in
        # read/write mode even though ``src`` itself is query-only.
        dst.execute("ATTACH DATABASE ? AS source", (source_uri,))
        dst.execute("PRAGMA journal_mode = OFF")
        dst.execute("PRAGMA synchronous = OFF")
        dst.execute("PRAGMA foreign_keys = OFF")
        dst.execute("PRAGMA temp_store = FILE")

        empty_compatibility_tables: list[str] = []
        missing_compatibility_tables: list[str] = []
        merged_reviewed_aliases = 0
        compact_logical_counts: dict[str, int] = {}
        compact_profile_metrics: dict[str, object] = {}
        if profile == PROFILE_VERCEL_ONTOLOGY_LIGHT:
            (
                empty_compatibility_tables,
                missing_compatibility_tables,
            ) = _export_vercel_ontology_light(src, dst)
        elif profile == PROFILE_VERCEL_ONTOLOGY_COMPLETE:
            (
                merged_reviewed_aliases,
                empty_compatibility_tables,
                missing_compatibility_tables,
            ) = _export_vercel_ontology_complete(src, dst)
        elif profile == PROFILE_VERCEL_ONTOLOGY_COMPACT:
            (
                merged_reviewed_aliases,
                empty_compatibility_tables,
                missing_compatibility_tables,
                compact_logical_counts,
                compact_profile_metrics,
            ) = _export_vercel_ontology_compact(src, dst)
        else:
            selected_tables = _resolve_tables(
                include_training_links=include_training_links,
                include_ontology=include_ontology,
                include_task_ontology=include_task_ontology,
            )
            for table in selected_tables:
                if not _table_exists(src, table):
                    raise RuntimeError(f"source table is missing: {table}")
                _copy_source_table(dst, table)

        # ncs_mcp.search_ncs uses aliases as a query-expansion table. Keep the
        # table when present; an empty compatible table is enough otherwise.
        if (
            profile != PROFILE_VERCEL_ONTOLOGY_COMPACT
            and _copy_query_aliases(src, dst)
        ):
            empty_compatibility_tables.append("ncs_query_aliases")

        if include_indexes:
            _execute_indexes(
                dst,
                _base_indexes(
                    compact=profile == PROFILE_VERCEL_ONTOLOGY_COMPACT,
                ),
                strict=profile
                in {
                    PROFILE_VERCEL_ONTOLOGY_LIGHT,
                    PROFILE_VERCEL_ONTOLOGY_COMPLETE,
                    PROFILE_VERCEL_ONTOLOGY_COMPACT,
                },
            )

            if profile == PROFILE_VERCEL_ONTOLOGY_LIGHT:
                _execute_indexes(
                    dst,
                    _vercel_ontology_light_indexes(),
                    strict=True,
                )
            elif profile == PROFILE_VERCEL_ONTOLOGY_COMPLETE:
                _execute_indexes(
                    dst,
                    _vercel_ontology_complete_indexes(),
                    strict=True,
                )
            elif profile == PROFILE_VERCEL_ONTOLOGY_COMPACT:
                _execute_indexes(
                    dst,
                    _vercel_ontology_compact_indexes(),
                    strict=True,
                )
            elif include_training_links:
                training_indexes = (
                    "CREATE INDEX idx_serving_course_unit_course ON ncs_training_course_unit_links(training_course_id)",
                    "CREATE INDEX idx_serving_course_unit_unit ON ncs_training_course_unit_links(unit_code)",
                    "CREATE INDEX idx_serving_course_concept_course ON ncs_training_course_concept_links(training_course_id)",
                    "CREATE INDEX idx_serving_course_concept_unit ON ncs_training_course_concept_links(unit_code)",
                    "CREATE INDEX idx_serving_course_concept_linked ON ncs_training_course_concept_links(concept_id)",
                    "CREATE INDEX idx_serving_course_element_course ON ncs_training_course_element_links(training_course_id)",
                    "CREATE INDEX idx_serving_course_element_unit ON ncs_training_course_element_links(unit_code)",
                    "CREATE INDEX idx_serving_course_goal_course ON training_goal_concept_links(training_course_id)",
                    "CREATE INDEX idx_serving_course_goal_concept ON training_goal_concept_links(concept_id)",
                    "CREATE INDEX idx_serving_delivery_course ON training_delivery_relations(training_course_id)",
                )
                _execute_indexes(dst, training_indexes)

            if profile == PROFILE_DEFAULT and include_ontology:
                ontology_indexes = (
                    "CREATE INDEX idx_serving_ont_concepts_type ON ontology_concepts(concept_type)",
                    "CREATE INDEX idx_serving_ont_concepts_key ON ontology_concepts(normalized_key)",
                    "CREATE INDEX idx_serving_ont_alias_concept ON ontology_concept_aliases(concept_id)",
                    "CREATE INDEX idx_serving_ont_alias_key ON ontology_concept_aliases(normalized_alias_key)",
                    "CREATE INDEX idx_serving_rel_source ON ontology_concept_relations(source_concept_id)",
                    "CREATE INDEX idx_serving_rel_target ON ontology_concept_relations(target_concept_id)",
                    "CREATE INDEX idx_serving_ksa_concept_ksa ON ksa_concept_links(ksa_id)",
                    "CREATE INDEX idx_serving_ksa_concept_concept ON ksa_concept_links(concept_id)",
                    "CREATE INDEX idx_serving_atomic_ksa ON ksa_atomic_items(ksa_id)",
                    "CREATE INDEX idx_serving_atomic_concept ON ksa_atomic_concept_links(concept_id)",
                    "CREATE INDEX idx_serving_criteria_concept ON criteria_concept_links(criteria_id)",
                )
                _execute_indexes(dst, ontology_indexes)

            if profile == PROFILE_DEFAULT and include_task_ontology:
                task_indexes = (
                    "CREATE INDEX idx_serving_task_ksa_source ON task_ksa_concept_relations(source_criteria_id)",
                    "CREATE INDEX idx_serving_task_ksa_target ON task_ksa_concept_relations(target_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_source ON task_similarity_links(source_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_target ON task_similarity_links(target_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_score ON task_similarity_links(similarity_score)",
                )
                _execute_indexes(dst, task_indexes)

        dst.commit()
        dst.execute("DETACH DATABASE source")

        if profile == PROFILE_DEFAULT:
            # Preserve the legacy report ordering for existing consumers.
            destination_tables = [*selected_tables, "ncs_query_aliases"]
        else:
            destination_tables = sorted(
                str(row[0])
                for row in dst.execute(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type = 'table'
                      AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            )
        counts = {
            table: int(
                dst.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            )
            for table in destination_tables
        }
        represented_source_tables = set(destination_tables)
        replaced_tables: dict[str, str] = {}
        if profile == PROFILE_VERCEL_ONTOLOGY_COMPLETE:
            represented_source_tables.add(TASK_KSA_RELATION_SOURCE_TABLE)
            replaced_tables[TASK_KSA_RELATION_SOURCE_TABLE] = (
                f"{TASK_KSA_RELATION_COMPACT_TABLE} + compatibility view"
            )
        elif profile == PROFILE_VERCEL_ONTOLOGY_COMPACT:
            represented_source_tables.update(
                {
                    "ontology_concept_relations",
                    "criteria_concept_links",
                    *VERCEL_ONTOLOGY_COMPACT_REPLACED_TABLES,
                }
            )
            replaced_tables.update(
                {
                    "ontology_concept_relations": (
                        f"{ONTOLOGY_RELATION_OUTGOING_TABLE} + "
                        f"{ONTOLOGY_RELATION_INCOMING_TABLE}"
                    ),
                    "criteria_concept_links": (
                        f"{CRITERIA_CONCEPT_FORWARD_TABLE} + "
                        f"{CRITERIA_CONCEPT_INVERSE_TABLE}"
                    ),
                    "ksa_atomic_items": (
                        f"{ATOMIC_COMPACT_TABLE} + compatibility view"
                    ),
                    "ksa_atomic_concept_links": (
                        f"{ATOMIC_COMPACT_TABLE} + compatibility view"
                    ),
                    "ncs_training_course_concept_links": (
                        f"{TRAINING_CONCEPT_COMPACT_TABLE} + compatibility view"
                    ),
                    "ncs_training_course_element_links": (
                        f"{TRAINING_ELEMENT_COMPACT_TABLE} + compatibility view"
                    ),
                    "training_goal_concept_links": (
                        f"{TRAINING_GOAL_COMPACT_TABLE} + compatibility view"
                    ),
                    "training_delivery_relations": (
                        f"{TRAINING_DELIVERY_COMPACT_TABLE} + compatibility view"
                    ),
                    "ncs_unit_job_base_links": (
                        f"{JOB_BASE_COMPACT_TABLE} + compatibility view"
                    ),
                }
            )
        omitted_tables = sorted(_source_table_names(src) - represented_source_tables)

        if profile == PROFILE_VERCEL_ONTOLOGY_COMPACT:
            dst.execute("VACUUM")
            compact_size = destination.stat().st_size
            if compact_size >= VERCEL_COMPACT_MAX_BYTES:
                raise RuntimeError(
                    "compact Vercel snapshot exceeds the hard size gate: "
                    f"{compact_size} >= {VERCEL_COMPACT_MAX_BYTES} bytes"
                )

    return {
        "source": str(source),
        "destination": str(destination),
        "profile": profile,
        "tables": counts,
        "omitted_or_empty_tables": {
            "omitted": omitted_tables,
            "empty_compatibility": sorted(set(empty_compatibility_tables)),
            "source_missing_compatibility": sorted(missing_compatibility_tables),
            "replaced": replaced_tables,
        },
        "profile_metrics": {
            "human_reviewed_label_aliases_merged": merged_reviewed_aliases,
            **compact_profile_metrics,
            "logical_counts": compact_logical_counts,
        },
        "size_bytes": destination.stat().st_size,
        "purpose": (
            "read-only Vercel ontology-light MCP serving DB"
            if profile == PROFILE_VERCEL_ONTOLOGY_LIGHT
            else (
                "read-only Vercel serving-complete ontology MCP DB"
                if profile == PROFILE_VERCEL_ONTOLOGY_COMPLETE
                else (
                    "read-only Vercel compact ontology MCP DB"
                    if profile == PROFILE_VERCEL_ONTOLOGY_COMPACT
                    else "read-only interview MCP serving DB"
                )
            )
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--destination", type=Path, default=Path("data/processed/ncs_interview.db"))
    parser.add_argument(
        "--profile",
        choices=SUPPORTED_PROFILES,
        default=PROFILE_DEFAULT,
        help=(
            "Select a serving profile. vercel-ontology-light keeps a narrow graph; "
            "vercel-ontology-complete preserves the full reviewed NCS/KSA, ontology, "
            "task-similarity, training, support, and gold-set evidence with compact "
            "task/KSA relation storage. vercel-ontology-compact stores lossless "
            "ontology/criterion postings below the Vercel 500 MiB boundary. "
            "Explicit profiles cannot be combined with "
            "--include-* flags."
        ),
    )
    parser.add_argument(
        "--include-training-links",
        action="store_true",
        help="Include training-unit/course linkage tables required by recommendation flows.",
    )
    parser.add_argument(
        "--include-ontology",
        action="store_true",
        help="Include ontology core tables (concepts/aliases/links).",
    )
    parser.add_argument(
        "--include-task-ontology",
        action="store_true",
        help="Include task-level ontology relations used by task transition recommendations.",
    )
    parser.add_argument(
        "--without-indexes",
        action="store_true",
        help="Skip index creation in the exported database.",
    )
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    try:
        _validate_profile_selection(
            profile=args.profile,
            include_training_links=args.include_training_links,
            include_ontology=args.include_ontology,
            include_task_ontology=args.include_task_ontology,
        )
    except ValueError as exc:
        parser.error(str(exc))
    report = export_serving_db(
        args.source,
        args.destination,
        profile=args.profile,
        include_training_links=args.include_training_links,
        include_ontology=args.include_ontology,
        include_task_ontology=args.include_task_ontology,
        include_indexes=not args.without_indexes,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
