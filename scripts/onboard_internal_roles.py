"""Validate enterprise roles and prepare candidate-only NCS mapping packets.

The input contains role descriptions, never employee or incumbent records.
Every operation is report-only/read-only: this script does not write SQLite,
Neo4j, review statuses, or approval decisions.

Personal-data screening is a bounded heuristic DLP guard for common identifiers,
not a proof that free text contains no personal or sensitive information.  An
organization must still apply its own DLP and human review before onboarding.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DEFAULT_DATABASE = ROOT / "data" / "processed" / "ncs.db"
DEFAULT_TEMPLATE = ROOT / "examples" / "internal_role_onboarding.template.json"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ncs_mcp.internal_job_roles import (  # noqa: E402
    ALIGNMENT_STATUSES,
    ContractValidationError,
    InternalJobRole,
    deterministic_gold_id,
    normalize_semantic_text,
    validate_alignment_candidate,
)
from ncs_mcp.internal_role_mapping import (  # noqa: E402
    INTERNAL_ROLE_MAPPING_PACKET_SCHEMA,
    map_internal_job_roles,
)
from ncs_mcp.builder_gold import load_internal_role_mapping_packet  # noqa: E402


INPUT_SCHEMA = "enterprise_internal_role_onboarding_v1"
VALIDATION_SCHEMA = "enterprise_internal_role_onboarding_validation_v1"
PACKET_SCHEMA = "enterprise_internal_role_onboarding_packet_v1"
PACKET_CHECK_SCHEMA = "enterprise_internal_role_onboarding_packet_check_v1"

ROOT_FIELDS = frozenset(
    {"schema", "organization_namespace", "template_only", "roles"}
)
ROLE_FIELDS = frozenset(
    {
        "role_id",
        "role_name",
        "dept",
        "job_description",
        "tasks",
        "skills",
        "target_level",
        "effective_date",
        "source_ref",
    }
)
REQUIRED_ROLE_FIELDS = frozenset(
    {"role_id", "role_name", "dept", "job_description", "tasks", "skills"}
)
ROLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,63}$")
EMAIL_RE = re.compile(
    r"(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}"
    r"(?![A-Z0-9.-])",
    re.IGNORECASE,
)
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+82[ .-]?(?:10|[2-6][1-5]?)|"
    r"(?:01[016789]|0[2-6][1-5]?))[ .-]?\d{3,4}[ .-]?\d{4}(?!\d)"
)
RESIDENT_ID_RE = re.compile(r"(?<!\d)\d{6}[ -]?[1-8]\d{6}(?!\d)")
LABELED_PERSON_NAME_RE = re.compile(
    r"(?:담당자\s*)?(?:성명|이름|담당자명|담당자|직원명|사원명|재직자명|"
    r"현직자명|person[ _-]*name|employee[ _-]*name)\s*[:=]\s*"
    r"[A-Z가-힣][A-Z가-힣 .'-]{1,80}",
    re.IGNORECASE,
)
EMPLOYEE_ID_RE = re.compile(
    r"(?:사번|사원번호|직원번호|근로자번호|employee[ _-]*id)\s*[:=]\s*"
    r"[A-Z0-9._-]{2,64}",
    re.IGNORECASE,
)
DATE_OF_BIRTH_RE = re.compile(
    r"(?:생년월일|출생일|date[ _-]*of[ _-]*birth|dob)\s*[:=]\s*"
    r"(?:19|20)?\d{2}[./ -]\d{1,2}[./ -]\d{1,2}",
    re.IGNORECASE,
)
LABELED_ADDRESS_RE = re.compile(
    r"(?:주소|거주지|자택주소|도로명주소|street[ _-]*address|address)\s*[:=]\s*"
    r"\S.{3,200}",
    re.IGNORECASE,
)
FORBIDDEN_VALUE_PATTERNS = (
    ("email_address", EMAIL_RE),
    ("phone_number", PHONE_RE),
    ("resident_registration_number", RESIDENT_ID_RE),
    ("labeled_person_name", LABELED_PERSON_NAME_RE),
    ("employee_identifier", EMPLOYEE_ID_RE),
    ("date_of_birth", DATE_OF_BIRTH_RE),
    ("labeled_street_or_address", LABELED_ADDRESS_RE),
)
FORBIDDEN_STATUS_VALUES = frozenset({"human_reviewed", "accepted", "reviewed"})
PII_SCREENING_METHOD = "heuristic_dlp_pattern_screen_v1"
PII_RESIDUAL_RISK = (
    "This heuristic result is not proof that free text contains no personal or "
    "sensitive data; organizational DLP and human review remain required."
)


class OnboardingValidationError(ValueError):
    """Raised when an onboarding input or packet violates the safe contract."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pii_screening(result: str = "no_known_pattern_detected") -> dict[str, str]:
    return {
        "method": PII_SCREENING_METHOD,
        "result": result,
        "residual_risk": PII_RESIDUAL_RISK,
    }


def _sha256_file(path: str | Path) -> tuple[str, int]:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"source file does not exist: {source}")
    digest = hashlib.sha256()
    byte_count = 0
    with source.open("rb") as handle:
        while True:
            chunk = handle.read(8 * 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
    return digest.hexdigest(), byte_count


def _canonical_json_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _without_generated_at(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _without_generated_at(nested)
            for key, nested in value.items()
            if str(key) != "generated_at"
        }
    if isinstance(value, list):
        return [_without_generated_at(item) for item in value]
    return value


def _mapping_fingerprint(value: Mapping[str, Any]) -> str:
    return _canonical_json_sha256(_without_generated_at(value))


def _decorate_mapping_packet(
    mapping_packet: dict[str, Any],
    *,
    template_only: bool,
    source_db_sha256: str,
) -> dict[str, Any]:
    mapping_packet.update(
        {
            "candidate_only": True,
            "human_review_required": True,
            "status_update_allowed": False,
            "db_writes": False,
            "neo4j_writes": False,
            "approval_claim": False,
            "template_only": template_only,
            "production_eligible": not template_only,
            "source_db_sha256": source_db_sha256,
        }
    )
    return mapping_packet


def _strict_keys(
    value: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    required: frozenset[str],
    path: str,
) -> None:
    keys = {str(key) for key in value}
    unknown = sorted(keys - allowed)
    missing = sorted(required - keys)
    if unknown:
        raise OnboardingValidationError(
            f"{path} contains unknown fields: {', '.join(unknown)}"
        )
    if missing:
        raise OnboardingValidationError(
            f"{path} is missing required fields: {', '.join(missing)}"
        )


def _text(
    value: Any,
    *,
    path: str,
    minimum: int = 1,
    maximum: int,
) -> str:
    if not isinstance(value, str):
        raise OnboardingValidationError(f"{path} must be a string")
    stripped = value.strip()
    if not minimum <= len(stripped) <= maximum:
        raise OnboardingValidationError(
            f"{path} length must be between {minimum} and {maximum} characters"
        )
    _reject_pii(stripped, path=path)
    return stripped


def _optional_text(
    value: Any,
    *,
    path: str,
    maximum: int,
) -> str | None:
    if value is None:
        return None
    return _text(value, path=path, maximum=maximum)


def _reject_pii(value: str, *, path: str) -> None:
    for label, pattern in FORBIDDEN_VALUE_PATTERNS:
        if pattern.search(value):
            raise OnboardingValidationError(
                f"{path} contains a prohibited personal identifier pattern: {label}"
            )


def _text_list(
    value: Any,
    *,
    path: str,
    maximum_items: int = 50,
    maximum_length: int = 500,
) -> tuple[str, ...]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
    ):
        raise OnboardingValidationError(f"{path} must be an array of strings")
    if not 1 <= len(value) <= maximum_items:
        raise OnboardingValidationError(
            f"{path} must contain between 1 and {maximum_items} items"
        )
    result: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        text = _text(
            item,
            path=f"{path}[{index}]",
            minimum=2,
            maximum=maximum_length,
        )
        key = normalize_semantic_text(text)
        if key in seen:
            raise OnboardingValidationError(f"{path} contains duplicate items")
        seen.add(key)
        result.append(text)
    return tuple(result)


def _iso_date(value: Any, *, path: str) -> str | None:
    if value is None:
        return None
    text = _text(value, path=path, maximum=10)
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise OnboardingValidationError(
            f"{path} must be an ISO date (YYYY-MM-DD)"
        ) from exc
    return text


@dataclass(frozen=True, slots=True)
class EnterpriseRoleProfile:
    role_id: str
    role_name: str
    dept: str
    job_description: str
    tasks: tuple[str, ...]
    skills: tuple[str, ...]
    target_level: str | None = None
    effective_date: str | None = None
    source_ref: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "role_name": self.role_name,
            "dept": self.dept,
            "job_description": self.job_description,
            "tasks": list(self.tasks),
            "skills": list(self.skills),
            "target_level": self.target_level,
            "effective_date": self.effective_date,
            "source_ref": self.source_ref,
        }

    def to_internal_role(self, organization_namespace: str) -> InternalJobRole:
        semantic_description = "\n".join(
            (
                f"소속 기능: {self.dept}",
                self.job_description,
                "요구 기술: " + "; ".join(self.skills),
            )
        )
        return InternalJobRole(
            organization_namespace=organization_namespace,
            role_id=self.role_id,
            display_name=self.role_name,
            description=semantic_description,
            duties=self.tasks,
            target_level=self.target_level,
            source="enterprise_internal_role_onboarding",
            effective_date=self.effective_date,
            provenance={
                "input_schema": INPUT_SCHEMA,
                "department": self.dept,
                "skills": list(self.skills),
                "source_ref": self.source_ref,
            },
        )


@dataclass(frozen=True, slots=True)
class EnterpriseRoleBatch:
    organization_namespace: str
    template_only: bool
    roles: tuple[EnterpriseRoleProfile, ...]

    def to_source_dict(self) -> dict[str, Any]:
        return {
            "schema": INPUT_SCHEMA,
            "organization_namespace": self.organization_namespace,
            "template_only": self.template_only,
            "roles": [role.to_dict() for role in self.roles],
        }

    def to_internal_roles(self) -> list[InternalJobRole]:
        return [
            role.to_internal_role(self.organization_namespace) for role in self.roles
        ]


def _validate_role_profile(payload: Mapping[str, Any], *, index: int) -> EnterpriseRoleProfile:
    path = f"roles[{index}]"
    _strict_keys(
        payload,
        allowed=ROLE_FIELDS,
        required=REQUIRED_ROLE_FIELDS,
        path=path,
    )
    role_id = _text(payload["role_id"], path=f"{path}.role_id", maximum=64)
    if ROLE_ID_RE.fullmatch(role_id) is None:
        raise OnboardingValidationError(
            f"{path}.role_id must be a stable ASCII slug using letters, digits, ., _, or -"
        )
    return EnterpriseRoleProfile(
        role_id=role_id,
        role_name=_text(
            payload["role_name"], path=f"{path}.role_name", minimum=2, maximum=200
        ),
        dept=_text(payload["dept"], path=f"{path}.dept", minimum=2, maximum=200),
        job_description=_text(
            payload["job_description"],
            path=f"{path}.job_description",
            minimum=20,
            maximum=5_000,
        ),
        tasks=_text_list(payload["tasks"], path=f"{path}.tasks"),
        skills=_text_list(payload["skills"], path=f"{path}.skills"),
        target_level=_optional_text(
            payload.get("target_level"), path=f"{path}.target_level", maximum=100
        ),
        effective_date=_iso_date(
            payload.get("effective_date"), path=f"{path}.effective_date"
        ),
        source_ref=_optional_text(
            payload.get("source_ref"), path=f"{path}.source_ref", maximum=200
        ),
    )


def validate_onboarding_input(payload: Mapping[str, Any]) -> EnterpriseRoleBatch:
    """Validate schema strictly and apply bounded heuristic PII screening."""

    if not isinstance(payload, Mapping):
        raise OnboardingValidationError("onboarding input must be a JSON object")
    _strict_keys(
        payload,
        allowed=ROOT_FIELDS,
        required=ROOT_FIELDS,
        path="input",
    )
    if payload.get("schema") != INPUT_SCHEMA:
        raise OnboardingValidationError(f"input.schema must equal {INPUT_SCHEMA}")
    organization_namespace = _text(
        payload.get("organization_namespace"),
        path="input.organization_namespace",
        minimum=3,
        maximum=64,
    )
    if ROLE_ID_RE.fullmatch(organization_namespace) is None:
        raise OnboardingValidationError(
            "input.organization_namespace must be a stable ASCII slug"
        )
    template_only = payload.get("template_only")
    if not isinstance(template_only, bool):
        raise OnboardingValidationError("input.template_only must be a boolean")
    raw_roles = payload.get("roles")
    if not isinstance(raw_roles, list) or not 1 <= len(raw_roles) <= 1_000:
        raise OnboardingValidationError(
            "input.roles must be an array containing between 1 and 1000 roles"
        )
    roles: list[EnterpriseRoleProfile] = []
    seen_ids: set[str] = set()
    for index, raw_role in enumerate(raw_roles):
        if not isinstance(raw_role, Mapping):
            raise OnboardingValidationError(f"roles[{index}] must be an object")
        role = _validate_role_profile(raw_role, index=index)
        role_key = normalize_semantic_text(role.role_id)
        if role_key in seen_ids:
            raise OnboardingValidationError(
                f"input.roles contains duplicate role_id: {role.role_id}"
            )
        seen_ids.add(role_key)
        roles.append(role)
    return EnterpriseRoleBatch(
        organization_namespace=organization_namespace,
        template_only=template_only,
        roles=tuple(roles),
    )


def build_validation_report(batch: EnterpriseRoleBatch) -> dict[str, Any]:
    roles = batch.to_internal_roles()
    return {
        "schema": VALIDATION_SCHEMA,
        "ok": True,
        "generated_at": _now(),
        "source_schema": INPUT_SCHEMA,
        "organization_namespace": batch.organization_namespace,
        "template_only": batch.template_only,
        "production_eligible": not batch.template_only,
        "role_count": len(batch.roles),
        "role_gold_ids": [role.gold_id for role in roles],
        "candidate_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "neo4j_writes": False,
        "approval_claim": False,
        "pii_screening": _pii_screening(),
    }


def prepare_candidate_packet(
    batch: EnterpriseRoleBatch,
    db_path: str | Path,
    *,
    limit: int = 5,
    allow_template_smoke: bool = False,
) -> dict[str, Any]:
    """Run the existing read-only mapper after strict onboarding validation."""

    if batch.template_only and not allow_template_smoke:
        raise OnboardingValidationError(
            "template_only input is blocked from packet preparation; use "
            "--allow-template-smoke only for an explicit non-production smoke run"
        )
    source_db_sha256, source_db_byte_count = _sha256_file(db_path)
    mapping_packet = _decorate_mapping_packet(
        map_internal_job_roles(
            batch.to_internal_roles(), db_path, limit=limit
        ),
        template_only=batch.template_only,
        source_db_sha256=source_db_sha256,
    )
    mapping_packet_sha256 = _canonical_json_sha256(mapping_packet)
    mapping_fingerprint_sha256 = _mapping_fingerprint(mapping_packet)
    packet = {
        "schema": PACKET_SCHEMA,
        "generated_at": _now(),
        "source_schema": INPUT_SCHEMA,
        "organization_namespace": batch.organization_namespace,
        "template_only": batch.template_only,
        "production_eligible": not batch.template_only,
        "candidate_only": True,
        "human_review_required": True,
        "status_update_allowed": False,
        "db_writes": False,
        "neo4j_writes": False,
        "approval_claim": False,
        "role_count": len(batch.roles),
        "mapping_limit": int(limit),
        "source_db_sha256": source_db_sha256,
        "source_db_byte_count": source_db_byte_count,
        "mapping_packet_sha256": mapping_packet_sha256,
        "mapping_fingerprint_sha256": mapping_fingerprint_sha256,
        "builder_artifact": None,
        "role_profiles": [role.to_dict() for role in batch.roles],
        "mapping_packet": mapping_packet,
        "pii_screening": _pii_screening(),
    }
    check = check_candidate_packet(packet, db_path=db_path)
    if check["ok"] is not True:
        raise OnboardingValidationError(
            "prepared candidate packet failed its own safety check: "
            + "; ".join(check["errors"])
        )
    return packet


def _walk_safety_fields(value: Any, *, path: str, errors: list[str]) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            nested_path = f"{path}.{key}"
            if key in {"approval_claim", "human_approval_claim"} and nested is not False:
                errors.append(f"{nested_path} must be false")
            if key in {"db_writes", "neo4j_writes", "status_update_allowed"} and nested is True:
                errors.append(f"{nested_path} must not be true")
            if key in {"status", "review_status"} and nested in FORBIDDEN_STATUS_VALUES:
                errors.append(f"{nested_path} contains a prohibited approval status")
            # Hex digests can coincidentally contain a 13-digit run that looks
            # like a Korean resident identifier.  They are system bindings,
            # not source free text, so keep them out of heuristic DLP matching.
            if isinstance(nested, str) and key.endswith("sha256"):
                continue
            _walk_safety_fields(nested, path=nested_path, errors=errors)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _walk_safety_fields(nested, path=f"{path}[{index}]", errors=errors)
    elif isinstance(value, str):
        for label, pattern in FORBIDDEN_VALUE_PATTERNS:
            if pattern.search(value):
                errors.append(f"{path} contains prohibited PII pattern: {label}")


def check_candidate_packet(
    payload: Any,
    *,
    db_path: str | Path,
    source_packet_file_sha256: str | None = None,
) -> dict[str, Any]:
    """Audit a prepared packet without treating an audit pass as approval."""

    errors: list[str] = []
    actual_db_sha256: str | None = None
    actual_db_byte_count: int | None = None
    recomputed_fingerprint: str | None = None
    if not isinstance(payload, Mapping):
        errors.append("packet must be a JSON object")
    else:
        expected_keys = {
            "schema",
            "generated_at",
            "source_schema",
            "organization_namespace",
            "template_only",
            "production_eligible",
            "candidate_only",
            "human_review_required",
            "status_update_allowed",
            "db_writes",
            "neo4j_writes",
            "approval_claim",
            "role_count",
            "mapping_limit",
            "source_db_sha256",
            "source_db_byte_count",
            "mapping_packet_sha256",
            "mapping_fingerprint_sha256",
            "builder_artifact",
            "role_profiles",
            "mapping_packet",
            "pii_screening",
        }
        unknown = sorted(set(payload) - expected_keys)
        missing = sorted(expected_keys - set(payload))
        if unknown:
            errors.append("packet contains unknown fields: " + ", ".join(unknown))
        if missing:
            errors.append("packet is missing fields: " + ", ".join(missing))
        if payload.get("schema") != PACKET_SCHEMA:
            errors.append(f"packet.schema must equal {PACKET_SCHEMA}")
        if payload.get("source_schema") != INPUT_SCHEMA:
            errors.append(f"packet.source_schema must equal {INPUT_SCHEMA}")
        for key, expected in (
            ("candidate_only", True),
            ("human_review_required", True),
            ("status_update_allowed", False),
            ("db_writes", False),
            ("neo4j_writes", False),
            ("approval_claim", False),
        ):
            if payload.get(key) is not expected:
                errors.append(f"packet.{key} must be {str(expected).lower()}")
        template_only = payload.get("template_only")
        if not isinstance(template_only, bool):
            errors.append("packet.template_only must be a boolean")
        elif payload.get("production_eligible") is not (not template_only):
            errors.append(
                "packet.production_eligible must equal not packet.template_only"
            )
        mapping_limit = payload.get("mapping_limit")
        if (
            isinstance(mapping_limit, bool)
            or not isinstance(mapping_limit, int)
            or not 1 <= mapping_limit <= 25
        ):
            errors.append("packet.mapping_limit must be an integer between 1 and 25")
        screening = payload.get("pii_screening")
        if not isinstance(screening, Mapping):
            errors.append("packet.pii_screening must be an object")
        else:
            if screening.get("method") != PII_SCREENING_METHOD:
                errors.append("packet.pii_screening.method is invalid")
            if screening.get("result") != "no_known_pattern_detected":
                errors.append("packet.pii_screening.result is invalid")
            if screening.get("residual_risk") != PII_RESIDUAL_RISK:
                errors.append(
                    "packet.pii_screening must preserve the heuristic DLP residual-risk notice"
                )

        profiles = payload.get("role_profiles")
        mapping_packet = payload.get("mapping_packet")
        if not isinstance(profiles, list):
            errors.append("packet.role_profiles must be an array")
            profiles = []
        try:
            source_batch = validate_onboarding_input(
                {
                    "schema": payload.get("source_schema"),
                    "organization_namespace": payload.get("organization_namespace"),
                    "template_only": payload.get("template_only"),
                    "roles": profiles,
                }
            )
        except (ContractValidationError, OnboardingValidationError, TypeError) as exc:
            errors.append(f"packet.role_profiles failed validation: {exc}")
            source_batch = None
        if payload.get("role_count") != len(profiles):
            errors.append("packet.role_count does not match role_profiles")
        builder_artifact = payload.get("builder_artifact")
        if builder_artifact is not None:
            if not isinstance(builder_artifact, Mapping):
                errors.append("packet.builder_artifact must be null or an object")
            else:
                for key, expected in (
                    ("schema", INTERNAL_ROLE_MAPPING_PACKET_SCHEMA),
                    ("candidate_only", True),
                    ("human_review_required", True),
                    ("status_update_allowed", False),
                    ("db_writes", False),
                    ("neo4j_writes", False),
                    ("approval_claim", False),
                    ("template_only", template_only),
                    ("production_eligible", payload.get("production_eligible")),
                ):
                    if builder_artifact.get(key) != expected:
                        errors.append(
                            f"packet.builder_artifact.{key} does not match "
                            "the candidate-only wrapper contract"
                        )
                if builder_artifact.get("role_count") != len(profiles):
                    errors.append(
                        "packet.builder_artifact.role_count does not match role_profiles"
                    )
                if builder_artifact.get("mapping_packet_sha256") != payload.get(
                    "mapping_packet_sha256"
                ):
                    errors.append(
                        "packet.builder_artifact.mapping_packet_sha256 does not "
                        "match wrapper"
                    )

        try:
            actual_db_sha256, actual_db_byte_count = _sha256_file(db_path)
        except (FileNotFoundError, OSError) as exc:
            errors.append(f"source DB hash failed: {exc}")
        if actual_db_sha256 is not None:
            if payload.get("source_db_sha256") != actual_db_sha256:
                errors.append("packet.source_db_sha256 does not match the checked DB")
            if payload.get("source_db_byte_count") != actual_db_byte_count:
                errors.append("packet.source_db_byte_count does not match the checked DB")

        if not isinstance(mapping_packet, Mapping):
            errors.append("packet.mapping_packet must be an object")
        else:
            if mapping_packet.get("schema") != INTERNAL_ROLE_MAPPING_PACKET_SCHEMA:
                errors.append(
                    "packet.mapping_packet.schema is not the existing mapper contract"
                )
            if mapping_packet.get("ncs_scope") != "all_classifications":
                errors.append("packet.mapping_packet must use all NCS classifications")
            if mapping_packet.get("major_code_filter") is not None:
                errors.append("packet.mapping_packet must not apply a major-code filter")
            for key, expected in (
                ("candidate_only", True),
                ("human_review_required", True),
                ("status_update_allowed", False),
                ("db_writes", False),
                ("neo4j_writes", False),
                ("approval_claim", False),
            ):
                if mapping_packet.get(key) is not expected:
                    errors.append(
                        "packet.mapping_packet."
                        f"{key} must be {str(expected).lower()}"
                    )
            if mapping_packet.get("template_only") is not template_only:
                errors.append("packet.mapping_packet.template_only must match wrapper")
            if mapping_packet.get("production_eligible") is not payload.get(
                "production_eligible"
            ):
                errors.append(
                    "packet.mapping_packet.production_eligible must match wrapper"
                )
            if mapping_packet.get("source_db_sha256") != actual_db_sha256:
                errors.append(
                    "packet.mapping_packet.source_db_sha256 does not match the checked DB"
                )
            actual_mapping_sha256 = _canonical_json_sha256(mapping_packet)
            actual_mapping_fingerprint = _mapping_fingerprint(mapping_packet)
            if payload.get("mapping_packet_sha256") != actual_mapping_sha256:
                errors.append("packet.mapping_packet_sha256 does not match mapping_packet")
            if payload.get("mapping_fingerprint_sha256") != actual_mapping_fingerprint:
                errors.append(
                    "packet.mapping_fingerprint_sha256 does not match mapping_packet"
                )
            role_results = mapping_packet.get("role_results")
            if not isinstance(role_results, list):
                errors.append("packet.mapping_packet.role_results must be an array")
                role_results = []
            if len(role_results) != len(profiles):
                errors.append("mapping role count does not match role_profiles")
            for index, result in enumerate(role_results):
                if not isinstance(result, Mapping):
                    errors.append(f"mapping role_results[{index}] must be an object")
                    continue
                status = result.get("status")
                if status not in ALIGNMENT_STATUSES:
                    errors.append(f"mapping role_results[{index}].status is not candidate-only")
                role = result.get("role")
                expected_gold_id = None
                if source_batch is not None and index < len(source_batch.roles):
                    expected_gold_id = deterministic_gold_id(
                        source_batch.organization_namespace,
                        source_batch.roles[index].role_id,
                    )
                if not isinstance(role, Mapping):
                    errors.append(f"mapping role_results[{index}].role must be an object")
                elif source_batch is not None and index < len(source_batch.roles):
                    expected_role = source_batch.to_internal_roles()[index].to_public_dict()
                    if dict(role) != expected_role:
                        errors.append(
                            f"mapping role_results[{index}] role does not exactly match "
                            "the canonical source profile projection"
                        )
                candidates = result.get("alignment_candidates")
                if not isinstance(candidates, list):
                    errors.append(
                        f"mapping role_results[{index}].alignment_candidates must be an array"
                    )
                    continue
                previous_score = 2.0
                targets: set[str] = set()
                for candidate_index, candidate in enumerate(candidates):
                    candidate_path = (
                        f"mapping role_results[{index}]"
                        f".alignment_candidates[{candidate_index}]"
                    )
                    if not isinstance(candidate, Mapping):
                        errors.append(f"{candidate_path} must be an object")
                        continue
                    try:
                        validated = validate_alignment_candidate(candidate)
                    except (ContractValidationError, TypeError, ValueError) as exc:
                        errors.append(f"{candidate_path} failed validation: {exc}")
                        continue
                    if validated.status not in ALIGNMENT_STATUSES:
                        errors.append(f"{candidate_path}.status is not candidate-only")
                    if validated.ncs_target_type != "ncs_job":
                        errors.append(f"{candidate_path} must target ncs_job")
                    if re.fullmatch(r"\d{8}", validated.ncs_target_key) is None:
                        errors.append(f"{candidate_path} has an invalid NCS job code")
                    if expected_gold_id is not None and validated.role_gold_id != expected_gold_id:
                        errors.append(f"{candidate_path} role identity mismatch")
                    if validated.score > previous_score:
                        errors.append(f"{candidate_path} is not sorted by descending score")
                    previous_score = validated.score
                    if validated.ncs_target_key in targets:
                        errors.append(f"{candidate_path} duplicates an NCS target")
                    targets.add(validated.ncs_target_key)

            if (
                source_batch is not None
                and isinstance(mapping_limit, int)
                and not isinstance(mapping_limit, bool)
                and 1 <= mapping_limit <= 25
                and actual_db_sha256 is not None
            ):
                try:
                    recomputed = _decorate_mapping_packet(
                        map_internal_job_roles(
                            source_batch.to_internal_roles(),
                            db_path,
                            limit=mapping_limit,
                        ),
                        template_only=source_batch.template_only,
                        source_db_sha256=actual_db_sha256,
                    )
                    recomputed_fingerprint = _mapping_fingerprint(recomputed)
                    if _without_generated_at(mapping_packet) != _without_generated_at(
                        recomputed
                    ):
                        errors.append(
                            "packet.mapping_packet does not match deterministic "
                            "recomputation from role profiles and source DB"
                        )
                except (OSError, sqlite3.DatabaseError, TypeError, ValueError) as exc:
                    errors.append(f"mapping packet recomputation failed: {exc}")
                    recomputed_fingerprint = None

    _walk_safety_fields(payload, path="packet", errors=errors)
    return {
        "schema": PACKET_CHECK_SCHEMA,
        "generated_at": _now(),
        "ok": not errors,
        "status": "pass" if not errors else "blocked",
        "candidate_only_verified": not errors,
        "error_count": len(errors),
        "errors": errors,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "neo4j_writes": False,
        "approval_claim": False,
        "human_review_required": True,
        "source_db_sha256": actual_db_sha256,
        "source_db_byte_count": actual_db_byte_count,
        "mapping_packet_sha256": (
            _canonical_json_sha256(payload.get("mapping_packet"))
            if isinstance(payload, Mapping)
            and isinstance(payload.get("mapping_packet"), Mapping)
            else None
        ),
        "mapping_fingerprint_sha256": (
            _mapping_fingerprint(payload.get("mapping_packet"))
            if isinstance(payload, Mapping)
            and isinstance(payload.get("mapping_packet"), Mapping)
            else None
        ),
        "recomputed_mapping_fingerprint_sha256": recomputed_fingerprint,
        "source_packet_file_sha256": source_packet_file_sha256,
        "pii_screening": _pii_screening(
            "no_known_pattern_detected"
            if not any("PII pattern" in error for error in errors)
            else "pattern_detected"
        ),
    }


def _load_json_object(path: Path) -> Mapping[str, Any]:
    source = path.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"input file does not exist: {source}")
    payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, Mapping):
        raise OnboardingValidationError("JSON root must be an object")
    return payload


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate role input only")
    validate.add_argument("--input", type=Path, default=DEFAULT_TEMPLATE)
    validate.add_argument("--out", type=Path)

    prepare = subparsers.add_parser(
        "prepare", help="prepare a read-only candidate mapping packet"
    )
    prepare.add_argument("--input", type=Path, required=True)
    prepare.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument(
        "--builder-out",
        type=Path,
        help=(
            "optional raw ncs_internal_role_mapping_packet_v1 output accepted "
            "by the Data Builder"
        ),
    )
    prepare.add_argument("--limit", type=int, default=5)
    prepare.add_argument(
        "--allow-template-smoke",
        action="store_true",
        help="allow template_only input for a clearly non-production smoke packet",
    )

    check = subparsers.add_parser("check", help="audit a prepared packet")
    check.add_argument("--packet", type=Path, required=True)
    check.add_argument("--db", type=Path, default=DEFAULT_DATABASE)
    check.add_argument("--out", type=Path)
    return parser


def _emit(payload: Mapping[str, Any], path: Path | None) -> None:
    if path is not None:
        _atomic_write(path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            batch = validate_onboarding_input(_load_json_object(args.input))
            _emit(build_validation_report(batch), args.out)
            return 0
        if args.command == "prepare":
            batch = validate_onboarding_input(_load_json_object(args.input))
            packet = prepare_candidate_packet(
                batch,
                args.db,
                limit=args.limit,
                allow_template_smoke=args.allow_template_smoke,
            )
            if args.builder_out is not None:
                _atomic_write(args.builder_out, packet["mapping_packet"])
                roles, candidates = load_internal_role_mapping_packet(
                    args.builder_out
                )
                builder_file_sha256, builder_file_byte_count = _sha256_file(
                    args.builder_out
                )
                packet["builder_artifact"] = {
                    "path": str(args.builder_out),
                    "schema": INTERNAL_ROLE_MAPPING_PACKET_SCHEMA,
                    "file_sha256": builder_file_sha256,
                    "file_byte_count": builder_file_byte_count,
                    "mapping_packet_sha256": packet["mapping_packet_sha256"],
                    "role_count": len(roles),
                    "alignment_candidate_count": len(candidates),
                    "candidate_only": True,
                    "human_review_required": True,
                    "status_update_allowed": False,
                    "db_writes": False,
                    "neo4j_writes": False,
                    "approval_claim": False,
                    "template_only": batch.template_only,
                    "production_eligible": not batch.template_only,
                }
                final_check = check_candidate_packet(packet, db_path=args.db)
                if final_check["ok"] is not True:
                    raise OnboardingValidationError(
                        "Builder artifact failed final safety check: "
                        + "; ".join(final_check["errors"])
                    )
            _emit(packet, args.out)
            return 0
        source_packet_file_sha256, _source_packet_size = _sha256_file(args.packet)
        report = check_candidate_packet(
            _load_json_object(args.packet),
            db_path=args.db,
            source_packet_file_sha256=source_packet_file_sha256,
        )
        _emit(report, args.out)
        return 0 if report["ok"] is True else 1
    except (
        ContractValidationError,
        OnboardingValidationError,
        FileNotFoundError,
        json.JSONDecodeError,
        OSError,
        TypeError,
        ValueError,
    ) as exc:
        parser.exit(1, f"error: internal role onboarding failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
