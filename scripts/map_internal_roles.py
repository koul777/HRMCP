"""Create read-only, candidate-only InternalJobRole -> NCSJob mapping packets."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from typing import Any, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DATABASE = ROOT / "data" / "processed" / "ncs.db"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.internal_job_roles import ContractValidationError, validate_internal_job_role  # noqa: E402
from ncs_mcp.internal_role_mapping import map_internal_job_roles  # noqa: E402


def _records(path: Path, *, envelope_key: str) -> list[Mapping[str, Any]]:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")
    if source.suffix.lower() in {".jsonl", ".ndjson"}:
        raw = [json.loads(line) for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    else:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
        raw = payload.get(envelope_key, []) if isinstance(payload, Mapping) else payload
        if isinstance(payload, Mapping) and envelope_key not in payload:
            raw = [payload]
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ValueError(f"{envelope_key} input must contain an object or an array of objects")
    return list(raw)


def _semantic_by_role(path: Path | None) -> dict[str, list[Mapping[str, Any]]]:
    if path is None:
        return {}
    result: dict[str, list[Mapping[str, Any]]] = {}
    for row in _records(path, envelope_key="semantic_candidates"):
        role_gold_id = row.get("role_gold_id")
        if not isinstance(role_gold_id, str) or not role_gold_id.strip():
            raise ValueError("semantic candidate records require role_gold_id")
        result.setdefault(role_gold_id, []).append(row)
    return result


def _atomic_write(path: Path, text: str) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=target.parent, prefix=f".{target.name}.", suffix=".tmp") as handle:
            temporary = Path(handle.name)
            handle.write(text)
            if not text.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="read-only NCS SQLite database")
    parser.add_argument("--roles", type=Path, required=True, help="InternalJobRole JSON or JSONL input")
    parser.add_argument("--out", type=Path, required=True, help="JSON or JSONL candidate packet destination")
    parser.add_argument("--semantic-candidates", type=Path, help="optional recall-only candidate JSON/JSONL")
    parser.add_argument("--limit", type=int, default=5, help="candidates per role (1..25)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        roles = [validate_internal_job_role(item) for item in _records(args.roles, envelope_key="internal_roles")]
        packet = map_internal_job_roles(
            roles, args.db, limit=args.limit, semantic_candidates_by_role=_semantic_by_role(args.semantic_candidates),
        )
        if args.out.suffix.lower() in {".jsonl", ".ndjson"}:
            output = "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in packet["role_results"])
        else:
            output = json.dumps(packet, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        _atomic_write(args.out, output)
    except (OSError, json.JSONDecodeError, sqlite3.DatabaseError, TypeError, ValueError, ContractValidationError) as exc:
        parser.exit(1, f"error: internal role mapping failed: {exc}\n")
    sys.stdout.write(json.dumps({
        "out": str(args.out), "role_count": len(packet["role_results"]),
        "db_writes": False, "neo4j_writes": False,
    }, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
