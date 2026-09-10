"""Safely inspect or launch a local NCS Gold-connected process."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.local_gold_runtime import (  # noqa: E402
    LOCAL_GOLD_CONTAINER,
    LOCAL_GOLD_CONTAINER_ALLOWLIST,
    LOCAL_GOLD_EMBEDDING_DEVICE_ALLOWLIST,
    LOCAL_GOLD_EMBEDDING_MODEL,
    LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST,
    LOCAL_GOLD_TARGET_ALLOWLIST,
    LOCAL_GOLD_TRANSPORT_ALLOWLIST,
    LocalGoldOptions,
    LocalGoldRuntimeError,
    failed_status,
    run_local_gold_target,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Inspect the fixed local Neo4j Gold container and optionally launch "
            "MCP or Builder with an in-memory credential bridge."
        )
    )
    parser.add_argument(
        "--target",
        choices=sorted(LOCAL_GOLD_TARGET_ALLOWLIST),
        default="status",
        help="Default status is read-only and does not launch a child.",
    )
    parser.add_argument(
        "--container",
        choices=sorted(LOCAL_GOLD_CONTAINER_ALLOWLIST),
        default=LOCAL_GOLD_CONTAINER,
    )
    parser.add_argument("--launch", action="store_true")
    parser.add_argument(
        "--transport",
        choices=sorted(LOCAL_GOLD_TRANSPORT_ALLOWLIST),
        default="stdio",
    )
    parser.add_argument("--mcp-port", type=int, default=8000)
    parser.add_argument("--embedding-dimensions", type=int, default=1024)
    parser.add_argument(
        "--embedding-model",
        choices=sorted(LOCAL_GOLD_EMBEDDING_MODEL_ALLOWLIST),
        default=LOCAL_GOLD_EMBEDDING_MODEL,
    )
    parser.add_argument(
        "--embedding-device",
        choices=sorted(LOCAL_GOLD_EMBEDDING_DEVICE_ALLOWLIST),
        default="cpu",
    )
    parser.add_argument("--disable-local-embedding", action="store_true")
    parser.add_argument("--allow-embedding-download", action="store_true")
    parser.add_argument("--database", default=None)
    parser.add_argument("--inspect-timeout", type=float, default=5.0)
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    parser.add_argument("--query-timeout", type=float, default=2.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    options = LocalGoldOptions(
        container_name=args.container,
        embedding_dimensions=args.embedding_dimensions,
        database=args.database,
        inspect_timeout_seconds=args.inspect_timeout,
        connect_timeout_seconds=args.connect_timeout,
        query_timeout_seconds=args.query_timeout,
        local_embedding_enabled=not args.disable_local_embedding,
        local_embedding_model=args.embedding_model,
        local_embedding_allow_download=args.allow_embedding_download,
        local_embedding_device=args.embedding_device,
    )
    try:
        report = run_local_gold_target(
            target=args.target,
            launch=args.launch,
            transport=args.transport,
            mcp_port=args.mcp_port,
            options=options,
        )
    except LocalGoldRuntimeError as exc:
        report = failed_status(target=args.target, code=exc.code)
        stream = sys.stderr if args.launch else sys.stdout
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), file=stream)
        return 1

    # Never write a preamble into an MCP stdio protocol stream.  An explicitly
    # launched child owns stdout/stderr until it exits.
    if args.launch:
        return int(report.get("child_return_code", 1))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
