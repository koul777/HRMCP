"""Read-only capacity preflight for the SQLite-to-Gold LPG projection.

This module deliberately does *not* invoke :mod:`ncs_mcp.gold_lpg`.  The
projector currently creates Python lists for all nodes and edges, so callers
can use this inexpensive count-only check to decide whether that operation is
safe before allocating those lists.  It has no driver dependency and opens the
source database through SQLite's ``mode=ro`` URI.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping


GOLD_READINESS_SCHEMA = "ncs_gold_projection_readiness_v1"
GOLD_SCOPE_CONTRACT_SCHEMA = "ncs_gold_serving_core_scope_v1"

# These active SQLite layers are intentionally not materialised by the bounded
# serving-core LPG.  The contract must name them rather than allowing a Neo4j
# consumer to mistake the projection for a complete ontology replica.
SQLITE_AUTHORITATIVE_FALLBACK_TABLES: dict[str, str] = {
    "ontology_concept_relations": "cross-concept ontology relation evidence",
    "task_similarity_links": "task transferability and similarity evidence",
    "ncs_career_paths": "career-path level and transition supporting evidence",
    "ncs_qualification_items": "qualification supporting evidence",
    "ncs_unit_qualification_links": "unit-to-qualification supporting evidence",
    "ncs_job_base_competencies": "job-base competency supporting evidence",
    "ncs_job_base_factors": "job-base factor supporting evidence",
    "ncs_unit_job_base_links": "unit-to-job-base supporting evidence",
}


@dataclass(frozen=True)
class GoldProjectionProfile:
    """Projection sections included in a planned Gold graph export.

    ``serving_core`` intentionally excludes the detailed
    ``task_ksa_concept_relations`` table.  That table can contain many millions
    of relationship rows; the core path retains criterion-to-concept evidence
    and a distinct element-to-concept summary instead.
    """

    name: str = "serving_core"
    include_classifications: bool = True
    include_competency_units: bool = True
    include_competency_elements: bool = True
    include_performance_criteria: bool = True
    include_ontology_concepts: bool = True
    include_criteria_concept_links: bool = True
    include_element_concept_summary: bool = True
    include_task_ksa_detailed_relations: bool = False
    include_training_courses: bool = True
    include_training_unit_links: bool = True
    include_training_concept_links: bool = True
    include_training_element_links: bool = True
    include_training_goal_links: bool = True
    include_training_delivery: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


SERVING_CORE_PROFILE = GoldProjectionProfile()


@dataclass(frozen=True)
class GoldReadinessThresholds:
    """Conservative, caller-overridable limits for in-memory projection."""

    max_in_memory_records: int = 1_000_000
    warning_in_memory_records: int = 250_000
    max_in_memory_bytes: int = 256 * 1024 * 1024
    warning_in_memory_bytes: int = 64 * 1024 * 1024
    streaming_batch_size: int = 10_000
    node_payload_bytes: int = 800
    edge_payload_bytes: int = 650
    in_memory_amplification: float = 3.0

    def __post_init__(self) -> None:
        if (
            self.max_in_memory_records < 1
            or self.warning_in_memory_records < 0
            or self.max_in_memory_bytes < 1
            or self.warning_in_memory_bytes < 0
            or self.streaming_batch_size < 1
            or self.node_payload_bytes < 1
            or self.edge_payload_bytes < 1
            or self.in_memory_amplification < 1
        ):
            raise ValueError("Gold readiness thresholds must be positive (warnings may be zero)")
        if self.warning_in_memory_records > self.max_in_memory_records:
            raise ValueError("warning_in_memory_records cannot exceed max_in_memory_records")
        if self.warning_in_memory_bytes > self.max_in_memory_bytes:
            raise ValueError("warning_in_memory_bytes cannot exceed max_in_memory_bytes")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RELEVANT_TABLES: dict[str, tuple[str, bool]] = {
    "classifications": ("include_classifications", False),
    "competency_units": ("include_competency_units", False),
    "competency_elements": ("include_competency_elements", False),
    "performance_criteria": ("include_performance_criteria", False),
    "ontology_concepts": ("include_ontology_concepts", False),
    "criteria_concept_links": ("include_criteria_concept_links", True),
    "task_ksa_concept_relations": ("include_task_ksa_detailed_relations", True),
    "ncs_training_courses": ("include_training_courses", True),
    "ncs_training_course_unit_links": ("include_training_unit_links", True),
    "ncs_training_course_concept_links": ("include_training_concept_links", True),
    "ncs_training_course_element_links": ("include_training_element_links", True),
    "training_goal_concept_links": ("include_training_goal_links", True),
    "training_delivery_relations": ("include_training_delivery", True),
}


def _quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _readonly_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {path}")
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=15)
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _table_columns(conn: sqlite3.Connection) -> dict[str, set[str]]:
    available_names = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    # Some SQLite builds expose optional virtual tables such as ``dbstat`` in
    # sqlite_master even when the Python runtime cannot load their module.
    # Gold readiness only needs the explicitly contracted source tables, so
    # never introspect unrelated application or SQLite-internal tables.
    names = sorted(
        (set(RELEVANT_TABLES) | set(SQLITE_AUTHORITATIVE_FALLBACK_TABLES))
        & available_names
    )
    return {
        name: {str(row[1]) for row in conn.execute(f"PRAGMA table_info({_quote_identifier(name)})")}
        for name in names
    }


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0])


def serving_core_scope_contract(
    table_counts: Mapping[str, Mapping[str, Any]],
    *,
    profile: GoldProjectionProfile = SERVING_CORE_PROFILE,
    omitted_inherited_training_concept_links: int = 0,
) -> dict[str, Any]:
    """Describe the intentional boundary between Gold and SQLite evidence.

    Counts are source-table metadata only.  This does not read source rows,
    infer approval, or suggest that an absent optional table is complete.
    """

    fallback_evidence = []
    for table, purpose in SQLITE_AUTHORITATIVE_FALLBACK_TABLES.items():
        details = table_counts.get(table, {})
        fallback_evidence.append(
            {
                "table": table,
                "purpose": purpose,
                "present": bool(details.get("present", False)),
                "row_count": int(details.get("row_count", 0) or 0),
                "serving_core_projected": False,
                "authority": "sqlite_authoritative_fallback",
            }
        )
    task_details = table_counts.get("task_ksa_concept_relations", {})
    return {
        "schema": GOLD_SCOPE_CONTRACT_SCHEMA,
        "profile": profile.name,
        "projection_kind": "hybrid_serving_core",
        "sqlite_authoritative": True,
        "neo4j_projection_authoritative": False,
        "complete_sqlite_ontology_replica": False,
        "fallback_evidence": fallback_evidence,
        "intentional_omissions": [
            {
                "code": (
                    "task_ksa_concept_relations_included"
                    if profile.include_task_ksa_detailed_relations
                    else "task_ksa_concept_relations_omitted"
                ),
                "table": "task_ksa_concept_relations",
                "present": bool(task_details.get("present", False)),
                "row_count": int(task_details.get("row_count", 0) or 0),
                "reason": (
                    "profile_includes_detailed_task_ksa_evidence"
                    if profile.include_task_ksa_detailed_relations
                    else "serving_core_uses_criterion_and_summary_evidence"
                ),
            },
            {
                "code": "inherited_course_concept_links_omitted",
                "table": "ncs_training_course_concept_links",
                "row_count": int(omitted_inherited_training_concept_links),
                "link_method": "unit_ksa_concept_inherited",
                "reason": "weak_candidate_expansion_not_direct_serving_evidence",
            },
        ],
        "db_writes": False,
        "human_approval_claim": False,
    }


def _merge_thresholds(
    thresholds: GoldReadinessThresholds | Mapping[str, Any] | None,
) -> GoldReadinessThresholds:
    if thresholds is None:
        return GoldReadinessThresholds()
    if isinstance(thresholds, GoldReadinessThresholds):
        return thresholds
    if not isinstance(thresholds, Mapping):
        raise TypeError("thresholds must be GoldReadinessThresholds, a mapping, or None")
    allowed = {item.name for item in fields(GoldReadinessThresholds)}
    unknown = sorted(set(thresholds) - allowed)
    if unknown:
        raise ValueError(f"Unknown Gold readiness threshold(s): {', '.join(unknown)}")
    return GoldReadinessThresholds(**dict(thresholds))


def _boundary_sample_digest(path: Path, *, sample_bytes: int = 65536) -> tuple[str, int]:
    """Hash a bounded first/last sample, never the complete database file."""

    size = path.stat().st_size
    digest = hashlib.sha256()
    sampled = 0
    with path.open("rb") as handle:
        first = handle.read(min(sample_bytes, size))
        digest.update(first)
        sampled += len(first)
        if size > sample_bytes:
            handle.seek(max(size - sample_bytes, 0))
            last = handle.read(sample_bytes)
            digest.update(last)
            sampled += len(last)
    return digest.hexdigest(), sampled


def _source_fingerprint(conn: sqlite3.Connection, path: Path) -> dict[str, Any]:
    stat = path.stat()
    sample_sha256, sampled_bytes = _boundary_sample_digest(path)
    page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
    page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    metadata = {
        "algorithm": "sha256_stat_schema_boundary_samples_v1",
        "database_name": path.name,
        "file_size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "page_count": page_count,
        "page_size": page_size,
        "schema_version": schema_version,
        "user_version": user_version,
        "sampled_bytes": sampled_bytes,
        "sample_sha256": sample_sha256,
        "full_file_hashed": False,
    }
    canonical = json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {**metadata, "fingerprint": hashlib.sha256(canonical).hexdigest()}


def _distinct_element_concept_count(
    conn: sqlite3.Connection, columns: Mapping[str, set[str]]
) -> tuple[int, str | None]:
    required = {
        "criteria_concept_links": {"link_id", "criteria_id", "concept_id"},
        "performance_criteria": {"criteria_id", "element_id"},
        "competency_elements": {"element_id"},
    }
    missing = [
        f"{table}.{column}"
        for table, needed in required.items()
        for column in sorted(needed - columns.get(table, set()))
    ]
    if missing:
        return 0, "missing columns: " + ", ".join(missing)
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT pc.element_id, ccl.concept_id
            FROM criteria_concept_links AS ccl
            JOIN performance_criteria AS pc ON pc.criteria_id = ccl.criteria_id
            JOIN competency_elements AS element ON element.element_id = pc.element_id
            WHERE ccl.link_id IS NOT NULL
              AND pc.element_id IS NOT NULL
              AND ccl.concept_id IS NOT NULL
        )
        """
    ).fetchone()
    return int(row[0]), None


def _valid_classification_job_count(
    conn: sqlite3.Connection, columns: Mapping[str, set[str]]
) -> tuple[int, str | None]:
    """Count classification rows that produce a Gold NCSJob identifier."""

    required = {"classification_id", "major_code", "middle_code", "small_code", "sub_code"}
    missing = sorted(required - columns.get("classifications", set()))
    if missing:
        return 0, "missing columns: " + ", ".join(f"classifications.{name}" for name in missing)
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM classifications
        WHERE classification_id IS NOT NULL
          AND TRIM(CAST(major_code AS TEXT)) <> ''
          AND TRIM(CAST(middle_code AS TEXT)) <> ''
          AND TRIM(CAST(small_code AS TEXT)) <> ''
          AND TRIM(CAST(sub_code AS TEXT)) <> ''
        """
    ).fetchone()
    return int(row[0]), None


def _distinct_job_concept_summary_count(
    conn: sqlite3.Connection, columns: Mapping[str, set[str]]
) -> tuple[int, str | None]:
    """Count the v2 serving-core NCSJob-to-concept aggregate edges.

    The Gold projector creates one typed edge per distinct resolved NCS job and
    concept after traversing classification -> unit -> element -> criterion.
    This query deliberately computes only that grouped cardinality; it never
    reads KSA text, course text, or relation payloads into Python.
    """

    required = {
        "classifications": {"classification_id", "major_code", "middle_code", "small_code", "sub_code"},
        "competency_units": {"unit_code", "classification_id"},
        "competency_elements": {"element_id", "unit_code"},
        "performance_criteria": {"criteria_id", "element_id"},
        "ontology_concepts": {"concept_id"},
        "criteria_concept_links": {"link_id", "criteria_id", "concept_id"},
    }
    missing = [
        f"{table}.{column}"
        for table, needed in required.items()
        for column in sorted(needed - columns.get(table, set()))
    ]
    if missing:
        return 0, "missing columns: " + ", ".join(missing)
    row = conn.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT DISTINCT
                COALESCE(CAST(c.major_code AS TEXT), '') ||
                COALESCE(CAST(c.middle_code AS TEXT), '') ||
                COALESCE(CAST(c.small_code AS TEXT), '') ||
                COALESCE(CAST(c.sub_code AS TEXT), '') AS job_code,
                ccl.concept_id
            FROM criteria_concept_links AS ccl
            JOIN performance_criteria AS pc ON pc.criteria_id = ccl.criteria_id
            JOIN competency_elements AS element ON element.element_id = pc.element_id
            JOIN competency_units AS unit ON unit.unit_code = element.unit_code
            JOIN classifications AS c ON c.classification_id = unit.classification_id
            JOIN ontology_concepts AS concept ON concept.concept_id = ccl.concept_id
            WHERE ccl.link_id IS NOT NULL
              AND TRIM(CAST(c.major_code AS TEXT)) <> ''
              AND TRIM(CAST(c.middle_code AS TEXT)) <> ''
              AND TRIM(CAST(c.small_code AS TEXT)) <> ''
              AND TRIM(CAST(c.sub_code AS TEXT)) <> ''
        )
        """
    ).fetchone()
    return int(row[0]), None


def _serving_core_emission_counts(
    conn: sqlite3.Connection, columns: Mapping[str, set[str]]
) -> tuple[dict[str, int] | None, str | None]:
    """Count v2 output cardinalities exactly, including malformed-source joins."""

    required = {
        "classifications": {"classification_id", "major_code", "middle_code", "small_code", "sub_code"},
        "competency_units": {"unit_code", "classification_id"},
        "competency_elements": {"element_id", "unit_code"},
        "performance_criteria": {"criteria_id", "element_id"},
        "ontology_concepts": {"concept_id"},
        "criteria_concept_links": {"link_id", "criteria_id", "concept_id"},
        "ncs_training_courses": {"training_course_id"},
        "ncs_training_course_unit_links": {"link_id", "training_course_id", "unit_code"},
        "ncs_training_course_concept_links": {"link_id", "training_course_id", "concept_id"},
        "ncs_training_course_element_links": {"link_id", "training_course_id", "element_id"},
        "training_goal_concept_links": {"link_id", "training_course_id", "concept_id"},
        "training_delivery_relations": {"relation_id", "training_course_id"},
    }
    missing = [
        f"{table}.{column}"
        for table, needed in required.items()
        for column in sorted(needed - columns.get(table, set()))
    ]
    if missing:
        return None, "missing columns: " + ", ".join(missing)
    inherited_filter = ""
    if "link_method" in columns["ncs_training_course_concept_links"]:
        inherited_filter = "AND (cc.link_method IS NULL OR TRIM(cc.link_method) != 'unit_ksa_concept_inherited')"
    row = conn.execute(
        f"""
        WITH normalized_classifications AS (
            SELECT classification_id,
                   TRIM(CAST(major_code AS TEXT)) AS major_code,
                   TRIM(CAST(middle_code AS TEXT)) AS middle_code,
                   TRIM(CAST(small_code AS TEXT)) AS small_code,
                   TRIM(CAST(sub_code AS TEXT)) AS sub_code
            FROM classifications WHERE classification_id IS NOT NULL
        ), valid_classifications AS (
            SELECT * FROM normalized_classifications
             WHERE major_code <> '' AND middle_code <> ''
               AND small_code <> '' AND sub_code <> ''
        ), category_nodes AS (
            SELECT 'm:' || major_code AS key FROM normalized_classifications WHERE major_code <> ''
            UNION SELECT 'mi:' || major_code || ':' || middle_code FROM normalized_classifications WHERE major_code <> '' AND middle_code <> ''
            UNION SELECT 's:' || major_code || ':' || middle_code || ':' || small_code FROM normalized_classifications WHERE major_code <> '' AND middle_code <> '' AND small_code <> ''
            UNION SELECT 'su:' || major_code || ':' || middle_code || ':' || small_code || ':' || sub_code FROM normalized_classifications WHERE major_code <> '' AND middle_code <> '' AND small_code <> '' AND sub_code <> ''
        ), jobs AS (
            SELECT DISTINCT major_code || middle_code || small_code || sub_code AS key FROM valid_classifications
        ), direct AS (
            SELECT ccl.link_id, ccl.concept_id, pc.element_id
            FROM criteria_concept_links ccl
            JOIN performance_criteria pc ON pc.criteria_id = ccl.criteria_id
            JOIN ontology_concepts oc ON oc.concept_id = ccl.concept_id
            WHERE ccl.link_id IS NOT NULL
        ), element_direct AS (
            SELECT direct.* FROM direct JOIN competency_elements ce ON ce.element_id = direct.element_id
        ), job_direct AS (
            SELECT element_direct.*, vc.major_code || vc.middle_code || vc.small_code || vc.sub_code AS job_key
            FROM element_direct
            JOIN competency_elements ce ON ce.element_id = element_direct.element_id
            JOIN competency_units cu ON cu.unit_code = ce.unit_code
            JOIN valid_classifications vc ON vc.classification_id = cu.classification_id
        )
        SELECT
          (SELECT COUNT(*) FROM category_nodes) + (SELECT COUNT(*) FROM jobs)
          + (SELECT COUNT(DISTINCT unit_code) FROM competency_units WHERE unit_code IS NOT NULL)
          + (SELECT COUNT(DISTINCT element_id) FROM competency_elements WHERE element_id IS NOT NULL)
          + (SELECT COUNT(DISTINCT criteria_id) FROM performance_criteria WHERE criteria_id IS NOT NULL)
          + (SELECT COUNT(DISTINCT concept_id) FROM ontology_concepts WHERE concept_id IS NOT NULL)
          + (SELECT COUNT(DISTINCT training_course_id) FROM ncs_training_courses WHERE training_course_id IS NOT NULL)
          + (SELECT COUNT(DISTINCT relation_id) FROM training_delivery_relations WHERE relation_id IS NOT NULL),
          COALESCE((SELECT SUM(
              CASE WHEN major_code <> '' AND middle_code <> '' THEN 1 ELSE 0 END +
              CASE WHEN major_code <> '' AND middle_code <> '' AND small_code <> '' THEN 1 ELSE 0 END +
              CASE WHEN major_code <> '' AND middle_code <> '' AND small_code <> '' AND sub_code <> '' THEN 1 ELSE 0 END +
              CASE WHEN major_code <> '' AND middle_code <> '' AND small_code <> '' AND sub_code <> '' THEN 1 ELSE 0 END
          ) FROM normalized_classifications), 0)
          + (SELECT COUNT(*) FROM competency_units cu JOIN valid_classifications vc ON vc.classification_id = cu.classification_id WHERE cu.unit_code IS NOT NULL)
          + (SELECT COUNT(*) FROM competency_elements ce JOIN competency_units cu ON cu.unit_code = ce.unit_code WHERE ce.element_id IS NOT NULL)
          + (SELECT COUNT(*) FROM performance_criteria pc JOIN competency_elements ce ON ce.element_id = pc.element_id WHERE pc.criteria_id IS NOT NULL)
          + (SELECT COUNT(*) FROM direct)
          + (SELECT COUNT(*) FROM (SELECT DISTINCT element_id, concept_id FROM element_direct))
          + (SELECT COUNT(*) FROM (SELECT DISTINCT job_key, concept_id FROM job_direct))
          + (SELECT COUNT(*) FROM ncs_training_course_unit_links l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN competency_units u ON u.unit_code = l.unit_code WHERE l.link_id IS NOT NULL)
          + (SELECT COUNT(*) FROM ncs_training_course_concept_links cc JOIN ncs_training_courses c ON c.training_course_id = cc.training_course_id JOIN ontology_concepts o ON o.concept_id = cc.concept_id WHERE cc.link_id IS NOT NULL {inherited_filter})
          + (SELECT COUNT(*) FROM ncs_training_course_element_links l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN competency_elements e ON e.element_id = l.element_id WHERE l.link_id IS NOT NULL)
          + (SELECT COUNT(*) FROM training_goal_concept_links l JOIN ncs_training_courses c ON c.training_course_id = l.training_course_id JOIN ontology_concepts o ON o.concept_id = l.concept_id WHERE l.link_id IS NOT NULL)
          + (SELECT COUNT(*) FROM training_delivery_relations d JOIN ncs_training_courses c ON c.training_course_id = d.training_course_id WHERE d.relation_id IS NOT NULL),
          (SELECT COUNT(*) FROM direct),
          (SELECT COUNT(*) FROM (SELECT DISTINCT element_id, concept_id FROM element_direct)),
          (SELECT COUNT(*) FROM (SELECT DISTINCT job_key, concept_id FROM job_direct)),
          (SELECT COUNT(*) FROM valid_classifications),
          (SELECT COUNT(*) FROM ncs_training_course_concept_links cc JOIN ncs_training_courses c ON c.training_course_id = cc.training_course_id JOIN ontology_concepts o ON o.concept_id = cc.concept_id WHERE cc.link_id IS NOT NULL {inherited_filter})
        """
    ).fetchone()
    names = ("nodes", "edges", "direct_edges", "element_summary", "job_summary", "valid_jobs", "course_concept_edges")
    return {name: int(row[index] or 0) for index, name in enumerate(names)}, None


def _serving_core_training_concept_link_count(
    conn: sqlite3.Connection, columns: Mapping[str, set[str]], total: int
) -> tuple[int, int]:
    """Count course-concept edges retained by the serving-core projector.

    Gold LPG v2 omits the inherited unit-KSA expansion only when the deployed
    source schema can distinguish it through ``link_method``.  Older schemas
    intentionally retain their full count because the preflight must not guess
    which rows are inherited.
    """

    table = "ncs_training_course_concept_links"
    if table not in columns or "link_method" not in columns[table]:
        return total, 0
    included = int(conn.execute(
        "SELECT COUNT(*) FROM ncs_training_course_concept_links "
        "WHERE link_method IS NULL OR TRIM(link_method) != ?",
        ("unit_ksa_concept_inherited",),
    ).fetchone()[0])
    return included, total - included


def _risk(
    estimates: Mapping[str, int | float], thresholds: GoldReadinessThresholds
) -> tuple[str, list[str], bool]:
    records = int(estimates["estimated_projection_records"])
    peak_bytes = int(estimates["estimated_peak_in_memory_bytes"])
    reasons: list[str] = []
    if records > thresholds.max_in_memory_records:
        reasons.append(
            f"estimated projection records ({records:,}) exceed the in-memory limit "
            f"({thresholds.max_in_memory_records:,})"
        )
    if peak_bytes > thresholds.max_in_memory_bytes:
        reasons.append(
            f"estimated peak memory ({peak_bytes:,} bytes) exceeds the in-memory limit "
            f"({thresholds.max_in_memory_bytes:,} bytes)"
        )
    if reasons:
        return "critical", reasons, False
    if records > thresholds.warning_in_memory_records:
        reasons.append(
            f"estimated projection records ({records:,}) exceed the streaming warning "
            f"({thresholds.warning_in_memory_records:,})"
        )
    if peak_bytes > thresholds.warning_in_memory_bytes:
        reasons.append(
            f"estimated peak memory ({peak_bytes:,} bytes) exceeds the streaming warning "
            f"({thresholds.warning_in_memory_bytes:,} bytes)"
        )
    if reasons:
        return "high", reasons, False
    return "low", ["estimated projection is within conservative in-memory thresholds"], True


def preflight_gold_projection(
    db_path: str | Path,
    *,
    profile: GoldProjectionProfile = SERVING_CORE_PROFILE,
    thresholds: GoldReadinessThresholds | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, count-only safety decision for Gold export.

    No source rows, credentials, or environment values are included in the
    result.  Missing optional tables are reported as unavailable rather than
    treated as errors.  A critical/high decision requires a streaming exporter
    (or a smaller profile); this function never starts an export itself.
    """

    if not isinstance(profile, GoldProjectionProfile):
        raise TypeError("profile must be a GoldProjectionProfile")
    effective_thresholds = _merge_thresholds(thresholds)
    path = Path(db_path)
    with closing(_readonly_connection(path)) as conn:
        columns = _table_columns(conn)
        table_counts: dict[str, dict[str, Any]] = {}
        optional_unavailable: list[str] = []
        for table, (flag, optional) in RELEVANT_TABLES.items():
            present = table in columns
            entry: dict[str, Any] = {
                "present": present,
                "row_count": _table_count(conn, table) if present else 0,
                "profile_enabled": bool(getattr(profile, flag)),
                "optional": optional,
            }
            if not present:
                entry["unavailable_reason"] = "table_not_present"
                if optional:
                    optional_unavailable.append(table)
            table_counts[table] = entry

        serving_core = profile.name == SERVING_CORE_PROFILE.name
        summary_count = 0
        summary_reason: str | None = None
        if profile.include_element_concept_summary:
            summary_count, summary_reason = _distinct_element_concept_count(conn, columns)
        job_concept_summary_count = 0
        job_concept_summary_reason: str | None = None
        valid_classification_job_rows = 0
        classification_job_reason: str | None = None
        if serving_core and profile.include_criteria_concept_links:
            job_concept_summary_count, job_concept_summary_reason = (
                _distinct_job_concept_summary_count(conn, columns)
            )
        if serving_core and profile.include_classifications:
            valid_classification_job_rows, classification_job_reason = _valid_classification_job_count(
                conn, columns
            )

        counts = {name: int(details["row_count"]) for name, details in table_counts.items()}
        retained_training_concept_links = counts["ncs_training_course_concept_links"]
        omitted_inherited_training_concept_links = 0
        if serving_core and profile.include_training_concept_links:
            retained_training_concept_links, omitted_inherited_training_concept_links = (
                _serving_core_training_concept_link_count(
                    conn, columns, retained_training_concept_links
                )
            )
        serving_counts: dict[str, int] | None = None
        serving_counts_reason: str | None = None
        if serving_core and profile == SERVING_CORE_PROFILE:
            serving_counts, serving_counts_reason = _serving_core_emission_counts(conn, columns)
        nodes = 0
        edges = 0
        if profile.include_classifications:
            nodes += counts["classifications"] * 5
            # Serving core keeps HAS_SUB_CATEGORY but removes one duplicate
            # HAS_NCS_JOB edge for every classification that resolves a job.
            edges += (
                counts["classifications"] * 5 - valid_classification_job_rows
                if serving_core
                else counts["classifications"] * 5
            )
        if profile.include_competency_units:
            nodes += counts["competency_units"]
            edges += counts["competency_units"]
        if profile.include_competency_elements:
            nodes += counts["competency_elements"]
            # v2 serving core retains only Unit-DEFINED_BY-Element.
            edges += counts["competency_elements"] if serving_core else counts["competency_elements"] * 2
        if profile.include_performance_criteria:
            # A serving-core criterion is co-labelled PerformanceCriterion:Task;
            # it does not materialise a second Task node or REPRESENTS_TASK edge.
            nodes += counts["performance_criteria"] if serving_core else counts["performance_criteria"] * 2
            edges += counts["performance_criteria"] if serving_core else counts["performance_criteria"] * 2
        if profile.include_ontology_concepts:
            nodes += counts["ontology_concepts"]
        if profile.include_criteria_concept_links:
            # v2 emits exactly one direct criterion-to-concept edge per link.
            edges += counts["criteria_concept_links"] if serving_core else counts["criteria_concept_links"] * 2
        if profile.include_element_concept_summary:
            edges += summary_count
        if serving_core and profile.include_criteria_concept_links:
            edges += job_concept_summary_count
        if not serving_core and profile.include_task_ksa_detailed_relations:
            edges += counts["task_ksa_concept_relations"]
        if profile.include_training_courses:
            nodes += counts["ncs_training_courses"]
        if profile.include_training_unit_links:
            edges += counts["ncs_training_course_unit_links"]
        if profile.include_training_concept_links:
            edges += retained_training_concept_links
        if profile.include_training_element_links:
            edges += counts["ncs_training_course_element_links"]
        if profile.include_training_goal_links:
            edges += counts["training_goal_concept_links"]
        if profile.include_training_delivery:
            nodes += counts["training_delivery_relations"]
            edges += counts["training_delivery_relations"]

        if serving_counts is not None:
            nodes = serving_counts["nodes"]
            edges = serving_counts["edges"]
            summary_count = serving_counts["element_summary"]
            job_concept_summary_count = serving_counts["job_summary"]
            valid_classification_job_rows = serving_counts["valid_jobs"]
            retained_training_concept_links = serving_counts["course_concept_edges"]

        source_rows = sum(counts.values())
        projection_records = nodes + edges
        serialized_bytes = (
            nodes * effective_thresholds.node_payload_bytes
            + edges * effective_thresholds.edge_payload_bytes
        )
        peak_bytes = int(serialized_bytes * effective_thresholds.in_memory_amplification)
        estimates: dict[str, int | float] = {
            "source_table_rows": source_rows,
            "estimated_nodes": nodes,
            "estimated_edges": edges,
            "estimated_projection_records": projection_records,
            "record_amplification_ratio": round(projection_records / max(source_rows, 1), 4),
            "estimated_serialized_payload_bytes": serialized_bytes,
            "in_memory_amplification_factor": effective_thresholds.in_memory_amplification,
            "estimated_peak_in_memory_bytes": peak_bytes,
            "distinct_element_concept_summary_edges": summary_count,
            "distinct_job_concept_summary_edges": job_concept_summary_count,
            "valid_classification_job_rows": valid_classification_job_rows,
            "retained_training_concept_link_edges": retained_training_concept_links,
            "omitted_inherited_training_concept_link_rows": omitted_inherited_training_concept_links,
        }
        risk_level, reasons, in_memory_allowed = _risk(estimates, effective_thresholds)
        if serving_core:
            reasons.append(
                "detailed task_ksa_concept_relations are deliberately excluded by the serving_core profile"
            )
        if omitted_inherited_training_concept_links:
            reasons.append(
                "serving_core excludes unit_ksa_concept_inherited course-concept links when link_method is available"
            )
        if summary_reason:
            reasons.append(f"element-to-concept summary unavailable: {summary_reason}")
        if job_concept_summary_reason:
            reasons.append(f"job-to-concept summary unavailable: {job_concept_summary_reason}")
        if classification_job_reason:
            reasons.append(f"classification job-edge adjustment unavailable: {classification_job_reason}")
        if serving_counts_reason:
            reasons.append(f"serving-core exact emission counts unavailable: {serving_counts_reason}")
        if optional_unavailable:
            reasons.append("optional tables unavailable: " + ", ".join(optional_unavailable))

        fallback_table_counts = {
            table: {
                "present": table in columns,
                "row_count": _table_count(conn, table) if table in columns else 0,
            }
            for table in SQLITE_AUTHORITATIVE_FALLBACK_TABLES
        }
        scope_contract = serving_core_scope_contract(
            {**table_counts, **fallback_table_counts},
            profile=profile,
            omitted_inherited_training_concept_links=omitted_inherited_training_concept_links,
        )

        streaming_required = not in_memory_allowed
        return {
            "schema": GOLD_READINESS_SCHEMA,
            "read_only": True,
            "db_writes": False,
            "status_mutation": False,
            "profile": profile.to_dict(),
            "thresholds": effective_thresholds.to_dict(),
            "source_fingerprint": _source_fingerprint(conn, path),
            "table_counts": table_counts,
            "scope_contract": scope_contract,
            "estimates": estimates,
            "risk_level": risk_level,
            "risk_reasons": reasons,
            "recommended_execution": {
                "mode": "in_memory" if in_memory_allowed else "streaming_required",
                "streaming_required": streaming_required,
                "recommended_batch_size": effective_thresholds.streaming_batch_size if streaming_required else None,
            },
            "in_memory_export_allowed": in_memory_allowed,
        }


# Short alias for callers that use the report as a readiness surface.
gold_projection_readiness = preflight_gold_projection


__all__ = [
    "GOLD_READINESS_SCHEMA",
    "GOLD_SCOPE_CONTRACT_SCHEMA",
    "GoldProjectionProfile",
    "GoldReadinessThresholds",
    "SERVING_CORE_PROFILE",
    "gold_projection_readiness",
    "preflight_gold_projection",
    "serving_core_scope_contract",
]
