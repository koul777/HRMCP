from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


SCHEMA = "ncs_serving_database_snapshot_v1"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
READINESS_CORE_TABLES = (
    "competency_units",
    "performance_criteria",
    "ksa_items",
    "ncs_training_courses",
)


class SnapshotPreparationError(RuntimeError):
    """Raised when a safe serving snapshot cannot be completed."""


@dataclass(frozen=True)
class SnapshotPaths:
    source_db: Path
    output_db: Path
    json_out: Path
    markdown_out: Path


def configure_utf8_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _resolved(path: Path, *, strict: bool = False) -> Path:
    return path.expanduser().resolve(strict=strict)


def _path_key(path: Path) -> str:
    return os.path.normcase(str(_resolved(path)))


def _sidecar_paths(path: Path) -> dict[str, Path]:
    return {suffix: Path(f"{path}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES}


def validate_paths(
    source_db: Path,
    output_db: Path,
    json_out: Path,
    markdown_out: Path,
) -> SnapshotPaths:
    try:
        source = _resolved(source_db, strict=True)
    except FileNotFoundError as exc:
        raise SnapshotPreparationError(f"source database does not exist: {source_db}") from exc
    if not source.is_file():
        raise SnapshotPreparationError(f"source database is not a file: {source}")

    output = _resolved(output_db)
    json_report = _resolved(json_out)
    markdown_report = _resolved(markdown_out)

    source_family = {source, *_sidecar_paths(source).values()}
    destination_family = {output, *_sidecar_paths(output).values()}
    source_keys = {_path_key(path) for path in source_family}
    destination_keys = {_path_key(path) for path in destination_family}
    report_keys = {_path_key(json_report), _path_key(markdown_report)}

    if destination_keys & source_keys:
        raise SnapshotPreparationError(
            "output database or one of its sidecars overlaps the source database family"
        )
    if _path_key(output) in report_keys or report_keys & (
        destination_keys - {_path_key(output)}
    ):
        raise SnapshotPreparationError(
            "output database family overlaps a JSON or Markdown report path"
        )
    if len(report_keys) != 2:
        raise SnapshotPreparationError("JSON and Markdown report paths must be distinct")
    if report_keys & source_keys:
        raise SnapshotPreparationError(
            "report paths must not overlap the source database or its sidecars"
        )

    if output.exists() or output.is_symlink():
        raise SnapshotPreparationError(
            f"output database already exists; overwrite is not allowed: {output}"
        )
    for sidecar in _sidecar_paths(output).values():
        if sidecar.exists() or sidecar.is_symlink():
            raise SnapshotPreparationError(
                f"destination sidecar path already exists: {sidecar}"
            )
    for report in (json_report, markdown_report):
        if report.is_symlink():
            raise SnapshotPreparationError(f"report path must not be a symlink: {report}")
        if report.exists() and not report.is_file():
            raise SnapshotPreparationError(f"report path is not a file: {report}")

    return SnapshotPaths(
        source_db=source,
        output_db=output,
        json_out=json_report,
        markdown_out=markdown_report,
    )


def _sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def snapshot_file(path: Path) -> dict[str, Any]:
    try:
        stat_before = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "sha256": None,
            "size_bytes": None,
            "mtime_ns": None,
            "mtime_utc": None,
            "stable_during_hash": not path.exists(),
        }

    try:
        sha256 = _sha256_file(path)
        stat_after = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "sha256": None,
            "size_bytes": None,
            "mtime_ns": None,
            "mtime_utc": None,
            "stable_during_hash": False,
        }

    stable = (
        stat_before.st_size == stat_after.st_size
        and stat_before.st_mtime_ns == stat_after.st_mtime_ns
    )
    return {
        "path": str(path),
        "exists": True,
        "sha256": sha256,
        "size_bytes": stat_after.st_size,
        "mtime_ns": stat_after.st_mtime_ns,
        "mtime_utc": datetime.fromtimestamp(stat_after.st_mtime, UTC).isoformat(),
        "stable_during_hash": stable,
    }


def compare_file_content(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "existence_unchanged": before.get("exists") == after.get("exists"),
        "sha256_unchanged": before.get("sha256") == after.get("sha256"),
        "size_unchanged": before.get("size_bytes") == after.get("size_bytes"),
        "mtime_unchanged": before.get("mtime_ns") == after.get("mtime_ns"),
        "before_snapshot_stable": before.get("stable_during_hash") is True,
        "after_snapshot_stable": after.get("stable_during_hash") is True,
    }
    content_unchanged = bool(
        checks["existence_unchanged"]
        and checks["sha256_unchanged"]
        and checks["size_unchanged"]
        and checks["before_snapshot_stable"]
        and checks["after_snapshot_stable"]
    )
    return {
        **checks,
        "content_unchanged": content_unchanged,
        "metadata_unchanged": bool(content_unchanged and checks["mtime_unchanged"]),
    }


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def core_table_inventory(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    inventory: dict[str, dict[str, Any]] = {}
    for table_name in READINESS_CORE_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        row_count = None
        if exists is not None:
            row_count = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
                ).fetchone()[0]
            )
        inventory[table_name] = {
            "exists": exists is not None,
            "row_count": row_count,
        }
    return inventory


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _backup_database(source_path: Path, output_path: Path) -> dict[str, Any]:
    source_conn: sqlite3.Connection | None = None
    destination_conn: sqlite3.Connection | None = None
    try:
        source_conn = _open_read_only(source_path)
        source_conn.execute("BEGIN")
        source_tables = core_table_inventory(source_conn)

        destination_conn = sqlite3.connect(str(output_path), timeout=30)
        destination_conn.execute("PRAGMA busy_timeout=30000")
        destination_mode_before = str(
            destination_conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).lower()
        source_conn.backup(destination_conn, pages=4096, sleep=0.05)
        destination_conn.commit()
        destination_mode_after = str(
            destination_conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        ).lower()
        destination_conn.commit()
        return {
            "source_core_tables": source_tables,
            "destination_journal_mode_before_backup": destination_mode_before,
            "destination_journal_mode_after_backup": destination_mode_after,
        }
    finally:
        if destination_conn is not None:
            destination_conn.close()
        if source_conn is not None:
            if source_conn.in_transaction:
                source_conn.rollback()
            source_conn.close()


def _inspect_destination(path: Path, *, quick_check: bool) -> dict[str, Any]:
    conn = _open_read_only(path)
    try:
        journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        core_tables = core_table_inventory(conn)
        quick_check_rows: list[str] | None = None
        quick_check_ok: bool | None = None
        if quick_check:
            quick_check_rows = [str(row[0]) for row in conn.execute("PRAGMA quick_check")]
            quick_check_ok = quick_check_rows == ["ok"]
        return {
            "journal_mode": journal_mode,
            "core_tables": core_tables,
            "quick_check": {
                "requested": quick_check,
                "ok": quick_check_ok,
                "rows": quick_check_rows,
            },
        }
    finally:
        conn.close()


def _core_table_validation(
    source_tables: dict[str, dict[str, Any]],
    destination_tables: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    table_checks: dict[str, dict[str, Any]] = {}
    for table_name in READINESS_CORE_TABLES:
        source = source_tables[table_name]
        destination = destination_tables[table_name]
        table_checks[table_name] = {
            "source_exists": source["exists"],
            "source_row_count": source["row_count"],
            "destination_exists": destination["exists"],
            "destination_row_count": destination["row_count"],
            "row_count_matches": (
                source["exists"] == destination["exists"]
                and source["row_count"] == destination["row_count"]
            ),
        }

    all_present = all(
        item["source_exists"] and item["destination_exists"]
        for item in table_checks.values()
    )
    all_nonempty = all(
        int(item["destination_row_count"] or 0) > 0 for item in table_checks.values()
    )
    all_counts_match = all(item["row_count_matches"] for item in table_checks.values())
    return {
        "tables": table_checks,
        "all_present": all_present,
        "all_nonempty": all_nonempty,
        "all_row_counts_match": all_counts_match,
        "ready": bool(all_present and all_nonempty and all_counts_match),
    }


def _sidecar_manifest(path: Path) -> dict[str, Any]:
    sidecars = {
        suffix: {
            "path": str(sidecar),
            "exists": sidecar.exists() or sidecar.is_symlink(),
        }
        for suffix, sidecar in _sidecar_paths(path).items()
    }
    return {
        "files": sidecars,
        "all_absent": all(not item["exists"] for item in sidecars.values()),
    }


def _remove_new_destination(path: Path) -> list[str]:
    cleanup_errors: list[str] = []
    for candidate in (*_sidecar_paths(path).values(), path):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_errors.append(f"{candidate}: {type(exc).__name__}")
    return cleanup_errors


def _reserve_new_destination(path: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    descriptor = os.open(path, flags, 0o600)
    os.close(descriptor)


def _storage_preflight(
    source_db: Path,
    source_wal: Path,
    output_parent: Path,
) -> dict[str, Any]:
    source_size = source_db.stat().st_size
    wal_size = source_wal.stat().st_size if source_wal.exists() else 0
    headroom = max(512 * 1024 * 1024, int(source_size * 0.1))
    required_free = source_size + wal_size + headroom
    available_free = shutil.disk_usage(output_parent).free
    return {
        "source_size_bytes": source_size,
        "source_wal_size_bytes": wal_size,
        "headroom_bytes": headroom,
        "required_free_bytes": required_free,
        "available_free_bytes_before": available_free,
        "ok": available_free >= required_free,
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _markdown_bool(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def render_markdown(report: dict[str, Any]) -> str:
    source = report["source"]
    destination = report["destination"]
    validation = report["validation"]
    lines = [
        "# Serving Database Snapshot",
        "",
        f"- schema: `{report['schema']}`",
        f"- status: `{report['status']}`",
        f"- created_at: `{report['created_at']}`",
        f"- source_db: `{source['path']}`",
        f"- output_db: `{destination['path']}`",
        f"- report_only: `{_markdown_bool(report['report_only'])}`",
        f"- db_write_scope: `{report['db_write_scope']}`",
        f"- source_logical_writes: `{_markdown_bool(report['source_logical_writes'])}`",
        f"- destination_db_created: `{_markdown_bool(report['destination_db_created'])}`",
        f"- status_update_allowed: `{_markdown_bool(report['status_update_allowed'])}`",
        f"- approval_claim: `{_markdown_bool(report['approval_claim'])}`",
        f"- external_api_calls: `{_markdown_bool(report['external_api_calls'])}`",
        "- human_review_status_writes: "
        f"`{_markdown_bool(report['human_review_status_writes'])}`",
        f"- storage_preflight_ok: `{_markdown_bool(report['storage_preflight']['ok'])}`",
        f"- required_free_bytes: `{report['storage_preflight']['required_free_bytes']}`",
        "- available_free_bytes_before: "
        f"`{report['storage_preflight']['available_free_bytes_before']}`",
        "",
        "## Destination",
        "",
        f"- sha256: `{destination['file']['sha256']}`",
        f"- size_bytes: `{destination['file']['size_bytes']}`",
        f"- mtime_ns: `{destination['file']['mtime_ns']}`",
        f"- mtime_utc: `{destination['file']['mtime_utc']}`",
        f"- journal_mode_after_close: `{destination['journal_mode_after_close']}`",
        "- sidecars_absent_after_close: "
        f"`{_markdown_bool(destination['sidecars_after_close']['all_absent'])}`",
        "",
        "## Source Observation",
        "",
        "| File | Before SHA-256 | Before bytes | After SHA-256 | After bytes | Content unchanged |",
        "| --- | --- | ---: | --- | ---: | --- |",
    ]
    for key in ("main", "wal"):
        item = source[key]
        lines.append(
            f"| {key} | `{item['before']['sha256']}` | "
            f"{item['before']['size_bytes']} | `{item['after']['sha256']}` | "
            f"{item['after']['size_bytes']} | "
            f"`{_markdown_bool(item['comparison']['content_unchanged'])}` |"
        )
    shm = source["shm_observation"]
    lines.extend(
        [
            "",
            "- source_main_and_wal_content_unchanged: "
            f"`{_markdown_bool(source['main_and_wal_content_unchanged'])}`",
            f"- shm_changed: `{_markdown_bool(shm['changed'])}`",
            f"- shm_observation_only: `{_markdown_bool(shm['observation_only'])}`",
            "",
            "## Core Tables",
            "",
            "| Table | Source rows | Destination rows | Match |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for table_name, item in validation["core_tables"]["tables"].items():
        lines.append(
            f"| `{table_name}` | {item['source_row_count']} | "
            f"{item['destination_row_count']} | "
            f"`{_markdown_bool(item['row_count_matches'])}` |"
        )
    quick_check = validation["quick_check"]
    lines.extend(
        [
            "",
            f"- core_tables_ready: `{_markdown_bool(validation['core_tables']['ready'])}`",
            f"- quick_check_requested: `{_markdown_bool(quick_check['requested'])}`",
            f"- quick_check_ok: `{_markdown_bool(quick_check['ok'])}`",
            "",
        ]
    )
    return "\n".join(lines)


def prepare_serving_database(
    *,
    source_db: Path,
    output_db: Path,
    json_out: Path,
    markdown_out: Path,
    quick_check: bool = False,
) -> dict[str, Any]:
    paths = validate_paths(source_db, output_db, json_out, markdown_out)
    paths.output_db.parent.mkdir(parents=True, exist_ok=True)

    source_wal = _sidecar_paths(paths.source_db)["-wal"]
    source_shm = _sidecar_paths(paths.source_db)["-shm"]
    destination_reserved = False

    try:
        storage_preflight = _storage_preflight(
            paths.source_db,
            source_wal,
            paths.output_db.parent,
        )
        if not storage_preflight["ok"]:
            raise SnapshotPreparationError(
                "insufficient free space for serving snapshot and safety headroom"
            )
        _reserve_new_destination(paths.output_db)
        destination_reserved = True
        source_before = {
            "main": snapshot_file(paths.source_db),
            "wal": snapshot_file(source_wal),
            "shm": snapshot_file(source_shm),
        }
        backup = _backup_database(paths.source_db, paths.output_db)
        destination_inspection = _inspect_destination(
            paths.output_db,
            quick_check=quick_check,
        )
        destination_sidecars = _sidecar_manifest(paths.output_db)
        core_validation = _core_table_validation(
            backup["source_core_tables"],
            destination_inspection["core_tables"],
        )

        validation_errors: list[str] = []
        if destination_inspection["journal_mode"] != "delete":
            validation_errors.append("destination_journal_mode_not_delete")
        if not destination_sidecars["all_absent"]:
            validation_errors.append("destination_sidecars_present")
        if not core_validation["ready"]:
            validation_errors.append("core_table_schema_or_count_check_failed")
        quick_check_report = destination_inspection["quick_check"]
        if quick_check and quick_check_report["ok"] is not True:
            validation_errors.append("destination_quick_check_failed")
        if validation_errors:
            raise SnapshotPreparationError(
                "destination validation failed: " + ", ".join(validation_errors)
            )

        source_after = {
            "main": snapshot_file(paths.source_db),
            "wal": snapshot_file(source_wal),
            "shm": snapshot_file(source_shm),
        }
        main_comparison = compare_file_content(
            source_before["main"], source_after["main"]
        )
        wal_comparison = compare_file_content(
            source_before["wal"], source_after["wal"]
        )
        shm_comparison = compare_file_content(
            source_before["shm"], source_after["shm"]
        )
        source_main_and_wal_content_unchanged = bool(
            main_comparison["content_unchanged"]
            and wal_comparison["content_unchanged"]
        )
        if not source_main_and_wal_content_unchanged:
            raise SnapshotPreparationError(
                "source database or WAL content changed while the serving "
                "snapshot was being prepared"
            )
        destination_file = snapshot_file(paths.output_db)
        if not destination_file["stable_during_hash"]:
            raise SnapshotPreparationError(
                "destination changed while its SHA-256 was being calculated"
            )

        report: dict[str, Any] = {
            "schema": SCHEMA,
            "ok": True,
            "status": "prepared",
            "created_at": datetime.now(UTC).isoformat(),
            "report_only": False,
            "db_writes": True,
            "db_write_scope": "new_serving_snapshot_only",
            "source_db_writes": False,
            "destination_db_writes": True,
            "source_logical_writes": False,
            "destination_db_created": True,
            "status_update_allowed": False,
            "approval_claim": False,
            "external_api_calls": False,
            "human_review_status_writes": False,
            "storage_preflight": storage_preflight,
            "source": {
                "path": str(paths.source_db),
                "open_uri_mode": "ro",
                "query_only": True,
                "main": {
                    "before": source_before["main"],
                    "after": source_after["main"],
                    "comparison": main_comparison,
                },
                "wal": {
                    "before": source_before["wal"],
                    "after": source_after["wal"],
                    "comparison": wal_comparison,
                },
                "main_and_wal_content_unchanged": (
                    source_main_and_wal_content_unchanged
                ),
                "shm_observation": {
                    "before": source_before["shm"],
                    "after": source_after["shm"],
                    "comparison": shm_comparison,
                    "changed": not shm_comparison["metadata_unchanged"],
                    "observation_only": True,
                    "used_as_logical_write_evidence": False,
                },
            },
            "destination": {
                "path": str(paths.output_db),
                "file": destination_file,
                "journal_mode_before_backup": backup[
                    "destination_journal_mode_before_backup"
                ],
                "journal_mode_after_backup": backup[
                    "destination_journal_mode_after_backup"
                ],
                "journal_mode_after_close": destination_inspection["journal_mode"],
                "sidecars_after_close": destination_sidecars,
            },
            "validation": {
                "default_validation": "schema_and_counts",
                "schema_count_check_performed": True,
                "core_tables": core_validation,
                "quick_check": quick_check_report,
            },
            "reports": {
                "json_path": str(paths.json_out),
                "markdown_path": str(paths.markdown_out),
            },
        }

        json_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        markdown_text = render_markdown(report)
        _atomic_write_text(paths.markdown_out, markdown_text)
        _atomic_write_text(paths.json_out, json_text)
        return report
    except BaseException as exc:
        cleanup_errors = (
            _remove_new_destination(paths.output_db)
            if destination_reserved
            else []
        )
        if cleanup_errors:
            raise SnapshotPreparationError(
                f"{exc}; destination cleanup failed: {', '.join(cleanup_errors)}"
            ) from exc
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a closed, DELETE-journal SQLite serving snapshot from a "
            "read-only source connection, including committed WAL content."
        )
    )
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--output-db", type=Path, required=True)
    parser.add_argument(
        "--out",
        "--json-out",
        dest="json_out",
        type=Path,
        required=True,
        help="JSON evidence report path.",
    )
    parser.add_argument("--markdown-out", type=Path, required=True)
    parser.add_argument(
        "--quick-check",
        action="store_true",
        help="Run PRAGMA quick_check after the default schema/count validation.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    try:
        report = prepare_serving_database(
            source_db=args.source_db,
            output_db=args.output_db,
            json_out=args.json_out,
            markdown_out=args.markdown_out,
            quick_check=args.quick_check,
        )
    except (OSError, sqlite3.Error, SnapshotPreparationError) as exc:
        error = {
            "schema": SCHEMA,
            "ok": False,
            "status": "failed",
            "report_only": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
            },
            "db_writes": None,
            "db_write_scope": "new_serving_snapshot_attempt_only",
            "source_db_writes": False,
            "destination_db_writes": None,
            "source_logical_writes": False,
            "destination_db_created": False,
            "status_update_allowed": False,
            "approval_claim": False,
            "external_api_calls": False,
            "human_review_status_writes": False,
        }
        print(json.dumps(error, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
