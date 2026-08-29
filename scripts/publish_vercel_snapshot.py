"""Build, verify, and safely publish the compact Vercel snapshot pair.

The canonical NCS SQLite database is the command's only required input.  This
command does not collect APIs, deploy Vercel, or write review statuses.  It
publishes only the verified compact ZIP and manifest into the selected Vercel
application's ``api`` directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_vercel_snapshot as builder  # noqa: E402


DEFAULT_DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"
ARCHIVE_NAME = "ncs_ontology_compact.zip"
MANIFEST_NAME = "ncs_ontology_compact.manifest.json"
REPORT_SCHEMA = "ncs_vercel_snapshot_publish_report_v1"


class SnapshotPublishError(RuntimeError):
    """Raised when the snapshot pair cannot be published safely."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


def _artifact(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SnapshotPublishError(f"artifact is not a regular file: {path}")
    size_bytes = path.stat().st_size
    if size_bytes <= 0:
        raise SnapshotPublishError(f"artifact is empty: {path}")
    return {
        "path": str(path),
        "bytes": size_bytes,
        "sha256": _sha256(path),
    }


def _optional_artifact(path: Path) -> dict[str, Any] | None:
    return _artifact(path) if path.exists() or path.is_symlink() else None


def _require_contained(root: Path, candidate: Path, *, label: str) -> Path:
    """Resolve *candidate* and reject equality with or escape from *root*."""
    resolved_root = root.expanduser().resolve(strict=False)
    resolved_candidate = candidate.expanduser().resolve(strict=False)
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise SnapshotPublishError(
            f"{label} escapes deploy root: {resolved_candidate}"
        ) from exc
    if not relative.parts:
        raise SnapshotPublishError(f"{label} must be below deploy root")
    return resolved_candidate


def _validate_paths(
    source: Path,
    deploy_root: Path,
    report_path: Path | None,
) -> tuple[Path, Path, Path, Path, Path | None]:
    resolved_source = source.expanduser().resolve(strict=True)
    if not resolved_source.is_file() or resolved_source.is_symlink():
        raise SnapshotPublishError(
            f"source must be a regular, non-symlink SQLite file: {resolved_source}"
        )

    if deploy_root.exists() and (deploy_root.is_symlink() or not deploy_root.is_dir()):
        raise SnapshotPublishError(
            f"deploy root must be a regular directory path: {deploy_root}"
        )
    resolved_root = deploy_root.expanduser().resolve(strict=False)
    api_dir = _require_contained(resolved_root, resolved_root / "api", label="deploy api directory")
    archive = _require_contained(
        resolved_root, api_dir / ARCHIVE_NAME, label="archive target"
    )
    manifest = _require_contained(
        resolved_root, api_dir / MANIFEST_NAME, label="manifest target"
    )
    if api_dir.exists() and (api_dir.is_symlink() or not api_dir.is_dir()):
        raise SnapshotPublishError(f"deploy api path must be a regular directory: {api_dir}")
    for label, target in (("archive", archive), ("manifest", manifest)):
        if target.is_symlink():
            raise SnapshotPublishError(f"{label} target must not be a symlink: {target}")
        if target == resolved_source:
            raise SnapshotPublishError(f"{label} target must not be the source database")

    resolved_report: Path | None = None
    if report_path is not None:
        resolved_report = report_path.expanduser().resolve(strict=False)
        if resolved_report == resolved_source:
            raise SnapshotPublishError("report path must not be the source database")
        try:
            resolved_report.relative_to(resolved_root)
        except ValueError:
            pass
        else:
            raise SnapshotPublishError(
                "report path must be outside deploy root; only the snapshot pair may be published"
            )
        if resolved_report.is_symlink() or (
            resolved_report.exists() and not resolved_report.is_file()
        ):
            raise SnapshotPublishError(f"report path must be a regular file path: {resolved_report}")
    return resolved_source, resolved_root, archive, manifest, resolved_report


def _pair_state(archive: Path, manifest: Path) -> tuple[str, dict[str, Any]]:
    archive_record = _optional_artifact(archive)
    manifest_record = _optional_artifact(manifest)
    if (archive_record is None) != (manifest_record is None):
        raise SnapshotPublishError(
            "deploy target contains an incomplete snapshot pair; refusing publication"
        )
    state = "complete" if archive_record is not None else "absent"
    return state, {"archive": archive_record, "manifest": manifest_record}


def _same_artifacts(
    expected: dict[str, Any], current: dict[str, Any]
) -> bool:
    for name in ("archive", "manifest"):
        left = expected.get(name)
        right = current.get(name)
        if (left is None) != (right is None):
            return False
        if left is not None and (
            left["bytes"] != right["bytes"] or left["sha256"] != right["sha256"]
        ):
            return False
    return True


def _reserve_temp_path(directory: Path, *, prefix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=directory)
    os.close(descriptor)
    return Path(raw_path)


def _copy_verified(source: Path, target_dir: Path, *, prefix: str) -> Path:
    incoming = _reserve_temp_path(target_dir, prefix=prefix)
    try:
        shutil.copyfile(source, incoming)
        source_record = _artifact(source)
        incoming_record = _artifact(incoming)
        if (
            source_record["bytes"] != incoming_record["bytes"]
            or source_record["sha256"] != incoming_record["sha256"]
        ):
            raise SnapshotPublishError(f"staged publication copy changed content: {source}")
        return incoming
    except Exception:
        incoming.unlink(missing_ok=True)
        raise


def _publish_pair(
    *,
    staged_archive: Path,
    staged_manifest: Path,
    target_archive: Path,
    target_manifest: Path,
    expected_old: dict[str, Any],
) -> dict[str, Any]:
    """Replace a verified pair, restoring the complete old pair on failure."""
    target_dir = target_archive.parent
    target_dir.mkdir(parents=True, exist_ok=True)
    if target_manifest.parent != target_dir:
        raise SnapshotPublishError("snapshot targets must share one publication directory")

    _, current_old = _pair_state(target_archive, target_manifest)
    if not _same_artifacts(expected_old, current_old):
        raise SnapshotPublishError(
            "deploy artifacts changed while the snapshot was building; refusing publication"
        )

    incoming = {
        target_archive: _copy_verified(
            staged_archive, target_dir, prefix=f".{ARCHIVE_NAME}.incoming."
        ),
        target_manifest: _copy_verified(
            staged_manifest, target_dir, prefix=f".{MANIFEST_NAME}.incoming."
        ),
    }
    backups: dict[Path, Path] = {}
    moved_old: list[Path] = []
    published: list[Path] = []
    rollback_errors: list[str] = []
    publication_ok = False
    try:
        for target in (target_archive, target_manifest):
            if current_old["archive" if target == target_archive else "manifest"] is None:
                continue
            backup = _reserve_temp_path(target_dir, prefix=f".{target.name}.backup.")
            backup.unlink()
            backups[target] = backup
            os.replace(target, backup)
            moved_old.append(target)

        # The manifest is the commit marker: consumers verify it against the
        # archive, so it is replaced only after the archive is in place.
        for target in (target_archive, target_manifest):
            os.replace(incoming[target], target)
            published.append(target)

        published_records = {
            "archive": _artifact(target_archive),
            "manifest": _artifact(target_manifest),
        }
        staged_records = {
            "archive": _artifact(staged_archive),
            "manifest": _artifact(staged_manifest),
        }
        if not _same_artifacts(staged_records, published_records):
            raise SnapshotPublishError("published snapshot hashes differ from verified staging")
        publication_ok = True
        return {
            "ok": True,
            "rollback_performed": False,
            "rollback_ok": None,
            "published_artifacts": published_records,
        }
    except Exception as exc:
        for target in reversed(published):
            try:
                target.unlink(missing_ok=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"remove {target}: {rollback_exc}")
        for target in reversed(moved_old):
            backup = backups[target]
            try:
                os.replace(backup, target)
            except OSError as rollback_exc:
                rollback_errors.append(f"restore {target}: {rollback_exc}")
        if rollback_errors:
            recovery = [str(path) for path in backups.values() if path.exists()]
            raise SnapshotPublishError(
                f"publication failed ({exc}); rollback failed: {'; '.join(rollback_errors)}; "
                f"recovery artifacts: {recovery}"
            ) from exc
        raise SnapshotPublishError(f"publication failed and was rolled back: {exc}") from exc
    finally:
        for path in incoming.values():
            path.unlink(missing_ok=True)
        if publication_ok:
            for path in backups.values():
                path.unlink(missing_ok=True)


def _source_after_build(source: Path, expected: dict[str, Any]) -> dict[str, Any]:
    current = builder._source_artifact(source)  # Reuse the builder's SQLite/header contract.
    if current["bytes"] != expected["bytes"] or current["sha256"] != expected["sha256"]:
        raise SnapshotPublishError("canonical source database changed during snapshot build")
    return current


def publish_snapshot(
    *,
    source: Path,
    deploy_root: Path = DEFAULT_DEPLOY_ROOT,
    report_path: Path | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build and publish one canonical DB, returning complete audit evidence."""
    source, deploy_root, target_archive, target_manifest, resolved_report = _validate_paths(
        source, deploy_root, report_path
    )
    pair_state, old_artifacts = _pair_state(target_archive, target_manifest)
    policy = {
        "source_database_mutated": False,
        "api_collection_called": False,
        "human_review_statuses_changed": False,
        "vercel_deployment_performed": False,
        "published_file_allowlist": [ARCHIVE_NAME, MANIFEST_NAME],
        "stage_verified_before_publish": False,
        "source_hash_rechecked_after_build": False,
        "path_containment_enforced": True,
        "two_file_publication_strategy": "backup_replace_archive_then_manifest_rollback",
    }

    with tempfile.TemporaryDirectory(prefix="ncs-vercel-publish-stage-") as temp_dir:
        stage_root = Path(temp_dir)
        output_db = stage_root / "ncs_ontology_compact.db"
        staged_archive = stage_root / ARCHIVE_NAME
        staged_manifest = stage_root / MANIFEST_NAME
        stage_report_path = stage_root / "build-report.json"
        try:
            build_report = builder.build_snapshot(
                source=source,
                output_db=output_db,
                archive=staged_archive,
                manifest=staged_manifest,
                report_path=stage_report_path,
                dry_run=dry_run,
            )
        except (OSError, builder.SnapshotBuildError) as exc:
            build_report = {
                "schema": builder.REPORT_SCHEMA,
                "ok": False,
                "dry_run": dry_run,
                "source": None,
                "stages": [],
                "artifacts": {},
                "error": {"stage": "build_preflight", "message": str(exc)},
            }
        report: dict[str, Any] = {
            "schema": REPORT_SCHEMA,
            "ok": False,
            "dry_run": dry_run,
            "source": build_report.get("source"),
            "deploy_root": str(deploy_root),
            "targets": {
                "archive": str(target_archive),
                "manifest": str(target_manifest),
                "report": str(resolved_report) if resolved_report else None,
            },
            "target_pair_state_before": pair_state,
            "old_artifacts": old_artifacts,
            "old_artifacts_replaced": False,
            "would_replace_old_artifacts": pair_state == "complete",
            "published_artifacts": {},
            "build": build_report,
            "publication": {
                "attempted": False,
                "rollback_performed": False,
                "rollback_ok": None,
            },
            "policy": policy,
        }
        if not build_report.get("ok"):
            report["error"] = {
                "stage": "build",
                "message": "snapshot build or verification failed",
            }
            return report

        if dry_run:
            report["ok"] = True
            return report

        stages = build_report.get("stages") or []
        if not stages or stages[-1].get("name") != "verify_archive_only" or stages[-1].get(
            "returncode"
        ) != 0:
            report["error"] = {
                "stage": "verification_contract",
                "message": "builder did not provide successful final verification evidence",
            }
            return report
        policy["stage_verified_before_publish"] = True

        try:
            _source_after_build(source, build_report["source"])
            policy["source_hash_rechecked_after_build"] = True
            staged_records = {
                "archive": _artifact(staged_archive),
                "manifest": _artifact(staged_manifest),
            }
            build_artifacts = build_report.get("artifacts") or {}
            for name in ("archive", "manifest"):
                evidence = build_artifacts.get(name) or {}
                if (
                    evidence.get("bytes") != staged_records[name]["bytes"]
                    or evidence.get("sha256") != staged_records[name]["sha256"]
                ):
                    raise SnapshotPublishError(
                        f"builder {name} evidence does not match staged artifact"
                    )
            report["publication"]["attempted"] = True
            publication = _publish_pair(
                staged_archive=staged_archive,
                staged_manifest=staged_manifest,
                target_archive=target_archive,
                target_manifest=target_manifest,
                expected_old=old_artifacts,
            )
        except (OSError, builder.SnapshotBuildError, SnapshotPublishError) as exc:
            message = str(exc)
            report["publication"].update(
                {
                    "rollback_performed": "rolled back" in message,
                    "rollback_ok": True if "rolled back" in message else None,
                }
            )
            report["error"] = {"stage": "publication", "message": message}
            return report

        report["publication"] = {
            "attempted": True,
            "rollback_performed": publication["rollback_performed"],
            "rollback_ok": publication["rollback_ok"],
        }
        report["published_artifacts"] = publication["published_artifacts"]
        report["old_artifacts_replaced"] = pair_state == "complete"
        report["ok"] = True
        return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _reserve_temp_path(path.parent, prefix=f".{path.name}.")
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, required=True, help="canonical input NCS SQLite database"
    )
    parser.add_argument(
        "--deploy-root",
        type=Path,
        default=DEFAULT_DEPLOY_ROOT,
        help="Vercel application root; artifacts are published only under its api directory",
    )
    parser.add_argument("--dry-run", action="store_true", help="build plan only; publish nothing")
    parser.add_argument("--report", type=Path, help="optional JSON publication report")
    args = parser.parse_args(argv)
    try:
        report = publish_snapshot(
            source=args.source,
            deploy_root=args.deploy_root,
            report_path=args.report,
            dry_run=args.dry_run,
        )
    except (OSError, builder.SnapshotBuildError, SnapshotPublishError) as exc:
        report = {
            "schema": REPORT_SCHEMA,
            "ok": False,
            "dry_run": args.dry_run,
            "error": {"stage": "preflight", "message": str(exc)},
        }
    validated_report = (report.get("targets") or {}).get("report")
    if args.report and validated_report:
        try:
            _write_report(Path(validated_report), report)
        except OSError as exc:
            print(
                json.dumps(
                    {**report, "report_write_error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
    elif args.report:
        report["report_write_skipped"] = "report path did not pass publication preflight"
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
