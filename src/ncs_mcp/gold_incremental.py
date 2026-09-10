"""Plan a bounded, review-gated incremental Gold LPG synchronization.

The module compares two canonical ``ncs_gold_lpg_ndjson_v1`` exports without
loading either graph into memory.  Its output is an *operation plan*, not a
Neo4j writer: upserts are safe to hand to a future loader, while every
tombstone is explicitly operator-gated.
"""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Callable, Iterator, Mapping

from ncs_mcp.gold_stream import GOLD_LPG_NDJSON_SCHEMA


GOLD_INCREMENTAL_SCHEMA = "ncs_gold_lpg_incremental_v1"
MAX_RECORD_BYTES = 8 * 1024 * 1024


class GoldIncrementalValidationError(ValueError):
    """Raised when a source export is not a valid canonical Gold NDJSON file."""


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _load_canonical_json(raw: bytes, *, path: Path, line_number: int) -> dict[str, Any]:
    if not raw.endswith(b"\n"):
        raise GoldIncrementalValidationError(f"{path}: line {line_number} is missing its newline")
    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise GoldIncrementalValidationError(
                    f"{path}: duplicate JSON key {key!r} at line {line_number}"
                )
            value[key] = item
        return value

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GoldIncrementalValidationError(
            f"{path}: invalid JSON at line {line_number}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise GoldIncrementalValidationError(f"{path}: line {line_number} must be a JSON object")
    if raw != _canonical_bytes(value):
        raise GoldIncrementalValidationError(
            f"{path}: line {line_number} is not canonical Gold NDJSON"
        )
    return value


def _record_payload(record: Mapping[str, Any], *, path: Path, line_number: int) -> tuple[str, dict[str, Any]]:
    record_type = record.get("record_type")
    if record_type not in {"node", "relationship"}:
        raise GoldIncrementalValidationError(
            f"{path}: line {line_number} has unsupported graph record_type {record_type!r}"
        )
    payload = record.get(record_type)
    if not isinstance(payload, dict):
        raise GoldIncrementalValidationError(
            f"{path}: line {line_number} has no object payload for {record_type!r}"
        )
    identifier = payload.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise GoldIncrementalValidationError(
            f"{path}: line {line_number} {record_type} has no non-empty string id"
        )
    if record_type == "relationship":
        if not isinstance(payload.get("source"), str) or not isinstance(payload.get("target"), str):
            raise GoldIncrementalValidationError(
                f"{path}: line {line_number} relationship requires string source and target"
            )
    return record_type, payload


def _validate_manifest(
    manifest: Mapping[str, Any],
    *,
    path: Path,
    observed_digest: str,
    record_count: int,
    node_count: int,
    edge_count: int,
) -> dict[str, Any]:
    if manifest.get("schema") != GOLD_LPG_NDJSON_SCHEMA:
        raise GoldIncrementalValidationError(f"{path}: unsupported Gold NDJSON schema")
    if manifest.get("records_sha256") != observed_digest:
        raise GoldIncrementalValidationError(f"{path}: records_sha256 does not match contents")
    if manifest.get("records_before_manifest") != record_count:
        raise GoldIncrementalValidationError(f"{path}: records_before_manifest does not match contents")
    if manifest.get("node_count") != node_count or manifest.get("edge_count") != edge_count:
        raise GoldIncrementalValidationError(f"{path}: graph record counts do not match manifest")
    profile = manifest.get("profile")
    projection_schema = manifest.get("projection_schema")
    if not isinstance(profile, dict) or not isinstance(projection_schema, str) or not projection_schema:
        raise GoldIncrementalValidationError(f"{path}: missing profile or projection_schema")
    return dict(manifest)


def _source_fingerprint(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Keep source identity useful for audit without copying payload rows."""

    snapshot = manifest.get("snapshot")
    return {
        "schema": manifest["schema"],
        "projection_schema": manifest["projection_schema"],
        "profile": manifest["profile"],
        "records_sha256": manifest["records_sha256"],
        "node_count": manifest["node_count"],
        "edge_count": manifest["edge_count"],
        "source_tables": manifest.get("source_tables", []),
        "snapshot": snapshot if isinstance(snapshot, dict) else {},
    }


def _sqlite_index(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA temp_store=FILE")
    conn.executescript(
        """
        CREATE TABLE previous_records (
            kind TEXT NOT NULL CHECK(kind IN ('node', 'relationship')),
            record_id TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            PRIMARY KEY(kind, record_id)
        ) WITHOUT ROWID;
        CREATE TABLE current_ids (
            kind TEXT NOT NULL CHECK(kind IN ('node', 'relationship')),
            record_id TEXT NOT NULL,
            PRIMARY KEY(kind, record_id)
        ) WITHOUT ROWID;
        CREATE TABLE changed_records (
            kind TEXT NOT NULL CHECK(kind IN ('node', 'relationship')),
            record_id TEXT NOT NULL,
            record_hash TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY(kind, record_id)
        ) WITHOUT ROWID;
        """
    )
    return conn


@contextmanager
def _temporary_index() -> Iterator[sqlite3.Connection]:
    with tempfile.TemporaryDirectory(prefix="ncs-gold-incremental-") as directory:
        conn = _sqlite_index(Path(directory) / "index.sqlite")
        try:
            yield conn
        finally:
            conn.close()


def _hash_payload(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def _read_export(
    path: Path,
    *,
    on_graph_record: Callable[[str, Mapping[str, Any]], None],
    record_observer: Callable[[int], None] | None,
) -> dict[str, Any]:
    """Validate a source stream while handing each graph record to a callback."""

    if not path.is_file():
        raise FileNotFoundError(f"Gold NDJSON export does not exist: {path}")
    digest = hashlib.sha256()
    record_count = node_count = edge_count = diagnostic_count = 0
    manifest: dict[str, Any] | None = None
    with path.open("rb") as handle:
        for line_number, raw in enumerate(iter(lambda: handle.readline(MAX_RECORD_BYTES + 1), b""), 1):
            if len(raw) > MAX_RECORD_BYTES:
                raise GoldIncrementalValidationError(
                    f"{path}: line {line_number} exceeds MAX_RECORD_BYTES={MAX_RECORD_BYTES}"
                )
            if record_observer is not None:
                record_observer(len(raw))
            record = _load_canonical_json(raw, path=path, line_number=line_number)
            record_type = record.get("record_type")
            if manifest is not None:
                raise GoldIncrementalValidationError(f"{path}: records appear after final manifest")
            if record_type == "manifest":
                raw_manifest = record.get("manifest")
                if not isinstance(raw_manifest, dict):
                    raise GoldIncrementalValidationError(f"{path}: manifest payload must be an object")
                manifest = _validate_manifest(
                    raw_manifest,
                    path=path,
                    observed_digest=digest.hexdigest(),
                    record_count=record_count,
                    node_count=node_count,
                    edge_count=edge_count,
                )
                if raw_manifest.get("diagnostic_count", diagnostic_count) != diagnostic_count:
                    raise GoldIncrementalValidationError(f"{path}: diagnostic_count does not match contents")
                continue
            if record_type == "diagnostic":
                if not isinstance(record.get("diagnostic"), dict):
                    raise GoldIncrementalValidationError(f"{path}: line {line_number} diagnostic must be an object")
                diagnostic_count += 1
            else:
                kind, payload = _record_payload(record, path=path, line_number=line_number)
                on_graph_record(kind, payload)
                if kind == "node":
                    node_count += 1
                else:
                    edge_count += 1
            record_count += 1
            digest.update(raw)
    if manifest is None:
        raise GoldIncrementalValidationError(f"{path}: final manifest record is missing")
    return manifest


def _operation_record(kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {"record_type": kind, kind: dict(payload)}


def _write_line(handle: Any, record: Mapping[str, Any]) -> bytes:
    encoded = _canonical_bytes(record)
    handle.write(encoded)
    return encoded


def _assert_output_path(previous: Path, current: Path, destination: Path) -> None:
    if destination == previous or destination == current:
        raise ValueError("incremental output path must be separate from both source NDJSON exports")
    if destination.exists() and destination.is_dir():
        raise IsADirectoryError(f"incremental output path is a directory: {destination}")


def plan_gold_lpg_incremental(
    previous_path: str | Path,
    current_path: str | Path,
    out_path: str | Path | None = None,
    *,
    _record_observer: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Return or atomically write a deterministic, review-gated Gold diff plan.

    When ``out_path`` is omitted, the same disk-backed planning work runs as a
    report-only dry run and the transient operation stream is discarded.
    """

    previous = Path(previous_path).resolve()
    current = Path(current_path).resolve()
    if previous == current:
        raise ValueError("previous and current Gold NDJSON exports must be different paths")
    destination = Path(out_path).resolve() if out_path is not None else None
    if destination is not None:
        _assert_output_path(previous, current, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)

    with _temporary_index() as index:
        def index_previous(kind: str, payload: Mapping[str, Any]) -> None:
            try:
                index.execute(
                    "INSERT INTO previous_records(kind, record_id, record_hash) VALUES (?, ?, ?)",
                    (kind, payload["id"], _hash_payload(payload)),
                )
            except sqlite3.IntegrityError as exc:
                raise GoldIncrementalValidationError(
                    f"{previous}: duplicate {kind} id {payload['id']!r}"
                ) from exc

        previous_manifest = _read_export(
            previous, on_graph_record=index_previous, record_observer=_record_observer
        )
        index.commit()

        def index_current(kind: str, payload: Mapping[str, Any]) -> None:
            record_id = payload["id"]
            try:
                index.execute(
                    "INSERT INTO current_ids(kind, record_id) VALUES (?, ?)", (kind, record_id)
                )
            except sqlite3.IntegrityError as exc:
                raise GoldIncrementalValidationError(
                    f"{current}: duplicate {kind} id {record_id!r}"
                ) from exc
            record_hash = _hash_payload(payload)
            previous_hash = index.execute(
                "SELECT record_hash FROM previous_records WHERE kind = ? AND record_id = ?",
                (kind, record_id),
            ).fetchone()
            if previous_hash is None or previous_hash[0] != record_hash:
                index.execute(
                    "INSERT INTO changed_records(kind, record_id, record_hash, payload_json) VALUES (?, ?, ?, ?)",
                    (
                        kind,
                        record_id,
                        record_hash,
                        _canonical_bytes(payload).decode("utf-8").rstrip("\n"),
                    ),
                )

        current_manifest = _read_export(
            current, on_graph_record=index_current, record_observer=_record_observer
        )
        index.commit()
        if previous_manifest["projection_schema"] != current_manifest["projection_schema"]:
            raise GoldIncrementalValidationError("projection_schema changed; full staged reload is required")
        if _canonical_bytes(previous_manifest["profile"]) != _canonical_bytes(current_manifest["profile"]):
            raise GoldIncrementalValidationError("Gold projection profile changed; full staged reload is required")

        change_count = index.execute("SELECT count(*) FROM changed_records").fetchone()[0]
        tombstone_count = index.execute(
            """
            SELECT count(*)
            FROM previous_records p
            LEFT JOIN current_ids c ON c.kind = p.kind AND c.record_id = p.record_id
            WHERE c.record_id IS NULL
            """
        ).fetchone()[0]

        operation_counts: Counter[str] = Counter()
        output_record_count = 0
        output_digest = hashlib.sha256()
        temp_name: str | None = None
        dry_run = destination is None
        try:
            if destination is None:
                temp_dir = tempfile.TemporaryDirectory(prefix="ncs-gold-incremental-plan-")
                target = Path(temp_dir.name) / "plan.ndjson"
            else:
                temp_dir = None
                target = destination
            with tempfile.NamedTemporaryFile(
                mode="wb", delete=False, dir=target.parent,
                prefix=f".{target.name}.", suffix=".tmp",
            ) as handle:
                temp_name = handle.name

                def emit(operation: str, payload: Mapping[str, Any]) -> None:
                    nonlocal output_record_count
                    encoded = _write_line(handle, _operation_record(operation, payload))
                    output_digest.update(encoded)
                    operation_counts[operation] += 1
                    output_record_count += 1

                for kind, _record_id, _record_hash, payload_json in index.execute(
                    """
                    SELECT kind, record_id, record_hash, payload_json FROM changed_records
                    ORDER BY CASE kind WHEN 'node' THEN 0 ELSE 1 END, record_id
                    """
                ):
                    emit(f"upsert_{kind}", json.loads(payload_json))
                # Relationship tombstones are deliberately listed before nodes so a
                # later, approved applier can respect referential constraints.
                for kind, record_id, record_hash in index.execute(
                    """
                    SELECT p.kind, p.record_id, p.record_hash
                    FROM previous_records p
                    LEFT JOIN current_ids c ON c.kind = p.kind AND c.record_id = p.record_id
                    WHERE c.record_id IS NULL
                    ORDER BY CASE p.kind WHEN 'relationship' THEN 0 ELSE 1 END, p.record_id
                    """
                ):
                    emit(
                        f"tombstone_{kind}",
                        {
                            "id": record_id,
                            "previous_record_sha256": record_hash,
                            "requires_operator_approval": True,
                            "action": "plan_only_no_apply",
                        },
                    )

                for operation in (
                    "upsert_node", "upsert_relationship",
                    "tombstone_relationship", "tombstone_node",
                ):
                    operation_counts.setdefault(operation, 0)
                manifest = {
                    "schema": GOLD_INCREMENTAL_SCHEMA,
                    "mode": "dry_run" if dry_run else "plan_written",
                    "operation_count": output_record_count,
                    "operation_counts": dict(sorted(operation_counts.items())),
                    "expected_change_records": change_count,
                    "expected_tombstone_records": tombstone_count,
                    "operations_sha256": output_digest.hexdigest(),
                    "records_before_manifest": output_record_count,
                    "source_fingerprints": {
                        "previous": _source_fingerprint(previous_manifest),
                        "current": _source_fingerprint(current_manifest),
                    },
                    "tombstones_are_plans_only": True,
                    "requires_operator_approval_for_tombstones": True,
                    "read_only": True,
                    "db_writes": False,
                    "source_files_modified": False,
                    "approval_claim": False,
                    "max_record_bytes": MAX_RECORD_BYTES,
                }
                _write_line(handle, _operation_record("manifest", manifest))
                handle.flush()
                os.fsync(handle.fileno())
            if destination is not None:
                Path(temp_name).replace(destination)
                temp_name = None
            return manifest
        finally:
            if temp_name is not None:
                Path(temp_name).unlink(missing_ok=True)
            if 'temp_dir' in locals() and temp_dir is not None:
                temp_dir.cleanup()


__all__ = [
    "GOLD_INCREMENTAL_SCHEMA",
    "GoldIncrementalValidationError",
    "MAX_RECORD_BYTES",
    "plan_gold_lpg_incremental",
]
