from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence


PLAN_SCHEMA = "ncs_source_change_plan_v1"


@dataclass(frozen=True)
class ProjectionColumn:
    """A deterministic field projected from a source table or join."""

    name: str
    expression: str


@dataclass(frozen=True)
class TableSpec:
    """Stable-key comparison contract for one logical source table."""

    name: str
    from_sql: str
    key_columns: tuple[ProjectionColumn, ...]
    content_columns: tuple[ProjectionColumn, ...]
    scope_columns: tuple[ProjectionColumn, ...] = ()
    schema_tables: tuple[str, ...] = ()

    @property
    def required_schema_tables(self) -> tuple[str, ...]:
        return self.schema_tables or (self.name,)


def _column(name: str, expression: str | None = None) -> ProjectionColumn:
    return ProjectionColumn(name=name, expression=expression or f't."{name}"')


# Only source and API evidence are compared here. Ontology/link tables are
# derived outputs and belong to the later rebuild phase selected by this plan.
DEFAULT_TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        name="classifications",
        from_sql='"classifications" AS t',
        key_columns=tuple(
            _column(name)
            for name in ("major_code", "middle_code", "small_code", "sub_code")
        ),
        content_columns=tuple(
            _column(name)
            for name in (
                "major_name",
                "middle_name",
                "small_name",
                "sub_name",
                "duty_def_api",
                "duty_order",
                "api_ncs_degr",
                "api_usg_yn",
            )
        ),
        scope_columns=(_column("major_code"),),
    ),
    TableSpec(
        name="competency_units",
        from_sql='"competency_units" AS t JOIN "classifications" AS c ON c."classification_id" = t."classification_id"',
        key_columns=(_column("unit_code"),),
        content_columns=(
            _column("base_unit_code"),
            _column("unit_version"),
            _column("unit_name_raw"),
            _column("unit_level_raw"),
            ProjectionColumn("major_code", 'c."major_code"'),
            ProjectionColumn("middle_code", 'c."middle_code"'),
            ProjectionColumn("small_code", 'c."small_code"'),
            ProjectionColumn("sub_code", 'c."sub_code"'),
            _column("api_unit_name"),
            _column("api_unit_level"),
            _column("api_definition"),
            _column("api_match_status"),
        ),
        scope_columns=(
            _column("unit_code"),
            ProjectionColumn("major_code", 'c."major_code"'),
        ),
        schema_tables=("competency_units", "classifications"),
    ),
    TableSpec(
        name="competency_elements",
        from_sql='"competency_elements" AS t',
        key_columns=(_column("unit_code"), _column("element_code_raw")),
        content_columns=tuple(
            _column(name)
            for name in (
                "element_no",
                "element_name_raw",
                "element_level_raw",
                "api_element_name",
                "api_element_level",
                "api_match_status",
            )
        ),
        scope_columns=(_column("unit_code"),),
    ),
    TableSpec(
        name="performance_criteria",
        from_sql=(
            '"performance_criteria" AS t '
            'JOIN "competency_elements" AS e ON e."element_id" = t."element_id"'
        ),
        key_columns=(
            ProjectionColumn("unit_code", 'e."unit_code"'),
            ProjectionColumn("element_code_raw", 'e."element_code_raw"'),
            _column("criteria_no"),
        ),
        content_columns=(_column("criteria_text_raw"),),
        scope_columns=(ProjectionColumn("unit_code", 'e."unit_code"'),),
        schema_tables=("performance_criteria", "competency_elements"),
    ),
    TableSpec(
        name="ksa_items",
        from_sql='"ksa_items" AS t JOIN "competency_elements" AS e ON e."element_id" = t."element_id"',
        key_columns=(
            ProjectionColumn("unit_code", 'e."unit_code"'),
            ProjectionColumn("element_code_raw", 'e."element_code_raw"'),
            _column("ksa_type_code"),
            _column("ksa_no"),
        ),
        content_columns=(_column("ksa_type_name"), _column("ksa_text_raw")),
        scope_columns=(ProjectionColumn("unit_code", 'e."unit_code"'),),
        schema_tables=("ksa_items", "competency_elements"),
    ),
    TableSpec(
        name="api_competency_units",
        from_sql='"api_competency_units" AS t',
        key_columns=(_column("ncs_cl_cd"),),
        content_columns=tuple(
            _column(name)
            for name in (
                "compe_unit_name",
                "compe_unit_level",
                "ncs_lclas_cdnm",
                "ncs_mclas_cdnm",
                "ncs_sclas_cdnm",
                "ncs_subd_cdnm",
                "compe_unit_def",
            )
        ),
        scope_columns=(ProjectionColumn("unit_code", 't."ncs_cl_cd"'),),
    ),
    TableSpec(
        name="ncs_training_courses",
        from_sql='"ncs_training_courses" AS t',
        key_columns=tuple(
            _column(name)
            for name in (
                "ncs_cl_cd",
                "train_goal",
                "train_time",
                "fac_name",
                "meth_name",
            )
        ),
        content_columns=tuple(
            _column(name)
            for name in (
                "compe_unit_name",
                "compe_unit_level",
                "ncs_lclas_cd",
                "ncs_lclas_cdnm",
                "ncs_mclas_cd",
                "ncs_mclas_cdnm",
                "ncs_sclas_cd",
                "ncs_sclas_cdnm",
                "ncs_subd_cd",
                "ncs_subd_cdnm",
            )
        ),
        scope_columns=(
            ProjectionColumn("unit_code", 't."ncs_cl_cd"'),
            _column("ncs_lclas_cd"),
        ),
    ),
    TableSpec(
        name="ncs_qualification_items",
        from_sql='"ncs_qualification_items" AS t',
        key_columns=(_column("jm_cd"),),
        content_columns=(_column("jm_nm"), _column("exam_insti_nm")),
    ),
    TableSpec(
        name="ncs_unit_qualification_links",
        from_sql='"ncs_unit_qualification_links" AS t',
        key_columns=tuple(
            _column(name)
            for name in (
                "unit_code",
                "jm_cd",
                "organ_std_ver_cd",
                "ablt_unit_typ_cd",
                "min_edu_trng_tm",
            )
        ),
        content_columns=tuple(
            _column(name)
            for name in (
                "edu_trng_std_tm_sum",
                "job_basis_ablt_std_tm",
                "mand_ablt_unit_std_tm",
                "sel_ablt_unit_std_tm",
                "compe_unit_name",
                "ablt_unit_typ_nm",
                "link_method",
                "confidence_score",
            )
        ),
        scope_columns=(_column("unit_code"),),
    ),
    TableSpec(
        name="ncs_job_base_competencies",
        from_sql='"ncs_job_base_competencies" AS t',
        key_columns=(_column("normalized_key"),),
        content_columns=(_column("competency_name"),),
    ),
    TableSpec(
        name="ncs_job_base_factors",
        from_sql=(
            '"ncs_job_base_factors" AS t JOIN "ncs_job_base_competencies" AS j '
            'ON j."job_base_competency_id" = t."job_base_competency_id"'
        ),
        key_columns=(
            ProjectionColumn("competency_key", 'j."normalized_key"'),
            _column("normalized_key"),
        ),
        content_columns=(_column("factor_name"),),
        schema_tables=("ncs_job_base_factors", "ncs_job_base_competencies"),
    ),
    TableSpec(
        name="ncs_unit_job_base_links",
        from_sql=(
            '"ncs_unit_job_base_links" AS t '
            'JOIN "ncs_job_base_competencies" AS j '
            'ON j."job_base_competency_id" = t."job_base_competency_id" '
            'LEFT JOIN "ncs_job_base_factors" AS f '
            'ON f."job_base_factor_id" = t."job_base_factor_id"'
        ),
        key_columns=(
            _column("unit_code"),
            ProjectionColumn("competency_key", 'j."normalized_key"'),
            ProjectionColumn("factor_key", 'f."normalized_key"'),
        ),
        content_columns=tuple(
            _column(name)
            for name in (
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
            )
        ),
        scope_columns=(_column("unit_code"), _column("ncs_lclas_cd")),
        schema_tables=(
            "ncs_unit_job_base_links",
            "ncs_job_base_competencies",
            "ncs_job_base_factors",
        ),
    ),
    TableSpec(
        name="ncs_career_paths",
        from_sql='"ncs_career_paths" AS t',
        key_columns=tuple(
            _column(name)
            for name in (
                "major_code_raw",
                "middle_code_raw",
                "small_code_raw",
                "job_code_raw",
                "competency_code_raw",
                "position_level_raw",
                "position_name",
            )
        ),
        content_columns=tuple(
            _column(name)
            for name in (
                "job_name",
                "competency_level_raw",
                "competency_name",
                "major_code",
                "middle_code",
                "small_code",
                "sub_code",
                "matched_unit_code",
                "classification_match_method",
                "unit_match_method",
                "confidence_score",
            )
        ),
        scope_columns=(_column("matched_unit_code"), _column("major_code")),
    ),
)


@dataclass(frozen=True)
class _ProjectedRow:
    key: tuple[Any, ...]
    order_key: tuple[str, ...]
    content_hash: str
    scopes: tuple[Any, ...]


class _BoundedScopes:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.values: dict[str, set[str]] = {}
        self.truncated: set[str] = set()

    def add(self, name: str, value: Any) -> None:
        if value is None or value == "":
            return
        rendered = str(value)
        bucket = self.values.setdefault(name, set())
        if rendered in bucket:
            return
        if len(bucket) >= self.limit:
            self.truncated.add(name)
            return
        bucket.add(rendered)

    def as_dict(self) -> dict[str, dict[str, Any]]:
        names = sorted(set(self.values) | self.truncated)
        return {
            name: {
                "values": sorted(self.values.get(name, set())),
                "captured_count": len(self.values.get(name, set())),
                "truncated": name in self.truncated,
            }
            for name in names
        }


def _connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_schema(
    connection: sqlite3.Connection, table_name: str
) -> list[dict[str, Any]]:
    return [
        {
            "cid": int(row[0]),
            "name": str(row[1]),
            "type": str(row[2]),
            "notnull": int(row[3]),
            "default": row[4],
            "pk": int(row[5]),
        }
        for row in connection.execute(
            f"PRAGMA table_info({_quote_identifier(table_name)})"
        )
    ]


def _schema_hash(schema: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        schema, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tagged_value(value: Any) -> Any:
    if value is None:
        return ["null", None]
    if isinstance(value, bytes):
        return ["bytes", value.hex()]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value.hex()]
    return ["text", str(value)]


def _hash_content(names: Sequence[str], values: Sequence[Any]) -> str:
    ordered_payload = [
        [name, _tagged_value(value)] for name, value in zip(names, values, strict=True)
    ]
    canonical = json.dumps(ordered_payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _order_expression(expression: str) -> str:
    # Stable textual ordering that keeps NULL distinct from the empty string.
    return (
        f"CASE WHEN {expression} IS NULL THEN '0:' "
        f"ELSE '1:' || typeof({expression}) || ':' || CAST({expression} AS TEXT) END"
    )


def _projection_sql(spec: TableSpec, *, order: bool) -> str:
    fields: list[str] = []
    for index, column in enumerate(spec.key_columns):
        fields.append(f'{column.expression} AS "__key_{index}"')
    for index, column in enumerate(spec.content_columns):
        fields.append(f'{column.expression} AS "__content_{index}"')
    for index, column in enumerate(spec.scope_columns):
        fields.append(f'{column.expression} AS "__scope_{index}"')
    for index, column in enumerate(spec.key_columns):
        fields.append(f'{_order_expression(column.expression)} AS "__order_{index}"')
    sql = f"SELECT {', '.join(fields)} FROM {spec.from_sql}"
    if order:
        sql += " ORDER BY " + ", ".join(
            f'"__order_{index}"' for index in range(len(spec.key_columns))
        )
    return sql


def _duplicate_key(
    connection: sqlite3.Connection, spec: TableSpec
) -> dict[str, Any] | None:
    projection = _projection_sql(spec, order=False)
    key_aliases = [f'"__key_{index}"' for index in range(len(spec.key_columns))]
    sql = (
        f"SELECT {', '.join(key_aliases)}, COUNT(*) AS duplicate_count "
        f"FROM ({projection}) GROUP BY {', '.join(key_aliases)} "
        "HAVING COUNT(*) > 1 LIMIT 1"
    )
    row = connection.execute(sql).fetchone()
    if row is None:
        return None
    return {
        "key": {
            column.name: row[index] for index, column in enumerate(spec.key_columns)
        },
        "count": int(row[-1]),
    }


def _iter_projected_rows(
    connection: sqlite3.Connection, spec: TableSpec
) -> Iterator[_ProjectedRow]:
    key_count = len(spec.key_columns)
    content_count = len(spec.content_columns)
    scope_count = len(spec.scope_columns)
    content_names = [column.name for column in spec.content_columns]
    cursor = connection.execute(_projection_sql(spec, order=True))
    for row in cursor:
        key = tuple(row[:key_count])
        content_start = key_count
        content_end = content_start + content_count
        scope_end = content_end + scope_count
        content = row[content_start:content_end]
        scopes = tuple(row[content_end:scope_end])
        order_key = tuple(str(value) for value in row[scope_end:])
        yield _ProjectedRow(
            key=key,
            order_key=order_key,
            content_hash=_hash_content(content_names, content),
            scopes=scopes,
        )


def _next_or_none(iterator: Iterator[_ProjectedRow]) -> _ProjectedRow | None:
    try:
        return next(iterator)
    except StopIteration:
        return None


def _render_key(spec: TableSpec, row: _ProjectedRow) -> dict[str, Any]:
    return {
        column.name: row.key[index] for index, column in enumerate(spec.key_columns)
    }


def _record_scopes(
    collector: _BoundedScopes, spec: TableSpec, row: _ProjectedRow
) -> None:
    for index, column in enumerate(spec.scope_columns):
        collector.add(column.name, row.scopes[index])


def _compare_table(
    baseline: sqlite3.Connection,
    candidate: sqlite3.Connection,
    spec: TableSpec,
    *,
    sample_limit: int,
    scope_limit: int,
) -> dict[str, Any]:
    baseline_rows = _iter_projected_rows(baseline, spec)
    candidate_rows = _iter_projected_rows(candidate, spec)
    old = _next_or_none(baseline_rows)
    new = _next_or_none(candidate_rows)
    counts = {"baseline": 0, "candidate": 0, "inserted": 0, "updated": 0, "deleted": 0}
    samples: dict[str, list[dict[str, Any]]] = {
        "inserted": [],
        "updated": [],
        "deleted": [],
    }
    scopes = _BoundedScopes(scope_limit)

    while old is not None or new is not None:
        if old is None:
            counts["candidate"] += 1
            counts["inserted"] += 1
            if len(samples["inserted"]) < sample_limit:
                samples["inserted"].append(_render_key(spec, new))
            _record_scopes(scopes, spec, new)
            new = _next_or_none(candidate_rows)
            continue
        if new is None:
            counts["baseline"] += 1
            counts["deleted"] += 1
            if len(samples["deleted"]) < sample_limit:
                samples["deleted"].append(_render_key(spec, old))
            _record_scopes(scopes, spec, old)
            old = _next_or_none(baseline_rows)
            continue
        if old.order_key < new.order_key:
            counts["baseline"] += 1
            counts["deleted"] += 1
            if len(samples["deleted"]) < sample_limit:
                samples["deleted"].append(_render_key(spec, old))
            _record_scopes(scopes, spec, old)
            old = _next_or_none(baseline_rows)
            continue
        if new.order_key < old.order_key:
            counts["candidate"] += 1
            counts["inserted"] += 1
            if len(samples["inserted"]) < sample_limit:
                samples["inserted"].append(_render_key(spec, new))
            _record_scopes(scopes, spec, new)
            new = _next_or_none(candidate_rows)
            continue

        counts["baseline"] += 1
        counts["candidate"] += 1
        if old.content_hash != new.content_hash:
            counts["updated"] += 1
            if len(samples["updated"]) < sample_limit:
                samples["updated"].append(_render_key(spec, new))
            _record_scopes(scopes, spec, old)
            _record_scopes(scopes, spec, new)
        old = _next_or_none(baseline_rows)
        new = _next_or_none(candidate_rows)

    changed = counts["inserted"] + counts["updated"] + counts["deleted"]
    denominator = max(counts["baseline"], counts["candidate"], 1)
    return {
        "table": spec.name,
        "status": "changed" if changed else "unchanged",
        "key_columns": [column.name for column in spec.key_columns],
        "content_hash_columns": [column.name for column in spec.content_columns],
        "counts": {**counts, "changed": changed},
        "change_ratio": changed / denominator,
        "change_samples": samples,
        "affected_scopes": scopes.as_dict(),
    }


def build_source_change_plan(
    baseline_db: str | Path,
    candidate_db: str | Path,
    *,
    table_specs: Sequence[TableSpec] = DEFAULT_TABLE_SPECS,
    full_rebuild_change_ratio_threshold: float = 0.10,
    per_table_change_ratio_threshold: float = 0.25,
    minimum_table_changes_for_fallback: int = 500,
    sample_limit: int = 10,
    scope_limit: int = 5_000,
) -> dict[str, Any]:
    """Create a read-only, deterministic source change plan for two NCS DBs.

    Structural incompatibility makes a full rebuild mandatory. Large but valid
    data changes only recommend a full rebuild, allowing the caller to retain a
    guarded incremental path for normal small NCS releases.
    """

    if not 0 <= full_rebuild_change_ratio_threshold <= 1:
        raise ValueError("full_rebuild_change_ratio_threshold must be between 0 and 1")
    if not 0 <= per_table_change_ratio_threshold <= 1:
        raise ValueError("per_table_change_ratio_threshold must be between 0 and 1")
    if minimum_table_changes_for_fallback < 1:
        raise ValueError("minimum_table_changes_for_fallback must be positive")
    if sample_limit < 0 or scope_limit < 1:
        raise ValueError(
            "sample_limit must be non-negative and scope_limit must be positive"
        )

    baseline_path = Path(baseline_db)
    candidate_path = Path(candidate_db)
    structural_reasons: list[dict[str, Any]] = []
    recommendation_reasons: list[dict[str, Any]] = []
    table_results: list[dict[str, Any]] = []
    checked_schema_pairs: set[tuple[str, str]] = set()

    with (
        closing(_connect_readonly(baseline_path)) as baseline,
        closing(_connect_readonly(candidate_path)) as candidate,
    ):
        for spec in table_specs:
            table_structural_error = False
            schema_evidence: dict[str, Any] = {}
            for table_name in spec.required_schema_tables:
                pair_key = (spec.name, table_name)
                if pair_key in checked_schema_pairs:
                    continue
                checked_schema_pairs.add(pair_key)
                baseline_exists = _table_exists(baseline, table_name)
                candidate_exists = _table_exists(candidate, table_name)
                if not baseline_exists or not candidate_exists:
                    structural_reasons.append(
                        {
                            "code": "missing_table",
                            "logical_table": spec.name,
                            "table": table_name,
                            "baseline_exists": baseline_exists,
                            "candidate_exists": candidate_exists,
                        }
                    )
                    table_structural_error = True
                    continue
                baseline_schema = _table_schema(baseline, table_name)
                candidate_schema = _table_schema(candidate, table_name)
                schema_evidence[table_name] = {
                    "baseline_hash": _schema_hash(baseline_schema),
                    "candidate_hash": _schema_hash(candidate_schema),
                    "matches": baseline_schema == candidate_schema,
                }
                if baseline_schema != candidate_schema:
                    structural_reasons.append(
                        {
                            "code": "schema_mismatch",
                            "logical_table": spec.name,
                            "table": table_name,
                            "baseline_schema": baseline_schema,
                            "candidate_schema": candidate_schema,
                        }
                    )
                    table_structural_error = True

            if table_structural_error:
                table_results.append(
                    {
                        "table": spec.name,
                        "status": "structural_error",
                        "schema": schema_evidence,
                    }
                )
                continue

            try:
                baseline_duplicate = _duplicate_key(baseline, spec)
                candidate_duplicate = _duplicate_key(candidate, spec)
            except sqlite3.Error as exc:
                structural_reasons.append(
                    {
                        "code": "projection_error",
                        "logical_table": spec.name,
                        "detail": str(exc),
                    }
                )
                table_results.append(
                    {
                        "table": spec.name,
                        "status": "structural_error",
                        "schema": schema_evidence,
                    }
                )
                continue

            if baseline_duplicate is not None or candidate_duplicate is not None:
                structural_reasons.append(
                    {
                        "code": "duplicate_stable_key",
                        "logical_table": spec.name,
                        "baseline_duplicate": baseline_duplicate,
                        "candidate_duplicate": candidate_duplicate,
                    }
                )
                table_results.append(
                    {
                        "table": spec.name,
                        "status": "duplicate_stable_key",
                        "schema": schema_evidence,
                    }
                )
                continue

            result = _compare_table(
                baseline,
                candidate,
                spec,
                sample_limit=sample_limit,
                scope_limit=scope_limit,
            )
            result["schema"] = schema_evidence
            table_results.append(result)
            if (
                result["counts"]["changed"] >= minimum_table_changes_for_fallback
                and result["change_ratio"] > per_table_change_ratio_threshold
            ):
                recommendation_reasons.append(
                    {
                        "code": "table_change_ratio_exceeded",
                        "table": spec.name,
                        "changed": result["counts"]["changed"],
                        "change_ratio": result["change_ratio"],
                        "threshold": per_table_change_ratio_threshold,
                    }
                )

    comparable_results = [result for result in table_results if "counts" in result]
    totals = {
        key: sum(int(result["counts"][key]) for result in comparable_results)
        for key in (
            "baseline",
            "candidate",
            "inserted",
            "updated",
            "deleted",
            "changed",
        )
    }
    overall_ratio = totals["changed"] / max(totals["baseline"], totals["candidate"], 1)
    if totals["changed"] and overall_ratio > full_rebuild_change_ratio_threshold:
        recommendation_reasons.append(
            {
                "code": "overall_change_ratio_exceeded",
                "changed": totals["changed"],
                "change_ratio": overall_ratio,
                "threshold": full_rebuild_change_ratio_threshold,
            }
        )

    full_rebuild_required = bool(structural_reasons)
    full_rebuild_recommended = full_rebuild_required or bool(recommendation_reasons)
    if full_rebuild_recommended:
        strategy = "full_rebuild"
    elif totals["changed"]:
        strategy = "incremental_rebuild"
    else:
        strategy = "no_rebuild"

    return {
        "schema": PLAN_SCHEMA,
        "comparison_mode": "read_only_streaming_merge",
        "row_hash_contract": {
            "algorithm": "sha256",
            "serialization": "ordered_column_name_and_typed_value_json_v1",
            "volatile_and_review_columns_excluded": True,
        },
        "thresholds": {
            "overall_change_ratio": full_rebuild_change_ratio_threshold,
            "per_table_change_ratio": per_table_change_ratio_threshold,
            "minimum_table_changes_for_fallback": minimum_table_changes_for_fallback,
        },
        "full_rebuild_required": full_rebuild_required,
        "full_rebuild_recommended": full_rebuild_recommended,
        "suggested_strategy": strategy,
        "structural_reasons": structural_reasons,
        "recommendation_reasons": recommendation_reasons,
        "totals": {**totals, "change_ratio": overall_ratio},
        "tables": table_results,
        "safety": {
            "database_writes": False,
            "review_status_writes": False,
            "source_rows_modified": False,
        },
    }


__all__ = [
    "DEFAULT_TABLE_SPECS",
    "PLAN_SCHEMA",
    "ProjectionColumn",
    "TableSpec",
    "build_source_change_plan",
]
