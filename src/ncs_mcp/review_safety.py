from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Iterable


REVIEW_PACKET_EXTENSIONS = (".json", ".jsonl", ".csv", ".md")
FORBIDDEN_AUTOMATIC_STATUS_TERMS = (
    "human_reviewed",
    "accepted",
    "reviewed",
    "rejected",
)


def neutralize_suggested_action(
    suggested_action: object,
    *,
    issue_type: object = None,
    target_type: object = None,
) -> str:
    raw_action = str(suggested_action or "").strip()
    normalized = raw_action.lower()
    status_write_markers = (
        "human_reviewed",
        "accepted",
        "reviewed",
        "rejected",
        "accept ",
        "accept this",
        "mark ",
        "review_status",
        "definition_status",
    )
    human_review_issue = "human_review_required" in str(issue_type or "")
    needs_neutral_action = not raw_action or any(
        marker in normalized for marker in status_write_markers
    ) or human_review_issue
    if not needs_neutral_action:
        return raw_action

    issue = str(issue_type or "").strip()
    target = str(target_type or "").strip()
    subject = "the evidence item"
    evidence_focus = "whether the proposed relationship is supported by evidence"
    if "training_goal" in issue or target == "training_goal_concept_link":
        subject = "the training goal link"
        evidence_focus = "whether the training goal directly supports the KSA link"
    elif "task_ksa" in issue or target == "task_ksa_concept_relation":
        subject = "the task-KSA relation"
        evidence_focus = "whether the task-KSA relation is supported by evidence"
    elif "concept" in issue or target == "ontology_concept":
        subject = "the ontology concept"
        evidence_focus = "whether the concept label, definition, and source evidence are valid"
    elif target:
        subject = target.replace("_", " ")

    neutral = (
        f"Human reviewer should inspect {evidence_focus} for {subject}; "
        "record any status change only through the controlled review workflow "
        "after a separate explicit human decision."
    )
    for term in FORBIDDEN_AUTOMATIC_STATUS_TERMS:
        neutral = neutral.replace(term, "trusted status")
    return neutral


def review_packet_artifact_ref(source_decision_packet: str | None) -> str:
    return (source_decision_packet or "").strip().partition("#")[0].strip()


def _normalized_extensions(extensions: Iterable[str]) -> tuple[str, ...]:
    return tuple(str(ext).lower() for ext in extensions)


def source_ref_has_supported_extension(
    source_decision_packet: str | None,
    *,
    extensions: Iterable[str] = REVIEW_PACKET_EXTENSIONS,
) -> bool:
    artifact_ref = review_packet_artifact_ref(source_decision_packet)
    if not artifact_ref:
        return False
    normalized = artifact_ref.replace("\\", "/").lower()
    return normalized.endswith(_normalized_extensions(extensions))


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_repo_reports_artifact(
    source_decision_packet: str | None,
    *,
    root: Path | None = None,
    extensions: Iterable[str] = REVIEW_PACKET_EXTENSIONS,
    allow_absolute: bool = True,
) -> Path | None:
    artifact_ref = review_packet_artifact_ref(source_decision_packet)
    if not artifact_ref:
        return None
    if not source_ref_has_supported_extension(artifact_ref, extensions=extensions):
        return None
    project_root = (root or repo_root()).resolve(strict=False)
    reports_root = (project_root / "reports").resolve(strict=False)
    raw_path = Path(artifact_ref)
    if raw_path.is_absolute():
        candidates = [raw_path] if allow_absolute else []
    else:
        candidates = [project_root / raw_path]
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if not resolved.is_file():
            continue
        try:
            resolved.relative_to(reports_root)
        except ValueError:
            continue
        return resolved
    return None


def normalize_source_decision_packet_ref(
    source_decision_packet: str | None,
    *,
    root: Path | None = None,
    extensions: Iterable[str] = REVIEW_PACKET_EXTENSIONS,
    allow_absolute: bool = True,
) -> str | None:
    value = (source_decision_packet or "").strip()
    if not value:
        return None
    artifact_ref, separator, fragment = value.partition("#")
    artifact_ref = artifact_ref.strip()
    fragment = fragment.strip()
    normalized_fragment = f"#{fragment}" if separator and fragment else ""
    normalized_artifact_ref = artifact_ref.replace("\\", "/")
    portable_candidate = f"{normalized_artifact_ref}{normalized_fragment}"
    if is_portable_reports_packet_ref(
        portable_candidate,
        extensions=extensions,
        allow_blank=False,
    ):
        return portable_candidate
    resolved = resolve_repo_reports_artifact(
        value,
        root=root,
        extensions=extensions,
        allow_absolute=allow_absolute,
    )
    if resolved is None:
        return value
    project_root = (root or repo_root()).resolve(strict=False)
    try:
        portable_artifact = resolved.relative_to(project_root).as_posix()
    except ValueError:
        return value
    return f"{portable_artifact}{normalized_fragment}"


def is_portable_reports_packet_ref(
    source_decision_packet: str | None,
    *,
    extensions: Iterable[str] = REVIEW_PACKET_EXTENSIONS,
    allow_blank: bool = True,
) -> bool:
    artifact_ref = review_packet_artifact_ref(source_decision_packet)
    if not artifact_ref:
        return allow_blank
    normalized = artifact_ref.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        return False
    parts = [part for part in normalized.split("/") if part and part != "."]
    if not parts or any(part == ".." for part in parts):
        return False
    if parts[0].lower() != "reports":
        return False
    return normalized.lower().endswith(_normalized_extensions(extensions))


def review_packet_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def evidence_refs_json_is_nonempty_string_list(value: str | None) -> bool:
    evidence_refs = str(value or "").strip()
    if not evidence_refs:
        return False
    try:
        parsed = json.loads(evidence_refs)
    except json.JSONDecodeError:
        return False
    return (
        isinstance(parsed, list)
        and bool(parsed)
        and all(isinstance(item, str) and item.strip() for item in parsed)
    )
