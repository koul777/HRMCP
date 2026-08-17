from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from scripts.build_qualification_guarded_batch_operator_decision import (
        PROJECT_ROOT,
        audit_decision_packet,
        portable_path,
        read_json,
        sha256_file,
        write_audit_markdown,
    )
except ImportError:  # pragma: no cover - direct script execution from scripts/
    from build_qualification_guarded_batch_operator_decision import (
        PROJECT_ROOT,
        audit_decision_packet,
        portable_path,
        read_json,
        sha256_file,
        write_audit_markdown,
    )


def build_existing_packet_audit(packet_path: Path, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    resolved = packet_path if packet_path.is_absolute() else root / packet_path
    packet = read_json(resolved)
    audit = audit_decision_packet(packet, base_dir=root)
    audit["source_packet"] = portable_path(resolved, root=root)
    audit["source_packet_sha256"] = sha256_file(resolved)
    audit["source_packet_exists_nonempty"] = bool(
        resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
    )
    audit["notes"] = [
        "This audit reads an existing qualification guarded-batch packet.",
        "It does not regenerate the operator CSV and cannot overwrite human-entered operator timing decisions.",
        "It does not authorize qualification API collection.",
    ]
    return audit


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit an existing qualification guarded-batch operator decision packet."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    audit = build_existing_packet_audit(args.packet, root=args.root)
    write_json(args.out, audit)
    if args.markdown_out:
        write_audit_markdown(args.markdown_out, audit)
    print(
        json.dumps(
            {
                "ok": audit.get("ok"),
                "schema": audit.get("schema"),
                "issue_count": audit.get("issue_count"),
                "source_packet": audit.get("source_packet"),
                "out_path": str(args.out),
                "markdown_path": str(args.markdown_out) if args.markdown_out else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if args.strict and audit.get("ok") is not True else 0


if __name__ == "__main__":
    raise SystemExit(main())
