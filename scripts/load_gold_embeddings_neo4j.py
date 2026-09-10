"""Validate or explicitly load a Gold embedding patch artifact into Neo4j.

Dry-run is the default.  ``--apply`` is required for node-property or vector
index writes.  SQLite and the immutable embedding artifact are never mutated.
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

from ncs_mcp.embedding_export import (  # noqa: E402
    EmbeddingPatchArtifactError,
    inspect_gold_embedding_patches,
    iter_gold_embedding_patches,
)
from ncs_mcp.neo4j_loader import (  # noqa: E402
    GoldLoadExecutionError,
    GoldLoadUnavailableError,
    GoldLoadValidationError,
    apply_embedding_patches,
    apply_vector_indexes,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--patches", type=Path, required=True, help="validated Gold embedding patch NDJSON")
    parser.add_argument("--apply", action="store_true", help="explicitly permit Neo4j writes")
    parser.add_argument("--create-indexes", action="store_true", help="validate/create the three fixed cosine indexes")
    parser.add_argument("--batch-size", type=int, default=1_000, help="Neo4j patch batch size (1..25000)")
    parser.add_argument("--max-retries", type=int, default=2, help="bounded idempotent retry count")
    parser.add_argument("--out", type=Path, help="optional atomic JSON load/index report")
    return parser


def _atomic_json(path: Path, payload: dict) -> None:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
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
    try:
        inspection = inspect_gold_embedding_patches(args.patches)
        dimensions = inspection["header"]["dimensions"]
        result = {
            "artifact": inspection,
            "patch_load": apply_embedding_patches(
                iter_gold_embedding_patches(args.patches),
                apply=args.apply,
                batch_size=args.batch_size,
                max_retries=args.max_retries,
            ),
        }
        if args.create_indexes:
            result["vector_indexes"] = apply_vector_indexes(
                dimensions,
                apply=args.apply,
                max_retries=args.max_retries,
            )
        if args.out is not None:
            _atomic_json(args.out, result)
    except (
        OSError,
        ValueError,
        EmbeddingPatchArtifactError,
        GoldLoadValidationError,
        GoldLoadUnavailableError,
        GoldLoadExecutionError,
    ) as exc:
        parser.exit(1, f"error: Gold embedding load failed: {exc}\n")
    sys.stdout.write(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
