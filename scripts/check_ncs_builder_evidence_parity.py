"""Read-only gate for a Builder candidate made from a newer local NCS DB.

Check that the candidate keeps current qualification evidence and older Builder
training relationships before packaging it. This does not approve any row or
write to any database.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


TRAINING_PROJECTIONS = {
    "ncs_training_course_unit_links": (
        "training_course_id, unit_code, link_method, confidence_score, review_status"
    ),
    "ncs_training_course_concept_links": (
        "training_course_id, unit_code, concept_id, link_method, "
        "confidence_score, evidence_text, review_status"
    ),
    "ncs_training_course_element_links": (
        "training_course_id, unit_code, element_id, link_method, "
        "confidence_score, evidence_text, review_status"
    ),
    "training_goal_concept_links": (
        "training_course_id, unit_code, element_id, concept_id, link_method, "
        "confidence_score, evidence_text, review_status"
    ),
    "training_delivery_relations": (
        "training_course_id, relation_type, relation_value, normalized_value, "
        "numeric_value, evidence_text, confidence_score, review_status"
    ),
}
QUALIFICATION_TABLES = (
    "ncs_qualification_items",
    "ncs_qualification_collection_status",
    "ncs_unit_qualification_links",
)


def _attach_read_only(conn: sqlite3.Connection, alias: str, path: Path) -> None:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Not a SQLite database: {resolved}")
    conn.execute(
        f"ATTACH DATABASE ? AS {alias}",
        (f"file:{resolved.as_posix()}?mode=ro",),
    )


def _missing_count(
    conn: sqlite3.Connection, table: str, columns: str, source: str
) -> int:
    return int(conn.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT {columns} FROM {source}.{table} "
        f"EXCEPT SELECT {columns} FROM candidate.{table})"
    ).fetchone()[0])


def _multiplicity_mismatch_count(
    conn: sqlite3.Connection, table: str, columns: str, source: str
) -> int:
    """Catch a missing duplicate even when the distinct projection survives."""
    return int(conn.execute(
        f"SELECT COUNT(*) FROM ("
        f"SELECT {columns}, COUNT(*) AS projection_multiplicity "
        f"FROM {source}.{table} GROUP BY {columns} "
        f"EXCEPT "
        f"SELECT {columns}, COUNT(*) AS projection_multiplicity "
        f"FROM candidate.{table} GROUP BY {columns})"
    ).fetchone()[0])


def check(
    current_db: Path, older_builder_db: Path, candidate_db: Path
) -> dict:
    paths = {
        "current": current_db.resolve(strict=True),
        "older_builder": older_builder_db.resolve(strict=True),
        "candidate": candidate_db.resolve(strict=True),
    }
    if len(set(paths.values())) != 3:
        raise ValueError("All three database paths must differ")
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("PRAGMA query_only=ON")
        for alias, path in paths.items():
            _attach_read_only(conn, alias, path)
        qualification = {}
        for table in QUALIFICATION_TABLES:
            qualification[table] = {
                "current_rows": int(conn.execute(
                    f"SELECT COUNT(*) FROM current.{table}"
                ).fetchone()[0]),
                "candidate_rows": int(conn.execute(
                    f"SELECT COUNT(*) FROM candidate.{table}"
                ).fetchone()[0]),
                "current_rows_missing_or_changed": _missing_count(
                    conn, table, "*", "current"
                ),
            }
        training = {}
        for table, columns in TRAINING_PROJECTIONS.items():
            training[table] = {
                "older_builder_rows": int(conn.execute(
                    f"SELECT COUNT(*) FROM older_builder.{table}"
                ).fetchone()[0]),
                "candidate_rows": int(conn.execute(
                    f"SELECT COUNT(*) FROM candidate.{table}"
                ).fetchone()[0]),
                "older_builder_rows_missing_or_changed": _missing_count(
                    conn, table, columns, "older_builder"
                ),
                "older_builder_projection_groups_with_changed_multiplicity": (
                    _multiplicity_mismatch_count(
                        conn, table, columns, "older_builder"
                    )
                ),
            }
    candidate_report = candidate_db.parent / "build.json"
    build = json.loads(candidate_report.read_text(encoding="utf-8"))
    ready = build.get("status") == "ready" and build.get("human_approval_claim") is False
    qualification_ok = all(
        value["current_rows_missing_or_changed"] == 0
        and value["candidate_rows"] >= value["current_rows"]
        for value in qualification.values()
    )
    training_ok = all(
        value["older_builder_rows_missing_or_changed"] == 0
        and value["older_builder_projection_groups_with_changed_multiplicity"] == 0
        and value["candidate_rows"] >= value["older_builder_rows"]
        for value in training.values()
    )
    return {
        "schema": "ncs_builder_evidence_parity_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "paths": {key: str(path) for key, path in paths.items()},
        "builder_version": build.get("version"),
        "builder_ready": ready,
        "qualification": qualification,
        "training": training,
        "qualification_ok": qualification_ok,
        "training_ok": training_ok,
        "ok": ready and qualification_ok and training_ok,
        "read_only": True,
        "db_writes": False,
        "human_approval_claim": False,
        "deployment_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-db", type=Path, required=True)
    parser.add_argument("--older-builder-db", type=Path, required=True)
    parser.add_argument("--candidate-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = check(args.current_db, args.older_builder_db, args.candidate_db)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({
        "out": str(args.out), "ok": report["ok"],
        "qualification_ok": report["qualification_ok"],
        "training_ok": report["training_ok"],
    }, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
