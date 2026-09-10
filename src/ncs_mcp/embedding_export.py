"""Atomic, bounded NDJSON exports of provider-neutral Gold embedding patches.

This module bridges the read-only SQLite semantic-input batches and a later,
explicit Neo4j embedding-patch apply step.  It never writes either database.
Semantic input text is intentionally kept in-process: the exported artifact
contains vectors and provenance hashes only.
"""

from __future__ import annotations

from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Iterable, Iterator, Mapping

try:
    from pydantic_core import from_json as _decode_json
except ImportError:  # pragma: no cover - mcp normally provides pydantic-core.
    _decode_json = json.loads

from .embedding_batches import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_FETCH_SIZE,
    DEFAULT_MAX_TEXT_CHARS,
    EMBEDDING_NODE_PATCH_SCHEMA,
    MAX_BATCH_SIZE,
    MAX_FETCH_SIZE,
    MAX_TEXT_CHARS,
    SUPPORTED_ENTITY_TYPES,
    build_embedding_patch_records,
    iter_embedding_input_batches,
)
from .embeddings import EmbeddingError, EmbeddingProvider


GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA = "ncs_gold_embedding_patch_ndjson_v1"
GOLD_EMBEDDING_SHARD_PLAN_SCHEMA = "ncs_gold_embedding_shard_plan_v1"
GOLD_EMBEDDING_SHARD_MANIFEST_SCHEMA = "ncs_gold_embedding_shard_manifest_v1"
_MAX_RECORDS = 5_000_000
_FORBIDDEN_TEXT_KEYS = frozenset(
    {
        "text",
        "semantic_text",
        "source_text",
        "criteria_text",
        "definition",
        "raw_text",
        "label_text",
    }
)


class EmbeddingPatchExportError(RuntimeError):
    """Base error for an artifact that cannot safely be emitted or consumed."""


class EmbeddingPatchArtifactError(EmbeddingPatchExportError, ValueError):
    """The on-disk patch artifact is malformed, incomplete, or unsafe."""


def _nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EmbeddingPatchExportError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, field: str, *, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
        raise EmbeddingPatchExportError(
            f"{field} must be an integer between 1 and {upper}"
        )
    return value


def _provider_dimensions(provider: EmbeddingProvider) -> int:
    if not bool(getattr(provider, "enabled", False)):
        raise EmbeddingPatchExportError("an enabled embedding provider is required")
    dimensions = getattr(provider, "dimensions", None)
    if dimensions is None:
        resolver = getattr(provider, "resolve_dimensions", None)
        if not callable(resolver):
            raise EmbeddingPatchExportError(
                "embedding provider must declare dimensions or expose resolve_dimensions()"
            )
        dimensions = resolver()
    return _positive_int(dimensions, "provider dimensions", upper=8_192)


def _provider_identity(provider: EmbeddingProvider) -> tuple[str, str, int]:
    return (
        _nonempty_text(getattr(provider, "provider_name", None), "provider_name"),
        _nonempty_text(getattr(provider, "model", None), "model"),
        _provider_dimensions(provider),
    )


def _canonical_line(record: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_fingerprint(path: str | Path) -> tuple[int, int, int | None]:
    """Cheap mutation sentinel used between the two authoritative SHA checks."""

    stat = Path(path).stat()
    return stat.st_size, stat.st_mtime_ns, getattr(stat, "st_ino", None)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value.casefold())
    )


def _resolved_child(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise EmbeddingPatchArtifactError("embedding shard manifest path is unsafe")
    try:
        candidate = (root / relative).resolve(strict=True)
        candidate.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise EmbeddingPatchArtifactError(
            "embedding shard manifest path escapes its directory"
        ) from exc
    return candidate


def _canonical_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(
                json.dumps(
                    value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            )
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _contains_semantic_text(value: object) -> bool:
    """Reject exact prohibited keys recursively while allowing audit counts."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and key.casefold() in _FORBIDDEN_TEXT_KEYS:
                return True
            if _contains_semantic_text(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_semantic_text(item) for item in value)
    return False


def _validate_patch_payload(
    payload: object,
    *,
    normalize_portable_values: bool = True,
) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise EmbeddingPatchArtifactError("patch record must be an object")
    if _contains_semantic_text(payload):
        raise EmbeddingPatchArtifactError(
            "patch artifact must not contain semantic text"
        )
    required = (
        "schema",
        "entity_type",
        "entity_id",
        "embedding",
        "content_hash",
        "cache_key",
        "provider",
        "model",
        "dimensions",
        "metadata",
    )
    if any(name not in payload for name in required):
        raise EmbeddingPatchArtifactError("patch record has missing required fields")
    if payload.get("schema") != EMBEDDING_NODE_PATCH_SCHEMA:
        raise EmbeddingPatchArtifactError("patch record schema is invalid")
    entity_type = payload.get("entity_type")
    entity_id = payload.get("entity_id")
    if (
        entity_type not in SUPPORTED_ENTITY_TYPES
        or not isinstance(entity_id, str)
        or not entity_id.startswith("ncs:")
    ):
        raise EmbeddingPatchArtifactError("patch record entity is invalid")
    dimensions = payload.get("dimensions")
    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or not 1 <= dimensions <= 8_192
    ):
        raise EmbeddingPatchArtifactError("patch record dimensions are invalid")
    embedding = payload.get("embedding")
    if not isinstance(embedding, list) or len(embedding) != dimensions:
        raise EmbeddingPatchArtifactError("patch record embedding dimension is invalid")
    try:
        # The batch builder already validates finiteness.  This check protects
        # an artifact read back from disk without accepting booleans as numbers.
        if any(
            isinstance(item, bool) or not isinstance(item, (int, float))
            for item in embedding
        ):
            raise ValueError
        if any(not math.isfinite(float(item)) for item in embedding):
            raise ValueError
    except (TypeError, ValueError):
        raise EmbeddingPatchArtifactError("patch record embedding is invalid") from None
    for field in ("content_hash", "cache_key", "provider", "model"):
        if not isinstance(payload.get(field), str) or not str(payload[field]).strip():
            raise EmbeddingPatchArtifactError("patch record provenance is incomplete")
    if not isinstance(payload.get("metadata"), Mapping):
        raise EmbeddingPatchArtifactError("patch record metadata is invalid")
    # Artifacts parsed from JSON already contain portable built-in values. Avoid
    # serializing every 1024-dimensional vector a second time during full shard
    # inspection; constructed caller payloads retain the defensive round-trip.
    if not normalize_portable_values:
        return dict(payload)
    # Round-trip via JSON to remove Mapping subclasses and retain only portable values.
    try:
        return json.loads(json.dumps(dict(payload), ensure_ascii=False, sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise EmbeddingPatchArtifactError(
            "patch record is not JSON serializable"
        ) from exc


def _write_line(handle: Any, record: Mapping[str, Any]) -> bytes:
    encoded = _canonical_line(record)
    handle.write(encoded)
    return encoded


def _validate_header(header: object) -> dict[str, Any]:
    if not isinstance(header, Mapping):
        raise EmbeddingPatchArtifactError("artifact header must be an object")
    if header.get("schema") != GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA:
        raise EmbeddingPatchArtifactError("artifact header schema is invalid")
    provider = header.get("provider")
    model = header.get("model")
    dimensions = header.get("dimensions")
    if (
        not isinstance(provider, str)
        or not provider
        or not isinstance(model, str)
        or not model
    ):
        raise EmbeddingPatchArtifactError(
            "artifact header provider identity is invalid"
        )
    if (
        isinstance(dimensions, bool)
        or not isinstance(dimensions, int)
        or not 1 <= dimensions <= 8_192
    ):
        raise EmbeddingPatchArtifactError("artifact header dimensions are invalid")
    return dict(header)


def inspect_gold_embedding_patches(path: str | Path) -> dict[str, Any]:
    """Fully validate a final patch artifact before a later serving-layer apply.

    The manifest digest covers every header and patch line, so truncated,
    appended, reordered, or altered artifacts fail before they can reach Neo4j.
    """

    artifact = Path(path)
    if not artifact.is_file() or artifact.stat().st_size <= 0:
        raise EmbeddingPatchArtifactError(
            "embedding patch artifact is missing or empty"
        )
    digest = hashlib.sha256()
    patch_counts: Counter[str] = Counter()
    patch_count = 0
    header: dict[str, Any] | None = None
    manifest: dict[str, Any] | None = None
    saw_manifest = False
    with artifact.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            if not raw.endswith(b"\n"):
                raise EmbeddingPatchArtifactError(
                    "artifact contains an incomplete final line"
                )
            try:
                record = _decode_json(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise EmbeddingPatchArtifactError(
                    f"artifact line {line_number} is invalid JSON"
                ) from exc
            if not isinstance(record, Mapping) or not isinstance(
                record.get("record_type"), str
            ):
                raise EmbeddingPatchArtifactError(
                    f"artifact line {line_number} has no record type"
                )
            kind = record["record_type"]
            if saw_manifest:
                raise EmbeddingPatchArtifactError(
                    "artifact has records after the manifest"
                )
            if kind == "header":
                if (
                    line_number != 1
                    or header is not None
                    or set(record) != {"record_type", "header"}
                ):
                    raise EmbeddingPatchArtifactError(
                        "artifact header position or shape is invalid"
                    )
                header = _validate_header(record["header"])
                digest.update(raw)
            elif kind == "patch":
                if header is None or set(record) != {"record_type", "patch"}:
                    raise EmbeddingPatchArtifactError(
                        "artifact patch position or shape is invalid"
                    )
                patch = _validate_patch_payload(
                    record["patch"],
                    normalize_portable_values=False,
                )
                if (
                    patch["provider"] != header["provider"]
                    or patch["model"] != header["model"]
                    or patch["dimensions"] != header["dimensions"]
                ):
                    raise EmbeddingPatchArtifactError(
                        "patch provider identity does not match artifact header"
                    )
                patch_counts[str(patch["entity_type"])] += 1
                patch_count += 1
                digest.update(raw)
            elif kind == "manifest":
                if (
                    header is None
                    or manifest is not None
                    or set(record) != {"record_type", "manifest"}
                ):
                    raise EmbeddingPatchArtifactError(
                        "artifact manifest position or shape is invalid"
                    )
                if not isinstance(record["manifest"], Mapping):
                    raise EmbeddingPatchArtifactError(
                        "artifact manifest must be an object"
                    )
                manifest = dict(record["manifest"])
                saw_manifest = True
            else:
                raise EmbeddingPatchArtifactError(
                    f"unsupported artifact record type: {kind}"
                )
    if header is None or manifest is None:
        raise EmbeddingPatchArtifactError(
            "artifact is incomplete: header or final manifest is missing"
        )
    expected = {
        "schema": GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA,
        "records_before_manifest": patch_count + 1,
        "records_sha256": digest.hexdigest(),
        "patch_count": patch_count,
        "patch_counts": dict(sorted(patch_counts.items())),
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise EmbeddingPatchArtifactError(
            "artifact manifest digest or counts do not match records"
        )
    if (
        manifest.get("semantic_text_included") is not False
        or manifest.get("db_writes") is not False
        or manifest.get("neo4j_writes") is not False
    ):
        raise EmbeddingPatchArtifactError("artifact safety contract is invalid")
    return {
        "header": header,
        "manifest": manifest,
        "patch_counts": dict(sorted(patch_counts.items())),
    }


def iter_gold_embedding_patches(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield raw patch payloads only after a complete fail-closed validation."""

    inspect_gold_embedding_patches(path)
    yield from _iter_prevalidated_gold_embedding_patches(path)


def _iter_prevalidated_gold_embedding_patches(
    path: str | Path,
) -> Iterator[dict[str, Any]]:
    """Stream patches after the caller has immediately validated the shard."""

    with Path(path).open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                record = _decode_json(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise EmbeddingPatchArtifactError(
                    f"artifact line {line_number} changed after validation"
                ) from exc
            if not isinstance(record, Mapping):
                raise EmbeddingPatchArtifactError(
                    f"artifact line {line_number} changed after validation"
                )
            if record.get("record_type") == "patch":
                patch = record.get("patch")
                if not isinstance(patch, Mapping):
                    raise EmbeddingPatchArtifactError(
                        f"artifact line {line_number} changed after validation"
                    )
                yield dict(patch)


def export_gold_embedding_patches(
    db_path: str | Path,
    destination: str | Path,
    provider: EmbeddingProvider,
    *,
    entity_types: Iterable[str] = SUPPORTED_ENTITY_TYPES,
    batch_size: int = DEFAULT_BATCH_SIZE,
    fetch_size: int = DEFAULT_FETCH_SIZE,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
    max_records: int | None = None,
    source_key_min: int | None = None,
    source_key_max: int | None = None,
    shard_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Export deterministic vector patches using an explicitly supplied provider.

    The write becomes visible only after a complete self-inspection succeeds.
    An exception leaves an existing destination untouched and deletes its
    temporary sibling.  ``max_records`` is intended for bounded smoke exports.
    """

    batch_size = _positive_int(batch_size, "batch_size", upper=MAX_BATCH_SIZE)
    fetch_size = _positive_int(fetch_size, "fetch_size", upper=MAX_FETCH_SIZE)
    max_text_chars = _positive_int(
        max_text_chars, "max_text_chars", upper=MAX_TEXT_CHARS
    )
    if max_records is not None:
        max_records = _positive_int(max_records, "max_records", upper=_MAX_RECORDS)
    provider_name, model, dimensions = _provider_identity(provider)
    raw_types = (
        (entity_types,) if isinstance(entity_types, str) else tuple(entity_types)
    )
    requested_set = set(raw_types)
    if not requested_set or requested_set.difference(SUPPORTED_ENTITY_TYPES):
        raise EmbeddingPatchExportError(
            "entity_types must be a non-empty supported subset"
        )
    requested_types = tuple(
        entity_type
        for entity_type in SUPPORTED_ENTITY_TYPES
        if entity_type in requested_set
    )
    if shard_binding is not None and not isinstance(shard_binding, Mapping):
        raise EmbeddingPatchExportError("shard_binding must be an object")

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    emitted = 0
    counts: Counter[str] = Counter()
    digest = hashlib.sha256()
    observed_batch = 0
    limited = False
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            header = {
                "schema": GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA,
                "provider": provider_name,
                "model": model,
                "dimensions": dimensions,
                "entity_types": list(requested_types),
                "batch_size": batch_size,
                "max_text_chars": max_text_chars,
                "max_records": max_records,
                "semantic_text_included": False,
                "db_writes": False,
                "neo4j_writes": False,
            }
            if shard_binding is not None:
                header["shard_binding"] = dict(shard_binding)
            digest.update(
                _write_line(handle, {"record_type": "header", "header": header})
            )
            stop = False
            input_batches = iter_embedding_input_batches(
                db_path,
                provider_name=provider_name,
                model=model,
                dimensions=dimensions,
                entity_types=requested_types,
                batch_size=batch_size,
                fetch_size=fetch_size,
                max_text_chars=max_text_chars,
                source_key_min=source_key_min,
                source_key_max=source_key_max,
            )
            try:
                for inputs in input_batches:
                    if max_records is not None:
                        remaining = max_records - emitted
                        if remaining <= 0:
                            limited = True
                            break
                        inputs = inputs[:remaining]
                    if not inputs:
                        continue
                    observed_batch = max(observed_batch, len(inputs))
                    vectors = provider.embed_texts([item.text for item in inputs])
                    patches = build_embedding_patch_records(
                        inputs, vectors, expected_dimension=dimensions
                    )
                    for patch in patches:
                        payload = _validate_patch_payload(patch.as_dict())
                        digest.update(
                            _write_line(
                                handle, {"record_type": "patch", "patch": payload}
                            )
                        )
                        counts[patch.entity_type] += 1
                        emitted += 1
                    if max_records is not None and emitted >= max_records:
                        limited = True
                        stop = True
                    if stop:
                        break
            finally:
                # Closing an interrupted generator releases the read-only
                # SQLite handle immediately on Windows as well as POSIX.
                input_batches.close()
            manifest = {
                "schema": GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA,
                "records_before_manifest": emitted + 1,
                "records_sha256": digest.hexdigest(),
                "patch_count": emitted,
                "patch_counts": dict(sorted(counts.items())),
                "provider": provider_name,
                "model": model,
                "dimensions": dimensions,
                "max_observed_batch_size": observed_batch,
                "limited_by_max_records": limited,
                "semantic_text_included": False,
                "read_only": True,
                "db_writes": False,
                "neo4j_writes": False,
                "approval_claim": False,
                "status_update_allowed": False,
            }
            if shard_binding is not None:
                manifest["shard_binding"] = dict(shard_binding)
            _write_line(handle, {"record_type": "manifest", "manifest": manifest})
            handle.flush()
            os.fsync(handle.fileno())
        inspected = inspect_gold_embedding_patches(temp_name)
        if inspected["manifest"] != manifest:
            raise EmbeddingPatchArtifactError(
                "self-inspection returned a different manifest"
            )
        os.replace(temp_name, output)
        return manifest
    except EmbeddingError:
        raise
    finally:
        Path(temp_name).unlink(missing_ok=True)


_ENTITY_SOURCE_TABLES = {
    "PerformanceCriterion": ("performance_criteria", "criteria_id"),
    "PerformanceElement": ("competency_elements", "element_id"),
    "KSAConcept": ("ontology_concepts", "concept_id"),
}


def build_gold_embedding_shard_plan(
    db_path: str | Path,
    *,
    provider: EmbeddingProvider,
    gold_records_sha256: str,
    source_db_sha256: str | None = None,
    shard_size: int = 10_000,
    entity_types: Iterable[str] = SUPPORTED_ENTITY_TYPES,
    max_text_chars: int = DEFAULT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """Create an immutable, keyset-only full-export plan without embedding.

    Ranges are based on stable source primary keys, in the public entity-type
    order.  The plan is intentionally independent of OFFSET and records the
    source byte digest so a resumed run cannot quietly switch databases.
    """

    shard_size = _positive_int(shard_size, "shard_size", upper=_MAX_RECORDS)
    max_text_chars = _positive_int(
        max_text_chars, "max_text_chars", upper=MAX_TEXT_CHARS
    )
    provider_name, model, dimensions = _provider_identity(provider)
    if not _is_sha256(gold_records_sha256):
        raise EmbeddingPatchExportError("gold_records_sha256 must be a SHA-256 digest")
    raw_types = (
        (entity_types,) if isinstance(entity_types, str) else tuple(entity_types)
    )
    requested = tuple(kind for kind in SUPPORTED_ENTITY_TYPES if kind in set(raw_types))
    if not requested or set(raw_types).difference(SUPPORTED_ENTITY_TYPES):
        raise EmbeddingPatchExportError(
            "entity_types must be a non-empty supported subset"
        )
    source = Path(db_path)
    if not source.is_file():
        raise FileNotFoundError(f"SQLite database does not exist: {source}")
    shards: list[dict[str, Any]] = []
    uri = f"{source.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        for entity_type in requested:
            table, key = _ENTITY_SOURCE_TABLES[entity_type]
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if exists is None:
                continue
            cursor = conn.execute(f"SELECT {key} FROM {table} ORDER BY {key}")
            pending: list[int] = []
            while True:
                rows = cursor.fetchmany(min(512, shard_size))
                if not rows:
                    break
                for row in rows:
                    pending.append(int(row[0]))
                    if len(pending) == shard_size:
                        shards.append(
                            {
                                "entity_type": entity_type,
                                "source_key_min": pending[0],
                                "source_key_max": pending[-1],
                                "source_row_count": len(pending),
                            }
                        )
                        pending = []
            if pending:
                shards.append(
                    {
                        "entity_type": entity_type,
                        "source_key_min": pending[0],
                        "source_key_max": pending[-1],
                        "source_row_count": len(pending),
                    }
                )
    finally:
        conn.close()
    for index, shard in enumerate(shards, start=1):
        shard["index"] = index
    base = {
        "schema": GOLD_EMBEDDING_SHARD_PLAN_SCHEMA,
        "entity_type_order": list(requested),
        "source_db_sha256": source_db_sha256 or _sha256_file(source),
        "gold_records_sha256": gold_records_sha256,
        "provider": provider_name,
        "model": model,
        "dimensions": dimensions,
        "text_policy": {
            "semantic_text_included": False,
            "max_text_chars": max_text_chars,
        },
        "shard_size": shard_size,
        "shards": shards,
        "db_writes": False,
        "approval_claim": False,
    }
    plan = dict(base)
    plan["plan_fingerprint"] = _canonical_digest(base)
    return plan


def _validate_shard_plan(
    plan: Mapping[str, Any], *, provider: EmbeddingProvider
) -> dict[str, Any]:
    if (
        not isinstance(plan, Mapping)
        or plan.get("schema") != GOLD_EMBEDDING_SHARD_PLAN_SCHEMA
    ):
        raise EmbeddingPatchArtifactError("embedding shard plan schema is invalid")
    material = dict(plan)
    fingerprint = material.pop("plan_fingerprint", None)
    if not isinstance(fingerprint, str) or fingerprint != _canonical_digest(material):
        raise EmbeddingPatchArtifactError("embedding shard plan fingerprint is invalid")
    provider_name, model, dimensions = _provider_identity(provider)
    if (plan.get("provider"), plan.get("model"), plan.get("dimensions")) != (
        provider_name,
        model,
        dimensions,
    ):
        raise EmbeddingPatchArtifactError(
            "embedding shard plan provider configuration changed"
        )
    if not _is_sha256(plan.get("source_db_sha256")) or not _is_sha256(
        plan.get("gold_records_sha256")
    ):
        raise EmbeddingPatchArtifactError("embedding shard plan digest is invalid")
    entity_order = plan.get("entity_type_order")
    if not isinstance(entity_order, list) or not entity_order:
        raise EmbeddingPatchArtifactError(
            "embedding shard plan entity order is invalid"
        )
    expected_order = [kind for kind in SUPPORTED_ENTITY_TYPES if kind in entity_order]
    if entity_order != expected_order or len(set(entity_order)) != len(entity_order):
        raise EmbeddingPatchArtifactError(
            "embedding shard plan entity order is invalid"
        )
    if not isinstance(plan.get("shard_size"), int) or plan["shard_size"] < 1:
        raise EmbeddingPatchArtifactError("embedding shard plan shard size is invalid")
    text_policy = plan.get("text_policy")
    if (
        not isinstance(text_policy, Mapping)
        or text_policy.get("semantic_text_included") is not False
        or not isinstance(text_policy.get("max_text_chars"), int)
        or not 1 <= text_policy["max_text_chars"] <= MAX_TEXT_CHARS
    ):
        raise EmbeddingPatchArtifactError("embedding shard plan text policy is invalid")
    previous_index = 0
    previous_by_type: dict[str, int] = {}
    previous_type_position = -1
    for shard in plan.get("shards", []):
        if (
            not isinstance(shard, Mapping)
            or shard.get("entity_type") not in entity_order
        ):
            raise EmbeddingPatchArtifactError("embedding shard plan shard is invalid")
        index, lower, upper, row_count = (
            shard.get("index"),
            shard.get("source_key_min"),
            shard.get("source_key_max"),
            shard.get("source_row_count"),
        )
        if (
            not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (index, lower, upper, row_count)
            )
            or index != previous_index + 1
            or lower > upper
            or not 1 <= row_count <= plan["shard_size"]
        ):
            raise EmbeddingPatchArtifactError("embedding shard plan range is invalid")
        entity_type = str(shard["entity_type"])
        type_position = entity_order.index(entity_type)
        if type_position < previous_type_position:
            raise EmbeddingPatchArtifactError(
                "embedding shard plan entity ordering is invalid"
            )
        if lower <= previous_by_type.get(entity_type, -1):
            raise EmbeddingPatchArtifactError("embedding shard plan ranges overlap")
        previous_by_type[entity_type] = upper
        previous_index = index
        previous_type_position = type_position
    return dict(plan)


def _validate_shard_plan_source_ranges(
    plan: Mapping[str, Any], db_path: str | Path
) -> None:
    """Confirm planned row counts and coverage once, without embedding or OFFSET."""

    source = Path(db_path)
    uri = f"{source.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        for entity_type in plan["entity_type_order"]:
            table, key = _ENTITY_SOURCE_TABLES[entity_type]
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if exists is None:
                if any(item["entity_type"] == entity_type for item in plan["shards"]):
                    raise EmbeddingPatchArtifactError(
                        "embedding shard plan references missing source table"
                    )
                continue
            total = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            planned = 0
            for shard in (
                item for item in plan["shards"] if item["entity_type"] == entity_type
            ):
                count = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE {key} >= ? AND {key} <= ?",
                        (shard["source_key_min"], shard["source_key_max"]),
                    ).fetchone()[0]
                )
                if count != shard["source_row_count"]:
                    raise EmbeddingPatchArtifactError(
                        "embedding shard plan source row count is invalid"
                    )
                planned += count
            if total != planned:
                raise EmbeddingPatchArtifactError(
                    "embedding shard plan does not cover source rows"
                )
    finally:
        conn.close()


def inspect_gold_embedding_shard_manifest(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        manifest = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EmbeddingPatchArtifactError(
            "embedding shard manifest is unreadable"
        ) from exc
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema") != GOLD_EMBEDDING_SHARD_MANIFEST_SCHEMA
    ):
        raise EmbeddingPatchArtifactError("embedding shard manifest schema is invalid")
    plan = manifest.get("plan")
    if not isinstance(plan, Mapping) or manifest.get("plan_fingerprint") != plan.get(
        "plan_fingerprint"
    ):
        raise EmbeddingPatchArtifactError(
            "embedding shard manifest plan binding is invalid"
        )
    plan_material = dict(plan)
    plan_fingerprint = plan_material.pop("plan_fingerprint", None)
    if (
        not isinstance(plan_fingerprint, str)
        or plan.get("schema") != GOLD_EMBEDDING_SHARD_PLAN_SCHEMA
        or plan_fingerprint != _canonical_digest(plan_material)
    ):
        raise EmbeddingPatchArtifactError(
            "embedding shard manifest plan fingerprint is invalid"
        )
    shards = manifest.get("shards")
    plan_shards = plan.get("shards")
    if (
        not isinstance(shards, list)
        or not isinstance(plan_shards, list)
        or len(shards) != len(plan_shards)
    ):
        raise EmbeddingPatchArtifactError("embedding shard manifest shards are invalid")
    if (
        manifest.get("source_db_sha256") != plan.get("source_db_sha256")
        or manifest.get("gold_records_sha256") != plan.get("gold_records_sha256")
        or manifest.get("semantic_text_included") is not False
        or manifest.get("db_writes") is not False
        or manifest.get("neo4j_writes") is not False
    ):
        raise EmbeddingPatchArtifactError(
            "embedding shard manifest safety binding is invalid"
        )
    counts: Counter[str] = Counter()
    total = 0
    for expected_index, (item, planned) in enumerate(zip(shards, plan_shards), start=1):
        if not isinstance(item, Mapping) or item.get("index") != expected_index:
            raise EmbeddingPatchArtifactError(
                "embedding shard manifest ordering is invalid"
            )
        descriptor = {
            "index": item.get("index"),
            "entity_type": item.get("entity_type"),
            "source_key_min": item.get("source_key_min"),
            "source_key_max": item.get("source_key_max"),
            "source_row_count": item.get("source_row_count"),
        }
        if not isinstance(planned, Mapping) or descriptor != {
            "index": planned.get("index"),
            "entity_type": planned.get("entity_type"),
            "source_key_min": planned.get("source_key_min"),
            "source_key_max": planned.get("source_key_max"),
            "source_row_count": planned.get("source_row_count"),
        }:
            raise EmbeddingPatchArtifactError(
                "embedding shard manifest descriptor is invalid"
            )
        relative = item.get("path")
        if not isinstance(relative, str):
            raise EmbeddingPatchArtifactError("embedding shard manifest path is unsafe")
        artifact = _resolved_child(source.parent, relative)
        inspection = inspect_gold_embedding_patches(artifact)
        binding = {
            "plan_fingerprint": plan_fingerprint,
            "source_db_sha256": plan.get("source_db_sha256"),
            "gold_records_sha256": plan.get("gold_records_sha256"),
            "index": item.get("index"),
            "entity_type": item.get("entity_type"),
            "source_key_min": item.get("source_key_min"),
            "source_key_max": item.get("source_key_max"),
        }
        if (
            inspection["header"].get("shard_binding") != binding
            or inspection["manifest"].get("shard_binding") != binding
        ):
            raise EmbeddingPatchArtifactError(
                "embedding shard manifest binding is invalid"
            )
        expected_header = {
            "provider": plan.get("provider"),
            "model": plan.get("model"),
            "dimensions": plan.get("dimensions"),
            "entity_types": [item.get("entity_type")],
            "max_text_chars": plan.get("text_policy", {}).get("max_text_chars"),
            "semantic_text_included": False,
            "db_writes": False,
            "neo4j_writes": False,
        }
        if any(
            inspection["header"].get(key) != value
            for key, value in expected_header.items()
        ):
            raise EmbeddingPatchArtifactError(
                "embedding shard provider configuration is invalid"
            )
        expected_patch_manifest = {
            "provider": plan.get("provider"),
            "model": plan.get("model"),
            "dimensions": plan.get("dimensions"),
            "semantic_text_included": False,
            "db_writes": False,
            "neo4j_writes": False,
        }
        if any(
            inspection["manifest"].get(key) != value
            for key, value in expected_patch_manifest.items()
        ):
            raise EmbeddingPatchArtifactError(
                "embedding shard provider configuration is invalid"
            )
        if item.get("sha256") != _sha256_file(artifact) or item.get(
            "records_sha256"
        ) != inspection["manifest"].get("records_sha256"):
            raise EmbeddingPatchArtifactError(
                "embedding shard manifest digest is invalid"
            )
        if item.get("patch_count") != inspection["manifest"].get(
            "patch_count"
        ) or item.get("patch_counts") != inspection["manifest"].get("patch_counts"):
            raise EmbeddingPatchArtifactError(
                "embedding shard manifest aggregate is invalid"
            )
        counts.update(inspection["manifest"].get("patch_counts", {}))
        total += int(inspection["manifest"].get("patch_count", 0))
    if manifest.get("patch_count") != total or manifest.get("patch_counts") != dict(
        sorted(counts.items())
    ):
        raise EmbeddingPatchArtifactError("embedding shard manifest totals are invalid")
    return dict(manifest)


def export_gold_embedding_shards(
    db_path: str | Path,
    destination_dir: str | Path,
    provider: EmbeddingProvider,
    *,
    plan: Mapping[str, Any],
    batch_size: int = DEFAULT_BATCH_SIZE,
    fetch_size: int = DEFAULT_FETCH_SIZE,
) -> dict[str, Any]:
    """Atomically resume a full shard plan and write its final manifest last."""

    plan = _validate_shard_plan(plan, provider=provider)
    source_start_sha256 = _sha256_file(db_path)
    if source_start_sha256 != plan["source_db_sha256"]:
        raise EmbeddingPatchArtifactError(
            "embedding shard plan source database changed"
        )
    _validate_shard_plan_source_ranges(plan, db_path)
    source_stat = _stat_fingerprint(db_path)
    root = Path(destination_dir)
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for shard in plan["shards"]:
        if _stat_fingerprint(db_path) != source_stat:
            raise EmbeddingPatchArtifactError(
                "embedding shard source database changed during export"
            )
        name = f"shard-{int(shard['index']):06d}-{shard['entity_type']}-{shard['source_key_min']}-{shard['source_key_max']}.ndjson"
        artifact = root / name
        binding = {
            "plan_fingerprint": plan["plan_fingerprint"],
            "source_db_sha256": plan["source_db_sha256"],
            "gold_records_sha256": plan["gold_records_sha256"],
            "index": shard["index"],
            "entity_type": shard["entity_type"],
            "source_key_min": shard["source_key_min"],
            "source_key_max": shard["source_key_max"],
        }
        valid = False
        if artifact.is_file():
            try:
                inspected = inspect_gold_embedding_patches(artifact)
                valid = (
                    inspected["header"].get("shard_binding") == binding
                    and inspected["manifest"].get("shard_binding") == binding
                )
            except EmbeddingPatchArtifactError:
                valid = False
        if not valid:
            export_gold_embedding_patches(
                db_path,
                artifact,
                provider,
                entity_types=(str(shard["entity_type"]),),
                batch_size=batch_size,
                fetch_size=fetch_size,
                max_text_chars=int(plan["text_policy"]["max_text_chars"]),
                source_key_min=int(shard["source_key_min"]),
                source_key_max=int(shard["source_key_max"]),
                shard_binding=binding,
            )
        inspected = inspect_gold_embedding_patches(artifact)
        entries.append(
            {
                "index": shard["index"],
                "path": name,
                "sha256": _sha256_file(artifact),
                "records_sha256": inspected["manifest"]["records_sha256"],
                "patch_count": inspected["manifest"]["patch_count"],
                "patch_counts": inspected["manifest"]["patch_counts"],
                "entity_type": shard["entity_type"],
                "source_key_min": shard["source_key_min"],
                "source_key_max": shard["source_key_max"],
                "source_row_count": shard["source_row_count"],
            }
        )
    if _sha256_file(db_path) != source_start_sha256:
        raise EmbeddingPatchArtifactError(
            "embedding shard source database changed during export"
        )
    totals: Counter[str] = Counter()
    for entry in entries:
        totals.update(entry["patch_counts"])
    manifest = {
        "schema": GOLD_EMBEDDING_SHARD_MANIFEST_SCHEMA,
        "plan": plan,
        "plan_fingerprint": plan["plan_fingerprint"],
        "source_db_sha256": plan["source_db_sha256"],
        "gold_records_sha256": plan["gold_records_sha256"],
        "shards": entries,
        "patch_count": sum(entry["patch_count"] for entry in entries),
        "patch_counts": dict(sorted(totals.items())),
        "semantic_text_included": False,
        "read_only": True,
        "db_writes": False,
        "neo4j_writes": False,
        "approval_claim": False,
        "status_update_allowed": False,
    }
    manifest_path = root / "ncs_gold_embeddings.manifest.json"
    _atomic_json(manifest_path, manifest)
    return inspect_gold_embedding_shard_manifest(manifest_path)


__all__ = [
    "EmbeddingPatchArtifactError",
    "EmbeddingPatchExportError",
    "GOLD_EMBEDDING_PATCH_NDJSON_SCHEMA",
    "GOLD_EMBEDDING_SHARD_MANIFEST_SCHEMA",
    "GOLD_EMBEDDING_SHARD_PLAN_SCHEMA",
    "build_gold_embedding_shard_plan",
    "export_gold_embedding_shards",
    "export_gold_embedding_patches",
    "inspect_gold_embedding_patches",
    "inspect_gold_embedding_shard_manifest",
    "iter_gold_embedding_patches",
]
