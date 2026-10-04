"""Read-only, all-major source self-retrieval audit (not semantic relevance gold)."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import unicodedata


ROOT = Path(__file__).resolve().parents[1]
PROMPT_TEMPLATES = {
    "literal": "{query}",
    "prefix": "NCS 기준으로 다음 직무를 찾아줘: {query}",
    "evidence": "{query}에 필요한 지식과 기술을 알려줘",
    "organization": "우리 회사에서 수행하는 업무 중 {query}에 대한 수행준거를 찾아줘",
    "polite": "{query}의 능력단위요소와 수행준거를 알려 주세요.",
    "standard": "국가직무능력표준에 따라 {query}에 대해 설명해주세요",
    "quoted": '다음 과업을 검색해 주세요: "{query}"',
}


def validate_output_path(output: Path, db: Path) -> Path:
    """A report-only command must never replace the source DB through an alias."""
    resolved = output.resolve()
    if resolved == db.resolve() or (resolved.exists() and resolved.samefile(db)):
        raise ValueError("The report output must not overwrite the source database.")
    if resolved.suffix.lower() != ".json":
        raise ValueError("The report output must use a .json extension.")
    return resolved


def build_cases(rows, *, kind="name", variant="raw", per_major_limit=0):
    """Group identical names so alternate units with the same name are valid."""
    grouped = {}
    for code, name, major in rows:
        query = str(code if kind == "code" else name or "").strip()
        if not query:
            continue
        case = grouped.setdefault(query, {"query": query, "expected": [], "majors": set()})
        case["expected"].append(str(code))
        case["majors"].add(str(major))
    cases = []
    counts = defaultdict(int)
    # Stable hashing samples across the complete vocabulary, not its first codes.
    for query, case in sorted(grouped.items(), key=lambda pair: hashlib.sha256(pair[0].encode()).hexdigest()):
        if per_major_limit and all(counts[m] >= per_major_limit for m in case["majors"]):
            continue
        for major in case["majors"]:
            counts[major] += 1
        case["majors"] = sorted(case["majors"])
        case["expected"] = sorted(set(case["expected"]))
        case["query"] = unicodedata.normalize("NFD", query) if variant == "nfd" else query
        cases.append(case)
    return cases


def evaluate(cases, search, *, limit=3):
    results = []
    for index, case in enumerate(cases, 1):
        started = time.perf_counter()
        payload = search(case["query"], scope="unit", limit=limit)
        top = [{"id": row["id"], "text": row["text"]} for row in payload.get("results", [])]
        rank = next((i for i, row in enumerate(top, 1) if str(row["id"]) in case["expected"]), None)
        error = payload.get("error") or {}
        results.append({**case, "rank": rank, "match_mode": payload.get("match_mode"),
                        "error_code": error.get("code") if isinstance(error, dict) else "search_error",
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "top": top})
        if index % 500 == 0:
            print(json.dumps({"completed": index, "cases": len(cases)}, ensure_ascii=False), flush=True)
    return results


def with_prompt_templates(cases, templates):
    """Wrap source queries without changing their authoritative identifiers.

    These are synthetic request-framing checks, not human relevance labels or
    independent natural-language holdout questions.
    """
    return [
        {**case, "source_query": case["query"], "prompt_template": template,
         "query": PROMPT_TEMPLATES[template].format(query=case["query"])}
        for case in cases for template in templates
    ]


def aggregate(results):
    def metrics(rows):
        count = len(rows)
        return {"cases": count,
                "hit_at_1": sum(row["rank"] == 1 for row in rows) / count if count else None,
                "hit_at_3": sum(row["rank"] is not None and row["rank"] <= 3 for row in rows) / count if count else None,
                "mrr": sum(1 / row["rank"] if row["rank"] else 0 for row in rows) / count if count else None}
    by_major = defaultdict(list)
    for row in results:
        for major in row["majors"]:
            by_major[major].append(row)
    return {"overall": metrics(results), "by_major": {major: metrics(rows) for major, rows in sorted(by_major.items())}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "src")
    parser.add_argument("--surface", choices=("core", "public"), default="core",
                        help="Public also exercises routing, job-scope inference, and the tool guard")
    parser.add_argument("--kind", choices=("name", "code"), default="name")
    parser.add_argument("--variant", choices=("raw", "nfd"), default="raw")
    parser.add_argument("--per-major-limit", type=int, default=0, help="0 audits every distinct source query")
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--fail-on-miss", action="store_true")
    parser.add_argument("--prompt-template", action="append", choices=(*PROMPT_TEMPLATES, "all"),
                        help="Repeat for synthetic request frames; default is an unwrapped source query")
    args = parser.parse_args()
    if args.per_major_limit < 0 or not 3 <= args.limit <= 100:
        parser.error("per-major-limit must be nonnegative and limit must be 3..100")
    db = args.db.resolve(strict=True)
    try:
        args.out = validate_output_path(args.out, db)
    except ValueError as exc:
        parser.error(str(exc))
    db_before = (db.stat().st_size, db.stat().st_mtime_ns)
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT cu.unit_code, cu.unit_name_raw, c.major_code "
                            "FROM competency_units cu JOIN classifications c USING (classification_id) "
                            "ORDER BY cu.unit_code").fetchall()
    finally:
        conn.close()
    if not rows:
        parser.error("source database has no units")
    cases = build_cases(rows, kind=args.kind, variant=args.variant, per_major_limit=args.per_major_limit)
    templates = (list(PROMPT_TEMPLATES) if "all" in (args.prompt_template or [])
                 else list(dict.fromkeys(args.prompt_template or [])))
    if templates:
        cases = with_prompt_templates(cases, templates)
    source = args.source_root.resolve(strict=True)
    core_path = source / "ncs_mcp/search/core.py"
    core_before = hashlib.sha256(core_path.read_bytes()).hexdigest()
    router_path = source / "ncs_mcp/query_router.py"
    router_before = hashlib.sha256(router_path.read_bytes()).hexdigest()
    sys.path.insert(0, str(source))
    os.environ["NCS_DB_PATH"] = str(db)
    os.environ["NCS_MCP_READ_ONLY"] = "1"
    os.environ["NCS_MCP_ENABLE_OPERATOR_TOOLS"] = "0"
    from ncs_mcp import server
    search = server.ncs_search if args.surface == "public" else server.search_ncs
    results = evaluate(cases, search, limit=args.limit)
    metrics = aggregate(results)
    if templates:
        metrics["by_prompt_template"] = {
            template: aggregate([row for row in results if row["prompt_template"] == template])["overall"]
            for template in templates
        }
    unchanged = (db.stat().st_size, db.stat().st_mtime_ns) == db_before
    runtime_unchanged = (hashlib.sha256(core_path.read_bytes()).hexdigest() == core_before
                         and hashlib.sha256(router_path.read_bytes()).hexdigest() == router_before)
    report = {"schema": "ncs_exact_lookup_audit_v1", "generated_at": datetime.now(UTC).isoformat(),
              "evidence_kind": ("synthetic_source_request_framing_not_semantic_gold" if templates
                                else "source_self_retrieval_not_semantic_gold"), "db_writes": False,
              "human_approval_claim": False, "source": {"db": str(db), "units": len(rows),
              "db_bytes": db_before[0], "db_mtime_ns": db_before[1], "db_unchanged_during_run": unchanged,
              "runtime": str(source), "search_core_sha256": core_before, "query_router_sha256": router_before,
              "runtime_unchanged_during_run": runtime_unchanged},
              "parameters": {"kind": args.kind, "variant": args.variant, "per_major_limit": args.per_major_limit,
                             "limit": args.limit, "surface": args.surface, "prompt_templates": templates},
              **metrics, "cases": results}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), **metrics}, ensure_ascii=False))
    return 1 if not unchanged or not runtime_unchanged or (args.fail_on_miss and metrics["overall"]["hit_at_1"] != 1) else 0


if __name__ == "__main__":
    raise SystemExit(main())
