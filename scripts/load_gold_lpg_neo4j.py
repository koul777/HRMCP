"""Validate or explicitly apply a streaming Gold LPG NDJSON artifact to Neo4j.

Without ``--apply`` this is a complete offline integrity check and does not
import the Neo4j driver.  ``--apply`` requires only the NCS_MCP_GOLD_* runtime
environment values and writes no SQLite data.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.neo4j_loader import (  # noqa: E402
    GoldLoadExecutionError,
    GoldLoadUnavailableError,
    GoldLoadValidationError,
    load_gold_lpg_ndjson,
    Neo4jLoaderSettings,
    reconcile_gold_lpg,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ndjson", type=Path, required=True, help="Gold serving-core NDJSON artifact")
    parser.add_argument("--apply", action="store_true", help="explicitly permit Neo4j writes")
    parser.add_argument("--checkpoint", type=Path, help="optional atomic resume checkpoint")
    parser.add_argument("--batch-size", type=int, default=1_000, help="Neo4j UNWIND batch size (1..25000)")
    parser.add_argument("--max-retries", type=int, default=2, help="bounded idempotent retry count")
    parser.add_argument("--no-schema", action="store_true", help="do not apply fixed Gold constraints/index DDL during --apply")
    parser.add_argument("--reconcile", action="store_true", help="after --apply, compare Neo4j label/type counts to the artifact")
    parser.add_argument("--out", type=Path, help="optional atomic JSON load/reconciliation report")
    return parser


def _atomic_json(path: Path, payload: dict) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", delete=False, dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.reconcile and not args.apply:
        parser.error("--reconcile requires --apply")
    try:
        result = load_gold_lpg_ndjson(
            args.ndjson,
            apply=args.apply,
            checkpoint_path=args.checkpoint,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            apply_schema=not args.no_schema,
        )
        if args.reconcile:
            result["reconciliation"] = reconcile_gold_lpg(result, settings=Neo4jLoaderSettings.from_env())
        if args.out is not None:
            _atomic_json(args.out, result)
    except (OSError, ValueError, GoldLoadValidationError, GoldLoadUnavailableError, GoldLoadExecutionError) as exc:
        parser.exit(1, f"error: Gold Neo4j load failed: {exc}\n")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
