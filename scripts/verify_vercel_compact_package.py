"""Verify the compact Vercel ontology archive and print reproducible evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CANONICAL_DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"
# Vercel's standard Python function limit applies to the complete assembled
# function, not merely the archive member.  Verify the `vercel build` output
# before deployment so dependencies and runtime files are included in this gate.
MAX_FUNCTION_BUNDLE_BYTES = 500_000_000
FUNCTION_CONFIG_NAME = ".vc-config.json"
FORBIDDEN_FUNCTION_MAP_ROOTS = frozenset(
    {
        ".vercel",
        "data",
        "deploy",
        "docs",
        "reports",
        "scripts",
        "tests",
        "tmp",
    }
)
FORBIDDEN_DATABASE_EXTENSIONS = (
    ".db",
    ".sqlite",
    ".sqlite3",
)
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

# These names are imported above; define the required logical bundle entries
# after the import so the verifier remains tied to the runtime contract.
REQUIRED_FUNCTION_MAP_PATHS = frozenset(
    {
        f"api/{COMPACT_ARCHIVE_NAME}",
        f"api/{COMPACT_MANIFEST_NAME}",
    }
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


def _normalize_file_map_path(raw_path: object, *, field: str) -> str:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"Vercel {field} must be a non-empty string")
    normalized = raw_path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    parsed = PurePosixPath(normalized)
    if parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError(f"Vercel {field} must stay inside the deployment root: {raw_path}")
    return parsed.as_posix()


def _is_database_path(path: str) -> bool:
    basename = PurePosixPath(path).name.lower()
    return any(
        basename.endswith(extension) or f"{extension}-" in basename
        for extension in FORBIDDEN_DATABASE_EXTENSIONS
    )


def _is_forbidden_logical_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    if not parts:
        return True
    lowered = tuple(part.lower() for part in parts)
    return lowered[0] in FORBIDDEN_FUNCTION_MAP_ROOTS or "__pycache__" in lowered


def _deployment_root_for_bundle(bundle_root: Path) -> Path:
    for candidate in (bundle_root, *bundle_root.parents):
        if candidate.name == ".vercel":
            return candidate.parent.resolve()
    return bundle_root


def _resolve_file_map_target(
    bundle_root: Path,
    deployment_root: Path,
    target_path: str,
) -> Path:
    relative_target = Path(*PurePosixPath(target_path).parts)
    search_roots = (deployment_root, bundle_root)
    for search_root in search_roots:
        candidate = (search_root / relative_target).resolve()
        try:
            candidate.relative_to(deployment_root)
        except ValueError as exc:
            raise ValueError(
                "Vercel filePathMap target escapes the deployment root: "
                f"{target_path}"
            ) from exc
        if candidate.is_file():
            if candidate.is_symlink():
                raise ValueError(
                    "Vercel filePathMap target must not be a symlink: "
                    f"{candidate}"
                )
            return candidate
    raise ValueError(f"Vercel filePathMap target does not exist: {target_path}")


def measure_function_bundle(
    bundle_path: Path,
    *,
    max_bytes: int = MAX_FUNCTION_BUNDLE_BYTES,
) -> dict[str, object]:
    """Measure physical files plus every source referenced by ``filePathMap``."""

    if max_bytes < 1:
        raise ValueError("function bundle maximum must be positive")
    if not bundle_path.is_dir():
        raise ValueError(f"Vercel function bundle does not exist: {bundle_path}")

    bundle_root = bundle_path.resolve()
    physical_bytes = 0
    physical_file_count = 0
    physical_paths: set[Path] = set()
    for candidate in bundle_root.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"Vercel function bundle must not contain symlinks: {candidate}")
        if not candidate.is_file():
            continue
        physical_bytes += candidate.stat().st_size
        physical_file_count += 1
        physical_paths.add(candidate.resolve())
        if physical_bytes >= max_bytes:
            raise ValueError(
                "Vercel function bundle exceeds the strict standard-function limit: "
                f"bytes={physical_bytes}, limit={max_bytes}"
            )

    config_path = bundle_root / FUNCTION_CONFIG_NAME
    if not config_path.is_file():
        raise ValueError(
            "Vercel function bundle is missing filePathMap metadata: "
            f"{config_path}"
        )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"unable to read Vercel function config: {config_path}") from exc
    file_path_map = config.get("filePathMap") if isinstance(config, dict) else None
    if not isinstance(file_path_map, dict) or not file_path_map:
        raise ValueError(f"Vercel function config has no filePathMap: {config_path}")

    deployment_root = _deployment_root_for_bundle(bundle_root)
    mapped_bytes = 0
    mapped_file_count = 0
    unique_mapped_file_count = 0
    unique_mapped_targets: set[Path] = set()
    mapped_paths: set[str] = set()
    for raw_logical_path, raw_target_path in file_path_map.items():
        logical_path = _normalize_file_map_path(
            raw_logical_path, field="filePathMap logical path"
        )
        target_path = _normalize_file_map_path(
            raw_target_path, field="filePathMap target"
        )
        if _is_database_path(logical_path) or _is_database_path(target_path):
            raise ValueError(
                "Vercel function filePathMap contains a forbidden database file: "
                f"logical={logical_path}, target={target_path}"
            )
        if _is_forbidden_logical_path(logical_path):
            raise ValueError(
                "Vercel function filePathMap contains a forbidden source path: "
                f"{logical_path}"
            )
        target = _resolve_file_map_target(bundle_root, deployment_root, target_path)
        mapped_file_count += 1
        if target not in physical_paths and target not in unique_mapped_targets:
            unique_mapped_targets.add(target)
            mapped_bytes += target.stat().st_size
            unique_mapped_file_count += 1
        mapped_paths.add(logical_path)
        assembled_bytes = physical_bytes + mapped_bytes
        if assembled_bytes >= max_bytes:
            raise ValueError(
                "Vercel function bundle exceeds the strict standard-function limit "
                "after filePathMap resolution: "
                f"bytes={assembled_bytes}, limit={max_bytes}, "
                f"logical={logical_path}"
            )

    missing_required = sorted(REQUIRED_FUNCTION_MAP_PATHS - mapped_paths)
    if missing_required:
        raise ValueError(
            "Vercel function filePathMap is missing compact deployment inputs: "
            + ", ".join(missing_required)
        )

    total_bytes = physical_bytes + mapped_bytes
    return {
        "bytes": total_bytes,
        "file_count": physical_file_count + unique_mapped_file_count,
        "max_bytes": max_bytes,
        "physical_bytes": physical_bytes,
        "physical_file_count": physical_file_count,
        "mapped_bytes": mapped_bytes,
        "mapped_file_count": mapped_file_count,
        "unique_mapped_file_count": unique_mapped_file_count,
        "file_path_map_checked": True,
        "file_path_map_path": str(config_path),
        "forbidden_mapping_count": 0,
        "database_mapping_count": 0,
        "required_mapping_paths": sorted(REQUIRED_FUNCTION_MAP_PATHS),
    }


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
