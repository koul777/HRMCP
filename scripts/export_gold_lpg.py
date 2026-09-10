"""Export the read-only NCS SQLite Gold LPG projection as deterministic JSON.

Without an output option this command is a count-only dry run: it prints the
deterministic readiness manifest without building node/edge lists and creates
no artifact.  Supplying ``--out`` explicitly writes the serving-core
projection only when the preflight allows it.  This command never connects to
Neo4j and never mutates SQLite.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DATABASE = ROOT / "data" / "processed" / "ncs.db"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_lpg import build_gold_lpg_projection  # noqa: E402
from ncs_mcp.gold_readiness import (  # noqa: E402
    SERVING_CORE_PROFILE,
    preflight_gold_projection,
)


class StreamingRequiredError(RuntimeError):
    """Raised when the current list-building exporter is unsafe for the source."""


def _render_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def _atomic_write_text(path: Path, content: str) -> None:
    """Write UTF-8 text through a same-directory temporary and replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _resolved_destination(path: Path) -> Path:
    return path.expanduser().resolve()


def _path_key(path: Path) -> str:
    """Return a resolved path key with the host OS case semantics."""

    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _validate_paths(
    database_path: Path,
    output_path: Path | None,
    manifest_path: Path | None,
) -> tuple[Path, Path | None, Path | None]:
    database = database_path.expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"SQLite database does not exist or is not a file: {database}")

    output = _resolved_destination(output_path) if output_path is not None else None
    manifest = _resolved_destination(manifest_path) if manifest_path is not None else None
    destinations = [path for path in (output, manifest) if path is not None]
    protected_sqlite_paths = {
        _path_key(database),
        *(
            _path_key(Path(f"{database}{suffix}"))
            for suffix in ("-wal", "-shm", "-journal")
        ),
    }
    if any(_path_key(destination) in protected_sqlite_paths for destination in destinations):
        raise ValueError(
            "output paths must not overwrite the source SQLite database or its sidecars"
        )
    if (
        output is not None
        and manifest is not None
        and _path_key(output) == _path_key(manifest)
    ):
        raise ValueError("--out and --manifest-out must name different files")
    for destination in destinations:
        if destination.exists() and destination.is_dir():
            raise ValueError(f"output path is a directory, not a file: {destination}")
    return database, output, manifest


def export_gold_lpg(
    database_path: Path,
    *,
    output_path: Path | None = None,
    manifest_path: Path | None = None,
) -> dict[str, Any]:
    """Preflight first and build only an explicitly requested safe projection."""

    database, output, manifest_output = _validate_paths(
        database_path,
        output_path,
        manifest_path,
    )
    readiness = preflight_gold_projection(database, profile=SERVING_CORE_PROFILE)
    projection: dict[str, Any] | None = None
    if output is not None:
        if not readiness["in_memory_export_allowed"]:
            raise StreamingRequiredError(
                "streaming_required: the serving-core JSON projection exceeds conservative "
                "in-memory limits; no streaming exporter is available, so no artifact "
                "was written"
            )
        projection = build_gold_lpg_projection(
            database,
            profile=SERVING_CORE_PROFILE,
        )

    summary = dict(readiness)
    if projection is not None:
        summary["projection_manifest"] = dict(projection["manifest"])

    # Render before replacing either destination so serialization failures do
    # not leave a partially refreshed artifact pair.
    rendered_projection = _render_json(projection) if output is not None else None
    rendered_manifest = _render_json(summary) if manifest_output is not None else None
    if output is not None and rendered_projection is not None:
        _atomic_write_text(output, rendered_projection)
    if manifest_output is not None and rendered_manifest is not None:
        _atomic_write_text(manifest_output, rendered_manifest)
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"source SQLite database (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="write the full JSON projection only after a passing count-only preflight",
    )
    parser.add_argument(
        "--manifest-out",
        type=Path,
        help="optionally write the deterministic manifest as a separate JSON file",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        summary = export_gold_lpg(
            args.db,
            output_path=args.out,
            manifest_path=args.manifest_out,
        )
    except (OSError, sqlite3.DatabaseError, StreamingRequiredError, TypeError, ValueError) as exc:
        parser.exit(1, f"error: Gold LPG export failed: {exc}\n")
    sys.stdout.write(_render_json(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
