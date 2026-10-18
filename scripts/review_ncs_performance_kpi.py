"""Review frozen, local function A/B evidence without accessing a DB or network.

This is a report-only K09 reviewer, not a commit, deployment, human-approval,
remote MCP latency, or independent semantic-accuracy gate. It recomputes metrics
from paired raw samples and never trusts the benchmark's summary as evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SEED = 20261018
MIN_RESAMPLES = 10000
REGRESSION_LIMIT = 0.05
COMPARISON_TOLERANCE = 1e-12
LABELS = ("baseline", "candidate")
PRAGMAS = ("query_only", "mmap_size", "cache_size", "foreign_keys", "busy_timeout")
EXCLUDED_RESPONSE_PATHS = ["$.audit.generated_at"]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read(path: Path) -> tuple[Any, str]:
    data = path.read_bytes()

    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite JSON constant: {value}")

    return json.loads(data.decode("utf-8-sig"), parse_constant=reject_constant), _sha(data)


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _path_key(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    value = value.replace("\\", "/").rstrip("/")
    return value.casefold() if ":" in value[:3] else value


def _timestamp(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("Cannot calculate a quantile from empty samples")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _statistic(values: list[float], statistic: str) -> float:
    return nearest_rank(values, 0.5 if statistic == "p50" else 0.95)


def paired_interval(
    strata: list[tuple[list[float], list[float]]], statistic: str, resamples: int
) -> tuple[float, float]:
    """Percentile CI; retain each case's sample count and its AB pairing.

    A workload contains separate case strata. The same sampled index is used
    for A and B, and cases are never silently dropped or pooled before sampling.
    Each metric starts from the documented seed, independent of iteration order.
    """
    rng = random.Random(SEED)
    changes: list[float] = []
    for _ in range(resamples):
        baseline: list[float] = []
        candidate: list[float] = []
        for before, after in strata:
            for _ in range(len(before)):
                index = rng.randrange(len(before))
                baseline.append(before[index])
                candidate.append(after[index])
        changes.append(_statistic(candidate, statistic) / _statistic(baseline, statistic) - 1)
    return nearest_rank(changes, 0.025), nearest_rank(changes, 0.975)


def _metric(
    strata: list[tuple[list[float], list[float]]], statistic: str, resamples: int, phase: str
) -> dict[str, Any]:
    before = [value for pair in strata for value in pair[0]]
    after = [value for pair in strata for value in pair[1]]
    baseline = _statistic(before, statistic)
    candidate = _statistic(after, statistic)
    change = candidate / baseline - 1
    lower, upper = paired_interval(strata, statistic, resamples)
    retest_required = phase == "initial" and change > REGRESSION_LIMIT + COMPARISON_TOLERANCE
    if upper <= REGRESSION_LIMIT + COMPARISON_TOLERANCE:
        status = "pass"
    elif lower > REGRESSION_LIMIT + COMPARISON_TOLERANCE:
        status = "warn" if phase == "initial" else "fail"
    else:
        status = "inconclusive"
    if retest_required and status == "pass":
        status = "warn"
    return {
        "statistic": statistic,
        "baseline_ms": baseline,
        "candidate_ms": candidate,
        "relative_change": change,
        "relative_change_percent": change * 100,
        "paired95_ci": {"lower": lower, "upper": upper},
        "sample_count_per_arm": len(before),
        "case_count": len(strata),
        "regression_limit": REGRESSION_LIMIT,
        "comparison_absolute_tolerance": COMPARISON_TOLERANCE,
        "regression_status": status,
        "initial_point_estimate_warning": retest_required,
        "planned_retest_required": retest_required,
    }


def _case_identity(case: Any) -> Any:
    if not isinstance(case, dict):
        return None
    return {key: case.get(key) for key in ("id", "workload", "params")}


def _normal_scope_no_match(body: dict[str, Any], workload: Any) -> bool:
    """Recognize the scope resolver's technical no-match response contract.

    resolve_ncs_query_scope returns ok=bool(candidates), so an empty candidate
    list can be a normally completed request. This classification is not a
    human judgment that abstention was correct, nor an exclusion from latency.
    Unknown failure shapes and explicit error/timeout markers remain failures.
    """
    return (
        workload in ("scope", "scope_edge")
        and body.get("ok") is False
        and body.get("candidates") == []
        and isinstance(body.get("query"), str)
        and isinstance(body.get("normalized_query"), str)
        and not any(key in body for key in ("error", "isError", "timeout", "timed_out"))
    )


def _validate(
    benchmark: dict[str, Any], plan: dict[str, Any], cases: Any, cases_hash: str, resamples: int
) -> tuple[list[str], str, list[dict[str, Any]]]:
    issues: list[str] = []

    def check(condition: bool, code: str) -> None:
        if not condition:
            issues.append(code)

    check(benchmark.get("schema") == "ncs_code_ab_v1", "benchmark_schema")
    check(plan.get("schema") == "ncs_performance_trial_plan_v1", "plan_schema")
    check(benchmark.get("mode") == "separate_process_interleaved_read_only_function_calls", "benchmark_mode")
    check(benchmark.get("deployment_performed") is False, "deployment_performed_or_unknown")
    check(plan.get("db_mutation") is False, "plan_db_mutation_or_unknown")
    check(benchmark.get("excluded_response_paths") == EXCLUDED_RESPONSE_PATHS, "response_exclusion_contract")
    check(benchmark.get("warmups_per_case") == 1, "warmup_contract")
    check(isinstance(resamples, int) and not isinstance(resamples, bool) and resamples >= MIN_RESAMPLES,
          "bootstrap_resamples_below_10000")
    check(plan.get("initial_runs_per_case") == 14, "initial_sample_plan")
    check(plan.get("retest_runs_per_case") == 30, "retest_sample_plan")
    check(plan.get("maximum_retests") == 1, "retest_budget_plan")
    runs = benchmark.get("runs_per_case")
    phase = "initial" if runs == 14 else "planned_retest" if runs == 30 else "invalid"
    check(type(runs) is int and phase != "invalid", "runs_must_match_frozen_14_or_30_plan")
    started = _timestamp(benchmark.get("started_at"))
    finished = _timestamp(benchmark.get("finished_at"))
    frozen = _timestamp(plan.get("frozen_at"))
    check(started is not None, "started_at_missing_or_invalid")
    check(finished is not None, "finished_at_missing_or_invalid")
    check(frozen is not None, "frozen_at_missing_or_invalid")
    if started and finished:
        check(finished >= started, "finish_before_start")
    if started and frozen:
        check(frozen <= started, "plan_frozen_after_run_started")

    check(isinstance(cases, list) and bool(cases), "empty_or_invalid_frozen_cases")
    expected = cases if isinstance(cases, list) else []
    identities = [_case_identity(case) for case in expected]
    check(all(item and isinstance(item["id"], str) and item["id"]
              and isinstance(item["workload"], str) and item["workload"]
              and isinstance(item["params"], dict) for item in identities), "invalid_frozen_case_identity")
    ids = [item.get("id") for item in identities if isinstance(item, dict)]
    check(len({str(value) for value in ids}) == len(ids), "duplicate_frozen_case_ids")
    check(benchmark.get("cases_sha256") == cases_hash, "fixture_hash_mismatch")
    raw_records = benchmark.get("records")
    check(isinstance(raw_records, list) and bool(raw_records), "empty_or_invalid_records")
    records = raw_records if isinstance(raw_records, list) else []
    check([_case_identity(record) for record in records] == identities, "case_ids_order_workload_or_params_mismatch")
    primary = plan.get("primary_metric")
    primary = primary if isinstance(primary, dict) else {}
    check(primary.get("statistic") == "pooled_p95_ms", "primary_statistic_unsupported")
    check(isinstance(primary.get("workload"), str) and primary.get("workload") in
          [item.get("workload") for item in identities if isinstance(item, dict)], "primary_workload_missing")

    dbs = [benchmark.get("database"), benchmark.get("candidate_database")]
    check(benchmark.get("same_database") is True, "same_database_not_true")
    for label, raw_db in zip(LABELS, dbs):
        db = raw_db if isinstance(raw_db, dict) else {}
        check(_is_hash(db.get("sha256_before")) and db.get("sha256_before") == db.get("sha256_after"),
              f"{label}:database_hash_missing_or_changed")
        check(db.get("unchanged") is True, f"{label}:database_unchanged_not_true")
        check(type(db.get("bytes")) is int and db["bytes"] > 0, f"{label}:database_size_invalid")
        check(bool(_path_key(db.get("path"))), f"{label}:database_path_missing")
    if all(isinstance(db, dict) for db in dbs):
        check(_path_key(dbs[0].get("path")) == _path_key(dbs[1].get("path")), "different_database_paths")
        check(dbs[0].get("sha256_before") == dbs[1].get("sha256_before"), "different_database_hashes")
        check(dbs[0].get("bytes") == dbs[1].get("bytes"), "different_database_sizes")

    source_before = benchmark.get("source_before")
    source_after = benchmark.get("source_after")
    source_before = source_before if isinstance(source_before, dict) else {}
    source_after = source_after if isinstance(source_after, dict) else {}
    check(benchmark.get("source_unchanged_during_run") is True, "source_unchanged_not_true")
    check(bool(source_before) and source_before == source_after, "source_manifest_changed_or_missing")
    for label in LABELS:
        for moment, source_map in (("before", source_before), ("after", source_after)):
            item = source_map.get(label)
            item = item if isinstance(item, dict) else {}
            files = item.get("files")
            valid = isinstance(files, dict) and bool(files) and all(
                isinstance(name, str) and _is_hash(value) for name, value in files.items())
            check(valid, f"{label}:{moment}:source_files_missing_or_invalid")
            if valid:
                digest = _sha(json.dumps(files, sort_keys=True).encode())
                check(item.get("sha256") == digest, f"{label}:{moment}:source_aggregate_hash_mismatch")
            check(bool(_path_key(item.get("root"))), f"{label}:{moment}:source_root_missing")

    workers = benchmark.get("workers")
    workers = workers if isinstance(workers, dict) else {}
    worker_values: list[dict[str, Any]] = []
    for label in LABELS:
        item = workers.get(label)
        item = item if isinstance(item, dict) else {}
        worker_values.append(item)
        check(item.get("ready") is True, f"{label}:worker_not_ready")
        pragmas = item.get("pragmas")
        pragmas = pragmas if isinstance(pragmas, dict) else {}
        check(all(key in pragmas for key in PRAGMAS), f"{label}:worker_pragmas_missing")
        check(type(pragmas.get("query_only")) is int and pragmas["query_only"] == 1,
              f"{label}:worker_not_query_only")
        source = source_before.get(label)
        source = source if isinstance(source, dict) else {}
        root = _path_key(source.get("root"))
        check(bool(root) and _path_key(item.get("source")) == root + "/ncs_mcp/server.py",
              f"{label}:worker_source_root_mismatch")
        check(bool(item.get("python")) and bool(item.get("sqlite")), f"{label}:worker_runtime_missing")
    for key in ("pragmas", "python", "sqlite"):
        check(worker_values[0].get(key) == worker_values[1].get(key), f"worker_{key}_mismatch")

    check(benchmark.get("all_responses_equal") is True, "reported_all_responses_equal_not_true")
    validated: list[dict[str, Any]] = []
    for position, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(f"record_{position}:not_object")
            continue
        case_id = str(record.get("id", position))
        prefix = f"case:{case_id}"
        check(record.get("response_equal") is True, prefix + ":reported_response_equality_not_true")
        samples = record.get("samples")
        warmups = record.get("warmup")
        samples = samples if isinstance(samples, dict) else {}
        warmups = warmups if isinstance(warmups, dict) else {}
        bodies: list[Any] = []
        elapsed: dict[str, list[float]] = {}
        for label in LABELS:
            warmup = warmups.get(label)
            warmup = warmup if isinstance(warmup, dict) else {}
            body = warmup.get("body")
            check(isinstance(body, dict) and bool(body), prefix + f":{label}:warmup_body_missing")
            body = body if isinstance(body, dict) else {}
            bodies.append(body)
            normal_no_match = _normal_scope_no_match(body, record.get("workload"))
            check((body.get("ok") is not False or normal_no_match) and body.get("isError") is not True
                  and not body.get("error") and not body.get("timeout") and not body.get("timed_out"),
                  prefix + f":{label}:error_response")
            body_bytes = json.dumps(body, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
            digest = _sha(body_bytes)
            returned = body.get("returned", len(body.get("candidates", [])))
            raw_samples = samples.get(label)
            check(isinstance(raw_samples, list) and bool(raw_samples), prefix + f":{label}:empty_samples")
            raw_samples = raw_samples if isinstance(raw_samples, list) else []
            check(len(raw_samples) == runs, prefix + f":{label}:sample_count_mismatch")
            elapsed[label] = []
            for index, sample in enumerate([warmup, *raw_samples]):
                tag = "warmup" if index == 0 else f"sample_{index - 1}"
                key = prefix + f":{label}:{tag}"
                if not isinstance(sample, dict):
                    issues.append(key + ":not_object")
                    continue
                check(not sample.get("error") and (sample.get("ok") is not False or normal_no_match)
                      and not sample.get("timeout") and not sample.get("timed_out")
                      and sample.get("isError") is not True,
                      key + ":error_sample")
                check(sample.get("ok") == body.get("ok"), key + ":response_status_mismatch")
                check(_positive_number(sample.get("elapsed_ms")), key + ":nonpositive_or_nonfinite_latency")
                check(sample.get("sha256") == digest, key + ":response_hash_mismatch")
                check(type(sample.get("bytes")) is int and sample["bytes"] == len(body_bytes),
                      key + ":response_size_mismatch")
                check(type(sample.get("returned")) is int and sample["returned"] >= 0
                      and sample["returned"] == returned, key + ":returned_count_mismatch")
                if index and _positive_number(sample.get("elapsed_ms")):
                    elapsed[label].append(float(sample["elapsed_ms"]))
            check(len(elapsed[label]) == runs, prefix + f":{label}:valid_sample_count_mismatch")
        check(bodies[0] == bodies[1], prefix + ":actual_response_body_mismatch")
        normal_no_match = _normal_scope_no_match(bodies[0], record.get("workload"))
        validated.append({"id": case_id, "workload": record.get("workload"), "params": record.get("params"),
                          "baseline": elapsed["baseline"], "candidate": elapsed["candidate"],
                          "zero_result": bodies[0].get("returned", len(bodies[0].get("candidates", []))) == 0,
                          "response_classification": "normal_no_match_contract" if normal_no_match else "normal_response",
                          "no_match_kind": ("empty_query" if not bodies[0]["query"].strip() else "no_match")
                          if normal_no_match else None})
    return list(dict.fromkeys(issues)), phase, validated


def review_performance(benchmark_path: Path, plan_path: Path, *, resamples: int = MIN_RESAMPLES) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "ncs_performance_kpi_review_v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "metric_id": "K09",
        "report_only": True,
        "db_writes": False,
        "network_calls": False,
        "status_update_allowed": False,
        "approval_claim": False,
        "commit_performed": False,
        "deployment_performed": False,
        "technical_performance_eligible": False,
        "validation_ok": False,
        "commit_eligible": False,
        "commit_status": "final_companion_validation_required",
        "status": "unknown",
        "validation_issues": [],
        "source_artifacts": {"benchmark": str(benchmark_path), "plan": str(plan_path)},
        "statistics": {"seed": SEED, "resamples": resamples, "minimum_resamples": MIN_RESAMPLES,
                       "confidence_level": 0.95, "method": "paired percentile bootstrap; cases stratified",
                       "p95": "nearest_rank", "p50": "nearest_rank", "warmup_excluded": True},
        "policy": {"initial_pairs_per_case": 14, "planned_retest_pairs_per_case": 30,
                   "maximum_planned_retests": 1, "retest_budget_independently_verified": False,
                   "regression_limit": REGRESSION_LIMIT,
                   "initial_point_regression": "warn and use the single preplanned retest",
                   "interval_crosses_regression_limit": "inconclusive; hold",
                   "retest_regression": "lower CI >5% fails; recurrence requires initial companion evidence",
                   "primary_improvement": "candidate/baseline - 1 paired95 CI upper <0"},
        "companion_checklist": [
            {"id": "quality_and_applicable_guardrails", "status": "unknown"},
            {"id": "staged_source_matches_measured_candidate", "status": "unknown"},
            {"id": "tests_lint_and_deploy_source_parity", "status": "unknown"},
            {"id": "harness_configs_dependencies_and_patch_manifest", "status": "unknown"},
            {"id": "retest_budget_and_all_trial_disclosure", "status": "unknown"},
            {"id": "source_only_commit_and_push_scope", "status": "unknown"},
        ],
        "limitations": [
            "Local function calls; excludes startup, imports, IPC and response hashing, and does not measure remote MCP SLOs.",
            "Response equality applies only to frozen cases; it is not independent human semantic validation.",
            "Scope ok=false with the strict empty-candidate contract is normal technical completion; empty_query/no_match remain in latency and do not establish correct semantic abstention.",
            "Validates recorded DB/source hashes, not the live DB or files; does not open a database or execute benchmarks.",
            "Source manifests cover Python files only; config, harness, dependencies, dirty patch and staged-source identity need companion evidence.",
            "Raw latency samples carry response hashes; only warmup bodies are available, so measured body equality relies on those recorded hashes.",
            "Paired case-stratified bootstrap does not establish independence of production traffic or remote cold instances.",
        ],
    }
    try:
        benchmark, benchmark_hash = _read(Path(benchmark_path))
        plan, plan_hash = _read(Path(plan_path))
        if not isinstance(benchmark, dict) or not isinstance(plan, dict):
            raise ValueError("Benchmark and plan must be JSON objects")
        cases_name = plan.get("primary_cases")
        if not isinstance(cases_name, str) or not cases_name:
            raise ValueError("Plan primary_cases is missing")
        cases_path = Path(cases_name)
        if not cases_path.is_absolute():
            cases_path = Path(plan_path).parent / cases_path
        cases, cases_hash = _read(cases_path)
        report["source_artifacts"].update({"benchmark_sha256": benchmark_hash, "plan_sha256": plan_hash,
                                           "cases": str(cases_path), "cases_sha256": cases_hash})
        issues, phase, records = _validate(benchmark, plan, cases, cases_hash, resamples)
        report.update({"validation_issues": issues, "measurement_phase": phase,
                       "window_start": benchmark.get("started_at"), "window_end": benchmark.get("finished_at"),
                       "trial": plan.get("trial"), "scope": plan.get("scope"),
                       "case_count": len(records), "runs_per_case": benchmark.get("runs_per_case"),
                       "normal_no_match_contract_counts": {
                           "empty_query": sum(record["no_match_kind"] == "empty_query" for record in records),
                           "no_match": sum(record["no_match_kind"] == "no_match" for record in records),
                       },
                       "source_manifests": {"before": benchmark.get("source_before"), "after": benchmark.get("source_after")},
                       "database_identity": benchmark.get("database"), "workers": benchmark.get("workers")})
        if issues:
            report["status"] = "inconclusive"
            return report
        query_metrics = []
        for record in records:
            metric = _metric([(record["baseline"], record["candidate"])], "p50", resamples, phase)
            metric.update({key: record[key] for key in (
                "id", "workload", "params", "zero_result", "response_classification", "no_match_kind")})
            query_metrics.append(metric)
        workload_metrics: dict[str, dict[str, Any]] = {}
        for workload in sorted({record["workload"] for record in records}):
            strata = [(record["baseline"], record["candidate"]) for record in records if record["workload"] == workload]
            workload_metrics[workload] = _metric(strata, "p95", resamples, phase)
        primary_workload = plan["primary_metric"]["workload"]
        primary = workload_metrics[primary_workload]
        improved = primary["paired95_ci"]["upper"] < 0
        all_metrics = [*query_metrics, *workload_metrics.values()]
        no_regression = all(metric["regression_status"] == "pass" for metric in all_metrics)
        report.update({
            "validation_ok": True,
            "query_metrics": query_metrics,
            "workload_metrics": workload_metrics,
            "raw_paired_elapsed_ms": [{key: record[key] for key in ("id", "workload", "baseline", "candidate")} for record in records],
            "primary": {"workload": primary_workload, "metric": "pooled_p95_ms", **primary,
                        "improvement_proven": improved},
            "all_regression_metrics_pass": no_regression,
            "technical_performance_eligible": improved and no_regression,
            "status": "pass" if improved and no_regression else "fail" if any(
                metric["regression_status"] == "fail" for metric in all_metrics) else "inconclusive" if any(
                metric["regression_status"] == "inconclusive" for metric in all_metrics) else "warn",
        })
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        report["status"] = "inconclusive"
        report["validation_issues"].append(f"input_or_metric_error:{type(exc).__name__}:{exc}")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--bootstrap-resamples", type=int, default=MIN_RESAMPLES)
    parser.add_argument("--require-improvement", action="store_true")
    args = parser.parse_args(argv)
    # Writing over inputs would destroy the evidence this report is meant to preserve.
    input_paths = {args.benchmark.resolve(), args.plan.resolve()}
    try:
        plan, _ = _read(args.plan)
        name = plan.get("primary_cases") if isinstance(plan, dict) else None
        if isinstance(name, str):
            path = Path(name)
            input_paths.add((path if path.is_absolute() else args.plan.parent / path).resolve())
    except (OSError, ValueError):
        pass
    if args.out.resolve() in input_paths:
        parser.error("--out must not overwrite benchmark, plan or cases")
    report = review_performance(args.benchmark, args.plan, resamples=args.bootstrap_resamples)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"out": str(args.out), "status": report["status"],
                      "technical_performance_eligible": report["technical_performance_eligible"],
                      "commit_eligible": report["commit_eligible"]}, ensure_ascii=False))
    if report["validation_issues"]:
        return 1
    return 1 if args.require_improvement and not report["technical_performance_eligible"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
