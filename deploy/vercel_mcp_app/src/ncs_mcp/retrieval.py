"""SQLite-authoritative hybrid candidate retrieval foundations.

The optional augmenter is intentionally recall-only: it may add candidate IDs
but cannot create evidence, trusted review state, or alter SQLite candidates.
No external/vector backend package is required by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from queue import Empty, Queue
import re
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable


HYBRID_RETRIEVAL_SCHEMA = "ncs_hybrid_retrieval_v1"
FORBIDDEN_SYNTHETIC_STATUSES = frozenset(
    {"human_reviewed", "accepted", "reviewed"}
)
MAX_ACTIVE_AUGMENTER_CALLS = 4

# A timed-out Python thread cannot be killed safely.  Admission is therefore
# process-local and bounded: a stuck backend can occupy at most these slots,
# after which calls immediately fall back to SQLite.  Backends remain required
# to enforce their own finite socket/transport deadlines so slots eventually
# return to the process.
_AUGMENTER_ADMISSION = BoundedSemaphore(MAX_ACTIVE_AUGMENTER_CALLS)
_AUGMENTER_ACTIVITY_LOCK = Lock()
_active_augmenter_calls = 0


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """A bounded candidate reference with explicit evidence eligibility."""

    candidate_id: str
    source: str
    candidate_status: str
    evidence_eligible: bool
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def sqlite(
        cls,
        candidate_id: Any,
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> "RetrievalCandidate":
        return cls(
            candidate_id=_candidate_id(candidate_id),
            source="sqlite",
            candidate_status="authoritative_baseline",
            evidence_eligible=True,
            payload=dict(payload or {}),
        )

    @classmethod
    def augmenter_reference(cls, candidate_id: Any) -> "RetrievalCandidate":
        return cls(
            candidate_id=_candidate_id(candidate_id),
            source="optional_augmenter",
            candidate_status="candidate_reference",
            evidence_eligible=False,
            payload={
                "evidence_role": "candidate_reference_only",
                "trusted_status": False,
            },
        )

    @property
    def id(self) -> str:
        """Compatibility alias for ID-centric callers."""

        return self.candidate_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "source": self.source,
            "candidate_status": self.candidate_status,
            "evidence_eligible": self.evidence_eligible,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """Candidate set and non-authoritative backend audit information."""

    candidates: tuple[RetrievalCandidate, ...]
    audit: Mapping[str, Any] = field(default_factory=dict)

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.candidates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": HYBRID_RETRIEVAL_SCHEMA,
            "candidate_ids": list(self.candidate_ids),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "audit": dict(self.audit),
        }


@runtime_checkable
class Retriever(Protocol):
    """Protocol for the authoritative SQLite retrieval adapter."""

    def retrieve(self, query: str, *, limit: int) -> RetrievalResult:
        """Return authoritative baseline candidates."""


@runtime_checkable
class CandidateAugmenter(Protocol):
    """Optional recall backend; returned values are interpreted as IDs only.

    Networked implementations must enforce a finite transport deadline.  The
    HybridRetriever timeout bounds caller latency, but it cannot terminate a
    Python thread blocked inside a backend or its client library.
    """

    enabled: bool
    transport_timeout_seconds: float

    def retrieve_candidate_ids(
        self,
        query: str,
        *,
        limit: int,
    ) -> Sequence[Any] | Iterable[Any]:
        """Return candidate identifiers, never evidence."""


class SQLiteRetriever:
    """Small adapter around an existing SQLite-backed search callable.

    The callable is expected to perform the actual domain SQL and may return a
    :class:`RetrievalResult`, ID sequence, or mapping rows containing ``id`` or
    ``candidate_id``.  This keeps the foundation independent of one table.
    """

    enabled = True
    backend_name = "sqlite"

    def __init__(self, search: Callable[..., Any]) -> None:
        if not callable(search):
            raise TypeError("search must be callable")
        self._search = search

    def retrieve(self, query: str, *, limit: int = 20) -> RetrievalResult:
        bounded_limit = _validate_limit(limit)
        raw = self._search(query, limit=bounded_limit)
        result = _coerce_baseline_result(raw)
        return replace(
            result,
            audit={
                **dict(result.audit),
                "schema": HYBRID_RETRIEVAL_SCHEMA,
                "sqlite_authoritative": True,
                "baseline_count": len(result.candidates),
            },
        )

    search = retrieve


class HybridRetriever:
    """Merge an authoritative baseline with bounded candidate-only recall.

    Optional backends must configure finite transport deadlines.  Caller
    timeout and process-wide admission protect this surface, while the backend
    deadline guarantees a timed-out worker eventually releases its slot.
    """

    def __init__(
        self,
        sqlite_retriever: Retriever,
        augmenter: CandidateAugmenter | Any | None = None,
        *,
        timeout_seconds: float = 0.25,
        max_augmented_candidates: int = 20,
    ) -> None:
        if sqlite_retriever is None:
            raise TypeError("sqlite_retriever is required")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self.sqlite_retriever = sqlite_retriever
        self.augmenter = augmenter
        self.timeout_seconds = float(timeout_seconds)
        self.max_augmented_candidates = _validate_limit(max_augmented_candidates)

    def retrieve(self, query: str, *, limit: int = 20) -> RetrievalResult:
        """Retrieve baseline first and optionally broaden its candidate IDs."""

        bounded_limit = _validate_limit(limit)
        baseline = _call_baseline(self.sqlite_retriever, query, bounded_limit)
        baseline = _coerce_baseline_result(baseline)
        baseline_candidates = baseline.candidates
        started = monotonic()

        common_audit: dict[str, Any] = {
            **dict(baseline.audit),
            "schema": HYBRID_RETRIEVAL_SCHEMA,
            "sqlite_authoritative": True,
            "baseline_count": len(baseline_candidates),
            "augmenter_can_broaden_ids_only": True,
            "augmenter_evidence_eligible": False,
            "human_status_synthesized": False,
            "backend_transport_deadline_required": True,
            "max_active_augmenter_calls": MAX_ACTIVE_AUGMENTER_CALLS,
        }

        if self.augmenter is None:
            return replace(
                baseline,
                audit={
                    **common_audit,
                    "augmenter": _backend_audit("missing", elapsed_ms=0),
                },
            )
        if getattr(self.augmenter, "enabled", True) is False:
            return replace(
                baseline,
                audit={
                    **common_audit,
                    "augmenter": _backend_audit(
                        "disabled",
                        backend=self.augmenter,
                        elapsed_ms=0,
                    ),
                },
            )

        transport_timeout = _transport_timeout_seconds(self.augmenter)
        if transport_timeout is None:
            return replace(
                baseline,
                audit={
                    **common_audit,
                    "augmenter": _backend_audit(
                        "transport_deadline_missing",
                        backend=self.augmenter,
                        elapsed_ms=0,
                    ),
                },
            )

        augmentation_limit = min(bounded_limit, self.max_augmented_candidates)
        outcome, raw_or_error = _bounded_augmenter_call(
            self.augmenter,
            query=query,
            limit=augmentation_limit,
            timeout_seconds=self.timeout_seconds,
        )
        elapsed_ms = int((monotonic() - started) * 1000)
        if outcome != "success":
            error_type = (
                type(raw_or_error).__name__ if isinstance(raw_or_error, BaseException) else None
            )
            return replace(
                baseline,
                audit={
                    **common_audit,
                    "augmenter": _backend_audit(
                        outcome,
                        backend=self.augmenter,
                        elapsed_ms=elapsed_ms,
                        error_type=error_type,
                        transport_timeout_seconds=transport_timeout,
                    ),
                },
            )

        materialized = raw_or_error
        if not isinstance(materialized, _MaterializedAugmenterResponse):
            return replace(
                baseline,
                audit={
                    **common_audit,
                    "augmenter": _backend_audit(
                        "invalid_response",
                        backend=self.augmenter,
                        elapsed_ms=elapsed_ms,
                        error_type="InvalidMaterializedResponse",
                        transport_timeout_seconds=transport_timeout,
                    ),
                },
            )
        raw_ids = materialized.candidate_ids
        seen = set(baseline.candidate_ids)
        additions: list[RetrievalCandidate] = []
        invalid_count = 0
        duplicate_count = 0
        for raw_id in raw_ids:
            try:
                candidate_id = _candidate_id(raw_id)
            except (TypeError, ValueError):
                invalid_count += 1
                continue
            if candidate_id in seen:
                duplicate_count += 1
                continue
            seen.add(candidate_id)
            additions.append(RetrievalCandidate.augmenter_reference(candidate_id))
            if len(additions) >= self.max_augmented_candidates:
                break

        return RetrievalResult(
            candidates=(*baseline_candidates, *additions),
            audit={
                **common_audit,
                "augmenter": _backend_audit(
                    "success",
                    backend=self.augmenter,
                    elapsed_ms=elapsed_ms,
                    returned_count=materialized.materialized_count,
                    added_count=len(additions),
                    duplicate_count=duplicate_count,
                    invalid_count=invalid_count,
                    truncated=materialized.truncated,
                    transport_timeout_seconds=transport_timeout,
                ),
            },
        )

    search = retrieve


def _validate_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return limit


def _candidate_id(value: Any) -> str:
    if isinstance(value, RetrievalCandidate):
        value = value.candidate_id
    if isinstance(value, Mapping):
        value = value.get("candidate_id", value.get("id"))
    if value is None or isinstance(value, bool):
        raise ValueError("candidate ID must be a non-empty scalar")
    candidate_id = str(value).strip()
    if not candidate_id:
        raise ValueError("candidate ID must be a non-empty scalar")
    return candidate_id


def _coerce_baseline_candidate(value: Any) -> RetrievalCandidate:
    if isinstance(value, RetrievalCandidate):
        return value
    if isinstance(value, Mapping):
        candidate_id = _candidate_id(value)
        return RetrievalCandidate.sqlite(candidate_id, payload=dict(value))
    return RetrievalCandidate.sqlite(value)


def _coerce_baseline_result(raw: Any) -> RetrievalResult:
    if isinstance(raw, RetrievalResult):
        return raw
    audit: Mapping[str, Any] = {}
    candidates_raw = raw
    if isinstance(raw, Mapping):
        candidates_raw = raw.get("candidates", raw.get("candidate_ids", ()))
        supplied_audit = raw.get("audit", {})
        if isinstance(supplied_audit, Mapping):
            audit = supplied_audit
    if candidates_raw is None:
        candidates_raw = ()
    if isinstance(candidates_raw, (str, bytes)):
        candidates_raw = (candidates_raw,)

    candidates: list[RetrievalCandidate] = []
    seen: set[str] = set()
    for value in candidates_raw:
        candidate = _coerce_baseline_candidate(value)
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        candidates.append(candidate)
    return RetrievalResult(candidates=tuple(candidates), audit=dict(audit))


def _call_baseline(retriever: Any, query: str, limit: int) -> Any:
    method = getattr(retriever, "retrieve", None)
    if not callable(method):
        method = getattr(retriever, "search", None)
    if not callable(method):
        raise TypeError("sqlite_retriever must expose retrieve() or search()")
    return method(query, limit=limit)


def _call_augmenter(augmenter: Any, query: str, limit: int) -> Any:
    for name in ("retrieve_candidate_ids", "retrieve", "search"):
        method = getattr(augmenter, name, None)
        if callable(method):
            return method(query, limit=limit)
    if callable(augmenter):
        return augmenter(query, limit=limit)
    raise TypeError("augmenter must expose retrieve_candidate_ids(), retrieve(), or search()")


def _transport_timeout_seconds(augmenter: Any) -> float | None:
    """Read the required finite backend deadline without exposing backend data."""

    try:
        value = getattr(augmenter, "transport_timeout_seconds")
    except Exception:
        return None
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


@dataclass(frozen=True, slots=True)
class _MaterializedAugmenterResponse:
    candidate_ids: tuple[Any, ...]
    materialized_count: int
    truncated: bool


class _InvalidAugmenterResponse(TypeError):
    """Internal signal for a non-iterable or malformed backend response."""


def _materialize_candidate_ids(
    raw: Any,
    *,
    limit: int,
) -> _MaterializedAugmenterResponse:
    """Consume no more than ``limit + 1`` values inside timeout isolation."""

    if isinstance(raw, RetrievalResult):
        values: Any = raw.candidate_ids
    elif isinstance(raw, Mapping):
        if "candidate_ids" in raw:
            values = raw["candidate_ids"]
        elif "candidates" in raw:
            values = raw["candidates"]
        elif "candidate_id" in raw or "id" in raw:
            values = (raw,)
        else:
            raise _InvalidAugmenterResponse(
                "mapping response must contain candidate_ids, candidates, candidate_id, or id"
            )
    elif raw is None:
        values = ()
    elif isinstance(raw, (str, bytes)):
        values = (raw,)
    else:
        values = raw

    try:
        iterator = iter(values)
    except TypeError as exc:
        raise _InvalidAugmenterResponse(
            "augmenter response must be an iterable of candidate IDs"
        ) from exc

    captured: list[Any] = []
    for _ in range(limit + 1):
        try:
            captured.append(next(iterator))
        except StopIteration:
            break
    truncated = len(captured) > limit
    return _MaterializedAugmenterResponse(
        candidate_ids=tuple(captured[:limit]),
        materialized_count=len(captured),
        truncated=truncated,
    )


def _bounded_augmenter_call(
    augmenter: Any,
    *,
    query: str,
    limit: int,
    timeout_seconds: float,
) -> tuple[str, Any]:
    """Run and materialize a backend call within bounded process admission."""

    if not _AUGMENTER_ADMISSION.acquire(blocking=False):
        return "capacity_exhausted", None
    global _active_augmenter_calls
    with _AUGMENTER_ACTIVITY_LOCK:
        _active_augmenter_calls += 1

    output: Queue[tuple[str, Any]] = Queue(maxsize=1)

    def invoke() -> None:
        global _active_augmenter_calls
        try:
            raw = _call_augmenter(augmenter, query, limit)
            materialized = _materialize_candidate_ids(raw, limit=limit)
            output.put(("success", materialized))
        except _InvalidAugmenterResponse as exc:
            output.put(("invalid_response", exc))
        except Exception as exc:
            # Iteration is deliberately inside this boundary, so generator
            # exceptions are safe backend errors and no partial IDs escape.
            output.put(("error", exc))
        finally:
            _AUGMENTER_ADMISSION.release()
            with _AUGMENTER_ACTIVITY_LOCK:
                _active_augmenter_calls -= 1

    worker = Thread(target=invoke, name="ncs-optional-retrieval", daemon=True)
    try:
        worker.start()
    except Exception as exc:
        _AUGMENTER_ADMISSION.release()
        with _AUGMENTER_ACTIVITY_LOCK:
            _active_augmenter_calls -= 1
        return "error", exc
    try:
        return output.get(timeout=timeout_seconds)
    except Empty:
        return "timeout", None


def active_augmenter_call_count() -> int:
    """Return process-local in-flight calls for health checks and tests."""

    with _AUGMENTER_ACTIVITY_LOCK:
        return _active_augmenter_calls


def _safe_backend_name(backend: Any) -> str:
    for attribute in ("backend_name", "provider_name", "name"):
        try:
            value = getattr(backend, attribute, None)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip())
            return normalized[:80]
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", type(backend).__name__)[:80]


def _backend_audit(
    state: str,
    *,
    backend: Any | None = None,
    elapsed_ms: int,
    error_type: str | None = None,
    returned_count: int = 0,
    added_count: int = 0,
    duplicate_count: int = 0,
    invalid_count: int = 0,
    truncated: bool = False,
    transport_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    audit: dict[str, Any] = {
        "state": state,
        "backend": _safe_backend_name(backend) if backend is not None else None,
        "elapsed_ms": max(int(elapsed_ms), 0),
        "returned_count": returned_count,
        "added_count": added_count,
        "duplicate_count": duplicate_count,
        "invalid_count": invalid_count,
        "truncated": bool(truncated),
        "candidate_only": True,
        "evidence_eligible": False,
        "trusted_status_allowed": False,
        "transport_deadline_required": True,
    }
    if transport_timeout_seconds is not None:
        audit["transport_timeout_seconds"] = transport_timeout_seconds
    if error_type:
        audit["error_type"] = error_type[:80]
    return audit


__all__ = [
    "CandidateAugmenter",
    "FORBIDDEN_SYNTHETIC_STATUSES",
    "HYBRID_RETRIEVAL_SCHEMA",
    "HybridRetriever",
    "MAX_ACTIVE_AUGMENTER_CALLS",
    "RetrievalCandidate",
    "RetrievalResult",
    "Retriever",
    "SQLiteRetriever",
    "active_augmenter_call_count",
]
