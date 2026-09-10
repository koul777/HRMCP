"""Export bounded, provider-neutral Gold embedding patches from SQLite.

The command reads only the local SQLite source and writes one atomic NDJSON
artifact.  It defaults to a local-only SentenceTransformer model load; pass
``--allow-download`` only when an operator explicitly permits model download.
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
DEFAULT_DATABASE = ROOT / "data" / "processed" / "ncs.db"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.embedding_batches import (  # noqa: E402
    DEFAULT_BATCH_SIZE,
    DEFAULT_FETCH_SIZE,
    DEFAULT_MAX_TEXT_CHARS,
    MAX_BATCH_SIZE,
    MAX_FETCH_SIZE,
    MAX_TEXT_CHARS,
    SUPPORTED_ENTITY_TYPES,
)
from ncs_mcp.embedding_export import (  # noqa: E402
    EmbeddingPatchExportError,
    export_gold_embedding_patches,
)
from ncs_mcp.embeddings import EmbeddingError  # noqa: E402
from ncs_mcp.local_embeddings import (  # noqa: E402
    LocalEmbeddingUnavailableError,
    SentenceTransformerEmbeddingProvider,
)


DEFAULT_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DATABASE, help="read-only SQLite source")
    parser.add_argument("--out", type=Path, required=True, help="required NDJSON patch artifact")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="local SentenceTransformer model name or cache path")
    parser.add_argument("--dimensions", type=int, help="optional expected embedding dimension")
    parser.add_argument("--device", help="optional SentenceTransformer device")
    parser.add_argument(
        "--allow-download",
        action="store_true",
        help="explicitly permit a missing model to download; default is local files only",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"embedding batch size (1..{MAX_BATCH_SIZE})")
    parser.add_argument("--fetch-size", type=int, default=DEFAULT_FETCH_SIZE, help=f"SQLite fetch size (1..{MAX_FETCH_SIZE})")
    parser.add_argument("--max-text-chars", type=int, default=DEFAULT_MAX_TEXT_CHARS, help=f"maximum semantic input length (1..{MAX_TEXT_CHARS})")
    parser.add_argument("--max-records", type=int, help="optional bounded smoke-export record cap")
    parser.add_argument(
        "--entity-type",
        action="append",
        choices=SUPPORTED_ENTITY_TYPES,
        dest="entity_types",
        help="repeat to restrict the export; default exports every supported type",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    provider = SentenceTransformerEmbeddingProvider(
        args.model,
        dimensions=args.dimensions,
        device=args.device,
        local_files_only=not args.allow_download,
    )
    try:
        manifest = export_gold_embedding_patches(
            args.db,
            args.out,
            provider,
            entity_types=tuple(args.entity_types or SUPPORTED_ENTITY_TYPES),
            batch_size=args.batch_size,
            fetch_size=args.fetch_size,
            max_text_chars=args.max_text_chars,
            max_records=args.max_records,
        )
    except (EmbeddingPatchExportError, EmbeddingError, LocalEmbeddingUnavailableError, OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
        parser.exit(1, f"error: Gold embedding export failed: {exc}\n")
    sys.stdout.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
