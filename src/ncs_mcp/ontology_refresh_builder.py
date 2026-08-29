from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import Any

from .db import (
    build_task_ksa_concept_relations,
    build_task_similarity_links,
    connect,
    ensure_ncs_ontology_relations,
    ensure_ontology_seeded,
    preprocess_ksa_atomic_items,
)
from .source_change_plan import build_source_change_plan
from .training_recommendation import build_training_course_ontology_links


REPORT_SCHEMA = "ncs_ontology_refresh_report_v1"
MANAGED_POINTER_SCHEMA = "ncs_ontology_refresh_baseline_pointer_v1"
BASELINE_LINEAGE_SCHEMA = "ncs_ontology_refresh_baseline_lineage_v1"
RULE_CONTRACT = {
    "version": 1,
    "atomic_ksa": {"reset": False},
    "task_ksa": {"reset": False},
    "co_required": {"relations_per_concept": 2, "candidate_global_rebuild": True},
    "task_similarity": {
        "max_links_per_task": 10,
        "min_shared_concepts": 2,
        "max_concept_task_frequency": 120,
        "candidate_global_rebuild": True,
    },
    "training_links": {"reset": False},
}
RAW_ONTOLOGY_TABLES = frozenset(
    {
        "classifications",
        "competency_units",
        "competency_elements",
        "performance_criteria",
        "ksa_items",
    }
)
TRAINING_TABLES = frozenset({"ncs_training_courses"})
TRUSTED_STATUSES = ("human_reviewed", "accepted", "reviewed")
TRUSTED_SQL = "'human_reviewed', 'accepted', 'reviewed'"
REQUIRED_SOURCE_TABLE_KEYS = {
    "competency_units": "unit_code",
    "competency_elements": "element_id",
    "performance_criteria": "criteria_id",
    "ksa_items": "ksa_id",
}
REQUIRED_DERIVED_TABLE_KEYS = {
    "ontology_concepts": "concept_id",
    "ksa_concept_links": "link_id",
    "ksa_atomic_items": "atomic_id",
    "task_ksa_concept_relations": "relation_id",
    "task_similarity_links": "similarity_id",
}
ALWAYS_NONEMPTY_DERIVED_TABLES = frozenset(
    {"ontology_concepts", "ksa_concept_links", "ksa_atomic_items"}
)


class RefreshBuilderError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _artifact(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError(path)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _same_artifact(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return expected.get("bytes") == actual.get("bytes") and expected.get(
        "sha256"
    ) == actual.get("sha256")


def _contained_path(root: Path, value: Any, *, label: str) -> Path:
    raw = Path(str(value or ""))
    if not str(raw):
        raise RefreshBuilderError(f"{label} path is missing")
    if raw.is_absolute():
        raise RefreshBuilderError(
            f"{label} path must be relative to the state directory"
        )
    resolved_root = root.resolve(strict=False)
    resolved = (resolved_root / raw).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise RefreshBuilderError(f"{label} path escapes the state directory") from exc
    return resolved


def _read_managed_baseline_pointer(
    state: Path, *, validate_artifacts: bool
) -> tuple[Path, dict[str, Any] | None]:
    pointer_path = state / "current.json"
    if not pointer_path.exists():
        return state / "baseline.db", None
    if pointer_path.is_symlink() or not pointer_path.is_file():
        raise RefreshBuilderError("managed baseline pointer is not a regular file")
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RefreshBuilderError("managed baseline pointer is invalid JSON") from exc
    if pointer.get("schema") != MANAGED_POINTER_SCHEMA:
        raise RefreshBuilderError("managed baseline pointer schema is invalid")
    baseline_record = pointer.get("baseline")
    if not isinstance(baseline_record, dict):
        raise RefreshBuilderError("managed baseline pointer has no baseline artifact")
    baseline = _contained_path(
        state, baseline_record.get("path"), label="managed baseline"
    )
    if baseline.is_symlink() or not baseline.is_file():
        raise RefreshBuilderError(
            "managed baseline pointer target is not a regular file"
        )
    lineage_record = pointer.get("lineage")
    if not isinstance(lineage_record, dict):
        raise RefreshBuilderError("managed baseline pointer has no lineage artifact")
    lineage = _contained_path(
        state, lineage_record.get("path"), label="managed baseline lineage"
    )
    if lineage.is_symlink() or not lineage.is_file():
        raise RefreshBuilderError("managed baseline lineage is not a regular file")
    if validate_artifacts:
        actual_baseline = _artifact(baseline)
        actual_lineage = _artifact(lineage)
        if not _same_artifact(baseline_record, actual_baseline):
            raise RefreshBuilderError(
                "managed baseline pointer hash does not match target"
            )
        if not _same_artifact(lineage_record, actual_lineage):
            raise RefreshBuilderError(
                "managed baseline lineage hash does not match pointer"
            )
        try:
            lineage_payload = json.loads(lineage.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RefreshBuilderError(
                "managed baseline lineage is invalid JSON"
            ) from exc
        if lineage_payload.get("schema") != BASELINE_LINEAGE_SCHEMA:
            raise RefreshBuilderError("managed baseline lineage schema is invalid")
        if not _same_artifact(lineage_payload.get("baseline") or {}, actual_baseline):
            raise RefreshBuilderError("managed baseline lineage target hash is invalid")
        if lineage_payload.get("rule_fingerprint") != pointer.get("rule_fingerprint"):
            raise RefreshBuilderError(
                "managed baseline pointer rule lineage is inconsistent"
            )
    return baseline, pointer


def resolve_managed_baseline(state_dir: str | Path) -> Path:
    """Resolve and validate the promoted baseline, with legacy baseline.db fallback."""
    state = Path(state_dir).expanduser().resolve(strict=False)
    baseline, _pointer = _read_managed_baseline_pointer(state, validate_artifacts=True)
    return baseline


def _sqlite_online_snapshot(source: Path, target: Path) -> None:
    """Copy a coherent SQLite view, including committed WAL pages, atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.snapshot.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_conn:
            with closing(sqlite3.connect(temporary, timeout=30)) as target_conn:
                source_conn.backup(target_conn)
                target_conn.commit()
                quick_check = str(
                    target_conn.execute("PRAGMA quick_check").fetchone()[0]
                )
                if quick_check != "ok":
                    raise RefreshBuilderError(
                        f"SQLite online backup failed quick_check: {quick_check}"
                    )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _rule_fingerprint() -> str:
    payload = json.dumps(
        RULE_CONTRACT, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def _trusted_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ):
        table = str(row[0])
        columns = {
            str(column[1]) for column in conn.execute(f'PRAGMA table_info("{table}")')
        }
        if "review_status" not in columns:
            continue
        counts[table] = int(
            conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE review_status IN ({TRUSTED_SQL})'
            ).fetchone()[0]
        )
    return counts


def _raw_ksa_hash(conn: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    if not _table_exists(conn, "ksa_items"):
        return f"sha256:{digest.hexdigest()}"
    for row in conn.execute(
        "SELECT ksa_id, ksa_text_raw FROM ksa_items ORDER BY ksa_id"
    ):
        payload = json.dumps(
            [row[0], row[1]], ensure_ascii=False, separators=(",", ":")
        )
        digest.update(payload.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _integrity(path: Path) -> dict[str, Any]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        # `quick_check` retains page/link consistency checks without making every
        # routine refresh scan the complete multi-GB database twice.
        result = str(conn.execute("PRAGMA quick_check").fetchone()[0])
        required_keys = {
            **REQUIRED_SOURCE_TABLE_KEYS,
            **REQUIRED_DERIVED_TABLE_KEYS,
        }
        missing = [table for table in required_keys if not _table_exists(conn, table)]
        table_counts: dict[str, dict[str, Any]] = {}
        for table, key in required_keys.items():
            if table in missing:
                table_counts[table] = {
                    "key": key,
                    "row_count": None,
                    "key_count": None,
                    "nonempty": False,
                }
                continue
            row_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            key_count = int(
                conn.execute(
                    f'SELECT COUNT(DISTINCT "{key}") FROM "{table}"'
                ).fetchone()[0]
            )
            table_counts[table] = {
                "key": key,
                "row_count": row_count,
                "key_count": key_count,
                "nonempty": row_count > 0 and key_count > 0,
            }

        source_ksa_count = int(
            (table_counts.get("ksa_items") or {}).get("row_count") or 0
        )
        required_nonempty = set(
            ALWAYS_NONEMPTY_DERIVED_TABLES if source_ksa_count else ()
        )
        task_relation_prerequisite = False
        similarity_prerequisite = False
        if source_ksa_count and not {
            "element_criteria_ksa_links",
            "ksa_items",
        } & set(missing):
            if _table_exists(conn, "element_criteria_ksa_links"):
                task_relation_prerequisite = bool(
                    conn.execute(
                        """
                        SELECT 1
                        FROM element_criteria_ksa_links links
                        JOIN ksa_items ksa ON ksa.ksa_id = links.ksa_id
                        GROUP BY links.criteria_id
                        HAVING COUNT(DISTINCT ksa.ksa_type_name) >= 2
                        LIMIT 1
                        """
                    ).fetchone()
                )
                similarity_prerequisite = bool(
                    conn.execute(
                        """
                        SELECT 1
                        FROM (
                            SELECT
                                left_links.criteria_id AS left_criteria_id,
                                right_links.criteria_id AS right_criteria_id,
                                COUNT(DISTINCT left_ksa.ksa_text_raw) AS shared_ksa_count
                            FROM element_criteria_ksa_links left_links
                            JOIN ksa_items left_ksa ON left_ksa.ksa_id = left_links.ksa_id
                            JOIN ksa_items right_ksa
                              ON right_ksa.ksa_text_raw = left_ksa.ksa_text_raw
                             AND right_ksa.ksa_type_name = left_ksa.ksa_type_name
                            JOIN element_criteria_ksa_links right_links
                              ON right_links.ksa_id = right_ksa.ksa_id
                             AND right_links.criteria_id <> left_links.criteria_id
                            GROUP BY left_links.criteria_id, right_links.criteria_id
                            HAVING shared_ksa_count >= 2
                        )
                        LIMIT 1
                        """
                    ).fetchone()
                )
        if task_relation_prerequisite:
            required_nonempty.add("task_ksa_concept_relations")
        if similarity_prerequisite:
            required_nonempty.add("task_similarity_links")
        empty_required_derived = sorted(
            table
            for table in required_nonempty
            if not (table_counts.get(table) or {}).get("nonempty")
        )
        key_count_failures = sorted(
            table
            for table in required_nonempty
            if (table_counts.get(table) or {}).get("row_count")
            != (table_counts.get(table) or {}).get("key_count")
        )
    return {
        "quick_check": result,
        "missing_required_tables": missing,
        "required_table_counts": table_counts,
        "required_nonempty_derived_tables": sorted(required_nonempty),
        "empty_required_derived_tables": empty_required_derived,
        "derived_key_count_failures": key_count_failures,
        "prerequisites": {
            "source_ksa_nonempty": source_ksa_count > 0,
            "task_ksa_relation_expected": task_relation_prerequisite,
            "task_similarity_expected": similarity_prerequisite,
        },
        "ok": (
            result == "ok"
            and not missing
            and not empty_required_derived
            and not key_count_failures
        ),
    }


def validate_ontology_database(path: str | Path) -> dict[str, Any]:
    return _integrity(Path(path).expanduser().resolve(strict=True))


def _changed_tables(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        table
        for table in plan.get("tables", [])
        if int(table.get("counts", {}).get("changed", 0))
    ]


def _has_truncated_scope(tables: list[dict[str, Any]]) -> bool:
    return any(
        bool(scope.get("truncated"))
        for table in tables
        for scope in table.get("affected_scopes", {}).values()
    )


def _baseline_rule_fingerprint(baseline: Path) -> str | None:
    sidecar = baseline.with_suffix(baseline.suffix + ".refresh.json")
    if not sidecar.is_file():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    value = payload.get("rule_fingerprint")
    return str(value) if value else "invalid"


def _select_strategy(
    plan: dict[str, Any] | None,
    *,
    baseline_exists: bool,
    baseline_rule_fingerprint: str | None,
    rule_fingerprint: str,
) -> tuple[str, list[str], list[dict[str, Any]]]:
    if not baseline_exists:
        return "bootstrap_additive_build", ["managed_baseline_missing"], []
    assert plan is not None
    changed = _changed_tables(plan)
    changed_names = {str(table.get("table")) for table in changed}
    reasons: list[str] = []
    if baseline_rule_fingerprint not in (None, rule_fingerprint):
        reasons.append("ontology_rule_fingerprint_changed")
    if plan.get("full_rebuild_required"):
        reasons.append("source_schema_or_key_contract_changed")
    if _has_truncated_scope(changed):
        reasons.append("affected_scope_truncated")
    destructive_ontology_change = any(
        str(table.get("table")) in RAW_ONTOLOGY_TABLES | TRAINING_TABLES
        and (
            int(table["counts"].get("updated", 0))
            or int(table["counts"].get("deleted", 0))
        )
        for table in changed
    )
    if destructive_ontology_change:
        reasons.append("source_update_or_delete_requires_destructive_reconciliation")
    if reasons:
        return "full_rebuild_required", reasons, changed
    if not changed:
        return "no_rebuild", ["source_projection_unchanged"], changed
    if not changed_names & (RAW_ONTOLOGY_TABLES | TRAINING_TABLES):
        return "supporting_evidence_refresh", ["supporting_evidence_only"], changed
    if plan.get("full_rebuild_recommended"):
        return "full_rebuild_fallback", ["change_threshold_exceeded"], changed
    if changed_names & RAW_ONTOLOGY_TABLES:
        return (
            "incremental_core_append",
            ["small_append_only_raw_ontology_change"],
            changed,
        )
    if changed_names & TRAINING_TABLES:
        return "training_link_append", ["small_append_only_training_change"], changed
    raise AssertionError("unreachable strategy selection")


def _incremental_conflicts(path: Path) -> dict[str, int]:
    with closing(connect(path, read_only=True)) as conn:
        similarity = 0
        co_required = 0
        if _table_exists(conn, "task_similarity_links"):
            similarity = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM task_similarity_links WHERE review_status IN ({TRUSTED_SQL})"
                ).fetchone()[0]
            )
        if _table_exists(conn, "ontology_concept_relations"):
            co_required = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) FROM ontology_concept_relations
                    WHERE relation_type='co_required_in_element'
                      AND review_status IN ({TRUSTED_SQL})
                    """
                ).fetchone()[0]
            )
    return {"task_similarity_links": similarity, "co_required_in_element": co_required}


def _run_pipeline(
    path: Path, *, bootstrap: bool
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stages: list[dict[str, Any]] = []
    conn = connect(path)
    try:
        raw_before = _raw_ksa_hash(conn)
        trusted_before = _trusted_counts(conn)
        stages.append(
            {"name": "ensure_ontology_seeded", "result": ensure_ontology_seeded(conn)}
        )
        stages.append(
            {
                "name": "preprocess_ksa_atomic_items",
                "result": preprocess_ksa_atomic_items(conn, reset=False),
            }
        )
        stages.append(
            {
                "name": "build_task_ksa_concept_relations",
                "result": build_task_ksa_concept_relations(conn, reset=False),
            }
        )
        if not bootstrap:
            conn.execute(
                "DELETE FROM ontology_concept_relations "
                "WHERE relation_type='co_required_in_element' "
                f"AND review_status NOT IN ({TRUSTED_SQL})"
            )
            conn.execute(
                f"DELETE FROM task_similarity_links WHERE review_status NOT IN ({TRUSTED_SQL})"
            )
            conn.commit()
        stages.append(
            {
                "name": "ensure_ncs_ontology_relations",
                "result": ensure_ncs_ontology_relations(conn, reset=False),
            }
        )
        stages.append(
            {
                "name": "build_task_similarity_links",
                "result": build_task_similarity_links(conn, reset=False),
            }
        )
        stages.append(
            {
                "name": "build_training_course_ontology_links",
                "result": build_training_course_ontology_links(conn, reset=False),
            }
        )
        raw_after = _raw_ksa_hash(conn)
        trusted_after = _trusted_counts(conn)
        if raw_after != raw_before:
            raise RefreshBuilderError("raw KSA invariant failed on prepared output")
        if trusted_after != trusted_before:
            raise RefreshBuilderError(
                "trusted review-state counts changed during refresh"
            )
        return stages, {
            "raw_ksa_hash_before": raw_before,
            "raw_ksa_hash_after": raw_after,
            "raw_ksa_preserved": True,
            "trusted_status_counts_before": trusted_before,
            "trusted_status_counts_after": trusted_after,
            "trusted_statuses_preserved": True,
        }
    finally:
        conn.close()


def _run_training_pipeline(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    conn = connect(path)
    try:
        raw_before = _raw_ksa_hash(conn)
        trusted_before = _trusted_counts(conn)
        stages = [
            {
                "name": "build_training_course_ontology_links",
                "result": build_training_course_ontology_links(conn, reset=False),
            }
        ]
        raw_after = _raw_ksa_hash(conn)
        trusted_after = _trusted_counts(conn)
        if raw_after != raw_before or trusted_after != trusted_before:
            raise RefreshBuilderError(
                "training refresh violated source or trusted-state invariants"
            )
        return stages, {
            "raw_ksa_hash_before": raw_before,
            "raw_ksa_hash_after": raw_after,
            "raw_ksa_preserved": True,
            "trusted_status_counts_before": trusted_before,
            "trusted_status_counts_after": trusted_after,
            "trusted_statuses_preserved": True,
        }
    finally:
        conn.close()


def build_ontology_refresh(
    candidate_db: str | Path,
    *,
    baseline_db: str | Path | None = None,
    state_dir: str | Path = ".state/ncs-ontology-refresh",
    prepared_output: str | Path | None = None,
    apply: bool = False,
    full_rebuild_change_ratio_threshold: float = 0.10,
    per_table_change_ratio_threshold: float = 0.25,
    minimum_table_changes_for_fallback: int = 500,
) -> dict[str, Any]:
    """Plan or safely prepare an ontology refresh from one candidate NCS DB.

    The source and managed baseline are opened read-only or copied; neither is
    modified. Destructive update/delete reconciliation is deliberately blocked.
    """
    candidate = Path(candidate_db).expanduser().resolve(strict=True)
    state = Path(state_dir).expanduser().resolve(strict=False)
    pointer: dict[str, Any] | None = None
    if baseline_db is not None:
        baseline = Path(baseline_db).expanduser().resolve(strict=False)
    else:
        baseline, pointer = _read_managed_baseline_pointer(
            state, validate_artifacts=False
        )
    candidate_before = _artifact(candidate)
    baseline_exists = baseline.is_file()
    baseline_before = _artifact(baseline) if baseline_exists else None
    if pointer is not None:
        if baseline_before is None or not _same_artifact(
            pointer.get("baseline") or {}, baseline_before
        ):
            raise RefreshBuilderError(
                "managed baseline pointer hash does not match target"
            )
        lineage_record = pointer.get("lineage") or {}
        lineage = _contained_path(
            state, lineage_record.get("path"), label="managed baseline lineage"
        )
        actual_lineage = _artifact(lineage)
        if not _same_artifact(lineage_record, actual_lineage):
            raise RefreshBuilderError(
                "managed baseline lineage hash does not match pointer"
            )
        try:
            lineage_payload = json.loads(lineage.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RefreshBuilderError(
                "managed baseline lineage is invalid JSON"
            ) from exc
        if lineage_payload.get("schema") != BASELINE_LINEAGE_SCHEMA:
            raise RefreshBuilderError("managed baseline lineage schema is invalid")
        if not _same_artifact(lineage_payload.get("baseline") or {}, baseline_before):
            raise RefreshBuilderError("managed baseline lineage target hash is invalid")
        if lineage_payload.get("rule_fingerprint") != pointer.get("rule_fingerprint"):
            raise RefreshBuilderError(
                "managed baseline pointer rule lineage is inconsistent"
            )
    rule_fingerprint = _rule_fingerprint()
    baseline_rule = _baseline_rule_fingerprint(baseline) if baseline_exists else None
    if pointer is not None and pointer.get("rule_fingerprint") != baseline_rule:
        raise RefreshBuilderError(
            "managed baseline pointer rule fingerprint does not match lineage"
        )
    change_plan = None
    if baseline_exists:
        change_plan = build_source_change_plan(
            baseline,
            candidate,
            full_rebuild_change_ratio_threshold=full_rebuild_change_ratio_threshold,
            per_table_change_ratio_threshold=per_table_change_ratio_threshold,
            minimum_table_changes_for_fallback=minimum_table_changes_for_fallback,
        )
    strategy, reasons, changed = _select_strategy(
        change_plan,
        baseline_exists=baseline_exists,
        baseline_rule_fingerprint=baseline_rule,
        rule_fingerprint=rule_fingerprint,
    )
    output = (
        Path(prepared_output).expanduser().resolve(strict=False)
        if prepared_output is not None
        else state
        / "prepared"
        / f"ncs-ontology-{candidate_before['sha256'].split(':', 1)[1][:12]}.db"
    )
    conflicts = (
        _incremental_conflicts(candidate)
        if strategy == "incremental_core_append"
        else {}
    )
    if any(conflicts.values()):
        strategy = "incremental_blocked_trusted_rows"
        reasons.append(
            "global_candidate_rebuild_would_overlap_trusted_similarity_or_co_required_rows"
        )

    blocked = strategy in {
        "full_rebuild_required",
        "full_rebuild_fallback",
        "incremental_blocked_trusted_rows",
    }
    stages: list[dict[str, Any]] = []
    invariants: dict[str, Any] = {
        "source_mutated": False,
        "baseline_mutated": False,
        "raw_ksa_preserved": None,
        "review_status_write_allowed": False,
        "api_calls": False,
        "deployment": False,
        "publication": False,
    }
    prepared: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    status = "planned"
    if apply and blocked:
        status = "blocked"
    elif apply and strategy == "no_rebuild":
        validation = _integrity(baseline)
        if validation["ok"]:
            status = "completed"
            with closing(connect(baseline, read_only=True)) as conn:
                baseline_raw_hash = _raw_ksa_hash(conn)
            invariants.update(
                {
                    "raw_ksa_preserved": True,
                    "raw_ksa_hash_before": baseline_raw_hash,
                    "raw_ksa_hash_after": baseline_raw_hash,
                    "trusted_statuses_preserved": True,
                }
            )
        else:
            blocked = True
            status = "blocked"
            reasons.append("managed_baseline_derived_ontology_validation_failed")
    elif apply:
        if output == candidate or output == baseline:
            raise RefreshBuilderError(
                "prepared output must differ from source and baseline"
            )
        if output.exists():
            raise RefreshBuilderError(f"prepared output already exists: {output}")
        _sqlite_online_snapshot(candidate, output)
        try:
            if strategy in {"bootstrap_additive_build", "incremental_core_append"}:
                stages, pipeline_invariants = _run_pipeline(
                    output, bootstrap=strategy == "bootstrap_additive_build"
                )
                invariants.update(pipeline_invariants)
            elif strategy == "training_link_append":
                stages, pipeline_invariants = _run_training_pipeline(output)
                invariants.update(pipeline_invariants)
            else:
                with closing(connect(output, read_only=True)) as conn:
                    invariants["raw_ksa_preserved"] = True
                    invariants["raw_ksa_hash_before"] = _raw_ksa_hash(conn)
                    invariants["raw_ksa_hash_after"] = invariants["raw_ksa_hash_before"]
            validation = _integrity(output)
            if not validation["ok"]:
                raise RefreshBuilderError(
                    f"prepared output validation failed: {validation}"
                )
            prepared = _artifact(output)
            status = "completed"
        except Exception:
            output.unlink(missing_ok=True)
            raise

    candidate_after = _artifact(candidate)
    if candidate_after != candidate_before:
        raise RefreshBuilderError("candidate source changed during refresh")
    baseline_after = _artifact(baseline) if baseline_exists else None
    if baseline_after != baseline_before:
        raise RefreshBuilderError("managed baseline changed during refresh")
    publisher_source = None
    if apply and status == "completed" and not blocked:
        publisher_source = prepared or baseline_before
    return {
        "schema": REPORT_SCHEMA,
        "ok": status == "completed" if apply else not blocked,
        "mode": "apply" if apply else "plan_only",
        "status": status,
        "source": candidate_before,
        "baseline": baseline_before or {"path": str(baseline), "exists": False},
        "prepared_output": prepared,
        "publisher_source": publisher_source,
        "rule_fingerprint": rule_fingerprint,
        "baseline_rule_fingerprint": baseline_rule,
        "change_plan": change_plan,
        "changed_tables": [table.get("table") for table in changed],
        "selected_strategy": strategy,
        "strategy_reasons": reasons,
        "stages": stages,
        "validation": validation,
        "safety": {
            **invariants,
            "trusted_row_conflicts": conflicts,
            "destructive_reconciliation_supported": False,
            "apply_blocked": blocked,
        },
        "next_publisher_command": (
            f'python scripts\\publish_vercel_snapshot.py --source "{publisher_source["path"]}"'
            if publisher_source is not None
            else None
        ),
        "baseline_promotion": {
            "automatic": False,
            "allowed_by_this_builder": False,
            "reason": "source and managed baseline are immutable inputs",
            "after_verified_publish": {
                "candidate_baseline": (
                    publisher_source["path"] if publisher_source is not None else None
                ),
                "rule_fingerprint": rule_fingerprint,
                "require_operator_confirmation": True,
            },
        },
        "api_refresh": {
            "performed": False,
            "network_calls": False,
            "note": "This builder consumes a supplied DB; API collection and freshness scheduling are separate guarded jobs.",
        },
    }


__all__ = [
    "BASELINE_LINEAGE_SCHEMA",
    "MANAGED_POINTER_SCHEMA",
    "REPORT_SCHEMA",
    "RULE_CONTRACT",
    "RefreshBuilderError",
    "build_ontology_refresh",
    "resolve_managed_baseline",
    "validate_ontology_database",
]
