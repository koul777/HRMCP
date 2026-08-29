from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.ontology_refresh_builder import (  # noqa: E402
    RefreshBuilderError,
    build_ontology_refresh,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan or safely prepare a change-aware NCS ontology refresh."
    )
    parser.add_argument(
        "source", type=Path, help="candidate ncs.db (the only required input)"
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument(
        "--state-dir", type=Path, default=ROOT / ".state" / "ncs-ontology-refresh"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--apply", action="store_true", help="copy and prepare an output DB"
    )
    args = parser.parse_args()
    try:
        report = build_ontology_refresh(
            args.source,
            baseline_db=args.baseline,
            state_dir=args.state_dir,
            prepared_output=args.output,
            apply=args.apply,
        )
    except (OSError, ValueError, RefreshBuilderError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
        )
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
