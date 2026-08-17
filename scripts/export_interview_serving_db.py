"""Export the read-only NCS graph used by hosted MCP deployments.

The canonical NCS_MCP database contains recommendation, ontology, training, and
audit tables and is intentionally large.  This utility creates a controlled
derived snapshot for interview/MCP serving use cases.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


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


def _resolve_tables(*, include_training_links: bool, include_ontology: bool, include_task_ontology: bool) -> tuple[str, ...]:
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


def export_serving_db(
    source: Path,
    destination: Path,
    *,
    include_training_links: bool = False,
    include_ontology: bool = False,
    include_task_ontology: bool = False,
    include_indexes: bool = True,
) -> dict[str, object]:
    if source.resolve() == destination.resolve():
        raise ValueError("destination must be different from source")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()

    source_uri = f"file:{source.resolve()}?mode=ro"
    selected_tables = _resolve_tables(
        include_training_links=include_training_links,
        include_ontology=include_ontology,
        include_task_ontology=include_task_ontology,
    )

    with sqlite3.connect(source_uri, uri=True) as src, sqlite3.connect(destination) as dst:
        src.execute("PRAGMA query_only = ON")
        dst.execute("ATTACH DATABASE ? AS source", (str(source.resolve()),))
        dst.execute("PRAGMA journal_mode = OFF")
        dst.execute("PRAGMA synchronous = OFF")
        dst.execute("PRAGMA foreign_keys = OFF")

        for table in selected_tables:
            if not _table_exists(src, table):
                raise RuntimeError(f"source table is missing: {table}")
            dst.execute(
                f"CREATE TABLE {_quote(table)} AS SELECT * FROM source.{_quote(table)}"
            )

        # ncs_mcp.search_ncs uses aliases as a query-expansion table. Keep the
        # table when present; an empty compatible table is enough otherwise.
        if _table_exists(src, "ncs_query_aliases"):
            dst.execute(
                'CREATE TABLE "ncs_query_aliases" AS SELECT * FROM source."ncs_query_aliases"'
            )
        else:
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
        if include_indexes:
            for statement in indexes:
                try:
                    dst.execute(statement)
                except sqlite3.OperationalError:
                    # A malformed/duplicate source index should not prevent the
                    # core serving tables from being exported.
                    continue

            if include_training_links:
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
                for statement in training_indexes:
                    try:
                        dst.execute(statement)
                    except sqlite3.OperationalError:
                        continue

            if include_ontology:
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
                for statement in ontology_indexes:
                    try:
                        dst.execute(statement)
                    except sqlite3.OperationalError:
                        continue

            if include_task_ontology:
                task_indexes = (
                    "CREATE INDEX idx_serving_task_ksa_source ON task_ksa_concept_relations(source_criteria_id)",
                    "CREATE INDEX idx_serving_task_ksa_target ON task_ksa_concept_relations(target_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_source ON task_similarity_links(source_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_target ON task_similarity_links(target_criteria_id)",
                    "CREATE INDEX idx_serving_task_sim_score ON task_similarity_links(similarity_score)",
                )
                for statement in task_indexes:
                    try:
                        dst.execute(statement)
                    except sqlite3.OperationalError:
                        continue

        dst.commit()
        dst.execute("DETACH DATABASE source")

        counts = {
            table: int(
                dst.execute(f"SELECT COUNT(*) FROM {_quote(table)}").fetchone()[0]
            )
            for table in (*selected_tables, "ncs_query_aliases")
        }

    return {
        "source": str(source),
        "destination": str(destination),
        "tables": counts,
        "size_bytes": destination.stat().st_size,
        "purpose": "read-only interview MCP serving DB",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("data/processed/ncs.db"))
    parser.add_argument("--destination", type=Path, default=Path("data/processed/ncs_interview.db"))
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
    report = export_serving_db(
        args.source,
        args.destination,
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
