"""CLI for the read-only all-NCS scope collision inventory."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.search.collision_audit import (
    DEFAULT_COLLISION_CAP,
    DEFAULT_HAZARD_CAP,
    DEFAULT_PATHS_PER_COLLISION,
    DEFAULT_SCENARIO_LIMIT,
    DEFAULT_SCOPE_LABEL_CAP,
    SCHEMA,
    write_report,
)


DEFAULT_DB = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_DIR = ROOT / "reports" / "overnight_sessions"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Inventory normalized NCS source-label collisions without database writes."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--top", type=int, default=100)
    parser.add_argument("--collision-cap", type=int, default=DEFAULT_COLLISION_CAP)
    parser.add_argument("--paths-per-collision", type=int, default=DEFAULT_PATHS_PER_COLLISION)
    parser.add_argument("--hazard-cap", type=int, default=DEFAULT_HAZARD_CAP)
    parser.add_argument("--scenario-limit", type=int, default=DEFAULT_SCENARIO_LIMIT)
    parser.add_argument("--scope-label-cap", type=int, default=DEFAULT_SCOPE_LABEL_CAP)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    stamp = date.today().isoformat().replace("-", "")
    json_path = args.out or (DEFAULT_DIR / f"ncs_scope_collision_audit_{stamp}.json")
    markdown_path = args.markdown_out or (DEFAULT_DIR / f"ncs_scope_collision_audit_{stamp}.md")
    report = write_report(
        args.db,
        json_path,
        markdown_path,
        top_n=args.top,
        scenario_limit=args.scenario_limit,
        collision_cap=args.collision_cap,
        paths_per_collision=args.paths_per_collision,
        hazard_cap=args.hazard_cap,
        scope_label_cap=args.scope_label_cap,
    )
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "scope": report["scope"],
                "metrics": report["metrics"],
                "out": str(json_path),
                "markdown_out": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
