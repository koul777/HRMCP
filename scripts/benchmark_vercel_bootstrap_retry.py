"""Measure Vercel bootstrap retry state machines without changing product code.

This harness uses the real ``api.bootstrap_runtime.ensure_bootstrap`` and the
real import-time readiness latch in ``api.mcp`` for the current-behaviour
probes. Candidate policies are intentionally local simulations. They provide
an evidence bundle for a later, separately reviewed product change.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, Iterable
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for _import_path in (ROOT, SRC):
    if str(_import_path) not in sys.path:
        sys.path.insert(0, str(_import_path))


SCHEMA = "ncs_vercel_bootstrap_retry_experiment_v1"
METRICS_SCHEMA = "ncs_vercel_bootstrap_metrics_v2"
RETRYABLE_REASONS = frozenset(
    {
        "lock_timeout_without_cache",
        "oserror_eagain",
        "oserror_ebusy",
        "oserror_etimedout",
    }
)
TERMINAL_REASONS = frozenset(
    {
        "enospc",
        "manifest_invalid",
        "validation_failed",
        "schema_mismatch",
        "count_mismatch",
        "unknown_materialization_exception",
    }
)
POLICIES = ("A_current", "B_one_bounded_retry", "C_ttl_backoff", "D_operator_reset")


def _not_initialized() -> dict[str, object]:
    return {
        "schema": METRICS_SCHEMA,
        "status": "not_initialized",
        "source": None,
        "ready": None,
    }


def _failure_metrics(result: str, *, error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": "ncs_vercel_snapshot_materialization_metrics_v1",
        "stages_ms": {"fixture_materialization": 5.0},
        "result": result,
        "lock_acquired": result != "lock_timeout_without_cache",
        "published": False,
    }
    if error is not None:
        payload["error"] = error
    return payload


class _ConcurrencyProbe:
    def __init__(self, result: tuple[bool, dict[str, Any]], delay_ms: float) -> None:
        self.result = result
        self.delay_ms = delay_ms
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, **_kwargs: object) -> tuple[bool, dict[str, Any]]:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay_ms / 1000.0)
            return self.result
        finally:
            with self._lock:
                self.active -= 1


def _runtime_patches(runtime: ModuleType, local_probe: _ConcurrencyProbe):
    return (
        mock.patch.object(runtime, "readiness_required_tables", return_value=("competency_units",)),
        mock.patch.object(runtime, "readiness_required_min_rows", return_value={}),
        mock.patch.object(runtime, "_bootstrap_db_from_url", return_value=False),
        mock.patch.object(runtime, "_bootstrap_db_from_explicit_path", return_value=False),
        mock.patch.object(runtime, "_bootstrap_db_from_local_snapshot", side_effect=local_probe),
    )


def _call_concurrently(callable_obj, concurrency: int) -> tuple[list[Any], list[float]]:
    barrier = threading.Barrier(concurrency)

    def invoke() -> tuple[Any, float]:
        barrier.wait()
        started = time.perf_counter()
        result = callable_obj()
        return result, (time.perf_counter() - started) * 1000.0

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pairs = list(executor.map(lambda _index: invoke(), range(concurrency)))
    return [item[0] for item in pairs], [item[1] for item in pairs]


def probe_current_ensure_bootstrap(*, concurrency: int = 8, delay_ms: float = 5.0) -> dict[str, Any]:
    """Exercise the current cache and lock with a transient first failure."""

    runtime = importlib.import_module("api.bootstrap_runtime")
    state = importlib.import_module("api.bootstrap_state")
    original_state = state.get_bootstrap_metrics()
    try:
        state.record_bootstrap_metrics(_not_initialized())
        sequential_probe = _ConcurrencyProbe(
            (False, _failure_metrics("lock_timeout_without_cache")), delay_ms
        )
        patches = _runtime_patches(runtime, sequential_probe)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = runtime.ensure_bootstrap()
            second = runtime.ensure_bootstrap()

        state.record_bootstrap_metrics(_not_initialized())
        concurrent_probe = _ConcurrencyProbe(
            (False, _failure_metrics("lock_timeout_without_cache")), delay_ms
        )
        patches = _runtime_patches(runtime, concurrent_probe)
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            concurrent_results, latencies_ms = _call_concurrently(
                runtime.ensure_bootstrap, concurrency
            )

        return {
            "sequential": {
                "first_status": first.get("status"),
                "second_status": second.get("status"),
                "first_ready": first.get("ready"),
                "second_ready": second.get("ready"),
                "materialization_calls": sequential_probe.calls,
                "not_ready_cached_permanently_in_process": (
                    sequential_probe.calls == 1
                    and first.get("status") == "not_ready"
                    and second.get("status") == "not_ready"
                ),
            },
            "concurrent": {
                "requests": concurrency,
                "statuses": sorted({str(item.get("status")) for item in concurrent_results}),
                "materialization_calls": concurrent_probe.calls,
                "max_concurrent_materializations": concurrent_probe.max_active,
                "single_flight_observed": (
                    concurrent_probe.calls == 1 and concurrent_probe.max_active == 1
                ),
                "request_latency_ms": _latency_summary(latencies_ms),
            },
        }
    finally:
        state.record_bootstrap_metrics(original_state)


def probe_current_failure_classification(*, delay_ms: float = 1.0) -> dict[str, Any]:
    """Show which materializer failures remain distinguishable at bootstrap."""

    runtime = importlib.import_module("api.bootstrap_runtime")
    state = importlib.import_module("api.bootstrap_state")
    original_state = state.get_bootstrap_metrics()
    fixture_shapes = {
        "lock_timeout": _failure_metrics("lock_timeout_without_cache"),
        "retryable_oserror": _failure_metrics(
            "materialization_exception", error="materialization_exception"
        ),
        "enospc": _failure_metrics(
            "materialization_exception", error="materialization_exception"
        ),
        "manifest_mismatch": _failure_metrics(
            "manifest_invalid", error="compact snapshot member size does not match manifest"
        ),
        "schema_or_count_mismatch": _failure_metrics("validation_failed"),
    }
    observations: dict[str, Any] = {}
    try:
        for label, metrics in fixture_shapes.items():
            state.record_bootstrap_metrics(_not_initialized())
            probe = _ConcurrencyProbe((False, metrics), delay_ms)
            patches = _runtime_patches(runtime, probe)
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                payload = runtime.ensure_bootstrap()
            local = payload.get("local_snapshot") or {}
            error = payload.get("error") or {}
            observations[label] = {
                "bootstrap_error_code": error.get("code"),
                "snapshot_result": local.get("result"),
                "snapshot_error": local.get("error"),
                "materialization_calls": probe.calls,
            }
    finally:
        state.record_bootstrap_metrics(original_state)

    observations["classification_gap"] = {
        "retryable_oserror_indistinguishable_from_enospc": (
            observations["retryable_oserror"] == observations["enospc"]
        ),
        "bootstrap_collapses_all_failures_to_no_verified_snapshot": all(
            item["bootstrap_error_code"] == "no_verified_snapshot"
            for key, item in observations.items()
            if key != "classification_gap"
        ),
        "safe_automatic_retry_possible_without_product_metric_change": False,
    }
    return observations


class _FakeLifespan:
    async def __aenter__(self):
        return None

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeBaseApp:
    def __init__(self) -> None:
        self.router = SimpleNamespace(lifespan_context=lambda _app: _FakeLifespan())

    async def __call__(self, _scope, _receive, send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})


class _FakeMcp:
    def __init__(self) -> None:
        self.settings = SimpleNamespace(streamable_http_path="/mcp", transport_security=None)

    def streamable_http_app(self) -> _FakeBaseApp:
        return _FakeBaseApp()


async def _invoke_post(app) -> int:
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/mcp",
            "headers": [],
        },
        receive,
        send,
    )
    return int(next(item["status"] for item in sent if item["type"] == "http.response.start"))


def probe_mcp_import_readiness_latch() -> dict[str, Any]:
    """Import the real api.mcp around fakes and prove its boolean is latched."""

    runtime = importlib.import_module("api.bootstrap_runtime")
    state = importlib.import_module("api.bootstrap_state")
    api_package = importlib.import_module("api")
    original_state = state.get_bootstrap_metrics()
    old_mcp_module = sys.modules.pop("api.mcp", None)
    old_server_module = sys.modules.get("ncs_mcp.server")
    had_api_mcp_attribute = hasattr(api_package, "mcp")
    old_api_mcp_attribute = getattr(api_package, "mcp", None)

    fake_server = ModuleType("ncs_mcp.server")
    fake_server.configure_transport = lambda **_kwargs: None
    fake_server.mcp = _FakeMcp()
    sys.modules["ncs_mcp.server"] = fake_server
    try:
        state.record_bootstrap_metrics(
            {"schema": METRICS_SCHEMA, "status": "not_ready", "ready": False}
        )
        with mock.patch.dict(
            os.environ,
            {"VERCEL": "1", "NCS_MCP_READ_ONLY": "1"},
            clear=False,
        ), mock.patch.object(
            runtime,
            "ensure_bootstrap",
            return_value={"schema": METRICS_SCHEMA, "status": "not_ready", "ready": False},
        ):
            module = importlib.import_module("api.mcp")

        import_latched_ready = bool(module._MCP_BOOTSTRAP_READY)
        state.record_bootstrap_metrics(
            {"schema": METRICS_SCHEMA, "status": "ready", "ready": True}
        )
        dynamic_metrics_ready = bool(module.bootstrap_metrics().get("ready"))
        with mock.patch.dict(
            os.environ,
            {"VERCEL": "1", "NCS_MCP_READ_ONLY": "1"},
            clear=False,
        ):
            post_status_after_state_recovery = asyncio.run(_invoke_post(module.app))
        return {
            "import_latched_ready": import_latched_ready,
            "state_ready_after_recovery": dynamic_metrics_ready,
            "post_status_after_state_recovery": post_status_after_state_recovery,
            "same_process_recovery_blocked_by_import_latch": (
                not import_latched_ready
                and dynamic_metrics_ready
                and post_status_after_state_recovery == 503
            ),
        }
    finally:
        sys.modules.pop("api.mcp", None)
        if old_mcp_module is not None:
            sys.modules["api.mcp"] = old_mcp_module
        if old_server_module is None:
            sys.modules.pop("ncs_mcp.server", None)
        else:
            sys.modules["ncs_mcp.server"] = old_server_module
        if had_api_mcp_attribute:
            setattr(api_package, "mcp", old_api_mcp_attribute)
        elif hasattr(api_package, "mcp"):
            delattr(api_package, "mcp")
        state.record_bootstrap_metrics(original_state)


@dataclass(frozen=True)
class AttemptOutcome:
    ready: bool
    reason: str

    @property
    def category(self) -> str:
        if self.ready:
            return "success"
        if self.reason in RETRYABLE_REASONS:
            return "transient"
        return "terminal"


class ScriptedExtractor:
    def __init__(self, outcomes: Iterable[AttemptOutcome], attempt_ms: float) -> None:
        self.outcomes = list(outcomes)
        if not self.outcomes:
            raise ValueError("at least one outcome is required")
        self.attempt_ms = attempt_ms
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.elapsed_ms = 0.0
        self._lock = threading.Lock()

    def __call__(self) -> AttemptOutcome:
        with self._lock:
            index = min(self.calls, len(self.outcomes) - 1)
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        started = time.perf_counter()
        try:
            time.sleep(self.attempt_ms / 1000.0)
            return self.outcomes[index]
        finally:
            elapsed = (time.perf_counter() - started) * 1000.0
            with self._lock:
                self.elapsed_ms += elapsed
                self.active -= 1


class RetryPolicyMachine:
    """A locked, bounded state machine used only by this experiment."""

    def __init__(
        self,
        policy: str,
        extractor: ScriptedExtractor,
        *,
        backoff_ms: float,
        max_attempts_per_generation: int = 2,
    ) -> None:
        if policy not in POLICIES:
            raise ValueError(f"unknown policy: {policy}")
        self.policy = policy
        self.extractor = extractor
        self.backoff_ms = backoff_ms
        self.max_attempts = max_attempts_per_generation
        self.state = "UNINITIALIZED"
        self.attempts_in_generation = 0
        self.next_retry_at_ms: float | None = None
        self.last_reason: str | None = None
        self.operator_resets = 0
        self._lock = threading.RLock()

    def _attempt(self, now_ms: float) -> None:
        self.state = "BOOTSTRAPPING"
        outcome = self.extractor()
        self.attempts_in_generation += 1
        self.last_reason = outcome.reason
        if outcome.ready:
            self.state = "READY"
            self.next_retry_at_ms = None
            return
        if outcome.category != "transient":
            self.state = "TERMINAL_NOT_READY"
            self.next_retry_at_ms = None
            return
        if self.policy in {"A_current", "D_operator_reset"}:
            self.state = "TERMINAL_NOT_READY"
            return
        if self.attempts_in_generation >= self.max_attempts:
            self.state = "TERMINAL_NOT_READY"
            return
        if self.policy == "B_one_bounded_retry":
            self.state = "RETRYING"
            self._attempt(now_ms)
            return
        self.state = "RETRY_WAIT"
        self.next_retry_at_ms = now_ms + self.backoff_ms

    def request(self, *, now_ms: float) -> dict[str, Any]:
        with self._lock:
            if self.state == "READY":
                return self.snapshot()
            if self.state == "TERMINAL_NOT_READY":
                return self.snapshot()
            if self.state == "RETRY_WAIT":
                if self.next_retry_at_ms is None or now_ms < self.next_retry_at_ms:
                    return self.snapshot()
            self._attempt(now_ms)
            return self.snapshot()

    def operator_reset(self) -> bool:
        with self._lock:
            if self.policy != "D_operator_reset" or self.state != "TERMINAL_NOT_READY":
                return False
            self.operator_resets += 1
            self.attempts_in_generation = 0
            self.next_retry_at_ms = None
            self.last_reason = None
            self.state = "UNINITIALIZED"
            return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "ready": self.state == "READY",
            "attempts_in_generation": self.attempts_in_generation,
            "next_retry_at_ms": self.next_retry_at_ms,
            "last_reason": self.last_reason,
            "operator_resets": self.operator_resets,
        }


def _latency_summary(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    if not ordered:
        return {"min": 0.0, "p50": 0.0, "p95": 0.0, "max": 0.0}
    p95_index = min(len(ordered) - 1, max(0, int(len(ordered) * 0.95 + 0.999) - 1))
    return {
        "min": round(ordered[0], 3),
        "p50": round(statistics.median(ordered), 3),
        "p95": round(ordered[p95_index], 3),
        "max": round(ordered[-1], 3),
    }


def _run_wave(machine: RetryPolicyMachine, concurrency: int, *, now_ms: float) -> dict[str, Any]:
    results, latencies = _call_concurrently(
        lambda: machine.request(now_ms=now_ms), concurrency
    )
    return {
        "requests": concurrency,
        "states": sorted({item["state"] for item in results}),
        "ready_count": sum(1 for item in results if item["ready"]),
        "latency_ms": _latency_summary(latencies),
    }


def run_policy_scenario(
    policy: str,
    outcomes: list[AttemptOutcome],
    *,
    concurrency: int,
    attempt_ms: float,
    backoff_ms: float,
    allow_operator_reset: bool,
) -> dict[str, Any]:
    extractor = ScriptedExtractor(outcomes, attempt_ms)
    machine = RetryPolicyMachine(policy, extractor, backoff_ms=backoff_ms)
    started = time.perf_counter()
    waves = [_run_wave(machine, concurrency, now_ms=0.0)]

    if machine.state == "RETRY_WAIT":
        waves.append(_run_wave(machine, concurrency, now_ms=backoff_ms - 0.001))
        waves.append(_run_wave(machine, concurrency, now_ms=backoff_ms))
    elif allow_operator_reset and machine.operator_reset():
        waves.append(_run_wave(machine, concurrency, now_ms=backoff_ms))

    final = machine.snapshot()
    minimum_attempts = 2 if final["ready"] and not outcomes[0].ready else 1
    duplicate_extractions = max(0, extractor.calls - minimum_attempts)
    return {
        "policy": policy,
        "final": final,
        "waves": waves,
        "materialization_attempts": extractor.calls,
        "materialization_elapsed_ms": round(extractor.elapsed_ms, 3),
        "experiment_wall_ms": round((time.perf_counter() - started) * 1000.0, 3),
        "max_concurrent_materializations": extractor.max_active,
        "duplicate_extractions": duplicate_extractions,
        "stampede_detected": extractor.max_active > 1,
        "modeled_backoff_ms": (
            backoff_ms if policy == "C_ttl_backoff" and len(waves) > 1 else 0.0
        ),
        "same_request_retry": policy == "B_one_bounded_retry" and extractor.calls > 1,
    }


def compare_candidate_policies(
    *, concurrency: int = 8, attempt_ms: float = 5.0, backoff_ms: float = 50.0
) -> dict[str, Any]:
    scenarios = {
        "lock_timeout_then_ready": [
            AttemptOutcome(False, "lock_timeout_without_cache"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "retryable_oserror_then_ready": [
            AttemptOutcome(False, "oserror_eagain"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "enospc_then_hypothetical_ready": [
            AttemptOutcome(False, "enospc"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "manifest_mismatch_then_hypothetical_ready": [
            AttemptOutcome(False, "manifest_invalid"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "schema_mismatch_then_hypothetical_ready": [
            AttemptOutcome(False, "schema_mismatch"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "count_mismatch_then_hypothetical_ready": [
            AttemptOutcome(False, "count_mismatch"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
        "unknown_oserror_then_hypothetical_ready": [
            AttemptOutcome(False, "unknown_materialization_exception"),
            AttemptOutcome(True, "published_new_snapshot"),
        ],
    }
    report: dict[str, Any] = {}
    for scenario_name, outcomes in scenarios.items():
        transient = outcomes[0].reason in RETRYABLE_REASONS
        candidate_runs = {
            policy: run_policy_scenario(
                policy,
                outcomes,
                concurrency=concurrency,
                attempt_ms=attempt_ms,
                backoff_ms=backoff_ms,
                allow_operator_reset=(policy == "D_operator_reset" and transient),
            )
            for policy in POLICIES
        }
        baseline_p50 = candidate_runs["A_current"]["waves"][0]["latency_ms"]["p50"]
        for candidate in candidate_runs.values():
            candidate_p50 = candidate["waves"][0]["latency_ms"]["p50"]
            candidate["initial_cold_latency_amplification_vs_A"] = round(
                candidate_p50 / baseline_p50, 3
            ) if baseline_p50 else None
        report[scenario_name] = {
            "first_failure_category": "transient" if transient else "terminal",
            "candidates": candidate_runs,
        }
    return report


def build_report(
    *, concurrency: int = 8, attempt_ms: float = 5.0, backoff_ms: float = 50.0
) -> dict[str, Any]:
    current = probe_current_ensure_bootstrap(concurrency=concurrency, delay_ms=attempt_ms)
    classification = probe_current_failure_classification(delay_ms=min(attempt_ms, 1.0))
    latch = probe_mcp_import_readiness_latch()
    candidates = compare_candidate_policies(
        concurrency=concurrency,
        attempt_ms=attempt_ms,
        backoff_ms=backoff_ms,
    )
    return {
        "schema": SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "product_code_mutated": False,
            "git_or_deploy_performed": False,
            "concurrency": concurrency,
            "simulated_attempt_ms": attempt_ms,
            "simulated_backoff_ms": backoff_ms,
            "note": "Candidate timing uses controlled local sleeps; current behavior probes call real modules with fixture materialization.",
        },
        "current_behavior": {
            "ensure_bootstrap": current,
            "failure_classification": classification,
            "mcp_import_readiness_latch": latch,
        },
        "candidate_results": candidates,
        "conclusion": {
            "safe_candidate": "C_ttl_backoff",
            "status": "conditional_on_typed_failure_metadata_and_dynamic_request_readiness",
            "why_not_B": (
                "The current lock wait can consume 45 seconds. An immediate same-request retry can double "
                "cold work and exceed the 30-second Vercel function duration before useful recovery."
            ),
            "why_not_D": (
                "Operator reset does not repair api.mcp's import-time readiness latch and is not automatic."
            ),
            "required_state_transitions": [
                "UNINITIALIZED -> BOOTSTRAPPING",
                "BOOTSTRAPPING -> READY on verified snapshot",
                "BOOTSTRAPPING -> RETRY_WAIT on explicitly retryable failure while attempts < 2",
                "RETRY_WAIT -> BOOTSTRAPPING only after retry_after under the same process lock",
                "BOOTSTRAPPING -> TERMINAL_NOT_READY on ENOSPC, manifest/schema/count/validation failure, unknown OSError, or exhausted retry budget",
                "READY is immutable for the process generation",
                "TERMINAL_NOT_READY -> UNINITIALIZED only through an authenticated operator reset after remediation; reset starts a new generation",
            ],
            "retry_contract": {
                "max_attempts_per_process_generation": 2,
                "single_flight_required": True,
                "retryable": sorted(RETRYABLE_REASONS),
                "terminal": sorted(TERMINAL_REASONS),
                "unknown_errors_fail_closed": True,
                "request_time_ready_check_required": True,
                "import_time_boolean_latch_forbidden": True,
            },
            "implementation_prerequisites": [
                "Preserve errno/category in snapshot metrics; current materialization_exception merges retryable OSError and ENOSPC.",
                "Replace api.mcp import-time readiness boolean use with a cheap retry-aware request-time accessor.",
                "Keep extraction and retry transition under one process-local single-flight lock.",
                "Make lock wait and retry deadline aware of the 30-second Vercel maxDuration.",
                "Do not expose operator reset as a public MCP tool.",
            ],
            "acceptance_tests": [
                "First lock timeout or EAGAIN/EBUSY/ETIMEDOUT enters RETRY_WAIT and at most one later request performs attempt 2.",
                "Eight concurrent requests produce max_concurrent_materializations=1 and no duplicate extraction.",
                "ENOSPC, manifest mismatch, schema mismatch, count mismatch, validation failure, and unknown OSError perform exactly one attempt.",
                "A transient failure followed by success changes POST from fail-closed 503 to normal MCP handling without module reload.",
                "A second transient failure becomes TERMINAL_NOT_READY; no third automatic attempt occurs.",
                "Public seven-tool MCP contract, read-only behavior, and compact snapshot bytes remain unchanged.",
            ],
            "rollback_gate": {
                "feature_flag": "NCS_MCP_BOOTSTRAP_RETRY_POLICY=current",
                "rollback_if": [
                    "more than two materialization attempts occur in one process generation",
                    "max_concurrent_materializations exceeds one",
                    "any terminal failure is retried automatically",
                    "successful cold-start p95 regresses by more than 10 percent against the same deployment baseline",
                    "retry path can exceed the configured Vercel request deadline",
                    "POST returns non-503 before a snapshot is fully verified",
                ],
            },
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    current = report["current_behavior"]
    sequential = current["ensure_bootstrap"]["sequential"]
    concurrent = current["ensure_bootstrap"]["concurrent"]
    latch = current["mcp_import_readiness_latch"]
    gap = current["failure_classification"]["classification_gap"]
    lines = [
        "# Vercel Bootstrap Retry Experiment",
        "",
        f"Generated: `{report['generated_at']}`",
        "",
        "## Current behavior reproduced from product modules",
        "",
        f"- Sequential first failure materializations: `{sequential['materialization_calls']}`.",
        f"- Same-process `not_ready` permanently cached: `{str(sequential['not_ready_cached_permanently_in_process']).lower()}`.",
        f"- Concurrent requests: `{concurrent['requests']}`; materializations: `{concurrent['materialization_calls']}`; max concurrent: `{concurrent['max_concurrent_materializations']}`.",
        f"- Process single-flight observed: `{str(concurrent['single_flight_observed']).lower()}`.",
        f"- Recovered state still returns POST 503 because of import latch: `{str(latch['same_process_recovery_blocked_by_import_latch']).lower()}`.",
        f"- Retryable OSError and ENOSPC are indistinguishable: `{str(gap['retryable_oserror_indistinguishable_from_enospc']).lower()}`.",
        "",
        "## Candidate comparison",
        "",
        "| Scenario | Policy | Ready | Attempts | Initial latency x A | Max concurrent | Duplicate extraction |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for scenario_name, scenario in report["candidate_results"].items():
        for policy, result in scenario["candidates"].items():
            lines.append(
                "| {scenario} | {policy} | {ready} | {attempts} | {amp} | {active} | {duplicate} |".format(
                    scenario=scenario_name,
                    policy=policy,
                    ready=str(result["final"]["ready"]).lower(),
                    attempts=result["materialization_attempts"],
                    amp=result["initial_cold_latency_amplification_vs_A"],
                    active=result["max_concurrent_materializations"],
                    duplicate=result["duplicate_extractions"],
                )
            )
    conclusion = report["conclusion"]
    lines.extend(
        [
            "",
            "## Conclusion",
            "",
            f"Safe candidate: **{conclusion['safe_candidate']}**, status: `{conclusion['status']}`.",
            "",
            conclusion["why_not_B"],
            "",
            conclusion["why_not_D"],
            "",
            "### Required transitions",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in conclusion["required_state_transitions"])
    lines.extend(["", "### Prerequisites", ""])
    lines.extend(f"- {item}" for item in conclusion["implementation_prerequisites"])
    lines.extend(["", "### Acceptance tests", ""])
    lines.extend(f"- {item}" for item in conclusion["acceptance_tests"])
    lines.extend(["", "### Rollback gates", ""])
    lines.append(f"- Feature flag: `{conclusion['rollback_gate']['feature_flag']}`.")
    lines.extend(f"- {item}" for item in conclusion["rollback_gate"]["rollback_if"])
    lines.extend(
        [
            "",
            "No product/API/snapshot files were changed, and no git or deployment action was performed.",
            "",
        ]
    )
    return "\n".join(lines)


def write_report(report: dict[str, Any], out: Path, markdown_out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_out.write_text(render_markdown(report), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("reports/vercel_bootstrap_retry_experiment_20260830.json"),
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=Path("reports/vercel_bootstrap_retry_experiment_20260830.md"),
    )
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--attempt-ms", type=float, default=5.0)
    parser.add_argument("--backoff-ms", type=float, default=50.0)
    args = parser.parse_args()
    if args.concurrency < 2:
        parser.error("--concurrency must be at least 2")
    if args.attempt_ms <= 0 or args.backoff_ms <= 0:
        parser.error("timing values must be positive")
    report = build_report(
        concurrency=args.concurrency,
        attempt_ms=args.attempt_ms,
        backoff_ms=args.backoff_ms,
    )
    write_report(report, args.out, args.markdown_out)
    print(json.dumps({"ok": True, "out": str(args.out), "markdown_out": str(args.markdown_out)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
