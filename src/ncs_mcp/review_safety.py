from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable


REVIEW_PACKET_EXTENSIONS = (".json", ".jsonl", ".csv", ".md")
AUTOMATED_REVIEWER_IDS = ("dashboard", "mcp", "automation", "automated_eval_gate", "system")
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


AUDIT_PROVENANCE_COLUMNS = (
    "id",
    "entity_type",
    "entity_id",
    "new_status",
    "reviewer_id",
    "source_decision_packet",
    "source_artifact_hash",
    "rationale",
    "evidence_refs_json",
)
# Job and tool ids (for example codex_report_review_20260619) are not people.
_AUTOMATED_REVIEWER_PREFIX = re.compile(r"(?:codex|claude|agent|automation|automated|system|mcp|dashboard)(?:$|[_\-:.])")
_PACKET_SHA256_CACHE: dict[tuple[str, int, int], str] = {}


def is_automated_reviewer(reviewer_id: object) -> bool:
    reviewer = str(reviewer_id or "").strip().lower()
    return not reviewer or reviewer in AUTOMATED_REVIEWER_IDS or bool(_AUTOMATED_REVIEWER_PREFIX.match(reviewer))


def _packet_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    if key not in _PACKET_SHA256_CACHE:
        _PACKET_SHA256_CACHE[key] = review_packet_sha256(path)
    return _PACKET_SHA256_CACHE[key]


def packet_backing_blockers(audit: Any, status: str) -> list[str]:
    """Why a review_audit_log row does not back `status` with a human decision packet.

    The packet must resolve to a reports artifact whose sha256 matches, with a
    human reviewer, rationale and evidence refs on the same row.
    """
    blockers = []
    if str(audit["new_status"] or "").strip() != status:
        blockers.append("new_status_matches_row")
    if is_automated_reviewer(audit["reviewer_id"]):
        blockers.append("human_reviewer_id")
    packet = str(audit["source_decision_packet"] or "").strip()
    packet_path = resolve_repo_reports_artifact(packet, extensions=REVIEW_PACKET_EXTENSIONS) if packet else None
    if packet_path is None:
        blockers.append("source_decision_packet_resolves_to_reports_artifact")
    digest = str(audit["source_artifact_hash"] or "").strip().lower()
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or (
        packet_path is not None and digest != "sha256:" + _packet_sha256(packet_path)
    ):
        blockers.append("source_artifact_hash_matches_reports_artifact")
    if not str(audit["rationale"] or "").strip():
        blockers.append("rationale")
    if not evidence_refs_json_is_nonempty_string_list(audit["evidence_refs_json"]):
        blockers.append("evidence_refs_json_valid")
    return blockers


def latest_audit_provenance(
    conn: Any,
    statuses: dict[str, str],
    entity_keys: dict[str, list[tuple[str, str]]],
) -> dict[str, tuple[list[str], Any]]:
    """Blockers of each row's most recent audit row, and that audit row.

    `statuses` maps a row key to its current review status; `entity_keys` maps
    the same key to the (entity_type, entity_id) pairs whose audit rows describe
    it. Only the latest of those audit rows counts: a later automated change
    must not ride on an earlier packet. Rows without an audit table, its
    provenance columns or any audit row get a blocker instead of an error.
    """
    if not statuses:
        return {}
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_audit_log'"
    ).fetchone() is not None
    columns = {row[1] for row in conn.execute("PRAGMA table_info(review_audit_log)")} if has_table else set()
    if not has_table or not set(AUDIT_PROVENANCE_COLUMNS) <= columns:
        blocker = "review_audit_log" if not has_table else "review_audit_log_columns"
        return {key: ([blocker], None) for key in statuses}
    owners: dict[tuple[str, str], list[str]] = {}
    for key, pairs in entity_keys.items():
        for entity_type, entity_id in pairs:
            owners.setdefault((entity_type, str(entity_id)), []).append(key)
    latest: dict[str, Any] = {}
    for entity_type in sorted({entity_type for entity_type, _ in owners}):
        ids = sorted(entity_id for owner_type, entity_id in owners if owner_type == entity_type)
        for audit in conn.execute(
            f"""
            SELECT {", ".join(AUDIT_PROVENANCE_COLUMNS)}
            FROM review_audit_log
            WHERE entity_type = ? AND entity_id IN (SELECT value FROM json_each(?))
            ORDER BY id
            """,
            (entity_type, json.dumps(ids)),
        ):
            for key in owners[(entity_type, str(audit["entity_id"]))]:
                if key not in latest or int(audit["id"]) > int(latest[key]["id"]):
                    latest[key] = audit
    return {
        key: (packet_backing_blockers(latest[key], status), latest[key]) if key in latest else (["review_audit_log"], None)
        for key, status in statuses.items()
    }


def packet_backed_entity_ids(conn: Any, entity_type: str, statuses: dict[str, str]) -> set[str]:
    """Entity ids whose latest review_audit_log row backs the current status with a packet.

    `statuses` maps entity_id to the entity's current review status. Without an
    audit table (compact serving snapshots) nothing is packet-backed.
    """
    provenance = latest_audit_provenance(
        conn,
        statuses,
        {entity_id: [(entity_type, entity_id)] for entity_id in statuses},
    )
    return {entity_id for entity_id, (blockers, _) in provenance.items() if not blockers}


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
