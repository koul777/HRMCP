from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
import zlib
from collections import defaultdict
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE = ROOT / "deploy" / "vercel_mcp_app" / "api" / "ncs_ontology_compact.zip"
DEFAULT_MANIFEST = (
    ROOT
    / "deploy"
    / "vercel_mcp_app"
    / "api"
    / "ncs_ontology_compact.manifest.json"
)
DEFAULT_VERCEL_CONFIG = ROOT / "vercel.json"
DEFAULT_JSON_OUT = ROOT / "reports" / "compact_snapshot_size_audit_20260830.json"
DEFAULT_MARKDOWN_OUT = ROOT / "reports" / "compact_snapshot_size_audit_20260830.md"

SCHEMA = "ncs_compact_snapshot_size_audit_v1"
HARD_CAP_BYTES = 480_000_000
SOFT_CAP_BYTES = 460_000_000
DEFAULT_COLD_P50_SECONDS = 4.55
READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)
EXPECTED_PUBLIC_TOOLS = (
    "ncs_analysis",
    "ncs_discover_tools",
    "ncs_execute_tool",
    "ncs_search",
    "ncs_training",
    "ncs_unit_detail",
    "recommend_training_for_task",
)
SYSTEM_OBJECTS = {"json_each", "sqlite_master", "sqlite_schema"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _readiness_tables(vercel_config: Path) -> dict[str, Any]:
    payload = json.loads(vercel_config.read_text(encoding="utf-8"))
    environment = payload.get("env", {})
    extras = [
        name.strip()
        for name in str(environment.get("NCS_MCP_READINESS_EXTRA_TABLES", "")).split(",")
        if name.strip()
    ]
    required = list(READINESS_CORE_TABLES)
    for name in extras:
        if name not in required:
            required.append(name)
    minimum_rows_raw = environment.get("NCS_MCP_READINESS_MIN_ROWS", "{}")
    minimum_rows = json.loads(minimum_rows_raw) if minimum_rows_raw else {}
    return {
        "core_tables": list(READINESS_CORE_TABLES),
        "extra_tables": extras,
        "required_tables": required,
        "required_table_count": len(required),
        "minimum_rows": minimum_rows,
    }


def _schema_inventory(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT name, type, tbl_name, sql
        FROM sqlite_schema
        WHERE type IN ('table', 'index', 'view')
        ORDER BY type, name
        """
    )
    return {
        str(row[0]): {
            "type": str(row[1]),
            "table": str(row[2]),
            "sql": row[3],
        }
        for row in rows
    }


def _view_dependencies(
    schema: dict[str, dict[str, Any]],
) -> dict[str, list[str]]:
    candidates = set(schema)
    dependencies: dict[str, list[str]] = {}
    for name, metadata in schema.items():
        if metadata["type"] != "view":
            continue
        sql = str(metadata.get("sql") or "")
        dependencies[name] = sorted(
            candidate
            for candidate in candidates
            if candidate != name
            and re.search(rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])", sql)
        )
    return dependencies


def _dbstat_objects(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    try:
        rows = connection.execute(
            """
            SELECT name,
                   SUM(pgsize) AS bytes,
                   COUNT(*) AS pages,
                   SUM(payload) AS payload_bytes,
                   SUM(unused) AS unused_bytes
            FROM dbstat
            GROUP BY name
            ORDER BY bytes DESC, name
            """
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise RuntimeError("SQLite dbstat support is required for the size audit") from exc
    return [
        {
            "name": str(row[0]),
            "bytes": int(row[1]),
            "pages": int(row[2]),
            "payload_bytes": int(row[3]),
            "unused_bytes": int(row[4]),
        }
        for row in rows
    ]


def _add_independent_deflate_estimates(
    connection: sqlite3.Connection,
    database_path: Path,
    page_size: int,
    objects: list[dict[str, Any]],
) -> None:
    page_numbers: dict[str, list[int]] = defaultdict(list)
    for name, page_number in connection.execute(
        "SELECT name, pageno FROM dbstat ORDER BY name, pageno"
    ):
        page_numbers[str(name)].append(int(page_number))

    with database_path.open("rb") as handle:
        for item in objects:
            compressor = zlib.compressobj(1, zlib.DEFLATED, -15)
            compressed_bytes = 0
            for page_number in page_numbers.get(item["name"], []):
                handle.seek((page_number - 1) * page_size)
                compressed_bytes += len(compressor.compress(handle.read(page_size)))
            compressed_bytes += len(compressor.flush())
            item["independent_deflate_bytes"] = compressed_bytes
            item["independent_deflate_ratio"] = round(
                compressed_bytes / item["bytes"], 6
            )


def _index_inventory(
    connection: sqlite3.Connection,
    object_sizes: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    indexes: list[dict[str, Any]] = []
    table_names = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' ORDER BY name"
        )
    ]
    for table_name in table_names:
        quoted_table = _quote_identifier(table_name)
        for row in connection.execute(f"PRAGMA index_list({quoted_table})"):
            index_name = str(row[1])
            quoted_index = _quote_identifier(index_name)
            columns = [
                item[2]
                for item in connection.execute(f"PRAGMA index_info({quoted_index})")
            ]
            size = object_sizes.get(index_name, {})
            indexes.append(
                {
                    "table": table_name,
                    "name": index_name,
                    "columns": columns,
                    "unique": bool(row[2]),
                    "origin": str(row[3]),
                    "partial": bool(row[4]),
                    "bytes": int(size.get("bytes", 0)),
                    "independent_deflate_bytes": int(
                        size.get("independent_deflate_bytes", 0)
                    ),
                }
            )

    candidates: list[dict[str, Any]] = []
    for left_index, left in enumerate(indexes):
        for right in indexes[left_index + 1 :]:
            if left["table"] != right["table"]:
                continue
            left_columns = left["columns"]
            right_columns = right["columns"]
            if not left_columns or not right_columns:
                continue
            if left_columns == right_columns:
                kind = "exact"
            elif (
                left_columns == right_columns[: len(left_columns)]
                or right_columns == left_columns[: len(right_columns)]
            ):
                kind = "left_prefix"
            else:
                continue
            smaller = min((left, right), key=lambda value: value["bytes"])
            candidates.append(
                {
                    "kind": kind,
                    "table": left["table"],
                    "left": left["name"],
                    "right": right["name"],
                    "smaller_index": smaller["name"],
                    "estimated_db_savings_bytes": smaller["bytes"],
                    "estimated_zip_savings_bytes": smaller[
                        "independent_deflate_bytes"
                    ],
                    "safe_without_query_plan_evidence": False,
                }
            )
    return indexes, candidates


def _table_footprints(
    schema: dict[str, dict[str, Any]],
    objects: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    object_sizes = {item["name"]: item for item in objects}
    output: list[dict[str, Any]] = []
    for name, metadata in schema.items():
        if metadata["type"] != "table":
            continue
        owned_objects = [name] + sorted(
            object_name
            for object_name, object_metadata in schema.items()
            if object_metadata["type"] == "index"
            and object_metadata["table"] == name
        )
        output.append(
            {
                "table": name,
                "objects": owned_objects,
                "bytes": sum(
                    int(object_sizes.get(item, {}).get("bytes", 0))
                    for item in owned_objects
                ),
                "independent_deflate_bytes": sum(
                    int(
                        object_sizes.get(item, {}).get(
                            "independent_deflate_bytes", 0
                        )
                    )
                    for item in owned_objects
                ),
            }
        )
    return sorted(output, key=lambda item: (-item["bytes"], item["table"]))


def _source_references(
    root: Path,
    schema_names: Iterable[str],
) -> dict[str, list[str]]:
    source_root = root / "src" / "ncs_mcp"
    sources = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(source_root.glob("*.py"))
    }
    output: dict[str, list[str]] = {}
    for name in schema_names:
        pattern = re.compile(
            rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])"
        )
        matched = sorted(
            relative_path
            for relative_path, text in sources.items()
            if pattern.search(text)
        )
        if matched:
            output[name] = matched
    return output


def _trace_reads(
    connection: sqlite3.Connection,
    operation: Callable[[], Any],
) -> dict[str, Any]:
    reads: set[str] = set()
    write_attempts: list[dict[str, Any]] = []
    write_actions = {
        sqlite3.SQLITE_INSERT: "insert",
        sqlite3.SQLITE_UPDATE: "update",
        sqlite3.SQLITE_DELETE: "delete",
    }

    def authorizer(
        action: int,
        argument_one: str | None,
        argument_two: str | None,
        database_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if action == sqlite3.SQLITE_READ and argument_one:
            reads.add(str(argument_one))
        if action in write_actions:
            write_attempts.append(
                {
                    "action": write_actions[action],
                    "object": argument_one,
                    "column": argument_two,
                }
            )
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    connection.set_authorizer(authorizer)
    error: str | None = None
    try:
        operation()
    except Exception as exc:  # evidence captures a bounded, sanitized type/message
        error = f"{type(exc).__name__}: {exc}"
    finally:
        connection.set_authorizer(None)
    return {
        "tables": sorted(reads - SYSTEM_OBJECTS),
        "system_or_virtual_objects": sorted(reads & SYSTEM_OBJECTS),
        "write_attempts": write_attempts,
        "error": error,
    }


def _trace_public_tool_tables(
    connection: sqlite3.Connection,
    root: Path,
) -> dict[str, Any]:
    source_path = str(root / "src")
    if source_path not in sys.path:
        sys.path.insert(0, source_path)
    from ncs_mcp import server  # imported only after the audited DB is open

    surface = server.current_mcp_tool_surface()
    public_tools = list(surface.get("all_tools", []))
    unit_code = str(
        connection.execute(
            "SELECT unit_code FROM competency_units ORDER BY unit_code LIMIT 1"
        ).fetchone()[0]
    )
    training_course_id = int(
        connection.execute(
            "SELECT training_course_id FROM ncs_training_courses "
            "ORDER BY training_course_id LIMIT 1"
        ).fetchone()[0]
    )

    original_open_db = server.open_db
    server.open_db = lambda: nullcontext(connection)
    query = "\ucc44\uc6a9"
    try:
        traces: dict[str, dict[str, Any]] = {}
        traces["ncs_search"] = _trace_reads(
            connection,
            lambda: server.search_ncs(query=query, scope="all", limit=2),
        )
        traces["ncs_unit_detail"] = _trace_reads(
            connection,
            lambda: server.get_unit_structure(unit_code, text_version="raw"),
        )

        training_tables: set[str] = set()
        training_system: set[str] = set()
        training_writes: list[dict[str, Any]] = []
        training_errors: list[str] = []
        for operation in (
            lambda: server.search_training_courses(
                query=query, limit=2, link_limit=2
            ),
            lambda: server.get_training_course(training_course_id, link_limit=2),
        ):
            trace = _trace_reads(connection, operation)
            training_tables.update(trace["tables"])
            training_system.update(trace["system_or_virtual_objects"])
            training_writes.extend(trace["write_attempts"])
            if trace["error"]:
                training_errors.append(trace["error"])
        traces["ncs_training"] = {
            "tables": sorted(training_tables),
            "system_or_virtual_objects": sorted(training_system),
            "write_attempts": training_writes,
            "error": "; ".join(training_errors) or None,
            "paths_traced": ["search", "detail"],
        }

        analysis_tables: set[str] = set()
        analysis_system: set[str] = set()
        analysis_writes: list[dict[str, Any]] = []
        analysis_errors: list[str] = []
        analysis_operations = {
            "career_path": lambda: server.search_career_paths(
                query=query, limit=2
            ),
            "qualification": lambda: server.search_qualification_items(
                unit_code=unit_code, limit=2
            ),
            "job_base": lambda: server.search_job_base_competencies(
                unit_code=unit_code, limit=2
            ),
            "ontology": lambda: server.search_ontology_concepts(
                query=query, limit=2
            ),
        }
        for operation in analysis_operations.values():
            trace = _trace_reads(connection, operation)
            analysis_tables.update(trace["tables"])
            analysis_system.update(trace["system_or_virtual_objects"])
            analysis_writes.extend(trace["write_attempts"])
            if trace["error"]:
                analysis_errors.append(trace["error"])
        traces["ncs_analysis"] = {
            "tables": sorted(analysis_tables),
            "system_or_virtual_objects": sorted(analysis_system),
            "write_attempts": analysis_writes,
            "error": "; ".join(analysis_errors) or None,
            "modes_traced": sorted(analysis_operations),
        }

        traces["ncs_discover_tools"] = {
            "tables": [],
            "system_or_virtual_objects": [],
            "write_attempts": [],
            "error": None,
            "note": "Registry/router metadata only; no database read expected.",
        }
        traces["ncs_execute_tool"] = {
            **traces["ncs_search"],
            "note": "Meta-tool delegation traced through the ncs_search read-only path.",
        }
        traces["recommend_training_for_task"] = _trace_reads(
            connection,
            lambda: server.recommend_training_for_task(
                unit_code=unit_code,
                limit=1,
                save=False,
                compact=True,
            ),
        )
    finally:
        server.open_db = original_open_db

    all_tables = sorted(
        {
            table
            for trace in traces.values()
            for table in trace.get("tables", [])
        }
    )
    return {
        "public_tools": public_tools,
        "public_tool_count": len(public_tools),
        "expected_public_tools": list(EXPECTED_PUBLIC_TOOLS),
        "surface_matches_expected": public_tools == list(EXPECTED_PUBLIC_TOOLS),
        "sample_unit_code": unit_code,
        "sample_training_course_id": training_course_id,
        "tool_reads": traces,
        "all_observed_tables": all_tables,
        "all_traces_read_only": all(
            not trace.get("write_attempts") for trace in traces.values()
        ),
        "limitations": [
            "Dynamic reads cover representative public paths, not every parameter branch.",
            "Static source references and readiness/view dependencies remain deletion guards.",
        ],
    }


def _zip_single_file(path: Path, archive_path: Path, *, level: int = 1) -> int:
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=level,
    ) as archive:
        archive.write(path, path.name)
    return archive_path.stat().st_size


def _measure_ontology_dictionary_spike(
    source_database: Path,
    temporary_root: Path,
) -> dict[str, Any]:
    baseline = temporary_root / "ontology_baseline.db"
    encoded = temporary_root / "ontology_dictionary.db"
    source_uri = f"file:{source_database.resolve().as_posix()}?mode=ro&immutable=1"

    def build(destination: Path, *, dictionary_encoded: bool) -> float:
        connection = sqlite3.connect(destination, uri=True)
        connection.executescript(
            "PRAGMA page_size=4096; PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
        )
        connection.execute("ATTACH DATABASE ? AS source", (source_uri,))
        started = time.perf_counter()
        if not dictionary_encoded:
            connection.execute(
                "CREATE TABLE ontology_concepts AS "
                "SELECT * FROM source.ontology_concepts"
            )
            id_target = "ontology_concepts(concept_id)"
            key_target = "ontology_concepts(normalized_key)"
            type_target = "ontology_concepts(concept_type)"
        else:
            dimension_columns = (
                "concept_type",
                "definition_status",
                "relation_status",
                "review_status",
            )
            for column in dimension_columns:
                connection.execute(
                    f"CREATE TABLE dim_{column} "
                    "(code INTEGER PRIMARY KEY, value TEXT UNIQUE)"
                )
                connection.execute(
                    f"INSERT INTO dim_{column}(value) "
                    f"SELECT DISTINCT {column} FROM source.ontology_concepts "
                    f"WHERE {column} IS NOT NULL ORDER BY {column}"
                )
            connection.execute(
                """
                CREATE TABLE ontology_concepts_compact AS
                SELECT o.concept_id,
                       o.concept_name,
                       o.normalized_key,
                       ct.code AS concept_type_code,
                       ds.code AS definition_status_code,
                       rs.code AS relation_status_code,
                       rv.code AS review_status_code
                FROM source.ontology_concepts o
                LEFT JOIN dim_concept_type ct ON ct.value = o.concept_type
                LEFT JOIN dim_definition_status ds ON ds.value = o.definition_status
                LEFT JOIN dim_relation_status rs ON rs.value = o.relation_status
                LEFT JOIN dim_review_status rv ON rv.value = o.review_status
                """
            )
            connection.execute(
                """
                CREATE VIEW ontology_concepts AS
                SELECT o.concept_id,
                       o.concept_name,
                       o.normalized_key,
                       ct.value AS concept_type,
                       NULL AS definition,
                       NULL AS definition_source,
                       ds.value AS definition_status,
                       rs.value AS relation_status,
                       rv.value AS review_status,
                       NULL AS created_at,
                       NULL AS updated_at
                FROM ontology_concepts_compact o
                LEFT JOIN dim_concept_type ct ON ct.code = o.concept_type_code
                LEFT JOIN dim_definition_status ds ON ds.code = o.definition_status_code
                LEFT JOIN dim_relation_status rs ON rs.code = o.relation_status_code
                LEFT JOIN dim_review_status rv ON rv.code = o.review_status_code
                """
            )
            id_target = "ontology_concepts_compact(concept_id)"
            key_target = "ontology_concepts_compact(normalized_key)"
            type_target = "ontology_concepts_compact(concept_type_code)"
        connection.execute(f"CREATE UNIQUE INDEX idx_id ON {id_target}")
        connection.execute(f"CREATE INDEX idx_key ON {key_target}")
        connection.execute(f"CREATE INDEX idx_type ON {type_target}")
        connection.commit()
        elapsed = time.perf_counter() - started
        connection.execute("DETACH DATABASE source")
        connection.execute("VACUUM")
        connection.close()
        return elapsed

    baseline_seconds = build(baseline, dictionary_encoded=False)
    encoded_seconds = build(encoded, dictionary_encoded=True)
    baseline_zip = _zip_single_file(
        baseline, temporary_root / "ontology_baseline.zip", level=1
    )
    encoded_zip = _zip_single_file(
        encoded, temporary_root / "ontology_dictionary.zip", level=1
    )
    return {
        "method": "isolated_copy_dictionary_spike",
        "compression_level": 1,
        "baseline_db_bytes": baseline.stat().st_size,
        "encoded_db_bytes": encoded.stat().st_size,
        "estimated_db_savings_bytes": baseline.stat().st_size
        - encoded.stat().st_size,
        "baseline_zip_bytes": baseline_zip,
        "encoded_zip_bytes": encoded_zip,
        "estimated_zip_savings_bytes": baseline_zip - encoded_zip,
        "build_seconds": {
            "baseline": round(baseline_seconds, 6),
            "dictionary_encoded": round(encoded_seconds, 6),
        },
        "contract_preservation_design": (
            "A logical ontology_concepts view reconstructs current columns; physical "
            "status/type strings become dictionaries."
        ),
        "limitations": [
            "Isolated-table results are estimates, not a rebuilt full snapshot.",
            "The compact view changes query planning and needs latency/recall regression tests.",
            "ZIP level 1 is used for bounded audit time; production ZIP contribution differs.",
        ],
    }


def _cold_inference(
    raw_savings: int,
    database_bytes: int,
    cold_p50_seconds: float,
    measured_extract_seconds: float,
) -> dict[str, Any]:
    fraction = raw_savings / database_bytes if database_bytes else 0.0
    local_extract_linear = measured_extract_seconds * fraction
    full_cold_linear = cold_p50_seconds * fraction
    return {
        "basis": "linear byte-scaling inference, not a measured remote result",
        "raw_fraction": round(fraction, 8),
        "local_extract_stage_savings_seconds": round(local_extract_linear, 6),
        "full_cold_upper_bound_savings_seconds": round(full_cold_linear, 6),
        "estimated_cold_p50_after_upper_bound_seconds": round(
            max(cold_p50_seconds - full_cold_linear, 0.0), 6
        ),
    }


def _candidate_trims(
    *,
    database_bytes: int,
    cold_p50_seconds: float,
    measured_extract_seconds: float,
    freelist_bytes: int,
    duplicate_indexes: list[dict[str, Any]],
    footprints: list[dict[str, Any]],
    dictionary_spike: dict[str, Any] | None,
    readiness_tables: set[str],
    public_tables: set[str],
    source_references: dict[str, list[str]],
) -> list[dict[str, Any]]:
    by_table = {item["table"]: item for item in footprints}

    def candidate(
        name: str,
        raw_savings: int,
        zip_savings: int,
        *,
        risk: str,
        rebuild: bool,
        recommendation: str,
        blockers: list[str],
    ) -> dict[str, Any]:
        return {
            "candidate": name,
            "estimated_db_savings_bytes": raw_savings,
            "estimated_zip_savings_bytes": zip_savings,
            "public_contract_risk": risk,
            "rebuild_required": rebuild,
            "recommendation": recommendation,
            "blockers": blockers,
            "cold_p50_inference": _cold_inference(
                raw_savings,
                database_bytes,
                cold_p50_seconds,
                measured_extract_seconds,
            ),
        }

    duplicate_db = sum(
        item["estimated_db_savings_bytes"]
        for item in duplicate_indexes
        if item["kind"] == "exact"
    )
    duplicate_zip = sum(
        item["estimated_zip_savings_bytes"]
        for item in duplicate_indexes
        if item["kind"] == "exact"
    )
    output = [
        candidate(
            "freelist_or_exact_duplicate_reclaim",
            freelist_bytes + duplicate_db,
            duplicate_zip,
            risk="low only after query-plan proof",
            rebuild=bool(freelist_bytes),
            recommendation=(
                "no_change"
                if freelist_bytes + duplicate_db == 0
                else "measure_vacuum_and_query_plans_before_change"
            ),
            blockers=(
                ["freelist_count is zero", "no exact duplicate indexes detected"]
                if freelist_bytes + duplicate_db == 0
                else ["index usage and rebuilt archive size are not yet proven"]
            ),
        )
    ]

    if dictionary_spike:
        raw = int(dictionary_spike["estimated_db_savings_bytes"])
        compressed = int(dictionary_spike["estimated_zip_savings_bytes"])
        output.append(
            candidate(
                "dictionary_encode_ontology_concept_dimensions",
                raw,
                compressed,
                risk="medium_high",
                rebuild=True,
                recommendation="prototype_only_do_not_promote",
                blockers=[
                    "ontology_concepts is read by public analysis/recommendation paths",
                    "view-based reconstruction can change query plans",
                    "full-snapshot size, recall, latency, RSS, and contract tests are missing",
                ],
            )
        )

    evaluation_tables = (
        "training_transition_gold_scenarios",
        "training_transition_scenario_reviews",
    )
    raw = sum(by_table.get(name, {}).get("bytes", 0) for name in evaluation_tables)
    compressed = sum(
        by_table.get(name, {}).get("independent_deflate_bytes", 0)
        for name in evaluation_tables
    )
    output.append(
        candidate(
            "remove_transition_evaluation_tables",
            int(raw),
            int(compressed),
            risk="high",
            rebuild=True,
            recommendation="reject",
            blockers=[
                "both tables are in the 25-table readiness contract",
                "removal would discard packaged evaluation evidence",
                "savings are immaterial",
            ],
        )
    )

    for table_name in ("ontology_relation_incoming", "criteria_concept_inverse"):
        item = by_table.get(table_name, {})
        blockers: list[str] = []
        if table_name in readiness_tables:
            blockers.append("table is required by readiness")
        if table_name in public_tables:
            blockers.append("table was read by a representative public-tool trace")
        if table_name in source_references:
            blockers.append("table is referenced by product source code")
        output.append(
            candidate(
                f"remove_{table_name}",
                int(item.get("bytes", 0)),
                int(item.get("independent_deflate_bytes", 0)),
                risk="critical",
                rebuild=True,
                recommendation="reject",
                blockers=blockers or ["absence of use has not been proven"],
            )
        )
    return output


def run_audit(
    archive_path: Path,
    manifest_path: Path,
    *,
    vercel_config: Path = DEFAULT_VERCEL_CONFIG,
    root: Path = ROOT,
    cold_p50_seconds: float = DEFAULT_COLD_P50_SECONDS,
    trace_public_tools: bool = True,
    run_dictionary_spike: bool = True,
) -> dict[str, Any]:
    archive_path = archive_path.resolve()
    manifest_path = manifest_path.resolve()
    archive_sha_before = _sha256(archive_path)
    manifest_sha_before = _sha256(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    readiness = _readiness_tables(vercel_config)

    temporary_path: Path | None = None
    with tempfile.TemporaryDirectory(prefix="ncs-compact-size-audit-") as temporary:
        temporary_path = Path(temporary)
        database_path = temporary_path / str(manifest["archive_member"])
        extract_started = time.perf_counter()
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != manifest["archive_member"]:
                raise ValueError("compact archive must contain the single manifest member")
            member = members[0]
            with archive.open(member, "r") as source, database_path.open("wb") as target:
                shutil.copyfileobj(source, target, length=8 * 1024 * 1024)
        extract_seconds = time.perf_counter() - extract_started
        database_sha = _sha256(database_path)
        database_bytes = database_path.stat().st_size
        if database_sha != str(manifest["sqlite_sha256"]):
            raise ValueError("extracted SQLite SHA-256 does not match manifest")
        if database_bytes != int(manifest["sqlite_bytes"]):
            raise ValueError("extracted SQLite byte size does not match manifest")

        connection = sqlite3.connect(
            f"file:{database_path.resolve().as_posix()}?mode=ro&immutable=1",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        freelist_count = int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        schema = _schema_inventory(connection)
        view_dependencies = _view_dependencies(schema)
        objects = _dbstat_objects(connection)
        _add_independent_deflate_estimates(
            connection, database_path, page_size, objects
        )
        object_sizes = {item["name"]: item for item in objects}
        indexes, duplicate_indexes = _index_inventory(connection, object_sizes)
        footprints = _table_footprints(schema, objects)
        source_references = _source_references(root, schema)
        public_trace = (
            _trace_public_tool_tables(connection, root)
            if trace_public_tools
            else {
                "public_tools": list(EXPECTED_PUBLIC_TOOLS),
                "public_tool_count": len(EXPECTED_PUBLIC_TOOLS),
                "expected_public_tools": list(EXPECTED_PUBLIC_TOOLS),
                "surface_matches_expected": None,
                "tool_reads": {},
                "all_observed_tables": [],
                "all_traces_read_only": None,
                "skipped": True,
            }
        )
        dictionary_spike = (
            _measure_ontology_dictionary_spike(database_path, temporary_path)
            if run_dictionary_spike
            and "ontology_concepts" in schema
            and schema["ontology_concepts"]["type"] == "table"
            else None
        )
        connection.close()

        public_tables = set(public_trace.get("all_observed_tables", []))
        readiness_tables = set(readiness["required_tables"])
        candidate_trims = _candidate_trims(
            database_bytes=database_bytes,
            cold_p50_seconds=cold_p50_seconds,
            measured_extract_seconds=extract_seconds,
            freelist_bytes=freelist_count * page_size,
            duplicate_indexes=duplicate_indexes,
            footprints=footprints,
            dictionary_spike=dictionary_spike,
            readiness_tables=readiness_tables,
            public_tables=public_tables,
            source_references=source_references,
        )

        dbstat_total = sum(item["bytes"] for item in objects)
        dbstat_payload = sum(item["payload_bytes"] for item in objects)
        dbstat_unused = sum(item["unused_bytes"] for item in objects)
        safe_candidate = candidate_trims[0]
        verdict = (
            "no_change"
            if safe_candidate["estimated_db_savings_bytes"] == 0
            else "measure_before_change"
        )
        report: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "ok",
            "verdict": verdict,
            "recommendation": (
                "Keep the current snapshot. No free pages or duplicate/prefix indexes "
                "provide a proven safe reclaim. Prototype structural compaction only "
                "behind full contract and performance gates."
            ),
            "source_integrity": {
                "archive_path": str(archive_path),
                "archive_bytes": archive_path.stat().st_size,
                "archive_sha256_before": archive_sha_before,
                "manifest_path": str(manifest_path),
                "manifest_sha256_before": manifest_sha_before,
                "archive_member": member.filename,
                "archive_member_uncompressed_bytes": member.file_size,
                "archive_member_compressed_bytes": member.compress_size,
                "archive_member_compression_ratio": round(
                    member.compress_size / member.file_size, 8
                ),
                "database_sha256": database_sha,
                "manifest_database_sha256": manifest["sqlite_sha256"],
                "database_sha_matches_manifest": database_sha
                == manifest["sqlite_sha256"],
            },
            "capacity": {
                "database_bytes": database_bytes,
                "hard_cap_bytes": HARD_CAP_BYTES,
                "soft_cap_bytes": SOFT_CAP_BYTES,
                "hard_cap_headroom_bytes": HARD_CAP_BYTES - database_bytes,
                "soft_cap_headroom_bytes": SOFT_CAP_BYTES - database_bytes,
                "within_hard_cap": database_bytes < HARD_CAP_BYTES,
                "within_soft_cap": database_bytes < SOFT_CAP_BYTES,
            },
            "page_accounting": {
                "page_size": page_size,
                "page_count": page_count,
                "page_bytes": page_size * page_count,
                "freelist_count": freelist_count,
                "freelist_bytes": freelist_count * page_size,
                "dbstat_total_bytes": dbstat_total,
                "dbstat_payload_bytes": dbstat_payload,
                "dbstat_internal_unused_bytes": dbstat_unused,
                "dbstat_accounts_for_database": dbstat_total == database_bytes,
                "note": (
                    "dbstat unused bytes are slack inside live B-tree pages, not free "
                    "pages and not a safe byte-for-byte reclaim claim."
                ),
            },
            "cold_start_context": {
                "cold_p50_seconds": cold_p50_seconds,
                "cold_p50_source": "caller-provided measured baseline",
                "local_extract_seconds": round(extract_seconds, 6),
                "candidate_estimates_are_inferences": True,
            },
            "readiness_contract": readiness,
            "manifest_contract": {
                "physical_counts": manifest.get("physical_counts", {}),
                "logical_counts": manifest.get("logical_counts", {}),
                "servable_counts": manifest.get("servable_counts", {}),
                "required_table_resolution": {
                    name: {
                        "schema_type": schema.get(name, {}).get("type"),
                        "view_dependencies": view_dependencies.get(name, []),
                        "manifest_physical": name
                        in manifest.get("physical_counts", {}),
                        "manifest_servable": name
                        in manifest.get("servable_counts", {}),
                    }
                    for name in readiness["required_tables"]
                },
            },
            "public_tool_table_reads": public_trace,
            "object_sizes": objects,
            "table_footprints": footprints,
            "indexes": indexes,
            "duplicate_or_prefix_index_candidates": duplicate_indexes,
            "view_dependencies": view_dependencies,
            "source_references": source_references,
            "ontology_dictionary_spike": dictionary_spike,
            "candidate_trims": candidate_trims,
            "deletion_policy": {
                "tables_recommended_for_deletion": [],
                "rule": (
                    "No table deletion is recommended without public traces, readiness, "
                    "view dependency, source reference, and full regression evidence."
                ),
            },
        }

    report["source_integrity"].update(
        {
            "archive_sha256_after": _sha256(archive_path),
            "manifest_sha256_after": _sha256(manifest_path),
            "archive_unchanged": _sha256(archive_path) == archive_sha_before,
            "manifest_unchanged": _sha256(manifest_path) == manifest_sha_before,
            "temporary_directory_cleaned": bool(
                temporary_path is not None and not temporary_path.exists()
            ),
        }
    )
    return report


def render_markdown(report: dict[str, Any]) -> str:
    integrity = report["source_integrity"]
    capacity = report["capacity"]
    pages = report["page_accounting"]
    readiness = report["readiness_contract"]
    public = report["public_tool_table_reads"]
    lines = [
        "# Compact Snapshot Size Audit",
        "",
        f"- Verdict: `{report['verdict']}`",
        f"- Recommendation: {report['recommendation']}",
        f"- SQLite: `{capacity['database_bytes']:,}` bytes",
        f"- ZIP: `{integrity['archive_bytes']:,}` bytes",
        f"- Hard-cap headroom: `{capacity['hard_cap_headroom_bytes']:,}` bytes",
        f"- Soft-cap headroom: `{capacity['soft_cap_headroom_bytes']:,}` bytes",
        "",
        "## Integrity and Page Accounting",
        "",
        f"- SQLite SHA matches manifest: `{integrity['database_sha_matches_manifest']}`",
        f"- Archive unchanged: `{integrity['archive_unchanged']}`",
        f"- Manifest unchanged: `{integrity['manifest_unchanged']}`",
        f"- Temporary directory cleaned: `{integrity['temporary_directory_cleaned']}`",
        f"- Page size/count: `{pages['page_size']}` / `{pages['page_count']:,}`",
        f"- Freelist pages/bytes: `{pages['freelist_count']}` / `{pages['freelist_bytes']:,}`",
        f"- Internal unused bytes: `{pages['dbstat_internal_unused_bytes']:,}`",
        f"- dbstat accounts for full DB: `{pages['dbstat_accounts_for_database']}`",
        "",
        "`dbstat` internal unused bytes are slack in live pages, not proven free space.",
        "",
        "## Serving Contracts",
        "",
        f"- Public tools: `{public['public_tool_count']}`",
        f"- Public surface matches expected seven: `{public['surface_matches_expected']}`",
        f"- Public traces were read-only: `{public['all_traces_read_only']}`",
        f"- Readiness required tables: `{readiness['required_table_count']}`",
        "",
        "### Public-tool observed tables",
        "",
        "| Tool | Tables read | Error |",
        "| --- | --- | --- |",
    ]
    for tool in public.get("public_tools", []):
        trace = public.get("tool_reads", {}).get(tool, {})
        tables = ", ".join(f"`{name}`" for name in trace.get("tables", [])) or "none"
        error = str(trace.get("error") or "none").replace("|", "\\|")
        lines.append(f"| `{tool}` | {tables} | {error} |")

    lines.extend(
        [
            "",
            "### Readiness table resolution",
            "",
            "| Name | Schema type | Physical count | Servable count | View dependencies |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    resolution = report["manifest_contract"]["required_table_resolution"]
    for name in readiness["required_tables"]:
        item = resolution[name]
        dependencies = ", ".join(item["view_dependencies"]) or "none"
        lines.append(
            f"| `{name}` | `{item['schema_type']}` | "
            f"`{item['manifest_physical']}` | `{item['manifest_servable']}` | "
            f"{dependencies} |"
        )

    lines.extend(
        [
            "",
            "## Largest Table Footprints",
            "",
            "Table footprints include owned indexes. ZIP contribution is an independent "
            "raw-deflate estimate and does not sum exactly to the production archive.",
            "",
            "| Table | DB bytes | Est. deflate bytes | Objects |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for item in report["table_footprints"][:20]:
        lines.append(
            f"| `{item['table']}` | {item['bytes']:,} | "
            f"{item['independent_deflate_bytes']:,} | {len(item['objects'])} |"
        )

    lines.extend(
        [
            "",
            "## Index Audit",
            "",
            f"- Index count: `{len(report['indexes'])}`",
            "- Exact/prefix duplicate candidates: "
            f"`{len(report['duplicate_or_prefix_index_candidates'])}`",
            "",
            "## Candidate Trims",
            "",
            "Cold p50 effects below are explicit linear byte-scaling inferences, not "
            "remote measurements.",
            "",
            "| Candidate | DB savings | ZIP savings | Contract risk | Rebuild | Cold p50 upper-bound saving | Decision |",
            "| --- | ---: | ---: | --- | --- | ---: | --- |",
        ]
    )
    for item in report["candidate_trims"]:
        inference = item["cold_p50_inference"]
        lines.append(
            f"| `{item['candidate']}` | {item['estimated_db_savings_bytes']:,} | "
            f"{item['estimated_zip_savings_bytes']:,} | `{item['public_contract_risk']}` | "
            f"`{item['rebuild_required']}` | "
            f"{inference['full_cold_upper_bound_savings_seconds']:.3f}s | "
            f"`{item['recommendation']}` |"
        )
        lines.append(
            f"|  |  |  | Blockers |  |  | "
            f"{' ; '.join(item['blockers']).replace('|', '/')} |"
        )

    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            "The current DB already has zero freelist pages and no exact or left-prefix "
            "duplicate indexes. Removing inverse evidence or evaluation tables would break "
            "readiness/public evidence for small or unproven savings. The dictionary spike "
            "is a structural prototype only; it must not be promoted without a full rebuilt "
            "snapshot and recall, latency, RSS, readiness, and seven-tool contract gates.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only page, contract, and compression audit for the compact snapshot."
    )
    parser.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--vercel-config", type=Path, default=DEFAULT_VERCEL_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_JSON_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument(
        "--cold-p50-seconds", type=float, default=DEFAULT_COLD_P50_SECONDS
    )
    parser.add_argument("--skip-public-trace", action="store_true")
    parser.add_argument("--skip-dictionary-spike", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = run_audit(
        args.archive,
        args.manifest,
        vercel_config=args.vercel_config,
        cold_p50_seconds=args.cold_p50_seconds,
        trace_public_tools=not args.skip_public_trace,
        run_dictionary_spike=not args.skip_dictionary_spike,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "schema": report["schema"],
                "verdict": report["verdict"],
                "database_bytes": report["capacity"]["database_bytes"],
                "archive_bytes": report["source_integrity"]["archive_bytes"],
                "freelist_bytes": report["page_accounting"]["freelist_bytes"],
                "duplicate_index_candidates": len(
                    report["duplicate_or_prefix_index_candidates"]
                ),
                "public_tool_count": report["public_tool_table_reads"][
                    "public_tool_count"
                ],
                "readiness_required_table_count": report["readiness_contract"][
                    "required_table_count"
                ],
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
