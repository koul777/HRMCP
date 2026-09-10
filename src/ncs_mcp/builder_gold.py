"""Optional Gold preparation/sync stage used by the desktop Data Builder.

Builder candidate readiness remains SQLite-only.  This bridge is deliberately
additive: it prepares an immutable Gold stream, validates it, and only writes
Neo4j when ``apply=True`` is passed by an operator-facing caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

from .api_refresh_builder import file_sha256
from .embedding_batches import SUPPORTED_ENTITY_TYPES
from .embedding_export import (
    build_gold_embedding_shard_plan,
    export_gold_embedding_shards,
    export_gold_embedding_patches,
    inspect_gold_embedding_patches,
    iter_gold_embedding_patches,
)
from .gold_incremental import plan_gold_lpg_incremental
from .gold_readiness import SERVING_CORE_PROFILE, preflight_gold_projection
from .gold_stream import export_gold_lpg_ndjson
from .internal_job_roles import (
    validate_alignment_candidate,
    validate_internal_job_role,
)
from .internal_role_mapping import INTERNAL_ROLE_MAPPING_PACKET_SCHEMA
from .neo4j_loader import (
    Neo4jLoaderSettings,
    apply_embedding_patches,
    apply_embedding_shards,
    apply_vector_indexes,
    inspect_gold_lpg_ndjson,
    load_gold_lpg_ndjson,
    reconcile_gold_lpg,
)


BUILDER_GOLD_SCHEMA = "ncs_data_builder_gold_v1"
GOLD_DIRECTORY_NAME = "gold"
GOLD_EXPORT_NAME = "ncs_gold_serving_core.ndjson"
GOLD_REPORT_NAME = "gold-build.json"
GOLD_LOAD_REPORT_NAME = "gold-load.json"
GOLD_LOAD_CHECKPOINT_NAME = "gold-load-checkpoint.json"
GOLD_INCREMENTAL_NAME = "gold-incremental.ndjson"
GOLD_EMBEDDING_EXPORT_NAME = "ncs_gold_embeddings.ndjson"
GOLD_EMBEDDING_REPORT_NAME = "gold-embeddings.json"
GOLD_EMBEDDING_LOAD_REPORT_NAME = "gold-embedding-load.json"
GOLD_EMBEDDING_SHARD_DIRECTORY_NAME = "embeddings-shards"
GOLD_EMBEDDING_SHARD_REPORT_NAME = "gold-embeddings-shards.json"
DEFAULT_GOLD_EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
DEFAULT_GOLD_EMBEDDING_DIMENSIONS = 1_024
MAX_ROLE_MAPPING_PACKET_BYTES = 20 * 1024 * 1024
MAX_INTERNAL_ROLES = 10_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def load_internal_role_mapping_packet(
    path: str | Path,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """Load one bounded, candidate-only mapper packet for a Builder export."""

    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(
            f"internal role mapping packet does not exist: {source}"
        )
    if source.stat().st_size > MAX_ROLE_MAPPING_PACKET_BYTES:
        raise ValueError("internal role mapping packet exceeds the bounded size limit")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if (
        not isinstance(payload, Mapping)
        or payload.get("schema") != INTERNAL_ROLE_MAPPING_PACKET_SCHEMA
    ):
        raise ValueError("internal role mapping packet schema is invalid")
    results = payload.get("role_results")
    if not isinstance(results, list) or len(results) > MAX_INTERNAL_ROLES:
        raise ValueError("internal role mapping packet role_results are invalid")
    roles: list[Any] = []
    alignments: list[Any] = []
    seen_roles: set[str] = set()
    for result in results:
        if not isinstance(result, Mapping):
            raise ValueError("internal role mapping result must be an object")
        raw_role = result.get("role")
        if not isinstance(raw_role, Mapping):
            raise ValueError("internal role mapping result needs a role object")
        # Mapper packets use the public projection, which includes two derived
        # fields that are intentionally not constructor inputs.  Recompute and
        # compare them so a tampered packet cannot change tenant identity or
        # the semantic text while the strict role validator still rejects all
        # other unknown/PII fields.
        derived_gold_id = raw_role.get("gold_id")
        derived_semantic_text = raw_role.get("normalized_semantic_text")
        role_payload = {
            key: value
            for key, value in raw_role.items()
            if key not in {"gold_id", "normalized_semantic_text"}
        }
        role = validate_internal_job_role(role_payload)
        if derived_gold_id is not None and derived_gold_id != role.gold_id:
            raise ValueError("internal role mapping packet gold_id is inconsistent")
        if (
            derived_semantic_text is not None
            and derived_semantic_text != role.normalized_semantic_text
        ):
            raise ValueError(
                "internal role mapping packet semantic text is inconsistent"
            )
        if role.gold_id in seen_roles:
            raise ValueError("internal role mapping packet contains duplicate roles")
        seen_roles.add(role.gold_id)
        raw_candidates = result.get("alignment_candidates", [])
        if not isinstance(raw_candidates, list):
            raise ValueError("alignment_candidates must be an array")
        candidates = [
            validate_alignment_candidate(candidate) for candidate in raw_candidates
        ]
        if any(candidate.role_gold_id != role.gold_id for candidate in candidates):
            raise ValueError("alignment candidate does not belong to its packet role")
        roles.append(role)
        alignments.extend(candidates)
    return tuple(roles), tuple(alignments)


def _role_overlay_summary(
    roles: tuple[Any, ...], alignments: tuple[Any, ...]
) -> dict[str, Any]:
    canonical = json.dumps(
        {
            "roles": [role.to_public_dict() for role in roles],
            "alignments": [candidate.to_public_dict() for candidate in alignments],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "enabled": bool(roles or alignments),
        "role_count": len(roles),
        "alignment_candidate_count": len(alignments),
        "canonical_sha256": hashlib.sha256(canonical).hexdigest(),
        "candidate_only": True,
        "approval_claim": False,
    }


def _safe_failure(
    *, builder_version: str, source_db_sha256: str, operation: str, exc: Exception
) -> dict[str, Any]:
    return {
        "schema": BUILDER_GOLD_SCHEMA,
        "builder_version": builder_version,
        "source_db_sha256": source_db_sha256,
        "operation": operation,
        "status": "failed",
        "error_type": type(exc).__name__,
        "generated_at": _now(),
        "source_db_writes": False,
        "human_approval_claim": False,
    }


def gold_preflight(
    *, builder_version: str, db_path: str | Path, source_db_sha256: str
) -> dict[str, Any]:
    """Return count-only Gold readiness bound to one verified Builder DB."""

    readiness = preflight_gold_projection(db_path, profile=SERVING_CORE_PROFILE)
    return {
        "schema": BUILDER_GOLD_SCHEMA,
        "builder_version": builder_version,
        "source_db_sha256": source_db_sha256,
        "operation": "preflight",
        "status": "ready",
        "readiness": readiness,
        "generated_at": _now(),
        "source_db_writes": False,
        "neo4j_writes": False,
        "human_approval_claim": False,
    }


def prepare_builder_gold(
    *,
    builder_version: str,
    version_dir: str | Path,
    db_path: str | Path,
    source_db_sha256: str,
    batch_size: int = 10_000,
    previous_export: str | Path | None = None,
    internal_roles: Iterable[Any] = (),
    role_alignments: Iterable[Any] = (),
) -> dict[str, Any]:
    """Export and fully validate a Builder version's immutable Gold stream."""

    version_folder = Path(version_dir).resolve()
    gold_dir = version_folder / GOLD_DIRECTORY_NAME
    export_path = gold_dir / GOLD_EXPORT_NAME
    report_path = gold_dir / GOLD_REPORT_NAME
    gold_dir.mkdir(parents=True, exist_ok=True)
    try:
        readiness = preflight_gold_projection(db_path, profile=SERVING_CORE_PROFILE)
        export_kwargs: dict[str, Any] = {"batch_size": batch_size}
        roles = tuple(validate_internal_job_role(role) for role in internal_roles)
        alignments = tuple(
            validate_alignment_candidate(candidate) for candidate in role_alignments
        )
        if roles or alignments:
            export_kwargs.update(
                internal_roles=roles,
                role_alignments=alignments,
            )
        manifest = export_gold_lpg_ndjson(db_path, export_path, **export_kwargs)
        inspection = inspect_gold_lpg_ndjson(export_path)
        if inspection["records_sha256"] != manifest["records_sha256"]:
            raise ValueError(
                "Gold export inspection digest does not match its manifest"
            )
        incremental: dict[str, Any] | None = None
        if previous_export is not None:
            incremental = plan_gold_lpg_incremental(
                previous_export,
                export_path,
                gold_dir / GOLD_INCREMENTAL_NAME,
            )
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "operation": "prepare",
            "status": "ready",
            "profile": SERVING_CORE_PROFILE.to_dict(),
            "export_path": str(export_path),
            "records_sha256": manifest["records_sha256"],
            "node_count": manifest["node_count"],
            "edge_count": manifest["edge_count"],
            "readiness": readiness,
            "inspection": {
                "import_record_count": inspection["import_record_count"],
                "node_group_counts": inspection["node_group_counts"],
                "edge_counts": inspection["edge_counts"],
            },
            "internal_role_overlay": _role_overlay_summary(roles, alignments),
            "incremental": incremental,
            "generated_at": _now(),
            "source_db_writes": False,
            "neo4j_writes": False,
            "human_approval_claim": False,
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        failure = _safe_failure(
            builder_version=builder_version,
            source_db_sha256=source_db_sha256,
            operation="prepare",
            exc=exc,
        )
        _atomic_json(report_path, failure)
        raise


def sync_builder_gold(
    *,
    builder_version: str,
    version_dir: str | Path,
    source_db_sha256: str,
    apply: bool = False,
    reconcile: bool = False,
    batch_size: int = 1_000,
    settings: Neo4jLoaderSettings | None = None,
) -> dict[str, Any]:
    """Validate or explicitly upsert a prepared Gold stream into Neo4j."""

    if reconcile and not apply:
        raise ValueError("reconcile requires apply=True")
    gold_dir = Path(version_dir).resolve() / GOLD_DIRECTORY_NAME
    export_path = gold_dir / GOLD_EXPORT_NAME
    prepared_path = gold_dir / GOLD_REPORT_NAME
    report_path = gold_dir / GOLD_LOAD_REPORT_NAME
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        if (
            prepared.get("status") != "ready"
            or prepared.get("builder_version") != builder_version
            or prepared.get("source_db_sha256") != source_db_sha256
        ):
            raise ValueError(
                "prepared Gold artifact is not bound to this Builder version"
            )
        inspection = inspect_gold_lpg_ndjson(export_path)
        if inspection["records_sha256"] != prepared.get("records_sha256"):
            raise ValueError("prepared Gold artifact digest has changed")
        load_result = load_gold_lpg_ndjson(
            export_path,
            apply=apply,
            settings=settings,
            checkpoint_path=gold_dir / GOLD_LOAD_CHECKPOINT_NAME if apply else None,
            batch_size=batch_size,
        )
        reconciliation = None
        if reconcile:
            resolved_settings = settings or Neo4jLoaderSettings.from_env()
            reconciliation = reconcile_gold_lpg(
                inspection,
                settings=resolved_settings,
            )
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "records_sha256": inspection["records_sha256"],
            "operation": "sync",
            "status": "applied" if apply else "validated_dry_run",
            "load": load_result,
            "reconciliation": reconciliation,
            "generated_at": _now(),
            "source_db_writes": False,
            "neo4j_writes": bool(apply),
            "human_approval_claim": False,
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        failure = _safe_failure(
            builder_version=builder_version,
            source_db_sha256=source_db_sha256,
            operation="sync",
            exc=exc,
        )
        _atomic_json(report_path, failure)
        raise


def prepare_builder_gold_embeddings(
    *,
    builder_version: str,
    version_dir: str | Path,
    db_path: str | Path,
    source_db_sha256: str,
    provider: Any,
    entity_types: Iterable[str] = SUPPORTED_ENTITY_TYPES,
    batch_size: int = 64,
    fetch_size: int = 128,
    max_text_chars: int = 8_192,
    max_records: int | None = None,
) -> dict[str, Any]:
    """Create a source-bound, text-free embedding patch for one Gold build."""

    gold_dir = Path(version_dir).resolve() / GOLD_DIRECTORY_NAME
    prepared_path = gold_dir / GOLD_REPORT_NAME
    export_path = gold_dir / GOLD_EMBEDDING_EXPORT_NAME
    report_path = gold_dir / GOLD_EMBEDDING_REPORT_NAME
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        if (
            prepared.get("status") != "ready"
            or prepared.get("builder_version") != builder_version
            or prepared.get("source_db_sha256") != source_db_sha256
        ):
            raise ValueError("prepared Gold graph is not bound to this Builder version")
        manifest = export_gold_embedding_patches(
            db_path,
            export_path,
            provider,
            entity_types=entity_types,
            batch_size=batch_size,
            fetch_size=fetch_size,
            max_text_chars=max_text_chars,
            max_records=max_records,
        )
        inspection = inspect_gold_embedding_patches(export_path)
        if inspection["manifest"] != manifest:
            raise ValueError("embedding export inspection does not match its manifest")
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "gold_records_sha256": prepared.get("records_sha256"),
            "operation": "prepare_embeddings",
            "status": "ready_smoke"
            if manifest.get("limited_by_max_records")
            else "ready_full",
            "embedding_export_path": str(export_path),
            "embedding_export_sha256": file_sha256(export_path),
            "provider": manifest.get("provider"),
            "model": manifest.get("model"),
            "dimensions": manifest.get("dimensions"),
            "patch_count": manifest.get("patch_count"),
            "patch_counts": manifest.get("patch_counts"),
            "limited_by_max_records": bool(manifest.get("limited_by_max_records")),
            "semantic_text_included": False,
            "generated_at": _now(),
            "source_db_writes": False,
            "neo4j_writes": False,
            "human_approval_claim": False,
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        failure = _safe_failure(
            builder_version=builder_version,
            source_db_sha256=source_db_sha256,
            operation="prepare_embeddings",
            exc=exc,
        )
        _atomic_json(report_path, failure)
        raise


def prepare_builder_gold_embedding_shards(
    *,
    builder_version: str,
    version_dir: str | Path,
    db_path: str | Path,
    source_db_sha256: str,
    provider: Any,
    gold_records_sha256: str | None = None,
    entity_types: Iterable[str] = SUPPORTED_ENTITY_TYPES,
    shard_size: int = 10_000,
    batch_size: int = 64,
    fetch_size: int = 128,
    max_text_chars: int = 8_192,
) -> dict[str, Any]:
    """Prepare resumable full embedding shards; the smoke export stays separate."""

    gold_dir = Path(version_dir).resolve() / GOLD_DIRECTORY_NAME
    graph_path = gold_dir / GOLD_REPORT_NAME
    report_path = gold_dir / GOLD_EMBEDDING_SHARD_REPORT_NAME
    try:
        graph = json.loads(graph_path.read_text(encoding="utf-8"))
        graph_digest = gold_records_sha256 or graph.get("records_sha256")
        if (
            graph.get("status") != "ready"
            or graph.get("builder_version") != builder_version
            or graph.get("source_db_sha256") != source_db_sha256
            or not isinstance(graph_digest, str)
        ):
            raise ValueError("prepared Gold graph is not bound to this Builder version")
        plan = build_gold_embedding_shard_plan(
            db_path,
            provider=provider,
            gold_records_sha256=graph_digest,
            source_db_sha256=source_db_sha256,
            shard_size=shard_size,
            entity_types=entity_types,
            max_text_chars=max_text_chars,
        )
        shard_dir = gold_dir / GOLD_EMBEDDING_SHARD_DIRECTORY_NAME
        manifest = export_gold_embedding_shards(
            db_path,
            shard_dir,
            provider,
            plan=plan,
            batch_size=batch_size,
            fetch_size=fetch_size,
        )
        manifest_path = shard_dir / "ncs_gold_embeddings.manifest.json"
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "operation": "prepare_embedding_shards",
            "status": "ready_full_resumable",
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "gold_records_sha256": graph_digest,
            "embedding_manifest_path": str(manifest_path),
            "embedding_manifest_sha256": file_sha256(manifest_path),
            "plan_fingerprint": manifest["plan_fingerprint"],
            "patch_count": manifest["patch_count"],
            "patch_counts": manifest["patch_counts"],
            "semantic_text_included": False,
            "source_db_writes": False,
            "neo4j_writes": False,
            "human_approval_claim": False,
            "generated_at": _now(),
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        _atomic_json(
            report_path,
            _safe_failure(
                builder_version=builder_version,
                source_db_sha256=source_db_sha256,
                operation="prepare_embedding_shards",
                exc=exc,
            ),
        )
        raise


def sync_builder_gold_embedding_shards(
    *,
    builder_version: str,
    version_dir: str | Path,
    source_db_sha256: str,
    apply: bool = False,
    create_indexes: bool = True,
    batch_size: int = 1_000,
    max_retries: int = 2,
    settings: Neo4jLoaderSettings | None = None,
) -> dict[str, Any]:
    """Apply a prepared full shard manifest with ledger-backed resume."""

    gold_dir = Path(version_dir).resolve() / GOLD_DIRECTORY_NAME
    report_path = gold_dir / GOLD_EMBEDDING_LOAD_REPORT_NAME
    try:
        prepared = json.loads(
            (gold_dir / GOLD_EMBEDDING_SHARD_REPORT_NAME).read_text(encoding="utf-8")
        )
        manifest_path = Path(prepared.get("embedding_manifest_path", ""))
        if (
            prepared.get("status") != "ready_full_resumable"
            or prepared.get("builder_version") != builder_version
            or prepared.get("source_db_sha256") != source_db_sha256
            or not manifest_path.is_file()
            or file_sha256(manifest_path) != prepared.get("embedding_manifest_sha256")
        ):
            raise ValueError(
                "prepared embedding shard manifest is not bound to this Builder version"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("prepared embedding shard manifest is invalid")
        if manifest.get("gold_records_sha256") != prepared.get("gold_records_sha256"):
            raise ValueError(
                "prepared embedding shard manifest Gold records digest has changed"
            )
        graph = (
            json.loads((gold_dir / GOLD_LOAD_REPORT_NAME).read_text(encoding="utf-8"))
            if apply
            else {}
        )
        reconciled = bool(
            graph.get("status") == "applied"
            and graph.get("builder_version") == builder_version
            and graph.get("source_db_sha256") == source_db_sha256
            and graph.get("records_sha256") == prepared.get("gold_records_sha256")
            and graph.get("records_sha256") == manifest.get("gold_records_sha256")
            and isinstance(graph.get("reconciliation"), Mapping)
            and graph["reconciliation"].get("ok") is True
        )
        result = apply_embedding_shards(
            manifest_path,
            apply=apply,
            reconciled=reconciled,
            ledger_path=gold_dir / "gold-embedding-shards-ledger.json",
            create_indexes=create_indexes,
            settings=settings,
            batch_size=batch_size,
            max_retries=max_retries,
        )
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "operation": "sync_embedding_shards",
            "status": "applied_full_resumable" if apply else "validated_dry_run",
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "patch_load": result,
            "source_db_writes": False,
            "neo4j_writes": bool(apply),
            "human_approval_claim": False,
            "generated_at": _now(),
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        _atomic_json(
            report_path,
            _safe_failure(
                builder_version=builder_version,
                source_db_sha256=source_db_sha256,
                operation="sync_embedding_shards",
                exc=exc,
            ),
        )
        raise


def sync_builder_gold_embeddings(
    *,
    builder_version: str,
    version_dir: str | Path,
    source_db_sha256: str,
    apply: bool = False,
    create_indexes: bool = True,
    batch_size: int = 1_000,
    max_retries: int = 2,
    settings: Neo4jLoaderSettings | None = None,
) -> dict[str, Any]:
    """Validate or explicitly apply a prepared Builder embedding patch."""

    gold_dir = Path(version_dir).resolve() / GOLD_DIRECTORY_NAME
    graph_report_path = gold_dir / GOLD_LOAD_REPORT_NAME
    prepared_path = gold_dir / GOLD_EMBEDDING_REPORT_NAME
    export_path = gold_dir / GOLD_EMBEDDING_EXPORT_NAME
    report_path = gold_dir / GOLD_EMBEDDING_LOAD_REPORT_NAME
    try:
        prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
        if (
            prepared.get("status") not in {"ready_smoke", "ready_full"}
            or prepared.get("builder_version") != builder_version
            or prepared.get("source_db_sha256") != source_db_sha256
        ):
            raise ValueError(
                "prepared embedding artifact is not bound to this Builder version"
            )
        inspection = inspect_gold_embedding_patches(export_path)
        if file_sha256(export_path) != prepared.get("embedding_export_sha256"):
            raise ValueError("prepared embedding artifact digest has changed")
        manifest = inspection["manifest"]
        if (
            manifest.get("patch_count") != prepared.get("patch_count")
            or manifest.get("dimensions") != prepared.get("dimensions")
            or manifest.get("provider") != prepared.get("provider")
            or manifest.get("model") != prepared.get("model")
        ):
            raise ValueError("prepared embedding manifest has changed")
        if apply:
            if not graph_report_path.is_file():
                raise ValueError(
                    "embedding apply requires a reconciled Gold graph for this Builder version"
                )
            graph_report = json.loads(graph_report_path.read_text(encoding="utf-8"))
            reconciliation = graph_report.get("reconciliation")
            if (
                graph_report.get("status") != "applied"
                or graph_report.get("builder_version") != builder_version
                or graph_report.get("source_db_sha256") != source_db_sha256
                or graph_report.get("records_sha256")
                != prepared.get("gold_records_sha256")
                or not isinstance(reconciliation, Mapping)
                or reconciliation.get("ok") is not True
            ):
                raise ValueError(
                    "embedding apply requires a reconciled Gold graph for this Builder version"
                )
        patch_load = apply_embedding_patches(
            iter_gold_embedding_patches(export_path),
            apply=apply,
            settings=settings,
            batch_size=batch_size,
            max_retries=max_retries,
        )
        vector_indexes = None
        if create_indexes:
            vector_indexes = apply_vector_indexes(
                int(manifest["dimensions"]),
                apply=apply,
                settings=settings,
                max_retries=max_retries,
            )
        loaded_count = sum(patch_load.get("embedding_patch_counts", {}).values())
        if loaded_count != manifest.get("patch_count"):
            raise ValueError(
                "embedding patch validation count does not match its manifest"
            )
        report = {
            "schema": BUILDER_GOLD_SCHEMA,
            "builder_version": builder_version,
            "source_db_sha256": source_db_sha256,
            "operation": "sync_embeddings",
            "status": (
                "applied_smoke"
                if apply and manifest.get("limited_by_max_records")
                else "applied_full"
                if apply
                else "validated_dry_run"
            ),
            "patch_load": patch_load,
            "vector_indexes": vector_indexes,
            "patch_count": manifest.get("patch_count"),
            "limited_by_max_records": bool(manifest.get("limited_by_max_records")),
            "semantic_text_included": False,
            "generated_at": _now(),
            "source_db_writes": False,
            "neo4j_writes": bool(apply),
            "human_approval_claim": False,
        }
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        failure = _safe_failure(
            builder_version=builder_version,
            source_db_sha256=source_db_sha256,
            operation="sync_embeddings",
            exc=exc,
        )
        _atomic_json(report_path, failure)
        raise


def verified_source_sha256(db_path: str | Path) -> str:
    """Compute the Builder source binding using its established hash helper."""

    return file_sha256(Path(db_path))


__all__ = [
    "BUILDER_GOLD_SCHEMA",
    "DEFAULT_GOLD_EMBEDDING_DIMENSIONS",
    "DEFAULT_GOLD_EMBEDDING_MODEL",
    "GOLD_EXPORT_NAME",
    "GOLD_INCREMENTAL_NAME",
    "GOLD_EMBEDDING_EXPORT_NAME",
    "GOLD_EMBEDDING_LOAD_REPORT_NAME",
    "GOLD_EMBEDDING_REPORT_NAME",
    "GOLD_LOAD_CHECKPOINT_NAME",
    "GOLD_LOAD_REPORT_NAME",
    "GOLD_REPORT_NAME",
    "MAX_INTERNAL_ROLES",
    "MAX_ROLE_MAPPING_PACKET_BYTES",
    "gold_preflight",
    "load_internal_role_mapping_packet",
    "prepare_builder_gold",
    "prepare_builder_gold_embeddings",
    "prepare_builder_gold_embedding_shards",
    "sync_builder_gold",
    "sync_builder_gold_embeddings",
    "sync_builder_gold_embedding_shards",
    "verified_source_sha256",
]
