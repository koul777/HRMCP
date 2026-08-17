from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ncs_mcp.agent_queue import _canonical_json_sha256, build_agent_queue_status_from_file


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
SCHEMA = "aihr_release_operator_refresh_dag_v1"
AUDIT_SCHEMA = "aihr_release_operator_refresh_dag_audit_v1"
FORBIDDEN_AUTOMATIC_STATUSES = ["human_reviewed", "accepted", "reviewed"]
QUEUE_STATUS_DELIVERY_FIELDS = (
    "out_path",
    "markdown_path",
    "csv_path",
    "html_path",
    "audit_path",
    "audit_markdown_path",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_fragment(value: str | Path | None) -> str:
    return str(value or "").partition("#")[0].strip()


def sha256_file(path: Path | None) -> str | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def portable_path(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> str | None:
    text = strip_fragment(path)
    if not text:
        return None
    resolved = Path(text).resolve(strict=False)
    try:
        return resolved.relative_to(root.resolve(strict=False)).as_posix()
    except ValueError:
        return str(path).replace("\\", "/")


def resolve_artifact(value: str | Path | None, *, root: Path = PROJECT_ROOT) -> Path | None:
    text = strip_fragment(value)
    if not text:
        return None
    path = Path(text)
    if path.is_absolute():
        return path
    rooted = root / path
    if rooted.exists():
        return rooted
    if path.exists():
        return path
    return rooted


def read_json(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def artifact_status(path: str | Path | None, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    resolved = resolve_artifact(path, root=root)
    return {
        "path": portable_path(path, root=root),
        "exists_nonempty": bool(
            resolved and resolved.exists() and resolved.is_file() and resolved.stat().st_size > 0
        ),
        "sha256": sha256_file(resolved),
    }


def dated_artifact_sort_key(path: Path) -> tuple[int, str, float]:
    match = re.search(r"(20\d{6}(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?)", path.stem)
    stamp = match.group(1) if match else ""
    date = int(stamp[:8]) if stamp[:8].isdigit() else 0
    return date, stamp, path.stat().st_mtime


def latest_report_path(
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
    stamp: str | None = None,
) -> Path:
    candidates: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for path in reports_dir.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            if any(token in path.name for token in exclude_substrings):
                continue
            if stamp and stamp not in path.stem:
                continue
            candidates.append(path)
            seen.add(path)
    if not candidates:
        raise FileNotFoundError(f"no report artifact matched: {patterns}")
    return max(candidates, key=dated_artifact_sort_key)


def input_or_latest(
    value: Path | None,
    *patterns: str,
    reports_dir: Path = REPORTS,
    exclude_substrings: tuple[str, ...] = ("_probe",),
    root: Path = PROJECT_ROOT,
    stamp: str | None = None,
) -> Path:
    if value is not None:
        return resolve_artifact(value, root=root) or value
    return latest_report_path(
        *patterns,
        reports_dir=reports_dir,
        exclude_substrings=exclude_substrings,
        stamp=stamp,
    )


def stamp_from_path(path: Path, *, fallback: str = "20260712_10h") -> str:
    match = re.search(r"(20\d{6}(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?)", path.stem)
    return match.group(1) if match else fallback


def artifact_stamp(path_text: str | Path | None) -> str | None:
    text = strip_fragment(path_text)
    match = re.search(r"(20\d{6}(?:_[A-Za-z0-9][A-Za-z0-9_-]*)?)", Path(text).stem)
    return match.group(1) if match else None


def stamp_family(value: str | None, *, target_stamp: str) -> str | None:
    if not value:
        return None
    return target_stamp if value == target_stamp or value.startswith(f"{target_stamp}_") else value


def powershell_quote(path: str | Path | None) -> str:
    text = str(path or "").replace("/", "\\")
    return "'" + text.replace("'", "''") + "'"


def node(
    *,
    order: int,
    node_id: str,
    title: str,
    command: str,
    depends_on: list[str],
    expected_outputs: list[str | Path | None],
    mutation_policy: str = "regenerate_reports_only",
    acceptance_checks: list[str] | None = None,
    hazards: list[str] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    return {
        "order": order,
        "id": node_id,
        "title": title,
        "depends_on": depends_on,
        "mutation_policy": mutation_policy,
        "command": command,
        "expected_outputs": [
            artifact_status(path, root=root) for path in expected_outputs if path
        ],
        "acceptance_checks": acceptance_checks or [],
        "hazards": hazards or [],
    }


def source_hash_checks_from_payload(
    payload: dict[str, Any],
    *,
    root: Path,
) -> dict[str, dict[str, Any]]:
    source_paths = payload.get("source_paths") if isinstance(payload.get("source_paths"), dict) else {}
    source_hashes = payload.get("source_hashes") if isinstance(payload.get("source_hashes"), dict) else {}
    checks: dict[str, dict[str, Any]] = {}
    for key, value in source_paths.items():
        if not value:
            continue
        resolved = resolve_artifact(value, root=root)
        actual = sha256_file(resolved)
        expected = source_hashes.get(key)
        checks[key] = {
            "path": portable_path(value, root=root),
            "expected_sha256": expected,
            "actual_sha256": actual,
            "hash_matches": bool(expected and actual and expected == actual),
        }
    return checks


def queue_source_check(queue_json: Path, payload: dict[str, Any], *, root: Path) -> dict[str, Any]:
    queue_sha = sha256_file(queue_json)
    expected = payload.get("source_queue_sha256")
    source_path = portable_path(payload.get("source_queue_path"), root=root)
    expected_path = portable_path(queue_json, root=root)
    return {
        "source_queue_path": source_path,
        "expected_source_queue_path": expected_path,
        "source_queue_path_matches_expected": source_path == expected_path,
        "expected_sha256": expected,
        "actual_sha256": queue_sha,
        "hash_matches": bool(expected and queue_sha and expected == queue_sha),
    }


def queue_status_snapshot_check(
    *,
    queue_json: Path,
    queue_status_json: Path,
    queue_run_payload: dict[str, Any],
    root: Path,
) -> dict[str, Any]:
    declared = str(queue_run_payload.get("queue_status_snapshot_sha256") or "").strip() or None
    status_payload = read_json(queue_status_json)
    current_status_payload = build_agent_queue_status_from_file(queue_json, workspace=root)
    artifact_projection = queue_status_snapshot_projection(status_payload)
    current_projection = queue_status_snapshot_projection(current_status_payload)
    artifact_hash = _canonical_json_sha256(artifact_projection) if artifact_projection else None
    current_hash = _canonical_json_sha256(current_projection)
    return {
        "queue_status_path": portable_path(queue_status_json, root=root),
        "declared_queue_status_snapshot_sha256": declared,
        "artifact_queue_status_snapshot_sha256": artifact_hash,
        "current_queue_status_snapshot_sha256": current_hash,
        "artifact_matches_declared": bool(declared and artifact_hash == declared),
        "current_matches_declared": bool(declared and current_hash == declared),
        "artifact_matches_current": bool(artifact_hash and artifact_hash == current_hash),
    }


def queue_status_snapshot_projection(payload: dict[str, Any]) -> dict[str, Any]:
    projected = dict(payload)
    for field in QUEUE_STATUS_DELIVERY_FIELDS:
        projected.pop(field, None)
    return projected


def build_refresh_dag(
    *,
    quality_report: Path,
    contract: Path,
    demo_json: Path,
    demo_html: Path,
    dashboard_verification: Path,
    release_readiness: Path,
    agent_queue: Path,
    queue_status: Path,
    queue_run_dryrun: Path,
    queue_run: Path,
    qualification_coverage_plan: Path,
    qualification_retry_hygiene: Path,
    qualification_decision: Path,
    qualification_decision_audit: Path,
    provenance_proofset_log: Path,
    transition_gap_json: Path,
    transition_gap_csv: Path,
    provenance_decision_sheet_json: Path,
    provenance_decision_sheet_csv: Path,
    provenance_decision_audit: Path,
    transition_crosswalk_json: Path,
    transition_crosswalk_csv: Path,
    transition_crosswalk_audit: Path,
    sprint_queue: Path,
    sprint_queue_audit: Path,
    next_actions: Path,
    operator_packet_integrity_audit: Path,
    handoff: Path,
    lineage_audit: Path,
    generated_at: str | None = None,
    root: Path = PROJECT_ROOT,
    stamp: str | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or now_iso()
    stamp = stamp or stamp_from_path(release_readiness)
    release = read_json(release_readiness)
    queue_status_payload = read_json(queue_status)
    queue_dryrun_payload = read_json(queue_run_dryrun)
    queue_run_payload = read_json(queue_run)
    qualification = read_json(qualification_decision)
    transition_crosswalk = read_json(transition_crosswalk_json)
    sprint = read_json(sprint_queue)
    next_actions_payload = read_json(next_actions)
    operator_audit = read_json(operator_packet_integrity_audit)
    lineage = read_json(lineage_audit)

    source_artifacts = {
        "quality_report": quality_report,
        "contract": contract,
        "demo_json": demo_json,
        "demo_html": demo_html,
        "dashboard_verification": dashboard_verification,
        "release_readiness": release_readiness,
        "agent_queue": agent_queue,
        "queue_status": queue_status,
        "queue_run_dryrun": queue_run_dryrun,
        "queue_run": queue_run,
        "qualification_coverage_plan": qualification_coverage_plan,
        "qualification_retry_hygiene": qualification_retry_hygiene,
        "qualification_decision": qualification_decision,
        "qualification_decision_audit": qualification_decision_audit,
        "provenance_proofset_log": provenance_proofset_log,
        "transition_gap_json": transition_gap_json,
        "transition_gap_csv": transition_gap_csv,
        "provenance_decision_sheet_json": provenance_decision_sheet_json,
        "provenance_decision_sheet_csv": provenance_decision_sheet_csv,
        "provenance_decision_audit": provenance_decision_audit,
        "transition_crosswalk_json": transition_crosswalk_json,
        "transition_crosswalk_csv": transition_crosswalk_csv,
        "transition_crosswalk_audit": transition_crosswalk_audit,
        "sprint_queue": sprint_queue,
        "sprint_queue_audit": sprint_queue_audit,
        "next_actions": next_actions,
        "operator_packet_integrity_audit": operator_packet_integrity_audit,
        "handoff": handoff,
        "lineage_audit": lineage_audit,
    }

    nodes = [
        node(
            order=1,
            node_id="base-proof",
            title="Regenerate base release proof artifacts",
            command=(
                "Run current proof commands for contract, quality gates, demo, guide/API "
                f"audits, qualification hygiene, and coverage plan for stamp {stamp}."
            ),
            depends_on=[],
            expected_outputs=[quality_report, contract, demo_json, demo_html],
            acceptance_checks=[
                "Every base proof artifact exists and shares the target stamp family.",
                "No DB write or human-review status promotion is claimed by report-only proofs.",
            ],
            root=root,
        ),
        node(
            order=2,
            node_id="release-seed",
            title="Regenerate release readiness and agent queue",
            command=(
                f"python scripts\\release_readiness_report.py --quality-report {powershell_quote(quality_report)} "
                f"--contract {powershell_quote(contract)} --demo-json {powershell_quote(demo_json)} "
                f"--demo-html {powershell_quote(demo_html)} --dashboard-verification "
                f"{powershell_quote(dashboard_verification)} --out {powershell_quote(release_readiness)} "
                f"--markdown-out {powershell_quote(release_readiness.with_suffix('.md'))} "
                f"--agent-queue-out {powershell_quote(agent_queue)} --agent-queue-markdown-out "
                f"{powershell_quote(agent_queue.with_suffix('.md'))}"
            ),
            depends_on=["base-proof"],
            expected_outputs=[release_readiness, release_readiness.with_suffix(".md"), agent_queue],
            acceptance_checks=[
                "release schema is aihr_release_readiness_v1.",
                "agent_work_queue_path matches the generated queue artifact.",
            ],
            hazards=["Regenerating release rewrites the queue and stales queue status/run hashes."],
            root=root,
        ),
        node(
            order=3,
            node_id="queue-preflight",
            title="Regenerate queue status preflight",
            command=(
                f"python scripts\\ncs_harness.py agent-queue-status --root {powershell_quote(root)} "
                f"--queue {powershell_quote(agent_queue)} "
                f"--out {powershell_quote(queue_status)} --markdown-out "
                f"{powershell_quote(queue_status.with_suffix('.md'))}"
            ),
            depends_on=["release-seed"],
            expected_outputs=[queue_status, queue_status.with_suffix(".md")],
            acceptance_checks=["queue status ok=true and blocked_count=0 for report-only execution."],
            root=root,
        ),
        node(
            order=4,
            node_id="queue-dryrun",
            title="Regenerate queue dry-run",
            command=(
                f"python scripts\\ncs_harness.py agent-queue-run-ready --root {powershell_quote(root)} "
                f"--queue {powershell_quote(agent_queue)} "
                f"--dry-run --out {powershell_quote(queue_run_dryrun)} --markdown-out "
                f"{powershell_quote(queue_run_dryrun.with_suffix('.md'))}"
            ),
            depends_on=["queue-preflight"],
            expected_outputs=[queue_run_dryrun, queue_run_dryrun.with_suffix(".md")],
            acceptance_checks=["dry_run=true and selected items are regenerate_reports_only."],
            root=root,
        ),
        node(
            order=5,
            node_id="queue-run",
            title="Regenerate report-only queue outputs",
            command=(
                f"python scripts\\ncs_harness.py agent-queue-run-ready --root {powershell_quote(root)} "
                f"--queue {powershell_quote(agent_queue)} "
                f"--out {powershell_quote(queue_run)} --markdown-out {powershell_quote(queue_run.with_suffix('.md'))}"
            ),
            depends_on=["queue-dryrun"],
            expected_outputs=[queue_run, queue_run.with_suffix(".md")],
            acceptance_checks=[
                "actual_run=true.",
                "failed_count=0, skipped_unsafe_count=0, acceptance_failed_count=0.",
            ],
            root=root,
        ),
        node(
            order=6,
            node_id="dashboard-verify",
            title="Verify dashboard against refreshed queue artifacts",
            command=(
                "python scripts\\ncs_harness.py verify-aihr-dashboard "
                f"--out {powershell_quote(dashboard_verification)} --markdown-out "
                f"{powershell_quote(dashboard_verification.with_suffix('.md'))}"
            ),
            depends_on=["queue-run"],
            expected_outputs=[dashboard_verification, dashboard_verification.with_suffix(".md")],
            acceptance_checks=["dashboard verification ok=true and static_artifacts are non-empty."],
            hazards=["Release and dashboard verification form a cycle; use cycle-safe hashes."],
            root=root,
        ),
        node(
            order=7,
            node_id="release-final",
            title="Regenerate final release with dashboard verification",
            command="Repeat release-seed, then queue-preflight/dryrun/run if agent queue hash changes.",
            depends_on=["dashboard-verify"],
            expected_outputs=[release_readiness, agent_queue, queue_status, queue_run],
            acceptance_checks=[
                "If queue sha changes, queue status/run are regenerated before downstream packets.",
                "Release remains blocked only by explicit human/API gates, not missing proof artifacts.",
            ],
            hazards=["This node is the main stale-hash boundary."],
            root=root,
        ),
        node(
            order=8,
            node_id="qualification-operator-decision",
            title="Regenerate qualification guarded-batch decision packet",
            command=(
                "python scripts\\ncs_harness.py build-qualification-guarded-batch-operator-decision "
                f"--root {powershell_quote(root)} --stamp {stamp} --coverage-plan "
                f"{powershell_quote(qualification_coverage_plan)} --retry-hygiene "
                f"{powershell_quote(qualification_retry_hygiene)} --release-readiness "
                f"{powershell_quote(release_readiness)} --queue-status "
                f"{powershell_quote(queue_status)} --queue-run {powershell_quote(queue_run)} "
                f"--out {powershell_quote(qualification_decision)} --markdown-out "
                f"{powershell_quote(qualification_decision.with_suffix('.md'))} --csv-out "
                f"{powershell_quote(qualification_decision.with_suffix('.csv'))} --audit-out "
                f"{powershell_quote(qualification_decision_audit)} --audit-markdown-out "
                f"{powershell_quote(qualification_decision_audit.with_suffix('.md'))} --strict"
            ),
            depends_on=["release-final"],
            expected_outputs=[
                qualification_decision,
                qualification_decision.with_suffix(".md"),
                qualification_decision.with_suffix(".csv"),
                qualification_decision_audit,
            ],
            acceptance_checks=["audit ok=true and execution_authorized=false."],
            root=root,
        ),
        node(
            order=9,
            node_id="provenance-proofset",
            title="Regenerate provenance reconfirmation proofset",
            command=(
                "python scripts\\ncs_harness.py export-human-review-provenance-reconfirmation-proofset "
                f"--decision-sheet-out {powershell_quote(provenance_decision_sheet_json)} "
                f"--decision-sheet-csv-out {powershell_quote(provenance_decision_sheet_csv)} "
                f"--decision-audit-out {powershell_quote(provenance_decision_audit)}"
            ),
            depends_on=["release-final"],
            expected_outputs=[
                provenance_decision_sheet_json,
                provenance_decision_sheet_csv,
                provenance_decision_audit,
                provenance_proofset_log,
            ],
            acceptance_checks=["decision fields stay blank until human review."],
            root=root,
        ),
        node(
            order=10,
            node_id="transition-crosswalk",
            title="Regenerate transition provenance crosswalk",
            command=(
                "python scripts\\ncs_harness.py build-transition-provenance-crosswalk "
                f"--transition-gap-csv {powershell_quote(transition_gap_csv)} "
                f"--provenance-decision-sheet-csv {powershell_quote(provenance_decision_sheet_csv)} "
                f"--provenance-decision-sheet-json {powershell_quote(provenance_decision_sheet_json)} "
                f"--out {powershell_quote(transition_crosswalk_json)} --markdown-out "
                f"{powershell_quote(transition_crosswalk_json.with_suffix('.md'))} --csv-out "
                f"{powershell_quote(transition_crosswalk_csv)} --audit-out "
                f"{powershell_quote(transition_crosswalk_audit)} --audit-markdown-out "
                f"{powershell_quote(transition_crosswalk_audit.with_suffix('.md'))} --strict"
            ),
            depends_on=["provenance-proofset"],
            expected_outputs=[transition_crosswalk_json, transition_crosswalk_csv, transition_crosswalk_audit],
            acceptance_checks=["crosswalk audit ok=true and source artifact suffix family matches."],
            root=root,
        ),
        node(
            order=11,
            node_id="blocker-sprint-queue",
            title="Regenerate blocker reduction sprint queue",
            command=(
                "python scripts\\ncs_harness.py build-aihr-blocker-reduction-sprint-queue "
                f"--root {powershell_quote(root)} --transition-crosswalk-csv {powershell_quote(transition_crosswalk_csv)} "
                f"--transition-crosswalk-audit-json {powershell_quote(transition_crosswalk_audit)} "
                f"--out {powershell_quote(sprint_queue)} --markdown-out "
                f"{powershell_quote(sprint_queue.with_suffix('.md'))} --csv-out "
                f"{powershell_quote(sprint_queue.with_suffix('.csv'))} --audit-out "
                f"{powershell_quote(sprint_queue_audit)} --audit-markdown-out "
                f"{powershell_quote(sprint_queue_audit.with_suffix('.md'))} --strict"
            ),
            depends_on=["qualification-operator-decision", "transition-crosswalk"],
            expected_outputs=[sprint_queue, sprint_queue.with_suffix(".csv"), sprint_queue_audit],
            acceptance_checks=[
                "sprint queue audit ok=true.",
                "transition scenarios have matching unique provenance decision rows.",
                "operator next-actions/lineage/integrity remain context-only, not source hashes.",
            ],
            root=root,
        ),
        node(
            order=12,
            node_id="operator-handoff-bundle",
            title="Regenerate operator next-actions, integrity, handoff, and lineage",
            command=(
                "python scripts\\ncs_harness.py build-aihr-operator-handoff-bundle "
                f"--root {powershell_quote(root)} --stamp {stamp} --release-readiness "
                f"{powershell_quote(release_readiness)} --queue-run {powershell_quote(queue_run)} "
                f"--qualification-decision-json {powershell_quote(qualification_decision)} "
                f"--qualification-decision-audit-json {powershell_quote(qualification_decision_audit)} "
                f"--provenance-proofset-log {powershell_quote(provenance_proofset_log)} "
                f"--blocker-sprint-queue-json {powershell_quote(sprint_queue)} "
                f"--blocker-sprint-queue-audit-json {powershell_quote(sprint_queue_audit)} "
                f"--transition-crosswalk-json {powershell_quote(transition_crosswalk_json)} "
                f"--transition-crosswalk-csv {powershell_quote(transition_crosswalk_csv)} "
                f"--transition-crosswalk-audit-json {powershell_quote(transition_crosswalk_audit)} "
                f"--next-actions-out {powershell_quote(next_actions)} "
                f"--next-actions-markdown-out {powershell_quote(next_actions.with_suffix('.md'))} "
                f"--operator-audit-out {powershell_quote(operator_packet_integrity_audit)} "
                f"--operator-audit-markdown-out {powershell_quote(operator_packet_integrity_audit.with_suffix('.md'))} "
                f"--handoff-out {powershell_quote(handoff)} --handoff-markdown-out "
                f"{powershell_quote(handoff.with_suffix('.md'))} --lineage-audit-out "
                f"{powershell_quote(lineage_audit)} --lineage-audit-markdown-out "
                f"{powershell_quote(lineage_audit.with_suffix('.md'))} --strict"
            ),
            depends_on=["blocker-sprint-queue"],
            expected_outputs=[next_actions, operator_packet_integrity_audit, handoff, lineage_audit],
            acceptance_checks=[
                "operator packet integrity audit ok=true.",
                "lineage audit ok=true.",
                "handoff embeds current next-actions and operator-audit hashes.",
            ],
            root=root,
        ),
    ]

    source_hash_artifacts = {
        "qualification_decision": qualification,
        "transition_crosswalk": transition_crosswalk,
        "sprint_queue": sprint,
        "next_actions": next_actions_payload,
    }
    current_artifacts = {
        key: artifact_status(path, root=root) for key, path in source_artifacts.items()
    }
    stamps = {
        key: artifact_stamp(status.get("path"))
        for key, status in current_artifacts.items()
        if status.get("path")
    }
    stamp_families = {
        key: stamp_family(value, target_stamp=stamp)
        for key, value in stamps.items()
        if value
    }
    nonempty_stamp_families = {value for value in stamp_families.values() if value}
    return {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "ok": True,
        "stamp": stamp,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "human_decision_required": True,
        "forbidden_automatic_statuses": FORBIDDEN_AUTOMATIC_STATUSES,
        "dag_contract": {
            "node_count": len(nodes),
            "nodes_are_ordered": True,
            "nodes_are_acyclic": True,
            "same_stamp_family_ok": nonempty_stamp_families <= {stamp},
            "stamps": stamps,
            "stamp_families": stamp_families,
            "cycle_safe_release_dashboard_boundary": True,
            "operator_downstream_artifacts_are_context_only_for_sprint_queue": True,
        },
        "current_artifacts": current_artifacts,
        "source_paths": {
            key: status.get("path") for key, status in current_artifacts.items()
        },
        "source_hashes": {
            key: status.get("sha256") for key, status in current_artifacts.items()
        },
        "embedded_source_hash_checks": {
            key: source_hash_checks_from_payload(payload, root=root)
            for key, payload in source_hash_artifacts.items()
        },
        "queue_hash_checks": {
            "queue_status": {
                "source_queue_path": portable_path(queue_status_payload.get("source_queue_path"), root=root),
                "expected_source_queue_path": portable_path(agent_queue, root=root),
                "source_queue_path_matches_expected": (
                    portable_path(queue_status_payload.get("source_queue_path"), root=root)
                    == portable_path(agent_queue, root=root)
                ),
            },
            "queue_run_dryrun": queue_source_check(agent_queue, queue_dryrun_payload, root=root),
            "queue_run": queue_source_check(agent_queue, queue_run_payload, root=root),
        },
        "queue_status_snapshot_checks": {
            "queue_run_dryrun": queue_status_snapshot_check(
                queue_json=agent_queue,
                queue_status_json=queue_status,
                queue_run_payload=queue_dryrun_payload,
                root=root,
            ),
            "queue_run": queue_status_snapshot_check(
                queue_json=agent_queue,
                queue_status_json=queue_status,
                queue_run_payload=queue_run_payload,
                root=root,
            ),
        },
        "release_queue_contract": {
            "release_agent_work_queue_path": portable_path(release.get("agent_work_queue_path"), root=root),
            "expected_agent_queue_path": portable_path(agent_queue, root=root),
            "matches": portable_path(release.get("agent_work_queue_path"), root=root)
            == portable_path(agent_queue, root=root),
        },
        "operator_audit_status": {
            "operator_packet_integrity_ok": operator_audit.get("ok"),
            "operator_packet_integrity_issue_count": operator_audit.get("issue_count"),
            "lineage_ok": lineage.get("ok"),
            "lineage_issue_count": lineage.get("issue_count"),
        },
        "nodes": nodes,
        "non_goals": [
            "This artifact does not run release, queue, dashboard, DB, or API commands.",
            "This artifact does not authorize qualification API collection.",
            "This artifact does not set human_reviewed, accepted, or reviewed.",
        ],
    }


def audit_refresh_dag(report: dict[str, Any], *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []

    def add_issue(code: str, **extra: Any) -> None:
        issue = {"code": code}
        issue.update(extra)
        issues.append(issue)

    for field in ("report_only", "human_decision_required"):
        if report.get(field) is not True:
            add_issue(f"{field}_not_true", value=report.get(field))
    for field in ("status_update_allowed", "db_writes", "api_calls", "approval_claim"):
        if report.get(field) is not False:
            add_issue(f"{field}_not_false", value=report.get(field))
    if report.get("forbidden_automatic_statuses") != FORBIDDEN_AUTOMATIC_STATUSES:
        add_issue("forbidden_automatic_statuses_mismatch")

    current_artifacts = (
        report.get("current_artifacts") if isinstance(report.get("current_artifacts"), dict) else {}
    )
    current_artifact_checks: dict[str, dict[str, Any]] = {}
    for key, status in current_artifacts.items():
        if not isinstance(status, dict):
            add_issue("current_artifact_status_not_object", artifact=key)
            continue
        path = resolve_artifact(status.get("path"), root=root)
        actual = sha256_file(path)
        exists_nonempty = bool(path and path.exists() and path.is_file() and path.stat().st_size > 0)
        current_artifact_checks[key] = {
            "path": status.get("path"),
            "expected_sha256": status.get("sha256"),
            "actual_sha256": actual,
            "exists_nonempty": exists_nonempty,
            "hash_matches": bool(status.get("sha256") and actual == status.get("sha256")),
        }
        if not exists_nonempty:
            add_issue("current_artifact_missing", artifact=key, path=status.get("path"))
        elif status.get("sha256") != actual:
            add_issue("current_artifact_hash_mismatch", artifact=key, path=status.get("path"))

    dag_contract = report.get("dag_contract") if isinstance(report.get("dag_contract"), dict) else {}
    for field in (
        "nodes_are_ordered",
        "nodes_are_acyclic",
        "same_stamp_family_ok",
        "cycle_safe_release_dashboard_boundary",
        "operator_downstream_artifacts_are_context_only_for_sprint_queue",
    ):
        if dag_contract.get(field) is not True:
            add_issue(f"dag_contract_{field}_not_true", value=dag_contract.get(field))

    nodes = report.get("nodes") if isinstance(report.get("nodes"), list) else []
    seen: set[str] = set()
    last_order = 0
    for node_payload in nodes:
        if not isinstance(node_payload, dict):
            add_issue("node_not_object")
            continue
        node_id = str(node_payload.get("id") or "")
        order = int(node_payload.get("order") or 0)
        if not node_id:
            add_issue("node_id_missing", order=order)
        if node_id in seen:
            add_issue("node_id_duplicate", node_id=node_id)
        for dependency in node_payload.get("depends_on") or []:
            if dependency not in seen:
                add_issue("dependency_not_previous_node", node_id=node_id, dependency=dependency)
        if order <= last_order:
            add_issue("node_order_not_increasing", node_id=node_id, order=order)
        seen.add(node_id)
        last_order = order
        for output in node_payload.get("expected_outputs") or []:
            if isinstance(output, dict) and output.get("exists_nonempty") is not True:
                add_issue(
                    "expected_output_missing",
                    node_id=node_id,
                    path=output.get("path"),
                )

    for artifact_name, checks in (report.get("embedded_source_hash_checks") or {}).items():
        if not isinstance(checks, dict):
            continue
        for source_key, check in checks.items():
            if not isinstance(check, dict):
                continue
            if check.get("hash_matches") is not True:
                add_issue(
                    "embedded_source_hash_stale",
                    artifact=artifact_name,
                    source_key=source_key,
                    path=check.get("path"),
                )

    queue_checks = report.get("queue_hash_checks") if isinstance(report.get("queue_hash_checks"), dict) else {}
    queue_status_check = (
        queue_checks.get("queue_status") if isinstance(queue_checks.get("queue_status"), dict) else {}
    )
    if queue_status_check.get("source_queue_path_matches_expected") is not True:
        add_issue(
            "queue_status_source_queue_path_mismatch",
            path=queue_status_check.get("source_queue_path"),
            expected=queue_status_check.get("expected_source_queue_path"),
        )
    for key in ("queue_run_dryrun", "queue_run"):
        check = queue_checks.get(key) if isinstance(queue_checks.get(key), dict) else {}
        if check.get("source_queue_path_matches_expected") is not True:
            add_issue(
                "queue_run_source_queue_path_mismatch",
                artifact=key,
                path=check.get("source_queue_path"),
                expected=check.get("expected_source_queue_path"),
            )
        if check.get("hash_matches") is not True:
            add_issue("queue_source_hash_stale", artifact=key, path=check.get("source_queue_path"))
    snapshot_checks = (
        report.get("queue_status_snapshot_checks")
        if isinstance(report.get("queue_status_snapshot_checks"), dict)
        else {}
    )
    for key, check in snapshot_checks.items():
        if not isinstance(check, dict):
            continue
        for field in (
            "artifact_matches_declared",
            "current_matches_declared",
            "artifact_matches_current",
        ):
            if check.get(field) is not True:
                add_issue(
                    f"queue_status_snapshot_{field}_not_true",
                    artifact=key,
                    value=check.get(field),
                )

    release_queue_contract = (
        report.get("release_queue_contract")
        if isinstance(report.get("release_queue_contract"), dict)
        else {}
    )
    if release_queue_contract.get("matches") is not True:
        add_issue("release_agent_work_queue_path_mismatch", value=release_queue_contract)

    operator_audit_status = (
        report.get("operator_audit_status")
        if isinstance(report.get("operator_audit_status"), dict)
        else {}
    )
    if operator_audit_status.get("operator_packet_integrity_ok") is not True:
        add_issue("operator_packet_integrity_not_ok", value=operator_audit_status)
    if operator_audit_status.get("lineage_ok") is not True:
        add_issue("operator_lineage_not_ok", value=operator_audit_status)

    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": now_iso(),
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "report_only": True,
        "status_update_allowed": False,
        "db_writes": False,
        "api_calls": False,
        "approval_claim": False,
        "current_artifact_checks": current_artifact_checks,
        "dag_contract": dag_contract,
    }


def write_markdown(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Release Operator Refresh DAG",
        "",
        f"- schema: `{report.get('schema')}`",
        f"- generated_at: `{report.get('generated_at')}`",
        f"- stamp: `{report.get('stamp')}`",
        f"- report_only: `{report.get('report_only')}`",
        f"- status_update_allowed: `{report.get('status_update_allowed')}`",
        f"- db_writes: `{report.get('db_writes')}`",
        f"- api_calls: `{report.get('api_calls')}`",
        f"- approval_claim: `{report.get('approval_claim')}`",
        "",
        "## DAG Contract",
    ]
    for key, value in (report.get("dag_contract") or {}).items():
        if key != "stamps":
            lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Current Artifacts"])
    for key, artifact in (report.get("current_artifacts") or {}).items():
        if not isinstance(artifact, dict):
            continue
        lines.append(
            f"- {key}: `{artifact.get('path')}` "
            f"exists_nonempty=`{artifact.get('exists_nonempty')}` "
            f"sha256=`{artifact.get('sha256')}`"
        )
    lines.extend(["", "## Source Hashes"])
    for key, value in (report.get("source_hashes") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Embedded Source Hash Checks"])
    for group, checks in (report.get("embedded_source_hash_checks") or {}).items():
        lines.append(f"### {group}")
        if isinstance(checks, dict):
            for key, check in checks.items():
                if not isinstance(check, dict):
                    continue
                lines.append(
                    f"- {key}: path=`{check.get('path')}` "
                    f"expected=`{check.get('expected_sha256')}` "
                    f"actual=`{check.get('actual_sha256')}` "
                    f"hash_matches=`{check.get('hash_matches')}`"
                )
    lines.extend(["", "## Queue Hash Checks"])
    for key, check in (report.get("queue_hash_checks") or {}).items():
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- {key}: source_queue_path=`{check.get('source_queue_path')}` "
            f"expected=`{check.get('expected_sha256')}` "
            f"actual=`{check.get('actual_sha256')}` "
            f"hash_matches=`{check.get('hash_matches')}` "
            f"path_matches=`{check.get('source_queue_path_matches_expected')}`"
        )
    lines.extend(["", "## Queue Status Snapshot Checks"])
    for key, check in (report.get("queue_status_snapshot_checks") or {}).items():
        if not isinstance(check, dict):
            continue
        lines.append(
            f"- {key}: declared=`{check.get('declared_queue_status_snapshot_sha256')}` "
            f"artifact=`{check.get('artifact_queue_status_snapshot_sha256')}` "
            f"current=`{check.get('current_queue_status_snapshot_sha256')}` "
            f"artifact_matches_declared=`{check.get('artifact_matches_declared')}` "
            f"current_matches_declared=`{check.get('current_matches_declared')}` "
            f"artifact_matches_current=`{check.get('artifact_matches_current')}`"
        )
    lines.extend(["", "## Operator Audit Status"])
    for key, value in (report.get("operator_audit_status") or {}).items():
        lines.append(f"- {key}: `{value}`")
    lines.extend(["", "## Nodes"])
    for item in report.get("nodes") or []:
        lines.extend(
            [
                f"### {item.get('order')}. {item.get('id')}",
                f"- title: {item.get('title')}",
                f"- depends_on: `{item.get('depends_on')}`",
                f"- mutation_policy: `{item.get('mutation_policy')}`",
                f"- command: `{item.get('command')}`",
            ]
        )
        for check in item.get("acceptance_checks") or []:
            lines.append(f"- acceptance: {check}")
        for hazard in item.get("hazards") or []:
            lines.append(f"- hazard: {hazard}")
        lines.append("")
    lines.extend(["## Non Goals"])
    for item in report.get("non_goals") or []:
        lines.append(f"- {item}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_audit_markdown(path: Path, audit: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-HR Release Operator Refresh DAG Audit",
        "",
        f"- schema: `{audit.get('schema')}`",
        f"- generated_at: `{audit.get('generated_at')}`",
        f"- ok: `{audit.get('ok')}`",
        f"- issue_count: `{audit.get('issue_count')}`",
        f"- report_only: `{audit.get('report_only')}`",
        f"- status_update_allowed: `{audit.get('status_update_allowed')}`",
        f"- db_writes: `{audit.get('db_writes')}`",
        f"- api_calls: `{audit.get('api_calls')}`",
        f"- approval_claim: `{audit.get('approval_claim')}`",
        "",
    ]
    if audit.get("issues"):
        lines.append("## Issues")
        for issue in audit.get("issues") or []:
            lines.append(f"- `{issue.get('code')}`: `{issue}`")
    else:
        lines.append("No release/operator refresh DAG issues found.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a report-only AI-HR release/operator refresh DAG."
    )
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--stamp")
    parser.add_argument("--quality-report", type=Path)
    parser.add_argument("--contract", type=Path)
    parser.add_argument("--demo-json", type=Path)
    parser.add_argument("--demo-html", type=Path)
    parser.add_argument("--dashboard-verification", type=Path)
    parser.add_argument("--release-readiness", type=Path)
    parser.add_argument("--agent-queue", type=Path)
    parser.add_argument("--queue-status", type=Path)
    parser.add_argument("--queue-run-dryrun", type=Path)
    parser.add_argument("--queue-run", type=Path)
    parser.add_argument("--qualification-coverage-plan", type=Path)
    parser.add_argument("--qualification-retry-hygiene", type=Path)
    parser.add_argument("--qualification-decision", type=Path)
    parser.add_argument("--qualification-decision-audit", type=Path)
    parser.add_argument("--provenance-proofset-log", type=Path)
    parser.add_argument("--transition-gap-json", type=Path)
    parser.add_argument("--transition-gap-csv", type=Path)
    parser.add_argument("--provenance-decision-sheet-json", type=Path)
    parser.add_argument("--provenance-decision-sheet-csv", type=Path)
    parser.add_argument("--provenance-decision-audit", type=Path)
    parser.add_argument("--transition-crosswalk-json", type=Path)
    parser.add_argument("--transition-crosswalk-csv", type=Path)
    parser.add_argument("--transition-crosswalk-audit", type=Path)
    parser.add_argument("--sprint-queue", type=Path)
    parser.add_argument("--sprint-queue-audit", type=Path)
    parser.add_argument("--next-actions", type=Path)
    parser.add_argument("--operator-packet-integrity-audit", type=Path)
    parser.add_argument("--handoff", type=Path)
    parser.add_argument("--lineage-audit", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--audit-out", type=Path)
    parser.add_argument("--audit-markdown-out", type=Path)
    parser.add_argument("--strict", action="store_true")
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    reports_dir = args.root / "reports"
    report = build_refresh_dag(
        quality_report=input_or_latest(args.quality_report, "aihr_quality_gates_with_transition_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        contract=input_or_latest(args.contract, "mcp_tool_contract_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        demo_json=input_or_latest(args.demo_json, "aihr_plan_demo_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_internal", "_alias", "_probe")),
        demo_html=input_or_latest(args.demo_html, "aihr_plan_demo_*.html", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        dashboard_verification=input_or_latest(args.dashboard_verification, "aihr_dashboard_surface_verification_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        release_readiness=input_or_latest(args.release_readiness, "aihr_release_readiness_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        agent_queue=input_or_latest(args.agent_queue, "aihr_agent_queue_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_status", "_run", "_dryrun", "_probe")),
        queue_status=input_or_latest(args.queue_status, "aihr_agent_queue_status_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        queue_run_dryrun=input_or_latest(args.queue_run_dryrun, "aihr_agent_queue_run_dryrun_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        queue_run=input_or_latest(args.queue_run, "aihr_agent_queue_run_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_dryrun", "_probe")),
        qualification_coverage_plan=input_or_latest(args.qualification_coverage_plan, "qualification_collection_coverage_plan_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        qualification_retry_hygiene=input_or_latest(args.qualification_retry_hygiene, "qualification_retry_hygiene_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        qualification_decision=input_or_latest(args.qualification_decision, "qualification_guarded_batch_operator_decision_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_audit", "_probe")),
        qualification_decision_audit=input_or_latest(args.qualification_decision_audit, "qualification_guarded_batch_operator_decision_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        provenance_proofset_log=input_or_latest(args.provenance_proofset_log, "command*provenance*proofset*.log", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        transition_gap_json=input_or_latest(args.transition_gap_json, "transition_trusted_scenario_provenance_gap_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        transition_gap_csv=input_or_latest(args.transition_gap_csv, "transition_trusted_scenario_provenance_gap_*.csv", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        provenance_decision_sheet_json=input_or_latest(args.provenance_decision_sheet_json, "human_review_provenance_reconfirmation_decision_sheet_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        provenance_decision_sheet_csv=input_or_latest(args.provenance_decision_sheet_csv, "human_review_provenance_reconfirmation_decision_sheet_*.csv", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        provenance_decision_audit=input_or_latest(args.provenance_decision_audit, "human_review_provenance_reconfirmation_decision_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        transition_crosswalk_json=input_or_latest(args.transition_crosswalk_json, "transition_provenance_operator_crosswalk_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_audit", "_probe")),
        transition_crosswalk_csv=input_or_latest(args.transition_crosswalk_csv, "transition_provenance_operator_crosswalk_*.csv", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        transition_crosswalk_audit=input_or_latest(args.transition_crosswalk_audit, "transition_provenance_operator_crosswalk_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        sprint_queue=input_or_latest(args.sprint_queue, "aihr_blocker_reduction_operator_sprint_queue_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp, exclude_substrings=("_audit", "_probe")),
        sprint_queue_audit=input_or_latest(args.sprint_queue_audit, "aihr_blocker_reduction_operator_sprint_queue_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        next_actions=input_or_latest(args.next_actions, "aihr_operator_next_actions_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        operator_packet_integrity_audit=input_or_latest(args.operator_packet_integrity_audit, "operator_review_packet_integrity_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        handoff=input_or_latest(args.handoff, "overnight_10h_operator_handoff_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        lineage_audit=input_or_latest(args.lineage_audit, "operator_report_lineage_sync_audit_*.json", reports_dir=reports_dir, root=args.root, stamp=args.stamp),
        stamp=args.stamp,
        root=args.root,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        write_markdown(args.markdown_out, report)
    audit = None
    if args.audit_out or args.audit_markdown_out or args.strict:
        audit = audit_refresh_dag(report, root=args.root)
        if args.audit_out:
            args.audit_out.parent.mkdir(parents=True, exist_ok=True)
            args.audit_out.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.audit_markdown_out:
            write_audit_markdown(args.audit_markdown_out, audit)
    print(
        json.dumps(
            {
                "ok": report.get("ok"),
                "schema": report.get("schema"),
                "node_count": len(report.get("nodes") or []),
                "out": str(args.out),
                "audit_ok": audit.get("ok") if audit else None,
                "audit_issue_count": audit.get("issue_count") if audit else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if args.strict and audit is not None and not audit.get("ok"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
