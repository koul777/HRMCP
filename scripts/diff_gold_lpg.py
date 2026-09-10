"""Create a review-gated incremental plan between two Gold LPG NDJSON exports.

Without ``--out`` this is a read-only dry run: it validates both exports and
prints deterministic operation counts but retains no operation file.  An
``--out`` file still contains no applied deletion; tombstones are plans that
require an explicit operator approval in a future loader.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.gold_incremental import (  # noqa: E402
    GoldIncrementalValidationError,
    plan_gold_lpg_incremental,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True, help="previous canonical Gold NDJSON export")
    parser.add_argument("--current", type=Path, required=True, help="current canonical Gold NDJSON export")
    parser.add_argument("--out", type=Path, help="optional incremental NDJSON plan destination")
    parser.add_argument("--dry-run", action="store_true", help="validate and report only; cannot be combined with --out")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.dry_run and args.out is not None:
        parser.error("--dry-run cannot be combined with --out")
    try:
        manifest = plan_gold_lpg_incremental(args.previous, args.current, None if args.dry_run else args.out)
    except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        parser.exit(1, f"error: Gold incremental plan failed: {exc}\n")
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
