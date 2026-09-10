"""Contracts for organization-owned internal roles and NCS alignment candidates.

This module deliberately contains no persistence or seed data.  Internal roles
are tenant-scoped records supplied by an organization; an alignment candidate is
only a reviewable hypothesis and never an approval or HR decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import copy
import hashlib
import math
import re
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
import unicodedata


INTERNAL_JOB_ROLE_SCHEMA = "internal_job_role_v1"
ROLE_ALIGNMENT_CANDIDATE_SCHEMA = "internal_job_role_alignment_candidate_v1"
ROLE_PROVENANCE_SCHEMA = "internal_job_role_provenance_v1"

ALIGNMENT_STATUSES = frozenset(
    {"candidate", "review_required", "ambiguous", "unresolved"}
)

# These names are rejected at the contract boundary.  A role describes a job,
# not a person holding the job.  Matching is intentionally conservative so
# variants such as employeeId and personal_email cannot leak through.
_FORBIDDEN_FIELD_TOKENS = frozenset(
    {
        "employee",
        "employeeid",
        "employeename",
        "person",
        "personid",
        "personal",
        "email",
        "phone",
        "mobile",
        "telephone",
        "address",
        "resident",
        "residentregistration",
        "ssn",
        "socialsecurity",
        "birth",
        "birthday",
        "dateofbirth",
        "userid",
        "username",
        "useremail",
        "reviewerid",
    }
)

# Korean exports commonly use these labels instead of English employee/PII
# field names.  Keep the broad matching limited to clearly personal labels;
# for example, ``직무이름`` remains a valid descriptive role field while an
# exact ``이름`` key is rejected.
_KOREAN_FORBIDDEN_FIELD_TOKENS = frozenset(
    {
        "사번",
        "사원번호",
        "직원번호",
        "근로자번호",
        "성명",
        "이름",
        "주민등록번호",
        "주민번호",
        "개인식별번호",
        "이메일",
        "전자우편",
        "연락처",
        "전화번호",
        "휴대전화",
        "휴대폰",
        "핸드폰",
        "주소",
        "생년월일",
    }
)


class ContractValidationError(ValueError):
    """Raised when an input cannot be represented safely by a contract."""


def normalize_semantic_text(value: Any) -> str:
    """Return deterministic, non-destructive text for semantic matching.

    Only a derived value is normalized: caller-owned strings and containers are
    never modified.  NFKC handles width variants, casefold handles casing, and
    whitespace collapsing avoids source-format differences without stripping
    meaningful punctuation or language-specific characters.
    """

    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _canonical_identity(value: Any, field: str) -> str:
    normalized = normalize_semantic_text(value)
    if not normalized:
        raise ContractValidationError(f"{field} must not be empty")
    # Identity comparison is stable across harmless separator and whitespace
    # differences while the original value remains available in the record.
    canonical = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-")
    if not canonical:
        raise ContractValidationError(f"{field} must contain an identifier")
    return canonical


def deterministic_gold_id(organization_namespace: str, role_id: str) -> str:
    """Compute the stable tenant-scoped role ID without storing anything."""

    namespace = _canonical_identity(organization_namespace, "organization_namespace")
    identifier = _canonical_identity(role_id, "role_id")
    digest = hashlib.sha256(f"{namespace}:{identifier}".encode("utf-8")).hexdigest()
    return f"ijr_{digest}"


generate_role_gold_id = deterministic_gold_id


def _field_token(key: Any) -> str:
    # ``\w`` is Unicode-aware in Python, so this preserves Hangul and other
    # identifier characters while dropping separators such as ``_`` and ``-``.
    # Underscore is part of ``\w`` in Python, hence it is excluded explicitly.
    return re.sub(
        r"[\W_]", "", unicodedata.normalize("NFKC", str(key)).casefold()
    )


def _is_forbidden_key(key: Any) -> bool:
    token = _field_token(key)
    if not token:
        return False
    if any(
        token == forbidden or token.endswith(forbidden) or forbidden in token
        for forbidden in _FORBIDDEN_FIELD_TOKENS
    ):
        return True
    # Generic Korean words such as 이름 are only treated as sensitive when
    # used as a field label on their own.  Other labels are distinctive enough
    # to match compound variants (e.g. employee_사번, 주민번호_hash).
    return any(
        token == forbidden or (forbidden != "이름" and forbidden in token)
        for forbidden in _KOREAN_FORBIDDEN_FIELD_TOKENS
    )


def _validate_no_forbidden_fields(value: Any, *, path: str = "record") -> None:
    """Reject personal/employee keys, including keys nested in evidence."""

    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _is_forbidden_key(key):
                raise ContractValidationError(
                    f"forbidden personal or employee field: {path}.{key}"
                )
            _validate_no_forbidden_fields(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, nested in enumerate(value):
            _validate_no_forbidden_fields(nested, path=f"{path}[{index}]")


def _freeze(value: Any) -> Any:
    """Make caller-owned nested values immutable without requiring a package."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_thaw(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_thaw(item) for item in value), key=repr)
    return value


def _text_items(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    else:
        if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
            raise ContractValidationError(f"{field} must be a sequence of strings")
        values = value
    result: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ContractValidationError(f"{field} items must be strings")
        result.append(item)
    return tuple(result)


def _copy_mapping(value: Mapping[str, Any] | None, field: str) -> Mapping[str, Any]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ContractValidationError(f"{field} must be an object")
    copied = copy.deepcopy(dict(value))
    _validate_no_forbidden_fields(copied, path=field)
    return _freeze(copied)


def _iso_date(value: str | date | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise ContractValidationError("effective_date must be an ISO date string")
    text = value.strip()
    if text:
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise ContractValidationError(
                "effective_date must be an ISO date (YYYY-MM-DD)"
            ) from exc
    return text or None


@dataclass(frozen=True, slots=True)
class InternalJobRole:
    """An immutable, tenant-scoped description of an organization role."""

    organization_namespace: str
    role_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    description: str = ""
    duties: tuple[str, ...] = ()
    target_level: str | None = None
    source: str | None = None
    effective_date: str | date | None = None
    provenance: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.organization_namespace, str):
            raise ContractValidationError("organization_namespace must be a string")
        if not isinstance(self.role_id, str):
            raise ContractValidationError("role_id must be a string")
        if not isinstance(self.display_name, str):
            raise ContractValidationError("display_name must be a string")
        _canonical_identity(self.organization_namespace, "organization_namespace")
        _canonical_identity(self.role_id, "role_id")
        if not normalize_semantic_text(self.display_name):
            raise ContractValidationError("display_name must not be empty")
        aliases = _text_items(self.aliases, "aliases")
        duties = _text_items(self.duties, "duties")
        if any(not normalize_semantic_text(item) for item in aliases):
            raise ContractValidationError("aliases must not contain empty values")
        if any(not normalize_semantic_text(item) for item in duties):
            raise ContractValidationError("duties must not contain empty values")
        if not isinstance(self.description, str):
            raise ContractValidationError("description must be a string")
        if self.target_level is not None and not isinstance(self.target_level, str):
            raise ContractValidationError("target_level must be a string or null")
        if self.source is not None and not isinstance(self.source, str):
            raise ContractValidationError("source must be a string or null")
        object.__setattr__(self, "aliases", tuple(copy.deepcopy(aliases)))
        object.__setattr__(self, "duties", tuple(copy.deepcopy(duties)))
        object.__setattr__(self, "provenance", _copy_mapping(self.provenance, "provenance"))
        object.__setattr__(self, "effective_date", _iso_date(self.effective_date))

    @property
    def canonical_organization_namespace(self) -> str:
        return _canonical_identity(self.organization_namespace, "organization_namespace")

    @property
    def canonical_role_id(self) -> str:
        return _canonical_identity(self.role_id, "role_id")

    @property
    def gold_id(self) -> str:
        """Stable tenant-scoped ID based only on the role identity."""

        return deterministic_gold_id(self.organization_namespace, self.role_id)

    @property
    def stable_id(self) -> str:
        return self.gold_id

    @property
    def normalized_semantic_text(self) -> str:
        values = [self.display_name, *sorted(self.aliases, key=normalize_semantic_text)]
        if self.description:
            values.append(self.description)
        values.extend(sorted(self.duties, key=normalize_semantic_text))
        if self.target_level:
            values.append(self.target_level)
        return normalize_semantic_text(" | ".join(values))

    @property
    def semantic_text(self) -> str:
        return self.normalized_semantic_text

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": INTERNAL_JOB_ROLE_SCHEMA,
            "organization_namespace": self.organization_namespace,
            "role_id": self.role_id,
            "gold_id": self.gold_id,
            "display_name": self.display_name,
            "aliases": list(self.aliases),
            "description": self.description,
            "duties": list(self.duties),
            "target_level": self.target_level,
            "source": self.source,
            "effective_date": self.effective_date,
            "normalized_semantic_text": self.normalized_semantic_text,
            "provenance": _thaw(self.provenance),
        }

    def to_public_dict(self) -> dict[str, Any]:
        """Return the fields safe for an external/public response."""

        result = self.to_dict()
        result["provenance"] = _public_safe(self.provenance)
        return result

    public_projection = to_public_dict

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "InternalJobRole":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("role payload must be an object")
        copied = copy.deepcopy(dict(payload))
        _validate_no_forbidden_fields(copied)
        aliases = {"name": "display_name", "role_name": "display_name", "id": "role_id"}
        normalized: dict[str, Any] = {}
        allowed = {
            "organization_namespace", "role_id", "display_name", "aliases",
            "description", "duties", "target_level", "source", "effective_date",
            "provenance", "schema",
        }
        for key, value in copied.items():
            target = aliases.get(key, key)
            if target not in allowed:
                raise ContractValidationError(f"unknown role field: {key}")
            if target != "schema":
                normalized[target] = value
        return cls(**normalized)


def validate_internal_job_role(payload: InternalJobRole | Mapping[str, Any]) -> InternalJobRole:
    """Validate and return an immutable role contract."""

    if isinstance(payload, InternalJobRole):
        return payload
    return InternalJobRole.from_mapping(payload)


def validate_alignment_candidate(
    payload: RoleAlignmentCandidate | Mapping[str, Any],
) -> RoleAlignmentCandidate:
    """Validate and return an immutable alignment candidate contract."""

    if isinstance(payload, RoleAlignmentCandidate):
        return payload
    return RoleAlignmentCandidate.from_mapping(payload)


def _public_safe(value: Any) -> Any:
    """Recursively omit personal/employee fields from public projections."""

    if isinstance(value, Mapping):
        return {
            str(key): _public_safe(item)
            for key, item in value.items()
            if not _is_forbidden_key(key)
        }
    if isinstance(value, (tuple, list)):
        return [_public_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_public_safe(item) for item in value), key=repr)
    return value


def public_safe_projection(value: Any) -> Any:
    """Redact personal/employee keys from an arbitrary response fragment.

    Contract constructors reject such fields; this helper is for defense in
    depth when projecting legacy or externally supplied evidence fragments.
    """

    return _public_safe(copy.deepcopy(value))


@dataclass(frozen=True, slots=True)
class RoleAlignmentCandidate:
    """A non-authoritative, reviewable role-to-NCS alignment hypothesis."""

    role_gold_id: str
    ncs_target_type: str
    ncs_target_key: str
    score: float
    method: str
    model: str | None = None
    evidence: tuple[Any, ...] = ()
    status: str = "candidate"
    provenance: Mapping[str, Any] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not isinstance(self.role_gold_id, str) or not normalize_semantic_text(self.role_gold_id):
            raise ContractValidationError("role_gold_id must not be empty")
        for field in ("ncs_target_type", "ncs_target_key", "method"):
            value = getattr(self, field)
            if not isinstance(value, str) or not normalize_semantic_text(value):
                raise ContractValidationError(f"{field} must not be empty")
        if self.model is not None and not isinstance(self.model, str):
            raise ContractValidationError("model must be a string or null")
        if not isinstance(self.score, (int, float)) or isinstance(self.score, bool):
            raise ContractValidationError("score must be a number")
        if not math.isfinite(float(self.score)) or not 0 <= float(self.score) <= 1:
            raise ContractValidationError("score must be between 0 and 1")
        if self.status not in ALIGNMENT_STATUSES:
            raise ContractValidationError(
                f"status must be one of {sorted(ALIGNMENT_STATUSES)}"
            )
        evidence = self.evidence if self.evidence is not None else ()
        if isinstance(evidence, Mapping):
            evidence = (evidence,)
        elif isinstance(evidence, (str, bytes, bytearray)) or not isinstance(evidence, Sequence):
            raise ContractValidationError("evidence must be a sequence or object")
        copied_evidence = copy.deepcopy(list(evidence))
        _validate_no_forbidden_fields(copied_evidence, path="evidence")
        object.__setattr__(self, "evidence", tuple(_freeze(item) for item in copied_evidence))
        object.__setattr__(self, "provenance", _copy_mapping(self.provenance, "provenance"))

    @property
    def gold_id(self) -> str:
        return self.role_gold_id

    @property
    def target_type(self) -> str:
        return self.ncs_target_type

    @property
    def target_key(self) -> str:
        return self.ncs_target_key

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": ROLE_ALIGNMENT_CANDIDATE_SCHEMA,
            "role_gold_id": self.role_gold_id,
            "ncs_target_type": self.ncs_target_type,
            "ncs_target_key": self.ncs_target_key,
            "score": float(self.score),
            "method": self.method,
            "model": self.model,
            "evidence": [_thaw(item) for item in self.evidence],
            "status": self.status,
            "provenance": _thaw(self.provenance),
        }

    def to_public_dict(self) -> dict[str, Any]:
        result = self.to_dict()
        result["evidence"] = _public_safe(result["evidence"])
        result["provenance"] = _public_safe(self.provenance)
        return result

    public_projection = to_public_dict

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> "RoleAlignmentCandidate":
        if not isinstance(payload, Mapping):
            raise ContractValidationError("alignment candidate payload must be an object")
        copied = copy.deepcopy(dict(payload))
        _validate_no_forbidden_fields(copied)
        aliases = {
            "gold_id": "role_gold_id",
            "target_type": "ncs_target_type",
            "target_key": "ncs_target_key",
        }
        allowed = {
            "role_gold_id", "ncs_target_type", "ncs_target_key", "score", "method",
            "model", "evidence", "status", "provenance", "schema",
        }
        normalized: dict[str, Any] = {}
        for key, value in copied.items():
            target = aliases.get(key, key)
            if target not in allowed:
                raise ContractValidationError(f"unknown alignment candidate field: {key}")
            if target != "schema":
                normalized[target] = value
        return cls(**normalized)


# Explicit aliases make the contract discoverable without creating separate
# types that could drift in validation or redaction behavior.
InternalJobRoleAlignmentCandidate = RoleAlignmentCandidate
AlignmentCandidate = RoleAlignmentCandidate
NCSAlignmentCandidate = RoleAlignmentCandidate


def public_safe_response(
    role: InternalJobRole | Mapping[str, Any],
    candidates: Iterable[RoleAlignmentCandidate | Mapping[str, Any]],
) -> dict[str, Any]:
    """Build a public response while keeping role and candidate provenance."""

    validated_role = validate_internal_job_role(role)
    validated_candidates: list[RoleAlignmentCandidate] = []
    for candidate in candidates:
        if isinstance(candidate, RoleAlignmentCandidate):
            validated_candidates.append(candidate)
        elif isinstance(candidate, Mapping):
            validated_candidates.append(RoleAlignmentCandidate.from_mapping(candidate))
        else:
            raise ContractValidationError("candidates must contain alignment contracts")
    for candidate in validated_candidates:
        if candidate.role_gold_id != validated_role.gold_id:
            raise ContractValidationError(
                "alignment candidate role_gold_id does not match the role identity"
            )
    return {
        "schema": "internal_job_role_public_response_v1",
        "role": validated_role.to_public_dict(),
        "alignment_candidates": [item.to_public_dict() for item in validated_candidates],
        "provenance": {
            "schema": ROLE_PROVENANCE_SCHEMA,
            "role_gold_id": validated_role.gold_id,
            "candidate_count": len(validated_candidates),
        },
    }


to_public_response = public_safe_response


__all__ = [
    "ALIGNMENT_STATUSES",
    "AlignmentCandidate",
    "ContractValidationError",
    "INTERNAL_JOB_ROLE_SCHEMA",
    "InternalJobRole",
    "InternalJobRoleAlignmentCandidate",
    "NCSAlignmentCandidate",
    "ROLE_ALIGNMENT_CANDIDATE_SCHEMA",
    "RoleAlignmentCandidate",
    "normalize_semantic_text",
    "deterministic_gold_id",
    "generate_role_gold_id",
    "public_safe_projection",
    "public_safe_response",
    "to_public_response",
    "validate_internal_job_role",
    "validate_alignment_candidate",
]
