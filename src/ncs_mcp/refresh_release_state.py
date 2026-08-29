from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .ontology_refresh_builder import (
    BASELINE_LINEAGE_SCHEMA,
    MANAGED_POINTER_SCHEMA,
    REPORT_SCHEMA as REFRESH_REPORT_SCHEMA,
    validate_ontology_database,
)


PROMOTION_REPORT_SCHEMA = "ncs_ontology_refresh_baseline_promotion_report_v1"
PUBLISH_REPORT_SCHEMA = "ncs_vercel_snapshot_publish_report_v1"
REMOTE_VERIFICATION_SCHEMA = "ncs_remote_mcp_transport_verification_v1"
SHA256_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")


class RefreshReleaseStateError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _artifact(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RefreshReleaseStateError(f"artifact is not a regular file: {path}")
    size = path.stat().st_size
    if size <= 0:
        raise RefreshReleaseStateError(f"artifact is empty: {path}")
    return {"path": str(path.resolve()), "bytes": size, "sha256": _sha256(path)}


def _same_identity(
    expected: Any, actual: dict[str, Any], *, require_path: bool
) -> bool:
    if not isinstance(expected, dict):
        return False
    if expected.get("bytes") != actual.get("bytes"):
        return False
    if expected.get("sha256") != actual.get("sha256"):
        return False
    if not SHA256_PATTERN.fullmatch(str(expected.get("sha256") or "")):
        return False
    if require_path:
        try:
            expected_path = (
                Path(str(expected.get("path") or "")).expanduser().resolve(strict=True)
            )
        except (OSError, RuntimeError):
            return False
        return expected_path == Path(str(actual["path"])).resolve(strict=True)
    return True


def _load_json_report(path: str | Path, *, label: str) -> tuple[Path, dict[str, Any]]:
    supplied = Path(path).expanduser()
    if supplied.is_symlink():
        raise RefreshReleaseStateError(f"{label} must not be a symlink")
    resolved = supplied.resolve(strict=True)
    if not resolved.is_file():
        raise RefreshReleaseStateError(f"{label} is not a regular file")
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RefreshReleaseStateError(f"{label} is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RefreshReleaseStateError(f"{label} must contain a JSON object")
    return resolved, payload


def _sidecar_paths(database: Path) -> list[Path]:
    return [
        database.with_name(database.name + suffix)
        for suffix in ("-wal", "-shm", "-journal")
    ]


def _safe_verification_url(value: Any, *, label: str) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise RefreshReleaseStateError(
            f"{label} URL must be a non-secret HTTP(S) endpoint without query or fragment"
        )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _write_json_temporary(directory: Path, name: str, payload: dict[str, Any]) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{name}.", suffix=".tmp", dir=directory
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return temporary


def _atomic_create_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = _write_json_temporary(path.parent, path.name, payload)
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise RefreshReleaseStateError(
                f"immutable lineage artifact already exists: {path}"
            )
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = _write_json_temporary(path.parent, path.name, payload)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_exact_immutable(
    source: Path, target: Path, expected: dict[str, Any]
) -> dict[str, Any]:
    if any(path.exists() for path in _sidecar_paths(source)):
        raise RefreshReleaseStateError(
            "publisher source gained a SQLite sidecar before promotion copy"
        )
    if not _same_identity(expected, _artifact(source), require_path=True):
        raise RefreshReleaseStateError("publisher source changed before promotion copy")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.incoming.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        with temporary.open("r+b") as stream:
            os.fsync(stream.fileno())
        copied = _artifact(temporary)
        if not _same_identity(expected, copied, require_path=False):
            raise RefreshReleaseStateError(
                "managed baseline copy does not match publisher source identity"
            )
        validation = validate_ontology_database(temporary)
        if not validation.get("ok"):
            raise RefreshReleaseStateError(
                "managed baseline copy failed derived ontology validation"
            )
        if any(path.exists() for path in _sidecar_paths(source)):
            raise RefreshReleaseStateError(
                "publisher source gained a SQLite sidecar during promotion copy"
            )
        if not _same_identity(expected, _artifact(source), require_path=True):
            raise RefreshReleaseStateError(
                "publisher source changed during promotion copy"
            )
        try:
            os.link(temporary, target)
        except FileExistsError:
            existing = _artifact(target)
            if not _same_identity(expected, existing, require_path=False):
                raise RefreshReleaseStateError(
                    "immutable baseline version exists with different content"
                )
        return _artifact(target)
    finally:
        temporary.unlink(missing_ok=True)


def _blocked_report(
    *,
    state: Path,
    blockers: list[dict[str, str]],
    inputs: dict[str, Any],
    publisher_source: dict[str, Any] | None = None,
    integrity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema": PROMOTION_REPORT_SCHEMA,
        "ok": False,
        "status": "blocked",
        "state_dir": str(state),
        "blockers": blockers,
        "inputs": inputs,
        "publisher_source": publisher_source,
        "integrity": integrity,
        "promoted_baseline": None,
        "lineage": None,
        "pointer": None,
        "safety": {
            "source_database_mutated": False,
            "api_calls": False,
            "deployment_performed": False,
            "publication_performed": False,
            "review_status_writes": False,
            "automatic_deletion": False,
        },
    }


def promote_refresh_baseline(
    *,
    refresh_report_path: str | Path,
    publish_report_path: str | Path,
    remote_verification_path: str | Path,
    staged_verification_path: str | Path | None = None,
    state_dir: str | Path = ".state/ncs-ontology-refresh",
) -> dict[str, Any]:
    """Promote only the exact source proven through refresh, publish, and remote verify."""
    supplied_state = Path(state_dir).expanduser()
    state_path_is_symlink = supplied_state.is_symlink()
    state = supplied_state.resolve(strict=False)
    inputs: dict[str, Any] = {}
    blockers: list[dict[str, str]] = []
    try:
        refresh_path, refresh = _load_json_report(
            refresh_report_path, label="refresh report"
        )
        publish_path, publish = _load_json_report(
            publish_report_path, label="publish report"
        )
        verify_path, verification = _load_json_report(
            remote_verification_path, label="remote verification report"
        )
        inputs = {
            "refresh_report": _artifact(refresh_path),
            "publish_report": _artifact(publish_path),
            "remote_verification": _artifact(verify_path),
        }
        staged_verification: dict[str, Any] | None = None
        if staged_verification_path is not None:
            staged_path, staged_verification = _load_json_report(
                staged_verification_path, label="staged verification report"
            )
            inputs["staged_verification"] = _artifact(staged_path)
    except (OSError, RefreshReleaseStateError) as exc:
        return _blocked_report(
            state=state,
            blockers=[{"code": "invalid_evidence_artifact", "message": str(exc)}],
            inputs=inputs,
        )

    if refresh.get("schema") != REFRESH_REPORT_SCHEMA:
        blockers.append(
            {
                "code": "refresh_schema_invalid",
                "message": "refresh report schema is not supported",
            }
        )
    if not (
        refresh.get("ok") is True
        and refresh.get("mode") == "apply"
        and refresh.get("status") == "completed"
        and (refresh.get("validation") or {}).get("ok") is True
        and (refresh.get("safety") or {}).get("apply_blocked") is False
    ):
        blockers.append(
            {
                "code": "refresh_not_successful_apply",
                "message": "refresh report is not a successful unblocked apply",
            }
        )

    publisher_record = refresh.get("publisher_source")
    publisher_source: Path | None = None
    actual_publisher: dict[str, Any] | None = None
    if not isinstance(publisher_record, dict):
        blockers.append(
            {
                "code": "publisher_source_missing",
                "message": "refresh report has no publisher_source artifact",
            }
        )
    else:
        try:
            supplied_publisher = Path(
                str(publisher_record.get("path") or "")
            ).expanduser()
            if supplied_publisher.is_symlink():
                raise RefreshReleaseStateError("publisher source must not be a symlink")
            publisher_source = supplied_publisher.resolve(strict=True)
            actual_publisher = _artifact(publisher_source)
            if not _same_identity(
                publisher_record, actual_publisher, require_path=True
            ):
                blockers.append(
                    {
                        "code": "publisher_source_hash_mismatch",
                        "message": "publisher source no longer matches refresh evidence",
                    }
                )
            existing_sidecars = [
                str(path) for path in _sidecar_paths(publisher_source) if path.exists()
            ]
            if existing_sidecars:
                blockers.append(
                    {
                        "code": "publisher_source_has_sqlite_sidecars",
                        "message": "publisher source must be closed and sidecar-free before exact promotion",
                    }
                )
        except (OSError, RefreshReleaseStateError) as exc:
            blockers.append({"code": "publisher_source_invalid", "message": str(exc)})

    if publish.get("schema") != PUBLISH_REPORT_SCHEMA:
        blockers.append(
            {
                "code": "publish_schema_invalid",
                "message": "publish report schema is not supported",
            }
        )
    if not (
        publish.get("ok") is True
        and publish.get("dry_run") is False
        and (publish.get("publication") or {}).get("attempted") is True
        and (publish.get("policy") or {}).get("stage_verified_before_publish") is True
        and (publish.get("policy") or {}).get("source_hash_rechecked_after_build")
        is True
    ):
        blockers.append(
            {
                "code": "publish_not_successful_non_dry",
                "message": "publish report is not a successful non-dry publication",
            }
        )
    if actual_publisher is not None and not _same_identity(
        publish.get("source"), actual_publisher, require_path=True
    ):
        blockers.append(
            {
                "code": "publish_source_identity_mismatch",
                "message": "publish report source is not the refresh publisher source",
            }
        )

    if verification.get("schema") != REMOTE_VERIFICATION_SCHEMA:
        blockers.append(
            {
                "code": "remote_verification_schema_invalid",
                "message": "remote verification schema is not supported",
            }
        )
    production_url: str | None = None
    try:
        production_url = _safe_verification_url(
            verification.get("url"), label="production verification"
        )
    except RefreshReleaseStateError as exc:
        blockers.append(
            {"code": "remote_verification_url_invalid", "message": str(exc)}
        )

    staged_url: str | None = None
    if staged_verification_path is not None:
        assert staged_verification is not None
        if staged_verification.get("schema") != REMOTE_VERIFICATION_SCHEMA:
            blockers.append(
                {
                    "code": "staged_verification_schema_invalid",
                    "message": "staged verification schema is not supported",
                }
            )
        if not (
            staged_verification.get("ok") is True
            and staged_verification.get("failures") == []
            and isinstance(staged_verification.get("checks"), dict)
            and bool(staged_verification.get("checks"))
        ):
            blockers.append(
                {
                    "code": "staged_verification_failed",
                    "message": "staged MCP verification did not pass",
                }
            )
        try:
            staged_url = _safe_verification_url(
                staged_verification.get("url"), label="staged verification"
            )
        except RefreshReleaseStateError as exc:
            blockers.append(
                {"code": "staged_verification_url_invalid", "message": str(exc)}
            )
    if not (
        verification.get("ok") is True
        and verification.get("failures") == []
        and isinstance(verification.get("checks"), dict)
        and bool(verification.get("checks"))
    ):
        blockers.append(
            {
                "code": "remote_verification_failed",
                "message": "remote MCP verification did not pass",
            }
        )

    rule_fingerprint = str(refresh.get("rule_fingerprint") or "")
    if not SHA256_PATTERN.fullmatch(rule_fingerprint):
        blockers.append(
            {
                "code": "rule_fingerprint_invalid",
                "message": "refresh rule fingerprint is invalid",
            }
        )

    integrity: dict[str, Any] | None = None
    if actual_publisher is not None:
        try:
            integrity = validate_ontology_database(Path(actual_publisher["path"]))
        except (OSError, ValueError) as exc:
            blockers.append(
                {"code": "publisher_integrity_unreadable", "message": str(exc)}
            )
        else:
            if not integrity.get("ok"):
                blockers.append(
                    {
                        "code": "publisher_integrity_failed",
                        "message": "publisher source lacks required derived ontology evidence",
                    }
                )

    if state_path_is_symlink or (
        state.exists() and (state.is_symlink() or not state.is_dir())
    ):
        blockers.append(
            {
                "code": "state_dir_invalid",
                "message": "state directory is not a regular directory",
            }
        )
    baselines_dir = state / "baselines"
    pointer_path = state / "current.json"
    if baselines_dir.exists() and (
        baselines_dir.is_symlink() or not baselines_dir.is_dir()
    ):
        blockers.append(
            {
                "code": "baselines_dir_invalid",
                "message": "managed baselines path is not a regular directory",
            }
        )
    if pointer_path.exists() and (
        pointer_path.is_symlink() or not pointer_path.is_file()
    ):
        blockers.append(
            {
                "code": "pointer_path_invalid",
                "message": "managed baseline pointer is not a regular file",
            }
        )
    if blockers:
        return _blocked_report(
            state=state,
            blockers=blockers,
            inputs=inputs,
            publisher_source=actual_publisher,
            integrity=integrity,
        )

    assert publisher_source is not None and actual_publisher is not None
    digest = actual_publisher["sha256"].split(":", 1)[1]
    rule_digest = rule_fingerprint.split(":", 1)[1]
    baseline_path = baselines_dir / f"ncs-ontology-{digest}-{rule_digest[:12]}.db"
    lineage_path = baseline_path.with_suffix(baseline_path.suffix + ".refresh.json")
    state.mkdir(parents=True, exist_ok=True)
    baselines_dir.mkdir(parents=True, exist_ok=True)

    try:
        if baseline_path.is_symlink() or lineage_path.is_symlink():
            raise RefreshReleaseStateError(
                "managed baseline version paths must not be symlinks"
            )
        if baseline_path.exists():
            baseline_artifact = _artifact(baseline_path)
            if not _same_identity(
                actual_publisher, baseline_artifact, require_path=False
            ):
                raise RefreshReleaseStateError(
                    "immutable baseline version exists with different content"
                )
        else:
            baseline_artifact = _copy_exact_immutable(
                publisher_source, baseline_path, actual_publisher
            )
        source_after = _artifact(publisher_source)
        if not _same_identity(actual_publisher, source_after, require_path=True):
            raise RefreshReleaseStateError("publisher source changed during promotion")

        promoted_at = datetime.now(UTC).isoformat()
        lineage_payload = {
            "schema": BASELINE_LINEAGE_SCHEMA,
            "promoted_at": promoted_at,
            "baseline": baseline_artifact,
            "publisher_source": actual_publisher,
            "rule_fingerprint": rule_fingerprint,
            "refresh_strategy": refresh.get("selected_strategy"),
            "evidence_artifacts": inputs,
            "verification_targets": {
                "staged": (
                    {
                        "url": staged_url,
                        "report": inputs["staged_verification"],
                    }
                    if staged_url is not None
                    else None
                ),
                "production": {
                    "url": production_url,
                    "report": inputs["remote_verification"],
                },
            },
            "safety": {
                "source_database_mutated": False,
                "api_calls": False,
                "deployment_performed": False,
                "publication_performed": False,
                "review_status_writes": False,
            },
        }
        if lineage_path.exists():
            existing_lineage = json.loads(lineage_path.read_text(encoding="utf-8"))
            if (
                existing_lineage.get("schema") != BASELINE_LINEAGE_SCHEMA
                or existing_lineage.get("rule_fingerprint") != rule_fingerprint
                or not _same_identity(
                    existing_lineage.get("baseline"),
                    baseline_artifact,
                    require_path=True,
                )
            ):
                raise RefreshReleaseStateError(
                    "immutable baseline lineage exists with different content"
                )
        else:
            _atomic_create_json(lineage_path, lineage_payload)
        lineage_artifact = _artifact(lineage_path)
        pointer_payload = {
            "schema": MANAGED_POINTER_SCHEMA,
            "updated_at": promoted_at,
            "baseline": {
                **baseline_artifact,
                "path": str(baseline_path.relative_to(state)),
            },
            "lineage": {
                **lineage_artifact,
                "path": str(lineage_path.relative_to(state)),
            },
            "rule_fingerprint": rule_fingerprint,
        }
        _atomic_replace_json(pointer_path, pointer_payload)
        pointer_artifact = _artifact(pointer_path)
    except (OSError, ValueError, json.JSONDecodeError, RefreshReleaseStateError) as exc:
        return _blocked_report(
            state=state,
            blockers=[{"code": "promotion_write_failed", "message": str(exc)}],
            inputs=inputs,
            publisher_source=actual_publisher,
            integrity=integrity,
        )

    return {
        "schema": PROMOTION_REPORT_SCHEMA,
        "ok": True,
        "status": "promoted",
        "state_dir": str(state),
        "blockers": [],
        "inputs": inputs,
        "publisher_source": actual_publisher,
        "integrity": integrity,
        "verification_targets": {
            "staged": staged_url,
            "production": production_url,
        },
        "promoted_baseline": baseline_artifact,
        "lineage": lineage_artifact,
        "pointer": pointer_artifact,
        "safety": {
            "source_database_mutated": False,
            "api_calls": False,
            "deployment_performed": False,
            "publication_performed": False,
            "review_status_writes": False,
            "automatic_deletion": False,
        },
    }


def write_promotion_report(path: str | Path, report: dict[str, Any]) -> None:
    target = Path(path).expanduser().resolve(strict=False)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RefreshReleaseStateError(
            "promotion report path is not a regular file path"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace_json(target, report)


__all__ = [
    "PROMOTION_REPORT_SCHEMA",
    "RefreshReleaseStateError",
    "promote_refresh_baseline",
    "write_promotion_report",
]
