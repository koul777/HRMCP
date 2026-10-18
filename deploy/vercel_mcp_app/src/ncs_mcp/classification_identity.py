"""Canonical NCS competency-unit to classification identity helpers.

The Excel source can repeat one competency unit under a borrowed or stale
classification.  A unit's immutable eight-digit base-code prefix is the
identity key; ``competency_units.classification_id`` is only cached linkage.
Serving requires one active path once API activity metadata is available;
a fresh Excel database may use one structurally exact path until then.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OFFICIAL_PREFIX_GLOB = "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]"


def _identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return value


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    table = _identifier(table)
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _classification_activity_known(conn: sqlite3.Connection) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM classifications "
        "WHERE TRIM(COALESCE(api_usg_yn, '')) != '' LIMIT 1"
    ).fetchone())


def _stored_classification_join(unit_alias: str, classification_alias: str) -> str:
    return (
        f"JOIN classifications {classification_alias} ON "
        f"{classification_alias}.classification_id = "
        f"{unit_alias}.classification_id"
    )


def unit_classification_join(
    conn: sqlite3.Connection,
    *,
    unit_alias: str = "cu",
    classification_alias: str = "c",
) -> str:
    """Return the fail-closed serving join for a competency unit.

    Current production schemas resolve official numeric NCS codes by their
    base-code prefix and require one active classification once any API
    activity metadata exists. Fresh Excel databases use one structurally
    exact path until then. The stored foreign key is deliberately ignored.
    Minimal legacy/test schemas that do
    not yet have ``api_usg_yn`` keep their historical join until migrated.
    Non-official synthetic unit identifiers also retain the stored join so
    isolated contract fixtures remain useful without pretending to be NCS
    identities.
    """

    unit_alias = _identifier(unit_alias)
    classification_alias = _identifier(classification_alias)
    if "api_usg_yn" not in _table_columns(conn, "classifications"):
        return _stored_classification_join(unit_alias, classification_alias)
    activity_filter = (
        "WHERE UPPER(TRIM(COALESCE(api_usg_yn, ''))) = 'Y' "
        if _classification_activity_known(conn) else ""
    )
    unit_columns = _table_columns(conn, "competency_units")
    code_column = "base_unit_code" if "base_unit_code" in unit_columns else "unit_code"
    unit_code = f"{unit_alias}.{code_column}"
    identity_alias = "ncs_unit_identity"
    official = (
        f"LENGTH({unit_code}) >= 8 AND "
        f"SUBSTR({unit_code}, 1, 8) GLOB '{_OFFICIAL_PREFIX_GLOB}'"
    )
    return (
        "LEFT JOIN ("
        "SELECT MIN(classification_id) AS classification_id, "
        "major_code, middle_code, small_code, sub_code "
        "FROM classifications "
        f"{activity_filter}"
        "GROUP BY major_code, middle_code, small_code, sub_code "
        "HAVING COUNT(*) = 1"
        f") {identity_alias} ON "
        f"{identity_alias}.major_code = SUBSTR({unit_code}, 1, 2) AND "
        f"{identity_alias}.middle_code = SUBSTR({unit_code}, 3, 2) AND "
        f"{identity_alias}.small_code = SUBSTR({unit_code}, 5, 2) AND "
        f"{identity_alias}.sub_code = SUBSTR({unit_code}, 7, 2) "
        f"JOIN classifications {classification_alias} ON "
        f"{classification_alias}.classification_id = CASE WHEN {official} "
        f"THEN {identity_alias}.classification_id ELSE {unit_alias}.classification_id END"
    )


def resolve_import_classification_id(
    conn: sqlite3.Connection,
    base_unit_code: str,
) -> int | None:
    """Resolve one structurally exact path while normalizing raw Excel rows.

    A fresh Excel build has no API activity metadata yet, so structural
    uniqueness is used until any activity metadata exists.  Once metadata is
    present (for example an incremental Builder candidate), only one active
    exact path is accepted.  Zero or multiple matches return ``None``.
    """

    prefix = str(base_unit_code or "")[:8]
    if len(prefix) != 8 or not prefix.isdigit():
        return None
    activity_known = _classification_activity_known(conn)
    clauses = [
        "c.major_code = ?",
        "c.middle_code = ?",
        "c.small_code = ?",
        "c.sub_code = ?",
    ]
    if activity_known:
        clauses.append("UPPER(TRIM(COALESCE(c.api_usg_yn, ''))) = 'Y'")
    rows = conn.execute(
        "SELECT c.classification_id FROM classifications c WHERE "
        + " AND ".join(clauses),
        (prefix[:2], prefix[2:4], prefix[4:6], prefix[6:8]),
    ).fetchall()
    if len(rows) != 1:
        return None
    return int(rows[0][0])


def reconcile_unit_classification_ids(
    conn: sqlite3.Connection,
    *,
    apply: bool = False,
) -> dict[str, Any]:
    """Plan or apply safe cached-FK repairs on a writable candidate DB."""

    unit_columns = _table_columns(conn, "competency_units")
    code_column = "base_unit_code" if "base_unit_code" in unit_columns else "unit_code"
    rows = conn.execute(
        f"""
        SELECT cu.unit_code, cu.classification_id AS current_classification_id,
               SUBSTR(cu.{code_column}, 1, 8) AS expected_path,
               COUNT(c.classification_id) AS active_match_count,
               MIN(c.classification_id) AS resolved_classification_id
        FROM competency_units cu
        LEFT JOIN classifications c
          ON UPPER(TRIM(COALESCE(c.api_usg_yn, ''))) = 'Y'
         AND c.major_code = SUBSTR(cu.{code_column}, 1, 2)
         AND c.middle_code = SUBSTR(cu.{code_column}, 3, 2)
         AND c.small_code = SUBSTR(cu.{code_column}, 5, 2)
         AND c.sub_code = SUBSTR(cu.{code_column}, 7, 2)
        WHERE LENGTH(cu.{code_column}) >= 8
          AND SUBSTR(cu.{code_column}, 1, 8) GLOB '{_OFFICIAL_PREFIX_GLOB}'
        GROUP BY cu.unit_code, cu.classification_id, SUBSTR(cu.{code_column}, 1, 8)
        ORDER BY cu.unit_code
        """
    ).fetchall()
    repairs: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    already_canonical = 0
    for row in rows:
        item = {
            "unit_code": row[0],
            "current_classification_id": int(row[1]),
            "expected_path": row[2],
            "active_match_count": int(row[3]),
            "resolved_classification_id": (
                int(row[4]) if row[4] is not None else None
            ),
        }
        if item["active_match_count"] == 0:
            missing.append(item)
        elif item["active_match_count"] != 1:
            ambiguous.append(item)
        elif item["current_classification_id"] == item["resolved_classification_id"]:
            already_canonical += 1
        else:
            repairs.append(item)

    if apply and repairs:
        conn.executemany(
            "UPDATE competency_units SET classification_id = ? WHERE unit_code = ?",
            (
                (item["resolved_classification_id"], item["unit_code"])
                for item in repairs
            ),
        )

    return {
        "schema": "ncs_unit_classification_reconciliation_v1",
        "apply": apply,
        "official_units_checked": len(rows),
        "already_canonical": already_canonical,
        "repair_count": len(repairs),
        "missing_active_path_count": len(missing),
        "ambiguous_active_path_count": len(ambiguous),
        "repairs": repairs,
        "missing_active_paths": missing,
        "ambiguous_active_paths": ambiguous,
    }
