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
    PROMOTION_REPORT_SCHEMA,
    RefreshReleaseStateError,
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

    # This legacy CLI cannot mint a Builder capability. Mutation now belongs
    # exclusively to a guarded DataBuilder operation; keep a clear,
    # machine-readable retirement response and optional report write.
    report = {
        "schema": PROMOTION_REPORT_SCHEMA,
        "ok": False,
        "status": "blocked",
        "state_dir": str(args.state_dir.expanduser().resolve(strict=False)),
        "blockers": [
            {
                "code": "builder_authorization_required",
                "message": "Baseline promotion requires a live DataBuilder operation.",
            }
        ],
        "inputs": {
            "refresh_report": {
                "path": str(args.refresh_report.expanduser().resolve(strict=False))
            },
            "publish_report": {
                "path": str(args.publish_report.expanduser().resolve(strict=False))
            },
            "remote_verification": {
                "path": str(args.remote_verification.expanduser().resolve(strict=False))
            },
            **(
                {
                    "staged_verification": {
                        "path": str(
                            args.staged_verification.expanduser().resolve(strict=False)
                        )
                    }
                }
                if args.staged_verification is not None
                else {}
            ),
        },
        "publisher_source": None,
        "integrity": None,
        "promoted_baseline": None,
        "lineage": None,
        "pointer": None,
        "safety": {
            "source_database_mutated": False,
            "api_calls": False,
            "deployment_performed": False,
            "publication_performed": False,
            "review_status_writes": False,
            "automatic_deletion": False,
        },
    }
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
