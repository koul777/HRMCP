"""Package a verified compact ontology DB for the standard Vercel function."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
import zipfile
from contextlib import closing
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CANONICAL_DEPLOY_ROOT = ROOT / "deploy" / "vercel_mcp_app"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.vercel_snapshot import (  # noqa: E402
    COMPACT_ARCHIVE_NAME,
    COMPACT_DATABASE_SCHEMA,
    COMPACT_MANIFEST_NAME,
    COMPACT_MANIFEST_SCHEMA,
    COMPACT_POSTING_CODEC,
    COMPACT_SNAPSHOT_NAME,
    MAX_SNAPSHOT_BYTES,
    inspect_compact_archive,
)
from ncs_mcp.builder_authorization import BuilderOperationContext  # noqa: E402
from ncs_mcp.builder_release import package_guard  # noqa: E402


_SQLITE_HEADER = b"SQLite format 3\x00"
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_compact_metadata(
    database_path: Path,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    database_uri = f"file:{database_path.resolve().as_posix()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(database_uri, uri=True)) as conn:
        conn.execute("PRAGMA query_only = ON")
        quick_check = conn.execute("PRAGMA quick_check").fetchone()
        if not quick_check or quick_check[0] != "ok":
            raise ValueError("compact SQLite PRAGMA quick_check failed")

        embedded_manifest = {
            str(key): str(value)
            for key, value in conn.execute(
                "SELECT manifest_key, manifest_value FROM serving_snapshot_manifest"
            ).fetchall()
        }
        if embedded_manifest.get("schema") != COMPACT_DATABASE_SCHEMA:
            raise ValueError("compact SQLite has an unexpected internal schema")
        codec = embedded_manifest.get("codec") or embedded_manifest.get("posting_codec")
        if codec != COMPACT_POSTING_CODEC:
            raise ValueError("compact SQLite has an unexpected posting codec")

        count_rows = conn.execute(
            """
            SELECT object_name, row_count, count_kind
            FROM serving_snapshot_table_counts
            ORDER BY count_kind, object_name
            """
        ).fetchall()
        count_maps: dict[str, dict[str, int]] = {
            "physical": {},
            "logical": {},
            "servable": {},
        }
        for raw_name, raw_count, raw_kind in count_rows:
            name = str(raw_name)
            kind = str(raw_kind)
            if (
                kind not in count_maps
                or not _SAFE_IDENTIFIER.fullmatch(name)
                or name in count_maps[kind]
                or isinstance(raw_count, bool)
                or not isinstance(raw_count, int)
                or raw_count < 0
            ):
                raise ValueError("compact SQLite count metadata is invalid")
            count_maps[kind][name] = int(raw_count)
        if not count_maps["physical"] or not count_maps["logical"]:
            raise ValueError("compact SQLite count metadata is incomplete")

        objects = {
            str(name): str(object_type)
            for name, object_type in conn.execute(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
        for table_name, expected_count in count_maps["physical"].items():
            if objects.get(table_name) != "table":
                raise ValueError(f"compact SQLite is missing physical object: {table_name}")
            actual_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]
            )
            if actual_count != expected_count:
                raise ValueError(
                    f"compact SQLite physical count mismatch for {table_name}: "
                    f"expected={expected_count}, actual={actual_count}"
                )

        # Servable objects are the public canonical tables/views.  Their
        # counts must be measured from the object itself because a canonical
        # view can expose logical rows that differ from a backing table.
        for object_name, expected_count in count_maps["servable"].items():
            if objects.get(object_name) not in {"table", "view"}:
                raise ValueError(
                    f"compact SQLite is missing servable object: {object_name}"
                )
            actual_count = int(
                conn.execute(f'SELECT COUNT(*) FROM "{object_name}"').fetchone()[0]
            )
            if actual_count != expected_count:
                raise ValueError(
                    f"compact SQLite servable count mismatch for {object_name}: "
                    f"expected={expected_count}, actual={actual_count}"
                )

        logical_queries = {
            "ontology_concept_relations": (
                "SELECT COALESCE(SUM(target_count), 0) FROM ontology_relation_outgoing"
            ),
            "criteria_concept_links_enriched": (
                "SELECT COALESCE(SUM(concept_count), 0) FROM criteria_concept_forward"
            ),
        }
        inverse_queries = {
            "ontology_concept_relations": (
                "SELECT COALESCE(SUM(source_count), 0) FROM ontology_relation_incoming"
            ),
            "criteria_concept_links_enriched": (
                "SELECT COALESCE(SUM(criteria_count), 0) FROM criteria_concept_inverse"
            ),
        }
        if set(count_maps["logical"]) != set(logical_queries):
            raise ValueError("compact SQLite logical count names are unsupported")
        for logical_name, expected_count in count_maps["logical"].items():
            forward_count = int(conn.execute(logical_queries[logical_name]).fetchone()[0])
            inverse_count = int(conn.execute(inverse_queries[logical_name]).fetchone()[0])
            if forward_count != expected_count or inverse_count != expected_count:
                raise ValueError(
                    f"compact SQLite logical count mismatch for {logical_name}: "
                    f"expected={expected_count}, forward={forward_count}, "
                    f"inverse={inverse_count}"
                )

    return count_maps["physical"], count_maps["logical"], count_maps["servable"]


def package_compact_snapshot(
    database_path: Path,
    archive_path: Path,
    manifest_path: Path,
    *, builder_context: BuilderOperationContext | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Validate a raw compact DB and atomically emit the deployable pair."""

    guard = None if dry_run else package_guard(builder_context)
    if guard is not None:
        guard.output(database_path)
        guard.output(archive_path)
        guard.output(manifest_path)
    if len({path.resolve() for path in (database_path, archive_path, manifest_path)}) != 3:
        raise ValueError('Compact database, archive and manifest paths must be distinct.')
    database_path = database_path.resolve()
    if not database_path.is_file():
        raise ValueError(f"compact SQLite does not exist: {database_path}")
    sqlite_bytes = database_path.stat().st_size
    if sqlite_bytes <= 0 or sqlite_bytes >= MAX_SNAPSHOT_BYTES:
        raise ValueError(
            f"compact SQLite must be smaller than {MAX_SNAPSHOT_BYTES} bytes; "
            f"actual={sqlite_bytes}"
        )
    with database_path.open("rb") as source:
        if source.read(len(_SQLITE_HEADER)) != _SQLITE_HEADER:
            raise ValueError("compact SQLite header is invalid")

    physical_counts, logical_counts, servable_counts = _read_compact_metadata(database_path)
    sqlite_sha256 = _sha256(database_path)
    manifest = {
        "schema": COMPACT_MANIFEST_SCHEMA,
        "archive_member": COMPACT_SNAPSHOT_NAME,
        "database_schema": COMPACT_DATABASE_SCHEMA,
        "codec": COMPACT_POSTING_CODEC,
        "sqlite_bytes": sqlite_bytes,
        "sqlite_sha256": sqlite_sha256,
        "physical_counts": physical_counts,
        "logical_counts": logical_counts,
        "servable_counts": servable_counts,
    }

    if dry_run:
        return {'ok': True, 'dry_run': True, 'manifest': manifest,
                'database': str(database_path), 'archive': str(archive_path),
                'manifest_path': str(manifest_path)}

    guard.output(archive_path)
    guard.output(manifest_path)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    archive_temp = archive_path.with_name('.' + archive_path.name + '.' + uuid.uuid4().hex + '.tmp')
    manifest_temp = manifest_path.with_name('.' + manifest_path.name + '.' + uuid.uuid4().hex + '.tmp')
    created = {}
    try:
        member = zipfile.ZipInfo(COMPACT_SNAPSHOT_NAME, date_time=(1980, 1, 1, 0, 0, 0))
        member.compress_type = zipfile.ZIP_DEFLATED
        member.external_attr = 0o600 << 16
        guard.output(archive_temp)
        with archive_temp.open('xb') as archive_stream, zipfile.ZipFile(
            archive_stream, "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
            allowZip64=True,
        ) as archive:
            info = os.fstat(archive_stream.fileno())
            created[archive_temp] = (info.st_dev, info.st_ino)
            with database_path.open("rb") as source, archive.open(
                member, "w", force_zip64=True
            ) as destination:
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    guard.check(database_path, archive_temp)
                    destination.write(chunk)
        guard.output(manifest_temp)
        with manifest_temp.open('x', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            created[manifest_temp] = (info.st_dev, info.st_ino)
            stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        inspect_compact_archive(archive_temp, manifest_temp)
        guard.output(archive_path)
        guard.check(archive_temp, manifest_temp)
        for path, identity in created.items():
            info = path.lstat()
            if (info.st_dev, info.st_ino) != identity:
                from ncs_mcp.builder_authorization import BuilderAuthorizationError
                raise BuilderAuthorizationError('Compact package temporary identity changed.')
        os.replace(archive_temp, archive_path)
        guard.output(manifest_path)
        guard.check(manifest_temp)
        os.replace(manifest_temp, manifest_path)
    finally:
        guard.check()
        for path, identity in created.items():
            if path.exists():
                guard.output(path)
                info = path.lstat()
                if (info.st_dev, info.st_ino) != identity:
                    from ncs_mcp.builder_authorization import BuilderAuthorizationError
                    raise BuilderAuthorizationError('Compact package temporary identity changed.')
                path.unlink()

    return {
        "ok": True,
        "deployment_root": str(CANONICAL_DEPLOY_ROOT.resolve()),
        "source_database": str(database_path),
        "archive_path": str(archive_path.resolve()),
        "manifest_path": str(manifest_path.resolve()),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": _sha256(archive_path),
        **manifest,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
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
    parser.add_argument("--out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.dry_run:
        parser.error('Compact packaging requires the live Windows NCS Data Builder; use --dry-run for inspection.')

    try:
        result = package_compact_snapshot(args.database, args.archive, args.manifest, dry_run=True)
    except (OSError, sqlite3.DatabaseError, ValueError, zipfile.BadZipFile) as exc:
        result = {"ok": False, "error": str(exc)}
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out and not args.dry_run:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
