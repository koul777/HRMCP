from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sys
from typing import Any, Callable, Iterable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


SCHEMA = "ncs_search_relevance_review_pack_v1"
DECISION_FIELDS = (
    "relevance_grade",
    "allowed_result_ids",
    "off_scope",
    "reviewer_id",
    "reviewed_at",
    "rationale",
)
CSV_FIELDS = (
    "case_id",
    "query",
    "rank",
    "stable_result_id",
    "result_type",
    *DECISION_FIELDS,
)
MATCH_TIER_BY_MODE = {"phrase": 0, "token_and": 1, "token_or": 2}


def _bounded_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}\u2026"


def _stable_result_id(result: Mapping[str, Any]) -> str:
    result_type = str(result.get("type") or "unknown").strip()
    source_id = str(result.get("id") or "").strip()
    if not source_id:
        raise ValueError(f"search result lacks an id: {result!r}")
    return f"{result_type}:{source_id}"


def _unit_code(result: Mapping[str, Any]) -> str | None:
    if result.get("type") == "unit":
        value = result.get("id")
    else:
        path = result.get("path") if isinstance(result.get("path"), Mapping) else {}
        value = path.get("unit_code")
    return str(value).strip() if value not in (None, "") else None


def _ordered_ids(payload: Mapping[str, Any], limit: int) -> list[str]:
    return [
        _stable_result_id(result)
        for result in list(payload.get("results") or [])[:limit]
    ]


def _scope_from_result(
    result: Mapping[str, Any],
    unit_scopes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    path = result.get("path") if isinstance(result.get("path"), Mapping) else {}
    code = _unit_code(result)
    resolved = unit_scopes.get(code or "", {})
    if result.get("type") == "unit":
        unit_name = result.get("text")
    else:
        unit_name = path.get("unit_name")
    return {
        "major_scope": {
            "major_code": resolved.get("major_code") or path.get("major_code"),
            "major_name": resolved.get("major_name") or path.get("major"),
        },
        "unit_scope": {
            "unit_code": code,
            "unit_name": unit_name or resolved.get("unit_name"),
            "element_id": path.get("element_id"),
            "element_name": path.get("element_name"),
        },
    }


def _review_result(
    result: Mapping[str, Any],
    *,
    rank: int,
    unit_scopes: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    mode = str(result.get("match_mode") or "") or None
    text = result.get("text")
    snippet_source = result.get("api_definition") if result.get("type") == "unit" else text
    matched_tokens = [
        _bounded_text(token, 80) for token in list(result.get("matched_tokens") or [])[:4]
    ]
    match_fields = [
        _bounded_text(field, 80) for field in list(result.get("match_fields") or [])[:6]
    ]
    return {
        "rank": rank,
        "stable_result_id": _stable_result_id(result),
        "result_type": result.get("type"),
        "source_id": result.get("id"),
        "title": _bounded_text(text, 140),
        "snippet": _bounded_text(snippet_source, 320),
        "match": {
            "tier": MATCH_TIER_BY_MODE.get(mode),
            "mode": mode,
            "matched_tokens": matched_tokens,
            "match_fields": match_fields,
        },
        **_scope_from_result(result, unit_scopes),
        "decision_template": {
            "relevance_grade": None,
            "allowed_result_ids": [],
            "off_scope": None,
            "reviewer_id": None,
            "reviewed_at": None,
            "rationale": "",
        },
    }


def _validate_source(source: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates = source.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("source candidate pack must contain non-empty candidates")
    for candidate in candidates:
        if candidate.get("evaluation_status") != "candidate_eval":
            raise ValueError("only candidate_eval rows may enter the review pack")
        if candidate.get("gold_label_present") is not False:
            raise ValueError("gold-labelled rows are not accepted by this candidate pack")
        if candidate.get("human_decision_present") is not False:
            raise ValueError("source already claims a human decision")
    return candidates


def collect_search_observations(
    candidates: Sequence[Mapping[str, Any]],
    *,
    search_fn: Callable[..., Mapping[str, Any]],
    limit: int = 10,
    stability_runs: int = 2,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 10:
        raise ValueError("review packet limit must be between 1 and 10")
    if stability_runs < 1:
        raise ValueError("stability_runs must be at least 1")
    observations: list[dict[str, Any]] = []
    for candidate in candidates:
        query = str(candidate.get("query") or "")
        scope = str(candidate.get("scope_candidate") or "all")
        samples = [
            dict(search_fn(query, scope=scope, limit=limit, offset=0))
            for _ in range(stability_runs)
        ]
        first = samples[0]
        orders = [_ordered_ids(sample, limit) for sample in samples]
        observations.append(
            {
                "candidate": dict(candidate),
                "search": first,
                "stable_order": all(order == orders[0] for order in orders[1:]),
                "observed_orders": orders,
            }
        )
    return observations


def build_review_pack(
    source: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    unit_scopes: Mapping[str, Mapping[str, Any]] | None = None,
    limit: int = 10,
    generated_at: str | None = None,
) -> dict[str, Any]:
    candidates = _validate_source(source)
    by_case = {
        str(observation["candidate"]["case_id"]): observation
        for observation in observations
    }
    if set(by_case) != {str(candidate["case_id"]) for candidate in candidates}:
        raise ValueError("search observations do not match source candidate case ids")
    scopes = unit_scopes or {}
    cases: list[dict[str, Any]] = []
    result_rows = 0
    stable_cases = 0
    for candidate in candidates:
        case_id = str(candidate["case_id"])
        observation = by_case[case_id]
        search = observation["search"]
        results = [
            _review_result(result, rank=rank, unit_scopes=scopes)
            for rank, result in enumerate(list(search.get("results") or [])[:limit], start=1)
        ]
        result_rows += len(results)
        stable_cases += int(bool(observation["stable_order"]))
        cases.append(
            {
                "case_id": case_id,
                "query": candidate.get("query"),
                "domain_group": candidate.get("domain_group"),
                "intent_candidate": candidate.get("intent_candidate"),
                "scope_candidate": candidate.get("scope_candidate"),
                "preferred_result_type_candidates": list(
                    candidate.get("preferred_result_type_candidates") or []
                ),
                "tags": list(candidate.get("tags") or []),
                "challenge_reason": _bounded_text(candidate.get("challenge_reason"), 320),
                "evaluation_status": "candidate_eval",
                "gold": False,
                "search_observation": {
                    "normalized_query": search.get("normalized_query"),
                    "query_tokens": list(search.get("query_tokens") or []),
                    "match_mode": search.get("match_mode"),
                    "match_mode_by_type": dict(search.get("match_mode_by_type") or {}),
                    "counts_by_type": dict(search.get("counts_by_type") or {}),
                    "returned": len(results),
                    "top_k": limit,
                    "rank_order_stable": bool(observation["stable_order"]),
                    "stability_run_count": len(observation["observed_orders"]),
                },
                "results": results,
            }
        )
    return {
        "schema": SCHEMA,
        "version": 1,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "mode": "read_only_candidate_relevance_review",
        "candidate_eval": True,
        "gold": False,
        "approval_claim": False,
        "db_writes": False,
        "source": {
            "schema": source.get("schema"),
            "candidate_count": len(candidates),
        },
        "review_contract": {
            "status": "human_review_required",
            "candidate_eval": True,
            "gold": False,
            "approval_claim": False,
            "db_writes": False,
            "automatic_relevance_labels": False,
            "decision_fields_initially_blank": True,
            "relevance_grade_scale": "0=not relevant, 1=marginal, 2=relevant, 3=highly relevant",
            "allowed_result_ids_format": "pipe-separated stable_result_id values",
            "metrics_after_human_review": ["Recall@K", "MRR", "nDCG", "Precision@K"],
        },
        "summary": {
            "case_count": len(cases),
            "result_row_count": result_rows,
            "top_k": limit,
            "stable_rank_order_case_count": stable_cases,
            "unstable_rank_order_case_count": len(cases) - stable_cases,
        },
        "cases": cases,
    }


def resolve_unit_scopes(
    unit_codes: Iterable[str],
    *,
    open_db: Callable[..., Any],
) -> dict[str, dict[str, Any]]:
    unique = sorted({str(code) for code in unit_codes if code})
    resolved: dict[str, dict[str, Any]] = {}
    with open_db() as conn:
        for start in range(0, len(unique), 400):
            chunk = unique[start : start + 400]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT cu.unit_code, cu.unit_name_raw,
                       c.major_code, c.major_name
                FROM competency_units cu
                LEFT JOIN classifications c
                  ON c.classification_id = cu.classification_id
                WHERE cu.unit_code IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for row in rows:
                resolved[str(row["unit_code"])] = {
                    "unit_name": row["unit_name_raw"],
                    "major_code": row["major_code"],
                    "major_name": row["major_name"],
                }
    return resolved


def decision_csv_text(pack: Mapping[str, Any]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    for case in pack.get("cases") or []:
        for result in case.get("results") or []:
            row = {
                "case_id": case.get("case_id"),
                "query": case.get("query"),
                "rank": result.get("rank"),
                "stable_result_id": result.get("stable_result_id"),
                "result_type": result.get("result_type"),
            }
            row.update({field: "" for field in DECISION_FIELDS})
            writer.writerow(row)
    return buffer.getvalue()


def markdown_text(pack: Mapping[str, Any]) -> str:
    summary = pack["summary"]
    lines = [
        "# NCS Search Relevance Review Pack",
        "",
        "> Candidate evaluation only. This is not a gold set, approval record, or DB write instruction.",
        "",
        f"- Schema: `{pack['schema']}`",
        f"- Cases: {summary['case_count']}",
        f"- Ranked rows: {summary['result_row_count']}",
        f"- Top-K: {summary['top_k']}",
        f"- Stable rank order: {summary['stable_rank_order_case_count']}/{summary['case_count']}",
        "- Human decision fields in the CSV are intentionally blank.",
        "- Fill `relevance_grade` (0-3) per ranked row. Use `allowed_result_ids` only when the reviewer can state the acceptable set for the query.",
        "",
    ]
    for case in pack.get("cases") or []:
        observation = case["search_observation"]
        lines.extend(
            [
                f"## {case['case_id']} - {case['query']}",
                "",
                f"- Scope candidate: `{case['scope_candidate']}`",
                f"- Intent candidate: `{case['intent_candidate']}`",
                f"- Match mode: `{observation.get('match_mode')}`",
                f"- Rank order stable: `{str(observation['rank_order_stable']).lower()}`",
                "",
                "| Rank | Stable ID | Type | Title | Match | Major | Unit |",
                "| ---: | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for result in case.get("results") or []:
            title = str(result.get("title") or "").replace("|", "\\|")
            major = result["major_scope"].get("major_name") or ""
            unit = result["unit_scope"].get("unit_name") or ""
            lines.append(
                f"| {result['rank']} | `{result['stable_result_id']}` | "
                f"{result['result_type']} | {title} | {result['match'].get('mode') or ''} | "
                f"{str(major).replace('|', '\\|')} | {str(unit).replace('|', '\\|')} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a human-only NCS search relevance review packet.")
    parser.add_argument(
        "--source",
        type=Path,
        default=ROOT / "reports" / "ncs_search_eval_candidates_20260830.json",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "ncs_search_relevance_review_pack_20260830.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "reports" / "ncs_search_relevance_review_pack_20260830.md",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=ROOT / "reports" / "ncs_search_relevance_review_decisions_20260830.csv",
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--stability-runs", type=int, default=2)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    from ncs_mcp import server

    source = json.loads(args.source.read_text(encoding="utf-8"))
    candidates = _validate_source(source)
    observations = collect_search_observations(
        candidates,
        search_fn=server.search_ncs,
        limit=args.limit,
        stability_runs=args.stability_runs,
    )
    codes = {
        code
        for observation in observations
        for result in observation["search"].get("results") or []
        for code in [_unit_code(result)]
        if code
    }
    scopes = resolve_unit_scopes(codes, open_db=server.open_db)
    pack = build_review_pack(
        source,
        observations,
        unit_scopes=scopes,
        limit=args.limit,
    )
    _write_text(args.out, json.dumps(pack, ensure_ascii=False, indent=2) + "\n")
    _write_text(args.markdown_out, markdown_text(pack))
    _write_text(args.csv_out, decision_csv_text(pack))
    print(
        json.dumps(
            {
                "ok": True,
                "schema": pack["schema"],
                "case_count": pack["summary"]["case_count"],
                "result_row_count": pack["summary"]["result_row_count"],
                "stable_rank_order_case_count": pack["summary"]["stable_rank_order_case_count"],
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
                "csv_out": str(args.csv_out),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
