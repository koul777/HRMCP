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
    resolve_managed_baseline,
)
from ncs_mcp.api_refresh_builder import validate_refresh_report_path, write_refresh_evidence  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Plan a change-aware NCS ontology refresh; apply through NCS Data Builder."
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
        "--apply", action="store_true", help="Reserved; apply requires NCS Data Builder."
    )
    args = parser.parse_args()
    if args.apply:
        print(json.dumps({"ok": False, "error": "builder_authorization_required"}))
        return 2
    try:
        baseline = args.baseline if args.baseline is not None else resolve_managed_baseline(args.state_dir)
        protected = (args.source, baseline)
        if args.report:
            validate_refresh_report_path(args.report, protected)
        report = build_ontology_refresh(
            args.source,
            baseline_db=args.baseline,
            state_dir=args.state_dir,
            prepared_output=args.output,
            apply=args.apply,
        )
        if args.report:
            write_refresh_evidence(report, args.report, protected_databases=protected)
    except (OSError, ValueError, RefreshBuilderError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2)
        )
        return 2
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
