from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORTS = PROJECT_ROOT / "reports"
READONLY_REFRESH_REPORTS = REPORTS / "overnight_sessions" / "readonly_refresh"


def _dated_artifact_sort_key(path: Path) -> tuple[int, float]:
    for part in reversed(path.stem.split("_")):
        if len(part) == 8 and part.isdigit():
            return int(part), path.stat().st_mtime
    return 0, path.stat().st_mtime


def _latest_report_path(*patterns: str, fallback: Path) -> Path:
    candidate_dirs = (REPORTS, REPORTS / "overnight_sessions", READONLY_REFRESH_REPORTS)
    candidates = []
    seen: set[Path] = set()
    for directory in candidate_dirs:
        for pattern in patterns:
            for path in directory.glob(pattern):
                if path.is_file() and path not in seen:
                    seen.add(path)
                    candidates.append(path)
    if not candidates:
        return fallback
    return max(candidates, key=_dated_artifact_sort_key)


def _artifact_suffix(path: Path, *, prefixes: tuple[str, ...]) -> str:
    stem = path.stem
    for prefix in prefixes:
        if stem.startswith(prefix):
            return stem.removeprefix(prefix)
    return stem


def default_output_path(reconfirm_packet_path: Path, suffix: str) -> Path:
    packet_suffix = _artifact_suffix(
        reconfirm_packet_path,
        prefixes=(
            "aihr_human_review_provenance_reconfirmation_packet_",
            "human_review_provenance_reconfirmation_packet_",
        ),
    )
    return REPORTS / f"human_review_safe_ops_checkpoint_{packet_suffix}{suffix}"


DEFAULT_SQF_READINESS = _latest_report_path(
    "sqf_human_review_readiness_hr_accounting_20*.json",
    fallback=REPORTS / "sqf_human_review_readiness_hr_accounting_20260620.json",
)
DEFAULT_SQF_DECISION_AUDIT = _latest_report_path(
    "sqf_report_claim_decision_audit_hr_accounting_20*.json",
    fallback=REPORTS / "sqf_report_claim_decision_audit_hr_accounting_20260620.json",
)
DEFAULT_SQF_GUARDED_PLAN = _latest_report_path(
    "sqf_guarded_import_plan_hr_accounting_20*.json",
    fallback=REPORTS / "sqf_guarded_import_plan_hr_accounting_20260620.json",
)
DEFAULT_PROVENANCE_AUDIT = _latest_report_path(
    "aihr_human_review_provenance_surface_audit_20*.json",
    "human_review_provenance_surface_audit_20*.json",
    fallback=REPORTS / "aihr_human_review_provenance_surface_audit_20260620.json",
)
DEFAULT_RECONFIRM_PACKET = _latest_report_path(
    "aihr_human_review_provenance_reconfirmation_packet_20*.json",
    "human_review_provenance_reconfirmation_packet_20*.json",
    fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_packet_20260620.json",
)
DEFAULT_RECONFIRM_DECISION_SHEET = _latest_report_path(
    "aihr_human_review_provenance_reconfirmation_decision_sheet_20*.json",
    "human_review_provenance_reconfirmation_decision_sheet_20*.json",
    fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_decision_sheet_20260620.json",
)
DEFAULT_RECONFIRM_DECISION_AUDIT = _latest_report_path(
    "aihr_human_review_provenance_reconfirmation_decision_audit_20*.json",
    "human_review_provenance_reconfirmation_decision_audit_20*.json",
    fallback=REPORTS / "aihr_human_review_provenance_reconfirmation_decision_audit_20260620.json",
)
DEFAULT_RECENT_STATUS_AUDIT = _latest_report_path(
    "review_status_recent_write_audit_20*.json",
    "recent_review_status_write_audit_20*.json",
    fallback=REPORTS / "review_status_recent_write_audit_20260620.json",
)
EXPECTED_RECENT_STATUS_AUDIT_SCHEMA = "aihr_recent_review_status_write_audit_v1"
EXPECTED_TRUSTED_STATUS_VALUES = {"accepted", "human_reviewed", "reviewed"}
EXPECTED_MONITORED_NON_TRUSTED_STATUS_VALUES = {"candidate_auto"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def content_sha256(payload: bytes | str) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"ok": False, "missing": True, "path": str(path)}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(path)}
    if isinstance(payload, dict):
        return payload
    return {"ok": False, "error": "json_root_not_object", "path": str(path)}


def rel(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def nested_bool(payload: dict[str, Any], *keys: str, default: bool = False) -> bool:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    if isinstance(current, bool):
        return current
    return default


def summarize_sqf(readiness: dict[str, Any], decision_audit: dict[str, Any], guarded_plan: dict[str, Any]) -> dict[str, Any]:
    decision_summary = decision_audit.get("summary") if isinstance(decision_audit.get("summary"), dict) else {}
    if not decision_summary:
        rows = decision_audit.get("rows") if isinstance(decision_audit.get("rows"), list) else []
        decision_summary = {
            "row_count": len(rows),
            "pending_blank_count": sum(1 for row in rows if isinstance(row, dict) and row.get("decision") in {None, "", "blank"}),
            "completed_decision_count": sum(1 for row in rows if isinstance(row, dict) and row.get("decision") not in {None, "", "blank"}),
            "import_ready_count": sum(1 for row in rows if isinstance(row, dict) and row.get("guarded_import_candidate")),
        }
    guarded_summary = guarded_plan.get("summary") if isinstance(guarded_plan.get("summary"), dict) else {}
    readiness_summary = readiness.get("summaries") if isinstance(readiness.get("summaries"), dict) else {}
    priority = readiness_summary.get("priority") if isinstance(readiness_summary.get("priority"), dict) else {}
    claim_queue = readiness_summary.get("claim_queue") if isinstance(readiness_summary.get("claim_queue"), dict) else {}
    human_review_summary = (
        readiness.get("human_review_summary")
        if isinstance(readiness.get("human_review_summary"), dict)
        else {}
    )
    return {
        "ok": bool(readiness.get("ok")) and bool(decision_audit.get("ok")) and bool(guarded_plan.get("ok")),
        "source_readiness_ok": bool(readiness.get("ok")),
        "source_decision_audit_ok": bool(decision_audit.get("ok")),
        "source_guarded_plan_ok": bool(guarded_plan.get("ok")),
        "allowed_use": readiness.get("allowed_use"),
        "status": readiness.get("status"),
        "approval_ready": bool(readiness.get("approval_ready")),
        "db_writes": bool(readiness.get("db_writes")),
        "status_update_allowed": bool(readiness.get("status_update_allowed")),
        "used_for_scoring": bool(readiness.get("used_for_scoring")),
        "claim_count": int(
            claim_queue.get("claim_count")
            or human_review_summary.get("claim_count")
            or decision_summary.get("row_count")
            or 0
        ),
        "p0_count": int(
            (priority.get("priority_counts") or {}).get("P0")
            or human_review_summary.get("p0_count")
            or 0
        ),
        "pending_decision_count": int(
            decision_summary.get("pending_blank_count")
            or guarded_summary.get("pending_count")
            or 0
        ),
        "completed_decision_count": int(
            decision_summary.get("completed_decision_count")
            or guarded_summary.get("completed_decision_count")
            or 0
        ),
        "guarded_import_candidate_count": int(
            decision_summary.get("guarded_import_candidate_count")
            or decision_summary.get("import_ready_count")
            or 0
        ),
        "planned_db_writes": int(guarded_plan.get("planned_db_writes") or 0),
        "execution_allowed": bool(guarded_plan.get("execution_allowed")),
        "safe_for_reviewer_evidence": (
            bool(readiness.get("ok"))
            and bool(decision_audit.get("ok"))
            and bool(guarded_plan.get("ok"))
            and readiness.get("allowed_use") == "supplementary_review_context_only"
            and not bool(readiness.get("db_writes"))
            and not bool(readiness.get("status_update_allowed"))
            and not bool(readiness.get("used_for_scoring"))
            and not bool(guarded_plan.get("execution_allowed"))
        ),
    }


def summarize_provenance(audit: dict[str, Any], reconfirm_packet: dict[str, Any]) -> dict[str, Any]:
    summary = audit.get("summary") if isinstance(audit.get("summary"), dict) else {}
    rows_without_packet_backed = int(summary.get("rows_without_packet_backed_provenance") or 0)
    provenance_gap_present = bool(summary.get("provenance_gap_present")) or rows_without_packet_backed > 0
    return {
        "ok": bool(audit.get("ok")),
        "surface_count": int(summary.get("surface_count") or 0),
        "legacy_trusted_status_rows_pending_reconfirmation": int(summary.get("row_count") or 0),
        "rows_packet_backed": int(summary.get("rows_packet_backed") or 0),
        "rows_without_packet_backed_provenance": rows_without_packet_backed,
        "provenance_gap_present": provenance_gap_present,
        "approval_ready": not provenance_gap_present,
        "db_writes": bool(summary.get("db_writes")),
        "reconfirmation_packet": {
            "ok": bool(reconfirm_packet.get("ok")),
            "schema": reconfirm_packet.get("schema"),
            "row_count": int(reconfirm_packet.get("row_count") or len(reconfirm_packet.get("rows") or [])),
            "db_writes": bool(reconfirm_packet.get("db_writes")),
            "status_update_allowed": bool(reconfirm_packet.get("status_update_allowed")),
        },
    }


def summarize_reconfirm_decision_sheet(decision_sheet: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(decision_sheet.get("ok")),
        "schema": decision_sheet.get("schema"),
        "source_packet": decision_sheet.get("source_packet"),
        "source_packet_sha256": decision_sheet.get("source_packet_sha256"),
        "row_count": int(decision_sheet.get("row_count") or 0),
        "blank_decision_count": int(decision_sheet.get("blank_decision_count") or 0),
        "completed_decision_count": int(decision_sheet.get("completed_decision_count") or 0),
        "db_writes": bool(decision_sheet.get("db_writes")),
        "status_update_allowed": bool(decision_sheet.get("status_update_allowed")),
        "approval_claim": bool(decision_sheet.get("approval_claim")),
        "human_decision_required": bool(decision_sheet.get("human_decision_required")),
    }


def summarize_reconfirm_decision_audit(decision_audit: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": bool(decision_audit.get("ok")),
        "schema": decision_audit.get("schema"),
        "csv": decision_audit.get("csv"),
        "source_packet": decision_audit.get("source_packet"),
        "source_packet_sha256": decision_audit.get("source_packet_sha256"),
        "row_count": int(decision_audit.get("row_count") or 0),
        "source_packet_row_count": int(decision_audit.get("source_packet_row_count") or 0),
        "pending_decision_count": int(decision_audit.get("pending_decision_count") or 0),
        "completed_decision_count": int(decision_audit.get("completed_decision_count") or 0),
        "invalid_decision_count": int(decision_audit.get("invalid_decision_count") or 0),
        "missing_required_field_row_count": int(
            decision_audit.get("missing_required_field_row_count") or 0
        ),
        "source_mismatch_count": int(decision_audit.get("source_mismatch_count") or 0),
        "source_identity_mismatch_count": int(
            decision_audit.get("source_identity_mismatch_count") or 0
        ),
        "source_decision_packet_not_found_count": int(
            decision_audit.get("source_decision_packet_not_found_count") or 0
        ),
        "invalid_evidence_refs_json_count": int(
            decision_audit.get("invalid_evidence_refs_json_count") or 0
        ),
        "unsafe_flag_count": int(decision_audit.get("unsafe_flag_count") or 0),
        "duplicate_csv_key_count": int(decision_audit.get("duplicate_csv_key_count") or 0),
        "missing_packet_row_count": int(decision_audit.get("missing_packet_row_count") or 0),
        "unexpected_csv_row_count": int(decision_audit.get("unexpected_csv_row_count") or 0),
        "missing_csv_columns": list(decision_audit.get("missing_csv_columns") or []),
        "action_eligible_count": int(decision_audit.get("action_eligible_count") or 0),
        "db_writes": bool(decision_audit.get("db_writes")),
        "status_update_allowed": bool(decision_audit.get("status_update_allowed")),
        "approval_claim": bool(decision_audit.get("approval_claim")),
        "guarded_apply_ready": bool(decision_audit.get("guarded_apply_ready")),
    }


def summarize_recent_status_audit(audit: dict[str, Any]) -> dict[str, Any]:
    trusted_values = set(str(value) for value in (audit.get("trusted_status_values") or []))
    monitored_non_trusted_values = set(
        str(value) for value in (audit.get("monitored_non_trusted_status_values") or [])
    )
    db_writes = audit.get("db_writes")
    status_update_allowed = audit.get("status_update_allowed")
    approval_claim = audit.get("approval_claim")
    policy_flags_present = (
        db_writes is not None
        and status_update_allowed is not None
        and approval_claim is not None
    )
    policy_flags_ok = (
        db_writes is False
        and status_update_allowed is False
        and approval_claim is False
    )
    return {
        "ok": bool(audit.get("ok")) and policy_flags_ok,
        "source_ok": bool(audit.get("ok")),
        "schema": audit.get("schema"),
        "schema_ok": audit.get("schema") == EXPECTED_RECENT_STATUS_AUDIT_SCHEMA,
        "generated_at": audit.get("generated_at"),
        "cutoff": audit.get("cutoff"),
        "read_only": bool(audit.get("read_only")),
        "review_audit_log_exists": bool(audit.get("review_audit_log_exists")),
        "review_audit_log_has_created_at": bool(audit.get("review_audit_log_has_created_at")),
        "recent_trusted_status_table_hit_count": int(audit.get("recent_trusted_status_table_hit_count") or 0),
        "recent_trusted_audit_log_count": int(audit.get("recent_trusted_audit_log_count") or 0),
        "recent_monitored_non_trusted_status_table_hit_count": int(
            audit.get("recent_monitored_non_trusted_status_table_hit_count") or 0
        ),
        "recent_monitored_non_trusted_audit_log_count": int(
            audit.get("recent_monitored_non_trusted_audit_log_count") or 0
        ),
        "recent_audit_log_total_count": int(audit.get("recent_audit_log_total_count") or 0),
        "recent_unverifiable_generic_timestamp_count": int(
            audit.get("recent_unverifiable_generic_timestamp_count") or 0
        ),
        "recent_monitored_non_trusted_unverifiable_generic_timestamp_count": int(
            audit.get("recent_monitored_non_trusted_unverifiable_generic_timestamp_count") or 0
        ),
        "unverifiable_no_timestamp_table_count": int(audit.get("unverifiable_no_timestamp_table_count") or 0),
        "monitored_non_trusted_unverifiable_no_timestamp_table_count": int(
            audit.get("monitored_non_trusted_unverifiable_no_timestamp_table_count") or 0
        ),
        "invalid_timestamp_table_row_count": int(audit.get("invalid_timestamp_table_row_count") or 0),
        "trusted_status_values": sorted(trusted_values),
        "trusted_status_values_complete": trusted_values == EXPECTED_TRUSTED_STATUS_VALUES,
        "monitored_non_trusted_status_values": sorted(monitored_non_trusted_values),
        "monitored_non_trusted_status_values_complete": (
            monitored_non_trusted_values == EXPECTED_MONITORED_NON_TRUSTED_STATUS_VALUES
        ),
        "policy_flags_present": policy_flags_present,
        "policy_flags_ok": policy_flags_ok,
        "db_writes": bool(db_writes),
        "status_update_allowed": bool(status_update_allowed),
        "approval_claim": bool(approval_claim),
    }


def command_policy() -> dict[str, Any]:
    return {
        "safe_report_only_commands": [
            {
                "command": "python scripts\\ncs_harness.py export-sqf-report-review-seedpack",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Build reviewer seedpack from existing SQF/NCS evidence.",
            },
            {
                "command": "python scripts\\ncs_harness.py export-sqf-report-claim-candidates",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Export candidate SQF-NCS review claims.",
            },
            {
                "command": "python scripts\\ncs_harness.py export-sqf-report-claim-decision-sheet",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Create blank reviewer decision sheet.",
            },
            {
                "command": "python scripts\\ncs_harness.py export-sqf-review-priority",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Prioritize review claims without approval.",
            },
            {
                "command": "python scripts\\ncs_harness.py audit-sqf-report-claim-decisions",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Validate filled reviewer annotations before any import planning.",
            },
            {
                "command": "python scripts\\ncs_harness.py summarize-sqf-human-review-readiness",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Summarize readiness and artifact leakage checks.",
            },
            {
                "command": "python scripts\\ncs_harness.py plan-sqf-guarded-import",
                "db_writes": False,
                "status_update_allowed": False,
                "purpose": "Dry-run import planning; emits no executable status update.",
            },
        ],
        "guarded_preprocessing_commands": [
            {
                "command": "python scripts\\ncs_harness.py collect-sqf-library",
                "db_writes_possible": True,
                "status_update_allowed": False,
                "purpose": "Refresh SQF source metadata and downloaded source files.",
            },
            {
                "command": "python scripts\\ncs_harness.py build-sqf-sqlite-model",
                "db_writes_possible": True,
                "status_update_allowed": False,
                "purpose": "Rebuild derived SQF ontology tables from preserved sources.",
            },
            {
                "command": "python scripts\\ncs_harness.py preprocess-sqf-documents",
                "db_writes_possible": True,
                "status_update_allowed": False,
                "purpose": "Extract SQF document pages and chunks from local source files.",
            },
            {
                "command": "python scripts\\ncs_harness.py build-sqf-precision-matches",
                "db_writes_possible": True,
                "status_update_allowed": False,
                "purpose": "Refresh SQF document-to-job-level evidence matches.",
            },
        ],
        "human_decision_only_commands": [
            {
                "command": "python scripts\\ncs_harness.py annotate-sqf-report-claim-decision",
                "db_writes": False,
                "status_update_allowed": False,
                "required_input": "Explicit reviewer decision, reviewer id, rationale, and evidence references.",
            }
        ],
        "prohibited_without_explicit_human_authorization": [
            "Any command or API path that auto-sets human_reviewed, reviewed, accepted, approved, or trusted.",
            "Any guarded import that maps a blank or incomplete CSV row into a DB status update.",
            "Any active recommendation scoring change that treats SQF report evidence as a score source.",
            "Any legacy SQF API collection while the NCS006 full-scope element collector is already writing the DB.",
        ],
    }


def build_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    readiness = load_json(args.sqf_readiness)
    decision_audit = load_json(args.sqf_decision_audit)
    guarded_plan = load_json(args.sqf_guarded_plan)
    provenance_audit = load_json(args.provenance_audit)
    reconfirm_packet = load_json(args.reconfirm_packet)
    reconfirm_decision_sheet = load_json(args.reconfirm_decision_sheet)
    reconfirm_decision_audit_path = getattr(
        args,
        "reconfirm_decision_audit",
        DEFAULT_RECONFIRM_DECISION_AUDIT,
    )
    reconfirm_decision_audit = load_json(reconfirm_decision_audit_path)
    recent_status_audit_path = getattr(args, "recent_status_audit", DEFAULT_RECENT_STATUS_AUDIT)
    recent_status_audit = load_json(recent_status_audit_path)
    sqf = summarize_sqf(readiness, decision_audit, guarded_plan)
    provenance = summarize_provenance(provenance_audit, reconfirm_packet)
    decision_sheet = summarize_reconfirm_decision_sheet(reconfirm_decision_sheet)
    decision_audit_summary = summarize_reconfirm_decision_audit(reconfirm_decision_audit)
    recent_status = summarize_recent_status_audit(recent_status_audit)
    expected_reconfirm_packet_ref = rel(args.reconfirm_packet)
    actual_reconfirm_packet_sha256 = (
        content_sha256(args.reconfirm_packet.read_bytes())
        if args.reconfirm_packet.exists()
        else ""
    )
    packet_row_count = provenance["reconfirmation_packet"]["row_count"]
    decision_sheet["source_packet_binding_ok"] = (
        str(decision_sheet.get("source_packet") or "").strip()
        == expected_reconfirm_packet_ref
    )
    decision_sheet["source_packet_hash_ok"] = (
        bool(actual_reconfirm_packet_sha256)
        and str(decision_sheet.get("source_packet_sha256") or "").strip()
        == actual_reconfirm_packet_sha256
    )
    decision_audit_summary["source_packet_binding_ok"] = (
        str(decision_audit_summary.get("source_packet") or "").strip()
        == expected_reconfirm_packet_ref
    )
    decision_audit_summary["source_packet_hash_ok"] = (
        bool(actual_reconfirm_packet_sha256)
        and str(decision_audit_summary.get("source_packet_sha256") or "").strip()
        == actual_reconfirm_packet_sha256
    )
    reconfirm_decision_audit_ok = (
        decision_audit_summary["ok"]
        and decision_sheet["source_packet_hash_ok"]
        and decision_audit_summary["source_packet_binding_ok"]
        and decision_audit_summary["source_packet_hash_ok"]
        and decision_audit_summary["row_count"] == decision_sheet["row_count"]
        and decision_audit_summary["row_count"] == decision_audit_summary["source_packet_row_count"]
        and decision_audit_summary["source_packet_row_count"] == packet_row_count
        and decision_audit_summary["source_mismatch_count"] == 0
        and decision_audit_summary["source_identity_mismatch_count"] == 0
        and decision_audit_summary["source_decision_packet_not_found_count"] == 0
        and decision_audit_summary["invalid_evidence_refs_json_count"] == 0
        and decision_audit_summary["unsafe_flag_count"] == 0
        and decision_audit_summary["duplicate_csv_key_count"] == 0
        and decision_audit_summary["missing_packet_row_count"] == 0
        and decision_audit_summary["unexpected_csv_row_count"] == 0
        and not decision_audit_summary["missing_csv_columns"]
        and not decision_audit_summary["db_writes"]
        and not decision_audit_summary["status_update_allowed"]
        and not decision_audit_summary["approval_claim"]
        and not decision_audit_summary["guarded_apply_ready"]
    )
    decision_audit_summary["checkpoint_contract_ok"] = reconfirm_decision_audit_ok
    unresolved_provenance_gap = (
        provenance["provenance_gap_present"]
        or provenance["rows_without_packet_backed_provenance"] > 0
        or decision_sheet["blank_decision_count"] > 0
    )
    return {
        "schema": "human_review_safe_ops_checkpoint_v1",
        "generated_at": now_iso(),
        "ok": (
            sqf["safe_for_reviewer_evidence"]
            and provenance["ok"]
            and provenance["reconfirmation_packet"]["ok"]
            and not unresolved_provenance_gap
            and not provenance["db_writes"]
            and not provenance["reconfirmation_packet"]["db_writes"]
            and not provenance["reconfirmation_packet"]["status_update_allowed"]
            and decision_sheet["ok"]
            and decision_sheet["source_packet_binding_ok"]
            and not decision_sheet["db_writes"]
            and not decision_sheet["status_update_allowed"]
            and not decision_sheet["approval_claim"]
            and decision_sheet["human_decision_required"]
            and reconfirm_decision_audit_ok
            and recent_status["ok"]
            and recent_status["source_ok"]
            and recent_status["schema_ok"]
            and recent_status["trusted_status_values_complete"]
            and recent_status["monitored_non_trusted_status_values_complete"]
            and recent_status["policy_flags_present"]
            and recent_status["policy_flags_ok"]
            and not recent_status["db_writes"]
            and not recent_status["status_update_allowed"]
            and not recent_status["approval_claim"]
            and recent_status["read_only"]
            and recent_status["review_audit_log_exists"]
            and recent_status["review_audit_log_has_created_at"]
            and recent_status["recent_trusted_status_table_hit_count"] == 0
            and recent_status["recent_trusted_audit_log_count"] == 0
            and recent_status["recent_monitored_non_trusted_status_table_hit_count"] == 0
            and recent_status["recent_monitored_non_trusted_audit_log_count"] == 0
            and recent_status["recent_unverifiable_generic_timestamp_count"] == 0
            and recent_status["recent_monitored_non_trusted_unverifiable_generic_timestamp_count"] == 0
            and recent_status["unverifiable_no_timestamp_table_count"] == 0
            and recent_status["monitored_non_trusted_unverifiable_no_timestamp_table_count"] == 0
            and recent_status["invalid_timestamp_table_row_count"] == 0
        ),
        "sqf_review": sqf,
        "legacy_trusted_status_provenance": provenance,
        "legacy_trusted_status_reconfirmation_decision_sheet": decision_sheet,
        "legacy_trusted_status_reconfirmation_decision_audit": decision_audit_summary,
        "recent_trusted_status_write_audit": recent_status,
        "unresolved_provenance_gap": unresolved_provenance_gap,
        "recommended_order": [
            "Review AI-HR provenance reconfirmation packet first for legacy trusted rows without packet-backed provenance.",
            "Use the reconfirmation decision sheet to record explicit decisions; blank rows are not import-ready.",
            "Check the recent trusted status write audit after report-only automation runs.",
            "Then review SQF P0 shortlist as supplementary evidence only.",
            "Do not run any guarded import until explicit human decisions include reviewer id, source packet, rationale, and evidence refs.",
        ],
        "reviewer_safe_artifacts": [
            rel(args.provenance_audit.with_suffix(".md")),
            rel(args.reconfirm_packet.with_suffix(".md")),
            rel(args.reconfirm_packet.with_suffix(".html")),
            rel(args.reconfirm_decision_sheet.with_suffix(".csv")),
            rel(args.reconfirm_decision_sheet.with_suffix(".html")),
            rel(args.reconfirm_decision_sheet.with_suffix(".md")),
            rel(reconfirm_decision_audit_path.with_suffix(".md")),
            rel(args.sqf_readiness.with_suffix(".md")),
            rel(args.sqf_decision_audit.with_suffix(".md")),
            rel(args.sqf_guarded_plan.with_suffix(".md")),
        ],
        "prohibited_actions": [
            "Do not auto-set human_reviewed, reviewed, or accepted.",
            "Do not treat SQF evidence as active recommendation scoring evidence.",
            "Do not write DB updates from blank or incomplete decision-sheet rows.",
            "Do not share JSON artifacts containing local absolute DB paths as broad reviewer handouts when markdown/html variants exist.",
        ],
        "command_policy": command_policy(),
        "source_artifacts": {
            "sqf_readiness": rel(args.sqf_readiness),
            "sqf_decision_audit": rel(args.sqf_decision_audit),
            "sqf_guarded_plan": rel(args.sqf_guarded_plan),
            "provenance_audit": rel(args.provenance_audit),
            "reconfirm_packet": rel(args.reconfirm_packet),
            "reconfirm_decision_sheet": rel(args.reconfirm_decision_sheet),
            "reconfirm_decision_audit": rel(reconfirm_decision_audit_path),
            "recent_status_audit": rel(recent_status_audit_path),
        },
        "policy": {
            "read_only_checkpoint": True,
            "db_writes": False,
            "status_updates": False,
            "secrets_included": False,
        },
    }


def write_markdown(path: Path, checkpoint: dict[str, Any]) -> None:
    sqf = checkpoint["sqf_review"]
    provenance = checkpoint["legacy_trusted_status_provenance"]
    decision_sheet = checkpoint["legacy_trusted_status_reconfirmation_decision_sheet"]
    decision_audit = checkpoint["legacy_trusted_status_reconfirmation_decision_audit"]
    recent_status = checkpoint["recent_trusted_status_write_audit"]
    lines = [
        "# Human Review Safe Ops Checkpoint",
        "",
        f"- Generated at: `{checkpoint['generated_at']}`",
        f"- Overall ok for report-only review operations: `{checkpoint['ok']}`",
        "",
        "## SQF Review Boundary",
        "",
        f"- safe_for_reviewer_evidence: `{sqf['safe_for_reviewer_evidence']}`",
        f"- allowed_use: `{sqf.get('allowed_use')}`",
        f"- approval_ready: `{sqf['approval_ready']}`",
        f"- db_writes: `{sqf['db_writes']}`",
        f"- status_update_allowed: `{sqf['status_update_allowed']}`",
        f"- used_for_scoring: `{sqf['used_for_scoring']}`",
        f"- claim_count: `{sqf['claim_count']}`",
        f"- P0 count: `{sqf['p0_count']}`",
        f"- pending decisions: `{sqf['pending_decision_count']}`",
        f"- guarded import candidates: `{sqf['guarded_import_candidate_count']}`",
        f"- planned DB writes: `{sqf['planned_db_writes']}`",
        "",
        "## Legacy Trusted Status Provenance",
        "",
        (
            "- legacy_trusted_status_rows_pending_reconfirmation: "
            f"`{provenance['legacy_trusted_status_rows_pending_reconfirmation']}`"
        ),
        f"- packet_backed_trusted_status_rows: `{provenance['rows_packet_backed']}`",
        f"- rows_without_packet_backed_provenance: `{provenance['rows_without_packet_backed_provenance']}`",
        f"- provenance_gap_present: `{provenance['provenance_gap_present']}`",
        "",
        "## Reconfirmation Decision Sheet",
        "",
        f"- row_count: `{decision_sheet['row_count']}`",
        f"- blank_decision_count: `{decision_sheet['blank_decision_count']}`",
        f"- completed_decision_count: `{decision_sheet['completed_decision_count']}`",
        f"- source_packet_binding_ok: `{decision_sheet['source_packet_binding_ok']}`",
        f"- source_packet_hash_ok: `{decision_sheet['source_packet_hash_ok']}`",
        f"- db_writes: `{decision_sheet['db_writes']}`",
        f"- status_update_allowed: `{decision_sheet['status_update_allowed']}`",
        f"- approval_claim: `{decision_sheet['approval_claim']}`",
        f"- human_decision_required: `{decision_sheet['human_decision_required']}`",
        "",
        "## Reconfirmation Decision Audit",
        "",
        f"- ok: `{decision_audit['ok']}`",
        f"- checkpoint_contract_ok: `{decision_audit['checkpoint_contract_ok']}`",
        f"- source_packet_binding_ok: `{decision_audit['source_packet_binding_ok']}`",
        f"- source_packet_hash_ok: `{decision_audit['source_packet_hash_ok']}`",
        f"- row_count: `{decision_audit['row_count']}`",
        f"- source_packet_row_count: `{decision_audit['source_packet_row_count']}`",
        f"- source_mismatch_count: `{decision_audit['source_mismatch_count']}`",
        f"- source_identity_mismatch_count: `{decision_audit['source_identity_mismatch_count']}`",
        f"- unsafe_flag_count: `{decision_audit['unsafe_flag_count']}`",
        f"- duplicate_csv_key_count: `{decision_audit['duplicate_csv_key_count']}`",
        f"- missing_packet_row_count: `{decision_audit['missing_packet_row_count']}`",
        f"- unexpected_csv_row_count: `{decision_audit['unexpected_csv_row_count']}`",
        f"- action_eligible_count: `{decision_audit['action_eligible_count']}`",
        f"- db_writes: `{decision_audit['db_writes']}`",
        f"- status_update_allowed: `{decision_audit['status_update_allowed']}`",
        f"- approval_claim: `{decision_audit['approval_claim']}`",
        f"- guarded_apply_ready: `{decision_audit['guarded_apply_ready']}`",
        "",
        "## Recent Trusted Status Write Audit",
        "",
        f"- ok: `{recent_status['ok']}`",
        f"- source_ok: `{recent_status['source_ok']}`",
        f"- schema_ok: `{recent_status['schema_ok']}`",
        f"- trusted_status_values_complete: `{recent_status['trusted_status_values_complete']}`",
        "- monitored_non_trusted_status_values_complete: "
        f"`{recent_status['monitored_non_trusted_status_values_complete']}`",
        f"- policy_flags_present: `{recent_status['policy_flags_present']}`",
        f"- policy_flags_ok: `{recent_status['policy_flags_ok']}`",
        f"- db_writes: `{recent_status['db_writes']}`",
        f"- status_update_allowed: `{recent_status['status_update_allowed']}`",
        f"- approval_claim: `{recent_status['approval_claim']}`",
        f"- cutoff: `{recent_status.get('cutoff')}`",
        f"- read_only: `{recent_status['read_only']}`",
        f"- review_audit_log_exists: `{recent_status['review_audit_log_exists']}`",
        f"- review_audit_log_has_created_at: `{recent_status['review_audit_log_has_created_at']}`",
        f"- recent_trusted_status_table_hit_count: `{recent_status['recent_trusted_status_table_hit_count']}`",
        f"- recent_trusted_audit_log_count: `{recent_status['recent_trusted_audit_log_count']}`",
        "- recent_monitored_non_trusted_status_table_hit_count: "
        f"`{recent_status['recent_monitored_non_trusted_status_table_hit_count']}`",
        "- recent_monitored_non_trusted_audit_log_count: "
        f"`{recent_status['recent_monitored_non_trusted_audit_log_count']}`",
        f"- recent_audit_log_total_count: `{recent_status['recent_audit_log_total_count']}`",
        f"- recent_unverifiable_generic_timestamp_count: `{recent_status['recent_unverifiable_generic_timestamp_count']}`",
        "- recent_monitored_non_trusted_unverifiable_generic_timestamp_count: "
        f"`{recent_status['recent_monitored_non_trusted_unverifiable_generic_timestamp_count']}`",
        f"- unverifiable_no_timestamp_table_count: `{recent_status['unverifiable_no_timestamp_table_count']}`",
        "- monitored_non_trusted_unverifiable_no_timestamp_table_count: "
        f"`{recent_status['monitored_non_trusted_unverifiable_no_timestamp_table_count']}`",
        f"- invalid_timestamp_table_row_count: `{recent_status['invalid_timestamp_table_row_count']}`",
        "",
        "## Recommended Order",
        "",
    ]
    for item in checkpoint["recommended_order"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Reviewer-Safe Artifacts", ""])
    for artifact in checkpoint["reviewer_safe_artifacts"]:
        lines.append(f"- `{artifact}`")
    lines.extend(["", "## Prohibited Actions", ""])
    for item in checkpoint["prohibited_actions"]:
        lines.append(f"- {item}")
    lines.extend(["", "## Command Policy", ""])
    policy = checkpoint["command_policy"]
    lines.append("### Safe Report-Only Commands")
    lines.append("")
    for item in policy["safe_report_only_commands"]:
        lines.append(f"- `{item['command']}`: {item['purpose']}")
    lines.extend(["", "### Guarded Preprocessing Commands", ""])
    for item in policy["guarded_preprocessing_commands"]:
        lines.append(f"- `{item['command']}`: {item['purpose']}")
    lines.extend(["", "### Human-Decision-Only Commands", ""])
    for item in policy["human_decision_only_commands"]:
        lines.append(f"- `{item['command']}`: requires {item['required_input']}")
    lines.extend(["", "### Prohibited Without Explicit Human Authorization", ""])
    for item in policy["prohibited_without_explicit_human_authorization"]:
        lines.append(f"- {item}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a report-only human-review operations checkpoint.")
    parser.add_argument("--sqf-readiness", type=Path, default=DEFAULT_SQF_READINESS)
    parser.add_argument("--sqf-decision-audit", type=Path, default=DEFAULT_SQF_DECISION_AUDIT)
    parser.add_argument("--sqf-guarded-plan", type=Path, default=DEFAULT_SQF_GUARDED_PLAN)
    parser.add_argument("--provenance-audit", type=Path, default=DEFAULT_PROVENANCE_AUDIT)
    parser.add_argument("--reconfirm-packet", type=Path, default=DEFAULT_RECONFIRM_PACKET)
    parser.add_argument("--reconfirm-decision-sheet", type=Path, default=DEFAULT_RECONFIRM_DECISION_SHEET)
    parser.add_argument("--reconfirm-decision-audit", type=Path, default=DEFAULT_RECONFIRM_DECISION_AUDIT)
    parser.add_argument("--recent-status-audit", type=Path, default=DEFAULT_RECENT_STATUS_AUDIT)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()

    checkpoint = build_checkpoint(args)
    args.out = args.out or default_output_path(args.reconfirm_packet, ".json")
    args.markdown_out = args.markdown_out or default_output_path(args.reconfirm_packet, ".md")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(checkpoint, ensure_ascii=False, indent=2), encoding="utf-8")
    write_markdown(args.markdown_out, checkpoint)
    print(
        json.dumps(
            {
                "out": rel(args.out),
                "markdown_out": rel(args.markdown_out),
                "ok": checkpoint["ok"],
                "sqf": checkpoint["sqf_review"],
                "legacy_trusted_status_provenance": checkpoint["legacy_trusted_status_provenance"],
                "legacy_trusted_status_reconfirmation_decision_audit": checkpoint[
                    "legacy_trusted_status_reconfirmation_decision_audit"
                ],
                "recent_trusted_status_write_audit": checkpoint["recent_trusted_status_write_audit"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
