"""Read-only regression gate for direct NCS scope execution.

The gate samples source-backed labels from every major classification and
executes the public ``ncs_search`` boundary in three forms:

* ``<label> 직무에 필요한 역량`` (scope is extracted from the query),
* ``<label>`` with ``job_scope=<label>``, and
* bare ``<label>`` (which must not acquire a hard scope).

It intentionally does not use holdout data, aliases, review status, or any
write-capable API.  The search function is injectable to keep tests entirely
synthetic; the CLI imports the serving facade only after selecting the
read-only DB path.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


LEVELS = ("major", "middle", "small", "sub")
PATH_FIELDS = tuple(
    field
    for level in LEVELS
    for field in (f"{level}_code", f"{level}_name")
)
DEFAULT_EXPECTED_MAJOR_COUNT = 24
MAX_FAILURE_EXAMPLES = 10
SCHEMA = "ncs_scope_execution_gate_v1"

SearchFn = Callable[..., Mapping[str, Any]]


def _text(value: Any) -> str:
    return " ".join(str(value or "").split()).strip()


def _key(row: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(_text(row.get(f"{level}_code")) for level in LEVELS)


def _prefix(row: Mapping[str, Any], depth: int) -> tuple[str, ...]:
    return tuple(_text(row.get(f"{level}_code")) for level in LEVELS[: depth + 1])


def _same_branch(ancestor: tuple[str, ...], descendant: tuple[str, ...]) -> bool:
    return len(ancestor) < len(descendant) and ancestor == descendant[: len(ancestor)]


def _canonical_label_candidates(
    rows: Iterable[Mapping[str, Any]], label: str
) -> list[dict[str, Any]]:
    """Return deepest candidates after collapsing same-branch ancestors."""
    normalized = _text(label).casefold()
    candidates: dict[tuple[int, tuple[str, ...]], dict[str, Any]] = {}
    for row in rows:
        for depth, level in enumerate(LEVELS):
            if _text(row.get(f"{level}_name")).casefold() != normalized:
                continue
            codes = _prefix(row, depth)
            if not all(codes):
                continue
            names = tuple(_text(row.get(f"{item}_name")) for item in LEVELS[: depth + 1])
            candidates[(depth, codes)] = {
                "depth": depth,
                "codes": codes,
                "names": names,
                "source_level": level,
            }
    ordered = sorted(
        candidates.values(),
        key=lambda item: (-int(item["depth"]), tuple(item["codes"])),
    )
    kept: list[dict[str, Any]] = []
    for candidate in ordered:
        if any(
            _same_branch(tuple(candidate["codes"]), tuple(existing["codes"]))
            for existing in kept
        ):
            continue
        kept.append(candidate)
    return kept


def sample_scope_cases(
    rows: Iterable[Mapping[str, Any]],
    *,
    expected_major_count: int = DEFAULT_EXPECTED_MAJOR_COUNT,
) -> dict[str, Any]:
    """Build deterministic, source-derived scope samples for every major.

    One label is selected at each available hierarchy level for each major.
    Ambiguity is computed globally from exact source labels, so a duplicate
    label on incompatible branches is expected to fail closed.
    """
    materialized = [dict(row) for row in rows]
    materialized.sort(key=lambda row: (_key(row), _text(row.get("classification_id"))))
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in materialized:
        major = _text(row.get("major_code"))
        if major:
            grouped.setdefault(major, []).append(row)

    samples: list[dict[str, Any]] = []
    for major_code in sorted(grouped):
        major_rows = grouped[major_code]
        for depth, level in enumerate(LEVELS):
            selected: dict[str, Any] | None = None
            seen_labels: set[str] = set()
            for row in major_rows:
                label = _text(row.get(f"{level}_name"))
                if not label or label.casefold() in seen_labels:
                    continue
                seen_labels.add(label.casefold())
                selected = row
                break
            if selected is None:
                continue
            label = _text(selected.get(f"{level}_name"))
            candidates = _canonical_label_candidates(materialized, label)
            if not candidates:
                continue
            canonical = candidates[0]
            samples.append(
                {
                    "major_code": major_code,
                    "major_name": _text(selected.get("major_name")),
                    "requested_level": level,
                    "label": label,
                    "canonical_depth": int(canonical["depth"]),
                    "canonical_codes": dict(
                        zip(LEVELS[: int(canonical["depth"]) + 1], canonical["codes"])
                    ),
                    "canonical_names": dict(
                        zip(LEVELS[: int(canonical["depth"]) + 1], canonical["names"])
                    ),
                    "canonical_source_level": canonical["source_level"],
                    "candidate_count": len(candidates),
                    "ambiguous": len(candidates) > 1,
                    "candidate_paths": [
                        {"codes": item["codes"], "names": item["names"]}
                        for item in candidates[:3]
                    ],
                }
            )
    covered = sorted(grouped)
    return {
        "samples": samples,
        "major_codes": covered,
        "major_count": len(covered),
        "expected_major_count": expected_major_count,
        "coverage_complete": len(covered) == expected_major_count,
    }


def load_classification_rows(db_path: Path | str) -> list[dict[str, Any]]:
    """Read only classification paths from SQLite using ``mode=ro``."""
    path = Path(db_path).resolve()
    uri = path.as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            """
            SELECT classification_id,
                   major_code, major_name,
                   middle_code, middle_name,
                   small_code, small_name,
                   sub_code, sub_name
            FROM classifications
            WHERE major_code IS NOT NULL AND TRIM(major_code) <> ''
            ORDER BY major_code, middle_code, small_code, sub_code, classification_id
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _error_code(result: Mapping[str, Any]) -> str | None:
    error = result.get("error")
    if isinstance(error, Mapping):
        value = error.get("code")
        return _text(value) or None
    if error:
        return _text(error)
    return None


def _result_rows(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = []

    def visit(value: Any, *, root: bool = False) -> None:
        if isinstance(value, Mapping):
            for key in ("results", "classifications"):
                child = value.get(key)
                if isinstance(child, list):
                    rows.extend(item for item in child if isinstance(item, Mapping))
            data = value.get("data")
            if isinstance(data, Mapping):
                visit(data)

    visit(result, root=True)
    return rows


def _row_path(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    path = row.get("path")
    if isinstance(path, Mapping):
        return path
    if any(field in row for field in PATH_FIELDS):
        return row
    return None


def _path_contains(row: Mapping[str, Any], sample: Mapping[str, Any]) -> bool:
    path = _row_path(row)
    if path is None:
        return False
    codes = sample.get("canonical_codes") or {}
    names = sample.get("canonical_names") or {}
    for level in LEVELS:
        if level not in codes:
            continue
        expected_code = _text(codes.get(level)).casefold()
        actual_code = _text(path.get(f"{level}_code")).casefold()
        if actual_code:
            if actual_code != expected_code:
                return False
            continue
        expected_name = _text(names.get(level)).casefold()
        actual_name = _text(path.get(f"{level}_name")).casefold()
        if actual_name != expected_name:
            return False
    return True


def _has_hard_scope(result: Mapping[str, Any]) -> bool:
    value = result.get("classification_filter_applied")
    if value is True:
        return True
    filt = result.get("classification_filter")
    if isinstance(filt, Mapping) and any(_text(v) for v in filt.values()):
        return True
    data = result.get("data")
    if isinstance(data, Mapping):
        return _has_hard_scope(data)
    return False


def _latency_stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "max_ms": None}
    ordered = sorted(values)

    def percentile(q: float) -> float:
        index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * q)))
        return round(ordered[index], 3)

    return {
        "count": len(values),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "max_ms": round(max(ordered), 3),
    }


def run_gate(
    db_path: Path | str,
    *,
    search_fn: SearchFn | None = None,
    expected_major_count: int = DEFAULT_EXPECTED_MAJOR_COUNT,
    failure_limit: int = MAX_FAILURE_EXAMPLES,
    scope: str = "unit",
) -> dict[str, Any]:
    """Execute the gate and return a JSON-serializable report."""
    started = time.perf_counter()
    resolved_db_path = Path(db_path).resolve()
    before_stat = resolved_db_path.stat()
    database_signature_before = {
        "size_bytes": before_stat.st_size,
        "mtime_ns": before_stat.st_mtime_ns,
    }
    rows = load_classification_rows(resolved_db_path)
    sampling = sample_scope_cases(rows, expected_major_count=expected_major_count)
    if search_fn is None:
        os.environ["NCS_DB_PATH"] = str(resolved_db_path)
        # Import after NCS_DB_PATH is selected; server.db() is read-only.
        from ncs_mcp.server import ncs_search

        search_fn = ncs_search

    calls: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    latencies: list[float] = []

    def record(sample: Mapping[str, Any], kind: str, result: Mapping[str, Any], elapsed: float) -> None:
        error_code = _error_code(result)
        result_rows = _result_rows(result)
        ambiguous = bool(sample.get("ambiguous"))
        contained = all(_path_contains(row, sample) for row in result_rows)
        if kind == "bare_term":
            # A bare lexical term is deliberately unscoped even when the same
            # source label is ambiguous across branches.  It must not be
            # forced into either branch or fail closed as a side effect of
            # scope inference that the caller never requested.
            hard_scope = _has_hard_scope(result)
            passed = not hard_scope and error_code in {None, "NOT_FOUND"}
            if passed:
                reason = None
            elif hard_scope:
                reason = "bare_term_acquired_hard_scope"
            else:
                reason = "bare_term_execution_failure"
        elif ambiguous:
            passed = error_code == "route_context_required" and not result_rows
            reason = None if passed else "ambiguous_scope_not_failed_closed"
        else:
            hard_scope = _has_hard_scope(result)
            execution_ok = error_code in {None, "NOT_FOUND"}
            passed = hard_scope and contained and execution_ok
            if passed:
                reason = None
            elif not hard_scope:
                reason = "explicit_scope_not_hard_bound"
            else:
                reason = "scope_containment_or_execution_failure"
        item = {
            "major_code": sample.get("major_code"),
            "requested_level": sample.get("requested_level"),
            "label": sample.get("label"),
            "kind": kind,
            "ambiguous_expected": ambiguous,
            "candidate_count": sample.get("candidate_count"),
            "returned_count": len(result_rows),
            "error_code": error_code,
            "contained": contained,
            "hard_scope_observed": _has_hard_scope(result),
            "latency_ms": round(elapsed * 1000, 3),
            "passed": passed,
        }
        if reason:
            item["failure_reason"] = reason
            if len(failures) < failure_limit:
                failures.append({
                    **item,
                    "response_excerpt": {
                        "error_code": error_code,
                        "returned_count": len(result_rows),
                    },
                })
        calls.append(item)

    for sample in sampling["samples"]:
        label = str(sample["label"])
        invocations = (
            (
                "explicit_full_query",
                {"query": f"{label} \uc9c1\ubb34\uc5d0 \ud544\uc694\ud55c \uc5ed\ub7c9"},
            ),
            ("job_scope_argument", {"query": label, "job_scope": label}),
            ("bare_term", {"query": label}),
        )
        for kind, kwargs in invocations:
            call_started = time.perf_counter()
            try:
                raw_result = search_fn(scope=scope, limit=20, **kwargs)
                result = raw_result if isinstance(raw_result, Mapping) else {"error": "invalid_result"}
            except Exception as exc:  # bounded, report-only failure evidence
                result = {"error": {"code": "exception", "message": str(exc)[:200]}}
            elapsed = time.perf_counter() - call_started
            latencies.append(elapsed * 1000)
            record(sample, kind, result, elapsed)

    unexpected_count = sum(1 for item in calls if not item["passed"])
    summary = {
        "major_count": sampling["major_count"],
        "expected_major_count": expected_major_count,
        "major_coverage_count": sampling["major_count"],
        "major_coverage_ratio": round(
            sampling["major_count"] / expected_major_count, 4
        ) if expected_major_count else 1.0,
        "coverage_complete": sampling["coverage_complete"],
        "sampled_scope_count": len(sampling["samples"]),
        "execution_count": len(calls),
        "unexpected_failure_count": unexpected_count,
    }
    after_stat = resolved_db_path.stat()
    database_signature_after = {
        "size_bytes": after_stat.st_size,
        "mtime_ns": after_stat.st_mtime_ns,
    }
    db_mutation = database_signature_before != database_signature_after
    ok = bool(
        summary["coverage_complete"]
        and unexpected_count == 0
        and not db_mutation
    )
    return {
        "schema": SCHEMA,
        "gate": "direct_scope_execution",
        "ok": ok,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(resolved_db_path),
        "source_derived": True,
        "non_holdout_regression": True,
        "execution_scope": scope,
        "holdout_inspected": False,
        "db_mutation": db_mutation,
        "database_signature_before": database_signature_before,
        "database_signature_after": database_signature_after,
        "aliases_status_writes": False,
        "summary": summary,
        "sampling": {
            "levels": list(LEVELS),
            "major_codes": sampling["major_codes"],
            "samples": sampling["samples"],
        },
        "latency_ms": _latency_stats(latencies),
        "unexpected_failures": failures,
        "execution": calls,
        "elapsed_total_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def render_markdown(report: Mapping[str, Any]) -> str:
    summary = report.get("summary") or {}
    latency = report.get("latency_ms") or {}
    failures = report.get("unexpected_failures") or []
    lines = [
        "# NCS Scope Execution Gate",
        "",
        f"- Status: **{'PASS' if report.get('ok') else 'FAIL'}**",
        f"- Schema: `{report.get('schema')}`",
        f"- Source-derived: `{report.get('source_derived')}`; non-holdout: `{report.get('non_holdout_regression')}`",
        f"- Search scope exercised: `{report.get('execution_scope', 'unit')}`",
        f"- holdout_inspected: `{report.get('holdout_inspected')}`; db_mutation: `{report.get('db_mutation')}`; aliases_status_writes: `{report.get('aliases_status_writes')}`",
        "",
        "## Coverage and execution",
        "",
        f"- Majors covered: {summary.get('major_coverage_count')}/{summary.get('expected_major_count')} ({summary.get('major_coverage_ratio')})",
        f"- Scope samples: {summary.get('sampled_scope_count')}; executions: {summary.get('execution_count')}",
        f"- Unexpected failures: {summary.get('unexpected_failure_count')}",
        f"- Latency p50/p95/max: {latency.get('p50_ms')} / {latency.get('p95_ms')} / {latency.get('max_ms')} ms",
        "",
        "## Regression contract",
        "",
        "Each source label is checked as an explicit full query, as a `job_scope` argument, and as a bare term. Canonical same-branch ancestor duplicates are collapsed; incompatible exact branches must return `route_context_required`. Bare terms must not acquire a hard classification filter.",
        "",
    ]
    if failures:
        lines.extend(["## Bounded failure examples", ""])
        for item in failures:
            lines.append(
                f"- `{item.get('major_code')}/{item.get('requested_level')}` "
                f"`{item.get('kind')}` label `{item.get('label')}`: "
                f"{item.get('failure_reason')} (error={item.get('error_code')}, returned={item.get('returned_count')})"
            )
        lines.append("")
    lines.extend(["## Major codes", "", ", ".join(report.get("sampling", {}).get("major_codes", [])), ""])
    return "\n".join(lines)


def write_report(report: Mapping[str, Any], json_out: Path | str, markdown_out: Path | str) -> None:
    json_path = Path(json_out)
    markdown_path = Path(markdown_out)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=None, help="SQLite DB path (default: configured NCS DB)")
    parser.add_argument("--json-out", default=None, help="JSON report output path")
    parser.add_argument("--markdown-out", default=None, help="Markdown report output path")
    parser.add_argument("--expected-major-count", type=int, default=DEFAULT_EXPECTED_MAJOR_COUNT)
    parser.add_argument(
        "--scope",
        choices=("unit", "element", "criteria", "ksa", "all"),
        default="unit",
        help="NCS search result scope to exercise (default: unit; all is slower)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.db:
        db_path = Path(args.db)
    else:
        from ncs_mcp.config import DEFAULT_DB_PATH

        db_path = DEFAULT_DB_PATH
    stamp = datetime.now().strftime("%Y%m%d")
    output_dir = Path("reports") / "overnight_sessions"
    json_out = Path(args.json_out) if args.json_out else output_dir / f"ncs_scope_execution_gate_{stamp}.json"
    markdown_out = Path(args.markdown_out) if args.markdown_out else output_dir / f"ncs_scope_execution_gate_{stamp}.md"
    try:
        report = run_gate(
            db_path,
            expected_major_count=max(0, args.expected_major_count),
            scope=args.scope,
        )
    except Exception as exc:
        report = {
            "schema": SCHEMA,
            "gate": "direct_scope_execution",
            "ok": False,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "db_path": str(Path(db_path).resolve()),
            "source_derived": True,
            "non_holdout_regression": True,
            "execution_scope": args.scope,
            "holdout_inspected": False,
            "db_mutation": False,
            "aliases_status_writes": False,
            "summary": {"unexpected_failure_count": 1},
            "latency_ms": _latency_stats([]),
            "unexpected_failures": [{"failure_reason": "gate_exception", "message": str(exc)[:200]}],
        }
    write_report(report, json_out, markdown_out)
    print(json.dumps({"ok": report["ok"], "json": str(json_out), "markdown": str(markdown_out)}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
