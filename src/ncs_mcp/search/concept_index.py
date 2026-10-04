"""Small, conservative candidate masks for ontology-concept LIKE searches.

Masks can reject impossible substrings, never decide matches or relevance.
The caller must retain the original LIKE predicates, ordering and limit.
ASCII folding matches native SQLite LIKE across Python Unicode versions.
"""
from __future__ import annotations

import re
import sqlite3
import zlib
from typing import Any

CONCEPT_MASK_TABLE = "ontology_concept_masks"
CONCEPT_MASK_SCHEMA = "ncs_concept_candidate_masks_v1"
CONCEPT_MASK_BITS = 47  # Fits SQLite's six-byte signed integer representation.
CONCEPT_MASK_REQUIRED_MANIFEST = {
    "concept_mask_schema": CONCEPT_MASK_SCHEMA,
    "concept_mask_policy": "ascii_fold_crc32_bigrams_v1",
    "concept_mask_bits": str(CONCEPT_MASK_BITS),
    "concept_mask_fields": "concept_name,normalized_key",
    "concept_mask_coverage": "complete_target_concept_ids",
}
_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
_LIKE_WILDCARDS = re.compile(r"[%_]")


def concept_text_mask(*values: str | None) -> int:
    mask = 0
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            raise TypeError("Concept mask fields must be TEXT or NULL")
        text = value.translate(_ASCII_FOLD)
        for index in range(len(text) - 1):
            mask |= 1 << (zlib.crc32(text[index:index + 2].encode("utf-8")) % CONCEPT_MASK_BITS)
    return mask


def concept_like_mask(pattern: str) -> int | None:
    # Native LIKE has no implicit escape character and terminates at NUL.
    # Every literal run is necessary even when wildcards separate the runs.
    literals = _LIKE_WILDCARDS.split(pattern.split("\0", 1)[0])
    return concept_text_mask(*literals) or None


def create_concept_mask_index(target: sqlite3.Connection) -> int:
    """Build a complete derived index on a fresh Builder target, not its source.

    An existing index is an error. The caller publishes the manifest only
    after generation and coverage checks succeed in the same target version.
    """
    target.execute(
        f"CREATE TABLE {CONCEPT_MASK_TABLE}("
        "concept_id INTEGER PRIMARY KEY, mask INTEGER NOT NULL "
        f"CHECK(mask >= 0 AND mask < {1 << CONCEPT_MASK_BITS}))"
    )
    cursor = target.execute(
        "SELECT concept_id, concept_name, normalized_key FROM ontology_concepts ORDER BY concept_id"
    )
    count = 0
    while rows := cursor.fetchmany(1000):
        target.executemany(
            f"INSERT INTO {CONCEPT_MASK_TABLE}(concept_id, mask) VALUES (?, ?)",
            ((row[0], concept_text_mask(row[1], row[2])) for row in rows),
        )
        count += len(rows)
    # Include both directions so a missing/null identifier or unexpected row
    # cannot be attested merely because the two table counts happen to agree.
    source_count = target.execute("SELECT COUNT(*) FROM ontology_concepts").fetchone()[0]
    missing = target.execute(
        f"SELECT concept_id FROM ontology_concepts EXCEPT SELECT concept_id FROM {CONCEPT_MASK_TABLE} LIMIT 1"
    ).fetchone()
    extra = target.execute(
        f"SELECT concept_id FROM {CONCEPT_MASK_TABLE} EXCEPT SELECT concept_id FROM ontology_concepts LIMIT 1"
    ).fetchone()
    if count != source_count or missing is not None or extra is not None:
        raise ValueError("Concept candidate mask coverage is incomplete")
    return count


def compatible_concept_masks(conn: Any, patterns: tuple[str, str]) -> tuple[int, ...] | None:
    """Return necessary masks only for a complete Builder-attested index.

    Snapshot checksum/immutability and the Builder coverage check establish
    data completeness, as with the lexical candidate indexes. Runtime checks
    the small contract and schema, without re-reading all concept fields.
    """
    masks = tuple(concept_like_mask(pattern) for pattern in patterns)
    if any(mask is None for mask in masks):
        return None  # An unindexable OR branch must retain the original scan.
    names = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?, ?)",
        (CONCEPT_MASK_TABLE, "serving_snapshot_manifest"),
    ).fetchall()
    if {row[0] for row in names} != {CONCEPT_MASK_TABLE, "serving_snapshot_manifest"}:
        return None
    # An older or incompatible producer may use this table name with a
    # different layout. Inspect before querying its columns so compatibility
    # failure selects the original scan instead of failing the whole request.
    manifest_columns = conn.execute("PRAGMA table_info(serving_snapshot_manifest)").fetchall()
    if not {"manifest_key", "manifest_value"}.issubset({row[1] for row in manifest_columns}):
        return None
    keys = (*CONCEPT_MASK_REQUIRED_MANIFEST, "concept_mask_rows")
    marks = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT manifest_key, manifest_value FROM serving_snapshot_manifest WHERE manifest_key IN ({marks})", keys,
    ).fetchall()
    manifest = dict(rows)
    if len(rows) != len(keys) or len(manifest) != len(keys):
        return None
    if any(manifest.get(key) != value for key, value in CONCEPT_MASK_REQUIRED_MANIFEST.items()):
        return None
    count = manifest.get("concept_mask_rows", "")
    if (not isinstance(count, str) or len(count) > 19 or not count.isascii()
            or not count.isdecimal() or str(int(count)) != count):
        return None
    columns = conn.execute(f"PRAGMA table_info({CONCEPT_MASK_TABLE})").fetchall()
    if [(row[1], row[2].upper(), row[3], row[5]) for row in columns] != [
        ("concept_id", "INTEGER", 0, 1), ("mask", "INTEGER", 1, 0),
    ]:
        return None
    # An ICU/custom LIKE can match characters outside ASCII case folding.
    # Unknown/older SQLite introspection also falls back to the original SQL.
    functions = conn.execute("PRAGMA function_list").fetchall()
    likes = [row for row in functions if row[0].lower() == "like" and row[4] in (2, -1)]
    if not likes or any(not row[1] for row in likes):
        return None
    return tuple(dict.fromkeys(masks))
