"""Physical compaction for the hosted NCS serving snapshot.

The canonical source database remains untouched.  These helpers materialize
numeric serving tables and recreate the public table shapes as SQLite views.
Atomic and training canonical columns, including evidence and timestamps, are
reconstructed losslessly from compact facts and shared dictionaries.  The
job-base view intentionally projects source payload and ingestion timestamps
as NULL because those internal collection fields are outside serving policy.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable


ATOMIC_COMPACT_TABLE = "ksa_atomic_facts_compact"
ATOMIC_SPLIT_METHOD_TABLE = "ksa_atomic_split_methods"
ATOMIC_TIMESTAMP_TABLE = "ksa_atomic_timestamps"

UNIT_CODE_TABLE = "serving_unit_codes"
LINK_METHOD_TABLE = "serving_link_methods"
REVIEW_STATUS_TABLE = "serving_review_statuses"
DELIVERY_TYPE_TABLE = "training_delivery_relation_types"
DELIVERY_VALUE_TABLE = "training_delivery_values"
TRAINING_EVIDENCE_TABLE = "training_link_evidence"
TRAINING_TIMESTAMP_TABLE = "training_link_timestamps"

TRAINING_CONCEPT_COMPACT_TABLE = "ncs_training_course_concept_links_compact"
TRAINING_ELEMENT_COMPACT_TABLE = "ncs_training_course_element_links_compact"
TRAINING_GOAL_COMPACT_TABLE = "training_goal_concept_links_compact"
TRAINING_DELIVERY_COMPACT_TABLE = "training_delivery_relations_compact"

JOB_BASE_COMPACT_TABLE = "ncs_unit_job_base_links_compact"

ATOMIC_CANONICAL_OBJECTS = (
    "ksa_atomic_items",
    "ksa_atomic_concept_links",
)
TRAINING_CANONICAL_OBJECTS = (
    "ncs_training_course_concept_links",
    "ncs_training_course_element_links",
    "training_goal_concept_links",
    "training_delivery_relations",
)
JOB_BASE_CANONICAL_OBJECTS = ("ncs_unit_job_base_links",)

ATOMIC_PHYSICAL_TABLES = (
    ATOMIC_COMPACT_TABLE,
    ATOMIC_SPLIT_METHOD_TABLE,
    ATOMIC_TIMESTAMP_TABLE,
)
TRAINING_PHYSICAL_TABLES = (
    UNIT_CODE_TABLE,
    LINK_METHOD_TABLE,
    REVIEW_STATUS_TABLE,
    DELIVERY_TYPE_TABLE,
    DELIVERY_VALUE_TABLE,
    TRAINING_EVIDENCE_TABLE,
    TRAINING_TIMESTAMP_TABLE,
    TRAINING_CONCEPT_COMPACT_TABLE,
    TRAINING_ELEMENT_COMPACT_TABLE,
    TRAINING_GOAL_COMPACT_TABLE,
    TRAINING_DELIVERY_COMPACT_TABLE,
)
JOB_BASE_PHYSICAL_TABLES = (
    JOB_BASE_COMPACT_TABLE,
)

ATOMIC_ITEM_COLUMNS = (
    "atomic_id",
    "ksa_id",
    "element_id",
    "ksa_type_name",
    "atom_index",
    "atom_text",
    "normalized_key",
    "split_method",
    "review_status",
    "created_at",
)
ATOMIC_LINK_COLUMNS = (
    "link_id",
    "atomic_id",
    "concept_id",
    "link_status",
    "created_at",
)
TRAINING_CONCEPT_COLUMNS = (
    "link_id",
    "training_course_id",
    "unit_code",
    "concept_id",
    "link_method",
    "confidence_score",
    "evidence_text",
    "review_status",
    "created_at",
    "updated_at",
)
TRAINING_ELEMENT_COLUMNS = (
    "link_id",
    "training_course_id",
    "unit_code",
    "element_id",
    "link_method",
    "confidence_score",
    "evidence_text",
    "review_status",
    "created_at",
    "updated_at",
)
TRAINING_GOAL_COLUMNS = (
    "link_id",
    "training_course_id",
    "unit_code",
    "element_id",
    "concept_id",
    "link_method",
    "confidence_score",
    "evidence_text",
    "review_status",
    "created_at",
    "updated_at",
)
TRAINING_DELIVERY_COLUMNS = (
    "relation_id",
    "training_course_id",
    "relation_type",
    "relation_value",
    "normalized_value",
    "numeric_value",
    "evidence_text",
    "confidence_score",
    "review_status",
    "created_at",
    "updated_at",
)
JOB_BASE_COLUMNS = (
    "link_id",
    "unit_code",
    "job_base_competency_id",
    "job_base_factor_id",
    "ncs_lclas_cd",
    "ncs_lclas_cdnm",
    "ncs_mclas_cd",
    "ncs_mclas_cdnm",
    "ncs_sclas_cd",
    "ncs_sclas_cdnm",
    "ncs_subd_cd",
    "ncs_subd_cdnm",
    "compe_unit_name",
    "link_method",
    "confidence_score",
    "source_payload",
    "api_fetched_at",
    "review_status",
    "created_at",
    "updated_at",
)
JOB_BASE_INTERNAL_COLUMNS = (
    "source_payload",
    "api_fetched_at",
    "created_at",
    "updated_at",
)
JOB_BASE_SERVABLE_COLUMNS = tuple(
    column for column in JOB_BASE_COLUMNS if column not in JOB_BASE_INTERNAL_COLUMNS
)

def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _columns(conn: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({_quote(table)})").fetchall()
    )


def require_exact_columns(
    conn: sqlite3.Connection,
    table: str,
    expected: tuple[str, ...],
) -> None:
    actual = _columns(conn, table)
    if actual != expected:
        raise RuntimeError(
            f"compact serving schema mismatch for {table}: "
            f"expected={list(expected)}, actual={list(actual)}"
        )


def _distinct_values(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> set[object]:
    return {
        row[0]
        for row in conn.execute(
            f"SELECT DISTINCT {_quote(column)} FROM {_quote(table)}"
        ).fetchall()
    }


def _require_non_null_dictionary_values(
    src: sqlite3.Connection,
    specs: Iterable[tuple[str, str]],
) -> None:
    unsafe: list[str] = []
    for table, column in specs:
        if None in _distinct_values(src, table, column):
            unsafe.append(f"{table}.{column}")
    if unsafe:
        raise RuntimeError(
            "compact dictionaries refuse NULL method/status/type values: "
            + ", ".join(unsafe)
        )


def _row_count(conn: sqlite3.Connection, object_name: str) -> int:
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {_quote(object_name)}"
        ).fetchone()[0]
    )


def _require_count_parity(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
    objects: Iterable[str],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name in objects:
        source_count = _row_count(src, name)
        destination_count = _row_count(dst, name)
        if source_count != destination_count:
            raise RuntimeError(
                f"compact row-count parity failed for {name}: "
                f"source={source_count}, destination={destination_count}"
            )
        counts[name] = destination_count
    return counts


def _assert_no_mismatches(
    dst: sqlite3.Connection,
    *,
    table: str,
    id_column: str,
    preserved_columns: Iterable[str],
) -> None:
    comparisons = " OR ".join(
        f"NOT (source_row.{_quote(column)} IS compact_row.{_quote(column)})"
        for column in preserved_columns
    )
    mismatch_count = int(
        dst.execute(
            f"""
            SELECT COUNT(*)
            FROM source.{_quote(table)} AS source_row
            LEFT JOIN main.{_quote(table)} AS compact_row
              ON compact_row.{_quote(id_column)} = source_row.{_quote(id_column)}
            WHERE compact_row.{_quote(id_column)} IS NULL
               OR {comparisons}
            """
        ).fetchone()[0]
    )
    if mismatch_count:
        raise RuntimeError(
            f"compact value parity failed for {table}: mismatches={mismatch_count}"
        )


def create_atomic_storage(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> dict[str, object]:
    """Create one atomic fact table and the two canonical compatibility views."""

    require_exact_columns(src, "ksa_atomic_items", ATOMIC_ITEM_COLUMNS)
    require_exact_columns(src, "ksa_atomic_concept_links", ATOMIC_LINK_COLUMNS)
    item_statuses = _distinct_values(src, "ksa_atomic_items", "review_status")
    link_statuses = _distinct_values(src, "ksa_atomic_concept_links", "link_status")
    if item_statuses != {"raw"} or link_statuses != {"raw"}:
        raise RuntimeError(
            "compact atomic storage requires raw-only source states; "
            f"items={sorted(str(value) for value in item_statuses)}, "
            f"links={sorted(str(value) for value in link_statuses)}"
        )

    counts = src.execute(
        """
        SELECT
            (SELECT COUNT(*) FROM ksa_atomic_items),
            (SELECT COUNT(*) FROM ksa_atomic_concept_links),
            (SELECT COUNT(DISTINCT atomic_id) FROM ksa_atomic_concept_links),
            (SELECT COUNT(*) FROM (
                SELECT atomic_id
                FROM ksa_atomic_concept_links
                GROUP BY atomic_id
                HAVING COUNT(*) <> 1
            )),
            (SELECT COUNT(*)
             FROM ksa_atomic_concept_links AS link
             LEFT JOIN ksa_atomic_items AS atomic
               ON atomic.atomic_id = link.atomic_id
             WHERE atomic.atomic_id IS NULL),
            (SELECT COUNT(DISTINCT link_id) FROM ksa_atomic_concept_links)
        """
    ).fetchone()
    if not (
        int(counts[0]) == int(counts[1]) == int(counts[2])
        and int(counts[3]) == 0
        and int(counts[4]) == 0
        and int(counts[5]) == int(counts[1])
    ):
        raise RuntimeError(
            "compact atomic storage requires exactly one concept link per atomic row; "
            f"diagnostics={tuple(int(value) for value in counts)}"
        )

    mismatch = src.execute(
        """
        SELECT
            SUM(NOT (atomic.element_id IS item.element_id)),
            SUM(NOT (atomic.ksa_type_name IS item.ksa_type_name)),
            SUM(NOT (atomic.normalized_key IS concept.normalized_key)),
            SUM(item.ksa_id IS NULL),
            SUM(concept.concept_id IS NULL)
        FROM ksa_atomic_items AS atomic
        LEFT JOIN ksa_items AS item ON item.ksa_id = atomic.ksa_id
        LEFT JOIN ksa_atomic_concept_links AS link
          ON link.atomic_id = atomic.atomic_id
        LEFT JOIN ontology_concepts AS concept
          ON concept.concept_id = link.concept_id
        """
    ).fetchone()
    mismatch_counts = tuple(int(value or 0) for value in mismatch)
    if any(mismatch_counts):
        raise RuntimeError(
            "compact atomic derivation guard failed for element/type/normalized-key "
            f"or referenced rows: diagnostics={mismatch_counts}"
        )

    _require_non_null_dictionary_values(
        src,
        (("ksa_atomic_items", "split_method"),),
    )
    dst.executescript(
        f"""
        CREATE TABLE {ATOMIC_SPLIT_METHOD_TABLE} (
            split_method_code INTEGER PRIMARY KEY,
            split_method TEXT NOT NULL UNIQUE
        );
        INSERT INTO {ATOMIC_SPLIT_METHOD_TABLE}(split_method)
        SELECT DISTINCT split_method
        FROM source.ksa_atomic_items
        ORDER BY split_method;

        CREATE TABLE {ATOMIC_TIMESTAMP_TABLE} (
            timestamp_code INTEGER PRIMARY KEY,
            timestamp_value TEXT NOT NULL
        );
        INSERT INTO {ATOMIC_TIMESTAMP_TABLE}(timestamp_value)
        SELECT created_at AS timestamp_value
        FROM source.ksa_atomic_items
        UNION
        SELECT created_at AS timestamp_value
        FROM source.ksa_atomic_concept_links
        ORDER BY timestamp_value;
        CREATE INDEX compact_build_atomic_timestamp_lookup
        ON {ATOMIC_TIMESTAMP_TABLE}(timestamp_value);

        CREATE TABLE {ATOMIC_COMPACT_TABLE} (
            atomic_id INTEGER PRIMARY KEY,
            ksa_id INTEGER NOT NULL,
            atom_index INTEGER NOT NULL,
            atom_text_override TEXT,
            concept_id INTEGER NOT NULL,
            original_link_id INTEGER NOT NULL,
            split_method_code INTEGER NOT NULL,
            item_created_at_code INTEGER NOT NULL,
            link_created_at_code INTEGER NOT NULL
        );
        INSERT INTO {ATOMIC_COMPACT_TABLE}(
            atomic_id, ksa_id, atom_index, atom_text_override,
            concept_id, original_link_id, split_method_code,
            item_created_at_code, link_created_at_code
        )
        SELECT
            atomic.atomic_id,
            atomic.ksa_id,
            atomic.atom_index,
            CASE
                WHEN atomic.atom_text IS item.ksa_text_raw THEN NULL
                ELSE atomic.atom_text
            END,
            link.concept_id,
            link.link_id,
            split_method.split_method_code,
            item_timestamp.timestamp_code,
            link_timestamp.timestamp_code
        FROM source.ksa_atomic_items AS atomic
        JOIN source.ksa_items AS item ON item.ksa_id = atomic.ksa_id
        JOIN source.ksa_atomic_concept_links AS link
          ON link.atomic_id = atomic.atomic_id
        JOIN {ATOMIC_SPLIT_METHOD_TABLE} AS split_method
          ON split_method.split_method = atomic.split_method
        JOIN {ATOMIC_TIMESTAMP_TABLE} AS item_timestamp
          ON item_timestamp.timestamp_value = atomic.created_at
        JOIN {ATOMIC_TIMESTAMP_TABLE} AS link_timestamp
          ON link_timestamp.timestamp_value = link.created_at
        ORDER BY atomic.atomic_id;

        DROP INDEX compact_build_atomic_timestamp_lookup;

        CREATE VIEW ksa_atomic_items AS
        SELECT
            atomic.atomic_id AS atomic_id,
            atomic.ksa_id AS ksa_id,
            item.element_id AS element_id,
            item.ksa_type_name AS ksa_type_name,
            atomic.atom_index AS atom_index,
            COALESCE(atomic.atom_text_override, item.ksa_text_raw) AS atom_text,
            concept.normalized_key AS normalized_key,
            split_method.split_method AS split_method,
            CAST('raw' AS TEXT) AS review_status,
            item_timestamp.timestamp_value AS created_at
        FROM {ATOMIC_COMPACT_TABLE} AS atomic
        JOIN ksa_items AS item ON item.ksa_id = atomic.ksa_id
        JOIN ontology_concepts AS concept
          ON concept.concept_id = atomic.concept_id
        JOIN {ATOMIC_SPLIT_METHOD_TABLE} AS split_method
          ON split_method.split_method_code = atomic.split_method_code
        JOIN {ATOMIC_TIMESTAMP_TABLE} AS item_timestamp
          ON item_timestamp.timestamp_code = atomic.item_created_at_code;

        CREATE VIEW ksa_atomic_concept_links AS
        SELECT
            atomic.original_link_id AS link_id,
            atomic.atomic_id AS atomic_id,
            atomic.concept_id AS concept_id,
            CAST('raw' AS TEXT) AS link_status,
            link_timestamp.timestamp_value AS created_at
        FROM {ATOMIC_COMPACT_TABLE} AS atomic
        JOIN {ATOMIC_TIMESTAMP_TABLE} AS link_timestamp
          ON link_timestamp.timestamp_code = atomic.link_created_at_code;
        """
    )
    counts_by_view = _require_count_parity(src, dst, ATOMIC_CANONICAL_OBJECTS)
    _assert_no_mismatches(
        dst,
        table="ksa_atomic_items",
        id_column="atomic_id",
        preserved_columns=ATOMIC_ITEM_COLUMNS,
    )
    _assert_no_mismatches(
        dst,
        table="ksa_atomic_concept_links",
        id_column="link_id",
        preserved_columns=ATOMIC_LINK_COLUMNS,
    )
    override_count, override_chars = dst.execute(
        f"""
        SELECT COUNT(atom_text_override),
               COALESCE(SUM(LENGTH(atom_text_override)), 0)
        FROM {ATOMIC_COMPACT_TABLE}
        """
    ).fetchone()
    return {
        "servable_counts": counts_by_view,
        "atom_text_override_count": int(override_count),
        "atom_text_override_chars": int(override_chars),
    }


def _create_shared_dictionaries(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> None:
    method_specs = tuple(
        (table, "link_method")
        for table in (
            "ncs_training_course_concept_links",
            "ncs_training_course_element_links",
            "training_goal_concept_links",
            "ncs_unit_job_base_links",
        )
    )
    status_specs = tuple(
        (table, "review_status")
        for table in (
            "ncs_training_course_concept_links",
            "ncs_training_course_element_links",
            "training_goal_concept_links",
            "training_delivery_relations",
            "ncs_unit_job_base_links",
        )
    )
    _require_non_null_dictionary_values(
        src,
        (*method_specs, *status_specs, ("training_delivery_relations", "relation_type")),
    )
    dst.executescript(
        f"""
        CREATE TABLE {UNIT_CODE_TABLE} (
            unit_code_code INTEGER PRIMARY KEY,
            unit_code TEXT NOT NULL UNIQUE
        );
        INSERT INTO {UNIT_CODE_TABLE}(unit_code)
        SELECT unit_code FROM source.ncs_training_course_concept_links
        WHERE unit_code IS NOT NULL
        UNION
        SELECT unit_code FROM source.ncs_training_course_element_links
        WHERE unit_code IS NOT NULL
        UNION
        SELECT unit_code FROM source.training_goal_concept_links
        WHERE unit_code IS NOT NULL
        UNION
        SELECT unit_code FROM source.ncs_unit_job_base_links
        WHERE unit_code IS NOT NULL
        ORDER BY unit_code;

        CREATE TABLE {LINK_METHOD_TABLE} (
            link_method_code INTEGER PRIMARY KEY,
            link_method TEXT NOT NULL UNIQUE
        );
        INSERT INTO {LINK_METHOD_TABLE}(link_method)
        SELECT link_method FROM source.ncs_training_course_concept_links
        UNION SELECT link_method FROM source.ncs_training_course_element_links
        UNION SELECT link_method FROM source.training_goal_concept_links
        UNION SELECT link_method FROM source.ncs_unit_job_base_links
        ORDER BY link_method;

        CREATE TABLE {REVIEW_STATUS_TABLE} (
            review_status_code INTEGER PRIMARY KEY,
            review_status TEXT NOT NULL UNIQUE
        );
        INSERT INTO {REVIEW_STATUS_TABLE}(review_status)
        SELECT review_status FROM source.ncs_training_course_concept_links
        UNION SELECT review_status FROM source.ncs_training_course_element_links
        UNION SELECT review_status FROM source.training_goal_concept_links
        UNION SELECT review_status FROM source.training_delivery_relations
        UNION SELECT review_status FROM source.ncs_unit_job_base_links
        ORDER BY review_status;

        CREATE TABLE {DELIVERY_TYPE_TABLE} (
            relation_type_code INTEGER PRIMARY KEY,
            relation_type TEXT NOT NULL UNIQUE
        );
        INSERT INTO {DELIVERY_TYPE_TABLE}(relation_type)
        SELECT DISTINCT relation_type
        FROM source.training_delivery_relations
        ORDER BY relation_type;

        CREATE TABLE {DELIVERY_VALUE_TABLE} (
            delivery_value_code INTEGER PRIMARY KEY,
            relation_value TEXT NOT NULL,
            normalized_value TEXT NOT NULL,
            UNIQUE(relation_value, normalized_value)
        );
        INSERT INTO {DELIVERY_VALUE_TABLE}(relation_value, normalized_value)
        SELECT DISTINCT relation_value, normalized_value
        FROM source.training_delivery_relations
        ORDER BY relation_value, normalized_value;

        CREATE TABLE {TRAINING_EVIDENCE_TABLE} (
            evidence_code INTEGER PRIMARY KEY,
            evidence_text TEXT NOT NULL
        );
        INSERT INTO {TRAINING_EVIDENCE_TABLE}(evidence_text)
        SELECT evidence_text AS evidence_text
        FROM source.ncs_training_course_concept_links
        WHERE evidence_text IS NOT NULL
        UNION
        SELECT evidence_text AS evidence_text
        FROM source.ncs_training_course_element_links
        WHERE evidence_text IS NOT NULL
        UNION
        SELECT evidence_text AS evidence_text
        FROM source.training_goal_concept_links
        WHERE evidence_text IS NOT NULL
        UNION
        SELECT evidence_text AS evidence_text
        FROM source.training_delivery_relations
        WHERE evidence_text IS NOT NULL
        ORDER BY evidence_text;

        CREATE TABLE {TRAINING_TIMESTAMP_TABLE} (
            timestamp_code INTEGER PRIMARY KEY,
            timestamp_value TEXT NOT NULL
        );
        INSERT INTO {TRAINING_TIMESTAMP_TABLE}(timestamp_value)
        SELECT created_at AS timestamp_value
        FROM source.ncs_training_course_concept_links
        WHERE created_at IS NOT NULL
        UNION SELECT updated_at AS timestamp_value
        FROM source.ncs_training_course_concept_links
        WHERE updated_at IS NOT NULL
        UNION SELECT created_at AS timestamp_value
        FROM source.ncs_training_course_element_links
        WHERE created_at IS NOT NULL
        UNION SELECT updated_at AS timestamp_value
        FROM source.ncs_training_course_element_links
        WHERE updated_at IS NOT NULL
        UNION SELECT created_at AS timestamp_value
        FROM source.training_goal_concept_links
        WHERE created_at IS NOT NULL
        UNION SELECT updated_at AS timestamp_value
        FROM source.training_goal_concept_links
        WHERE updated_at IS NOT NULL
        UNION SELECT created_at AS timestamp_value
        FROM source.training_delivery_relations
        WHERE created_at IS NOT NULL
        UNION SELECT updated_at AS timestamp_value
        FROM source.training_delivery_relations
        WHERE updated_at IS NOT NULL
        ORDER BY timestamp_value;

        CREATE INDEX compact_build_training_evidence_lookup
        ON {TRAINING_EVIDENCE_TABLE}(evidence_text);
        CREATE INDEX compact_build_training_timestamp_lookup
        ON {TRAINING_TIMESTAMP_TABLE}(timestamp_value);
        """
    )


def create_training_storage(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> dict[str, object]:
    """Create compact training link tables and canonical compatibility views."""

    for table, columns in (
        ("ncs_training_course_concept_links", TRAINING_CONCEPT_COLUMNS),
        ("ncs_training_course_element_links", TRAINING_ELEMENT_COLUMNS),
        ("training_goal_concept_links", TRAINING_GOAL_COLUMNS),
        ("training_delivery_relations", TRAINING_DELIVERY_COLUMNS),
        ("ncs_unit_job_base_links", JOB_BASE_COLUMNS),
    ):
        require_exact_columns(src, table, columns)
    _create_shared_dictionaries(src, dst)

    dst.executescript(
        f"""
        CREATE TABLE {TRAINING_CONCEPT_COMPACT_TABLE} (
            link_id INTEGER PRIMARY KEY,
            training_course_id INTEGER NOT NULL,
            unit_code_code INTEGER,
            concept_id INTEGER NOT NULL,
            link_method_code INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            evidence_code INTEGER,
            review_status_code INTEGER NOT NULL,
            created_at_code INTEGER,
            updated_at_code INTEGER
        );
        INSERT INTO {TRAINING_CONCEPT_COMPACT_TABLE}
        SELECT link.link_id, link.training_course_id, unit_code.unit_code_code,
               link.concept_id, link_method.link_method_code,
               link.confidence_score, evidence.evidence_code,
               review_status.review_status_code,
               created.timestamp_code, updated.timestamp_code
        FROM source.ncs_training_course_concept_links AS link
        LEFT JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code = link.unit_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method = link.link_method
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = link.review_status
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_text = link.evidence_text
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_value = link.created_at
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_value = link.updated_at
        ORDER BY link.link_id;

        CREATE TABLE {TRAINING_ELEMENT_COMPACT_TABLE} (
            link_id INTEGER PRIMARY KEY,
            training_course_id INTEGER NOT NULL,
            unit_code_code INTEGER NOT NULL,
            element_id INTEGER NOT NULL,
            link_method_code INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            evidence_code INTEGER,
            review_status_code INTEGER NOT NULL,
            created_at_code INTEGER,
            updated_at_code INTEGER
        );
        INSERT INTO {TRAINING_ELEMENT_COMPACT_TABLE}
        SELECT link.link_id, link.training_course_id, unit_code.unit_code_code,
               link.element_id, link_method.link_method_code,
               link.confidence_score, evidence.evidence_code,
               review_status.review_status_code,
               created.timestamp_code, updated.timestamp_code
        FROM source.ncs_training_course_element_links AS link
        JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code = link.unit_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method = link.link_method
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = link.review_status
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_text = link.evidence_text
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_value = link.created_at
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_value = link.updated_at
        ORDER BY link.link_id;

        CREATE TABLE {TRAINING_GOAL_COMPACT_TABLE} (
            link_id INTEGER PRIMARY KEY,
            training_course_id INTEGER NOT NULL,
            unit_code_code INTEGER,
            element_id INTEGER,
            concept_id INTEGER NOT NULL,
            link_method_code INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            evidence_code INTEGER,
            review_status_code INTEGER NOT NULL,
            created_at_code INTEGER,
            updated_at_code INTEGER
        );
        INSERT INTO {TRAINING_GOAL_COMPACT_TABLE}
        SELECT link.link_id, link.training_course_id, unit_code.unit_code_code,
               link.element_id, link.concept_id, link_method.link_method_code,
               link.confidence_score, evidence.evidence_code,
               review_status.review_status_code,
               created.timestamp_code, updated.timestamp_code
        FROM source.training_goal_concept_links AS link
        LEFT JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code = link.unit_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method = link.link_method
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = link.review_status
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_text = link.evidence_text
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_value = link.created_at
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_value = link.updated_at
        ORDER BY link.link_id;

        CREATE TABLE {TRAINING_DELIVERY_COMPACT_TABLE} (
            relation_id INTEGER PRIMARY KEY,
            training_course_id INTEGER NOT NULL,
            relation_type_code INTEGER NOT NULL,
            delivery_value_code INTEGER NOT NULL,
            numeric_value REAL,
            confidence_score REAL NOT NULL,
            evidence_code INTEGER,
            review_status_code INTEGER NOT NULL,
            created_at_code INTEGER,
            updated_at_code INTEGER
        );
        INSERT INTO {TRAINING_DELIVERY_COMPACT_TABLE}
        SELECT relation.relation_id, relation.training_course_id,
               relation_type.relation_type_code,
               delivery_value.delivery_value_code,
               relation.numeric_value, relation.confidence_score,
               evidence.evidence_code, review_status.review_status_code,
               created.timestamp_code, updated.timestamp_code
        FROM source.training_delivery_relations AS relation
        JOIN {DELIVERY_TYPE_TABLE} AS relation_type
          ON relation_type.relation_type = relation.relation_type
        JOIN {DELIVERY_VALUE_TABLE} AS delivery_value
          ON delivery_value.relation_value = relation.relation_value
         AND delivery_value.normalized_value = relation.normalized_value
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = relation.review_status
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_text = relation.evidence_text
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_value = relation.created_at
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_value = relation.updated_at
        ORDER BY relation.relation_id;

        CREATE VIEW ncs_training_course_concept_links AS
        SELECT link.link_id, link.training_course_id, unit_code.unit_code,
               link.concept_id, link_method.link_method, link.confidence_score,
               evidence.evidence_text,
               review_status.review_status,
               created.timestamp_value AS created_at,
               updated.timestamp_value AS updated_at
        FROM {TRAINING_CONCEPT_COMPACT_TABLE} AS link
        LEFT JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code_code = link.unit_code_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method_code = link.link_method_code
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = link.review_status_code
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_code = link.evidence_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_code = link.created_at_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_code = link.updated_at_code;

        CREATE VIEW ncs_training_course_element_links AS
        SELECT link.link_id, link.training_course_id, unit_code.unit_code,
               link.element_id, link_method.link_method, link.confidence_score,
               evidence.evidence_text,
               review_status.review_status,
               created.timestamp_value AS created_at,
               updated.timestamp_value AS updated_at
        FROM {TRAINING_ELEMENT_COMPACT_TABLE} AS link
        JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code_code = link.unit_code_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method_code = link.link_method_code
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = link.review_status_code
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_code = link.evidence_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_code = link.created_at_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_code = link.updated_at_code;

        CREATE VIEW training_goal_concept_links AS
        SELECT link.link_id, link.training_course_id, unit_code.unit_code,
               link.element_id, link.concept_id, link_method.link_method,
               link.confidence_score, evidence.evidence_text,
               review_status.review_status,
               created.timestamp_value AS created_at,
               updated.timestamp_value AS updated_at
        FROM {TRAINING_GOAL_COMPACT_TABLE} AS link
        LEFT JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code_code = link.unit_code_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method_code = link.link_method_code
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = link.review_status_code
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_code = link.evidence_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_code = link.created_at_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_code = link.updated_at_code;

        CREATE VIEW training_delivery_relations AS
        SELECT relation.relation_id, relation.training_course_id,
               relation_type.relation_type,
               delivery_value.relation_value,
               delivery_value.normalized_value,
               relation.numeric_value,
               evidence.evidence_text,
               relation.confidence_score,
               review_status.review_status,
               created.timestamp_value AS created_at,
               updated.timestamp_value AS updated_at
        FROM {TRAINING_DELIVERY_COMPACT_TABLE} AS relation
        JOIN {DELIVERY_TYPE_TABLE} AS relation_type
          ON relation_type.relation_type_code = relation.relation_type_code
        JOIN {DELIVERY_VALUE_TABLE} AS delivery_value
          ON delivery_value.delivery_value_code = relation.delivery_value_code
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = relation.review_status_code
        LEFT JOIN {TRAINING_EVIDENCE_TABLE} AS evidence
          ON evidence.evidence_code = relation.evidence_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS created
          ON created.timestamp_code = relation.created_at_code
        LEFT JOIN {TRAINING_TIMESTAMP_TABLE} AS updated
          ON updated.timestamp_code = relation.updated_at_code;

        DROP INDEX compact_build_training_evidence_lookup;
        DROP INDEX compact_build_training_timestamp_lookup;
        """
    )
    counts = _require_count_parity(src, dst, TRAINING_CANONICAL_OBJECTS)
    policies = (
        (
            "ncs_training_course_concept_links",
            "link_id",
            TRAINING_CONCEPT_COLUMNS,
        ),
        (
            "ncs_training_course_element_links",
            "link_id",
            TRAINING_ELEMENT_COLUMNS,
        ),
        (
            "training_goal_concept_links",
            "link_id",
            TRAINING_GOAL_COLUMNS,
        ),
        (
            "training_delivery_relations",
            "relation_id",
            TRAINING_DELIVERY_COLUMNS,
        ),
    )
    for table, id_column, preserved in policies:
        _assert_no_mismatches(
            dst,
            table=table,
            id_column=id_column,
            preserved_columns=preserved,
        )
    return {
        "servable_counts": counts,
        "link_methods": sorted(
            str(row[0])
            for row in dst.execute(
                f"SELECT link_method FROM {LINK_METHOD_TABLE} ORDER BY link_method"
            ).fetchall()
        ),
        "review_statuses": sorted(
            str(row[0])
            for row in dst.execute(
                f"SELECT review_status FROM {REVIEW_STATUS_TABLE} ORDER BY review_status"
            ).fetchall()
        ),
        "evidence_dictionary_count": _row_count(dst, TRAINING_EVIDENCE_TABLE),
        "timestamp_dictionary_count": _row_count(dst, TRAINING_TIMESTAMP_TABLE),
    }


def create_job_base_storage(
    src: sqlite3.Connection,
    dst: sqlite3.Connection,
) -> dict[str, object]:
    """Compact tool-consumed job-base fields and omit internal collection metadata."""

    require_exact_columns(src, "ncs_unit_job_base_links", JOB_BASE_COLUMNS)
    required_reference_columns = {
        "classifications": {
            "classification_id",
            "major_code",
            "major_name",
            "middle_code",
            "middle_name",
            "small_code",
            "small_name",
            "sub_code",
            "sub_name",
        },
        "competency_units": {
            "unit_code",
            "classification_id",
            "unit_name_raw",
        },
        "ncs_job_base_competencies": {"job_base_competency_id"},
        "ncs_job_base_factors": {
            "job_base_factor_id",
            "job_base_competency_id",
        },
    }
    for table, required in required_reference_columns.items():
        missing = required - set(_columns(src, table))
        if missing:
            raise RuntimeError(
                f"compact job-base reference schema mismatch for {table}: "
                f"missing={sorted(missing)}"
            )

    unsafe_reference_count = int(
        src.execute(
            """
            SELECT COUNT(*)
            FROM ncs_unit_job_base_links AS link
            LEFT JOIN competency_units AS unit ON unit.unit_code = link.unit_code
            LEFT JOIN classifications AS classification
              ON classification.classification_id = unit.classification_id
            LEFT JOIN ncs_job_base_competencies AS competency
              ON competency.job_base_competency_id = link.job_base_competency_id
            LEFT JOIN ncs_job_base_factors AS factor
              ON factor.job_base_factor_id = link.job_base_factor_id
            WHERE unit.unit_code IS NULL
               OR classification.classification_id IS NULL
               OR competency.job_base_competency_id IS NULL
               OR (link.job_base_factor_id IS NOT NULL
                   AND factor.job_base_factor_id IS NULL)
               OR (factor.job_base_factor_id IS NOT NULL
                   AND factor.job_base_competency_id
                       <> link.job_base_competency_id)
            """
        ).fetchone()[0]
    )
    if unsafe_reference_count:
        raise RuntimeError(
            "compact job-base reconstruction guard found orphaned or "
            f"cross-competency references: {unsafe_reference_count}"
        )

    dst.executescript(
        f"""
        CREATE TABLE {JOB_BASE_COMPACT_TABLE} (
            link_id INTEGER PRIMARY KEY,
            unit_code_code INTEGER NOT NULL,
            job_base_competency_id INTEGER NOT NULL,
            job_base_factor_id INTEGER,
            override_mask INTEGER NOT NULL,
            ncs_lclas_cd_override TEXT,
            ncs_lclas_cdnm_override TEXT,
            ncs_mclas_cd_override TEXT,
            ncs_mclas_cdnm_override TEXT,
            ncs_sclas_cd_override TEXT,
            ncs_sclas_cdnm_override TEXT,
            ncs_subd_cd_override TEXT,
            ncs_subd_cdnm_override TEXT,
            compe_unit_name_override TEXT,
            link_method_code INTEGER NOT NULL,
            confidence_score REAL NOT NULL,
            review_status_code INTEGER NOT NULL
        );
        INSERT INTO {JOB_BASE_COMPACT_TABLE}
        SELECT
            link.link_id,
            unit_code.unit_code_code,
            link.job_base_competency_id,
            link.job_base_factor_id,
            (CASE WHEN link.ncs_lclas_cd IS classification.major_code THEN 0 ELSE 1 END)
            + (CASE WHEN link.ncs_lclas_cdnm IS classification.major_name THEN 0 ELSE 2 END)
            + (CASE WHEN link.ncs_mclas_cd IS classification.middle_code THEN 0 ELSE 4 END)
            + (CASE WHEN link.ncs_mclas_cdnm IS classification.middle_name THEN 0 ELSE 8 END)
            + (CASE WHEN link.ncs_sclas_cd IS classification.small_code THEN 0 ELSE 16 END)
            + (CASE WHEN link.ncs_sclas_cdnm IS classification.small_name THEN 0 ELSE 32 END)
            + (CASE WHEN link.ncs_subd_cd IS classification.sub_code THEN 0 ELSE 64 END)
            + (CASE WHEN link.ncs_subd_cdnm IS classification.sub_name THEN 0 ELSE 128 END)
            + (CASE WHEN link.compe_unit_name IS unit.unit_name_raw THEN 0 ELSE 256 END),
            CASE WHEN link.ncs_lclas_cd IS classification.major_code THEN NULL ELSE link.ncs_lclas_cd END,
            CASE WHEN link.ncs_lclas_cdnm IS classification.major_name THEN NULL ELSE link.ncs_lclas_cdnm END,
            CASE WHEN link.ncs_mclas_cd IS classification.middle_code THEN NULL ELSE link.ncs_mclas_cd END,
            CASE WHEN link.ncs_mclas_cdnm IS classification.middle_name THEN NULL ELSE link.ncs_mclas_cdnm END,
            CASE WHEN link.ncs_sclas_cd IS classification.small_code THEN NULL ELSE link.ncs_sclas_cd END,
            CASE WHEN link.ncs_sclas_cdnm IS classification.small_name THEN NULL ELSE link.ncs_sclas_cdnm END,
            CASE WHEN link.ncs_subd_cd IS classification.sub_code THEN NULL ELSE link.ncs_subd_cd END,
            CASE WHEN link.ncs_subd_cdnm IS classification.sub_name THEN NULL ELSE link.ncs_subd_cdnm END,
            CASE WHEN link.compe_unit_name IS unit.unit_name_raw THEN NULL ELSE link.compe_unit_name END,
            link_method.link_method_code,
            link.confidence_score,
            review_status.review_status_code
        FROM source.ncs_unit_job_base_links AS link
        JOIN source.competency_units AS unit ON unit.unit_code = link.unit_code
        JOIN source.classifications AS classification
          ON classification.classification_id = unit.classification_id
        JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code = link.unit_code
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method = link.link_method
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status = link.review_status
        ORDER BY link.link_id;

        CREATE VIEW ncs_unit_job_base_links (
            link_id, unit_code, job_base_competency_id, job_base_factor_id,
            ncs_lclas_cd, ncs_lclas_cdnm, ncs_mclas_cd, ncs_mclas_cdnm,
            ncs_sclas_cd, ncs_sclas_cdnm, ncs_subd_cd, ncs_subd_cdnm,
            compe_unit_name, link_method, confidence_score, source_payload,
            api_fetched_at, review_status, created_at, updated_at
        ) AS
        SELECT
            link.link_id,
            unit_code.unit_code,
            link.job_base_competency_id,
            link.job_base_factor_id,
            CASE WHEN (link.override_mask & 1) <> 0
                 THEN link.ncs_lclas_cd_override ELSE classification.major_code END,
            CASE WHEN (link.override_mask & 2) <> 0
                 THEN link.ncs_lclas_cdnm_override ELSE classification.major_name END,
            CASE WHEN (link.override_mask & 4) <> 0
                 THEN link.ncs_mclas_cd_override ELSE classification.middle_code END,
            CASE WHEN (link.override_mask & 8) <> 0
                 THEN link.ncs_mclas_cdnm_override ELSE classification.middle_name END,
            CASE WHEN (link.override_mask & 16) <> 0
                 THEN link.ncs_sclas_cd_override ELSE classification.small_code END,
            CASE WHEN (link.override_mask & 32) <> 0
                 THEN link.ncs_sclas_cdnm_override ELSE classification.small_name END,
            CASE WHEN (link.override_mask & 64) <> 0
                 THEN link.ncs_subd_cd_override ELSE classification.sub_code END,
            CASE WHEN (link.override_mask & 128) <> 0
                 THEN link.ncs_subd_cdnm_override ELSE classification.sub_name END,
            CASE WHEN (link.override_mask & 256) <> 0
                 THEN link.compe_unit_name_override ELSE unit.unit_name_raw END,
            link_method.link_method,
            link.confidence_score,
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT),
            review_status.review_status,
            CAST(NULL AS TEXT),
            CAST(NULL AS TEXT)
        FROM {JOB_BASE_COMPACT_TABLE} AS link
        JOIN {UNIT_CODE_TABLE} AS unit_code
          ON unit_code.unit_code_code = link.unit_code_code
        JOIN competency_units AS unit ON unit.unit_code = unit_code.unit_code
        JOIN classifications AS classification
          ON classification.classification_id = unit.classification_id
        JOIN {LINK_METHOD_TABLE} AS link_method
          ON link_method.link_method_code = link.link_method_code
        JOIN {REVIEW_STATUS_TABLE} AS review_status
          ON review_status.review_status_code = link.review_status_code;
        """
    )
    counts = _require_count_parity(src, dst, JOB_BASE_CANONICAL_OBJECTS)
    _assert_no_mismatches(
        dst,
        table="ncs_unit_job_base_links",
        id_column="link_id",
        preserved_columns=JOB_BASE_SERVABLE_COLUMNS,
    )
    internal_value_count = int(
        dst.execute(
            """
            SELECT COUNT(*)
            FROM ncs_unit_job_base_links
            WHERE source_payload IS NOT NULL
               OR api_fetched_at IS NOT NULL
               OR created_at IS NOT NULL
               OR updated_at IS NOT NULL
            """
        ).fetchone()[0]
    )
    if internal_value_count:
        raise RuntimeError(
            "compact job-base internal serving columns must remain NULL: "
            f"rows={internal_value_count}"
        )
    override_rows = int(
        dst.execute(
            f"SELECT COUNT(*) FROM {JOB_BASE_COMPACT_TABLE} WHERE override_mask <> 0"
        ).fetchone()[0]
    )
    return {
        "servable_counts": counts,
        "override_row_count": override_rows,
        "omitted_internal_columns": list(JOB_BASE_INTERNAL_COLUMNS),
    }


def compact_physical_tables() -> tuple[str, ...]:
    return (
        *ATOMIC_PHYSICAL_TABLES,
        *TRAINING_PHYSICAL_TABLES,
        *JOB_BASE_PHYSICAL_TABLES,
    )


def compact_canonical_objects() -> tuple[str, ...]:
    return (
        *ATOMIC_CANONICAL_OBJECTS,
        *TRAINING_CANONICAL_OBJECTS,
        *JOB_BASE_CANONICAL_OBJECTS,
    )
