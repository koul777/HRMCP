"""Write a bounded-memory serving-core Gold LPG NDJSON export.

This command only reads the SQLite source.  It never accepts a Neo4j
connection, profile override, or unsafe-force option.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DATABASE = ROOT / "data" / "processed" / "ncs.db"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_stream import MAX_STREAM_BATCH_SIZE, export_gold_lpg_ndjson  # noqa: E402


def _load_records(path: Path | None, *, envelope_key: str) -> list[Mapping[str, Any]]:
    """Read a small role overlay from JSON or JSONL without rewriting it."""

    if path is None:
        return []
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")
    if source.suffix.lower() in {".jsonl", ".ndjson"}:
        records: list[Any] = []
        for line_number, line in enumerate(source.read_text(encoding="utf-8-sig").splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid JSONL record at line {line_number}: {source}"
                ) from exc
    else:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        if isinstance(payload, Mapping) and envelope_key in payload:
            records = payload[envelope_key]
        elif isinstance(payload, Mapping):
            records = [payload]
        else:
            records = payload
    if not isinstance(records, list) or not all(isinstance(item, Mapping) for item in records):
        raise ValueError(f"{envelope_key} input must contain an object or an array of objects")
    return records


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="source SQLite database")
    parser.add_argument("--out", type=Path, required=True, help="required NDJSON destination")
    parser.add_argument(
        "--batch-size", type=int, default=10_000,
        help=f"SQLite fetchmany limit (1..{MAX_STREAM_BATCH_SIZE}; default: 10000)",
    )
    parser.add_argument(
        "--internal-roles",
        type=Path,
        help="optional small JSON/JSONL InternalJobRole source",
    )
    parser.add_argument(
        "--role-alignments",
        type=Path,
        help="optional small JSON/JSONL RoleAlignmentCandidate source",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        roles = _load_records(args.internal_roles, envelope_key="internal_roles")
        alignments = _load_records(args.role_alignments, envelope_key="role_alignments")
        manifest = export_gold_lpg_ndjson(
            args.db,
            args.out,
            batch_size=args.batch_size,
            internal_roles=roles,
            role_alignments=alignments,
        )
    except (OSError, json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        parser.exit(1, f"error: Gold LPG streaming export failed: {exc}\n")
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
