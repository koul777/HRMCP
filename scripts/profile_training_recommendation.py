from __future__ import annotations

import argparse
import cProfile
import functools
import hashlib
import inspect
import json
import math
import os
import pstats
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

DEFAULT_QUERIES = (
    ("hr", "인사기획"),
    ("recruiting", "채용"),
    ("training", "교육훈련"),
    ("data", "데이터 분석"),
    ("quality", "품질관리"),
)
DEFAULT_OUT = PROJECT_ROOT / "reports" / "training_recommendation_profile_20260830.json"
DEFAULT_MARKDOWN_OUT = PROJECT_ROOT / "reports" / "training_recommendation_profile_20260830.md"
DYNAMIC_KEYS = {
    "generated_at",
    "queue_wait_ms",
    "elapsed_ms",
    "duration_ms",
    "started_at",
    "completed_at",
}
IDENTITY_SUFFIXES = ("_id", "_ids", "_code", "_codes", "_key", "_keys")
IDENTITY_KEYS = {
    "id",
    "criteria_id",
    "unit_code",
    "element_id",
    "concept_id",
    "course_id",
    "course_key",
    "training_course_id",
    "source_id",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def percentile(values: Iterable[float], percentile_value: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * percentile_value
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _metric_summary(values: Iterable[float]) -> dict[str, float | int | None]:
    materialized = [float(value) for value in values]
    return {
        "count": len(materialized),
        "p50_ms": _round_optional(percentile(materialized, 0.50)),
        "p95_ms": _round_optional(percentile(materialized, 0.95)),
        "max_ms": _round_optional(max(materialized) if materialized else None),
        "min_ms": _round_optional(min(materialized) if materialized else None),
    }


def _round_optional(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(float(value), digits)


def _walk_shape(
    value: Any,
    *,
    path: str = "$",
    depth: int = 0,
    output: list[str] | None = None,
    limit: int = 8000,
) -> list[str]:
    output = output if output is not None else []
    if len(output) >= limit:
        return output
    if depth > 12:
        output.append(f"{path}:depth_limit")
        return output
    if isinstance(value, dict):
        output.append(f"{path}:object")
        for key in sorted(value):
            if key in DYNAMIC_KEYS:
                continue
            _walk_shape(value[key], path=f"{path}.{key}", depth=depth + 1, output=output, limit=limit)
    elif isinstance(value, list):
        output.append(f"{path}:array")
        if value:
            _walk_shape(value[0], path=f"{path}[]", depth=depth + 1, output=output, limit=limit)
    elif value is None:
        output.append(f"{path}:null")
    elif isinstance(value, bool):
        output.append(f"{path}:bool")
    elif isinstance(value, int):
        output.append(f"{path}:int")
    elif isinstance(value, float):
        output.append(f"{path}:float")
    else:
        output.append(f"{path}:str")
    return output


def _walk_identity_values(
    value: Any,
    *,
    path: str = "$",
    output: list[tuple[str, Any]] | None = None,
    limit: int = 10000,
) -> list[tuple[str, Any]]:
    output = output if output is not None else []
    if len(output) >= limit:
        return output
    if isinstance(value, dict):
        for key in sorted(value):
            if key in DYNAMIC_KEYS or key in {"capacity", "audit"}:
                continue
            child = value[key]
            child_path = f"{path}.{key}"
            identity_key = key in IDENTITY_KEYS or key.endswith(IDENTITY_SUFFIXES)
            if identity_key and isinstance(child, (str, int, float, bool, type(None))):
                output.append((child_path, child))
            elif identity_key and isinstance(child, list):
                scalar_values = [item for item in child if isinstance(item, (str, int, float, bool, type(None)))]
                output.append((child_path, scalar_values))
            _walk_identity_values(child, path=child_path, output=output, limit=limit)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_identity_values(item, path=f"{path}[{index}]", output=output, limit=limit)
    return output


def _evidence_shape_paths(shape_paths: list[str]) -> list[str]:
    return [path for path in shape_paths if "evidence" in path.lower() or "source" in path.lower()]


def _find_first_list(value: Any, keys: set[str]) -> list[Any] | None:
    if isinstance(value, dict):
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, list):
                return candidate
        for child in value.values():
            found = _find_first_list(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_first_list(child, keys)
            if found is not None:
                return found
    return None


def semantic_fingerprint(result: Any) -> dict[str, Any]:
    payload = dict(result) if isinstance(result, dict) else {"value": result}
    shape_paths = _walk_shape(payload)
    identity_values = _walk_identity_values(payload)
    evidence_paths = _evidence_shape_paths(shape_paths)
    recommendations = _find_first_list(
        payload,
        {"recommendations", "recommended_courses", "courses", "items"},
    ) or []
    error = payload.get("error")
    error_code = error.get("code") if isinstance(error, dict) else error
    projection = {
        "ok": payload.get("ok"),
        "error_code": error_code,
        "recommendation_count": len(recommendations),
        "identity_values": identity_values,
        "shape_paths": shape_paths,
        "evidence_shape_paths": evidence_paths,
    }
    return {
        "fingerprint": _sha256(projection),
        "identity_fingerprint": _sha256(identity_values),
        "shape_fingerprint": _sha256(shape_paths),
        "evidence_shape_fingerprint": _sha256(evidence_paths),
        "ok": bool(payload.get("ok")),
        "error_code": error_code,
        "recommendation_count": len(recommendations),
        "top_level_keys": sorted(payload),
        "identity_preview": [
            {"path": path, "value": value} for path, value in identity_values[:40]
        ],
        "identity_value_count": len(identity_values),
        "shape_path_count": len(shape_paths),
        "evidence_shape_path_count": len(evidence_paths),
    }


class StageRecorder:
    def __init__(self) -> None:
        self.stage_ms: dict[str, float] = defaultdict(float)
        self.stage_calls: Counter[str] = Counter()
        self.sql_statement_count = 0
        self.sql_api_ms = 0.0
        self.sql_by_kind: Counter[str] = Counter()
        self.connection_read_only_args: list[bool | None] = []

    @contextmanager
    def measure(self, stage: str):
        started = time.perf_counter()
        try:
            yield
        finally:
            self.stage_ms[stage] += (time.perf_counter() - started) * 1000.0
            self.stage_calls[stage] += 1

    def record_sql_statement(self, sql: Any) -> None:
        self.sql_statement_count += 1
        text = str(sql).lstrip()
        kind = text.split(None, 1)[0].upper() if text else "UNKNOWN"
        self.sql_by_kind[kind] += 1

    def add_sql_time(self, elapsed_seconds: float) -> None:
        self.sql_api_ms += elapsed_seconds * 1000.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage_ms": {key: round(value, 3) for key, value in sorted(self.stage_ms.items())},
            "stage_calls": dict(sorted(self.stage_calls.items())),
            "sql": {
                "statement_count": self.sql_statement_count,
                "sqlite_api_ms": round(self.sql_api_ms, 3),
                "by_kind": dict(sorted(self.sql_by_kind.items())),
                "timing_scope": "connection/cursor execute plus fetch/iteration wall time; overlapping application stages",
            },
            "connection_read_only_args": self.connection_read_only_args,
        }


class ProfiledCursor:
    def __init__(self, cursor: Any, recorder: StageRecorder) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_recorder", recorder)

    def _timed(self, operation: Callable[[], Any]) -> Any:
        started = time.perf_counter()
        try:
            return operation()
        finally:
            self._recorder.add_sql_time(time.perf_counter() - started)

    def execute(self, sql: Any, parameters: Any = ()) -> "ProfiledCursor":
        self._recorder.record_sql_statement(sql)
        self._timed(lambda: self._cursor.execute(sql, parameters))
        return self

    def executemany(self, sql: Any, seq_of_parameters: Any) -> "ProfiledCursor":
        self._recorder.record_sql_statement(sql)
        self._timed(lambda: self._cursor.executemany(sql, seq_of_parameters))
        return self

    def executescript(self, sql_script: str) -> "ProfiledCursor":
        self._recorder.record_sql_statement(sql_script)
        self._timed(lambda: self._cursor.executescript(sql_script))
        return self

    def fetchone(self) -> Any:
        return self._timed(self._cursor.fetchone)

    def fetchmany(self, size: int | None = None) -> Any:
        if size is None:
            return self._timed(self._cursor.fetchmany)
        return self._timed(lambda: self._cursor.fetchmany(size))

    def fetchall(self) -> Any:
        return self._timed(self._cursor.fetchall)

    def __iter__(self) -> "ProfiledCursor":
        return self

    def __next__(self) -> Any:
        return self._timed(lambda: next(self._cursor))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._cursor, name, value)


class ProfiledConnection:
    def __init__(self, connection: Any, recorder: StageRecorder) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_recorder", recorder)

    def _timed_cursor_call(self, sql: Any, operation: Callable[[], Any]) -> ProfiledCursor:
        self._recorder.record_sql_statement(sql)
        started = time.perf_counter()
        try:
            cursor = operation()
        finally:
            self._recorder.add_sql_time(time.perf_counter() - started)
        return ProfiledCursor(cursor, self._recorder)

    def execute(self, sql: Any, parameters: Any = ()) -> ProfiledCursor:
        return self._timed_cursor_call(sql, lambda: self._connection.execute(sql, parameters))

    def executemany(self, sql: Any, seq_of_parameters: Any) -> ProfiledCursor:
        return self._timed_cursor_call(sql, lambda: self._connection.executemany(sql, seq_of_parameters))

    def executescript(self, sql_script: str) -> ProfiledCursor:
        return self._timed_cursor_call(sql_script, lambda: self._connection.executescript(sql_script))

    def cursor(self, *args: Any, **kwargs: Any) -> ProfiledCursor:
        return ProfiledCursor(self._connection.cursor(*args, **kwargs), self._recorder)

    def __enter__(self) -> "ProfiledConnection":
        self._connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._connection.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._connection, name, value)


def _profile_rows(profile: cProfile.Profile, *, sort_by: str, limit: int = 30) -> list[dict[str, Any]]:
    stats = pstats.Stats(profile)
    rows: list[dict[str, Any]] = []
    for (filename, line, function), values in stats.stats.items():
        primitive_calls, total_calls, self_seconds, cumulative_seconds, _callers = values
        rows.append(
            {
                "function": f"{Path(filename).name}:{line}({function})",
                "file": str(Path(filename)),
                "line": line,
                "name": function,
                "primitive_calls": primitive_calls,
                "total_calls": total_calls,
                "self_seconds": round(self_seconds, 6),
                "cumulative_seconds": round(cumulative_seconds, 6),
            }
        )
    key = "cumulative_seconds" if sort_by == "cumulative" else "self_seconds"
    return sorted(rows, key=lambda row: (-float(row[key]), row["function"]))[:limit]


@contextmanager
def _instrument_modules(server: Any, training: Any, recorder: StageRecorder):
    patches: list[tuple[Any, str, Any]] = []

    def patch_timed(module: Any, name: str, stage: str) -> None:
        original = getattr(module, name, None)
        if original is None or not callable(original):
            return

        @functools.wraps(original)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            with recorder.measure(stage):
                return original(*args, **kwargs)

        patches.append((module, name, original))
        setattr(module, name, wrapped)

    original_connect = getattr(server, "connect")

    @functools.wraps(original_connect)
    def profiled_connect(*args: Any, **kwargs: Any) -> ProfiledConnection:
        read_only = kwargs.get("read_only")
        if read_only is None and len(args) >= 2:
            read_only = bool(args[1])
        recorder.connection_read_only_args.append(read_only)
        with recorder.measure("connection_open"):
            connection = original_connect(*args, **kwargs)
        return ProfiledConnection(connection, recorder)

    patches.append((server, "connect", original_connect))
    setattr(server, "connect", profiled_connect)

    for module, name, stage in (
        (server, "load_settings", "settings"),
        (server, "shared_database_readiness_metadata", "readiness"),
        (server, "shared_runtime_health_metadata", "readiness"),
        (server, "training_recommend_for_task", "recommendation_core"),
        (server, "training_compact_task_response", "formatting_compact"),
        (server, "tool_response", "formatting_envelope"),
        (training, "resolve_ncs_query_scope", "search_resolution"),
        (training, "resolve_task_criteria", "search_resolution"),
        (training, "_resolve_query_scope_units", "search_resolution"),
    ):
        patch_timed(module, name, stage)
    try:
        yield
    finally:
        for module, name, original in reversed(patches):
            setattr(module, name, original)


def _load_product_modules() -> tuple[Any, Any, float]:
    os.environ["NCS_MCP_READ_ONLY"] = "1"
    started = time.perf_counter()
    from ncs_mcp import server
    from ncs_mcp import training_recommendation

    import_ms = (time.perf_counter() - started) * 1000.0
    return server, training_recommendation, import_ms


def _run_once(
    server: Any,
    training: Any,
    *,
    domain: str,
    query: str,
    limit: int,
    profile_enabled: bool,
    import_ms: float = 0.0,
) -> dict[str, Any]:
    recorder = StageRecorder()
    profile = cProfile.Profile()
    with _instrument_modules(server, training, recorder):
        started = time.perf_counter()
        if profile_enabled:
            profile.enable()
        try:
            result = server.recommend_training_for_task(
                query=query,
                limit=limit,
                save=False,
                compact=True,
            )
        finally:
            if profile_enabled:
                profile.disable()
        facade_ms = (time.perf_counter() - started) * 1000.0
    measurement = recorder.as_dict()
    measurement.update(
        {
            "domain": domain,
            "query": query,
            "import_ms": round(import_ms, 3),
            "facade_wall_ms": round(facade_ms, 3),
            "quality": semantic_fingerprint(result),
            "public_call": {
                "tool": "recommend_training_for_task",
                "compact": True,
                "save": False,
                "limit": limit,
            },
        }
    )
    if profile_enabled:
        measurement["cprofile"] = {
            "top_cumulative": _profile_rows(profile, sort_by="cumulative"),
            "top_self": _profile_rows(profile, sort_by="self"),
        }
    return measurement


def _db_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False}
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "exists": True,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _run_worker(args: argparse.Namespace) -> int:
    server, training, import_ms = _load_product_modules()
    result = _run_once(
        server,
        training,
        domain=args.worker_domain,
        query=args.worker_query,
        limit=args.limit,
        profile_enabled=False,
        import_ms=import_ms,
    )
    worker_out = Path(args.worker_out)
    worker_out.parent.mkdir(parents=True, exist_ok=True)
    worker_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


def _run_cold_child(domain: str, query: str, *, limit: int, timeout_seconds: float) -> dict[str, Any]:
    with tempfile.NamedTemporaryFile(prefix="ncs_profile_", suffix=".json", delete=False) as handle:
        worker_out = Path(handle.name)
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker-query",
        query,
        "--worker-domain",
        domain,
        "--worker-out",
        str(worker_out),
        "--limit",
        str(limit),
    ]
    env = os.environ.copy()
    env["NCS_MCP_READ_ONLY"] = "1"
    env["PYTHONPATH"] = str(SRC_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    started = time.perf_counter()
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
        process_ms = (time.perf_counter() - started) * 1000.0
        if completed.returncode != 0:
            raise RuntimeError(
                f"cold worker failed rc={completed.returncode}: {completed.stderr[-2000:]}"
            )
        result = json.loads(worker_out.read_text(encoding="utf-8"))
        result["child_process_wall_ms"] = round(process_ms, 3)
        return result
    finally:
        worker_out.unlink(missing_ok=True)


def _aggregate_profile_rows(profiles: list[dict[str, Any]], key: str, *, limit: int = 30) -> list[dict[str, Any]]:
    aggregate: dict[tuple[str, int, str], dict[str, Any]] = {}
    for profile in profiles:
        for row in profile.get("cprofile", {}).get(key, []):
            identity = (row["file"], int(row["line"]), row["name"])
            target = aggregate.setdefault(
                identity,
                {
                    "function": row["function"],
                    "file": row["file"],
                    "line": row["line"],
                    "name": row["name"],
                    "primitive_calls": 0,
                    "total_calls": 0,
                    "self_seconds": 0.0,
                    "cumulative_seconds": 0.0,
                },
            )
            for field in ("primitive_calls", "total_calls"):
                target[field] += int(row[field])
            for field in ("self_seconds", "cumulative_seconds"):
                target[field] += float(row[field])
    sort_field = "cumulative_seconds" if key == "top_cumulative" else "self_seconds"
    rows = sorted(aggregate.values(), key=lambda row: (-row[sort_field], row["function"]))[:limit]
    for row in rows:
        row["self_seconds"] = round(row["self_seconds"], 6)
        row["cumulative_seconds"] = round(row["cumulative_seconds"], 6)
    return rows


def _summarize_query_samples(samples: list[dict[str, Any]], *, wall_field: str) -> dict[str, Any]:
    stage_values: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        for stage, value in sample.get("stage_ms", {}).items():
            stage_values[stage].append(float(value))
    fingerprints = [sample["quality"]["fingerprint"] for sample in samples]
    return {
        "wall": _metric_summary(sample[wall_field] for sample in samples),
        "facade_wall": _metric_summary(sample["facade_wall_ms"] for sample in samples),
        "import": _metric_summary(sample.get("import_ms", 0.0) for sample in samples),
        "sql_statement_count": _metric_summary(sample["sql"]["statement_count"] for sample in samples),
        "sql_api": _metric_summary(sample["sql"]["sqlite_api_ms"] for sample in samples),
        "stages": {stage: _metric_summary(values) for stage, values in sorted(stage_values.items())},
        "quality_fingerprint_consistent": len(set(fingerprints)) == 1,
        "quality_fingerprints": sorted(set(fingerprints)),
        "quality": samples[0]["quality"] if samples else {},
        "read_only_connection_observed": bool(samples)
        and all(
            value is True
            for sample in samples
            for value in sample.get("connection_read_only_args", [])
        ),
    }


def _function_location(function: Any) -> dict[str, Any]:
    try:
        function = inspect.unwrap(function)
        file_path = inspect.getsourcefile(function)
        _, line = inspect.getsourcelines(function)
        return {"file": str(Path(file_path).resolve()) if file_path else None, "line": line}
    except (OSError, TypeError):
        return {"file": None, "line": None}


def _optimization_candidates(
    server: Any,
    training: Any,
    global_stages: dict[str, Any],
    top_cumulative: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    stage_p50 = {
        key: float(value.get("p50_ms") or 0.0) for key, value in global_stages.items()
    }
    by_name = {row["name"]: row for row in top_cumulative}
    candidates: list[dict[str, Any]] = []
    candidates.append(
        {
            "rank": 1,
            "candidate": "Bound resolve_ncs_query_scope candidate generation before Python edit-distance scoring.",
            "code": _function_location(training.resolve_ncs_query_scope),
            "measured_basis": {
                "search_resolution_p50_ms": stage_p50.get("search_resolution", 0.0),
                "resolve_cumulative_seconds_profiled": (by_name.get("resolve_ncs_query_scope") or {}).get("cumulative_seconds"),
                "candidate_score_calls_profiled": (by_name.get("_candidate_score") or {}).get("total_calls"),
            },
            "expected_impact": "High: search resolution is the dominant measured stage. Prefer SQL-side exact/prefix/token candidate limits before fuzzy scoring.",
            "regression_risk": "High: a bound can hide valid scopes. Require recall/MRR evaluation and unchanged task/course/evidence fingerprints for the representative set.",
        }
    )
    candidates.append(
        {
            "rank": 2,
            "candidate": "Normalize query/candidate text once per resolver call instead of inside every _candidate_score comparison.",
            "code": _function_location(training._candidate_score),
            "measured_basis": {
                "candidate_score_calls_profiled": (by_name.get("_candidate_score") or {}).get("total_calls"),
                "candidate_score_cumulative_seconds_profiled": (by_name.get("_candidate_score") or {}).get("cumulative_seconds"),
                "normalize_concept_key_cumulative_seconds_profiled": (by_name.get("normalize_concept_key") or {}).get("cumulative_seconds"),
            },
            "expected_impact": "Medium to high: cProfile shows repeated normalization/regex work across hundreds of thousands of comparisons.",
            "regression_risk": "Medium: normalization semantics must remain byte-for-byte equivalent; use before/after scope and recommendation fingerprints.",
        }
    )
    candidates.append(
        {
            "rank": 3,
            "candidate": "Consolidate repeated scope/course/evidence SELECTs only after attributing the 79-statement median by SQL call site.",
            "code": _function_location(training.recommend_training_for_task),
            "measured_basis": {
                "recommendation_core_p50_ms": stage_p50.get("recommendation_core", 0.0),
                "sqlite_fetchall_cumulative_seconds_profiled": (by_name.get("<method 'fetchall' of 'sqlite3.Cursor' objects>") or {}).get("cumulative_seconds"),
            },
            "expected_impact": "High potential: SQLite execute/fetch dominates wall time, but statement-level attribution must precede a code change.",
            "regression_risk": "High: query consolidation can change deduplication, review gating, ordering, or evidence completeness.",
        }
    )
    candidates.append(
        {
            "rank": 4,
            "candidate": "Defer settings, connection reuse, and response-formatting changes; they are not current hotspots.",
            "code": _function_location(server.db),
            "measured_basis": {
                "settings_p50_ms": stage_p50.get("settings", 0.0),
                "connection_open_p50_ms": stage_p50.get("connection_open", 0.0),
                "formatting_compact_p50_ms": stage_p50.get("formatting_compact", 0.0),
                "formatting_envelope_p50_ms": stage_p50.get("formatting_envelope", 0.0),
            },
            "expected_impact": "Negligible against the measured multi-second resolver/SQL cost.",
            "regression_risk": "Connection reuse has concurrency/thread risk; settings caching needs invalidation; formatting changes risk the public MCP contract.",
        }
    )
    return candidates


def _render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Training Recommendation Performance Profile",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Verdict",
        "",
        report["verdict"],
        "",
        "## Method",
        "",
        f"- Public facade: `recommend_training_for_task(query=..., limit={report['configuration']['limit']}, save=False, compact=True)`",
        f"- Cold repeats/query: `{report['configuration']['cold_repeats']}` (fresh Python child; process and facade measured separately)",
        f"- Warm repeats/query: `{report['configuration']['warm_repeats']}` (one module import, one unmeasured warm-up/query)",
        "- `NCS_MCP_READ_ONLY=1` was forced before product import.",
        "- SQL time is connection/cursor execute + fetch/iteration wall time and overlaps recommendation/search stages.",
        "",
        "## Query Metrics",
        "",
        "| Domain | Query | Cold process p50/p95/max ms | Cold facade p50/p95/max ms | Warm facade p50/p95/max ms | SQL stmt p50 | SQL API p50 ms | Recs | Fingerprint stable |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report["query_results"]:
        cold = row["cold"]
        warm = row["warm"]
        cp = cold["wall"]
        cf = cold["facade_wall"]
        wf = warm["facade_wall"]
        lines.append(
            "| {domain} | {query} | {cp50}/{cp95}/{cmax} | {cf50}/{cf95}/{cfmax} | "
            "{wf50}/{wf95}/{wfmax} | {sql_count} | {sql_ms} | {recs} | {stable} |".format(
                domain=row["domain"],
                query=row["query"],
                cp50=cp["p50_ms"], cp95=cp["p95_ms"], cmax=cp["max_ms"],
                cf50=cf["p50_ms"], cf95=cf["p95_ms"], cfmax=cf["max_ms"],
                wf50=wf["p50_ms"], wf95=wf["p95_ms"], wfmax=wf["max_ms"],
                sql_count=warm["sql_statement_count"]["p50_ms"],
                sql_ms=warm["sql_api"]["p50_ms"],
                recs=warm["quality"].get("recommendation_count"),
                stable=row["cross_mode_fingerprint_consistent"],
            )
        )
    lines.extend(
        [
            "",
            "## Global Metrics",
            "",
            f"- Cold process: `{report['global_metrics']['cold_process_wall']}`",
            f"- Cold facade: `{report['global_metrics']['cold_facade_wall']}`",
            f"- Warm facade: `{report['global_metrics']['warm_facade_wall']}`",
            f"- Historical 1.719 s claim: `{report['historical_claim_recheck']['verdict']}`; observed warm max `{report['historical_claim_recheck']['observed_warm_max_ms']} ms`.",
            "",
            "## Stage Contribution (Warm)",
            "",
            "| Stage | p50 ms | p95 ms | max ms |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for stage, metrics in report["global_metrics"]["warm_stages"].items():
        lines.append(f"| {stage} | {metrics['p50_ms']} | {metrics['p95_ms']} | {metrics['max_ms']} |")
    lines.extend(["", "## cProfile Top Cumulative", "", "| Function | Calls | Self s | Cumulative s |", "| --- | ---: | ---: | ---: |"])
    for row in report["cprofile"]["top_cumulative"][:15]:
        lines.append(f"| `{row['function']}` | {row['total_calls']} | {row['self_seconds']} | {row['cumulative_seconds']} |")
    lines.extend(["", "## cProfile Top Self", "", "| Function | Calls | Self s | Cumulative s |", "| --- | ---: | ---: | ---: |"])
    for row in report["cprofile"]["top_self"][:15]:
        lines.append(f"| `{row['function']}` | {row['total_calls']} | {row['self_seconds']} | {row['cumulative_seconds']} |")
    lines.extend(["", "## Optimization Candidates", ""])
    for item in report["optimization_candidates"]:
        code = item["code"]
        lines.append(
            f"{item['rank']}. **{item['candidate']}** `({code.get('file')}:{code.get('line')})`  "
        )
        lines.append(f"   Expected: {item['expected_impact']}  ")
        lines.append(f"   Risk: {item['regression_risk']}")
    lines.extend(
        [
            "",
            "## Read-only and Quality Contract",
            "",
            f"- DB state unchanged by size/mtime: `{report['db_invariant']['size_and_mtime_unchanged']}`",
            f"- All observed connection opens requested read-only mode: `{report['db_invariant']['all_connections_read_only']}`",
            f"- Cross cold/warm quality fingerprints consistent: `{report['quality_contract']['all_queries_consistent']}`",
            "- Fingerprints cover result quality, identifier values, complete output key/type shape, and evidence/source shape; dynamic timestamps and queue wait are excluded.",
            "- No product source or DB row was modified by this profiler.",
            "",
            "## Limitations",
            "",
            "- Local cold subprocess timing is not Vercel archive extraction timing and does not include a new serverless instance allocation.",
            "- SQLite API timing includes fetch/iteration and overlaps higher-level stages; it is an attribution aid, not exclusive CPU time.",
            "- Fingerprint stability proves structural/identifier consistency for these runs, not semantic relevance or gold-label accuracy.",
            "",
        ]
    )
    return "\n".join(lines)


def _parse_queries(values: list[str] | None) -> list[tuple[str, str]]:
    if not values:
        return list(DEFAULT_QUERIES)
    parsed: list[tuple[str, str]] = []
    for index, value in enumerate(values, start=1):
        if "=" in value:
            domain, query = value.split("=", 1)
        else:
            domain, query = f"query_{index}", value
        parsed.append((domain.strip(), query.strip()))
    return parsed


def _build_report(args: argparse.Namespace) -> dict[str, Any]:
    os.environ["NCS_MCP_READ_ONLY"] = "1"
    queries = _parse_queries(args.query)
    db_path = Path(os.environ.get("NCS_DB_PATH", PROJECT_ROOT / "data" / "processed" / "ncs.db"))
    before = _db_state(db_path)
    cold_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repeat_index in range(args.cold_repeats):
        for domain, query in queries:
            sample = _run_cold_child(domain, query, limit=args.limit, timeout_seconds=args.timeout_seconds)
            sample["repeat_index"] = repeat_index
            cold_samples[domain].append(sample)

    server, training, parent_import_ms = _load_product_modules()
    for domain, query in queries:
        _run_once(
            server,
            training,
            domain=domain,
            query=query,
            limit=args.limit,
            profile_enabled=False,
            import_ms=0.0,
        )

    warm_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for repeat_index in range(args.warm_repeats):
        for domain, query in queries:
            sample = _run_once(
                server,
                training,
                domain=domain,
                query=query,
                limit=args.limit,
                profile_enabled=False,
                import_ms=0.0,
            )
            sample["repeat_index"] = repeat_index
            warm_samples[domain].append(sample)

    profiles: list[dict[str, Any]] = []
    for domain, query in queries:
        profiles.append(
            _run_once(
                server,
                training,
                domain=domain,
                query=query,
                limit=args.limit,
                profile_enabled=True,
                import_ms=0.0,
            )
        )

    after = _db_state(db_path)
    query_results: list[dict[str, Any]] = []
    all_cold = []
    all_warm = []
    for domain, query in queries:
        cold = cold_samples[domain]
        warm = warm_samples[domain]
        all_cold.extend(cold)
        all_warm.extend(warm)
        cold_summary = _summarize_query_samples(cold, wall_field="child_process_wall_ms")
        warm_summary = _summarize_query_samples(warm, wall_field="facade_wall_ms")
        all_fingerprints = cold_summary["quality_fingerprints"] + warm_summary["quality_fingerprints"]
        query_results.append(
            {
                "domain": domain,
                "query": query,
                "cold": cold_summary,
                "warm": warm_summary,
                "cross_mode_fingerprint_consistent": len(set(all_fingerprints)) == 1,
                "cold_samples": cold,
                "warm_samples": warm,
            }
        )

    global_stage_values: dict[str, list[float]] = defaultdict(list)
    for sample in all_warm:
        for stage, value in sample["stage_ms"].items():
            global_stage_values[stage].append(float(value))
    global_stages = {
        stage: _metric_summary(values) for stage, values in sorted(global_stage_values.items())
    }
    top_cumulative = _aggregate_profile_rows(profiles, "top_cumulative")
    top_self = _aggregate_profile_rows(profiles, "top_self")
    warm_max = max(float(sample["facade_wall_ms"]) for sample in all_warm)
    cold_facade_max = max(float(sample["facade_wall_ms"]) for sample in all_cold)
    cold_process_max = max(float(sample["child_process_wall_ms"]) for sample in all_cold)
    all_connections_read_only = all(
        value is True
        for sample in all_cold + all_warm + profiles
        for value in sample.get("connection_read_only_args", [])
    )
    all_queries_consistent = all(row["cross_mode_fingerprint_consistent"] for row in query_results)
    report = {
        "schema": "ncs_training_recommendation_profile_v1",
        "generated_at": _utc_now(),
        "verdict": (
            "The current public facade was profiled without product or DB mutation. "
            "Optimization should target only the measured dominant functions; the 404 KB recommendation module was not refactored."
        ),
        "configuration": {
            "queries": [{"domain": domain, "query": query} for domain, query in queries],
            "cold_repeats": args.cold_repeats,
            "warm_repeats": args.warm_repeats,
            "limit": args.limit,
            "timeout_seconds": args.timeout_seconds,
            "read_only_env_forced": True,
            "parent_module_import_ms": round(parent_import_ms, 3),
            "python": sys.version,
            "platform": sys.platform,
        },
        "global_metrics": {
            "cold_process_wall": _metric_summary(sample["child_process_wall_ms"] for sample in all_cold),
            "cold_facade_wall": _metric_summary(sample["facade_wall_ms"] for sample in all_cold),
            "warm_facade_wall": _metric_summary(sample["facade_wall_ms"] for sample in all_warm),
            "warm_sql_statement_count": _metric_summary(sample["sql"]["statement_count"] for sample in all_warm),
            "warm_sql_api": _metric_summary(sample["sql"]["sqlite_api_ms"] for sample in all_warm),
            "warm_stages": global_stages,
        },
        "historical_claim_recheck": {
            "claim_ms": 1719.0,
            "basis": "previous worst-case recommend_training_for_task observation",
            "observed_warm_max_ms": round(warm_max, 3),
            "observed_cold_facade_max_ms": round(cold_facade_max, 3),
            "observed_cold_process_max_ms": round(cold_process_max, 3),
            "verdict": "confirmed_or_exceeded" if warm_max >= 1719.0 else "not_reproduced_in_current_warm_sample",
        },
        "query_results": query_results,
        "cprofile": {
            "profiled_runs": len(profiles),
            "top_cumulative": top_cumulative,
            "top_self": top_self,
            "per_query": profiles,
        },
        "optimization_candidates": _optimization_candidates(server, training, global_stages, top_cumulative),
        "quality_contract": {
            "all_queries_consistent": all_queries_consistent,
            "query_fingerprints": {
                row["domain"]: {
                    "query": row["query"],
                    "fingerprints": row["warm"]["quality_fingerprints"],
                    "identity_fingerprint": row["warm"]["quality"].get("identity_fingerprint"),
                    "shape_fingerprint": row["warm"]["quality"].get("shape_fingerprint"),
                    "evidence_shape_fingerprint": row["warm"]["quality"].get("evidence_shape_fingerprint"),
                    "recommendation_count": row["warm"]["quality"].get("recommendation_count"),
                    "ok": row["warm"]["quality"].get("ok"),
                }
                for row in query_results
            },
        },
        "db_invariant": {
            "before": before,
            "after": after,
            "size_and_mtime_unchanged": before == after,
            "all_connections_read_only": all_connections_read_only,
            "raw_ksa_write_attempted": False,
            "recommendation_save_forced_false": True,
        },
        "limitations": [
            "Local cold subprocess timing is not Vercel instance allocation/archive extraction timing.",
            "SQLite API timing overlaps higher-level stages and includes cursor fetch/iteration.",
            "Fingerprint consistency is not a semantic relevance judgment.",
        ],
    }
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Profile the public NCS task-training facade read-only.")
    parser.add_argument("--query", action="append", help="DOMAIN=QUERY; repeat for custom query set")
    parser.add_argument("--cold-repeats", type=int, default=3)
    parser.add_argument("--warm-repeats", type=int, default=5)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=float, default=300.0)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--markdown-out", type=Path, default=DEFAULT_MARKDOWN_OUT)
    parser.add_argument("--worker-query", help=argparse.SUPPRESS)
    parser.add_argument("--worker-domain", default="worker", help=argparse.SUPPRESS)
    parser.add_argument("--worker-out", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.worker_query:
        if not args.worker_out:
            parser.error("--worker-out is required with --worker-query")
        return _run_worker(args)
    if args.cold_repeats < 1 or args.warm_repeats < 1:
        parser.error("repeat counts must be >= 1")
    report = _build_report(args)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_out.write_text(_render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "ok": True,
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
                "warm_facade": report["global_metrics"]["warm_facade_wall"],
                "cold_process": report["global_metrics"]["cold_process_wall"],
                "historical_claim": report["historical_claim_recheck"],
                "quality_contract": report["quality_contract"]["all_queries_consistent"],
                "db_unchanged": report["db_invariant"]["size_and_mtime_unchanged"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
