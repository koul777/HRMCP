from __future__ import annotations

from collections.abc import Iterable, Sequence
import sqlite3
from typing import Any


COMPACT_POSTING_CODEC = "delta_uvarint_v1"
ONTOLOGY_RELATION_TYPE_TABLE = "ontology_relation_types"
ONTOLOGY_RELATION_OUTGOING_TABLE = "ontology_relation_outgoing"
ONTOLOGY_RELATION_INCOMING_TABLE = "ontology_relation_incoming"
CRITERIA_CONCEPT_FORWARD_TABLE = "criteria_concept_forward"
CRITERIA_CONCEPT_INVERSE_TABLE = "criteria_concept_inverse"


def encode_posting_ids(values: Iterable[int]) -> bytes:
    """Encode sorted unique non-negative IDs as delta-compressed uvarints."""
    output = bytearray()
    previous = 0
    for value in sorted({int(item) for item in values}):
        if value < 0:
            raise ValueError("posting IDs must be non-negative")
        delta = value - previous
        while delta >= 0x80:
            output.append((delta & 0x7F) | 0x80)
            delta >>= 7
        output.append(delta)
        previous = value
    return bytes(output)


def decode_posting_ids(payload: bytes | bytearray | memoryview | None) -> list[int]:
    """Decode a ``delta_uvarint_v1`` posting payload."""
    if payload is None:
        return []
    current = 0
    shift = 0
    previous = 0
    values: list[int] = []
    for raw_byte in bytes(payload):
        current |= (raw_byte & 0x7F) << shift
        if raw_byte & 0x80:
            shift += 7
            if shift > 63:
                raise ValueError("posting contains an oversized uvarint")
            continue
        previous += current
        values.append(previous)
        current = 0
        shift = 0
    if shift:
        raise ValueError("posting ends with a truncated uvarint")
    return values


def sqlite_object_exists(
    conn: sqlite3.Connection,
    name: str,
    *,
    object_types: Sequence[str] = ("table", "view"),
) -> bool:
    placeholders = ",".join("?" for _ in object_types)
    row = conn.execute(
        f"SELECT 1 FROM sqlite_master WHERE type IN ({placeholders}) AND name = ?",
        (*object_types, name),
    ).fetchone()
    return row is not None


def has_compact_criteria_postings(conn: sqlite3.Connection) -> bool:
    return all(
        sqlite_object_exists(conn, table)
        for table in (CRITERIA_CONCEPT_FORWARD_TABLE, CRITERIA_CONCEPT_INVERSE_TABLE)
    )


def has_compact_ontology_postings(conn: sqlite3.Connection) -> bool:
    return all(
        sqlite_object_exists(conn, table)
        for table in (
            ONTOLOGY_RELATION_TYPE_TABLE,
            ONTOLOGY_RELATION_OUTGOING_TABLE,
            ONTOLOGY_RELATION_INCOMING_TABLE,
        )
    )


def criteria_concept_ids(
    conn: sqlite3.Connection,
    criteria_ids: Iterable[int],
) -> dict[int, list[int]]:
    ids = sorted({int(value) for value in criteria_ids})
    if not ids:
        return {}
    if not has_compact_criteria_postings(conn):
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT criteria_id, concept_id
            FROM criteria_concept_links
            WHERE criteria_id IN ({placeholders})
              AND LOWER(COALESCE(link_status, '')) <> 'rejected'
            ORDER BY criteria_id, concept_id
            """,
            ids,
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(int(row[0]), []).append(int(row[1]))
        return result

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT criteria_id, concept_count, concept_ids
        FROM {CRITERIA_CONCEPT_FORWARD_TABLE}
        WHERE criteria_id IN ({placeholders})
        ORDER BY criteria_id
        """,
        ids,
    ).fetchall()
    result = {}
    for row in rows:
        decoded = decode_posting_ids(row[2])
        if len(decoded) != int(row[1]):
            raise ValueError(
                f"criteria posting count mismatch for criteria_id={int(row[0])}"
            )
        result[int(row[0])] = decoded
    return result


def concept_criteria_ids(
    conn: sqlite3.Connection,
    concept_ids: Iterable[int],
) -> dict[int, list[int]]:
    ids = sorted({int(value) for value in concept_ids})
    if not ids:
        return {}
    if not sqlite_object_exists(conn, CRITERIA_CONCEPT_INVERSE_TABLE):
        placeholders = ",".join("?" for _ in ids)
        rows = conn.execute(
            f"""
            SELECT concept_id, criteria_id
            FROM criteria_concept_links
            WHERE concept_id IN ({placeholders})
              AND LOWER(COALESCE(link_status, '')) <> 'rejected'
            ORDER BY concept_id, criteria_id
            """,
            ids,
        ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(int(row[0]), []).append(int(row[1]))
        return result

    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT concept_id, criteria_count, criteria_ids
        FROM {CRITERIA_CONCEPT_INVERSE_TABLE}
        WHERE concept_id IN ({placeholders})
        ORDER BY concept_id
        """,
        ids,
    ).fetchall()
    result = {}
    for row in rows:
        decoded = decode_posting_ids(row[2])
        if len(decoded) != int(row[1]):
            raise ValueError(
                f"criteria inverse posting count mismatch for concept_id={int(row[0])}"
            )
        result[int(row[0])] = decoded
    return result


def _synthetic_relation_id(source_id: int, relation_type_code: int, target_id: int) -> int:
    return source_id * 10_000_000 + relation_type_code * 1_000_000 + target_id


def _relation_type_map(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    columns = {
        str(row[1])
        for row in conn.execute(
            f"PRAGMA table_info({ONTOLOGY_RELATION_TYPE_TABLE})"
        ).fetchall()
    }
    label_sql = "relation_label" if "relation_label" in columns else "relation_type"
    return {
        int(row[0]): (str(row[1]), str(row[2]))
        for row in conn.execute(
            f"""
            SELECT relation_type_code, relation_type, {label_sql}
            FROM {ONTOLOGY_RELATION_TYPE_TABLE}
            ORDER BY relation_type_code
            """
        ).fetchall()
    }


def compact_ontology_relation_rows(
    conn: sqlite3.Connection,
    *,
    source_ids: Iterable[int] | None = None,
    target_ids: Iterable[int] | None = None,
    incident_ids: Iterable[int] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read logical ontology edges from compact forward/inverse postings.

    ``source_ids`` and ``target_ids`` constrain the respective endpoint.  When
    ``incident_ids`` is provided, both incoming and outgoing postings are read.
    The returned keys match the legacy relation-row shape used by public tools.
    """
    if not has_compact_ontology_postings(conn):
        raise ValueError("compact ontology postings are not available")
    source_filter = {int(value) for value in source_ids or ()}
    target_filter = {int(value) for value in target_ids or ()}
    incident_filter = {int(value) for value in incident_ids or ()}
    type_map = _relation_type_map(conn)
    edges: dict[tuple[int, int, int], dict[str, Any]] = {}

    def add_edge(source_id: int, type_code: int, target_id: int) -> None:
        if source_filter and source_id not in source_filter:
            return
        if target_filter and target_id not in target_filter:
            return
        relation_type, relation_label = type_map[type_code]
        key = (source_id, type_code, target_id)
        edges[key] = {
            "relation_id": _synthetic_relation_id(source_id, type_code, target_id),
            "source_concept_id": source_id,
            "target_concept_id": target_id,
            "relation_type": relation_type,
            "relation_label": relation_label,
            "review_status": "candidate",
        }

    outgoing_keys = sorted(source_filter | incident_filter)
    if outgoing_keys:
        placeholders = ",".join("?" for _ in outgoing_keys)
        rows = conn.execute(
            f"""
            SELECT source_concept_id, relation_type_code, target_count, target_ids
            FROM {ONTOLOGY_RELATION_OUTGOING_TABLE}
            WHERE source_concept_id IN ({placeholders})
            ORDER BY source_concept_id, relation_type_code
            """,
            outgoing_keys,
        ).fetchall()
        for row in rows:
            targets = decode_posting_ids(row[3])
            if len(targets) != int(row[2]):
                raise ValueError(
                    "ontology outgoing posting count mismatch for "
                    f"source_concept_id={int(row[0])}"
                )
            for target_id in targets:
                add_edge(int(row[0]), int(row[1]), target_id)

    incoming_keys = sorted(target_filter | incident_filter)
    if incoming_keys:
        placeholders = ",".join("?" for _ in incoming_keys)
        rows = conn.execute(
            f"""
            SELECT target_concept_id, relation_type_code, source_count, source_ids
            FROM {ONTOLOGY_RELATION_INCOMING_TABLE}
            WHERE target_concept_id IN ({placeholders})
            ORDER BY target_concept_id, relation_type_code
            """,
            incoming_keys,
        ).fetchall()
        for row in rows:
            sources = decode_posting_ids(row[3])
            if len(sources) != int(row[2]):
                raise ValueError(
                    "ontology incoming posting count mismatch for "
                    f"target_concept_id={int(row[0])}"
                )
            for source_id in sources:
                add_edge(source_id, int(row[1]), int(row[0]))

    rows = [edges[key] for key in sorted(edges)]
    if limit is not None:
        return rows[: max(int(limit), 0)]
    return rows


def ontology_relation_rows(
    conn: sqlite3.Connection,
    *,
    source_ids: Iterable[int] | None = None,
    target_ids: Iterable[int] | None = None,
    incident_ids: Iterable[int] | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Return relation rows from either the canonical or compact schema."""
    if has_compact_ontology_postings(conn):
        return compact_ontology_relation_rows(
            conn,
            source_ids=source_ids,
            target_ids=target_ids,
            incident_ids=incident_ids,
            limit=limit,
        )

    source_filter = sorted({int(value) for value in source_ids or ()})
    target_filter = sorted({int(value) for value in target_ids or ()})
    incident_filter = sorted({int(value) for value in incident_ids or ()})
    clauses: list[str] = []
    params: list[int] = []
    if source_filter:
        placeholders = ",".join("?" for _ in source_filter)
        clauses.append(f"source_concept_id IN ({placeholders})")
        params.extend(source_filter)
    if target_filter:
        placeholders = ",".join("?" for _ in target_filter)
        clauses.append(f"target_concept_id IN ({placeholders})")
        params.extend(target_filter)
    if incident_filter:
        placeholders = ",".join("?" for _ in incident_filter)
        clauses.append(
            f"(source_concept_id IN ({placeholders}) OR "
            f"target_concept_id IN ({placeholders}))"
        )
        params.extend(incident_filter)
        params.extend(incident_filter)
    where = " AND ".join(f"({clause})" for clause in clauses) or "1 = 1"
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(max(int(limit), 0))
    rows = conn.execute(
        f"""
        SELECT relation_id, source_concept_id, target_concept_id,
               relation_type, relation_label, review_status
        FROM ontology_concept_relations
        WHERE {where}
          AND LOWER(COALESCE(review_status, '')) <> 'rejected'
        ORDER BY relation_id{limit_sql}
        """,
        params,
    ).fetchall()
    columns = (
        "relation_id",
        "source_concept_id",
        "target_concept_id",
        "relation_type",
        "relation_label",
        "review_status",
    )
    return [dict(zip(columns, row)) for row in rows]
