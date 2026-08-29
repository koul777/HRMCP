"""Verify the compact Vercel ontology archive and print reproducible evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CANONICAL_DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"
# Vercel's standard Python function limit applies to the complete assembled
# function, not merely the archive member.  Verify the `vercel build` output
# before deployment so dependencies and runtime files are included in this gate.
MAX_FUNCTION_BUNDLE_BYTES = 500_000_000
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.vercel_snapshot import (  # noqa: E402
    COMPACT_ARCHIVE_NAME,
    COMPACT_MANIFEST_NAME,
    COMPACT_SNAPSHOT_NAME,
    inspect_compact_archive,
    materialize_compact_snapshot,
    readiness_required_min_rows,
    readiness_required_tables,
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_function_bundle_path(bundle_path: Path) -> Path:
    """Resolve the Vercel Python function directory across builder layouts."""

    bundle_path = bundle_path.resolve()
    if bundle_path.is_dir():
        return bundle_path
    candidate = (
        CANONICAL_DEPLOY_ROOT / ".vercel" / "output" / "functions" / "python.func"
    ).resolve()
    if bundle_path == candidate:
        return bundle_path
    if bundle_path.name == "index.func":
        legacy_candidate = (
            bundle_path.parent.parent / "python.func"
        ).resolve()
        if legacy_candidate.is_dir():
            return legacy_candidate
    return bundle_path


def measure_function_bundle(
    bundle_path: Path,
    *,
    max_bytes: int = MAX_FUNCTION_BUNDLE_BYTES,
) -> dict[str, int]:
    """Return the physical size of an assembled Vercel function directory."""

    if max_bytes < 1:
        raise ValueError("function bundle maximum must be positive")
    if not bundle_path.is_dir():
        raise ValueError(f"Vercel function bundle does not exist: {bundle_path}")

    bundle_root = bundle_path.resolve()
    total_bytes = 0
    file_count = 0
    for candidate in bundle_root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"Vercel function bundle must not contain symlinks: {candidate}")
        if not candidate.is_file():
            continue
        total_bytes += candidate.stat().st_size
        file_count += 1
        if total_bytes >= max_bytes:
            raise ValueError(
                "Vercel function bundle exceeds the strict standard-function limit: "
                f"bytes={total_bytes}, limit={max_bytes}"
            )
    return {"bytes": total_bytes, "file_count": file_count, "max_bytes": max_bytes}


def _deployment_readiness_contract(
    manifest_path: Path,
) -> tuple[tuple[str, ...], dict[str, int], Path | None]:
    """Load the nearest Vercel environment contract for a packaged snapshot."""

    manifest_parent = manifest_path.resolve().parent
    candidate_roots = []
    if manifest_parent.name == "api":
        candidate_roots.append(manifest_parent.parent)
    candidate_roots.append(manifest_parent)

    for root in candidate_roots:
        config_path = root / "vercel.json"
        if not config_path.is_file():
            continue
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"unable to read Vercel readiness contract: {config_path}") from exc
        environment = config.get("env") if isinstance(config, dict) else None
        if not isinstance(environment, dict):
            raise ValueError(f"Vercel readiness contract has no env object: {config_path}")
        normalized = {str(key): str(value) for key, value in environment.items()}
        return (
            readiness_required_tables(normalized),
            readiness_required_min_rows(normalized),
            config_path,
        )

    return readiness_required_tables({}), readiness_required_min_rows({}), None


def verify_package(
    archive_path: Path,
    manifest_path: Path,
    *,
    function_bundle_path: Path | None = None,
    require_function_bundle: bool = False,
) -> dict[str, object]:
    manifest, member = inspect_compact_archive(archive_path, manifest_path)
    required_tables, minimum_rows, readiness_config_path = (
        _deployment_readiness_contract(manifest_path)
    )
    with tempfile.TemporaryDirectory(prefix="ncs-ontology-compact-verify-") as temp_dir:
        destination = Path(temp_dir) / COMPACT_SNAPSHOT_NAME
        materialized = materialize_compact_snapshot(
            archive_path,
            manifest_path,
            destination,
            required_tables=required_tables,
            minimum_rows=minimum_rows,
        )
        destination_bytes = destination.stat().st_size if destination.exists() else 0

    archive_bytes = archive_path.stat().st_size
    package_ok = materialized and destination_bytes == manifest["sqlite_bytes"]
    function_bundle: dict[str, object]
    if function_bundle_path is None:
        function_bundle = {
            "checked": False,
            "required": require_function_bundle,
            "ok": not require_function_bundle,
        }
    else:
        resolved_bundle_path = resolve_function_bundle_path(function_bundle_path)
        bundle_measurement = measure_function_bundle(resolved_bundle_path)
        function_bundle = {
            "checked": True,
            "required": require_function_bundle,
            "ok": True,
            "path": str(resolved_bundle_path),
            **bundle_measurement,
        }

    return {
        "ok": package_ok and bool(function_bundle["ok"]),
        "deployment_root": str(CANONICAL_DEPLOY_ROOT.resolve()),
        "archive_path": str(archive_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "archive_bytes": archive_bytes,
        "archive_sha256": _sha256(archive_path),
        "manifest_sha256": _sha256(manifest_path),
        "archive_member": member.filename,
        "member_compressed_bytes": member.compress_size,
        "sqlite_bytes": member.file_size,
        "sqlite_sha256": manifest["sqlite_sha256"],
        "compression_ratio": round(member.file_size / member.compress_size, 4),
        "manifest_schema": manifest["schema"],
        "database_schema": manifest["database_schema"],
        "codec": manifest["codec"],
        "physical_counts": manifest["physical_counts"],
        "logical_counts": manifest["logical_counts"],
        "servable_counts": manifest.get("servable_counts", {}),
        "readiness_contract": {
            "config_path": (
                str(readiness_config_path.resolve()) if readiness_config_path else None
            ),
            "required_tables": list(required_tables),
            "minimum_rows": minimum_rows,
        },
        "function_bundle": function_bundle,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        default=CANONICAL_DEPLOY_ROOT / "api" / COMPACT_ARCHIVE_NAME,
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=CANONICAL_DEPLOY_ROOT / "api" / COMPACT_MANIFEST_NAME,
    )
    parser.add_argument(
        "--function-bundle",
        type=Path,
        default=(
            CANONICAL_DEPLOY_ROOT
            / ".vercel"
            / "output"
            / "functions"
            / "python.func"
        ),
        help="assembled function directory emitted by `vercel build`",
    )
    parser.add_argument(
        "--skip-function-bundle-check",
        action="store_true",
        help="only inspect the archive pair; not sufficient as a deployment gate",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    try:
        result = verify_package(
            args.archive,
            args.manifest,
            function_bundle_path=(
                None if args.skip_function_bundle_check else args.function_bundle
            ),
            require_function_bundle=not args.skip_function_bundle_check,
        )
    except (OSError, ValueError) as exc:
        result = {
            "ok": False,
            "archive_path": str(args.archive),
            "manifest_path": str(args.manifest),
            "error": str(exc),
        }
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
