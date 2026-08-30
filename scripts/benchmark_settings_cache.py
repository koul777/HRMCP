from __future__ import annotations

import argparse
import builtins
import hashlib
import json
import math
import os
import statistics
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp import config


ENV_KEYS = (
    "NCS_EXCEL_PATH",
    "NCS_DB_PATH",
    "NCS_SERVICE_KEY",
    "NCS_SQF_SERVICE_KEY",
    "NCS_STUDY_MODULE_SERVICE_KEY",
    "NCS_LEARNING_MODULE_SERVICE_KEY",
    "NCS_TRAINING_COURSE_SERVICE_KEY",
    "NCS_QUALIFICATION_SERVICE_KEY",
    "NCS_NCS_CL_CD_JM_SERVICE_KEY",
    "NCS_JOB_BASE_SERVICE_KEY",
    "NCS_JOB_BASE_COMPETENCY_SERVICE_KEY",
    "NCS_REPORTS_DIR",
    "NCS_MCP_ENABLE_OPERATOR_TOOLS",
    "NCS_MCP_ENABLE_ADVANCED_TOOLS",
    "NCS_MCP_READ_ONLY",
    "NCS_MCP_MAX_CONCURRENT_RECOMMENDATIONS",
    "NCS_MCP_RECOMMENDATION_QUEUE_TIMEOUT_SECONDS",
)
BATCH_SIZES = (1, 10, 24, 100)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summarize_samples(samples_ms: list[float], calls: int) -> dict[str, float | int]:
    return {
        "calls_per_sample": calls,
        "samples": len(samples_ms),
        "total_p50_ms": round(statistics.median(samples_ms), 6),
        "total_p95_ms": round(percentile(samples_ms, 0.95), 6),
        "per_call_p50_ms": round(statistics.median(samples_ms) / calls, 6),
        "per_call_p95_ms": round(percentile(samples_ms, 0.95) / calls, 6),
    }


def clear_config_file_caches() -> None:
    config._ENV_FILE_CACHE.clear()
    config._ENV_LOAD_STAMPS.clear()


class LruCandidate:
    """Candidate A: fastest cache, intentionally lacking a public contract."""

    def __init__(self, loader: Callable[[], Any]) -> None:
        self._cached = lru_cache(maxsize=1)(loader)

    def __call__(self) -> Any:
        return self._cached()

    def benchmark_clear(self) -> None:
        self._cached.cache_clear()


def environment_fingerprint() -> tuple[str, int | None, int | None, str]:
    env_path = (config.PROJECT_ROOT / ".env").resolve()
    try:
        stat = env_path.stat()
        stamp = stat.st_mtime_ns
        size = stat.st_size
    except OSError:
        stamp = None
        size = None
    digest = hashlib.sha256()
    for key in ENV_KEYS:
        digest.update(key.encode("utf-8"))
        digest.update(b"=")
        value = os.environ.get(key)
        digest.update(b"<unset>" if value is None else value.encode("utf-8"))
        digest.update(b"\0")
    return str(env_path), stamp, size, digest.hexdigest()


class FingerprintCandidate:
    """Candidate B: bounded single-entry cache keyed by all settings inputs."""

    def __init__(
        self,
        loader: Callable[[], Any],
        fingerprint: Callable[[], Any] = environment_fingerprint,
    ) -> None:
        self._loader = loader
        self._fingerprint = fingerprint
        self._lock = threading.RLock()
        self._key: Any = object()
        self._value: Any = None

    def __call__(self) -> Any:
        key = self._fingerprint()
        with self._lock:
            if key != self._key:
                self._value = self._loader()
                self._key = key
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._key = object()
            self._value = None


class ExplicitClearCandidate:
    """Candidate C: LRU speed with a required, named invalidation operation."""

    def __init__(self, loader: Callable[[], Any]) -> None:
        self._cached = lru_cache(maxsize=1)(loader)

    def __call__(self) -> Any:
        return self._cached()

    def clear_settings_cache(self) -> None:
        self._cached.cache_clear()


def benchmark_callable(
    callable_: Callable[[], Any],
    *,
    repeats: int,
    before_sample: Callable[[], None] | None = None,
) -> dict[str, dict[str, float | int]]:
    results: dict[str, dict[str, float | int]] = {}
    for calls in BATCH_SIZES:
        samples: list[float] = []
        for _ in range(repeats):
            if before_sample is not None:
                before_sample()
            started = time.perf_counter_ns()
            for _call in range(calls):
                callable_()
            samples.append((time.perf_counter_ns() - started) / 1_000_000)
        results[str(calls)] = summarize_samples(samples, calls)
    return results


class _ReadProxy:
    def __init__(self, stream: Any, counters: dict[str, int]) -> None:
        self._stream = stream
        self._counters = counters

    def read(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["read_calls"] += 1
        return self._stream.read(*args, **kwargs)

    def readline(self, *args: Any, **kwargs: Any) -> Any:
        self._counters["read_calls"] += 1
        return self._stream.readline(*args, **kwargs)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._stream)

    def __enter__(self) -> "_ReadProxy":
        self._stream.__enter__()
        return self

    def __exit__(self, *args: Any) -> Any:
        return self._stream.__exit__(*args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._stream, name)


@contextmanager
def count_env_file_io(env_path: Path) -> Iterator[dict[str, int]]:
    target = env_path.resolve()
    counters = {"open_calls": 0, "read_calls": 0}
    original_builtin_open = builtins.open
    original_path_open = Path.open

    def is_target(value: Any) -> bool:
        try:
            return Path(value).resolve() == target
        except (OSError, TypeError, ValueError):
            return False

    def counted_builtin_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        stream = original_builtin_open(file, *args, **kwargs)
        if is_target(file):
            counters["open_calls"] += 1
            return _ReadProxy(stream, counters)
        return stream

    def counted_path_open(path_self: Path, *args: Any, **kwargs: Any) -> Any:
        stream = original_path_open(path_self, *args, **kwargs)
        if is_target(path_self):
            counters["open_calls"] += 1
            return _ReadProxy(stream, counters)
        return stream

    with patch("builtins.open", counted_builtin_open), patch.object(Path, "open", counted_path_open):
        yield counters


def measure_env_file_io() -> dict[str, Any]:
    env_path = config.PROJECT_ROOT / ".env"
    clear_config_file_caches()
    with count_env_file_io(env_path) as cold_counts:
        config.load_settings()
    config.load_settings()
    with count_env_file_io(env_path) as warm_counts:
        for _ in range(100):
            config.load_settings()
    return {
        "env_file_exists": env_path.exists(),
        "cold_single_call": dict(cold_counts),
        "warm_100_calls": dict(warm_counts),
        "note": "Counts include pathlib and python-dotenv reads; no values are captured.",
    }


@contextmanager
def isolated_settings_environment(project_root: Path) -> Iterator[None]:
    old_root = config.PROJECT_ROOT
    old_file_cache = dict(config._ENV_FILE_CACHE)
    old_load_stamps = dict(config._ENV_LOAD_STAMPS)
    old_values = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        config.PROJECT_ROOT = project_root
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        clear_config_file_caches()
        yield
    finally:
        config.PROJECT_ROOT = old_root
        clear_config_file_caches()
        config._ENV_FILE_CACHE.update(old_file_cache)
        config._ENV_LOAD_STAMPS.update(old_load_stamps)
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _env_change_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        with isolated_settings_environment(root):
            os.environ["NCS_DB_PATH"] = str(root / "first.db")
            current_first = config.load_settings().db_path
            os.environ["NCS_DB_PATH"] = str(root / "second.db")
            current_second = config.load_settings().db_path

            os.environ["NCS_DB_PATH"] = str(root / "first.db")
            lru = LruCandidate(config.load_settings)
            lru_first = lru().db_path
            os.environ["NCS_DB_PATH"] = str(root / "second.db")
            lru_second = lru().db_path

            os.environ["NCS_DB_PATH"] = str(root / "first.db")
            keyed = FingerprintCandidate(config.load_settings)
            keyed_first = keyed().db_path
            os.environ["NCS_DB_PATH"] = str(root / "second.db")
            keyed_second = keyed().db_path

            os.environ["NCS_DB_PATH"] = str(root / "first.db")
            explicit = ExplicitClearCandidate(config.load_settings)
            explicit_first = explicit().db_path
            os.environ["NCS_DB_PATH"] = str(root / "second.db")
            explicit_stale = explicit().db_path
            explicit.clear_settings_cache()
            explicit_second = explicit().db_path

    return {
        "current_reloads_environment": current_first != current_second,
        "candidate_a_stale": lru_first == lru_second,
        "candidate_b_reloads_environment": keyed_first != keyed_second,
        "candidate_c_stale_without_clear": explicit_first == explicit_stale,
        "candidate_c_reloads_after_clear": explicit_first != explicit_second,
    }


def _env_file_change_scenario() -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        env_path = root / ".env"
        env_path.write_text(f"NCS_DB_PATH={root / 'first.db'}\n", encoding="utf-8")
        with isolated_settings_environment(root):
            first = config.load_settings().db_path
            env_path.write_text(f"NCS_DB_PATH={root / 'second.db'}\n", encoding="utf-8")
            stamp = env_path.stat().st_mtime_ns
            os.utime(env_path, ns=(stamp + 1_000_000, stamp + 1_000_000))
            second = config.load_settings().db_path
            keyed = FingerprintCandidate(config.load_settings)
            keyed_before = keyed().db_path
            env_path.write_text(f"NCS_DB_PATH={root / 'third.db'}\n", encoding="utf-8")
            stamp = env_path.stat().st_mtime_ns
            os.utime(env_path, ns=(stamp + 1_000_000, stamp + 1_000_000))
            keyed_after = keyed().db_path
    return {
        "current_reload_observed": first != second,
        "current_stale_due_to_env_promotion": first == second,
        "candidate_b_key_changed_but_loader_stale": keyed_before == keyed_after,
        "explanation": (
            "The loader promotes .env values into os.environ with override=False; later file edits "
            "cannot replace that process value even when file caches or fingerprint keys change."
        ),
    }


def _cwd_and_env_path_scenario() -> dict[str, Any]:
    original_cwd = Path.cwd()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        child = root / "child"
        child.mkdir()
        before = environment_fingerprint()
        try:
            os.chdir(child)
            after_cwd = environment_fingerprint()
        finally:
            os.chdir(original_cwd)
        with patch.object(config, "PROJECT_ROOT", root):
            after_root = environment_fingerprint()
    return {
        "cwd_change_affects_key": before != after_cwd,
        "project_root_env_file_change_affects_key": before != after_root,
        "fixed_project_root_contract_preserved": before == after_cwd,
    }


def _thread_scenario(workers: int = 8) -> dict[str, Any]:
    lru_count = 0
    lru_count_lock = threading.Lock()
    release = threading.Event()
    all_entered = threading.Event()

    def racing_loader() -> object:
        nonlocal lru_count
        with lru_count_lock:
            lru_count += 1
            if lru_count >= workers:
                all_entered.set()
        release.wait(timeout=2.0)
        return object()

    lru = LruCandidate(racing_loader)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(lru) for _ in range(workers)]
        all_entered.wait(timeout=2.0)
        release.set()
        lru_values = [future.result(timeout=2.0) for future in futures]

    keyed_count = 0
    keyed_count_lock = threading.Lock()

    def single_loader() -> object:
        nonlocal keyed_count
        with keyed_count_lock:
            keyed_count += 1
        time.sleep(0.01)
        return object()

    keyed = FingerprintCandidate(single_loader, fingerprint=lambda: "stable")
    with ThreadPoolExecutor(max_workers=workers) as executor:
        keyed_values = list(executor.map(lambda _index: keyed(), range(workers)))

    return {
        "workers": workers,
        "candidate_a_loader_calls_on_concurrent_miss": lru_count,
        "candidate_a_single_identity": len({id(value) for value in lru_values}) == 1,
        "candidate_b_loader_calls_on_concurrent_miss": keyed_count,
        "candidate_b_single_identity": len({id(value) for value in keyed_values}) == 1,
    }


def evaluate_correctness_scenarios() -> dict[str, Any]:
    return {
        "environment_change": _env_change_scenario(),
        "env_file_change": _env_file_change_scenario(),
        "cwd_and_env_file_path": _cwd_and_env_path_scenario(),
        "thread_concurrency": _thread_scenario(),
        "monkeypatch_contract": {
            "candidate_a": "A cached value survives loader-side monkeypatch/state changes.",
            "candidate_b": "B refreshes only when its declared fingerprint changes.",
            "candidate_c": "Tests that mutate settings inputs must call clear_settings_cache().",
            "risk": "Imported load_settings symbols still need patching at the consumer module, as today.",
        },
    }


def build_recommendation(
    timings: dict[str, Any], correctness: dict[str, Any]
) -> dict[str, Any]:
    current_24 = timings["current_warm"]["24"]["total_p50_ms"]
    candidate_24 = timings["candidate_c_warm_hit"]["24"]["total_p50_ms"]
    savings_24 = max(0.0, float(current_24) - float(candidate_24))
    return {
        "verdict": "do_not_promote_settings_object_cache_yet",
        "absolute_p50_savings_for_24_calls_ms": round(savings_24, 6),
        "candidate_a": "reject: stale on environment changes and duplicate concurrent misses",
        "candidate_b": (
            "defer: strongest automatic invalidation and single-flight behavior, but .env edits remain "
            "stale because the existing loader promotes file values into os.environ"
        ),
        "candidate_c": (
            "conditional only: fastest simple contract, but every environment-mutating test, CLI reload, "
            "and deployment bootstrap must invoke clear_settings_cache()"
        ),
        "next_step": (
            "Keep the existing mtime-based .env parse cache. If settings-object caching is still desired, "
            "first make .env reads side-effect-free or track which os.environ keys were injected, then add "
            "an explicit clear_settings_cache() contract and regression tests before production promotion."
        ),
        "correctness_gate_passed": False,
        "blocking_scenario": "env_file_change",
        "evidence": correctness["env_file_change"],
    }


def run_benchmark(repeats: int = 100) -> dict[str, Any]:
    clear_config_file_caches()
    current_cold = benchmark_callable(
        config.load_settings,
        repeats=repeats,
        before_sample=clear_config_file_caches,
    )
    config.load_settings()
    current_warm = benchmark_callable(config.load_settings, repeats=repeats)

    lru = LruCandidate(config.load_settings)
    lru()
    candidate_a = benchmark_callable(lru, repeats=repeats)

    keyed = FingerprintCandidate(config.load_settings)
    keyed()
    candidate_b = benchmark_callable(keyed, repeats=repeats)

    explicit = ExplicitClearCandidate(config.load_settings)
    explicit()
    candidate_c = benchmark_callable(explicit, repeats=repeats)

    explicit_miss = ExplicitClearCandidate(config.load_settings)
    candidate_c_miss = benchmark_callable(
        explicit_miss,
        repeats=repeats,
        before_sample=explicit_miss.clear_settings_cache,
    )
    timings = {
        "current_cold_env_cache_per_sample": current_cold,
        "current_warm": current_warm,
        "candidate_a_lru_warm_hit": candidate_a,
        "candidate_b_fingerprint_warm_hit": candidate_b,
        "candidate_c_warm_hit": candidate_c,
        "candidate_c_settings_miss_env_warm": candidate_c_miss,
    }
    correctness = evaluate_correctness_scenarios()
    report = {
        "schema": "ncs_settings_cache_experiment_v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "product_files_modified": False,
            "raw_data_modified": False,
            "secrets_captured": False,
            "repeats_per_batch_size": repeats,
            "batch_sizes": list(BATCH_SIZES),
        },
        "current_implementation": {
            "settings_object_cache": False,
            "env_file_value_cache": "mtime-keyed _ENV_FILE_CACHE",
            "env_load_stamp_cache": "mtime-keyed _ENV_LOAD_STAMPS",
            "env_file_path": "PROJECT_ROOT/.env (fixed; independent of cwd)",
        },
        "env_file_io": measure_env_file_io(),
        "timings": timings,
        "correctness_scenarios": correctness,
    }
    report["recommendation"] = build_recommendation(timings, correctness)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    timings = report["timings"]
    lines = [
        "# Settings Cache Experiment",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Verdict",
        "",
        f"**{report['recommendation']['verdict']}**",
        "",
        report["recommendation"]["next_step"],
        "",
        "## Timing (batch total p50 / p95, ms)",
        "",
        "| Strategy | 1 call | 10 calls | 24 calls | 100 calls |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    labels = {
        "current_cold_env_cache_per_sample": "Current, env cache cold each sample",
        "current_warm": "Current, warm",
        "candidate_a_lru_warm_hit": "A: lru_cache warm hit",
        "candidate_b_fingerprint_warm_hit": "B: fingerprint warm hit",
        "candidate_c_warm_hit": "C: explicit-clear warm hit",
        "candidate_c_settings_miss_env_warm": "C: settings miss, env cache warm",
    }
    for key, label in labels.items():
        cells = []
        for calls in BATCH_SIZES:
            row = timings[key][str(calls)]
            cells.append(f"{row['total_p50_ms']:.6f} / {row['total_p95_ms']:.6f}")
        lines.append(f"| {label} | " + " | ".join(cells) + " |")

    io = report["env_file_io"]
    env_change = report["correctness_scenarios"]["environment_change"]
    env_file_change = report["correctness_scenarios"]["env_file_change"]
    threads = report["correctness_scenarios"]["thread_concurrency"]
    lines.extend(
        [
            "",
            "## I/O Evidence",
            "",
            f"- `.env` exists: `{io['env_file_exists']}`",
            f"- Cold one-call opens/reads: `{io['cold_single_call']['open_calls']}` / `{io['cold_single_call']['read_calls']}`",
            f"- Warm 100-call opens/reads: `{io['warm_100_calls']['open_calls']}` / `{io['warm_100_calls']['read_calls']}`",
            "- No `.env` values or service keys were captured.",
            "",
            "## Correctness Findings",
            "",
            f"- Current loader observes direct environment changes: `{env_change['current_reloads_environment']}`.",
            f"- Candidate A becomes stale after an environment change: `{env_change['candidate_a_stale']}`.",
            f"- Candidate B observes direct environment changes: `{env_change['candidate_b_reloads_environment']}`.",
            f"- Candidate C requires clear; refresh after clear: `{env_change['candidate_c_reloads_after_clear']}`.",
            f"- Existing loader stays stale after an in-process `.env` edit: `{env_file_change['current_stale_due_to_env_promotion']}`.",
            f"- Candidate A loader executions on {threads['workers']} simultaneous misses: `{threads['candidate_a_loader_calls_on_concurrent_miss']}`.",
            f"- Candidate B loader executions on {threads['workers']} simultaneous misses: `{threads['candidate_b_loader_calls_on_concurrent_miss']}`.",
            "",
            "## Promotion Gate",
            "",
            f"- 24-call absolute p50 saving with C: `{report['recommendation']['absolute_p50_savings_for_24_calls_ms']}` ms.",
            f"- Correctness gate passed: `{report['recommendation']['correctness_gate_passed']}`.",
            "- A: reject.",
            "- B: defer until `.env` loading is side-effect-free or injected environment values are tracked.",
            "- C: only consider with a public `clear_settings_cache()` contract and all mutation/bootstrap tests updated.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark load_settings cache policies.")
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "reports" / "settings_cache_experiment_20260830.json",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=ROOT / "reports" / "settings_cache_experiment_20260830.md",
    )
    args = parser.parse_args()
    if args.repeats < 3:
        parser.error("--repeats must be at least 3")
    report = run_benchmark(repeats=args.repeats)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.markdown_out.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "out": str(args.out),
                "markdown_out": str(args.markdown_out),
                "verdict": report["recommendation"]["verdict"],
                "p50_savings_24_calls_ms": report["recommendation"][
                    "absolute_p50_savings_for_24_calls_ms"
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
