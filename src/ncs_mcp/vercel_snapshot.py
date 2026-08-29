"""Safe materialization of the compact ontology SQLite snapshot on Vercel."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from typing import Any, Mapping


LOGGER = logging.getLogger(__name__)

COMPACT_ARCHIVE_NAME = "ncs_ontology_compact.zip"
COMPACT_SNAPSHOT_NAME = "ncs_ontology_compact.db"
COMPACT_MANIFEST_NAME = "ncs_ontology_compact.manifest.json"
COMPACT_MANIFEST_SCHEMA = "ncs_ontology_compact_manifest_v1"
COMPACT_DATABASE_SCHEMA = "ncs_vercel_ontology_compact_v2"
COMPACT_POSTING_CODEC = "delta_uvarint_v1"

# The normal Vercel function package is limited to 500 MB. Leave headroom for
# Python sources and dependencies and reject a raw SQLite member at or above the
# release gate, even when a malformed ZIP advertises a smaller compressed size.
MAX_SNAPSHOT_BYTES = 480_000_000
MAX_ARCHIVE_BYTES = 480_000_000
MAX_MANIFEST_BYTES = 1_000_000
MAX_COMPRESSION_RATIO = 200.0

# Compatibility aliases now point at the compact standard-function artifact;
# there is no 5 GiB direct-bundle path.
SNAPSHOT_MEMBER_NAME = COMPACT_SNAPSHOT_NAME
COMPLETE_SNAPSHOT_NAME = COMPACT_SNAPSHOT_NAME
MAX_BUNDLED_DB_BYTES = MAX_SNAPSHOT_BYTES
MAX_ZIP_MEMBER_BYTES = MAX_SNAPSHOT_BYTES

DEFAULT_REQUIRED_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)
_SQLITE_HEADER = b"SQLite format 3\x00"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERIFIED_STAMP_SCHEMA = "ncs_ontology_compact_verified_v1"


def external_db_override_allowed(env: Mapping[str, str] | None = None) -> bool:
    """Return whether an external path/URL may replace the bundled release."""

    environment = os.environ if env is None else env
    return environment.get("NCS_MCP_ALLOW_EXTERNAL_DB_OVERRIDE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def readiness_required_tables(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Return the core and explicitly configured readiness tables in stable order."""

    environment = os.environ if env is None else env
    names = list(DEFAULT_REQUIRED_TABLES)
    for raw_name in environment.get("NCS_MCP_READINESS_EXTRA_TABLES", "").split(","):
        name = raw_name.strip()
        if not name or name in names or not _SAFE_IDENTIFIER.fullmatch(name):
            continue
        names.append(name)
    return tuple(names)


def readiness_required_min_rows(env: Mapping[str, str] | None = None) -> dict[str, int]:
    """Parse the deployment's table-to-minimum-row-count readiness contract."""

    environment = os.environ if env is None else env
    raw_value = environment.get("NCS_MCP_READINESS_MIN_ROWS", "").strip()
    if not raw_value:
        return {}
    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError("NCS_MCP_READINESS_MIN_ROWS must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("NCS_MCP_READINESS_MIN_ROWS must be a JSON object")

    minimum_rows: dict[str, int] = {}
    for raw_name, raw_count in payload.items():
        name = str(raw_name).strip()
        if not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError(f"invalid readiness table name: {raw_name!r}")
        if isinstance(raw_count, bool):
            raise ValueError(f"invalid minimum row count for {name}: {raw_count!r}")
        try:
            count = int(raw_count)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"invalid minimum row count for {name}: {raw_count!r}"
            ) from exc
        if count < 1:
            raise ValueError(f"minimum row count for {name} must be positive")
        minimum_rows[name] = count
    return minimum_rows


def _required_tables_with_manifest(
    required_tables: tuple[str, ...],
    minimum_rows: Mapping[str, int],
) -> tuple[str, ...]:
    names = list(required_tables)
    for name in minimum_rows:
        if name not in names:
            names.append(name)
    return tuple(names)


def sqlite_snapshot_is_usable(
    path: Path,
    *,
    expected_size: int | None = None,
    required_tables: tuple[str, ...] = DEFAULT_REQUIRED_TABLES,
    minimum_rows: Mapping[str, int] | None = None,
    run_quick_check: bool = False,
    max_bytes: int = MAX_SNAPSHOT_BYTES,
) -> bool:
    """Validate a SQLite file used by the explicitly enabled override path."""

    try:
        minima = dict(minimum_rows or {})
        table_names = _required_tables_with_manifest(required_tables, minima)
        if not path.is_file():
            return False
        file_size = path.stat().st_size
        if file_size <= 0 or file_size >= max_bytes:
            return False
        if expected_size is not None and file_size != expected_size:
            return False
        with path.open("rb") as source:
            if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return False

        database_uri = f"file:{path.resolve().as_posix()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(database_uri, uri=True)) as conn:
            conn.execute("PRAGMA query_only = ON")
            if run_quick_check:
                quick_check = conn.execute("PRAGMA quick_check").fetchone()
                if not quick_check or quick_check[0] != "ok":
                    return False
            for table_name in table_names:
                if not _SAFE_IDENTIFIER.fullmatch(table_name):
                    return False
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                    (table_name,),
                ).fetchone()
                if row is None:
                    return False
                minimum = minima.get(table_name)
                if minimum is None:
                    if conn.execute(
                        f'SELECT 1 FROM "{table_name}" LIMIT 1'
                    ).fetchone() is None:
                        return False
                else:
                    row_count = conn.execute(
                        f'SELECT COUNT(*) FROM "{table_name}"'
                    ).fetchone()
                    if not row_count or int(row_count[0]) < minimum:
                        return False
    except (OSError, sqlite3.DatabaseError):
        return False
    return True


def _strict_count_map(
    payload: Any,
    *,
    field: str,
    allow_empty: bool = False,
) -> dict[str, int]:
    if not isinstance(payload, dict) or (not payload and not allow_empty):
        raise ValueError(f"compact manifest {field} must be a non-empty object")
    counts: dict[str, int] = {}
    for raw_name, raw_count in payload.items():
        name = str(raw_name).strip()
        if not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError(f"invalid compact manifest count name: {raw_name!r}")
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise ValueError(f"invalid compact manifest count for {name}: {raw_count!r}")
        counts[name] = raw_count
    return counts


def load_compact_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and strictly validate the immutable sidecar manifest."""

    try:
        size = manifest_path.stat().st_size
        if size <= 0 or size > MAX_MANIFEST_BYTES:
            raise ValueError("compact manifest size is outside the safe range")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("unable to read compact snapshot manifest") from exc
    if not isinstance(payload, dict):
        raise ValueError("compact snapshot manifest must be a JSON object")
    if payload.get("schema") != COMPACT_MANIFEST_SCHEMA:
        raise ValueError("unsupported compact snapshot manifest schema")
    if payload.get("archive_member") != COMPACT_SNAPSHOT_NAME:
        raise ValueError("compact snapshot manifest names an unexpected archive member")
    if payload.get("database_schema") != COMPACT_DATABASE_SCHEMA:
        raise ValueError("unsupported compact snapshot database schema")
    if payload.get("codec") != COMPACT_POSTING_CODEC:
        raise ValueError("unsupported compact snapshot posting codec")

    sqlite_bytes = payload.get("sqlite_bytes")
    if (
        isinstance(sqlite_bytes, bool)
        or not isinstance(sqlite_bytes, int)
        or sqlite_bytes <= 0
        or sqlite_bytes >= MAX_SNAPSHOT_BYTES
    ):
        raise ValueError("compact snapshot sqlite_bytes is outside the safe range")
    sqlite_sha256 = str(payload.get("sqlite_sha256", "")).strip().lower()
    if not _SHA256.fullmatch(sqlite_sha256):
        raise ValueError("compact snapshot sqlite_sha256 is invalid")

    normalized = dict(payload)
    normalized["sqlite_sha256"] = sqlite_sha256
    normalized["physical_counts"] = _strict_count_map(
        payload.get("physical_counts"), field="physical_counts"
    )
    normalized["logical_counts"] = _strict_count_map(
        payload.get("logical_counts"), field="logical_counts"
    )
    # ``servable_counts`` was added after the first v1 manifests shipped.  A
    # missing field is retained as an empty map so legacy fixtures remain
    # readable; a present field is still validated strictly.
    normalized["servable_counts"] = (
        _strict_count_map(
            payload["servable_counts"], field="servable_counts", allow_empty=True
        )
        if "servable_counts" in payload
        else {}
    )
    for table_name in DEFAULT_REQUIRED_TABLES:
        if (
            table_name not in normalized["physical_counts"]
            and table_name not in normalized["servable_counts"]
        ):
            raise ValueError(f"compact manifest is missing required table: {table_name}")
    return normalized


def _manifest_fingerprint(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def inspect_compact_archive(
    archive_path: Path,
    manifest_path: Path,
) -> tuple[dict[str, Any], zipfile.ZipInfo]:
    """Validate archive metadata without extracting its SQLite member."""

    manifest = load_compact_manifest(manifest_path)
    try:
        archive_bytes = archive_path.stat().st_size
        if archive_bytes <= 0 or archive_bytes >= MAX_ARCHIVE_BYTES:
            raise ValueError("compact snapshot archive size is outside the safe range")
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            if len(members) != 1:
                raise ValueError("compact snapshot archive must contain exactly one member")
            member = members[0]
            if member.is_dir() or member.filename != COMPACT_SNAPSHOT_NAME:
                raise ValueError("compact snapshot archive member name is invalid")
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("compact snapshot archive member path is unsafe")
            if member.flag_bits & 0x1:
                raise ValueError("encrypted compact snapshot archives are not supported")
            if member.compress_type != zipfile.ZIP_DEFLATED:
                raise ValueError("compact snapshot archive member must use DEFLATE")
            if member.file_size != manifest["sqlite_bytes"]:
                raise ValueError("compact snapshot member size does not match manifest")
            if member.file_size <= 0 or member.file_size >= MAX_SNAPSHOT_BYTES:
                raise ValueError("compact snapshot member is outside the safe range")
            if member.compress_size <= 0:
                raise ValueError("compact snapshot compressed size is invalid")
            ratio = member.file_size / member.compress_size
            if ratio > MAX_COMPRESSION_RATIO:
                raise ValueError("compact snapshot compression ratio exceeds safety cap")
            return manifest, member
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise ValueError("unable to inspect compact snapshot archive") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_embedded_counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    rows = conn.execute(
        """
        SELECT object_name, row_count, count_kind
        FROM serving_snapshot_table_counts
        ORDER BY count_kind, object_name
        """
    ).fetchall()
    result: dict[str, dict[str, int]] = {
        "physical": {},
        "logical": {},
        "servable": {},
    }
    for raw_name, raw_count, raw_kind in rows:
        name = str(raw_name)
        kind = str(raw_kind)
        if kind not in result or not _SAFE_IDENTIFIER.fullmatch(name):
            raise ValueError("compact snapshot contains invalid embedded count metadata")
        if isinstance(raw_count, bool) or int(raw_count) < 0:
            raise ValueError("compact snapshot contains an invalid embedded count")
        if name in result[kind]:
            raise ValueError("compact snapshot contains duplicate embedded count metadata")
        result[kind][name] = int(raw_count)
    return result


def _validate_compact_database(
    database_path: Path,
    manifest: Mapping[str, Any],
    *,
    computed_sha256: str | None = None,
    required_tables: tuple[str, ...] = DEFAULT_REQUIRED_TABLES,
    minimum_rows: Mapping[str, int] | None = None,
) -> bool:
    """Validate content-bound schema/count metadata after extraction."""

    try:
        stat = database_path.stat()
        if stat.st_size != manifest["sqlite_bytes"] or stat.st_size >= MAX_SNAPSHOT_BYTES:
            return False
        with database_path.open("rb") as source:
            if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return False
        actual_sha256 = computed_sha256 or _sha256_file(database_path)
        if actual_sha256 != manifest["sqlite_sha256"]:
            return False

        database_uri = f"file:{database_path.resolve().as_posix()}?mode=ro&immutable=1"
        with closing(sqlite3.connect(database_uri, uri=True)) as conn:
            conn.execute("PRAGMA query_only = ON")
            objects = {
                str(name): str(kind)
                for name, kind in conn.execute(
                    """
                    SELECT name, type
                    FROM sqlite_master
                    WHERE type IN ('table', 'view')
                    """
                ).fetchall()
            }
            if objects.get("serving_snapshot_manifest") != "table":
                return False
            if objects.get("serving_snapshot_table_counts") != "table":
                return False
            embedded_manifest = {
                str(key): str(value)
                for key, value in conn.execute(
                    "SELECT manifest_key, manifest_value FROM serving_snapshot_manifest"
                ).fetchall()
            }
            if embedded_manifest.get("schema") != manifest["database_schema"]:
                return False
            embedded_codec = embedded_manifest.get("codec") or embedded_manifest.get(
                "posting_codec"
            )
            if embedded_codec != manifest["codec"]:
                return False

            embedded_counts = _read_embedded_counts(conn)
            if embedded_counts["physical"] != manifest["physical_counts"]:
                return False
            if embedded_counts["logical"] != manifest["logical_counts"]:
                return False
            if embedded_counts["servable"] != manifest.get("servable_counts", {}):
                return False
            for object_name in embedded_counts["physical"]:
                if objects.get(object_name) != "table":
                    return False
            for object_name in embedded_counts["servable"]:
                if objects.get(object_name) not in {"table", "view"}:
                    return False

            minima = dict(minimum_rows or {})
            for table_name in _required_tables_with_manifest(required_tables, minima):
                if not _SAFE_IDENTIFIER.fullmatch(table_name) or table_name not in objects:
                    return False
                # Public readiness checks must use the count of the canonical
                # serving object (which may be a view), falling back to the
                # physical table count for legacy manifests.
                embedded_count = embedded_counts["servable"].get(table_name)
                if embedded_count is None:
                    embedded_count = embedded_counts["physical"].get(table_name)
                if embedded_count is None:
                    return False
                minimum = minima.get(table_name, 1)
                if embedded_count < minimum:
                    return False
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError):
        return False
    return True


def _verified_stamp_path(destination_path: Path) -> Path:
    return destination_path.with_suffix(destination_path.suffix + ".verified.json")


def _cached_snapshot_is_verified(
    destination_path: Path,
    manifest: Mapping[str, Any],
) -> bool:
    stamp_path = _verified_stamp_path(destination_path)
    try:
        stat = destination_path.stat()
        if stat.st_size != manifest["sqlite_bytes"]:
            return False
        with destination_path.open("rb") as source:
            if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
                return False
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        return stamp == {
            "schema": _VERIFIED_STAMP_SCHEMA,
            "manifest_fingerprint": _manifest_fingerprint(manifest),
            "sqlite_bytes": stat.st_size,
            "sqlite_mtime_ns": stat.st_mtime_ns,
            "sqlite_sha256": manifest["sqlite_sha256"],
        }
    except (OSError, json.JSONDecodeError, TypeError):
        return False


def _write_verified_stamp(destination_path: Path, manifest: Mapping[str, Any]) -> None:
    stat = destination_path.stat()
    stamp = {
        "schema": _VERIFIED_STAMP_SCHEMA,
        "manifest_fingerprint": _manifest_fingerprint(manifest),
        "sqlite_bytes": stat.st_size,
        "sqlite_mtime_ns": stat.st_mtime_ns,
        "sqlite_sha256": manifest["sqlite_sha256"],
    }
    stamp_path = _verified_stamp_path(destination_path)
    fd, raw_temp = tempfile.mkstemp(
        prefix=f".{stamp_path.name}.", suffix=".tmp", dir=stamp_path.parent
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(stamp, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, stamp_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _acquire_lock(lock_path: Path, *, timeout_seconds: float) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    stale_after_seconds = max(120.0, timeout_seconds * 4)
    while time.monotonic() < deadline:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            return fd
        except FileExistsError:
            try:
                if time.time() - lock_path.stat().st_mtime > stale_after_seconds:
                    lock_path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.05)
    return None


def materialize_compact_snapshot(
    archive_path: Path,
    manifest_path: Path,
    destination_path: Path,
    *,
    required_tables: tuple[str, ...] = DEFAULT_REQUIRED_TABLES,
    minimum_rows: Mapping[str, int] | None = None,
    lock_timeout_seconds: float = 45.0,
) -> bool:
    """Extract, content-validate, and atomically publish the compact DB once."""

    try:
        manifest, expected_member = inspect_compact_archive(archive_path, manifest_path)
    except ValueError as exc:
        LOGGER.error("Compact Vercel DB package validation failed: %s", exc)
        return False

    if _cached_snapshot_is_verified(destination_path, manifest):
        return True

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination_path.with_suffix(destination_path.suffix + ".lock")
    lock_fd = _acquire_lock(lock_path, timeout_seconds=lock_timeout_seconds)
    if lock_fd is None:
        return _cached_snapshot_is_verified(destination_path, manifest)

    temp_path: Path | None = None
    try:
        if _cached_snapshot_is_verified(destination_path, manifest):
            return True
        # An invalid cached file is not a published snapshot. Removing it before
        # extraction keeps peak /tmp usage below the standard function allowance.
        destination_path.unlink(missing_ok=True)
        _verified_stamp_path(destination_path).unlink(missing_ok=True)

        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.",
            suffix=".tmp",
            dir=destination_path.parent,
        )
        os.close(temp_fd)
        temp_path = Path(temp_name)
        digest = hashlib.sha256()
        with zipfile.ZipFile(archive_path, "r") as archive:
            member = archive.getinfo(COMPACT_SNAPSHOT_NAME)
            if (
                member.CRC != expected_member.CRC
                or member.file_size != expected_member.file_size
                or member.compress_size != expected_member.compress_size
            ):
                return False
            with archive.open(member, "r") as source, temp_path.open("wb") as output:
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    digest.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())

        if not _validate_compact_database(
            temp_path,
            manifest,
            computed_sha256=digest.hexdigest(),
            required_tables=required_tables,
            minimum_rows=minimum_rows,
        ):
            LOGGER.error("Extracted compact Vercel DB snapshot failed validation")
            return False
        os.replace(temp_path, destination_path)
        temp_path = None
        _write_verified_stamp(destination_path, manifest)
        return True
    except (OSError, KeyError, zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error):
        LOGGER.exception("Unable to materialize the compact Vercel DB snapshot")
        return False
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def materialize_sqlite_zip(
    archive_path: Path,
    destination_path: Path,
    *,
    manifest_path: Path | None = None,
    required_tables: tuple[str, ...] = DEFAULT_REQUIRED_TABLES,
    minimum_rows: Mapping[str, int] | None = None,
    lock_timeout_seconds: float = 45.0,
) -> bool:
    """Compatibility wrapper for the compact archive materializer."""

    sidecar = manifest_path or archive_path.with_name(COMPACT_MANIFEST_NAME)
    return materialize_compact_snapshot(
        archive_path,
        sidecar,
        destination_path,
        required_tables=required_tables,
        minimum_rows=minimum_rows,
        lock_timeout_seconds=lock_timeout_seconds,
    )
