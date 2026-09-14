"""Execute source-derived off-path scope hazards as a read-only regression gate.

The collision inventory intentionally produces *candidate* hazards rather than
turning them into aliases or deny-lists.  This gate executes a bounded,
stratified sample of those candidates through the public ``ncs_search``
boundary and verifies that an explicit classification filter excludes the
off-path row.  It never reads holdout data and never writes the database.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA = "ncs_scope_hazard_execution_gate_v1"
LEVELS = ("major", "middle", "small", "sub")
DEFAULT_LIMIT = 120
MAX_FAILURES = 20


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _result_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []
    for key in ("results", "classifications"):
        value = result.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, Mapping))
    data = result.get("data")
    if isinstance(data, Mapping):
        rows.extend(_result_rows(data))
    return rows


def _error_code(result: Mapping[str, Any]) -> str | None:
    error = result.get("error")
    if isinstance(error, Mapping):
        return _text(error.get("code")) or None
    return _text(error) or None


def _path_key(row: Mapping[str, Any]) -> str:
    path = row.get("path")
    if not isinstance(path, Mapping):
        path = row
    values = [_text(path.get(f"{level}_code")) for level in LEVELS]
    while values and not values[-1]:
        values.pop()
    return "/".join(values)


def _scope_key(scope_filter: Mapping[str, Any]) -> str:
    values = [_text(scope_filter.get(f"{level}_code")) for level in LEVELS]
    while values and not values[-1]:
        values.pop()
    return "/".join(values)


def _has_hard_scope(result: Mapping[str, Any]) -> bool:
    if result.get("classification_filter_applied") is True:
        return True
    value = result.get("classification_filter")
    if isinstance(value, Mapping) and any(_text(item) for item in value.values()):
        return True
    data = result.get("data")
    return isinstance(data, Mapping) and _has_hard_scope(data)


def _stratified_sample(items: Iterable[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    materialized = [dict(item) for item in items]
    materialized.sort(key=lambda item: (_text(item.get("hazard_kind")), _text(item.get("scenario_id"))))
    if limit <= 0 or not materialized:
        return []
    kinds = sorted({_text(item.get("hazard_kind")) for item in materialized})
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    # Keep at least one representative of each hazard kind, then round-robin
    # to avoid a large prefix stratum starving exact/internal candidates.
    for kind in kinds:
        for item in materialized:
            if _text(item.get("hazard_kind")) == kind:
                key = _text(item.get("scenario_id"))
                if key not in used:
                    selected.append(item)
                    used.add(key)
                break
    buckets = {
        kind: [item for item in materialized if _text(item.get("hazard_kind")) == kind]
        for kind in kinds
    }
    indices = {kind: 0 for kind in kinds}
    while len(selected) < limit:
        progressed = False
        for kind in kinds:
            bucket = buckets[kind]
            while indices[kind] < len(bucket):
                item = bucket[indices[kind]]
                indices[kind] += 1
                key = _text(item.get("scenario_id"))
                if key in used:
                    continue
                selected.append(item)
                used.add(key)
                progressed = True
                break
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected[:limit]


def _db_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def run_gate(
    db_path: Path | str,
    inventory_path: Path | str,
    *,
    scenario_limit: int = DEFAULT_LIMIT,
    scope: str = "all",
) -> dict[str, Any]:
    db = Path(db_path).resolve()
    inventory = Path(inventory_path).resolve()
    source = json.loads(inventory.read_text(encoding="utf-8"))
    candidates = source.get("scope_hazard_scenarios") or []
    scenarios = _stratified_sample(candidates, scenario_limit)
    before = _db_signature(db)
    os.environ["NCS_DB_PATH"] = str(db)
    from ncs_mcp.server import ncs_search

    calls: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latencies: list[float] = []
    for scenario in scenarios:
        query = _text(scenario.get("query"))
        scope_filter = scenario.get("scope_filter") or {}
        off_path = scenario.get("off_path_leaf") or {}
        expected_scope = _scope_key(scope_filter)
        off_path_key = _text(off_path.get("path_key"))
        started = time.perf_counter()
        try:
            result = ncs_search(
                query=query,
                scope=scope,
                limit=20,
                classification_filter=scope_filter,
            )
            if not isinstance(result, Mapping):
                result = {"error": "invalid_result"}
        except Exception as exc:  # bounded, report-only evidence
            result = {"error": {"code": "exception", "message": str(exc)[:200]}}
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        latencies.append(elapsed_ms)
        rows = _result_rows(result)
        paths = [_path_key(row) for row in rows]
        error_code = _error_code(result)
        off_path_present = bool(off_path_key and off_path_key in paths)
        in_scope = all(
            not _path_key(row) or _path_key(row) == expected_scope or _path_key(row).startswith(expected_scope + "/")
            for row in rows
        )
        passed = bool(
            error_code in {None, "NOT_FOUND"}
            and _has_hard_scope(result)
            and in_scope
            and not off_path_present
        )
        item = {
            "scenario_id": scenario.get("scenario_id"),
            "hazard_kind": scenario.get("hazard_kind"),
            "query": query,
            "scope_key": expected_scope,
            "off_path_key": off_path_key,
            "returned_count": len(rows),
            "error_code": error_code,
            "hard_scope_observed": _has_hard_scope(result),
            "off_path_present": off_path_present,
            "all_rows_in_scope": in_scope,
            "latency_ms": elapsed_ms,
            "passed": passed,
        }
        calls.append(item)
        if not passed and len(failures) < MAX_FAILURES:
            failures.append(item)

    after = _db_signature(db)
    db_mutation = before != after
    kind_counts = Counter(_text(item.get("hazard_kind")) for item in calls)
    failure_count = sum(1 for item in calls if not item["passed"])
    return {
        "schema": SCHEMA,
        "gate": "source_derived_off_path_scope_execution",
        "ok": bool(calls) and failure_count == 0 and not db_mutation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db),
        "inventory_path": str(inventory),
        "source": "official_ncs_source_tables",
        "source_derived": True,
        "non_holdout_regression": True,
        "holdout_inspected": False,
        "db_mutation": db_mutation,
        "aliases_generated": False,
        "human_review_statuses_written": False,
        "scenario_limit": scenario_limit,
        "summary": {
            "candidate_count": len(candidates),
            "executed_count": len(calls),
            "unexpected_failure_count": failure_count,
            "hazard_kind_counts": dict(kind_counts),
            "latency_ms": {
                "max": round(max(latencies), 3) if latencies else None,
                "p50": round(sorted(latencies)[len(latencies) // 2], 3) if latencies else None,
            },
        },
        "unexpected_failures": failures,
        "execution": calls,
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# NCS Scope Hazard Execution Gate",
        "",
        f"- Status: **{'PASS' if report.get('ok') else 'FAIL'}**",
        f"- Source-derived/non-holdout: `{report.get('source_derived')}`/`{report.get('non_holdout_regression')}`",
        f"- holdout_inspected: `{report.get('holdout_inspected')}`; db_mutation: `{report.get('db_mutation')}`",
        f"- Candidates: {summary.get('candidate_count')}; executed: {summary.get('executed_count')}; unexpected failures: {summary.get('unexpected_failure_count')}",
        f"- Hazard strata: `{summary.get('hazard_kind_counts')}`",
        "",
        "An explicit source-backed classification filter must keep every returned row inside the requested path and exclude the off-path candidate. This gate does not create aliases, deny-lists, or review decisions.",
        "",
    ]
    failures = report.get("unexpected_failures") or []
    if failures:
        lines.extend(["## Bounded failures", ""])
        for item in failures:
            lines.append(
                f"- `{item.get('scenario_id')}` ({item.get('hazard_kind')}): "
                f"scope={item.get('scope_key')}, off_path={item.get('off_path_key')}, "
                f"error={item.get('error_code')}, returned={item.get('returned_count')}"
            )
        lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--scenario-limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--scope", choices=("unit", "element", "criteria", "ksa", "all"), default="all")
    parser.add_argument("--json-out", required=True)
    parser.add_argument("--markdown-out", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_gate(args.db, args.inventory, scenario_limit=max(0, args.scenario_limit), scope=args.scope)
    except Exception as exc:
        report = {
            "schema": SCHEMA,
            "gate": "source_derived_off_path_scope_execution",
            "ok": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "source_derived": True,
            "non_holdout_regression": True,
            "holdout_inspected": False,
            "db_mutation": False,
            "aliases_generated": False,
            "human_review_statuses_written": False,
            "summary": {"unexpected_failure_count": 1},
            "unexpected_failures": [{"failure_reason": "gate_exception", "message": str(exc)[:200]}],
        }
    json_path = Path(args.json_out)
    markdown_path = Path(args.markdown_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "json": str(json_path), "markdown": str(markdown_path)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
