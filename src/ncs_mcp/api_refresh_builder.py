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
import secrets
import sqlite3
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import load_settings
from .builder_authorization import (
    BuilderAuthorizationError,
    BuilderOperationContext,
    require_builder_context,
)
from .sqlite_diagnostics import is_dbstat_table
from .db import connect
from .job_base_api import collect_job_base_competencies
from .training_course_api import collect_training_courses
from .training_recommendation import build_training_course_ontology_links


ALLOWED_SOURCES = ("training-courses", "job-base")
PROHIBITED_SOURCES = frozenset(
    {"qualification", "qualifications", "ncs006", "elements", "element"}
)
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


def _absolute_path(path: str | Path) -> Path:
    """Make *path* absolute without expanding Windows 8.3 path spelling."""

    return Path(os.path.abspath(Path(path).expanduser()))


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
    if output_db.resolve(strict=False) == source_db.resolve(strict=False):
        return "prepared_output_must_not_be_the_source_db"
    if output_db.suffix.lower() != ".db":
        return "prepared_output_must_be_a_db_file"
    parts = {
        part.lower()
        for candidate in (output_db, output_db.resolve(strict=False))
        for part in candidate.parts
    }
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
        candidate = _absolute_path(output_path)
    else:
        base_dir = (
            _absolute_path(state_dir)
            if state_dir is not None
            else DEFAULT_STATE_DIR
        )
        candidate = (
            base_dir
            / f"ncs_refresh_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.db"
        )
    return candidate, _prepared_output_error(source_db, candidate)


def file_sha256(file_path: Path, progress: Callable | None = None, stage: str = "DB 파일 해시 검사") -> str:
    digest = hashlib.sha256()
    total = Path(file_path).stat().st_size
    completed = 0
    if progress:
        progress({"stage": stage, "completed": 0, "total": total, "unit": "바이트"})
    with Path(file_path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            completed += len(chunk)
            if progress and (completed % (32 * 1024 * 1024) == 0 or completed == total):
                progress({"stage": stage, "completed": completed, "total": total, "unit": "바이트"})
    return digest.hexdigest()


def _prepare_working_copy(
    source_db: Path, prepared_output: Path, progress: Callable | None = None,
    *, authorize: Callable[[], None] | None = None,
) -> Path:
    """Create a consistent SQLite snapshot without checkpointing the source DB.

    ``sqlite3.Connection.backup`` reads committed WAL frames as part of its
    snapshot.  A byte copy of ``ncs.db`` would silently omit such frames when
    the source has ``-wal``/``-shm`` sidecars, while checkpointing would mutate
    the immutable source artifact.
    """

    if authorize:
        authorize()
    prepared_output.parent.mkdir(parents=True, exist_ok=True)
    temporary = prepared_output.with_name(
        f"{prepared_output.name}.building-{uuid.uuid4().hex}.tmp"
    )
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = sqlite3.connect(
            f"file:{source_db.resolve().as_posix()}?mode=ro", uri=True
        )
        if authorize:
            authorize()
        destination_conn = sqlite3.connect(temporary)
        def backup_progress(status: int, remaining: int, total: int) -> None:
            if progress:
                progress({"stage": "작업 DB 복사", "completed": total - remaining,
                          "total": total, "unit": "페이지"})
            if authorize:
                authorize()
        if authorize:
            authorize()
        source_conn.backup(destination_conn, pages=4096, progress=backup_progress)
        if progress:
            progress("복사한 DB 무결성 검사")
        quick_check = destination_conn.execute("PRAGMA quick_check").fetchall()
        if not quick_check or any(str(row[0]).lower() != "ok" for row in quick_check):
            raise sqlite3.DatabaseError("prepared_working_copy_quick_check_failed")
        destination_conn.close()
        destination_conn = None
        source_conn.close()
        source_conn = None
        if authorize:
            authorize()
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
            "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        placeholders = ",".join("?" for _ in TRUSTED_REVIEW_STATUSES)
        for table_name, create_sql in tables:
            if is_dbstat_table(create_sql):
                continue
            quoted_table = '"' + str(table_name).replace('"', '""') + '"'
            columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            for column in columns:
                column_name = str(column[1])
                if column_name != "review_status" and not column_name.endswith(
                    "_review_status"
                ):
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


def _quoted_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def trusted_review_status_identity_digest(db_path: Path) -> dict[str, dict[str, Any]]:
    """Return a stable identity digest for every protected review-status field.

    Counts alone cannot distinguish a trusted status moving from one row to
    another. Every protected field is serialized as its declared primary-key
    tuple plus status, ordered by that key. A protected field with no declared
    primary key is rejected rather than silently using an unstable surrogate.
    """

    protected: dict[str, dict[str, Any]] = {}
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        placeholders = ",".join("?" for _ in TRUSTED_REVIEW_STATUSES)
        for table_name, create_sql in tables:
            if is_dbstat_table(create_sql):
                continue
            table = str(table_name)
            quoted_table = _quoted_identifier(table)
            columns = conn.execute(f"PRAGMA table_info({quoted_table})").fetchall()
            primary_key_columns = [
                str(column[1])
                for column in sorted(columns, key=lambda column: int(column[5] or 0))
                if int(column[5] or 0) > 0
            ]
            status_columns = [
                str(column[1])
                for column in columns
                if str(column[1]) == "review_status"
                or str(column[1]).endswith("_review_status")
            ]
            if status_columns and not primary_key_columns:
                raise sqlite3.DatabaseError(
                    f"trusted_review_status_table_missing_primary_key:{table}"
                )
            for column_name in status_columns:
                quoted_status = _quoted_identifier(column_name)
                quoted_keys = [_quoted_identifier(column) for column in primary_key_columns]
                cursor = conn.execute(
                    f"SELECT {', '.join([*quoted_keys, quoted_status])} "
                    f"FROM {quoted_table} WHERE {quoted_status} IN ({placeholders}) "
                    f"ORDER BY {', '.join(quoted_keys)}",
                    TRUSTED_REVIEW_STATUSES,
                )
                digest = hashlib.sha256()
                header = json.dumps(
                    {
                        "table": table,
                        "review_status_column": column_name,
                        "primary_key_columns": primary_key_columns,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                digest.update(header.encode("utf-8"))
                digest.update(b"\n")
                count = 0
                for row in cursor:
                    digest.update(
                        json.dumps(
                            list(row), ensure_ascii=False, separators=(",", ":")
                        ).encode("utf-8")
                    )
                    digest.update(b"\n")
                    count += 1
                protected[f"{table}.{column_name}"] = {
                    "count": count,
                    "primary_key_columns": primary_key_columns,
                    "sha256": f"sha256:{digest.hexdigest()}",
                }
    finally:
        conn.close()
    return dict(sorted(protected.items()))


@contextmanager
def exclusive_refresh_lock(db_path: Path) -> Iterable[Path]:
    """Publish a complete owner record atomically without replacing another lock.

    The private hard link pins the inode until release. Cleanup checks both that
    identity and the random token; a replaced or edited lock is left intact.
    Path comparison/unlink is not an OS compare-and-delete primitive: an external
    process racing that final pair remains outside this cooperative lock model.
    """

    lock_path = db_path.with_name(f"{db_path.name}{LOCK_SUFFIX}")
    metadata = {"schema": "ncs_api_refresh_lock_v1", "owner_token": secrets.token_hex(32),
                "created_at": _utc_now(), "pid": os.getpid(),
                "source": str(db_path.resolve())}
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{lock_path.name}.", dir=lock_path.parent)
    owner_path = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle)
            handle.flush()
            os.fsync(handle.fileno())
            owner_stat = os.fstat(handle.fileno())
        try:
            os.link(owner_path, lock_path)
        except FileExistsError as exc:
            raise RefreshLockError("refresh_lock_already_exists") from exc
        published = True
        yield lock_path
    finally:
        try:
            if published:
                current = lock_path.lstat()
                if ((current.st_dev, current.st_ino) == (owner_stat.st_dev, owner_stat.st_ino)
                        and not lock_path.is_symlink()
                        and json.loads(lock_path.read_text(encoding="utf-8")) == metadata):
                    lock_path.unlink()
        except (OSError, ValueError):
            pass
        finally:
            owner_path.unlink(missing_ok=True)


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
        "credentials_present": {
            source: bool(credentials.get(source)) for source in sources
        },
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
    progress: Callable | None = None,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
    builder_context: BuilderOperationContext | None = None,
) -> dict[str, Any]:
    """Plan or run the narrow append-only supplemental API refresh.

    ``apply=False`` is intentionally the default and never opens a write
    connection. ``apply=True`` requires an explicit live Builder capability and
    confines outputs/checkpoints to its version directory. It copies the source before any
    collector runs; collectors and link building receive only that copy.
    A failed or unprovable source never implies deletion or stale-row cleanup.
    With ``checkpoint_dir`` and an explicit ``output_path``, proven source-major
    calls are journaled and the working copy is retained. ``resume=True`` checks
    source, parameters, DB identity and original invariants before reusing it.
    Interrupted majors rerun their idempotent upserts; final guards always rerun.
    """

    selected_sources = tuple(
        dict.fromkeys(str(source).strip() for source in sources if str(source).strip())
    )
    resolved_db = Path(db_path).expanduser().resolve()
    if apply:
        try:
            context = require_builder_context(
                builder_context, action="resume" if resume else "refresh_api"
            )
            version_dir = Path(context.state_dir) / "versions" / str(context.version)
            if version_dir.resolve() != version_dir or Path(context.root).resolve() != Path(context.root):
                raise BuilderAuthorizationError("Builder version path was redirected.")
            if state_dir is not None:
                require_builder_context(context, action=context.action,
                                        version_dir=state_dir)
                if output_path is None:
                    state_dir = None
            output_path = (
                Path(output_path).expanduser().resolve()
                if output_path is not None else version_dir / "ncs.db"
            )
            require_builder_context(context, action=context.action,
                                    version_dir=output_path.parent)
            if checkpoint_dir is not None and (
                Path(checkpoint_dir).resolve() != version_dir / "api-checkpoint"
                or (Path(checkpoint_dir) / "api_checkpoint.json").resolve()
                != version_dir / "api-checkpoint" / "api_checkpoint.json"
            ):
                raise BuilderAuthorizationError("Checkpoint is outside this Builder version.")
        except BuilderAuthorizationError:
            return {
                **_base_evidence(resolved_db, selected_sources, apply=True, credentials={}),
                "outcome": "blocked_preflight",
                "preflight_errors": ["builder_authorization_required"],
                "finished_at": _utc_now(),
            }
    active_credentials = dict(
        _credentials_from_settings() if credentials is None else credentials
    )
    evidence = _base_evidence(
        resolved_db, selected_sources, apply=apply, credentials=active_credentials
    )
    preflight_errors: list[str] = []
    if not selected_sources:
        preflight_errors.append("at_least_one_source_is_required")
    invalid_sources = [
        source for source in selected_sources if source not in ALLOWED_SOURCES
    ]
    if invalid_sources:
        preflight_errors.append(
            "unsupported_or_prohibited_sources:" + ",".join(sorted(invalid_sources))
        )
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
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": preflight_errors,
                "finished_at": _utc_now(),
            }
        )
        return evidence

    try:
        major_codes = discover_major_codes(resolved_db)
    except (sqlite3.Error, OSError):
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": ["major_code_discovery_failed"],
                "finished_at": _utc_now(),
            }
        )
        return evidence
    if not major_codes:
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": ["no_major_codes_discovered"],
                "finished_at": _utc_now(),
            }
        )
        return evidence
    evidence["major_codes"] = major_codes
    evidence["major_count"] = len(major_codes)
    if not apply:
        evidence.update(
            {
                "outcome": "plan_only",
                "writes_performed": False,
                "finished_at": _utc_now(),
            }
        )
        return evidence

    checkpoint_path = Path(checkpoint_dir).resolve() / "api_checkpoint.json" if checkpoint_dir else None
    checkpoint: dict[str, Any] | None = None
    if resume and (checkpoint_path is None or output_path is None):
        return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["resume_requires_checkpoint_and_output"]}
    if checkpoint_path and output_path is None:
        return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_requires_output_path"]}
    if checkpoint_path and checkpoint_path.exists():
        if not resume:
            return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_already_exists_use_resume"]}
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if (not isinstance(checkpoint, dict)
                    or not isinstance(checkpoint.get("identity"), dict)
                    or not isinstance(checkpoint.get("completed"), dict)
                    or not all(isinstance(value, dict) and value.get("completion_proven") is True
                               for value in checkpoint["completed"].values())):
                raise ValueError("invalid checkpoint")
        except (OSError, ValueError):
            return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_unreadable"]}
    elif resume:
        return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_missing"]}
    prepared_output, output_error = _resolve_prepared_output(
        resolved_db,
        output_path=output_path,
        state_dir=state_dir,
    )
    if checkpoint is not None and output_error == "prepared_output_already_exists":
        output_error = None
    if checkpoint_path:
        retain_failed_output = True
    if output_error or prepared_output is None:
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": [
                    output_error or "prepared_output_resolution_failed"
                ],
                "finished_at": _utc_now(),
            }
        )
        return evidence
    evidence["failed_output_policy"] = (
        "retain_for_operator_review" if retain_failed_output else "delete_failed_copy"
    )

    operations = callables or RefreshCallables()
    tell = progress or (lambda event: None)
    working_copy_created = False
    phase = "source_invariant_check"
    def authorize() -> None:
        require_builder_context(context, action="resume" if resume else "refresh_api",
                                version_dir=prepared_output.parent)
        if (version_dir.resolve() != version_dir
                or prepared_output.resolve() != prepared_output
                or (checkpoint_path is not None and checkpoint_path.resolve()
                    != version_dir / "api-checkpoint" / "api_checkpoint.json")):
            raise BuilderAuthorizationError("Builder version path was redirected.")

    try:
        authorize()
        with exclusive_refresh_lock(resolved_db):
            source_before_file_hash = file_sha256(resolved_db, progress)
            tell("원본 KSA와 사람 검토 상태 검사")
            source_before_raw_hash = raw_ksa_sha256(resolved_db)
            source_before_trusted = trusted_review_status_counts(resolved_db)
            source_before_trusted_identity = trusted_review_status_identity_digest(
                resolved_db
            )
            evidence["source_invariants_before"] = {
                "file_sha256": source_before_file_hash,
                "raw_ksa_sha256": source_before_raw_hash,
                "trusted_review_status_counts": source_before_trusted,
                "trusted_review_status_identity_digest": source_before_trusted_identity,
            }
            identity = {
                "schema": "ncs_api_checkpoint_v1", "source": str(resolved_db),
                "source_invariants": evidence["source_invariants_before"],
                "source_wal_sha256": file_sha256(Path(str(resolved_db) + "-wal")) if checkpoint_path and Path(str(resolved_db) + "-wal").exists() else None,
                "output": str(prepared_output), "sources": list(selected_sources),
                "major_codes": major_codes, "parameters": evidence["limits"],
            }
            if checkpoint is not None:
                if checkpoint.get("identity") != identity:
                    return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_identity_mismatch"]}
                if not prepared_output.is_file() or prepared_output.stat().st_ino != checkpoint.get("working_inode"):
                    return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_working_db_mismatch"]}
                with closing(sqlite3.connect(f"file:{prepared_output.as_posix()}?mode=ro", uri=True)) as binding:
                    token = binding.execute("SELECT token FROM builder_api_checkpoint_identity").fetchone()
                if not token or token[0] != checkpoint.get("token"):
                    return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_working_db_mismatch"]}
            phase = "working_copy_backup"
            authorize()
            working_db = prepared_output if checkpoint is not None else _prepare_working_copy(
                resolved_db, prepared_output, progress, authorize=authorize)
            working_copy_created = True
            phase = "working_copy_invariant_check"
            tell("작업 DB 원문·검토 상태 검사")
            before_raw_hash = raw_ksa_sha256(working_db)
            before_trusted = trusted_review_status_counts(working_db)
            before_trusted_identity = trusted_review_status_identity_digest(working_db)
            evidence["working_copy_invariants_before"] = {
                "raw_ksa_sha256": before_raw_hash,
                "trusted_review_status_counts": before_trusted,
                "trusted_review_status_identity_digest": before_trusted_identity,
            }
            if checkpoint is not None and checkpoint.get("baseline") != evidence["working_copy_invariants_before"]:
                return {**evidence, "outcome": "blocked_preflight", "preflight_errors": ["checkpoint_invariant_mismatch"]}
            if checkpoint_path and checkpoint is None:
                checkpoint = {"identity": identity, "baseline": evidence["working_copy_invariants_before"],
                              "token": uuid.uuid4().hex, "working_inode": working_db.stat().st_ino, "completed": {}}
                authorize()
                with closing(sqlite3.connect(working_db)) as binding, binding:
                    binding.execute("CREATE TABLE IF NOT EXISTS builder_api_checkpoint_identity(token TEXT NOT NULL)")
                    binding.execute("DELETE FROM builder_api_checkpoint_identity")
                    binding.execute("INSERT INTO builder_api_checkpoint_identity VALUES (?)", (checkpoint["token"],))
                authorize()
                write_refresh_evidence(checkpoint, checkpoint_path, protected_databases=(resolved_db, working_db))
            evidence["resumed"] = resume
            source_results: dict[str, list[dict[str, Any]]] = {
                source: [] for source in selected_sources
            }
            source_unproven: list[str] = []
            source_failures: list[str] = []
            warnings: list[str] = []

            phase = "api_collection"
            proven_calls = 0
            total_calls = len(selected_sources) * len(major_codes)
            for source in selected_sources:
                credential = active_credentials[source]
                for major_code in major_codes:
                    call_key = f"{source}:{major_code}"
                    if checkpoint is not None and call_key in checkpoint["completed"]:
                        source_results[source].append({**checkpoint["completed"][call_key], "resumed_from_checkpoint": True})
                        proven_calls += 1
                        if source == "job-base" and checkpoint["completed"][call_key].get("missing_local_units"):
                            warnings.append(f"job-base:{major_code}:missing_local_units")
                        continue
                    tell({"stage": "API 수집 완료 범위", "completed": proven_calls, "total": total_calls,
                          "unit": "API·대분류", "detail": f"현재 {source} / {major_code}"})
                    try:
                        progress_options = {"progress_callback": progress} if progress and callables is None else {}
                        authorize()
                        if source == "training-courses":
                            result = operations.collect_training(
                                working_db,
                                credential,
                                major_code=major_code,
                                module_name=None,
                                page_no=1,
                                num_of_rows=500,
                                max_pages=None,
                                **progress_options,
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
                                **progress_options,
                            )
                            safe_result = _safe_job_base_result(result)
                            proven = _job_base_completion_proven(result)
                            if safe_result["missing_local_units"]:
                                warnings.append(
                                    f"job-base:{major_code}:missing_local_units"
                                )
                        authorize()
                        source_results[source].append(
                            {
                                "major_code": major_code,
                                "completion_proven": proven,
                                **safe_result,
                            }
                        )
                        if not proven:
                            source_unproven.append(f"{source}:{major_code}")
                        else:
                            proven_calls += 1
                            if checkpoint is not None:
                                checkpoint["completed"][call_key] = source_results[source][-1]
                                authorize()
                                write_refresh_evidence(checkpoint, checkpoint_path, protected_databases=(resolved_db, working_db))
                        tell({"stage": "API 수집 완료 범위", "completed": proven_calls,
                              "total": total_calls, "unit": "API·대분류"})
                    except BuilderAuthorizationError:
                        raise
                    except Exception as exc:  # Preserve later-major evidence; never reconcile failures.
                        source_results[source].append(
                            {
                                "major_code": major_code,
                                "completion_proven": False,
                                "error_type": type(exc).__name__,
                            }
                        )
                        source_failures.append(f"{source}:{major_code}")

            evidence["source_results"] = source_results
            evidence["warnings"] = warnings
            evidence["failed_sources"] = source_failures
            evidence["unproven_sources"] = source_unproven
            phase = "post_collection_invariant_check"
            tell("API 수집 후 원문·검토 상태 검사")
            after_collection_raw_hash = raw_ksa_sha256(working_db)
            after_collection_trusted = trusted_review_status_counts(working_db)
            after_collection_trusted_identity = trusted_review_status_identity_digest(
                working_db
            )
            collection_invariants_unchanged = (
                before_raw_hash == after_collection_raw_hash
                and before_trusted == after_collection_trusted
                and before_trusted_identity == after_collection_trusted_identity
            )
            evidence["invariants_after_collection"] = {
                "raw_ksa_sha256": after_collection_raw_hash,
                "trusted_review_status_counts": after_collection_trusted,
                "trusted_review_status_identity_digest": after_collection_trusted_identity,
                "unchanged": collection_invariants_unchanged,
            }
            if not collection_invariants_unchanged:
                source_failures.append("source_integrity")
            linked: dict[str, Any] | None = None
            training_fully_proven = (
                "training-courses" in selected_sources
                and not any(
                    item.startswith("training-courses:")
                    for item in source_unproven + source_failures
                )
                and collection_invariants_unchanged
            )
            if training_fully_proven:
                phase = "training_link_build"
                tell("교육과정·온톨로지 관계 연결 (전체 작업량 사전 산정 불가)")
                try:
                    authorize()
                    conn = connect(working_db)
                    try:
                        authorize()
                        linked = operations.build_training_links(conn, reset=False)
                    finally:
                        conn.close()
                    evidence["training_link_build"] = {
                        "performed": True,
                        "reset": False,
                        "result_keys": sorted(linked.keys()),
                    }
                except BuilderAuthorizationError:
                    raise
                except Exception as exc:
                    source_failures.append("training-links")
                    evidence["training_link_build"] = {
                        "performed": False,
                        "reset": False,
                        "error_type": type(exc).__name__,
                    }
            elif "training-courses" in selected_sources:
                evidence["training_link_build"] = {
                    "performed": False,
                    "reset": False,
                    "reason": "training_completion_not_proven",
                }

            phase = "final_invariant_check"
            tell("최종 원문·검토 상태 보존 검사")
            after_raw_hash = raw_ksa_sha256(working_db)
            after_trusted = trusted_review_status_counts(working_db)
            after_trusted_identity = trusted_review_status_identity_digest(working_db)
            evidence["working_copy_invariants_after"] = {
                "raw_ksa_sha256": after_raw_hash,
                "trusted_review_status_counts": after_trusted,
                "trusted_review_status_identity_digest": after_trusted_identity,
            }
            invariants_unchanged = (
                before_raw_hash == after_raw_hash
                and before_trusted == after_trusted
                and before_trusted_identity == after_trusted_identity
            )
            evidence["working_copy_invariants_unchanged"] = invariants_unchanged
            if not invariants_unchanged:
                evidence["outcome"] = "failed_no_reconcile"
                evidence["invariant_failure"] = (
                    "raw_ksa_or_trusted_review_status_changed"
                )
            elif source_failures:
                evidence["outcome"] = "failed_no_reconcile"
            elif source_unproven:
                evidence["outcome"] = "inconclusive_no_publish"
            elif warnings:
                evidence["outcome"] = "completed_with_warnings"
            else:
                evidence["outcome"] = "succeeded_append_only"
            source_after_file_hash = file_sha256(resolved_db, progress, "최종 원본 DB 해시 검사")
            tell("최종 원문·검토 상태 비교")
            source_after_raw_hash = raw_ksa_sha256(resolved_db)
            source_after_trusted = trusted_review_status_counts(resolved_db)
            source_after_trusted_identity = trusted_review_status_identity_digest(
                resolved_db
            )
            source_unchanged = (
                source_before_file_hash == source_after_file_hash
                and source_before_raw_hash == source_after_raw_hash
                and source_before_trusted == source_after_trusted
                and source_before_trusted_identity == source_after_trusted_identity
            )
            if checkpoint_path:
                source_wal = Path(str(resolved_db) + "-wal")
                source_unchanged = source_unchanged and identity["source_wal_sha256"] == (
                    file_sha256(source_wal) if source_wal.exists() else None
                )
            evidence["source_invariants_after"] = {
                "file_sha256": source_after_file_hash,
                "raw_ksa_sha256": source_after_raw_hash,
                "trusted_review_status_counts": source_after_trusted,
                "trusted_review_status_identity_digest": source_after_trusted_identity,
                "unchanged": source_unchanged,
            }
            if not source_unchanged:
                evidence["outcome"] = "failed_no_reconcile"
                evidence["source_integrity_failure"] = (
                    "source_db_changed_during_refresh"
                )
            evidence["source_writes_performed"] = False
            evidence["working_copy_writes_performed"] = True
            authorize()
            if evidence["outcome"] in {
                "succeeded_append_only",
                "completed_with_warnings",
            }:
                evidence["prepared_output"] = str(prepared_output)
            elif retain_failed_output:
                evidence["failed_output"] = str(prepared_output)
            else:
                prepared_output.unlink(missing_ok=True)
                working_copy_created = False
                evidence["failed_output_deleted"] = True
    except BuilderAuthorizationError:
        # Loss of authority must stop all subsequent mutations, including cleanup.
        evidence.pop("prepared_output", None)
        evidence.update({"outcome": "failed_no_reconcile", "failure_type": "BuilderAuthorizationError",
                         "failed_phase": phase, "failure_reason": "builder_authorization_required",
                         "source_writes_performed": False})
        if working_copy_created:
            evidence["failed_output"] = str(prepared_output)
    except RefreshLockError:
        evidence.update(
            {
                "outcome": "blocked_preflight",
                "preflight_errors": ["refresh_lock_already_exists"],
                "source_writes_performed": False,
            }
        )
    except (OSError, sqlite3.Error) as exc:
        evidence.update(
            {
                "outcome": "failed_no_reconcile",
                "failure_type": type(exc).__name__,
                "failed_phase": phase,
                "failure_reason": _safe_local_failure_reason(exc),
                "source_writes_performed": False,
            }
        )
        if working_copy_created and prepared_output.exists():
            if retain_failed_output:
                evidence["failed_output"] = str(prepared_output)
            else:
                authorize()
                prepared_output.unlink(missing_ok=True)
                evidence["failed_output_deleted"] = True
    evidence["finished_at"] = _utc_now()
    return evidence


def _safe_local_failure_reason(exc: Exception) -> str:
    """Return known diagnostic categories, never arbitrary exception text/URLs."""
    message = str(exc).lower()
    if "no such module: dbstat" in message:
        return "sqlite_dbstat_module_unavailable"
    if "database is locked" in message or "database table is locked" in message:
        return "sqlite_database_locked"
    if "disk is full" in message or "no space left" in message:
        return "disk_full"
    if "readonly" in message or "read-only" in message:
        return "sqlite_read_only"
    return "local_database_or_filesystem_error"


def validate_refresh_report_path(output_path: Path, protected_databases: Iterable[str | Path]) -> Path:
    """Reject report aliases of source/baseline SQLite files and their sidecars."""
    destination = Path(output_path).expanduser().resolve(strict=False)
    for database in protected_databases:
        # Both spellings matter when the database itself is a symlink.
        original = Path(database).expanduser()
        for base in (original, original.resolve(strict=False)):
            for suffix in ("", "-wal", "-shm", "-journal"):
                protected = Path(str(base) + suffix)
                if destination == protected.resolve(strict=False) or (
                    destination.exists() and protected.exists() and destination.samefile(protected)
                ):
                    raise ValueError("report_path_conflicts_with_protected_database")
    return destination


def write_refresh_evidence(
    report: Mapping[str, Any], output_path: Path, *,
    protected_databases: Iterable[str | Path] = (),
) -> Path:
    """Atomically write structured evidence without leaking credentials or API payloads."""

    protected = tuple(protected_databases)
    destination = validate_refresh_report_path(output_path, protected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False, suffix=".tmp"
    ) as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        temporary_path = Path(handle.name)
    try:
        # Resolve the original spelling again: parent symlink/junction changes
        # must not redirect a preflight failure report into an immutable input.
        if validate_refresh_report_path(output_path, protected) != destination:
            raise ValueError("report_parent_changed_during_write")
        validate_refresh_report_path(destination, protected)
        temporary_path.replace(destination)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination
