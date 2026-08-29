from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.refresh_release_state import (  # noqa: E402
    RefreshReleaseStateError,
    promote_refresh_baseline,
    write_promotion_report,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Promote an exact ontology refresh publisher source only after "
            "successful publication and remote MCP verification evidence."
        )
    )
    parser.add_argument("--refresh-report", type=Path, required=True)
    parser.add_argument("--publish-report", type=Path, required=True)
    parser.add_argument(
        "--staged-verification",
        type=Path,
        help="optional exact staged-deployment MCP verification JSON",
    )
    parser.add_argument("--remote-verification", type=Path, required=True)
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=ROOT / ".state" / "ncs-ontology-refresh",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    report = promote_refresh_baseline(
        refresh_report_path=args.refresh_report,
        publish_report_path=args.publish_report,
        staged_verification_path=args.staged_verification,
        remote_verification_path=args.remote_verification,
        state_dir=args.state_dir,
    )
    if args.out:
        try:
            write_promotion_report(args.out, report)
        except (OSError, RefreshReleaseStateError) as exc:
            print(
                json.dumps(
                    {**report, "report_write_error": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
