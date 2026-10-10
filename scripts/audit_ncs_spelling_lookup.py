"""All-major synthetic single-edit lookup audit; not semantic relevance gold."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from datetime import UTC, datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from audit_ncs_exact_lookup import aggregate, evaluate, validate_output_path


def build_typo_cases(rows, per_major_limit=5):
    grouped = {}
    for code, name, major in rows:
        key = "".join(str(name or "").split())
        if not 4 <= len(key) <= 39 or not all("가" <= char <= "힣" for char in key):
            continue
        case = grouped.setdefault(key, {"query": str(name), "expected": set(), "majors": set()})
        case["expected"].add(str(code))
        case["majors"].add(str(major))
    by_major = sorted({major for case in grouped.values() for major in case["majors"]})
    cases = []
    seen = set()
    for major in by_major:
        pool = sorted(
            (key for key, case in grouped.items() if major in case["majors"]),
            key=lambda key: hashlib.sha256((major + ":" + key).encode()).hexdigest(),
        )
        selected = 0
        for key in pool:
            index = len(key) // 2
            family = (len(cases) % 4)
            if family == 0:
                typo = key[:index] + ("힣" if key[index] != "힣" else "가") + key[index + 1:]
            elif family == 1:
                typo = key[:index] + key[index + 1:]
            elif family == 2:
                typo = key[:index] + key[index] + key[index:]
            else:
                typo = key[:index - 1] + key[index] + key[index - 1] + key[index + 1:]
            if typo == key or typo in grouped or typo in seen:
                continue
            case = grouped[key]
            cases.append({
                "query": typo, "source_query": case["query"],
                "expected": sorted(case["expected"]), "majors": [major],
                "edit_family": ("substitution", "deletion", "insertion", "transposition")[family],
            })
            seen.add(typo)
            selected += 1
            if selected >= per_major_limit:
                break
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT / "src")
    parser.add_argument("--per-major-limit", type=int, default=5)
    args = parser.parse_args()
    if args.per_major_limit <= 0:
        parser.error("--per-major-limit must be positive")
    db = args.db.resolve()
    output = validate_output_path(args.out, db)
    before = (db.stat().st_size, db.stat().st_mtime_ns)
    with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) as conn:
        rows = conn.execute(
            "SELECT cu.unit_code, cu.unit_name_raw, c.major_code FROM competency_units cu "
            "JOIN classifications c ON c.classification_id = cu.classification_id"
        ).fetchall()
    cases = build_typo_cases(rows, args.per_major_limit)
    os.environ["NCS_DB_PATH"] = str(db)
    os.environ["NCS_MCP_READ_ONLY"] = "true"
    os.environ["NCS_MCP_OPERATOR_TOOLS"] = "false"
    sys.path.insert(0, str(args.source_root.resolve()))
    from ncs_mcp import server
    results = evaluate(cases, server.search_ncs, limit=3)
    identity = hashlib.sha256()
    for file in sorted((args.source_root / "ncs_mcp").rglob("*.py")):
        identity.update(str(file.relative_to(args.source_root)).encode())
        identity.update(file.read_bytes())
    unchanged = before == (db.stat().st_size, db.stat().st_mtime_ns)
    report = {
        "schema": "ncs_synthetic_spelling_lookup_audit_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_sha256": identity.hexdigest(), "db_path": str(db),
        "source_db_unchanged": unchanged,
        "interpretation": "Synthetic source self-retrieval only; no human relevance or approval claim.",
        "metrics": aggregate(results), "cases": results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"source_db_unchanged": unchanged, **report["metrics"]["overall"]}))
    return 0 if unchanged and results else 1


if __name__ == "__main__":
    raise SystemExit(main())
