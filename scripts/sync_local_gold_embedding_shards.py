"""Validate or apply one Builder full-embedding shard set to local Neo4j.

The apply path reads credentials from the fixed, loopback-only local Gold
container and keeps them in process memory.  It never writes an env file or
prints credential values.  Dry-run is the default and needs no Neo4j access.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.data_builder import BuilderError, DataBuilder  # noqa: E402
from ncs_mcp.local_gold_runtime import (  # noqa: E402
    LocalGoldRuntimeError,
    build_local_gold_child_environment,
    inspect_local_gold_container,
)
from ncs_mcp.neo4j_loader import Neo4jLoaderSettings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="verified Builder version")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write patches and receipts; default only validates artifacts",
    )
    parser.add_argument("--batch-size", type=int, default=1_000)
    parser.add_argument("--max-retries", type=int, default=2)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = None
        if args.apply:
            container = inspect_local_gold_container()
            overlay = build_local_gold_child_environment(
                container,
                base_environment={},
            )
            settings = Neo4jLoaderSettings.from_env(overlay)
        result = DataBuilder(ROOT).sync_gold_embedding_shards(
            args.version,
            apply=args.apply,
            batch_size=args.batch_size,
            max_retries=args.max_retries,
            settings=settings,
        )
    except LocalGoldRuntimeError as exc:
        sys.stderr.write(
            json.dumps(
                {"ok": False, "error_code": exc.code, "secrets_exposed": False},
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    except BuilderError:
        sys.stderr.write(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "builder_embedding_shard_sync_failed",
                    "secrets_exposed": False,
                },
                sort_keys=True,
            )
            + "\n"
        )
        return 1

    sys.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
