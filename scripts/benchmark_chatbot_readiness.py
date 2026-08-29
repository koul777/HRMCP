from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


SCHEMA = "ncs_chatbot_readiness_benchmark_v1"
QUERY_ROUTE_SCHEMA = "ncs_query_route_v1"
GUIDE_TRACE_SCHEMA = "aihr_training_system_guide_trace_v1"
GUIDE_CHECKS = {
    "job_scope",
    "task_ksa",
    "course_link",
    "required_optional",
    "level_delivery",
    "human_review",
}
PLAN_MATRIX_FIELDS = {
    "job_scope",
    "target_level_band",
    "education_type",
    "required_optional_basis",
    "delivery_operation",
    "planner_grouping",
    "task_ksa_basis",
    "facility_constraint_fit",
    "human_review",
    "course_fit",
}
COURSE_FIT_FIELDS = {"level", "hours", "methods", "facilities"}
SAVE_FORCED_TOOLS = {
    "recommend_training_for_task",
    "recommend_training_transition",
    "plan_ncs_education_path",
}
SQLITE_FILE_MANIFEST_SCHEMA = "sqlite_database_file_manifest_v1"
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    route_query: str
    expected_route: str
    tool_name: str
    params: dict[str, Any]
    validator: Callable[[dict[str, Any]], list[str]]


def _sha256_file(path: Path, *, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _snapshot_file(path: Path) -> dict[str, Any]:
    try:
        stat_before = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "sha256": None,
            "size_bytes": None,
            "mtime_ns": None,
            "stable_during_hash": not path.exists(),
        }

    try:
        sha256 = _sha256_file(path)
        stat_after = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "sha256": None,
            "size_bytes": None,
            "mtime_ns": None,
            "stable_during_hash": False,
        }

    stable = (
        stat_before.st_size == stat_after.st_size
        and stat_before.st_mtime_ns == stat_after.st_mtime_ns
    )
    return {
        "path": str(path),
        "exists": True,
        "sha256": sha256,
        "size_bytes": stat_after.st_size,
        "mtime_ns": stat_after.st_mtime_ns,
        "stable_during_hash": stable,
    }


def snapshot_database(path: Path) -> dict[str, Any]:
    base = _snapshot_file(path)
    sidecars = {
        suffix: _snapshot_file(Path(f"{path}{suffix}"))
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    return {
        # Preserve the original base-DB fingerprint fields for report consumers.
        "sha256": base["sha256"],
        "size_bytes": base["size_bytes"],
        "mtime_ns": base["mtime_ns"],
        "stable_during_hash": base["stable_during_hash"],
        "manifest_schema": SQLITE_FILE_MANIFEST_SCHEMA,
        "base": base,
        "sidecars": sidecars,
    }


def _legacy_base_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    base = snapshot.get("base")
    if isinstance(base, dict):
        return base
    return {
        "path": None,
        "exists": snapshot.get("sha256") is not None,
        "sha256": snapshot.get("sha256"),
        "size_bytes": snapshot.get("size_bytes"),
        "mtime_ns": snapshot.get("mtime_ns"),
        "stable_during_hash": snapshot.get("stable_during_hash"),
    }


def _absent_file_snapshot() -> dict[str, Any]:
    return {
        "path": None,
        "exists": False,
        "sha256": None,
        "size_bytes": None,
        "mtime_ns": None,
        "stable_during_hash": True,
    }


def _compare_file_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    checks = {
        "existence_unchanged": before.get("exists") == after.get("exists"),
        "sha256_unchanged": before.get("sha256") == after.get("sha256"),
        "size_unchanged": before.get("size_bytes") == after.get("size_bytes"),
        "mtime_unchanged": before.get("mtime_ns") == after.get("mtime_ns"),
        "before_snapshot_stable": before.get("stable_during_hash") is True,
        "after_snapshot_stable": after.get("stable_during_hash") is True,
    }
    content_unchanged = bool(
        checks["existence_unchanged"]
        and checks["sha256_unchanged"]
        and checks["size_unchanged"]
        and checks["before_snapshot_stable"]
        and checks["after_snapshot_stable"]
    )
    unchanged = all(checks.values())
    existed_before = before.get("exists") is True
    exists_after = after.get("exists") is True
    if not existed_before and exists_after:
        change_type = "created"
    elif existed_before and not exists_after:
        change_type = "deleted"
    elif not unchanged and existed_before and exists_after:
        change_type = "modified"
    elif not unchanged:
        change_type = "unstable_snapshot"
    else:
        change_type = "unchanged"
    return {
        **checks,
        "existed_before": existed_before,
        "exists_after": exists_after,
        "change_type": change_type,
        "content_unchanged": content_unchanged,
        "unchanged": unchanged,
    }


def compare_database_snapshots(
    before: dict[str, Any],
    after: dict[str, Any],
) -> dict[str, Any]:
    base_comparison = _compare_file_snapshots(
        _legacy_base_snapshot(before),
        _legacy_base_snapshot(after),
    )
    checks = {
        "sha256_unchanged": base_comparison["sha256_unchanged"],
        "size_unchanged": base_comparison["size_unchanged"],
        "mtime_unchanged": base_comparison["mtime_unchanged"],
        "before_snapshot_stable": base_comparison["before_snapshot_stable"],
        "after_snapshot_stable": base_comparison["after_snapshot_stable"],
    }
    before_sidecars = before.get("sidecars")
    after_sidecars = after.get("sidecars")
    if not isinstance(before_sidecars, dict):
        before_sidecars = {}
    if not isinstance(after_sidecars, dict):
        after_sidecars = {}
    sidecar_comparisons = {
        suffix: _compare_file_snapshots(
            before_sidecars.get(suffix, _absent_file_snapshot()),
            after_sidecars.get(suffix, _absent_file_snapshot()),
        )
        for suffix in SQLITE_SIDECAR_SUFFIXES
    }
    changed_sidecars = [
        suffix
        for suffix, comparison in sidecar_comparisons.items()
        if not comparison["unchanged"]
    ]
    content_changed_sidecars = [
        suffix
        for suffix, comparison in sidecar_comparisons.items()
        if not comparison["content_unchanged"]
    ]
    base_unchanged = base_comparison["unchanged"]
    sidecars_unchanged = not changed_sidecars
    storage_content_unchanged = bool(
        base_comparison["content_unchanged"] and not content_changed_sidecars
    )
    return {
        **checks,
        "base_unchanged": base_unchanged,
        "sidecars_unchanged": sidecars_unchanged,
        "changed_sidecars": changed_sidecars,
        "storage_content_unchanged": storage_content_unchanged,
        "content_changed_sidecars": content_changed_sidecars,
        "base_comparison": base_comparison,
        "sidecar_comparisons": sidecar_comparisons,
        "all_unchanged": base_unchanged and sidecars_unchanged,
    }


def latency_summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"sample_count": 0, "p50": None, "p95": None, "max": None}

    ordered = sorted(values)

    def percentile(fraction: float) -> float:
        position = (len(ordered) - 1) * fraction
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)

    return {
        "sample_count": len(ordered),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "max": round(ordered[-1], 3),
    }


@contextmanager
def _temporary_environment(values: dict[str, str]) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def _payload_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    data = payload.get("data")
    if isinstance(data, dict):
        return data.get(key)
    return None


def _validate_search_result(result: dict[str, Any]) -> list[str]:
    rows = _payload_value(result, "results")
    return [] if isinstance(rows, list) and rows else ["search_results_missing_or_empty"]


def _validate_task_result(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if _payload_value(result, "view") != "compact_training_task":
        errors.append("task_view_invalid")
    courses = _payload_value(result, "recommended_courses")
    if not isinstance(courses, list) or not courses:
        errors.append("task_recommended_courses_missing_or_empty")
    return errors


def _validate_transition_result(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if _payload_value(result, "view") != "compact_training_transition":
        errors.append("transition_view_invalid")
    courses = _payload_value(result, "recommended_courses")
    if not isinstance(courses, list) or not courses:
        errors.append("transition_recommended_courses_missing_or_empty")
    if not isinstance(_payload_value(result, "transition_summary"), dict):
        errors.append("transition_summary_missing")
    return errors


def _validate_plan_result(result: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if _payload_value(result, "view") != "ncs_education_plan":
        errors.append("plan_view_invalid")

    plan_route = _payload_value(result, "query_route")
    if not isinstance(plan_route, dict):
        errors.append("plan_query_route_missing")
    else:
        if plan_route.get("schema") != QUERY_ROUTE_SCHEMA:
            errors.append("plan_query_route_schema_invalid")
        if plan_route.get("tool") != "plan_ncs_education_path":
            errors.append("plan_query_route_tool_invalid")
        if not isinstance(plan_route.get("expected_tool_chain"), list):
            errors.append("plan_expected_tool_chain_missing")
        if not isinstance(plan_route.get("route_contract"), dict):
            errors.append("plan_route_contract_missing")
        if not plan_route.get("route_fingerprint"):
            errors.append("plan_route_fingerprint_missing")

    if _payload_value(result, "missing_query_route_fields") != []:
        errors.append("plan_missing_query_route_fields_not_empty")

    path_rows = _payload_value(result, "recommended_path")
    if not isinstance(path_rows, list) or not path_rows:
        errors.append("recommended_path_missing_or_empty")
    else:
        roles = {
            str(row.get("role"))
            for row in path_rows
            if isinstance(row, dict) and row.get("role")
        }
        for required_role in (
            "scope_confirmation",
            "core_gap_training",
            "supporting_or_adjacent_training",
        ):
            if required_role not in roles:
                errors.append(f"recommended_path_role_missing:{required_role}")

    matrix = _payload_value(result, "training_system_matrix")
    if not isinstance(matrix, list) or not matrix:
        errors.append("training_system_matrix_missing_or_empty")
    else:
        for index, row in enumerate(matrix):
            if not isinstance(row, dict):
                errors.append(f"training_system_matrix_row_invalid:{index}")
                continue
            missing_fields = sorted(PLAN_MATRIX_FIELDS - set(row))
            for field in missing_fields:
                errors.append(f"training_system_matrix_field_missing:{index}:{field}")
            course_fit = row.get("course_fit")
            if not isinstance(course_fit, dict):
                errors.append(f"training_system_matrix_course_fit_invalid:{index}")
            else:
                for field in sorted(COURSE_FIT_FIELDS - set(course_fit)):
                    errors.append(f"training_system_matrix_course_fit_field_missing:{index}:{field}")

    guide_trace = _payload_value(result, "training_system_guide_trace")
    if not isinstance(guide_trace, dict):
        errors.append("training_system_guide_trace_missing")
    else:
        if guide_trace.get("schema") != GUIDE_TRACE_SCHEMA:
            errors.append("training_system_guide_trace_schema_invalid")
        check_codes = {
            str(item.get("check") or item.get("code"))
            for item in guide_trace.get("checks", [])
            if isinstance(item, dict)
        }
        for code in sorted(GUIDE_CHECKS - check_codes):
            errors.append(f"training_system_guide_check_missing:{code}")
    return errors


def build_scenarios(
    *,
    current_query: str,
    target_query: str,
    task_query: str,
    search_query: str,
    limit: int,
) -> list[BenchmarkScenario]:
    return [
        BenchmarkScenario(
            scenario_id="structure_search",
            route_query=f"{search_query} NCS search",
            expected_route="structure_search",
            tool_name="ncs_search",
            params={"query": search_query, "scope": "unit", "limit": limit},
            validator=_validate_search_result,
        ),
        BenchmarkScenario(
            scenario_id="task_training",
            route_query=f"{task_query} competency unit training course",
            expected_route="task_training",
            tool_name="recommend_training_for_task",
            params={
                "query": task_query,
                "limit": limit,
                "compact": True,
                "save": False,
            },
            validator=_validate_task_result,
        ),
        BenchmarkScenario(
            scenario_id="training_transition",
            route_query=f"from {current_query} to {target_query} reskilling path",
            expected_route="training_transition",
            tool_name="recommend_training_transition",
            params={
                "current_query": current_query,
                "target_query": target_query,
                "limit": limit,
                "compact": True,
                "save": False,
            },
            validator=_validate_transition_result,
        ),
        BenchmarkScenario(
            scenario_id="education_system_design",
            route_query=f"from {current_query} to {target_query} education system",
            expected_route="education_system_design",
            tool_name="plan_ncs_education_path",
            params={
                "current_query": current_query,
                "target_query": target_query,
                "limit": limit,
                "save": False,
            },
            validator=_validate_plan_result,
        ),
    ]


def _validate_route(route: Any, scenario: BenchmarkScenario) -> list[str]:
    if not isinstance(route, dict):
        return ["query_route_not_object"]
    errors: list[str] = []
    if route.get("schema") != QUERY_ROUTE_SCHEMA:
        errors.append("query_route_schema_invalid")
    if route.get("scenario") != scenario.expected_route:
        errors.append("query_route_scenario_mismatch")
    if route.get("tool") != scenario.tool_name:
        errors.append("query_route_tool_mismatch")
    if route.get("available") is not True:
        errors.append("query_route_tool_unavailable")
    if route.get("missing_params") != []:
        errors.append("query_route_missing_params")
    if not route.get("route_fingerprint"):
        errors.append("query_route_fingerprint_missing")
    expected_chain = route.get("expected_tool_chain")
    if not isinstance(expected_chain, list) or scenario.tool_name not in expected_chain:
        errors.append("query_route_expected_tool_chain_invalid")
    contract = route.get("route_contract")
    if not isinstance(contract, dict):
        errors.append("query_route_contract_missing")
    else:
        if contract.get("schema") != QUERY_ROUTE_SCHEMA:
            errors.append("query_route_contract_schema_invalid")
        if contract.get("primary_tool") != scenario.tool_name:
            errors.append("query_route_contract_primary_tool_mismatch")
    return errors


def _validate_common_result(
    result: Any,
    scenario: BenchmarkScenario,
    route: dict[str, Any],
) -> list[str]:
    if not isinstance(result, dict):
        return ["result_not_object"]
    errors: list[str] = []
    if result.get("ok") is not True:
        errors.append("result_ok_not_true")
    if result.get("error") not in (None, {}):
        errors.append("result_error_present")
    meta = result.get("meta_execution")
    if not isinstance(meta, dict):
        errors.append("meta_execution_missing")
        return errors
    if meta.get("tool_name") != scenario.tool_name:
        errors.append("meta_execution_tool_mismatch")
    if meta.get("route_contract_schema") != QUERY_ROUTE_SCHEMA:
        errors.append("meta_execution_route_schema_invalid")
    if meta.get("route_fingerprint") != route.get("route_fingerprint"):
        errors.append("meta_execution_route_fingerprint_mismatch")
    if meta.get("route_tool_allowed") is not True:
        errors.append("meta_execution_route_tool_not_allowed")
    if meta.get("route_tool_mismatch") is not False:
        errors.append("meta_execution_route_tool_mismatch")
    if scenario.tool_name in SAVE_FORCED_TOOLS and meta.get("save_forced_false") is not True:
        errors.append("meta_execution_save_not_forced_false")
    return errors


def _route_summary(route: Any) -> dict[str, Any]:
    if not isinstance(route, dict):
        return {}
    return {
        "schema": route.get("schema"),
        "scenario": route.get("scenario"),
        "tool": route.get("tool"),
        "available": route.get("available"),
        "missing_params": route.get("missing_params"),
        "expected_tool_chain": route.get("expected_tool_chain"),
        "route_fingerprint": route.get("route_fingerprint"),
        "risk_flag_codes": [
            item.get("code")
            for item in route.get("risk_flags", [])
            if isinstance(item, dict)
        ],
        "guard_flag_codes": [
            item.get("code")
            for item in route.get("guard_flags", [])
            if isinstance(item, dict)
        ],
    }


def _result_summary(result: Any, scenario: BenchmarkScenario) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    error = result.get("error")
    error_code = error.get("code") if isinstance(error, dict) else None
    count_key = "results" if scenario.tool_name == "ncs_search" else "recommended_courses"
    rows = _payload_value(result, count_key)
    capacity = _payload_value(result, "capacity")
    return {
        "ok": result.get("ok"),
        "view": _payload_value(result, "view"),
        "item_count": len(rows) if isinstance(rows, list) else None,
        "error_code": error_code,
        "capacity": capacity if isinstance(capacity, dict) else None,
    }


def _safe_exception_message(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ")
    return message[:500]


def _execute_scenario(server: Any, scenario: BenchmarkScenario) -> dict[str, Any]:
    started = time.perf_counter_ns()
    route: Any = None
    result: Any = None
    errors: list[str] = []
    exception: dict[str, str] | None = None
    try:
        route = server.route_ncs_query(
            scenario.route_query,
            available_tool_names=server.tool_registry.NCS_EXECUTABLE_TOOL_NAMES,
        )
        errors.extend(_validate_route(route, scenario))
        if not errors:
            params = dict(scenario.params)
            params["_route_query"] = scenario.route_query
            params["_route_fingerprint"] = route["route_fingerprint"]
            result = server.ncs_execute_tool(scenario.tool_name, params)
            errors.extend(_validate_common_result(result, scenario, route))
            if isinstance(result, dict):
                errors.extend(scenario.validator(result))
    except Exception as exc:  # pragma: no cover - exercised through failure report behavior
        errors.append("scenario_execution_exception")
        exception = {
            "type": type(exc).__name__,
            "message": _safe_exception_message(exc),
        }
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    return {
        "elapsed_ms": round(elapsed_ms, 3),
        "result_valid": not errors,
        "validation_errors": errors,
        "route": _route_summary(route),
        "result": _result_summary(result, scenario),
        "exception": exception,
    }


def _read_only_preflight(db_path: Path, server: Any) -> dict[str, Any]:
    from ncs_mcp.db import connect

    errors: list[str] = []
    conn = connect(db_path, read_only=True)
    try:
        query_only = bool(conn.execute("PRAGMA query_only").fetchone()[0])
        sqlite_version = str(conn.execute("SELECT sqlite_version()").fetchone()[0])
    finally:
        conn.close()
    if not query_only:
        errors.append("sqlite_query_only_not_enabled")

    settings = server.load_settings()
    configured_path = Path(settings.db_path).resolve()
    configured_read_only = bool(getattr(settings, "read_only_mode", False))
    if configured_path != db_path:
        errors.append("server_db_path_mismatch")
    if not configured_read_only:
        errors.append("server_read_only_mode_not_enabled")

    readiness = server.database_readiness_metadata(db_path)
    if readiness.get("ready") is not True:
        errors.append("database_readiness_check_failed")
    return {
        "ok": not errors,
        "errors": errors,
        "configured_db_path": str(configured_path),
        "configured_read_only_mode": configured_read_only,
        "sqlite_query_only": query_only,
        "sqlite_version": sqlite_version,
        "database_readiness": readiness,
    }


def run_benchmark(
    db_path: Path | str,
    *,
    iterations: int = 5,
    warmup_iterations: int = 1,
    concurrency: int = 1,
    limit: int = 3,
    current_query: str = "HR planning",
    target_query: str = "HR planning",
    task_query: str | None = None,
    search_query: str | None = None,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    if warmup_iterations < 0:
        raise ValueError("warmup_iterations must be at least 0")
    if concurrency < 1:
        raise ValueError("concurrency must be at least 1")
    if limit < 1:
        raise ValueError("limit must be at least 1")

    resolved_db_path = Path(db_path).expanduser().resolve()
    if not resolved_db_path.exists():
        raise FileNotFoundError(f"SQLite DB does not exist: {resolved_db_path}")
    if not resolved_db_path.is_file():
        raise ValueError(f"SQLite DB path is not a file: {resolved_db_path}")

    task_query = task_query or target_query
    search_query = search_query or target_query
    for name, value in {
        "current_query": current_query,
        "target_query": target_query,
        "task_query": task_query,
        "search_query": search_query,
    }.items():
        if not value.strip():
            raise ValueError(f"{name} must not be blank")

    before = snapshot_database(resolved_db_path)
    scenario_reports: list[dict[str, Any]] = []
    preflight: dict[str, Any]
    all_latencies: list[float] = []
    all_capacity_queue_waits: list[float] = []

    with _temporary_environment(
        {
            "NCS_DB_PATH": str(resolved_db_path),
            "NCS_MCP_READ_ONLY": "1",
            # This benchmark intentionally exercises the advanced transition
            # and education-planning facades.  Production may keep those tools
            # hidden, so opt in only for the bounded benchmark process.
            "NCS_MCP_ENABLE_ADVANCED_TOOLS": "1",
        }
    ):
        from ncs_mcp import server

        preflight = _read_only_preflight(resolved_db_path, server)
        scenarios = build_scenarios(
            current_query=current_query,
            target_query=target_query,
            task_query=task_query,
            search_query=search_query,
            limit=limit,
        )
        warmups_by_scenario: dict[str, list[dict[str, Any]]] = {}
        runs_by_scenario: dict[str, list[dict[str, Any]]] = {
            scenario.scenario_id: [] for scenario in scenarios
        }
        for scenario in scenarios:
            warmups_by_scenario[scenario.scenario_id] = [
                _execute_scenario(server, scenario)
                for _ in range(warmup_iterations)
            ]

        measured_started = time.perf_counter_ns()
        if concurrency == 1:
            for scenario in scenarios:
                for iteration in range(1, iterations + 1):
                    run = _execute_scenario(server, scenario)
                    run["iteration"] = iteration
                    runs_by_scenario[scenario.scenario_id].append(run)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
                futures = {
                    executor.submit(_execute_scenario, server, scenario): (
                        scenario.scenario_id,
                        iteration,
                    )
                    for scenario in scenarios
                    for iteration in range(1, iterations + 1)
                }
                for future in concurrent.futures.as_completed(futures):
                    scenario_id, iteration = futures[future]
                    run = future.result()
                    run["iteration"] = iteration
                    runs_by_scenario[scenario_id].append(run)
        measured_wall_ms = (time.perf_counter_ns() - measured_started) / 1_000_000

        for scenario in scenarios:
            warmups = warmups_by_scenario[scenario.scenario_id]
            runs = sorted(
                runs_by_scenario[scenario.scenario_id],
                key=lambda item: int(item["iteration"]),
            )
            for iteration in range(1, iterations + 1):
                all_latencies.append(float(runs[iteration - 1]["elapsed_ms"]))
            queue_waits = [
                float(run["result"]["capacity"]["queue_wait_ms"])
                for run in runs
                if isinstance(run.get("result"), dict)
                and isinstance(run["result"].get("capacity"), dict)
                and run["result"]["capacity"].get("queue_wait_ms") is not None
            ]
            all_capacity_queue_waits.extend(queue_waits)
            measured_valid = sum(1 for run in runs if run["result_valid"])
            warmup_valid = sum(1 for run in warmups if run["result_valid"])
            scenario_reports.append(
                {
                    "id": scenario.scenario_id,
                    "route_query": scenario.route_query,
                    "expected_route": scenario.expected_route,
                    "tool": scenario.tool_name,
                    "params": scenario.params,
                    "route": runs[0]["route"] if runs else {},
                    "valid": measured_valid == iterations and warmup_valid == warmup_iterations,
                    "result_validity": {
                        "measured_valid": measured_valid,
                        "measured_total": iterations,
                        "warmup_valid": warmup_valid,
                        "warmup_total": warmup_iterations,
                    },
                    "latency_ms": latency_summary(
                        [float(run["elapsed_ms"]) for run in runs]
                    ),
                    "capacity_queue_wait_ms": latency_summary(queue_waits),
                    "runs": runs,
                }
            )

    after = snapshot_database(resolved_db_path)
    immutability = compare_database_snapshots(before, after)
    total_runs = sum(len(item["runs"]) for item in scenario_reports)
    valid_runs = sum(
        1
        for item in scenario_reports
        for run in item["runs"]
        if run["result_valid"]
    )
    all_scenarios_valid = bool(scenario_reports) and all(
        item["valid"] for item in scenario_reports
    )
    ok = bool(preflight.get("ok")) and all_scenarios_valid and immutability["all_unchanged"]

    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "ok": ok,
        "readiness_status": "ready" if ok else "not_ready",
        "mutation_policy": "report_only",
        "status_update_allowed": False,
        "db_writes": False if immutability["all_unchanged"] else None,
        "approval_claim": False,
        "external_api_calls": False,
        "network_access_required": False,
        "human_status_changes_observed": False if immutability["all_unchanged"] else None,
        "configuration": {
            "iterations": iterations,
            "warmup_iterations": warmup_iterations,
            "concurrency": concurrency,
            "limit": limit,
            "current_query": current_query,
            "target_query": target_query,
            "task_query": task_query,
            "search_query": search_query,
        },
        "database": {
            "path": str(resolved_db_path),
            "before": before,
            "after": after,
            "immutability": immutability,
            "filesystem_mutation_observed": not immutability["all_unchanged"],
            "storage_content_unchanged": immutability["storage_content_unchanged"],
        },
        "read_only_preflight": preflight,
        "summary": {
            "scenario_count": len(scenario_reports),
            "valid_scenario_count": sum(1 for item in scenario_reports if item["valid"]),
            "total_measured_runs": total_runs,
            "valid_measured_runs": valid_runs,
            "invalid_measured_runs": total_runs - valid_runs,
            "result_validity_rate": round(valid_runs / total_runs, 6) if total_runs else 0.0,
            "latency_ms": latency_summary(all_latencies),
            "capacity_queue_wait_ms": latency_summary(all_capacity_queue_waits),
            "measured_wall_ms": round(measured_wall_ms, 3),
            "throughput_requests_per_second": (
                round(total_runs / (measured_wall_ms / 1000), 3)
                if measured_wall_ms > 0
                else None
            ),
        },
        "scenarios": scenario_reports,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark public NCS chatbot routes against an explicit SQLite DB "
            "without modifying the database."
        )
    )
    parser.add_argument("--db", required=True, help="Explicit SQLite DB path.")
    parser.add_argument("--out", help="Optional JSON report path. JSON is always printed to stdout.")
    parser.add_argument("--markdown-out", help="Optional concise Markdown report path.")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmup-iterations", type=int, default=1)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--current-query", default="HR planning")
    parser.add_argument("--target-query", default="HR planning")
    parser.add_argument("--task-query")
    parser.add_argument("--search-query")
    return parser.parse_args(argv)


def write_markdown(report: dict[str, Any], path: Path) -> None:
    summary = report.get("summary") or {}
    immutability = (report.get("database") or {}).get("immutability") or {}
    latency = summary.get("latency_ms") or {}
    lines = [
        "# Institutional Chatbot Readiness Benchmark",
        "",
        f"- ok: `{str(report.get('ok')).lower()}`",
        f"- readiness_status: `{report.get('readiness_status')}`",
        f"- scenario_count: `{summary.get('scenario_count')}`",
        f"- concurrency: `{(report.get('configuration') or {}).get('concurrency')}`",
        f"- result_validity_rate: `{summary.get('result_validity_rate')}`",
        f"- latency_p50_ms: `{latency.get('p50')}`",
        f"- latency_p95_ms: `{latency.get('p95')}`",
        f"- latency_max_ms: `{latency.get('max')}`",
        f"- throughput_requests_per_second: `{summary.get('throughput_requests_per_second')}`",
        f"- database_unchanged: `{str(immutability.get('all_unchanged')).lower()}`",
        f"- database_base_unchanged: `{str(immutability.get('base_unchanged')).lower()}`",
        f"- database_sidecars_unchanged: `{str(immutability.get('sidecars_unchanged')).lower()}`",
        f"- database_changed_sidecars: `{immutability.get('changed_sidecars') or []}`",
        f"- external_api_calls: `{str(report.get('external_api_calls')).lower()}`",
        "",
        "## Scenarios",
        "",
        "| Scenario | Tool | Valid | p50 (ms) | p95 (ms) | Max (ms) |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for scenario in report.get("scenarios") or []:
        scenario_latency = scenario.get("latency_ms") or {}
        lines.append(
            f"| {scenario.get('id')} | `{scenario.get('tool')}` | "
            f"{str(scenario.get('valid')).lower()} | {scenario_latency.get('p50')} | "
            f"{scenario_latency.get('p95')} | {scenario_latency.get('max')} |"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = Path(args.db).expanduser().resolve()
    if args.out and Path(args.out).expanduser().resolve() == db_path:
        raise ValueError("The report output path must not be the SQLite DB path.")
    report = run_benchmark(
        db_path,
        iterations=args.iterations,
        warmup_iterations=args.warmup_iterations,
        concurrency=args.concurrency,
        limit=args.limit,
        current_query=args.current_query,
        target_query=args.target_query,
        task_query=args.task_query,
        search_query=args.search_query,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        output_path = Path(args.out).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered, encoding="utf-8")
    if args.markdown_out:
        write_markdown(report, Path(args.markdown_out).expanduser().resolve())
    print(rendered, end="")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
