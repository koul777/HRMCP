"""Verify the read-only Vercel ontology-complete SQLite snapshot.

The verifier is intentionally independent from database initialization code.
It opens the snapshot with SQLite ``mode=ro&immutable=1``, checks the serving
manifest and evidence counts, and never creates or migrates database objects.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import quote


SCHEMA = "ncs_vercel_complete_snapshot_verification_v1"
DEFAULT_DB_PATH = Path(
    "deploy/vercel_mcp_app/api/ncs_interview_serving_complete.db"
)
SQLITE_HEADER = b"SQLite format 3\x00"

DEFAULT_EXACT_TABLE_COUNTS: dict[str, int] = {
    "ksa_items": 574_279,
    "ksa_atomic_items": 644_384,
    "ontology_concepts": 533_909,
    "ksa_concept_links": 574_279,
    "ksa_atomic_concept_links": 644_384,
    "criteria_concept_links": 2_455_465,
    "element_criteria_ksa_links": 2_458_668,
    "ontology_concept_relations": 3_235_434,
    "task_similarity_links": 1_521_339,
    "training_transition_gold_scenarios": 100,
    "training_transition_scenario_reviews": 11,
    "task_ksa_relations_compact": 14_475_815,
    "learning_module_concept_links": 0,
}

DEFAULT_MANIFEST_VALUES: dict[str, str] = {
    "profile": "vercel-ontology-complete",
    "schema": "ncs_vercel_ontology_complete_v1",
    "task_ksa_storage": "task_ksa_relations_compact",
    "task_ksa_compatibility_view": "task_ksa_concept_relations",
    "label_candidate_scope": "all_statuses",
    "source_access": "read_only_immutable",
}

DEFAULT_GOLD_STATUS_COUNTS: dict[str, int] = {
    "candidate": 20,
    "candidate_auto": 69,
    "reviewed": 11,
}

FORBIDDEN_OBJECT_NAMES = frozenset(
    {
        "review_audit_log",
        "ncs_study_modules",
    }
)
ALLOWED_EMPTY_COMPATIBILITY_OBJECTS = frozenset(
    {"learning_module_concept_links"}
)
FORBIDDEN_OBJECT_PREFIXES = (
    "education_recommendation_",
    "learning_module_",
    "ncs_study_module",
    "sqf_",
)

TASK_VIEW_COLUMNS = (
    "relation_id",
    "criteria_id",
    "element_id",
    "source_concept_id",
    "relation_type",
    "target_concept_id",
    "source_atomic_id",
    "target_atomic_id",
    "evidence_text",
    "confidence_score",
    "review_status",
    "created_at",
)


@dataclass(frozen=True)
class SnapshotExpectations:
    """Acceptance thresholds for one reproducible serving snapshot."""

    exact_table_counts: Mapping[str, int]
    manifest_values: Mapping[str, str]
    gold_status_counts: Mapping[str, int]
    min_human_reviewed_labels: int = 755
    # 755 reviewed candidate rows collapse to 742 distinct concept/label keys.
    # Of those, 480 require a new alias row; the remainder already match an
    # existing concept name or alias. Coverage is checked separately below.
    min_merged_reviewed_aliases: int = 480
    forbidden_object_names: frozenset[str] = FORBIDDEN_OBJECT_NAMES
    forbidden_object_prefixes: tuple[str, ...] = FORBIDDEN_OBJECT_PREFIXES
    allowed_empty_compatibility_objects: frozenset[str] = (
        ALLOWED_EMPTY_COMPATIBILITY_OBJECTS
    )


DEFAULT_EXPECTATIONS = SnapshotExpectations(
    exact_table_counts=DEFAULT_EXACT_TABLE_COUNTS,
    manifest_values=DEFAULT_MANIFEST_VALUES,
    gold_status_counts=DEFAULT_GOLD_STATUS_COUNTS,
)


def _sqlite_uri(path: Path) -> str:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    return f"file:{encoded}?mode=ro&immutable=1"


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _add_check(
    checks: list[dict[str, Any]],
    check_id: str,
    passed: bool | None,
    *,
    actual: Any = None,
    expected: Any = None,
    detail: str | None = None,
) -> None:
    status = "skipped" if passed is None else ("pass" if passed else "fail")
    item: dict[str, Any] = {"id": check_id, "status": status}
    if actual is not None:
        item["actual"] = actual
    if expected is not None:
        item["expected"] = expected
    if detail:
        item["detail"] = detail
    checks.append(item)


def _finalize(report: dict[str, Any]) -> dict[str, Any]:
    checks = report["checks"]
    passed = sum(check["status"] == "pass" for check in checks)
    failed = sum(check["status"] == "fail" for check in checks)
    skipped = sum(check["status"] == "skipped" for check in checks)
    report["summary"] = {
        "check_count": len(checks),
        "passed_count": passed,
        "failed_count": failed,
        "skipped_count": skipped,
    }
    report["ok"] = failed == 0
    return report


def _object_catalog(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        str(row[0]): str(row[1])
        for row in conn.execute(
            """
            SELECT name, type
            FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0])


def _group_counts(
    conn: sqlite3.Connection,
    table: str,
    column: str,
) -> dict[str, int]:
    return {
        str(row[0]): int(row[1])
        for row in conn.execute(
            f"""
            SELECT {_quote(column)}, COUNT(*)
            FROM {_quote(table)}
            GROUP BY {_quote(column)}
            ORDER BY {_quote(column)}
            """
        ).fetchall()
    }


def _relation_projection_metrics(
    conn: sqlite3.Connection,
    object_name: str,
) -> dict[str, int | None]:
    row = conn.execute(
        f"""
        SELECT
            COUNT(*),
            MIN(relation_id),
            MAX(relation_id),
            SUM(relation_id),
            SUM(criteria_id),
            SUM(source_atomic_id),
            SUM(target_atomic_id),
            SUM(CAST(ROUND(confidence_score * 1000000.0) AS INTEGER))
        FROM {_quote(object_name)}
        """
    ).fetchone()
    keys = (
        "row_count",
        "min_relation_id",
        "max_relation_id",
        "relation_id_sum",
        "criteria_id_sum",
        "source_atomic_id_sum",
        "target_atomic_id_sum",
        "confidence_millionths_sum",
    )
    return {key: (None if value is None else int(value)) for key, value in zip(keys, row)}


def _relation_distribution(
    conn: sqlite3.Connection,
    *,
    compatibility_view: bool,
) -> list[dict[str, Any]]:
    if compatibility_view:
        rows = conn.execute(
            """
            SELECT relation_type, review_status, COUNT(*)
            FROM task_ksa_concept_relations
            GROUP BY relation_type, review_status
            ORDER BY relation_type, review_status
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT relation_type.relation_type,
                   review_status.review_status,
                   COUNT(*)
            FROM task_ksa_relations_compact AS relation
            JOIN task_ksa_relation_types AS relation_type
              ON relation_type.relation_type_code = relation.relation_type_code
            JOIN task_ksa_review_statuses AS review_status
              ON review_status.review_status_code = relation.review_status_code
            GROUP BY relation_type.relation_type, review_status.review_status
            ORDER BY relation_type.relation_type, review_status.review_status
            """
        ).fetchall()
    return [
        {
            "relation_type": str(row[0]),
            "review_status": str(row[1]),
            "row_count": int(row[2]),
        }
        for row in rows
    ]


def _verify_connected_snapshot(
    conn: sqlite3.Connection,
    report: dict[str, Any],
    *,
    expectations: SnapshotExpectations,
    run_quick_check: bool,
) -> None:
    checks: list[dict[str, Any]] = report["checks"]
    metrics: dict[str, Any] = report["metrics"]
    conn.execute("PRAGMA query_only = ON")

    if run_quick_check:
        quick_check_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
        metrics["quick_check"] = quick_check_rows
        _add_check(
            checks,
            "sqlite_quick_check",
            quick_check_rows == ["ok"],
            actual=quick_check_rows,
            expected=["ok"],
        )
    else:
        metrics["quick_check"] = None
        _add_check(
            checks,
            "sqlite_quick_check",
            None,
            detail="skipped by --skip-quick-check",
        )

    objects = _object_catalog(conn)
    metrics["object_count"] = len(objects)

    manifest: dict[str, str] = {}
    if objects.get("serving_snapshot_manifest") == "table":
        manifest = {
            str(row[0]): str(row[1])
            for row in conn.execute(
                """
                SELECT manifest_key, manifest_value
                FROM serving_snapshot_manifest
                ORDER BY manifest_key
                """
            ).fetchall()
        }
    metrics["manifest"] = manifest
    for key, expected_value in expectations.manifest_values.items():
        _add_check(
            checks,
            f"manifest_{key}",
            manifest.get(key) == expected_value,
            actual=manifest.get(key),
            expected=expected_value,
        )

    table_counts: dict[str, int | None] = {}
    for table, expected_count in expectations.exact_table_counts.items():
        actual_count: int | None = None
        if objects.get(table) == "table":
            actual_count = _table_count(conn, table)
        table_counts[table] = actual_count
        _add_check(
            checks,
            f"exact_count_{table}",
            actual_count == expected_count,
            actual=actual_count,
            expected=expected_count,
        )
    metrics["table_counts"] = table_counts

    forbidden = sorted(
        name
        for name in objects
        if name not in expectations.allowed_empty_compatibility_objects
        and (
            name in expectations.forbidden_object_names
            or any(name.startswith(prefix) for prefix in expectations.forbidden_object_prefixes)
        )
    )
    metrics["forbidden_objects"] = forbidden
    _add_check(
        checks,
        "no_default_seed_or_legacy_contamination",
        not forbidden,
        actual=forbidden,
        expected=[],
    )

    triggers = sorted(
        name
        for name, object_type in objects.items()
        if object_type == "trigger"
    )
    metrics["triggers"] = triggers
    _add_check(
        checks,
        "no_write_triggers",
        not triggers,
        actual=triggers,
        expected=[],
    )

    if objects.get("training_transition_gold_scenarios") == "table":
        gold_status_counts = _group_counts(
            conn,
            "training_transition_gold_scenarios",
            "review_status",
        )
    else:
        gold_status_counts = {}
    metrics["gold_status_counts"] = gold_status_counts
    _add_check(
        checks,
        "gold_status_distribution",
        gold_status_counts == dict(expectations.gold_status_counts),
        actual=gold_status_counts,
        expected=dict(expectations.gold_status_counts),
        detail="prevents initialization-time default scenario seeding or status drift",
    )

    label_metrics: dict[str, Any] = {
        "human_reviewed_count": None,
        "human_reviewed_distinct_count": None,
        "merged_alias_source_count": None,
        "missing_alias_count": None,
    }
    label_tables_present = (
        objects.get("ontology_concept_label_candidates") == "table"
        and objects.get("ontology_concept_aliases") == "table"
    )
    if label_tables_present:
        reviewed_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concept_label_candidates
                WHERE review_status = 'human_reviewed'
                """
            ).fetchone()[0]
        )
        distinct_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT DISTINCT
                        concept_id,
                        LOWER(TRIM(COALESCE(normalized_label_key, ''))) AS label_key,
                        LOWER(TRIM(COALESCE(label_text, ''))) AS label_text
                    FROM ontology_concept_label_candidates
                    WHERE review_status = 'human_reviewed'
                )
                """
            ).fetchone()[0]
        )
        merged_alias_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM ontology_concept_aliases
                WHERE alias_source = 'ontology_label_human_reviewed'
                """
            ).fetchone()[0]
        )
        missing_alias_count = int(
            conn.execute(
                """
                WITH reviewed AS (
                    SELECT DISTINCT
                        concept_id,
                        LOWER(TRIM(COALESCE(normalized_label_key, ''))) AS label_key,
                        LOWER(TRIM(COALESCE(label_text, ''))) AS label_text
                    FROM ontology_concept_label_candidates
                    WHERE review_status = 'human_reviewed'
                )
                SELECT COUNT(*)
                FROM reviewed
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM ontology_concept_aliases AS alias
                    WHERE alias.concept_id = reviewed.concept_id
                      AND (
                          LOWER(TRIM(COALESCE(alias.normalized_alias_key, '')))
                              = reviewed.label_key
                          OR LOWER(TRIM(COALESCE(alias.alias_text, '')))
                              = reviewed.label_text
                      )
                )
                """
            ).fetchone()[0]
        )
        label_metrics.update(
            {
                "human_reviewed_count": reviewed_count,
                "human_reviewed_distinct_count": distinct_count,
                "merged_alias_source_count": merged_alias_count,
                "missing_alias_count": missing_alias_count,
            }
        )
    metrics["human_reviewed_label_aliases"] = label_metrics
    _add_check(
        checks,
        "human_reviewed_label_minimum",
        label_metrics["human_reviewed_count"] is not None
        and label_metrics["human_reviewed_count"]
        >= expectations.min_human_reviewed_labels,
        actual=label_metrics["human_reviewed_count"],
        expected={"minimum": expectations.min_human_reviewed_labels},
    )
    _add_check(
        checks,
        "human_reviewed_alias_merge_minimum",
        label_metrics["merged_alias_source_count"] is not None
        and label_metrics["merged_alias_source_count"]
        >= expectations.min_merged_reviewed_aliases,
        actual=label_metrics["merged_alias_source_count"],
        expected={"minimum": expectations.min_merged_reviewed_aliases},
    )
    _add_check(
        checks,
        "human_reviewed_label_alias_coverage",
        label_metrics["missing_alias_count"] == 0,
        actual=label_metrics["missing_alias_count"],
        expected=0,
    )

    compact_metrics: dict[str, Any] = {
        "compact_object_type": objects.get("task_ksa_relations_compact"),
        "compatibility_object_type": objects.get("task_ksa_concept_relations"),
    }
    task_objects_present = (
        objects.get("task_ksa_relations_compact") == "table"
        and objects.get("task_ksa_concept_relations") == "view"
        and objects.get("task_ksa_relation_types") == "table"
        and objects.get("task_ksa_review_statuses") == "table"
    )
    _add_check(
        checks,
        "compact_relation_objects",
        task_objects_present,
        actual=compact_metrics,
        expected={
            "compact_object_type": "table",
            "compatibility_object_type": "view",
        },
    )

    view_columns: list[str] = []
    if objects.get("task_ksa_concept_relations") == "view":
        view_columns = [
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info('task_ksa_concept_relations')"
            ).fetchall()
        ]
    compact_metrics["compatibility_view_columns"] = view_columns
    _add_check(
        checks,
        "compact_compatibility_view_contract",
        tuple(view_columns) == TASK_VIEW_COLUMNS,
        actual=view_columns,
        expected=list(TASK_VIEW_COLUMNS),
    )

    if task_objects_present:
        compact_projection = _relation_projection_metrics(
            conn,
            "task_ksa_relations_compact",
        )
        view_projection = _relation_projection_metrics(
            conn,
            "task_ksa_concept_relations",
        )
        compact_distribution = _relation_distribution(
            conn,
            compatibility_view=False,
        )
        view_distribution = _relation_distribution(
            conn,
            compatibility_view=True,
        )
        orphan_dictionary_count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM task_ksa_relations_compact AS relation
                LEFT JOIN task_ksa_relation_types AS relation_type
                  ON relation_type.relation_type_code = relation.relation_type_code
                LEFT JOIN task_ksa_review_statuses AS review_status
                  ON review_status.review_status_code = relation.review_status_code
                WHERE relation_type.relation_type_code IS NULL
                   OR review_status.review_status_code IS NULL
                """
            ).fetchone()[0]
        )
    else:
        compact_projection = None
        view_projection = None
        compact_distribution = None
        view_distribution = None
        orphan_dictionary_count = None
    compact_metrics.update(
        {
            "compact_projection": compact_projection,
            "compatibility_view_projection": view_projection,
            "compact_distribution": compact_distribution,
            "compatibility_view_distribution": view_distribution,
            "orphan_dictionary_code_count": orphan_dictionary_count,
        }
    )
    metrics["compact_relation_parity"] = compact_metrics
    _add_check(
        checks,
        "compact_relation_projection_parity",
        compact_projection is not None and compact_projection == view_projection,
        actual={"compact": compact_projection, "view": view_projection},
        expected="identical aggregate projection",
    )
    _add_check(
        checks,
        "compact_relation_distribution_parity",
        compact_distribution is not None
        and compact_distribution == view_distribution,
        actual={"compact": compact_distribution, "view": view_distribution},
        expected="identical relation-type and review-status distribution",
    )
    _add_check(
        checks,
        "compact_relation_dictionary_integrity",
        orphan_dictionary_count == 0,
        actual=orphan_dictionary_count,
        expected=0,
    )


def verify_snapshot(
    path: Path,
    *,
    run_quick_check: bool = True,
    expectations: SnapshotExpectations = DEFAULT_EXPECTATIONS,
) -> dict[str, Any]:
    """Return a machine-readable acceptance report without mutating ``path``."""
    path = Path(path)
    report: dict[str, Any] = {
        "schema": SCHEMA,
        "path": str(path),
        "access": {
            "sqlite_mode": "ro",
            "immutable": True,
            "query_only": True,
        },
        "checks": [],
        "metrics": {},
        "ok": False,
    }
    checks: list[dict[str, Any]] = report["checks"]

    exists = path.is_file()
    _add_check(checks, "file_exists", exists, actual=exists, expected=True)
    if not exists:
        return _finalize(report)

    size_bytes = path.stat().st_size
    report["metrics"]["size_bytes"] = size_bytes
    try:
        with path.open("rb") as stream:
            header = stream.read(len(SQLITE_HEADER))
    except OSError as exc:
        _add_check(
            checks,
            "sqlite_header",
            False,
            detail=f"unable to read file header: {exc}",
        )
        return _finalize(report)

    valid_header = header == SQLITE_HEADER
    _add_check(
        checks,
        "sqlite_header",
        valid_header,
        actual=header.hex(),
        expected=SQLITE_HEADER.hex(),
    )
    if not valid_header:
        return _finalize(report)

    try:
        with closing(sqlite3.connect(_sqlite_uri(path), uri=True)) as conn:
            _verify_connected_snapshot(
                conn,
                report,
                expectations=expectations,
                run_quick_check=run_quick_check,
            )
    except sqlite3.Error as exc:
        _add_check(
            checks,
            "sqlite_read_only_open",
            False,
            detail=f"SQLite verification failed: {exc}",
        )
    else:
        _add_check(
            checks,
            "sqlite_read_only_open",
            True,
            actual="mode=ro&immutable=1; query_only=ON",
            expected="read-only immutable connection",
        )
    return _finalize(report)


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Snapshot path (default: {DEFAULT_DB_PATH.as_posix()})",
    )
    parser.add_argument(
        "--skip-quick-check",
        action="store_true",
        help="Skip PRAGMA quick_check for a faster count/contract-only run.",
    )
    parser.add_argument(
        "--out",
        "--report",
        dest="report_path",
        type=Path,
        help="Optionally write the same JSON report to this path.",
    )
    args = parser.parse_args(argv)

    report = verify_snapshot(
        args.db,
        run_quick_check=not args.skip_quick_check,
        expectations=DEFAULT_EXPECTATIONS,
    )
    if args.report_path:
        try:
            _write_report(args.report_path, report)
        except OSError as exc:
            _add_check(
                report["checks"],
                "report_write",
                False,
                detail=f"unable to write report: {exc}",
            )
            _finalize(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
