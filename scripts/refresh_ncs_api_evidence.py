"""CLI for the guarded append-only supplemental NCS API refresh adapter."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ncs_mcp.api_refresh_builder import (  # noqa: E402
    ALLOWED_SOURCES,
    refresh_ncs_api_evidence,
    validate_refresh_report_path,
    write_refresh_evidence,
)


def _default_report_path() -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "reports" / f"ncs_api_refresh_evidence_{stamp}.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan the all-major append-only NCS API refresh; apply through NCS Data Builder."
    )
    parser.add_argument(
        "--db", type=Path, default=ROOT / "data" / "processed" / "ncs.db"
    )
    parser.add_argument(
        "--source", action="append", choices=ALLOWED_SOURCES, dest="sources"
    )
    parser.add_argument(
        "--apply", action="store_true", help="Reserved; apply requires NCS Data Builder."
    )
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "--output",
        type=Path,
        help="New prepared SQLite output. The source DB is never written.",
    )
    output_group.add_argument(
        "--state-dir",
        type=Path,
        help="Directory for a newly named prepared SQLite output.",
    )
    parser.add_argument(
        "--retain-failed-output",
        action="store_true",
        help="Keep an incomplete prepared copy for an operator to inspect; default deletes it.",
    )
    parser.add_argument("--out", type=Path, default=_default_report_path())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        print(json.dumps({"ok": False, "error": "builder_authorization_required"}))
        return 2
    try:
        validate_refresh_report_path(args.out, (args.db,))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    report = refresh_ncs_api_evidence(
        args.db,
        sources=args.sources or ALLOWED_SOURCES,
        apply=args.apply,
        output_path=args.output,
        state_dir=args.state_dir,
        retain_failed_output=args.retain_failed_output,
    )
    try:
        destination = write_refresh_evidence(report, args.out, protected_databases=(args.db,))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 2
    print(destination)
    return (
        0
        if report.get("outcome")
        in {"plan_only", "succeeded_append_only", "completed_with_warnings"}
        else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
