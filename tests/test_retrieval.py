from __future__ import annotations

from pathlib import Path
import sys
from threading import Event
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.retrieval import (
    MAX_ACTIVE_AUGMENTER_CALLS,
    HybridRetriever,
    RetrievalCandidate,
    SQLiteRetriever,
    active_augmenter_call_count,
)


class _Augmenter:
    enabled = True
    backend_name = "fixture-vector"
    transport_timeout_seconds = 1.0

    def __init__(self, values: list[object]) -> None:
        self.values = values

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> list[object]:
        del query, limit
        return self.values


class _DisabledAugmenter:
    enabled = False
    backend_name = "disabled-vector"
    transport_timeout_seconds = 1.0


class _FailingAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> list[str]:
        del query, limit
        raise RuntimeError("secret backend detail must not escape")


class _SlowAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> list[str]:
        del query, limit
        time.sleep(0.25)
        return ["late"]


class _InvalidAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> int:
        del query, limit
        return 42


class _MissingDeadlineAugmenter:
    enabled = True

    def __init__(self) -> None:
        self.called = False

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> list[str]:
        del query, limit
        self.called = True
        return ["must-not-run"]


class _HungAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def __init__(self, release: Event) -> None:
        self.release = release

    def retrieve_candidate_ids(self, query: str, *, limit: int) -> list[str]:
        del query, limit
        self.release.wait(timeout=self.transport_timeout_seconds)
        return ["released"]


class _DelayedGeneratorAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def retrieve_candidate_ids(self, query: str, *, limit: int):
        del query, limit

        def values():
            for index in range(20):
                time.sleep(0.04)
                yield f"delayed-{index}"

        return values()


class _InfiniteGeneratorAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def __init__(self) -> None:
        self.materialized = 0

    def retrieve_candidate_ids(self, query: str, *, limit: int):
        del query, limit
        index = 0
        while True:
            self.materialized += 1
            yield f"generated-{index}"
            index += 1


class _ExplodingGeneratorAugmenter:
    enabled = True
    transport_timeout_seconds = 1.0

    def retrieve_candidate_ids(self, query: str, *, limit: int):
        del query, limit
        yield "partial-result-must-not-escape"
        raise RuntimeError("secret iteration failure")


class RetrievalFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        deadline = time.monotonic() + 1.0
        while active_augmenter_call_count() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(active_augmenter_call_count(), 0)
        self.sqlite = SQLiteRetriever(lambda query, limit: ["unit-1", "unit-2"][:limit])

    def test_missing_and_disabled_augmenter_equal_sqlite_baseline(self) -> None:
        baseline = self.sqlite.retrieve("인사", limit=5)
        missing = HybridRetriever(self.sqlite).retrieve("인사", limit=5)
        disabled = HybridRetriever(
            self.sqlite,
            _DisabledAugmenter(),
        ).retrieve("인사", limit=5)

        self.assertEqual(missing.candidates, baseline.candidates)
        self.assertEqual(disabled.candidates, baseline.candidates)
        self.assertEqual(missing.audit["augmenter"]["state"], "missing")
        self.assertEqual(disabled.audit["augmenter"]["state"], "disabled")

    def test_missing_backend_transport_deadline_falls_back_without_calling(self) -> None:
        augmenter = _MissingDeadlineAugmenter()
        baseline = self.sqlite.retrieve("인사", limit=5)
        result = HybridRetriever(self.sqlite, augmenter).retrieve("인사", limit=5)

        self.assertEqual(result.candidates, baseline.candidates)
        self.assertFalse(augmenter.called)
        self.assertEqual(
            result.audit["augmenter"]["state"],
            "transport_deadline_missing",
        )

    def test_error_and_timeout_return_baseline_unchanged_with_safe_audit(self) -> None:
        baseline = self.sqlite.retrieve("인사", limit=5)
        failed = HybridRetriever(
            self.sqlite,
            _FailingAugmenter(),
        ).retrieve("인사", limit=5)
        started = time.monotonic()
        timed_out = HybridRetriever(
            self.sqlite,
            _SlowAugmenter(),
            timeout_seconds=0.02,
        ).retrieve("인사", limit=5)

        self.assertEqual(failed.candidates, baseline.candidates)
        self.assertEqual(timed_out.candidates, baseline.candidates)
        self.assertEqual(failed.audit["augmenter"]["state"], "error")
        self.assertEqual(failed.audit["augmenter"]["error_type"], "RuntimeError")
        self.assertNotIn("secret", str(failed.audit))
        self.assertEqual(timed_out.audit["augmenter"]["state"], "timeout")
        self.assertLess(time.monotonic() - started, 0.2)

        invalid = HybridRetriever(self.sqlite, _InvalidAugmenter()).retrieve(
            "인사",
            limit=5,
        )
        self.assertEqual(invalid.candidates, baseline.candidates)
        self.assertEqual(invalid.audit["augmenter"]["state"], "invalid_response")

    def test_repeated_hung_calls_exhaust_bounded_process_capacity(self) -> None:
        release = Event()
        augmenter = _HungAugmenter(release)
        retriever = HybridRetriever(
            self.sqlite,
            augmenter,
            timeout_seconds=0.005,
        )
        baseline = self.sqlite.retrieve("인사", limit=5)
        try:
            timed_out = [retriever.retrieve("인사", limit=5) for _ in range(MAX_ACTIVE_AUGMENTER_CALLS)]
            started = time.monotonic()
            capacity = retriever.retrieve("인사", limit=5)

            self.assertTrue(
                all(item.audit["augmenter"]["state"] == "timeout" for item in timed_out)
            )
            self.assertEqual(active_augmenter_call_count(), MAX_ACTIVE_AUGMENTER_CALLS)
            self.assertEqual(capacity.candidates, baseline.candidates)
            self.assertEqual(capacity.audit["augmenter"]["state"], "capacity_exhausted")
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            release.set()
            deadline = time.monotonic() + 1.0
            while active_augmenter_call_count() and time.monotonic() < deadline:
                time.sleep(0.005)
        self.assertEqual(active_augmenter_call_count(), 0)

    def test_delayed_generator_materialization_remains_inside_timeout(self) -> None:
        baseline = self.sqlite.retrieve("인사", limit=3)
        started = time.monotonic()
        result = HybridRetriever(
            self.sqlite,
            _DelayedGeneratorAugmenter(),
            timeout_seconds=0.005,
            max_augmented_candidates=3,
        ).retrieve("인사", limit=3)
        elapsed = time.monotonic() - started

        self.assertEqual(result.candidates, baseline.candidates)
        self.assertEqual(result.audit["augmenter"]["state"], "timeout")
        self.assertLess(elapsed, 0.1)
        deadline = time.monotonic() + 0.5
        while active_augmenter_call_count() and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_infinite_generator_is_materialized_to_limit_plus_one(self) -> None:
        augmenter = _InfiniteGeneratorAugmenter()
        result = HybridRetriever(
            self.sqlite,
            augmenter,
            max_augmented_candidates=3,
        ).retrieve("인사", limit=3)

        self.assertEqual(augmenter.materialized, 4)
        self.assertEqual(
            result.candidate_ids,
            ("unit-1", "unit-2", "generated-0", "generated-1", "generated-2"),
        )
        self.assertEqual(result.audit["augmenter"]["returned_count"], 4)
        self.assertTrue(result.audit["augmenter"]["truncated"])

    def test_generator_iteration_exception_returns_unmodified_baseline(self) -> None:
        baseline = self.sqlite.retrieve("인사", limit=5)
        result = HybridRetriever(
            self.sqlite,
            _ExplodingGeneratorAugmenter(),
        ).retrieve("인사", limit=5)

        self.assertEqual(result.candidates, baseline.candidates)
        self.assertEqual(result.audit["augmenter"]["state"], "error")
        self.assertEqual(result.audit["augmenter"]["error_type"], "RuntimeError")
        self.assertNotIn("secret", str(result.audit))
        self.assertNotIn("partial-result", str(result.candidate_ids))

    def test_augmentation_deduplicates_and_only_adds_safe_references(self) -> None:
        untrusted_payload = {
            "candidate_id": "unit-3",
            "review_status": "human_reviewed",
            "evidence": "invented",
        }
        result = HybridRetriever(
            self.sqlite,
            _Augmenter(["unit-2", untrusted_payload, "unit-3", "unit-4"]),
        ).retrieve("인사", limit=5)

        self.assertEqual(result.candidate_ids, ("unit-1", "unit-2", "unit-3", "unit-4"))
        vector_only = result.candidates[2:]
        self.assertTrue(all(item.candidate_status == "candidate_reference" for item in vector_only))
        self.assertTrue(all(not item.evidence_eligible for item in vector_only))
        self.assertTrue(all(item.payload.get("trusted_status") is False for item in vector_only))
        self.assertNotIn("human_reviewed", str(vector_only))
        self.assertEqual(result.audit["augmenter"]["added_count"], 2)
        self.assertFalse(result.audit["human_status_synthesized"])

    def test_sqlite_rows_remain_authoritative_and_keep_payload(self) -> None:
        sqlite = SQLiteRetriever(
            lambda query, limit: [
                {
                    "candidate_id": "course-1",
                    "review_status": "raw",
                    "evidence": "direct sqlite relation",
                }
            ]
        )
        result = HybridRetriever(sqlite, _Augmenter(["course-2"])).retrieve(
            "교육",
            limit=5,
        )

        baseline, augmented = result.candidates
        self.assertIsInstance(baseline, RetrievalCandidate)
        self.assertEqual(baseline.source, "sqlite")
        self.assertTrue(baseline.evidence_eligible)
        self.assertEqual(baseline.payload["evidence"], "direct sqlite relation")
        self.assertEqual(augmented.source, "optional_augmenter")
        self.assertFalse(augmented.evidence_eligible)


if __name__ == "__main__":
    unittest.main()
