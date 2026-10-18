"""Build a private holdout-v3 candidate pool without freezing or measuring it.

Excludes queries already used in regression, development, and holdout v1/v2
fixtures. Writes candidates only; an operator must review expected unit codes
before any freeze. Does not call search evaluation and does not print the
candidate queries into Markdown reports.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
DEFAULT_SOURCES = (
    FIXTURES / "ncs_search_eval_nl.json",
    FIXTURES / "ncs_search_eval_nl_dev.json",
    FIXTURES / "ncs_search_eval_nl_holdout.json",
    FIXTURES / "ncs_search_eval_nl_holdout_v2.json",
)


def _load_queries(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"fixture must be a list: {path}")
    return {str(case.get("query") or "").strip() for case in payload if case.get("query")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed-pool",
        type=Path,
        help="Optional JSON list of draft {query, expected_unit_codes, category} rows",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "ncs_search_holdout_v3_candidates_private.json",
    )
    args = parser.parse_args(argv)

    blocked: set[str] = set()
    for path in DEFAULT_SOURCES:
        if path.exists():
            blocked |= _load_queries(path)

    drafts: list[dict[str, Any]] = []
    rejected: list[dict[str, str]] = []
    if args.seed_pool and args.seed_pool.exists():
        for row in json.loads(args.seed_pool.read_text(encoding="utf-8")):
            query = str(row.get("query") or "").strip()
            if not query:
                continue
            if query in blocked:
                rejected.append({"query": query, "reason": "overlaps_existing_fixture"})
                continue
            drafts.append(
                {
                    "query": query,
                    "expected_unit_codes": list(row.get("expected_unit_codes") or []),
                    "category": row.get("category"),
                    "status": "draft_needs_operator_review",
                }
            )

    report = {
        "schema": "ncs_search_holdout_v3_candidate_pool_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "frozen": False,
        "measured": False,
        "blocked_query_count": len(blocked),
        "draft_count": len(drafts),
        "rejected_count": len(rejected),
        "drafts": drafts,
        "rejected": rejected,
        "contract": {
            "public_markdown_may_include_individual_queries": False,
            "human_review_or_approval_claim": False,
            "next_step": (
                "Operator reviews drafts, verifies unit codes in DB, then freezes "
                "tests/fixtures/ncs_search_eval_nl_holdout_v3.json."
            ),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "blocked_query_count": len(blocked),
                "draft_count": len(drafts),
                "rejected_count": len(rejected),
                "frozen": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
