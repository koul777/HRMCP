"""Guarded, append-only refresh adapter for selected NCS supplemental APIs.

This module intentionally does *not* publish a Vercel snapshot.  It prepares a
local canonical ``ncs.db`` for the separate snapshot publisher after a data
operator has reviewed the evidence it emits.  The adapter is deliberately
narrow: it can collect all majors for training courses and job-base evidence,
but never reconciles, deletes, or refreshes qualification/element data.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_settings
from .db import connect
from .job_base_api import collect_job_base_competencies
from .training_course_api import collect_training_courses
from .training_recommendation import build_training_course_ontology_links


ALLOWED_SOURCES = ("training-courses", "job-base")
PROHIBITED_SOURCES = frozenset({"qualification", "qualifications", "ncs006", "elements", "element"})
TRUSTED_REVIEW_STATUSES = ("human_reviewed", "accepted", "reviewed")
LOCK_SUFFIX = ".api-refresh.lock"
DEFAULT_STATE_DIR = Path(__file__).resolve().parents[2] / ".state" / "ncs-api-refresh"

TrainingCollector = Callable[..., dict[str, Any]]
JobBaseCollector = Callable[..., dict[str, Any]]
LinkBuilder = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class RefreshCallables:
    """Injection seam for tests; production defaults use the established collectors."""

    collect_training: TrainingCollector = collect_training_courses
    collect_job_base: JobBaseCollector = collect_job_base_competencies
    build_training_links: LinkBuilder = build_training_course_ontology_links


class RefreshLockError(RuntimeError):
    """Raised when an append-only refresh is already in progress for a DB."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _canonical_db_error(db_path: Path) -> str | None:
    """Reject serving/deployment locations and require the local ncs.db filename.

    A test fixture can still use a temporary directory as long as the canonical
    artifact name is ``ncs.db``.  This avoids hard-coding one developer drive
    while preventing accidental execution against Vercel's ephemeral copy.
    """

    if db_path.name.lower() != "ncs.db":
        return "db_path_must_be_named_ncs.db"
    parts = {part.lower() for part in db_path.resolve().parts}
    forbidden = {"deploy", ".vercel", "vercel"}
    if _truthy(os.getenv("VERCEL")):
        forbidden.add("tmp")
    if parts.intersection(forbidden):
        return "db_path_is_not_a_local_refresh_database"
    return None


def _prepared_output_error(source_db: Path, output_db: Path) -> str | None:
    if output_db == source_db:
        return "prepared_output_must_not_be_the_source_db"
    if output_db.suffix.lower() != ".db":
        return "prepared_output_must_be_a_db_file"
    parts = {part.lower() for part in output_db.parts}
    forbidden = {"deploy", ".vercel", "vercel"}
    if _truthy(os.getenv("VERCEL")):
        forbidden.add("tmp")
    if parts.intersection(forbidden):
        return "prepared_output_is_not_a_local_state_path"
    if output_db.exists():
        return "prepared_output_already_exists"
    return None


def _resolve_prepared_output(
    source_db: Path,
    *,
    output_path: Path | None,
    state_dir: Path | None,
) -> tuple[Path | None, str | None]:
    if output_path is not None and state_dir is not None:
        return None, "output_path_and_state_dir_are_mutually_exclusive"
    if output_path is not None:
        candidate = Path(output_path).expanduser().resolve()
    else:
        base_dir = Path(state_dir).expanduser().resolve() if state_dir is not None else DEFAULT_STATE_DIR
        candidate = base_dir / f"ncs_refresh_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.db"
    return candidate, _prepared_output_error(source_db, candidate)


def file_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prepare_working_copy(source_db: Path, prepared_output: Path) -> Path:
    """Create a consistent SQLite snapshot without checkpointing the source DB.

    ``sqlite3.Connection.backup`` reads committed WAL frames as part of its
    snapshot.  A byte copy of ``ncs.db`` would silently omit such frames when
    the source has ``-wal``/``-shm`` sidecars, while checkpointing would mutate
    the immutable source artifact.
    """

    prepared_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = prepared_output.with_name(f"{prepared_output.name}.building-{uuid.uuid4().hex}.tmp")
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(f"file:{source_db.resolve().as_posix()}?mode=ro", uri=True)
        destination_conn = sqlite3.connect(temporary)
        source_conn.backup(destination_conn)
        quick_check = destination_conn.execute("PRAGMA quick_check").fetchall()
        if not quick_check or any(str(row[0]).lower() != "ok" for row in quick_check):
            raise sqlite3.DatabaseError("prepared_working_copy_quick_check_failed")
        destination_conn.close()
        destination_conn = None
        source_conn.close()
        source_conn = None
        temporary.replace(prepared_output)
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            source_conn.close()
        if temporary.exists():
            temporary.unlink()
    return prepared_output


def discover_major_codes(db_path: Path) -> list[str]:
    """Read the complete major-code scope from the database, never from CLI input."""

    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT TRIM(major_code)
            FROM classifications
            WHERE TRIM(COALESCE(major_code, '')) <> ''
            ORDER BY TRIM(major_code)
            """
        ).fetchall()
    finally:
        conn.close()
    return [str(row[0]).zfill(2) for row in rows]


def raw_ksa_sha256(db_path: Path) -> str:
    """Stable source-row digest; raw KSA text is never changed by this adapter."""

    digest = hashlib.sha256()
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        cursor = conn.execute(
            "SELECT ksa_id, ksa_text_raw FROM ksa_items ORDER BY ksa_id"
        )
        for ksa_id, raw_text in cursor:
            digest.update(str(ksa_id).encode("utf-8"))
            digest.update(b"\x1f")
            digest.update(str(raw_text or "").encode("utf-8"))
            digest.update(b"\x1e")
    finally:
        conn.close()
    return digest.hexdigest()


def trusted_review_status_counts(db_path: Path) -> dict[str, int]:
    """Count trusted status values across all explicit review-status columns."""

    counts: dict[str, int] = {}
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        placeholders = ",".join("?" for _ in TRUSTED_REVIEW_STATUSES)
        for (table_name,) in tables:
            quoted_table = '"' + str(table_name).replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            for column in columns:
                column_name = str(column[1])
                if column_name != "review_status" and not column_name.endswith("_review_status"):
                    continue
                quoted_column = '"' + column_name.replace('"', '""') + '"'
                rows = conn.execute(
                    f"SELECT {quoted_column}, COUNT(*) FROM {quoted_table} "
                    f"WHERE {quoted_column} IN ({placeholders}) GROUP BY {quoted_column}",
                    TRUSTED_REVIEW_STATUSES,
                ).fetchall()
                for status, count in rows:
                    counts[f"{table_name}.{column_name}.{status}"] = int(count)
    finally:
        conn.close()
    return dict(sorted(counts.items()))


@contextmanager
def exclusive_refresh_lock(db_path: Path) -> Iterable[Path]:
    """Use O_EXCL so concurrent local refresh jobs cannot interleave writes."""

    lock_path = db_path.with_name(f"{db_path.name}{LOCK_SUFFIX}")
    try:
        descriptor = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RefreshLockError("refresh_lock_already_exists") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"created_at": _utc_now(), "pid": os.getpid()}, handle)
        yield lock_path
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _credentials_from_settings() -> dict[str, str | None]:
    settings = load_settings()
    return {
        "training-courses": settings.training_course_service_key,
        "job-base": settings.job_base_service_key,
    }


def _safe_training_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "pages_processed": int(result.get("pages_processed") or 0),
        "rows_upserted": int(result.get("rows_upserted") or 0),
        "reported_total_count": result.get("reported_total_count"),
        "reported_total_page": result.get("reported_total_page"),
        "training_courses_total": result.get("training_courses_total"),
    }


def _safe_job_base_result(result: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": result.get("ok") is True,
        "pages_processed": int(result.get("pages_processed") or 0),
        "rows_processed": int(result.get("rows_processed") or 0),
        "links_upserted": int(result.get("links_upserted") or 0),
        "missing_local_units": int(result.get("missing_local_units") or 0),
        "reported_total_count": result.get("reported_total_count"),
        "reported_total_page": result.get("reported_total_page"),
        "error_count": int(result.get("error_count") or 0),
    }


def _training_completion_proven(result: Mapping[str, Any]) -> bool:
    """The legacy collector lacks an explicit completion flag, so prove it conservatively."""

    pages = int(result.get("pages_processed") or 0)
    total_page = result.get("reported_total_page")
    if not isinstance(total_page, int) or total_page < 0:
        return False
    return pages >= 1 and pages >= total_page


def _job_base_completion_proven(result: Mapping[str, Any]) -> bool:
    return result.get("ok") is True and int(result.get("error_count") or 0) == 0


def _base_evidence(
    db_path: Path,
    sources: Iterable[str],
    *,
    apply: bool,
    credentials: Mapping[str, str | None],
) -> dict[str, Any]:
    return {
        "schema": "ncs_api_refresh_evidence_v1",
        "started_at": _utc_now(),
        "db_artifact": db_path.name,
        "mode": "apply" if apply else "plan_only",
        "sources": list(sources),
        "credentials_present": {source: bool(credentials.get(source)) for source in sources},
        "limits": {
            "allowed_sources": list(ALLOWED_SOURCES),
            "scope": "all_major_codes_discovered_from_db",
            "module_name": None,
            "page_no": 1,
            "num_of_rows": 500,
            "max_pages": None,
            "append_only": True,
            "reconcile_absent_rows": False,
            "qualification_or_ncs006": "refused",
            "publish_or_deploy": "not_performed",
            "source_db_mutation": "forbidden",
            "working_copy": "required_for_apply",
        },
        "publish_performed": False,
        "deploy_performed": False,
    }


def refresh_ncs_api_evidence(
    db_path: Path,
    *,
    sources: Iterable[str] = ALLOWED_SOURCES,
    apply: bool = False,
    output_path: Path | None = None,
    state_dir: Path | None = None,
    retain_failed_output: bool = False,
    credentials: Mapping[str, str | None] | None = None,
    callables: RefreshCallables | None = None,
) -> dict[str, Any]:
    """Plan or run the narrow append-only supplemental API refresh.

    ``apply=False`` is intentionally the default and never opens a write
    connection.  ``apply=True`` copies the canonical source DB before any
    collector runs; collectors and link building receive only that copy.
    A failed or unprovable source never implies deletion or stale-row cleanup.
    """

    selected_sources = tuple(dict.fromkeys(str(source).strip() for source in sources if str(source).strip()))
    resolved_db = Path(db_path).expanduser().resolve()
    active_credentials = dict(_credentials_from_settings() if credentials is None else credentials)
    evidence = _base_evidence(resolved_db, selected_sources, apply=apply, credentials=active_credentials)
    preflight_errors: list[str] = []
    if not selected_sources:
        preflight_errors.append("at_least_one_source_is_required")
    invalid_sources = [source for source in selected_sources if source not in ALLOWED_SOURCES]
    if invalid_sources:
        preflight_errors.append("unsupported_or_prohibited_sources:" + ",".join(sorted(invalid_sources)))
    db_error = _canonical_db_error(resolved_db)
    if db_error:
        preflight_errors.append(db_error)
    if not resolved_db.is_file():
        preflight_errors.append("canonical_ncs_db_not_found")
    if _truthy(os.getenv("NCS_MCP_READ_ONLY")):
        preflight_errors.append("read_only_environment_refuses_refresh")
    for source in selected_sources:
        if source in ALLOWED_SOURCES and not active_credentials.get(source):
            preflight_errors.append(f"missing_credentials:{source}")
    if preflight_errors:
        evidence.update({"outcome": "blocked_preflight", "preflight_errors": preflight_errors, "finished_at": _utc_now()})
        return evidence

    try:
        major_codes = discover_major_codes(resolved_db)
    except (sqlite3.Error, OSError):
        evidence.update({"outcome": "blocked_preflight", "preflight_errors": ["major_code_discovery_failed"], "finished_at": _utc_now()})
        return evidence
    if not major_codes:
        evidence.update({"outcome": "blocked_preflight", "preflight_errors": ["no_major_codes_discovered"], "finished_at": _utc_now()})
        return evidence
    evidence["major_codes"] = major_codes
    evidence["major_count"] = len(major_codes)
    if not apply:
        evidence.update({"outcome": "plan_only", "writes_performed": False, "finished_at": _utc_now()})
        return evidence

    prepared_output, output_error = _resolve_prepared_output(
        resolved_db,
        output_path=output_path,
        state_dir=state_dir,
    )
    if output_error or prepared_output is None:
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": [output_error or "prepared_output_resolution_failed"],
                "finished_at": _utc_now(),
            }
        )
        return evidence
    evidence["failed_output_policy"] = "retain_for_operator_review" if retain_failed_output else "delete_failed_copy"

    operations = callables or RefreshCallables()
    working_copy_created = False
    try:
        with exclusive_refresh_lock(resolved_db):
            source_before_file_hash = file_sha256(resolved_db)
            source_before_raw_hash = raw_ksa_sha256(resolved_db)
            source_before_trusted = trusted_review_status_counts(resolved_db)
            evidence["source_invariants_before"] = {
                "file_sha256": source_before_file_hash,
                "raw_ksa_sha256": source_before_raw_hash,
                "trusted_review_status_counts": source_before_trusted,
            }
            working_db = _prepare_working_copy(resolved_db, prepared_output)
            working_copy_created = True
            before_raw_hash = raw_ksa_sha256(working_db)
            before_trusted = trusted_review_status_counts(working_db)
            evidence["working_copy_invariants_before"] = {
                "raw_ksa_sha256": before_raw_hash,
                "trusted_review_status_counts": before_trusted,
            }
            source_results: dict[str, list[dict[str, Any]]] = {source: [] for source in selected_sources}
            source_unproven: list[str] = []
            source_failures: list[str] = []
            warnings: list[str] = []

            for source in selected_sources:
                credential = active_credentials[source]
                for major_code in major_codes:
                    try:
                        if source == "training-courses":
                            result = operations.collect_training(
                                working_db,
                                credential,
                                major_code=major_code,
                                module_name=None,
                                page_no=1,
                                num_of_rows=500,
                                max_pages=None,
                            )
                            safe_result = _safe_training_result(result)
                            proven = _training_completion_proven(result)
                        else:
                            result = operations.collect_job_base(
                                working_db,
                                credential,
                                major_code=major_code,
                                module_name=None,
                                page_no=1,
                                num_of_rows=500,
                                max_pages=None,
                            )
                            safe_result = _safe_job_base_result(result)
                            proven = _job_base_completion_proven(result)
                            if safe_result["missing_local_units"]:
                                warnings.append(f"job-base:{major_code}:missing_local_units")
                        source_results[source].append({"major_code": major_code, "completion_proven": proven, **safe_result})
                        if not proven:
                            source_unproven.append(f"{source}:{major_code}")
                    except Exception as exc:  # Preserve later-major evidence; never reconcile failures.
                        source_results[source].append({"major_code": major_code, "completion_proven": False, "error_type": type(exc).__name__})
                        source_failures.append(f"{source}:{major_code}")

            evidence["source_results"] = source_results
            evidence["warnings"] = warnings
            after_collection_raw_hash = raw_ksa_sha256(working_db)
            after_collection_trusted = trusted_review_status_counts(working_db)
            collection_invariants_unchanged = (
                before_raw_hash == after_collection_raw_hash
                and before_trusted == after_collection_trusted
            )
            evidence["invariants_after_collection"] = {
                "raw_ksa_sha256": after_collection_raw_hash,
                "trusted_review_status_counts": after_collection_trusted,
                "unchanged": collection_invariants_unchanged,
            }
            if not collection_invariants_unchanged:
                source_failures.append("source_integrity")
            linked: dict[str, Any] | None = None
            training_fully_proven = (
                "training-courses" in selected_sources
                and not any(item.startswith("training-courses:") for item in source_unproven + source_failures)
                and collection_invariants_unchanged
            )
            if training_fully_proven:
                try:
                    conn = connect(working_db)
                    try:
                        linked = operations.build_training_links(conn, reset=False)
                    finally:
                        conn.close()
                    evidence["training_link_build"] = {"performed": True, "reset": False, "result_keys": sorted(linked.keys())}
                except Exception as exc:
                    source_failures.append("training-links")
                    evidence["training_link_build"] = {"performed": False, "reset": False, "error_type": type(exc).__name__}
            elif "training-courses" in selected_sources:
                evidence["training_link_build"] = {"performed": False, "reset": False, "reason": "training_completion_not_proven"}

            after_raw_hash = raw_ksa_sha256(working_db)
            after_trusted = trusted_review_status_counts(working_db)
            evidence["working_copy_invariants_after"] = {
                "raw_ksa_sha256": after_raw_hash,
                "trusted_review_status_counts": after_trusted,
            }
            invariants_unchanged = before_raw_hash == after_raw_hash and before_trusted == after_trusted
            evidence["working_copy_invariants_unchanged"] = invariants_unchanged
            if not invariants_unchanged:
                evidence["outcome"] = "failed_no_reconcile"
                evidence["invariant_failure"] = "raw_ksa_or_trusted_review_status_changed"
            elif source_failures:
                evidence["outcome"] = "failed_no_reconcile"
            elif source_unproven:
                evidence["outcome"] = "inconclusive_no_publish"
            elif warnings:
                evidence["outcome"] = "completed_with_warnings"
            else:
                evidence["outcome"] = "succeeded_append_only"
            source_after_file_hash = file_sha256(resolved_db)
            source_after_raw_hash = raw_ksa_sha256(resolved_db)
            source_after_trusted = trusted_review_status_counts(resolved_db)
            source_unchanged = (
                source_before_file_hash == source_after_file_hash
                and source_before_raw_hash == source_after_raw_hash
                and source_before_trusted == source_after_trusted
            )
            evidence["source_invariants_after"] = {
                "file_sha256": source_after_file_hash,
                "raw_ksa_sha256": source_after_raw_hash,
                "trusted_review_status_counts": source_after_trusted,
                "unchanged": source_unchanged,
            }
            if not source_unchanged:
                evidence["outcome"] = "failed_no_reconcile"
                evidence["source_integrity_failure"] = "source_db_changed_during_refresh"
            evidence["source_writes_performed"] = False
            evidence["working_copy_writes_performed"] = True
            if evidence["outcome"] in {"succeeded_append_only", "completed_with_warnings"}:
                evidence["prepared_output"] = str(prepared_output)
            elif retain_failed_output:
                evidence["failed_output"] = str(prepared_output)
            else:
                prepared_output.unlink(missing_ok=True)
                working_copy_created = False
                evidence["failed_output_deleted"] = True
    except RefreshLockError:
        evidence.update({"outcome": "blocked_preflight", "preflight_errors": ["refresh_lock_already_exists"], "source_writes_performed": False})
    except (OSError, sqlite3.Error) as exc:
        evidence.update({"outcome": "failed_no_reconcile", "failure_type": type(exc).__name__, "source_writes_performed": False})
        if working_copy_created and prepared_output.exists():
            if retain_failed_output:
                evidence["failed_output"] = str(prepared_output)
            else:
                prepared_output.unlink(missing_ok=True)
                evidence["failed_output_deleted"] = True
    evidence["finished_at"] = _utc_now()
    return evidence


def write_refresh_evidence(report: Mapping[str, Any], output_path: Path) -> Path:
    """Atomically write structured evidence without leaking credentials or API payloads."""

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    temporary_path.replace(destination)
    return destination
