"""Guarded, streaming loader for a serving-core Gold LPG NDJSON artifact.

The SQLite exporter is the authority for graph content.  This module consumes
its immutable NDJSON artifact without building an in-memory projection, then
optionally sends idempotent, fixed-template batches to Neo4j.  ``apply=False``
is deliberately the default: validating an artifact must never require the
optional Neo4j dependency or any network credentials.

The loader has no deletion or source-DB mutation capability.  A checkpoint is
an operational resume marker only; it never grants approval to source data.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
import time
from typing import Any

from .gold_lpg import (
    ALLOWED_NODE_IMPORT_GROUPS,
    ALLOWED_RELATIONSHIP_TYPES,
    NEO4J_DOMAIN_EDGE_MERGE_CYPHER,
    NEO4J_DOMAIN_NODE_MERGE_CYPHER,
    NEO4J_SCHEMA_DDL,
    neo4j_vector_index_ddl,
)
from .gold_stream import GOLD_LPG_NDJSON_SCHEMA
from .embedding_export import (
    _iter_prevalidated_gold_embedding_patches,
    inspect_gold_embedding_patches,
    inspect_gold_embedding_shard_manifest,
)


GOLD_NEO4J_LOAD_SCHEMA = "ncs_gold_neo4j_load_v1"
GOLD_NEO4J_CHECKPOINT_SCHEMA = "ncs_gold_neo4j_checkpoint_v1"
GOLD_EMBEDDING_SHARD_LEDGER_SCHEMA = "ncs_gold_embedding_shard_ledger_v1"
_DEFAULT_BATCH_SIZE = 1_000
_MAX_BATCH_SIZE = 25_000
_DEFAULT_QUERY_TIMEOUT_SECONDS = 30.0
_DEFAULT_CONNECT_TIMEOUT_SECONDS = 10.0
_MAX_TIMEOUT_SECONDS = 300.0

_ENV_ENABLED = "NCS_MCP_GOLD_ENABLED"
_ENV_URI = "NCS_MCP_GOLD_URI"
_ENV_USERNAME = "NCS_MCP_GOLD_USERNAME"
_ENV_PASSWORD = "NCS_MCP_GOLD_PASSWORD"
_ENV_DATABASE = "NCS_MCP_GOLD_DATABASE"
_ENV_CONNECT_TIMEOUT_SECONDS = "NCS_MCP_GOLD_CONNECT_TIMEOUT_SECONDS"
_ENV_QUERY_TIMEOUT_SECONDS = "NCS_MCP_GOLD_QUERY_TIMEOUT_SECONDS"
_LOADER_ENV_ALLOWLIST = frozenset(
    {
        _ENV_ENABLED,
        _ENV_URI,
        _ENV_USERNAME,
        _ENV_PASSWORD,
        _ENV_DATABASE,
        _ENV_CONNECT_TIMEOUT_SECONDS,
        _ENV_QUERY_TIMEOUT_SECONDS,
    }
)
_EMBEDDING_ENTITY_LABELS = frozenset(
    {"PerformanceCriterion", "PerformanceElement", "KSAConcept"}
)
_EMBEDDING_PATCH_SCHEMA = "ncs_embedding_node_patch_v1"
_EMBEDDING_PATCH_CYPHER = {
    label: (
        "UNWIND $patches AS row\n"
        f"MATCH (node:LpgNode:{label} {{id: row.entity_id}})\n"
        "SET node.embedding = row.embedding,\n"
        "    node.embedding_content_hash = row.content_hash,\n"
        "    node.embedding_cache_key = row.cache_key,\n"
        "    node.embedding_provider = row.provider,\n"
        "    node.embedding_model = row.model,\n"
        "    node.embedding_dimensions = row.dimensions\n"
        "RETURN count(node) AS matched_count"
    )
    for label in _EMBEDDING_ENTITY_LABELS
}
_EMBEDDING_RECEIPT_READ_CYPHER = (
    "MATCH (receipt:NcsEmbeddingShardReceipt {manifest_sha256: $manifest_sha256})\n"
    "RETURN receipt.shard_index AS shard_index, receipt.shard_sha256 AS shard_sha256"
)
_EMBEDDING_RECEIPT_WRITE_CYPHER = (
    "MERGE (receipt:NcsEmbeddingShardReceipt {manifest_sha256: $manifest_sha256, "
    "shard_index: $shard_index})\n"
    "SET receipt.shard_sha256 = $shard_sha256,\n"
    "    receipt.plan_fingerprint = $plan_fingerprint,\n"
    "    receipt.gold_records_sha256 = $gold_records_sha256"
)


class GoldLoadValidationError(ValueError):
    """The input artifact or resume marker violates the loader contract."""


class GoldLoadUnavailableError(RuntimeError):
    """A guarded apply was requested but a Neo4j backend is unavailable."""


class GoldLoadExecutionError(RuntimeError):
    """A Neo4j write batch failed after bounded retries."""


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _positive_timeout(value: object, default: float) -> float:
    text = _text(value)
    if text is None:
        return default
    try:
        parsed = float(text)
    except ValueError as exc:
        raise GoldLoadValidationError("loader timeout is invalid") from exc
    if not 0 < parsed <= _MAX_TIMEOUT_SECONDS:
        raise GoldLoadValidationError("loader timeout is invalid")
    return parsed


@dataclass(frozen=True, slots=True)
class Neo4jLoaderSettings:
    """Secret-safe configuration read from the narrow Gold environment scope."""

    enabled: bool = False
    uri: str | None = field(default=None, repr=False)
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    database: str | None = None
    connect_timeout_seconds: float = _DEFAULT_CONNECT_TIMEOUT_SECONDS
    query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None
    ) -> "Neo4jLoaderSettings":
        source = os.environ if environ is None else environ
        enabled = (_text(source.get(_ENV_ENABLED)) or "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        return cls(
            enabled=enabled,
            uri=_text(source.get(_ENV_URI)),
            username=_text(source.get(_ENV_USERNAME)),
            password=_text(source.get(_ENV_PASSWORD)),
            database=_text(source.get(_ENV_DATABASE)),
            connect_timeout_seconds=_positive_timeout(
                source.get(_ENV_CONNECT_TIMEOUT_SECONDS),
                _DEFAULT_CONNECT_TIMEOUT_SECONDS,
            ),
            query_timeout_seconds=_positive_timeout(
                source.get(_ENV_QUERY_TIMEOUT_SECONDS), _DEFAULT_QUERY_TIMEOUT_SECONDS
            ),
        )

    def validation_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not self.enabled:
            errors.append("gold_loader_disabled")
        for name, value in (
            ("uri", self.uri),
            ("username", self.username),
            ("password", self.password),
            ("database", self.database),
        ):
            if _text(value) is None:
                errors.append(f"{name}_missing")
        for name, value in (
            ("connect_timeout", self.connect_timeout_seconds),
            ("query_timeout", self.query_timeout_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 < float(value) <= _MAX_TIMEOUT_SECONDS
            ):
                errors.append(f"{name}_invalid")
        return tuple(errors)

    def readiness(self) -> dict[str, Any]:
        errors = self.validation_errors()
        return {
            "enabled": self.enabled,
            "configured": not errors,
            "uri_present": _text(self.uri) is not None,
            "username_present": _text(self.username) is not None,
            "password_present": _text(self.password) is not None,
            "database_present": _text(self.database) is not None,
            "issues": list(errors),
        }


def _node_group(node: Mapping[str, Any]) -> str:
    labels = node.get("labels")
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(label, str) for label in labels)
    ):
        raise GoldLoadValidationError("node labels must be a non-empty string list")
    received = set(labels)
    for group, expression in ALLOWED_NODE_IMPORT_GROUPS.items():
        expected = {"LpgNode", *expression.split(":")}
        if received == expected and len(labels) == len(received):
            return group
    raise GoldLoadValidationError("node labels are outside the Gold LPG allowlist")


def _validate_provenance(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GoldLoadValidationError("record provenance must be an object")
    if _text(value.get("source_table")) is None or value.get("source_key") is None:
        raise GoldLoadValidationError("record provenance is incomplete")
    return value


def _validate_node(node: object) -> tuple[str, str]:
    if not isinstance(node, Mapping):
        raise GoldLoadValidationError("node record must be an object")
    identifier = _text(node.get("id"))
    if identifier is None or not identifier.startswith("ncs:"):
        raise GoldLoadValidationError("node id must be an ncs identifier")
    group = _node_group(node)
    properties = node.get("properties")
    if (
        not isinstance(properties, Mapping)
        or properties.get("id") != identifier
        or _text(properties.get("node_type")) is None
    ):
        raise GoldLoadValidationError(
            "node properties must include matching id and node_type"
        )
    _validate_provenance(node.get("provenance"))
    return identifier, group


def _validate_relationship(edge: object) -> tuple[str, str]:
    if not isinstance(edge, Mapping):
        raise GoldLoadValidationError("relationship record must be an object")
    identifier, source, target, relationship_type = (
        _text(edge.get("id")),
        _text(edge.get("source")),
        _text(edge.get("target")),
        _text(edge.get("type")),
    )
    if None in (identifier, source, target) or not all(
        value.startswith("ncs:") for value in (identifier, source, target)
    ):
        raise GoldLoadValidationError(
            "relationship identifiers must be ncs identifiers"
        )
    if relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
        raise GoldLoadValidationError(
            "relationship type is outside the Gold LPG allowlist"
        )
    properties = edge.get("properties")
    if (
        not isinstance(properties, Mapping)
        or properties.get("id") != identifier
        or properties.get("edge_type") != relationship_type
    ):
        raise GoldLoadValidationError(
            "relationship properties must include matching id and edge_type"
        )
    _validate_provenance(edge.get("provenance"))
    return identifier, relationship_type


class _NodeIdIndex:
    """Disk-backed node-ID index; validation stays bounded for large exports."""

    def __init__(self) -> None:
        self._directory = tempfile.TemporaryDirectory(prefix="ncs-gold-node-index-")
        self._path = Path(self._directory.name) / "nodes.sqlite"
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("CREATE TABLE node_ids (id TEXT PRIMARY KEY)")
        self._conn.execute("CREATE TABLE edge_ids (id TEXT PRIMARY KEY)")
        self._pending: list[tuple[str]] = []
        self._pending_edges: list[tuple[str]] = []

    def add(self, identifier: str) -> None:
        self._pending.append((identifier,))
        if len(self._pending) >= _DEFAULT_BATCH_SIZE:
            self.flush()

    def flush(self) -> None:
        if not self._pending:
            return
        try:
            self._conn.executemany("INSERT INTO node_ids(id) VALUES (?)", self._pending)
        except sqlite3.IntegrityError as exc:
            raise GoldLoadValidationError("duplicate node id in NDJSON") from exc
        self._pending.clear()

    def contains(self, identifier: str) -> bool:
        self.flush()
        return (
            self._conn.execute(
                "SELECT 1 FROM node_ids WHERE id = ?", (identifier,)
            ).fetchone()
            is not None
        )

    def add_edge(self, identifier: str) -> None:
        self._pending_edges.append((identifier,))
        if len(self._pending_edges) >= _DEFAULT_BATCH_SIZE:
            self.flush_edges()

    def flush_edges(self) -> None:
        if not self._pending_edges:
            return
        try:
            self._conn.executemany(
                "INSERT INTO edge_ids(id) VALUES (?)", self._pending_edges
            )
        except sqlite3.IntegrityError as exc:
            raise GoldLoadValidationError(
                "duplicate relationship id in NDJSON"
            ) from exc
        self._pending_edges.clear()

    def close(self) -> None:
        self._conn.close()
        self._directory.cleanup()


def _iter_records(path: Path) -> Iterator[tuple[int, bytes, Mapping[str, Any]]]:
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.strip():
                raise GoldLoadValidationError("NDJSON must not contain blank records")
            try:
                record = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GoldLoadValidationError(
                    f"invalid NDJSON record at line {line_number}"
                ) from exc
            if not isinstance(record, Mapping):
                raise GoldLoadValidationError(
                    f"NDJSON record at line {line_number} must be an object"
                )
            yield line_number, raw, record


def inspect_gold_lpg_ndjson(ndjson_path: str | Path) -> dict[str, Any]:
    """Validate artifact order, references, manifest counts, and raw digest.

    The temporary node-ID index is intentionally disk-backed.  It is never the
    source SQLite database and disappears before this function returns.
    """

    source = Path(ndjson_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Gold LPG NDJSON does not exist: {source}")
    stage = "node"
    digest = hashlib.sha256()
    record_count = node_count = edge_count = diagnostic_count = 0
    node_counts: Counter[str] = Counter()
    edge_counts: Counter[str] = Counter()
    node_group_counts: Counter[str] = Counter()
    manifest: Mapping[str, Any] | None = None
    index = _NodeIdIndex()
    try:
        for line_number, raw, record in _iter_records(source):
            record_type = record.get("record_type")
            if record_type == "manifest":
                if (
                    manifest is not None
                    or len(record) != 2
                    or not isinstance(record.get("manifest"), Mapping)
                ):
                    raise GoldLoadValidationError(
                        "NDJSON must end with exactly one manifest record"
                    )
                manifest = record["manifest"]
                stage = "manifest"
                continue
            if stage == "manifest":
                raise GoldLoadValidationError("NDJSON has records after its manifest")
            digest.update(raw)
            record_count += 1
            if record_type == "node":
                if stage != "node" or len(record) != 2:
                    raise GoldLoadValidationError(
                        "node records must precede relationships"
                    )
                identifier, group = _validate_node(record.get("node"))
                index.add(identifier)
                node_count += 1
                node_group_counts[group] += 1
                node_counts[str(record["node"]["properties"]["node_type"])] += 1
            elif record_type == "relationship":
                if stage == "diagnostic" or len(record) != 2:
                    raise GoldLoadValidationError(
                        "relationship records must precede diagnostics"
                    )
                stage = "relationship"
                edge = record.get("relationship")
                edge_id, edge_type = _validate_relationship(edge)
                if not index.contains(str(edge["source"])) or not index.contains(
                    str(edge["target"])
                ):
                    raise GoldLoadValidationError(
                        "relationship references a node absent from the artifact"
                    )
                index.add_edge(edge_id)
                edge_count += 1
                edge_counts[edge_type] += 1
            elif record_type == "diagnostic":
                if (
                    not isinstance(record.get("diagnostic"), Mapping)
                    or len(record) != 2
                ):
                    raise GoldLoadValidationError("diagnostic record must be an object")
                stage = "diagnostic"
                diagnostic_count += 1
            else:
                raise GoldLoadValidationError(
                    f"unsupported NDJSON record type at line {line_number}"
                )
        if manifest is None:
            raise GoldLoadValidationError("NDJSON is missing its final manifest")
        index.flush()
        index.flush_edges()
        expected = {
            "schema": GOLD_LPG_NDJSON_SCHEMA,
            "records_before_manifest": record_count,
            "records_sha256": digest.hexdigest(),
            "node_count": node_count,
            "edge_count": edge_count,
            "diagnostic_count": diagnostic_count,
            "node_counts": dict(sorted(node_counts.items())),
            "edge_counts": dict(sorted(edge_counts.items())),
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise GoldLoadValidationError(
                    f"NDJSON manifest {key} does not match records"
                )
        return {
            "schema": GOLD_NEO4J_LOAD_SCHEMA,
            "ndjson_path": str(source),
            "records_sha256": digest.hexdigest(),
            "records_before_manifest": record_count,
            "import_record_count": node_count + edge_count,
            "node_count": node_count,
            "edge_count": edge_count,
            "node_counts": dict(sorted(node_counts.items())),
            "node_group_counts": dict(sorted(node_group_counts.items())),
            "edge_counts": dict(sorted(edge_counts.items())),
            "manifest": dict(manifest),
            "source_db_writes": False,
            "approval_claim": False,
        }
    finally:
        index.close()


def _read_checkpoint(path: Path, inspection: Mapping[str, Any]) -> int:
    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldLoadValidationError("loader checkpoint is unreadable") from exc
    if (
        not isinstance(data, Mapping)
        or data.get("schema") != GOLD_NEO4J_CHECKPOINT_SCHEMA
    ):
        raise GoldLoadValidationError("loader checkpoint schema is invalid")
    if data.get("records_sha256") != inspection["records_sha256"]:
        raise GoldLoadValidationError(
            "loader checkpoint belongs to a different NDJSON artifact"
        )
    completed = data.get("completed_import_records")
    if (
        isinstance(completed, bool)
        or not isinstance(completed, int)
        or not 0 <= completed <= inspection["import_record_count"]
    ):
        raise GoldLoadValidationError(
            "loader checkpoint completed_import_records is invalid"
        )
    return completed


def _atomic_checkpoint(
    path: Path, inspection: Mapping[str, Any], completed_records: int
) -> None:
    checkpoint = {
        "schema": GOLD_NEO4J_CHECKPOINT_SCHEMA,
        "records_sha256": inspection["records_sha256"],
        "records_before_manifest": inspection["records_before_manifest"],
        "completed_import_records": completed_records,
        "read_only_source": True,
        "approval_claim": False,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp = Path(handle.name)
            json.dump(
                checkpoint,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        # On Windows a short-lived reader, antivirus scanner, or indexing
        # process can temporarily deny replacement of the destination.  The
        # graph batch is idempotent, so use a small bounded retry window rather
        # than aborting an otherwise healthy multi-million-record sync.
        for attempt in range(8):
            try:
                temp.replace(path)
                temp = None
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(min(0.05 * (2**attempt), 1.0))
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _validate_checkpoint_path(
    ndjson_path: Path, checkpoint_path: str | Path | None
) -> Path | None:
    if checkpoint_path is None:
        return None
    checkpoint = Path(checkpoint_path).resolve()
    if checkpoint == ndjson_path:
        raise GoldLoadValidationError(
            "checkpoint path must not overwrite the NDJSON artifact"
        )
    if checkpoint.exists() and checkpoint.is_dir():
        raise IsADirectoryError(f"checkpoint path is a directory: {checkpoint}")
    return checkpoint


def _default_driver_factory(
    settings: Neo4jLoaderSettings,
) -> tuple[Any, Callable[..., Any]]:
    try:
        neo4j = importlib.import_module("neo4j")
        driver = neo4j.GraphDatabase.driver(
            settings.uri,
            auth=(settings.username, settings.password),
            connection_timeout=settings.connect_timeout_seconds,
        )
        driver.verify_connectivity()
        return driver, neo4j.Query
    except Exception:
        raise GoldLoadUnavailableError("Neo4j loader backend is unavailable") from None


def _run_write(
    driver: Any,
    query_factory: Callable[..., Any],
    cypher: str,
    parameter_name: str,
    rows: list[Mapping[str, Any]],
    settings: Neo4jLoaderSettings,
) -> Any:
    try:
        query = query_factory(cypher, timeout=settings.query_timeout_seconds)
        return driver.execute_query(
            query,
            parameters_={parameter_name: rows},
            database_=settings.database,
            routing_="w",
        )
    except Exception:
        raise GoldLoadExecutionError("Neo4j write batch failed") from None


def _run_with_retries(
    operation: Callable[[], Any], *, max_retries: int, sleep: Callable[[float], None]
) -> Any:
    for attempt in range(max_retries + 1):
        try:
            return operation()
        except GoldLoadExecutionError:
            if attempt >= max_retries:
                raise
            sleep(min(2**attempt, 4.0))
    raise AssertionError("unreachable")  # pragma: no cover


def _iter_import_records(
    path: Path,
) -> Iterator[tuple[int, str, Mapping[str, Any], str]]:
    """Yield validated node/edge records in source order for an already-inspected file."""

    record_index = 0
    for _, _, record in _iter_records(path):
        record_type = record.get("record_type")
        if record_type not in {"node", "relationship"}:
            continue
        record_index += 1
        if record_type == "node":
            _, group = _validate_node(record["node"])
            yield record_index, group, record["node"], "nodes"
        else:
            _, relationship_type = _validate_relationship(record["relationship"])
            yield record_index, relationship_type, record["relationship"], "edges"


def _flush_batch(
    driver: Any,
    query_factory: Callable[..., Any],
    settings: Neo4jLoaderSettings,
    batch: list[Mapping[str, Any]],
    group: str,
    parameter_name: str,
    *,
    max_retries: int,
    sleep: Callable[[float], None],
) -> None:
    cypher = (
        NEO4J_DOMAIN_NODE_MERGE_CYPHER[group]
        if parameter_name == "nodes"
        else NEO4J_DOMAIN_EDGE_MERGE_CYPHER[group]
    )
    _run_with_retries(
        lambda: _run_write(
            driver, query_factory, cypher, parameter_name, batch, settings
        ),
        max_retries=max_retries,
        sleep=sleep,
    )


def _validate_embedding_patch(patch: object) -> tuple[str, dict[str, Any]]:
    """Normalize a patch to the seven fixed properties accepted by Gold."""

    if not isinstance(patch, Mapping):
        raise GoldLoadValidationError("embedding patch must be an object")
    if patch.get("schema") != _EMBEDDING_PATCH_SCHEMA:
        raise GoldLoadValidationError("embedding patch schema is invalid")
    entity_type = _text(patch.get("entity_type"))
    entity_id = _text(patch.get("entity_id"))
    if (
        entity_type not in _EMBEDDING_ENTITY_LABELS
        or entity_id is None
        or not entity_id.startswith("ncs:")
    ):
        raise GoldLoadValidationError("embedding patch entity is not allowlisted")
    embedding = patch.get("embedding")
    if not isinstance(embedding, (list, tuple)) or not embedding:
        raise GoldLoadValidationError("embedding patch vector is invalid")
    normalized_embedding: list[float] = []
    for value in embedding:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise GoldLoadValidationError("embedding patch vector is invalid")
        normalized_embedding.append(float(value))
    dimensions = patch.get("dimensions")
    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or dimensions != len(normalized_embedding)
        or not 1 <= dimensions <= 8192
    ):
        raise GoldLoadValidationError("embedding patch dimensions are invalid")
    required_text = {
        name: _text(patch.get(name))
        for name in ("content_hash", "cache_key", "provider", "model")
    }
    if any(value is None for value in required_text.values()):
        raise GoldLoadValidationError("embedding patch metadata is incomplete")
    return entity_type, {
        "entity_id": entity_id,
        "embedding": normalized_embedding,
        "content_hash": required_text["content_hash"],
        "cache_key": required_text["cache_key"],
        "provider": required_text["provider"],
        "model": required_text["model"],
        "dimensions": dimensions,
    }


def _require_embedding_match_count(result: Any, expected_count: int) -> None:
    """Fail closed when a fixed-label MATCH does not cover its full batch."""

    if not isinstance(result, tuple) or not result or not isinstance(result[0], list):
        raise GoldLoadExecutionError("embedding patch match count is invalid")
    rows = result[0]
    if len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise GoldLoadExecutionError("embedding patch match count is invalid")
    matched_count = rows[0].get("matched_count")
    if (
        isinstance(matched_count, bool)
        or not isinstance(matched_count, int)
        or matched_count != expected_count
    ):
        raise GoldLoadExecutionError(
            "embedding patch match count does not cover its batch"
        )


def apply_embedding_patches(
    patches: Iterator[Mapping[str, Any]] | list[Mapping[str, Any]],
    *,
    apply: bool = False,
    settings: Neo4jLoaderSettings | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    max_retries: int = 2,
    driver_factory: Callable[
        [Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]
    ] = _default_driver_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate and explicitly apply allowed embedding-only node updates.

    It accepts provider-neutral records from ``embedding_batches``.  Metadata
    and arbitrary properties are intentionally ignored; only the fixed vector
    provenance fields in :data:`_EMBEDDING_PATCH_CYPHER` can reach Neo4j.
    """

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= _MAX_BATCH_SIZE
    ):
        raise ValueError(
            f"batch_size must be an integer between 1 and {_MAX_BATCH_SIZE}"
        )
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 10
    ):
        raise ValueError("max_retries must be an integer between 0 and 10")
    groups: dict[str, list[dict[str, Any]]] = {
        label: [] for label in _EMBEDDING_ENTITY_LABELS
    }
    counts: Counter[str] = Counter()
    driver: Any | None = None
    query_factory: Callable[..., Any] | None = None
    if apply:
        resolved = settings or Neo4jLoaderSettings.from_env()
        if resolved.validation_errors():
            raise GoldLoadUnavailableError("Neo4j loader configuration is unavailable")
        driver, query_factory = driver_factory(resolved)
    else:
        resolved = None
    batches_written = 0

    def flush(label: str, batch: list[dict[str, Any]]) -> None:
        nonlocal batches_written
        result = _run_with_retries(
            lambda: _run_write(
                driver,
                query_factory,
                _EMBEDDING_PATCH_CYPHER[label],
                "patches",
                batch,
                resolved,
            ),
            max_retries=max_retries,
            sleep=sleep,
        )
        _require_embedding_match_count(result, len(batch))
        batches_written += 1

    try:
        for patch in patches:
            label, normalized = _validate_embedding_patch(patch)
            groups[label].append(normalized)
            counts[label] += 1
            if len(groups[label]) >= batch_size:
                if apply:
                    flush(label, groups[label])
                groups[label] = []
        for label, batch in groups.items():
            if batch and apply:
                flush(label, batch)
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass
    return {
        "schema": GOLD_NEO4J_LOAD_SCHEMA,
        "mode": "applied" if apply else "dry_run",
        "embedding_patch_counts": dict(sorted(counts.items())),
        "batches_written": batches_written,
        "neo4j_writes": bool(apply),
        "allowed_properties": [
            "embedding",
            "embedding_content_hash",
            "embedding_cache_key",
            "embedding_provider",
            "embedding_model",
            "embedding_dimensions",
        ],
        "approval_claim": False,
    }


def _atomic_embedding_ledger(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolved_shard_path(manifest_path: Path, relative: object) -> Path:
    if (
        not isinstance(relative, str)
        or Path(relative).is_absolute()
        or ".." in Path(relative).parts
    ):
        raise GoldLoadValidationError("embedding shard path is unsafe")
    try:
        root = manifest_path.parent.resolve(strict=True)
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root)
    except (OSError, ValueError) as exc:
        raise GoldLoadValidationError(
            "embedding shard path escapes manifest directory"
        ) from exc
    return candidate


def _receipt_rows(result: Any) -> list[Mapping[str, Any]]:
    if not isinstance(result, tuple) or not result:
        raise GoldLoadExecutionError(
            "embedding shard receipt query returned an invalid result"
        )
    rows = result[0]
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise GoldLoadExecutionError(
            "embedding shard receipt query returned an invalid result"
        )
    return rows


def _read_server_embedding_receipts(
    manifest_sha256: str,
    *,
    settings: Neo4jLoaderSettings,
    driver_factory: Callable[[Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]],
) -> dict[int, str]:
    driver, query_factory = driver_factory(settings)
    try:
        query = query_factory(
            _EMBEDDING_RECEIPT_READ_CYPHER, timeout=settings.query_timeout_seconds
        )
        rows = _receipt_rows(
            driver.execute_query(
                query,
                parameters_={"manifest_sha256": manifest_sha256},
                database_=settings.database,
                routing_="r",
            )
        )
    except GoldLoadExecutionError:
        raise
    except Exception:
        raise GoldLoadExecutionError("embedding shard receipt query failed") from None
    finally:
        try:
            driver.close()
        except Exception:
            pass
    receipts: dict[int, str] = {}
    for row in rows:
        index, digest = row.get("shard_index"), row.get("shard_sha256")
        if (
            isinstance(index, int)
            and not isinstance(index, bool)
            and isinstance(digest, str)
        ):
            receipts[index] = digest
        else:
            raise GoldLoadExecutionError(
                "embedding shard receipt query returned invalid data"
            )
    return receipts


def _write_server_embedding_receipt(
    *,
    manifest_sha256: str,
    shard_index: int,
    shard_sha256: str,
    plan_fingerprint: str,
    gold_records_sha256: str,
    settings: Neo4jLoaderSettings,
    driver_factory: Callable[[Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]],
    max_retries: int,
    sleep: Callable[[float], None],
) -> None:
    driver, query_factory = driver_factory(settings)
    try:

        def write() -> Any:
            try:
                query = query_factory(
                    _EMBEDDING_RECEIPT_WRITE_CYPHER,
                    timeout=settings.query_timeout_seconds,
                )
                return driver.execute_query(
                    query,
                    parameters_={
                        "manifest_sha256": manifest_sha256,
                        "shard_index": shard_index,
                        "shard_sha256": shard_sha256,
                        "plan_fingerprint": plan_fingerprint,
                        "gold_records_sha256": gold_records_sha256,
                    },
                    database_=settings.database,
                    routing_="w",
                )
            except Exception:
                raise GoldLoadExecutionError(
                    "embedding shard receipt write failed"
                ) from None

        _run_with_retries(write, max_retries=max_retries, sleep=sleep)
    finally:
        try:
            driver.close()
        except Exception:
            pass


def _read_embedding_ledger(
    path: Path,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
) -> set[int]:
    expected = {str(item["index"]): str(item["sha256"]) for item in manifest["shards"]}
    if not path.exists():
        return set()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GoldLoadValidationError("embedding shard ledger is unreadable") from exc
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != GOLD_EMBEDDING_SHARD_LEDGER_SCHEMA
    ):
        raise GoldLoadValidationError("embedding shard ledger schema is invalid")
    if (
        value.get("manifest_fingerprint") != manifest.get("plan_fingerprint")
        or value.get("manifest_sha256") != manifest_sha256
        or value.get("shards") != expected
    ):
        raise GoldLoadValidationError(
            "embedding shard ledger is bound to a different manifest"
        )
    completed = value.get("completed")
    if not isinstance(completed, list) or any(
        not isinstance(item, int) for item in completed
    ):
        raise GoldLoadValidationError("embedding shard ledger completion is invalid")
    if any(str(item) not in expected for item in completed):
        raise GoldLoadValidationError("embedding shard ledger completion is invalid")
    return set(completed)


def apply_embedding_shards(
    manifest_path: str | Path,
    *,
    apply: bool = False,
    reconciled: bool = False,
    ledger_path: str | Path | None = None,
    create_indexes: bool = True,
    settings: Neo4jLoaderSettings | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    max_retries: int = 2,
    driver_factory: Callable[
        [Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]
    ] = _default_driver_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate and resumably apply a final embedding shard manifest.

    A local ledger is only advanced after one whole validated shard returns.
    Consequently a failed or interrupted shard is replayed idempotently on the
    next explicit apply.  Vector indexes are deferred until every shard is
    ledger-complete.
    """

    manifest_source = Path(manifest_path)
    manifest = inspect_gold_embedding_shard_manifest(manifest_source)
    manifest_sha256 = _file_sha256(manifest_source)
    if apply and not reconciled:
        raise GoldLoadValidationError(
            "embedding shard apply requires a reconciled Gold graph"
        )
    if apply:
        resolved = settings or Neo4jLoaderSettings.from_env()
        if resolved.validation_errors():
            raise GoldLoadUnavailableError("Neo4j loader configuration is unavailable")
    else:
        resolved = None
        indexes = (
            apply_vector_indexes(
                int(manifest["plan"]["dimensions"]),
                apply=False,
                settings=settings,
                max_retries=max_retries,
                driver_factory=driver_factory,
                sleep=sleep,
            )
            if create_indexes
            else None
        )
        return {
            "schema": GOLD_NEO4J_LOAD_SCHEMA,
            "mode": "dry_run",
            "manifest_fingerprint": manifest["plan_fingerprint"],
            "completed_shards": [],
            "shard_count": len(manifest["shards"]),
            "batches_written": 0,
            "embedding_patch_counts": dict(manifest["patch_counts"]),
            "vector_indexes": indexes,
            "neo4j_writes": False,
            "source_db_writes": False,
            "approval_claim": False,
        }
    if ledger_path is None:
        ledger = manifest_source.with_name("ncs_gold_embeddings.apply-ledger.json")
    else:
        ledger = Path(ledger_path)
    if ledger.resolve() == manifest_source.resolve():
        raise GoldLoadValidationError(
            "embedding shard ledger must not overwrite the manifest"
        )
    local_completed = (
        _read_embedding_ledger(ledger, manifest, manifest_sha256) if apply else set()
    )
    expected_shas = {
        str(item["index"]): str(item["sha256"]) for item in manifest["shards"]
    }
    server_receipts = (
        _read_server_embedding_receipts(
            manifest_sha256,
            settings=resolved,
            driver_factory=driver_factory,
        )
        if apply
        else {}
    )
    completed: set[int] = {
        index
        for index in local_completed
        if server_receipts.get(index) == expected_shas[str(index)]
    }
    written = 0
    applied_counts: Counter[str] = Counter()
    for shard in manifest["shards"]:
        index = int(shard["index"])
        if server_receipts.get(index) == shard["sha256"]:
            completed.add(index)
            continue
        artifact = _resolved_shard_path(manifest_source, shard["path"])
        # The final manifest was fully validated once above.  Revalidate only
        # this pending shard immediately before its mutation, not all shards.
        inspection = inspect_gold_embedding_patches(artifact)
        binding = {
            "plan_fingerprint": manifest["plan_fingerprint"],
            "source_db_sha256": manifest["source_db_sha256"],
            "gold_records_sha256": manifest["gold_records_sha256"],
            "index": index,
            "entity_type": shard["entity_type"],
            "source_key_min": shard["source_key_min"],
            "source_key_max": shard["source_key_max"],
        }
        if (
            _file_sha256(artifact) != shard["sha256"]
            or inspection["header"].get("shard_binding") != binding
            or inspection["manifest"].get("shard_binding") != binding
        ):
            raise GoldLoadValidationError("embedding shard changed before apply")
        result = apply_embedding_patches(
            _iter_prevalidated_gold_embedding_patches(artifact),
            apply=apply,
            settings=settings,
            batch_size=batch_size,
            max_retries=max_retries,
            driver_factory=driver_factory,
            sleep=sleep,
        )
        applied_counts.update(result["embedding_patch_counts"])
        written += int(result["batches_written"])
        if apply:
            _write_server_embedding_receipt(
                manifest_sha256=manifest_sha256,
                shard_index=index,
                shard_sha256=str(shard["sha256"]),
                plan_fingerprint=str(manifest["plan_fingerprint"]),
                gold_records_sha256=str(manifest["gold_records_sha256"]),
                settings=resolved,
                driver_factory=driver_factory,
                max_retries=max_retries,
                sleep=sleep,
            )
            server_receipts[index] = str(shard["sha256"])
            completed.add(index)
            _atomic_embedding_ledger(
                ledger,
                {
                    "schema": GOLD_EMBEDDING_SHARD_LEDGER_SCHEMA,
                    "manifest_fingerprint": manifest["plan_fingerprint"],
                    "manifest_sha256": manifest_sha256,
                    "shards": expected_shas,
                    "completed": sorted(completed),
                    "approval_claim": False,
                },
            )
    planned_indices = {int(shard["index"]) for shard in manifest["shards"]}
    if apply:
        server_receipts = _read_server_embedding_receipts(
            manifest_sha256,
            settings=resolved,
            driver_factory=driver_factory,
        )
    all_complete = (
        planned_indices
        and all(
            server_receipts.get(index) == expected_shas[str(index)]
            for index in planned_indices
        )
        if apply
        else True
    )
    indexes = None
    if create_indexes and apply and not all_complete:
        raise GoldLoadExecutionError(
            "embedding shard receipts are incomplete; indexes were not created"
        )
    if create_indexes and all_complete:
        indexes = apply_vector_indexes(
            int(manifest["plan"]["dimensions"]),
            apply=apply,
            settings=settings,
            max_retries=max_retries,
            driver_factory=driver_factory,
            sleep=sleep,
        )
    return {
        "schema": GOLD_NEO4J_LOAD_SCHEMA,
        "mode": "applied" if apply else "dry_run",
        "manifest_fingerprint": manifest["plan_fingerprint"],
        "completed_shards": sorted(completed),
        "shard_count": len(manifest["shards"]),
        "batches_written": written,
        "embedding_patch_counts": dict(sorted(applied_counts.items())),
        "vector_indexes": indexes,
        "neo4j_writes": bool(apply),
        "source_db_writes": False,
        "approval_claim": False,
    }


def apply_vector_indexes(
    dimensions: int,
    *,
    apply: bool = False,
    settings: Neo4jLoaderSettings | None = None,
    max_retries: int = 2,
    driver_factory: Callable[
        [Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]
    ] = _default_driver_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Validate or explicitly create the fixed Gold vector indexes.

    Index names, labels, properties, and similarity function come exclusively
    from :func:`neo4j_vector_index_ddl`; the caller controls only a validated
    embedding dimensionality.  Dry-run mode never imports the Neo4j driver.
    """

    statements = neo4j_vector_index_ddl(dimensions)
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 10
    ):
        raise ValueError("max_retries must be an integer between 0 and 10")
    if not apply:
        return {
            "schema": GOLD_NEO4J_LOAD_SCHEMA,
            "mode": "dry_run",
            "embedding_dimensions": dimensions,
            "vector_indexes": len(statements),
            "neo4j_writes": False,
            "approval_claim": False,
        }

    resolved = settings or Neo4jLoaderSettings.from_env()
    if resolved.validation_errors():
        raise GoldLoadUnavailableError("Neo4j loader configuration is unavailable")
    driver, query_factory = driver_factory(resolved)
    written = 0
    try:
        for cypher in statements:

            def execute(statement: str = cypher) -> Any:
                try:
                    query = query_factory(
                        statement, timeout=resolved.query_timeout_seconds
                    )
                    return driver.execute_query(
                        query,
                        database_=resolved.database,
                        routing_="w",
                    )
                except Exception:
                    raise GoldLoadExecutionError(
                        "Neo4j vector-index DDL failed"
                    ) from None

            _run_with_retries(execute, max_retries=max_retries, sleep=sleep)
            written += 1
    finally:
        try:
            driver.close()
        except Exception:
            pass
    return {
        "schema": GOLD_NEO4J_LOAD_SCHEMA,
        "mode": "applied",
        "embedding_dimensions": dimensions,
        "vector_indexes": written,
        "neo4j_writes": True,
        "approval_claim": False,
    }


def load_gold_lpg_ndjson(
    ndjson_path: str | Path,
    *,
    apply: bool = False,
    settings: Neo4jLoaderSettings | None = None,
    checkpoint_path: str | Path | None = None,
    batch_size: int = _DEFAULT_BATCH_SIZE,
    max_retries: int = 2,
    apply_schema: bool = True,
    driver_factory: Callable[
        [Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]
    ] = _default_driver_factory,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Inspect, then optionally apply one immutable Gold NDJSON artifact.

    ``apply`` is the explicit mutation gate.  A dry run verifies the complete
    artifact but neither imports ``neo4j`` nor reads the loader environment.
    """

    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= _MAX_BATCH_SIZE
    ):
        raise ValueError(
            f"batch_size must be an integer between 1 and {_MAX_BATCH_SIZE}"
        )
    if (
        isinstance(max_retries, bool)
        or not isinstance(max_retries, int)
        or not 0 <= max_retries <= 10
    ):
        raise ValueError("max_retries must be an integer between 0 and 10")
    source = Path(ndjson_path).resolve()
    checkpoint = _validate_checkpoint_path(source, checkpoint_path)
    inspection = inspect_gold_lpg_ndjson(source)
    if not apply:
        return {
            **inspection,
            "mode": "dry_run",
            "neo4j_writes": False,
            "driver_used": False,
            "checkpoint_written": False,
        }

    resolved = settings or Neo4jLoaderSettings.from_env()
    if resolved.validation_errors():
        raise GoldLoadUnavailableError("Neo4j loader configuration is unavailable")
    completed = _read_checkpoint(checkpoint, inspection) if checkpoint else 0
    driver, query_factory = driver_factory(resolved)
    batches_written = schema_written = 0
    pending: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    pending_count = 0
    last_record = completed

    def flush_window(window_last_record: int) -> None:
        nonlocal batches_written, pending, pending_count, last_record
        # One bounded source window can contain interleaved KSA subtypes or
        # relationship types.  Grouping inside the window avoids thousands of
        # one-row transactions while preserving node-before-relationship order.
        for (parameter_name, group), rows in sorted(
            pending.items(),
            key=lambda item: (
                0 if item[0][0] == "nodes" else 1,
                item[0][1],
            ),
        ):
            _flush_batch(
                driver,
                query_factory,
                resolved,
                rows,
                group,
                parameter_name,
                max_retries=max_retries,
                sleep=sleep,
            )
            batches_written += 1
        # Advance only after every group in the window succeeded.  If one
        # group fails, a retry replays prior MERGEs safely and cannot skip data.
        last_record = window_last_record
        if checkpoint:
            _atomic_checkpoint(checkpoint, inspection, last_record)
        pending = {}
        pending_count = 0

    try:
        if apply_schema:
            for cypher in NEO4J_SCHEMA_DDL:
                _run_with_retries(
                    lambda cypher=cypher: _run_write(
                        driver, query_factory, cypher, "_schema", [], resolved
                    ),
                    max_retries=max_retries,
                    sleep=sleep,
                )
                schema_written += 1
        for record_index, group, payload, parameter_name in _iter_import_records(
            source
        ):
            if record_index <= completed:
                continue
            pending.setdefault((parameter_name, group), []).append(payload)
            pending_count += 1
            if pending_count >= batch_size:
                flush_window(record_index)
        if pending:
            flush_window(record_index)
        if checkpoint and last_record == inspection["import_record_count"]:
            _atomic_checkpoint(checkpoint, inspection, last_record)
    finally:
        try:
            driver.close()
        except Exception:
            pass
    return {
        **inspection,
        "mode": "applied",
        "neo4j_writes": True,
        "schema_statements_written": schema_written,
        "batches_written": batches_written,
        "resumed_from_record": completed,
        "completed_import_records": last_record,
        "checkpoint_path": str(checkpoint) if checkpoint else None,
        "checkpoint_written": checkpoint is not None,
        "approval_claim": False,
    }


def _extract_count(result: Any) -> int:
    records = result[0] if isinstance(result, tuple) else result
    if isinstance(records, list) and records:
        row = records[0]
        value = row.get("count") if isinstance(row, Mapping) else row["count"]
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    raise GoldLoadExecutionError("Neo4j reconciliation returned an invalid count")


def reconcile_gold_lpg(
    inspection: Mapping[str, Any],
    *,
    settings: Neo4jLoaderSettings,
    driver_factory: Callable[
        [Neo4jLoaderSettings], tuple[Any, Callable[..., Any]]
    ] = _default_driver_factory,
) -> dict[str, Any]:
    """Read-only post-load label/type count reconciliation against a manifest."""

    if settings.validation_errors():
        raise GoldLoadUnavailableError("Neo4j loader configuration is unavailable")
    driver, query_factory = driver_factory(settings)
    observed_nodes: dict[str, int] = {}
    observed_edges: dict[str, int] = {}
    try:
        for group, expected in inspection.get("node_group_counts", {}).items():
            labels = ALLOWED_NODE_IMPORT_GROUPS.get(group)
            if labels is None:
                raise GoldLoadValidationError("reconciliation group is not allowlisted")
            cypher = f"MATCH (node:LpgNode:{labels}) RETURN count(node) AS count"
            query = query_factory(cypher, timeout=settings.query_timeout_seconds)
            observed_nodes[group] = _extract_count(
                driver.execute_query(query, database_=settings.database, routing_="r")
            )
        for relationship_type, expected in inspection.get("edge_counts", {}).items():
            if relationship_type not in ALLOWED_RELATIONSHIP_TYPES:
                raise GoldLoadValidationError(
                    "reconciliation relationship type is not allowlisted"
                )
            cypher = (
                f"MATCH ()-[edge:{relationship_type}]->() RETURN count(edge) AS count"
            )
            query = query_factory(cypher, timeout=settings.query_timeout_seconds)
            observed_edges[relationship_type] = _extract_count(
                driver.execute_query(query, database_=settings.database, routing_="r")
            )
    finally:
        try:
            driver.close()
        except Exception:
            pass
    expected_nodes = dict(inspection.get("node_group_counts", {}))
    expected_edges = dict(inspection.get("edge_counts", {}))
    return {
        "schema": GOLD_NEO4J_LOAD_SCHEMA,
        "read_only": True,
        "db_writes": False,
        "approval_claim": False,
        "expected_node_group_counts": expected_nodes,
        "observed_node_group_counts": observed_nodes,
        "expected_edge_counts": expected_edges,
        "observed_edge_counts": observed_edges,
        "ok": observed_nodes == expected_nodes and observed_edges == expected_edges,
    }


__all__ = [
    "GOLD_NEO4J_CHECKPOINT_SCHEMA",
    "GOLD_NEO4J_LOAD_SCHEMA",
    "GoldLoadExecutionError",
    "GoldLoadUnavailableError",
    "GoldLoadValidationError",
    "Neo4jLoaderSettings",
    "apply_embedding_patches",
    "apply_embedding_shards",
    "apply_vector_indexes",
    "inspect_gold_lpg_ndjson",
    "load_gold_lpg_ndjson",
    "reconcile_gold_lpg",
]
