"""Offline experiment: rescue a semantically strong NCS unit from rank 4+.

Lexical search ranks a unit by where a query token appears, so a unit whose
name literally contains the query words outranks the unit that actually does
the work. This experiment keeps the lexical top two untouched and promotes one
lower-ranked candidate into third place when its embedding similarity beats the
current top three by a margin.

It is research tooling, not a serving path. It never writes to the database,
never changes a review state, and nothing in `src/ncs_mcp` imports it. The
embedding model runs locally; install it with the `gold` optional dependencies.

    python scripts/experiment_semantic_rescue_rerank.py --device cuda \
        --out reports/semantic_rescue_rerank_<DATE>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "dragonkue/multilingual-e5-small-ko-v2"
DEFAULT_FIXTURES = (
    ROOT / "tests" / "fixtures" / "ncs_search_eval_nl_dev.json",
    ROOT / "tests" / "fixtures" / "ncs_search_eval_nl.json",
)
# The frozen holdout is deliberately absent: it is measured once per release
# decision, never inside a tuning loop.


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, action="append", dest="fixtures")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "processed" / "ncs.db")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--margins",
        type=float,
        nargs="+",
        default=[0.0, 0.02, 0.05, 0.08, 0.12],
        help="Similarity margin a rank-4+ candidate must beat to reach third place.",
    )
    parser.add_argument("--out", type=Path)
    return parser.parse_args(argv)


def collect_candidates(fixture: Path, limit: int) -> list[dict[str, Any]]:
    from ncs_mcp import server

    items = []
    for case in json.loads(fixture.read_text(encoding="utf-8")):
        rows = server.search_ncs(query=case["query"], scope="unit", limit=limit)
        items.append(
            {
                "query": case["query"],
                "category": case["category"],
                "expected": list(case["expected_unit_codes"]),
                "candidates": [
                    {
                        "id": row["id"],
                        "text": row["text"],
                        "definition": row.get("api_definition") or "",
                    }
                    for row in rows.get("results", [])
                ],
            }
        )
    return items


def attach_similarities(items: list[dict[str, Any]], model: Any) -> None:
    for item in items:
        candidates = item["candidates"]
        if not candidates:
            item["similarities"] = []
            continue
        query_vector = model.encode(
            [f"query: {item['query']}"], normalize_embeddings=True
        )
        passage_vectors = model.encode(
            [f"passage: {c['text']}. {c['definition']}" for c in candidates],
            normalize_embeddings=True,
            batch_size=32,
        )
        item["similarities"] = (passage_vectors @ query_vector.T).ravel().tolist()


def rescue_order(item: dict[str, Any], margin: float) -> list[int]:
    """Lexical order, except one strong candidate may take third place."""
    count = len(item["candidates"])
    if count <= 3:
        return list(range(count))
    similarities = item["similarities"]
    tail = range(3, count)
    best = max(tail, key=lambda index: similarities[index])
    if similarities[best] > max(similarities[:3]) + margin:
        return [0, 1, best] + [i for i in range(count) if i not in (0, 1, best)]
    return list(range(count))


def measure(items: list[dict[str, Any]], margin: float | None) -> dict[str, Any]:
    hit_at_1 = hit_at_3 = 0
    for item in items:
        order = (
            list(range(len(item["candidates"])))
            if margin is None
            else rescue_order(item, margin)
        )
        ids = [item["candidates"][index]["id"] for index in order]
        expected = set(item["expected"])
        hit_at_1 += bool(ids) and ids[0] in expected
        hit_at_3 += any(code in expected for code in ids[:3])
    total = len(items) or 1
    return {
        "case_count": len(items),
        "hit_at_1": round(hit_at_1 / total, 4),
        "hit_at_3": round(hit_at_3 / total, 4),
    }


def changed_cases(items: list[dict[str, Any]], margin: float) -> list[dict[str, str]]:
    changes = []
    for item in items:
        expected = set(item["expected"])
        before = [c["id"] for c in item["candidates"][:3]]
        after = [
            item["candidates"][index]["id"] for index in rescue_order(item, margin)[:3]
        ]
        was, now = any(c in expected for c in before), any(c in expected for c in after)
        if was != now:
            changes.append(
                {
                    "outcome": "improved" if now else "regressed",
                    "category": item["category"],
                    "query": item["query"],
                }
            )
    return changes


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    os.environ.setdefault("NCS_DB_PATH", str(args.db))
    sys.path.insert(0, str(ROOT / "src"))
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(args.model, device=args.device)
    fixtures = args.fixtures or list(DEFAULT_FIXTURES)
    report: dict[str, Any] = {
        "schema": "ncs_semantic_rescue_rerank_experiment_v1",
        "kind": "offline_experiment_not_a_serving_path",
        "model": args.model,
        "result_limit": args.limit,
        "database_writes": False,
        "status_updates": False,
        "fixtures": {},
    }
    for fixture in fixtures:
        items = collect_candidates(Path(fixture), args.limit)
        attach_similarities(items, model)
        entry = {
            "lexical_only": measure(items, None),
            "by_margin": {
                str(margin): measure(items, margin) for margin in args.margins
            },
            "changed_cases_by_margin": {
                str(margin): changed_cases(items, margin) for margin in args.margins
            },
        }
        report["fixtures"][str(Path(fixture).as_posix())] = entry
        print(f"== {fixture}")
        print(f"   lexical only: {entry['lexical_only']}")
        for margin in args.margins:
            print(f"   margin {margin}: {entry['by_margin'][str(margin)]}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
