"""Trace benchmark workloads or compare their SQL statement identities.

Checks SQL identity only; does not check Python line coverage, response
equivalence, latency, or K09 applicability by itself.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any

LIMITATION = (
    "Checks SQL identity only; does not check Python line coverage, response "
    "equivalence, latency, or K09 applicability by itself."
)


def _cases_by_id(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a non-empty list of cases")
    indexed = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise ValueError("Each case must have a non-empty string id")
        if case["id"] in indexed:
            raise ValueError(f"Duplicate case id: {case['id']}")
        if not isinstance(case.get("workload"), str) or not isinstance(case.get("params"), dict):
            raise ValueError(f"Invalid workload or params: {case['id']}")
        indexed[case["id"]] = case
    return indexed


def _report_header(schema: str) -> dict[str, Any]:
    return {
        "schema": schema,
        "sql_identity_only": True,
        "python_line_coverage_checked": False,
        "limitations": LIMITATION,
    }


def trace_cases(source: Path, db: Path, cases: list[dict[str, Any]]) -> dict[str, Any]:
    _cases_by_id(cases)
    source = source.resolve(strict=True)
    db = db.resolve(strict=True)
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source))
    os.environ.update({
        "NCS_DB_PATH": str(db),
        "NCS_MCP_READ_ONLY": "1",
        "NCS_MCP_ENABLE_OPERATOR_TOOLS": "0",
    })
    from ncs_mcp import server
    from ncs_mcp.db import connect

    if not Path(server.__file__).resolve().is_relative_to(source):
        raise ValueError("ncs_mcp is already imported from another source; use a fresh process")
    benchmark_path = Path(__file__).with_name("benchmark_ncs_code_ab.py")
    spec = importlib.util.spec_from_file_location("trace_benchmark_workloads", benchmark_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {benchmark_path}")
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    statements: list[str] | None = None

    @contextmanager
    def open_db():
        conn = connect(db, read_only=True)
        try:
            if statements is not None:
                conn.set_trace_callback(statements.append)
            yield conn
        finally:
            conn.close()

    previous_open_db = server.open_db
    server.open_db = open_db
    records = []
    try:
        for case in cases:
            fn = benchmark.workload_function(server, case["workload"], open_db)
            fn(**case["params"])
            statements = []
            fn(**case["params"])
            records.append({
                "id": case["id"], "workload": case["workload"], "params": case["params"],
                "statement_count": len(statements),
                "sha256": hashlib.sha256("\n".join(statements).encode("utf-8")).hexdigest(),
            })
            statements = None
    finally:
        server.open_db = previous_open_db
    return {
        **_report_header("ncs_query_sql_trace_v1"),
        "source": str(source), "database": str(db),
        "read_only": True, "warmups_per_case": 1, "traced_calls_per_case": 1,
        "trace_scope": "Workload SQL after connection initialization; setup PRAGMAs are excluded.",
        "hash_encoding": "sha256 of UTF-8 newline-joined SQLite trace statements, in execution order",
        "records": records,
    }


def compare_traces(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    for report in (a, b):
        if report.get("schema") != "ncs_query_sql_trace_v1":
            raise ValueError("Expected ncs_query_sql_trace_v1 reports")
    left = _cases_by_id(a["records"])
    right = _cases_by_id(b["records"])
    if left.keys() != right.keys():
        raise ValueError("Trace reports must contain the same case ids")
    records = []
    for case_id, first in left.items():
        second = right[case_id]
        if any(first[key] != second[key] for key in ("workload", "params")):
            raise ValueError(f"Case workload or params differ: {case_id}")
        for record in (first, second):
            count, digest = record.get("statement_count"), record.get("sha256")
            if type(count) is not int or count < 0 or not isinstance(digest, str) or len(digest) != 64:
                raise ValueError(f"Invalid SQL trace count or hash: {case_id}")
            if any(char not in "0123456789abcdef" for char in digest):
                raise ValueError(f"Invalid SQL trace hash: {case_id}")
        records.append({
            "id": case_id, "workload": first["workload"], "params": first["params"],
            "statement_counts": {"a": first["statement_count"], "b": second["statement_count"]},
            "sha256": {"a": first["sha256"], "b": second["sha256"]},
            "sql_identical": first["statement_count"] == second["statement_count"]
            and first["sha256"] == second["sha256"],
        })
    identical = sum(record["sql_identical"] for record in records)
    return {
        **_report_header("ncs_query_sql_trace_comparison_v1"),
        "case_count": len(records), "sql_identical_count": identical,
        "sql_different_count": len(records) - identical, "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--compare", nargs=2, type=Path, metavar=("A", "B"))
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.compare:
        if any((args.source, args.db, args.cases)):
            parser.error("--compare cannot be combined with --source, --db or --cases")
        inputs = args.compare
    else:
        if not all((args.source, args.db, args.cases)):
            parser.error("--source, --db and --cases are required for tracing")
        inputs = [args.db, args.cases]
    if any(args.out.resolve() == path.resolve() for path in inputs):
        parser.error("--out must not overwrite an input file")
    if args.compare:
        report = compare_traces(*(json.loads(path.read_text(encoding="utf-8-sig")) for path in args.compare))
        report["inputs"] = [str(path.resolve()) for path in args.compare]
    else:
        cases = json.loads(args.cases.read_text(encoding="utf-8-sig"))
        report = trace_cases(args.source, args.db, cases)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
